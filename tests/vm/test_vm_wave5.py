"""scripts/vm_wave5.py (interface I13): SSH on the installed system, remote access on the live medium, host keys, network_status, gentoo-chroot.

Every rule is judged on recorded or hand-written output of the real tools: no VM is started.
"""
from __future__ import annotations

import json

import pytest

import vm_wave5 as w5
from gentoo_installer.manifest import InstallManifest
from tests.vm.helpers import PUBLIC_KEY, load_vm_module

vm = load_vm_module()

SSHD_T_GENTOO = """port 22
addressfamily any
usepam yes
permitrootlogin no
pubkeyauthentication yes
passwordauthentication no
kbdinteractiveauthentication yes
permitemptypasswords no
listenaddress 0.0.0.0:22
listenaddress [::]:22
"""
SSHD_T_KEYS_ONLY = SSHD_T_GENTOO.replace("kbdinteractiveauthentication yes", "kbdinteractiveauthentication no")
SSHD_T_OLD_NAME = SSHD_T_GENTOO.replace("kbdinteractiveauthentication", "challengeresponseauthentication")


def manifest(**changes) -> InstallManifest:
    result = vm.build_manifest(vm.SCENARIO_MAP["openrc"], PUBLIC_KEY)
    for path, value in changes.items():
        section, key = path.split("__")
        setattr(getattr(result, section), key, value)
    return result


# --- sshd on the installed system -----------------------------------------------------------------------------------

OPENRC_OK = " * status: started\n---\n            sshd |      default\n         syslog-ng |      default\n"
SYSTEMD_OK = "active\n---\nenabled\n"


def test_openrc_sshd_started_and_in_the_default_runlevel_passes():
    assert w5.parse_sshd_service_probe(0, OPENRC_OK, "openrc").success


@pytest.mark.parametrize(("output", "fragment"), [
    (" * status: stopped\n---\n  sshd | default\n", "not started"),
    (" * status: crashed\n---\n  sshd | default\n", "crashed"),
    (" * status: started\n---\n  syslog-ng | default\n", "not in the default runlevel"),
    (" * status: started\n", "no second section"),
])
def test_openrc_sshd_problems_are_named(output, fragment):
    result = w5.parse_sshd_service_probe(0, output, "openrc")
    assert not result.success and fragment in result.error


def test_systemd_needs_active_and_enabled():
    assert w5.parse_sshd_service_probe(0, SYSTEMD_OK, "systemd").success
    result = w5.parse_sshd_service_probe(3, "inactive\n---\ndisabled\n", "systemd")
    assert not result.success and "not active" in result.error and "not enabled" in result.error


def test_the_service_command_matches_the_init_system():
    assert "rc-service sshd status" in w5.sshd_service_command("openrc") and "rc-update show default" in w5.sshd_service_command("openrc")
    assert "systemctl is-active sshd" in w5.sshd_service_command("systemd") and "is-enabled sshd" in w5.sshd_service_command("systemd")


def test_sshd_t_is_parsed_with_the_first_value_of_a_keyword():
    settings = w5.sshd_settings(SSHD_T_GENTOO)
    assert settings["permitrootlogin"] == "no" and settings["listenaddress"] == "0.0.0.0:22" and settings["port"] == "22"


def test_the_default_manifest_wants_root_login_off_and_password_login_through_keyboard_interactive():
    result = w5.parse_sshd_config_probe(0, SSHD_T_GENTOO, manifest())
    assert result.success, result.error
    assert "permitrootlogin=no" in result.output


def test_the_old_name_of_the_keyboard_interactive_setting_still_counts():
    assert w5.parse_sshd_config_probe(0, SSHD_T_OLD_NAME, manifest()).success


def test_a_keys_only_manifest_needs_password_login_off():
    keys_only = manifest(accounts__ssh_password_login=False)
    assert w5.parse_sshd_config_probe(0, SSHD_T_KEYS_ONLY, keys_only).success
    result = w5.parse_sshd_config_probe(0, SSHD_T_GENTOO, keys_only)
    assert not result.success and "password login is possible" in result.error and "kbdinteractiveauthentication" in result.error


