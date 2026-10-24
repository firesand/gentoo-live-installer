"""PORT: the portage stage (make.conf, package.use and friends on the target)."""

from __future__ import annotations

from ..configure import PackageInstallError
from .context import InstallError
from .storage import StorageStages


class PortageStage(StorageStages):
    def _portage(self) -> None:
        self.configurer.notify = self.log
        plan = self._plan()
        self.configurer.configure_portage(self.manifest, plan)
        self.configurer.chroot(("eselect", "profile", "set", self.manifest.effective_profile()))
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
