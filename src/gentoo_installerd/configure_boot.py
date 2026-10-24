"""BOOT: kernel, initramfs, bootloader, EFI and Secure Boot."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Sequence

from gentoo_installer import layout as guided_layout
from gentoo_installer.manifest import CMDLINE_TOKEN_RE, InstallManifest, System

from .configure_base import _plan_list, _plan_scalar, ConfigurationError, ConfigurerBase
from .configure_boot_extra import (  # noqa: F401 - re-exported for the callers of the old module
    DIST_KERNELS, ENROLL_FILE, SAFE_GRAPHICS_TITLE, SAFE_GRAPHICS_UKI, SECURE_BOOT_CERT, SECURE_BOOT_KEY, SHIM_NOT_YET, BootExtrasMixin,
    safe_tokens, secure_boot_enroll_steps)
from .configure_fstab import ExistingStack
from .runner import Runner, RunnerError
from .storage import LayoutEntry


NVRAM_FAILURE = re.compile(r"efibootmgr|EFI variables|efivars|NVRAM", re.IGNORECASE)


# --- keeping the EFI system partition current after kernel updates ------------------------------------

# One script, installed for both installkernel flavours: the traditional one runs /etc/kernel/postinst.d
# hooks as "hook <version> <image>", systemd's kernel-install runs /etc/kernel/install.d/*.install plugins as
# "plugin add|remove <version> <entry-dir> <image> [initrd...]".
ESP_SYNC_HOOKS = ("etc/kernel/postinst.d/95-gentoo-installer-esp", "etc/kernel/install.d/95-gentoo-installer-esp.install")


_ESP_SYNC_TEMPLATE = r"""#!/bin/sh
# Written by the Gentoo live installer.
# systemd-boot, the EFI stub and the Unified Kernel Image load their kernel from the EFI system
# partition, which installkernel does not touch. This hook copies each newly installed kernel there.
# It never fails the kernel installation: problems are only reported.
ROOT="${ESP_SYNC_ROOT:-}"
ESP="@ESP@"
UKI=@UKI@
FALLBACK_COPY=@FALLBACK@
SAFE=@SAFE@
SIGN=@SIGN@
SIGN_LOADER=@SIGN_LOADER@
SB_KEY="@SB_KEY@"
SB_CERT="@SB_CERT@"
case "$1" in
    add) shift ;;
    remove) exit 0 ;;
esac
version="$1"
[ -n "$version" ] || exit 0
kernel=
for candidate in "$ROOT/boot/vmlinuz-$version" "$ROOT/boot/kernel-$version"; do
    if [ -f "$candidate" ]; then kernel="$candidate"; break; fi
done
if [ -z "$kernel" ]; then
    echo "gentoo-installer-esp: no kernel image for $version under /boot; the EFI system partition was not updated" >&2
    exit 0
fi
initrd="$ROOT/boot/initramfs-$version.img"
[ -f "$initrd" ] || initrd=
if [ -z "$ROOT" ] && ! grep -q " $ESP " /proc/mounts 2>/dev/null; then
    echo "gentoo-installer-esp: $ESP is not mounted; the EFI system partition was not updated" >&2
    exit 0
fi
esp="$ROOT$ESP"
replace() {
    cp "$1" "$2.new" && mv -f "$2.new" "$2" || { rm -f "$2.new"; return 1; }
}
# Secure Boot: sign a file with the installed key unless it is already signed with it.
sign() {
    [ "$SIGN" = 1 ] || return 0
    if sbverify --cert "$ROOT$SB_CERT" "$1" >/dev/null 2>&1; then return 0; fi
    if sbsign --key "$ROOT$SB_KEY" --cert "$ROOT$SB_CERT" --output "$1.signed" "$1" && mv -f "$1.signed" "$1"; then return 0; fi
    rm -f "$1.signed"
    echo "gentoo-installer-esp: could not sign $1; the PC may refuse to start it with Secure Boot on" >&2
    return 1
}
# One Unified Kernel Image: name, file with its kernel command line.
build_uki() {
    name="$1"; cmdfile="$2"; out="$esp/EFI/Linux/$1"
    set -- ukify build --linux "$kernel" --cmdline "$(cat "$ROOT/etc/kernel/$cmdfile" 2>/dev/null)" --output "$out.new"
    [ -n "$initrd" ] && set -- "$@" --initrd "$initrd"
    if "$@" && sign "$out.new" && mv -f "$out.new" "$out"; then return 0; fi
    rm -f "$out.new"
    echo "gentoo-installer-esp: could not build the Unified Kernel Image $name for $version" >&2
    return 1
}
if [ "$UKI" = 1 ]; then
    mkdir -p "$esp/EFI/Linux"
    if build_uki gentoo.efi cmdline; then
        [ "$FALLBACK_COPY" = 1 ] && replace "$esp/EFI/Linux/gentoo.efi" "$esp/EFI/BOOT/BOOTX64.EFI"
    fi
    [ "$SAFE" = 1 ] && build_uki gentoo-safe.efi cmdline-safe
