"""The VM probes of administrator rights and leftover config files: what pkexec and Portage must show on the installed system."""
from __future__ import annotations

import subprocess

import vm_probes

from tests.hardware.builders import manifest

AS_ROOT = """\x1b[1;31m==== AUTHENTICATING FOR org.freedesktop.policykit.exec ====\x1b[0m\r
Authentication is needed to run `/usr/bin/id' as the super user\r
Authenticating as: root\r
Password: \r
polkit-agent-helper-1: pam_authenticate failed: Authentication failure\r
\x1b[1;31m==== AUTHENTICATION FAILED ====\x1b[0m\r
Error executing command as another user: Not authorized\r
"""
AS_USER = """\x1b[1;31m==== AUTHENTICATING FOR org.freedesktop.policykit.exec ====\x1b[0m\r
Authentication is needed to run `/usr/bin/id' as the super user\r
Authenticating as: Disposable VM test user (vmtest)\r
Password: \r
\x1b[1;31m==== AUTHENTICATION COMPLETE ====\x1b[0m\r
0\r
"""
WRONG_PASSWORD = AS_USER.replace("==== AUTHENTICATION COMPLETE ====", "==== AUTHENTICATION FAILED ====").replace("0\r\n", "Error executing command as another user: Not authorized\r\n")


def test_the_users_own_password_gets_root():
    assert vm_probes.parse_polkit_probe(0, AS_USER, manifest()).success


def test_a_dialog_that_wants_root_fails_even_when_it_would_be_answered():
    result = vm_probes.parse_polkit_probe(0, AS_ROOT, manifest())
    assert not result.success and "root's password" in result.error


def test_a_refused_password_fails():
    result = vm_probes.parse_polkit_probe(0, WRONG_PASSWORD, manifest())
    assert not result.success and "did not get root" in result.error


def test_a_system_without_polkit_has_nothing_to_pass():
    assert vm_probes.parse_polkit_probe(0, "no pkexec\n", manifest()).success


def test_su_and_a_user_outside_wheel_keep_root_in_the_dialog():
    assert vm_probes.parse_polkit_probe(0, AS_ROOT, manifest(accounts={"privilege": "su"})).success
    assert vm_probes.parse_polkit_probe(0, AS_ROOT, manifest(accounts={"groups": ["audio"]})).success


def test_a_failed_command_is_reported_as_failed():
    assert not vm_probes.parse_polkit_probe(255, "", manifest()).success


def test_the_probe_command_is_valid_shell_and_python_in_it():
    subprocess.run(["sh", "-n", "-c", vm_probes.POLKIT_COMMAND], check=True)
    code = vm_probes.POLKIT_COMMAND.split("python3 -c '", 1)[1].rsplit("'", 1)[0]
    compile(code, "polkit-probe", "exec")
    assert "'" not in code  # it stands in single quotes in the shell


def test_no_stock_file_may_wait_beside_the_installers():
    assert vm_probes.parse_config_proposals_probe(0, "\n").success
    result = vm_probes.parse_config_proposals_probe(0, "/etc/default/._cfg0000_grub\n/etc/conf.d/._cfg0000_zram-init\n")
    assert not result.success and "._cfg0000_grub" in result.error
