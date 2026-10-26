"""Interface I13 end to end against the fake lab: the new scenarios run the harness's real orchestration against a scripted guest."""
from __future__ import annotations

import json

import pytest

from tests.vm.fake_env import EVENT_STREAM, Lab, guest_rules
from tests.vm.helpers import PUBLIC_KEY, load_vm_module
from tests.vm.test_vm_wave5 import CHROOT_OK, KEYGEN_A, KEYGEN_B, STATUS_OFF, STATUS_ON, STATUS_OFF_AGAIN

vm = load_vm_module()
pytestmark = pytest.mark.slow  # real processes and sockets

PASSWORD = "maple-river-sun42"
CLEAN = json.dumps({"mounts": [], "sessions": [], "read_only": [], "dm": []}) + "\n"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return Lab(vm, tmp_path, monkeypatch)


def live_rules(*, keys_second=KEYGEN_B, off_closes=True, login_refusal="gentoo@127.0.0.1: Permission denied (publickey,password).") -> list[dict]:
    """A live medium that behaves as interface I9 says (the first rules win)."""
    rules = [
        {"user": "^root$", "match": "live_inspection", "inspection": {}},
        {"user": "^root$", "match": r"^rc-service sshd status$", "stdout": " * status: started\n"},
        {"user": "^root$", "match": r"^gentoo-remote-access on$", "touch": ["remote-on"], "stdout": STATUS_ON},
        {"user": "^root$", "match": r"^gentoo-remote-access off$", "remove": ["remote-on"] if off_closes else [], "stdout": STATUS_OFF_AGAIN},
        {"user": "^root$", "match": r"^gentoo-remote-access status$", "requires": "remote-on", "stdout": STATUS_ON},
        {"user": "^root$", "match": r"^gentoo-remote-access status$", "stdout": STATUS_OFF},
        {"user": "^gentoo$", "match": r"LIVE-LOGIN", "requires": "remote-on", "stdout": "LIVE-LOGIN-42-gentoo\n"},
        {"user": "^gentoo$", "match": r"LIVE-LOGIN", "rc": 255, "stderr": login_refusal + "\n"},
        {"user": "^root$", "match": r"ssh_host_\*_key\.pub", "times": 1, "stdout": KEYGEN_A},
        {"user": "^root$", "match": r"ssh_host_\*_key\.pub", "stdout": keys_second},
    ]
    return rules + guest_rules(vm, "live-remote-access", guard="vm_disk_guard")


def test_the_live_remote_access_scenario_passes_and_records_what_it_proved(lab):
    lab.rules = live_rules()
    assert lab.run("--scenario", "live-remote-access") == 0, lab.result().get("error")
    run = lab.run_dir()
    live = lab.result()["live"]
    assert live["problems"] == []
    assert live["remote_access"]["success"] is True
    assert [step["step"] for step in live["remote_access"]["steps"]][:2] == ["sshd runs at boot", "status says remote access is off at boot"]
    assert live["host_keys"]["problems"] == [] and live["host_keys"]["boot_1"]["ED25519"] != live["host_keys"]["boot_2"]["ED25519"]
    assert len(live["network_status"]) == 2 and all(item["success"] for item in live["network_status"]), "network_status ran in both boots"
    assert PASSWORD not in (run / "result.json").read_text() and PASSWORD not in (run / "live-remote-access.json").read_text()
    assert not (run / "askpass.sh").exists(), "the askpass helper is deleted as soon as the login is over"
    logins = [call for call in lab.ssh_calls() if call["user"] == "gentoo"]
    assert len(logins) == 2 and all(call["options"].get("PubkeyAuthentication") == "no" for call in logins)
    assert not any(PASSWORD in json.dumps(call) for call in lab.ssh_calls()), "the password is never on a command line"


def test_a_host_key_that_survives_a_reboot_fails_the_run(lab):
    lab.rules = live_rules(keys_second=KEYGEN_A)
    assert lab.run("--scenario", "live-remote-access") == 1
    assert "part of the image" in lab.result()["error"]


