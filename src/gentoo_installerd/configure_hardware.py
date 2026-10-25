"""BOOT: applying the hardware plan on the target."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Sequence

from gentoo_installer import recommend
from gentoo_installer.manifest import InstallManifest

from .configure_base import _plan_list, _plan_scalar, ConfigurationError
from .configure_portage import MEMORY_ENV_FILE, PACKAGE_ENV_FILE, PortageMixin
from .runner import RunnerError

KEYWORDS_FILE = "etc/portage/package.accept_keywords/installer-hardware"
UNMASK_FILE = "etc/portage/package.unmask/installer-hardware"
REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
REPO_URI = re.compile(r"https://[A-Za-z0-9._~/-]+")
SYNC_TYPES = ("git",)
FIRST_STEPS_HEADER = ("First steps on your new Gentoo system\n"
                      "====================================\n"
                      "The installer wrote this file because of the hardware it found in this computer.\n\n")


def repo_specs(plan: object | None) -> list[tuple[str, str, str]]:
    """(name, sync-type, sync-uri) of the plan's companion repositories. Each plan entry reads ``NAME SYNC-TYPE SYNC-URI``; anything else is refused."""
    specs: list[tuple[str, str, str]] = []
    for entry in _plan_list(plan, "repos"):
        parts = str(entry).split()
        if len(parts) != 3 or not REPO_NAME.fullmatch(parts[0]) or parts[1] not in SYNC_TYPES or not REPO_URI.fullmatch(parts[2]):
            raise ConfigurationError(f"The plan names a software source the installer cannot set up: {str(entry)[:80]}")
        specs.append((parts[0], parts[1], parts[2]))
    return specs


class HardwareMixin(PortageMixin):
    if TYPE_CHECKING:
        def group_exists(self, name: str) -> bool: ...  # configure_accounts

    def run_plan_steps(self, plan: object | None = None) -> list[str]:
        """What the plan wants once the desktop packages are in: first-steps.txt, group membership, user services, then its post-install commands.

        Failures are reported, never fatal.
        """
        plan = self._plan(plan)
        warnings: list[str] = []
        warnings += self.write_first_steps(plan)
        warnings += self.join_plan_groups(plan)
        warnings += self.enable_user_services(plan)
        for step in _plan_list(plan, "post_steps"):
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
        plan = self._plan(plan)
        files = [(str(path), str(content), int(mode)) for path, content, mode in _plan_list(plan, "files")]
        if self.memory_limit_raises(files):
            self.notify("The compile-job limit for memory-heavy packages is not below MAKEOPTS of the new system, so it is not needed")
            files = [item for item in files if item[0].lstrip("/") not in (MEMORY_ENV_FILE, PACKAGE_ENV_FILE)]
        for path, content, mode in files:
            self.write(path, content, mode)
        self.write_plan_portage(plan)

    def write_plan_portage(self, plan: object | None = None) -> None:
        """Portage files of the plan's schema 2 fields: accepted testing versions, unmasked atoms and companion repositories (repos.conf)."""
        plan = self._plan(plan)
        keywords = dict(getattr(plan, "package_keywords", None) or {})
        if keywords:
            self.write(KEYWORDS_FILE, "# Written by the Gentoo installer: one exact version each, never a whole package.\n"
                       + "".join(f"{atom} {' '.join(words)}\n" for atom, words in sorted(keywords.items())))
        unmask = _plan_list(plan, "package_unmask")
        if unmask:
            self.write(UNMASK_FILE, "# Written by the Gentoo installer.\n" + "".join(f"{atom}\n" for atom in unmask))
        for name, sync_type, uri in repo_specs(plan):
            self.write(f"etc/portage/repos.conf/{name}.conf",
                       f"[{name}]\nlocation = /var/db/repos/{name}\nsync-type = {sync_type}\nsync-uri = {uri}\nauto-sync = yes\n")

    def prepare_plan_repos(self, manifest: InstallManifest, plan: object | None = None) -> list[str]:
        """Fetch the companion repositories of the plan (git is installed first); returns a warning when that failed.

        A failure is not fatal: the packages that need a repository then fail on their own and are reported as not installed.
        """
        plan = self._plan(plan)
        try:
            specs = repo_specs(plan)
        except ConfigurationError as exc:
            return [str(exc)]
        if not specs:
            return []
        try:
            self.emerge(manifest, ("--noreplace", "dev-vcs/git"))  # type: ignore[attr-defined]  # configure_packages
            for name, _sync_type, _uri in specs:
                self.chroot(("emaint", "sync", "-r", name))
        except RunnerError as exc:
            names = ", ".join(name for name, _t, _u in specs)
            return [f"The extra software sources ({names}) could not be downloaded: {str(exc)[-160:]}. The packages that need them were skipped."]
        return []

    def write_first_steps(self, plan: object | None = None) -> list[str]:
        """first-steps.txt in the first user's home: the plan's hints, for a newcomer who wonders what the installer did."""
        lines = [str(line) for line in _plan_list(plan, "first_steps")]
        user = str(_plan_scalar(plan, "first_user", "") or "")
        if not lines or not user or not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
            return []
        try:
            self.write(f"home/{user}/first-steps.txt", FIRST_STEPS_HEADER + "".join(f"* {line}\n" for line in lines))
            self.chroot(("chown", f"{user}:", f"/home/{user}/first-steps.txt"))
        except (RunnerError, OSError, ConfigurationError) as exc:
            return [f"first-steps.txt could not be written: {str(exc)[-120:]}"]
        return []

    def join_plan_groups(self, plan: object | None = None) -> list[str]:
        """The groups of the plan (plugdev, i2c ...) for the first user, each only if the group exists now (its package made it)."""
        groups = [str(group) for group in _plan_list(plan, "groups")]
        user = str(_plan_scalar(plan, "first_user", "") or "")
        if not groups or not user:
            return []
        warnings: list[str] = []
        for group in groups:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", group):
                continue
            if not self.group_exists(group):
                self.notify(f"The group {group} does not exist, so {user} was not added to it")
                continue
            try:
                self.chroot(("usermod", "-aG", group, user))
            except RunnerError as exc:
                warnings.append(f"{user} could not be added to the group {group}: {str(exc)[-120:]}")
        return warnings

    def enable_user_services(self, plan: object | None = None) -> list[str]:
        """Services of the user's own session (``systemctl --global enable``); only systemd has them."""
        if _plan_scalar(plan, "init", "systemd") != "systemd":
            return []
        warnings: list[str] = []
        for item in _plan_list(plan, "user_services"):
            name = str(item[0])
            try:
                self.chroot(("systemctl", "--global", "enable", name))
            except RunnerError as exc:
                warnings.append(f"The user service {name} could not be enabled: {str(exc)[-120:]}")
        return warnings

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
