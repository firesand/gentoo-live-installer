"""Fake target roots for the distributions rescue supports.

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
DEBIAN_KVER = "6.12.43+deb13-amd64"
UBUNTU_KVER = "6.8.0-45-generic"
FEDORA_KVER = "6.11.9-300.fc41.x86_64"
ARCH_KVER = "6.11.5-arch1-1"
SUSE_KVER = "6.11.5-1-default"

OS_RELEASE = {
    "gentoo": 'NAME=Gentoo\nID=gentoo\nPRETTY_NAME="Gentoo Linux"\nANSI_COLOR="1;32"\nHOME_URL="https://www.gentoo.org/"\n',
    "debian": 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nNAME="Debian GNU/Linux"\nVERSION_ID="13"\nVERSION="13 (trixie)"\nVERSION_CODENAME=trixie\nID=debian\n',
    "ubuntu": 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nNAME="Ubuntu"\nVERSION_ID="24.04"\nVERSION="24.04.1 LTS (Noble Numbat)"\nID=ubuntu\nID_LIKE=debian\n',
    "pop": 'NAME="Pop!_OS"\nVERSION="22.04 LTS"\nID=pop\nID_LIKE="ubuntu debian"\nPRETTY_NAME="Pop!_OS 22.04 LTS"\nVERSION_ID="22.04"\n',
    "fedora": 'NAME="Fedora Linux"\nVERSION="41 (Cloud Edition)"\nID=fedora\nVERSION_ID=41\nVARIANT="Cloud Edition"\nVARIANT_ID=cloud\nPRETTY_NAME="Fedora Linux 41 (Cloud Edition)"\n',
    "rhel": 'NAME="Red Hat Enterprise Linux"\nVERSION="9.4 (Plow)"\nID="rhel"\nID_LIKE="fedora"\nVERSION_ID="9.4"\nPRETTY_NAME="Red Hat Enterprise Linux 9.4 (Plow)"\n',
    "arch": 'NAME="Arch Linux"\nPRETTY_NAME="Arch Linux"\nID=arch\nBUILD_ID=rolling\nANSI_COLOR="38;2;23;147;209"\n',
    "opensuse": 'NAME="openSUSE Tumbleweed"\n# VERSION="20241020"\nID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\nVERSION_ID="20241020"\nPRETTY_NAME="openSUSE Tumbleweed"\n',
    "microos": 'NAME="openSUSE MicroOS"\nID="opensuse-microos"\nID_LIKE="suse opensuse opensuse-tumbleweed"\nVERSION_ID="20241020"\nPRETTY_NAME="openSUSE MicroOS"\n',
    "silverblue": 'NAME="Fedora Linux"\nID=fedora\nVERSION_ID=41\nVARIANT_ID=silverblue\nPRETTY_NAME="Fedora Linux 41 (Silverblue)"\n',
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


def base_system(root: Path, family: str, *, merged: bool = True, init: str = "systemd") -> None:
    if merged:
        merged_usr(root, sbin_link="usr/bin" if family in {"gentoo", "arch", "fedora", "opensuse"} else "usr/sbin")
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
        link(root, "usr/sbin/init" if not merged or family in {"debian", "ubuntu"} else "usr/bin/init", "../lib/systemd/systemd")
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
    names = {"gentoo": "Gentoo", "debian": "debian", "ubuntu": "ubuntu", "fedora": "fedora", "rhel": "redhat", "arch": "GRUB", "opensuse": "opensuse", "pop": "Pop_OS-1234"}
    directory = dirname or names.get(family, family)
    if loaders and not systemd_boot:
        put(esp, f"EFI/{directory}/grubx64.efi", size=2 << 20)
        if shim:
            put(esp, f"EFI/{directory}/shimx64.efi", size=1 << 20)
            put(esp, f"EFI/{directory}/mmx64.efi", size=1 << 20)
            put(esp, f"EFI/{directory}/BOOTX64.CSV", "shimx64.efi,Debian,,This is the boot entry for Debian\n")
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

    Options: ``init`` (openrc|systemd), ``bootloader`` (grub|systemd-boot|efistub|none), ``esp_mount``,
    ``kernels`` (list of versions), ``initramfs`` (True|False|'truncated'), ``fstab`` (full text),
    ``crypttab``, ``selinux``, ``grub_cfg`` (bool), ``shim`` (bool), ``kernel_image`` (bool),
    ``root_uuid``, ``release`` (key into OS_RELEASE), ``os_release`` (overrides), ``separate_boot`` (no
    boot files in the root tree), ``receipt`` (dict for install.json).
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
    base_system(root, "gentoo", init=init)
    write_os_release(root, "gentoo")
    put(root, "etc/gentoo-release", "Gentoo Base System release 2.18\n")
    suffix = "/systemd" if init == "systemd" else ""
    link(root, "etc/portage/make.profile", f"../../var/db/repos/gentoo/profiles/default/linux/amd64/23.0/desktop/plasma{suffix}")
    put(root, "etc/portage/make.conf", 'COMMON_FLAGS="-O2 -pipe"\nCPU_FLAGS_X86="aes avx2"\n')
    kvers = opts.get("kernels", [GENTOO_KVER])
    for kver in kvers:
        pkg = kver.split("-")[0]
        suffix_name = "gentoo-kernel-bin" if kver.endswith("-bin") else "gentoo-kernel"
        put(root, f"var/db/pkg/sys-kernel/{suffix_name}-{pkg}/CATEGORY", "sys-kernel\n")
        put(root, f"var/db/pkg/sys-kernel/{suffix_name}-{pkg}/PF", f"{suffix_name}-{pkg}\n")
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
    _fstab_and_crypttab(root, opts, "/efi")
    if opts.get("kernel_cmdline", True):
        put(root, "etc/kernel/cmdline", f"root=UUID={opts.get('root_uuid', ROOT_UUID)} ro quiet\n")
    if "receipt" in opts:
        put(root, "etc/gentoo-live-installer/install.json", json.dumps(opts["receipt"]))
    put(root, "var/log/emerge.log", "1700000000: Started emerge on\n")
    put(root, "var/log/messages", "Oct 24 08:00:00 localhost kernel: boot\n")


def _debian_like(root: Path, opts: dict[str, Any], key: str) -> None:
    base_system(root, key, init="systemd", merged=opts.get("merged", True))
    write_os_release(root, key)
    put(root, "etc/debian_version", "13.0\n")
    kver = opts.get("kernels", [UBUNTU_KVER if key == "ubuntu" else DEBIAN_KVER])
    for ver in kver:
        put(root, f"var/lib/dpkg/info/linux-image-{ver}.list", f"/boot/vmlinuz-{ver}\n/boot/config-{ver}\n/lib/modules/{ver}/kernel/x.ko\n")
    status = "".join(f"Package: linux-image-{ver}\nStatus: install ok installed\nVersion: 1.0\n\n" for ver in kver)
    if opts.get("half_configured"):
        status += "Package: grub-efi-amd64\nStatus: install ok half-configured\nVersion: 2.12\n\n"
    put(root, "var/lib/dpkg/status", status)
    _common_boot(root, opts, kver, initrd_prefix="boot/initrd.img-", initrd_suffix="")
    for name in ("update-grub", "update-initramfs", "grub-install", "grub-mkconfig", "apt-get", "dpkg", "chpasswd", "usermod", "chage", "passwd", "efibootmgr", "dpkg-query", "bootctl" if key == "pop" else ""):
        if not name:
            continue
        body = elf() + (b"--uefi-secure-boot --removable --bootloader-id" if name == "grub-install" and opts.get("secure_boot_flag", True) else b"")
        tool(root, f"usr/sbin/{name}" if not opts.get("merged", True) or name in {"update-grub", "update-initramfs", "grub-install", "grub-mkconfig", "chpasswd", "usermod"} else f"usr/bin/{name}", body)
    put(root, "etc/initramfs-tools/initramfs.conf", "MODULES=dep\nBUSYBOX=auto\n")
    put(root, "etc/default/grub", 'GRUB_DEFAULT=0\nGRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\nGRUB_CMDLINE_LINUX=""\n')
    if opts.get("shim", opts.get("firmware") == "uefi"):
        put(root, "usr/lib/shim/shimx64.efi.signed", size=1 << 20)
        put(root, "usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed", size=2 << 20)
    put(root, "etc/passwd", "root:x:0:0:root:/root:/bin/bash\nuser:x:1000:1000::/home/user:/bin/bash\n")
    put(root, "etc/shadow", "root:*:19000:0:99999:7:::\nuser:$y$j9T$x:19000:0:99999:7:::\n", mode=0o640)
    put(root, "etc/login.defs", "UID_MIN 1000\n")
    _fstab_and_crypttab(root, opts, "/boot/efi")
    put(root, "var/log/syslog", "Oct 24 08:00:00 host kernel: boot\n")


FEDORA_GRUB_CFG = (
    "#\n# DO NOT EDIT THIS FILE\n#\n# It is automatically generated by grub2-mkconfig using templates\n# from /etc/grub.d and settings from /etc/default/grub\n#\n"
    "### BEGIN /etc/grub.d/00_header ###\nset pager=1\nif [ -f ${config_directory}/grubenv ]; then\n  load_env -f ${config_directory}/grubenv\nelif [ -s $prefix/grubenv ]; then\n  load_env\nfi\n"
    "if [ \"${next_entry}\" ] ; then\n   set default=\"${next_entry}\"\n   set next_entry=\n   save_env next_entry\n   set boot_once=true\nelse\n   set default=\"${saved_entry}\"\nfi\n"
    "### END /etc/grub.d/00_header ###\n" + "### BEGIN /etc/grub.d/08_fallback_counting ###\ninsmod increment\n### END /etc/grub.d/08_fallback_counting ###\n" * 4 +
    "### BEGIN /etc/grub.d/10_linux ###\ninsmod part_gpt\ninsmod ext2\nset root='hd0,gpt3'\ninsmod blscfg\nblscfg\n### END /etc/grub.d/10_linux ###\n")


def _fedora_like(root: Path, opts: dict[str, Any], key: str) -> None:
    base_system(root, key, init="systemd")
    write_os_release(root, key)
    put(root, "etc/fedora-release" if key == "fedora" else "etc/redhat-release", "Fedora release 41\n" if key == "fedora" else "Red Hat Enterprise Linux release 9.4\n")
    kvers = opts.get("kernels", [FEDORA_KVER])
    machine_id = "0123456789abcdef0123456789abcdef"
    put(root, "etc/machine-id", machine_id + "\n")
    for ver in kvers:
        put(root, f"usr/lib/modules/{ver}/vmlinuz", size=11 << 20)
        put(root, f"usr/lib/modules/{ver}/modules.dep", "x\n")
        if not opts.get("separate_boot"):
            put(root, f"boot/vmlinuz-{ver}", size=11 << 20)
            if opts.get("initramfs", True):
                put(root, f"boot/initramfs-{ver}.img", size=(0 if opts.get("initramfs") == "truncated" else 30 << 20))
            if opts.get("bls", True):
                put(root, f"boot/loader/entries/{machine_id}-{ver}.conf",
                    f"title Fedora Linux ({ver})\nversion {ver}\nlinux /vmlinuz-{ver}\ninitrd /initramfs-{ver}.img\n"
                    f"options root=UUID={opts.get('root_uuid', ROOT_UUID)} ro rootflags=subvol=root\ngrub_users $grub_users\ngrub_arg --unrestricted\ngrub_class fedora\n")
    if not opts.get("separate_boot") and opts.get("grub_cfg", True):
        put(root, "boot/grub2/grub.cfg", FEDORA_GRUB_CFG)
    for name in ("dnf", "dracut", "kernel-install", "rpm", "chpasswd", "usermod", "chage", "passwd", "efibootmgr", "grub2-mkconfig", "grub2-install", "grubby"):
        tool(root, f"usr/bin/{name}", elf())
    if opts.get("selinux", key == "fedora"):
        put(root, "etc/selinux/config", "SELINUX=enforcing\nSELINUXTYPE=targeted\n")
        (root / "etc/selinux/targeted").mkdir(parents=True, exist_ok=True)
    put(root, "var/lib/rpm/rpmdb.sqlite", "")
    put(root, "etc/passwd", "root:x:0:0:root:/root:/bin/bash\nuser:x:1000:1000::/home/user:/bin/bash\n")
    put(root, "etc/shadow", "root:!locked::0:99999:7:::\nuser:$y$j9T$x:19000:0:99999:7:::\n", mode=0o600)
    put(root, "etc/login.defs", "UID_MIN 1000\n")
    _fstab_and_crypttab(root, opts, "/boot/efi", root_fs="btrfs" if opts.get("btrfs") else "ext4")
    put(root, "var/log/dnf.log", "2024-10-24 dnf start\n")


def _arch(root: Path, opts: dict[str, Any]) -> None:
    base_system(root, "arch", init="systemd")
    write_os_release(root, "arch")
    put(root, "etc/arch-release", "")
    kvers = opts.get("kernels", [ARCH_KVER])
    for ver in kvers:
        put(root, f"usr/lib/modules/{ver}/pkgbase", "linux\n")
        put(root, f"usr/lib/modules/{ver}/modules.dep", "x\n")
        put(root, f"usr/lib/modules/{ver}/vmlinuz", size=11 << 20)
        put(root, f"var/lib/pacman/local/linux-{ver.replace('-arch', '.arch').replace('-1', '-1')}/desc", "%NAME%\nlinux\n")
    if not opts.get("separate_boot"):
        put(root, "boot/vmlinuz-linux", size=11 << 20)
        if opts.get("initramfs", True):
            put(root, "boot/initramfs-linux.img", size=20 << 20)
        if opts.get("bootloader", "grub") == "grub":
            put(root, "boot/grub/grub.cfg", grub_cfg(kvers[0], kernel="vmlinuz-linux", initrd="initramfs-linux").replace("/boot/vmlinuz-linux-" + kvers[0], "/boot/vmlinuz-linux").replace("/boot/initramfs-linux-" + kvers[0] + ".img", "/boot/initramfs-linux.img"))
    put(root, "etc/mkinitcpio.d/linux.preset", "ALL_kver='/boot/vmlinuz-linux'\nPRESETS=('default')\ndefault_image='/boot/initramfs-linux.img'\n")
    put(root, "etc/pacman.d/mirrorlist", "Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch\n")
    for name in ("pacman", "mkinitcpio", "grub-install", "grub-mkconfig", "bootctl", "chpasswd", "usermod", "passwd", "efibootmgr"):
        tool(root, f"usr/bin/{name}", elf())
    put(root, "etc/passwd", "root:x:0:0::/root:/bin/bash\nuser:x:1000:1000::/home/user:/bin/bash\n")
    put(root, "etc/shadow", "root:!*:19000::::::\nuser:$y$x:19000::::::\n", mode=0o600)
    _fstab_and_crypttab(root, opts, "/boot")
    put(root, "var/log/pacman.log", "[2024-10-24T08:00:00+0000] [PACMAN] Running 'pacman -Syu'\n")


def _opensuse(root: Path, opts: dict[str, Any]) -> None:
    key = opts.get("release", "opensuse")
    base_system(root, "opensuse", init="systemd")
    write_os_release(root, key)
    kvers = opts.get("kernels", [SUSE_KVER])
    for ver in kvers:
        put(root, f"usr/lib/modules/{ver}/modules.dep", "x\n")
        if not opts.get("separate_boot"):
            put(root, f"boot/vmlinuz-{ver}", size=11 << 20)
            put(root, f"boot/initrd-{ver}", size=25 << 20)
    put(root, "boot/grub2/grub.cfg", "set default=0\n# generated by grub2-mkconfig\nmenuentry 'openSUSE' {\n\tlinux /boot/vmlinuz-%s root=UUID=%s\n}\n" % (kvers[0], ROOT_UUID))
    put(root, "etc/sysconfig/bootloader", "LOADER_TYPE=grub2-efi\n")
    for name in ("zypper", "update-bootloader", "shim-install", "grub2-install", "grub2-mkconfig", "dracut", "chpasswd", "usermod", "passwd"):
        tool(root, f"usr/bin/{name}", elf())
    put(root, "etc/passwd", "root:x:0:0:root:/root:/bin/bash\n")
    put(root, "etc/shadow", "root:$6$x:19000::::::\n", mode=0o640)
    _fstab_and_crypttab(root, opts, "/boot/efi", root_fs="btrfs")
    if key == "microos":
        (root / "var/lib/overlay").mkdir(parents=True, exist_ok=True)


def _alpine(root: Path, opts: dict[str, Any]) -> None:
    base_system(root, "alpine", init="openrc", merged=False)
    write_os_release(root, "alpine")
    put(root, "etc/alpine-release", "3.20.3\n")
    for name in ("apk", "mkinitfs", "grub-install"):
        tool(root, f"usr/sbin/{name}" if name != "apk" else "sbin/apk", elf())
    _fstab_and_crypttab(root, opts, "/boot/efi")


def _void(root: Path, opts: dict[str, Any]) -> None:
    base_system(root, "void", init="systemd", merged=True)
    write_os_release(root, "void")
    _fstab_and_crypttab(root, opts, "/boot/efi")


def _nixos(root: Path, opts: dict[str, Any]) -> None:
    for name in ("nix/store", "etc", "boot", "bin", "usr/bin"):
        (root / name).mkdir(parents=True, exist_ok=True)
    write_os_release(root, "nixos")
    tool(root, "bin/sh", elf())
    _fstab_and_crypttab(root, opts, "/boot")


def _unknown(root: Path, opts: dict[str, Any]) -> None:
    for name in ("bin", "usr/bin", "etc"):
        (root / name).mkdir(parents=True, exist_ok=True)
    tool(root, "bin/sh", elf(0x3E))
    _fstab_and_crypttab(root, opts, "/boot")


def _aarch64(root: Path, opts: dict[str, Any]) -> None:
    _debian_like(root, opts, "debian")
    for name in ("usr/bin/env", "usr/bin/sh"):
        tool(root, name, elf(0xB7))


def _ostree(root: Path, opts: dict[str, Any]) -> None:
    _fedora_like(root, opts, "fedora")
    write_os_release(root, "silverblue")
    (root / "ostree/repo").mkdir(parents=True, exist_ok=True)


def _microos(root: Path, opts: dict[str, Any]) -> None:
    _opensuse(root, {**opts, "release": "microos"})


def _not_a_system(root: Path, opts: dict[str, Any]) -> None:
    put(root, "photos/readme.txt", "holiday\n")
    put(root, "backup/notes.txt", "x\n")


BUILDERS = {
    "gentoo": _gentoo,
    "debian": lambda r, o: _debian_like(r, o, "debian"),
    "ubuntu": lambda r, o: _debian_like(r, o, "ubuntu"),
    "pop": lambda r, o: _debian_like(r, o, "pop"),
    "fedora": lambda r, o: _fedora_like(r, o, "fedora"),
    "rhel": lambda r, o: _fedora_like(r, o, "rhel"),
    "arch": _arch,
    "opensuse": _opensuse,
    "microos": _microos,
    "alpine": _alpine,
    "void": _void,
    "nixos": _nixos,
    "unknown": _unknown,
    "aarch64": _aarch64,
    "ostree": _ostree,
    "data": _not_a_system,
}
