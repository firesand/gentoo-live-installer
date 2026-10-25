"""Hardware backlog H04, the pure parts: desktop sessions and pactl, the Safe graphics entry, the Secure Boot module experiment, DMI vendors."""
from __future__ import annotations

import json
import shlex

import pytest

import vm_hw
import vm_secureboot as sb
import vm_session as session
import vm_wave5 as w5
from tests.vm.helpers import PUBLIC_KEY, load_vm_module

vm = load_vm_module()

# --- desktop sessions -----------------------------------------------------------------------------------------------------

PROCESSES = "systemd\nsddm\nsddm-greeter-qt\nXorg\nbash\n"


def report(sessions: str, manager: str, processes: str = PROCESSES) -> str:
    return f"{sessions}\n---\n{manager}\n---\n{processes}"


def test_a_running_display_manager_with_a_greeter_and_a_session_file_passes():
    output = report("/usr/share/wayland-sessions:\nplasma.desktop\n\n/usr/share/xsessions:\nplasmax11.desktop\n", " * status: started")
    assert session.parse_desktop_session_probe(0, output, "kde", "openrc").success


@pytest.mark.parametrize(("sessions", "manager", "processes", "fragment"), [
    ("gnome.desktop", " * status: started", PROCESSES, "no session file for kde"),
    ("plasma.desktop", " * status: stopped", PROCESSES, "display manager is not running"),
    ("plasma.desktop", " * status: started", "systemd\nbash\n", "black screen"),
])
def test_each_desktop_failure_is_named(sessions, manager, processes, fragment):
    result = session.parse_desktop_session_probe(3, report(sessions, manager, processes), "kde", "openrc")
    assert not result.success and fragment in result.error


def test_systemd_uses_is_active_and_a_window_manager_needs_no_display_manager():
    assert session.parse_desktop_session_probe(0, report("xfce.desktop", "active", "lightdm\nXorg\n"), "xfce", "systemd").success
    assert not session.parse_desktop_session_probe(3, report("xfce.desktop", "inactive", "Xorg\n"), "xfce", "systemd").success
    assert session.parse_desktop_session_probe(3, report("sway.desktop", "inactive", "systemd\n"), "sway", "systemd").success
    assert session.parse_desktop_session_probe(3, report("i3.desktop", "unknown", "systemd\n"), "i3", "openrc").success


def test_a_report_in_the_wrong_shape_and_an_unknown_desktop_are_failures():
    assert not session.parse_desktop_session_probe(0, "nonsense", "kde", "openrc").success
    assert not session.parse_desktop_session_probe(0, report("x.desktop", "active"), "enlightenment", "systemd").success


def test_the_commands_match_the_init_system():
    assert "rc-service display-manager status" in session.desktop_session_command("openrc")
    assert "systemctl is-active display-manager" in session.desktop_session_command("systemd")
    assert "gentoo-pipewire-launcher" in session.pactl_command("openrc") and "gentoo-pipewire-launcher" not in session.pactl_command("systemd")
    assert "XDG_RUNTIME_DIR=/run/user/$(id -u)" in session.pactl_command("systemd")


PACTL = "Server String: /run/user/1000/pulse/native\nServer Name: PulseAudio (on PipeWire 1.6.8)\nDefault Sink: auto_null\n"


def test_pactl_must_answer_with_pipewire():
    assert session.parse_pactl_probe(0, PACTL).success
    assert "not PipeWire" in session.parse_pactl_probe(0, PACTL.replace("PulseAudio (on PipeWire 1.6.8)", "pulseaudio 17.0")).error
    broken = session.parse_pactl_probe(1, "Connection failure: Connection refused")
    assert not broken.success and "no sound server" in broken.error


