"""PORT: the desktop stage."""

from __future__ import annotations

from ..configure import PackageInstallError
from .context import InstallError
from .storage import StorageStages


class DesktopStage(StorageStages):
    def _desktop(self) -> None:
        """Desktop and optional packages, installed after the bootloader.

        The system is already bootable here, so a failure of an optional package is reported as a
        warning (``degraded``); only a desktop environment that cannot be installed fails the stage,
        flagged ``bootable`` so the front end can offer to reboot into the base system.
        """
        self.configurer.notify = self.log
        plan = self._plan()
        try:
            result = self.configurer.install_desktop(self.manifest, plan)
        except PackageInstallError as exc:
            self.log(f"The base system is installed and bootable; only the desktop failed: {exc}")
            error = InstallError(f"{exc} The base system is installed and bootable; reboot to use it or retry the desktop installation.")
            error.bootable = True  # type: ignore[attr-defined]
            raise error from exc
        warnings = [*result.degraded, *self.configurer.configure_services(self.manifest, plan, phase="desktop")]
        self.configurer.configure_desktop_session(self.manifest)
        warnings.extend(self.configurer.run_plan_steps(plan))
        # Only binary packages can leave the installed USE flags different from make.conf, so source
        # installs skip the (minutes-long) check.
        drift = self.configurer.world_drift(self.manifest) if self.manifest.portage.binhost else []
        if drift:
            # Visible in install.log; the next @world update will rebuild these with the configured USE.
            self.log(f"NOTE: {len(drift)} installed package(s) differ from the configured USE flags: {', '.join(drift[:12])}"
                     + (", ..." if len(drift) > 12 else ""))
        if warnings:
            self.degraded.extend(warnings)
            for warning in warnings:
                self.log("WARNING: " + warning)
            self.emit("progress", "desktop", "Installed with warnings", degraded=warnings, bootable=True)
