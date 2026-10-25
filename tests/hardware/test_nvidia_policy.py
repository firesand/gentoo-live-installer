"""The NVIDIA choice model (B03), plan details (B04), runtime power management (B08), the temporary-file check (B22), the add-ons (B23)
and the Secure Boot refusal (B07)."""

from __future__ import annotations

import pytest

from gentoo_installer import hardware_data as data
from gentoo_installer.manifest import InstallManifest
from gentoo_installerd import hwplan

from tests.hardware.builders import gpu, manifest, profile, systemd

NV = "x11-drivers/nvidia-drivers"


def auto(**more):
    """The wizard's Guided choice: Automatic with the NVIDIA licence ticked."""
    base = {"hardware": {"nvidia_license_accepted": True}, "system": {"kernel": "gentoo-kernel-bin"}}
    for section, values in more.items():
        base.setdefault(section, {}).update(values)
    return manifest(**base)


def laptop(*cards, **more):
    return profile(form="laptop", gpus=(gpu("intel", "gen8+", integrated=True, boot=True), *cards), **more)


def dgpu(generation, **extra):
    return gpu("nvidia", generation, outputs=extra.pop("outputs", 0), **extra)


# -- B03: the Automatic table ----------------------------------------------------------------------------

@pytest.mark.parametrize(("machine", "branch", "flavour"), [
    pytest.param(profile(gpus=(gpu("nvidia", "turing"),)), "595", "open", id="desktop-turing"),
    pytest.param(profile(gpus=(gpu("nvidia", "ampere_plus"),)), "595", "open", id="desktop-ada"),
    pytest.param(profile(gpus=(gpu("nvidia", "blackwell"),)), "595", "open", id="desktop-blackwell"),
    pytest.param(profile(gpus=(gpu("nvidia", "turing+"),)), "595", "open", id="desktop-coarse-turing-plus"),
    pytest.param(profile(gpus=(gpu("nvidia", "maxwell-volta"),)), "580", "closed", id="desktop-pascal"),
    pytest.param(profile(gpus=(gpu("nvidia", "maxwell-volta", slot="0000:01:00.0"), gpu("nvidia", "turing", slot="0000:02:00.0"))), "580", "closed", id="pascal-and-turing"),
    pytest.param(laptop(dgpu("ampere_plus")), "595", "open", id="hybrid-ampere"),
    pytest.param(laptop(dgpu("turing")), "595", "open", id="hybrid-turing"),
    pytest.param(laptop(dgpu("blackwell")), "595", "open", id="hybrid-blackwell"),
    pytest.param(laptop(dgpu("maxwell-volta")), "580", "closed", id="hybrid-pascal"),
    pytest.param(profile(hypervisor="kvm", gpus=(gpu("nvidia", "ampere_plus"),)), "595", "open", id="vm-with-a-passed-through-card"),
])
def test_automatic_with_the_licence_picks_the_branch_and_the_modules(machine, branch, flavour):
    decision = hwplan.nvidia_decision(auto(), hwplan._profile(machine))
    assert (decision.branch, decision.flavour, decision.mode, decision.errors) == (branch, flavour, "nvidia", [])
    assert decision.reason
    result = hwplan.plan(auto(), machine)
    assert result.driver_atoms == [NV] and result.scalars["nvidia_branch"] == branch and result.scalars["nvidia_flavour"] == flavour
    assert ("kernel-open" in result.package_use[NV]) is False           # 595 has no such flag; 580 closed does not use it
    assert bool(result.package_mask) is (branch == "580")


@pytest.mark.parametrize("machine", [
    pytest.param(profile(gpus=(gpu("nvidia", "kepler"),)), id="kepler"),
    pytest.param(profile(gpus=(gpu("nvidia", "fermi"),)), id="fermi"),
    pytest.param(profile(gpus=(gpu("nvidia", "older"),)), id="older"),
    pytest.param(profile(hypervisor="kvm", gpus=(gpu("virtio"),)), id="vm"),
    pytest.param(profile(gpus=(gpu("amd", "gcn3+"),)), id="no-nvidia-card"),
])
def test_automatic_uses_nouveau_where_no_proprietary_driver_fits_and_does_not_complain(machine):
    result = hwplan.plan(auto(), machine)
    assert result.driver_atoms == [] and result.scalars["gpu_driver"] == "open"
    assert [issue for issue in hwplan.validate_plan(auto(), machine) if issue.severity == "error"] == []


