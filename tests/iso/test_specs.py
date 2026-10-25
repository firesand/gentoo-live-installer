"""The Catalyst specs and the Portage configuration of the live image."""
from __future__ import annotations

from pathlib import Path
import re

import pytest

from .conftest import ROOT, load_script

verify = load_script("scripts/verify-iso.py", "verify_iso")

STAGE1 = ROOT / "iso/specs/livecd-stage1.spec"
STAGE2 = ROOT / "iso/specs/livecd-stage2.spec"
RUNLEVELS = {"boot", "default", "sysinit", "nonetwork", "shutdown"}
# Which package supplies each service of livecd/rcadd. Packages that are not listed in
# livecd/packages themselves come with a listed one.
SERVICE_PACKAGE = {
    "udev": "sys-apps/systemd-utils",
    "dbus": "sys-apps/dbus",
    "elogind": "sys-auth/elogind",
    "NetworkManager": "net-misc/networkmanager",
    "chronyd": "net-misc/chrony",
    "gentoo-installerd": "app-admin/gentoo-live-installer",
    "gentoo-rescue-approver": "app-admin/gentoo-live-installer",
    "sshd": "net-misc/openssh",
}
IMPLICIT_PACKAGES = {"sys-apps/systemd-utils", "sys-apps/dbus"}
# Packages the hardware support and the repair tool need on the live medium.
REQUIRED_ATOMS = {
    "sys-apps/memtest86+", "sys-boot/mokutil", "app-crypt/sbsigntools", "sys-process/psmisc",
    "sys-block/thin-provisioning-tools", "app-misc/jq", "sys-apps/pv", "sys-apps/hdparm", "sys-apps/lshw",
    "app-portage/cpuid2cpuflags", "x11-apps/setxkbmap", "x11-apps/xrandr", "sys-apps/pciutils",
    "sys-apps/smartmontools", "sys-apps/nvme-cli", "sys-fs/ddrescue", "sys-fs/ntfs3g", "sys-fs/exfatprogs",
    "sys-fs/f2fs-tools", "sys-fs/jfsutils", "sys-boot/grub", "sys-fs/btrfs-progs", "sys-fs/xfsprogs", "sys-fs/dosfstools", "sys-fs/lvm2",
    "sys-fs/mdadm", "sys-fs/cryptsetup", "sys-boot/efibootmgr", "sys-boot/os-prober", "app-admin/testdisk",
    "sys-block/gparted", "x11-terms/xfce4-terminal", "net-misc/chrony", "www-client/links",
    "xfce-extra/xfce4-xkb-plugin",
}


def spec(path: Path) -> dict:
    return verify.parse_spec(path.read_text(encoding="utf-8"))


def packages() -> list[str]:
    return verify.spec_list(spec(STAGE1), "livecd/packages")


def atom_name(atom: str) -> str:
    return re.sub(r"^[=<>~!]+", "", atom).split(":")[0].split("[")[0]


def test_stage1_packages_have_no_duplicates() -> None:
    names = [re.sub(r"-[0-9][0-9a-z._-]*$", "", atom_name(a)) for a in packages()]
    assert len(names) == len(set(names)), sorted({n for n in names if names.count(n) > 1})


def test_stage1_has_the_rescue_and_hardware_union() -> None:
    listed = {atom_name(a) for a in packages()}
    assert REQUIRED_ATOMS <= listed, sorted(REQUIRED_ATOMS - listed)


def test_stage1_does_not_ship_shim() -> None:
    """H9: the ISO cannot be Secure Boot signed, so shim would be dead weight."""
    assert "sys-boot/shim" not in {atom_name(a) for a in packages()}


def test_stage1_atoms_are_well_formed() -> None:
    for atom in packages():
        assert re.fullmatch(r"=?[a-z0-9][a-z0-9+_.-]*/[A-Za-z0-9][A-Za-z0-9+_.-]*", atom), atom


