"""I9, the image half: sshd at boot, the drop-in that refuses empty passwords, and gentoo-remote-access."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest

from .conftest import ROOT, write_executable

SCRIPT = ROOT / "packaging/gentoo-overlay/app-admin/gentoo-live-installer/files/gentoo-remote-access"
DROPIN = ROOT / "iso/root_overlay/etc/ssh/sshd_config.d/10-gentoo-live.conf"

KEYGEN = """#!/bin/sh
# ssh-keygen -l -E sha256 -f FILE
file=$5
type=$(basename "$file" | sed 's/ssh_host_\\(.*\\)_key.pub/\\1/' | tr a-z A-Z)
echo "256 SHA256:abc${type}def root@live ($type)"
"""


@pytest.fixture()
def box(tmp_path: Path):
    """Fake id, chpasswd, passwd, rc-service, ip and ssh-keygen, with a state directory."""
    state = tmp_path / "state"
    state.mkdir()
    keys = tmp_path / "keys"
    keys.mkdir()
    bin_dir = tmp_path / "bin"
    write_executable(bin_dir / "id", f'#!/bin/sh\nprintf "%s\\n" "$(cat {state}/uid 2>/dev/null || echo 0)"\n')
    write_executable(bin_dir / "chpasswd", f"#!/bin/sh\ncat >> {state}/chpasswd\n[ ! -e {state}/chpasswd-fails ]\n")
    write_executable(
        bin_dir / "passwd",
        f'#!/bin/sh\nif [ "$1" = -S ]; then cat {state}/passwd-status 2>/dev/null || echo "gentoo NP 2026-10-25 0 99999 7 -1"; exit 0; fi\n'
        f'echo "$@" >> {state}/passwd-calls\n',
    )
    write_executable(
        bin_dir / "rc-service",
        f'#!/bin/sh\necho "$@" >> {state}/rc-calls\n'
        f'if [ "$2" = status ]; then [ -e {state}/sshd-running ]; exit $?; fi\n'
        f'if [ "$2" = start ]; then [ ! -e {state}/start-fails ] && touch {state}/sshd-running; exit $?; fi\n',
    )
    write_executable(
        bin_dir / "ip",
        f'#!/bin/sh\ncat {state}/ip 2>/dev/null || true\n',
    )
    write_executable(bin_dir / "ssh-keygen", KEYGEN)
    # the script reads host keys from /etc/ssh; point it at a directory of our own with a sed-free trick:
    patched = tmp_path / "gentoo-remote-access"
    patched.write_text(SCRIPT.read_text().replace("/etc/ssh/", f"{keys}/"))
    patched.chmod(0o755)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "LC_ALL": "C"}

    class Box:
        def __init__(self) -> None:
            self.state = state
            self.keys = keys

        def run(self, *args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["bash", str(patched), *args], capture_output=True, text=True, env=env, timeout=30, check=False)

        def put(self, name: str, text: str) -> None:
            (state / name).write_text(text)

    return Box()


IP_OUT = "2: eth0    inet 192.168.1.23/24 brd 192.168.1.255 scope global dynamic eth0\\       valid_lft 86000sec\n"


def test_the_script_is_installed_by_the_package_and_is_valid_bash() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, check=False).returncode == 0
    ebuild = next((ROOT / "packaging/gentoo-overlay/app-admin/gentoo-live-installer").glob("*.ebuild")).read_text()
    assert 'dobin "${FILESDIR}/gentoo-remote-access"' in ebuild
    # not behind the rescue condition: it belongs to the live disc's remote access
    install = ebuild.index('dobin "${FILESDIR}/gentoo-remote-access"')
    assert install > ebuild.index("src_install()") and "if [[" not in ebuild[ebuild.rindex("\n", 0, install - 200) : install]
    assert "net-misc/openssh" in ebuild and "sys-apps/iproute2" in ebuild


def test_status_shows_addresses_fingerprints_and_that_nobody_can_log_in_yet(box) -> None:
    box.put("ip", IP_OUT)
    (box.keys / "ssh_host_ed25519_key.pub").write_text("x")
    (box.keys / "ssh_host_rsa_key.pub").write_text("x")
    box.put("sshd-running", "")
    out = box.run("status")
    assert out.returncode == 0, out.stderr
    assert "sshd:      running" in out.stdout
    assert "ssh gentoo@192.168.1.23" in out.stdout
    assert out.stdout.startswith("remote login:  off (gentoo has no password, so nobody can log in yet")
    assert "ED25519  SHA256:abcED25519def" in out.stdout and "RSA  SHA256:abcRSAdef" in out.stdout
    assert box.run().stdout == out.stdout, "status is the default"
    assert not (box.state / "chpasswd").exists() and not (box.state / "passwd-calls").exists(), "status changes nothing"


def test_status_without_network_or_keys_explains_what_to_do(box) -> None:
    out = box.run("status")
    assert "not running" in out.stdout and "no network address yet" in out.stdout and "no host keys yet" in out.stdout


def test_on_sets_a_generated_readable_password_and_starts_sshd(box) -> None:
    box.put("ip", IP_OUT)
    result = box.run("on")
    assert result.returncode == 0, result.stderr
    written = (box.state / "chpasswd").read_text()
    assert re.fullmatch(r"gentoo:[a-z]+-[a-z]+-[a-z]+\d\d\n", written), written
    password = written.split(":", 1)[1].strip()
    assert f"password:  {password}\n" in result.stdout and result.stdout.count("password:") == 1, "the password once, alone on its line"
    assert "start" in (box.state / "rc-calls").read_text() and "sshd" in (box.state / "rc-calls").read_text()
    assert "ssh gentoo@192.168.1.23" in result.stdout and "sudo gentoo-remote-access off" in result.stdout
    # two runs make two different passwords
    box.run("on")
    passwords = {line for line in (box.state / "chpasswd").read_text().splitlines()}
    assert len(passwords) == 2


def test_on_with_a_password_uses_it_and_does_not_print_it(box) -> None:
    box.put("sshd-running", "")
    result = box.run("on", "my secret:pw")
    assert result.returncode == 0, result.stderr
    assert (box.state / "chpasswd").read_text() == "gentoo:my secret:pw\n"
    assert "my secret" not in result.stdout and "the one you typed" in result.stdout
    assert "start" not in (box.state / "rc-calls").read_text(), "a running sshd is left alone"


def test_on_and_off_need_administrator_rights(box) -> None:
    box.put("uid", "1000")
    for action in ("on", "off"):
        result = box.run(action)
        assert result.returncode == 1 and f"sudo gentoo-remote-access {action}" in result.stderr
    assert not (box.state / "chpasswd").exists() and not (box.state / "passwd-calls").exists()


def test_off_removes_the_password_and_keeps_sshd(box) -> None:
    box.put("sshd-running", "")
    result = box.run("off")
    assert result.returncode == 0
    assert (box.state / "passwd-calls").read_text() == "-d gentoo\n"
    assert not (box.state / "rc-calls").exists(), "sshd keeps running"
    assert result.stdout.startswith("remote login:  off") and "nobody can log in" in result.stdout


def test_pam_chatter_of_chpasswd_is_shown_only_when_it_fails(box) -> None:
    """passwdqc prints "You can now choose the new password" with a suggested password of its own, which is not the one set."""
    chatter = 'You can now choose the new password.\nyou can pick this as your password: "Somali5chap!Vault".\n'
    chpasswd = box.state.parent / "bin/chpasswd"
    chpasswd.write_text(f"#!/bin/sh\ncat >> {box.state}/chpasswd\nprintf '%s' '{chatter}'\n[ ! -e {box.state}/chpasswd-fails ]\n")
    result = box.run("on")
    assert result.returncode == 0 and "Somali5chap" not in result.stdout + result.stderr
    box.put("chpasswd-fails", "")
    result = box.run("on")
    assert result.returncode == 1 and "choose the new password" in result.stderr and "could not set the password" in result.stderr


def test_failures_are_reported(box) -> None:
    box.put("chpasswd-fails", "")
    assert box.run("on").returncode == 1
    (box.state / "chpasswd-fails").unlink()
    box.put("start-fails", "")
    result = box.run("on")
    assert result.returncode == 1 and "sshd could not be started" in result.stderr


def test_password_state_is_read_from_passwd(box) -> None:
    box.put("passwd-status", "gentoo P 2026-10-25 0 99999 7 -1\n")
    assert box.run("status").stdout.startswith("remote login:  on (gentoo has a password)")


def test_unknown_action_prints_usage(box) -> None:
    result = box.run("bogus")
    assert result.returncode == 2 and "Usage: gentoo-remote-access" in result.stderr
    assert box.run("--help").returncode == 0


# --- the files of the image ---------------------------------------------------------------------


def test_the_dropin_only_refuses_empty_passwords() -> None:
    lines = [line.split("#")[0].strip() for line in DROPIN.read_text().splitlines()]
    assert [line for line in lines if line] == ["PermitEmptyPasswords no"], "root login stays as on the official media"


def test_the_dropin_sorts_before_the_files_of_the_live_package_and_the_test_flavour() -> None:
    names = sorted([DROPIN.name, "9999999gentoo-livecd.conf", "9999999gentoo-pam.conf", "90-installer-vm-test.conf"])
    assert names[0] == DROPIN.name


def test_the_greetings_name_both_ways_to_log_in_from_another_computer() -> None:
    for name in ("motd", "issue"):
        text = (ROOT / "iso/root_overlay/etc" / name).read_text()
        assert "gentoo-remote-access" in text
    motd = (ROOT / "iso/root_overlay/etc/motd").read_text()
    assert "sudo passwd root" in motd and "ssh root@ADDRESS" in motd, "the Handbook's way"


def test_sshd_is_not_added_a_second_time_for_the_test_flavour() -> None:
    entry = (ROOT / "iso/builder-entrypoint.sh").read_text()
    body = entry.split("configure_test_access() {")[1].split("\n}\n")[0]
    assert "sshd|default" not in body and "livecd-stage2.spec" not in body
