"""The hardware plan in a real install: probe -> plan -> preflight -> stages (it used to exist only in unit tests).

Discovery.inspect() never probed the machine, so Installer._make_plan() found no hardware and every install
ran without a plan. These tests drive the installer on the captured fixture machines and check that what is
rendered and installed really differs per machine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gentoo_installer import hardware
from gentoo_installer.manifest import InstallManifest
from gentoo_installerd import inspect as insp
from gentoo_installerd.configure import Configurer, render_make_conf
from gentoo_installerd.hwplan import PLAN_SCHEMA, HardwarePlan
from gentoo_installerd.inspect import Discovery, Inspection, ProbeRunner
from gentoo_installerd.install import InstallError, Installer, describe_clock_offset
from gentoo_installerd.proxy import ClockProbe
from gentoo_installerd.runner import DryRunRunner, RunnerError

from tests.backend.helpers import FakeRunner, lsblk
from tests.hardware.helpers import FIXTURES, fixture_dict

GiB = 2**30


# ------------------------------------------------------------------------------------------ helpers


def default_manifest(machine: str | None = None, **changes) -> InstallManifest:
    """The untouched defaults (KDE, OpenRC, GRUB, binary packages) plus a disk and passwords."""
    m = InstallManifest()
    m.storage.device = "/dev/sda"
    m.storage.device_id = "disk-serial"
    m.accounts.password = "user-password"
    m.accounts.root_password = "root-password"
    if machine:
        disks = [disk for disk in fixture_dict(machine)["disks"] if disk["installable"]]
        m.storage.device = disks[0]["path"]
    for name, value in changes.items():
        section, _, field = name.partition("__")
        setattr(getattr(m, section), field, value)
    return m


class RecordingConfigurer(Configurer):
    """A dry-run Configurer that remembers what it would have written (a dry run writes nothing)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.files: dict[str, str] = {}

    def write_bytes(self, relative, data: bytes, mode: int = 0o644) -> None:
        self.files[str(relative)] = data.decode("utf-8")


class StaticInspector:
    def __init__(self, inspection: Inspection) -> None:
        self.inspection = inspection

    def inspect(self) -> Inspection:
        return self.inspection


def inspection_of(machine: str | None = None, **changes) -> Inspection:
    result = Inspection("x86_64", 8 * GiB, True, True, {"lsblk": True}, [], hardware=fixture_dict(machine) if machine else {})
    for name, value in changes.items():
        setattr(result, name, value)
    return result


def dry_install(tmp_path: Path, monkeypatch, machine: str, m: InstallManifest | None = None):
    """Run a complete dry-run install on a captured machine; returns (installer, configurer, runner, events)."""
    monkeypatch.setattr(insp, "_network_ready", lambda *args, **kwargs: True)
    runner = FakeRunner()
    runner.respond("lsblk", lsblk())
    configurer = RecordingConfigurer(runner, tmp_path / "target", dry_run=True)
    events: list = []
    installer = Installer(m or default_manifest(machine), runner=runner, target_root=tmp_path / "target", state_dir=tmp_path / "state",
                          cache_dir=tmp_path / "cache", dry_run=True, configurer=configurer, emit=events.append,
                          inspector=Discovery(runner, hardware_root=FIXTURES / machine))
    assert installer.run(), [event.error for event in events if event.error]
    return installer, configurer, runner, events


def emerged(runner: FakeRunner) -> list[str]:
    """Every atom the install asked emerge for (the arguments of the chrooted emerge commands)."""
    atoms: list[str] = []
    for command in runner.commands:
        if command[:1] == ("chroot",) and "emerge" in command:
            atoms.extend(argument for argument in command[command.index("emerge") + 1:] if "/" in argument and not argument.startswith("-"))
    return atoms


# ------------------------------------------------------------------ the install differs per machine


