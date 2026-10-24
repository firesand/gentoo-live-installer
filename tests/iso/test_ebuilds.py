"""The overlay packages: dependencies for every program the daemon runs, metadata, licences, flags."""
from __future__ import annotations

import configparser
import os
from pathlib import Path
import re
import shutil
import subprocess
from xml.etree import ElementTree

import pytest

from .conftest import ROOT

OVERLAY = ROOT / "packaging/gentoo-overlay"
INSTALLER = OVERLAY / "app-admin/gentoo-live-installer"
HWMONUX = OVERLAY / "app-misc/hwmonux"
PORTAGEHUB = OVERLAY / "app-portage/portagehub"
GROUP = OVERLAY / "acct-group/gentoo-installer"
PACKAGES = [INSTALLER, HWMONUX, PORTAGEHUB, GROUP]


def ebuild_of(directory: Path) -> Path:
    found = sorted(directory.glob("*.ebuild"))
    assert len(found) == 1, f"{directory.name} must have exactly one ebuild, found {[f.name for f in found]}"
    return found[0]


def variable(text: str, name: str) -> str:
    match = re.search(rf'^{name}="(.*?)"', text, re.M | re.S)
    return match.group(1) if match else ""


def atoms(depend: str) -> list[str]:
    """Package atoms of a dependency string, without USE conditionals, versions or USE dependencies."""
    result = []
    for token in re.findall(r"[^\s()]+", depend):
        if token.endswith("?") or token in ("||", "!"):
            continue
        token = re.sub(r"\[[^\]]*\]$", "", token)
        token = re.sub(r"^[<>=~!]+", "", token)
        if re.fullmatch(r"[a-z0-9-]+/[A-Za-z0-9_.+-]+", token):
            result.append(token)
    return result


def test_one_ebuild_per_package_and_no_stale_revision() -> None:
    assert ebuild_of(INSTALLER).name == "gentoo-live-installer-0.1.0-r5.ebuild"
    assert ebuild_of(HWMONUX).name == "hwmonux-1.0.0-r1.ebuild"
    assert ebuild_of(PORTAGEHUB).name == "portagehub-0.4.0.ebuild"
    assert ebuild_of(GROUP).name == "gentoo-installer-0.ebuild"


# --- RDEPEND covers every program the daemon runs (I11) -------------------------------------

# The program -> the atom that provides it. Atoms in the ebuild may be conditional on USE flags.
TOOL_PACKAGE = {
    "lsblk": "sys-apps/util-linux", "findmnt": "sys-apps/util-linux", "blkid": "sys-apps/util-linux",
    "partprobe": "sys-block/parted", "parted": "sys-block/parted", "sgdisk": "sys-apps/gptfdisk",
    "udevadm": "virtual/udev", "mkfs.ext4": "sys-fs/e2fsprogs", "mkswap": "sys-apps/util-linux",
    "swapon": "sys-apps/util-linux", "swapoff": "sys-apps/util-linux", "mount": "sys-apps/util-linux",
    "umount": "sys-apps/util-linux", "tar": "app-arch/tar", "gpg": "app-crypt/gnupg", "gpgv": "app-crypt/gnupg",
    "mkfs.xfs": "sys-fs/xfsprogs", "mkfs.btrfs": "sys-fs/btrfs-progs", "mkfs.vfat": "sys-fs/dosfstools",
    "cryptsetup": "sys-fs/cryptsetup", "pvcreate": "sys-fs/lvm2", "vgcreate": "sys-fs/lvm2", "lvcreate": "sys-fs/lvm2",
    "pvs": "sys-fs/lvm2", "vgchange": "sys-fs/lvm2", "mdadm": "sys-fs/mdadm", "sbsign": "app-crypt/sbsigntools",
    "sbverify": "app-crypt/sbsigntools", "efibootmgr": "sys-boot/efibootmgr",
    # hardware detection and the repair tool
    "cpuid2cpuflags": "app-portage/cpuid2cpuflags", "setxkbmap": "x11-apps/setxkbmap", "xrandr": "x11-apps/xrandr",
    "lspci": "sys-apps/pciutils", "smartctl": "sys-apps/smartmontools", "nvme": "sys-apps/nvme-cli", "fuser": "sys-process/psmisc",
    "dosfsck": "sys-fs/dosfstools", "e2fsck": "sys-fs/e2fsprogs", "mkfs.fat": "sys-fs/dosfstools",
    # storage preflight and clean-up: holders, device-mapper maps, Btrfs, wiping
    "wipefs": "sys-apps/util-linux", "blkdiscard": "sys-apps/util-linux", "lscpu": "sys-apps/util-linux", "lsof": "sys-process/lsof",
    "dmsetup": "sys-fs/lvm2", "lvs": "sys-fs/lvm2", "btrfs": "sys-fs/btrfs-progs",
}