def test_password_login_that_is_asked_for_but_impossible_fails():
    result = w5.parse_sshd_config_probe(0, SSHD_T_KEYS_ONLY, manifest())
    assert not result.success and "impossible" in result.error


def test_keyboard_interactive_without_pam_is_not_a_password_login():
    no_pam = SSHD_T_GENTOO.replace("usepam yes", "usepam no")
    assert not w5.password_login_possible(w5.sshd_settings(no_pam))


@pytest.mark.parametrize(("old", "new", "fragment"), [
    ("permitrootlogin no", "permitrootlogin prohibit-password", "permitrootlogin is 'prohibit-password'"),
    ("permitemptypasswords no", "permitemptypasswords yes", "permitemptypasswords"),
    ("pubkeyauthentication yes", "pubkeyauthentication no", "pubkeyauthentication"),
])
def test_unsafe_settings_fail(old, new, fragment):
    result = w5.parse_sshd_config_probe(0, SSHD_T_GENTOO.replace(old, new), manifest())
    assert not result.success and fragment in result.error


def test_empty_output_and_a_failed_sshd_t_are_failures_and_a_sudo_problem_is_advisory():
    assert not w5.parse_sshd_config_probe(0, "", manifest()).success
    assert "status 1" in w5.parse_sshd_config_probe(1, "sshd: no hostkeys available", manifest()).error
    sudo = w5.parse_sshd_config_probe(1, "sudo: a password is required", manifest())
    assert sudo.advisory and not sudo.success


def test_the_probes_are_listed_for_a_manifest_with_ssh_and_not_without():
    names = [probe[0] for probe in w5.sshd_probes(manifest(), "openrc", b"pw\n")]
    assert names == ["sshd-service", "sshd-config"]
    assert w5.sshd_probes(manifest(options__ssh=False), "openrc", b"pw\n") == []


def test_the_installed_system_probes_of_the_harness_include_sshd():
    scenario = vm.SCENARIO_MAP["systemd"]
    probes = vm.installed_system_probes(vm.build_manifest(scenario, PUBLIC_KEY), scenario)
    by_name = {probe[0]: probe for probe in probes}
    assert "sshd-service" in by_name and "sshd-config" in by_name
    assert "systemctl is-active sshd" in by_name["sshd-service"][1]
    assert by_name["sshd-config"][3] == (vm.TEST_PASSWORD + "\n").encode(), "sshd -T needs root: the sudo password goes on stdin"


# --- remote access on the live medium -------------------------------------------------------------------------------

STATUS_OFF = """Remote access (SSH): disabled
sshd is running, but nobody can log in over the network until you turn this on.
Turn it on with:  gentoo-remote-access on
"""
STATUS_ON = """Remote access (SSH): enabled
Log in from another computer with:
  ssh gentoo@10.0.2.15
Password: maple-river-sun42
Key fingerprints (compare them when ssh asks):
  ED25519 SHA256:q2VnT5zTq0u8Yx2Jr3m9L1uXk0pQw8cF7aD4eB6hGzs
  RSA     SHA256:Zk9Qm2vXc7Lw1Rt5Yp0aBn8Hd3JsUe4FgTi6OyCxMv
"""
STATUS_OFF_AGAIN = "Remote access (SSH): disabled\nThe password of gentoo was removed again.\n"


def test_remote_access_state_user_address_password_and_fingerprints_are_read_from_the_text():
    access = w5.parse_remote_access(STATUS_ON)
    assert access.state == "enabled" and access.user == "gentoo" and access.addresses == ("10.0.2.15",)
    assert access.password == "maple-river-sun42"
    assert [kind for kind, _digest in access.fingerprints] == ["ED25519", "RSA"]
    assert all(digest.startswith("SHA256:") for _kind, digest in access.fingerprints)
    assert w5.parse_remote_access(STATUS_OFF).state == "disabled"
    assert w5.parse_remote_access(STATUS_OFF).password == ""


@pytest.mark.parametrize(("text", "state"), [
    ("Remote access is now on.\n", "enabled"),
    ("SSH access is turned off.\n", "disabled"),
    ("Remote access: ON\n", "enabled"),
    ("hello\n", "unknown"),
    ("", "unknown"),
])
def test_remote_access_tolerates_other_wordings_and_admits_when_it_cannot_tell(text, state):
    assert w5.parse_remote_access(text).state == state


