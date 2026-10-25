"""The text-mode boot entries "Repair a system" and "Install in text mode".

Three parts have to agree: the boot menu adds `gentoo.autostart=<tool>`, the profile.d hook
starts that tool on the first console login, and the image contract demands both. This file
tests the menu and its verifier rules; the hook has its own tests further down.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from .conftest import ROOT, load_script, write_executable

render = load_script("iso/render-config.py", "render_config")
verify = load_script("scripts/verify-iso.py", "verify_iso")

CONTRACT = json.loads((ROOT / "iso/image-contract.json").read_text())
ISO_FILES = {"boot/gentoo", "boot/gentoo.igz", "memtest64.bios", "memtest.efi64"}
REPAIR = "Repair a system (text console)"
TEXT_INSTALL = "Install in text mode"
AI_RESCUE = "AI rescue (text console)"


@pytest.fixture()
def grub_text(pins: dict[str, str]) -> str:
    values = render.token_values(pins, "/build", "2", "4")
    template = (ROOT / "iso/cd_overlay/boot/grub/grub.cfg.in").read_text()
    return render.render_text(template, values, "grub.cfg.in")


@pytest.fixture()
def entries(grub_text: str):
    return {entry.title: entry for entry in verify.parse_grub_cfg(grub_text)[0]}


def check(grub_text: str, pins: dict[str, str]):
    report = verify.Report()
    verify.check_grub(report, grub_text, pins["ISO_VOLID"], pins["ISO_BOOTARGS"].split(), set(ISO_FILES), CONTRACT)
    return report


def failed(report) -> set[str]:
    return {c.id for c in report.errors}


# --- the menu --------------------------------------------------------------------------------


def test_the_menu_offers_a_repair_entry_and_a_text_install_entry(entries) -> None:
    assert REPAIR in entries and TEXT_INSTALL in entries
    repair, install = entries[REPAIR].linux, entries[TEXT_INSTALL].linux
    assert len(repair) == 1 and len(install) == 1
    assert {"nox", "dokeymap", "gentoo.autostart=rescue"} <= set(repair[0].split())
    assert {"nox", "dokeymap", "gentoo.autostart=installer-text"} <= set(install[0].split())


def test_both_entries_ask_for_a_keyboard_layout_because_passwords_are_typed(entries) -> None:
    """A LUKS passphrase or an account password is typed in both tools: the layout is chosen first."""
    for title in (REPAIR, TEXT_INSTALL):
        words = entries[title].linux[0].split()
        assert "dokeymap" in words and "nox" in words, title


def test_only_the_three_text_entries_carry_an_autostart_token(entries) -> None:
    carrying = {title for title, entry in entries.items() if any("gentoo.autostart=" in line for line in entry.linux)}
    assert carrying == {REPAIR, AI_RESCUE, TEXT_INSTALL}


def test_the_default_entry_is_still_the_desktop(entries, grub_text: str) -> None:
    first = verify.parse_grub_cfg(grub_text)[0][0]
    assert first.title == "Gentoo installer and rescue desktop"
    assert not {"nox", "dokeymap"} & set(first.linux[0].split())
    assert "gentoo.autostart" not in first.linux[0]


def test_repair_comes_before_the_plain_shell_and_the_troubleshooting_submenu(grub_text: str) -> None:
    titles = [e.title for e in verify.parse_grub_cfg(grub_text)[0]]
    assert titles.index(REPAIR) < titles.index(TEXT_INSTALL) < titles.index("Text console only (a shell, asks for a keyboard layout)")
    assert titles.index(TEXT_INSTALL) < titles.index("Verbose boot (kernel and initramfs messages)")


def test_the_entries_keep_the_dracut_arguments_in_catalysts_order(entries, pins: dict[str, str]) -> None:
    tail = f"root=live:CDLABEL={pins['ISO_VOLID']} rd.live.dir=/ rd.live.squashimg=image.squashfs cdroot"
    for title in (REPAIR, TEXT_INSTALL):
        line = entries[title].linux[0]
        assert tail in line and line.index(tail) < line.index("nox"), title
        assert line.split()[2:2 + len(pins["ISO_BOOTARGS"].split())] == pins["ISO_BOOTARGS"].split(), "the shared boot arguments come first"
    assert any(line.strip() == "initrd /boot/gentoo.igz" for line in entries[REPAIR].lines)


def test_the_contract_names_exactly_the_values_the_menu_uses(entries) -> None:
    used = {word.split("=", 1)[1] for e in entries.values() for line in e.linux for word in line.split() if word.startswith("gentoo.autostart=")}
    assert used == set(CONTRACT["grub"]["autostart_values"]) == {"rescue", "ai-rescue", "installer-text"}
    assert {REPAIR, TEXT_INSTALL} <= set(CONTRACT["grub"]["required_titles"])  # plus Safe graphics (H1)


# --- the verifier refuses what would break the console hook ------------------------------------


def test_the_rendered_menu_passes_the_verifier(grub_text: str, pins: dict[str, str]) -> None:
    report = check(grub_text, pins)
    assert report.ok, [c.id + ": " + c.detail for c in report.errors]
    assert "grub.autostart" in {c.id for c in report.checks}


def test_a_menu_without_the_repair_entry_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    start = grub_text.index(f"menuentry '{REPAIR}'")
    end = grub_text.index("menuentry 'Install in text mode'")
    report = check(grub_text[:start] + grub_text[end:], pins)
    assert {"grub.required_titles", "grub.has_gentoo.autostart=rescue", "grub.argument_group"} <= failed(report)


def test_a_menu_without_the_text_install_entry_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    start = grub_text.index("menuentry 'Install in text mode'")
    end = grub_text.index("menuentry 'Text console only")
    report = check(grub_text[:start] + grub_text[end:], pins)
    assert {"grub.required_titles", "grub.has_gentoo.autostart=installer-text"} <= failed(report)


def test_an_autostart_token_on_a_desktop_entry_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    broken = grub_text.replace("cdroot rd.live.ram=1\n", "cdroot rd.live.ram=1 gentoo.autostart=rescue\n", 1)
    assert broken != grub_text
    report = check(broken, pins)
    assert "grub.autostart" in failed(report)
    assert "also the desktop" in next(c.detail for c in report.errors if c.id == "grub.autostart")


def test_an_autostart_value_the_hook_does_not_know_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    broken = grub_text.replace("nox dokeymap gentoo.autostart=rescue", "nox dokeymap gentoo.autostart=repair", 1)
    assert broken != grub_text
    report = check(broken, pins)
    assert "grub.autostart" in failed(report)
    assert "gentoo.autostart=repair" in next(c.detail for c in report.errors if c.id == "grub.autostart")


# --- the console hook: /etc/profile.d/20-gentoo-autostart.sh --------------------------------

HOOK = ROOT / "iso/root_overlay/etc/profile.d/20-gentoo-autostart.sh"
TOOLS = {"rescue": "gentoo-rescue", "ai-rescue": "gentoo-rescue-ai", "installer-text": "gentoo-installer-text"}


class HookRun:
    """Source the hook in an interactive shell whose sudo, tty and tools are stand-ins that write a log."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "calls.log"
        self.state = tmp_path / "state"
        self.cmdline = tmp_path / "cmdline"
        self.socket = tmp_path / "server.sock"
        self.socket.touch()  # the installer service is up unless a test removes this
        self.do_socket = tmp_path / "do.sock"
        self.do_socket.touch()  # the approval service of the AI rescue entry, likewise
        write_executable(self.bin / "tty", '#!/bin/sh\necho "$FAKE_TTY"\n')
        os.symlink(shutil.which("mkdir"), self.bin / "mkdir")  # the only external program the hook needs besides sudo and tty

    def provide(self, *names: str, status: int = 0) -> None:
        for name in names:
            write_executable(self.bin / name, f'#!/bin/sh\necho "{name} $*" >> "$CALL_LOG"\nexit {status}\n')

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run(self, cmdline: str | None, *, tty: str = "/dev/tty1", interactive: bool = True, display: bool = False,
            shell: tuple[str, ...] = ("bash", "--norc"), after: str = "", wait: int = 3) -> subprocess.CompletedProcess:
        if cmdline is not None:
            self.cmdline.write_text(cmdline + "\n")
        env = {
            "PATH": str(self.bin), "HOME": str(self.tmp), "CALL_LOG": str(self.log), "FAKE_TTY": tty,
            "GENTOO_AUTOSTART_CMDLINE": str(self.cmdline), "GENTOO_AUTOSTART_STATE": str(self.state),
            "GENTOO_AUTOSTART_SOCKET": str(self.socket), "GENTOO_AUTOSTART_WAIT": str(wait), "FAKE_SOCKET": str(self.socket),
            "GENTOO_AUTOSTART_DO_SOCKET": str(self.do_socket), "FAKE_DO_SOCKET": str(self.do_socket),
        }
        if display:
            env["DISPLAY"] = ":0"
        argv = [shutil.which(shell[0]), *shell[1:], *(["-i"] if interactive else []), "-c", f". {HOOK}; {after}"]
        return subprocess.run(argv, capture_output=True, text=True, env=env, check=False, stdin=subprocess.DEVNULL, timeout=30)