def test_the_blackwell_card_next_to_an_older_one_is_an_error_with_its_own_code():
    machine = profile(gpus=(gpu("nvidia", "maxwell-volta", slot="0000:01:00.0"), gpu("nvidia", "blackwell", slot="0000:02:00.0")))
    for m in (auto(), auto(hardware={"gpu_driver": "nvidia"})):
        assert [issue.code for issue in hwplan.validate_plan(m, machine) if issue.severity == "error"] == ["hw.nvidia_mixed_unsupported"]
    assert hwplan.plan(auto(), machine).driver_atoms == []


def test_without_the_licence_automatic_is_nouveau_and_says_why():
    machine = profile(gpus=(gpu("nvidia", "ampere_plus", name="AD102 [GeForce RTX 4090]"),))
    m = manifest()
    result = hwplan.plan(m, machine)
    assert result.driver_atoms == [] and result.scalars["gpu_driver"] == "open" and result.kernel_cmdline == []
    skipped = [w for w in result.warnings if w.code == "hw.nvidia_skipped"]
    assert len(skipped) == 1 and skipped[0].severity == "info" and "licence" in skipped[0].message
    decision = hwplan.nvidia_decision(m, hwplan._profile(machine))
    assert decision.available == ("595", "open") and decision.branch == ""
    assert [issue for issue in hwplan.validate_plan(m, machine) if issue.severity == "error"] == []


def test_a_forced_choice_is_never_overridden_by_automatic():
    machine = profile(gpus=(gpu("nvidia", "ampere_plus"),))
    assert hwplan.plan(auto(hardware={"gpu_driver": "nouveau"}), machine).driver_atoms == []
    assert hwplan.plan(auto(hardware={"gpu_driver": "basic"}), machine).driver_atoms == []
    assert hwplan.plan(auto(hardware={"gpu_driver": "nvidia"}), machine).driver_atoms == [NV]


def test_a_secure_boot_or_kernel_problem_lowers_automatic_to_nouveau_but_blocks_an_explicit_choice():
    machine = profile(gpus=(gpu("nvidia", "ampere_plus"),))
    signing = {"system": {"secure_boot": True, "secure_boot_key": "/k", "secure_boot_certificate": "/c"}}
    automatic = auto(**signing)
    assert hwplan.plan(automatic, machine).driver_atoms == []
    assert any(w.code == "hw.nvidia_skipped" and w.severity == "warning" and "Secure Boot" in w.message for w in hwplan.plan(automatic, machine).warnings)
    assert [i for i in hwplan.validate_plan(automatic, machine) if i.severity == "error"] == []
    explicit = auto(hardware={"gpu_driver": "nvidia"}, **signing)
    assert "hw.nvidia_secure_boot" in {i.code for i in hwplan.validate_plan(explicit, machine) if i.severity == "error"}


@pytest.mark.parametrize(("hardware", "machine", "code"), [
    ({"nvidia_modules": "closed"}, profile(gpus=(gpu("nvidia", "blackwell"),)), "hw.nvidia_closed_blackwell"),
    ({"nvidia_modules": "open"}, profile(gpus=(gpu("nvidia", "maxwell-volta"),)), "hw.nvidia_open_unsupported_gpu"),
    ({"nvidia_modules": "closed", "nvidia_branch": "595"}, profile(gpus=(gpu("nvidia", "turing"),)), "hw.nvidia_modules_branch"),
    ({"nvidia_branch": "595"}, profile(gpus=(gpu("nvidia", "maxwell-volta"),)), "hw.nvidia_unsupported_gpu"),
    ({"nvidia_branch": "580"}, profile(gpus=(gpu("nvidia", "blackwell"),)), "hw.nvidia_unsupported_gpu"),
    ({"nvidia_branch": "535"}, profile(gpus=(gpu("nvidia", "turing"),)), "hw.nvidia_branch_unavailable"),
    ({"nvidia_branch": "470"}, profile(gpus=(gpu("nvidia", "kepler"),)), "hw.nvidia_branch_unavailable"),
])
def test_impossible_module_and_branch_choices_are_validation_errors(hardware, machine, code):
    m = auto(hardware={"gpu_driver": "nvidia", **hardware})
    assert code in {issue.code for issue in hwplan.validate_plan(m, machine) if issue.severity == "error"}
    assert hwplan.plan(m, machine).driver_atoms == []


