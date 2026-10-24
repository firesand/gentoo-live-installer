from __future__ import annotations

import pytest

from gentoo_installer import hardware as hw
from gentoo_installer import recommend as rec
from gentoo_installer.manifest import InstallManifest
from gentoo_installerd import hwplan

from tests.hardware.builders import GIB, disk, gpu, manifest, profile
from tests.hardware.helpers import FIXTURE_NAMES, fixture_dict

KIB = 1024 * 1024  # KiB per GiB


# -- formulas (the tables of the design) ------------------------------------------------------------

@pytest.mark.parametrize(("threads", "ram_gib", "avail_gib", "swap_mib", "jobs", "emerge_jobs"), [
    (2, 2.0, 1.2, 4096, 1, 1),
    (4, 4.0, 2.6, 4096, 2, 1),
    (8, 8.0, 6.4, 8192, 5, 1),
    (16, 16.0, 14.0, 8192, 9, 2),
    (32, 46.7, 44.5, 8192, 24, 2),
    (64, 256.0, 250.0, 8192, 32, 3),
    (1, 1.0, 0.5, 0, 1, 1),
    (8, 15.5, 14.0, 0, 7, 2),     # a "16 GiB" PC reports about 15.5 GiB
])
def test_build_jobs(threads, ram_gib, avail_gib, swap_mib, jobs, emerge_jobs):
    assert rec.recommend_jobs(threads, int(avail_gib * KIB), swap_mib) == jobs
    assert rec.recommend_emerge_jobs(int(ram_gib * KIB), threads) == emerge_jobs
    assert rec.recommend_make_opts(threads, int(avail_gib * KIB), swap_mib) == f"-j{jobs} -l{threads}"


def test_build_jobs_never_exceed_threads_and_survive_zero_input():
    assert rec.recommend_jobs(2, 100 * KIB) == 2
    assert rec.recommend_jobs(0, 0) == 1 and rec.recommend_make_opts(0, 0) == "-j1 -l1"


@pytest.mark.parametrize(("ram_gib", "plain", "zram", "hibernate"), [
    (1, 2048, 2048, 2048),
    (2, 4096, 2048, 3072),
    (4, 4096, 2048, 6144),       # Ubuntu SwapFaq: RAM + sqrt(RAM)
    (8, 8192, 2048, 11264),
    (16, 8192, 0, 20480),
    (48, 8192, 0, 56320),
])
def test_swap_sizes(ram_gib, plain, zram, hibernate):
    total = ram_gib * KIB
    assert rec.recommend_swap_mib(total) == plain
    assert rec.recommend_swap_mib(total, zram=True) == zram
    assert rec.recommend_swap_mib(total, hibernate=True) == hibernate


def test_installed_ram_is_rounded_up_from_memtotal():
    assert rec.nominal_ram_gib(48993456) == 47 and rec.nominal_ram_gib(4 * KIB) == 4 and rec.nominal_ram_gib(0) == 1


@pytest.mark.parametrize(("ram_gib", "expected"), [(0, False), (1.5, True), (7.9, True), (8, False), (47, False)])
def test_zram_by_default(ram_gib, expected):
    assert rec.zram_by_default(int(ram_gib * KIB)) is expected


@pytest.mark.parametrize(("desktop", "binhost", "expected"), [
    ("minimal", True, (8, 15)), ("xfce", True, (14, 22)), ("gnome", True, (18, 28)), ("kde", True, (24, 40)),
    ("kde", False, (40, 56)), ("minimal", False, (18, 25)), ("unknown", True, (24, 40)),
])
def test_minimum_root_sizes(desktop, binhost, expected):
    assert rec.minimum_root_gib(desktop, binhost) == expected


@pytest.mark.parametrize(("dpis", "expected"), [
    ([], 96), ([0], 96), ([96], 96), ([111], 96), ([117], 120), ([141], 144), ([158], 144), ([163], 168), ([284], 240), ([61], 96),
])
def test_scale_steps(dpis, expected):
    displays = [hw.DisplayInfo(connector=f"c{i}", dpi=dpi, internal=True) for i, dpi in enumerate(dpis)]
    assert rec.recommend_dpi(displays) == expected


def test_scale_prefers_the_internal_panel():
    panel = hw.DisplayInfo(connector="eDP-1", dpi=158, internal=True)
    monitor = hw.DisplayInfo(connector="DP-1", dpi=96, internal=False)
    assert rec.recommend_dpi([monitor, panel]) == 144 and rec.recommend_dpi([monitor]) == 96