@pytest.fixture()
def hook(tmp_path: Path) -> HookRun:
    return HookRun(tmp_path)


def errors(result: subprocess.CompletedProcess) -> list[str]:
    """What the hook wrote to stderr; bash itself complains about the missing terminal of an interactive test shell."""
    return [line for line in result.stderr.splitlines() if "terminal process group" not in line and "no job control" not in line]


BASE = "BOOT_IMAGE=/boot/gentoo nodhcp secureconsole root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.dir=/ cdroot nox dokeymap"


@pytest.mark.parametrize(("value", "tool"), sorted(TOOLS.items()))
def test_the_first_console_login_starts_the_tool_with_sudo(hook: HookRun, value: str, tool: str) -> None:
    hook.provide("sudo", tool)
    result = hook.run(f"{BASE} gentoo.autostart={value}")
    assert result.returncode == 0, result.stderr
    assert hook.calls() == [f"sudo {tool}"]
    assert f"sudo {tool}" in result.stdout and "back at the shell" in result.stdout.lower()
    assert errors(result) == []


def test_the_tool_starts_once_not_at_every_login(hook: HookRun) -> None:
    """The login shell on tty1 comes back after 'exit' (autologin): the tool must not start a second time."""
    hook.provide("sudo", "gentoo-rescue")
    hook.run(f"{BASE} gentoo.autostart=rescue")
    second = hook.run(None)
    assert hook.calls() == ["sudo gentoo-rescue"]
    assert second.stdout == ""