def test_every_guided_desktop_has_a_package_and_a_scenario_per_init():
    assert set(session.GUIDED_DESKTOPS) == {"kde", "gnome", "xfce", "cinnamon", "mate", "lxqt", "gnome-light", "sway", "i3"}
    from gentoo_installer.manifest import DESKTOPS
    assert set(DESKTOPS) - {"minimal"} == set(session.GUIDED_DESKTOPS), "a desktop the manifest knows must be tested"
    for desktop in session.GUIDED_DESKTOPS:
        for init in ("openrc", "systemd"):
            scenario = vm.SCENARIO_MAP[f"desktop-{desktop}-{init}"]
            assert scenario.desktop == desktop and scenario.init == init and not scenario.in_all
            manifest = vm.build_manifest(scenario, PUBLIC_KEY, "none")
            assert manifest.system.desktop == desktop and manifest.options.pipewire and manifest.system.profile == ""
            assert ("/systemd" in manifest.effective_profile()) == (init == "systemd")
            names = [probe[0] for probe in vm.desktop_probe_plan(manifest)]
            assert names == ["desktop", "desktop-session", "audio-pactl"]


def test_the_old_desktops_keep_their_package_probe_and_gain_the_session_probes():
    manifest = vm.build_manifest(vm.SCENARIO_MAP["desktop-kde-openrc"], PUBLIC_KEY, "none")
    plan = {probe[0]: probe for probe in vm.desktop_probe_plan(manifest)}
    assert "plasma-meta" in plan["desktop"][1] and "DISPLAYMANAGER" in plan["desktop"][1]
    assert vm.desktop_probe_plan(vm.build_manifest(vm.SCENARIO_MAP["openrc"], PUBLIC_KEY)) == []


# --- the Safe graphics entry --------------------------------------------------------------------------------------------------

GRUB_CFG = """menuentry 'Gentoo GNU/Linux' --class gentoo {
	linux /vmlinuz-6.12.58-gentoo-dist root=UUID=0a1b ro quiet
	initrd /initramfs-6.12.58-gentoo-dist.img
}
submenu 'Advanced options for Gentoo GNU/Linux' {
	menuentry 'Gentoo GNU/Linux, with Linux 6.12.58-gentoo-dist' {
		linux /vmlinuz-6.12.58-gentoo-dist root=UUID=0a1b ro quiet
		initrd /initramfs-6.12.58-gentoo-dist.img
	}
	menuentry 'Safe graphics (no video driver, for a black screen)' {
		linux /vmlinuz-6.12.58-gentoo-dist root=UUID=0a1b ro nomodeset module_blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm,nouveau
		initrd /initramfs-6.12.58-gentoo-dist.img
	}
}
"""
SDBOOT = """== /efi/loader/entries/gentoo.conf
title Gentoo Linux
linux /EFI/Gentoo/vmlinuz
options root=UUID=0a1b ro quiet
== /efi/loader/entries/gentoo-safe-graphics.conf
title Gentoo Linux (Safe graphics)
linux /EFI/Gentoo/vmlinuz
options root=UUID=0a1b ro nomodeset module_blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm,nouveau
"""
EFIBOOTMGR = "BootCurrent: 0001\nBootOrder: 0001,0002\nBoot0001* Gentoo\tHD(1,GPT,x)/File(\\EFI\\Gentoo\\vmlinuz.efi)\nBoot0002* Gentoo (Safe graphics)\tHD(1,GPT,x)/File(\\EFI\\Gentoo\\vmlinuz.efi)\n"


def test_grub_lists_the_entry_inside_the_submenu_and_grub_reboot_gets_the_path():
    entry = session.find_safe_entry("grub", GRUB_CFG)
    assert entry["label"] == "Advanced options for Gentoo GNU/Linux > Safe graphics (no video driver, for a black screen)"
    assert session.safe_entry_problems(entry) == []
    command = session.oneshot_command("grub", entry)
    assert shlex.split(command)[-2:] == ["grub-reboot", "Advanced options for Gentoo GNU/Linux>Safe graphics (no video driver, for a black screen)"]