class TestInstallOnCapturedMachines:
    def test_an_amd_laptop_gets_amd_firmware_and_no_intel_microcode(self, tmp_path, monkeypatch):
        installer, configurer, runner, _ = dry_install(tmp_path, monkeypatch, "amd-laptop-sof")
        atoms = emerged(runner)
        assert "sys-kernel/linux-firmware" in atoms and "sys-firmware/sof-firmware" in atoms
        assert not any("intel-microcode" in atom for atom in atoms)
        licenses = configurer.files["etc/portage/package.license/installer-hardware"]
        assert licenses == "sys-kernel/linux-firmware linux-fw-redistributable\n"
        assert "intel-ucode" not in licenses
        assert "sys-kernel/linux-firmware initramfs redistributable" in configurer.files["etc/portage/package.use/installer-hardware"]
        assert "etc/portage/package.license/installer" not in configurer.files  # the installer itself names no licence

    def test_the_ASUS_desktop_gets_intel_microcode_but_no_proprietary_nvidia_without_the_opt_in(self, tmp_path, monkeypatch):
        installer, configurer, runner, _ = dry_install(tmp_path, monkeypatch, "asus-desktop-rtx4090")
        atoms = emerged(runner)
        assert "sys-firmware/intel-microcode" in atoms
        assert not any("nvidia" in atom for atom in atoms)
        assert "prime-run" not in " ".join(configurer.files) and "switcheroo" not in " ".join(atoms)
        assert "NVIDIA" not in configurer.files["etc/portage/package.license/installer-hardware"]
        licenses = configurer.files["etc/portage/package.license/installer-hardware"]
        assert "sys-firmware/intel-microcode intel-ucode" in licenses
        assert installer.hardware_plan.scalars["gpu_driver"] == "open"

    def test_a_virtual_machine_gets_guest_tools_and_no_microcode(self, tmp_path, monkeypatch):
        installer, configurer, runner, _ = dry_install(tmp_path, monkeypatch, "qemu-q35-virtio", default_manifest("qemu-q35-virtio"))
        atoms = emerged(runner)
        assert "app-emulation/qemu-guest-agent" in atoms and "app-emulation/spice-vdagent" in atoms
        assert not any("microcode" in atom for atom in atoms)
        assert "intel-ucode" not in configurer.files["etc/portage/package.license/installer-hardware"]
        assert installer.hardware_plan.scalars["microcode"] == "none"

    def test_the_same_manifest_renders_differently_on_every_machine(self, tmp_path, monkeypatch):
        rendered = {}
        for index, machine in enumerate(("amd-laptop-sof", "asus-desktop-rtx4090", "qemu-q35-virtio")):
            _, configurer, runner, _ = dry_install(tmp_path / str(index), monkeypatch, machine)
            rendered[machine] = (configurer.files["etc/portage/package.use/installer-hardware"],
                                 configurer.files["etc/portage/package.license/installer-hardware"], tuple(sorted(set(emerged(runner)))))
            make_conf = configurer.files["etc/portage/make.conf"]
            assert "CPU_FLAGS_X86" not in make_conf and "VIDEO_CARDS" not in make_conf  # empty means: keep the profile's value
        uses, licences, atoms = (({rendered[machine][position] for machine in rendered}) for position in range(3))
        assert len(uses) == 3 and len(atoms) == 3
        # Intel microcode is the only licence that depends on the CPU vendor here
        assert len(licences) == 2 and "intel-ucode" in rendered["asus-desktop-rtx4090"][1]
        assert all("intel-ucode" not in rendered[machine][1] for machine in ("amd-laptop-sof", "qemu-q35-virtio"))

    def test_opting_in_to_the_proprietary_driver_installs_it_on_the_RTX_4090(self, tmp_path, monkeypatch):
        m = default_manifest("asus-desktop-rtx4090", hardware__gpu_driver="nvidia", hardware__nvidia_license_accepted=True)
        installer, configurer, runner, _ = dry_install(tmp_path, monkeypatch, "asus-desktop-rtx4090", m)
        assert "x11-drivers/nvidia-drivers" in emerged(runner)
        assert "x11-drivers/nvidia-drivers NVIDIA-2025" in configurer.files["etc/portage/package.license/installer-hardware"]
        assert "VIDEO_CARDS='${VIDEO_CARDS} nvidia'" in configurer.files["etc/portage/make.conf"]
        assert any(argument == "nvidia-drm.modeset=1" for argument in configurer.files["etc/kernel/cmdline"].split())

    def test_the_plan_summary_is_logged_and_sent_with_the_preflight_event(self, tmp_path, monkeypatch):
        _, _, _, events = dry_install(tmp_path, monkeypatch, "amd-laptop-sof")
        logs = [event.message for event in events if event.type == "log"]
        assert any(line.startswith("Hardware setup: Firmware: Linux firmware") for line in logs)
        preflight = next(event for event in events if event.type == "progress" and event.stage == "preflight")
        assert "Audio: Sound Open Firmware (many laptop audio chips need it)" in preflight.data["hardware_setup"]
        assert "hardware" not in preflight.data["inspection"]  # the client has it from the inspect reply

    def test_no_probe_data_still_gives_a_generic_plan(self, tmp_path):
        from tests.backend.helpers import FakeInspector

        events: list = []
        installer = Installer(default_manifest(), runner=DryRunRunner(), target_root=tmp_path / "t", state_dir=tmp_path / "s", dry_run=True,
                              inspector=FakeInspector(), emit=events.append)
        assert installer.run()
        assert installer.hardware_plan.early_atoms == ["sys-firmware/intel-microcode", "sys-kernel/linux-firmware"]