class FakeLive:
    """The live medium as the flow sees it: ``run`` for root commands and ``login`` for the host's password login."""

    def __init__(self, *, on_works=True, off_works=True, sshd_started=True, status_first=STATUS_OFF, off_output=STATUS_OFF_AGAIN,
                 refuse_text="Permission denied, please try again."):
        self.enabled = False
        self.on_works, self.off_works, self.sshd_started, self.status_first = on_works, off_works, sshd_started, status_first
        self.off_output, self.refuse_text = off_output, refuse_text
        self.log: list[str] = []
        self.first_status = True

    def run(self, command: str) -> tuple[int, str]:
        self.log.append(command)
        if command == "rc-service sshd status":
            return (0, " * status: started\n") if self.sshd_started else (3, " * status: stopped\n")
        if command.endswith(" status"):
            if self.first_status:
                self.first_status = False
                return 0, self.status_first
            return 0, STATUS_ON if self.enabled else STATUS_OFF_AGAIN
        if command.endswith(" on"):
            self.enabled = self.on_works
            return 0, STATUS_ON
        if command.endswith(" off"):
            self.enabled = self.enabled and not self.off_works
            return 0, self.off_output
        raise AssertionError(command)

    def login(self, password: str) -> tuple[int, str]:
        assert password == "maple-river-sun42"
        if self.enabled:
            return 0, w5.LIVE_LOGIN_OK + "\n"
        return 255, self.refuse_text


def flow(live: FakeLive) -> w5.RemoteAccessReport:
    return w5.live_remote_access_flow(live.run, live.login)


def test_the_whole_remote_access_flow_passes_on_a_medium_that_behaves():
    report = flow(FakeLive())
    assert report.success, report.problems
    assert [step["step"] for step in report.steps][:3] == ["sshd runs at boot", "status says remote access is off at boot",
                                                              "on enables remote access and prints the password"]
    assert "maple-river-sun42" not in json.dumps(report.to_dict()), "the password never goes into the record"


def test_a_medium_whose_remote_access_is_already_on_at_boot_fails():
    report = flow(FakeLive(status_first=STATUS_ON))
    assert not report.success and any("off at boot" in problem for problem in report.problems)


def test_a_medium_that_never_starts_sshd_fails():
    report = flow(FakeLive(sshd_started=False))
    assert any("sshd runs at boot" in problem for problem in report.problems)


def test_on_that_does_not_open_the_login_fails():
    report = flow(FakeLive(on_works=False))
    assert any("a password login as gentoo works after on" in problem for problem in report.problems)


def test_off_that_leaves_the_password_working_fails():
    report = flow(FakeLive(off_works=False))
    assert any("refused after off" in problem and "still works" in problem for problem in report.problems)


def test_a_failed_login_that_is_not_a_refusal_proves_nothing():
    report = flow(FakeLive(refuse_text="ssh: connect to host 127.0.0.1 port 2222: Connection refused"))
    assert any("inconclusive" in problem for problem in report.problems)


def test_on_without_a_password_stops_the_flow_with_one_clear_problem():
    class NoPassword(FakeLive):
        def run(self, command):
            return (0, "Remote access (SSH): enabled\nssh gentoo@10.0.2.15\n") if command.endswith(" on") else super().run(command)

    report = flow(NoPassword())
    assert any("prints the password" in problem and "missing" in problem for problem in report.problems)
    assert len(report.steps) == 3


def test_the_password_login_never_offers_the_harness_key_and_never_puts_the_password_in_the_arguments():
    argv = w5.password_login_arguments("ssh", "127.0.0.1", 2222, "gentoo", "/run/known_hosts.live")
    joined = " ".join(argv)
    assert "PubkeyAuthentication=no" in joined and "PreferredAuthentications=password,keyboard-interactive" in joined
    assert "-i" not in argv and "BatchMode=no" in joined and argv[-3] == "gentoo@127.0.0.1"
    assert "maple" not in joined
    env = w5.password_login_environment("/run/askpass.sh")
    assert env["SSH_ASKPASS_REQUIRE"] == "force" and env["SSH_ASKPASS"] == "/run/askpass.sh"
    script = w5.askpass_script("pa ss'word")
    assert script.startswith("#!/bin/sh\n") and "printf '%s\\n' 'pa ss'\"'\"'word'" in script