else
    replace "$kernel" "$esp/vmlinuz" || echo "gentoo-installer-esp: could not copy the kernel to $ESP" >&2
    if [ -n "$initrd" ]; then
        replace "$initrd" "$esp/initramfs" || echo "gentoo-installer-esp: could not copy the initramfs to $ESP" >&2
    fi
fi
# systemd-boot itself is updated by systemd; the new copy is unsigned until it is signed here.
if [ "$SIGN_LOADER" = 1 ]; then
    for loader in "$esp/EFI/systemd/systemd-bootx64.efi" "$esp/EFI/BOOT/BOOTX64.EFI"; do
        [ -f "$loader" ] && sign "$loader"
    done
fi
exit 0
"""


def render_esp_sync_hook(esp: str, uki: bool, fallback_copy: bool = False, *, safe: bool = False, sign: bool = False,
                         sign_loader: bool = False) -> str:
    """The hook script. ``safe`` also builds the safe-graphics image, ``sign`` signs each new image with the key in /etc/secureboot,
    ``sign_loader`` also signs systemd-boot (and the fallback copy) when systemd has replaced it."""
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", esp):
        raise ConfigurationError("Invalid EFI system partition mount point")
    flag = lambda value: "1" if value else "0"  # noqa: E731
    return (_ESP_SYNC_TEMPLATE.replace("@ESP@", esp).replace("@UKI@", flag(uki)).replace("@FALLBACK@", flag(fallback_copy))
            .replace("@SAFE@", flag(safe)).replace("@SIGN@", flag(sign)).replace("@SIGN_LOADER@", flag(sign_loader))
            .replace("@SB_KEY@", SECURE_BOOT_KEY).replace("@SB_CERT@", SECURE_BOOT_CERT))


# --- checks on the live system, for the daemon's preflight --------------------------------------------


def check_secure_boot_material(runner: Runner, system: System) -> list[str]:
    """Is the Secure Boot signing key readable and does the certificate belong to it? (Nothing to check when the installer makes the key.)"""
    if system.secure_boot_mode == "create":
        return []
    if system.secure_boot_mode == "shim":
        return [SHIM_NOT_YET]
    key, certificate = Path(system.secure_boot_key), Path(system.secure_boot_certificate)
    problems: list[str] = []
    for label, path in (("signing key", key), ("certificate", certificate)):
        if not path.is_file() or not os.access(path, os.R_OK):
            problems.append(f"The Secure Boot {label} is missing or not readable: {path}")
    if problems:
        return problems
    try:
        public_from_certificate = runner.run(("openssl", "x509", "-noout", "-pubkey", "-in", str(certificate))).stdout.strip()
        public_from_key = runner.run(("openssl", "pkey", "-pubout", "-in", str(key))).stdout.strip()
    except RunnerError as exc:
        return [f"The Secure Boot key or certificate cannot be parsed: {str(exc)[-200:]}"]
    if public_from_certificate and public_from_key and public_from_certificate != public_from_key:
        problems.append("The Secure Boot certificate does not belong to the signing key")
    return problems


# GRUB's EFI file names by target: the program on the ESP and the one on the removable-media path.
GRUB_EFI_FILES = {"x86_64-efi": ("grubx64.efi", "BOOTX64.EFI"), "i386-efi": ("grubia32.efi", "BOOTIA32.EFI")}


def grub_efi_target(plan: object | None) -> str:
    """x86_64-efi, or i386-efi for the PCs with 32-bit UEFI firmware (the plan knows which)."""
    target = _plan_scalar(plan, "grub_target", "x86_64-efi")
    return target if target in GRUB_EFI_FILES else "x86_64-efi"


def efi_arch(plan: object | None) -> str:
    """The suffix of systemd's EFI files (systemd-boot<arch>.efi, linux<arch>.efi.stub): x64, or ia32 on 32-bit UEFI firmware."""
    return "ia32" if grub_efi_target(plan) == "i386-efi" else "x64"