def test_nothing_starts_without_the_token(hook: HookRun) -> None:
    hook.provide("sudo", "gentoo-rescue", "gentoo-installer-text")
    result = hook.run(BASE)
    assert hook.calls() == [] and result.stdout == "" and errors(result) == []
    assert not hook.state.exists(), "no token must not use up the one-time switch"


@pytest.mark.parametrize("cmdline", [
    f"{BASE} gentoo.autostart=repair", f"{BASE} gentoo.autostart=", f"{BASE} gentoo.autostart", f"{BASE} xgentoo.autostart=rescue",
    f"{BASE} gentoo.autostart=rescue2", f"{BASE} gentoo.autostart=Rescue", f"{BASE} foo=gentoo.autostart=rescue", f"{BASE} gentoo.autostart=rescue,installer-text",
])
def test_only_the_two_known_values_count_and_only_as_whole_words(hook: HookRun, cmdline: str) -> None:
    hook.provide("sudo", "gentoo-rescue", "gentoo-installer-text")
    result = hook.run(cmdline)
    assert hook.calls() == [] and result.stdout == ""


def test_the_repair_tool_wins_when_both_tokens_are_given(hook: HookRun) -> None:
    hook.provide("sudo", "gentoo-rescue", "gentoo-installer-text")
    hook.run(f"{BASE} gentoo.autostart=installer-text gentoo.autostart=rescue")
    assert hook.calls() == ["sudo gentoo-rescue"]