def test_a_password_that_still_works_after_off_fails_the_run(lab):
    lab.rules = live_rules(off_closes=False)
    assert lab.run("--scenario", "live-remote-access") == 1
    assert "refused after off" in lab.result()["error"] and "still works" in lab.result()["error"]


def test_a_refusal_that_is_really_a_network_failure_is_inconclusive(lab):
    lab.rules = live_rules(login_refusal="ssh: connect to host 127.0.0.1 port 2222: Connection refused")
    assert lab.run("--scenario", "live-remote-access") == 1
    assert "inconclusive" in lab.result()["error"] or "after on" in lab.result()["error"]


def test_the_network_status_of_every_live_boot_is_judged(lab):
    broken = {"user": "^root$", "match": "network_status",
              "stdout": json.dumps({"events": [{"type": "result", "data": {"online": False, "default_route": False, "dns": False, "problem": "cable",
                                                                           "message": "Plug in the network cable.", "devices": []}}]}) + "\n"}
    lab.rules = [broken, *live_rules()]
    assert lab.run("--scenario", "live-remote-access") == 1
    assert "network_status in the live system" in lab.result()["error"] and "cable" in lab.result()["error"]


def test_a_daemon_without_network_actions_is_a_warning_not_a_failure(lab, capsys):
    unsupported = {"user": "^root$", "match": "network_status",
                   "stdout": json.dumps({"events": [{"type": "error", "error": "unsupported", "data": {"code": "proto.unsupported_action"}}]}) + "\n"}
    lab.rules = [unsupported, *live_rules()]
    assert lab.run("--scenario", "live-remote-access") == 0
    assert "no network_status action" in capsys.readouterr().err
    assert lab.result()["live"]["network_status"][0]["advisory"] is True


def test_every_installed_system_is_asked_about_sshd(lab):
    lab.rules = guest_rules(vm, "openrc", sudo=True)
    assert lab.run("--scenario", "openrc") == 0
    names = {probe["name"]: probe for probe in lab.result()["probes"]}
    assert names["sshd-service"]["success"] and names["sshd-config"]["success"]
    assert "permitrootlogin=no" in names["sshd-config"]["output"]


def test_a_keys_only_install_must_have_password_login_off_in_sshd(lab):
    lab.rules = guest_rules(vm, "ssh-keys-only", sudo=True)
    assert lab.run("--scenario", "ssh-keys-only") == 0
    manifest = json.loads((lab.run_dir() / "manifest.json").read_text())
    assert manifest["accounts"]["ssh_password_login"] is False
    assert "kbdinteractiveauthentication=no" in {p["name"]: p for p in lab.result()["probes"]}["sshd-config"]["output"]


def test_an_sshd_that_still_takes_passwords_fails_a_keys_only_install(lab):
    leaky = {"user": "^vmtest$", "match": r"sshd -T$", "stdout": "usepam yes\npermitrootlogin no\npasswordauthentication yes\npermitemptypasswords no\n"}
    lab.rules = [leaky, *guest_rules(vm, "ssh-keys-only", sudo=True)]
    assert lab.run("--scenario", "ssh-keys-only") == 1
    assert "sshd-config" in lab.result()["error"] and "password login is possible" in lab.result()["error"]


def chroot_rules(output: str = CHROOT_OK, *, writable_output: str | None = None, clean: str = CLEAN, **kwargs) -> list[dict]:
    """Two sessions: ``gentoo-chroot --read-only`` (no CHROOT-WRITE line, none is asked for) and ``gentoo-chroot --writable`` (writes)."""
    read_only = output.replace("(chroot) CHROOT-WRITE-42\n", "")
    return [{"user": "^root$", "match": r"^gentoo-chroot --read-only$", "stdin_to": "chroot-input-read-only.txt", "stdout": read_only},
            {"user": "^root$", "match": r"^gentoo-chroot --writable$", "stdin_to": "chroot-input-writable.txt", "stdout": writable_output or output},
            {"user": "^root$", "match": "HARNESS_FS_ROOT", "stdout": clean}] + guest_rules(vm, "rescue-chroot", sudo=True, **kwargs)


