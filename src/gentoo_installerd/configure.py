from __future__ import annotations

import ipaddress
import os
import re
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from gentoo_installer.manifest import InstallManifest

from .runner import CommandResult, Runner, RunnerError
from .storage import LayoutEntry


class ConfigurationError(RuntimeError):
    pass


ATOM = re.compile(r"[A-Za-z0-9_+.-]+/[A-Za-z0-9_+.-]+(?::[A-Za-z0-9_.-]+)?")


def shell_value(value: str) -> str:
    return shlex.quote(value)


def ninja_opts_from_make_opts(make_opts: str) -> str:
    """Keep Ninja bounded even when Portage exports an empty NINJAOPTS."""
    try:
        tokens = shlex.split(make_opts)
    except ValueError as exc:
        raise ConfigurationError("Invalid MAKEOPTS") from exc
    jobs = 2
    load: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-j", "--jobs", "-l", "--load-average"} and index + 1 < len(tokens):
            candidate = tokens[index + 1]
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", candidate):
                token = token + ("=" if token.startswith("--") else "") + candidate
                index += 1
        if token in {"-j", "--jobs", "-j0", "--jobs=0"}:
            jobs = 2  # An unbounded make request must not unbound Ninja.
        match = re.fullmatch(r"(?:-j|--jobs=)([1-9][0-9]*)", token)
        if match:
            jobs = int(match.group(1))
        match = re.fullmatch(r"(?:-l|--load-average=)([0-9]+(?:\.[0-9]+)?)", token)
        if match:
            load = match.group(1)
        index += 1
    return f"-j{jobs}" + (f" -l{load}" if load is not None else "")


def render_make_conf(manifest: InstallManifest) -> str:
    p = manifest.portage
    values: list[tuple[str, str]] = [
        ("COMMON_FLAGS", p.cflags),
        ("CFLAGS", p.cflags),
        ("CXXFLAGS", p.cxxflags),
        ("RUSTFLAGS", p.rustflags),
        ("MAKEOPTS", p.make_opts),
        ("NINJAOPTS", ninja_opts_from_make_opts(p.make_opts)),
        ("EMERGE_DEFAULT_OPTS", p.emerge_opts),
        ("ACCEPT_LICENSE", p.accept_license),
        ("GENTOO_MIRRORS", p.mirror),
        ("USE", " ".join(p.use_flags)),
        ("FEATURES", " ".join([*p.features, *(["getbinpkg", "binpkg-request-signature"] if p.binhost else [])])),
        ("CPU_FLAGS_X86", p.cpu_flags),
        ("VIDEO_CARDS", p.video_cards),
        ("GRUB_PLATFORMS", "pc efi-64"),
    ]
    if p.binhost:
        values.append(("PORTAGE_BINHOST", p.binhost_url))
    lines: list[str] = []
    emitted: set[str] = set()
    for key, value in values:
        if key in emitted:
            continue
        emitted.add(key)
        lines.append(f"{key}={shell_value(value)}")
    return "\n".join(lines) + "\n"


def render_package_use(package_use: Mapping[str, Sequence[str]]) -> str:
    lines: list[str] = []
    for atom, flags in sorted(package_use.items()):
        if not ATOM.fullmatch(atom) or any(not re.fullmatch(r"-?[A-Za-z0-9_+@-]+", flag) for flag in flags):
            raise ConfigurationError(f"Invalid package.use entry: {atom}")
        lines.append(f"{atom} {' '.join(flags)}".rstrip())
    return "\n".join(lines) + ("\n" if lines else "")


def render_fstab(layout: Sequence[LayoutEntry], uuids: Mapping[str, str]) -> str:
    lines = ["# <file system> <mount point> <type> <options> <dump> <pass>"]
    for entry in sorted(layout, key=lambda item: (item.mountpoint != "/", item.mountpoint)):
        uuid = uuids.get(entry.device, "")
        source = f"UUID={uuid}" if uuid else entry.device
        if entry.filesystem == "swap":
            lines.append(f"{source} none swap sw 0 0")
        elif entry.mountpoint:
            pass_number = 1 if entry.mountpoint == "/" else 2
            options = "defaults,noatime" if entry.filesystem != "vfat" else "defaults,umask=0077"
            lines.append(f"{source} {entry.mountpoint} {entry.filesystem} {options} 0 {pass_number}")
    return "\n".join(lines) + "\n"


