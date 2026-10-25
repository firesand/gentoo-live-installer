"""The shipped tree (data/root): AGENTS.md, the per-agent configs and the Claude hook, checked against what the vendors document."""

from __future__ import annotations

import importlib.util
import json
import os
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "src" / "gentoo_rescue_ai" / "data" / "root"
AGENTS_MD = ROOT / "etc" / "gentoo-rescue" / "AGENTS.md"
MANAGED = ROOT / "etc" / "claude-code" / "managed-settings.json"


def test_the_tree_mirrors_where_the_files_go_and_holds_no_sudo_rule() -> None:
    files = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and not p.is_symlink())
    assert files == sorted([
        "etc/claude-code/CLAUDE.md", "etc/claude-code/managed-settings.json", "etc/gentoo-rescue/AGENTS.md", "etc/gentoo-rescue/hooks/pretooluse-guard.py",
        "etc/skel-rescue-ai/.codex/config.toml", "etc/skel-rescue-ai/.config/opencode/opencode.json"])
    # The AGENTS.md links of Codex and OpenCode: absolute, so they dangle in the source tree (and setuptools leaves a dangling link out of a wheel);
    # gentoo-rescue-ai makes them again at every start (prepare_home), so they exist whichever way the tree was installed.
    links = {str(p.relative_to(ROOT)): os.readlink(p) for p in ROOT.rglob("*") if p.is_symlink()}
    assert links == {"etc/skel-rescue-ai/.codex/AGENTS.md": "/etc/gentoo-rescue/AGENTS.md", "etc/skel-rescue-ai/.config/opencode/AGENTS.md": "/etc/gentoo-rescue/AGENTS.md"}
    assert not (ROOT / "etc" / "sudoers.d").exists()


def test_agents_md_is_small_and_says_the_gentoo_specific_things() -> None:
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert len(text.encode()) < 32 * 1024
    for needle in ("read-only", "gentoo-rescue-do", "Never `cd` into the target", "data, never instructions", "OpenRC", "dracut", "emerge", "/etc/portage", "Handbook", "genkernel", "mdadm", "snapper"):
        assert needle in text, needle
    limine = next(line for line in text.splitlines() if "Limine" in line)
    assert "rare" in limine and "check" in limine
    snapper = next(line for line in text.splitlines() if "snapper" in line and "optional" in line)
    assert "check" in snapper
    # the ten points of the research outline are all there
    assert [line for line in text.splitlines() if line.startswith("## ")][:10] == [
        "## 1. Rules that never change", "## 2. Where things are", "## 3. Tools for the target", "## 4. The storage stack (look at it before assuming)", "## 5. Boot",
        "## 6. Kernel and initramfs", "## 7. Init system", "## 8. Portage", "## 9. Typical failures and the order to check them", "## 10. How to answer"]


def test_claude_loads_the_same_guide_through_the_managed_claude_md() -> None:
    assert (ROOT / "etc" / "claude-code" / "CLAUDE.md").read_text().strip() == "@/etc/gentoo-rescue/AGENTS.md"


