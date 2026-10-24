"""Telling Gentoo from every other Linux by a root's own files (no target code is executed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_installerd.rescue.osrelease import NOT_GENTOO, detect_init, family_for, identify, parse_os_release, read_os_info
from gentoo_installerd.rescue.paths import RootFS

from . import fakeroot

FAMILY_TABLE = [
    ("gentoo", "", "gentoo"), ("GENTOO", "", "gentoo"), ("funtoo", "gentoo", "gentoo"), ("calculate", "gentoo", "gentoo"), ("pentoo", "gentoo", "gentoo"),
    ("arch", "", "generic"), ("manjaro", "arch", "generic"), ("cachyos", "arch", "generic"), ("debian", "", "generic"), ("ubuntu", "debian", "generic"),
    ("linuxmint", "ubuntu debian", "generic"), ("fedora", "", "generic"), ("rhel", "fedora", "generic"), ("opensuse-tumbleweed", "opensuse suse", "generic"),
    ("alpine", "", "generic"), ("void", "", "generic"), ("nixos", "", "generic"), ("slackware", "", "generic"), ("somethingnew", "", "generic"), ("", "", "generic"),
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
    [("gentoo", "gentoo", "gentoo"), ("debian", "debian", "generic"), ("ubuntu", "ubuntu", "generic"), ("fedora", "fedora", "generic"), ("arch", "arch", "generic"),
     ("opensuse", "opensuse-tumbleweed", "generic"), ("alpine", "alpine", "generic"), ("void", "void", "generic"), ("nixos", "nixos", "generic")],
)
def test_fake_roots_are_identified(tmp_path: Path, family: str, expected_id: str, expected_family: str) -> None:
    root = fakeroot.make_root(tmp_path / family, family)
    with RootFS(root) as fs:
        facts = identify(fs)
    assert facts is not None
    assert facts.os.id == expected_id and facts.os.family == expected_family
    assert facts.os.pretty_name
    assert facts.arch == "x86_64"


def test_the_reason_a_system_is_not_opened_is_one_plain_sentence() -> None:
    assert NOT_GENTOO == "Not a Gentoo system - this repair tool only supports Gentoo"


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


def test_an_old_gentoo_without_os_release_is_found_by_its_release_file(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    fakeroot.put(root, "etc/gentoo-release", "Gentoo Base System release 2.15\n")
    with RootFS(root) as fs:
        info = read_os_info(fs)
    assert info is not None and info.family == "gentoo" and info.id == "gentoo" and info.pretty_name == "Gentoo Base System release 2.15" and info.source == "/etc/gentoo-release"


@pytest.mark.parametrize("path,content", [("etc/debian_version", "12.5\n"), ("etc/fedora-release", "Fedora release 38\n"), ("etc/arch-release", ""), ("etc/alpine-release", "3.9.0\n")])
def test_the_release_files_of_other_distributions_are_not_told_apart(tmp_path: Path, path: str, content: str) -> None:
    """Nothing but Gentoo is identified by an old release file: such a root is only an unidentified Linux when it has an fstab and programs, else nothing."""
    root = tmp_path / "legacy"
    fakeroot.put(root, path, content)
    with RootFS(root) as fs:
        assert read_os_info(fs) is None
    fakeroot.put(root, "etc/fstab", "")
    (root / "usr/bin").mkdir(parents=True)
    with RootFS(root) as fs:
        info = read_os_info(fs)
    assert info is not None and info.family == "generic" and info.pretty_name == "Unidentified Linux"


def test_roots_without_markers_are_ignored_but_fstab_plus_bin_counts(tmp_path: Path) -> None:
    data = fakeroot.make_root(tmp_path / "data", "data")
    odd = fakeroot.make_root(tmp_path / "odd", "unknown")
    with RootFS(data) as fs:
        assert identify(fs) is None
    with RootFS(odd) as fs:
        facts = identify(fs)
    assert facts is not None and facts.os.family == "generic" and facts.os.id == "linux"


def test_the_architecture_of_the_programs_is_read(tmp_path: Path) -> None:
    arm = fakeroot.make_root(tmp_path / "arm", "gentoo", arch="aarch64")
    with RootFS(arm) as fs:
        facts = identify(fs)
    assert facts is not None and facts.os.family == "gentoo" and facts.arch == "aarch64"
    assert "immutable" not in facts.to_dict()  # image-based systems are other distributions: they are not Gentoo, and not opened


@pytest.mark.parametrize("init,expected", [("openrc", "openrc"), ("systemd", "systemd")])
def test_init_detection(tmp_path: Path, init: str, expected: str) -> None:
    root = fakeroot.make_root(tmp_path / init, "gentoo", init=init)
    with RootFS(root) as fs:
        assert detect_init(fs) == expected


def test_gentoo_profile_is_read_from_the_symlink_text(tmp_path: Path) -> None:
    root = fakeroot.make_root(tmp_path / "g", "gentoo", init="systemd")
    with RootFS(root) as fs:
        facts = identify(fs)
    assert facts is not None
    assert facts.extra["profile"] == "default/linux/amd64/23.0/desktop/plasma/systemd"
    assert facts.init == "systemd"
