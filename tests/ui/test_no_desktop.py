"""gentoo-installer started without a desktop says what to do, instead of Qt's abort about the xcb plugin.

The first test on a real PC: the desktop had not come up, gentoo-installer was typed on the text console, and Qt aborted with
"could not connect to display ... xcb-cursor0 or libxcb-cursor0 is needed".
"""
from __future__ import annotations

from gentoo_installer import app


def test_no_display_means_no_qt_and_a_plain_explanation(capsys) -> None:
    assert not app.graphical_session({})
    assert app.graphical_session({"DISPLAY": ":0"}) and app.graphical_session({"WAYLAND_DISPLAY": "wayland-0"})
    assert app.graphical_session({"QT_QPA_PLATFORM": "offscreen"}), "a platform chosen on purpose (tests, VNC) is respected"


def test_main_returns_before_qt_when_there_is_no_desktop(monkeypatch, capsys) -> None:
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app, "QApplication", lambda *a: (_ for _ in ()).throw(AssertionError("Qt must not start")))
    assert app.main([]) == 3
    err = capsys.readouterr().err
    assert "needs the graphical desktop" in err and "Safe graphics" in err and "sudo gentoo-installer-text" in err
    assert "/var/log/Xorg.0.log" in err
