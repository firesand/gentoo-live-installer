"""Fake target roots: a Gentoo, which the repair tool opens, and other Linux systems, which it lists and refuses.

``make_root(base, family, **options)`` writes a small but realistic tree: real text for the
configuration the engine reads, empty executable files for tools, sparse files for kernels and
initramfs images. Nothing here touches a device; every test runs on a temporary directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT_UUID = "998f7697-b591-4cb9-a8e7-d1c52c798ac7"
BOOT_UUID = "4c1d9c1e-2b0e-4d69-9f5b-6a1c3f5e7a10"
ESP_UUID = "ABCD-1234"
SWAP_UUID = "0d1f2a3b-4c5d-4e6f-8a7b-9c0d1e2f3a4b"
HOME_UUID = "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9"
DEAD_UUID = "deadbeef-dead-beef-dead-beefdeadbeef"
LUKS_UUID = "1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b"

GENTOO_KVER = "6.18.57-gentoo-dist-bin"

OS_RELEASE = {
    "gentoo": 'NAME=Gentoo\nID=gentoo\nPRETTY_NAME="Gentoo Linux"\nANSI_COLOR="1;32"\nHOME_URL="https://www.gentoo.org/"\n',
    "debian": 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nNAME="Debian GNU/Linux"\nVERSION_ID="13"\nVERSION="13 (trixie)"\nVERSION_CODENAME=trixie\nID=debian\n',
    "ubuntu": 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nNAME="Ubuntu"\nVERSION_ID="24.04"\nVERSION="24.04.1 LTS (Noble Numbat)"\nID=ubuntu\nID_LIKE=debian\n',
    "fedora": 'NAME="Fedora Linux"\nVERSION="41 (Cloud Edition)"\nID=fedora\nVERSION_ID=41\nVARIANT="Cloud Edition"\nVARIANT_ID=cloud\nPRETTY_NAME="Fedora Linux 41 (Cloud Edition)"\n',
    "arch": 'NAME="Arch Linux"\nPRETTY_NAME="Arch Linux"\nID=arch\nBUILD_ID=rolling\nANSI_COLOR="38;2;23;147;209"\n',
    "opensuse": 'NAME="openSUSE Tumbleweed"\n# VERSION="20241020"\nID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\nVERSION_ID="20241020"\nPRETTY_NAME="openSUSE Tumbleweed"\n',
    "alpine": 'NAME="Alpine Linux"\nID=alpine\nVERSION_ID=3.20.3\nPRETTY_NAME="Alpine Linux v3.20"\n',
    "void": 'NAME="Void"\nID="void"\nPRETTY_NAME="Void Linux"\n',
    "nixos": 'NAME=NixOS\nID=nixos\nVERSION_ID="24.11"\nPRETTY_NAME="NixOS 24.11 (Vicuna)"\n',
}


def elf(machine: int = 0x3E, bits: int = 2) -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = bits
    header[5] = 1
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header)


def put(root: Path, path: str, text: str = "", *, mode: int | None = None, size: int | None = None) -> Path:
    target = root / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    if size is not None:
        with open(target, "wb") as handle:
            handle.truncate(size)
    else:
        target.write_text(text)
    if mode is not None:
        target.chmod(mode)
    return target


def tool(root: Path, path: str, content: bytes = b"#!/bin/sh\n") -> Path:
    target = root / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    target.chmod(0o755)
    return target


def link(root: Path, path: str, target: str) -> None:
    where = root / path.lstrip("/")
    where.parent.mkdir(parents=True, exist_ok=True)
    if where.is_symlink() or where.exists():
        where.unlink()
    where.symlink_to(target)


def merged_usr(root: Path, *, sbin_link: str = "usr/bin") -> None:
    for name in ("usr/bin", "usr/lib", "usr/lib64", "usr/share", "etc", "var", "boot"):
        (root / name).mkdir(parents=True, exist_ok=True)
    link(root, "bin", "usr/bin")
    link(root, "sbin", sbin_link)
    link(root, "lib", "usr/lib")
    link(root, "lib64", "usr/lib64")
    if sbin_link == "usr/bin":
        link(root, "usr/sbin", "bin")
    else:
        (root / "usr/sbin").mkdir(parents=True, exist_ok=True)


def base_system(root: Path, *, merged: bool = True, init: str = "systemd") -> None:
    if merged:
        merged_usr(root)
    else:
        for name in ("bin", "sbin", "usr/bin", "usr/sbin", "lib", "etc", "var", "boot"):
            (root / name).mkdir(parents=True, exist_ok=True)
    for name in ("proc", "sys", "dev", "run", "tmp", "home", "root", "mnt", "srv", "opt"):
        (root / name).mkdir(parents=True, exist_ok=True)
    tool(root, "usr/bin/env", elf())
    tool(root, "usr/bin/sh", elf())
    tool(root, "usr/bin/bash", elf())
    tool(root, "usr/bin/true", elf())
    if init == "systemd":
        tool(root, "usr/lib/systemd/systemd", elf())
        link(root, "usr/bin/init" if merged else "usr/sbin/init", "../lib/systemd/systemd")
    elif init == "openrc":
        tool(root, "usr/bin/openrc", elf())
        tool(root, "usr/bin/openrc-init", elf())
        link(root, "usr/bin/init" if merged else "usr/sbin/init", "openrc-init")
        (root / "etc/init.d").mkdir(parents=True, exist_ok=True)


def write_os_release(root: Path, key: str, **overrides: str) -> None:
    text = OS_RELEASE[key]
    for name, value in overrides.items():
        lines = [line for line in text.splitlines() if not line.startswith(name + "=")]
        text = "\n".join(lines) + f"\n{name}={value}\n"
    put(root, "usr/lib/os-release", text)
    link(root, "etc/os-release", "../usr/lib/os-release")


def fstab_text(*, root: str = ROOT_UUID, esp: str | None = None, esp_mount: str = "/efi", boot: str | None = None, swap: str | None = SWAP_UUID, home: str | None = None,
               root_fs: str = "ext4", extra: str = "") -> str:
    lines = ["# /etc/fstab: static file system information.", f"UUID={root}\t/\t{root_fs}\tdefaults,noatime\t0 1"]
    if boot:
        lines.append(f"UUID={boot}\t/boot\text4\tdefaults,noatime\t0 2")
    if esp:
        lines.append(f"UUID={esp}\t{esp_mount}\tvfat\tumask=0077\t0 2")
    if swap:
        lines.append(f"UUID={swap}\tnone\tswap\tsw\t0 0")
    if home:
        lines.append(f"UUID={home}\t/home\text4\tdefaults,noatime\t0 2")
    if extra:
        lines.append(extra.rstrip("\n"))
    return "\n".join(lines) + "\n"


def grub_cfg(kver: str, *, root_uuid: str = ROOT_UUID, kernel: str = "vmlinuz", initrd: str = "initramfs") -> str:
    return (
        "#\n# DO NOT EDIT THIS FILE\n#\nset default=0\nset timeout=5\n"
        f"menuentry 'Linux {kver}' {{\n\tsearch --no-floppy --fs-uuid --set=root {root_uuid}\n"
        f"\tlinux /boot/{kernel}-{kver} root=UUID={root_uuid} ro\n\tinitrd /boot/{initrd}-{kver}.img\n}}\n"
    )


def kernel_files(root: Path, kver: str, *, initramfs: bool | str = True, image: bool = True, modules: bool = True, prefix: str = "boot/vmlinuz-",
                 initrd_prefix: str = "boot/initramfs-", initrd_suffix: str = ".img") -> None:
    if image:
        put(root, f"{prefix}{kver}", size=11 << 20)
    if initramfs == "truncated":
        put(root, f"{initrd_prefix}{kver}{initrd_suffix}", size=0)
    elif initramfs:
        put(root, f"{initrd_prefix}{kver}{initrd_suffix}", size=32 << 20)
    if modules:
        put(root, f"usr/lib/modules/{kver}/modules.dep", "kernel/fs/ext4/ext4.ko:\n")


def esp_tree(esp: Path, family: str, *, loaders: bool = True, fallback: bool = True, shim: bool = False, dirname: str | None = None, systemd_boot: bool = False) -> Path:
    """The contents of an EFI system partition (a separate tree, mounted below the root in sessions)."""
    esp.mkdir(parents=True, exist_ok=True)
    directory = dirname or {"gentoo": "Gentoo"}.get(family, family)
    if loaders and not systemd_boot:
        put(esp, f"EFI/{directory}/grubx64.efi", size=2 << 20)
        if shim:
            put(esp, f"EFI/{directory}/shimx64.efi", size=1 << 20)
            put(esp, f"EFI/{directory}/mmx64.efi", size=1 << 20)
            put(esp, f"EFI/{directory}/BOOTX64.CSV", "shimx64.efi,Linux,,This is the boot entry for Linux\n")
        put(esp, f"EFI/{directory}/grub.cfg", f"search.fs_uuid {ROOT_UUID} root\nset prefix=($root)'/boot/grub'\nconfigfile $prefix/grub.cfg\n")
    if systemd_boot:
        put(esp, "EFI/systemd/systemd-bootx64.efi", size=1 << 20)
        put(esp, "loader/loader.conf", "default arch.conf\ntimeout 3\n")
        put(esp, "loader/entries/arch.conf", f"title Arch Linux\nlinux /vmlinuz-linux\ninitrd /initramfs-linux.img\noptions root=UUID={ROOT_UUID} rw\n")
    if fallback:
        put(esp, "EFI/BOOT/BOOTX64.EFI", size=2 << 20)
        if shim:
            put(esp, "EFI/BOOT/fbx64.efi", size=1 << 20)
    return esp


def make_root(base: Path, family: str, **opts: Any) -> Path:
    """Create ``base`` as the root of a system of the given family and return it.

    ``family`` is "gentoo", or the name of another distribution (a key of OS_RELEASE), or "unknown" (a Linux that says nothing about itself),
    or "data" (not a system at all).

    Options of a Gentoo: ``init`` (openrc|systemd), ``arch`` ("aarch64" for programs the live system cannot run), ``bootloader`` (grub|systemd-boot|efistub|none),
    ``esp_mount``, ``kernels`` (list of versions), ``initramfs`` (True|False|'truncated'), ``fstab`` (full text), ``root_fs`` (the type fstab gives the root), ``crypttab``, ``selinux``, ``grub_cfg`` (bool),
    ``kernel_image`` (bool), ``root_uuid``, ``separate_boot`` (no boot files in the root tree), ``receipt`` (dict for install.json).
    """
    base.mkdir(parents=True, exist_ok=True)
    builder = BUILDERS[family]
    builder(base, opts)
    return base


def _common_boot(root: Path, opts: dict[str, Any], kvers: list[str], *, image_prefix: str = "boot/vmlinuz-", initrd_prefix: str = "boot/initramfs-", initrd_suffix: str = ".img",
                 cfg: str = "boot/grub/grub.cfg") -> None:
    if opts.get("separate_boot"):
        return
    for kver in kvers:
        kernel_files(root, kver, initramfs=opts.get("initramfs", True), image=opts.get("kernel_image", True), prefix=image_prefix, initrd_prefix=initrd_prefix,
                     initrd_suffix=initrd_suffix)
    if opts.get("bootloader", "grub") == "grub" and opts.get("grub_cfg", True):
        put(root, cfg, grub_cfg(kvers[0], root_uuid=opts.get("root_uuid", ROOT_UUID)))


def _fstab_and_crypttab(root: Path, opts: dict[str, Any], default_esp_mount: str, *, root_fs: str = "ext4") -> None:
    if "fstab" in opts:
        if opts["fstab"] is not None:
            put(root, "etc/fstab", opts["fstab"])
    else:
        uefi = opts.get("firmware", "bios") == "uefi"
        if uefi:
            (root / opts.get("esp_mount", default_esp_mount).lstrip("/")).mkdir(parents=True, exist_ok=True)
        put(root, "etc/fstab", fstab_text(root=opts.get("root_uuid", ROOT_UUID), esp=ESP_UUID if uefi else None, esp_mount=opts.get("esp_mount", default_esp_mount),
                                         boot=BOOT_UUID if opts.get("separate_boot_fs") else None, root_fs=root_fs))
    if opts.get("crypttab"):
        put(root, "etc/crypttab", opts["crypttab"])


def _gentoo(root: Path, opts: dict[str, Any]) -> None:
    init = opts.get("init", "openrc")
    base_system(root, init=init)
    write_os_release(root, "gentoo")
    put(root, "etc/gentoo-release", "Gentoo Base System release 2.18\n")
    suffix = "/systemd" if init == "systemd" else ""
    link(root, "etc/portage/make.profile", f"../../var/db/repos/gentoo/profiles/default/linux/amd64/23.0/desktop/plasma{suffix}")
    put(root, "etc/portage/make.conf", 'COMMON_FLAGS="-O2 -pipe"\nCPU_FLAGS_X86="aes avx2"\n')
    kvers = opts.get("kernels", [GENTOO_KVER])
    for kver in kvers:
        pkg = kver.split("-")[0]
        suffix_name = opts.get("kernel_pkg") or ("gentoo-kernel-bin" if kver.endswith("-bin") else "gentoo-kernel")
        put(root, f"var/db/pkg/sys-kernel/{suffix_name}-{pkg}/CATEGORY", "sys-kernel\n")
        put(root, f"var/db/pkg/sys-kernel/{suffix_name}-{pkg}/PF", f"{suffix_name}-{pkg}\n")
        if opts.get("kernel_pkg"):  # a package from an overlay: only the eclasses it inherited tell that it is a distribution kernel
            put(root, f"var/db/pkg/sys-kernel/{suffix_name}-{pkg}/INHERITED", "toolchain-funcs linux-info dist-kernel-utils kernel-install kernel-build optfeature\n")
        put(root, f"usr/src/linux-{kver}/include/config/kernel.release", kver + "\n")
    put(root, "var/db/pkg/sys-kernel/installkernel-68-r1/USE", opts.get("installkernel_use", "dracut grub\n"))
    put(root, "var/db/pkg/sys-kernel/installkernel-68-r1/PF", "installkernel-68-r1\n")
    _common_boot(root, opts, kvers)
    if not opts.get("separate_boot"):
        put(root, "boot/amd-uc.img", size=1 << 20)
    for name in ("grub-install", "grub-mkconfig", "dracut", "emerge", "eselect", "chpasswd", "usermod", "chage", "passwd", "efibootmgr", "genkernel" if opts.get("genkernel") else "", "revdep-rebuild" if opts.get("gentoolkit") else "", "bootctl" if opts.get("bootctl") else ""):
        if name:
            tool(root, f"usr/bin/{name}", elf())
    put(root, "etc/passwd", "root:x:0:0:root:/root:/bin/bash\nuser:x:1000:1000::/home/user:/bin/bash\nnobody:x:65534:65534::/:/sbin/nologin\n")
    put(root, "etc/shadow", "root:$6$salt$hash:19000:0:::::\nuser:!:19000:0:99999:7:::\n", mode=0o640)
    put(root, "etc/login.defs", "UID_MIN 1000\n")
    _fstab_and_crypttab(root, opts, "/efi", root_fs=opts.get("root_fs", "ext4"))
    if opts.get("kernel_cmdline", True):
        put(root, "etc/kernel/cmdline", f"root=UUID={opts.get('root_uuid', ROOT_UUID)} ro quiet\n")
    if "receipt" in opts:
        put(root, "etc/gentoo-live-installer/install.json", json.dumps(opts["receipt"]))
    put(root, "var/log/emerge.log", "1700000000: Started emerge on\n")
    put(root, "var/log/messages", "Oct 24 08:00:00 localhost kernel: boot\n")
    if opts.get("arch") == "aarch64":  # the programs are for another processor: the live system cannot run them
        for name in ("usr/bin/env", "usr/bin/sh"):
            tool(root, name, elf(0xB7))


def _other_linux(root: Path, opts: dict[str, Any], key: str) -> None:
    """Any distribution but Gentoo: enough to be identified and listed (the repair tool never reads more of it than that)."""
    openrc = key in {"alpine", "void"}
    base_system(root, init="openrc" if openrc else "systemd", merged=key != "alpine")
    write_os_release(root, key)
    put(root, "etc/passwd", "root:x:0:0:root:/root:/bin/bash\nuser:x:1000:1000::/home/user:/bin/bash\n")
    _fstab_and_crypttab(root, opts, "/boot/efi")


def _unknown(root: Path, opts: dict[str, Any]) -> None:
    for name in ("bin", "usr/bin", "etc"):
        (root / name).mkdir(parents=True, exist_ok=True)
    tool(root, "bin/sh", elf(0x3E))
    _fstab_and_crypttab(root, opts, "/boot")


def _not_a_system(root: Path, opts: dict[str, Any]) -> None:
    put(root, "photos/readme.txt", "holiday\n")
    put(root, "backup/notes.txt", "x\n")


BUILDERS = {
    "gentoo": _gentoo,
    **{key: (lambda root, opts, key=key: _other_linux(root, opts, key)) for key in ("debian", "ubuntu", "fedora", "arch", "opensuse", "alpine", "void", "nixos")},
    "unknown": _unknown,
    "data": _not_a_system,
}


def nvidia(root: Path, *, driver: str = "580.126.18", module: str | None = "580.126.18", kver: str = GENTOO_KVER, nouveau: bool = True, video_cards: str | None = "nvidia",
           distfile: bool = True, blacklist: bool = False) -> None:
    """The NVIDIA driver in a Gentoo tree: its Portage record, the kernel module of ``kver`` (``module`` is the version inside it; None = no module), VIDEO_CARDS and the distfile."""
    if driver:
        put(root, f"var/db/pkg/x11-drivers/nvidia-drivers-{driver}/CATEGORY", "x11-drivers\n")
        put(root, f"var/db/pkg/x11-drivers/nvidia-drivers-{driver}/PF", f"nvidia-drivers-{driver}\n")
    if module is not None:
        target = put(root, f"usr/lib/modules/{kver}/video/nvidia.ko")
        target.write_bytes(b"\x7fELF" + b"\0" * 4096 + b"\0license=NVIDIA\0version=" + module.encode() + b"\0vermagic=6.18\0" + b"\0" * 4096)
    put(root, f"usr/lib/modules/{kver}/modules.dep", "kernel/fs/ext4/ext4.ko:\n" + ("kernel/drivers/gpu/drm/nouveau/nouveau.ko:\n" if nouveau else ""))
    if video_cards is not None:
        put(root, "etc/portage/make.conf", f'COMMON_FLAGS="-O2 -pipe"\nVIDEO_CARDS="{video_cards}"\nCPU_FLAGS_X86="aes avx2"\n')
    if distfile:
        put(root, f"var/cache/distfiles/NVIDIA-Linux-x86_64-{driver}.run", "x")
    if blacklist:
        put(root, "etc/modprobe.d/blacklist-nouveau.conf", "# keep nouveau away\nblacklist nouveau\noptions nouveau modeset=0\n")
    put(root, "usr/src/linux/Makefile", "VERSION = 6\n")