def test_console_font_hint_for_large_or_dense_screens():
    assert rec.console_font_hint([hw.DisplayInfo(width=3840, height=2160, dpi=163)]) == "ter-132b"
    assert rec.console_font_hint([hw.DisplayInfo(width=1920, height=1080, dpi=200)]) == "ter-132b"
    assert rec.console_font_hint([hw.DisplayInfo(width=1920, height=1080, dpi=110)]) == "" and rec.console_font_hint([]) == ""


# -- recommend() ---------------------------------------------------------------------------------------------

def found(m, machine):
    return {r.code: r for r in rec.recommend(machine, m)}


def test_the_authors_pc_only_gets_the_optional_nvidia_suggestion():
    m = manifest()
    m.storage.swap_mib = 8192
    m.portage.make_opts = ""
    result = found(m, fixture_dict("asus-desktop-rtx4090"))
    assert set(result) == {"rec.nvidia_proprietary"}
    suggestion = result["rec.nvidia_proprietary"]
    assert (suggestion.field, suggestion.suggestion, suggestion.severity) == ("hardware.gpu_driver", "nvidia", "info")
    assert "595" in suggestion.message and "licence" in suggestion.message


@pytest.mark.parametrize(("ram", "desktop", "code", "suggestion"), [
    (1.5, "kde", "rec.low_memory_desktop", "minimal"),
    (1.5, "xfce", "rec.low_memory_desktop", "minimal"),
    (3.0, "kde", "rec.tight_memory", "xfce"),
    (3.0, "gnome", "rec.tight_memory", "xfce"),
])
def test_low_memory_advice(ram, desktop, code, suggestion):
    result = found(manifest(system={"desktop": desktop}), profile(ram_gib=ram))
    assert result[code].suggestion == suggestion and result[code].field == "system.desktop"


@pytest.mark.parametrize(("ram", "desktop"), [(1.5, "minimal"), (3.0, "xfce"), (8.0, "kde")])
def test_no_memory_advice_when_it_is_not_needed(ram, desktop):
    assert not {"rec.low_memory_desktop", "rec.tight_memory"} & set(found(manifest(system={"desktop": desktop}), profile(ram_gib=ram)))


HWMONUX_LOW_MEMORY = "HWMonux needs PySide6, which is compiled from source: about 40 minutes and up to 4 GiB of memory per compile job."


@pytest.mark.parametrize("desktop", ["kde", "xfce", "minimal"])
@pytest.mark.parametrize("ram", [2.0, 3.8, 5.7])
def test_hwmonux_is_advised_against_on_a_small_machine(ram, desktop):
    advice = found(manifest(system={"desktop": desktop}), profile(ram_gib=ram))["rec.hwmonux_low_memory"]
    assert (advice.field, advice.suggestion, advice.severity) == ("options.install_hwmonux", "false", "info")
    assert HWMONUX_LOW_MEMORY in advice.message and f"{ram:.1f} GiB" in advice.message


@pytest.mark.parametrize(("ram", "options"), [
    (7.7, {}),                                  # an 8 GiB PC reports about 7.7 GiB
    (15.5, {}),
    (47, {}),
    (0, {}),                                    # nothing is known about the memory
    (3.8, {"install_hwmonux": False}),          # already off
])
def test_no_hwmonux_advice_when_the_memory_is_enough_or_it_is_off(ram, options):
    assert "rec.hwmonux_low_memory" not in found(manifest(options=options), profile(ram_gib=ram))


def test_the_default_of_hwmonux_stays_the_users_decision_on_a_small_machine():
    """The advice is shown, the choice is not made: defaults_for and apply_defaults leave options.install_hwmonux alone (the VM harness expects HWMonux by default)."""
    machine = profile(ram_gib=3.8, threads=2)
    m = manifest()
    assert m.options.install_hwmonux is True
    assert "options.install_hwmonux" not in rec.defaults_for(machine, m)
    assert "options.install_hwmonux" not in rec.defaults_for(machine, m, resolve_auto=True)
    rec.apply_defaults(m, machine, resolve_auto=True)
    assert m.options.install_hwmonux is True


def test_swap_advice():
    assert found(manifest(), profile(ram_gib=16))["rec.swap_size"].suggestion == "8192"
    assert "rec.swap_size" not in found(manifest(storage={"swap_mib": 8192}), profile(ram_gib=16))       # a deliberate value is left alone
    result = found(manifest(hardware={"hibernate": True}), profile(ram_gib=16))
    assert result["rec.hibernate_swap"].suggestion == "20480" and result["rec.hibernate_swap"].severity == "warning"
    assert found(manifest(), profile(ram_gib=4))["rec.swap_size"].suggestion == "2048"   # with zram on a small machine