def test_the_rescue_chroot_scenario_runs_both_sessions_with_commands_on_standard_input_and_checks_nothing_stays_mounted(lab):
    lab.rules = chroot_rules()
    assert lab.run("--scenario", "rescue-chroot") == 0, lab.result().get("error")
    run = lab.run_dir()
    sessions = lab.result()["chroot"]["sessions"]
    assert [(item["mode"], item["problems"], item["exit"]) for item in sessions] == [("read-only", [], 0), ("writable", [], 0)]
    typed = (run / "fake-guest" / "chroot-input-writable.txt").read_text()
    assert typed.splitlines()[0] != "y" and typed.rstrip().endswith("exit") and "emerge --info" in typed and "grub-mkconfig" in typed and "touch /root/" in typed
    read_only = (run / "fake-guest" / "chroot-input-read-only.txt").read_text()
    assert not read_only.startswith("y") and "touch" not in read_only


def test_a_chroot_without_dns_fails_the_scenario(lab):
    lab.rules = chroot_rules(CHROOT_OK.replace("CHROOT-DNS-42", "nothing"))
    assert lab.run("--scenario", "rescue-chroot") == 1
    assert "read-only: DNS does not work inside the chroot" in lab.result()["error"]


def test_a_writable_session_that_cannot_write_fails_the_scenario(lab):
    lab.rules = chroot_rules(CHROOT_OK, writable_output=CHROOT_OK.replace("CHROOT-WRITE-42", "touch: cannot touch: Read-only file system"))
    assert lab.run("--scenario", "rescue-chroot") == 1
    error = lab.result()["error"]
    assert "writable: the system was opened read-only" in error and "read-only: " not in error


def test_a_chroot_that_leaves_the_system_mounted_fails_the_scenario(lab):
    lab.rules = chroot_rules(clean=json.dumps({"mounts": ["/mnt/rescue/proc"], "sessions": [], "read_only": [], "dm": []}) + "\n")
    assert lab.run("--scenario", "rescue-chroot") == 1
    assert "still mounted: /mnt/rescue/proc" in lab.result()["error"]


def test_the_network_answer_is_asked_again_while_the_daemon_and_dhcp_come_up(lab):
    """sshd answers before the daemon has made its socket: the first answers are socket errors, then the real one. The run must pass."""
    not_yet = {"user": "^root$", "match": "network_status", "times": 3,
               "stdout": json.dumps({"events": [{"type": "harness-error", "error": "[Errno 2] No such file or directory"}]}) + "\n"}
    lab.rules = [not_yet, *live_rules()]
    assert lab.run("--scenario", "live-remote-access") == 0, lab.result().get("error")
    records = lab.result()["live"]["network_status"]
    assert records[0]["success"] is True and records[0]["attempts"] == 4 and records[1]["attempts"] == 1


def test_the_new_scenarios_refuse_combinations_that_cannot_work(lab, capsys):
    import dataclasses

    luks_chroot = dataclasses.replace(vm.SCENARIO_MAP["rescue-chroot"], luks=True)
    assert any("passphrase" in problem for problem in vm.hardware_problems(luks_chroot, None))
    lonely = dataclasses.replace(vm.SCENARIO_MAP["openrc"], live_checks=True)
    assert any("live ISO only" in problem for problem in vm.hardware_problems(lonely, None))
    assert vm.hardware_problems(vm.SCENARIO_MAP["live-remote-access"], None) == []
    assert vm.hardware_problems(vm.SCENARIO_MAP["rescue-chroot"], None) == []


def test_the_new_scenarios_are_manifest_correct():
    keys_only = vm.build_manifest(vm.SCENARIO_MAP["ssh-keys-only"], PUBLIC_KEY)
    assert keys_only.accounts.ssh_password_login is False and keys_only.options.ssh is True and keys_only.accounts.ssh_authorized_keys
    assert vm.build_manifest(vm.SCENARIO_MAP["openrc"], PUBLIC_KEY).accounts.ssh_password_login is True
    assert not vm.SCENARIO_MAP["live-remote-access"].in_all and not vm.SCENARIO_MAP["rescue-chroot"].in_all
    assert EVENT_STREAM  # the shared fixture stays importable from this module's neighbours
