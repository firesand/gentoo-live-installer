"""BOOT: applying the hardware plan on the target."""

from __future__ import annotations

import re
from typing import Sequence

from gentoo_installer import recommend

from .configure_base import _plan_list, ConfigurationError
from .configure_portage import MEMORY_ENV_FILE, PACKAGE_ENV_FILE, PortageMixin
from .runner import RunnerError


class HardwareMixin(PortageMixin):
    def run_plan_steps(self, plan: object | None = None) -> list[str]:
        """Post-install commands from the hardware plan; failures are reported, never fatal."""
        warnings: list[str] = []
        for step in _plan_list(self._plan(plan), "post_steps"):
            argv = tuple(str(part) for part in step)
            try:
                self.chroot(argv)
            except RunnerError as exc:
                warnings.append(f"{' '.join(argv)[:80]} failed: {str(exc)[-160:]}")
        return warnings

    def apply_hardware(self, plan: object | None = None) -> list[str]:
        """Write the plan's files and run its post-install steps; returns warnings for steps that failed."""
        self.write_plan_files(plan)
        return self.run_plan_steps(plan)

    def write_plan_files(self, plan: object | None = None) -> None:
        """Files the hardware plan wants on the target (written as root:root).

        The plan sized its limit for the memory-heavy packages from what the probe saw. The setting in force is MAKEOPTS in
        make.conf of the new system, and package.env replaces it for the packages it names: a limit that is not below that
        setting would raise those packages, so the two files are left out then.
        """
        files = [(str(path), str(content), int(mode)) for path, content, mode in _plan_list(self._plan(plan), "files")]
        if self.memory_limit_raises(files):
            self.notify("The compile-job limit for memory-heavy packages is not below MAKEOPTS of the new system, so it is not needed")
            files = [item for item in files if item[0].lstrip("/") not in (MEMORY_ENV_FILE, PACKAGE_ENV_FILE)]
        for path, content, mode in files:
            self.write(path, content, mode)

    def memory_limit_raises(self, files: Sequence[tuple[str, str, int]]) -> bool:
        """Whether the plan's limit for the memory-heavy packages is not below MAKEOPTS of the new system."""
        text = next((content for path, content, _mode in files if path.lstrip("/") == MEMORY_ENV_FILE), "")
        match = re.search(r'(?m)^MAKEOPTS="?([^"\n]*)', text)
        if not match or self.dry_run:
            return False
        try:
            machine = recommend.make_opts_jobs(self._variable("etc/portage/make.conf", "MAKEOPTS"))
        except (OSError, ValueError, ConfigurationError):
            return False
        limit = recommend.make_opts_jobs(match.group(1))
        return limit is not None and machine is not None and limit >= machine