def test_spec_continuation_lines_have_no_colon() -> None:
    """Catalyst splits ANY line with a colon into key and value."""
    for path in (STAGE1, STAGE2):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            content = re.sub(r"\s*#.*$", "", line)
            if line[:1] in (" ", "\t") and content.strip():
                assert ":" not in content, f"{path.name}:{number}: {line!r}"


def test_spec_parser_agrees_with_catalyst() -> None:
    config = pytest.importorskip("catalyst.config")
    for path in (STAGE1, STAGE2):
        expected = config.SpecParser(str(path)).get_values()
        assert verify.parse_spec(path.read_text(encoding="utf-8")) == expected


def test_stage2_rcadd_is_well_formed_and_complete() -> None:
    rcadd = verify.spec_list(spec(STAGE2), "livecd/rcadd")
    assert len(rcadd) == len(set(rcadd))
    listed = {atom_name(a) for a in packages()} | IMPLICIT_PACKAGES
    for item in rcadd:
        service, _, level = item.partition("|")
        assert service and level in RUNLEVELS, item
        assert SERVICE_PACKAGE[service] in listed, f"{service} is enabled but {SERVICE_PACKAGE[service]} is not installed"
    # chronyd corrects the clock (G5); acpid is neutralised (H4), elogind owns the power keys.
    assert "chronyd|default" in rcadd
    assert not [item for item in rcadd if item.startswith("acpid|")]
    assert "gentoo-installerd|default" in rcadd
    assert "sshd|default" in rcadd  # I9: reachable from another computer on every flavour


def test_stage2_boot_arguments_come_from_the_pins(pins: dict[str, str]) -> None:
    values = spec(STAGE2)
    assert values["livecd/bootargs"] == "@BOOTARGS@"
    assert values["livecd/volid"] == "@VOLID@"
    words = pins["ISO_BOOTARGS"].split()
    # secureconsole has a consumer (livecd-tools' fixinittab); dokeymap would stop every boot.
    assert "secureconsole" in words and "nodhcp" in words
    assert "dokeymap" not in words


def test_stage2_uses_the_project_overlay_and_readme() -> None:
    values = spec(STAGE2)
    assert values["livecd/overlay"] == "@BUILD_DIR@/cd_overlay"
    assert values["livecd/readme"] == "@BUILD_DIR@/README-live.txt"
    assert values["livecd/root_overlay"] == "@BUILD_DIR@/root_overlay"
    assert values["livecd/fsscript"] == "@BUILD_DIR@/fsscript-stage2.sh"
    assert values["livecd/type"] == "gentoo-release-livecd"


def test_stage2_weight_reductions_are_the_safe_ones() -> None:
    values = spec(STAGE2)
    unmerge = verify.spec_list(values, "livecd/unmerge")
    assert "dev-lang/rust-bin" in unmerge and "net-misc/netifrc" in unmerge
    assert "dev-lang/go" in unmerge, "a BDEPEND of restic and rclone only: about 140 MiB of the image"
    removed = verify.spec_list(values, "livecd/rm")
    assert "/var/cache/binhost" in removed
    for path in removed:
        assert path.startswith("/") and ".." not in path and path != "/"
    # Headers, libLLVM, firmware, locales and man pages stay: users compile, run Mesa and read manuals.
    for kept in ("/usr/include", "/usr/lib/llvm", "/usr/lib/firmware", "/usr/share/man", "/usr/share/info"):
        assert not any(path == kept or path.startswith(kept + "/") for path in removed), kept


def test_stage2_rm_removes_only_static_qt_libraries_from_lib64() -> None:
    removed = verify.spec_list(spec(STAGE2), "livecd/rm")
    assert [p for p in removed if p.startswith("/usr/lib64")] == ["/usr/lib64/libQt6*.a"]


def test_portage_use_flags_for_the_union() -> None:
    lines = [l.split() for l in (ROOT / "iso/portage/package.use/live-installer").read_text().splitlines() if l.strip() and not l.startswith("#")]
    use = {parts[0]: parts[1:] for parts in lines}
    assert {"boot", "bios64", "uefi64"} <= set(use["sys-apps/memtest86+"])
    assert "thin" in use["sys-fs/lvm2"]
    assert "liquidctl" in use["app-misc/hwmonux"]
    assert "secureboot" in use["app-admin/gentoo-live-installer"]