def test_the_open_modules_on_a_turing_card_can_ride_on_the_long_term_branch():
    m = auto(hardware={"gpu_driver": "nvidia", "nvidia_modules": "open", "nvidia_branch": "580"})
    result = hwplan.plan(m, profile(gpus=(gpu("nvidia", "turing"),)))
    assert result.scalars["nvidia_flavour"] == "open" and "kernel-open" in result.package_use[NV]


def test_the_closed_modules_are_a_turing_option_on_the_long_term_branch():
    m = auto(hardware={"gpu_driver": "nvidia", "nvidia_modules": "closed"})
    result = hwplan.plan(m, profile(gpus=(gpu("nvidia", "turing"),)))
    assert result.scalars["nvidia_branch"] == "580" and result.scalars["nvidia_flavour"] == "closed" and "kernel-open" not in result.package_use[NV]


def test_a_saved_manifest_with_the_old_open_choice_loads_as_nouveau():
    loaded = InstallManifest.from_dict({"hardware": {"gpu_driver": "open"}})
    assert loaded.hardware.gpu_driver == "nouveau"
    assert hwplan.plan(loaded, profile(gpus=(gpu("nvidia", "ampere_plus"),))).driver_atoms == []


# -- B04: the plan details -------------------------------------------------------------------------------

def test_no_nvidia_plan_carries_the_fbdev_argument_and_modeset_stays_on_580_only():
    for generation, expected in (("ampere_plus", []), ("blackwell", []), ("maxwell-volta", ["nvidia-drm.modeset=1"])):
        result = hwplan.plan(auto(), profile(gpus=(gpu("nvidia", generation),)))
        assert result.kernel_cmdline == expected and not [arg for arg in result.kernel_cmdline if "fbdev" in arg]


@pytest.mark.parametrize(("desktop", "wayland"), [("kde", True), ("gnome", True), ("gnome-light", True), ("sway", True), ("xfce", False), ("i3", False), ("minimal", False)])
def test_the_wayland_use_flag_follows_the_desktops_that_have_a_wayland_session(desktop, wayland):
    result = hwplan.plan(auto(system={"desktop": desktop}), profile(gpus=(gpu("nvidia", "ampere_plus"),)))
    assert ("wayland" in result.package_use[NV]) is wayland


def test_dist_kernel_is_set_for_the_nvidia_driver_only_never_globally():
    result = hwplan.plan(auto(), profile(gpus=(gpu("nvidia", "ampere_plus"),)))
    assert result.package_use[NV][0] == "dist-kernel" and "*/*" not in result.package_use
    assert [atom for atom, flags in result.package_use.items() if "dist-kernel" in flags] == [NV]
    assert not hwplan.plan(auto(), profile(gpus=(gpu("amd", "gcn3+"),))).package_use.get("*/*")


def test_the_safe_graphics_entry_is_always_planned_with_the_driver_and_otherwise_not():
    machine = profile(gpus=(gpu("nvidia", "ampere_plus"),))
    result = hwplan.plan(auto(), machine)
    entry = result.scalars["safe_graphics"]
    assert entry["title"] == "Gentoo Linux (safe graphics)" and "nomodeset" in entry["cmdline"]
    assert any("nvidia_drm" in argument for argument in entry["cmdline"])
    forced = hwplan.plan(auto(hardware={"safe_graphics_entry": False}), machine)
    assert forced.scalars["safe_graphics"] == entry and any(w.code == "hw.safe_graphics_required" for w in forced.warnings)
    assert "safe_graphics" not in hwplan.plan(manifest(), machine).scalars
    assert "safe_graphics" not in hwplan.plan(auto(), profile(gpus=(gpu("amd", "gcn3+"),))).scalars


def test_a_failed_build_leaves_a_retry_hint_for_first_steps_txt():
    result = hwplan.plan(auto(accounts={"username": "ada"}), profile(gpus=(gpu("nvidia", "ampere_plus"),)))
    hint = next(step for step in result.post_steps if "first-steps.txt" in step[2])
    assert "/etc/modprobe.d/nvidia.conf" in hint[2] and hint[3:5] == ["sh", "ada"]
    assert "emerge --ask x11-drivers/nvidia-drivers" in hint[5] and "download.nvidia.com" in hint[5]
    assert hint[2].rstrip().endswith("exit 0")                       # best effort, never a failure
    assert result.scalars["first_user"] == "ada"
    assert any("entry that says safe graphics" in line for line in result.first_steps)
    assert any("@module-rebuild" in line for line in result.first_steps)