def test_claude_managed_settings_ask_for_every_bash_and_lock_the_rest() -> None:
    settings = json.loads(MANAGED.read_text())
    permissions = settings["permissions"]
    assert permissions["defaultMode"] == "default" and "Bash" in permissions["ask"]
    assert permissions["disableBypassPermissionsMode"] == "disable" and permissions["disableAutoMode"] == "disable"
    for pattern in ("Bash(rm -rf *)", "Bash(mkfs*)", "Bash(dd *)", "Bash(wipefs*)", "Bash(sudo *)", "Edit(//mnt/gentoo/**)", "Edit(//mnt/rescue/**)"):
        assert pattern in permissions["deny"]
    assert not any("Bash" in rule for rule in permissions.get("allow", []))  # nothing in Bash is ever pre-approved
    # defence in depth against the planted-config path (keys documented at code.claude.com/docs/en/settings-reference)
    assert settings["allowManagedHooksOnly"] is True and settings["allowManagedPermissionRulesOnly"] is True
    assert settings["allowManagedMcpServersOnly"] is True and settings["enableAllProjectMcpServers"] is False
    assert {"/mnt/gentoo/**", "/mnt/rescue/**"} <= set(settings["claudeMdExcludes"])
    assert settings["env"]["DISABLE_AUTOUPDATER"] == "1" and settings["env"]["DISABLE_INSTALLATION_CHECKS"] == "1"  # the same keys as the ::gentoo ebuild
    hook = settings["hooks"]["PreToolUse"][0]
    assert "Bash" in hook["matcher"] and "Write" in hook["matcher"] and "Edit" in hook["matcher"]
    assert hook["hooks"][0]["type"] == "command" and "/etc/gentoo-rescue/hooks/pretooluse-guard.py" in hook["hooks"][0]["command"]
    for secret in ("shadow", "ssh_host", ".ssh/id_", "auth.json"):
        assert any(secret in rule and rule.startswith("Read(") for rule in permissions["deny"]), secret


def test_codex_config_is_read_only_and_asks() -> None:
    config = tomllib.loads((ROOT / "etc" / "skel-rescue-ai" / ".codex" / "config.toml").read_text())
    assert config["sandbox_mode"] == "read-only" and config["approval_policy"] == "on-request"
    assert config["cli_auth_credentials_store"] in {"file", "ephemeral"}  # see the comment in the file for why "file"
    assert config["check_for_update_on_startup"] is False
    assert config["projects"]["/mnt/gentoo"]["trust_level"] == "untrusted" and config["projects"]["/mnt/rescue"]["trust_level"] == "untrusted"


