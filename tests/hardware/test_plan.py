"""Rules of the hardware planner on made-up machines (the fixture goldens are in test_plan_golden.py)."""

from __future__ import annotations

import pytest

from gentoo_installer import hardware as hw
from gentoo_installerd import hwplan

from tests.hardware.builders import GIB, disk, gpu, manifest, profile, systemd

NV = "x11-drivers/nvidia-drivers"
FW = "sys-kernel/linux-firmware"
UC = "sys-firmware/intel-microcode"


def plan(m, machine):
    return hwplan.plan(m, machine)


def quiet(**extra):
    """Options with nothing that adds firmware reasons: isolates one rule at a time."""
    return {"options": {"firmware": False, "microcode": False, **extra}}


# -- the failure that started it all ----------------------------------------------------------

@pytest.mark.parametrize("machine", [
    profile(vendor="intel", gpus=(gpu("nvidia", "turing+", boot=True),)),
    profile(vendor="amd", gpus=(gpu("amd", "gcn3+", boot=True),)),
    profile(vendor="intel", form="laptop", gpus=(gpu("intel", "gen8+", integrated=True, boot=True), gpu("nvidia", "turing+"))),
    profile(vendor="intel", hypervisor="qemu", gpus=(gpu("virtio"),)),
    profile(),
    {},
])
def test_the_default_binhost_plan_never_writes_empty_or_narrowing_make_conf_values(machine):
    """K5: an empty CPU_FLAGS_X86 or VIDEO_CARDS wiped the profile defaults and broke the default KDE closure (opencv REQUIRED_USE)."""
    result = plan(manifest(), machine)
    assert "CPU_FLAGS_X86" not in result.make_conf and "VIDEO_CARDS" not in result.make_conf
    assert all(value.strip() for value in result.make_conf.values())
    assert "MAKEOPTS" not in result.make_conf and "L10N" not in result.make_conf  # decided by the configure step


# -- microcode (NOBS-1) ---------------------------------------------------------------------------

def test_intel_microcode_only_on_intel_without_the_whole_firmware_package():
    result = plan(manifest(options={"firmware": False}), profile(vendor="intel"))
    assert result.early_atoms == [UC]
    assert result.package_license == {UC: ["intel-ucode"]}
    assert result.package_use[UC] == ["initramfs"]
    assert result.scalars["microcode"] == "intel"


def test_amd_microcode_comes_from_linux_firmware_and_needs_no_intel_package():
    result = plan(manifest(options={"firmware": False}), profile(vendor="amd"))
    assert result.early_atoms == [FW]
    assert result.package_use[FW] == ["initramfs", "redistributable"]
    assert result.package_license == {FW: ["linux-fw-redistributable"]}
    assert result.scalars["microcode"] == "amd"


@pytest.mark.parametrize(("machine", "microcode"), [
    (profile(vendor="intel", hypervisor="qemu"), "none"),
    (profile(vendor="amd", hypervisor="vmware"), "none"),
    (profile(vendor="hygon"), "none"),
    (profile(vendor="other"), "none"),
])
def test_no_microcode_in_a_vm_or_for_other_vendors(machine, microcode):
    result = plan(manifest(options={"firmware": False}), machine)
    assert UC not in result.all_atoms() and FW not in result.all_atoms()
    assert result.scalars["microcode"] == microcode and any("microcode skipped" in note.lower() for note in result.notes)


def test_both_families_when_portable_or_when_the_vendor_is_unknown():
    for m, machine in ((manifest(hardware={"target": "portable"}), profile(vendor="amd")), (manifest(), {})):
        result = plan(m, machine)
        assert {UC, FW} <= set(result.early_atoms) and result.scalars["microcode"] == "both"
        assert result.package_use[FW][:1] == ["initramfs"]


def test_microcode_option_off_installs_none_but_firmware_may_stay():
    result = plan(manifest(options={"microcode": False}), profile(vendor="intel"))
    assert result.early_atoms == [FW] and result.scalars["microcode"] == "none"
    assert result.package_use[FW] == ["redistributable"]


# -- firmware ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(("machine", "reason"), [
    (profile(gpus=(gpu("amd", "gcn3+"),)), "AMD graphics"),
    (profile(wifi=True), "Wi-Fi"),
    (profile(bluetooth=True), "Bluetooth"),
    (profile(gpus=(gpu("nvidia", "turing+"),)), "NVIDIA GPU system processor firmware"),
    (profile(gpus=(gpu("intel", "gen8+", integrated=True),)), "Intel graphics"),
    (profile(nics=(hw.NicInfo(iface="enp1s0", kind="ethernet", driver="r8169", bus="pci"),)), "Ethernet adapter"),
])
def test_firmware_is_forced_on_when_the_hardware_needs_it(machine, reason):
    result = plan(manifest(**quiet()), machine)
    assert result.early_atoms == [FW] and result.scalars["firmware_forced"] is True
    assert reason in result.scalars["firmware_reasons"] and any(reason in note for note in result.notes)


def test_firmware_is_not_forced_in_a_vm_or_for_ordinary_hardware():
    assert plan(manifest(**quiet()), profile(hypervisor="qemu", gpus=(gpu("virtio"),), wifi=True)).early_atoms == []
    assert plan(manifest(**quiet()), profile(gpus=(gpu("intel", "gen3", integrated=True),))).early_atoms == []
    assert plan(manifest(**quiet()), profile(gpus=(gpu("nvidia", "kepler"),))).early_atoms == []


def test_firmware_is_wanted_by_the_option_alone():
    result = plan(manifest(options={"firmware": True, "microcode": False}), profile())
    assert result.early_atoms == [FW] and result.scalars["firmware_forced"] is False


def test_portable_install_gets_everything():
    result = plan(manifest(hardware={"target": "portable"}, options={"firmware": False, "microcode": False}), profile())
    assert result.early_atoms == [FW]
    assert A_SOF in result.optional_atoms


A_SOF = "sys-firmware/sof-firmware"