def test_make_opts_advice_only_replaces_the_old_fixed_default():
    machine = profile(threads=32, ram_gib=46)
    assert found(manifest(portage={"make_opts": "-j2"}), machine)["rec.make_opts"].suggestion == "-j21 -l32"
    assert "rec.make_opts" not in found(manifest(portage={"make_opts": ""}), machine) and "rec.make_opts" not in found(manifest(portage={"make_opts": "-j6"}), machine)
    assert "rec.make_opts" not in found(manifest(portage={"make_opts": "-j2"}), profile(threads=2, ram_gib=4))


def test_removable_disk_suggests_a_portable_install():
    stick = disk("/dev/sdb", bus="usb", hotplug=True)
    result = found(manifest(storage={"device": "/dev/sdb"}), profile(disks=(stick,)))
    assert (result["rec.portable_target"].field, result["rec.portable_target"].suggestion) == ("hardware.target", "portable")
    assert "rec.portable_target" not in found(manifest(storage={"device": "/dev/sdb"}, hardware={"target": "portable"}), profile(disks=(stick,)))
    assert "rec.portable_target" not in found(manifest(), profile())


def test_disk_size_advice_between_minimum_and_recommended():
    result = found(manifest(), profile(disks=(disk(size=30 * GIB),)))
    assert "rec.disk_size" in result
    assert "rec.disk_size" not in found(manifest(), profile(disks=(disk(size=100 * GIB),))) and "rec.disk_size" not in found(manifest(), profile(disks=(disk(size=10 * GIB),)))


def test_graphics_advice():
    assert "rec.nvidia_proprietary" in found(manifest(), profile(gpus=(gpu("nvidia", "maxwell-volta"),)))
    assert "580" in found(manifest(), profile(gpus=(gpu("nvidia", "maxwell-volta"),)))["rec.nvidia_proprietary"].message
    assert "rec.nvidia_legacy" in found(manifest(), profile(gpus=(gpu("nvidia", "kepler"),)))
    already = found(manifest(hardware={"gpu_driver": "nvidia", "nvidia_license_accepted": True}), profile(gpus=(gpu("nvidia", "turing+"),)))
    assert "rec.nvidia_proprietary" not in already
    assert "rec.nvidia_proprietary" not in found(manifest(), profile(hypervisor="qemu", gpus=(gpu("virtio"),)))
    assert "rec.old_intel_graphics" in found(manifest(), profile(gpus=(gpu("intel", "gen3", integrated=True),)))
    assert "rec.old_intel_graphics" not in found(manifest(portage={"binhost": False}), profile(gpus=(gpu("intel", "gen3", integrated=True),)))
    hybrid = profile(form="laptop", gpus=(gpu("intel", "gen8+", integrated=True, boot=True), gpu("nvidia", "turing+")))
    assert "rec.prime" in found(manifest(hardware={"prime": "off"}), hybrid) and "rec.prime" not in found(manifest(), hybrid)


def test_laptop_vm_and_clock_advice():
    assert found(manifest(hardware={"power": "none"}), profile(form="laptop"))["rec.laptop_power"].suggestion == "auto"
    assert "rec.laptop_power" not in found(manifest(), profile(form="laptop"))
    assert found(manifest(hardware={"guest_tools": "none"}), profile(hypervisor="qemu", gpus=(gpu("virtio"),)))["rec.guest_tools"].field == "hardware.guest_tools"
    machine = profile()
    machine["clock"]["rtc_mode"] = "local"
    assert found(manifest(), machine)["rec.rtc_local"].suggestion == "true"
    assert "rec.rtc_local" not in found(manifest(system={"rtc_local": True}), machine)
    windows = profile()
    windows["firmware"]["windows_boot_entry"] = True
    assert "rec.rtc_local" in found(manifest(), windows)


def test_hidpi_advice_is_for_xfce_only():
    dense = profile(displays=(hw.DisplayInfo(connector="eDP-1", internal=True, dpi=163),))
    assert found(manifest(system={"desktop": "xfce"}), dense)["rec.hidpi"].suggestion == "dpi:168"
    assert "rec.hidpi" not in found(manifest(), dense)


def test_recommendations_serialise_and_accept_a_profile_object_or_dict():
    machine = profile(ram_gib=1.5)
    items = rec.recommend(hw.HardwareProfile.from_dict(machine), manifest())
    assert items and items == rec.recommend(machine, manifest())
    assert set(items[0].to_dict()) == {"code", "severity", "message", "field", "suggestion"}
    assert rec.recommend(None, manifest()) == [] and rec.recommend({}, manifest()) == []


