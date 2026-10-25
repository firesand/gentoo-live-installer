"""BOOT: hostname, locale, time zone, keyboard, hosts, services, logger, cron and the desktop session."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from gentoo_installer import keymaps
from gentoo_installer.manifest import Accounts, InstallManifest, System

from .configure_base import _plan_list, ConfigurationError, ConfigurerBase, shell_value
from .configure_boot import check_secure_boot_material
from .configure_fstab import root_stack_problems
from .runner import Runner, RunnerError


def locale_gen_line(locale: str, supported: str = "") -> str:
    """A line for /etc/locale.gen: the name and the charmap glibc generates it with."""
    for line in supported.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == locale:
            return f"{locale} {fields[1]}"
    codeset = re.search(r"\.([A-Za-z0-9-]+)", locale)
    return f"{locale} {codeset.group(1) if codeset else 'UTF-8'}"


# What each choice of the tools tab installs (an empty list: it is part of the stage3 or of systemd).
PRIVILEGE_ATOMS = {"sudo": "app-admin/sudo", "doas": "app-admin/doas", "run0": "sys-auth/polkit"}  # su is part of sys-apps/shadow
CRON_ATOMS = {"cronie": "sys-process/cronie", "dcron": "sys-process/dcron", "fcron": "sys-process/fcron", "bcron": "sys-process/bcron"}
LOGGER_ATOMS = {"sysklogd": ["app-admin/sysklogd"], "syslog-ng": ["app-admin/syslog-ng", "app-admin/logrotate"], "metalog": ["app-admin/metalog"]}
LOGGER_SERVICES = {"sysklogd": "sysklogd", "syslog-ng": "syslog-ng", "metalog": "metalog"}  # OpenRC only: systemd keeps its own journal


def render_hosts(hostname: str, domain: str = "", extra: Sequence[str] = ()) -> str:
    """/etc/hosts: the computer's own name (with its domain only when the user gave one) and any extra lines."""
    own = f"{hostname}.{domain} {hostname}" if domain else hostname
    text = f"127.0.0.1\tlocalhost\n::1\tlocalhost\n127.0.1.1\t{own}\n"
    return text + "".join(" ".join(line.split()) + "\n" for line in extra if line.strip())


def render_sshd_config(accounts: Accounts) -> str:
    """/etc/ssh/sshd_config.d/10-installer.conf. Root never logs in remotely; "password login" means the PAM keyboard-interactive prompt
    (Gentoo's own 9999999gentoo-pam.conf already turns PasswordAuthentication off and lets PAM ask)."""
    lines = ["# Written by the Gentoo LiveCD installer. Change it here, then restart sshd.", "PermitRootLogin no"]
    if not accounts.ssh_password_login:
        lines.extend(("PasswordAuthentication no", "KbdInteractiveAuthentication no"))
    return "\n".join(lines) + "\n"


def normalised_locale(name: str) -> str:
    """How `locale -a` spells a locale: en_US.UTF-8 is listed as en_US.utf8."""
    base, dot, codeset = name.partition(".")
    codeset, at, modifier = codeset.partition("@")
    return base + (dot + codeset.lower().replace("-", "") if dot else "") + (at + modifier if at else "")


def generated_locales(system: System) -> list[str]:
    """The locales for /etc/locale.gen: the main one, the extra ones and en_US.UTF-8 (many programs and installers expect it)."""
    wanted = [system.locale, *system.extra_locales]
    if system.locale not in {"C", "C.UTF-8", "POSIX"}:
        wanted.append("en_US.UTF-8")
    return [name for name in dict.fromkeys(wanted) if name not in {"C", "C.UTF-8", "POSIX"}]


# --- keyboard files ----------------------------------------------------------------------------


def _keyboard_is_default(system: System) -> bool:
    return (system.keyboard_layout == "us" and not system.keyboard_variant
            and system.keyboard_model == "pc105" and not system.keyboard_options)


