"""The Wave 6 scenarios end to end against the fake lab: the harness's own orchestration (boot entry, keyboard, port forward, upload, result files)
with a scripted guest. The checks themselves are tested in test_vm_wave6.py."""
from __future__ import annotations

import argparse
import json
import types

import pytest

from tests.vm.fake_env import Lab, guest_rules
from tests.vm.helpers import PUBLIC_KEY, load_vm_module
from tests.vm.iso_fixture import write_iso
from tests.vm.stick import PASSWORD, SHARE_PASSWORD, Stick

vm = load_vm_module()
pytestmark = pytest.mark.slow  # real processes and sockets

MENU = """\
set default=0
menuentry 'Gentoo installer and rescue desktop' {
\tlinux /boot/gentoo quiet root=live:CDLABEL=LIVE cdroot
\tinitrd /boot/gentoo.igz
}
menuentry 'Repair a system (text console)' {
\tlinux /boot/gentoo quiet root=live:CDLABEL=LIVE cdroot nox gentoo.autostart=rescue
\tinitrd /boot/gentoo.igz
}
menuentry 'AI rescue (text console)' {
\tlinux /boot/gentoo quiet root=live:CDLABEL=LIVE cdroot nox gentoo.autostart=ai-rescue
\tinitrd /boot/gentoo.igz
}
"""
AI_CMDLINE = "BOOT_IMAGE=/boot/gentoo quiet root=live:CDLABEL=LIVE cdroot nox gentoo.autostart=ai-rescue"
CLEAN = json.dumps({"mounts": [], "sessions": [], "read_only": [], "dm": []}) + "\n"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    lab = Lab(vm, tmp_path, monkeypatch)
    monkeypatch.setattr(vm.ScenarioRunner, "AI_LAUNCHER_SECONDS", 3)
    monkeypatch.setattr(vm.ScenarioRunner, "AI_LAUNCHER_SETTLE", 0.1)
    monkeypatch.setattr(vm.ScenarioRunner, "AI_SLEEP_SCALE", 0.001)
    write_iso(lab.iso, {"/boot/grub/grub.cfg": MENU.encode(), "/boot/gentoo": b"KERNEL", "/boot/gentoo.igz": b"INITRD"})
    return lab


def rules(scenario: str, **extra) -> list[dict]:
    root = {"user": "^root$"}
    return [*extra.get("rules", []),
            {**root, "match": r"^cat /proc/cmdline$", "stdout": AI_CMDLINE + "\n"},
            {**root, "match": r"import base64, json, os, pwd, sys", "stdin_to": "agents-upload.json"},
            {**root, "match": r"HARNESS_FS_ROOT", "stdout": CLEAN}] + guest_rules(vm, scenario)


def use_stick(monkeypatch, stick: Stick):
    """The checks talk to a scripted stick; everything around them (QEMU, the boot entry, the port forward, the upload) is the real orchestration."""
    monkeypatch.setattr(vm.ScenarioRunner, "ai_hands", lambda self, live: stick.hands())
    monkeypatch.setattr(vm.ScenarioRunner, "share_fetch", staticmethod(stick.fetch))


def test_the_live_scenario_boots_the_ai_entry_runs_every_check_and_records_them(lab, monkeypatch):
    stick = Stick()
    use_stick(monkeypatch, stick)
    lab.rules = rules("ai-rescue-live")
    assert lab.run("--scenario", "ai-rescue-live") == 0, lab.result().get("error")
    run, result = lab.run_dir(), lab.result()
    assert result["boot_entry"]["entry"] == "AI rescue (text console)" and "gentoo.autostart=ai-rescue" in result["boot_entry"]["arguments"]
    record = result["ai_rescue"]
    assert record["mount"] is False and record["launcher"] == {"typed": ["<Enter>", "n"], "session": True}
    assert set(record["flows"]) == {"do", "share", "agents"} and all(flow["success"] for flow in record["flows"].values())
    assert [probe["name"] for probe in record["probes"]] == ["approver", "yama", "ai-identity", "ai-consoles", "ai-session", "ai-cwd"]
    assert (run / "ai-rescue.json").is_file()
    text = (run / "result.json").read_text() + (run / "ai-rescue.json").read_text()
    assert PASSWORD not in text and SHARE_PASSWORD not in text
    forwards = [json.loads(line) for line in (run / "fake-qmp-hmp.jsonl").read_text().splitlines()]
    assert [item["command-line"].split()[0] for item in forwards] == ["hostfwd_add", "hostfwd_add", "hostfwd_remove", "hostfwd_remove"]
    assert all("tcp:127.0.0.1:" in item["command-line"] and "-10.0.2.15:23456" in item["command-line"] for item in forwards if item["command-line"].startswith("hostfwd_add"))
    upload = json.loads((run / "fake-guest" / "agents-upload.json").read_text())
    assert upload["pins_path"] == "/tmp/gentoo-vm-agent-pins.json" and set(upload["pins"]) >= {"claude", "codex", "opencode"}