def test_login_verdicts_need_the_computed_marker_not_just_exit_zero():
    assert w5.login_succeeded(0, "LIVE-LOGIN-42-gentoo\n")
    assert not w5.login_succeeded(0, "")
    assert not w5.login_succeeded(0, "echo LIVE-LOGIN-$((6*7))-$(id -un)")
    assert w5.login_was_refused(255, "gentoo@127.0.0.1: Permission denied (publickey,password).")
    assert not w5.login_was_refused(255, "Connection timed out")


# --- host keys ----------------------------------------------------------------------------------------------------

KEYGEN_A = """256 SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA root@livecd (ECDSA)
256 SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB root@livecd (ED25519)
3072 SHA256:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC root@livecd (RSA)
"""
KEYGEN_B = KEYGEN_A.replace("AAAA", "aaaa").replace("BBBB", "bbbb").replace("CCCC", "cccc")


def test_host_keys_are_read_per_type():
    keys = w5.parse_host_keys(0, KEYGEN_A)
    assert sorted(keys) == ["ECDSA", "ED25519", "RSA"] and keys["ED25519"].startswith("SHA256:B")
    assert w5.parse_host_keys(1, KEYGEN_A) == {} and w5.parse_host_keys(0, "ssh-keygen: no such file") == {}


def test_two_boots_with_different_keys_pass():
    assert w5.host_key_problems([w5.parse_host_keys(0, KEYGEN_A), w5.parse_host_keys(0, KEYGEN_B)]) == []


def test_a_key_that_is_the_same_in_two_boots_is_reported_by_type():
    same_ed25519 = [line for line in KEYGEN_A.splitlines() if "ED25519" in line][0]
    partly = "\n".join(same_ed25519 if "ED25519" in line else line for line in KEYGEN_B.splitlines()) + "\n"
    problems = w5.host_key_problems([w5.parse_host_keys(0, KEYGEN_A), w5.parse_host_keys(0, partly)])
    assert len(problems) == 1 and "ED25519" in problems[0] and "part of the image" in problems[0]


def test_a_boot_without_host_keys_is_a_problem():
    assert "no SSH host keys" in w5.host_key_problems([w5.parse_host_keys(0, KEYGEN_A), {}])[0]


# --- network_status -----------------------------------------------------------------------------------------------

def status_line(**changes) -> str:
    status = {"online": True, "mirror": True, "default_route": True, "dns": True, "wifi_present": False, "wifi_enabled": False, "rfkill_hard": False,
              "devices": [{"name": "ens3", "kind": "ethernet", "state": "connected", "driver": "virtio_net", "connection": "Wired connection 1",
                           "ipv4": ["10.0.2.15/24"], "ipv6": []}], "problem": "", "message": ""}
    status.update(changes)
    return json.dumps({"events": [{"type": "result", "data": status, "version": 1}]})


def test_a_wired_vm_with_working_networking_passes():
    result = w5.parse_network_status_probe(0, status_line())
    assert result.success and result.data["status"]["online"] is True
    assert json.loads(result.output)["problem"] == ""


@pytest.mark.parametrize(("changes", "fragment"), [
    ({"online": False}, "online is False"),
    ({"default_route": False, "problem": "no_route", "message": "No route."}, "default_route"),
    ({"dns": False, "problem": "no_dns"}, "dns is False"),
    ({"mirror": False}, "mirror"),
    ({"devices": []}, "no network devices"),
    ({"devices": [{"name": "wlan0", "kind": "wifi", "state": "connected"}]}, "no connected ethernet"),
    ({"problem": "cable", "message": "Plug in the cable."}, "problem 'cable'"),
])
def test_network_problems_are_named(changes, fragment):
    result = w5.parse_network_status_probe(0, status_line(**changes))
    assert not result.success and fragment in result.error and not result.advisory


