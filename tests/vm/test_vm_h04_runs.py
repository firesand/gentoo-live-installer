"""Hardware backlog H04 against the fake lab: vendor DMI, desktop sessions, the Safe graphics entry and the Secure Boot module experiment."""
from __future__ import annotations

import json

import pytest

import vm_hw
import vm_secureboot as sb
from tests.vm.fake_env import Lab, guest_rules
from tests.vm.helpers import load_vm_module
from tests.vm.test_vm_h04 import CMDLINE, EFIBOOTMGR, GRUB_CFG, SDBOOT, experiment

vm = load_vm_module()
pytestmark = pytest.mark.slow  # real processes and sockets

ABSENT = [{"user": "^vmtest$", "match": r"app-misc.*glob.*hwmonux-", "stdout": "absent\n"},
          {"user": "^vmtest$", "match": r"app-portage.*glob.*portagehub-", "stdout": "absent\n"},
          {"user": "^vmtest$", "match": r"app-admin.*glob.*gentoo-settings-", "stdout": "absent\n"},
          {"user": "^vmtest$", "match": r"sys-kernel.*glob.*kernelsmith-", "stdout": "absent\n"}]  # --companions none
MODULES = "virtio_net 40960 0 - Live 0x0\nvirtio_blk 24576 2 - Live 0x0\n"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return Lab(vm, tmp_path, monkeypatch)


def hardware_report(scenario_name: str, **machine) -> dict:
    preset = vm_hw.SMBIOS_PRESETS[vm.SCENARIO_MAP[scenario_name].hardware.smbios]
    values = {"sys_vendor": preset.manufacturer, "product_name": preset.product, "product_version": preset.version,
              "vendor_family": scenario_name.split("-")[1]}
    values.update(machine)
    return {"machine": values, "firmware": {"mode": "uefi"}}


def vendor_rules(scenario_name: str, **machine) -> list[dict]:
    return [{"user": "^root$", "match": "live_inspection", "inspection": {}},
            {"user": "^root$", "match": r"gentoo_installer\.hardware", "stdout": json.dumps(hardware_report(scenario_name, **machine)) + "\n"},
            *guest_rules(vm, scenario_name)]


@pytest.mark.parametrize("name", ["dmi-asus-rog", "dmi-razer-blade", "dmi-lenovo-legion"])
def test_a_vendor_vm_is_read_by_the_hardware_probe_as_that_vendor(lab, name):
    lab.rules = vendor_rules(name)
    assert lab.run("--scenario", name) == 0, lab.result().get("error")
    live = lab.result()["live"]
    assert live["problems"] == [] and live["hardware_report"]["machine"]["sys_vendor"] == vm_hw.SMBIOS_PRESETS[vm.SCENARIO_MAP[name].hardware.smbios].manufacturer
    argv = json.loads((lab.run_dir() / "fake-qemu-argv-live.json").read_text())
    assert any("manufacturer=" in argument for argument in argv), "the VM was started with the vendor's SMBIOS strings"


def test_a_vendor_vm_that_the_probe_reads_as_qemu_fails(lab):
    lab.rules = vendor_rules("dmi-razer-blade", sys_vendor="QEMU")
    assert lab.run("--scenario", "dmi-razer-blade") == 1
    assert "hardware report: machine.sys_vendor" in lab.result()["error"]


def test_a_desktop_scenario_checks_the_session_and_the_sound_server(lab):
    lab.rules = guest_rules(vm, "desktop-xfce-openrc", sudo=True, companions=False, extra=ABSENT)
    assert lab.run("--scenario", "desktop-xfce-openrc", "--companions", "none") == 0, lab.result().get("error")
    probes = {probe["name"]: probe for probe in lab.result()["probes"]}
    assert probes["desktop-session"]["success"] and probes["audio-pactl"]["success"] and probes["desktop"]["success"]
    manifest = json.loads((lab.run_dir() / "manifest.json").read_text())
    assert manifest["system"]["desktop"] == "xfce" and manifest["options"]["pipewire"] is True


def test_a_desktop_without_a_greeter_fails_the_run(lab):
    blind = {"user": "^vmtest$", "match": r"/usr/share/xsessions", "stdout": "/usr/share/xsessions:\nxfce.desktop\n\n---\n * status: started\n\n---\nsystemd\nbash\n"}
    lab.rules = [blind, *guest_rules(vm, "desktop-xfce-openrc", sudo=True, companions=False, extra=ABSENT)]
    assert lab.run("--scenario", "desktop-xfce-openrc", "--companions", "none") == 1
    assert "desktop-session" in lab.result()["error"] and "black screen" in lab.result()["error"]


def safe_rules(scenario_name: str, listing: str, *, cmdline: str = CMDLINE, modules: str = MODULES, list_command: str) -> list[dict]:
    return [{"user": "^vmtest$", "match": list_command, "stdout": listing},
            {"user": "^vmtest$", "match": r"(?:grub-reboot|bootctl set-oneshot|efibootmgr --bootnext)", "touch": ["oneshot-chosen"]},
            {"user": "^vmtest$", "match": r"^cat /proc/cmdline; echo ---; cat /proc/modules$", "stdout": f"{cmdline}\n---\n{modules}"},
            *guest_rules(vm, scenario_name, sudo=True)]