def test_the_summary_tells_about_the_local_compile_and_the_gnome_rebuild():
    kde = hwplan.plan(auto(), profile(gpus=(gpu("nvidia", "ampere_plus"),)))
    assert any("compiled on this PC" in note and "download.nvidia.com" in note for note in kde.notes)
    assert not any("mutter" in note for note in kde.notes)
    gnome = hwplan.plan(auto(system={"desktop": "gnome"}), profile(gpus=(gpu("nvidia", "ampere_plus"),)))
    assert any("mutter" in note for note in gnome.notes)


def test_basic_mode_blacklists_the_drivers_and_removes_the_wayland_sessions():
    machine = profile(gpus=(gpu("nvidia", "ampere_plus"),))
    kde = hwplan.plan(manifest(hardware={"gpu_driver": "basic"}), machine)
    files = {path: content for path, content, _mode in kde.files}
    assert "nomodeset" in kde.kernel_cmdline and kde.driver_atoms == [] and kde.scalars["gpu_mode"] == "basic"
    assert "blacklist nouveau" in files["/etc/modprobe.d/installer-basic-graphics.conf"] and "blacklist nvidia" in files["/etc/modprobe.d/installer-basic-graphics.conf"]
    assert "DisplayServer=x11" in files["/etc/sddm.conf.d/10-installer-x11.conf"]
    gnome = hwplan.plan(manifest(hardware={"gpu_driver": "basic"}, system={"desktop": "gnome"}), machine)
    assert "WaylandEnable=false" in {path: content for path, content, _mode in gnome.files}["/etc/gdm/custom.conf"]
    assert "nvk" not in hwplan.plan(manifest(hardware={"gpu_driver": "basic"}, portage={"binhost": False}), machine).make_conf.get("VIDEO_CARDS", "")
    sway = manifest(hardware={"gpu_driver": "basic"}, system={"desktop": "sway"})
    assert "hw.basic_wayland_only" in {i.code for i in hwplan.validate_plan(sway, machine) if i.severity == "error"}


def test_nvk_is_offered_for_kepler_and_newer_in_a_source_build():
    for generation, expected in (("kepler", "nouveau nvk"), ("maxwell-volta", "nouveau nvk"), ("turing", "nouveau nvk"), ("fermi", "nouveau")):
        result = hwplan.plan(manifest(portage={"binhost": False}), profile(gpus=(gpu("nvidia", generation),)))
        assert result.make_conf["VIDEO_CARDS"] == expected, generation


# -- B08: hybrid laptops ----------------------------------------------------------------------------------

def files_of(result):
    return {path: content for path, content, _mode in result.files}


def test_runtime_power_management_only_for_a_turing_hybrid_laptop_with_closed_modules():
    closed = auto(hardware={"nvidia_modules": "closed"})
    on = hwplan.plan(closed, laptop(dgpu("turing")))
    files = files_of(on)
    assert 'NVreg_DynamicPowerManagement=0x02' in files["/etc/modprobe.d/nvidia-pm.conf"]
    rules = files["/etc/udev/rules.d/80-nvidia-pm.rules"]
    assert '0x030000' in rules and '0x030200' in rules and 'ATTR{power/control}="auto"' in rules and 'ATTR{power/control}="on"' in rules
    assert on.scalars["nvidia_rtd3"] == "on" and any("/etc/modprobe.d/nvidia-pm.conf" in line for line in on.first_steps)
    assert "/etc/modprobe.d/nvidia.conf" not in files                    # the packaged file is never overwritten


