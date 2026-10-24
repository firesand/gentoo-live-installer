"""I8 / NET-02 / NET-04: the daemon's network actions against recorded nmcli output (no real network is touched)."""

from __future__ import annotations

import json
import logging

import pytest

from gentoo_installerd import netactions
from gentoo_installerd.events import ErrorCode
from gentoo_installerd.server import ActionError, ActionRegistry
from tests.backend.helpers import FakeRunner

PASSWORD = "correct-horse-battery"

WIFI_LIST = (
    " :Cafe:42:WPA2:2437 MHz\n"
    "*:Home\\:5:80:WPA2 WPA3:5180 MHz\n"
    " :Home\\:5:55:WPA2 WPA3:2412 MHz\n"
    " :Open Guest:70:--:2462 MHz\n"
    " :Office:90:WPA2 802.1X:5745 MHz\n"
    " :Legacy:30:WEP:2412 MHz\n"
    " :Hidden::60:WPA2:2412 MHz\n"
    " ::50:WPA2:2412 MHz\n"
    " :Sixgig:65:WPA3:6115 MHz\n"
)
DEVICES = "enp5s0:ethernet:connected:Wired connection 1\nwlp4s0:wifi:disconnected:--\nlo:loopback:unmanaged:--\n"
IP_ADDR = json.dumps([
    {"ifname": "lo", "flags": ["LOOPBACK"], "addr_info": []},
    {"ifname": "enp5s0", "flags": ["UP", "LOWER_UP"], "addr_info": [{"family": "inet", "local": "192.168.1.20", "prefixlen": 24}]},
    {"ifname": "wlp4s0", "flags": [], "addr_info": []},
])


class Channel:
    def __init__(self, running: bool = False) -> None:
        self.sent: list = []
        self.install_manager = type("Manager", (), {"is_running": lambda self: running})()

    def send(self, event) -> None:
        self.sent.append(event)

    @property
    def data(self) -> dict:
        assert [e.type for e in self.sent] == ["result", "completed"]
        return self.sent[0].data


@pytest.fixture
def fake(monkeypatch, tmp_path):
    runner = FakeRunner()
    runner.respond("nmcli --version", "nmcli tool, version 1.46")
    runner.respond("nmcli -t -f WIFI-HW,WIFI radio", "enabled:enabled\n")
    runner.respond("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status", DEVICES)
    runner.respond("ip -j addr", IP_ADDR)
    runner.respond("ip -j route", json.dumps([{"dst": "default"}]))
    runner.respond("ping", "ok")
    runner.respond("getent", "1.2.3.4 gentoo.org")
    runner.respond("nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY,FREQ device wifi list", WIFI_LIST)
    monkeypatch.setattr(netactions, "make_runner", lambda: runner)
    monkeypatch.setattr(netactions, "CONNECTIONS_DIR", tmp_path / "system-connections")
    monkeypatch.setattr(netactions, "read_driver", lambda name: "r8169")
    monkeypatch.setattr(netactions, "probe_mirror", lambda host, proxy: True)
    return runner


def test_registration_under_network_v1():
    registry = ActionRegistry()
    netactions.register(registry)
    assert {"network_status", "wifi_scan", "wifi_connect", "wifi_radio", "wifi_live_connections", "network_static"} <= set(registry.actions())
    assert "network.v1" in registry.capabilities()["features"]


# ------------------------------------------------------------------ parsing


def test_terse_fields_unescape_colons():
    assert netactions.split_terse(r"a\:b:c\\d:") == ["a:b", "c\\d", ""]