@pytest.mark.parametrize(("machine", "choice", "expected"), [
    (profile(form="laptop", audio_vendors=("8086",), needs_sof=True), "auto", True),
    (profile(form="laptop", audio_vendors=("8086",)), "auto", True),     # an Intel laptop: cheap insurance
    (profile(form="laptop", audio_vendors=("1022",)), "auto", True),
    (profile(form="desktop", audio_vendors=("8086",)), "auto", False),
    (profile(form="desktop", audio_vendors=("8086",), needs_sof=True), "auto", True),   # the live kernel bound an SOF driver
    (profile(form="laptop", audio_vendors=("10de",)), "auto", False),
    (profile(form="laptop", audio_vendors=("8086",), needs_sof=True), "no", False),
    (profile(form="desktop"), "yes", True),
    (profile(hypervisor="qemu", needs_sof=True), "auto", False),
])
def test_sof_firmware(machine, choice, expected):
    result = plan(manifest(hardware={"sof_firmware": choice}), machine)
    assert (A_SOF in result.optional_atoms) is expected
    assert result.scalars["sof_firmware"] == ("yes" if expected else "no")
    assert A_SOF not in result.early_atoms  # audio firmware is best-effort, never boot-critical


# -- CPU flags (H7, hardware.target) ----------------------------------------------------------------

DETECTED = ("aes", "avx", "avx2", "mmx", "mmxext", "sse", "sse2", "sse3", "ssse3")


@pytest.mark.parametrize(("mode", "binhost", "target", "expected"), [
    ("auto", True, "this", None),
    ("auto", False, "this", " ".join(DETECTED)),
    ("detect", True, "this", " ".join(DETECTED)),
    ("detect", False, "portable", None),      # portable keeps the profile defaults, even when asked to detect
    ("auto", False, "portable", None),
    ("profile", False, "this", None),
])
def test_cpu_flags_modes(mode, binhost, target, expected):
    result = plan(manifest(hardware={"cpu_flags_mode": mode, "target": target}, portage={"binhost": binhost}), profile())
    assert result.make_conf.get("CPU_FLAGS_X86") == expected


def test_manual_cpu_flags_and_the_legacy_explicit_value():
    typed = "aes avx2"
    assert plan(manifest(hardware={"cpu_flags_mode": "manual"}, portage={"cpu_flags": typed}), profile()).make_conf["CPU_FLAGS_X86"] == typed
    assert plan(manifest(portage={"cpu_flags": typed}), profile()).make_conf["CPU_FLAGS_X86"] == typed  # auto + a typed value: the old behaviour
    empty = plan(manifest(hardware={"cpu_flags_mode": "manual"}), profile())
    assert "CPU_FLAGS_X86" not in empty.make_conf and any(w.code == "hw.cpu_flags_empty" for w in empty.warnings)


def test_detect_without_flags_warns_instead_of_writing_nothing_silently():
    result = plan(manifest(hardware={"cpu_flags_mode": "detect"}), profile(flags=()))
    assert "CPU_FLAGS_X86" not in result.make_conf and any(w.code == "hw.cpu_flags_unavailable" for w in result.warnings)


# -- VIDEO_CARDS (binhost policy) --------------------------------------------------------------------

@pytest.mark.parametrize(("gpus", "expected"), [
    ((gpu("intel", "gen8+", integrated=True),), "intel"),
    ((gpu("intel", "gen3", integrated=True),), "intel i915"),
    ((gpu("amd", "gcn3+"),), "amdgpu radeonsi"),
    ((gpu("amd", "gcn1-2"),), "amdgpu radeonsi radeon"),
    ((gpu("amd", "terascale"),), "radeon r600"),
    ((gpu("amd", "r300"),), "radeon r300"),
    ((gpu("amd", "older"),), None),
    ((gpu("nvidia", "turing+"),), "nouveau nvk"),          # NVK (Vulkan) is part of a source build for Turing and newer
    ((gpu("nvidia", "maxwell-volta"),), "nouveau"),
    ((gpu("nvidia", "kepler"),), "nouveau"),
    ((gpu("intel", "gen8+", integrated=True), gpu("nvidia", "turing+")), "intel nouveau nvk"),
    ((gpu("amd", "gcn3+", integrated=True), gpu("amd", "gcn3+")), "amdgpu radeonsi"),
    ((gpu("virtio"),), "virgl"),
    ((gpu("vmware"),), None),            # never vmware: open-vm-tools[resolutionkms] needs mesa[-video_cards_vmware]
    ((gpu("virtualbox"),), None),
    ((gpu("qxl"),), None),
    ((gpu("hyperv"),), None),
    ((), None),
])
def test_video_cards_for_source_builds_is_the_minimal_list(gpus, expected):
    result = plan(manifest(portage={"binhost": False}), profile(gpus=gpus))
    assert result.make_conf.get("VIDEO_CARDS") == expected


@pytest.mark.parametrize("gpus", [(gpu("amd", "terascale"),), (gpu("intel", "gen3", integrated=True),), (gpu("virtio"),), (gpu("nvidia", "turing+"),)])
def test_video_cards_is_left_alone_with_the_binhost(gpus):
    """Mesa and libdrm carry the VIDEO_CARDS flags; the profile's radeon flag already builds r300 and r600."""
    assert "VIDEO_CARDS" not in plan(manifest(), profile(gpus=gpus)).make_conf


def test_old_intel_graphics_are_explained_not_compiled():
    result = plan(manifest(), profile(gpus=(gpu("intel", "gen3", integrated=True),)))
    assert any("old Intel graphics" in note for note in result.notes)
    assert any(rec.code == "rec.old_intel_graphics" for rec in __import__("gentoo_installer.recommend", fromlist=["x"]).recommend(profile(gpus=(gpu("intel", "gen3", integrated=True),)), manifest()))


def test_explicit_video_cards_is_written_verbatim():
    result = plan(manifest(portage={"video_cards": "intel nvidia"}), profile(gpus=(gpu("amd", "gcn3+"),)))
    assert result.make_conf["VIDEO_CARDS"] == "intel nvidia"


def test_portable_targets_do_not_narrow_video_cards():
    result = plan(manifest(hardware={"target": "portable"}, portage={"binhost": False}), profile(gpus=(gpu("amd", "gcn3+"),)))
    assert "VIDEO_CARDS" not in result.make_conf


