"""HWMonux on a small computer: the text installer says what its PySide6 build costs, and never leaves HWMonux out by itself."""

from __future__ import annotations

import pytest

from gentoo_installer import recommend
from gentoo_installer.recommend import Recommendation
from gentoo_installer.textui.console import Console
from gentoo_installer.textui.daemon import Daemon
from gentoo_installer.textui.inventory import MachineView
from gentoo_installer.textui.summary import HWMONUX_LOW_MEMORY, ONLY_ELSEWHERE, Summary, build_summary
from gentoo_installer.textui.wizard import Wizard

from tests.textui.support import FakeHost, FakeInstaller, Script, inspect_reply, valid_manifest

NOTE = "HWMonux needs PySide6, which is compiled from source: about 40 minutes and up to 4 GiB of memory per compile job."
SERIALS = {"/dev/vda": "VDA-SERIAL-0001"}


@pytest.fixture
def start(installer, host: FakeHost):
    """``start(steps, reply=None)`` -> (wizard, script, fake service); the default machine is the 4 GiB virtual machine."""

    def make(steps, reply=None):
        fake: FakeInstaller = installer(reply if reply is not None else inspect_reply("qemu-q35-virtio", serials=SERIALS))
        script = Script(steps)
        console = Console(script.stdin, script.out, environ={"TERM": "dumb"})
        daemon = Daemon(fake.path)
        wizard = Wizard(console, host, daemon, view=MachineView(fake.reply, local_by_path=host.by_path_names()), state=daemon.probe())
        return wizard, script, fake

    return make


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


def pyside_lines(summary: Summary) -> list[str]:
    return [line for line in summary.notes + summary.advice if "PySide6" in line]


def test_the_summary_says_it_once_when_hwmonux_will_be_installed() -> None:
    """The service advises about HWMonux below 8 GiB (rec.hwmonux_low_memory): the summary shows that advice once, and where to change it."""
    manifest = valid_manifest()
    assert manifest.options.install_hwmonux
    summary = build_summary(manifest, small_view())
    [line] = pyside_lines(summary)
    assert line in summary.advice and NOTE in line and line.endswith(ONLY_ELSEWHERE)


def test_without_advice_from_the_service_the_summary_has_its_own_note(monkeypatch) -> None:
    monkeypatch.setattr(recommend, "recommend", lambda profile, manifest: [])
    summary = build_summary(valid_manifest(), small_view())
    assert pyside_lines(summary) == [NOTE] and NOTE in summary.notes


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
    assert summary.advice == [f"{advice.message} {ONLY_ELSEWHERE}"]  # the text installer has no question for HWMonux
    assert advice.message not in summary.notes and NOTE not in summary.notes  # said once: the advice already carries it
    wizard, script, _ = start([("Which desktop", "3"), ("Which init system", "")])
    wizard.step_system()
    assert "Short on memory: HWMonux takes long to build." in script.flat and NOTE not in script.flat


@pytest.mark.parametrize("memory_bytes, shown", [(0, False), (int(0.9 * 1024**3), True), (8 * 1024**3, False)])
def test_the_memory_rule_reads_the_reply(memory_bytes: int, shown: bool) -> None:
    reply = inspect_reply("asus-desktop-rtx4090")
    reply["hardware"]["memory"]["total_kib"] = memory_bytes // 1024
    reply["memory_bytes"] = memory_bytes
    assert len(pyside_lines(build_summary(valid_manifest(), MachineView(reply)))) == int(shown)
