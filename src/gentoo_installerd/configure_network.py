"""NET: the target's network setup, NetworkManager keyfiles and live profile import."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import types
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from gentoo_installer.manifest import InstallManifest, Network

from .configure_base import ConfigurationError, ConfigurerBase


# Where a live profile's certificates may be read from (never arbitrary files such as /etc/shadow).
CERTIFICATE_ROOTS = ("/home/", "/root/", "/etc/NetworkManager/", "/etc/ssl/", "/etc/pki/", "/etc/ca-certificates/",
                     "/usr/share/ca-certificates/", "/usr/local/share/ca-certificates/")


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


def render_dhcpcd_static(manifest: InstallManifest) -> str:
    """The static block appended to the stock /etc/dhcpcd.conf."""
    network = manifest.network
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", network.interface):
        raise ConfigurationError("dhcpcd with a static address needs a valid network interface name")
    lines = ["", "# Static configuration written by the Gentoo live installer", f"interface {network.interface}",
             f"static ip_address={network.address}", f"static routers={network.gateway}"]
    if network.dns:
        lines.append(f"static domain_name_servers={' '.join(network.dns)}")
    return "\n".join(lines) + "\n"


def _keyfile_escape(value: str) -> str:
    """Escape a string for a NetworkManager keyfile (GKeyFile string rules)."""
    escaped = value.replace("\\", "\\\\")
    return "\\s" + escaped[1:] if escaped.startswith(" ") else escaped


def render_nm_ip_sections(network: Network | types.SimpleNamespace) -> list[str]:
    """[ipv4]/[ipv6] sections of a NetworkManager keyfile for the manifest's network settings."""
    if network.dhcp:
        return ["", "[ipv4]", "method=auto", "", "[ipv6]", "method=auto"]
    try:
        address = ipaddress.ip_interface(network.address)
        gateway = ipaddress.ip_address(network.gateway)
        dns = [ipaddress.ip_address(server) for server in network.dns]
    except ValueError as exc:
        raise ConfigurationError("Invalid static NetworkManager address or DNS server") from exc
    if address.version != gateway.version:
        raise ConfigurationError("Static address and gateway must use the same IP family")
    lines: list[str] = []
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
    return lines


def render_nm_wifi(network: Network, connection_uuid: str | None = None) -> str:
    """A NetworkManager keyfile for the Wi-Fi network chosen in the installer."""
    ssid, password = network.wifi_ssid, network.wifi_password
    if not ssid or any(ord(char) < 32 or ord(char) == 127 for char in ssid + password) or len(ssid.encode("utf-8")) > 32:
        raise ConfigurationError("The Wi-Fi network name or password is invalid")
    # The static address belongs to the wired link unless the user named a wireless interface.
    static = not network.dhcp and network.interface.startswith("wl")
    lines = ["[connection]", f"id={_keyfile_escape(ssid)}", f"uuid={connection_uuid or uuid.uuid4()}", "type=wifi",
             "autoconnect=true", "", "[wifi]", "mode=infrastructure", f"ssid={_keyfile_escape(ssid)}"]
    if password:
        lines.extend(("", "[wifi-security]", "key-mgmt=wpa-psk", f"psk={_keyfile_escape(password)}"))
    lines.extend(render_nm_ip_sections(network if static else types.SimpleNamespace(dhcp=True)))
    return "\n".join(lines) + "\n"


def render_nm_wired(network: Network, connection_uuid: str | None = None) -> str:
    """A NetworkManager keyfile with the static wired configuration."""
    interface = network.interface
    if interface and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface):
        raise ConfigurationError("Invalid NetworkManager interface name")
    lines = ["[connection]", "id=Installer wired static", f"uuid={connection_uuid or uuid.uuid4()}", "type=ethernet",
             "autoconnect=true"]
    if interface:
        lines.append(f"interface-name={interface}")
    lines.extend(("", "[ethernet]"))
    lines.extend(render_nm_ip_sections(network))
    return "\n".join(lines) + "\n"


# Profile types worth carrying to another machine; bridges, bonds, VLANs and tunnels describe the live setup.
_IMPORTABLE_NM_TYPES = {"wifi", "802-11-wireless", "ethernet", "802-3-ethernet", "vpn", "wireguard", "gsm", "cdma", "pppoe"}


