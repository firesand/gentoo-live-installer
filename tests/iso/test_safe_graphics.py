"""H1: the live kernel is built from source with a fragment that gives it a generic framebuffer DRM driver."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .conftest import ROOT, load_script

verify = load_script("scripts/verify-iso.py", "verify_iso")
PACKAGE = ROOT / "packaging/gentoo-overlay/app-admin/gentoo-live-installer"
FRAGMENT = PACKAGE / "files/live-safe-graphics.config"
CONTRACT = json.loads((ROOT / "iso/image-contract.json").read_text())
SPEC = verify.parse_spec((ROOT / "iso/specs/livecd-stage2.spec").read_text())


def symbols(text: str) -> dict[str, str]:
    return verify.parse_kernel_config(text)


def test_the_fragment_sets_the_symbols_the_verifier_requires() -> None:
    wanted = symbols(FRAGMENT.read_text())
    for name in ("DRM", "SYSFB_SIMPLEFB", "DRM_SIMPLEDRM", "DRM_EFIDRM", "DRM_VESADRM"):
        assert wanted.get(name) == "y", name
    for family in CONTRACT["kernel_config"]["families"]:
        for name in family["symbols"]:
            assert name in wanted, f"the contract requires {name} but the fragment does not set it"
    # the fragment must not turn the native drivers off: only add the generic one
    assert not [n for n, v in wanted.items() if v == "n"]
    assert all(re.fullmatch(r"CONFIG_[A-Z0-9_]+=y|#.*|", line) for line in FRAGMENT.read_text().splitlines())


def test_the_package_installs_the_fragment_where_the_kernel_eclass_reads_it() -> None:
    ebuild = next(PACKAGE.glob("*.ebuild")).read_text()
    assert "safe-graphics" in re.search(r'^IUSE="([^"]*)"', ebuild, re.M).group(1)
    assert "insinto /etc/kernel/config.d" in ebuild
    # the eclass globs *.config; the name sorts after the distribution's own fragments
    assert 'newins "${FILESDIR}/live-safe-graphics.config" 50-gentoo-live-safe-graphics.config' in ebuild
    use = (ROOT / "iso/portage/package.use/live-installer").read_text()
    assert re.search(r"^app-admin/gentoo-live-installer .*\bsafe-graphics\b", use, re.M)


def test_stage1_installs_the_package_so_the_fragment_exists_before_stage2_builds_the_kernel() -> None:
    stage1 = verify.spec_list(verify.parse_spec((ROOT / "iso/specs/livecd-stage1.spec").read_text()), "livecd/packages")
    assert "app-admin/gentoo-live-installer" in stage1
    # the kernel is built by stage2 (boot/kernel), not stage1
    assert "sys-kernel/gentoo-kernel" not in stage1 and "sys-kernel/gentoo-kernel-bin" not in stage1


def test_stage2_builds_the_distribution_kernel_from_source_without_a_second_copy_of_the_fragment() -> None:
    assert SPEC["boot/kernel/gentoo/distkernel"] == "yes"
    assert SPEC["boot/kernel/gentoo/sources"] == "sys-kernel/gentoo-kernel"
    # Catalyst's _copy_kernel_config does os.makedirs(/etc/kernel/config.d) and so fails when the live package
    # already installed the directory; the package is the one way the fragment gets there.
    assert not [k for k in SPEC if k.startswith("boot/kernel/gentoo/") and k.endswith("/config")]
    assert "boot/kernel/gentoo/config" not in SPEC
    text = (ROOT / "iso/specs/livecd-stage2.spec").read_text()
    assert not re.search(r"^\s*options\s*:.*kerncache", text, re.M)
    catalyst = (ROOT / "iso/builder-entrypoint.sh").read_text()
    assert "kerncache" not in re.search(r"options=.*", catalyst).group(0), "Catalyst's kernel cache never hits for a distribution kernel"


def test_a_binary_kernel_package_cannot_replace_the_build_with_the_fragment() -> None:
    make_conf = (ROOT / "iso/portage/make.conf").read_text()
    options = re.search(r'^EMERGE_DEFAULT_OPTS="([^"]*)"', make_conf, re.M).group(1)
    assert "--usepkg-exclude=sys-kernel/gentoo-kernel" in options.split()
    assert "--usepkg-exclude=sys-kernel/gentoo-kernel-bin" not in options.split()


def test_the_menu_entry_and_the_contract_agree() -> None:
    assert "boot/gentoo-config" in CONTRACT["iso"]["required_files"]
    assert "Safe graphics (for a black screen or no picture)" in CONTRACT["grub"]["required_titles"]
    assert CONTRACT["grub"]["forbidden_titles"] == []
    template = (ROOT / "iso/cd_overlay/boot/grub/grub.cfg.in").read_text()
    assert "No \"safe graphics\" entry" not in template


# --- the verifier -------------------------------------------------------------------------------


def run(config: str, initramfs: list[str] | None = None):
    report = verify.Report()
    verify.check_kernel_config(report, config, CONTRACT, initramfs)
    return report


def test_parse_kernel_config() -> None:
    parsed = symbols('CONFIG_A=y\nCONFIG_B=m\n# CONFIG_C is not set\nCONFIG_D="text"\nCONFIG_E=12\n# a comment\n')
    assert parsed == {"A": "y", "B": "m", "C": "n", "D": "text", "E": "12"}


def test_a_kernel_with_simpledrm_built_in_passes() -> None:
    report = run("CONFIG_SYSFB_SIMPLEFB=y\nCONFIG_DRM_SIMPLEDRM=y\n")
    assert report.ok and "simpledrm" in report.checks[0].detail


def test_a_kernel_with_efidrm_and_vesadrm_passes_too() -> None:
    assert run("# CONFIG_SYSFB_SIMPLEFB is not set\nCONFIG_DRM_EFIDRM=y\nCONFIG_DRM_VESADRM=y\n").ok


def test_the_kernel_of_the_binary_package_is_refused() -> None:
    """The r19 image's kernel: gentoo-kernel-bin 6.18.57."""
    report = run("# CONFIG_SYSFB_SIMPLEFB is not set\n# CONFIG_DRM_SIMPLEDRM is not set\n# CONFIG_DRM_EFIDRM is not set\n# CONFIG_DRM_VESADRM is not set\nCONFIG_FB_EFI=y\n")
    assert not report.ok
    detail = report.errors[0].detail
    assert "Safe graphics" in detail and "SYSFB_SIMPLEFB" in detail and "DRM_EFIDRM" in detail


def test_half_a_family_is_not_enough() -> None:
    assert not run("CONFIG_SYSFB_SIMPLEFB=y\n").ok
    assert not run("CONFIG_DRM_SIMPLEDRM=y\n").ok
    assert not run("CONFIG_DRM_EFIDRM=y\n").ok


def test_simpledrm_is_a_bool_only_with_simplefb() -> None:
    # SYSFB_SIMPLEFB is a bool: a module value would be a broken configuration
    assert not run("CONFIG_SYSFB_SIMPLEFB=m\nCONFIG_DRM_SIMPLEDRM=y\n").ok


def test_a_module_must_be_in_the_initramfs() -> None:
    config = "CONFIG_SYSFB_SIMPLEFB=y\nCONFIG_DRM_SIMPLEDRM=m\n"
    assert not run(config, ["usr/lib/modules/6.18.57/kernel/drivers/gpu/drm/drm.ko.xz"]).ok
    assert run(config, ["usr/lib/modules/6.18.57/kernel/drivers/gpu/drm/sysfb/simpledrm.ko.xz"]).ok
    # an unreadable initramfs is reported by its own check, not twice
    assert run(config, None).ok