# -- NVIDIA -------------------------------------------------------------------------------------------

def nvidia_manifest(**more):
    base = {"hardware": {"gpu_driver": "nvidia", "nvidia_license_accepted": True}, "system": {"kernel": "gentoo-kernel-bin"}}
    for section, values in more.items():
        base.setdefault(section, {}).update(values)
    return manifest(**base)


def test_proprietary_nvidia_on_turing_and_newer_uses_branch_595():
    result = plan(nvidia_manifest(), profile(gpus=(gpu("nvidia", "turing+", name="AD102 [GeForce RTX 4090]"),)))
    assert result.driver_atoms == [NV]
    assert result.package_license[NV] == ["NVIDIA-2025"]
    assert result.package_use[NV] == ["dist-kernel", "wayland"]
    assert result.package_mask == []
    assert result.make_conf["VIDEO_CARDS"] == "${VIDEO_CARDS} nvidia"
    assert result.kernel_cmdline == ["nvidia-drm.modeset=1", "nvidia-drm.fbdev=1"]
    assert result.scalars["gpu_driver"] == "nvidia" and result.scalars["nvidia_branch"] == "595"
    assert ["sh", "-c", "if [ -f /etc/modprobe.d/nvidia.conf ]; then dracut --force --regenerate-all; fi"] in result.post_steps
    assert FW not in result.early_atoms or "NVIDIA" not in " ".join(result.scalars["firmware_reasons"])  # the open GSP firmware reason is dropped


def test_pascal_gets_the_long_term_branch_and_a_mask_that_pins_it():
    result = plan(nvidia_manifest(), profile(gpus=(gpu("nvidia", "maxwell-volta"),)))
    assert result.scalars["nvidia_branch"] == "580"
    assert result.package_mask == [">=x11-drivers/nvidia-drivers-581"]
    assert "kernel-open" not in result.package_use[NV]  # the open modules need Turing or newer


def test_branch_580_on_a_turing_card_uses_the_open_kernel_modules():
    result = plan(nvidia_manifest(hardware={"nvidia_branch": "580"}), profile(gpus=(gpu("nvidia", "turing+"),)))
    assert result.package_use[NV] == ["dist-kernel", "wayland", "kernel-open"] and result.package_mask


def test_a_mixed_pc_uses_580_which_drives_both_generations():
    result = plan(nvidia_manifest(), profile(gpus=(gpu("nvidia", "maxwell-volta", slot="0000:01:00.0"), gpu("nvidia", "turing+", slot="0000:02:00.0"))))
    assert result.scalars["nvidia_branch"] == "580" and "kernel-open" not in result.package_use[NV]


def test_wayland_flag_only_for_wayland_desktops():
    assert "wayland" not in plan(nvidia_manifest(system={"desktop": "xfce"}), profile(gpus=(gpu("nvidia", "turing+"),))).package_use[NV]
    assert "wayland" in plan(nvidia_manifest(system={"desktop": "gnome"}), profile(gpus=(gpu("nvidia", "turing+"),))).package_use[NV]


def test_proprietary_nvidia_with_source_builds_keeps_the_igpu_tokens():
    result = plan(nvidia_manifest(portage={"binhost": False}), profile(form="laptop", gpus=(gpu("intel", "gen8+", integrated=True), gpu("nvidia", "turing+"))))
    assert result.make_conf["VIDEO_CARDS"] == "intel nvidia"
    only = plan(nvidia_manifest(portage={"binhost": False}), profile(gpus=(gpu("nvidia", "turing+"),)))
    assert only.make_conf["VIDEO_CARDS"] == "nvidia"        # a source build of Mesa needs no other driver


@pytest.mark.parametrize(("mutation", "code"), [
    ({"system": {"secure_boot": True, "secure_boot_key": "k", "secure_boot_certificate": "c"}}, "hw.nvidia_secure_boot"),
    ({"system": {"kernel": "manual"}}, "hw.nvidia_kernel"),
    ({"system": {"initramfs": "genkernel"}}, "hw.nvidia_kernel"),
    ({"hardware": {"nvidia_branch": "470"}}, "hw.nvidia_branch_unavailable"),
])
def test_proprietary_nvidia_is_refused_for_incompatible_settings(mutation, code):
    machine = profile(gpus=(gpu("nvidia", "turing+"),))
    m = nvidia_manifest(**mutation)
    assert code in {issue.code for issue in hwplan.validate_plan(m, machine) if issue.severity == "error"}
    result = plan(m, machine)
    assert result.driver_atoms == [] and NV not in result.package_license and any(w.code == "hw.nvidia_skipped" for w in result.warnings)
    assert result.scalars["gpu_driver"] == "open" and "nvidia" not in result.make_conf.get("VIDEO_CARDS", "")


@pytest.mark.parametrize(("generation", "branch"), [("kepler", ""), ("fermi", ""), ("older", ""), ("maxwell-volta", "595")])
def test_unsupported_generations_are_errors(generation, branch):
    machine = profile(gpus=(gpu("nvidia", generation, name="Some Card"),))
    m = nvidia_manifest(hardware={"nvidia_branch": branch or "auto"})
    issues = hwplan.validate_plan(m, machine)
    assert "hw.nvidia_unsupported_gpu" in {i.code for i in issues if i.severity == "error"}
    assert plan(m, machine).driver_atoms == []


def test_no_nvidia_gpu_is_an_error_unless_portable():
    machine = profile(gpus=(gpu("amd", "gcn3+"),))
    assert "hw.nvidia_no_gpu" in {i.code for i in hwplan.validate_plan(nvidia_manifest(), machine)}
    portable = nvidia_manifest(hardware={"target": "portable"})
    assert not [i for i in hwplan.validate_plan(portable, machine) if i.severity == "error"]
    result = plan(portable, machine)
    assert result.driver_atoms == [NV] and result.scalars["nvidia_branch"] == "595"


def test_a_guessed_generation_is_flagged():
    machine = profile(gpus=(gpu("nvidia", "turing+", guess=True),))
    assert "hw.nvidia_generation_guess" in {i.code for i in hwplan.validate_plan(nvidia_manifest(), machine)}