def test_an_unknown_mirror_is_not_a_problem():
    assert w5.parse_network_status_probe(0, status_line(mirror=None)).success


def test_a_daemon_without_network_actions_is_advisory():
    error = {"type": "error", "error": "unsupported action", "data": {"code": "proto.unsupported_action"}}
    result = w5.parse_network_status_probe(0, json.dumps({"events": [error]}))
    assert result.advisory and not result.success and "network.v1" in result.error


def test_no_answer_at_all_is_a_failure_not_an_advisory():
    result = w5.parse_network_status_probe(1, json.dumps({"events": [{"type": "harness-error", "error": "[Errno 2] No such file or directory"}]}))
    assert not result.success and not result.advisory and "No such file" in result.error
    assert not w5.parse_network_status_probe(1, "Traceback ...").success


def test_the_snippet_talks_the_daemons_wire_format_and_compiles():
    compile(w5.NETWORK_STATUS_SNIPPET, "snippet", "exec")
    assert "'action': 'network_status'" in w5.NETWORK_STATUS_SNIPPET and "'version': 1" in w5.NETWORK_STATUS_SNIPPET
    assert "/run/gentoo-installer/server.sock" in w5.network_status_command()


# --- gentoo-chroot -------------------------------------------------------------------------------------------------

CHROOT_OK = """Found 1 Gentoo system: Gentoo Linux on /dev/vda2 (OpenRC)
Changes are real. Open it read-write? [y/N]
Mounted /dev/vda2 on /run/gentoo-rescue/s1
Common fixes: emerge --sync ...   Type exit to leave; the system is unmounted for you.
(chroot) CHROOT-FOUND-42
(chroot) CHROOT-RELEASE-Gentoo Base System release 2.18
(chroot) CHROOT-DNS-42
(chroot) CHROOT-EMERGE-42
(chroot) CHROOT-GRUB-42
(chroot) Unmounted.
"""


def test_the_chroot_input_answers_the_question_runs_the_commands_and_exits():
    text = w5.chroot_input("grub")
    lines = text.splitlines()
    assert lines[0] == "y" and lines[-1] == "exit" and text.endswith("\n")
    assert any("emerge --info" in line for line in lines) and any("grub-mkconfig" in line for line in lines)
    assert not any("grub-mkconfig" in line for line in w5.chroot_input("systemd-boot").splitlines())
    assert w5.chroot_input("grub", writable=False).splitlines()[0] != "y"
    assert w5.chroot_command() == "gentoo-chroot" and w5.chroot_command(writable=False) == "gentoo-chroot --read-only"


def test_a_session_that_found_mounted_resolved_and_ended_passes():
    assert w5.chroot_problems(0, CHROOT_OK, "grub") == []


def test_the_echo_of_a_command_is_not_its_output():
    echoed = w5.chroot_input("grub")
    assert w5.chroot_problems(0, echoed, "grub"), "the typed commands alone must not satisfy the checks"


@pytest.mark.parametrize(("remove", "fragment"), [
    ("CHROOT-DNS-42", "DNS does not work"),
    ("CHROOT-EMERGE-42", "emerge --info failed"),
    ("CHROOT-GRUB-42", "grub-mkconfig failed"),
])
def test_each_failed_command_is_named(remove, fragment):
    problems = w5.chroot_problems(0, CHROOT_OK.replace(remove, "nothing"), "grub")
    assert any(fragment in problem for problem in problems)


def test_a_system_that_was_not_found_is_reported_as_that():
    problems = w5.chroot_problems(1, "No Gentoo systems found.\n", "grub")
    assert any("did not find the installed system" in problem for problem in problems) and any("status 1" in problem for problem in problems)


def test_a_shell_in_the_wrong_system_is_caught_by_the_release_file():
    output = CHROOT_OK.replace("CHROOT-RELEASE-Gentoo Base System release 2.18\n", "")
    assert any("gentoo-release" in problem for problem in w5.chroot_problems(0, output, "grub"))


def test_other_bootloaders_do_not_need_grub():
    output = CHROOT_OK.replace("(chroot) CHROOT-GRUB-42\n", "")
    assert w5.chroot_problems(0, output, "systemd-boot") == []