# -- apply_defaults -------------------------------------------------------------------------------------------

def test_apply_defaults_seeds_machine_facts():
    m = InstallManifest()
    rec.apply_defaults(m, fixture_dict("old-bios-sata-radeon"))
    assert m.storage.firmware == "bios"                    # G2: a BIOS-booted session must not preselect UEFI
    assert m.storage.swap_mib == 2048                      # 4 GiB with zram
    assert m.options.bluetooth is False                    # no radio on this PC
    assert m.options.microcode is True


def test_apply_defaults_in_a_vm_turns_microcode_off_and_keeps_bluetooth_when_a_radio_exists():
    m = InstallManifest()
    rec.apply_defaults(m, fixture_dict("qemu-q35-virtio"))
    assert m.options.microcode is False and m.storage.firmware == "uefi"
    laptop = InstallManifest()
    rec.apply_defaults(laptop, fixture_dict("thinkpad-intel-nvidia-hybrid"))
    assert laptop.options.bluetooth is True and laptop.options.microcode is True and laptop.storage.swap_mib == 8192


def test_apply_defaults_leaves_user_choices_alone():
    m = InstallManifest()
    m.storage.swap_mib = 12345
    m.options.bluetooth = False
    m.portage.video_cards = "intel"
    rec.apply_defaults(m, fixture_dict("old-bios-sata-radeon"))
    assert m.storage.swap_mib == 12345 and m.portage.video_cards == "intel"


def test_apply_defaults_never_fills_video_cards_or_the_nvidia_driver():
    for name in FIXTURE_NAMES:
        m = InstallManifest()
        rec.apply_defaults(m, fixture_dict(name), resolve_auto=True)
        assert m.portage.video_cards == "" and m.hardware.gpu_driver in ("auto", "nouveau") and not m.hardware.nvidia_license_accepted


def test_the_firmware_seed_can_be_skipped_for_an_explicit_manifest():
    m = InstallManifest()
    rec.apply_defaults(m, fixture_dict("old-bios-sata-radeon"), include_firmware=False)
    assert m.storage.firmware == "uefi"


def test_apply_defaults_ignores_a_failed_firmware_probe():
    machine = fixture_dict("old-bios-sata-radeon")
    machine["errors"] = ["firmware: OSError: boom"]
    m = InstallManifest()
    rec.apply_defaults(m, machine)
    assert m.storage.firmware == "uefi"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
@pytest.mark.parametrize("builder", ["openrc", "systemd"])
def test_resolving_auto_does_not_change_what_the_planner_decides(name, builder):
    """apply_defaults(resolve_auto=True) writes the planner's own answers, so the plan stays the same."""
    machine = fixture_dict(name)
    m = manifest() if builder == "openrc" else __import__("tests.hardware.builders", fromlist=["systemd"]).systemd()
    m.storage.swap_mib = 6144  # a deliberate size, which the seeding leaves alone: the compile jobs of the heavy packages follow the swap
    before = hwplan.plan(m, machine).to_dict()
    resolved = manifest() if builder == "openrc" else __import__("tests.hardware.builders", fromlist=["systemd"]).systemd()
    resolved.storage.swap_mib = 6144
    rec.apply_defaults(resolved, machine, resolve_auto=True, include_firmware=False)
    assert all(getattr(resolved.hardware, field) != "auto" for field in ("power", "thermald", "zram", "trim", "sof_firmware", "nvk", "prime", "removable_fallback", "hidpi"))
    after = hwplan.plan(resolved, machine).to_dict()
    # In a VM the microcode option is switched off, which only removes the explanatory note.
    assert {k: v for k, v in after.items() if k != "notes"} == {k: v for k, v in before.items() if k != "notes"}
    assert set(before["notes"]) - set(after["notes"]) <= {"CPU microcode skipped: the hypervisor of this virtual machine owns it."}
    # idempotent
    rec.apply_defaults(resolved, machine, resolve_auto=True, include_firmware=False)
    assert hwplan.plan(resolved, machine).to_dict() == after


def test_defaults_for_is_a_pure_preview():
    m = InstallManifest()
    changes = rec.defaults_for(fixture_dict("old-bios-sata-radeon"), m)
    assert changes == {"storage.firmware": "bios", "storage.swap_mib": 2048, "options.bluetooth": False}
    assert m == InstallManifest()


@pytest.mark.parametrize("machine", [None, {}, {"warnings": []}, "junk"])
def test_apply_defaults_without_detection_changes_nothing(machine):
    m = InstallManifest()
    rec.apply_defaults(m, machine)    # type: ignore[arg-type]
    assert m == InstallManifest()
