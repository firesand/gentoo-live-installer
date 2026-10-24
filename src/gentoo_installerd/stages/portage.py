"""PORT: the portage stage (make.conf, package.use and friends on the target)."""

from __future__ import annotations

from ..configure import PackageInstallError
from .context import InstallError
from .storage import StorageStages


class PortageNotes(StorageStages):
    def _portage_note(self, note: str) -> None:
        """A choice that could not be applied as asked: in the log and in the result as a warning."""
        self.log("WARNING: " + note)
        if note not in self.degraded:
            self.degraded.append(note)


class PortageStage(PortageNotes):
    def _portage(self) -> None:
        self.configurer.notify = self.log
        plan = self._plan()
        hardware = getattr(self.inspection, "hardware", None) or None
        for note in self.configurer.configure_portage(self.manifest, plan, hardware=hardware):
            self._portage_note(note)
        # effective_profile() is the profile the user chose, else the one derived from init system and desktop.
        self.configurer.chroot(("eselect", "profile", "set", self.manifest.effective_profile()))
        # The keys that check binary packages must exist before the first emerge. If they cannot be set up the binary
        # package host is switched off (everything is built from source) rather than accepting an unchecked package.
        warning = self.configurer.prepare_binhost_trust(self.manifest, plan, hardware=hardware)
        if warning:
            self._portage_note(warning)
        # Resolve everything the later stages will emerge now: an impossible combination of
        # options fails here, in minutes and with the disk still reusable, not hours later. That includes
        # the tools of volumes that existed before (lvm2, mdadm) and the USE flag lvm2 needs for them.
        stack = self._storage_stack()
        self.configurer.prepare_stack_packages(stack)
        required = [*self.configurer.kernel_stage_atoms(self.manifest, self.storage.layout, plan, stack),
                    *self.configurer.kernel_atoms(self.manifest), *self.configurer.essential_atoms(self.manifest, plan),
                    *self.configurer.bootloader_atoms(self.manifest), *self.configurer.desktop_atoms(self.manifest, plan).required]
        try:
            self.configurer.pretend(self.manifest, list(dict.fromkeys(required)))
        except PackageInstallError as exc:
            raise InstallError(str(exc)) from exc