class BootMixin(BootExtrasMixin):
    # ----- package sets ------------------------------------------------------------------------

    def early_boot_atoms(self, manifest: InstallManifest, plan: object | None = None) -> list[str]:
        """Firmware and microcode: the hardware plan decides (it reads options.firmware and options.microcode)."""
        return list(dict.fromkeys(str(atom) for atom in _plan_list(self._plan(plan), "early_atoms")))

    def kernel_stage_atoms(self, manifest: InstallManifest, layout: Sequence[LayoutEntry] = (), plan: object | None = None,
                           stack: ExistingStack | None = None) -> list[str]:
        """What must exist before the kernel and initramfs are built."""
        # The console font is built into the initramfs, which copies every font installed when it is built.
        fonts = ["media-fonts/terminus-font"] if manifest.system.console_font.startswith("ter-") else []
        return list(dict.fromkeys([*self.storage_tool_atoms(manifest, stack), *self.early_boot_atoms(manifest, plan),
                                   *self.filesystem_atoms(manifest, layout), *fonts]))

    def kernel_atoms(self, manifest: InstallManifest) -> list[str]:
        atoms = ["sys-kernel/installkernel"]
        if manifest.system.kernel != "manual":
            atoms.append(f"sys-kernel/{manifest.system.kernel}")
        if manifest.system.initramfs == "dracut":
            atoms.append("sys-kernel/dracut")
        return [*atoms, *self.kernel_extra_atoms(manifest)]

    def kernel_extra_atoms(self, manifest: InstallManifest) -> list[str]:
        """Tools that go with a distribution kernel: the right I/O scheduler per disk, and eclean-kernel when asked for."""
        if manifest.system.kernel not in DIST_KERNELS:
            return []
        return ["sys-block/io-scheduler-udev-rules", *(["app-admin/eclean-kernel"] if manifest.options.eclean_kernel else [])]

    def bootloader_atoms(self, manifest: InstallManifest) -> list[str]:
        system = manifest.system
        systemd_package = self.systemd_package(system)
        if system.bootloader == "none":
            return []  # the user sets the boot loader up
        atoms = [{"grub": "sys-boot/grub", "systemd-boot": systemd_package, "efi-stub": "sys-boot/efibootmgr"}[system.bootloader]]
        if system.bootloader == "grub" and system.dual_boot:
            atoms.append("sys-boot/os-prober")
        if system.uki and systemd_package not in atoms:
            atoms.append(systemd_package)
        if system.secure_boot:
            atoms.append("app-crypt/sbsigntools")
        return atoms

    # ----- initramfs, boot loader -----------------------------------------------------------------

    def configure_initramfs(self, manifest: InstallManifest, root_device: str, luks_device: str | None = None,
                            plan: object | None = None, resume_device: str | None = None, stack: ExistingStack | None = None) -> None:
        """Persist the root discovery arguments before a kernel/initramfs is built.

        ``resume_device`` is the swap partition that hibernation writes to; the kernel looks for the image there.
        ``stack`` is what the layout sits on that already existed (LVM volumes, md arrays): dracut activates nothing
        without rd.lvm.lv or rd.md.uuid, whatever modules it has.
        """
        plan = self._plan(plan)
        root_uuid = "" if self.dry_run else self.runner.run(
            ("blkid", "-s", "UUID", "-o", "value", root_device)).stdout.strip()
        if not self.dry_run and not re.fullmatch(r"[0-9a-fA-F-]{36}", root_uuid):
            raise ConfigurationError("Root filesystem has no valid UUID for stable boot configuration")
        arguments = [f"root={'UUID=' + root_uuid if root_uuid else root_device}", "rw"]
        modules: list[str] = []
        if manifest.storage.luks:
            if not luks_device:
                raise ConfigurationError("LUKS backing device is required for initramfs configuration")
            uuid = "dry-run-luks-uuid" if self.dry_run else self.runner.run(
                ("blkid", "-s", "UUID", "-o", "value", luks_device)).stdout.strip()
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid) and not self.dry_run:
                raise ConfigurationError("LUKS backing device has no valid UUID")
            # Dracut uses rd.luks.uuid to select the backing device. The
            # rd.luks.name mapping only names a device selected that way.
            arguments.extend((f"rd.luks.uuid={uuid}", f"rd.luks.name={uuid}={guided_layout.luks_mapper_name(manifest.storage)}"))
            if _plan_scalar(plan, "luks_allow_discards", False):
                # TRIM through the encryption: the user accepted what that tells an attacker about free space.
                arguments.append(f"rd.luks.options={uuid}=discard" if manifest.system.init == "systemd" else f"rd.luks.allow-discards={uuid}")
            modules.append("crypt")
            if self.luks_prompt_uses_console_keymap(manifest):
                keymap = self.resolve_console_keymap(manifest)
                arguments.append(f"rd.vconsole.keymap={keymap}")
                # Non-systemd initramfs images take the keymap from the command line only; systemd ones read this file.
                self.write("etc/vconsole.conf", f"KEYMAP={keymap}\n")
        if manifest.system.console_font:
            # The font of the early console and the passphrase prompt. dracut sets 8x16 unless told otherwise:
            # a non-systemd initramfs takes the font from the command line, a systemd one from vconsole.conf.
            arguments.append(f"rd.vconsole.font={manifest.system.console_font}")
            self.set_shell_variable("etc/vconsole.conf", "FONT", manifest.system.console_font)
        if manifest.storage.lvm:
            arguments.append("rd.lvm.lv=gentoo/root")
            modules.append("lvm")
        if manifest.storage.raid_level:
            modules.append("mdraid")
        if stack is not None:
            arguments.extend(token for token in [*(f"rd.lvm.lv={lv}" for lv in stack.lvs), *(f"rd.md.uuid={uuid}" for uuid in stack.md_uuids)] if token not in arguments)
            modules.extend(name for name, wanted in (("lvm", stack.lvm), ("mdraid", stack.md)) if wanted and name not in modules)
        if resume_device:
            swap_uuid = "" if self.dry_run else self.runner.run(("blkid", "-s", "UUID", "-o", "value", resume_device)).stdout.strip()
            if not self.dry_run and not re.fullmatch(r"[0-9a-fA-F-]{36}", swap_uuid):
                raise ConfigurationError("The swap partition has no valid UUID, so the system could not resume from hibernation")
            arguments.append(f"resume={'UUID=' + swap_uuid if swap_uuid else resume_device}")
            modules.append("resume")  # included by hostonly="no" anyway; asked for so that it stays that way
        arguments.extend(token for token in _plan_list(plan, "kernel_cmdline") if CMDLINE_TOKEN_RE.fullmatch(str(token)))
        arguments.extend(self.serial_console_tokens(manifest))
        arguments.extend(manifest.system.kernel_cmdline_extra.split())
        cmdline = " ".join(arguments)
        self.write("etc/kernel/cmdline", cmdline + "\n")
        # The installer runs dracut inside a chroot before the target fstab
        # exists. A host-only image would describe the live installer's root.
        dracut_config = 'hostonly="no"\n'
        if modules:
            dracut_config += 'add_dracutmodules+=" ' + " ".join(modules) + ' "\n'
        self.write("etc/dracut.conf.d/gentoo-installer.conf", dracut_config)
        if manifest.system.bootloader == "grub" and len(arguments) > 2:
            self.set_grub_setting("GRUB_CMDLINE_LINUX", " ".join(arguments[2:]))
        self.link_cmdline_for_dracut(manifest)

    def set_grub_setting(self, key: str, value: str) -> None:
        self.set_shell_variable("etc/default/grub", key, value)

    def _kernel_cmdline(self, root_device: str) -> str:
        path = self.path("etc/kernel/cmdline")
        return path.read_text(encoding="utf-8").strip() if not self.dry_run and path.is_file() else f"root={root_device} rw"

    def removable_fallback_wanted(self, manifest: InstallManifest, plan: object | None = None, *, nvram_failed: bool = False) -> bool:
        """Should the removable-media boot path (EFI/BOOT/BOOTX64.EFI) be written?"""
        plan = self._plan(plan)
        choice = str(_plan_scalar(plan, "removable_fallback", "") or getattr(plan, "removable_fallback", "") or manifest.hardware.removable_fallback)
        if choice == "auto":
            # An ESP the installer just created has nothing to protect; a shared one keeps other systems' files.
            choice = "always" if manifest.storage.mode == "erase" or manifest.hardware.target == "portable" else "if-missing"
        if nvram_failed and choice == "never":
            self.notify("warning: the firmware did not accept a boot entry, so the fallback boot path is written regardless of the 'never' setting")
            return True
        if choice == "never":
            return False
        if choice == "if-missing" and not nvram_failed:
            efi = self.efi_mountpoint(manifest).lstrip("/")
            return self.dry_run or not self.path(f"{efi}/EFI/BOOT/{GRUB_EFI_FILES[grub_efi_target(plan)][1]}").exists()
        return True

    def _grub_install_efi(self, manifest: InstallManifest, plan: object | None, efi_mountpoint: str) -> bool:
        """Install GRUB for UEFI with a firmware entry and the removable fallback; True when NVRAM was written."""
        base = ("grub-install", f"--target={grub_efi_target(plan)}", f"--efi-directory={efi_mountpoint}")
        nvram = True
        try:
            self.chroot((*base, "--bootloader-id=Gentoo"))
        except RunnerError as exc:
            if not NVRAM_FAILURE.search(exc.output or str(exc)):
                raise
            self.notify("warning: the firmware refused the Gentoo boot entry; installing GRUB without it (the fallback path boots instead)")
            self.chroot((*base, "--bootloader-id=Gentoo", "--no-nvram"))
            nvram = False
        if self.removable_fallback_wanted(manifest, plan, nvram_failed=not nvram):
            self.chroot((*base, "--removable", "--no-nvram"))
        else:
            self.notify("The removable-media fallback boot path was not written (kept existing files)")
        return nvram

    def configure_bootloader(self, manifest: InstallManifest, root_device: str, efi_device: str | None = None,
                             plan: object | None = None) -> None:
        plan = self._plan(plan)
        system, storage = manifest.system, manifest.storage
        efi_mountpoint = self.efi_mountpoint(manifest)
        efi_prefix = efi_mountpoint.lstrip("/")
        cmdline = self._kernel_cmdline(root_device)
        if system.bootloader == "none":
            self.notify("No boot loader was installed, as you asked. The kernel and initramfs are under /boot and the kernel options "
                        "are in /etc/kernel/cmdline: set up your own boot loader before you restart.")
            return
        if system.secure_boot and system.secure_boot_mode == "shim":
            raise ConfigurationError(SHIM_NOT_YET)
        if system.secure_boot and storage.firmware != "uefi":
            raise ConfigurationError("Secure Boot requires UEFI firmware")
        if system.secure_boot and system.bootloader == "grub":
            raise ConfigurationError("Secure Boot is not supported with GRUB yet; choose systemd-boot or the EFI stub with a signed UKI")
        if system.secure_boot and system.bootloader != "grub" and not system.uki:
            raise ConfigurationError("Secure Boot with systemd-boot or EFI stub requires a signed UKI")
        if system.uki and storage.firmware != "uefi":
            raise ConfigurationError("A Unified Kernel Image requires UEFI firmware")
        if system.secure_boot and not self.dry_run:
            problems = self.secure_boot_problems(manifest)
            if problems:
                raise ConfigurationError("; ".join(problems))
        if system.secure_boot:
            self.prepare_secure_boot(manifest)  # the key stays on the installed system, so kernel updates are signed too
        safe = self.write_safe_graphics_files(manifest, cmdline, plan)
        safe_cmdline = " ".join(safe_tokens(cmdline))
        if system.uki:
            self._build_uki(manifest, root_device)
            if safe:
                self._build_uki(manifest, root_device, SAFE_GRAPHICS_UKI, safe_cmdline)
        elif system.bootloader != "grub":
            self._copy_boot_files(manifest)
        fallback_written = False
        if system.bootloader == "grub":
            if storage.firmware == "uefi":
                self._grub_install_efi(manifest, plan, efi_mountpoint)
            else:
                self.chroot(("grub-install", "--target=i386-pc", storage.device))
            if system.uki:
                if not efi_device:
                    raise ConfigurationError("GRUB with UKI requires an EFI system partition")
                efi_uuid = "dry-run-efi-uuid" if self.dry_run else self.runner.run(
                    ("blkid", "-s", "UUID", "-o", "value", efi_device)).stdout.strip()
                if not self.dry_run and not re.fullmatch(r"[0-9a-fA-F-]+", efi_uuid):
                    raise ConfigurationError("EFI system partition has no valid UUID")
                entries = self._grub_chain_entry("Gentoo Linux", efi_uuid, "gentoo.efi")
                if safe:
                    entries += self._grub_chain_entry(SAFE_GRAPHICS_TITLE, efi_uuid, SAFE_GRAPHICS_UKI)
                self.write("etc/grub.d/40_custom", '#!/bin/sh\nexec tail -n +3 $0\n\n' + entries, 0o755)
            if system.dual_boot:
                # GRUB ships with os-prober disabled; other systems only appear in the menu when it is enabled.
                self.set_grub_setting("GRUB_DISABLE_OS_PROBER", "false")
            self.chroot(("grub-mkconfig", "-o", "/boot/grub/grub.cfg"))
        elif system.bootloader == "systemd-boot":
            try:
                self.chroot(("bootctl", f"--path={efi_mountpoint}", "install"))
            except RunnerError as exc:
                if not NVRAM_FAILURE.search(exc.output or str(exc)):
                    raise
                self.notify("warning: the firmware refused the boot entry; systemd-boot is installed on the fallback path only")
                self.chroot(("bootctl", f"--path={efi_mountpoint}", "--no-variables", "install"))
            initrd = "initrd /initramfs\n" if system.initramfs != "none" and not system.uki else ""
            self.write(f"{efi_prefix}/loader/loader.conf", "default gentoo.conf\ntimeout 3\n")
            boot_entries = [("gentoo", "Gentoo Linux", "gentoo.efi", cmdline)]
            if safe:
                boot_entries.append(("gentoo-safe", SAFE_GRAPHICS_TITLE, SAFE_GRAPHICS_UKI, safe_cmdline))
            for name, title, uki_name, options in boot_entries:
                linux = f"/EFI/Linux/{uki_name}" if system.uki else "/vmlinuz"
                self.write(f"{efi_prefix}/loader/entries/{name}.conf",
                           f"title {title}\nlinux {linux}\n{initrd}" + ("" if system.uki else f"options {options}\n"))
        else:
            if not root_device.startswith("/dev/"):
                raise ConfigurationError("EFI stub requires a stable root device")
            if system.uki and self.removable_fallback_wanted(manifest, plan):
                fallback_written = self._copy_uki_fallback(efi_prefix)
            elif not system.uki:
                self.notify("The EFI stub without a Unified Kernel Image has no fallback boot path; enable UKI to get one")
            disk, part = storage.device, "1"
            if efi_device and not self.dry_run:
                part = self.runner.run(("lsblk", "--noheadings", "--output", "PARTN", efi_device)).stdout.strip()
                parent = self.runner.run(("lsblk", "--noheadings", "--output", "PKNAME", efi_device)).stdout.strip()
                if not part.isdigit() or not re.fullmatch(r"[A-Za-z0-9._+-]+", parent):
                    raise ConfigurationError("Could not determine the EFI partition number and parent disk")
                disk = "/dev/" + parent
            # efibootmgr puts each new entry first in the boot order: the safe one is made first so that "Gentoo" stays the default.
            if safe:
                try:
                    self._efi_stub_entry("Gentoo (safe graphics)", SAFE_GRAPHICS_UKI, safe_cmdline, disk, part, manifest)
                except RunnerError:
                    self.notify("warning: the firmware refused the safe-graphics boot entry")
            try:
                self._efi_stub_entry("Gentoo", "gentoo.efi", cmdline, disk, part, manifest)
            except RunnerError:
                if not fallback_written:
                    raise
                self.notify("warning: the firmware refused the boot entry; the system boots from the fallback path")
        self.install_esp_sync_hook(manifest, fallback_copy=fallback_written, safe=safe)
        if system.secure_boot:
            self._sign_boot_images(manifest, efi_mountpoint, sign_fallback_copy=fallback_written, safe=safe)

    @staticmethod
    def _grub_chain_entry(title: str, efi_uuid: str, image: str) -> str:
        return (f'menuentry "{title}" {{\n'
                f'    search --no-floppy --fs-uuid --set=root {efi_uuid}\n'
                f'    chainloader /EFI/Linux/{image}\n}}\n')

    def _efi_stub_entry(self, label: str, uki_name: str, options: str, disk: str, part: str, manifest: InstallManifest) -> None:
        system = manifest.system
        loader = f"\\EFI\\Linux\\{uki_name}" if system.uki else "\\vmlinuz"
        command = ["efibootmgr", "--create", "--disk", disk, "--part", part, "--label", label, "--loader", loader]
        if not system.uki:
            initrd = " initrd=\\initramfs" if system.initramfs != "none" else ""
            command.extend(("--unicode", f"{options}{initrd}"))
        self.chroot(tuple(command))

    def install_esp_sync_hook(self, manifest: InstallManifest, fallback_copy: bool = False, safe: bool = False) -> None:
        """Make kernel updates reach the EFI system partition (systemd-boot, EFI stub, UKI), signed again when Secure Boot is on."""
        system = manifest.system
        if system.bootloader == "grub" and not system.uki:
            return
        script = render_esp_sync_hook(self.efi_mountpoint(manifest), system.uki, fallback_copy, safe=safe and system.uki,
                                      sign=system.secure_boot, sign_loader=system.secure_boot and system.bootloader == "systemd-boot")
        for hook in ESP_SYNC_HOOKS:
            self.write(hook, script, 0o755)

    def _copy_uki_fallback(self, efi_prefix: str) -> bool:
        if self.dry_run:
            return True
        source = self.path(f"{efi_prefix}/EFI/Linux/gentoo.efi")
        if not source.is_file():
            return False
        destination = self.path(f"{efi_prefix}/EFI/BOOT")
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination / "BOOTX64.EFI")
        return True

    # ----- Secure Boot ---------------------------------------------------------------------------------

    def secure_boot_problems(self, manifest: InstallManifest) -> list[str]:
        """Problems with the signing key and certificate on the live system (readable, matching pair)."""
        return check_secure_boot_material(self.runner, manifest.system)

    def cleanup_secrets(self) -> None:
        """Remove signing material an older installer left on the live system; plain filesystem calls so a cancelled install still cleans up."""
        for name in ("signing.key", "signing.crt"):
            try:
                os.unlink(self.secret_dir / name)
            except OSError:
                pass
        try:
            os.rmdir(self.secret_dir)
        except OSError:
            pass

    def _sign_boot_images(self, manifest: InstallManifest, efi_mountpoint: str, *, sign_fallback_copy: bool = False, safe: bool = False) -> None:
        """Sign what the firmware starts, with the key that stays in /etc/secureboot of the installed system."""
        system = manifest.system
        images: list[str] = []
        if system.uki:
            images.append(f"{efi_mountpoint}/EFI/Linux/gentoo.efi")
            if safe:
                images.append(f"{efi_mountpoint}/EFI/Linux/{SAFE_GRAPHICS_UKI}")
        if system.bootloader == "systemd-boot":
            images.extend((f"{efi_mountpoint}/EFI/systemd/systemd-bootx64.efi", f"{efi_mountpoint}/EFI/BOOT/BOOTX64.EFI"))
        elif sign_fallback_copy:  # the copy of the UKI that was put on the removable-media path
            images.append(f"{efi_mountpoint}/EFI/BOOT/BOOTX64.EFI")
        for image in dict.fromkeys(images):
            signed = image + ".signed"
            self.chroot(("sbsign", "--key", SECURE_BOOT_KEY, "--cert", SECURE_BOOT_CERT, "--output", signed, image))
            self.chroot(("sbverify", "--cert", SECURE_BOOT_CERT, signed))
            self.chroot(("mv", signed, image))
        self._publish_certificate(manifest, efi_mountpoint)

    def _publish_certificate(self, manifest: InstallManifest, efi_mountpoint: str) -> None:
        """Put the certificate (DER) on the ESP so it can be enrolled from the firmware's setup menu."""
        destination = f"{efi_mountpoint}/{ENROLL_FILE}"
        if not self.dry_run:
            self.path(destination.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        self.chroot(("openssl", "x509", "-in", SECURE_BOOT_CERT, "-outform", "DER", "-out", destination))
        for line in secure_boot_enroll_steps(manifest):
            self.notify(line)

    # ----- boot images -----------------------------------------------------------------------------

    def _boot_images(self, manifest: InstallManifest) -> tuple[Path, Path | None]:
        boot = self.path("boot")
        # installkernel names the file vmlinuz-<version>; systemd's kernel-install (compat layout) kernel-<version>.
        kernels = sorted([*boot.glob("vmlinuz-*"), *boot.glob("kernel-*")], key=lambda path: path.stat().st_mtime, reverse=True)
        if not kernels:
            raise ConfigurationError("No installed kernel was found under /boot")
        initramfs = None
        if manifest.system.initramfs != "none":
            images = sorted([*boot.glob("initramfs-*.img"), *boot.glob("initrd-*")], key=lambda path: path.stat().st_mtime, reverse=True)
            if not images:
                raise ConfigurationError("No installed initramfs was found under /boot")
            initramfs = images[0]
        return kernels[0], initramfs

    def _build_uki(self, manifest: InstallManifest, root_device: str, name: str = "gentoo.efi", cmdline: str | None = None) -> None:
        efi_mountpoint = self.efi_mountpoint(manifest)
        efi_prefix = efi_mountpoint.lstrip("/")
        if self.dry_run:
            kernel, initramfs = "/boot/vmlinuz", "/boot/initramfs" if manifest.system.initramfs != "none" else None
        else:
            kernel_path, initramfs_path = self._boot_images(manifest)
            kernel, initramfs = "/boot/" + kernel_path.name, "/boot/" + initramfs_path.name if initramfs_path else None
            self.path(f"{efi_prefix}/EFI/Linux").mkdir(parents=True, exist_ok=True)
        command = ["ukify", "build", "--linux", kernel, "--cmdline", cmdline if cmdline is not None else self._kernel_cmdline(root_device),
                   "--output", f"{efi_mountpoint}/EFI/Linux/{name}"]
        if initramfs:
            command.extend(("--initrd", initramfs))
        self.chroot(tuple(command))

    def _copy_boot_files(self, manifest: InstallManifest) -> None:
        if self.dry_run:
            return
        kernel, initramfs = self._boot_images(manifest)
        efi = self.path(self.efi_mountpoint(manifest).lstrip("/"))
        efi.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(kernel, efi / "vmlinuz")
        if initramfs:
            shutil.copyfile(initramfs, efi / "initramfs")

    def verify_boot_setup(self, manifest: InstallManifest, root_device: str, *, root_uuid: str = "", plan: object | None = None,
                          stack: ExistingStack | None = None) -> list[str]:
        """Check the finished installation; returns a list of problems (empty when it looks bootable)."""
        plan = self._plan(plan)
        if self.dry_run:
            return []
        system, storage = manifest.system, manifest.storage
        problems: list[str] = []
        boot = self.path("boot")
        kernels = [p for pattern in ("vmlinuz-*", "kernel-*") for p in boot.glob(pattern) if p.is_file() and p.stat().st_size]
        initramfs = [p for pattern in ("initramfs*", "initrd*") for p in boot.glob(pattern) if p.is_file() and p.stat().st_size]
        if not kernels:
            problems.append("No kernel image was installed under /boot")
        if system.initramfs != "none" and not initramfs:
            problems.append("No initramfs was installed under /boot")
        fstab = self.read_text("etc/fstab")
        if root_uuid and f"UUID={root_uuid}" not in fstab:
            problems.append("/etc/fstab does not mention the root filesystem")
        elif not root_uuid and root_device not in fstab:
            problems.append("/etc/fstab does not mention the root filesystem")
        efi = self.efi_mountpoint(manifest).lstrip("/")
        if system.bootloader == "none":
            return problems  # the user installs the boot loader
        if system.bootloader == "grub":
            config = self.read_text("boot/grub/grub.cfg")
            if not re.search(r"^\s*linux(?:efi)?\s+\S+", config, re.MULTILINE):
                problems.append("grub.cfg has no menu entry that loads a kernel")
            else:
                for wanted, present in (("rd.luks.uuid", storage.luks), ("rd.lvm.lv", storage.lvm or (stack is not None and stack.early_lvm)),
                                        ("rd.md.uuid", stack is not None and stack.early_md)):
                    if present and wanted not in config:
                        problems.append(f"grub.cfg does not pass {wanted}, so the root filesystem would not be found")
            if storage.firmware == "uefi":
                installed, removable = GRUB_EFI_FILES[grub_efi_target(plan)]
                if not self.path(f"{efi}/EFI/Gentoo/{installed}").is_file() and not self.path(f"{efi}/EFI/BOOT/{removable}").is_file():
                    problems.append("No GRUB EFI image was installed on the EFI system partition")
        elif system.bootloader == "systemd-boot":
            if not self.path(f"{efi}/loader/entries/gentoo.conf").is_file():
                problems.append("The systemd-boot entry was not written")
        if system.uki and not self.path(f"{efi}/EFI/Linux/gentoo.efi").is_file():
            problems.append("The Unified Kernel Image is missing from the EFI system partition")
        return problems

    def initramfs_modules(self) -> set[str] | None:
        """dracut modules inside the newest initramfs; None when they cannot be listed."""
        if self.dry_run:
            return None
        images = sorted(self.path("boot").glob("initramfs-*.img"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not images:
            return None
        try:
            output = self.chroot(("lsinitrd", "-m", "/boot/" + images[0].name), capture=True).stdout
        except RunnerError:
            return None
        modules = {line.strip() for line in output.splitlines() if re.fullmatch(r"\s*[a-z0-9][a-z0-9_-]*", line)}
        return modules or None