def render_xorg_keyboard(system: System) -> str:
    lines = ['Section "InputClass"', '\tIdentifier "system-keyboard"', '\tMatchIsKeyboard "on"',
             f'\tOption "XkbLayout" "{system.keyboard_layout}"', f'\tOption "XkbModel" "{system.keyboard_model}"']
    if system.keyboard_variant:
        lines.append(f'\tOption "XkbVariant" "{system.keyboard_variant}"')
    if system.keyboard_options:
        lines.append(f'\tOption "XkbOptions" "{system.keyboard_options}"')
    lines.append("EndSection")
    return "\n".join(lines) + "\n"


def render_kxkbrc(system: System) -> str:
    lines = ["[Layout]", f"LayoutList={system.keyboard_layout}", "Use=true", f"Model={system.keyboard_model}"]
    if system.keyboard_variant:
        lines.append(f"VariantList={system.keyboard_variant}")
    if system.keyboard_options:
        lines.extend((f"Options={system.keyboard_options}", "ResetOldOptions=true"))
    return "\n".join(lines) + "\n"


def render_dconf_keyboard(system: System) -> str:
    layouts = system.keyboard_layout.split(",")
    variants = system.keyboard_variant.split(",") if system.keyboard_variant else []
    sources = []
    for index, layout in enumerate(layouts):
        variant = variants[index] if index < len(variants) else ""
        sources.append(f"('xkb', '{layout}{'+' + variant if variant else ''}')")
    lines = ["[org/gnome/desktop/input-sources]", f"sources=[{', '.join(sources)}]"]
    if system.keyboard_options:
        lines.append("xkb-options=[" + ", ".join(f"'{option}'" for option in system.keyboard_options.split(",")) + "]")
    return "\n".join(lines) + "\n"


def choice_problems(manifest: InstallManifest) -> list[str]:
    """Choices of the accounts and tools that cannot be carried out; a choice is refused here, never silently ignored."""
    accounts, options, system = manifest.accounts, manifest.options, manifest.system
    problems: list[str] = []
    if accounts.ssh_root_login:
        problems.append("Root login over SSH is not available: log in with your own account and use sudo (or su) for administrator rights")
    if accounts.root_locked():
        if "wheel" not in accounts.groups:
            problems.append("Turning root login off needs an administrator account: the user must be in the wheel group")
        if accounts.privilege == "su":
            problems.append("su needs the root password, so it cannot be the administrator tool when root login is turned off: choose sudo, doas or run0")
    if options.journal_persistent and system.init != "systemd":
        problems.append("Keeping the journal across reboots needs systemd; OpenRC keeps its logs in /var/log")
    if options.gaming == "native" and not options.gaming_accept_external:
        problems.append("Native gaming downloads Steam from Valve and uses an outside repository and testing (~amd64) packages: tick the box that accepts this, or choose Flatpak gaming")
    return problems


