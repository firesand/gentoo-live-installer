"""CORE-06: the review page: one scrolling page, grouped rows with Edit links, previews, a Start button that waits for the typed disk name."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QAbstractScrollArea, QScrollArea

from gentoo_installer.app import REVIEW, STORAGE, SYSTEM
from gentoo_installer.widgets import Page

SIZES = [(1366, 768), (1024, 600)]


def to_review(window, size=None):
    if size:
        window.resize(*size)
    window.storage.disk_group.set_value("/dev/sda")
    window.storage.save_manifest()
    window.accounts.password.setText("user-password")
    window.accounts.password_confirm.setText("user-password")
    window.accounts.root_password.setText("root-password")
    window.accounts.root_confirm.setText("root-password")
    for page in window.pages[:6]:
        page.save_manifest()
    window._show_page(REVIEW)
    return window.review


@pytest.mark.parametrize("size", SIZES)
def test_no_scroll_area_is_nested_in_the_summary(make_window, size):
    window = make_window()
    review = to_review(window, size)
    inner = [w for w in review.findChildren(QAbstractScrollArea) if w is not review.scroll_area]
    assert inner == []
    assert [w for w in review.findChildren(QScrollArea)] == [review.scroll_area]


@pytest.mark.parametrize("size", SIZES)
def test_start_waits_for_the_typed_disk_name(make_window, size):
    window = make_window()
    review = to_review(window, size)
    assert not window.next.isEnabled()
    assert window.next.text() == "Type the disk name to enable"
    assert window.next.objectName() != "dangerButton"  # grey, not red, until it can be pressed
    review.confirmation.setText("/dev/sd")
    assert not window.next.isEnabled()
    review.confirmation.setText("/dev/sda")
    assert window.next.isEnabled() and window.next.text() == "&Start installation"
    assert window.next.objectName() == "dangerButton"
    review.confirmation.setText("")
    assert not window.next.isEnabled()
    # The erase warning and the typed confirmation are above the summary.
    order = review.content
    assert order.indexOf(review.warning) < order.indexOf(review.confirm_card) < order.indexOf(review.groups)


def test_rows_are_plain_and_grouped_by_step_with_edit_links(make_window):
    window = make_window()
    review = to_review(window)
    rows = {label: (title, value) for title, label, value in review.rows()}
    for label in ("Init system", "Kernel", "Initramfs", "Boot menu", "Secure Boot", "Administrator tool", "Groups", "Root login",
                  "Language", "Time zone", "Keyboard", "Swap", "Filesystem tools", "Graphics driver", "Remote login (SSH)"):
        assert label in rows, label
    assert rows["Init system"] == ("Base system", "OpenRC")
    assert rows["Kernel"][1] == "Gentoo prebuilt kernel"
    assert rows["Administrator tool"][1] == "sudo" and "wheel" in rows["Groups"][1]
    assert rows["Time zone"][0] == "Accounts" and rows["Swap"][0] == "Storage"
    edits = [b for b in review.findChildren(type(window.back)) if b.text() == "Edit"]
    assert len(edits) == 4
    seen = []
    review.editRequested.connect(seen.append)
    next(b for b in edits if b.accessibleName() == "Edit Storage").click()
    assert seen == [STORAGE]


def test_edit_link_goes_back_to_that_step(make_window):
    window = make_window()
    review = to_review(window)
    next(b for b in review.findChildren(type(window.back)) if b.accessibleName() == "Edit Base system").click()
    assert window.current == SYSTEM


def test_a_page_adds_its_rows_without_touching_the_review(make_window):
    window = make_window()
    window.system.summary_rows = lambda: [("Init system", "OpenRC (your choice)"), ("Extra thing", "yes")]  # type: ignore[method-assign]
    review = to_review(window)
    rows = {label: (title, value) for title, label, value in review.rows()}
    assert rows["Init system"] == ("Base system", "OpenRC (your choice)")  # the page's value wins, listed once
    assert rows["Extra thing"] == ("Base system", "yes")
    assert [label for _, label, _ in review.rows()].count("Init system") == 1
    assert Page.summary_rows(window.system) == []


def test_previews_are_collapsed_in_guided_and_open_in_advanced(make_window):
    window = make_window()
    review = to_review(window)
    titles = [section.title for section in review.previews]
    assert titles[:3] == ["Partition plan (Handbook table)", "/etc/fstab", "make.conf and package.use"]
    assert not any(section.is_open() for section in review.previews)
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    assert all(section.is_open() for section in review.previews)
    texts = " ".join(label.text() for section in review.previews for label in section.findChildren(type(review.warning)))
    assert "ACCEPT_LICENSE" in texts or "COMMON_FLAGS" in texts
    assert "UUID=" in texts