# ------------------------------------------------------------------------ what the inspect action reports


class TestDiscoveryReportsTheHardware:
    def test_the_inspection_carries_the_hardware_profile_and_its_warnings(self, monkeypatch):
        monkeypatch.setattr(insp, "_network_ready", lambda *args, **kwargs: True)
        runner = FakeRunner()
        runner.respond("lsblk", lsblk())
        data = Discovery(runner, hardware_root=FIXTURES / "amd-laptop-sof").inspect().to_dict()
        assert data["hardware"]["cpu"]["vendor"] == "amd" and data["hardware"]["machine"]["form_factor"] == "laptop"
        codes = {item["code"] for item in data["warnings"]}
        assert {"hw.fw_sb_enabled", "hw.bat_ac_offline"} <= codes
        assert all(set(item) == {"code", "severity", "message", "action"} for item in data["warnings"])
        assert "hardware_error" not in data

    def test_preflight_warnings_travel_in_the_same_shape(self, monkeypatch):
        monkeypatch.setattr(insp, "_network_ready", lambda *args, **kwargs: True)
        monkeypatch.setattr(insp.Path, "read_text", lambda self, *a, **k: "MemTotal:  1048576 kB\n" if str(self) == "/proc/meminfo" else "")
        runner = FakeRunner()
        runner.respond("lsblk", lsblk())
        data = Discovery(runner, hardware_root=FIXTURES / "qemu-q35-virtio").inspect().to_dict()
        assert {"code": "preflight.memory_low", "severity": "warning", "message": "At least 2 GiB of memory is recommended", "action": ""} in data["warnings"]

    def test_plain_text_warnings_of_other_inspectors_are_wrapped(self):
        legacy = Inspection("x86_64", GiB, True, True, {}, [], [], ["something to know"])
        assert legacy.to_dict()["warnings"] == [{"code": "inspect.warning", "severity": "warning", "message": "something to know", "action": ""}]
        assert "hardware" not in legacy.to_dict()  # absent means: this inspector did not look

    def test_a_probe_that_breaks_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(insp, "_network_ready", lambda *args, **kwargs: True)

        def broken(*args, **kwargs):
            raise RuntimeError("unusual firmware")

        monkeypatch.setattr(insp.hardware, "probe", broken)
        runner = FakeRunner()
        runner.respond("lsblk", lsblk())
        data = Discovery(runner).inspect().to_dict()
        assert data["hardware_error"] == "RuntimeError: unusual firmware" and "hardware" not in data

    def test_a_captured_machine_is_replayed_with_its_own_clock_and_runs_nothing(self, monkeypatch):
        seen = {}

        def fake_probe(root, runner=None, *, now=None, recorder=None):
            seen.update(root=root, runner=runner, now=now)
            return hardware.HardwareProfile()

        monkeypatch.setattr(insp.hardware, "probe", fake_probe)
        runner = FakeRunner()
        Discovery(runner, hardware_root=FIXTURES / "amd-laptop-sof").probe_hardware()
        assert seen["runner"] is None and seen["root"] == FIXTURES / "amd-laptop-sof"
        assert seen["now"] == hardware.fixture_now(FIXTURES / "amd-laptop-sof") and seen["now"] > 1_700_000_000
        assert not runner.commands

    def test_the_real_machine_is_probed_through_a_bounded_read_only_runner(self, monkeypatch):
        monkeypatch.delenv("GENTOO_HW_ROOT")
        seen = {}
        monkeypatch.setattr(insp.hardware, "probe", lambda root, runner=None, **kwargs: seen.update(root=root, runner=runner) or hardware.HardwareProfile())
        runner = FakeRunner()
        Discovery(runner).probe_hardware()
        assert str(seen["root"]) == "/" and isinstance(seen["runner"], ProbeRunner)
        seen["runner"].run(("lscpu",))
        assert runner.commands == [("lscpu",)] and runner.options[-1]["timeout"] == 5.0
        with pytest.raises(RunnerError, match="may only run its own read-only commands"):
            seen["runner"].run(("rm", "-rf", "/"))
        assert runner.commands == [("lscpu",)]

    def test_a_preview_probes_with_the_probes_own_runner_because_the_dry_run_runner_answers_nothing(self, monkeypatch):
        monkeypatch.delenv("GENTOO_HW_ROOT")
        seen = {}
        monkeypatch.setattr(insp.hardware, "probe", lambda root, runner=None, **kwargs: seen.update(runner=runner, root=root) or hardware.HardwareProfile())
        Discovery(DryRunRunner()).probe_hardware()
        assert "runner" in seen and seen["runner"] is None

    def test_the_inspect_action_does_not_probe_a_second_time(self, monkeypatch):
        from gentoo_installerd import server

        probes: list = []
        real = insp.hardware.probe
        monkeypatch.setattr(insp.hardware, "probe", lambda *args, **kwargs: probes.append(1) or real(*args, **kwargs))
        monkeypatch.setattr(insp, "_network_ready", lambda *args, **kwargs: True)
        monkeypatch.setattr(server, "SubprocessRunner", lambda: _InspectRunner())
        sent: list = []

        class Channel:
            def send(self, event):
                sent.append(event)

        server.handle_inspect({}, Channel())
        assert len(probes) == 1
        assert sent[0].data["hardware"]["cpu"]["vendor"] == "intel" and "warnings" in sent[0].data


