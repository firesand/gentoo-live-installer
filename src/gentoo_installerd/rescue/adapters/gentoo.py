"""Gentoo: dracut or genkernel, GRUB or systemd-boot, kernels from sys-kernel/gentoo-kernel(-bin) (docs: never trust the install hooks)."""

from __future__ import annotations

import re
from typing import Any, ClassVar

from ..builder import PlanBuilder
from ..errors import RescueError, Unsupported
from ..view import SessionView
from . import base
from .base import Adapter

DIST_KERNELS = ("sys-kernel/gentoo-kernel-bin", "sys-kernel/gentoo-kernel", "sys-kernel/vanilla-kernel")
CPV_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/[A-Za-z0-9_+-]+$")


class GentooAdapter(Adapter):
    family: ClassVar[str] = "gentoo"
    label: ClassVar[str] = "Gentoo"
    default_bootloader_id: ClassVar[str] = "Gentoo"
    esp_default: ClassVar[str] = "/efi"
    tools: ClassVar[frozenset[str]] = frozenset({"grub-install", "grub-mkconfig", "dracut", "emerge", "eselect", "bootctl", "genkernel", "kernel-install", "chpasswd", "usermod", "passwd",
                                                  "chage", "faillock", "lsinitrd", "uefi-mkconfig", "true", "journalctl"})

    # ---- facts (read from files only) ---------------------------------------------------------------------------------------------
    @staticmethod
    def kernel_packages(view: SessionView) -> dict[str, str]:
        packages = view.installed_packages()
        return {f"{name}-{version}": name for name, version in packages.items() if name in DIST_KERNELS}

    @staticmethod
    def installkernel_use(view: SessionView) -> str:
        for entry in view.listdir("/var/db/pkg/sys-kernel"):
            if entry.startswith("installkernel"):
                return view.read(f"/var/db/pkg/sys-kernel/{entry}/USE", 4096)
        return ""

    def initramfs_tool(self, view: SessionView) -> str:
        if view.which("dracut"):
            return "dracut"
        if view.which("genkernel") and any(k.image and k.image.startswith("/boot/kernel-") for k in view.kernels()):
            return "genkernel"
        return "none"

    def kernel_kind(self, view: SessionView) -> str:
        """'dist' (sources present, emerge --config works), 'dist-incomplete' (needs a re-merge), 'genkernel' or 'manual'."""
        packages = view.installed_packages()
        for name, version in packages.items():
            if name in DIST_KERNELS:
                for kernel in view.kernels():
                    if kernel.version.startswith(version) and view.exists(f"/usr/src/linux-{kernel.version}/include/config/kernel.release"):
                        return "dist"
                return "dist-incomplete"
        return "genkernel" if view.which("genkernel") else "manual"

    def active_loader(self, view: SessionView) -> str:
        boot = view.boot()
        return boot.loader or ("grub" if view.which("grub-install") else "")

    # ---- actions -------------------------------------------------------------------------------------------------------------------------
    def initramfs(self, b: PlanBuilder) -> None:
        view = b.view
        tool = self.initramfs_tool(view)
        b.title, b.risk = "Rebuild the initramfs", "medium"
        kernels = [k for k in view.kernels() if k.modules]
        selected = b.versions("kernels", [k.version for k in kernels])
        generic = bool(b.params.get("generic"))
        b.summary = "Creates a new initramfs for the selected kernels so the root file system can be found and mounted."
        if tool == "none":
            raise Unsupported("Neither dracut nor genkernel is installed in this system. Reinstall sys-kernel/dracut from the terminal, then run this again.")
        chosen = [k for k in kernels if not selected or k.version in selected]
        b.precondition(bool(chosen), "A kernel with a module tree was found" if chosen else "No kernel with a module tree (/usr/lib/modules/<version>) was found")
        free = view.free_bytes("/boot")
        b.precondition(free == 0 or free >= 100 << 20, "/boot has enough free space" if free >= 100 << 20 or free == 0 else f"/boot has only {free // (1 << 20)} MiB free")
        if tool == "dracut":
            flags = ["--force"] + (["--no-hostonly"] if generic else [])
            if selected:
                for kernel in chosen:
                    b.chroot(["dracut", *flags, f"/boot/initramfs-{kernel.version}.img", kernel.version], writes=(f"/boot/initramfs-{kernel.version}.img",), timeout=900)
            else:
                b.chroot(["dracut", *flags, "--regenerate-all"], writes=("/boot/initramfs-<version>.img",), timeout=1800)
        else:
            b.chroot(["genkernel", "--install", "initramfs"], writes=("/boot/initramfs-*",), timeout=1800)
        if generic:
            b.warn("A generic initramfs includes drivers for many kinds of hardware, so the system can start on a different computer. It is larger than a host-specific one.")
        for kernel in chosen:
            b.check("file_min_size", path=f"/boot/initramfs-{kernel.version}.img", bytes=1 << 20)

    def config(self, b: PlanBuilder) -> None:
        view = b.view
        loader = self.active_loader(view)
        b.title, b.risk = "Regenerate the boot menu", "medium"
        b.summary = "Rebuilds grub.cfg from the kernels found in /boot."
        if loader != "grub":
            raise Unsupported("The boot loader here is not GRUB. Kernel entries of systemd-boot are written when a kernel is (re)installed; use 'Reinstall the kernel'." if loader == "systemd-boot"
                              else "No GRUB installation was found, so there is no menu file to regenerate. Reinstall the boot loader instead.")
        path = view.boot().grub_cfg or "/boot/grub/grub.cfg"
        b.chroot(["grub-mkconfig", "-o", path], writes=(path,), timeout=600)
        base.grub_config_checks(b, path)

    def kernel(self, b: PlanBuilder) -> None:
        view = b.view
        b.title, b.risk = "Reinstall the kernel", "high"
        b.summary = "Reinstalls the distribution kernel, then rebuilds the initramfs and the boot menu explicitly (the install hooks alone are not trusted)."
        kind = self.kernel_kind(view)
        packages = self.kernel_packages(view)
        if kind in {"genkernel", "manual"} or not packages:
            raise Unsupported("This system uses a manually built or genkernel kernel. Rebuild it in the terminal: cd /usr/src/linux && make modules_install install && genkernel --install initramfs")
        mode = b.choice("mode", {"config", "remerge"}, "config" if kind == "dist" else "remerge")
        chosen = str(b.params.get("package", "")) or sorted(packages)[-1]
        if chosen not in packages:
            raise RescueError("bad_request", f"{chosen!r} is not an installed kernel package", param="package")
        atom = f"={chosen}"
        guard = view.installkernel_guard(self.installkernel_use(view))
        if guard.blocks:
            b.warn(f"Gentoo's installkernel refuses to run in a chroot here ({guard.reason}). A temporary override file is created and removed again; programs inside the chroot see a synthesized kernel command line.")
            directory = guard.override_path.rsplit("/", 1)[0]
            b.file({"op": "mkdir", "path": directory, "mode": 0o755, "parents": True}, f"mkdir {directory}", group="guard")
            b.file({"op": "touch", "path": guard.override_path, "mode": 0o644}, f"create {guard.override_path} (temporary, removed afterwards)", writes=(guard.override_path,), group="guard")
            b.file({"op": "remove", "path": guard.override_path}, f"remove {guard.override_path}", cleanup=True, group="guard")
        if mode == "config":
            b.precondition(kind == "dist", "The kernel sources needed by 'emerge --config' are present" if kind == "dist"
                           else "The kernel sources (/usr/src/linux-<version>) are missing; choose mode 'remerge' (needs the network)")
            b.chroot(["emerge", "--ask=n", "--config", atom], writes=("/boot/vmlinuz-*", "/boot/initramfs-*", "/usr/lib/modules/*"), timeout=3600)
        else:
            base.need_network(b, "download and rebuild the kernel package")
            b.chroot(["emerge", "--ask=n", "--oneshot", atom], writes=("/usr/src/linux-*", "/boot/*", "/usr/lib/modules/*"), timeout=7200)
        if self.initramfs_tool(view) == "dracut":
            b.chroot(["dracut", "--force", "--regenerate-all"], writes=("/boot/initramfs-<version>.img",), timeout=1800)
        if self.active_loader(view) == "grub":
            path = view.boot().grub_cfg or "/boot/grub/grub.cfg"
            b.chroot(["grub-mkconfig", "-o", path], writes=(path,), timeout=600)
            base.grub_config_checks(b, path)
        b.hints.append("Reboot and remove the live medium.")

    def bootloader(self, b: PlanBuilder) -> None:
        view = b.view
        loader = str(b.params.get("loader", "")) or self.active_loader(view) or "grub"
        if loader not in {"grub", "systemd-boot", "efistub"}:
            raise RescueError("bad_request", "loader must be grub, systemd-boot or efistub", param="loader")
        b.title, b.risk = "Reinstall the boot loader", "high"
        b.needs["uefi_nvram"] = base.nvram_ok(b.env)
        if self.active_loader(view) not in {"", loader} and not b.params.get("loader"):
            b.warn(f"The files of {self.active_loader(view)} are on this system; you asked for {loader}.")
        firmware = base.firmware_choice(b)
        if loader == "grub":
            self._grub(b, firmware)
        elif loader == "systemd-boot":
            self._systemd_boot(b, firmware)
        else:
            self._efistub(b, firmware)

    def _grub(self, b: PlanBuilder, firmware: str) -> None:
        view = b.view
        if not view.which("grub-install"):
            raise Unsupported("GRUB is not installed in this system. Reinstall sys-boot/grub from the terminal (emerge --oneshot sys-boot/grub) and run this again.")
        b.summary = "Writes GRUB to the " + ("EFI system partition" if firmware == "uefi" else "disk") + " and regenerates its menu."
        if firmware == "uefi":
            esp = base.esp_for(b)
            if esp is None:
                raise Unsupported("The EFI system partition is not mounted. Fix fstab (or choose it in the mount plan) and try again.")
            ident = base.bootloader_id(b, esp, self.default_bootloader_id, ("gentoo",))
            removable = base.removable_wanted(b, esp)
            base.backup_esp(b, esp)
            base.grub_uefi_steps(b, "grub-install", esp, ident, removable=removable)
            base.esp_loader_checks(b, esp, ident, removable)
            if b.env.uefi and base.nvram_ok(b.env):
                b.check("efi_entry", loader=ident)
        else:
            base.grub_bios_steps(b, "grub-install", base.target_disks(b))
        path = view.boot().grub_cfg or "/boot/grub/grub.cfg"
        b.chroot(["grub-mkconfig", "-o", path], writes=(path,), timeout=600, when="chroot_ok")
        base.grub_config_checks(b, path)
        b.hints.append("Reboot and remove the live medium." + (" If the firmware does not list the system, choose the disk in its boot menu (often F12 or Esc)." if firmware == "uefi" else ""))

    def _systemd_boot(self, b: PlanBuilder, firmware: str) -> None:
        view = b.view
        if firmware != "uefi":
            raise Unsupported("systemd-boot needs UEFI; this system is installed for legacy BIOS.")
        if not view.which("bootctl"):
            raise Unsupported("bootctl (systemd) is not installed in this system.")
        esp = base.esp_for(b)
        if esp is None:
            raise Unsupported("The EFI system partition is not mounted.")
        installed = any(name.lower() == "systemd" for name in esp.files)
        b.title, b.summary = "Reinstall systemd-boot", "Writes systemd-boot to the EFI system partition."
        base.backup_esp(b, esp)
        flags = [] if base.nvram_ok(b.env) else ["--variables=no"]
        b.chroot(["bootctl", f"--esp-path={esp.mountpoint}", *flags, "update" if installed else "install"], writes=(f"{esp.mountpoint}/EFI/systemd/*", f"{esp.mountpoint}/EFI/BOOT/BOOTX64.EFI"), timeout=300)
        b.check("esp_file", dir=f"{esp.mountpoint}/EFI/systemd", name="systemd-bootx64.efi")
        b.warn("Entries for your kernels come from the installkernel layout; use 'Reinstall the kernel' if no entry is listed.")

    def _efistub(self, b: PlanBuilder, firmware: str) -> None:
        raise Unsupported("This system starts its kernel directly from the firmware (EFI stub), so there are no boot loader files to reinstall. Use 'Add a boot entry' to register the kernel again.")

    def finish(self, b: PlanBuilder) -> None:
        b.title, b.risk = "Finish an interrupted merge", "medium"
        b.summary = "Continues the package list Portage was working on (emerge --resume)."
        if not b.view.portage_resume_pending():
            raise Unsupported("Portage has no unfinished merge list to resume.")
        base.need_network(b, "download the remaining packages")
        b.chroot(["emerge", "--ask=n", "--resume"], writes=("the packages being merged",), timeout=14400)
