"""I9: remote_status, remote_enable and remote_disable on the live medium (fakes only)."""

from __future__ import annotations

import json
import logging

import pytest

from gentoo_installerd import remote
from gentoo_installerd.events import ErrorCode
from gentoo_installerd.server import ActionError, ActionRegistry
from tests.backend.helpers import FakeRunner

IP_ADDR = json.dumps([{"ifname": "lo", "flags": ["LOOPBACK"], "addr_info": []},
                      {"ifname": "enp5s0", "flags": ["UP"], "addr_info": [{"family": "inet", "local": "192.168.1.20", "prefixlen": 24}]}])
FINGERPRINT = "256 SHA256:abcDEF123 root@livecd (ED25519)"


class Channel:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, event) -> None:
        self.sent.append(event)

    @property
    def data(self) -> dict:
        assert [e.type for e in self.sent] == ["result", "completed"]
        return self.sent[0].data


@pytest.fixture
def live(monkeypatch):
    runner = FakeRunner()
    runner.respond("ip -j addr", IP_ADDR)
    runner.respond("passwd -S gentoo", "gentoo NP 2026-10-24 0 99999 7 -1\n")
    runner.respond("ssh-keygen -l", FINGERPRINT)
    monkeypatch.setattr(remote, "make_runner", lambda: runner)
    monkeypatch.setattr(remote, "is_live_medium", lambda: True)
    monkeypatch.setattr(remote, "host_key_files", lambda: ["/etc/ssh/ssh_host_ed25519_key.pub"])
    return runner


def test_registration():
    registry = ActionRegistry()
    remote.register(registry)
    assert {"remote_status", "remote_enable", "remote_disable"} <= set(registry.actions())
    assert "remote.v1" in registry.capabilities()["features"]


def test_status_in_the_boot_state(live):
    channel = Channel()
    remote.handle_remote_status({}, channel)
    data = channel.data
    assert data["enabled"] is False and data["sshd_running"] is True and data["user"] == "gentoo" and data["port"] == 22
    assert data["addresses"] == ["192.168.1.20"] and data["fingerprints"] == [{"type": "ED25519", "sha256": "SHA256:abcDEF123"}]


def test_enable_sets_the_password_on_stdin_only_and_returns_it_once(live, caplog):
    caplog.set_level(logging.DEBUG)
    live.responses = [item for item in live.responses if not item[0].startswith("passwd -S")]
    live.respond("passwd -S gentoo", "gentoo P 2026-10-24 0 99999 7 -1\n")
    channel = Channel()
    remote.handle_remote_enable({"password": "sesame-seed-42"}, channel)
    data = channel.data
    assert data["ok"] and data["password"] == "sesame-seed-42" and data["enabled"] is True and data["generated"] is False
    index = live.commands.index(("chpasswd",))
    assert live.inputs[index] == "gentoo:sesame-seed-42\n" and live.secrets[index] is True
    assert "sesame-seed-42" not in " ".join(" ".join(c) for c in live.commands) and "sesame-seed-42" not in caplog.text


def test_a_generated_password_is_three_words_and_two_digits(live):
    channel = Channel()
    remote.handle_remote_enable({}, channel)
    password = channel.data["password"]
    words = password[:-2].split("-")
    assert channel.data["generated"] is True and len(words) == 3 and all(w in remote.WORDS for w in words) and password[-2:].isdigit()
    assert live.inputs[live.commands.index(("chpasswd",))] == f"gentoo:{password}\n"


def test_enable_starts_sshd_when_it_is_not_running(live):
    live.failures["rc-service sshd status"] = 3
    remote.handle_remote_enable({}, Channel())
    assert ("rc-service", "sshd", "start") in live.commands


def test_disable_restores_the_empty_password(live):
    channel = Channel()
    remote.handle_remote_disable({}, channel)
    assert ("passwd", "-d", "gentoo") in live.commands and channel.data["ok"]


def test_short_password_and_control_characters_are_refused(live):
    for bad in ("abc", "bad\npass1234"):
        with pytest.raises(ActionError) as caught:
            remote.handle_remote_enable({"password": bad}, Channel())
        assert caught.value.code == ErrorCode.BAD_REQUEST
    assert ("chpasswd",) not in live.commands


def test_refused_on_an_installed_system(live, monkeypatch):
    monkeypatch.setattr(remote, "is_live_medium", lambda: False)
    for handler in (remote.handle_remote_status, remote.handle_remote_enable, remote.handle_remote_disable):
        with pytest.raises(ActionError):
            handler({}, Channel())
    assert live.commands == []
