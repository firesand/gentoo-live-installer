"""ACCT: users, groups and privileges on the target."""

from __future__ import annotations

import re

from gentoo_installer.manifest import InstallManifest

from .configure_base import ConfigurationError, ConfigurerBase
from .runner import RunnerError


class AccountsMixin(ConfigurerBase):
    def group_exists(self, name: str) -> bool:
        try:
            self.chroot(("getent", "group", name))
        except RunnerError:
            return False
        return True

    def wanted_groups(self, manifest: InstallManifest) -> list[str]:
        """The groups of the daily user, in the order given (PortageHub's group is added when it is installed)."""
        a = manifest.accounts
        group_names = list(dict.fromkeys(a.groups))
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", group) for group in group_names):
            raise ConfigurationError("Account groups contain an invalid name")
        # PortageHub reads the emerge log and the binary package cache through this group.
        if manifest.options.install_portagehub and "portage" not in group_names:
            group_names.append("portage")
        return group_names

    def missing_groups(self, manifest: InstallManifest) -> list[str]:
        """Wanted groups that the user is not yet in (all of them until the user exists)."""
        username = manifest.accounts.username
        try:
            have = set(self.chroot(("id", "-nG", username), capture=True).stdout.split()) if not self.dry_run else set()
        except RunnerError:
            have = set()
        return [group for group in self.wanted_groups(manifest) if group not in have]

    def configure_accounts(self, manifest: InstallManifest) -> None:
        a = manifest.accounts
        wanted = self.wanted_groups(manifest)
        # A group that does not exist (yet) is skipped, never an error: "cron" appears with the cron daemon, "gamemode" with gaming.
        # add_late_groups joins the user to the ones that exist after the packages are in.
        group_names = [group for group in wanted if group == "wheel" or self.group_exists(group)]
        skipped = [group for group in wanted if group not in group_names]
        if skipped:
            self.notify("The user joins these groups once their packages are installed: " + ", ".join(skipped))
        groups = ",".join(group_names)
        command = ["useradd", "--create-home", "--shell", "/bin/bash"]
        if a.full_name:
            command.extend(("--comment", a.full_name))
        if groups:
            command.extend(("--groups", groups))
        command.append(a.username)
        try:
            if self.dry_run:
                raise RunnerError(("id",), 1)
            self.chroot(("id", "-u", a.username))
        except RunnerError:
            self.chroot(tuple(command))
        pairs = [(a.username, a.password)]
        if not a.root_locked():
            # "same" means the user's password; "separate" has its own.
            pairs.append(("root", a.password if a.root_mode == "same" else a.root_password))
        payload = "".join(f"{user}:{password}\n" for user, password in pairs)
        self.chroot(("chpasswd",), stdin=payload, secret=True)
        if a.root_locked():
            self.chroot(("passwd", "--lock", "root"))
        if a.ssh_authorized_keys:
            if any("\n" in key or "\r" in key for key in a.ssh_authorized_keys):
                raise ConfigurationError("SSH authorized keys must be single-line keys")
            path = f"home/{a.username}/.ssh/authorized_keys"
            self.write(path, "\n".join(a.ssh_authorized_keys) + "\n", 0o600)
            self.chroot(("chown", "-R", f"{a.username}:{a.username}", f"/home/{a.username}/.ssh"))
            self.chroot(("chmod", "700", f"/home/{a.username}/.ssh"))
        self.chroot(("env-update",))

    def add_late_groups(self, manifest: InstallManifest) -> None:
        """Join the user to wanted groups that exist now (cron appears with the cron daemon). Safe to repeat; a failure is a warning."""
        username = manifest.accounts.username
        for group in self.missing_groups(manifest):
            if not self.group_exists(group):
                self.notify(f"warning: the group {group} does not exist on the new system, so {username} was not added to it")
                continue
            try:
                self.chroot(("usermod", "--append", "--groups", group, username))
            except RunnerError as exc:
                self.notify(f"warning: {username} could not be added to the group {group} ({exc})")

    def configure_privileges(self, manifest: InstallManifest) -> None:
        """sudo and doas get a wheel rule; run0 (systemd's, asks polkit) and su (needs root's password) have nothing to configure."""
        tool = manifest.accounts.privilege
        if tool == "sudo":
            self.write("etc/sudoers.d/wheel", "%wheel ALL=(ALL:ALL) ALL\n", 0o440)
        elif tool == "doas":
            self.write("etc/doas.conf", "permit persist :wheel\n", 0o600)