def daemon_tools() -> list[str]:
    text = (ROOT / "src/gentoo_installerd/inspect.py").read_text()
    tools: list[str] = []
    for name in ("REQUIRED_TOOLS", "OPTIONAL_TOOLS"):
        match = re.search(rf"^{name}\s*=\s*\(([^)]*)\)", text, re.M)
        assert match, f"{name} not found in inspect.py"
        tools += re.findall(r'"([^"]+)"', match.group(1))
    return tools


def test_every_tool_the_daemon_checks_is_pulled_in_by_the_ebuild() -> None:
    rdepend = set(atoms(variable(ebuild_of(INSTALLER).read_text(), "RDEPEND")))
    tools = daemon_tools()
    unknown = [t for t in tools if t not in TOOL_PACKAGE]
    assert not unknown, f"add {unknown} to TOOL_PACKAGE, and its package to RDEPEND, in the same change"
    missing = {t: TOOL_PACKAGE[t] for t in tools if TOOL_PACKAGE[t] not in rdepend}
    assert not missing, f"RDEPEND of the installer lacks the package of {missing}"


def test_polkit_is_not_a_dependency_of_the_installer() -> None:
    """Nothing in src/ uses polkit; the PortageHub package brings its own."""
    assert "sys-auth/polkit" not in atoms(variable(ebuild_of(INSTALLER).read_text(), "RDEPEND"))
    assert "sys-auth/polkit" in atoms(variable(ebuild_of(PORTAGEHUB).read_text(), "RDEPEND"))


def test_installer_ebuild_declares_optional_features_by_use_flag() -> None:
    text = ebuild_of(INSTALLER).read_text()
    assert variable(text, "IUSE").split() == ["+btrfs", "+efi", "secureboot", "+xfs"]
    rdepend = variable(text, "RDEPEND")
    for flag, atom in (("btrfs", "sys-fs/btrfs-progs"), ("efi", "sys-boot/efibootmgr"), ("secureboot", "app-crypt/sbsigntools"), ("xfs", "sys-fs/xfsprogs")):
        assert re.search(rf"{flag}\?\s*\(\s*{re.escape(atom)}\s*\)", rdepend), flag
    assert "dev-python/pyside[gui,widgets,${PYTHON_USEDEP}]" in rdepend


@pytest.mark.parametrize("directory", [INSTALLER, HWMONUX, PORTAGEHUB])
def test_distfiles_are_not_fetchable(directory: Path) -> None:
    """The distfile is produced by the ISO builder; Portage must not try to download it."""
    text = ebuild_of(directory).read_text()
    assert variable(text, "RESTRICT").split() == ["mirror", "fetch"]
    assert "pkg_nofetch()" in text and 'SRC_URI="${P}.tar.zst"' in text


def test_installer_installs_the_service_files_and_the_rescue_launcher_when_present() -> None:
    text = ebuild_of(INSTALLER).read_text()
    assert 'newinitd "${FILESDIR}/gentoo-installerd.initd" gentoo-installerd' in text
    assert 'newconfd "${FILESDIR}/gentoo-installerd.confd" gentoo-installerd' in text
    assert 'systemd_dounit "${FILESDIR}/gentoo-installerd.service"' in text
    # the launcher follows the source tree, not the names of console scripts that pyproject.toml may define
    assert re.search(r'if \[\[ -d \$\{S\}/src/gentoo_installerd/rescue \]\]; then\s+domenu "\$\{S\}/data/gentoo-rescue.desktop"', text)