def test_open_driver_is_the_default_even_with_an_nvidia_card():
    result = plan(manifest(), profile(gpus=(gpu("nvidia", "turing+"),)))
    assert result.driver_atoms == [] and NV not in result.package_license and result.kernel_cmdline == []
    assert result.scalars["gpu_driver"] == "open"


def test_nvk_only_when_mesa_is_built_anyway_or_asked_for():
    machine = profile(gpus=(gpu("nvidia", "turing+"),))
    source = plan(manifest(portage={"binhost": False}), machine)
    assert source.make_conf["VIDEO_CARDS"] == "nouveau nvk" and source.package_use["media-libs/mesa"] == ["vulkan"]
    assert "VIDEO_CARDS" not in plan(manifest(), machine).make_conf
    forced = plan(manifest(hardware={"nvk": "yes"}), machine)
    assert forced.make_conf["VIDEO_CARDS"] == "${VIDEO_CARDS} nvk" and any("NVK" in n for n in forced.notes)
    assert "nvk" not in plan(manifest(hardware={"nvk": "no"}, portage={"binhost": False}), machine).make_conf["VIDEO_CARDS"]
    assert plan(nvidia_manifest(hardware={"nvk": "yes"}), machine).scalars["nvk"] == "no"  # the proprietary driver replaces it


# -- hybrid graphics -----------------------------------------------------------------------------------

def test_hybrid_laptop_gets_switching_helpers_but_the_authors_desktop_does_not():
    igpu = gpu("intel", "gen8+", integrated=True, boot=False, outputs=0, slot="0000:00:02.0")
    dgpu = gpu("nvidia", "turing+", boot=True, outputs=2)
    desktop = plan(manifest(), profile(form="desktop", gpus=(igpu, dgpu)))
    assert "sys-power/switcheroo-control" not in desktop.optional_atoms and desktop.scalars["prime"] == "off" and desktop.scalars["hybrid"] is False
    laptop = plan(manifest(), profile(form="laptop", gpus=(igpu, dgpu)))
    assert "sys-power/switcheroo-control" in laptop.optional_atoms and ("switcheroo-control", "default") in laptop.optional_services
    assert laptop.scalars["prime"] == "offload" and laptop.scalars["hybrid"] is True


def test_prime_run_only_with_the_proprietary_driver_on_a_hybrid_laptop():
    igpu, dgpu = gpu("intel", "gen8+", integrated=True, boot=True), gpu("nvidia", "turing+")
    proprietary = plan(nvidia_manifest(), profile(form="laptop", gpus=(igpu, dgpu)))
    paths = {path: (content, mode) for path, content, mode in proprietary.files}
    assert paths["/usr/local/bin/prime-run"][1] == 0o755 and "__NV_PRIME_RENDER_OFFLOAD=1" in paths["/usr/local/bin/prime-run"][0]
    assert not [f for f in plan(manifest(), profile(form="laptop", gpus=(igpu, dgpu))).files if f[0].endswith("prime-run")]
    off = plan(nvidia_manifest(hardware={"prime": "off"}), profile(form="laptop", gpus=(igpu, dgpu)))
    assert "sys-power/switcheroo-control" not in off.optional_atoms and not [f for f in off.files if f[0].endswith("prime-run")]
    assert plan(manifest(hardware={"prime": "offload"}), profile(form="desktop", gpus=(dgpu,))).scalars["prime"] == "off"  # needs a second GPU


# -- HWMonux (COMP-NEW-3) --------------------------------------------------------------------------------

def test_hwmonux_gets_nvidia_sensors_only_after_the_proprietary_driver_installed():
    machine = profile(gpus=(gpu("nvidia", "turing+"),))
    with_driver = plan(nvidia_manifest(), machine)
    step = next(s for s in with_driver.post_steps if "hwmonux" in " ".join(s))
    assert step[:2] == ["sh", "-c"] and "/etc/modprobe.d/nvidia.conf" in step[2] and "app-misc/hwmonux nvidia" in step[2]
    # Never as a static USE flag: dev-python/nvidia-ml-py needs the licence-masked driver, so a failed driver build would abort the base stage.
    assert "app-misc/hwmonux" not in with_driver.package_use
    assert not [s for s in plan(manifest(), machine).post_steps if "hwmonux" in " ".join(s)]
    assert not [s for s in plan(nvidia_manifest(options={"install_hwmonux": False}), machine).post_steps if "hwmonux" in " ".join(s)]


# -- laptop power ---------------------------------------------------------------------------------------

def services(result, optional=True):
    return [name for name, _level in (result.optional_services if optional else result.services)]


@pytest.mark.parametrize(("desktop", "init", "expected_atom", "expected_services", "tlp_use"), [
    ("kde", "openrc", "sys-power/power-profiles-daemon", ["power-profiles-daemon"], None),
    ("gnome", "systemd", "sys-power/power-profiles-daemon", ["power-profiles-daemon"], None),
    ("xfce", "openrc", "sys-power/tlp", ["tlp"], ["-ppd"]),
    ("minimal", "systemd", "sys-power/tlp", ["tlp"], ["-ppd"]),
])
def test_laptop_power_management_is_ppd_for_kde_and_gnome_and_tlp_for_the_rest(desktop, init, expected_atom, expected_services, tlp_use):
    build = manifest if init == "openrc" else systemd
    result = plan(build(system={"desktop": desktop}, **quiet()), profile(form="laptop", vendor="amd"))
    powered = [a for a in result.optional_atoms if a in ("sys-power/tlp", "sys-power/power-profiles-daemon")]
    assert powered == [expected_atom]                      # never both
    assert [s for s in services(result) if s in ("tlp", "tlp-pd", "power-profiles-daemon")] == expected_services
    if tlp_use:
        assert "-ppd" in result.package_use["sys-power/tlp"]
    else:
        assert "sys-power/tlp" not in result.package_use


def test_tlp_without_networkmanager_drops_rdw_and_with_it_keeps_it():
    machine = profile(form="laptop")
    plain = plan(manifest(system={"desktop": "xfce"}, network={"manager": "dhcpcd"}, **quiet()), machine)
    assert plain.package_use["sys-power/tlp"] == ["-ppd", "-rdw"]
    nm = plan(manifest(system={"desktop": "xfce"}, **quiet()), machine)
    assert nm.package_use["sys-power/tlp"] == ["-ppd"]