@pytest.mark.parametrize("tty", ["/dev/tty2", "/dev/pts/0", "/dev/ttyS0", "not a tty"])
def test_only_the_first_console_starts_the_tool(hook: HookRun, tty: str) -> None:
    hook.provide("sudo", "gentoo-rescue")
    result = hook.run(f"{BASE} gentoo.autostart=rescue", tty=tty)
    assert hook.calls() == [] and result.stdout == ""
    assert not hook.state.exists(), "another console must not use up the one-time switch"


def test_a_graphical_session_never_starts_it(hook: HookRun) -> None:
    hook.provide("sudo", "gentoo-rescue")
    result = hook.run(f"{BASE} gentoo.autostart=rescue", display=True)
    assert hook.calls() == [] and result.stdout == ""


def test_scripts_and_scp_see_nothing(hook: HookRun) -> None:
    hook.provide("sudo", "gentoo-rescue")
    result = hook.run(f"{BASE} gentoo.autostart=rescue", interactive=False)
    assert hook.calls() == [] and result.stdout == "" and errors(result) == []


def test_a_failing_tool_leaves_the_user_at_the_shell_with_the_exit_status(hook: HookRun) -> None:
    hook.provide("sudo", status=3)
    hook.provide("gentoo-rescue")
    result = hook.run(f"{BASE} gentoo.autostart=rescue", after="echo SHELL-CONTINUES")
    assert "exit status 3" in result.stdout and "sudo gentoo-rescue" in result.stdout.split("exit status 3")[1]
    assert result.stdout.rstrip().endswith("SHELL-CONTINUES"), "the login must go on to the prompt"
    assert result.returncode == 0


@pytest.mark.parametrize("missing", ["sudo", "gentoo-rescue"])
def test_a_missing_program_is_explained_and_the_login_goes_on(hook: HookRun, missing: str) -> None:
    hook.provide(*{"sudo", "gentoo-rescue"} - {missing})
    result = hook.run(f"{BASE} gentoo.autostart=rescue", after="echo SHELL-CONTINUES")
    assert "gentoo-rescue is not on this disc" in result.stdout
    assert result.stdout.rstrip().endswith("SHELL-CONTINUES")
    assert hook.calls() == []


def test_an_unreadable_command_line_is_silent(hook: HookRun) -> None:
    hook.provide("sudo", "gentoo-rescue")
    result = hook.run(None)  # the file does not exist
    assert hook.calls() == [] and result.stdout == "" and errors(result) == []


def test_the_hook_works_in_posix_mode_and_leaves_no_variables_behind(hook: HookRun) -> None:
    hook.provide("sudo", "gentoo-installer-text")
    result = hook.run(f"{BASE} gentoo.autostart=installer-text", shell=("bash", "--posix", "--norc"), after="set")
    assert hook.calls() == ["sudo gentoo-installer-text"]
    assert "gentoo_autostart" not in result.stdout.split("back at the shell")[-1], "the login shell keeps no helper variables"


def test_the_hook_text_matches_the_contract_and_the_menu() -> None:
    text = HOOK.read_text()
    values = set(re.findall(r"gentoo\.autostart=([a-z-]+) ", text))
    assert values == set(CONTRACT["grub"]["autostart_values"])
    for value, tool in TOOLS.items():
        assert f"gentoo.autostart={value} " in text and f"gentoo_autostart_tool={tool}" in text
    assert "sudo" in text and "/dev/tty1" in text and "/tmp/.gentoo-autostart" in text
    assert text.isascii()
    paths = {item["path"] for item in CONTRACT["squashfs"]["required"]}
    assert "etc/profile.d/20-gentoo-autostart.sh" in paths
    must = [r for r in CONTRACT["squashfs"]["content"] if r["path"] == "etc/profile.d/20-gentoo-autostart.sh"]
    assert must and all(re.search(pattern, text, re.M) for r in must for pattern in r["must_match"])


