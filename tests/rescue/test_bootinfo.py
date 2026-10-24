"""What /boot looks like: which files are kernels and which initramfs belongs to which, and what must not be mistaken for a kernel."""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_installerd.rescue.bootinfo import collect_kernels
from gentoo_installerd.rescue.paths import RootFS

from . import fakeroot as fr

MiB = 1 << 20


def kernels_of(root: Path) -> dict[str, tuple[str, str, bool]]:
    with RootFS(root) as fs:
        return {k.version: (k.image, k.initramfs, k.modules) for k in collect_kernels(fs)}


def layout(root: Path, files: dict[str, int], modules: tuple[str, ...] = (), dirs: tuple[str, ...] = ()) -> Path:
    for path, size in files.items():
        fr.put(root, path, size=size)
    for release in modules:
        fr.put(root, f"usr/lib/modules/{release}/modules.dep", "x\n")
    for directory in dirs:
        (root / directory).mkdir(parents=True, exist_ok=True)
    return root


def test_directories_that_tools_keep_in_boot_are_not_kernels(tmp_path: Path) -> None:
    """The author's real /boot: kernel-backup and kernel-configs are directories (found by running the scan against it)."""
    root = layout(tmp_path, {"boot/vmlinuz-7.3.0-cachyos-dist": 16 * MiB, "boot/initramfs-7.3.0-cachyos-dist.img": 25 * MiB, "boot/System.map-7.3.0-cachyos-dist": 11 * MiB,
                             "boot/config-7.3.0-cachyos-dist": 300_000, "boot/amd-uc.img": 300_000}, modules=("7.3.0-cachyos-dist",), dirs=("boot/kernel-backup", "boot/kernel-configs"))
    assert kernels_of(root) == {"7.3.0-cachyos-dist": ("/boot/vmlinuz-7.3.0-cachyos-dist", "/boot/initramfs-7.3.0-cachyos-dist.img", True)}


def test_a_symlinked_image_counts_and_a_dangling_one_does_not(tmp_path: Path) -> None:
    root = layout(tmp_path, {"boot/real-kernel": 8 * MiB}, modules=("6.1.0",))
    (root / "boot/vmlinuz-6.1.0").symlink_to("real-kernel")
    (root / "boot/vmlinuz-5.0.0").symlink_to("missing-kernel")
    assert kernels_of(root) == {"6.1.0": ("/boot/vmlinuz-6.1.0", "", True)}


def test_genkernel_names_belong_to_the_release_of_their_module_tree(tmp_path: Path) -> None:
    root = layout(tmp_path, {"boot/kernel-genkernel-x86_64-6.6.30-gentoo": 9 * MiB, "boot/initramfs-genkernel-x86_64-6.6.30-gentoo": 20 * MiB}, modules=("6.6.30-gentoo",))
    assert kernels_of(root) == {"6.6.30-gentoo": ("/boot/kernel-genkernel-x86_64-6.6.30-gentoo", "/boot/initramfs-genkernel-x86_64-6.6.30-gentoo", True)}


@pytest.mark.parametrize("files,modules,expected", [
    # unversioned links, .old copies and checksum files are not kernels of their own
    ({"boot/vmlinuz-6.8.0-31-gentoo": 14 * MiB, "boot/initramfs-6.8.0-31-gentoo.img": 60 * MiB, "boot/vmlinuz.old": 14 * MiB, "boot/initramfs.img.old": 60 * MiB, "boot/vmlinuz": 14 * MiB,
      "boot/vmlinuz-6.8.0-31-gentoo.hmac": 100},
     ("6.8.0-31-gentoo",), {"6.8.0-31-gentoo": ("/boot/vmlinuz-6.8.0-31-gentoo", "/boot/initramfs-6.8.0-31-gentoo.img", True)}),
    # an initramfs named initrd-<release>
    ({"boot/vmlinuz-6.8.5-gentoo": 13 * MiB, "boot/initrd-6.8.5-gentoo": 40 * MiB, "boot/vmlinuz": 13 * MiB}, ("6.8.5-gentoo",),
     {"6.8.5-gentoo": ("/boot/vmlinuz-6.8.5-gentoo", "/boot/initrd-6.8.5-gentoo", True)}),
    # a module tree whose kernel was removed is listed without an image
    ({"boot/vmlinuz-6.8.0-31-gentoo": 14 * MiB}, ("6.8.0-31-gentoo", "6.5.0-old"), {"6.8.0-31-gentoo": ("/boot/vmlinuz-6.8.0-31-gentoo", "", True), "6.5.0-old": ("", "", True)}),
])
def test_boot_layouts(tmp_path: Path, files, modules, expected) -> None:
    assert kernels_of(layout(tmp_path, files, modules)) == expected


@pytest.mark.parametrize("versions,expected", [
    (["6.9.12-gentoo", "6.10.3-gentoo", "6.2.1-gentoo"], ["6.2.1-gentoo", "6.9.12-gentoo", "6.10.3-gentoo"]),  # the numbers cross a digit width
    (["7.3.0-cachyos-dist", "7.2.11-cachyos-dist", "7.10.0-cachyos-dist"], ["7.2.11-cachyos-dist", "7.3.0-cachyos-dist", "7.10.0-cachyos-dist"]),
    (["6.18.57-gentoo-dist", "6.18.57-gentoo-dist-bin", "6.18.9-gentoo-dist"], ["6.18.9-gentoo-dist", "6.18.57-gentoo-dist", "6.18.57-gentoo-dist-bin"]),
])
def test_kernels_are_listed_oldest_first_by_number_not_by_text(tmp_path: Path, versions: list[str], expected: list[str]) -> None:
    root = layout(tmp_path, {f"boot/vmlinuz-{version}": 14 * MiB for version in versions})
    with RootFS(root) as fs:
        assert [kernel.version for kernel in collect_kernels(fs)] == expected