def test_explicit_tlp_on_kde_keeps_the_ppd_interface_through_tlp_pd():
    result = plan(manifest(hardware={"power": "tlp"}, **quiet()), profile(form="laptop"))
    assert "sys-power/tlp" in result.optional_atoms and "sys-power/power-profiles-daemon" not in result.optional_atoms
    assert services(result)[:2] == ["tlp", "tlp-pd"] and "-ppd" not in result.package_use.get("sys-power/tlp", [])


@pytest.mark.parametrize("machine", [profile(form="desktop"), profile(hypervisor="kvm", gpus=(gpu("virtio"),)), profile(form="server")])
def test_no_power_management_on_desktops_servers_and_vms(machine):
    result = plan(manifest(**quiet()), machine)
    assert result.scalars["power"] == "none" and not [a for a in result.optional_atoms if "power" in a or "tlp" in a]


def test_portable_installs_do_not_pick_power_management_for_the_installing_laptop():
    assert plan(manifest(hardware={"target": "portable"}, **quiet()), profile(form="laptop")).scalars["power"] == "none"


def test_thermald_on_intel_laptops_only():
    assert "sys-power/thermald" in plan(manifest(**quiet()), profile(form="laptop", vendor="intel")).optional_atoms
    for machine in (profile(form="laptop", vendor="amd"), profile(form="desktop", vendor="intel"), profile(form="vm", hypervisor="qemu")):
        assert "sys-power/thermald" not in plan(manifest(**quiet()), machine).optional_atoms
    assert "sys-power/thermald" in plan(manifest(hardware={"thermald": "yes"}, **quiet()), profile(form="desktop", vendor="intel")).optional_atoms
    assert "sys-power/thermald" not in plan(manifest(hardware={"thermald": "no"}, **quiet()), profile(form="laptop", vendor="intel")).optional_atoms


def test_fingerprint_and_fwupd_are_opt_in():
    assert not {"sys-auth/fprintd", "sys-apps/fwupd"} & set(plan(manifest(), profile(form="laptop")).optional_atoms)
    result = plan(manifest(hardware={"fingerprint": True, "fwupd": True}), profile(form="laptop"))
    assert {"sys-auth/fprintd", "sys-apps/fwupd"} <= set(result.optional_atoms)


# -- TRIM -----------------------------------------------------------------------------------------------

def trim_files(result):
    return {path: (content, mode) for path, content, mode in result.files}


@pytest.mark.parametrize(("device", "weekly"), [
    (disk(rota=False), True),
    (disk("/dev/sda", bus="sata", rota=False), True),
    (disk("/dev/sda", bus="sata", rota=True), False),               # a spinning disk gains nothing
    (disk("/dev/sdb", bus="usb", rota=False, hotplug=True), False),  # USB sticks: TRIM is often unsupported or wears the medium
    (disk("/dev/mmcblk0", bus="sd", rota=False, hotplug=True), False),
    (disk("/dev/vda", bus="virtio", rota=True), False),
    (disk(rota=None), False),                                        # unknown: do nothing rather than guess
])
def test_trim_follows_the_target_disk(device, weekly):
    m = manifest(storage={"device": device.path}, **quiet())
    result = plan(m, profile(disks=(device,)))
    assert result.scalars["trim"] == ("weekly" if weekly else "off")
    assert ("/etc/cron.weekly/fstrim" in trim_files(result)) is weekly


def test_trim_on_openrc_is_a_weekly_cron_script():
    result = plan(manifest(**quiet()), profile())
    content, mode = trim_files(result)["/etc/cron.weekly/fstrim"]
    assert content == "#!/bin/sh\n# Weekly TRIM of mounted solid-state filesystems (written by the Gentoo installer).\nexec /sbin/fstrim --all --quiet-unsupported\n"
    assert mode == 0o755 and ("fstrim.timer", "default") not in result.services


def test_trim_on_systemd_enables_the_timer():
    result = plan(systemd(**quiet()), profile())
    assert ("fstrim.timer", "default") in result.services and "/etc/cron.weekly/fstrim" not in trim_files(result)


def test_trim_without_a_cron_daemon_adds_one():
    result = plan(manifest(options={"cron": False, "firmware": False, "microcode": False}), profile())
    assert "sys-process/cronie" in result.required_atoms and ("cronie", "default") in result.services
    assert "sys-process/cronie" not in plan(manifest(**quiet()), profile()).required_atoms  # the option already installs it
    assert "sys-process/cronie" not in plan(systemd(options={"cron": False, "firmware": False, "microcode": False}), profile()).required_atoms


def test_trim_and_encryption():
    luks = plan(manifest(storage={"luks": True, "luks_passphrase": "long-enough-pass"}, **quiet()), profile())
    assert luks.scalars["trim"] == "off" and luks.scalars["luks_allow_discards"] is False
    allowed = plan(manifest(storage={"luks": True, "luks_passphrase": "long-enough-pass"}, hardware={"luks_discard": True}, **quiet()), profile())
    assert allowed.scalars["trim"] == "weekly" and allowed.scalars["luks_allow_discards"] is True
    forced = plan(manifest(storage={"luks": True, "luks_passphrase": "long-enough-pass"}, hardware={"trim": "weekly"}, **quiet()), profile())
    assert forced.scalars["trim"] == "weekly" and any("discards" in n for n in forced.notes)


def test_trim_choices_and_btrfs_hint():
    assert plan(manifest(hardware={"trim": "off"}, **quiet()), profile()).scalars["trim"] == "off"
    hdd = disk("/dev/nvme0n1", bus="sata", rota=True)
    assert plan(manifest(hardware={"trim": "weekly"}, **quiet()), profile(disks=(hdd,))).scalars["trim"] == "weekly"   # an explicit choice is honoured
    btrfs = plan(manifest(storage={"filesystem": "btrfs"}, **quiet()), profile())
    assert btrfs.scalars["btrfs_discard_async"] is True      # render_fstab adds discard=async on flash
    assert plan(manifest(**quiet()), profile()).scalars["btrfs_discard_async"] is False
    assert plan(manifest(storage={"filesystem": "btrfs"}, **quiet()), profile(disks=(hdd,))).scalars["btrfs_discard_async"] is False


