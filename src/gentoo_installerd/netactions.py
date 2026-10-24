"""Network actions of the daemon (feature ``network.v1``, interface I8): status, Wi-Fi and a fixed address for the live session.

Everything goes through NetworkManager's ``nmcli``. No secret is ever put into a command line (other local users can read
``/proc/*/cmdline``): a Wi-Fi profile is written as a keyfile (mode 0600), loaded with ``nmcli connection load`` and started with
``nmcli connection up uuid U``. A failed attempt deletes the profile it made.

Replies are one ``result`` event whose ``data`` carries ``ok`` and the answer, then ``completed``. Actions that change the network
are refused while an installation runs; the read-only ones are not.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import uuid as uuidlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gentoo_installer import netdiag

from .configure_network import _keyfile_escape
from .events import ErrorCode, Event, completed_event
from .runner import Runner, RunnerError, SubprocessRunner
from .server import ActionError, Channel

if TYPE_CHECKING:
    from .server import ActionRegistry

LOG = logging.getLogger(__name__)

FEATURE = "network.v1"
ACTIONS = ("network_status", "wifi_scan", "wifi_connect", "wifi_radio", "wifi_live_connections", "network_static")
CONNECTIONS_DIR = Path("/etc/NetworkManager/system-connections")
SYS_NET = Path("/sys/class/net")
STATUS_TIMEOUT = 8.0
SCAN_TIMEOUT = 25.0
CONNECT_WAIT_SECONDS = 45
CONNECT_TIMEOUT = 60.0
ACTION_TIMEOUT = 30.0
SECURITY_KINDS = ("open", "wpa-psk", "sae", "owe", "wep", "enterprise")


def make_runner() -> Runner:
    """The runner that executes the commands (tests replace this with a fake)."""
    return SubprocessRunner()


def read_driver(name: str) -> str:
    """The kernel driver of an adapter, from sysfs ('' when unknown)."""
    try:
        return (SYS_NET / name / "device" / "driver").resolve().name
    except OSError:
        return ""


def probe_mirror(host: str, proxy: str) -> bool:
    from gentoo_installer import system_info
    return system_info.probe_mirror(host, proxy).reachable


def register(registry: ActionRegistry) -> None:
    registry.register_action("network_status", handle_network_status, feature=FEATURE)
    registry.register_action("wifi_scan", handle_wifi_scan, feature=FEATURE)
    registry.register_action("wifi_connect", handle_wifi_connect, feature=FEATURE)
    registry.register_action("wifi_radio", handle_wifi_radio, feature=FEATURE)
    registry.register_action("wifi_live_connections", handle_wifi_live_connections, feature=FEATURE)
    registry.register_action("network_static", handle_network_static, feature=FEATURE)


# ------------------------------------------------------------------------------------------------ parsing


def split_terse(line: str) -> list[str]:
    """Fields of one line of ``nmcli -t`` output: ':' separates, '\\:' and '\\\\' are escapes."""
    fields: list[str] = []
    current: list[str] = []
    chars = iter(line)
    for char in chars:
        if char == "\\":
            current.append(next(chars, ""))
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def parse_device_status(text: str) -> list[dict[str, str]]:
    """``nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status`` as dictionaries."""
    rows = []
    for line in text.splitlines():
        parts = split_terse(line)
        if len(parts) >= 4 and parts[0]:
            rows.append({"device": parts[0], "type": parts[1], "state": parts[2], "connection": parts[3] if parts[3] != "--" else ""})
    return rows


def device_kind(nm_type: str, name: str = "") -> str:
    return {"ethernet": "ethernet", "wifi": "wifi", "gsm": "wwan", "cdma": "wwan"}.get(nm_type) or netdiag.guess_kind(name)


def device_state(nm_state: str) -> str:
    """NetworkManager's state text to the five states of the protocol."""
    base = nm_state.split(" ", 1)[0]
    if base == "connected":
        return "connected"
    if base == "connecting":
        return "connecting"
    if base == "unmanaged":
        return "unmanaged"
    if base == "unavailable":
        return "unavailable"
    return "disconnected"


