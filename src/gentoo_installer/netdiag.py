"""NET-01: the Handbook's network tests in order, with one plain sentence for the first one that fails.

Handbook Networking#Testing_the_network and #Obtaining_interface_info: look at the adapters (``ip addr``), check the
default route, ping an address, resolve a name. This module only parses and decides; the callers run the commands, so
the root daemon (``network_status``), the GUI and the text installer share one implementation.

``Runner`` below is any function that runs a command and returns its output, or ``None`` when the command failed.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

Run = Callable[[list[str]], "str | None"]

PING_ADDRESS = "1.1.1.1"
LOOKUP_NAME = "gentoo.org"

# The problem codes of the daemon's network_status (interface I8).
PROBLEMS = ("cable", "no_device", "wifi_off", "wifi_hard_off", "no_connection", "no_route", "no_dns", "no_mirror", "proxy")

MESSAGES = {
    "no_device": "No network adapter was found, only the computer itself. The adapter or its driver is missing: try a USB network "
                 "adapter, or a phone with USB tethering.",
    "cable": "The network cable is not connected, or the router is switched off. Plug the cable in, or connect to Wi-Fi instead.",
    "wifi_off": "Wi-Fi is switched off. Switch it on to see the networks around you.",
    "wifi_hard_off": "Wireless is switched off by a key or a switch. Press the Wi-Fi key (often Fn together with a key that has an antenna), "
                     "then check again.",
    "no_connection_wired": "Your cable is connected but no address was given: the router may not offer DHCP. "
                           "You can set the address by hand under More network settings.",
    "no_connection_wifi": "Wi-Fi is not connected yet. Pick your network from the list and type its password.",
    "no_connection": "This computer has no network address yet. Connect a cable or pick a Wi-Fi network.",
    "no_route": "The computer has an address, but the router does not lead to the internet. Check that the router itself is online.",
    "no_dns": "The internet answers, but names such as gentoo.org cannot be looked up. The name server (DNS) may be wrong: "
              "try 1.1.1.1 under More network settings.",
    "no_mirror": "The internet works, but the Gentoo download server cannot be reached securely. A wrong clock or a firewall can cause this.",
    "proxy": "The Gentoo download server cannot be reached through your proxy. Check the proxy address under More network settings.",
}

# (key, short label) in the Handbook's order; the 'Why not?' expander lists them.
STEPS = (
    ("link", "A network adapter with a link"),
    ("address", "An address from the router"),
    ("route", "A route to the internet"),
    ("ping", f"Reaching {PING_ADDRESS}"),
    ("dns", f"Looking up {LOOKUP_NAME}"),
    ("mirror", "A secure connection to the Gentoo download server"),
)


@dataclass
class Adapter:
    name: str
    kind: str = "other"  # ethernet | wifi | wwan | other
    state: str = "disconnected"  # connected | connecting | disconnected | unavailable | unmanaged
    carrier: bool = False
    ipv4: list[str] = field(default_factory=list)
    ipv6: list[str] = field(default_factory=list)
    connection: str = ""
    driver: str = ""

    @property
    def has_address(self) -> bool:
        return bool(self.ipv4)

    def describe(self) -> str:
        """'enp5s0 (connected, 192.168.1.20)' or 'wlp4s0 (not connected)'."""
        if self.has_address:
            return f"{self.name} (connected, {self.ipv4[0].split('/')[0]})"
        if self.kind == "ethernet" and not self.carrier:
            return f"{self.name} (no cable)"
        return f"{self.name} (not connected)"


@dataclass
class Step:
    key: str
    label: str
    ok: bool | None  # None: not tried because an earlier step failed
    detail: str = ""


@dataclass
class Diagnosis:
    adapters: list[Adapter]
    steps: list[Step]
    problem: str = ""  # "" when everything works, else one of PROBLEMS
    message: str = ""

    @property
    def online(self) -> bool:
        return not self.problem

    def summary_lines(self) -> list[str]:
        """'Wired: enp5s0 (connected, 192.168.1.20)' lines for the readiness row."""
        lines = []
        for title, kind in (("Wired", "ethernet"), ("Wi-Fi", "wifi"), ("Mobile", "wwan")):
            lines.extend(f"{title}: {adapter.describe()}" for adapter in self.adapters if adapter.kind == kind)
        lines.extend(f"Other: {adapter.describe()}" for adapter in self.adapters if adapter.kind == "other")
        return lines


def guess_kind(name: str) -> str:
    """The kind of adapter from its predictable name (enp5s0, wlp4s0, wwan0, ...)."""
    if name.startswith(("en", "eth")):
        return "ethernet"
    if name.startswith(("wl", "wlan")):
        return "wifi"
    if name.startswith("ww"):
        return "wwan"
    return "other"


def parse_ip_addr(text: str) -> list[Adapter]:
    """Adapters from ``ip -j addr``; the loopback is left out. Empty on text that is not that JSON."""
    try:
        rows = json.loads(text)
    except (ValueError, TypeError):
        return []
    adapters: list[Adapter] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("ifname"), str):
            continue
        name = row["ifname"]
        flags = row.get("flags") if isinstance(row.get("flags"), list) else []
        if name == "lo" or "LOOPBACK" in flags:
            continue
        adapter = Adapter(name, guess_kind(name), carrier="LOWER_UP" in flags)
        for info in row.get("addr_info") or []:
            if not isinstance(info, dict) or not info.get("local"):
                continue
            cidr = f"{info['local']}/{info.get('prefixlen', '')}".rstrip("/")
            if info.get("family") == "inet":
                adapter.ipv4.append(cidr)
            elif info.get("family") == "inet6" and info.get("scope") == "global":
                adapter.ipv6.append(cidr)
        adapter.state = "connected" if adapter.ipv4 else "disconnected"
        adapters.append(adapter)
    return adapters


def has_default_route(text: str) -> bool:
    """Whether ``ip -j route`` (or plain ``ip route``) shows a default route."""
    try:
        rows = json.loads(text)
    except (ValueError, TypeError):
        return bool(re.search(r"^default\b", text or "", re.MULTILINE))
    return any(isinstance(row, dict) and row.get("dst") == "default" for row in rows if isinstance(rows, list))


def resolved(text: str | None) -> bool:
    """Whether ``getent hosts NAME`` printed an address."""
    return bool(text and re.search(r"\d+\.\d+\.\d+\.\d+|[0-9a-f:]{3,}", text.split(None, 1)[0] if text.split() else ""))


def diagnose(run: Run, *, adapters: list[Adapter] | None = None, mirror: Callable[[], bool] | None = None, proxy: bool = False,
             wifi_enabled: bool | None = None, rfkill_hard: bool = False) -> Diagnosis:
    """Run the tests in the Handbook's order and name the first one that fails.

    ``adapters`` are used as given when the caller knows more than ``ip`` does (NetworkManager's kinds and states).
    ``mirror`` is the secure-connection test (None: not tested, treated as passing). ``wifi_enabled`` False and
    ``rfkill_hard`` describe the Wi-Fi switch.
    """
    if adapters is None:
        adapters = parse_ip_addr(run(["ip", "-j", "addr"]) or "")
    steps = [Step(key, label, None) for key, label in STEPS]
    by_key = {step.key: step for step in steps}
    wifi = [adapter for adapter in adapters if adapter.kind == "wifi"]
    wired = [adapter for adapter in adapters if adapter.kind == "ethernet"]

    def stop(problem: str, message: str | None = None) -> Diagnosis:
        return Diagnosis(adapters, steps, problem, message or MESSAGES[problem])

    if not adapters:
        by_key["link"].ok = False
        return stop("no_device")
    if rfkill_hard and wifi and not wired:
        by_key["link"].ok = False
        return stop("wifi_hard_off")
    if wifi_enabled is False and wifi and not any(adapter.carrier for adapter in wired):
        by_key["link"].ok = False
        return stop("wifi_off")
    connected = [adapter for adapter in adapters if adapter.has_address]
    if not connected:
        linked = [adapter for adapter in wired if adapter.carrier]
        if linked:
            by_key["link"].ok = True
            by_key["address"].ok = False
            return stop("no_connection", MESSAGES["no_connection_wired"])
        by_key["link"].ok = False
        if wired and not wifi:
            return stop("cable")
        if wifi:
            return stop("no_connection", MESSAGES["no_connection_wifi"])
        return stop("no_connection")
    by_key["link"].ok = by_key["address"].ok = True
    by_key["address"].detail = ", ".join(adapter.describe() for adapter in connected)
    route = has_default_route(run(["ip", "-j", "route"]) or "")
    by_key["route"].ok = route
    if not route:
        return stop("no_route")
    ping = run(["ping", "-c", "1", "-W", "3", PING_ADDRESS])
    by_key["ping"].ok = ping is not None
    if ping is None:
        return stop("no_route")
    lookup = run(["getent", "hosts", LOOKUP_NAME])
    by_key["dns"].ok = resolved(lookup)
    if not resolved(lookup):
        return stop("no_dns")
    if mirror is not None:
        ok = bool(mirror())
        by_key["mirror"].ok = ok
        if not ok:
            return stop("proxy" if proxy else "no_mirror")
    else:
        by_key["mirror"].ok = None
    return Diagnosis(adapters, steps)


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