def test_manual_layout_on_two_disks_trims_if_any_is_flash():
    from gentoo_installer.manifest import Partition

    ssd, hdd = disk("/dev/nvme0n1"), disk("/dev/sda", bus="sata", rota=True)
    m = manifest(storage={"mode": "manual", "device": "/dev/sda", "partitions": [Partition("/dev/sda1", "/", "ext4")]}, **quiet())
    assert plan(m, profile(disks=(ssd, hdd))).scalars["trim"] == "off"          # everything is on the spinning disk
    m.storage.partitions.append(Partition("/dev/nvme0n1p1", "/home", "ext4"))
    assert plan(m, profile(disks=(ssd, hdd))).scalars["trim"] == "weekly"       # a /home on flash makes weekly TRIM useful


# -- zram -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("ram", "expected"), [(1.9, "yes"), (3.8, "yes"), (7.6, "yes"), (7.99, "yes"), (8.0, "no"), (15.5, "no"), (46.7, "no")])
def test_zram_auto_threshold(ram, expected):
    assert plan(manifest(**quiet()), profile(ram_gib=ram)).scalars["zram"] == expected


def test_zram_openrc_configuration():
    result = plan(manifest(**quiet()), profile(ram_gib=4))
    assert "sys-block/zram-init" in result.optional_atoms and ("zram-init", "default") in result.optional_services
    content = trim_files(result)["/etc/conf.d/zram-init"][0]
    assert 'type0="swap"' in content and f'size0="{result.scalars["zram_mib"]}"' in content and 'algo0="zstd"' in content
    assert result.scalars["zram_mib"] == 2048     # 50% of 4 GiB


def test_zram_systemd_configuration_adapts_to_the_machine_at_boot():
    result = plan(systemd(hardware={"zram_percent": 25}, **quiet()), profile(ram_gib=4))
    assert "sys-apps/zram-generator" in result.optional_atoms and not result.optional_services
    assert "zram-size = min(ram * 25 / 100, 16384)" in trim_files(result)["/etc/systemd/zram-generator.conf"][0]


def test_zram_size_is_capped_and_can_be_forced_or_disabled():
    assert plan(manifest(hardware={"zram": "yes", "zram_percent": 100}, **quiet()), profile(ram_gib=64)).scalars["zram_mib"] == 16384
    assert "zram" not in " ".join(plan(manifest(hardware={"zram": "no"}, **quiet()), profile(ram_gib=2)).optional_atoms)


def test_portable_openrc_does_not_size_zram_for_the_installing_machine():
    assert plan(manifest(hardware={"target": "portable"}, **quiet()), profile(ram_gib=4)).scalars["zram"] == "no"
    assert plan(systemd(hardware={"target": "portable"}, **quiet()), profile(ram_gib=4)).scalars["zram"] == "yes"


# -- guest tools --------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("hypervisor", "init", "desktop", "atoms", "svcs"), [
    ("qemu", "openrc", "kde", ["app-emulation/qemu-guest-agent", "app-emulation/spice-vdagent"], ["qemu-guest-agent", "spice-vdagent"]),
    ("kvm", "openrc", "minimal", ["app-emulation/qemu-guest-agent"], ["qemu-guest-agent"]),
    ("qemu", "systemd", "xfce", ["app-emulation/qemu-guest-agent", "app-emulation/spice-vdagent"], ["qemu-guest-agent", "spice-vdagentd.socket"]),
    ("vmware", "openrc", "kde", ["app-emulation/open-vm-tools"], ["vmware-tools"]),
    ("vmware", "systemd", "kde", ["app-emulation/open-vm-tools"], ["vmtoolsd", "vgauthd"]),
    ("virtualbox", "openrc", "kde", ["app-emulation/virtualbox-guest-additions"], ["virtualbox-guest-additions"]),
])
def test_guest_tools_per_hypervisor(hypervisor, init, desktop, atoms, svcs):
    build = manifest if init == "openrc" else systemd
    result = plan(build(system={"desktop": desktop}, **quiet()), profile(hypervisor=hypervisor, gpus=(gpu(hypervisor if hypervisor != "kvm" else "qemu"),)))
    assert [a for a in result.optional_atoms if a in atoms or "emulation" in a] == atoms
    assert [s for s in services(result) if s not in ("zram-init",)] == svcs
    assert not [a for a in result.required_atoms + result.early_atoms if "emulation" in a]   # guest tools never abort an install


def test_virtualbox_additions_use_the_gui_flag_only_with_a_desktop():
    machine = profile(hypervisor="virtualbox", gpus=(gpu("virtualbox"),))
    assert plan(manifest(**quiet()), machine).package_use["app-emulation/virtualbox-guest-additions"] == ["gui"]
    assert "app-emulation/virtualbox-guest-additions" not in plan(manifest(system={"desktop": "minimal"}, **quiet()), machine).package_use


@pytest.mark.parametrize("hypervisor", ["hyperv", "xen", "parallels", "bhyve", "other"])
def test_no_guest_tools_where_the_tree_has_none(hypervisor):
    result = plan(manifest(**quiet()), profile(hypervisor=hypervisor, gpus=(gpu("hyperv"),)))
    assert not [a for a in result.optional_atoms if "emulation" in a] and any("No guest tools" in n for n in result.notes)


def test_guest_tools_can_be_switched_off():
    result = plan(manifest(hardware={"guest_tools": "none"}, **quiet()), profile(hypervisor="qemu", gpus=(gpu("virtio"),)))
    assert not [a for a in result.optional_atoms if "emulation" in a]


# -- printers, audio ----------------------------------------------------------------------------------------------