def test_rescue_desktop_entry_is_valid() -> None:
    parser = configparser.ConfigParser(delimiters=("=",), interpolation=None)
    parser.optionxform = str
    parser.read_string((ROOT / "data/gentoo-rescue.desktop").read_text())
    entry = parser["Desktop Entry"]
    assert entry["Type"] == "Application" and entry["Exec"] == "gentoo-installer --mode repair" and entry["Terminal"] == "false"
    assert entry["Categories"].endswith(";")


def test_the_source_archive_contains_the_data_directory() -> None:
    """The ebuild reads ${S}/data/gentoo-rescue.desktop, so prepare_source_archives must pack data/."""
    entrypoint = (ROOT / "iso/builder-entrypoint.sh").read_text()
    assert re.search(r"-cf - pyproject\.toml src data\b", entrypoint)


# --- HWMonux and PortageHub ---------------------------------------------------------------------


def test_hwmonux_solves_on_a_stable_system_by_default() -> None:
    text = ebuild_of(HWMONUX).read_text()
    assert variable(text, "KEYWORDS") == "amd64"
    # nvidia is off by default: nvidia-ml-py needs the licence-masked nvidia-drivers.
    assert variable(text, "IUSE").split() == ["nvidia", "liquidctl"]
    rdepend = variable(text, "RDEPEND")
    assert re.search(r"liquidctl\?\s*\(\s*app-misc/liquidctl\[", rdepend), "liquidctl is ~amd64 only: keep it behind its flag"
    assert not re.search(r"^\s*app-misc/liquidctl", rdepend, re.M), "an unconditional liquidctl dependency breaks stable systems"


def test_hwmonux_loads_only_the_safe_sensor_module() -> None:
    conf = (HWMONUX / "files/hwmonux-modules.conf").read_text()
    modules = [l.strip() for l in conf.splitlines() if l.strip() and not l.startswith("#")]
    assert modules == ["drivetemp"], "Super I/O drivers (nct6775, it87) are never loaded blindly: probing can hang a laptop's EC"
    assert "newins \"${FILESDIR}/hwmonux-modules.conf\" hwmonux.conf" in ebuild_of(HWMONUX).read_text()
    assert "insinto /etc/modules-load.d" in ebuild_of(HWMONUX).read_text()


def test_portagehub_licences_and_rust() -> None:
    text = ebuild_of(PORTAGEHUB).read_text()
    assert "|| ( dev-lang/rust-bin dev-lang/rust )" in variable(text, "BDEPEND")
    licences = variable(text, "LICENSE").split()
    assert "GPL-2+" in licences and len(licences) == len(set(licences)) and len(licences) > 5
    assert not [l for l in licences if "-or-later" in l or "-only" in l], "use Gentoo licence names, not SPDX"


@pytest.mark.parametrize("directory", [INSTALLER, HWMONUX, PORTAGEHUB])
def test_licences_exist_and_are_free(directory: Path, pinned_tree: Path) -> None:
    groups: dict[str, list[str]] = {}
    for line in (pinned_tree / "profiles/license_groups").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, _, rest = line.partition(" ")
            groups[name] = rest.split()

    def expand(name: str, seen: set[str]) -> set[str]:
        out: set[str] = set()
        for item in groups.get(name, []):
            if item.startswith("@") and item[1:] not in seen:
                seen.add(item[1:])
                out |= expand(item[1:], seen)
            elif not item.startswith("@"):
                out.add(item)
        return out

    free = expand("FREE", {"FREE"})
    for licence in variable(ebuild_of(directory).read_text(), "LICENSE").split():
        assert (pinned_tree / "licenses" / licence).is_file(), f"{licence} is not a Gentoo licence"
        assert licence in free, f"{licence} is not in @FREE, which is the target's default ACCEPT_LICENSE"


# --- the pinned tree ----------------------------------------------------------------------------