def test_every_stage1_atom_and_use_flag_exists_in_the_pinned_tree(pinned_tree: Path) -> None:
    for atom in packages():
        name = re.sub(r"^=", "", atom)
        name = re.sub(r"-[0-9][0-9A-Za-z._-]*$", "", name) if atom.startswith("=") else name
        category, package = name.split("/")
        local = ROOT / "packaging/gentoo-overlay" / category / package
        assert (pinned_tree / category / package).is_dir() or local.is_dir(), f"{name} is in neither the pinned tree nor the overlay"
    for line in (ROOT / "iso/portage/package.use/live-installer").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        atom, *flags = line.split()
        category, package = atom.split("/")
        directory = pinned_tree / category / package
        if not directory.is_dir():
            directory = ROOT / "packaging/gentoo-overlay" / category / package
        assert directory.is_dir(), atom
        iuse: set[str] = set()
        for ebuild in directory.glob("*.ebuild"):
            for match in re.finditer(r'^IUSE="([^"]*)"', ebuild.read_text(errors="replace"), re.M):
                iuse |= {flag.lstrip("+-") for flag in match.group(1).split()}
            # USE_EXPAND flags are listed by their expanded names in IUSE_EFFECTIVE
            iuse |= {flag for flag in re.findall(r"\bgrub_platforms_\w+|\bpython_targets_\w+", ebuild.read_text(errors="replace"))}
        for flag in flags:
            assert flag.lstrip("-") in iuse or flag.lstrip("-").startswith(("grub_platforms_", "python_")), f"{atom}: unknown USE flag {flag}"


def test_livecd_keys_are_valid_for_catalyst_4_2() -> None:
    """A misspelled key is rejected by Catalyst only after stage1 has run for hours."""
    pytest.importorskip("catalyst")
    from catalyst.targets.livecd_stage1 import livecd_stage1
    from catalyst.targets.livecd_stage2 import livecd_stage2

    base = {"subarch", "version_stamp", "target", "rel_type", "profile", "snapshot_treeish", "source_subpath", "compression_mode",
            "portage_confdir", "repos"}
    for path, target in ((STAGE1, livecd_stage1), (STAGE2, livecd_stage2)):
        keys = set(spec(path))
        unknown = {k for k in keys if k not in base and k not in target.valid_values and not k.startswith("boot/kernel")}
        assert not unknown, f"{path.name}: Catalyst does not know {sorted(unknown)}"
        missing = set(target.required_values) - keys - {"boot/kernel"} if target is livecd_stage2 else set()
        assert not missing, missing
    assert "boot/kernel" in spec(STAGE2)


def test_everything_the_contract_forbids_is_removed_by_the_spec() -> None:
    """Drift guard: a forbidden path that no spec key removes makes the last step of a long build fail."""
    import json

    contract = json.loads((ROOT / "iso/image-contract.json").read_text())
    values = spec(STAGE2)
    removed = [p.lstrip("/") for p in verify.spec_list(values, "livecd/rm")]
    emptied = [p.lstrip("/") for p in verify.spec_list(values, "livecd/empty")]
    unmerged_files = {"opt/rust-bin*": "dev-lang/rust-bin"}  # removed by emerge -C, not by a path
    for item in contract["squashfs"]["forbidden"]:
        pattern = item if isinstance(item, str) else item["pattern"]
        accounted = (
            any(pattern in (r, r + "*", r + "/*") for r in removed)
            or any(pattern == e + "/*" for e in emptied)
            or (pattern in unmerged_files and unmerged_files[pattern] in verify.spec_list(values, "livecd/unmerge"))
        )
        assert accounted, f"image-contract.json forbids {pattern}, but nothing in livecd-stage2.spec removes it"
    # the removal list itself must be consistent with the weight-reduction promise of the docs
    assert "etc/portage/make.conf.old" in removed and "etc/portage/make.conf.extra" in removed