def test_printer_drivers():
    result = plan(manifest(hardware={"printer_drivers": ["gutenprint", "hplip"]}), profile())
    assert {"net-print/gutenprint", "net-print/hplip"} <= set(result.optional_atoms)
    assert result.package_use["net-dns/avahi"] == ["python"]            # hplip needs avahi[python]
    assert result.package_use["net-print/gutenprint"] == ["cups"]
    assert not result.required_atoms                                       # printing is already on by default
    no_cups = plan(manifest(hardware={"printer_drivers": ["hplip"]}, options={"printing": False}), profile())
    assert "net-print/cups" in no_cups.required_atoms and ("cupsd", "default") in no_cups.services
    assert "net-dns/avahi" not in plan(manifest(hardware={"printer_drivers": ["gutenprint"]}), profile()).package_use
    assert ("cups", "default") in plan(systemd(hardware={"printer_drivers": ["hplip"]}, options={"printing": False}), profile()).services


def test_pipewire_gets_its_sound_server():
    result = plan(manifest(), profile())
    assert result.package_use["media-video/pipewire"] == ["sound-server"]
    step = next(s for s in result.post_steps if "pipewire" in " ".join(s))
    assert step[-2:] == ["sh", "gentoo"] and "getent group pipewire" in step[2]          # best effort: never fails the install
    assert "media-video/pipewire" not in plan(manifest(options={"pipewire": False}), profile()).package_use
    assert "pipewire-alsa" in plan(manifest(hardware={"pipewire_alsa": True}), profile()).package_use["media-video/pipewire"]


# -- boot, display -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("m_kwargs", "machine", "expected"), [
    ({}, profile(), "always"),                                                                  # erase mode creates the ESP
    ({"hardware": {"target": "portable"}}, profile(), "always"),
    ({"hardware": {"removable_fallback": "never"}}, profile(), "never"),
    ({"hardware": {"removable_fallback": "if-missing"}}, profile(), "if-missing"),
    ({"storage": {"firmware": "bios"}}, profile(firmware="bios"), "never"),                      # BIOS has no EFI fallback
    ({}, profile(firmware="bios"), "always"),                                                    # UEFI target from a BIOS-booted session
    ({"system": {"bootloader": "efi-stub"}}, profile(), "never"),                                 # a bare kernel cannot carry its command line
    ({"system": {"bootloader": "efi-stub", "uki": True}}, profile(), "always"),
    ({"storage": {"device": "/dev/sdb"}}, profile(disks=(disk("/dev/sdb", bus="usb", hotplug=True),)), "always"),
])
def test_removable_fallback_policy(m_kwargs, machine, expected):
    assert plan(manifest(**m_kwargs), machine).scalars["removable_fallback"] == expected


def test_manual_mode_with_a_preserved_esp_only_fills_the_fallback_if_missing():
    from gentoo_installer.manifest import Partition
    keep = manifest(storage={"mode": "manual", "partitions": [Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False), Partition("/dev/nvme0n1p2", "/", "ext4")]})
    fresh = manifest(storage={"mode": "manual", "partitions": [Partition("/dev/nvme0n1p1", "/efi", "vfat", format=True), Partition("/dev/nvme0n1p2", "/", "ext4")]})
    assert plan(keep, profile()).scalars["removable_fallback"] == "if-missing"
    assert plan(fresh, profile()).scalars["removable_fallback"] == "always"


def display(dpi: int, internal: bool = True):
    return hw.DisplayInfo(connector="eDP-1", internal=internal, width=1920, height=1080, mm_width=300, mm_height=170, dpi=dpi)


@pytest.mark.parametrize(("dpi", "expected"), [(96, "off"), (111, "off"), (125, "dpi:120"), (141, "dpi:144"), (158, "dpi:144"), (163, "dpi:168"), (284, "dpi:240"), (0, "off")])
def test_hidpi_auto_picks_a_scale_step(dpi, expected):
    assert plan(manifest(), profile(displays=(display(dpi),))).scalars["hidpi"] == expected


def test_hidpi_choices_and_the_xfce_file():
    machine = profile(displays=(display(163),))
    assert plan(manifest(hardware={"hidpi": "off"}), machine).scalars["hidpi"] == "off"
    assert plan(manifest(hardware={"hidpi": "dpi:192"}), machine).scalars["hidpi"] == "dpi:192"
    kde = plan(manifest(), machine)
    assert kde.skel_files() == []                       # Plasma and GNOME scale per screen themselves
    xfce = plan(manifest(system={"desktop": "xfce"}), machine)
    path, content, mode = xfce.skel_files()[0]
    assert path == "/etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml" and mode == 0o644
    assert '<property name="DPI" type="int" value="168"/>' in content and content.startswith("<?xml")
    assert all(not f[0].startswith("/etc/skel/") for f in xfce.system_files())


def test_the_primary_display_is_the_internal_panel():
    external = hw.DisplayInfo(connector="DP-1", internal=False, width=3840, height=2160, mm_width=600, mm_height=340, dpi=161)
    assert plan(manifest(), profile(displays=(external, display(100)))).scalars["hidpi"] == "off"


# -- the user's own package.use wins -------------------------------------------------------------------------------

def test_an_explicit_user_flag_beats_the_plan_and_is_reported():
    machine = profile(form="laptop")
    m = manifest(system={"desktop": "xfce"}, portage={"package_use": {"sys-power/tlp": ["ppd"]}}, **quiet())
    result = plan(m, machine)
    assert "-ppd" not in result.package_use.get("sys-power/tlp", [])
    assert any(w.code == "hw.package_use_conflict" and "sys-power/tlp" in w.message for w in result.warnings)


# -- invariants over a grid of machines and settings ------------------------------------------------------------------

ATOM = __import__("re").compile(r"[A-Za-z0-9_+.-]+/[A-Za-z0-9_+.-]+(?::[A-Za-z0-9_.-]+)?")
FLAG = __import__("re").compile(r"-?[A-Za-z0-9_+@-]+")


def grid():
    from tests.hardware.helpers import FIXTURE_NAMES, fixture_dict

    machines = [fixture_dict(name) for name in FIXTURE_NAMES] + [{}]
    for init in ("openrc", "systemd"):
        for desktop in ("kde", "xfce", "minimal"):
            for binhost in (True, False):
                for target in ("this", "portable"):
                    for gpu_driver in ("auto", "nvidia"):
                        for power in ("auto", "tlp", "ppd"):
                            yield init, desktop, binhost, target, gpu_driver, power, machines