def test_systemd_boot_and_the_efi_stub_entries_are_found():
    entry = session.find_safe_entry("systemd-boot", SDBOOT)
    assert entry["id"] == "gentoo-safe-graphics.conf" and session.safe_entry_problems(entry) == []
    assert session.oneshot_command("systemd-boot", entry).endswith("bootctl set-oneshot gentoo-safe-graphics.conf")
    stub = session.find_safe_entry("efi-stub", EFIBOOTMGR)
    assert stub["id"] == "0002" and session.oneshot_command("efi-stub", stub).endswith("efibootmgr --bootnext 0002")


@pytest.mark.parametrize("loader", ["grub", "systemd-boot", "efi-stub"])
def test_a_menu_without_the_entry_gives_nothing(loader):
    plain = {"grub": GRUB_CFG.replace("Safe graphics", "Rescue"), "systemd-boot": SDBOOT.replace("Safe graphics", "Rescue"),
             "efi-stub": EFIBOOTMGR.replace("Safe graphics", "Rescue")}[loader]
    assert session.find_safe_entry(loader, plain) == {}


def test_an_entry_that_leaves_the_graphics_drivers_on_is_reported():
    weak = session.find_safe_entry("grub", GRUB_CFG.replace("nomodeset ", "").replace(",nouveau", ""))
    problems = session.safe_entry_problems(weak)
    assert any("nomodeset" in problem for problem in problems) and any("nouveau" in problem for problem in problems)


CMDLINE = "BOOT_IMAGE=/vmlinuz root=UUID=0a1b ro nomodeset module_blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm,nouveau"


def test_the_safe_boot_needs_the_arguments_and_no_graphics_module():
    modules = "virtio_net 40960 0 - Live 0x0\nvirtio_blk 24576 2 - Live 0x0\n"
    assert session.parse_safe_boot_probe(0, f"{CMDLINE}\n---\n{modules}").success
    loaded = session.parse_safe_boot_probe(0, f"{CMDLINE}\n---\nnouveau 2000000 0 - Live 0x0\n{modules}")
    assert not loaded.success and "nouveau" in loaded.error
    normal = session.parse_safe_boot_probe(0, "BOOT_IMAGE=/vmlinuz root=UUID=0a1b ro\n---\n" + modules)
    assert "nomodeset" in normal.error and "module_blacklist" in normal.error
    assert not session.parse_safe_boot_probe(1, "").success


# --- Secure Boot and out-of-tree modules --------------------------------------------------------------------------------------

def experiment(**changes) -> dict:
    base = {"kernel": "6.12.58-gentoo-dist", "secure_boot": True, "lockdown": "integrity", "sig_enforce": "N", "keyrings": [".builtin_trusted_keys", ".platform"],
            "tainted_before": 0, "build_tree": True, "build": "ok",
            "unsigned": {"loaded": False, "status": 1, "message": "insmod: ERROR: could not insert module: Key was rejected by service", "dmesg": []},
            "signed": {"loaded": False, "status": 1, "message": "Key was rejected by service", "dmesg": []}}
    base.update(changes)
    return base


def test_unsigned_modules_that_load_under_secure_boot_are_evidence_for_allowing_nvidia_with_a_warning():
    report = experiment(lockdown="none", unsigned={"loaded": True, "status": 0, "message": "", "dmesg": ["module verification failed: signature and/or required key missing - tainting kernel"]},
                        unsigned_tainted=4096)
    verdict = sb.decision(report)
    assert verdict["verdict"] == "unsigned-modules-load" and verdict["unsigned_module_loads"] is True and verdict["tainted_after_unsigned_load"] == 4096
    assert "warning" in verdict["why"]


def test_refused_unsigned_modules_mean_keep_refusing_and_a_signed_one_shows_the_signed_path():
    refused = sb.decision(experiment())
    assert refused["verdict"] == "unsigned-modules-refused" and "keep refusing" in refused["why"] and "lockdown integrity" in refused["why"]
    signed = sb.decision(experiment(signed={"loaded": True, "status": 0, "message": "", "dmesg": []}))
    assert signed["verdict"] == "only-signed-modules-load" and "signed path" in signed["why"]


