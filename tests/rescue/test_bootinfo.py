"""What /boot looks like on different distributions: which files are kernels and which initramfs belongs to which, and what must not be mistaken for a kernel."""

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
    # Debian and Ubuntu: unversioned links and .old copies are not kernels of their own
    ({"boot/vmlinuz-6.8.0-31-generic": 14 * MiB, "boot/initrd.img-6.8.0-31-generic": 60 * MiB, "boot/vmlinuz.old": 14 * MiB, "boot/initrd.img.old": 60 * MiB, "boot/vmlinuz": 14 * MiB,
      "boot/vmlinuz-6.8.0-31-generic.hmac": 100},
     ("6.8.0-31-generic",), {"6.8.0-31-generic": ("/boot/vmlinuz-6.8.0-31-generic", "/boot/initrd.img-6.8.0-31-generic", True)}),
    # Fedora and RHEL: a rescue kernel next to the installed ones
    ({"boot/vmlinuz-6.8.5-300.fc40.x86_64": 14 * MiB, "boot/initramfs-6.8.5-300.fc40.x86_64.img": 40 * MiB, "boot/.vmlinuz-6.8.5-300.fc40.x86_64.hmac": 100,
      "boot/vmlinuz-0-rescue-0123456789abcdef0123456789abcdef": 14 * MiB, "boot/initramfs-0-rescue-0123456789abcdef0123456789abcdef.img": 90 * MiB},
     ("6.8.5-300.fc40.x86_64",), {"6.8.5-300.fc40.x86_64": ("/boot/vmlinuz-6.8.5-300.fc40.x86_64", "/boot/initramfs-6.8.5-300.fc40.x86_64.img", True),
                                  "0-rescue-0123456789abcdef0123456789abcdef": ("/boot/vmlinuz-0-rescue-0123456789abcdef0123456789abcdef", "/boot/initramfs-0-rescue-0123456789abcdef0123456789abcdef.img", False)}),
    # openSUSE: initrd-<release>
    ({"boot/vmlinuz-6.8.5-1-default": 13 * MiB, "boot/initrd-6.8.5-1-default": 40 * MiB, "boot/vmlinuz": 13 * MiB}, ("6.8.5-1-default",),
     {"6.8.5-1-default": ("/boot/vmlinuz-6.8.5-1-default", "/boot/initrd-6.8.5-1-default", True)}),
    # Alpine: the flavor is the version
    ({"boot/vmlinuz-lts": 9 * MiB, "boot/initramfs-lts": 10 * MiB, "boot/System.map-lts": MiB}, (), {"lts": ("/boot/vmlinuz-lts", "/boot/initramfs-lts", False)}),
    # Void
    ({"boot/vmlinuz-6.6.30_1": 11 * MiB, "boot/initramfs-6.6.30_1.img": 12 * MiB}, ("6.6.30_1",), {"6.6.30_1": ("/boot/vmlinuz-6.6.30_1", "/boot/initramfs-6.6.30_1.img", True)}),
    # a module tree whose kernel was removed is listed without an image
    ({"boot/vmlinuz-6.8.0-31-generic": 14 * MiB}, ("6.8.0-31-generic", "6.5.0-old"), {"6.8.0-31-generic": ("/boot/vmlinuz-6.8.0-31-generic", "", True), "6.5.0-old": ("", "", True)}),
])
def test_boot_layouts_of_the_big_distributions(tmp_path: Path, files, modules, expected) -> None:
    assert kernels_of(layout(tmp_path, files, modules)) == expected


def test_arch_images_are_named_after_the_package_base_not_the_release(tmp_path: Path) -> None:
    root = layout(tmp_path, {"boot/vmlinuz-linux": 12 * MiB, "boot/initramfs-linux.img": 20 * MiB, "boot/initramfs-linux-fallback.img": 80 * MiB, "boot/vmlinuz-linux-lts": 12 * MiB,
                             "boot/initramfs-linux-lts.img": 20 * MiB}, modules=("6.8.5-arch1-1", "6.6.30-1-lts"))
    fr.put(root, "usr/lib/modules/6.8.5-arch1-1/pkgbase", "linux\n")
    fr.put(root, "usr/lib/modules/6.6.30-1-lts/pkgbase", "linux-lts\n")
    assert kernels_of(root) == {"6.8.5-arch1-1": ("/boot/vmlinuz-linux", "/boot/initramfs-linux.img", True), "6.6.30-1-lts": ("/boot/vmlinuz-linux-lts", "/boot/initramfs-linux-lts.img", True)}
