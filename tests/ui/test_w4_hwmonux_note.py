"""HWMonux on a small computer: the note next to its tick box. It only informs: the box is never unticked behind the user's back."""

from __future__ import annotations

import pytest

from gentoo_installer import recommend
from gentoo_installer.pages import customize
from gentoo_installer.recommend import Recommendation
from gentoo_installer.textui.summary import HWMONUX_LOW_MEMORY
from tests.ui.conftest import sample_info

NOTE = "HWMonux needs PySide6, which is compiled from source: about 40 minutes and up to 4 GiB of memory per compile job."


def applications_tab(window):
    page = window.customize
    page.tabs.setCurrentIndex(3)  # Hardware, Portage, Network, Applications
    return page


def test_the_window_and_the_text_installer_say_the_same_thing() -> None:
    assert customize.HWMONUX_LOW_MEMORY == HWMONUX_LOW_MEMORY == NOTE


def test_a_small_computer_gets_the_note_next_to_the_hwmonux_box(make_window) -> None:
    window = make_window(info=sample_info(memory_gib=4))
    page = applications_tab(window)
    assert page.hwmonux_note.isVisibleTo(page) and page.hwmonux_note.text() == NOTE
    assert NOTE in page.checks["install_hwmonux"].toolTip()
    assert page.checks["install_hwmonux"].isChecked()  # the note informs; it never decides
    page.save_manifest()
    assert window.manifest.options.install_hwmonux is True


@pytest.mark.parametrize("gib, shown", [(0.9, True), (3.9, True), (7.86, True), (8.0, False), (15.5, False), (46.7, False), (0, False)])
def test_the_note_is_for_computers_with_less_than_8_gib(make_window, gib: float, shown: bool) -> None:
    """MemTotal of an '8 GiB' PC is about 7.8: it is told too, because four 4 GiB compile jobs do not fit into 8 GiB. No reading (0) is no note."""
    window = make_window(info=sample_info(memory_gib=gib))
    page = applications_tab(window)
    assert page.hwmonux_note.isVisibleTo(page) is shown
    assert window.manifest.options.install_hwmonux is True


def test_the_note_follows_the_box_and_the_box_never_follows_the_computer(make_window) -> None:
    window = make_window(info=sample_info(memory_gib=4))
    page = applications_tab(window)
    box = page.checks["install_hwmonux"]
    box.setChecked(False)  # the user leaves HWMonux out: nothing to warn about any more
    assert not page.hwmonux_note.isVisibleTo(page)
    window.apply_info(sample_info(memory_gib=2))  # another look at the computer must not tick it again
    page.save_manifest()
    assert not box.isChecked() and window.manifest.options.install_hwmonux is False and not page.hwmonux_note.isVisibleTo(page)
    box.setChecked(True)
    assert page.hwmonux_note.isVisibleTo(page)
    window.apply_info(sample_info(memory_gib=32))  # a computer with enough memory: the note goes, the choice stays
    assert not page.hwmonux_note.isVisibleTo(page) and box.isChecked()
    page.save_manifest()
    assert window.manifest.options.install_hwmonux is True


def test_a_minimal_system_has_no_hwmonux_and_so_no_note_until_it_is_ticked(make_window) -> None:
    window = make_window(info=sample_info(memory_gib=4))
    window.system.desktop_group.set_value("minimal")
    page = applications_tab(window)
    assert not page.checks["install_hwmonux"].isChecked() and not page.hwmonux_note.isVisibleTo(page)
    page.checks["install_hwmonux"].setChecked(True)  # the user can tick it anyway
    assert page.hwmonux_note.isVisibleTo(page)


def test_the_advice_of_the_service_is_shown_when_it_gives_one(make_window, monkeypatch) -> None:
    advice = Recommendation("rec.hwmonux_low_memory", "info", "Short on memory: HWMonux takes long to build.", "options.install_hwmonux")
    monkeypatch.setattr(recommend, "recommend", lambda profile, manifest: [advice])
    info = sample_info(memory_gib=4)
    info.hardware = {"memory": {"total_kib": 4 * 1024**2}}
    window = make_window(info=info)
    page = applications_tab(window)
    assert page.hwmonux_note.isVisibleTo(page) and page.hwmonux_note.text() == advice.message


def test_a_failing_advice_falls_back_to_the_memory_rule(make_window, monkeypatch) -> None:
    def broken(profile, manifest):
        raise RuntimeError("advice bug")

    monkeypatch.setattr(recommend, "recommend", broken)
    info = sample_info(memory_gib=4)
    info.hardware = {"memory": {"total_kib": 4 * 1024**2}}
    page = applications_tab(make_window(info=info))
    assert page.hwmonux_note.isVisibleTo(page) and page.hwmonux_note.text() == NOTE