def live_input_problems(manifest: InstallManifest, *, root: str | Path = "/", runner: Runner | None = None) -> list[str]:
    """Choices that this live system can already tell are unusable.

    The daemon's preflight runs this before any disk is touched, so a typo in a time zone, keymap or
    locale (or a wrong Secure Boot key) stops the installation at the start instead of after the
    disk was wiped. Lists that the live image does not have are not checked.
    """
    base = Path(root)
    system = manifest.system
    problems: list[str] = []
    zones = base / "usr/share/zoneinfo"
    if zones.is_dir() and not (zones / system.timezone).is_file():
        problems.append(f"The time zone {system.timezone!r} does not exist on this system")
    available = keymaps.list_console_keymaps((base / "usr/share/keymaps", base / "usr/share/kbd/keymaps"))
    wanted = system.keymap if system.keymap not in {"", "auto"} else keymaps.console_keymap_for(system.keyboard_layout, system.keyboard_variant)
    if available and wanted not in available:
        close = ", ".join(keymaps.near_matches(wanted, sorted(available))) or "none"
        problems.append(f"The console keymap {wanted!r} does not exist (similar: {close})")
    supported = base / "usr/share/i18n/SUPPORTED"
    if supported.is_file() and system.locale not in {"C", "C.UTF-8", "POSIX"}:
        names = {line.split()[0] for line in supported.read_text(errors="replace").splitlines() if line.strip() and not line.startswith("#")}
        if names and system.locale not in names:
            problems.append(f"The locale {system.locale!r} is not available (see /usr/share/i18n/SUPPORTED)")
    fonts = base / "usr/share/consolefonts"
    if system.console_font and fonts.is_dir() and not list(fonts.glob(f"{system.console_font}.*")) and not system.console_font.startswith("ter-"):
        problems.append(f"The console font {system.console_font!r} is not installed")
    problems.extend(choice_problems(manifest))
    if system.secure_boot and runner is not None:
        problems.extend(check_secure_boot_material(runner, system))
    if manifest.storage.mode == "manual" and runner is not None:
        problems.extend(root_stack_problems(manifest, runner))
    return problems


