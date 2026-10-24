"""The synthesized /proc/cmdline and Gentoo's installkernel chroot guard (review R3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_installerd.rescue import cmdline
from gentoo_installerd.rescue.paths import RootFS

from . import fakeroot as fr

LIVE = "BOOT_IMAGE=/boot/gentoo dokeymap nodhcp secureconsole root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.dir=/ rd.live.squashimg=image.squashfs cdroot quiet"


def test_sanitize_removes_every_live_argument() -> None:
    cleaned = cmdline.sanitize(LIVE + " rd.live.ram=1 nox")
    assert cleaned == "quiet"
    assert "root=live" not in cleaned and "rd.live" not in cleaned
    assert cmdline.contains_live_arguments("root=live:LABEL=x") and cmdline.contains_live_arguments("cdroot") and not cmdline.contains_live_arguments("root=UUID=x ro quiet")


@pytest.mark.parametrize("arguments", ["nox", "nogui", "rd.debug", "dokeymap", "nodhcp", "dospeakup", "secureconsole", "overlayfs", "quiet nox rd.debug dokeymap"])
def test_ordinary_arguments_of_an_installed_system_are_not_live_arguments(arguments: str) -> None:
    """nox and nogui are honoured by display-manager-init on any system, rd.debug is a dracut switch, dokeymap asks for a keymap before a LUKS passphrase."""
    assert not cmdline.contains_live_arguments(f"root=UUID=x ro {arguments}")


@pytest.mark.parametrize("argument", ["root=live:CDLABEL=GENTOO", "root=live:LABEL=x", "rd.live.image", "rd.live.dir=/", "rd.live.ram=1", "cdroot", "CDLABEL=GENTOO"])
def test_arguments_that_only_a_live_medium_has_are_found(argument: str) -> None:
    assert cmdline.contains_live_arguments(f"root=UUID=x ro {argument}")
    assert cmdline.contains_live_arguments(f"BOOT_IMAGE=/boot/gentoo nox {argument}")


def test_target_kernel_cmdline_file_wins(tmp_path: Path) -> None:
    root = fr.make_root(tmp_path / "g", "gentoo")
    with RootFS(root) as fs:
        derived = cmdline.derive(fs, fr.ROOT_UUID)
    assert derived.text == f"root=UUID={fr.ROOT_UUID} ro quiet" and derived.source == "/etc/kernel/cmdline" and derived.added == []


def test_grub_defaults_then_bls_then_grub_cfg(tmp_path: Path) -> None:
    """Without /etc/kernel/cmdline the command line comes from /etc/default/grub, else from a boot entry, else from the menu file."""
    defaults = fr.make_root(tmp_path / "d", "gentoo", kernel_cmdline=False, firmware="uefi")
    fr.put(defaults, "etc/default/grub", 'GRUB_DEFAULT=0\nGRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\nGRUB_CMDLINE_LINUX=""\n')
    with RootFS(defaults) as fs:
        derived = cmdline.derive(fs, fr.ROOT_UUID)
    assert derived.source == "/etc/default/grub" and derived.text.startswith("quiet splash") and f"root=UUID={fr.ROOT_UUID}" in derived.text and derived.text.endswith(" ro")
    entries = fr.make_root(tmp_path / "e", "gentoo", kernel_cmdline=False)
    fr.put(entries, "boot/loader/entries/gentoo.conf", f"title Gentoo\nlinux /vmlinuz-{fr.GENTOO_KVER}\noptions root=UUID={fr.ROOT_UUID} ro rootflags=subvol=root\n")
    with RootFS(entries) as fs:
        derived = cmdline.derive(fs, fr.ROOT_UUID)
    assert derived.source == "/boot/loader/entries" and "rootflags=subvol=root" in derived.text
    menu = fr.make_root(tmp_path / "m", "gentoo", kernel_cmdline=False)
    with RootFS(menu) as fs:
        derived = cmdline.derive(fs, fr.ROOT_UUID)
    assert derived.source == "grub.cfg" and derived.text == f"root=UUID={fr.ROOT_UUID} ro"


def test_a_live_leak_in_the_targets_own_files_is_scrubbed(tmp_path: Path) -> None:
    root = fr.make_root(tmp_path / "g", "gentoo")
    fr.put(root, "etc/kernel/cmdline", LIVE + "\n")
    with RootFS(root) as fs:
        derived = cmdline.derive(fs, fr.ROOT_UUID)
    assert not cmdline.contains_live_arguments(derived.text) and "root=live" not in derived.text and "rd.live" not in derived.text
    assert derived.text == f"quiet root=UUID={fr.ROOT_UUID} ro"


def test_stack_arguments_are_added_only_when_missing(tmp_path: Path) -> None:
    root = fr.make_root(tmp_path / "g", "gentoo", kernel_cmdline=False)
    with RootFS(root) as fs:
        derived = cmdline.derive(fs, fr.ROOT_UUID, subvol_path="/@", luks_uuid=fr.LUKS_UUID, lvm_vg="vg0", md_uuid="abc")
    tokens = derived.text.split()
    assert f"root=UUID={fr.ROOT_UUID}" in tokens and "rootflags=subvol=@" in tokens and f"rd.luks.uuid={fr.LUKS_UUID}" in tokens and "rd.lvm.vg=vg0" in tokens and "rd.md.uuid=abc" in tokens
    fr.put(root, "etc/kernel/cmdline", f"root=UUID={fr.ROOT_UUID} rd.luks.uuid={fr.LUKS_UUID} rw\n")
    with RootFS(root) as fs:
        again = cmdline.derive(fs, fr.ROOT_UUID, luks_uuid=fr.LUKS_UUID)
    assert again.text.split().count("rw") == 1 and "ro" not in again.text.split() and again.added == []


def test_a_system_without_any_configuration_still_gets_a_root_argument(tmp_path: Path) -> None:
    root = fr.make_root(tmp_path / "u", "unknown")
    with RootFS(root) as fs:
        derived = cmdline.derive(fs, fr.ROOT_UUID)
    assert derived.text == f"root=UUID={fr.ROOT_UUID} ro" and derived.source == "the root file system"


@pytest.mark.parametrize("use,files,blocks", [
    ("dracut grub", {}, True),                                # dracut would use /proc/cmdline
    ("dracut grub", {"etc/cmdline": "root=UUID=x\n"}, False),
    ("dracut grub", {"etc/cmdline.d/root.conf": "root=UUID=x\n"}, False),
    ("dracut grub", {"etc/dracut.conf": "kernel_cmdline=\"root=UUID=x\"\n"}, False),
    ("dracut grub", {"etc/dracut.conf.d/90-host.conf": "hostonly=\"no\"\n"}, False),
    ("dracut grub", {"etc/dracut.conf": "hostonly=\"no\"\nhostonly_cmdline=\"yes\"\n"}, True),
    ("grub", {}, False),                                      # no initramfs generator, no BLS layout: nothing to guard
    ("dracut systemd systemd-boot", {"etc/cmdline": "x\n"}, True),   # BLS layout needs /etc/kernel/cmdline too
    ("dracut systemd systemd-boot", {"etc/cmdline": "x\n", "etc/kernel/cmdline": "root=UUID=x\n"}, False),
    ("dracut", {"etc/kernel/preinst.d/05-check-chroot.install": ""}, False),   # already overridden
    # the vendor settings of installkernel count, as they do for kernel-install itself; /etc overrides them
    ("dracut", {"usr/lib/kernel/install.conf": "layout=bls\n", "etc/cmdline": "x\n"}, True),
    ("dracut", {"usr/lib/kernel/install.conf": "layout=bls\n", "etc/cmdline": "x\n", "etc/kernel/install.conf": "layout=grub\n"}, False),
    ("dracut", {"usr/lib/kernel/install.conf": "# managed\nlayout=uki\n", "etc/cmdline": "x\n", "usr/lib/kernel/cmdline": "root=UUID=x\n"}, False),
    # the real configuration of a Gentoo system with the dist kernel: hostonly_cmdline=no comes from dracut's own vendor file
    ("dracut grub", {"usr/lib/dracut/dracut.conf.d/01-gentoo.conf": 'hostonly="yes"\nhostonly_cmdline="no"\n', "usr/lib/kernel/install.conf": "layout=grub\ninitrd_generator=dracut\n"}, False),
])
def test_installkernel_guard(tmp_path: Path, use: str, files: dict[str, str], blocks: bool) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for path, text in files.items():
        fr.put(root, path, text)
    with RootFS(root) as fs:
        info = cmdline.installkernel_guard(fs, use)
    assert info.blocks is blocks
    assert info.override_path == ("/etc/kernel/install.d/05-check-chroot.install" if "systemd" in use.split() else "/etc/kernel/preinst.d/05-check-chroot.install")
    if blocks:
        assert info.reason