_NM_CERTIFICATE_KEYS = {"ca-cert", "client-cert", "private-key", "phase2-ca-cert", "phase2-client-cert", "phase2-private-key"}


@dataclass
class ImportedProfile:
    text: str
    connection_type: str
    certificates: list[tuple[str, str]] = field(default_factory=list)  # (placeholder, live path)


def rewrite_keyfile(text: str, *, portable: bool = False, drop_wifi_interface: bool = False) -> ImportedProfile | None:
    """Prepare a live NetworkManager profile for the installed system; None when it should be skipped.

    ``permissions=`` names the live user, which does not exist on the target, so it is dropped to
    make the profile system-wide. For portable installs ``interface-name=`` of Ethernet profiles is
    dropped too, because device names differ from PC to PC.
    """
    section = ""
    connection_type = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "connection" and line.startswith("type="):
            connection_type = line.partition("=")[2].strip()
    if connection_type not in _IMPORTABLE_NM_TYPES:
        return None
    out: list[str] = []
    certificates: list[tuple[str, str]] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "connection" and line.startswith("permissions="):
            continue
        elif portable and section == "connection" and line.startswith("interface-name=") and connection_type in {"ethernet", "802-3-ethernet"}:
            continue
        elif drop_wifi_interface and section == "connection" and line.startswith("interface-name="):
            continue  # a Wi-Fi profile copied for a portable target (network.copy_live_wifi) is not tied to this PC's card
        elif section.startswith("802-1x") and "=" in line and line.partition("=")[0] in _NM_CERTIFICATE_KEYS:
            key, _, value = line.partition("=")
            path = value[len("file://"):] if value.startswith("file://") else value
            if path.startswith("/"):
                placeholder = f"@@CERT{len(certificates)}@@"
                certificates.append((placeholder, path))
                out.append(f"{key}={placeholder}")
                continue
        out.append(raw)
    return ImportedProfile("\n".join(out) + "\n", connection_type, certificates)


WIFI_NOTE = "Very old Wi-Fi security (WEP, TKIP) will not connect."


def network_atoms(manifest: InstallManifest) -> list[str]:
    """Packages the network choices add on top of the manager itself (NET-05, NET-06)."""
    n = manifest.network
    atoms: list[str] = []
    if n.manager == "netifrc" and n.dhcp:
        atoms.append("net-misc/dhcpcd")  # netifrc's "dhcp" needs a client
    if n.wireless_tools:
        atoms.extend(("net-wireless/iw", "net-wireless/wpa_supplicant"))
    if n.ppp:
        atoms.append("net-dialup/ppp")
    return atoms