def test_the_hook_waits_for_the_installer_service_and_then_starts_the_tool(hook: HookRun) -> None:
    """gentoo-installerd can start a moment after the first console; the stand-in sleep brings it up."""
    hook.socket.unlink()
    hook.provide("sudo", "gentoo-installer-text")
    write_executable(hook.bin / "sleep", '#!/bin/sh\necho "sleep $*" >> "$CALL_LOG"\n: > "$FAKE_SOCKET"\n')
    result = hook.run(f"{BASE} gentoo.autostart=installer-text")
    assert hook.calls() == ["sleep 1", "sudo gentoo-installer-text"]
    assert result.stdout.count("Waiting for the installer service") == 1


def test_the_wait_is_bounded_and_the_tool_starts_anyway(hook: HookRun) -> None:
    """With no installer service the repair tool still works on its own (root runs the engine in-process)."""
    hook.socket.unlink()
    hook.provide("sudo", "gentoo-rescue")
    write_executable(hook.bin / "sleep", '#!/bin/sh\necho "sleep $*" >> "$CALL_LOG"\n')
    result = hook.run(f"{BASE} gentoo.autostart=rescue", wait=2)
    assert hook.calls() == ["sleep 1", "sleep 1", "sudo gentoo-rescue"]
    assert "Waiting for the installer service" in result.stdout


def test_there_is_no_waiting_when_the_service_is_already_up(hook: HookRun) -> None:
    hook.provide("sudo", "gentoo-rescue")
    write_executable(hook.bin / "sleep", '#!/bin/sh\necho "sleep $*" >> "$CALL_LOG"\n')
    result = hook.run(f"{BASE} gentoo.autostart=rescue")
    assert hook.calls() == ["sudo gentoo-rescue"] and "Waiting" not in result.stdout


def test_the_text_installer_script_exists_wherever_the_disc_names_it() -> None:
    """Integration tripwire: the greeting, the boot entry and the hook send users to gentoo-installer-text (the console script of src/gentoo_installer/textui).

    Skipped while the text installer is not part of this tree; once its package exists, pyproject.toml must
    declare the console script, or the first text-mode install would end at 'command not found'."""
    if not (ROOT / "src/gentoo_installer/textui/__init__.py").exists():
        pytest.skip("the text installer is not part of this tree")
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'^gentoo-installer-text\s*=\s*"gentoo_installer\.textui\.cli:main"', pyproject, re.M), (
        "add gentoo-installer-text = \"gentoo_installer.textui.cli:main\" to [project.scripts] in pyproject.toml"
    )


# --- gentoo-chroot: the short form of "gentoo-rescue chroot" ----------------------------------

CHROOT_HELPER = ROOT / "packaging/gentoo-overlay/app-admin/gentoo-live-installer/files/gentoo-chroot"


def run_helper(tmp_path: Path, *arguments: str, with_tool: bool = True) -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    if with_tool:
        write_executable(bin_dir / "gentoo-rescue", '#!/bin/sh\nprintf "%s\\n" "$@" >> "$CALL_LOG"\nexit 7\n')
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "CALL_LOG": str(log)}
    result = subprocess.run(["sh", str(CHROOT_HELPER), *arguments], capture_output=True, text=True, env=env, check=False, timeout=30)
    return result, (log.read_text().splitlines() if log.exists() else [])


def test_gentoo_chroot_hands_everything_to_the_repair_tool(tmp_path: Path) -> None:
    result, calls = run_helper(tmp_path, "s1", "--writable", "--typed", "nvme0n1p2")
    assert calls == ["chroot", "s1", "--writable", "--typed", "nvme0n1p2"]
    assert result.returncode == 7, "the exit status of the shell is the helper's exit status (exec)"