def test_a_run_without_secure_boot_or_without_a_build_tree_proves_nothing():
    off = sb.decision(experiment(secure_boot=False))
    assert off["verdict"] == "inconclusive" and "Secure Boot was not active" in off["why"]
    none = sb.decision(experiment(build="no kernel build tree at /lib/modules/6.12/build", build_tree=False))
    assert none["verdict"] == "inconclusive" and "no kernel build tree" in none["why"]


def test_the_probe_keeps_the_verdict_and_fails_when_it_is_inconclusive():
    good = sb.parse_module_experiment_probe(0, "noise\n" + json.dumps(experiment()) + "\n")
    assert good.success and good.data["decision"]["verdict"] == "unsigned-modules-refused" and good.data["report"]["kernel"].startswith("6.12")
    bad = sb.parse_module_experiment_probe(0, json.dumps(experiment(secure_boot=False)))
    assert not bad.success and not bad.advisory and "Secure Boot was not active" in bad.error
    assert "no report" in sb.parse_module_experiment_probe(1, "Traceback").error
    sudo = sb.parse_module_experiment_probe(1, "sudo: a password is required")
    assert sudo.advisory


def test_the_experiment_program_compiles_reads_stdin_and_never_prints_the_key():
    compile(sb.MODULE_EXPERIMENT_SNIPPET, "snippet", "exec")
    data = sb.module_experiment_input("pw", b"PRIVATE KEY", b"CERT")
    first, _, rest = data.decode().partition("\n")
    assert first == "pw" and json.loads(rest)["key"] and "PRIVATE KEY" not in rest, "the key travels base64 encoded on stdin, not as text or an argument"
    command = sb.module_experiment_command()
    assert command.startswith("sudo -S -p '' python3 -c ") and "PRIVATE" not in command


def test_the_key_and_variable_store_commands_name_their_files():
    assert sb.key_generation_command("/k")[-4:] == ["-keyout", "/k/db.key", "-out", "/k/db.crt"] and "-nodes" in sb.key_generation_command("/k")
    enrol = sb.enrol_command("/k", "/usr/share/edk2-ovmf/OVMF_VARS.fd", "/k/vars.fd")
    assert enrol[0] == "virt-fw-vars" and "--set-pk" in enrol and "--add-db" in enrol and "--secure-boot" in enrol and enrol.count("/k/db.crt") == 3


# --- the scenarios and their arguments ----------------------------------------------------------------------------------------

def test_the_secure_boot_scenario_installs_a_signed_uki_with_the_test_key_paths_on_the_live_system():
    scenario = vm.SCENARIO_MAP["secure-boot-modules"]
    manifest = vm.build_manifest(scenario, PUBLIC_KEY, "none")
    system = manifest.system
    assert (system.bootloader, system.uki, system.secure_boot, system.secure_boot_mode) == ("systemd-boot", True, True, "own-key")
    assert system.secure_boot_key == sb.REMOTE_KEY and system.secure_boot_certificate == sb.REMOTE_CERT
    assert scenario.hardware.secure_boot and vm.hardware_problems(scenario, None) == []


def test_the_secure_boot_scenario_refuses_to_start_without_its_files():
    import argparse
    args = argparse.Namespace(sb_key=None, sb_cert=None, ovmf_vars_file=None, rescue_break="none", rescue_command=None, rescue_repair=False,
                              target_image=None, target_manifest=None, install_mode=None)
    problems = vm.rescue_problems(vm.SCENARIO_MAP["secure-boot-modules"], args)
    assert len([p for p in problems if "needs" in p]) == 3 and any("--sb-key" in p for p in problems) and any("--ovmf-vars-file" in p for p in problems)


