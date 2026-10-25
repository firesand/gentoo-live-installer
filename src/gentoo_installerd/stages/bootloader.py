"""BOOT: the bootloader stage."""

from __future__ import annotations

from ..configure import GRUB_EFI_FILES, grub_efi_target, RECEIPT_PATH
from .storage import StorageStages


class BootloaderStage(StorageStages):
    def _bootloader(self) -> None:
        self.configurer.notify = self.log
        atoms = self.configurer.bootloader_atoms(self.manifest)
        if atoms:  # "no boot loader" has none
            self.configurer.emerge(self.manifest, ("--noreplace", *atoms))
        else:
            self.warnings.append("No boot loader was installed, as you asked: this PC will not start the new system until you set one up "
                                 "(the kernel options are in /etc/kernel/cmdline).")
        root = self._root_device()
        efi = next((entry.device for entry in self.storage.layout if entry.mountpoint in {"/efi", "/boot/efi"}), None)
        self.configurer.configure_bootloader(self.manifest, root, efi, self._plan())
        self.configurer.write_receipt(self.manifest, root, self.storage.layout, self.storage._stack.get("luks_device"), self._plan(), stack=self._storage_stack())

    def _verify_bootloader(self) -> bool:
        efi = self.configurer.efi_mountpoint(self.manifest).lstrip("/")
        if self.manifest.system.bootloader == "none":
            return bool(self._target_file(RECEIPT_PATH))  # the user installs the boot loader
        if self.manifest.system.uki and not self._target_file(f"{efi}/EFI/Linux/gentoo.efi"):
            return False
        if not self._target_file(RECEIPT_PATH):
            return False
        if self.manifest.system.bootloader == "efi-stub":
            return "Gentoo" in self.runner.run(("efibootmgr",)).stdout
        installed, removable = GRUB_EFI_FILES[grub_efi_target(self._plan())]
        if self.manifest.system.bootloader == "grub" and self.manifest.storage.firmware == "uefi" \
                and not (self._target_file(f"{efi}/EFI/Gentoo/{installed}") or self._target_file(f"{efi}/EFI/BOOT/{removable}")):
            return False
        path = "boot/grub/grub.cfg" if self.manifest.system.bootloader == "grub" else f"{efi}/loader/entries/gentoo.conf"
        return self._target_file(path)