def test_gentoo_chroot_help_reaches_the_repair_tool(tmp_path: Path) -> None:
    _, calls = run_helper(tmp_path, "--help")
    assert calls == ["chroot", "--help"]


def test_gentoo_chroot_without_arguments_finds_the_system_itself(tmp_path: Path) -> None:
    """Wave 5 (I12): no session needed; the repair tool finds, opens and releases the system."""
    result, calls = run_helper(tmp_path)
    assert calls == ["chroot"] and result.returncode == 7


def test_gentoo_chroot_uses_only_names_the_repair_tool_knows() -> None:
    """'chroot' is an alias of the shell sub-command and 'status' lists sessions: both are in rescue/cli.py."""
    cli = (ROOT / "src/gentoo_installerd/rescue/cli.py").read_text()
    assert 'aliases=["chroot"]' in cli and 'sub.add_parser("status"' in cli


# --- the AI rescue entry (wave 6) ----------------------------------------------------------------


def test_the_ai_rescue_entry_is_a_text_entry_that_asks_for_the_keyboard_first(entries) -> None:
    words = entries[AI_RESCUE].linux[0].split()
    assert {"nox", "dokeymap", "gentoo.autostart=ai-rescue"} <= set(words)
    assert "gentoo.autostart=rescue" not in words, "the plain repair entry stays free of the agents"
    titles = list(entries)
    assert titles.index(REPAIR) < titles.index(AI_RESCUE) < titles.index(TEXT_INSTALL)


def test_no_entry_replaces_the_kernels_security_module_list(grub_text: str) -> None:
    """Yama (ptrace_scope=1) is what keeps the agent processes apart: an lsm= word could drop it."""
    assert not re.search(r"(?m)^\s*linux\b.*\blsm=", grub_text)


def test_the_ai_rescue_hook_waits_for_the_approval_service_too(hook: HookRun) -> None:
    hook.do_socket.unlink()
    hook.provide("sudo", "gentoo-rescue-ai")
    write_executable(hook.bin / "sleep", '#!/bin/sh\necho "sleep $*" >> "$CALL_LOG"\n: > "$FAKE_DO_SOCKET"\n')
    result = hook.run(f"{BASE} gentoo.autostart=ai-rescue")
    assert hook.calls() == ["sleep 1", "sudo gentoo-rescue-ai"]
    assert result.stdout.count("Waiting for the approval service") == 1 and "Waiting for the installer service" not in result.stdout


def test_the_approval_wait_is_bounded_and_the_tool_starts_anyway(hook: HookRun) -> None:
    hook.do_socket.unlink()
    hook.provide("sudo", "gentoo-rescue-ai")
    write_executable(hook.bin / "sleep", '#!/bin/sh\necho "sleep $*" >> "$CALL_LOG"\n')
    hook.run(f"{BASE} gentoo.autostart=ai-rescue", wait=2)
    assert hook.calls() == ["sleep 1", "sleep 1", "sudo gentoo-rescue-ai"]


def test_the_other_tools_never_wait_for_the_approval_service(hook: HookRun) -> None:
    hook.do_socket.unlink()
    hook.provide("sudo", "gentoo-rescue")
    result = hook.run(f"{BASE} gentoo.autostart=rescue")
    assert hook.calls() == ["sudo gentoo-rescue"] and "approval" not in result.stdout


def test_the_plain_repair_tool_wins_over_the_ai_token(hook: HookRun) -> None:
    hook.provide("sudo", "gentoo-rescue", "gentoo-rescue-ai")
    hook.run(f"{BASE} gentoo.autostart=ai-rescue gentoo.autostart=rescue")
    assert hook.calls() == ["sudo gentoo-rescue"]


def test_the_agents_are_never_started_under_another_name(hook: HookRun) -> None:
    """The hook starts one program, as root through sudo; the agents themselves are started by gentoo-rescue-ai."""
    text = HOOK.read_text()
    assert not re.search(r"\b(claude|codex|opencode|tmux|su -)\b", re.sub(r"(?m)^\s*#.*$", "", text))
