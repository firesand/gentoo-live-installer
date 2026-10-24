"""pages/help.py (HelpNote, Badge, HANDBOOK_LINKS, AdvancedSection) and the window's Detail level switch."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLineEdit, QWidget

from gentoo_installer import theme
from gentoo_installer.pages import help as help_module
from gentoo_installer.pages.help import BADGE_KINDS, HANDBOOK_LINKS, AdvancedSection, Badge, HelpNote, handbook_url

PREFIX = "https://wiki.gentoo.org/wiki/Handbook:AMD64/"


def test_every_handbook_link_points_into_the_amd64_handbook():
    for topic in ("Disks", "Stage", "Base", "Kernel", "System", "Tools", "Bootloader", "Finalizing", "Networking"):
        assert topic in HANDBOOK_LINKS
    for topic, url in HANDBOOK_LINKS.items():
        assert url.startswith(PREFIX), (topic, url)
        assert " " not in url, (topic, url)
    assert HANDBOOK_LINKS["Disks"].endswith("/Installation/Disks")


def test_handbook_url_accepts_a_topic_or_a_path():
    assert handbook_url("Kernel") == HANDBOOK_LINKS["Kernel"]
    assert handbook_url("Installation/Disks#Partitioning") == PREFIX + "Installation/Disks#Partitioning"


def test_help_note_shows_the_sentence_and_a_handbook_link(qapp, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(help_module, "open_url", lambda url: opened.append(url) or True)
    note = HelpNote("Dracut builds the first RAM disk.", "Kernel")
    note.show_note()
    assert note.popup is not None and note.popup.label.text() == "Dracut builds the first RAM disk."
    assert note.popup.link is not None and HANDBOOK_LINKS["Kernel"] in note.popup.link.text()
    note.popup.link.linkActivated.emit(HANDBOOK_LINKS["Kernel"])
    assert opened == [HANDBOOK_LINKS["Kernel"]]
    assert note.accessibleName() and note.text == note.accessibleDescription()
    note.popup.close()


def test_help_note_without_an_anchor_has_no_link_and_works_offline(qapp, monkeypatch):
    def offline(url):
        raise OSError("no browser")

    monkeypatch.setattr(help_module.QDesktopServices, "openUrl", offline)
    note = HelpNote("Only a sentence.")
    note.show_note()
    assert note.popup.link is None and note.popup.label.text() == "Only a sentence."
    assert note.open_handbook() is False
    linked = HelpNote("A sentence.", "Disks")
    assert linked.open_handbook() is False  # the browser failed: nothing raised, the sentence is still shown
    linked.show_note()
    assert linked.popup.label.text() == "A sentence."
    linked.popup.close()
    note.popup.close()


def contrast(first: str, second: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    a, b = sorted((luminance(first), luminance(second)), reverse=True)
    return (a + 0.05) / (b + 0.05)


@pytest.mark.parametrize("dark", [False, True])
def test_badge_text_is_readable_in_both_themes(dark):
    for kind in BADGE_KINDS:
        foreground, background = theme.badge_colors(kind, dark)
        assert contrast(foreground, background) >= 4.5, (kind, dark)


def test_badges_build_for_every_kind_and_refuse_others(qapp):
    for kind in ("Default", "Alternative", "Optional", "Recommended"):
        badge = Badge(kind)
        assert badge.text() == kind and badge.accessibleName() == kind
        assert theme.badge_colors(kind)[0] in badge.styleSheet()
    with pytest.raises(ValueError):
        Badge("Mandatory")


def test_advanced_section_follows_the_detail_level_and_keeps_values(qapp):
    section = AdvancedSection("test-page-a")
    edit = QLineEdit()
    section.add_widget(edit)
    section.show()
    assert not section.is_open()  # Guided: collapsed...
    edit.setText("kept")
    section.set_advanced(True)
    assert section.is_open() and edit.isVisible()
    section.set_advanced(False)
    assert not section.is_open() and edit.text() == "kept"  # ...but the value survives
    section.set_open(True)  # still reachable by hand
    assert section.is_open()
    section.set_advanced(True)
    section.set_advanced(False)
    assert section.is_open()  # Guided returns to what the user chose
    section.close()


def test_advanced_section_remembers_its_state_per_page(qapp):
    first = AdvancedSection("test-page-b")
    first.set_open(True)
    again = AdvancedSection("test-page-b")
    other = AdvancedSection("test-page-c")
    assert again.is_open() and not other.is_open()


def test_the_window_has_a_detail_level_switch_that_reaches_every_page(make_window):
    window = make_window()
    assert window.mode.accessibleName() == "Detail level"
    assert window.mode.itemText(0) == "Guided (recommended choices)" and window.mode.itemText(1) == "Advanced (every option)"
    assert window.mode.toolTip()
    seen: dict[int, list[bool]] = {}
    for index, page in enumerate(window.pages):
        assert callable(getattr(page, "set_advanced", None)), type(page).__name__
        original = page.set_advanced
        page.set_advanced = lambda advanced, i=index, o=original: (seen.setdefault(i, []).append(advanced), o(advanced))[1]
    window.mode.setCurrentIndex(1)
    window.mode.setCurrentIndex(0)
    assert seen == {index: [True, False] for index in range(len(window.pages))}


def test_switching_the_detail_level_keeps_what_was_entered(make_window):
    window = make_window()
    window.customize.mirror.setText("https://example.org/gentoo")
    window.system.signing_key.setText("/keys/db.key")
    window.mode.setCurrentIndex(1)
    window.mode.setCurrentIndex(0)
    assert window.customize.mirror.text() == "https://example.org/gentoo"
    assert window.system.signing_key.text() == "/keys/db.key"
    assert isinstance(window.system.boot_section, QWidget)