class _InspectRunner:
    """Answers the daemon's inventory commands (the machine under test is the qemu fixture, via GENTOO_HW_ROOT)."""

    def run(self, argv, **kwargs):
        from gentoo_installerd.runner import CommandResult

        return CommandResult(tuple(argv), 0, lsblk() if argv[0] == "lsblk" else "")


# --------------------------------------------------------------------------------- the clock


class TestClock:
    def test_the_mirrors_view_of_the_clock_goes_to_the_client_in_the_documented_sign(self, monkeypatch):
        monkeypatch.setattr(insp, "_network_ready", lambda *args, **kwargs: True)
        monkeypatch.setattr(insp, "mirror_clock_probe", lambda *args, **kwargs: ClockProbe(skew=3600.0, tls_ok=True))
        runner = FakeRunner()
        runner.respond("lsblk", lsblk())
        clock = Discovery(runner).inspect().to_dict()["clock"]
        assert clock == {"skew_seconds": -3600.0, "tls_ok": True}  # mirror time minus this clock: the clock is an hour ahead

    def test_nothing_is_asked_when_the_mirror_cannot_be_reached(self, monkeypatch):
        monkeypatch.setattr(insp, "_network_ready", lambda *args, **kwargs: False)
        monkeypatch.setattr(insp, "mirror_clock_probe", lambda *args, **kwargs: pytest.fail("no network"))
        runner = FakeRunner()
        runner.respond("lsblk", lsblk())
        assert "clock" not in Discovery(runner).inspect().to_dict()

    @pytest.mark.parametrize(("seconds", "text"), [(601, "10 minutes"), (1200, "20 minutes"), (7200, "2 hours"), (3 * 3600, "3 hours"),
                                                   (3 * 86400, "3 days"), (20 * 365 * 86400, "7300 days")])
    def test_the_offset_is_described_in_a_unit_people_use(self, seconds, text):
        assert describe_clock_offset(seconds) == text

    @pytest.mark.parametrize("skew", [1200.0, -1200.0])
    def test_a_clock_that_is_off_by_more_than_ten_minutes_stops_the_install(self, tmp_path, skew):
        installer = Installer(default_manifest(), runner=FakeRunner(), target_root=tmp_path / "t", state_dir=tmp_path / "s",
                              inspector=StaticInspector(inspection_of(clock={"skew_seconds": skew})))
        with pytest.raises(InstallError, match=r"The system clock is off by 20 minutes\. Set the date or connect to the network so it syncs, then try again\."):
            installer._preflight()

    def test_ten_minutes_are_tolerated_and_a_missing_measurement_is_not_a_failure(self, tmp_path):
        for clock in ({"skew_seconds": 600.0}, {"tls_ok": False}, {}):
            installer = Installer(default_manifest(), runner=FakeRunner(), target_root=tmp_path / "t", state_dir=tmp_path / "s",
                                  inspector=StaticInspector(inspection_of(clock=clock)))
            installer._preflight()

    def test_a_preview_reports_the_clock_but_never_blocks(self, tmp_path):
        logs: list[str] = []
        installer = Installer(default_manifest(), runner=FakeRunner(), target_root=tmp_path / "t", state_dir=tmp_path / "s", dry_run=True,
                              inspector=StaticInspector(inspection_of(clock={"skew_seconds": 7200.0})))
        installer.log = logs.append  # type: ignore[method-assign]
        installer._preflight()
        assert not any("system clock" in line for line in logs)  # a preview is not offered the clock check at all