class SystemMixin(ConfigurerBase):
    # ----- identity, accounts, network --------------------------------------------------------------

    def resolve_console_keymap(self, manifest: InstallManifest) -> str:
        """The console keymap to write: the chosen one if kbd has it, else US with a warning."""
        system = manifest.system
        wanted = system.keymap
        if wanted in {"", "auto"}:
            wanted = keymaps.console_keymap_for(system.keyboard_layout, system.keyboard_variant)
        if self.dry_run or wanted == "us":
            return wanted
        available = keymaps.list_console_keymaps((self.path("usr/share/keymaps"), self.path("usr/share/kbd/keymaps")))
        if available and wanted not in available:
            close = ", ".join(keymaps.near_matches(wanted, sorted(available))) or "none"
            self.notify(f"warning: the console keymap {wanted!r} does not exist on the installed system (similar: {close}); using 'us'")
            return "us"
        return wanted

    def luks_prompt_uses_console_keymap(self, manifest: InstallManifest) -> bool:
        """True when the console keymap was chosen through the keyboard layout, so the LUKS prompt can use it.

        The passphrase is typed in the live session; the boot prompt may only use a non-US map when
        that layout was also active while typing (the layout chooser switches it).
        """
        system = manifest.system
        derived = keymaps.console_keymap_for(system.keyboard_layout, system.keyboard_variant)
        keymap = system.keymap if system.keymap not in {"", "auto"} else derived
        return keymap != "us" and derived == keymap

    def keyboard_files(self, manifest: InstallManifest) -> list[tuple[str, str, int]]:
        """X11/Wayland keyboard layout for display managers and sessions (graphical desktops only)."""
        system = manifest.system
        if system.desktop == "minimal" or _keyboard_is_default(system):
            return []
        files = [("etc/X11/xorg.conf.d/00-keyboard.conf", render_xorg_keyboard(system), 0o644)]
        if system.desktop == "kde":
            # Plasma (X11 and Wayland) and the SDDM greeter read kxkbrc, not xorg.conf.d.
            files.append(("etc/xdg/kxkbrc", render_kxkbrc(system), 0o644))
        elif system.desktop == "gnome":
            files.append(("etc/dconf/profile/user", "user-db:user\nsystem-db:local\n", 0o644))
            files.append(("etc/dconf/db/local.d/00-gentoo-installer-keyboard", render_dconf_keyboard(system), 0o644))
        return files

    def configure_identity(self, manifest: InstallManifest) -> None:
        accounts, system = manifest.accounts, manifest.system
        keymap = self.resolve_console_keymap(manifest)
        self.write("etc/hostname", accounts.hostname + "\n")
        self.write("etc/conf.d/hostname", f'hostname="{accounts.hostname}"\n')
        self.write("etc/hosts", render_hosts(accounts.hostname, system.domain, system.hosts_extra))
        locales = generated_locales(system)
        supported = "" if self.dry_run else self.read_text("usr/share/i18n/SUPPORTED")
        self.write("etc/locale.gen", "".join(locale_gen_line(name, supported) + "\n" for name in locales))
        self.write("etc/env.d/02locale", f"LANG={shell_value(system.locale)}\n")
        # systemd, its user manager and localed read /etc/locale.conf, not env.d.
        self.write("etc/locale.conf", f"LANG={system.locale}\n")
        self.write("etc/conf.d/keymaps", f'keymap="{keymap}"\n')
        vconsole = f"KEYMAP={keymap}\n" + (f"FONT={system.console_font}\n" if system.console_font else "")
        self.write("etc/vconsole.conf", vconsole)
        if system.console_font:
            self.write("etc/conf.d/consolefont", f'consolefont="{system.console_font}"\n')
        if system.rtc_local:
            # The hardware clock keeps local time (Windows dual boot); hwclock and systemd read adjtime.
            self.write("etc/adjtime", "0.0 0 0.0\n0\nLOCAL\n")
            self.write("etc/conf.d/hwclock", 'clock="local"\n')
        for path, content, mode in self.keyboard_files(manifest):
            self.write(path, content, mode)
        zone = self.path(f"usr/share/zoneinfo/{system.timezone}")
        if not self.dry_run and not zone.is_file():
            raise ConfigurationError(f"Timezone is not installed in the target: {system.timezone}")
        localtime = self.path("etc") / "localtime"
        if not self.dry_run:
            localtime.parent.mkdir(parents=True, exist_ok=True)
            localtime.unlink(missing_ok=True)
            localtime.symlink_to(f"/usr/share/zoneinfo/{system.timezone}")
        self.write("etc/timezone", system.timezone + "\n")
        self.chroot(("locale-gen",))
        self.chroot(("env-update",))
        self.check_locales(system, locales)

    def check_locales(self, system: System, locales: Sequence[str]) -> None:
        """After locale-gen, `locale -a` must list what was asked for. A problem is a warning: the system still installs."""
        if system.locale in {"C", "POSIX"}:
            self.notify(f"warning: the locale {system.locale} is not a language: programs show English text and cannot display accents or other scripts. Choose a UTF-8 locale for daily use")
        if not locales:
            return
        try:
            listed = self.chroot(("locale", "-a"), capture=True).stdout
        except RunnerError as exc:
            self.notify(f"warning: the generated locales could not be checked ({exc})")
            return
        have = {normalised_locale(line.strip()) for line in listed.splitlines() if line.strip()}
        missing = [name for name in locales if normalised_locale(name) not in have]
        if missing:
            self.notify("warning: locale-gen did not produce " + ", ".join(missing) + "; those languages will not be available (see /etc/locale.gen)")

    def configure_machine_id(self, manifest: InstallManifest) -> None:
        """The Handbook's machine-id step (non-fatal): OpenRC uses dbus-uuidgen once dbus is there, systemd sets it up and applies its presets."""
        if manifest.system.init == "openrc":
            if not self.is_installed("sys-apps/dbus"):
                return
            steps: list[tuple[str, ...]] = [("dbus-uuidgen", "--ensure=/etc/machine-id")]
        else:
            steps = [("systemd-machine-id-setup",), ("systemctl", "preset-all", "--preset-mode=enable-only")]
        for step in steps:
            try:
                self.chroot(step)
            except RunnerError as exc:
                self.notify(f"warning: {' '.join(step)} failed ({exc}); the system sets this up by itself at first boot")

    def configure_sshd(self, manifest: InstallManifest) -> None:
        """The remote-login policy, written only when the user wants SSH (the service itself is enabled with the other base services)."""
        if manifest.options.ssh:
            self.write("etc/ssh/sshd_config.d/10-installer.conf", render_sshd_config(manifest.accounts))

    def configure_journal(self, manifest: InstallManifest) -> None:
        """systemd: keep the journal across reboots (valuable for repair) by creating /var/log/journal."""
        if manifest.system.init == "systemd" and manifest.options.journal_persistent and not self.dry_run:
            self._mkdirs(self.path("var/log/journal"))

    # ----- the system tools of the Handbook ---------------------------------------------------------

    def cron_daemon(self, manifest: InstallManifest) -> str:
        """The cron daemon to install ('' for none). An OpenRC syslog-ng needs one for logrotate even when none was chosen."""
        options = manifest.options
        if options.cron_enabled:
            return options.cron
        return "cronie" if self.wants_cron(manifest) else ""

    def system_tool_atoms(self, manifest: InstallManifest) -> list[str]:
        """Packages of the choices on the tools tab: administrator tool, ssh, time sync, cron, logger and the optional tools."""
        system, options = manifest.system, manifest.options
        atoms = [PRIVILEGE_ATOMS[manifest.accounts.privilege]] if manifest.accounts.privilege in PRIVILEGE_ATOMS else []
        if options.ssh:
            atoms.append("net-misc/openssh")
        if options.timesync == "chrony":
            atoms.append("net-misc/chrony")
        cron = self.cron_daemon(manifest)
        if cron:
            atoms.append(CRON_ATOMS[cron])
        if options.file_indexing or options.mlocate:
            atoms.append("sys-apps/mlocate")
        if options.shell_completion:
            atoms.append("app-shells/bash-completion")
        if options.gentoolkit:
            atoms.append("app-portage/gentoolkit")
        if options.eclean_kernel:
            atoms.append("app-admin/eclean-kernel")
        if options.logger_enabled:
            if system.init == "openrc":
                atoms.extend(LOGGER_ATOMS[options.logger])
            else:
                atoms.append("sys-apps/systemd")
        return atoms

    def configure_cron_daemon(self, manifest: InstallManifest) -> None:
        """The first-run step some cron daemons need (non-fatal): dcron reads /etc/crontab once, fcron builds its tables."""
        step = {"dcron": ("crontab", "/etc/crontab"), "fcron": ("emerge", "--config", "sys-process/fcron")}.get(self.cron_daemon(manifest))
        if step is None:
            return
        try:
            self.chroot(step)
        except RunnerError as exc:
            self.notify(f"warning: {' '.join(step)} failed ({exc}); run it once after the first start")

    def wants_cron(self, manifest: InstallManifest) -> bool:
        """logrotate runs from cron, so an OpenRC syslog-ng brings a cron daemon with it (sysklogd and metalog rotate their own logs)."""
        options = manifest.options
        return options.cron_enabled or (options.logger == "syslog-ng" and manifest.system.init == "openrc")

    # ----- services ---------------------------------------------------------------------------------

    def service_specs(self, manifest: InstallManifest, plan: object | None = None, phase: str = "all") -> list[tuple[str, str, bool]]:
        """(service, runlevel, optional) triples; optional services are skipped when the package is missing."""
        system, options, network = manifest.system, manifest.options, manifest.network
        plan = self._plan(plan)
        base: list[tuple[str, str, bool]] = []
        desktop: list[tuple[str, str, bool]] = []
        if network.manager == "networkmanager":
            base.append(("NetworkManager", "default", False))
        elif network.manager == "dhcpcd":
            base.append(("dhcpcd", "default", False))
        elif network.manager == "systemd-networkd":
            base.extend((("systemd-networkd", "default", False), ("systemd-resolved", "default", False)))
        if options.ssh:
            base.append(("sshd", "default", False))
        if options.timesync == "chrony":
            base.append(("chronyd", "default", False))
        elif options.timesync == "timesyncd":
            base.append(("systemd-timesyncd", "default", False))
        cron = self.cron_daemon(manifest)
        if cron:
            base.append((cron, "default", cron == "bcron"))  # bcron's service name is not verified: a miss is a warning
        if options.logger_enabled and system.init == "openrc":
            base.append((LOGGER_SERVICES[options.logger], "default", False))
        if network.manager == "netifrc":
            interface = network.interface
            if not re.fullmatch(r"[A-Za-z0-9_]+", interface):
                raise ConfigurationError("netifrc requires a valid network interface name")
            base.append((f"net.{interface}", "default", False))
        if system.init == "openrc" and (system.desktop != "minimal" or options.bluetooth or options.printing):
            # Without NetworkManager nothing else pulls the system bus into a runlevel.
            desktop.append(("dbus", "default", True))
        if system.init == "openrc" and system.desktop != "minimal":
            desktop.append(("elogind", "boot", True))
        if options.bluetooth:
            desktop.append(("bluetooth", "default", True))
        if options.printing:
            desktop.append(("cupsd" if system.init == "openrc" else "cups", "default", True))
        if system.console_font and system.init == "openrc":
            desktop.append(("consolefont", "boot", True))
        if system.desktop != "minimal":
            manager = {"kde": "sddm", "gnome": "gdm", "xfce": "lightdm"}[system.desktop]
            desktop.append(("display-manager" if system.init == "openrc" else manager, "default", True))
        for item in [*_plan_list(plan, "services"), *_plan_list(plan, "optional_services")]:
            name, runlevel = (item, "default") if isinstance(item, str) else (item[0], item[1])
            if (str(name), str(runlevel), True) not in desktop:
                desktop.append((str(name), str(runlevel), True))
        if phase == "base":
            return base
        if phase == "desktop":
            return desktop
        return [*base, *desktop]

    def service_installed(self, name: str, init: str) -> bool:
        if self.dry_run:
            return True
        if init == "openrc":
            return self.path(f"etc/init.d/{name}").exists()
        # A timer or a socket is named with its suffix (fstrim.timer); a plain name is a service.
        unit = name if name.endswith((".service", ".timer", ".socket", ".path", ".target")) else f"{name}.service"
        return any(self.path(f"{prefix}/{unit}").exists() for prefix in ("usr/lib/systemd/system", "lib/systemd/system", "etc/systemd/system"))

    def configure_services(self, manifest: InstallManifest, plan: object | None = None, phase: str = "all") -> list[str]:
        """Enable services; returns warnings for optional services whose package is not installed."""
        system = manifest.system
        warnings: list[str] = []
        if phase in {"all", "base"} or (phase == "desktop" and system.init == "openrc"):
            self.configure_machine_id(manifest)  # before the installer's own enables; on OpenRC dbus may arrive with the desktop
        if phase in {"all", "desktop"} and system.desktop != "minimal":
            manager = {"kde": "sddm", "gnome": "gdm", "xfce": "lightdm"}[system.desktop]
            if system.init == "openrc" and self.service_installed("display-manager", "openrc"):
                self.write("etc/conf.d/display-manager", f'DISPLAYMANAGER="{manager}"\n')
        for name, runlevel, optional in self.service_specs(manifest, plan, phase):
            if name.startswith("net.") and system.init == "openrc" and not self.dry_run:
                source = self.path("etc/init.d/net.lo")
                link = self.path(f"etc/init.d/{name}")
                if not source.is_file():
                    raise ConfigurationError("netifrc init script is missing")
                if not link.exists() and not link.is_symlink():
                    link.symlink_to("net.lo")
            if optional and not self.service_installed(name, system.init):
                warnings.append(f"Service {name} was not enabled because its package is not installed")
                self.notify("warning: " + warnings[-1])
                continue
            if system.init == "openrc":
                self.chroot(("rc-update", "add", name, runlevel))
            else:
                self.chroot(("systemctl", "enable", name))
        return warnings
