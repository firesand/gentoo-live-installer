"""PORT: the repository stage (the Portage tree)."""

from __future__ import annotations

from ..proxy import proxy_environment
from .storage import StorageStages


class RepositoryStage(StorageStages):
    def _repository(self) -> None:
        self.configurer.notify = self.log
        # make.conf first: emerge-webrsync reads GENTOO_MIRRORS from it, and a proxy has to be
        # known before the first download.
        self.configurer.configure_portage(self.manifest, self._plan())
        environment = proxy_environment(self.manifest.network.proxy)
        if self.manifest.portage.rsync_mirror:
            self.configurer.chroot(("emaint", "sync", "--auto"), env=environment)
        else:
            self.configurer.chroot(("emerge-webrsync",), env=environment)
