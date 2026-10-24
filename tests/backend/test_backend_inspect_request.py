"""The inspect action with the settings the installer window sends along (mirror, proxy, what the selection needs)."""

from __future__ import annotations

import pytest

from gentoo_installerd import inspect as insp
from gentoo_installerd import server
from gentoo_installerd.runner import CommandResult

from tests.backend.helpers import lsblk


class Runner:
    def run(self, argv, **kwargs):
        return CommandResult(tuple(argv), 0, lsblk() if argv[0] == "lsblk" else "")


class Channel:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, event) -> None:
        self.sent.append(event)


@pytest.fixture
def inspect_action(monkeypatch):
    calls: list[tuple[str, str]] = []

    def network(host, port, proxy="", timeout=5.0):
        calls.append((f"{host}:{port}", proxy))
        return True

    monkeypatch.setattr(insp, "_network_ready", network)
    monkeypatch.setattr(server, "SubprocessRunner", Runner)

    def run(request: dict):
        channel = Channel()
        server.handle_inspect(request, channel)
        assert [event.type for event in channel.sent] == ["status", "completed"]
        return channel.sent[0].data, calls

    return run


def settings(**sections) -> dict:
    return {"action": "inspect", "manifest": sections}


def test_the_network_check_goes_where_the_install_will_go(inspect_action):
    data, calls = inspect_action(settings(portage={"mirror": "https://mirror.example.org:8443/gentoo"}, network={"proxy": "http://proxy.lan:3128"}))
    assert calls == [("mirror.example.org:8443", "http://proxy.lan:3128")]
    assert data["network"] is True


def test_without_settings_the_default_mirror_is_checked_and_nothing_is_claimed_about_a_selection(inspect_action):
    data, calls = inspect_action({"action": "inspect"})
    assert calls == [("distfiles.gentoo.org:443", "")]
    assert "missing_for_selection" not in data


def test_tools_the_selection_needs_but_the_live_system_lacks_are_listed(inspect_action, monkeypatch):
    monkeypatch.setattr(insp.shutil, "which", lambda tool: None if tool in {"cryptsetup", "mkfs.btrfs", "btrfs"} else "/usr/bin/" + tool)
    data, _ = inspect_action(settings(storage={"luks": True, "filesystem": "btrfs", "swap_mib": 0}))
    assert data["missing_for_selection"] == ["btrfs", "cryptsetup", "mkfs.btrfs"]
    data, _ = inspect_action(settings(storage={"luks": False, "filesystem": "ext4"}))
    assert data["missing_for_selection"] == []


def test_signing_tools_are_not_needed_on_the_live_system_for_secure_boot(inspect_action, monkeypatch):
    monkeypatch.setattr(insp.shutil, "which", lambda tool: None if tool in {"sbsign", "sbverify"} else "/usr/bin/" + tool)
    data, _ = inspect_action(settings(system={"secure_boot": True, "bootloader": "systemd-boot", "uki": True}))
    assert data["missing_for_selection"] == []
    assert data["tools"]["sbsign"] is False  # still reported, for anyone who wants to know


@pytest.mark.parametrize("payload", ["text", 5, ["storage"], {"surprise": 1}, {"storage": {"luks": "yes"}}, {"storage": "x"}])
def test_settings_that_make_no_sense_are_ignored_not_an_error(inspect_action, payload):
    data, calls = inspect_action({"action": "inspect", "manifest": payload})
    assert calls == [("distfiles.gentoo.org:443", "")] and "missing_for_selection" not in data and "architecture" in data