@pytest.mark.parametrize(("label", "m", "machine"), [
    ("ampere hybrid", auto(hardware={"nvidia_modules": "closed"}), laptop(dgpu("ampere_plus"))),
    ("turing, open modules", auto(), laptop(dgpu("turing"))),
    ("turing desktop", auto(hardware={"nvidia_modules": "closed"}), profile(gpus=(gpu("nvidia", "turing"),))),
    ("turing with the screen on the nvidia card (MUX)", auto(hardware={"nvidia_modules": "closed"}), laptop(dgpu("turing", outputs=2))),
    ("turing laptop with only the nvidia card", auto(hardware={"nvidia_modules": "closed"}), profile(form="laptop", gpus=(gpu("nvidia", "turing"),))),
    ("switched off", auto(hardware={"nvidia_modules": "closed", "nvidia_rtd3": "off"}), laptop(dgpu("turing"))),
])
def test_no_runtime_power_management_files_elsewhere(label, m, machine):
    result = hwplan.plan(m, machine)
    assert "/etc/modprobe.d/nvidia-pm.conf" not in files_of(result), label
    assert "/etc/udev/rules.d/80-nvidia-pm.rules" not in files_of(result), label
    assert not [e for e in hwplan.validate_plan(m, machine) if e.severity == "error"], label


def test_asking_for_runtime_power_management_where_it_cannot_work_is_refused_with_the_reason():
    for m, machine, fragment in (
        (auto(hardware={"nvidia_rtd3": "on"}), profile(gpus=(gpu("nvidia", "turing"),)), "closed kernel modules"),
        (auto(hardware={"nvidia_rtd3": "on", "nvidia_modules": "closed"}), profile(gpus=(gpu("nvidia", "turing"),)), "laptops"),
        (auto(hardware={"nvidia_rtd3": "on", "nvidia_modules": "closed"}), laptop(dgpu("turing", outputs=1)), "screen"),
        (manifest(hardware={"nvidia_rtd3": "on"}), laptop(dgpu("turing")), "not installed"),
    ):
        errors = [i for i in hwplan.validate_plan(m, machine) if i.code == "hw.nvidia_rtd3_unavailable"]
        assert errors and fragment in errors[0].message


def test_prime_offload_is_the_default_and_the_glx_vendor_is_never_global():
    result = hwplan.plan(auto(), laptop(dgpu("ampere_plus")))
    assert result.scalars["prime"] == "offload"
    files = files_of(result)
    assert "__GLX_VENDOR_LIBRARY_NAME=nvidia" in files["/usr/local/bin/prime-run"]
    assert not [path for path, content in files.items() if path.startswith("/etc/environment") or "__GLX_VENDOR_LIBRARY_NAME" in content and path != "/usr/local/bin/prime-run"]


def test_a_single_nvidia_card_with_the_screens_on_it_gets_no_switching_helpers():
    result = hwplan.plan(auto(), profile(form="laptop", gpus=(gpu("nvidia", "ampere_plus", boot=True, outputs=1),)))
    assert result.scalars["prime"] == "off" and "sys-power/switcheroo-control" not in result.optional_atoms


# -- B22: room for the video memory ------------------------------------------------------------------------

def big_card(**extra):
    card = gpu("nvidia", "ampere_plus", name="AD102 [GeForce RTX 4090]", **extra)
    card.vram_mib = 24576
    return card


def test_a_small_root_for_a_24_gib_card_carries_the_warning():
    from tests.hardware.builders import GIB, disk

    small = profile(gpus=(big_card(),), disks=(disk(size=40 * GIB),))
    result = hwplan.plan(auto(storage={"swap_mib": 2048}), small)
    warning = [w for w in result.warnings if w.code == "hw.nvidia_vram_tmp"]
    assert len(warning) == 1 and "24 GiB" in warning[0].message
    roomy = profile(gpus=(big_card(),), disks=(disk(size=512 * GIB),))
    assert not [w for w in hwplan.plan(auto(), roomy).warnings if w.code == "hw.nvidia_vram_tmp"]


def test_a_tmpfs_var_tmp_is_checked_against_half_of_the_memory():
    from gentoo_installer.manifest import Partition

    rows = [Partition("/dev/nvme0n1p1", "/boot/efi", "vfat"), Partition("/dev/nvme0n1p2", "/", "ext4"), Partition("tmpfs", "/var/tmp", "tmpfs")]
    m = auto(storage={"mode": "manual", "partitions": rows})
    result = hwplan.plan(m, profile(gpus=(big_card(),), ram_gib=16))
    assert [w for w in result.warnings if w.code == "hw.nvidia_vram_tmp"]
    assert not [w for w in hwplan.plan(m, profile(gpus=(big_card(),), ram_gib=64)).warnings if w.code == "hw.nvidia_vram_tmp"]