# Dependencies that only exist behind a USE flag that is off by default and are ~amd64 only.
UNSTABLE_BEHIND_FLAG = {"app-misc/liquidctl"}


def stable_amd64(tree: Path, atom: str) -> bool:
    category, package = atom.split("/")
    for ebuild in (tree / category / package).glob("*.ebuild"):
        text = ebuild.read_text(errors="replace")
        keywords = " ".join(re.findall(r'^\s*KEYWORDS="([^"]*)"', text, re.M))
        if re.search(r"(?<![~-])\bamd64\b", keywords):
            return True
        if "KEYWORDS" not in text and "inherit" in text:  # live ebuilds, eclass keywords: not decidable here
            continue
    return False


@pytest.mark.parametrize("directory", [INSTALLER, HWMONUX, PORTAGEHUB, GROUP])
def test_every_dependency_exists_and_is_stable_in_the_pinned_tree(directory: Path, pinned_tree: Path) -> None:
    text = ebuild_of(directory).read_text()
    found = set()
    for name in ("RDEPEND", "DEPEND", "BDEPEND"):
        found |= set(atoms(variable(text, name)))
    for atom in sorted(found):
        category, package = atom.split("/")
        assert (pinned_tree / category / package).is_dir() or (OVERLAY / category / package).is_dir(), f"{atom} does not exist"
        if atom in UNSTABLE_BEHIND_FLAG or (OVERLAY / category / package).is_dir():
            continue
        assert stable_amd64(pinned_tree, atom), f"{atom} has no stable amd64 version in the pinned tree, but {directory.name} is keyworded amd64"


def test_pyside_has_the_flags_the_installer_requires(pinned_tree: Path) -> None:
    text = (pinned_tree / "dev-python/pyside/pyside-6.11.2.ebuild").read_text()
    assert '["+gui"]="Gui"' in text and '["+widgets"]="Widgets"' in text


# --- metadata -----------------------------------------------------------------------------------


@pytest.mark.parametrize("directory", PACKAGES, ids=lambda d: d.name)
def test_metadata_xml_exists_and_documents_every_flag(directory: Path) -> None:
    path = directory / "metadata.xml"
    assert path.is_file(), f"{directory.name} needs a metadata.xml"
    root = ElementTree.parse(path).getroot()
    assert root.tag == "pkgmetadata" and root.findtext("longdescription")
    documented = {flag.get("name") for flag in root.findall("use/flag")}
    iuse = {f.lstrip("+") for f in variable(ebuild_of(directory).read_text(), "IUSE").split()}
    assert documented == iuse, f"metadata.xml flags {documented} differ from IUSE {iuse}"
    for flag in root.findall("use/flag"):
        assert (flag.text or "").strip()
    assert root.find("maintainer") is None or root.find("maintainer").get("type") in ("person", "project")


@pytest.mark.skipif(not os.environ.get("ISO_RUN_PKGCHECK") or shutil.which("pkgcheck") is None,
                    reason="set ISO_RUN_PKGCHECK=1 (and install pkgcheck) to scan a copy of the overlay")
def test_pkgcheck_finds_only_the_known_and_documented_issues(tmp_path: Path) -> None:
    copy = tmp_path / "overlay"
    shutil.copytree(OVERLAY, copy)
    env = {**os.environ, "HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "c"), "XDG_DATA_HOME": str(tmp_path / "d"), "XDG_CONFIG_HOME": str(tmp_path / "f")}
    result = subprocess.run(["pkgcheck", "scan", "--repo", str(copy)], capture_output=True, text=True, env=env, check=False)
    issues = [l.strip() for l in result.stdout.splitlines() if l.startswith("  ")]
    # Manifests are made by the builder (the distfiles are generated); python3_15 needs a newer pyside;
    # liquidctl is ~amd64 and sits behind the liquidctl flag of HWMonux.
    unexpected = [
        l for l in issues
        if not l.startswith(("MissingManifest", "PythonCompatUpdate"))
        and not (l.startswith("NonsolvableDeps") and "liquidctl" in l)
    ]
    assert not unexpected, unexpected