def test_scan_gives_one_row_per_ssid_sorted_in_use_first(fake):
    channel = Channel()
    netactions.handle_wifi_scan({}, channel)
    rows = channel.data["networks"]
    assert [r["ssid"] for r in rows][0] == "Home:5"  # in use first
    by = {r["ssid"]: r for r in rows}
    assert by["Home:5"]["signal"] == 80 and by["Home:5"]["in_use"] and by["Home:5"]["band"] == "5"
    assert by["Open Guest"]["security"] == "open" and by["Office"]["security"] == "enterprise" and by["Legacy"]["security"] == "wep"
    assert by["Sixgig"]["security"] == "sae" and by["Sixgig"]["band"] == "6" and by["Cafe"]["band"] == "2.4"
    assert "" not in by and len(rows) == len(by)
    assert [r["signal"] for r in rows[1:]] == sorted((r["signal"] for r in rows[1:]), reverse=True)


def test_scan_failure_is_a_clear_answer(fake):
    fake.failures["nmcli -t -f IN-USE"] = 1
    channel = Channel()
    netactions.handle_wifi_scan({}, channel)
    assert channel.data["ok"] is False and channel.data["networks"] == []


@pytest.mark.parametrize("output,reason", [
    ("Error: Connection activation failed: Secrets were required, but not provided.", "wrong_password"),
    ("Error: Timeout expired (45 seconds)", "timeout"),
    ("Error: No network with SSID 'x' found.", "not_found"),
    ("Error: No suitable device found for this connection (device wlp4s0 not available because device is strictly unmanaged).", "no_device"),
    ("Error: Wi-Fi is disabled", "wifi_off"),
    ("something odd", "failed"),
])
def test_failure_reasons(output, reason):
    assert netactions.failure_reason(output) == reason


# ------------------------------------------------------------------ status


def test_status_in_a_wired_vm(fake):
    channel = Channel()
    netactions.handle_network_status({"check_mirror": True}, channel)
    data = channel.data
    assert data["online"] is True and data["problem"] == "" and data["mirror"] is True
    assert data["default_route"] and data["dns"] and data["wifi_present"] and data["wifi_enabled"] and not data["rfkill_hard"]
    wired = next(d for d in data["devices"] if d["name"] == "enp5s0")
    assert wired == {"name": "enp5s0", "kind": "ethernet", "state": "connected", "driver": "r8169", "connection": "Wired connection 1",
                     "ipv4": ["192.168.1.20/24"], "ipv6": []}
    assert next(d for d in data["devices"] if d["name"] == "wlp4s0")["kind"] == "wifi"
    assert all(d["name"] != "lo" for d in data["devices"])


def test_status_reports_the_first_failing_step(fake):
    fake.failures["getent"] = 2
    channel = Channel()
    netactions.handle_network_status({}, channel)
    assert channel.data["problem"] == "no_dns" and channel.data["online"] is False and channel.data["message"]


def test_status_hard_blocked_wifi_without_cable(fake):
    fake.responses = [item for item in fake.responses if not item[0].startswith(("nmcli -t -f WIFI-HW", "nmcli -t -f DEVICE", "ip -j addr"))]
    fake.respond("nmcli -t -f WIFI-HW,WIFI radio", "disabled:enabled\n")
    fake.respond("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status", "wlp4s0:wifi:unavailable:--\n")
    fake.respond("ip -j addr", "[]")
    channel = Channel()
    netactions.handle_network_status({}, channel)
    assert channel.data["problem"] == "wifi_hard_off" and channel.data["rfkill_hard"] is True


# ------------------------------------------------------------------ connect


