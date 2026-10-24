"""CORE-10: the sidebar rail below 1100 px, the one-line header, dark badges, tab bar and selected-card styling."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTabBar

from gentoo_installer import theme
from gentoo_installer.theme import apply_theme
from gentoo_installer.widgets import ChoiceCard, ChoiceGroup, SelectableCard
from tests.ui.test_help_widgets import contrast

SIZES = [(1024, 600), (1366, 768)]


@pytest.fixture
def scheme(qapp):
    """Force a colour scheme for one test and restore the light one afterwards."""

    def use(dark: bool) -> None:
        apply_theme(qapp, dark)

    yield use
    apply_theme(qapp, False)


@pytest.mark.parametrize("size", SIZES)
def test_the_sidebar_is_a_numbered_rail_below_1100_px(make_window, size):
    window = make_window()
    window.resize(*size)
    QApplication.processEvents()
    if size[0] < theme.RAIL_BELOW:
        assert window.rail and window.sidebar.width() < 80
        assert window.step_buttons[0].text() == "1" and window.step_buttons[2].text() == "3"
        assert "Storage" in window.step_buttons[2].toolTip() and window.step_buttons[2].accessibleName() == "Storage"
        assert not window.brand_label.isVisibleTo(window.sidebar)
    else:
        assert not window.rail and window.sidebar.width() == theme.SIDEBAR_WIDTH
        assert window.step_buttons[2].text().endswith("Storage")


def test_the_rail_comes_and_goes_with_the_window_width(make_window):
    window = make_window()
    window.resize(1000, 640)
    QApplication.processEvents()
    assert window.rail
    window._mark_problem(window.pages[1], True)
    assert window.step_buttons[1].text() == "2●"
    window.resize(1300, 700)
    QApplication.processEvents()
    assert not window.rail and window.step_buttons[1].text() == "2   Readiness   ●"


def test_the_page_header_is_one_line(make_window):
    window = make_window()
    window.resize(1024, 600)
    QApplication.processEvents()
    assert abs(window.header_title.geometry().center().y() - window.header_subtitle.geometry().center().y()) < 12
    assert window.header_title.geometry().right() <= window.header_subtitle.geometry().left()


@pytest.mark.parametrize("dark", [False, True])
def test_every_badge_keeps_its_contrast_and_the_dark_ones_are_outlines(dark):
    for kind in theme.BADGE_COLORS["dark" if dark else "light"]:
        text, fill = theme.badge_colors(kind, dark)
        assert contrast(text, fill) >= 4.5, (kind, dark)
        if dark:
            assert fill == "#292631" and theme.badge_outline(kind, dark) != fill  # an outline pill on the card colour
            assert contrast(theme.badge_outline(kind, dark), fill) >= 3.0  # and the outline itself can be seen


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("size", SIZES)
def test_screenshots_render_in_both_schemes_at_both_sizes(make_window, scheme, dark, size):
    scheme(dark)
    window = make_window()
    window.resize(*size)
    QApplication.processEvents()
    for page in (0, 6):
        window._show_page(page)
        image = window.grab().toImage()
        assert image.width() > 0 and image.height() > 0
    background = window.grab().toImage().pixelColor(window.width() // 2, 3).lightness()
    assert (background < 100) == dark  # the page really is drawn in the scheme


def test_the_selected_card_has_a_filled_mark_and_the_tab_bar_is_styled(qtbot, scheme):
    scheme(False)
    group = ChoiceGroup([("a", "Alpha", "first", False), ("b", "Beta", "second", True)])
    qtbot.addWidget(group)
    group.set_value("b")
    assert group.cards["b"].mark.text() == "◉" and group.cards["a"].mark.text() == "○"
    assert group.cards["b"].property("selected") is True
    assert SelectableCard is ChoiceCard
    sheet = QApplication.instance().styleSheet()
    assert "QFrame#choiceCard[selected=true] { border: 2px solid" in sheet
    assert "QTabBar::tab:selected" in sheet and "border-radius: 9px" in sheet
    assert QTabBar().styleSheet() == ""  # the rule comes from the application style sheet, not a per-widget copy
