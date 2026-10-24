"""HWMonux on a small computer: the text installer says what its PySide6 build costs, and never leaves HWMonux out by itself."""

from __future__ import annotations

import pytest

from gentoo_installer import recommend
from gentoo_installer.recommend import Recommendation
from gentoo_installer.textui.inventory import MachineView
from gentoo_installer.textui.summary import HWMONUX_LOW_MEMORY, build_summary

from tests.textui.support import inspect_reply, valid_manifest
from tests.textui.test_wizard_steps import start  # noqa: F401 - a fixture

NOTE = "HWMonux needs PySide6, which is compiled from source: about 40 minutes and up to 4 GiB of memory per compile job."
SERIALS = {"/dev/vda": "VDA-SERIAL-0001"}


def test_the_text_is_the_agreed_sentence() -> None:
    assert HWMONUX_LOW_MEMORY == NOTE


# --- the desktop step: HWMonux comes with every desktop ----------------------------------------------------------------------


def test_the_desktop_step_shows_the_note_on_a_small_computer(start) -> None:
    wizard, script, _ = start([("Which desktop", "3"), ("Which init system", "")])  # the 4 GiB virtual machine
    wizard.step_system()
    assert NOTE in script.flat
    assert "settings file" in script.flat and "graphical installer" in script.flat  # the text installer has no question for it: say where to leave it out
    assert wizard.manifest.options.install_hwmonux is True  # a note, not a decision


def test_an_eight_gib_computer_that_reports_7_9_gib_still_gets_it(start) -> None:
    """MemTotal is a few percent below the memory that was fitted, and four 4 GiB compile jobs do not fit into 8 GiB either."""
    wizard, script, _ = start([("Which desktop", "3"), ("Which init system", "")], inspect_reply("vmware-guest"))
    wizard.step_system()
    assert NOTE in script.flat


def test_a_computer_with_enough_memory_gets_no_note(start) -> None:
    wizard, script, _ = start([("Which desktop", "1"), ("Which init system", "")], inspect_reply("asus-desktop-rtx4090"))
    wizard.step_system()
    assert "PySide6" not in script.flat and wizard.manifest.options.install_hwmonux is True


def test_a_minimal_system_has_no_hwmonux_and_no_note(start) -> None:
    wizard, script, _ = start([("Which desktop", "4"), ("Which init system", "")])
    wizard.step_system()
    assert "PySide6" not in script.flat and wizard.manifest.options.install_hwmonux is False


# --- the summary: every way to start shows it ----------------------------------------------------------------------------------


def small_view() -> MachineView:
    return MachineView(inspect_reply("qemu-q35-virtio", serials=SERIALS))


def test_the_summary_lists_the_note_when_hwmonux_will_be_installed() -> None:
    manifest = valid_manifest()
    assert manifest.options.install_hwmonux
    notes = build_summary(manifest, small_view()).notes
    assert [note for note in notes if "PySide6" in note] == [NOTE]


def test_the_summary_has_no_note_when_it_is_left_out_or_the_memory_is_large() -> None:
    manifest = valid_manifest()
    manifest.options.install_hwmonux = False
    assert not any("PySide6" in note for note in build_summary(manifest, small_view()).notes)
    big = MachineView(inspect_reply("asus-desktop-rtx4090", serials={"/dev/nvme0n1": "X"}))
    assert not any("PySide6" in note for note in build_summary(valid_manifest(), big).notes)


def test_the_summary_never_changes_the_choice() -> None:
    manifest = valid_manifest()
    build_summary(manifest, small_view())
    assert manifest.options.install_hwmonux is True


# --- the service's own advice ----------------------------------------------------------------------------------------------------


def test_the_advice_of_the_service_is_used_when_it_gives_one(monkeypatch, start) -> None:
    advice = Recommendation("rec.hwmonux_low_memory", "info", "Short on memory: HWMonux takes long to build.", "options.install_hwmonux")
    monkeypatch.setattr(recommend, "recommend", lambda profile, manifest: [advice])
    manifest = valid_manifest()
    summary = build_summary(manifest, small_view())
    assert summary.advice == [advice.message]
    assert advice.message not in summary.notes and NOTE not in summary.notes  # said once: the advice already carries it
    wizard, script, _ = start([("Which desktop", "3"), ("Which init system", "")])
    wizard.step_system()
    assert "Short on memory: HWMonux takes long to build." in script.flat and NOTE not in script.flat


@pytest.mark.parametrize("memory_bytes, shown", [(0, False), (int(0.9 * 1024**3), True), (8 * 1024**3, False)])
def test_the_memory_rule_reads_the_reply(memory_bytes: int, shown: bool) -> None:
    reply = inspect_reply("asus-desktop-rtx4090")
    reply["hardware"]["memory"]["total_kib"] = memory_bytes // 1024
    reply["memory_bytes"] = memory_bytes
    notes = build_summary(valid_manifest(), MachineView(reply)).notes
    assert any("PySide6" in note for note in notes) is shown