def security_of(text: str) -> str:
    """The protocol's security kind from the SECURITY column of ``nmcli device wifi list``."""
    text = text.strip()
    if text in {"", "--"}:
        return "open"
    words = set(text.replace(",", " ").split())
    if "802.1X" in words:
        return "enterprise"
    if "OWE" in words:
        return "owe"
    if words & {"WPA2", "WPA1", "WPA"}:
        return "wpa-psk"
    if "WPA3" in words:
        return "sae"
    if "WEP" in words:
        return "wep"
    return "wpa-psk"


def band_of(frequency: str) -> str:
    match = re.match(r"\s*(\d+)", frequency)
    mhz = int(match.group(1)) if match else 0
    return "2.4" if 0 < mhz < 3000 else "5" if mhz < 5925 and mhz else "6" if mhz else "2.4"


def parse_wifi_list(text: str, known: set[str] | None = None) -> list[dict[str, Any]]:
    """``nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY,FREQ device wifi list`` as one row per SSID (the strongest).

    In use first, then known networks, then by signal. Hidden networks (empty SSID) are left out.
    """
    known = known or set()
    best: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        parts = split_terse(line)
        if len(parts) < 5 or not parts[1]:
            continue
        in_use, ssid, signal, security, frequency = parts[:5]
        try:
            strength = max(0, min(100, int(signal)))
        except ValueError:
            strength = 0
        row = {"ssid": ssid, "signal": strength, "security": security_of(security), "in_use": in_use.strip() == "*",
               "known": ssid in known, "band": band_of(frequency)}
        old = best.get(ssid)
        if old is None:
            best[ssid] = row
            continue
        row["in_use"] = row["in_use"] or old["in_use"]
        if old["signal"] >= strength:
            old["in_use"] = row["in_use"]
        else:
            best[ssid] = row
    return sorted(best.values(), key=lambda r: (not r["in_use"], not r["known"], -r["signal"], r["ssid"].lower()))


def failure_reason(output: str) -> str:
    """The protocol's failure reason from what ``nmcli connection up`` printed."""
    text = output.lower()
    if "timeout expired" in text or "timed out" in text:
        return "timeout"
    if "secrets were required" in text or "no secrets" in text or "802-11-wireless-security" in text or "supplicant" in text \
            or "authentication" in text or "(7)" in text:
        return "wrong_password"
    if "no network with ssid" in text or "could not be found" in text or "not found" in text:
        return "not_found"
    if "no suitable device" in text or "no wi-fi device" in text or "device not found" in text or "no device" in text:
        return "no_device"
    if "wi-fi is disabled" in text or "wireless is disabled" in text or "rfkill" in text:
        return "wifi_off"
    return "failed"


REASON_MESSAGES = {
    "wrong_password": "The password was not accepted. Check it and try again.",
    "not_found": "That network is out of reach right now. Move closer to the router and scan again.",
    "timeout": "The network did not answer in time. Try again, or move closer to the router.",
    "no_device": "No Wi-Fi adapter was found. It may need a driver, or you can use a cable instead.",
    "wifi_off": "Wi-Fi is switched off. Switch it on first.",
    "wifi_hard_off": netdiag.MESSAGES["wifi_hard_off"],
    "enterprise": "This network needs a user name and certificate (802.1X). Use the network icon in the panel, or More network settings.",
    "failed": "The connection could not be made.",
}


def secret_flags_stored(flags_text: str) -> bool:
    """Whether the psk-flags field of a profile says the secret is kept in the profile itself (0)."""
    return flags_text.strip().split(" ", 1)[0] in {"0", ""}


# ------------------------------------------------------------------------------------------------ helpers