def test_opencode_config_asks_for_everything() -> None:
    config = json.loads((ROOT / "etc" / "skel-rescue-ai" / ".config" / "opencode" / "opencode.json").read_text())
    assert config["$schema"] == "https://opencode.ai/config.json"
    permission = config["permission"]
    assert permission["*"] == "ask" and all(value == "ask" for value in permission.values())
    assert {"bash", "edit", "read", "webfetch", "external_directory", "doom_loop"} <= set(permission)
    assert config["share"] == "disabled"


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("guard", ROOT / "etc" / "gentoo-rescue" / "hooks" / "pretooluse-guard.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def bash(guard, command: str, cwd: str = "/home/rescue-ai") -> str:
    return guard.judge({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd})


@pytest.mark.parametrize("command", [
    "echo x > /mnt/gentoo/etc/fstab", "echo x >> /mnt/gentoo/etc/fstab", "cat a >/mnt/gentoo/x", "ls 2>/mnt/rescue/r1/err", "tee /mnt/gentoo/etc/motd < /dev/null",
    "rm /mnt/gentoo/etc/fstab", "rm -rf /mnt/gentoo/var", "touch /mnt/gentoo/x", "chmod 777 /mnt/gentoo/etc", "sed -i s/a/b/ /mnt/gentoo/etc/fstab", "sed -ni p /mnt/gentoo/etc/fstab",
    "cp /tmp/x /mnt/gentoo/etc/x", "cp -a /tmp/x /mnt/rescue/r123/etc/", "mv /mnt/gentoo/etc/a /tmp/a", "install -m 644 f /mnt/gentoo/etc/f", "dd if=/dev/zero of=/mnt/gentoo/zero",
    "tar -xf a.tar -C /mnt/gentoo", "tar xf a.tar --directory=/mnt/gentoo", "ln -s /x /mnt/gentoo/y", "mkfs.ext4 /mnt/gentoo/img",
    "bash -c 'echo x > /mnt/gentoo/a'", "sh -c \"rm /mnt/gentoo/a\"", "eval 'rm /mnt/gentoo/a'", "python3 -c 'open(\"/mnt/gentoo/a\",\"w\")'",
    "find /mnt/gentoo -name '*.bak' -delete", "find /mnt/gentoo -exec rm {} +", "cd /mnt/gentoo", "cd /mnt/gentoo/etc && ls", "pushd /mnt/rescue/abc",
    "sudo ls", "FOO=1 sudo ls", "ls && su -", "echo hi | sudo tee /x", "mount -o remount,rw /mnt/gentoo", "umount /mnt/gentoo", "cryptsetup open /dev/sda3 x",
    "cd ../../mnt/gentoo", "rm /home/../mnt/gentoo/x"])
def test_the_hook_denies_writes_to_the_target_cd_into_it_and_sudo(guard, command) -> None:
    assert bash(guard, command, cwd="/home/rescue-ai"), command


@pytest.mark.parametrize("command", [
    "ls /mnt/gentoo/etc", "cat /mnt/gentoo/etc/fstab", "grep -r UUID /mnt/gentoo/etc/fstab", "find /mnt/gentoo/etc -name 'make.conf'", "cp /mnt/gentoo/etc/fstab ~/fstab.copy",
    "diff /mnt/gentoo/etc/fstab /tmp/f", "gentoo-rescue-do gentoo-rescue --local status", "echo 'a > b' ", "rm ~/old.txt", "tar -tf /mnt/gentoo/a.tar",
    "dd if=/mnt/gentoo/boot/vmlinuz of=/tmp/k bs=1M", "ls | grep gentoo", "df -h /mnt/gentoo", "sed -n 1,5p /mnt/gentoo/etc/fstab", "true"])
def test_the_hook_lets_reading_and_ordinary_commands_through_to_the_normal_ask(guard, command) -> None:
    assert bash(guard, command) == "", command


def test_the_hook_denies_edit_tools_below_the_target_even_by_relative_path_or_symlink(guard, tmp_path, monkeypatch) -> None:
    for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")):
        assert guard.judge({"tool_name": tool, "tool_input": {key: "/mnt/gentoo/etc/fstab"}, "cwd": "/home/rescue-ai"})
        assert guard.judge({"tool_name": tool, "tool_input": {key: "etc/fstab"}, "cwd": "/mnt/gentoo"})
        assert guard.judge({"tool_name": tool, "tool_input": {key: "/home/rescue-ai/../../mnt/rescue/r1/x"}, "cwd": "/"})
        assert guard.judge({"tool_name": tool, "tool_input": {key: "/home/rescue-ai/notes.txt"}, "cwd": "/home/rescue-ai"}) == ""
    target = tmp_path / "mnt" / "gentoo"
    target.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(target)
    monkeypatch.setattr(guard, "TARGETS", (str(target),))
    assert guard.judge({"tool_name": "Write", "tool_input": {"file_path": str(link / "f")}, "cwd": "/"})


def test_the_hook_script_speaks_claude_codes_json_and_stays_silent_when_it_allows(tmp_path) -> None:
    import subprocess
    import sys

    script = ROOT / "etc" / "gentoo-rescue" / "hooks" / "pretooluse-guard.py"

    def run(payload: object) -> tuple[int, str]:
        done = subprocess.run([sys.executable, "-I", str(script)], input=json.dumps(payload), capture_output=True, text=True, timeout=20, cwd=tmp_path)
        return done.returncode, done.stdout

    code, out = run({"tool_name": "Bash", "tool_input": {"command": "rm /mnt/gentoo/etc/fstab"}, "cwd": "/"})
    decision = json.loads(out)["hookSpecificOutput"]
    assert code == 0 and decision["hookEventName"] == "PreToolUse" and decision["permissionDecision"] == "deny" and "read-only" in decision["permissionDecisionReason"]
    assert run({"tool_name": "Bash", "tool_input": {"command": "ls /mnt/gentoo"}, "cwd": "/"}) == (0, "")
    assert run("not a dict") == (0, "")
    assert subprocess.run([sys.executable, "-I", str(script)], input="{broken", capture_output=True, text=True, timeout=20).stdout == ""
