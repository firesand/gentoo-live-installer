"""Accessibility, keyboard use, display robustness, launchers, the Welcome page and the keyboard layout."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAccessible, QAccessibleActionInterface, QColor, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QMainWindow, QMessageBox

from gentoo_installer import app as app_module
from gentoo_installer import keyboard, theme
from gentoo_installer.app import bounded_window_size
from gentoo_installer.manifest import InstallManifest
from gentoo_installer.widgets import Card, ChoiceGroup, Page, StatusRow


@pytest.fixture
def themed(qapp):
    """The real stylesheet, restored afterwards so other tests keep the plain style."""
    style, palette, sheet = qapp.style().objectName(), qapp.palette(), qapp.styleSheet()
    saved = (theme.TEXT, theme.MUTED, theme.SURFACE, theme.BACKGROUND, theme.BORDER)
    theme.apply_theme(qapp)
    yield qapp
    qapp.setStyleSheet(sheet)
    qapp.setPalette(palette)
    qapp.setStyle(style)
    theme.TEXT, theme.MUTED, theme.SURFACE, theme.BACKGROUND, theme.BORDER = saved


def luminance(color: QColor) -> float:
    def channel(value: int) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green()) + 0.0722 * channel(color.blue())


def contrast(a: QColor, b: QColor) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# --- G8: radio semantics and keyboard ------------------------------------------------------------------------


def group(qtbot, count=3, **kwargs):
    widget = ChoiceGroup([(f"k{i}", f"Choice {i}", f"Description {i}", False) for i in range(count)], columns=1, **kwargs)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def test_choice_cards_are_radio_buttons_for_assistive_technology(qtbot):
    choices = group(qtbot)
    first, second = choices.cards["k0"], choices.cards["k1"]
    interface = QAccessible.queryAccessibleInterface(first)
    assert interface.role() == QAccessible.Role.RadioButton
    assert interface.text(QAccessible.Text.Name) == "Choice 0"
    assert interface.state().checkable and interface.state().focusable
    assert not interface.state().checked
    first.setChecked(True)
    assert interface.state().checked
    assert not QAccessible.queryAccessibleInterface(second).state().checked
    assert QAccessibleActionInterface.pressAction() in interface.actionInterface().actionNames()
    QAccessible.queryAccessibleInterface(second).actionInterface().doAction(QAccessibleActionInterface.pressAction())
    assert second.isChecked() and not first.isChecked()


def test_only_the_selected_card_is_in_the_tab_chain_and_arrows_move_the_selection(qtbot):
    choices = group(qtbot)
    assert [c.focusPolicy() == Qt.FocusPolicy.StrongFocus for c in choices.cards.values()] == [True, False, False]
    choices.set_value("k1")
    assert [c.focusPolicy() == Qt.FocusPolicy.StrongFocus for c in choices.cards.values()] == [False, True, False]
    QTest.keyClick(choices.cards["k1"], Qt.Key.Key_Down)
    assert choices.value() == "k2"
    QTest.keyClick(choices.cards["k2"], Qt.Key.Key_Down)
    assert choices.value() == "k0"  # wraps around
    QTest.keyClick(choices.cards["k0"], Qt.Key.Key_Up)
    assert choices.value() == "k2"
    QTest.keyClick(choices.cards["k2"], Qt.Key.Key_Space)
    assert choices.value() == "k2"


def test_arrow_keys_skip_disabled_cards(qtbot):
    choices = group(qtbot)
    choices.set_card_enabled("k1", False, "not here")
    choices.set_value("k0")
    QTest.keyClick(choices.cards["k0"], Qt.Key.Key_Right)
    assert choices.value() == "k2"
    assert choices.cards["k1"].toolTip() == "not here"


def test_set_choices_keeps_the_widget_and_the_selection(qtbot):
    choices = group(qtbot)
    choices.set_value("k1")
    assert choices.set_choices([("k0", "A", "", False), ("k1", "B", "", False), ("k9", "New", "", False)]) is True
    assert list(choices.cards) == ["k0", "k1", "k9"] and choices.value() == "k1"
    assert choices.set_choices([("k0", "A", "", False)]) is False and choices.value() == ""


def test_tab_leaves_the_multiline_fields(make_window):
    window = make_window()
    assert window.accounts.ssh_keys.tabChangesFocus()
    assert window.customize.use_flags.tabChangesFocus() and window.customize.package_use.tabChangesFocus()


def test_return_in_a_field_presses_continue_and_alt_arrows_are_shortcuts(make_window, monkeypatch):
    window = make_window()
    window.storage.disk_group.set_value("/dev/sda")
    window.accounts.password.setText("user-password")
    window.accounts.password_confirm.setText("user-password")
    window.accounts.root_password.setText("root-password")
    window.accounts.root_confirm.setText("root-password")
    window.accounts.save_manifest()
    window.storage.save_manifest()
    window._show_page(5)
    QTest.keyClick(window.accounts.hostname, Qt.Key.Key_Return)
    assert window.current == 6
    shortcuts = {s.key().toString(): s for s in window.findChildren(QShortcut)}
    assert {"Alt+Left", "Alt+Right"} <= set(shortcuts)
    shortcuts["Alt+Left"].activated.emit()
    assert window.current == 5
    assert window.next.text() == "&Continue" and window.back.text() == "&Back"


def test_the_first_focus_is_a_field_or_the_primary_action(make_window):
    window = make_window()
    assert window.welcome.focusWidget() is window.welcome.install_button
    window._show_page(5)
    assert window.accounts.focusWidget() is window.accounts.hostname
    window._show_page(2)
    assert window.storage.focusWidget() is not None


def test_mnemonics_on_the_navigation_and_the_readiness_actions(make_window):
    window = make_window()
    assert "&" in window.back.text() and "&" in window.next.text()
    assert "&" in window.readiness.refresh_button.text()


def test_the_current_step_stays_readable_even_when_disabled(themed, make_window):
    window = make_window()
    window.install_started = True
    window._show_page(7)
    window.show()
    button = window.step_buttons[7]
    assert not button.isEnabled() and button.property("current")
    image = button.grab().toImage()
    background = QColor(image.pixel(3, image.height() // 2))
    text_pixels = [QColor(image.pixel(x, y)) for x in range(image.width()) for y in range(image.height())]
    best = max(contrast(pixel, background) for pixel in text_pixels)
    assert best >= 4.5, best
    assert contrast(QColor(theme.SIDEBAR_DISABLED), QColor(theme.SIDEBAR)) >= 4.5
    assert contrast(QColor("white"), QColor(theme.SIDEBAR_CURRENT)) >= 4.5
    window.install_started = False


def test_the_focus_ring_has_a_stylesheet_rule(themed):
    assert "QFrame#choiceCard:focus" in themed.styleSheet() and "QPushButton:focus" in themed.styleSheet()


@pytest.mark.parametrize(
    ("requested", "available", "expected"),
    [
        ((1180, 790), (1024, 740), (1024, 692)),  # 1024x768 with a panel
        ((1180, 790), (800, 570), (800, 560)),  # 800x600: the supported minimum
        ((1180, 790), (1920, 1050), (1180, 790)),
        ((900, 600), (1920, 1050), (900, 600)),  # never grown
    ],
)
def test_bounded_window_size_table(requested, available, expected):
    assert bounded_window_size(*requested, *available) == expected


def test_widget_colours_follow_the_theme_at_run_time(qtbot):
    old = theme.MUTED
    theme.MUTED = "#123456"
    try:
        row = StatusRow("x")
        assert "#123456" in row.indicator.styleSheet()
    finally:
        theme.MUTED = old


# --- G9: polish ------------------------------------------------------------------------------------------------------


def test_combo_boxes_keep_their_native_arrow(themed, qtbot):
    combo = QComboBox()
    combo.addItems(["one", "two"])
    qtbot.addWidget(combo)
    combo.resize(200, 36)
    combo.show()
    image = combo.grab().toImage()
    background = QColor(image.pixel(image.width() - 3, 3))
    arrow = sum(
        1 for x in range(image.width() - 28, image.width() - 4) for y in range(6, image.height() - 6)
        if contrast(QColor(image.pixel(x, y)), background) > 1.6
    )
    assert arrow > 10


def test_the_applications_tab_does_not_stretch_its_card(themed, make_window):
    window = make_window()
    window.resize(1180, 800)
    window._show_page(4)
    window.customize.tabs.setCurrentIndex(2)
    QApplication.processEvents()
    card = window.customize.tabs.currentWidget().findChild(Card)
    assert card.height() <= card.sizeHint().height() * 1.3


def test_guided_start_state_is_applied_once_at_startup(make_window):
    window = make_window()
    assert window.mode.currentData() == "guided"
    assert not window.storage.encryption.isVisibleTo(window.storage)
    assert "Recommended choices" in window.header_subtitle.text()


def test_qwidget_methods_are_no_longer_shadowed(make_window):
    window = make_window()
    card = Card("t")
    assert callable(card.layout) and card.layout() is card.content
    page = Page("t")
    assert callable(page.layout) and page.layout() is not page.content
    assert callable(window.accounts.locale) and window.accounts.locale_box.isEditable()
    assert not hasattr(window.system, "profile") or window.system.profile is not None


def test_a_launcher_that_fails_says_what_to_do(make_window, monkeypatch):
    window = make_window()
    shown = []
    monkeypatch.setattr(app_module.QProcess, "startDetached", staticmethod(lambda program, arguments=[]: False))
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text, *a, **k: shown.append((title, text)))
    window.launch_application("hwmonux")
    assert shown[0][0] == "Application unavailable" and "hwmonux" in shown[0][1] and "terminal" in shown[0][1]
    window.reboot()
    assert shown[1][0] == "Restart" and "manually" in shown[1][1]


def test_network_settings_failure_is_explained(make_window, monkeypatch):
    window = make_window()
    shown = []
    monkeypatch.setattr(app_module.QProcess, "startDetached", staticmethod(lambda program, arguments=[]: False))
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text, *a, **k: shown.append((title, text)))
    window.readiness.networkSettingsRequested.emit()
    assert shown[0][0] == "Network settings" and "nmtui" in shown[0][1]


def test_locales_come_from_glibc_with_a_short_fallback(tmp_path):
    from gentoo_installer.pages.accounts import COMMON_LOCALES, load_locales

    supported = tmp_path / "SUPPORTED"
    supported.write_text(
        "# comment\nde_DE.UTF-8 UTF-8\nde_DE ISO-8859-1\naa_ER UTF-8\nca_ES@valencia UTF-8\nen_US.UTF-8 UTF-8\nfoo ISO-8859-1\n")
    assert load_locales(supported) == ["en_US.UTF-8", "aa_ER.UTF-8", "ca_ES.UTF-8@valencia", "de_DE.UTF-8"]
    assert load_locales(tmp_path / "missing") == list(COMMON_LOCALES)
    empty = tmp_path / "empty"
    empty.write_text("# nothing usable\n")
    assert load_locales(empty) == list(COMMON_LOCALES)


def test_a_missing_browser_shows_the_handbook_address(make_window, monkeypatch):
    window = make_window()
    monkeypatch.setattr(app_module.QDesktopServices, "openUrl", staticmethod(lambda url: False))
    seen = {}

    def exec_(self):
        seen["text"] = self.text()
        seen["buttons"] = [b.text() for b in self.buttons()]
        return 0

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    window.open_handbook()
    assert app_module.HANDBOOK_URL in seen["text"] and "Copy link" in seen["buttons"]


def test_reboot_tries_elogind_first(make_window, monkeypatch):
    window = make_window()
    calls = []

    def start(program, arguments=[]):
        calls.append(program)
        return program == "systemctl"

    monkeypatch.setattr(app_module.QProcess, "startDetached", staticmethod(start))
    window.reboot()
    assert calls == ["loginctl", "systemctl"]


# --- Welcome: two primary choices and the keyboard --------------------------------------------------------------------------


def test_welcome_offers_install_and_repair(make_window):
    window = make_window()
    assert window.welcome.install_button.text() == "Install Gentoo"
    assert window.welcome.repair_button.text() == "Repair an existing system"
    window.welcome.install_button.click()
    assert window.current == 1
    window._show_page(0)
    window.next.click()  # the Continue button keeps working on the first page
    assert window.current == 1


def test_repair_without_the_repair_tools_points_to_the_terminal_command(make_window, monkeypatch):
    window = make_window()
    monkeypatch.delitem(sys.modules, "gentoo_installer.rescue_app", raising=False)
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text, *a, **k: shown.append((title, text)))
    window.welcome.repair_button.click()
    assert shown[0][0] == "Repair an existing system" and "'gentoo-rescue'" in shown[0][1]
    assert window.isVisible()


def fake_rescue_module(monkeypatch, record):
    class RescueWindow(QMainWindow):
        def __init__(self, socket_path):
            super().__init__()
            record.append(socket_path)

    module = types.ModuleType("gentoo_installer.rescue_app")
    module.RescueWindow = RescueWindow
    monkeypatch.setitem(sys.modules, "gentoo_installer.rescue_app", module)
    return RescueWindow


def test_repair_opens_the_rescue_window_and_returns_to_the_installer(make_window, monkeypatch, qtbot):
    window = make_window()
    sockets = []
    fake_rescue_module(monkeypatch, sockets)
    window.welcome.repair_button.click()
    assert sockets == [window.socket_path]
    assert not window.isVisible() and window._repair_window.isVisible()
    window._repair_window.close()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qtbot.waitUntil(window.isVisible, timeout=2000)


def test_a_broken_repair_module_is_reported_not_swallowed(make_window, monkeypatch):
    window = make_window()
    broken = types.ModuleType("gentoo_installer.rescue_app")

    def explode(socket_path):
        raise RuntimeError("daemon too old")

    broken.RescueWindow = explode
    monkeypatch.setitem(sys.modules, "gentoo_installer.rescue_app", broken)
    shown = []
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text, *a, **k: shown.append(text))
    window.open_repair()
    assert "daemon too old" in shown[0] and window.isVisible()


def test_picking_a_layout_applies_it_and_prefills_the_manifest(make_window, monkeypatch):
    calls = []
    monkeypatch.setattr(keyboard, "start_detached", lambda program, arguments: (calls.append((program, arguments)), True)[1])
    window = make_window()
    chooser = window.welcome.keyboard
    assert window.manifest.system.keyboard_layout == "us" and calls == []  # showing it changes nothing
    chooser.layout_box.setCurrentIndex(chooser.layout_box.findData("de"))
    chooser._layout_activated(0)
    assert calls[-1] == ("setxkbmap", ["-layout", "de", "-variant", "", "-option", ""])
    assert (window.manifest.system.keyboard_layout, window.manifest.system.keyboard_variant, window.manifest.system.keymap) == ("de", "", "de-latin1")
    nodead = chooser.variant_box.findData("nodeadkeys")
    assert nodead >= 0
    chooser.variant_box.setCurrentIndex(nodead)
    chooser._variant_activated(0)
    assert window.manifest.system.keymap == "de-latin1-nodeadkeys"
    assert calls[-1][1][:4] == ["-layout", "de", "-variant", "nodeadkeys"]
    window._show_page(5)
    assert window.accounts.keymap.currentText() == "de-latin1-nodeadkeys"
    assert "de (nodeadkeys)" in window.accounts.keyboard_summary.text()


def test_the_welcome_chooser_shows_the_manifests_layout_without_applying_it(make_window, monkeypatch):
    calls = []
    monkeypatch.setattr(keyboard, "start_detached", lambda program, arguments: (calls.append(arguments), True)[1])
    manifest = InstallManifest()
    manifest.system.keyboard_layout = "fr"
    window = make_window(manifest=manifest)
    assert window.welcome.keyboard.value() == ("fr", "") and calls == []


def test_the_accounts_page_can_send_the_user_back_to_the_keyboard_choice(make_window):
    window = make_window()
    window._show_page(5)
    window.accounts.keyboardChangeRequested.emit()
    assert window.current == 0


def test_every_console_keymap_in_the_table_exists_on_the_live_image():
    directory = Path("/usr/share/keymaps")
    if not directory.is_dir():
        pytest.skip("no kbd keymaps on this host")
    missing = sorted(name for name in set(keyboard.CONSOLE_KEYMAPS.values()) if not keyboard.keymap_exists(name))
    assert missing == []


@pytest.mark.parametrize(
    ("layout", "variant", "expected"),
    [("us", "", "us"), ("us", "dvorak", "dvorak"), ("gb", "", "uk"), ("de", "nodeadkeys", "de-latin1-nodeadkeys"), ("pt", "", "pt-latin9"),
     ("ch", "fr", "fr_CH-latin1"), ("ru", "", "us"), ("us,ru", "", "us"), ("de,us", "nodeadkeys,", "de-latin1-nodeadkeys"), ("xx", "", "us"), ("us", "colemak", "us")],
)
def test_console_keymap_choice(layout, variant, expected):
    assert keyboard.console_keymap(layout, variant) == expected


def test_layouts_come_from_xkeyboard_config_with_a_fallback(tmp_path):
    xml = tmp_path / "evdev.xml"
    xml.write_text(
        "<xkbConfigRegistry><layoutList>"
        "<layout><configItem><name>us</name><description>English (US)</description></configItem>"
        "<variantList><variant><configItem><name>dvorak</name><description>English (Dvorak)</description></configItem></variant></variantList></layout>"
        "<layout><configItem><name>de</name><description>German</description></configItem></layout>"
        "</layoutList></xkbConfigRegistry>")
    layouts = keyboard.load_layouts(xml)
    assert [item.name for item in layouts] == ["us", "de"] and layouts[0].variants == (("dvorak", "English (Dvorak)"),)
    fallback = keyboard.load_layouts(tmp_path / "missing.xml")
    assert any(item.name == "de" for item in fallback)
    (tmp_path / "broken.xml").write_text("<nope")
    assert keyboard.load_layouts(tmp_path / "broken.xml") == fallback


def test_the_chooser_searches_and_keeps_multi_layout_values(qtbot):
    chooser = keyboard.KeyboardChooser()
    qtbot.addWidget(chooser)
    chooser.search.setText("german")
    assert chooser.layout_box.count() >= 1 and "German" in chooser.layout_box.itemText(0)
    chooser.search.clear()
    chooser.set_selection("us,ru")
    assert chooser.value() == ("us,ru", "") and chooser.layout_box.itemText(0) == "Custom: us,ru"
    assert "us" == keyboard.console_keymap(*chooser.value())


# --- the command line ----------------------------------------------------------------------------------------------------------


class FakeApplication:
    def __init__(self, argv):
        self.ran = 0

    def setApplicationName(self, name):
        pass

    setOrganizationName = setDesktopFileName = setApplicationName

    def exec(self):
        self.ran += 1
        return 0


@pytest.fixture
def fake_main(monkeypatch):
    monkeypatch.setattr(app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(app_module, "apply_theme", lambda app: None)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # main() installs its own; restore afterwards
    import threading

    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(keyboard, "start_detached", lambda program, arguments: True)


def test_main_repair_mode_without_the_repair_tools_explains_and_exits(fake_main, monkeypatch):
    monkeypatch.delitem(sys.modules, "gentoo_installer.rescue_app", raising=False)
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text, *a, **k: shown.append(text))
    assert app_module.main(["--mode", "repair"]) == 0
    assert "gentoo-rescue" in shown[0]


def test_main_repair_mode_opens_the_rescue_window(fake_main, monkeypatch, qtbot):
    sockets = []
    fake_rescue_module(monkeypatch, sockets)
    assert app_module.main(["--mode", "repair", "--socket", "/run/x.sock"]) == 0
    assert sockets == ["/run/x.sock"]


def test_main_install_mode_marks_a_command_line_manifest_as_explicit(fake_main, monkeypatch, tmp_path):
    seen = {}

    class StubWindow:
        def __init__(self, manifest, socket, **kwargs):
            seen.update(manifest=manifest, socket=socket, **kwargs)

        def show(self):
            pass

    monkeypatch.setattr(app_module, "InstallerWindow", StubWindow)
    manifest = InstallManifest()
    manifest.storage.device = "/dev/sda"
    manifest.accounts.password = manifest.accounts.root_password = "long-enough-pw"
    path = tmp_path / "m.json"
    path.write_text(manifest.to_json())
    assert app_module.main(["--manifest", str(path)]) == 0
    assert seen["firmware_explicit"] is True and seen["manifest"].storage.device == "/dev/sda"
    seen.clear()
    assert app_module.main([]) == 0
    assert seen["firmware_explicit"] is False
    assert seen["manifest"].portage.make_opts.startswith("-j")  # a fresh install builds with what this computer can sustain


def test_main_reports_a_broken_manifest_file(fake_main, monkeypatch, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{nope")
    shown = []
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text, *a, **k: shown.append(title))
    assert app_module.main(["--manifest", str(path)]) == 2
    assert shown == ["Cannot load installation settings"]
