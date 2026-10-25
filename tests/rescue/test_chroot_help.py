"""The banner of common fixes of a terminal in a repaired system: it fits the system and only reads files."""

from __future__ import annotations

from gentoo_installerd.rescue import chroot_help

from . import fakeroot as fr
from .flow import mount_through_the_manager, uefi_gentoo_lab


def view_of(lab_factory, **options):
    lab, tree = uefi_gentoo_lab(lab_factory, **options)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    session = manager.session(sid)
    return manager.view_for(session), session, tree


def commands(view) -> list[str]:
    return [command for command, _note in chroot_help.fixes(view)]


def test_the_fixes_of_a_uefi_system_name_its_efi_partition_and_kernel(lab_factory) -> None:
    view, _session, _tree = view_of(lab_factory)
    listed = commands(view)
    assert listed[:2] == ["emerge --sync", "emerge --ask --update --deep --newuse @world"]
    assert "emerge --ask --oneshot sys-kernel/gentoo-kernel-bin" in listed
    assert "dracut --force --regenerate-all" in listed
    assert any(item.startswith("grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=") for item in listed)
    assert "grub-mkconfig -o /boot/grub/grub.cfg" in listed
    assert "passwd user" in listed and any(item.endswith("/etc/fstab") for item in listed)
    assert not any("@module-rebuild" in item for item in listed)  # no NVIDIA driver here


def test_a_bios_system_reinstalls_grub_to_the_disk(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory, uefi=False, firmware="bios")
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    listed = commands(manager.view_for(manager.session(sid)))
    assert "grub-install /dev/nvme0n1" in listed and not any("x86_64-efi" in item for item in listed)


def test_an_nvidia_system_gets_the_module_rebuild_hint(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    fr.put(tree, "var/db/pkg/x11-drivers/nvidia-drivers-580.126.18/CATEGORY", "x11-drivers\n")
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert "emerge --ask --oneshot @module-rebuild" in commands(manager.view_for(manager.session(sid)))


def test_the_banner_without_a_view_still_says_where_you_are_and_how_to_leave() -> None:
    text = chroot_help.banner(title="Gentoo Linux", state="READ-ONLY", dns=False)
    assert "a shell in Gentoo Linux, READ-ONLY" in text and "DNS" not in text and chroot_help.LEAVE_UNMOUNTED in text
