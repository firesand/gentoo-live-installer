"""Distribution detection from a root's own files (no target code is executed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_installerd.rescue.osrelease import detect_immutable, detect_init, family_for, identify, parse_os_release, read_os_info
from gentoo_installerd.rescue.paths import RootFS

from . import fakeroot

FAMILY_TABLE = [
    ("gentoo", "", "gentoo"), ("funtoo", "gentoo", "gentoo"), ("calculate", "gentoo", "gentoo"),
    ("arch", "", "arch"), ("manjaro", "arch", "arch"), ("endeavouros", "arch", "arch"), ("cachyos", "arch", "arch"), ("artix", "arch", "arch"),
    ("garuda", "arch", "arch"), ("debian", "", "debian"), ("ubuntu", "debian", "debian"), ("linuxmint", "ubuntu debian", "debian"),
    ("pop", "ubuntu debian", "debian"), ("kali", "debian", "debian"), ("raspbian", "debian", "debian"), ("devuan", "debian", "debian"),
    ("zorin", "ubuntu debian", "debian"), ("fedora", "", "fedora"), ("rhel", "fedora", "fedora"), ("centos", "rhel fedora", "fedora"),
    ("almalinux", "rhel centos fedora", "fedora"), ("rocky", "rhel centos fedora", "fedora"), ("ol", "fedora", "fedora"), ("amzn", "fedora", "fedora"),
    ("nobara", "rhel centos fedora", "fedora"), ("opensuse-leap", "suse opensuse", "suse"), ("opensuse-tumbleweed", "opensuse suse", "suse"),
    ("opensuse-microos", "suse opensuse opensuse-tumbleweed", "suse"), ("sles", "suse", "suse"), ("sled", "suse", "suse"), ("alpine", "", "generic"),
    ("void", "", "generic"), ("nixos", "", "generic"), ("slackware", "", "generic"), ("guix", "", "generic"), ("solus", "", "generic"),
    ("clear-linux-os", "", "generic"), ("photon", "", "generic"), ("somethingnew", "", "generic"), ("somethingnew", "debian", "debian"),
    ("", "", "generic"), ("UBUNTU", "DEBIAN", "debian"),
]


@pytest.mark.parametrize("os_id,like,family", FAMILY_TABLE)
def test_family_mapping(os_id: str, like: str, family: str) -> None:
    assert family_for(os_id, tuple(like.split())) == family


def test_os_release_parser_handles_quoting_comments_and_junk() -> None:
    fields = parse_os_release('# comment\nNAME="Debian GNU/Linux"\nID=debian\nVERSION=\'13 (trixie)\'\nPRETTY_NAME="He said \\"hi\\""\n\nbadline\n1BAD=x\nEMPTY=\nID_LIKE=  "ubuntu debian"  \n')
    assert fields["NAME"] == "Debian GNU/Linux"
    assert fields["ID"] == "debian"
    assert fields["VERSION"] == "13 (trixie)"
    assert fields["EMPTY"] == ""
    assert fields["ID_LIKE"] == "ubuntu debian"
    assert "1BAD" not in fields and "badline" not in fields


@pytest.mark.parametrize(
    "family,expected_id,expected_family",
    [("gentoo", "gentoo", "gentoo"), ("debian", "debian", "debian"), ("ubuntu", "ubuntu", "debian"), ("pop", "pop", "debian"), ("fedora", "fedora", "fedora"),
     ("rhel", "rhel", "fedora"), ("arch", "arch", "arch"), ("opensuse", "opensuse-tumbleweed", "suse"), ("alpine", "alpine", "generic"),
     ("void", "void", "generic"), ("nixos", "nixos", "generic")],
)
def test_fake_roots_are_identified(tmp_path: Path, family: str, expected_id: str, expected_family: str) -> None:
    root = fakeroot.make_root(tmp_path / family, family)
    with RootFS(root) as fs:
        facts = identify(fs)
    assert facts is not None
    assert facts.os.id == expected_id and facts.os.family == expected_family
    assert facts.os.pretty_name
    assert facts.arch == "x86_64"


def test_etc_os_release_symlink_into_usr_lib_is_followed_in_root(tmp_path: Path) -> None:
    root = fakeroot.make_root(tmp_path / "g", "gentoo")
    assert (root / "etc/os-release").is_symlink()
    with RootFS(root) as fs:
        info = read_os_info(fs)
    assert info is not None and info.source == "/etc/os-release" and info.id == "gentoo"


def test_hostile_os_release_symlink_does_not_read_the_live_system(tmp_path: Path) -> None:
    root = tmp_path / "hostile"
    (root / "etc").mkdir(parents=True)
    (root / "etc/os-release").symlink_to("//etc/os-release")  # the live /etc/os-release exists on any Linux host
    (root / "usr/bin").mkdir(parents=True)
    with RootFS(root) as fs:
        assert read_os_info(fs) is None


@pytest.mark.parametrize(
    "path,content,family,os_id",
    [("etc/gentoo-release", "Gentoo Base System release 2.15\n", "gentoo", "gentoo"), ("etc/arch-release", "", "arch", "arch"),
     ("etc/debian_version", "12.5\n", "debian", "debian"), ("etc/fedora-release", "Fedora release 38\n", "fedora", "fedora"),
     ("etc/redhat-release", "CentOS release 6.10\n", "fedora", "fedora"), ("etc/SuSE-release", "openSUSE 13.2\n", "suse", "suse"),
     ("etc/alpine-release", "3.9.0\n", "generic", "alpine"), ("etc/slackware-version", "Slackware 15.0\n", "generic", "slackware")],
)
def test_legacy_release_files(tmp_path: Path, path: str, content: str, family: str, os_id: str) -> None:
    root = tmp_path / "legacy"
    fakeroot.put(root, path, content)
    with RootFS(root) as fs:
        info = read_os_info(fs)
    assert info is not None and info.family == family and info.id == os_id and info.source == "/" + path


def test_legacy_ubuntu_is_told_apart_from_debian(tmp_path: Path) -> None:
    root = tmp_path / "old-ubuntu"
    fakeroot.put(root, "etc/debian_version", "buster/sid\n")
    fakeroot.put(root, "etc/lsb-release", "DISTRIB_ID=Ubuntu\nDISTRIB_RELEASE=16.04\n")
    with RootFS(root) as fs:
        info = read_os_info(fs)
    assert info is not None and info.id == "ubuntu" and info.family == "debian"


def test_roots_without_markers_are_ignored_but_fstab_plus_bin_counts(tmp_path: Path) -> None:
    data = fakeroot.make_root(tmp_path / "data", "data")
    odd = fakeroot.make_root(tmp_path / "odd", "unknown")
    with RootFS(data) as fs:
        assert identify(fs) is None
    with RootFS(odd) as fs:
        facts = identify(fs)
    assert facts is not None and facts.os.family == "generic" and facts.os.id == "linux"


def test_architecture_and_immutable_detection(tmp_path: Path) -> None:
    arm = fakeroot.make_root(tmp_path / "arm", "aarch64")
    ostree = fakeroot.make_root(tmp_path / "ostree", "ostree")
    micro = fakeroot.make_root(tmp_path / "micro", "microos")
    nix = fakeroot.make_root(tmp_path / "nix", "nixos")
    plain = fakeroot.make_root(tmp_path / "plain", "fedora")
    with RootFS(arm) as fs:
        assert identify(fs).arch == "aarch64"  # type: ignore[union-attr]
    with RootFS(ostree) as fs:
        assert identify(fs).immutable == "ostree"  # type: ignore[union-attr]
    with RootFS(micro) as fs:
        assert identify(fs).immutable == "transactional"  # type: ignore[union-attr]
    with RootFS(nix) as fs:
        assert identify(fs).immutable == "nix"  # type: ignore[union-attr]
    with RootFS(plain) as fs:
        assert identify(fs).immutable == ""  # type: ignore[union-attr]


@pytest.mark.parametrize("family,init,expected", [("gentoo", "openrc", "openrc"), ("gentoo", "systemd", "systemd"), ("debian", "systemd", "systemd"), ("fedora", "systemd", "systemd"),
                                                  ("arch", "systemd", "systemd"), ("alpine", "openrc", "openrc")])
def test_init_detection(tmp_path: Path, family: str, init: str, expected: str) -> None:
    opts = {"init": init} if family == "gentoo" else {}
    root = fakeroot.make_root(tmp_path / family, family, **opts)
    with RootFS(root) as fs:
        assert detect_init(fs) == expected


def test_gentoo_profile_is_read_from_the_symlink_text(tmp_path: Path) -> None:
    root = fakeroot.make_root(tmp_path / "g", "gentoo", init="systemd")
    with RootFS(root) as fs:
        facts = identify(fs)
    assert facts is not None
    assert facts.extra["profile"] == "default/linux/amd64/23.0/desktop/plasma/systemd"
    assert facts.init == "systemd"