def test_the_safe_graphics_scenarios_ask_for_the_nvidia_driver_and_one_loader_each():
    loaders = {}
    for name in ("safe-graphics-grub", "safe-graphics-systemd-boot", "safe-graphics-efi-stub"):
        manifest = vm.build_manifest(vm.SCENARIO_MAP[name], PUBLIC_KEY)
        assert (manifest.hardware.gpu_driver, manifest.hardware.nvidia_license_accepted, manifest.hardware.safe_graphics_entry) == ("nvidia", True, True)
        loaders[name] = manifest.system.bootloader
    assert loaders == {"safe-graphics-grub": "grub", "safe-graphics-systemd-boot": "systemd-boot", "safe-graphics-efi-stub": "efi-stub"}


@pytest.mark.parametrize(("preset", "vendor", "chassis"), [
    ("laptop-asus-rog", "ASUSTeK COMPUTER INC.", 10), ("laptop-razer-blade", "Razer", 10), ("laptop-lenovo-legion", "LENOVO", 10),
])
def test_the_vendor_presets_become_qemu_smbios_arguments(preset, vendor, chassis, tmp_path):
    assert vm_hw.SMBIOS_PRESETS[preset].manufacturer == vendor and vm_hw.SMBIOS_PRESETS[preset].chassis_type == chassis
    arguments = vm_hw.smbios_arguments(vm_hw.VMHardware(smbios=preset), tmp_path)
    assert any(f"manufacturer={vendor}" in argument for argument in arguments) and (tmp_path / "smbios-chassis.bin").is_file()
    probe = vm.parse_dmi_probe(0, f"{vendor}\n{chassis}\n", vm_hw.SMBIOS_PRESETS[preset])
    assert probe.success
    assert not vm.parse_dmi_probe(0, f"QEMU\n{chassis}\n", vm_hw.SMBIOS_PRESETS[preset]).success


def test_the_dmi_presets_agree_with_the_hardware_fixtures_of_the_same_machines():
    from tests.hardware.helpers import fixture_path
    for preset, fixture in (("laptop-asus-rog", "asus-rog-zephyrus-intel-nvidia-mux"), ("laptop-razer-blade", "razer-blade-intel-nvidia"),
                            ("laptop-lenovo-legion", "lenovo-legion-amd-nvidia")):
        dmi = fixture_path(fixture) / "sys/class/dmi/id"
        spec = vm_hw.SMBIOS_PRESETS[preset]
        assert (dmi / "sys_vendor").read_text().strip() == spec.manufacturer
        assert (dmi / "product_name").read_text().strip() == spec.product
        assert (dmi / "product_version").read_text().strip() == spec.version


def test_the_vendor_scenarios_expect_what_the_hardware_probe_reports():
    for name in ("dmi-asus-rog", "dmi-razer-blade", "dmi-lenovo-legion"):
        scenario = vm.SCENARIO_MAP[name]
        assert scenario.live_only and scenario.live_expect and not scenario.in_all
        report = {"machine": {"sys_vendor": vm_hw.SMBIOS_PRESETS[scenario.hardware.smbios].manufacturer,
                              "product_name": vm_hw.SMBIOS_PRESETS[scenario.hardware.smbios].product,
                              "product_version": vm_hw.SMBIOS_PRESETS[scenario.hardware.smbios].version,
                              "vendor_family": scenario.hardware.smbios.split("-")[1]}}
        assert vm.check_expectations(report, dict(scenario.live_expect)) == [], name
        report["machine"]["sys_vendor"] = "QEMU"
        assert vm.check_expectations(report, dict(scenario.live_expect)), name


def test_the_hardware_report_is_read_from_the_command_output():
    assert w5.parse_hardware_report(0, "warning\n" + json.dumps({"machine": {"sys_vendor": "Razer"}})) == {"machine": {"sys_vendor": "Razer"}}
    assert w5.parse_hardware_report(1, "{}") == {} and w5.parse_hardware_report(0, "no json") == {} and w5.parse_hardware_report(0, "{broken") == {}