def test_the_video_memory_is_known_by_name_or_by_the_big_window_and_laptop_cards_are_left_alone():
    assert data.nvidia_vram_mib("AD102 [GeForce RTX 4090]") == 24576
    assert data.nvidia_vram_mib("AD103 [GeForce RTX 4080 SUPER]") == 16384
    assert data.nvidia_vram_mib("AD102 [GeForce RTX 4090 Laptop GPU]") == 0
    assert data.vram_from_resources("0x00000000f0000000 0x00000000f0ffffff 0x0000000000040200\n"
                                    "0x0000004000000000 0x00000047ffffffff 0x000000000014220c\n") == 32768
    assert data.vram_from_resources("0x0000004000000000 0x000000400fffffff 0x000000000014220c\n") == 0       # 256 MiB window: no resizable BAR
    assert data.vram_from_resources("junk") == 0


# -- B23: add-ons --------------------------------------------------------------------------------------------

def test_each_addon_adds_exactly_its_keyword_licence_or_service():
    machine = profile(gpus=(gpu("nvidia", "ampere_plus"),))
    vaapi = hwplan.plan(auto(hardware={"nvidia_extras": ["vaapi"]}), machine)
    assert "media-libs/nvidia-vaapi-driver" in vaapi.optional_atoms
    assert vaapi.package_keywords == {"=media-libs/nvidia-vaapi-driver-0.0.18": ["~amd64"]}
    nvtop = hwplan.plan(auto(hardware={"nvidia_extras": ["nvtop"]}), machine)
    assert "sys-process/nvtop" in nvtop.optional_atoms and not nvtop.package_keywords
    persistenced = hwplan.plan(auto(hardware={"nvidia_extras": ["persistenced"]}), machine)
    assert "persistenced" in persistenced.package_use[NV] and ("nvidia-persistenced", "default") in persistenced.optional_services
    cuda = hwplan.plan(auto(hardware={"nvidia_extras": ["cuda"], "nvidia_cuda_license_accepted": True}), machine)
    assert cuda.package_license["dev-util/nvidia-cuda-toolkit"] == ["NVIDIA-CUDA"]
    assert cuda.package_keywords == {"=dev-util/nvidia-cuda-toolkit-12.9.2": ["~amd64"]}
    plain = hwplan.plan(auto(), machine)
    assert not plain.package_keywords and "sys-process/nvtop" not in plain.optional_atoms


def test_cuda_is_impossible_without_the_second_licence_tick_and_addons_need_the_driver():
    machine = profile(gpus=(gpu("nvidia", "ampere_plus"),))
    assert any("CUDA" in error for error in auto(hardware={"nvidia_extras": ["cuda"]}).validate())
    assert "dev-util/nvidia-cuda-toolkit" not in hwplan.plan(auto(hardware={"nvidia_extras": ["cuda"]}), machine).optional_atoms
    no_driver = manifest(hardware={"nvidia_extras": ["nvtop"]})
    assert "hw.nvidia_extras_need_driver" in {i.code for i in hwplan.validate_plan(no_driver, machine) if i.severity == "error"}


# -- B07: Secure Boot ---------------------------------------------------------------------------------------

def test_the_refusal_names_the_unsigned_modules_and_the_taint_in_one_shared_sentence():
    machine = profile(gpus=(gpu("nvidia", "ampere_plus"),))
    m = auto(hardware={"gpu_driver": "nvidia"}, system={"secure_boot": True, "secure_boot_key": "/k", "secure_boot_certificate": "/c"})
    message = next(i.message for i in hwplan.validate_plan(m, machine) if i.code == "hw.nvidia_secure_boot")
    assert message == data.SECURE_BOOT_MODULES_MESSAGE.format(what="The NVIDIA driver")
    assert "no signature" in message and "tainted" in message and "Signed modules are not available yet" in message


def test_signed_modules_are_refused_until_the_secure_boot_test_exists():
    for value in ("auto", "kernel_key"):
        errors = [i for i in hwplan.validate_plan(auto(hardware={"signed_modules": value}), profile()) if i.code == "hw.signed_modules_unavailable"]
        assert errors and "not available yet" in errors[0].message
    assert not [i for i in hwplan.validate_plan(auto(), profile()) if i.code == "hw.signed_modules_unavailable"]
