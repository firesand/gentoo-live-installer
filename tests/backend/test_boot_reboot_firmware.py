"""B05: the reboot_firmware action (feature hardware.v1), run with fakes only."""

from __future__ import annotations

import pytest

from gentoo_installer import hardware
from gentoo_installerd import hwactions
from gentoo_installerd.server import ActionError, ActionRegistry

from tests.backend.helpers import FakeRunner
from tests.backend.test_backend_hwactions import Channel


@pytest.fixture
def firmware(monkeypatch, tmp_path):
    class Fixture:
        runner = FakeRunner()
        state = hardware.FirmwareInfo(mode="uefi", can_reboot_to_firmware=True)
        variable = tmp_path / "OsIndications"
    fixture = Fixture()
    monkeypatch.setattr(hwactions, "read_firmware", lambda: fixture.state)
    monkeypatch.setattr(hwactions, "make_runner", lambda: fixture.runner)
    monkeypatch.setattr(hwactions, "OS_INDICATIONS_PATH", fixture.variable)
    return fixture


def test_it_is_registered_next_to_the_clock_actions():
    registry = ActionRegistry()
    hwactions.register(registry)
    assert "reboot_firmware" in registry.actions()
    assert registry.lookup("reboot_firmware") is hwactions.handle_reboot_firmware


def test_it_sets_the_firmware_setup_bit_and_restarts(firmware):
    firmware.variable.write_bytes(bytes([7, 0, 0, 0]) + (0x4).to_bytes(8, "little"))
    channel = Channel()
    hwactions.handle_reboot_firmware({}, channel)
    data = firmware.variable.read_bytes()
    assert data[:4] == bytes([7, 0, 0, 0]) and int.from_bytes(data[4:], "little") == 0x5  # the other bits are kept
    commands = firmware.runner.commands
    assert commands[0][:2] == ("chattr", "-i") and commands[1] == ("loginctl", "reboot")  # immutable efivarfs file first
    assert channel.sent[0].data["ok"] is True


def test_a_missing_variable_is_created_with_the_standard_attributes(firmware):
    hwactions.handle_reboot_firmware({}, Channel())
    assert firmware.variable.read_bytes() == bytes([7, 0, 0, 0]) + (1).to_bytes(8, "little")


def test_a_pc_that_cannot_do_it_is_told_so_and_nothing_runs(firmware):
    firmware.state = hardware.FirmwareInfo(mode="bios")
    channel = Channel()
    hwactions.handle_reboot_firmware({}, channel)
    assert channel.sent[0].data["ok"] is False and "firmware" in channel.sent[0].data["message"]
    assert not firmware.runner.commands and not firmware.variable.exists()


def test_it_is_refused_while_an_installation_runs(firmware):
    with pytest.raises(ActionError):
        hwactions.handle_reboot_firmware({}, Channel(running=True))
    assert not firmware.runner.commands
