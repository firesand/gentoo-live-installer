"""Boot facts of a mounted system: kernels, initramfs images, loaders and configuration (offline file checks)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .errors import RescueError
from .paths import RootFS

MIN_INITRAMFS_BYTES = 1 << 20  # anything smaller is an interrupted write
KERNEL_IMAGE_PREFIXES = ("vmlinuz-", "vmlinux-", "kernel-")
INITRAMFS_PATTERNS = ("initramfs-{v}.img", "initrd.img-{v}", "initrd-{v}", "initramfs-{v}", "initrd-{v}.img", "initramfs-{v}.gz")
BOOT_TOOLS = (
    "grub-install", "grub2-install", "update-grub", "grub-mkconfig", "grub2-mkconfig", "bootctl", "kernel-install", "dracut", "mkinitcpio", "update-initramfs", "genkernel",
    "mkinitfs", "mkinitrd", "dnf", "yum", "apt-get", "pacman", "zypper", "emerge", "efibootmgr", "chpasswd", "passwd", "usermod", "journalctl", "kernelstub", "refind-install",
    "shim-install", "update-bootloader", "eselect", "lsinitrd", "grubby", "sbctl", "ukify", "uefi-mkconfig", "revdep-rebuild", "rpm", "dpkg-query",
)
CMDLINE_FILES = ("/etc/kernel/cmdline", "/usr/lib/kernel/cmdline", "/etc/cmdline", "/etc/default/grub", "/etc/kernel/uki.conf")


@dataclass
class KernelInfo:
    version: str
    image: str = ""
    image_size: int = 0
    initramfs: str = ""
    initramfs_size: int = 0
    modules: bool = False
    pkgbase: str = ""

    @property
    def initramfs_truncated(self) -> bool:
        return bool(self.initramfs) and self.initramfs_size < MIN_INITRAMFS_BYTES

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version, "image": bool(self.image), "image_path": self.image, "initramfs": bool(self.initramfs), "initramfs_path": self.initramfs,
            "initramfs_size": self.initramfs_size, "modules": self.modules,
        }


@dataclass
class BootInfo:
    kernels: list[KernelInfo] = field(default_factory=list)
    loaders: list[str] = field(default_factory=list)  # every loader whose files are present
    loader: str = ""  # best guess of the active one
    grub_cfg: str = ""
    grub_cfg_size: int = 0
    bls_entries: list[str] = field(default_factory=list)
    grub_platforms: list[str] = field(default_factory=list)
    cmdline_sources: list[str] = field(default_factory=list)
    tools: dict[str, str] = field(default_factory=dict)
    microcode: list[str] = field(default_factory=list)
    uki: list[str] = field(default_factory=list)
    esp_files: dict[str, list[str]] = field(default_factory=dict)  # loader directory name -> files
    has_boot_dir: bool = True

    @property
    def uses_initramfs(self) -> bool:
        return any(kernel.initramfs for kernel in self.kernels) or any(tool in self.tools for tool in ("dracut", "mkinitcpio", "update-initramfs", "genkernel", "mkinitfs", "mkinitrd"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kernels": [kernel.to_dict() for kernel in self.kernels], "loaders": self.loaders, "loader": self.loader, "grub_cfg": self.grub_cfg, "grub_cfg_size": self.grub_cfg_size,
            "bls": bool(self.bls_entries), "grub_platforms": self.grub_platforms, "cmdline_sources": self.cmdline_sources, "microcode": self.microcode, "uki": self.uki,
            "tools": sorted(self.tools), "esp_dirs": sorted(self.esp_files),
        }


def _size(fs: RootFS, path: str) -> int:
    try:
        return fs.stat(path).st_size
    except (OSError, RescueError):
        return 0


def _module_versions(fs: RootFS) -> dict[str, str]:
    """kernel release -> pkgbase ('' when the distribution does not record one)."""
    result: dict[str, str] = {}
    for base in ("/usr/lib/modules", "/lib/modules"):
        for version in fs.listdir_or(base):
            if fs.is_dir(f"{base}/{version}"):
                result.setdefault(version, fs.read_text_or(f"{base}/{version}/pkgbase", "", 256).strip())
    return result


def _find_initramfs(fs: RootFS, version: str, pkgbase: str, boot_names: set[str]) -> str:
    keys = [version] + ([pkgbase] if pkgbase else [])
    for key in keys:
        for pattern in INITRAMFS_PATTERNS:
            name = pattern.format(v=key)
            if name in boot_names:
                return "/boot/" + name
    return ""


def collect_kernels(fs: RootFS) -> list[KernelInfo]:
    boot_names = set(fs.listdir_or("/boot"))
    versions = _module_versions(fs)
    found: dict[str, KernelInfo] = {}
    for name in sorted(boot_names):
        for prefix in KERNEL_IMAGE_PREFIXES:
            if name.startswith(prefix) and not name.endswith((".old", ".bak", ".sig")):
                suffix = name[len(prefix):]
                version = suffix
                pkgbase = ""
                for release, base in versions.items():  # Arch names the image after the package base, not the release
                    if base and base == suffix:
                        version, pkgbase = release, base
                info = found.setdefault(version, KernelInfo(version, pkgbase=pkgbase))
                info.image, info.image_size = f"/boot/{name}", _size(fs, f"/boot/{name}")
    for release, base in versions.items():
        found.setdefault(release, KernelInfo(release, pkgbase=base))
        found[release].modules = fs.exists(f"/usr/lib/modules/{release}/modules.dep") or fs.exists(f"/lib/modules/{release}/modules.dep")
        if not found[release].image and fs.exists(f"/usr/lib/modules/{release}/vmlinuz") and not boot_names:
            found[release].image = f"/usr/lib/modules/{release}/vmlinuz"
    for info in found.values():
        info.initramfs = _find_initramfs(fs, info.version, info.pkgbase, boot_names)
        info.initramfs_size = _size(fs, info.initramfs) if info.initramfs else 0
    return sorted(found.values(), key=lambda item: item.version)


def collect_boot(fs: RootFS, esp: RootFS | None = None) -> BootInfo:
    info = BootInfo(has_boot_dir=fs.is_dir("/boot"))
    info.kernels = [kernel for kernel in collect_kernels(fs) if kernel.image or kernel.modules]
    for tool in BOOT_TOOLS:
        found = fs.which(tool)
        if found:
            info.tools[tool] = found
    for cfg in ("/boot/grub/grub.cfg", "/boot/grub2/grub.cfg"):
        if fs.is_file(cfg):
            info.grub_cfg, info.grub_cfg_size = cfg, _size(fs, cfg)
            break
    for directory in ("/boot/grub", "/boot/grub2"):
        for platform in fs.listdir_or(directory):
            if platform in {"i386-pc", "x86_64-efi", "i386-efi", "arm64-efi"} and platform not in info.grub_platforms:
                info.grub_platforms.append(platform)
    info.bls_entries = [name for name in fs.listdir_or("/boot/loader/entries") if name.endswith(".conf")]
    info.microcode = [name for name in fs.listdir_or("/boot") if name.endswith("-uc.img") or name in {"intel-ucode.img", "amd-ucode.img"}]
    info.cmdline_sources = [path for path in CMDLINE_FILES if fs.is_file(path)] + [f"/etc/cmdline.d/{name}" for name in fs.listdir_or("/etc/cmdline.d") if name.endswith(".conf")]
    loaders: list[str] = []
    if info.grub_cfg or info.grub_platforms:
        loaders.append("grub")
    if fs.is_file("/boot/extlinux/extlinux.conf"):
        loaders.append("extlinux")
    if fs.is_file("/etc/lilo.conf"):
        loaders.append("lilo")
    boot_loader_conf = fs.is_file("/boot/loader/loader.conf")  # ESP mounted at /boot (Arch, Pop!_OS)
    if boot_loader_conf:
        loaders.append("systemd-boot")
    for tree in ([esp] if esp is not None else []):
        _scan_esp(tree, info, loaders)
    if "kernelstub" in info.tools and "systemd-boot" not in loaders and not info.grub_cfg:
        loaders.append("systemd-boot")
    info.loaders = list(dict.fromkeys(loaders))
    info.loader = _guess_active(info)
    return info


def _scan_esp(esp: RootFS, info: BootInfo, loaders: list[str]) -> None:
    for directory in esp.listdir_or("/EFI"):
        files = esp.listdir_or(f"/EFI/{directory}")
        info.esp_files[directory] = files
        lowered = {name.lower() for name in files}
        if directory.lower() == "refind" or "refind_x64.efi" in lowered:
            loaders.append("refind")
        elif directory.lower() == "systemd" or "systemd-bootx64.efi" in lowered:
            loaders.append("systemd-boot")
        elif directory.lower() == "linux":
            info.uki = [name for name in files if name.lower().endswith(".efi")]
        elif directory.lower() not in {"boot", "microsoft"} and ({"grubx64.efi", "shimx64.efi", "grub.efi"} & lowered):
            loaders.append("grub")
    if esp.is_file("/loader/loader.conf") and "systemd-boot" not in loaders:
        loaders.append("systemd-boot")


def _guess_active(info: BootInfo) -> str:
    if not info.loaders:
        return "efistub" if info.uki else ""
    if len(info.loaders) == 1:
        return info.loaders[0]
    if "systemd-boot" in info.loaders and "grub" in info.loaders and info.grub_cfg:
        return "grub"  # both installed; the diagnosis refines this with the firmware's boot order
    return info.loaders[0]


# ---- kernel command line sources (review R3) --------------------------------------------------------------------
GRUB_VAR_RE = re.compile(r'^\s*(GRUB_CMDLINE_LINUX(?:_DEFAULT)?)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S*))\s*(?:#.*)?$')
LIVE_TOKENS = ("root=live:", "rd.live.", "rd.live=", "BOOT_IMAGE=", "initrd=", "cdroot", "dokeymap", "nodhcp", "secureconsole")


def grub_default_cmdline(text: str) -> str:
    parts: list[str] = []
    for line in text.splitlines():
        match = GRUB_VAR_RE.match(line)
        if match:
            parts.append(match.group(2) or match.group(3) or match.group(4) or "")
    return " ".join(part for part in parts if part).strip()


def contains_live_arguments(cmdline: str) -> bool:
    return any(token in cmdline for token in ("root=live:", "rd.live.", "CDLABEL=", "cdroot"))


def first_text_cmdline(fs: RootFS) -> tuple[str, str]:
    """(cmdline, source path) from the target's own configuration, '' when it has none."""
    for path in ("/etc/kernel/cmdline", "/usr/lib/kernel/cmdline", "/etc/cmdline"):
        text = fs.read_text_or(path, "", 4096).strip()
        if text:
            return " ".join(text.split()), path
    for name in sorted(fs.listdir_or("/etc/cmdline.d")):
        if name.endswith(".conf"):
            text = fs.read_text_or(f"/etc/cmdline.d/{name}", "", 4096).strip()
            if text:
                return " ".join(text.split()), f"/etc/cmdline.d/{name}"
    text = grub_default_cmdline(fs.read_text_or("/etc/default/grub", "", 16384))
    if text:
        return text, "/etc/default/grub"
    return "", ""
