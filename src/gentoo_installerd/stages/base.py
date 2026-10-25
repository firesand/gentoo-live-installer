"""CORE: the base stage (essential packages and services)."""

from __future__ import annotations

from ..configure import PackageInstallError
from .context import InstallError
from .storage import StorageStages


class BaseStage(StorageStages):
    def _base(self) -> None:
        """Essential packages and services: after this the system can be managed from a console."""
        self.configurer.notify = self.log
        try:
            self.configurer.install_essential(self.manifest, self._plan())
        except PackageInstallError as exc:
            raise InstallError(str(exc)) from exc
        self.configurer.configure_network_late(self.manifest)
        self.configurer.configure_sshd(self.manifest)
        self.configurer.configure_journal(self.manifest)
        self.configurer.configure_cron_daemon(self.manifest)
        self.configurer.add_late_groups(self.manifest)
        self.configurer.configure_services(self.manifest, self._plan(), phase="base")