def render_hosts(hostname: str) -> str:
    return f"127.0.0.1\tlocalhost\n::1\tlocalhost\n127.0.1.1\t{hostname}.localdomain {hostname}\n"


def render_systemd_network(manifest: InstallManifest) -> str:
    network = manifest.network
    lines = ["[Match]", "Name=en* eth*", "", "[Network]"]
    if network.dhcp:
        lines.append("DHCP=yes")
    else:
        lines.extend((f"Address={network.address}", f"Gateway={network.gateway}"))
        lines.extend(f"DNS={server}" for server in network.dns)
    return "\n".join(lines) + "\n"


def render_netifrc(manifest: InstallManifest) -> str:
    network = manifest.network
    interface = network.interface
    if not re.fullmatch(r"[A-Za-z0-9_]+", interface):
        raise ConfigurationError("netifrc requires a valid network interface name")
    if network.dhcp:
        return f'config_{interface}="dhcp"\n'
    dns = " ".join(network.dns)
    return f'config_{interface}="{network.address}"\nroutes_{interface}="default via {network.gateway}"\ndns_servers_{interface}="{dns}"\n'


class Configurer:
    def __init__(self, runner: Runner, target_root: str | Path, *, dry_run: bool = False,
                 companion_bundle: str | Path = "/usr/local/share/gentoo-installer") -> None:
        self.runner = runner
        self.root = Path(target_root)
        self.dry_run = dry_run
        self.companion_bundle = Path(companion_bundle)

    def path(self, relative: str | Path) -> Path:
        value = str(relative)
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ConfigurationError("Invalid target path")
        pure = PurePosixPath(value)
        if ".." in pure.parts:
            raise ConfigurationError(f"Target path escapes installation root: {value}")
        candidate = self.root / value.lstrip("/")
        root_resolved = self.root.resolve()
        resolved = candidate.resolve()
        if os.path.commonpath((str(root_resolved), str(resolved))) != str(root_resolved):
            raise ConfigurationError(f"Target path escapes installation root: {value}")
        return resolved

    def write(self, relative: str | Path, content: str, mode: int = 0o644) -> None:
        path = self.path(relative)
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path = self.path(relative)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        try:
            temporary_path.chmod(mode)
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def configure_portage(self, manifest: InstallManifest) -> None:
        self.write("etc/portage/make.conf", render_make_conf(manifest))
        package_use = {atom: list(flags) for atom, flags in manifest.portage.package_use.items()}
        if manifest.system.initramfs == "dracut":
            # Distribution kernels with initramfs require this installkernel
            # hook before the kernel package is merged.
            flags = package_use.setdefault("sys-kernel/installkernel", [])
            flags[:] = [flag for flag in flags if flag != "-dracut"]
            if "dracut" not in flags:
                flags.append("dracut")
        if manifest.options.microcode:
            for atom, required in (
                ("sys-kernel/linux-firmware", ("initramfs", "redistributable")),
                ("sys-firmware/intel-microcode", ("initramfs",)),
            ):
                flags = package_use.setdefault(atom, [])
                flags[:] = [flag for flag in flags if flag not in {f"-{name}" for name in required}]
                flags.extend(name for name in required if name not in flags)
        if manifest.system.uki or manifest.system.bootloader == "systemd-boot":
            atom = "sys-apps/systemd" if manifest.system.init == "systemd" else "sys-apps/systemd-utils"
            required = ["boot", *(["kernel-install"] if manifest.system.init == "openrc" else []),
                        *(["ukify"] if manifest.system.uki else [])]
            package_use[atom] = [flag for flag in package_use.get(atom, []) if flag not in ("-boot", "-kernel-install", "-ukify")]
            package_use[atom].extend(flag for flag in required if flag not in package_use[atom])
        if manifest.options.install_hwmonux:
            # Minimal profiles do not enable the GUI flags required by
            # PySide/Qt, even when a graphical companion is selected.
            for atom, required in (("sys-apps/i2c-tools", "python"),
                                   ("dev-qt/qtbase", "opengl"),
                                   ("dev-qt/qttools", "opengl"),
                                   ("x11-libs/libxkbcommon", "X")):
                flags = package_use.setdefault(atom, [])
                flags[:] = [flag for flag in flags if flag != f"-{required}"]
                if required not in flags:
                    flags.append(required)
        if manifest.options.install_hwmonux or manifest.options.install_portagehub:
            # Both companions can pull in Mesa[X], which requires libglvnd[X]
            # on the minimal profile.
            flags = package_use.setdefault("media-libs/libglvnd", [])
            flags[:] = [flag for flag in flags if flag != "-X"]
            if "X" not in flags:
                flags.append("X")
        if manifest.network.manager == "networkmanager":
            # The selected NetworkManager build uses wpa_supplicant's D-Bus
            # interface when its Wi-Fi support is enabled.
            flags = package_use.setdefault("net-wireless/wpa_supplicant", [])
            flags[:] = [flag for flag in flags if flag != "-dbus"]
            if "dbus" not in flags:
                flags.append("dbus")
        if manifest.storage.lvm:
            # lvm2[-lvm] installs only device-mapper, without the LVM tools
            # needed to activate the root volume in the initramfs.
            flags = package_use.setdefault("sys-fs/lvm2", [])
            flags[:] = [flag for flag in flags if flag != "-lvm"]
            if "lvm" not in flags:
                flags.append("lvm")
        if manifest.options.install_portagehub and manifest.system.init == "systemd":
            # PortageHub's polkit dependency needs systemd's policykit support
            # even on the minimal systemd profile.
            flags = package_use.setdefault("sys-apps/systemd", [])
            flags[:] = [flag for flag in flags if flag != "-policykit"]
            if "policykit" not in flags:
                flags.append("policykit")
        if manifest.system.init == "systemd" and manifest.storage.luks:
            # Dracut's systemd-cryptsetup module needs the helper installed by
            # systemd[cryptsetup]; the systemd stage3 does not enable it.
            flags = package_use.setdefault("sys-apps/systemd", [])
            flags[:] = [flag for flag in flags if flag != "-cryptsetup"]
            if "cryptsetup" not in flags:
                flags.append("cryptsetup")
        if manifest.system.desktop != "minimal":
            # Current binary libwebp[tiff] and tiff[webp] depend on each other.
            flags = package_use.setdefault("media-libs/libwebp", [])
            flags[:] = [flag for flag in flags if flag != "tiff"]
            if "-tiff" not in flags:
                flags.append("-tiff")
        self.write("etc/portage/package.use/installer", render_package_use(package_use))
        package_licenses: list[str] = []
        if manifest.options.firmware or manifest.options.microcode:
            package_licenses.append("sys-kernel/linux-firmware linux-fw-redistributable")
        if manifest.options.microcode:
            package_licenses.append("sys-firmware/intel-microcode intel-ucode")
        self.write("etc/portage/package.license/installer", "\n".join(package_licenses) + ("\n" if package_licenses else ""))
        if manifest.portage.binhost:
            self.write("etc/portage/binrepos.conf/gentoo.conf", f"[gentoo]\npriority = 9999\nsync-uri = {manifest.portage.binhost_url}\n")
        if manifest.portage.rsync_mirror:
            if not manifest.portage.rsync_mirror.startswith(("rsync://", "https://")):
                raise ConfigurationError("Repository sync mirror must use rsync or HTTPS")
            self.write("etc/portage/repos.conf/gentoo.conf", f"[gentoo]\nlocation = /var/db/repos/gentoo\nsync-type = {'rsync' if manifest.portage.rsync_mirror.startswith('rsync:') else 'git'}\nsync-uri = {manifest.portage.rsync_mirror}\nauto-sync = yes\n")

    def configure_identity(self, manifest: InstallManifest) -> None:
        accounts, system = manifest.accounts, manifest.system
        self.write("etc/hostname", accounts.hostname + "\n")
        self.write("etc/conf.d/hostname", f'hostname="{accounts.hostname}"\n')
        self.write("etc/hosts", render_hosts(accounts.hostname))
        self.write("etc/locale.gen", f"{system.locale} UTF-8\n")
        self.write("etc/env.d/02locale", f"LANG={shell_value(system.locale)}\n")
        self.write("etc/conf.d/keymaps", f'keymap="{system.keymap}"\n')
        self.write("etc/vconsole.conf", f"KEYMAP={system.keymap}\n")
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

    def configure_accounts(self, manifest: InstallManifest) -> None:
        a = manifest.accounts
        groups = ",".join(a.groups)
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", group) for group in a.groups):
            raise ConfigurationError("Account groups contain an invalid name")
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
        self.chroot(("env-update",))

    def configure_network(self, manifest: InstallManifest) -> None:
        n = manifest.network
        if n.manager == "networkmanager":
            self.write("etc/NetworkManager/conf.d/dhcp.conf", "[main]\ndhcp=internal\n")
            if n.wifi_ssid:
                key = "[connection]\ntype=wifi\nautoconnect=true\n\n[wifi]\nmode=infrastructure\nssid=" + n.wifi_ssid + "\n\n[wifi-security]\nkey-mgmt=wpa-psk\npsk=" + n.wifi_password + "\n\n[ipv4]\nmethod=" + ("auto" if n.dhcp else "manual") + "\n"
                if not n.dhcp:
                    key += f"address1={n.address},{n.gateway}\ndns={';'.join(n.dns)};\n"
                self.write("etc/NetworkManager/system-connections/installer-wifi.nmconnection", key, 0o600)
            elif not n.dhcp:
                if n.interface and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", n.interface):
                    raise ConfigurationError("Invalid NetworkManager interface name")
                try:
                    address = ipaddress.ip_interface(n.address)
                    gateway = ipaddress.ip_address(n.gateway)
                    dns = [ipaddress.ip_address(server) for server in n.dns]
                except ValueError as exc:
                    raise ConfigurationError("Invalid static NetworkManager address or DNS server") from exc
                if address.version != gateway.version:
                    raise ConfigurationError("Static address and gateway must use the same IP family")
                lines = ["[connection]", "id=Installer wired static", f"uuid={uuid.uuid4()}",
                         "type=ethernet", "autoconnect=true"]
                if n.interface:
                    lines.append(f"interface-name={n.interface}")
                lines.extend(("", "[ethernet]"))
                for version in (4, 6):
                    servers = [str(server) for server in dns if server.version == version]
                    method = "manual" if address.version == version else ("auto" if servers else "disabled")
                    lines.extend(("", f"[ipv{version}]", f"method={method}"))
                    if address.version == version:
                        lines.extend((f"address1={address}", f"gateway={gateway}"))
                    if servers:
                        lines.append(f"dns={';'.join(servers)};")
                        if address.version != version:
                            lines.append("ignore-auto-dns=true")
                self.write("etc/NetworkManager/system-connections/installer-wired.nmconnection",
                           "\n".join(lines) + "\n", 0o600)
        elif n.manager == "dhcpcd":
            content = "" if n.dhcp else f"interface eth0\nstatic ip_address={n.address}\nstatic routers={n.gateway}\nstatic domain_name_servers={' '.join(n.dns)}\n"
            self.write("etc/dhcpcd.conf", content)
        elif n.manager == "netifrc":
            self.write("etc/conf.d/net", render_netifrc(manifest))
        else:
            self.write("etc/systemd/network/20-wired.network", render_systemd_network(manifest))
        if n.dns and n.manager not in {"systemd-networkd", "dhcpcd"}:
            self.write("etc/resolv.conf", "".join(f"nameserver {server}\n" for server in n.dns))

    def early_boot_atoms(self, manifest: InstallManifest) -> list[str]:
        atoms: list[str] = []
        if manifest.options.firmware or manifest.options.microcode:
            atoms.append("sys-kernel/linux-firmware")
        if manifest.options.microcode:
            atoms.append("sys-firmware/intel-microcode")
        return atoms

    def package_atoms(self, manifest: InstallManifest) -> list[str]:
        system, options = manifest.system, manifest.options
        atoms: list[str] = []
        network_package = {
            "networkmanager": "net-misc/networkmanager",
            "dhcpcd": "net-misc/dhcpcd",
            "netifrc": "net-misc/netifrc",
        }.get(manifest.network.manager)
        if network_package:
            atoms.append(network_package)
        atoms.extend(self.early_boot_atoms(manifest))
        if system.desktop == "kde":
            atoms.extend(("kde-plasma/plasma-meta", "kde-apps/kde-apps-meta", "gui-apps/sddm", "x11-base/xorg-server", "sys-power/power-profiles-daemon"))
        elif system.desktop == "gnome":
            atoms.extend(("gnome-base/gnome", "gnome-base/gdm", "x11-base/xorg-server"))
        elif system.desktop == "xfce":
            atoms.extend(("xfce-base/xfce4-meta", "x11-base/xorg-server", "x11-misc/lightdm", "x11-misc/lightdm-gtk-greeter"))
        if system.desktop != "minimal" and system.init == "openrc":
            atoms.append("gui-libs/display-manager-init")
        if options.pipewire:
            atoms.append("media-video/pipewire")
        if options.bluetooth:
            atoms.extend(("net-wireless/bluez", "net-wireless/blueman"))
        if options.printing:
            atoms.append("net-print/cups")
        if options.flatpak:
            atoms.append("sys-apps/flatpak")
        if options.ssh:
            atoms.append("net-misc/openssh")
        if options.chrony:
            atoms.append("net-misc/chrony")
        if options.cron:
            atoms.append("sys-process/cronie")
        if options.file_indexing:
            atoms.append("sys-apps/mlocate")
        if options.shell_completion:
            atoms.append("app-shells/bash-completion")
        if options.logger:
            atoms.append("app-admin/syslog-ng" if system.init == "openrc" else "sys-apps/systemd")
        if options.install_hwmonux:
            atoms.append("app-misc/hwmonux")
        if options.install_portagehub:
            atoms.append("app-portage/portagehub")
        if system.secure_boot:
            atoms.append("app-crypt/sbsigntools")
        if system.uki:
            atoms.append("sys-apps/systemd" if system.init == "systemd" else "sys-apps/systemd-utils")
        atoms.extend(options.extra_packages)
        atoms.append("app-admin/sudo" if manifest.accounts.privilege == "sudo" else "app-admin/doas")
        return list(dict.fromkeys(atoms))

    def configure_services(self, manifest: InstallManifest) -> None:
        system, options = manifest.system, manifest.options
        services: list[str] = []
        if manifest.network.manager == "networkmanager":
            services.append("NetworkManager")
        elif manifest.network.manager == "dhcpcd":
            services.append("dhcpcd")
        elif manifest.network.manager == "systemd-networkd":
            services.extend(("systemd-networkd", "systemd-resolved"))
        if options.bluetooth:
            services.append("bluetooth")
        if options.printing:
            services.append("cupsd" if system.init == "openrc" else "cups")
        if options.ssh:
            services.append("sshd" if system.init == "openrc" else "sshd")
        if options.chrony:
            services.append("chronyd" if system.init == "openrc" else "chronyd")
        if options.cron:
            services.append("cronie")
        if options.logger and system.init == "openrc":
            services.append("syslog-ng")
        if manifest.network.manager == "netifrc":
            # netifrc needs one net.<interface> OpenRC service per link.
            interface = manifest.network.interface
            if not re.fullmatch(r"[A-Za-z0-9_]+", interface):
                raise ConfigurationError("netifrc requires a valid network interface name")
            if not self.dry_run:
                source = self.path("etc/init.d/net.lo")
                link = self.path(f"etc/init.d/net.{interface}")
                if not source.is_file():
                    raise ConfigurationError("netifrc init script is missing")
                if not link.exists() and not link.is_symlink():
                    link.symlink_to("net.lo")
            services.append(f"net.{interface}")
        if system.desktop != "minimal":
            manager = {"kde": "sddm", "gnome": "gdm", "xfce": "lightdm"}[system.desktop]
            if system.init == "openrc":
                self.write("etc/conf.d/display-manager", f'DISPLAYMANAGER="{manager}"\n')
                services.append("display-manager")
            else:
                services.append(manager)
        for service in services:
            if system.init == "openrc":
                self.chroot(("rc-update", "add", service, "default"))
            else:
                self.chroot(("systemctl", "enable", service))

    def install_packages(self, manifest: InstallManifest) -> None:
        self.configure_companion_overlay(manifest)
        atoms = self.package_atoms(manifest)
        if atoms:
            self.chroot(("emerge", "--noreplace", *atoms))

    def configure_initramfs(self, manifest: InstallManifest, root_device: str,
                            luks_device: str | None = None) -> None:
        """Persist the root discovery arguments before a kernel/initramfs is built."""
        root_uuid = "" if self.dry_run else self.runner.run(
            ("blkid", "-s", "UUID", "-o", "value", root_device)).stdout.strip()
        if not self.dry_run and not re.fullmatch(r"[0-9a-fA-F-]{36}", root_uuid):
            raise ConfigurationError("Root filesystem has no valid UUID for stable boot configuration")
        arguments = [f"root={'UUID=' + root_uuid if root_uuid else root_device}", "rw"]
        modules: list[str] = []
        if manifest.storage.luks:
            if not luks_device:
                raise ConfigurationError("LUKS backing device is required for initramfs configuration")
            uuid = "dry-run-luks-uuid" if self.dry_run else self.runner.run(
                ("blkid", "-s", "UUID", "-o", "value", luks_device)).stdout.strip()
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid) and not self.dry_run:
                raise ConfigurationError("LUKS backing device has no valid UUID")
            # Dracut uses rd.luks.uuid to select the backing device. The
            # rd.luks.name mapping only names a device selected that way.
            arguments.extend((f"rd.luks.uuid={uuid}", f"rd.luks.name={uuid}=gentoo-root"))
            modules.append("crypt")
        if manifest.storage.lvm:
            arguments.append("rd.lvm.lv=gentoo/root")
            modules.append("lvm")
        if manifest.storage.raid_level:
            modules.append("mdraid")
        cmdline = " ".join(arguments)
        self.write("etc/kernel/cmdline", cmdline + "\n")
        # The installer runs dracut inside a chroot before the target fstab
        # exists. A host-only image would describe the live installer's root.
        dracut_config = 'hostonly="no"\n'
        if modules:
            dracut_config += 'add_dracutmodules+=" ' + " ".join(modules) + ' "\n'
        self.write("etc/dracut.conf.d/gentoo-installer.conf", dracut_config)
        if manifest.system.bootloader == "grub" and manifest.storage.luks:
            path = self.path("etc/default/grub")
            old = "" if self.dry_run or not path.exists() else path.read_text(encoding="utf-8")
            self.write("etc/default/grub", old.rstrip("\n") + "\n" +
                       f"GRUB_CMDLINE_LINUX={shell_value(' '.join(arguments[2:]))}\n")

    def _kernel_cmdline(self, root_device: str) -> str:
        path = self.path("etc/kernel/cmdline")
        return path.read_text(encoding="utf-8").strip() if not self.dry_run and path.is_file() else f"root={root_device} rw"

    def configure_companion_overlay(self, manifest: InstallManifest) -> None:
        if not (manifest.options.install_hwmonux or manifest.options.install_portagehub):
            return
        if self.dry_run:
            return
        overlay = self.companion_bundle / "overlay"
        distfiles = self.companion_bundle / "distfiles"
        if not (overlay / "profiles/repo_name").is_file():
            raise ConfigurationError(f"Live companion overlay is missing: {overlay}")
        destination = self.path("var/db/repos/gentoo-live-installer")
        shutil.copytree(overlay, destination, dirs_exist_ok=True, symlinks=False)
        for atom, basename in (("app-misc/hwmonux", "hwmonux-1.0.0.tar.zst"),
                               ("app-portage/portagehub", "portagehub-0.4.0.tar.zst")):
            enabled = manifest.options.install_hwmonux if atom.startswith("app-misc/") else manifest.options.install_portagehub
            if enabled:
                source = distfiles / basename
                if not source.is_file():
                    raise ConfigurationError(f"Live companion source archive is missing: {source}")
                cache = self.path("var/cache/distfiles")
                cache.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, cache / basename)
        if manifest.options.install_hwmonux:
            # liquidctl is available for amd64 in the pinned Gentoo tree, but
            # carries only the testing keyword; HWMonux uses its USB sensors.
            self.write("etc/portage/package.accept_keywords/gentoo-installer-companions",
                       "app-misc/liquidctl ~amd64\ndev-python/hidapi ~amd64\n")
        self.write("etc/portage/repos.conf/gentoo-live-installer.conf",
                   "[gentoo-live-installer]\nlocation = /var/db/repos/gentoo-live-installer\nmasters = gentoo\nauto-sync = no\n")

    @staticmethod
    def efi_mountpoint(manifest: InstallManifest) -> str:
        if manifest.storage.mode == "manual" and any(part.mountpoint == "/boot/efi" for part in manifest.storage.partitions):
            return "/boot/efi"
        return "/efi"

    def configure_bootloader(self, manifest: InstallManifest, root_device: str, efi_device: str | None = None) -> None:
        system, storage = manifest.system, manifest.storage
        efi_mountpoint = self.efi_mountpoint(manifest)
        efi_prefix = efi_mountpoint.lstrip("/")
        cmdline = self._kernel_cmdline(root_device)
        if system.secure_boot and storage.firmware != "uefi":
            raise ConfigurationError("Secure Boot requires UEFI firmware")
        if system.secure_boot and system.bootloader != "grub" and not system.uki:
            raise ConfigurationError("Secure Boot with systemd-boot or EFI stub requires a signed UKI")
        if system.uki and storage.firmware != "uefi":
            raise ConfigurationError("A Unified Kernel Image requires UEFI firmware")
        if system.uki:
            self._build_uki(manifest, root_device)
        elif system.bootloader != "grub":
            self._copy_boot_files(manifest)
        if system.bootloader == "grub":
            if storage.firmware == "uefi":
                self.chroot(("grub-install", "--target=x86_64-efi", f"--efi-directory={efi_mountpoint}", "--bootloader-id=Gentoo"))
            else:
                self.chroot(("grub-install", "--target=i386-pc", storage.device))
            if system.uki:
                if not efi_device:
                    raise ConfigurationError("GRUB with UKI requires an EFI system partition")
                efi_uuid = "dry-run-efi-uuid" if self.dry_run else self.runner.run(
                    ("blkid", "-s", "UUID", "-o", "value", efi_device)).stdout.strip()
                if not self.dry_run and not re.fullmatch(r"[0-9a-fA-F-]+", efi_uuid):
                    raise ConfigurationError("EFI system partition has no valid UUID")
                self.write("etc/grub.d/40_custom", '#!/bin/sh\nexec tail -n +3 $0\n\n'
                           'menuentry "Gentoo Linux" {\n'
                           f'    search --no-floppy --fs-uuid --set=root {efi_uuid}\n'
                           '    chainloader /EFI/Linux/gentoo.efi\n}\n', 0o755)
            self.chroot(("grub-mkconfig", "-o", "/boot/grub/grub.cfg"))
        elif system.bootloader == "systemd-boot":
            self.chroot(("bootctl", f"--path={efi_mountpoint}", "install"))
            initrd = "initrd /initramfs\n" if system.initramfs != "none" and not system.uki else ""
            linux = "/EFI/Linux/gentoo.efi" if system.uki else "/vmlinuz"
            options = "" if system.uki else f"options {cmdline}\n"
            self.write(f"{efi_prefix}/loader/loader.conf", "default gentoo.conf\ntimeout 3\n")
            self.write(f"{efi_prefix}/loader/entries/gentoo.conf", f"title Gentoo Linux\nlinux {linux}\n{initrd}{options}")
        else:
            if not root_device.startswith("/dev/"):
                raise ConfigurationError("EFI stub requires a stable root device")
            disk, part = storage.device, "1"
            if efi_device and not self.dry_run:
                part = self.runner.run(("lsblk", "--noheadings", "--output", "PARTN", efi_device)).stdout.strip()
                parent = self.runner.run(("lsblk", "--noheadings", "--output", "PKNAME", efi_device)).stdout.strip()
                if not part.isdigit() or not re.fullmatch(r"[A-Za-z0-9._+-]+", parent):
                    raise ConfigurationError("Could not determine the EFI partition number and parent disk")
                disk = "/dev/" + parent
            loader = "\\EFI\\Linux\\gentoo.efi" if system.uki else "\\vmlinuz"
            command = ["efibootmgr", "--create", "--disk", disk, "--part", part, "--label", "Gentoo", "--loader", loader]
            if not system.uki:
                initrd = " initrd=\\initramfs" if system.initramfs != "none" else ""
                command.extend(("--unicode", f"{cmdline}{initrd}"))
            self.chroot(tuple(command))
        if system.secure_boot:
            key, certificate = Path(system.secure_boot_key), Path(system.secure_boot_certificate)
            if not self.dry_run and (not key.is_file() or not certificate.is_file()):
                raise ConfigurationError("Secure Boot signing key or certificate is missing")
            if not self.dry_run:
                self.write("root/secure-boot.key", key.read_text(), 0o600)
                self.write("root/secure-boot.crt", certificate.read_text(), 0o644)
            images: list[str] = []
            if system.uki:
                images.append(f"{efi_mountpoint}/EFI/Linux/gentoo.efi")
            if system.bootloader == "grub":
                images.append(f"{efi_mountpoint}/EFI/Gentoo/grubx64.efi")
            elif system.bootloader == "systemd-boot":
                images.extend((f"{efi_mountpoint}/EFI/systemd/systemd-bootx64.efi", f"{efi_mountpoint}/EFI/BOOT/BOOTX64.EFI"))
            try:
                for image in images:
                    signed = image + ".signed"
                    self.chroot(("sbsign", "--key", "/root/secure-boot.key", "--cert", "/root/secure-boot.crt", "--output", signed, image))
                    self.chroot(("sbverify", "--cert", "/root/secure-boot.crt", signed))
                    self.chroot(("mv", signed, image))
            finally:
                self.chroot(("rm", "-f", "/root/secure-boot.key", "/root/secure-boot.crt"))

    def _boot_images(self, manifest: InstallManifest) -> tuple[Path, Path | None]:
        boot = self.path("boot")
        kernels = sorted(boot.glob("vmlinuz-*"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not kernels:
            raise ConfigurationError("No installed kernel was found under /boot")
        initramfs = None
        if manifest.system.initramfs != "none":
            images = sorted([*boot.glob("initramfs-*.img"), *boot.glob("initrd-*")], key=lambda path: path.stat().st_mtime, reverse=True)
            if not images:
                raise ConfigurationError("No installed initramfs was found under /boot")
            initramfs = images[0]
        return kernels[0], initramfs

    def _build_uki(self, manifest: InstallManifest, root_device: str) -> None:
        efi_mountpoint = self.efi_mountpoint(manifest)
        efi_prefix = efi_mountpoint.lstrip("/")
        if self.dry_run:
            kernel, initramfs = "/boot/vmlinuz", "/boot/initramfs" if manifest.system.initramfs != "none" else None
        else:
            kernel_path, initramfs_path = self._boot_images(manifest)
            kernel, initramfs = "/boot/" + kernel_path.name, "/boot/" + initramfs_path.name if initramfs_path else None
            self.path(f"{efi_prefix}/EFI/Linux").mkdir(parents=True, exist_ok=True)
        command = ["ukify", "build", "--linux", kernel, "--cmdline", self._kernel_cmdline(root_device), "--output", f"{efi_mountpoint}/EFI/Linux/gentoo.efi"]
        if initramfs:
            command.extend(("--initrd", initramfs))
        self.chroot(tuple(command))

    def _copy_boot_files(self, manifest: InstallManifest) -> None:
        if self.dry_run:
            return
        kernel, initramfs = self._boot_images(manifest)
        efi = self.path(self.efi_mountpoint(manifest).lstrip("/"))
        efi.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copyfile(kernel, efi / "vmlinuz")
        if initramfs:
            shutil.copyfile(initramfs, efi / "initramfs")

    def chroot(self, argv: Sequence[str], *, stdin: str | None = None, secret: bool = False) -> CommandResult:
        return self.runner.run(("chroot", str(self.root), *argv), stdin=stdin, secret=secret)