@pytest.mark.parametrize(("name", "listing", "command"), [
    ("safe-graphics-grub", GRUB_CFG, r"^cat /boot/grub/grub\.cfg$"),
    ("safe-graphics-systemd-boot", SDBOOT, r"loader/entries"),
    ("safe-graphics-efi-stub", EFIBOOTMGR, r"efibootmgr$"),
])
def test_the_safe_graphics_entry_is_found_chosen_once_and_booted(lab, name, listing, command):
    lab.rules = safe_rules(name, listing, list_command=command)
    assert lab.run("--scenario", name, "--no-world-probe") == 0, lab.result().get("error")
    evidence = lab.result()["safe_graphics"]
    assert evidence["entry"] and evidence["boot"]["success"] is True
    assert (lab.run_dir() / "oneshot-chosen").exists()
    manifest = json.loads((lab.run_dir() / "manifest.json").read_text())
    assert manifest["hardware"]["gpu_driver"] == "nvidia" and manifest["hardware"]["safe_graphics_entry"] is True


def test_a_boot_menu_without_the_entry_fails(lab):
    lab.rules = safe_rules("safe-graphics-grub", GRUB_CFG.replace("Safe graphics", "Rescue"), list_command=r"^cat /boot/grub/grub\.cfg$")
    assert lab.run("--scenario", "safe-graphics-grub", "--no-world-probe") == 1
    assert "has no Safe graphics entry" in lab.result()["error"]


def test_a_safe_boot_that_still_loads_nouveau_fails(lab):
    lab.rules = safe_rules("safe-graphics-grub", GRUB_CFG, modules=MODULES + "nouveau 2000000 0 - Live 0x0\n", list_command=r"^cat /boot/grub/grub\.cfg$")
    assert lab.run("--scenario", "safe-graphics-grub", "--no-world-probe") == 1
    assert "Safe graphics boot" in lab.result()["error"] and "nouveau" in lab.result()["error"]


@pytest.fixture
def secure_files(tmp_path):
    key, cert, variables = tmp_path / "db.key", tmp_path / "db.crt", tmp_path / "vars.fd"
    key.write_bytes(b"TEST ONLY PRIVATE KEY\n")
    cert.write_bytes(b"TEST ONLY CERTIFICATE\n")
    variables.write_bytes(b"vars with the certificate enrolled")
    return ["--sb-key", str(key), "--sb-cert", str(cert), "--ovmf-vars-file", str(variables)]


def secure_rules(report: dict) -> list[dict]:
    return [{"user": "^root$", "match": r"^mkdir|^umask 077 && mkdir", "stdin_to": "sb-key", "rc": 0},
            {"user": "^root$", "match": r"db\.crt$", "stdin_to": "sb-cert", "rc": 0},
            {"user": "^vmtest$", "match": r"gentoo_vm_dummy", "stdout": json.dumps(report) + "\n"},
            *guest_rules(vm, "secure-boot-modules", sudo=True)]


def test_the_secure_boot_scenario_uploads_the_test_key_and_records_the_verdict(lab, secure_files, capsys):
    lab.rules = secure_rules(experiment())
    assert lab.run("--scenario", "secure-boot-modules", "--no-world-probe", *secure_files) == 0, lab.result().get("error")
    run = lab.run_dir()
    verdict = lab.result()["secure_boot_modules"]["decision"]
    assert verdict["verdict"] == "unsigned-modules-refused" and verdict["secure_boot"] is True
    assert (run / "fake-guest" / "sb-key").read_bytes() == b"TEST ONLY PRIVATE KEY\n" and (run / "fake-guest" / "sb-cert").read_bytes() == b"TEST ONLY CERTIFICATE\n"
    assert "SECURE BOOT secure-boot-modules: unsigned-modules-refused" in capsys.readouterr().out
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["system"]["secure_boot_key"] == sb.REMOTE_KEY and manifest["system"]["uki"] is True
    qemu = json.loads((run / "fake-qemu-argv-live.json").read_text())
    assert any("property=secure,value=on" in argument for argument in qemu), "OVMF's Secure Boot build needs the pflash secure property"
    assert b"PRIVATE" not in b"".join(path.read_bytes() for path in run.glob("*.log")), "the private key is not in any log"


def test_a_secure_boot_run_that_was_not_secure_fails(lab, secure_files):
    lab.rules = secure_rules(experiment(secure_boot=False))
    assert lab.run("--scenario", "secure-boot-modules", "--no-world-probe", *secure_files) == 1
    assert "secure-boot-modules" in lab.result()["error"] and "Secure Boot was not active" in lab.result()["error"]


def test_the_secure_boot_scenario_needs_its_files(lab):
    lab.rules = secure_rules(experiment())
    with pytest.raises(SystemExit) as stop:
        lab.run("--scenario", "secure-boot-modules")
    assert stop.value.code == 2
