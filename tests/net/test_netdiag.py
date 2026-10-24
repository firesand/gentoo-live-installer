"""NET-01: the Handbook's tests in order, each failing step with its sentence (canned ip, ping and getent output)."""

from __future__ import annotations

import json

import pytest

from gentoo_installer import netdiag


def ip_addr(*rows):
    return json.dumps([{"ifname": name, "flags": flags, "addr_info": [{"family": "inet", "local": addr, "prefixlen": 24}] if addr else []}
                       for name, flags, addr in rows])


LO = ("lo", ["LOOPBACK", "UP", "LOWER_UP"], "127.0.0.1")
WIRED_UP = ("enp5s0", ["BROADCAST", "UP", "LOWER_UP"], "192.168.1.20")
WIRED_NOADDR = ("enp5s0", ["BROADCAST", "UP", "LOWER_UP"], None)
WIRED_NOCABLE = ("enp5s0", ["BROADCAST", "UP"], None)
WIFI_DOWN = ("wlp4s0", ["BROADCAST"], None)
ROUTE = json.dumps([{"dst": "default", "gateway": "192.168.1.1"}])


def runner(addr, route=ROUTE, ping="1 packets", lookup="104.18.1.1 gentoo.org"):
    answers = {"ip -j addr": addr, "ip -j route": route}

    def run(argv):
        key = " ".join(argv)
        if key.startswith("ping"):
            return ping
        if key.startswith("getent"):
            return lookup
        return answers.get(key)
    return run


def test_everything_works():
    diagnosis = netdiag.diagnose(runner(ip_addr(LO, WIRED_UP)), mirror=lambda: True)
    assert diagnosis.online and diagnosis.problem == "" and all(step.ok for step in diagnosis.steps)
    assert diagnosis.summary_lines() == ["Wired: enp5s0 (connected, 192.168.1.20)"]


def test_only_loopback_means_no_device_and_names_hardware_or_driver():
    diagnosis = netdiag.diagnose(runner(ip_addr(LO)))
    assert diagnosis.problem == "no_device" and "driver" in diagnosis.message


def test_cable_connected_but_no_address():
    diagnosis = netdiag.diagnose(runner(ip_addr(LO, WIRED_NOADDR)))
    assert diagnosis.problem == "no_connection"
    assert diagnosis.message.startswith("Your cable is connected but no address was given")


def test_unplugged_cable():
    diagnosis = netdiag.diagnose(runner(ip_addr(LO, WIRED_NOCABLE)))
    assert diagnosis.problem == "cable" and "cable" in diagnosis.message


def test_wifi_not_connected_says_to_pick_a_network():
    diagnosis = netdiag.diagnose(runner(ip_addr(LO, WIFI_DOWN)))
    assert diagnosis.problem == "no_connection" and "Pick your network" in diagnosis.message
    assert diagnosis.summary_lines() == ["Wi-Fi: wlp4s0 (not connected)"]


def test_wifi_switched_off_and_hard_blocked():
    off = netdiag.diagnose(runner(ip_addr(LO, WIFI_DOWN)), wifi_enabled=False)
    hard = netdiag.diagnose(runner(ip_addr(LO, WIFI_DOWN)), rfkill_hard=True)
    assert off.problem == "wifi_off" and hard.problem == "wifi_hard_off" and "Wi-Fi key" in hard.message


def test_no_default_route():
    diagnosis = netdiag.diagnose(runner(ip_addr(LO, WIRED_UP), route="[]"))
    assert diagnosis.problem == "no_route" and [s.ok for s in diagnosis.steps][:3] == [True, True, False]


def test_ping_fails_means_no_route_to_the_internet():
    diagnosis = netdiag.diagnose(runner(ip_addr(LO, WIRED_UP), ping=None))
    assert diagnosis.problem == "no_route" and diagnosis.steps[3].ok is False


def test_dns_fails():
    diagnosis = netdiag.diagnose(runner(ip_addr(LO, WIRED_UP), lookup=None))
    assert diagnosis.problem == "no_dns" and "1.1.1.1" in diagnosis.message


@pytest.mark.parametrize("proxy,problem", [(False, "no_mirror"), (True, "proxy")])
def test_mirror_fails(proxy, problem):
    diagnosis = netdiag.diagnose(runner(ip_addr(LO, WIRED_UP)), mirror=lambda: False, proxy=proxy)
    assert diagnosis.problem == problem and diagnosis.steps[5].ok is False


def test_every_problem_code_has_a_message():
    for problem in netdiag.PROBLEMS:
        assert problem in netdiag.MESSAGES


def test_garbage_input_is_no_adapters():
    assert netdiag.parse_ip_addr("not json") == []
    assert netdiag.has_default_route("default via 10.0.0.1 dev eth0")
    assert not netdiag.resolved(None)