def grid_plans():
    for init, desktop, binhost, target, gpu_driver, power, machines in grid():
        m = (manifest if init == "openrc" else systemd)(
            system={"desktop": desktop, "kernel": "gentoo-kernel-bin"}, portage={"binhost": binhost},
            hardware={"target": target, "gpu_driver": gpu_driver, "nvidia_license_accepted": True, "power": power, "printer_drivers": ["hplip"], "fwupd": True})
        for machine in machines:
            yield m, machine, hwplan.plan(m, machine)


def test_invariants_hold_for_every_machine_and_setting():
    count = 0
    for m, machine, result in grid_plans():
        count += 1
        atoms = result.all_atoms()
        assert not {"sys-power/tlp", "sys-power/power-profiles-daemon"} <= set(atoms)
        assert all(ATOM.fullmatch(atom) for atom in atoms), atoms
        assert all(ATOM.fullmatch(atom) and all(FLAG.fullmatch(flag) for flag in flags) for atom, flags in result.package_use.items())
        assert all(ATOM.fullmatch(atom) and all(item for item in items) for atom, items in result.package_license.items())
        assert "ACCEPT_LICENSE" not in result.make_conf and all(value.strip() for value in result.make_conf.values())
        assert "vmware" not in result.make_conf.get("VIDEO_CARDS", "").split()
        if result.scalars["microcode"] in ("intel", "none"):
            assert A_UCODE_AMD_FREE(result)
        if machine and machine["machine"]["hypervisor"] and not m.hardware.target == "portable":
            assert UC not in atoms
        if machine and machine["cpu"]["vendor"] == "amd" and m.hardware.target == "this":
            assert UC not in atoms
        assert len(result.services) == len(set(result.services)) and len(result.optional_services) == len(set(result.optional_services))
        assert all(path.startswith("/") and ".." not in path and 0 <= mode <= 0o777 for path, _content, mode in result.files)
        assert all(step and all(isinstance(part, str) and part and "\n" not in part for part in step) for step in result.post_steps)
        assert all(token for token in result.kernel_cmdline) and len(result.kernel_cmdline) == len(set(result.kernel_cmdline))
        assert result.to_dict() == hwplan.HardwarePlan.from_dict(result.to_dict()).to_dict()
        if result.driver_atoms:
            assert result.package_license[NV] == ["NVIDIA-2025"] and "dist-kernel" in result.package_use[NV]
        else:
            assert NV not in result.package_license and not result.kernel_cmdline
        assert set(result.skel_files()) | set(result.system_files()) == set(result.files)
    assert count > 1000


def A_UCODE_AMD_FREE(result):
    return result.scalars["microcode"] != "both"


def test_planning_is_deterministic():
    machine = profile(form="laptop", gpus=(gpu("intel", "gen8+", integrated=True, boot=True), gpu("nvidia", "turing+")))
    first, second = plan(nvidia_manifest(), machine), plan(nvidia_manifest(), machine)
    assert first.to_dict() == second.to_dict()


def test_a_missing_or_partial_profile_degrades_to_safe_defaults():
    for machine in ({}, None, {"cpu": {"vendor": "intel"}}, {"gpus": [{"vendor": "nvidia"}]}, {"disks": "junk"}):
        result = plan(manifest(), machine)  # type: ignore[arg-type]
        assert isinstance(result, hwplan.HardwarePlan) and "CPU_FLAGS_X86" not in result.make_conf
    assert any("detection was not available" in n for n in plan(manifest(), {}).notes)
    assert hwplan.validate_plan(manifest(), {}) == []        # no probe, nothing to cross-check


def test_a_hardware_profile_object_is_accepted_as_well_as_its_dict():
    machine = profile(vendor="amd")
    assert plan(manifest(), hw.HardwareProfile.from_dict(machine)).to_dict() == plan(manifest(), machine).to_dict()


def test_plan_serialisation_redacts_file_contents_on_request():
    result = plan(manifest(**quiet()), profile())
    redacted = result.to_dict(redact_files=True)["files"][0]
    assert set(redacted) == {"path", "mode", "size", "sha256"} and len(redacted["sha256"]) == 64
    assert "fstrim" not in str(redacted) or redacted["path"].endswith("fstrim")
    restored = hwplan.HardwarePlan.from_dict(result.to_dict())
    assert restored.files == result.files and restored.services == result.services and restored.summary == result.summary


def test_summary_lines_are_short_human_sentences():
    result = plan(nvidia_manifest(), profile(form="laptop", gpus=(gpu("intel", "gen8+", integrated=True, boot=True, name="TigerLake-H GT1 [UHD Graphics]"),
                                                                    gpu("nvidia", "turing+", name="GA104GLM [RTX A5000 Mobile]"))))
    assert "Graphics: NVIDIA RTX A5000 Mobile - proprietary driver 595" in result.summary
    assert "Graphics: Intel UHD Graphics - open Intel driver" in result.summary
    assert all(len(line) < 200 and not line.endswith("\n") for line in result.summary)


# -- boot target, 32-bit UEFI --------------------------------------------------------------------------------

def test_grub_target_follows_the_firmware():
    assert plan(manifest(), profile()).scalars["grub_target"] == "x86_64-efi"
    assert plan(manifest(storage={"firmware": "bios"}), profile(firmware="bios")).scalars["grub_target"] == "i386-pc"


def test_32_bit_uefi_adds_the_efi_32_grub_platform():
    machine = profile()
    machine["firmware"]["efi_bits"] = 32
    result = plan(manifest(), machine)
    assert result.make_conf["GRUB_PLATFORMS"] == "pc efi-64 efi-32" and result.scalars["grub_target"] == "i386-efi"
    assert any("32-bit UEFI" in note for note in result.notes)
    assert "GRUB_PLATFORMS" not in plan(manifest(system={"bootloader": "systemd-boot"}), machine).make_conf    # only GRUB has the platform list
    assert "GRUB_PLATFORMS" not in plan(manifest(), profile()).make_conf
    assert "GRUB_PLATFORMS" not in plan(manifest(storage={"firmware": "bios"}), machine).make_conf
