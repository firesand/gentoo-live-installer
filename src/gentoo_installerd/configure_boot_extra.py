"""BOOT: what the boot code adds around the kernel and the bootloader.

* the "safe graphics" boot entry that starts without the NVIDIA drivers (hardware item B05);
* the Portage and kernel files of a distribution kernel (dist-kernel USE, the .config snippet, README, serial console);
* Secure Boot keys that stay on the installed system, so that every later kernel update is signed again (BOOT-07).
"""

from __future__ import annotations

from pathlib import Path

from gentoo_installer.manifest import InstallManifest, System

from .configure_base import ConfigurationError, ConfigurerBase, _plan_scalar

DIST_KERNELS = ("gentoo-kernel-bin", "gentoo-kernel")

# --- safe graphics -----------------------------------------------------------------------------------------

SAFE_GRAPHICS_TOKENS = ("nomodeset", "module_blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm,nouveau")
SAFE_GRAPHICS_TITLE = "Gentoo Linux (safe graphics)"
SAFE_GRAPHICS_GRUB_TITLE = "Gentoo Linux, safe graphics (choose this if the screen stays black)"
SAFE_GRAPHICS_UKI = "gentoo-safe.efi"
SAFE_GRAPHICS_GRUB_SCRIPT = "etc/grub.d/42_gentoo_safe_graphics"
SAFE_GRAPHICS_CMDLINE_FILE = "etc/kernel/cmdline-safe"
KERNEL_README = "etc/kernel/README"
KERNEL_CONFIG_SNIPPET = "etc/kernel/config.d/00-installer.config"
BOOT_PACKAGE_USE = "etc/portage/package.use/installer-boot"
KERNEL_KEYWORDS = "etc/portage/package.accept_keywords/installer-kernel"
# The newest distribution kernel is in the testing branch; these three packages are all it takes (the virtual is what
# out-of-tree drivers such as NVIDIA rebuild against).
NEWEST_KERNEL_KEYWORDS = ("sys-kernel/gentoo-kernel-bin", "sys-kernel/gentoo-kernel", "virtual/dist-kernel")

# --- Secure Boot -------------------------------------------------------------------------------------------

SECURE_BOOT_DIR = "/etc/secureboot"
SECURE_BOOT_KEY = f"{SECURE_BOOT_DIR}/gentoo.key"
SECURE_BOOT_CERT = f"{SECURE_BOOT_DIR}/gentoo.crt"
ENROLL_FILE = "EFI/enroll/gentoo-secureboot.cer"
SHIM_NOT_YET = "Secure Boot through shim and MOK is not available yet. Choose “Create a signing key for me” or “Use my own key”, or turn Secure Boot off."


def snippet_applies(system: System) -> bool:
    """The kernel .config lines go only to the Gentoo kernel that is built on the PC."""
    return system.kernel == "gentoo-kernel" and bool(system.kernel_config_snippet.strip())


def safe_tokens(cmdline: str) -> list[str]:
    """The kernel arguments of the safe entry: the normal ones without the NVIDIA mode-setting switches, plus the safe tokens."""
    kept = [token for token in cmdline.split() if not token.startswith(("nvidia-drm.", "nvidia_drm."))
            and token not in SAFE_GRAPHICS_TOKENS]
    return [*kept, *SAFE_GRAPHICS_TOKENS]


def secure_boot_enroll_steps(manifest: InstallManifest) -> list[str]:
    """What to do once after the first restart, for the finish page and /etc/secureboot/ENROLL.txt."""
    efi = ConfigurerBase.efi_mountpoint(manifest)
    return [
        "Secure Boot: enroll your signing certificate once, then the PC starts only boot files signed with your key.",
        f"1. The certificate is on the EFI partition: {efi}/{ENROLL_FILE}.",
        "2. Restart into the firmware settings and open the Secure Boot page (often called Key Management).",
        "3. Put Secure Boot into Setup Mode (or choose Custom keys), then add that file to the allowed signatures (db).",
        "4. Turn Secure Boot on and save. Kernel updates are signed automatically with the key in /etc/secureboot (root only).",
    ]