def test_a_stick_that_breaks_a_rule_fails_the_run_naming_every_broken_rule(lab, monkeypatch):
    use_stick(monkeypatch, Stick(groups="rescue-ai tty", share_runs_as="root"))
    lab.rules = rules("ai-rescue-live")
    assert lab.run("--scenario", "ai-rescue-live") == 1
    error = lab.result()["error"]
    assert "The AI rescue stick does not behave as designed" in error
    assert "ai-identity" in error and "tty" in error and "ttyd runs as root" in error
    assert lab.result()["ai_rescue"]["flows"]["share"]["success"] is False and lab.result()["ai_rescue"]["flows"]["do"]["success"] is True


def test_the_full_scenario_installs_first_then_checks_gentoo_rescue_mount(lab, monkeypatch):
    use_stick(monkeypatch, Stick())
    lab.rules = rules("ai-rescue")
    assert lab.run("--scenario", "ai-rescue") == 0, lab.result().get("error")
    record = lab.result()["ai_rescue"]
    assert record["mount"] is True and set(record["flows"]) == {"do", "share", "agents", "mount"}
    assert record["flows"]["mount"]["success"] and record["flows"]["mount"]["leftovers"] == []
    assert lab.result()["install"] is not None


def test_a_mount_that_leaves_something_behind_fails_the_run(lab, monkeypatch):
    use_stick(monkeypatch, Stick())
    dirty = json.dumps({"mounts": ["/mnt/rescue/r1a2b3"], "sessions": [], "read_only": [], "dm": []}) + "\n"
    lab.rules = rules("ai-rescue", rules=[{"user": "^root$", "match": r"HARNESS_FS_ROOT", "stdout": dirty}])
    assert lab.run("--scenario", "ai-rescue") == 1
    assert "after --unmount, still mounted: /mnt/rescue/r1a2b3" in lab.result()["error"]


def test_a_launcher_that_never_opens_the_session_is_reported_with_the_keys_typed_on_tty1(lab):
    lab.rules = rules("ai-rescue-live", rules=[{"user": "^root$", "match": r"tmux has-session", "rc": 1, "stdout": "no server running\n"}])
    assert lab.run("--scenario", "ai-rescue-live") == 1
    error = lab.result()["error"]
    assert "did not open the tmux session 'rescue' as rescue-ai" in error and "typed on tty1: <Enter>, n" in error
    keys = [json.loads(line) for line in (lab.run_dir() / "fake-qmp-keys.jsonl").read_text().splitlines()]
    pressed = [event["data"]["key"]["data"] for item in keys for event in item["events"] if event["data"]["down"]]
    assert pressed == ["ret", "n", "ret"], "Enter for the password screen, then n and Enter for 'open your Gentoo system now?'"


def test_the_scenarios_need_a_display_and_no_luks():
    import dataclasses

    base = vm.SCENARIO_MAP["ai-rescue-live"]
    assert vm.hardware_problems(base, None) == []
    blind = dataclasses.replace(base, hardware=vm.VMHardware(vga="none"))
    assert any("not --vga none" in problem for problem in vm.hardware_problems(blind, None))
    assert any("LUKS" in problem for problem in vm.hardware_problems(dataclasses.replace(base, luks=True), None))
    assert any("boot entry" in problem for problem in vm.hardware_problems(dataclasses.replace(base, boot_entry=""), None))


def test_the_password_is_read_without_keeping_it_in_any_log(tmp_path):
    runner = vm.ScenarioRunner(argparse.Namespace(iso=tmp_path / "a.iso", ssh_user="root", command_timeout=5), vm.SCENARIO_MAP["ai-rescue-live"], "", None)
    runner.run_dir = tmp_path
    outputs = {"cat /run/gentoo-rescue/do-password": f"{PASSWORD}\n", "echo hi": f"saw {PASSWORD}\n"}

    def fake_ssh(user, remote, log_name, timeout, *, stdin=None):
        log = tmp_path / log_name
        log.write_text(outputs[remote])
        return 0, log

    runner.ssh = fake_ssh
    live = types.SimpleNamespace(type_text=lambda text: None)
    hands = runner.ai_hands(live)
    assert hands.password() == PASSWORD
    assert PASSWORD not in (tmp_path / "ai-password.log").read_text()
    code, text = hands.run("echo hi", "later")
    assert text == "saw [hidden]\n" and PASSWORD not in (tmp_path / "ai-001-later.log").read_text()