class NetworkMixin(ConfigurerBase):
    def network_atoms(self, manifest: InstallManifest) -> list[str]:
        return network_atoms(manifest)

    def configure_network(self, manifest: InstallManifest) -> None:
        n = manifest.network
        if n.manager == "networkmanager":
            self.write("etc/NetworkManager/conf.d/dhcp.conf", "[main]\ndhcp=internal\n")
            if n.wifi_ssid:
                self._write_profile("installer-wifi.nmconnection", render_nm_wifi(n))
            if not n.dhcp and not n.interface.startswith("wl"):
                self._write_profile("installer-wired.nmconnection", render_nm_wired(n))
            if n.import_live_profiles:
                self.import_live_network_profiles(manifest)
            elif n.copy_live_wifi:
                self.copy_live_wifi_profile(manifest)
        elif n.manager == "dhcpcd":
            if not n.dhcp:
                render_dhcpcd_static(manifest)  # validates the settings now; the block is added once the package exists
        elif n.manager == "netifrc":
            self.write("etc/conf.d/net", render_netifrc(manifest))
        else:
            self.write("etc/systemd/network/20-wired.network", render_systemd_network(manifest))
        if n.dns and n.manager not in {"systemd-networkd", "dhcpcd"}:
            self.write("etc/resolv.conf", "".join(f"nameserver {server}\n" for server in n.dns))

    def _write_profile(self, name: str, content: str) -> None:
        directory = self.path("etc/NetworkManager/system-connections")
        if not self.dry_run:
            self._mkdirs(directory, 0o700)
        self.write(f"etc/NetworkManager/system-connections/{name}", content, 0o600)

    def configure_network_late(self, manifest: InstallManifest) -> None:
        """Settings that extend a file the network package installs (so they run after it)."""
        n = manifest.network
        if n.manager == "dhcpcd" and not n.dhcp:
            marker = "# Static configuration written by the Gentoo live installer"
            stock = "" if self.dry_run else self.read_text("etc/dhcpcd.conf")
            if marker not in stock:
                self.write("etc/dhcpcd.conf", stock.rstrip("\n") + ("\n" if stock else "") + render_dhcpcd_static(manifest))

    def copy_live_wifi_profile(self, manifest: InstallManifest) -> str:
        """Copy the one live Wi-Fi connection named by ``network.copy_live_wifi`` (with its secret) to the target.

        Returns the profile's file name, or '' when it was not copied. A uuid that no longer exists is a warning, not an error.
        """
        wanted = manifest.network.copy_live_wifi
        if self.dry_run or not wanted or not self.live_network_dir.is_dir():
            return ""
        portable = manifest.hardware.target == "portable"
        pattern = re.compile(rf"^uuid={re.escape(wanted)}\s*$", re.MULTILINE | re.IGNORECASE)
        for source in sorted(self.live_network_dir.glob("*.nmconnection")):
            if source.name.startswith("installer-") or not source.is_file():
                continue
            try:
                text = source.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not pattern.search(text):
                continue
            profile = rewrite_keyfile(text, portable=portable, drop_wifi_interface=portable)
            if profile is None or profile.connection_type not in {"wifi", "802-11-wireless"}:
                break
            self._write_profile(source.name, profile.text)
            self.notify(f"Copied the Wi-Fi connection {source.stem} from this session")
            return source.name
        self.notify(f"warning: the Wi-Fi connection {wanted} of this session was not found; the installed system will not know it")
        return ""

    def import_live_network_profiles(self, manifest: InstallManifest) -> list[str]:
        """Copy this session's NetworkManager connections (Wi-Fi, VPN, enterprise) to the target."""
        imported: list[str] = []
        if self.dry_run or not self.live_network_dir.is_dir():
            return imported
        portable = manifest.hardware.target == "portable"
        for source in sorted(self.live_network_dir.glob("*.nmconnection")):
            if source.name.startswith("installer-") or not source.is_file():
                continue
            try:
                profile = rewrite_keyfile(source.read_text(encoding="utf-8"), portable=portable)
            except (OSError, UnicodeDecodeError):
                self.notify(f"warning: network profile {source.name} could not be read and was skipped")
                continue
            if profile is None:
                continue
            text = profile.text
            missing = False
            for index, (placeholder, live_path) in enumerate(profile.certificates):
                certificate = Path(live_path)
                try:
                    if not live_path.startswith(CERTIFICATE_ROOTS) or ".." in certificate.parts or not certificate.is_file() \
                            or certificate.stat().st_size > 1024 * 1024:
                        missing = True
                        break
                    data = certificate.read_bytes()
                except OSError:
                    missing = True
                    break
                target_name = f"etc/NetworkManager/certs/{hashlib.sha256(source.name.encode()).hexdigest()[:8]}-{index}-{certificate.name}"
                self.write_bytes(target_name, data, 0o600)
                text = text.replace(placeholder, "/" + target_name)
            if missing:
                self.notify(f"warning: network profile {source.name} refers to a certificate that is missing; skipped")
                continue
            self._write_profile(source.name, text)
            imported.append(source.stem)
        if imported:
            self.notify("Copied network connections from this session: " + ", ".join(imported))
        return imported

    def finalize_network(self, manifest: InstallManifest) -> None:
        """Hand /etc/resolv.conf to systemd-resolved once nothing else needs name resolution."""
        if manifest.system.init != "systemd" or manifest.network.manager != "systemd-networkd" or self.dry_run:
            return
        resolv = self.path("etc") / "resolv.conf"
        resolv.unlink(missing_ok=True)
        resolv.symlink_to("../run/systemd/resolve/stub-resolv.conf")
