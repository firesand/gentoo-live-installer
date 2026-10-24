"""GUI log file, last-resort exception handler and the diagnostics bundle."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading

import pytest
from PySide6.QtWidgets import QFileDialog

from gentoo_installer import diagnostics
from gentoo_installer.manifest import InstallManifest
from gentoo_installer.system_info import DiskInfo
from tests.ui.conftest import sample_info


@pytest.fixture
def gui_log(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = diagnostics.setup_gui_logging()
    yield path
    for handler in list(diagnostics.LOG.handlers):
        if getattr(handler, "_gentoo_gui_log", False):
            diagnostics.LOG.removeHandler(handler)
            handler.close()


@pytest.fixture
def restore_hooks(monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)


def test_the_gui_log_is_private_and_rotating(gui_log, tmp_path):
    assert gui_log == tmp_path / "cache" / "gentoo-installer" / "gui.log"
    assert stat.S_IMODE(os.stat(gui_log).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(gui_log.parent).st_mode) == 0o700
    diagnostics.LOG.warning("something odd happened")
    for handler in diagnostics.LOG.handlers:
        handler.flush()
    assert "something odd happened" in gui_log.read_text()
    handler = next(h for h in diagnostics.LOG.handlers if getattr(h, "_gentoo_gui_log", False))
    assert handler.maxBytes == 1_000_000 and handler.backupCount == 3


def test_setting_up_logging_twice_does_not_duplicate_lines(gui_log):
    diagnostics.setup_gui_logging()
    assert len([h for h in diagnostics.LOG.handlers if getattr(h, "_gentoo_gui_log", False)]) == 1


def test_an_unwritable_cache_does_not_stop_the_gui(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory should be")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    assert diagnostics.setup_gui_logging() is None


def test_an_unhandled_exception_is_logged_and_shown_once(gui_log):
    shown = []

    def boom():
        raise RuntimeError("slot exploded")

    try:
        boom()
    except RuntimeError:
        info = sys.exc_info()
    text = diagnostics.handle_exception(*info, show=lambda text, path: shown.append((text, path)))
    assert "slot exploded" in text
    assert shown and shown[0][1] == gui_log and "RuntimeError" in shown[0][0]
    for handler in diagnostics.LOG.handlers:
        handler.flush()
    assert "slot exploded" in gui_log.read_text()


def test_the_error_dialog_is_rate_limited_and_never_recursive(gui_log, restore_hooks):
    diagnostics.install_excepthook(show=lambda text, path: None)
    shown = []

    def show(text, path):
        shown.append(text)
        raise ValueError("the dialog itself failed")  # must not escape or loop

    err = (RuntimeError, RuntimeError("x"), None)
    for _ in range(6):
        diagnostics.handle_exception(*err, show=show)
    assert len(shown) == 3


def test_the_excepthook_routes_to_the_dialog_and_returns_the_previous_hook(gui_log, restore_hooks):
    previous = sys.excepthook
    shown = []
    assert diagnostics.install_excepthook(show=lambda text, path: shown.append(text)) is previous
    try:
        raise KeyError("in a slot")
    except KeyError:
        sys.excepthook(*sys.exc_info())
    assert shown and "in a slot" in shown[0]
    assert threading.excepthook is not threading.__excepthook__


def test_url_credentials_are_redacted_everywhere():
    assert diagnostics.redact_url_userinfo("http://alice:S3cretPass@proxy.corp:3128") == "http://alice:<redacted>@proxy.corp:3128"
    assert diagnostics.redact_url_userinfo("see https://bob:pw@host/x and http://carol:pw2@h2") == "see https://bob:<redacted>@host/x and http://carol:<redacted>@h2"
    assert diagnostics.redact_url_userinfo("http://proxy.corp:3128") == "http://proxy.corp:3128"
    assert diagnostics.strip_url_userinfo("socks5h://u:p@h:1080") == "socks5h://h:1080"
    assert diagnostics.scrub_text("pw=hunter22 end", ["hunter22", "ab"]) == "pw=<redacted> end"


def test_the_bundle_has_the_documented_keys_and_no_secrets(gui_log, monkeypatch):
    monkeypatch.setattr(diagnostics, "run_command", lambda argv, **kw: "output of " + " ".join(argv) + " with hunter-two-pw in it")
    manifest = InstallManifest()
    manifest.accounts.password = "hunter-two-pw"
    manifest.accounts.root_password = "root-secret-pw"
    manifest.storage.luks_passphrase = "luks-secret-pw"
    manifest.network.proxy = "http://alice:S3cretPass@proxy.corp:3128"
    info = sample_info(disks=[DiskInfo("/dev/sda", "Samsung SSD 870", 1024**4, serial="S5Y1NX0T123456", wwid="0x5002538f12345678")])
    diagnostics.LOG.error("GUI problem with root-secret-pw")
    for handler in diagnostics.LOG.handlers:
        handler.flush()
    bundle = diagnostics.collect_bundle(
        manifest=manifest, info=info, socket_path="/run/x.sock", visible_log="line one\nluks-secret-pw leaked here\nline three",
        reason="installation failed", daemon_document=lambda: {"state": "running", "document": {"dmesg": ["x"]}})
    assert set(bundle) >= {"format", "generated", "reason", "gui", "platform", "manifest", "system_info", "gui_log_tail", "install_log_tail", "daemon", "commands"}
    assert set(bundle["commands"]) >= {"lsblk", "lspci", "lsusb", "efibootmgr", "cmdline", "dmesg"}
    text = json.dumps(bundle)
    for secret in ("hunter-two-pw", "root-secret-pw", "luks-secret-pw", "S3cretPass", "S5Y1NX0T123456", "5002538f12345678"):
        assert secret not in text, secret
    assert bundle["manifest"]["network"]["proxy"] == "http://alice:<redacted>@proxy.corp:3128"
    assert bundle["system_info"]["disks"][0]["serial"] == "…123456"
    assert bundle["daemon"]["document"] == {"dmesg": ["x"]}
    assert any("GUI problem" in line for line in bundle["gui_log_tail"])


def test_a_failing_daemon_part_does_not_lose_the_bundle(gui_log, monkeypatch):
    monkeypatch.setattr(diagnostics, "run_command", lambda argv, **kw: "")

    def broken():
        raise OSError("socket gone")

    bundle = diagnostics.collect_bundle(info=sample_info(), daemon_document=broken)
    assert bundle["daemon"] == {"error": "socket gone"}
    assert bundle["manifest"] is None


def test_the_command_runner_never_raises_and_limits_output():
    assert diagnostics.run_command(("definitely-not-a-command-xyz",)).startswith("(not available")
    long = diagnostics.run_command(("python3", "-c", "print('x' * 200000)"), limit=1000)
    assert len(long) <= 1000


def test_write_bundle_is_private(tmp_path):
    path = diagnostics.write_bundle(tmp_path / "deep" / "bundle.json", {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_save_diagnostics_from_the_readiness_page(make_window, qtbot, tmp_path, monkeypatch, gui_log):
    monkeypatch.setattr(diagnostics, "run_command", lambda argv, **kw: "ok")
    window = make_window()
    window.manifest.accounts.password = "typed-password-1"
    target = tmp_path / "usb" / "diag.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    informed = []
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text, *a, **k: informed.append((title, text)))
    window._show_page(1)
    with qtbot.waitSignal(window.progress.logSaved, timeout=10000):
        window.readiness.diagnostics_button.click()
    data = json.loads(target.read_text())
    assert data["reason"] == "readiness" and "typed-password-1" not in target.read_text()
    assert data["system_info"]["disks"][0]["path"] == "/dev/sda"
    assert informed and informed[0][0] == "Diagnostics" and str(target) in informed[0][1]


def test_save_diagnostics_from_the_failure_panel_includes_the_visible_log(make_window, qtbot, tmp_path, monkeypatch, gui_log):
    monkeypatch.setattr(diagnostics, "run_command", lambda argv, **kw: "ok")
    window = make_window()
    window._show_page(7)
    window.progress.handle_event({"type": "log", "message": "the last visible line"})
    window.progress.handle_event({"type": "error", "message": "boom", "error": "boom"})
    window.progress.handle_event({"type": "completed", "data": {"success": False}})
    target = tmp_path / "diag.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    with qtbot.waitSignal(window.progress.logSaved, timeout=10000):
        window.progress.diagnostics_button.click()
    data = json.loads(target.read_text())
    assert data["reason"] == "installation failed"
    assert "the last visible line" in data["install_log_tail"]
    assert "Saved diagnostics" in window.progress.notice.text()


def test_the_bundle_thread_never_touches_widgets(make_window, qtbot, tmp_path, monkeypatch, gui_log):
    monkeypatch.setattr(diagnostics, "run_command", lambda argv, **kw: "ok")
    window = make_window()
    threads = []
    original = window.progress.visible_text
    window.progress.visible_text = lambda: (threads.append(threading.current_thread()), original())[1]
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "d.json"), ""))
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    with qtbot.waitSignal(window.progress.logSaved, timeout=10000):
        window.save_diagnostics("x")
    assert threads == [threading.main_thread()]


def test_cancelling_the_save_dialog_writes_nothing(make_window, tmp_path, monkeypatch):
    window = make_window()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
    window.save_diagnostics("x")
    assert not list(tmp_path.glob("*.json"))


def test_the_default_bundle_name_is_timestamped():
    name = diagnostics.default_bundle_name()
    assert name.startswith("gentoo-installer-diagnostics-") and name.endswith(".json")