class BootExtrasMixin(ConfigurerBase):
    # ----- safe graphics -------------------------------------------------------------------------------------

    def safe_graphics_wanted(self, manifest: InstallManifest, plan: object | None = None) -> bool:
        """The entry exists when the proprietary NVIDIA driver is installed and the user did not turn the entry off."""
        return bool(manifest.hardware.safe_graphics_entry) and _plan_scalar(self._plan(plan), "gpu_driver", "") == "nvidia" \
            and manifest.system.bootloader != "none"

    def write_safe_graphics_files(self, manifest: InstallManifest, cmdline: str, plan: object | None = None) -> bool:
        """The safe command line for the ESP hook and, with GRUB, the menu script. False when no entry is wanted."""
        if not self.safe_graphics_wanted(manifest, plan):
            return False
        safe = " ".join(safe_tokens(cmdline))
        self.write(SAFE_GRAPHICS_CMDLINE_FILE, safe + "\n")
        system = manifest.system
        if system.bootloader == "grub" and not system.uki:
            arguments = " ".join(token for token in safe_tokens(cmdline) if not token.startswith("root=") and token != "rw")
            self.write(SAFE_GRAPHICS_GRUB_SCRIPT, _GRUB_SAFE_SCRIPT.replace("@TITLE@", SAFE_GRAPHICS_GRUB_TITLE).replace("@ARGS@", arguments), 0o755)
        return True

    # ----- distribution kernel files ------------------------------------------------------------------------------

    def write_boot_portage_files(self, manifest: InstallManifest, plan: object | None = None) -> None:
        """Before the kernel is merged: dist-kernel USE, the kernel .config lines, Secure Boot signing, the README."""
        system = manifest.system
        if system.kernel not in DIST_KERNELS:
            return  # a manual or custom kernel gets none of these files
        use_lines = ["# Written by the Gentoo live installer.",
                     "# dist-kernel makes Portage rebuild out-of-tree drivers (NVIDIA, VirtualBox...) whenever the kernel changes.",
                     "*/* dist-kernel"]
        if system.secure_boot:
            use_lines.append(f"sys-kernel/{system.kernel} secureboot" + (" modules-sign" if system.kernel == "gentoo-kernel" else ""))
        self.write(BOOT_PACKAGE_USE, "\n".join(use_lines) + "\n")
        if system.kernel_series == "newest":
            self.write(KERNEL_KEYWORDS, "\n".join([
                "# Written by the Gentoo live installer: the newest Gentoo kernel, from the testing branch. A normal update",
                "# (emerge --update --deep --newuse @world) brings each new kernel series as soon as Gentoo ships it.",
                "# For the long-term kernel that is stable instead, delete this file and run the same update.",
                *(f"{atom} ~amd64" for atom in NEWEST_KERNEL_KEYWORDS)]) + "\n")
        if snippet_applies(system):
            self.write(KERNEL_CONFIG_SNIPPET, "# Written by the Gentoo live installer; merged into Gentoo's standard kernel configuration.\n"
                       + system.kernel_config_snippet.strip("\n") + "\n")
        if system.secure_boot:
            self.set_shell_variable("etc/portage/make.conf", "SECUREBOOT_SIGN_KEY", SECURE_BOOT_KEY)
            self.set_shell_variable("etc/portage/make.conf", "SECUREBOOT_SIGN_CERT", SECURE_BOOT_CERT)
            if system.kernel == "gentoo-kernel":
                self.set_shell_variable("etc/portage/make.conf", "MODULES_SIGN_KEY", SECURE_BOOT_KEY)
                self.set_shell_variable("etc/portage/make.conf", "MODULES_SIGN_CERT", SECURE_BOOT_CERT)
                self.set_shell_variable("etc/portage/make.conf", "MODULES_SIGN_HASH", "sha512")
            self.write("etc/kernel/uki.conf", f"[UKI]\nSecureBootPrivateKey={SECURE_BOOT_KEY}\nSecureBootCertificate={SECURE_BOOT_CERT}\n")
        self.write(KERNEL_README, self.kernel_readme(manifest))

    @staticmethod
    def kernel_readme(manifest: InstallManifest) -> str:
        system = manifest.system
        lines = ["Written by the Gentoo live installer.", "",
                 f"Kernel: sys-kernel/{system.kernel}, updated with the rest of the system (emerge --update @world).",
                 (f"Series: the newest Gentoo kernel (the testing keyword in /{KERNEL_KEYWORDS}); each new series arrives with an update."
                  if system.kernel_series == "newest" else "Series: the long-term kernel that is stable in Gentoo."),
                 "Startup options: /etc/kernel/cmdline (and GRUB_CMDLINE_LINUX in /etc/default/grub with GRUB)."]
        if system.bootloader != "grub" or system.uki:
            lines += ["", "Boot files: systemd-boot, the EFI stub and Unified Kernel Images load the kernel from the EFI system partition,",
                      "which installkernel does not touch. The installer added two hooks that copy or rebuild the boot files after every",
                      "kernel installation: /etc/kernel/postinst.d/95-gentoo-installer-esp and /etc/kernel/install.d/95-gentoo-installer-esp.install."]
        if system.secure_boot:
            lines += ["", f"Secure Boot: the hook signs every new boot image with the key in {SECURE_BOOT_DIR} (root only)."]
        if system.kernel == "gentoo-kernel" and system.kernel_config_snippet.strip():
            lines += ["", "Your own kernel configuration lines are in /etc/kernel/config.d/00-installer.config."]
        lines += ["", "Old kernels: eclean-kernel (app-admin/eclean-kernel) removes the ones you no longer need."]
        return "\n".join(lines) + "\n"

    def link_cmdline_for_dracut(self, manifest: InstallManifest) -> None:
        """systemd-boot: dracut and ukify read the same line (/etc/cmdline.d/00-installkernel.conf links to /etc/kernel/cmdline)."""
        if manifest.system.bootloader != "systemd-boot" or self.dry_run:
            return
        directory = self.path("etc/cmdline.d")
        self._mkdirs(directory)
        link = directory / "00-installkernel.conf"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to("../kernel/cmdline")

    # ----- serial console -----------------------------------------------------------------------------------------

    def serial_console_tokens(self, manifest: InstallManifest) -> list[str]:
        """console= arguments: the screen stays a console and the serial port is the last, so it receives the boot messages."""
        return ["console=tty0", f"console={manifest.system.serial_console}"] if manifest.system.serial_console else []

    def configure_serial_getty(self, manifest: InstallManifest) -> None:
        """A login prompt on the serial port."""
        console = manifest.system.serial_console
        if not console:
            return
        port, _, rest = console.partition(",")
        speed = "".join(ch for ch in rest if ch.isdigit()) or "115200"
        if manifest.system.init == "systemd":
            self.chroot(("systemctl", "enable", f"serial-getty@{port}.service"))
            return
        line = f"s0:12345:respawn:/sbin/agetty -L {speed} {port} vt100"
        old = "" if self.dry_run else self.read_text("etc/inittab")
        kept = [entry for entry in old.splitlines() if not entry.lstrip("# ").startswith("s0:")]
        self.write("etc/inittab", "\n".join([*kept, line]).strip("\n") + "\n")

    # ----- Secure Boot keys that stay on the installed system ------------------------------------------------------

    def prepare_secure_boot(self, manifest: InstallManifest) -> None:
        """Put the signing key (made here, or the user's own) into /etc/secureboot of the target: root only, 0600.

        The hook that runs after each kernel installation signs with it, so updates keep booting. Safe to call twice."""
        system = manifest.system
        if not system.secure_boot:
            return
        if system.secure_boot_mode == "shim":
            raise ConfigurationError(SHIM_NOT_YET)
        directory = self.path(SECURE_BOOT_DIR.lstrip("/"))
        if not self.dry_run:
            self._mkdirs(directory.parent)
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        if system.secure_boot_mode == "create":
            if not self.dry_run and self.path(SECURE_BOOT_KEY.lstrip("/")).exists():
                return
            self.chroot(("openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-days", "3650", "-nodes",
                         "-subj", "/CN=Gentoo Secure Boot key/", "-keyout", SECURE_BOOT_KEY, "-out", SECURE_BOOT_CERT))
        else:
            key, certificate = Path(system.secure_boot_key), Path(system.secure_boot_certificate)
            if not self.dry_run:
                self.write_bytes(SECURE_BOOT_KEY.lstrip("/"), key.read_bytes(), 0o600)
                self.write_bytes(SECURE_BOOT_CERT.lstrip("/"), certificate.read_bytes(), 0o644)
        self.chroot(("chmod", "0600", SECURE_BOOT_KEY))
        self.chroot(("chmod", "0644", SECURE_BOOT_CERT))
        self.write(f"{SECURE_BOOT_DIR.lstrip('/')}/ENROLL.txt", "\n".join(secure_boot_enroll_steps(manifest)) + "\n")


_GRUB_SAFE_SCRIPT = r"""#!/bin/sh
# Written by the Gentoo live installer.
# A second menu entry that starts the same kernels without the NVIDIA drivers and mode setting.
# Use it when the screen stays black after the normal entry. grub-mkconfig runs this after 10_linux.
entries=$(GRUB_CMDLINE_LINUX='@ARGS@' GRUB_CMDLINE_LINUX_DEFAULT='' GRUB_DISABLE_RECOVERY=true GRUB_DISABLE_SUBMENU=y /etc/grub.d/10_linux)
[ -n "$entries" ] || exit 0
cat <<EOF
submenu '@TITLE@' {
$entries
}
EOF
"""