def _run(runner: Runner, argv: list[str], timeout: float = ACTION_TIMEOUT) -> str | None:
    """Stdout of a command, or None when it failed."""
    try:
        return runner.run(argv, timeout=timeout).stdout
    except RunnerError:
        return None


def _reply(channel: Channel, ok: bool, message: str = "", **data: Any) -> None:
    channel.send(Event("result", message=message, data={"ok": ok, "message": message, **data}))
    channel.send(completed_event(ok, message))


def _refuse_while_installing(channel: Channel) -> None:
    manager: Any = getattr(channel, "install_manager", None)
    if manager is not None and manager.is_running():
        raise ActionError("The network cannot be changed while an installation is running.", ErrorCode.BUSY)


def _need_nmcli(runner: Runner) -> None:
    if _run(runner, ["nmcli", "--version"], 5.0) is None:
        raise ActionError("NetworkManager is not available here.", ErrorCode.FAILED)


def _wifi_radio(runner: Runner) -> tuple[bool | None, bool]:
    """(software switch on, hardware switch blocking) from ``nmcli -t -f WIFI-HW,WIFI radio``; (None, False) when unknown."""
    text = _run(runner, ["nmcli", "-t", "-f", "WIFI-HW,WIFI", "radio"], STATUS_TIMEOUT)
    first = split_terse((text or "").strip().splitlines()[0]) if text and text.strip() else []
    if len(first) < 2:
        return None, False
    return first[1] == "enabled", first[0] != "enabled"