# --------------------------------------------------------------------------------- preflight


class TestPreflight:
    def installer(self, tmp_path, inspection, m=None, **kwargs) -> Installer:
        return Installer(m or default_manifest(), runner=FakeRunner(), target_root=tmp_path / "t", state_dir=tmp_path / "s",
                         inspector=StaticInspector(inspection), **kwargs)

    def test_what_the_hardware_probe_found_blocks_the_install(self, tmp_path):
        found = fixture_dict("asus-desktop-rtx4090")
        found["warnings"] = [{"code": "hw.st_no_disk", "severity": "error", "message": "No disk that can receive an installation was found.", "action": ""}]
        with pytest.raises(InstallError, match="Preflight failed: No disk that can receive an installation was found\\."):
            self.installer(tmp_path, inspection_of(hardware=found))._preflight()

    def test_the_manifest_is_checked_against_the_machine(self, tmp_path):
        m = default_manifest("qemu-q35-virtio", hardware__gpu_driver="nvidia", hardware__nvidia_license_accepted=True)
        with pytest.raises(InstallError, match="no NVIDIA graphics card was found"):
            self.installer(tmp_path, inspection_of("qemu-q35-virtio"), m)._preflight()

    def test_every_blocker_is_listed_once(self, tmp_path):
        found = fixture_dict("qemu-q35-virtio")
        found["warnings"] = [{"code": "hw.clock_skew", "severity": "error", "message": "The clock says 2000.", "action": "set_time"}] * 2
        with pytest.raises(InstallError) as caught:
            self.installer(tmp_path, inspection_of(hardware=found, errors=["Memory size could not be determined"]), default_manifest("qemu-q35-virtio"))._preflight()
        assert str(caught.value).count("The clock says 2000.") == 1 and "Memory size could not be determined" in str(caught.value)

    def test_in_a_preview_the_same_findings_are_only_logged(self, tmp_path):
        found = fixture_dict("asus-desktop-rtx4090")
        found["warnings"] = [{"code": "hw.st_no_disk", "severity": "error", "message": "No disk that can receive an installation was found.", "action": ""}]
        logs: list[str] = []
        installer = self.installer(tmp_path, inspection_of(hardware=found), dry_run=True)
        installer.log = logs.append  # type: ignore[method-assign]
        installer._preflight()
        assert "[dry-run] preflight finding (fatal in a real run): No disk that can receive an installation was found." in logs

    def test_a_secure_boot_install_no_longer_needs_the_signing_tools_on_the_live_system(self, tmp_path):
        key, certificate = tmp_path / "sb.key", tmp_path / "sb.crt"
        key.write_text("key")
        certificate.write_text("certificate")
        m = default_manifest(system__secure_boot=True, system__secure_boot_key=str(key), system__secure_boot_certificate=str(certificate),
                             system__bootloader="systemd-boot", system__uki=True)
        tools = {"lsblk": True, "sbsign": False, "sbverify": False, "mkfs.vfat": True, "efibootmgr": True}
        self.installer(tmp_path, inspection_of(tools=tools), m, live_root=tmp_path / "live")._preflight()  # signing happens in the new system

    def test_a_missing_tool_that_the_selection_needs_still_stops_the_install(self, tmp_path):
        m = default_manifest(storage__luks=True, storage__luks_passphrase="long enough passphrase")
        with pytest.raises(InstallError, match="Selected configuration needs missing tools: cryptsetup"):
            self.installer(tmp_path, inspection_of(tools={"cryptsetup": False}), m)._preflight()

    def test_choices_the_live_system_cannot_honour_stop_the_install_before_the_disk_is_touched(self, tmp_path):
        live = tmp_path / "live"
        (live / "usr/share/zoneinfo").mkdir(parents=True)
        (live / "usr/share/zoneinfo/UTC").write_text("")
        m = default_manifest(system__timezone="Asia/Jakarta")  # a real zone, but this live system does not have it
        with pytest.raises(InstallError, match="Preflight failed: The time zone 'Asia/Jakarta' does not exist on this system"):
            self.installer(tmp_path, inspection_of(), m, live_root=live)._preflight()

    def test_an_undetected_machine_cannot_contradict_the_users_nvidia_choice(self, tmp_path):
        m = default_manifest(hardware__gpu_driver="nvidia", hardware__nvidia_license_accepted=True)
        installer = self.installer(tmp_path, inspection_of(), m)
        installer._preflight()  # nothing was probed: not a reason to claim there is no NVIDIA card
        assert installer.hardware_plan.driver_atoms == ["x11-drivers/nvidia-drivers"]


