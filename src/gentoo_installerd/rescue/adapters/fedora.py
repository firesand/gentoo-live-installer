"""Fedora, RHEL and derivatives: dracut, BLS entries via kernel-install, grub2 and dnf (shim and GRUB come from packages)."""

from __future__ import annotations

from typing import ClassVar

from ..builder import PlanBuilder
from ..errors import Unsupported
from ..view import SessionView
from . import base
from .base import Adapter

ESP_DIRS = {"fedora": "fedora", "rhel": "redhat", "centos": "centos", "almalinux": "almalinux", "rocky": "rocky", "ol": "redhat", "nobara": "fedora"}
LABELS = {"fedora": "Fedora", "rhel": "Red Hat Enterprise Linux", "centos": "CentOS Linux", "almalinux": "AlmaLinux", "rocky": "Rocky Linux"}


class FedoraAdapter(Adapter):
    family: ClassVar[str] = "fedora"
    label: ClassVar[str] = "Fedora"
    esp_default: ClassVar[str] = "/boot/efi"
    tools: ClassVar[frozenset[str]] = frozenset({"dracut", "kernel-install", "grub2-mkconfig", "grub2-install", "dnf", "yum", "rpm", "chpasswd", "usermod", "passwd", "chage", "faillock",
                                                  "grubby", "gen_grub_cfgstub", "true", "journalctl"})

    def esp_dir(self, view: SessionView) -> str:
        return ESP_DIRS.get(view.os.flavor, view.os.flavor or "fedora")

    def manager(self, view: SessionView) -> str:
        return "dnf" if view.which("dnf") else "yum" if view.which("yum") else ""

    def grub_cfg_path(self, view: SessionView) -> str:
        """BLS systems keep the real config in /boot/grub2; older ones and EL 8 keep it on the ESP."""
        esp = view.esp()
        if esp is not None:
            on_esp = f"{esp.mountpoint}/EFI/{self.esp_dir(view)}/grub.cfg"
            text = view.read(on_esp, 1 << 20) if view.exists(on_esp) else ""
            stub = len(text) < 4096 and ("configfile" in text or "search" in text)
            if text and not stub:
                return on_esp
        return "/boot/grub2/grub.cfg"

    def _require_grub(self, view: SessionView) -> None:
        loader = view.boot().loader
        if loader and loader != "grub":
            raise Unsupported(f"The active boot loader of this system is {loader}, not GRUB, so this action is not offered.")

    # ---- actions -------------------------------------------------------------------------------------------------------------------------
    def initramfs(self, b: PlanBuilder) -> None:
        view = b.view
        b.title, b.risk = "Rebuild the initramfs", "medium"
        b.summary = "Creates a new initramfs for the selected kernels so the root file system can be found and mounted."
        if not view.which("dracut"):
            raise Unsupported("dracut is not installed in this system. Reinstall it with 'dnf reinstall dracut' from the terminal.")
        kernels = [k for k in view.kernels() if k.modules]
        selected = b.versions("kernels", [k.version for k in kernels])
        flags = ["--force"] + (["--no-hostonly"] if b.params.get("generic") else [])
        b.precondition(bool(kernels), "A kernel with a module tree was found" if kernels else "No kernel with a module tree (/usr/lib/modules/<version>) was found")
        if selected:
            for version in selected:
                b.chroot(["dracut", *flags, f"/boot/initramfs-{version}.img", version], writes=(f"/boot/initramfs-{version}.img",), timeout=900)
        else:
            b.chroot(["dracut", *flags, "--regenerate-all"], writes=("/boot/initramfs-<version>.img",), timeout=1800)
        for kernel in [k for k in kernels if not selected or k.version in selected]:
            b.check("file_min_size", path=f"/boot/initramfs-{kernel.version}.img", bytes=1 << 20)

    def config(self, b: PlanBuilder) -> None:
        view = b.view
        self._require_grub(view)
        b.title, b.risk = "Regenerate the boot menu", "medium"
        b.summary = "Creates missing boot entries for the installed kernels and rebuilds grub.cfg."
        if not view.which("grub2-mkconfig"):
            raise Unsupported("grub2 is not installed in this system. Reinstall it with 'dnf reinstall grub2-tools' from the terminal.")
        base.clean_cmdline_sources(b)
        entries = " ".join(view.boot().bls_entries)
        for kernel in view.kernels():
            if kernel.image and kernel.modules and kernel.version not in entries and view.which("kernel-install"):
                if not any(step.argv[:2] == ("kernel-install", "add") for step in b.steps):
                    b.cmdline_notice()
                b.chroot(["kernel-install", "add", kernel.version, f"/usr/lib/modules/{kernel.version}/vmlinuz"], writes=("/boot/loader/entries/*",), timeout=600)
        path = self.grub_cfg_path(view)
        b.chroot(["grub2-mkconfig", "-o", path], writes=(path,), timeout=600)
        base.grub_config_checks(b, path)

    def bootloader(self, b: PlanBuilder) -> None:
        view = b.view
        self._require_grub(view)
        b.title, b.risk = "Reinstall the boot loader", "high"
        firmware = base.firmware_choice(b)
        b.needs["uefi_nvram"] = base.nvram_ok(b.env)
        b.summary = "Reinstalls the signed boot files from the distribution's packages and regenerates the menu."
        path = self.grub_cfg_path(view)
        if firmware == "uefi":
            esp = base.esp_for(b)
            if esp is None:
                raise Unsupported(base.esp_missing_reason(b))
            manager = self.manager(view)
            if not manager:
                raise Unsupported("Neither dnf nor yum was found in this system.")
            ident = self.esp_dir(view)
            base.need_network(b, "download shim and GRUB (Fedora installs them from signed packages, never with grub2-install)")
            base.backup_esp(b, esp)
            directory = f"{esp.mountpoint}/EFI/{ident}"
            files = next((names for name, names in esp.files.items() if name.lower() == ident.lower()), None)
            if files is None:  # the packages copy their files with 'cp -a ... || :', which does nothing when the directory (a reformatted ESP) is not there
                b.file({"op": "mkdir", "path": directory, "mode": 0o755, "parents": True}, f"mkdir {directory}", writes=(directory,))
            # grub2-common makes the grub.cfg stub that sends GRUB to /boot/grub2/grub.cfg; shim and GRUB alone leave it at its prompt (Fedora's own recipe lists all three)
            b.chroot([manager, "reinstall", "-y", "shim-x64", "grub2-efi-x64", "grub2-common"], writes=(f"{directory}/*",), timeout=1800)
            if "grub.cfg" not in {name.lower() for name in files or []} and view.which("gen_grub_cfgstub"):  # for an image whose packages do not bring the stub
                b.chroot(["gen_grub_cfgstub", "/boot/grub2", directory], writes=(f"{directory}/grub.cfg",), timeout=120, on_fail="continue")
            b.chroot(["grub2-mkconfig", "-o", path], writes=(path,), timeout=600)
            base.esp_loader_checks(b, esp, ident, False, ("shimx64.efi", "grubx64.efi", "grub.cfg"))
            if base.nvram_ok(b.env) and not b.params.get("no_entry"):
                node = view.graph.find(esp.device)
                label = LABELS.get(view.os.flavor, "Linux")
                b.host(["efibootmgr", "--create", "--disk", esp.disk, "--part", str(node.partn or 1), "--label", label, "--loader", f"\\EFI\\{ident}\\shimx64.efi"],
                       display=f"efibootmgr --create --disk {esp.disk} --part {node.partn or 1} --label \"{label}\" --loader '\\EFI\\{ident}\\shimx64.efi'", writes=("UEFI boot entry",))
            if base.removable_wanted(b, esp):
                b.warn("The fallback path EFI/BOOT/BOOTX64.EFI is provided by the shim package of this distribution; use 'Add a boot entry' if the firmware cannot find the system.")
        else:
            if not view.which("grub2-install"):
                raise Unsupported("grub2-install is not available in this system.")
            base.grub_bios_steps(b, "grub2-install", base.target_disks(b))
            b.chroot(["grub2-mkconfig", "-o", path], writes=(path,), timeout=600, when="chroot_ok")
        base.grub_config_checks(b, path)
        b.hints.append("Reboot and remove the live medium.")

    def kernel(self, b: PlanBuilder) -> None:
        view = b.view
        manager = self.manager(view)
        b.title, b.risk = "Reinstall the kernel", "high"
        b.summary = "Reinstalls the kernel packages (their scripts add the boot entry), then rebuilds the initramfs and the boot menu explicitly."
        if not manager:
            raise Unsupported("Neither dnf nor yum was found in this system.")
        base.need_network(b, "download the kernel packages")
        b.cmdline_notice()  # the packages' scripts run kernel-install, which writes the boot entry
        b.chroot([manager, "reinstall", "-y", "--setopt=strict=0", "kernel-core", "kernel-modules-core", "kernel-modules"], writes=("/boot/*", "/usr/lib/modules/*"), timeout=3600)
        if view.which("dracut"):
            b.chroot(["dracut", "--force", "--regenerate-all"], writes=("/boot/initramfs-<version>.img",), timeout=1800)
        if view.which("grub2-mkconfig") and view.boot().loader in {"grub", ""}:
            path = self.grub_cfg_path(view)
            b.chroot(["grub2-mkconfig", "-o", path], writes=(path,), timeout=600)
            base.grub_config_checks(b, path)
        b.hints.append("Reboot and remove the live medium. The first boot relabels the files when SELinux is enabled.")

    def finish(self, b: PlanBuilder) -> None:
        b.title, b.risk = "Rebuild the package database", "medium"
        b.summary = "Rebuilds the rpm database after an interrupted transaction."
        if not b.view.which("rpm"):
            raise Unsupported("rpm is not available in this system.")
        b.chroot(["rpm", "--rebuilddb"], writes=("/var/lib/rpm/*",), timeout=900)