def test_connect_writes_a_private_keyfile_and_keeps_the_password_out_of_every_command(fake, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    channel = Channel()
    netactions.handle_wifi_connect({"ssid": "Home:5", "password": PASSWORD}, channel)
    data = channel.data
    assert data["ok"] is True and data["ssid"] == "Home:5" and data["uuid"]
    files = list((tmp_path / "system-connections").iterdir())
    assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
    text = files[0].read_text()
    assert f"psk={PASSWORD}" in text and "psk-flags=0" in text and "key-mgmt=wpa-psk" in text
    assert PASSWORD not in " ".join(" ".join(c) for c in fake.commands)
    assert PASSWORD not in caplog.text and PASSWORD not in json.dumps([e.data for e in channel.sent])
    assert ("nmcli", "--wait", "45", "connection", "up", "uuid", data["uuid"]) in fake.commands


def test_connect_failure_deletes_the_profile_and_names_the_reason(fake, tmp_path):
    from gentoo_installerd.runner import RunnerError
    original = fake.run

    def run(argv, **kwargs):
        if tuple(argv[:4]) == ("nmcli", "--wait", "45", "connection"):
            fake.commands.append(tuple(argv))
            raise RunnerError(tuple(argv), 4, "Error: Connection activation failed: Secrets were required, but not provided.")
        return original(argv, **kwargs)
    fake.run = run
    channel = Channel()
    netactions.handle_wifi_connect({"ssid": "Home:5", "password": PASSWORD}, channel)
    assert channel.data["ok"] is False and channel.data["reason"] == "wrong_password"
    assert PASSWORD not in channel.data["message"]
    assert list((tmp_path / "system-connections").iterdir()) == []
    assert any(c[:3] == ("nmcli", "connection", "delete") for c in fake.commands)


def test_open_network_needs_no_password(fake, tmp_path):
    channel = Channel()
    netactions.handle_wifi_connect({"ssid": "Open Guest"}, channel)
    assert channel.data["ok"] is True
    text = next((tmp_path / "system-connections").iterdir()).read_text()
    assert "wifi-security" not in text


@pytest.mark.parametrize("ssid,reason", [("Office", "enterprise"), ("Legacy", "failed")])
def test_enterprise_and_wep_are_not_attempted(fake, tmp_path, ssid, reason):
    channel = Channel()
    netactions.handle_wifi_connect({"ssid": ssid, "password": PASSWORD}, channel)
    assert channel.data["ok"] is False and channel.data["reason"] == reason
    assert not (tmp_path / "system-connections").exists() or list((tmp_path / "system-connections").iterdir()) == []


def test_short_password_is_refused_before_anything_is_written(fake):
    channel = Channel()
    netactions.handle_wifi_connect({"ssid": "Home:5", "password": "short"}, channel)
    assert channel.data["reason"] == "wrong_password" and "short" not in channel.data["message"]


def test_rfkill_blocks_are_named(fake):
    fake.responses = [item for item in fake.responses if not item[0].startswith("nmcli -t -f WIFI-HW")]
    fake.respond("nmcli -t -f WIFI-HW,WIFI radio", "disabled:enabled\n")
    channel = Channel()
    netactions.handle_wifi_connect({"ssid": "Home:5", "password": PASSWORD}, channel)
    assert channel.data["reason"] == "wifi_hard_off" and "Wi-Fi key" in channel.data["message"]
    fake.responses = [item for item in fake.responses if not item[0].startswith("nmcli -t -f WIFI-HW")]
    fake.respond("nmcli -t -f WIFI-HW,WIFI radio", "enabled:disabled\n")
    channel = Channel()
    netactions.handle_wifi_connect({"ssid": "Home:5", "password": PASSWORD}, channel)
    assert channel.data["reason"] == "wifi_off"


def test_bad_ssid_is_a_request_error(fake):
    with pytest.raises(ActionError) as caught:
        netactions.handle_wifi_connect({"ssid": "x" * 40}, Channel())
    assert caught.value.code == ErrorCode.BAD_REQUEST


def test_network_changes_are_refused_during_an_installation(fake):
    for handler, request in ((netactions.handle_wifi_connect, {"ssid": "a", "password": PASSWORD}), (netactions.handle_wifi_radio, {"on": True}),
                             (netactions.handle_network_static, {"dhcp": True})):
        with pytest.raises(ActionError) as caught:
            handler(request, Channel(running=True))
        assert caught.value.code == ErrorCode.BUSY
    netactions.handle_wifi_scan({}, Channel(running=True))  # read-only: allowed


def test_radio(fake):
    channel = Channel()
    netactions.handle_wifi_radio({"on": False}, channel)
    assert ("nmcli", "radio", "wifi", "off") in fake.commands and channel.data["ok"]
    with pytest.raises(ActionError):
        netactions.handle_wifi_radio({"on": "yes"}, Channel())


# ------------------------------------------------------------------ live connections


def test_live_connections_say_whether_the_secret_is_stored(fake):
    fake.respond("nmcli -t -f UUID,TYPE,ACTIVE connection show", "u-1:802-11-wireless:yes\nu-2:802-3-ethernet:yes\nu-3:802-11-wireless:no\n")
    fake.respond("nmcli -t -f 802-11-wireless.ssid,802-11-wireless-security.key-mgmt,802-11-wireless-security.psk-flags connection show uuid u-1",
                 "802-11-wireless.ssid:Home\n802-11-wireless-security.key-mgmt:wpa-psk\n802-11-wireless-security.psk-flags:1 (agent-owned)\n")
    fake.respond("nmcli -t -f 802-11-wireless.ssid,802-11-wireless-security.key-mgmt,802-11-wireless-security.psk-flags connection show uuid u-3",
                 "802-11-wireless.ssid:Cafe\n802-11-wireless-security.key-mgmt:wpa-psk\n802-11-wireless-security.psk-flags:0 (none)\n")
    channel = Channel()
    netactions.handle_wifi_live_connections({}, channel)
    assert channel.data["connections"] == [{"uuid": "u-1", "ssid": "Home", "active": True, "secret_stored": False},
                                           {"uuid": "u-3", "ssid": "Cafe", "active": False, "secret_stored": True}]


# ------------------------------------------------------------------ static address (NET-04)


def test_validation_messages():
    assert netactions.validate_static("192.168.1.50/24", "192.168.1.1", ["1.1.1.1"]) == ("192.168.1.50/24", "192.168.1.1", ["1.1.1.1"])
    for args in (("192.168.1.50", "192.168.1.1", []), ("nope/24", "192.168.1.1", []), ("192.168.1.50/24", "10.0.0.1", []),
                 ("192.168.1.50/24", "x", []), ("192.168.1.50/24", "192.168.1.1", ["dns"]), ("192.168.1.50/24", "fe80::1", [])):
        with pytest.raises(ValueError):
            netactions.validate_static(*args)


def test_static_commands(fake):
    channel = Channel()
    netactions.handle_network_static({"address": "192.168.1.50/24", "gateway": "192.168.1.1", "dns": ["1.1.1.1", "1.0.0.1"]}, channel)
    assert channel.data["ok"] and channel.data["connection"] == "Wired connection 1"
    assert fake.commands[-2] == ("nmcli", "connection", "modify", "Wired connection 1", "ipv4.method", "manual", "ipv4.addresses", "192.168.1.50/24",
                                 "ipv4.gateway", "192.168.1.1", "ipv4.dns", "1.1.1.1,1.0.0.1", "ipv4.ignore-auto-dns", "yes")
    assert fake.commands[-1] == ("nmcli", "--wait", "30", "connection", "up", "Wired connection 1")


def test_static_validation_error_is_a_request_error(fake):
    with pytest.raises(ActionError) as caught:
        netactions.handle_network_static({"address": "192.168.1.50/24", "gateway": "10.9.9.9"}, Channel())
    assert caught.value.code == ErrorCode.BAD_REQUEST
    assert not any("modify" in c for c in fake.commands)


def test_back_to_dhcp(fake):
    channel = Channel()
    netactions.handle_network_static({"dhcp": True}, channel)
    assert ("nmcli", "connection", "modify", "Wired connection 1", "remove", "ipv4.addresses") in fake.commands
    assert fake.commands[-2][4:6] == ("ipv4.method", "auto") and channel.data["ok"]