def collect_adapters(runner: Runner) -> list[netdiag.Adapter]:
    """Adapters from ``ip`` enriched with NetworkManager's kind, state and connection name."""
    adapters = netdiag.parse_ip_addr(_run(runner, ["ip", "-j", "addr"], STATUS_TIMEOUT) or "")
    status = {row["device"]: row for row in parse_device_status(
        _run(runner, ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"], STATUS_TIMEOUT) or "")}
    known = {adapter.name for adapter in adapters}
    for name, row in status.items():
        if name == "lo" or row["type"] in {"loopback", "bridge", "tun", "veth", "bond", "wireguard", "dummy"}:
            continue
        if name not in known:  # a Wi-Fi card that is switched off has no ip entry in some drivers
            adapters.append(netdiag.Adapter(name, netdiag.guess_kind(name)))
    for adapter in adapters:
        row = status.get(adapter.name)
        if row:
            adapter.kind = device_kind(row["type"], adapter.name)
            adapter.state = device_state(row["state"])
            adapter.connection = row["connection"]
        adapter.driver = read_driver(adapter.name)
    return adapters


def _known_ssids(runner: Runner) -> dict[str, str]:
    """{ssid: uuid} of the stored Wi-Fi profiles (system-owned or the user's)."""
    text = _run(runner, ["nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"], STATUS_TIMEOUT) or ""
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = split_terse(line)
        if len(parts) >= 2 and parts[1] in {"802-11-wireless", "wifi"}:
            ssid = _run(runner, ["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", "uuid", parts[0]], STATUS_TIMEOUT)
            if ssid and ssid.strip():
                result.setdefault(ssid.strip(), parts[0])
    return result


# ------------------------------------------------------------------------------------------------ actions


def handle_network_status(request: dict, channel: Channel) -> None:
    runner = make_runner()
    wifi_enabled, hard = _wifi_radio(runner)
    adapters = collect_adapters(runner)
    proxy = str(request.get("proxy") or "")
    host = str(request.get("mirror") or "distfiles.gentoo.org")
    mirror = (lambda: probe_mirror(host, proxy)) if request.get("check_mirror") else None

    def run(argv: list[str]) -> str | None:
        return _run(runner, argv, STATUS_TIMEOUT)

    diagnosis = netdiag.diagnose(run, adapters=adapters, mirror=mirror, proxy=bool(proxy), wifi_enabled=wifi_enabled, rfkill_hard=hard)
    wifi_present = any(adapter.kind == "wifi" for adapter in adapters)
    route = next((step.ok for step in diagnosis.steps if step.key == "route"), None)
    dns = next((step.ok for step in diagnosis.steps if step.key == "dns"), None)
    mirror_step = next((step.ok for step in diagnosis.steps if step.key == "mirror"), None)
    data = {
        "online": diagnosis.online, "mirror": mirror_step, "default_route": bool(route), "dns": bool(dns),
        "wifi_present": wifi_present, "wifi_enabled": bool(wifi_enabled) if wifi_present else False, "rfkill_hard": hard,
        "devices": [{"name": a.name, "kind": a.kind, "state": a.state, "driver": a.driver, "connection": a.connection,
                     "ipv4": a.ipv4, "ipv6": a.ipv6} for a in adapters],
        "problem": diagnosis.problem, "message": diagnosis.message,
        "steps": [{"key": s.key, "label": s.label, "ok": s.ok, "detail": s.detail} for s in diagnosis.steps],
    }
    _reply(channel, True, diagnosis.message or "The network works.", **data)


def handle_wifi_scan(request: dict, channel: Channel) -> None:
    runner = make_runner()
    _need_nmcli(runner)
    rescan = request.get("rescan", True) is not False
    known = set(_known_ssids(runner))
    text = _run(runner, ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list", "--rescan", "yes" if rescan else "no"],
                SCAN_TIMEOUT)
    if text is None:
        _reply(channel, False, "The Wi-Fi networks could not be listed. Is Wi-Fi switched on?", networks=[])
        return
    _reply(channel, True, "", networks=parse_wifi_list(text, known))


def render_wifi_keyfile(ssid: str, password: str, security: str, connection_uuid: str, hidden: bool = False) -> str:
    """A system-wide Wi-Fi profile with its secret stored in the file (psk-flags=0)."""
    lines = ["[connection]", f"id={_keyfile_escape(ssid)}", f"uuid={connection_uuid}", "type=wifi", "autoconnect=true", "",
             "[wifi]", "mode=infrastructure", f"ssid={_keyfile_escape(ssid)}"]
    if hidden:
        lines.append("hidden=true")
    if security in {"wpa-psk", "sae"}:
        lines.extend(("", "[wifi-security]", f"key-mgmt={'sae' if security == 'sae' else 'wpa-psk'}",
                      f"psk={_keyfile_escape(password)}", "psk-flags=0"))
    elif security == "owe":
        lines.extend(("", "[wifi-security]", "key-mgmt=owe"))
    lines.extend(("", "[ipv4]", "method=auto", "", "[ipv6]", "method=auto"))
    return "\n".join(lines) + "\n"


def _write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def _security_for(request: dict, runner: Runner, ssid: str) -> str:
    wanted = request.get("security")
    if wanted in SECURITY_KINDS:
        return str(wanted)
    if not request.get("password"):
        return "open"
    text = _run(runner, ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list", "--rescan", "no"], SCAN_TIMEOUT) or ""
    for row in parse_wifi_list(text):
        if row["ssid"] == ssid:
            return str(row["security"])
    return "wpa-psk"


def _fail(channel: Channel, reason: str, message: str | None = None) -> None:
    _reply(channel, False, message or REASON_MESSAGES.get(reason, REASON_MESSAGES["failed"]), reason=reason)


def handle_wifi_connect(request: dict, channel: Channel) -> None:
    _refuse_while_installing(channel)
    ssid, password = request.get("ssid"), request.get("password") or ""
    if not isinstance(ssid, str) or not ssid or len(ssid.encode("utf-8")) > 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ActionError("Enter the name of the Wi-Fi network (at most 32 bytes).", ErrorCode.BAD_REQUEST)
    if not isinstance(password, str) or any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise ActionError("The Wi-Fi password contains characters that cannot be used.", ErrorCode.BAD_REQUEST)
    hidden = request.get("hidden") is True
    runner = make_runner()
    _need_nmcli(runner)
    wifi_enabled, hard = _wifi_radio(runner)
    if hard:
        return _fail(channel, "wifi_hard_off")
    if wifi_enabled is False:
        return _fail(channel, "wifi_off")
    security = _security_for({**request, "password": password}, runner, ssid)
    if security == "enterprise":
        return _fail(channel, "enterprise")
    if security == "wep":
        return _fail(channel, "failed", "This network uses WEP, a very old and insecure Wi-Fi security that is not supported. Change it on the router to WPA2.")
    if security in {"wpa-psk", "sae"}:
        if not password:
            return _fail(channel, "wrong_password", "This network needs a password.")
        if not (8 <= len(password) <= 63 or re.fullmatch(r"[0-9A-Fa-f]{64}", password)):
            return _fail(channel, "wrong_password", "A Wi-Fi password has 8 to 63 characters.")
    connection_uuid = str(uuidlib.uuid4())
    path = CONNECTIONS_DIR / f"live-wifi-{connection_uuid}.nmconnection"
    try:
        CONNECTIONS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_private(path, render_wifi_keyfile(ssid, password, security, connection_uuid, hidden))
    except OSError as exc:
        LOG.warning("Could not write the Wi-Fi profile: %s", exc.strerror)
        return _fail(channel, "failed", "The Wi-Fi profile could not be saved.")
    try:
        if _run(runner, ["nmcli", "connection", "load", str(path)], ACTION_TIMEOUT) is None:
            raise RunnerError(["nmcli", "connection", "load"], 1, "profile not loaded")
        runner.run(["nmcli", "--wait", str(CONNECT_WAIT_SECONDS), "connection", "up", "uuid", connection_uuid], timeout=CONNECT_TIMEOUT)
    except RunnerError as exc:
        _delete_profile(runner, connection_uuid, path)
        reason = failure_reason(exc.output or str(exc))
        return _fail(channel, reason)
    _reply(channel, True, f"Connected to {ssid}.", uuid=connection_uuid, ssid=ssid)


def _delete_profile(runner: Runner, connection_uuid: str, path: Path) -> None:
    _run(runner, ["nmcli", "connection", "delete", "uuid", connection_uuid], ACTION_TIMEOUT)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOG.warning("Could not remove %s", path.name)


def handle_wifi_radio(request: dict, channel: Channel) -> None:
    _refuse_while_installing(channel)
    on = request.get("on")
    if not isinstance(on, bool):
        raise ActionError("Say whether Wi-Fi should be on or off.", ErrorCode.BAD_REQUEST)
    runner = make_runner()
    _need_nmcli(runner)
    _, hard = _wifi_radio(runner)
    if hard and on:
        return _fail(channel, "wifi_hard_off")
    if _run(runner, ["nmcli", "radio", "wifi", "on" if on else "off"], ACTION_TIMEOUT) is None:
        return _fail(channel, "failed", "Wi-Fi could not be switched.")
    _reply(channel, True, "Wi-Fi is on." if on else "Wi-Fi is off.", on=on)


def handle_wifi_live_connections(request: dict, channel: Channel) -> None:
    runner = make_runner()
    _need_nmcli(runner)
    listing = _run(runner, ["nmcli", "-t", "-f", "UUID,TYPE,ACTIVE", "connection", "show"], STATUS_TIMEOUT) or ""
    rows = []
    for line in listing.splitlines():
        parts = split_terse(line)
        if len(parts) < 3 or parts[1] not in {"802-11-wireless", "wifi"}:
            continue
        connection_uuid = parts[0]
        fields = _run(runner, ["nmcli", "-t", "-f", "802-11-wireless.ssid,802-11-wireless-security.key-mgmt,802-11-wireless-security.psk-flags",
                               "connection", "show", "uuid", connection_uuid], STATUS_TIMEOUT) or ""
        values = {}
        for field_line in fields.splitlines():
            key, _, value = field_line.partition(":")
            values[key] = value.replace("\\:", ":")
        ssid = values.get("802-11-wireless.ssid", "")
        if not ssid:
            continue
        secured = bool(values.get("802-11-wireless-security.key-mgmt", "").strip("-"))
        stored = (not secured) or secret_flags_stored(values.get("802-11-wireless-security.psk-flags", "0"))
        rows.append({"uuid": connection_uuid, "ssid": ssid, "active": parts[2] == "yes", "secret_stored": stored})
    _reply(channel, True, "", connections=rows)


# ----------------------------------------------------------------------------- static address (NET-04)


def validate_static(address: str, gateway: str, dns: list[str]) -> tuple[str, str, list[str]]:
    """The normalised (address/prefix, gateway, dns) or ValueError with a sentence for the user."""
    try:
        interface = ipaddress.ip_interface(address.strip())
    except ValueError:
        raise ValueError("The address must look like 192.168.1.50/24 (address, slash, prefix length).") from None
    if "/" not in address:
        raise ValueError("Add the prefix length to the address, for example 192.168.1.50/24.")
    try:
        gate = ipaddress.ip_address(gateway.strip())
    except ValueError:
        raise ValueError("The gateway must be an address such as 192.168.1.1.") from None
    if gate.version != interface.version:
        raise ValueError("The address and the gateway must both be IPv4, or both IPv6.")
    if gate not in interface.network:
        raise ValueError("The gateway is not in the same network as the address. Check the prefix length.")
    servers = []
    for server in dns:
        try:
            servers.append(str(ipaddress.ip_address(server.strip())))
        except ValueError:
            raise ValueError(f"{server!r} is not a DNS server address.") from None
    return str(interface), str(gate), servers


def static_commands(connection: str, address: str, gateway: str, dns: list[str]) -> list[list[str]]:
    """The nmcli commands that give a live connection a fixed address (no secrets in them)."""
    version = "ipv6" if ":" in address else "ipv4"
    modify = ["nmcli", "connection", "modify", connection, f"{version}.method", "manual", f"{version}.addresses", address,
              f"{version}.gateway", gateway, f"{version}.dns", ",".join(dns), f"{version}.ignore-auto-dns", "yes" if dns else "no"]
    return [modify, ["nmcli", "--wait", "30", "connection", "up", connection]]


def handle_network_static(request: dict, channel: Channel) -> None:
    """{address, gateway, dns: [..], connection?}: give the live connection a fixed address. {dhcp: true} puts it back."""
    _refuse_while_installing(channel)
    runner = make_runner()
    _need_nmcli(runner)
    connection = str(request.get("connection") or "")
    if not connection:
        status = parse_device_status(_run(runner, ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"], STATUS_TIMEOUT) or "")
        active = [row for row in status if row["type"] in {"ethernet", "wifi"} and row["state"].startswith("connected") and row["connection"]]
        active.sort(key=lambda row: row["type"] != "ethernet")
        if not active:
            return _fail(channel, "no_device", "There is no connected network to change. Connect a cable or Wi-Fi first.")
        connection = active[0]["connection"]
    if request.get("dhcp") is True:
        commands = [["nmcli", "connection", "modify", connection, "ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", "",
                     "ipv4.dns", "", "ipv4.ignore-auto-dns", "no"], ["nmcli", "--wait", "30", "connection", "up", connection]]
    else:
        raw_dns = request.get("dns") or []
        try:
            address, gateway, servers = validate_static(str(request.get("address") or ""), str(request.get("gateway") or ""),
                                                        [str(item) for item in raw_dns] if isinstance(raw_dns, list) else [])
        except ValueError as exc:
            raise ActionError(str(exc), ErrorCode.BAD_REQUEST) from None
        commands = static_commands(connection, address, gateway, servers)
    for argv in commands:
        try:
            runner.run(argv, timeout=CONNECT_TIMEOUT)
        except RunnerError:
            return _fail(channel, "failed", "The address could not be applied to the connection.")
    _reply(channel, True, "The address was applied.", connection=connection)
