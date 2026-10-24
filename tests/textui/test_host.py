"""The live-system helpers of the console installer (textui.host), with the programs and files replaced."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gentoo_installer.textui import host as host_module
from gentoo_installer.textui.host import COMMON_LOCALES, Host, parse_supported_locales
from gentoo_installer.textui.inventory import MachineView

from tests.hardware.helpers import fixture_path

LSBLK = Path(__file__).resolve().parents[1] / "rescue" / "data" / "author_pc_lsblk.json"


class CannedPrograms:
    """Stands in for subprocess: ``run`` answers like lsblk, everything else fails."""

    class SubprocessError(Exception):
        pass

    class TimeoutExpired(SubprocessError):
        pass

    calls: list[list[str]] = []

    @classmethod
    def run(cls, argv, **kwargs):
        cls.calls.append(list(argv))
        if argv[0] == "lsblk":
            return SimpleNamespace(stdout=LSBLK.read_text(), stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="no such program\n", returncode=127)

    @staticmethod
    def call(argv, **kwargs):
        return 0


def test_the_local_inspection_looks_like_the_services_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    CannedPrograms.calls = []
    monkeypatch.setattr(host_module, "subprocess", CannedPrograms)
    monkeypatch.setenv("GENTOO_HW_ROOT", str(fixture_path("asus-desktop-rtx4090")))  # the probe replays a captured machine and starts no program
    reply = Host().local_inspection()
    assert reply["source"] == "local" and reply["architecture"] and reply["uefi"] is True and reply["memory_bytes"] > 40 * 1024**3
    assert reply["hardware"]["cpu"]["threads"] and reply["warnings"] == reply["hardware"]["warnings"]
    assert CannedPrograms.calls and all(call[0] == "lsblk" for call in CannedPrograms.calls)  # the only program started is the read-only lsblk
    view = MachineView(reply)
    paths = {entry.disk.path for entry in view.disk_entries()}
    assert {"/dev/nvme0n1", "/dev/sda", "/dev/sdb"} <= paths and view.uefi is True and view.network is None
    assert view.profile.gpus and view.tools["lsblk"] in (True, False)


def test_without_lsblk_the_local_inspection_has_no_disks_but_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(CannedPrograms):
        @classmethod
        def run(cls, argv, **kwargs):
            raise FileNotFoundError("lsblk")

    monkeypatch.setattr(host_module, "subprocess", Broken)
    monkeypatch.setenv("GENTOO_HW_ROOT", str(fixture_path("qemu-q35-virtio")))
    reply = Host().local_inspection()
    assert reply["disks"] == [] and reply["hardware"]["machine"]["hypervisor"] == "qemu"


def test_the_programs_the_installer_may_start_report_failures_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_module, "subprocess", CannedPrograms)
    monkeypatch.setattr(host_module.os, "geteuid", lambda: 0)
    ok, why = Host().loadkeys("de-latin1")
    assert not ok and why == "no such program"
    ok, why = Host().reboot()
    assert not ok and why == "no such program"  # tried loginctl, systemctl and reboot
    assert [call[:2] for call in CannedPrograms.calls[-3:]] == [["loginctl", "reboot"], ["systemctl", "reboot"], ["reboot"]]
    monkeypatch.setattr(host_module.os, "geteuid", lambda: 1000)
    Host().loadkeys("us")
    assert CannedPrograms.calls[-1] == ["sudo", "-n", "loadkeys", "us"]
    assert Host().run_interactive(["nmtui"]) == 0


def test_a_missing_program_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    class Missing(CannedPrograms):
        @classmethod
        def run(cls, argv, **kwargs):
            raise FileNotFoundError(argv[0])

    monkeypatch.setattr(host_module, "subprocess", Missing)
    monkeypatch.setattr(host_module.os, "geteuid", lambda: 0)
    assert Host().loadkeys("us") == (False, "loadkeys is not installed on this system")


def test_the_supported_locales_of_glibc_are_read_and_a_short_list_is_the_fallback(tmp_path: Path) -> None:
    supported = tmp_path / "SUPPORTED"
    supported.write_text("# comment\nde_DE.UTF-8 UTF-8\nde_DE ISO-8859-1\nen_US.UTF-8 UTF-8\nde_DE@euro ISO-8859-15\nsr_RS.UTF-8@latin UTF-8\n")
    assert Host(supported_locales=supported).locales() == ["en_US.UTF-8", "de_DE.UTF-8", "sr_RS.UTF-8@latin"]
    assert Host(supported_locales=tmp_path / "missing").locales() == list(COMMON_LOCALES)
    assert parse_supported_locales("") == []


def test_the_time_zones_and_keymaps_come_from_the_system() -> None:
    host = Host()
    zones = host.timezones()
    assert "UTC" in zones or zones == []  # a system without a zone database lists none
    assert isinstance(host.console_keymaps(), set)
