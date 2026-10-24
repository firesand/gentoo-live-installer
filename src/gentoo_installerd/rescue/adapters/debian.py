"""Debian, Ubuntu and derivatives: initramfs-tools, update-grub, apt (review R14: the active loader decides which actions exist)."""

from __future__ import annotations

from typing import ClassVar

from ..builder import PlanBuilder
from ..errors import RescueError, Unsupported
from ..view import SessionView
from . import base
from .base import Adapter

IDS = {"debian": "debian", "ubuntu": "ubuntu", "linuxmint": "ubuntu", "kali": "kali", "pop": "Pop_OS", "raspbian": "debian", "devuan": "devuan", "zorin": "ubuntu", "elementary": "ubuntu"}
APT_OPTIONS = ("-o", "Dpkg::Options::=--force-confold", "-o", "Dpkg::Options::=--force-confdef")
ENV = {"DEBIAN_FRONTEND": "noninteractive", "DEBCONF_NONINTERACTIVE_SEEN": "true"}
POLICY_RC = "/usr/sbin/policy-rc.d"


class DebianAdapter(Adapter):
    family: ClassVar[str] = "debian"
    label: ClassVar[str] = "Debian"
    esp_default: ClassVar[str] = "/boot/efi"
    tools: ClassVar[frozenset[str]] = frozenset({"grub-install", "update-grub", "grub-mkconfig", "update-initramfs", "apt-get", "dpkg", "bootctl", "kernelstub", "chpasswd", "usermod", "passwd",
                                                  "chage", "faillock", "true", "journalctl", "dpkg-query"})

    def ident(self, view: SessionView) -> str:
        return IDS.get(view.os.flavor, "debian" if view.os.id not in IDS else IDS[view.os.id])

    def active_loader(self, view: SessionView) -> str:
        return view.boot().loader or ("grub" if view.which("grub-install") else "")

    def _require_grub(self, view: SessionView) -> None:
        loader = self.active_loader(view)
        if loader and loader != "grub":
            raise Unsupported(f"The active boot loader of this system is {loader}, not GRUB, so GRUB actions are not offered. "
                              + ("Use 'Reinstall the boot loader' to repair systemd-boot, or 'Rebuild the initramfs'." if loader == "systemd-boot" else ""))

    def _policy_rc(self, b: PlanBuilder) -> None:
        """Keep package scripts from starting services inside the chroot (removed again afterwards)."""
        if not b.view.exists(POLICY_RC):
            b.file({"op": "write", "path": POLICY_RC, "content": "#!/bin/sh\nexit 101\n", "mode": 0o755, "backup": False, "create": True}, f"create {POLICY_RC} (temporary, removed afterwards)",
                   writes=(POLICY_RC,), group="policy")
            b.file({"op": "remove", "path": POLICY_RC}, f"remove {POLICY_RC}", cleanup=True, group="policy")

    # ---- actions -------------------------------------------------------------------------------------------------------------------------
    def initramfs(self, b: PlanBuilder) -> None:
        view = b.view
        b.title, b.risk = "Rebuild the initramfs", "medium"
        b.summary = "Creates a new initramfs for the selected kernels so the root file system can be found and mounted."
        if not view.which("update-initramfs"):
            raise Unsupported("initramfs-tools is not installed in this system (update-initramfs is missing). Reinstall it with 'apt-get install --reinstall initramfs-tools' from the terminal.")
        kernels = [k for k in view.kernels() if k.image]
        selected = b.versions("kernels", [k.version for k in kernels])
        generic = bool(b.params.get("generic"))
        b.precondition(bool(kernels), "A kernel was found" if kernels else "No kernel image was found in /boot")
        free = view.free_bytes("/boot")
        b.precondition(free == 0 or free >= 100 << 20, "/boot has enough free space" if free >= 100 << 20 or free == 0 else f"/boot has only {free // (1 << 20)} MiB free")
        if generic and "MODULES=dep" in view.read("/etc/initramfs-tools/initramfs.conf", 65536):
            path = "/etc/initramfs-tools/conf.d/zz-rescue-generic"
            b.file({"op": "write", "path": path, "content": "MODULES=most\n", "mode": 0o644, "backup": False, "create": True, "parents": True}, f"write {path} (temporary: include drivers for many kinds of hardware)",
                   writes=(path,), group="generic")
            b.file({"op": "remove", "path": path}, f"remove {path}", cleanup=True, group="generic")
        if selected:
            for version in selected:
                mode = "-u" if any(k.version == version and k.initramfs for k in kernels) else "-c"
                b.chroot(["update-initramfs", mode, "-k", version], env=ENV, writes=(f"/boot/initrd.img-{version}",), timeout=900)
        else:
            mode = "-u" if any(k.initramfs for k in kernels) else "-c"
            b.chroot(["update-initramfs", mode, "-k", "all"], env=ENV, writes=("/boot/initrd.img-*",), timeout=1800)
        for kernel in [k for k in kernels if not selected or k.version in selected]:
            b.check("file_min_size", path=f"/boot/initrd.img-{kernel.version}", bytes=1 << 20)

    def config(self, b: PlanBuilder) -> None:
        view = b.view
        self._require_grub(view)
        b.title, b.risk = "Regenerate the boot menu", "medium"
        b.summary = "Rebuilds grub.cfg from the kernels found in /boot."
        path = view.boot().grub_cfg or "/boot/grub/grub.cfg"
        base.clean_cmdline_sources(b)
        if view.which("update-grub"):
            b.chroot(["update-grub"], env=ENV, writes=(path,), timeout=600)
        elif view.which("grub-mkconfig"):
            b.chroot(["grub-mkconfig", "-o", path], writes=(path,), timeout=600)
        else:
            raise Unsupported("GRUB is not installed in this system. Reinstall it with 'apt-get install --reinstall grub-efi-amd64' (or grub-pc) from the terminal.")
        base.grub_config_checks(b, path)

    def bootloader(self, b: PlanBuilder) -> None:
        view = b.view
        loader = self.active_loader(view)
        wanted = str(b.params.get("loader", "")) or loader or "grub"
        if wanted == "systemd-boot":
            return self._systemd_boot(b)
        self._require_grub(view)
        b.title, b.risk = "Reinstall the boot loader", "high"
        b.needs["uefi_nvram"] = base.nvram_ok(b.env)
        firmware = base.firmware_choice(b)
        b.summary = "Writes GRUB " + ("and the Secure Boot loader chain " if firmware == "uefi" else "") + "and regenerates its menu."
        method = b.choice("method", {"grub-install", "packages"}, "grub-install")
        if firmware == "uefi":
            esp = base.esp_for(b)
            if esp is None:
                raise Unsupported("The EFI system partition is not mounted. Fix fstab (or choose it in the mount plan) and try again.")
            ident = base.bootloader_id(b, esp, self.ident(view), (self.ident(view).lower(), "ubuntu", "debian"))
            shim = view.exists("/usr/lib/shim/shimx64.efi.signed") or view.exists("/usr/lib/shim/shimx64.efi.signed.latest")
            signed = view.exists("/usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed")
            secure = shim and signed and view.grub_binary_mentions("grub-install", b"uefi-secure-boot")
            if shim and signed and not secure:
                b.warn("This system uses the Secure Boot loader chain, but its grub-install does not know --uefi-secure-boot; the chain may not be restored. Reinstall with method 'packages'.")
            removable = base.removable_wanted(b, esp)
            base.backup_esp(b, esp)
            if method == "packages":
                base.need_network(b, "download the boot loader packages")
                self._policy_rc(b)
                b.chroot(["apt-get", "install", "--reinstall", "-y", *APT_OPTIONS, "grub-efi-amd64", "grub-efi-amd64-signed", "shim-signed"], env=ENV, writes=(f"{esp.mountpoint}/EFI/{ident}/*",), timeout=1800)
            else:
                base.grub_uefi_steps(b, "grub-install", esp, ident, secure_boot=secure, removable=removable)
            base.esp_loader_checks(b, esp, ident, removable and method != "packages", ("shimx64.efi", "grubx64.efi") if secure or method == "packages" else ("grubx64.efi",))
        else:
            base.grub_bios_steps(b, "grub-install", base.target_disks(b))
        path = view.boot().grub_cfg or "/boot/grub/grub.cfg"
        if view.which("update-grub"):
            b.chroot(["update-grub"], env=ENV, writes=(path,), timeout=600, when="chroot_ok")
        else:
            b.chroot(["grub-mkconfig", "-o", path], writes=(path,), timeout=600, when="chroot_ok")
        base.grub_config_checks(b, path)
        b.hints.append("Reboot and remove the live medium.")

    def _systemd_boot(self, b: PlanBuilder) -> None:
        view = b.view
        if not view.which("bootctl"):
            raise Unsupported("bootctl is not installed in this system.")
        esp = base.esp_for(b)
        if esp is None:
            raise Unsupported("The EFI system partition is not mounted.")
        installed = any(name.lower() == "systemd" for name in esp.files)
        b.title, b.risk, b.summary = "Reinstall systemd-boot", "high", "Writes systemd-boot to the EFI system partition."
        base.backup_esp(b, esp)
        flags = [] if base.nvram_ok(b.env) else ["--variables=no"]
        b.chroot(["bootctl", f"--esp-path={esp.mountpoint}", *flags, "update" if installed else "install"], writes=(f"{esp.mountpoint}/EFI/systemd/*",), timeout=300)
        b.check("esp_file", dir=f"{esp.mountpoint}/EFI/systemd", name="systemd-bootx64.efi")
        b.warn("Boot entries are written by the distribution's kernel tools (kernelstub on Pop!_OS); use 'Rebuild the initramfs' and reinstall the kernel if none is listed.")

    def kernel(self, b: PlanBuilder) -> None:
        view = b.view
        b.title, b.risk = "Reinstall the kernel", "high"
        b.summary = "Reinstalls the kernel package, then rebuilds its initramfs and the boot menu."
        packages = view.installed_packages()
        known = [k.version for k in view.kernels() if k.image]
        selected = b.versions("kernels", known) or known[-1:]
        if not selected:
            raise Unsupported("No installed kernel was found. Install one from the terminal (apt-get install linux-image-amd64).")
        names: list[str] = []
        for version in selected:
            for prefix in ("linux-image-", "linux-modules-", "linux-modules-extra-"):
                package = f"{prefix}{version}"
                if package in packages:
                    names.append(package)
        if not names:
            raise Unsupported("The kernel package for this kernel could not be determined. Reinstall it from the terminal.")
        base.need_network(b, "download the kernel package")
        self._policy_rc(b)
        b.chroot(["apt-get", "install", "--reinstall", "-y", *APT_OPTIONS, *names], env=ENV, writes=("/boot/*", "/usr/lib/modules/*"), timeout=3600)
        for version in selected:
            b.chroot(["update-initramfs", "-u", "-k", version], env=ENV, writes=(f"/boot/initrd.img-{version}",), timeout=900)
        if self.active_loader(view) == "grub" and view.which("update-grub"):
            b.chroot(["update-grub"], env=ENV, writes=("/boot/grub/grub.cfg",), timeout=600)
            base.grub_config_checks(b, view.boot().grub_cfg or "/boot/grub/grub.cfg")
        b.hints.append("Reboot and remove the live medium.")

    def finish(self, b: PlanBuilder) -> None:
        half = b.view.dpkg_half_configured()
        b.title, b.risk = "Finish an interrupted package operation", "medium"
        b.summary = "Configures the packages dpkg left half-installed and fixes broken dependencies."
        if not half:
            raise Unsupported("dpkg reports no half-configured packages.")
        self._policy_rc(b)
        b.chroot(["dpkg", "--configure", "-a"], env=ENV, writes=("the packages dpkg left unfinished",), timeout=3600)
        base.need_network(b, "fetch any missing packages")
        b.chroot(["apt-get", "-f", "install", "-y", *APT_OPTIONS], env=ENV, writes=("the packages being repaired",), timeout=3600)
