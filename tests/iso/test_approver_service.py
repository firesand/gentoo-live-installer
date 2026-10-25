"""Wave 6: the OpenRC service of the root approver, and the package that installs it (iso owner).

The init script is run by sh with stand-ins for what OpenRC provides (einfo, eerror, checkpath, default_start) and for fuser."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess

import pytest

from .conftest import ROOT, load_script, write_executable

verify = load_script("scripts/verify-iso.py", "verify_iso")
CONTRACT = json.loads((ROOT / "iso/image-contract.json").read_text())
PACKAGE = ROOT / "packaging/gentoo-overlay/app-admin/gentoo-live-installer"
INITD = PACKAGE / "files/gentoo-rescue-approver.initd"
CONFD = PACKAGE / "files/gentoo-rescue-approver.confd"
AI_CMDLINE = "BOOT_IMAGE=/boot/gentoo nodhcp secureconsole root=live:CDLABEL=X cdroot nox dokeymap gentoo.autostart=ai-rescue"


class Service:
    """The init script sourced in a shell whose OpenRC functions write a log."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.run_dir = tmp_path / "run/gentoo-rescue"
        self.log_dir = tmp_path / "log/gentoo-rescue"
        self.dev = tmp_path / "dev"
        self.dev.mkdir()
        self.cmdline = tmp_path / "cmdline"
        self.calls = tmp_path / "calls.log"
        self.bin = tmp_path / "bin"
        self.command = tmp_path / "gentoo-rescue-approver"
        write_executable(self.command, "#!/bin/sh\n")
        # fuser -s FILE succeeds (the file is in use) for the files listed in $BUSY
        write_executable(self.bin / "fuser", '#!/bin/sh\nfor busy in $BUSY; do [ "$busy" = "$2" ] && exit 0; done\nexit 1\n')
        for number in range(1, 13):
            (self.dev / f"tty{number}").touch()
        self.set_cmdline(AI_CMDLINE)

    def set_cmdline(self, text: str) -> None:
        self.cmdline.write_text(text + "\n")

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.tmp),
            "GENTOO_RESCUE_RUN_DIR": str(self.run_dir), "GENTOO_RESCUE_LOG_DIR": str(self.log_dir), "GENTOO_RESCUE_CMDLINE": str(self.cmdline),
            "GENTOO_RESCUE_DEV_DIR": str(self.dev), "CALLS": str(self.calls), "RC_SVCNAME": "gentoo-rescue-approver",
        }
        env.update(extra or {})
        return env

    def run(self, body: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        script = f"""
einfo() {{ echo "einfo: $*"; }}
eerror() {{ echo "eerror: $*"; }}
checkpath() {{ echo "checkpath $*" >> "$CALLS"; while [ $# -gt 1 ]; do shift; done; mkdir -p "$1"; }}
default_start() {{ echo "default_start $command_args" >> "$CALLS"; }}
chown() {{ echo "chown $*" >> "$CALLS"; }}
. {INITD}
command={self.command}
RESCUE_VT=${{RESCUE_VT:-9}}
{body}
"""
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=self.env(extra), check=False, timeout=30)

    def logged(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture()
def service(tmp_path: Path) -> Service:
    return Service(tmp_path)


def test_the_scripts_are_valid_shell() -> None:
    for path in (INITD, CONFD):
        assert subprocess.run(["sh", "-n", str(path)], capture_output=True, check=False).returncode == 0, path


def test_the_ai_rescue_boot_entry_starts_the_approver(service: Service) -> None:
    result = service.run("start_pre && start; echo status=$?")
    assert "status=0" in result.stdout, result.stdout + result.stderr
    started = [line for line in service.logged() if line.startswith("default_start")]
    assert started == [f"default_start --vt 9 --socket {service.run_dir}/do.sock --audit {service.log_dir}/do.log "]
    assert (service.run_dir / "do-password").is_file()


def test_the_password_is_twelve_typeable_characters_in_a_file_only_root_reads(service: Service) -> None:
    service.run("start_pre")
    path = service.run_dir / "do-password"
    text = path.read_text()
    assert re.fullmatch(r"[a-km-z2-9]{4}-[a-km-z2-9]{4}-[a-km-z2-9]{4}\n", text), repr(text)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not set("ilo01") & set(text.strip().replace("-", "")), "no look-alike characters: it is typed from a screen"


def test_every_start_makes_a_new_password(service: Service) -> None:
    service.run("start_pre")
    first = (service.run_dir / "do-password").read_text()
    service.run("start_pre")
    assert (service.run_dir / "do-password").read_text() != first


def test_the_directories_are_root_owned_and_the_log_is_private(service: Service) -> None:
    service.run("start_pre")
    checks = [line for line in service.logged() if line.startswith("checkpath")]
    assert f"checkpath -d -m 0710 -o root:rescue-ai {service.run_dir}" in checks, "the agents may enter the socket directory, not list it"
    assert f"checkpath -d -m 0700 -o root:root {service.log_dir}" in checks, "the audit log is root's alone"
    assert f"chown root:root {service.run_dir}/do-password" in service.logged()


@pytest.mark.parametrize(
    "cmdline",
    [
        "BOOT_IMAGE=/boot/gentoo nodhcp secureconsole cdroot",
        "BOOT_IMAGE=/boot/gentoo nox dokeymap gentoo.autostart=rescue",
        "BOOT_IMAGE=/boot/gentoo nox dokeymap gentoo.autostart=installer-text",
        "BOOT_IMAGE=/boot/gentoo nox gentoo.autostart=ai-rescue2",
        "BOOT_IMAGE=/boot/gentoo nox xgentoo.autostart=ai-rescue",
        "BOOT_IMAGE=/boot/gentoo nox gentoo.autostart=AI-rescue",
        "BOOT_IMAGE=/boot/gentoo nox foo=gentoo.autostart=ai-rescue",
        "BOOT_IMAGE=/boot/gentoo nox gentoo.autostart=ai-rescue,rescue",
        "",
    ],
)
def test_no_other_boot_entry_has_an_approver(service: Service, cmdline: str) -> None:
    service.set_cmdline(cmdline)
    result = service.run("start_pre && start; echo status=$?")
    assert "status=0" in result.stdout and "not the AI rescue boot entry" in result.stdout
    assert not [line for line in service.logged() if line.startswith(("default_start", "checkpath"))]
    assert not service.run_dir.exists(), "no socket directory and no password on any other boot"


def test_an_unreadable_command_line_means_no_approver(service: Service) -> None:
    service.cmdline.unlink()
    result = service.run("start_pre && start; echo status=$?")
    assert "status=0" in result.stdout and not service.run_dir.exists()


def test_the_conf_d_switch_starts_it_anywhere(service: Service) -> None:
    service.set_cmdline("BOOT_IMAGE=/boot/gentoo")
    result = service.run("RESCUE_APPROVER_ALWAYS=yes; start_pre && start; echo status=$?")
    assert "status=0" in result.stdout and any(line.startswith("default_start") for line in service.logged())


def test_the_approver_takes_the_next_console_when_tty9_is_in_use(service: Service) -> None:
    result = service.run("start_pre", extra={"BUSY": f"{service.dev}/tty9"})
    assert "--vt 10 " in " ".join(service.logged()) or "tty10" in result.stdout
    result = service.run("start_pre; echo $command_args", extra={"BUSY": f"{service.dev}/tty9 {service.dev}/tty10"})
    assert "--vt 11 " in result.stdout
    # gentoo-rescue-ai checks that console and names its Alt+F key: it reads the number the service chose
    assert (service.run_dir / "approver-vt").read_text() == "11\n"
    service.run("stop_post")
    assert not (service.run_dir / "approver-vt").exists()


def test_a_missing_console_is_skipped_and_none_free_is_an_error(service: Service) -> None:
    (service.dev / "tty9").unlink()
    assert "--vt 10 " in service.run("start_pre; echo $command_args").stdout
    busy = " ".join(f"{service.dev}/tty{n}" for n in range(9, 13))
    result = service.run("start_pre; echo status=$?", extra={"BUSY": busy})
    assert "status=1" in result.stdout and "no free console" in result.stdout
    assert not (service.run_dir / "do-password").exists()


def test_the_console_number_comes_from_conf_d(service: Service) -> None:
    assert "--vt 12 " in service.run("RESCUE_VT=12; start_pre; echo $command_args").stdout


def test_a_missing_approver_program_stops_the_start(service: Service) -> None:
    service.command.unlink()
    result = service.run("start_pre; echo status=$?")
    assert "status=1" in result.stdout and "AI rescue package is not installed" in result.stdout


def test_stopping_removes_the_socket_and_the_password(service: Service) -> None:
    service.run("start_pre")
    (service.run_dir / "do.sock").touch()
    service.run("stop_post")
    assert not (service.run_dir / "do.sock").exists() and not (service.run_dir / "do-password").exists()


def test_stopping_on_a_boot_that_started_nothing_is_quiet(service: Service) -> None:
    """On every other entry start() ran no daemon: there is no pid file, and the stop at shutdown must not fail on that."""
    result = service.run("pidfile=" + str(service.tmp / "none.pid") + "; default_stop() { echo default_stop; }; stop; echo status=$?")
    assert "status=0" in result.stdout and "default_stop" not in result.stdout
    (service.tmp / "some.pid").write_text("1\n")
    result = service.run("pidfile=" + str(service.tmp / "some.pid") + "; default_stop() { echo default_stop; }; stop; echo status=$?")
    assert "default_stop" in result.stdout


def test_a_stale_socket_from_an_earlier_start_does_not_survive_a_start(service: Service) -> None:
    service.run_dir.mkdir(parents=True)
    (service.run_dir / "do.sock").touch()
    service.run("start_pre")
    assert not (service.run_dir / "do.sock").exists()


# --- what the init script must never do ----------------------------------------------------------


def code(path: Path) -> str:
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


def test_the_service_runs_as_root_without_sudo_and_never_prints_the_password() -> None:
    text = code(INITD)
    assert "command_user" not in text and not re.search(r"\bsudo\b|\bsu\b|\bdoas\b", text)
    for line in text.splitlines():
        if re.search(r"\$\{?password\b", line):
            assert re.search(r"(?<![|>])>\s*\"\$\{rescue_run_dir\}/do-password\"", line) or line.strip().startswith(("case", "password=")), f"the password goes into its file only: {line}"
    assert "umask 077" in text and "0600" in text
    assert not re.search(r"\bdepend\b[^\n]*\bdbus\b|\bneed\b[^\n]*(net|dbus)", text), "no network and no D-Bus: the approver works on a broken PC"


def test_the_service_is_started_by_openrc_and_by_nothing_else() -> None:
    spec = verify.parse_spec((ROOT / "iso/specs/livecd-stage2.spec").read_text())
    assert "gentoo-rescue-approver|default" in verify.spec_list(spec, "livecd/rcadd")
    sources = [p for p in (ROOT / "iso").rglob("*") if p.is_file() and p.suffix in (".sh", ".py", "")] + list((PACKAGE / "files").iterdir())
    for path in sources:
        if path == INITD or path.name in ("fsscript-stage2.sh",):
            continue
        for line in path.read_text(errors="replace").splitlines():
            assert not re.search(r"(rc-service|service|start-stop-daemon)\s+(gentoo-rescue-approver|.*gentoo-rescue-approver)", line) or line.lstrip().startswith("#"), f"{path}: {line}"


def test_the_confd_names_the_console_and_does_not_enable_the_approver_everywhere() -> None:
    text = CONFD.read_text()
    assert re.search(r"(?m)^RESCUE_VT=9$", text)
    assert re.search(r'(?m)^RESCUE_APPROVER_ALWAYS="no"$', text)


def test_the_script_and_the_contract_agree() -> None:
    rules = [r for r in CONTRACT["squashfs"]["content"] if r["path"] == "etc/init.d/gentoo-rescue-approver"]
    text = INITD.read_text()
    assert rules
    for pattern in rules[0]["must_match"]:
        assert re.search(pattern, text, re.M), pattern
    for pattern in rules[0]["must_not_match"]:
        assert not re.search(pattern, code(INITD), re.M), pattern


# --- the package -----------------------------------------------------------------------------------

EBUILD = next(PACKAGE.glob("*.ebuild")).read_text()


def test_the_ebuild_has_a_rescue_ai_flag_for_the_live_disc_only() -> None:
    assert re.search(r'IUSE="[^"]*\brescue-ai\b', EBUILD)
    assert not re.search(r'IUSE="[^"]*\+rescue-ai', EBUILD), "off by default: an installed system has no approver"
    assert re.search(r"(?m)^app-admin/gentoo-live-installer\b.*\brescue-ai\b", (ROOT / "iso/portage/package.use/live-installer").read_text())


def test_the_ebuild_installs_the_service_the_fragment_and_the_data_tree_behind_the_flag() -> None:
    block = EBUILD.split("if use rescue-ai; then", 1)[1].split("\n\tfi\n", 1)[0]
    assert 'newinitd "${FILESDIR}/gentoo-rescue-approver.initd" gentoo-rescue-approver' in block
    assert 'newconfd "${FILESDIR}/gentoo-rescue-approver.confd" gentoo-rescue-approver' in block
    assert 'newins "${FILESDIR}/live-rescue-ai.config" 51-gentoo-live-rescue-ai.config' in block
    assert "insinto /etc/kernel/config.d" in block
    guarded = block.split("if [[ -d ${S}/src/gentoo_rescue_ai/data/root ]]; then", 1)[1]
    assert 'cp -a --no-preserve=ownership "${S}/src/gentoo_rescue_ai/data/root/." "${ED}/"' in guarded
    assert "chmod go-w" in guarded and "/etc/gentoo-rescue" in guarded and "/etc/claude-code" in guarded


def test_the_ebuild_needs_the_programs_the_ai_tool_runs() -> None:
    rdepend = re.search(r'(?s)RDEPEND="(.*?)"\nBDEPEND', EBUILD).group(1)
    block = re.search(r"rescue-ai\? \((.*?)\n\t\)", rdepend, re.S).group(1)
    needed = {"app-misc/tmux", "dev-libs/openssl", "media-gfx/qrencode", "sys-apps/kbd", "sys-apps/ripgrep", "www-apps/ttyd"}
    assert needed <= set(block.split()), needed - set(block.split())
    assert "!dev-util/claude-code" in block.split(), "both packages install /etc/claude-code/managed-settings.json"


def test_the_files_the_ebuild_names_exist_and_are_not_executable() -> None:
    for name in ("gentoo-rescue-approver.initd", "gentoo-rescue-approver.confd", "live-rescue-ai.config"):
        path = PACKAGE / "files" / name
        assert path.is_file() and not path.stat().st_mode & 0o111, f"pkgcheck: {name} in files/ is installed with its own mode"


def test_the_metadata_documents_the_flag() -> None:
    assert 'flag name="rescue-ai"' in (PACKAGE / "metadata.xml").read_text()


def test_the_fsscript_and_the_contract_expect_the_programs_of_the_tools() -> None:
    block = re.search(r"required_commands=\(\n(.*?)\n\)", (ROOT / "iso/fsscript-stage2.sh").read_text(), re.S).group(1)
    in_script = set(re.sub(r"#.*", "", block).split())
    for command in ("tmux", "ttyd", "qrencode", "rg", "openssl", "chvt", "setpriv", "snapper", "fsarchiver", "ncdu", "tcpdump", "mtr", "dd_rescue", "dislocker",
                    "foremost", "memtester", "restic", "rclone", "mount.cifs", "vim", "mc"):
        assert command in in_script and command in CONTRACT["squashfs"]["commands"], command


def test_the_package_files_of_the_ai_package_are_required_only_when_the_source_has_them() -> None:
    """The ai owner's data tree and console scripts: required when the sources contain them, which they do after the merge."""
    conditional = {item["path"]: item["when_source"] for item in CONTRACT["squashfs"]["required"] if "when_source" in item}
    for path in ("etc/gentoo-rescue/AGENTS.md", "etc/claude-code/CLAUDE.md", "etc/claude-code/managed-settings.json", "etc/skel-rescue-ai"):
        assert conditional[path] == f"src/gentoo_rescue_ai/data/root/{path}", path
    for script in ("agents", "login", "ai", "do", "approver", "mount", "share"):
        assert conditional[f"usr/bin/gentoo-rescue-{script}"] == {"path": "pyproject.toml", "contains": f"gentoo-rescue-{script} ="}


def test_those_requirements_fail_a_listing_that_lacks_them_once_the_sources_have_them() -> None:
    def missing(source) -> str:
        result = verify.Report()
        verify.check_squashfs(result, verify.parse_description("dir etc 0755 0 0\n"), CONTRACT, [], "release", lambda p: b"", "UID: 0\nGID: 0", (), source,
                              lambda path, text: path == "pyproject.toml" and text in 'gentoo-rescue-do = "x"\ngentoo-rescue-ai = "y"')
        return next((c.detail for c in result.errors if c.id == "squashfs.required"), "")

    assert "etc/gentoo-rescue/AGENTS.md" not in missing(lambda p: False)
    both = missing(lambda p: p.startswith("src/gentoo_rescue_ai/data/root/"))
    assert "etc/gentoo-rescue/AGENTS.md" in both and "etc/claude-code/managed-settings.json" in both and "etc/skel-rescue-ai" in both
    scripts = missing(lambda p: False)
    assert "usr/bin/gentoo-rescue-do" in scripts and "usr/bin/gentoo-rescue-ai" in scripts and "usr/bin/gentoo-rescue-share" not in scripts