# ------------------------------------------------------------- the plan is kept for a resume


class TestSavedPlan:
    def started(self, tmp_path, machine="amd-laptop-sof", resume=False, m=None) -> Installer:
        installer = Installer(m or default_manifest(machine), runner=FakeRunner(), target_root=tmp_path / "t", state_dir=tmp_path / "s", resume=resume,
                              inspector=StaticInspector(inspection_of(machine)))
        return installer

    def test_the_decision_is_saved_privately_and_bound_to_the_manifest(self, tmp_path):
        installer = self.started(tmp_path)
        installer._preflight()
        path = tmp_path / "s" / "hardware-plan.json"
        assert path.stat().st_mode & 0o777 == 0o600
        saved = json.loads(path.read_text())
        assert saved["version"] == 1 and saved["fingerprint"] == installer.checkpoints.fingerprint
        assert saved["plan"]["schema"] == PLAN_SCHEMA
        assert HardwarePlan.from_dict(saved["plan"]) == installer.hardware_plan  # nothing is lost on the way, files included

    def test_a_preview_saves_nothing(self, tmp_path):
        installer = Installer(default_manifest(), runner=FakeRunner(), target_root=tmp_path / "t", state_dir=tmp_path / "s", dry_run=True,
                              inspector=StaticInspector(inspection_of("amd-laptop-sof")))
        installer._preflight()
        assert not (tmp_path / "s").exists()

    def test_a_resume_uses_the_saved_decision_even_when_the_machine_looks_different_now(self, tmp_path):
        first = self.started(tmp_path)
        first._preflight()
        first._log_file.close()
        m = default_manifest("amd-laptop-sof")
        second = Installer(m, runner=FakeRunner(), target_root=tmp_path / "t", state_dir=tmp_path / "s", resume=True,
                           inspector=StaticInspector(inspection_of("asus-desktop-rtx4090")))
        logs: list[str] = []
        second.log = logs.append  # type: ignore[method-assign]
        second._preflight()
        assert second.hardware_plan == first.hardware_plan
        assert "sys-firmware/intel-microcode" not in second.hardware_plan.early_atoms  # the ASUS desktop would have chosen it
        assert "Using the hardware-specific setup that was decided when this installation started" in logs

    def test_a_resume_without_a_saved_plan_decides_again(self, tmp_path):
        installer = self.started(tmp_path, resume=True)
        logs: list[str] = []
        installer.log = logs.append  # type: ignore[method-assign]
        installer._preflight()
        assert installer.hardware_plan is not None and "No saved hardware-specific setup was found; deciding it again" in logs
        assert (tmp_path / "s" / "hardware-plan.json").exists()

    @pytest.mark.parametrize("damage", ["fingerprint", "schema", "garbage"])
    def test_a_saved_plan_that_does_not_belong_to_this_installation_is_refused(self, tmp_path, damage):
        first = self.started(tmp_path)
        first._preflight()
        first._log_file.close()
        path = tmp_path / "s" / "hardware-plan.json"
        saved = json.loads(path.read_text())
        if damage == "fingerprint":
            saved["fingerprint"] = "0" * 64
        elif damage == "schema":
            saved["plan"]["schema"] = PLAN_SCHEMA + 1
        path.write_text("{not json" if damage == "garbage" else json.dumps(saved))
        with pytest.raises(InstallError, match="saved hardware-specific setup"):
            self.started(tmp_path, resume=True)._preflight()

    def test_a_new_install_ignores_the_file_of_an_earlier_one(self, tmp_path):
        first = self.started(tmp_path, "amd-laptop-sof")
        first._preflight()
        first._log_file.close()
        second = self.started(tmp_path, "asus-desktop-rtx4090")
        second._preflight()
        assert "sys-firmware/intel-microcode" in second.hardware_plan.early_atoms
        assert json.loads((tmp_path / "s" / "hardware-plan.json").read_text())["fingerprint"] == second.checkpoints.fingerprint

    def test_the_copy_in_the_new_system_keeps_paths_and_hashes_but_no_file_contents(self, tmp_path):
        installer = self.started(tmp_path, "thinkpad-intel-nvidia-hybrid", m=default_manifest("thinkpad-intel-nvidia-hybrid", hardware__gpu_driver="nvidia",
                                                                                              hardware__nvidia_license_accepted=True))
        installer._preflight()
        assert "__NV_PRIME_RENDER_OFFLOAD" in (tmp_path / "s" / "hardware-plan.json").read_text()  # the private state file has them
        target = tmp_path / "t"
        (target / "etc").mkdir(parents=True)
        (target / "etc/gentoo-release").write_text("Gentoo\n")
        installer._target_holds_system = lambda: True  # type: ignore[method-assign]
        installer._preserve_install_log()
        copy = target / "var/log/gentoo-installer/hardware-plan.json"
        assert copy.stat().st_mode & 0o777 == 0o600
        saved = json.loads(copy.read_text())
        assert saved["plan"]["schema"] == PLAN_SCHEMA and saved["version"] == 1
        assert {item["path"] for item in saved["plan"]["files"]} >= {"/usr/local/bin/prime-run"}
        assert all(set(item) == {"path", "mode", "size", "sha256"} for item in saved["plan"]["files"])
        assert "__NV_PRIME_RENDER_OFFLOAD" not in copy.read_text()
