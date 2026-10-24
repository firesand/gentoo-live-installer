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

    def configure_accounts(self, manifest: InstallManifest) -> None:
        a = manifest.accounts
        group_names = list(a.groups)
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", group) for group in group_names):
            raise ConfigurationError("Account groups contain an invalid name")
        # PortageHub reads the emerge log and the binary package cache through this group.
        if manifest.options.install_portagehub and "portage" not in group_names and self.group_exists("portage"):
            group_names.append("portage")
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
        if not a.disable_root_login:
            pairs.append(("root", a.root_password))
        payload = "".join(f"{user}:{password}\n" for user, password in pairs)
        self.chroot(("chpasswd",), stdin=payload, secret=True)
        if a.disable_root_login:
            self.chroot(("passwd", "--lock", "root"))
        if a.ssh_authorized_keys:
            if any("\n" in key or "\r" in key for key in a.ssh_authorized_keys):
                raise ConfigurationError("SSH authorized keys must be single-line keys")
            path = f"home/{a.username}/.ssh/authorized_keys"
            self.write(path, "\n".join(a.ssh_authorized_keys) + "\n", 0o600)
            self.chroot(("chown", "-R", f"{a.username}:{a.username}", f"/home/{a.username}/.ssh"))
            self.chroot(("chmod", "700", f"/home/{a.username}/.ssh"))
        self.chroot(("env-update",))

    def configure_privileges(self, manifest: InstallManifest) -> None:
        if manifest.accounts.privilege == "sudo":
            self.write("etc/sudoers.d/wheel", "%wheel ALL=(ALL:ALL) ALL\n", 0o440)
        else:
            self.write("etc/doas.conf", "permit persist :wheel\n", 0o600)
