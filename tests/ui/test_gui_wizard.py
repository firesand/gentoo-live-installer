"""Readiness gating, background inspection, section routing, desktop/profile rules, draft hygiene."""

from __future__ import annotations

import json
import time

import pytest
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from gentoo_installer import app as app_module
from gentoo_installer.app import InstallerWindow, blank_secrets, load_draft, load_redacted_manifest
from gentoo_installer.daemon_client import DaemonState
from gentoo_installer.manifest import InstallManifest, ManifestError
from gentoo_installer.pages import readiness as readiness_module
from gentoo_installer.pages.system import PUBLISHED_PROFILES, desktop_problem, profile_path, profile_problem, stage_family
from gentoo_installer.system_info import DiskInfo
from tests.ui.conftest import sample_info

GIB = 1024**3


def populate(window):
    window.storage.disk_group.set_value("/dev/sda")
    window.storage.save_manifest()
    window.accounts.password.setText("user-password")
    window.accounts.password_confirm.setText("user-password")
    window.accounts.root_password.setText("root-password")
    window.accounts.root_confirm.setText("root-password")
    for page in window.pages[:6]:
        page.save_manifest()


# --- G4: Readiness gates Continue ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "needle"),
    [("not-running", "rc-service gentoo-installerd"), ("stale", "stale socket"), ("no-access", "gentoo-installer group")],
)
def test_a_missing_service_blocks_continue_with_instructions(make_window, monkeypatch, kind, needle):
    from gentoo_installer.daemon_client import describe_os_error

    message = {"not-running": describe_os_error(FileNotFoundError(2, "x"), "/run/s.sock")[1],
               "stale": describe_os_error(ConnectionRefusedError(111, "x"), "/run/s.sock")[1],
               "no-access": describe_os_error(PermissionError(13, "x"), "/run/s.sock")[1]}[kind]
    window = make_window()
    window.apply_info(sample_info(daemon=DaemonState(kind, message)))
    assert window.readiness.rows["service"].status == "error"
    valid, text = window.readiness.validate_page()
    assert not valid and needle in text
    window._show_page(1)
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *a, **k: shown.append(text))
    window.next.click()
    assert window.current == 1 and needle in shown[0]


def test_the_service_log_tail_is_shown_when_it_can_be_read(make_window, tmp_path, monkeypatch):
    log = tmp_path / "gentoo-installerd.log"
    log.write_text("\n".join(f"line {i}" for i in range(40)) + "\n")
    monkeypatch.setattr(readiness_module, "DAEMON_LOG", log)
    window = make_window()
    window.apply_info(sample_info(daemon=DaemonState("stale", "restart it")))
    text = window.readiness.service_log.text()
    assert "line 39" in text and "line 20" in text and "line 19" not in text
    assert window.readiness.service_log.isVisibleTo(window.readiness)


def test_no_usable_disk_blocks_continue(make_window):
    window = make_window(info=sample_info(disks=[DiskInfo("/dev/sdb", "Tiny", 4 * GIB, serial="t"), DiskInfo("/dev/sdc", "Live", 32 * GIB, serial="l", live_medium=True)]))
    valid, text = window.readiness.validate_page()
    assert not valid and "No suitable disk" in text and "/dev/sdb 4.0 GiB" in text


def test_a_serial_less_disk_is_a_warning_when_it_can_be_approved_and_an_error_when_not(make_window):
    ok = make_window(info=sample_info(disks=[DiskInfo("/dev/vda", "QEMU", 64 * GIB, by_path="pci-0000:00:04.0")]))
    assert ok.readiness.rows["disk"].status == "warning"
    assert ok.readiness.validate_page() == (True, "")
    blocked = make_window(info=sample_info(disks=[DiskInfo("/dev/vda", "QEMU", 64 * GIB)]))
    assert blocked.readiness.rows["disk"].status == "error"
    valid, text = blocked.readiness.validate_page()
    assert not valid and "virtio-blk-pci,drive=d0,serial=MYDISK" in text and "<serial>MYDISK</serial>" in text


def test_missing_required_tools_and_wrong_architecture_block(make_window):
    window = make_window(info=sample_info(missing_tools=["parted", "gpg"]))
    valid, text = window.readiness.validate_page()
    assert not valid and "parted, gpg" in text
    window = make_window(info=sample_info(architecture="aarch64"))
    assert window.readiness.validate_page() == (False, "This installer supports AMD64/x86-64 systems only.")
    window = make_window(info=sample_info(memory_gib=0.5))
    assert "less than 1 GiB" in window.readiness.validate_page()[1]


def test_a_missing_network_needs_the_continue_anyway_box(make_window):
    window = make_window(info=sample_info(network=False, network_error="Network is unreachable"))
    page = window.readiness
    assert page.rows["network"].status == "error" and "proxy" in page.rows["network"].detail.text()
    valid, text = page.validate_page()
    assert not valid and "Continue anyway" in text
    assert page.continue_anyway.isVisibleTo(page)
    page.continue_anyway.setChecked(True)
    assert page.validate_page() == (True, "")


def test_a_wrong_clock_is_an_error_with_instructions_and_offers_the_daemons_sync(make_window, monkeypatch, qtbot):
    state = DaemonState("running", features=frozenset({"attach.v1"}), actions=frozenset({"sync_time"}))
    window = make_window()
    window.apply_info(sample_info(clock_skew=-3 * 86400, daemon=state))
    row = window.readiness.rows["clock"]
    assert row.status == "error" and "3 days ahead of" in row.detail.text()
    assert window.readiness.sync_button.isVisibleTo(window.readiness)
    assert not window.readiness.validate_page()[0]
    started = []
    monkeypatch.setattr(app_module, "BackendWorker", lambda manifest, socket, parent=None, **kw: started.append(kw) or _NoopWorker())
    window.readiness.sync_button.click()
    assert started == [{"request": {"action": "sync_time"}}]


class _NoopWorker:
    def __init__(self):
        self.finished = _Signal()

    def start(self):
        pass

    def deleteLater(self):
        pass

    def wait(self, *_args):
        return True


class _Signal:
    def connect(self, *_args):
        pass


def test_a_slightly_wrong_clock_is_only_a_warning(make_window):
    window = make_window(info=sample_info(clock_skew=1800.0))
    assert window.readiness.rows["clock"].status == "warning"
    assert window.readiness.validate_page() == (True, "")


def test_hardware_findings_from_the_daemon_are_listed(make_window):
    warnings = [
        {"code": "hw.fw_sb_enabled", "severity": "warning", "message": "Secure Boot is on", "action": "restart_firmware"},
        {"code": "hw.gpu_hybrid", "severity": "info", "message": "Integrated and discrete graphics", "action": ""},
    ]
    window = make_window(info=sample_info(warnings=warnings, errors=["Missing required tools: x", "Something odd"]))
    page = window.readiness
    assert page.notes_card.isVisibleTo(page)
    shown = [page.notes_box.itemAt(i).widget().detail.text() for i in range(page.notes_box.count())]
    assert any("Secure Boot is on" in text and "restart firmware" in text for text in shown)
    assert any("Something odd" in text for text in shown) and not any("Missing required tools" in text for text in shown)
    assert page.validate_page() == (True, "")


def test_a_global_blocker_from_the_daemon_stops_the_wizard(make_window):
    window = make_window(info=sample_info(warnings=[{"code": "hw.st_no_disk", "severity": "error", "message": "No offerable disk", "action": ""}]))
    assert window.readiness.validate_page() == (False, "No offerable disk")


def test_the_proxy_is_validated_saved_and_used_for_the_next_check(make_window, monkeypatch):
    window = make_window()
    window._show_page(1)
    window.readiness.proxy.setText("proxy.example")
    valid, text = window.readiness.validate_page()
    assert not valid and text == "Download proxy must look like http://host:3128"
    window.readiness.proxy.setText("http://alice:pw@proxy.example:3128")
    assert window.readiness.validate_page() == (True, "")
    targets = []
    monkeypatch.setattr(app_module.system_info, "set_probe_target", lambda mirror, proxy="": targets.append((mirror, proxy)))
    window.refresh_readiness()
    assert window.manifest.network.proxy == "http://alice:pw@proxy.example:3128"
    assert targets[-1] == ("https://distfiles.gentoo.org", "http://alice:pw@proxy.example:3128")


def test_customize_validates_the_same_proxy_syntax(make_window):
    window = make_window()
    window.customize.proxy.setText("http://proxy:3128")
    assert window.customize.validate_page() == (True, "")
    window.customize.proxy.setText("ftp://nope")
    assert window.customize.validate_page() == (False, "Download proxy must look like http://host:3128")
    assert "not stored on the installed system" in window.customize.proxy.toolTip()


# --- G6: the window does not wait for the inspection -------------------------------------------------------------------


def slow_inspection(monkeypatch, seconds, infos):
    calls = []

    def inspect():
        calls.append(time.monotonic())
        info = infos[min(len(calls), len(infos)) - 1]
        time.sleep(seconds[min(len(calls), len(seconds)) - 1])
        return info

    monkeypatch.setattr("gentoo_installer.app.inspect_system", inspect)
    return calls


def test_the_window_appears_at_once_and_the_rows_say_checking(qtbot, monkeypatch, tmp_path):
    slow_inspection(monkeypatch, [0.8], [sample_info()])
    started = time.monotonic()
    window = InstallerWindow(InstallManifest(), str(tmp_path / "s.sock"))
    qtbot.addWidget(window)
    window.show()
    assert time.monotonic() - started < 0.5
    assert not window.info.inspected and window.info.source == "pending"
    assert window.readiness.rows["disk"].status == "checking"
    window._show_page(1)
    assert not window.next.isEnabled()  # Continue waits for the first inspection
    with qtbot.waitSignal(window.inspectionFinished, timeout=5000):
        pass
    assert window.info.inspected and window.readiness.rows["disk"].status == "ok"
    assert window.next.isEnabled()
    assert list(window.storage.disk_group.cards) == ["/dev/sda"]


def test_check_again_does_not_freeze_the_event_loop(qtbot, monkeypatch, tmp_path):
    slow_inspection(monkeypatch, [0.0, 0.8], [sample_info()])
    window = InstallerWindow(InstallManifest(), str(tmp_path / "s.sock"))
    qtbot.addWidget(window)
    window.show()
    with qtbot.waitSignal(window.inspectionFinished, timeout=5000):
        pass
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(50)
    window.refresh_readiness()
    assert window.readiness.rows["network"].status == "checking" and not window.readiness.refresh_button.isEnabled()
    with qtbot.waitSignal(window.inspectionFinished, timeout=5000):
        pass
    timer.stop()
    assert len(ticks) >= 8  # ~0.8 s at 50 ms: the loop kept running
    assert window.readiness.refresh_button.isEnabled()


def test_a_superseded_inspection_is_ignored(qtbot, monkeypatch, tmp_path):
    old = sample_info(memory_gib=1.5)
    new = sample_info(memory_gib=32)
    slow_inspection(monkeypatch, [0.0, 0.8, 0.0], [sample_info(), old, new])
    window = InstallerWindow(InstallManifest(), str(tmp_path / "s.sock"))
    qtbot.addWidget(window)
    window.show()
    with qtbot.waitSignal(window.inspectionFinished, timeout=5000):
        pass
    window.refresh_readiness()  # slow one
    window.refresh_readiness()  # supersedes it
    with qtbot.waitSignal(window.inspectionFinished, timeout=5000):
        pass
    qtbot.wait(1000)  # the slow worker finishes later and must not overwrite the newer result
    assert window.info.memory_gib == 32


def test_wait_for_inspection_is_available_to_scripts(qtbot, monkeypatch, tmp_path):
    slow_inspection(monkeypatch, [0.3], [sample_info()])
    window = InstallerWindow(InstallManifest(), str(tmp_path / "s.sock"))
    qtbot.addWidget(window)
    assert window.wait_for_inspection(5000)
    assert window.info.disks[0].path == "/dev/sda"


def test_a_failing_inspection_still_ends_the_checking_state(qtbot, monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("lsblk exploded")

    monkeypatch.setattr("gentoo_installer.app.inspect_system", boom)
    window = InstallerWindow(InstallManifest(), str(tmp_path / "s.sock"))
    qtbot.addWidget(window)
    with qtbot.waitSignal(window.inspectionFinished, timeout=5000):
        pass
    assert window.info.inspected and "lsblk exploded" in " ".join(window.info.errors)
    assert window.readiness.rows["disk"].status == "error"


# --- NEW-GD-7: errors are routed by section ---------------------------------------------------------------------------------


def test_a_page_blocks_on_its_sections_whatever_the_wording(make_window, monkeypatch):
    window = make_window()
    base = InstallManifest.validate_sections

    def sections(self):
        result = base(self)
        result["system"] = ["Frobnicate the widget first"]
        return result

    monkeypatch.setattr(InstallManifest, "validate_sections", sections)
    assert window.system.validate_page() == (False, "Frobnicate the widget first")
    assert window.storage.validate_page()[1] != "Frobnicate the widget first"


def test_a_late_error_navigates_to_the_page_that_owns_it(make_window, monkeypatch):
    window = make_window()
    populate(window)
    window._show_page(5)
    base = InstallManifest.validate_sections

    def sections(self):  # a rule that only the whole-manifest check sees
        result = base(self)
        result["storage"] = ["Late storage problem"]
        return result

    monkeypatch.setattr(InstallManifest, "validate_sections", sections)
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *a, **k: shown.append((title, text)))
    window.accounts.validate_page = lambda: (True, "")  # this page's own sections are fine
    window.next.click()
    assert shown == [("Installation settings", "Late storage problem")]
    assert window.current == 2


@pytest.mark.parametrize(
    ("section", "page"),
    [("storage", 2), ("system", 3), ("portage", 4), ("network", 4), ("options", 4), ("accounts", 5), ("locale", 5), ("hardware", 6)],
)
def test_every_section_has_an_owning_page(make_window, section, page):
    window = make_window()
    assert window._page_for_section(section) == page


def test_a_redacted_secret_is_routed_to_the_page_that_holds_it(make_window):
    window = make_window()
    window.manifest.storage.luks_passphrase = "<redacted>"
    assert window._page_for_section("general") == 2
    window.manifest.storage.luks_passphrase = ""
    window.manifest.network.wifi_password = "<redacted>"
    assert window._page_for_section("general") == 4


# --- NOBS-5: KDE needs a Standard or Desktop stage3 -----------------------------------------------------------------------------------


def choose_stage(window, variant):
    page = window.system
    page.stage.setCurrentIndex(page.stage.findData(variant))


@pytest.mark.parametrize("variant", ["amd64-nomultilib-openrc", "amd64-hardened-openrc", "amd64-hardened-systemd"])
def test_choosing_a_stage_that_cannot_build_kde_disables_the_card_and_moves_to_minimal(make_window, variant):
    window = make_window()
    page = window.system
    page.init_group.set_value("systemd" if variant.endswith("systemd") else "openrc")
    page.desktop_group.set_value("kde")
    choose_stage(window, variant)
    assert not page.desktop_group.cards["kde"].isEnabled() and page.desktop_group.cards["gnome"].isEnabled()
    assert "stage3" in page.desktop_group.cards["kde"].toolTip()
    assert page.desktop_group.value() == "minimal"
    assert page.family_note.isVisibleTo(page) and "KDE Plasma needs a Standard or Desktop stage3" in page.family_note.text()
    page.save_manifest()
    assert "/desktop" not in window.manifest.system.profile and window.manifest.system.desktop == "minimal"
    choose_stage(window, "amd64-systemd" if variant.endswith("systemd") else "amd64-openrc")
    assert page.desktop_group.cards["kde"].isEnabled()


def test_a_loaded_kde_on_hardened_manifest_is_not_changed_silently_but_refused(make_window):
    manifest = InstallManifest()
    manifest.system.stage_variant = "amd64-hardened-openrc"
    manifest.system.profile = "default/linux/amd64/23.0/hardened"
    manifest.system.desktop = "kde"
    window = make_window(manifest=manifest)
    window._show_page(3)
    assert window.system.desktop_group.value() == "kde"
    valid, message = window.system.validate_page()
    assert not valid and "stage3" in message and "Choose a Standard stage3" in message
    assert window.manifest.system.desktop == "kde"


def test_gnome_on_hardened_is_allowed_with_a_note_and_a_review_warning(make_window):
    window = make_window()
    page = window.system
    page.desktop_group.set_value("gnome")
    choose_stage(window, "amd64-hardened-openrc")
    assert page.desktop_group.value() == "gnome"
    assert "Not verified" in page.family_note.text()
    page.save_manifest()
    from gentoo_installer.pages.review import manifest_warnings

    assert any("GNOME on a Hardened stage3" in w for w in manifest_warnings(window.manifest))


def test_the_desktop_table():
    assert desktop_problem("amd64-openrc", "kde") == "" and desktop_problem("amd64-desktop-systemd", "kde") == ""
    assert "stage3" in desktop_problem("amd64-nomultilib-openrc", "kde")
    assert desktop_problem("amd64-hardened-systemd", "minimal") == ""
    assert stage_family("amd64-hardened-systemd") == "hardened" and stage_family("amd64-desktop-openrc") == "desktop"


# --- NOBS-6: the profile is a choice, not free text ------------------------------------------------------------------------------------------


def test_the_profile_combo_is_not_editable(make_window):
    assert not make_window().system.profile.isEditable()


@pytest.mark.parametrize(
    "profile",
    ["foo/bar baz", "../../etc/passwd", "default/linux/arm64/23.0", "default/linux/amd64/17.1/desktop", "default/linux/amd64/23.0/desktop/plasma/nomultilib"],
)
def test_an_unknown_loaded_profile_is_shown_flagged_and_refused(make_window, profile):
    manifest = InstallManifest()
    manifest.system.profile = profile
    window = make_window(manifest=manifest)
    window._show_page(3)
    assert window.system.profile.currentData() == profile and "not a published profile" in window.system.profile.currentText()
    valid, message = window.system.validate_page()
    assert not valid and "profile" in message.lower()


def test_an_empty_profile_means_derive_it(make_window):
    manifest = InstallManifest()
    manifest.system.profile = ""
    window = make_window(manifest=manifest)
    window._show_page(3)
    assert window.system.profile.currentText() == "default/linux/amd64/23.0/desktop/plasma"
    assert window.system.validate_page() == (True, "")


def test_profile_consistency_rules():
    assert profile_problem("default/linux/amd64/23.0/systemd", "openrc", "amd64-openrc")
    assert profile_problem("default/linux/amd64/23.0", "systemd", "amd64-systemd")
    assert profile_problem("default/linux/amd64/23.0/no-multilib", "openrc", "amd64-openrc")
    assert profile_problem("default/linux/amd64/23.0/hardened", "openrc", "amd64-openrc")
    assert profile_problem("default/linux/amd64/23.0/desktop/plasma", "openrc", "amd64-openrc") == ""
    for init in ("openrc", "systemd"):
        for desktop in ("kde", "gnome", "xfce", "minimal"):
            for multilib, hardened, variant in ((True, False, f"amd64-{init}"), (False, False, f"amd64-nomultilib-{init}"), (True, True, f"amd64-hardened-{init}")):
                path = profile_path(init, desktop, multilib=multilib, hardened=hardened)
                assert path in PUBLISHED_PROFILES and profile_problem(path, init, variant) == ""
    assert len(PUBLISHED_PROFILES) == 14


# --- NOBS-7: the multilib box and the stage3 agree --------------------------------------------------------------------------------------------------------


def test_unchecking_and_rechecking_multilib_returns_to_the_standard_stage(make_window):
    window = make_window()
    page = window.system
    page.desktop_group.set_value("gnome")
    fresh = make_window().system
    fresh.desktop_group.set_value("gnome")
    page.multilib.setChecked(False)
    assert page.stage.currentData() == "amd64-nomultilib-openrc" and page.profile.currentText().endswith("/no-multilib")
    page.multilib.setChecked(True)
    assert page.stage.currentData() == "amd64-openrc"
    page.save_manifest()
    assert window.manifest.system.multilib is True and window.manifest.system.stage_variant == "amd64-openrc"
    assert page.profile.currentText() == fresh.profile.currentText()
    assert window.manifest.validate_sections()["system"] == []


def test_the_multilib_toggle_keeps_the_init_system(make_window):
    window = make_window()
    page = window.system
    page.init_group.set_value("systemd")
    page.multilib.setChecked(False)
    page.multilib.setChecked(True)
    assert page.stage.currentData() == "amd64-systemd"


# --- COMP-NEW-7: companions do not belong on a minimal system -----------------------------------------------------------------------------------------------


def test_switching_to_minimal_unticks_the_graphical_companions_and_back_restores_them(make_window):
    window = make_window()
    assert window.manifest.options.install_hwmonux and window.manifest.options.install_portagehub
    window.system.desktop_group.set_value("minimal")
    assert not window.manifest.options.install_hwmonux and not window.manifest.options.install_portagehub
    window._show_page(4)
    assert not window.customize.checks["install_hwmonux"].isChecked()
    window.customize.tabs.setCurrentIndex(2)
    assert window.customize.companion_note.isVisibleTo(window.customize)
    window.customize.checks["install_hwmonux"].setChecked(True)  # the user can tick them again
    window.customize.save_manifest()
    assert window.manifest.options.install_hwmonux
    window._show_page(3)
    window.system.desktop_group.set_value("xfce")
    assert window.manifest.options.install_portagehub  # restored


def test_a_loaded_minimal_manifest_keeps_its_companions(make_window):
    manifest = InstallManifest()
    manifest.system.desktop = "minimal"
    manifest.options.install_hwmonux = manifest.options.install_portagehub = True
    window = make_window(manifest=manifest)
    for index in range(1, 6):
        window._show_page(index)
    assert manifest.options.install_hwmonux and manifest.options.install_portagehub
    from gentoo_installer.pages.review import manifest_warnings

    assert any("graphical applications" in w for w in manifest_warnings(manifest))


# --- G9: build jobs ----------------------------------------------------------------------------------------------------------------------------------------


def test_jobs_default_to_what_the_computer_can_sustain(make_window, monkeypatch):
    from gentoo_installer.pages import customize

    monkeypatch.setattr(customize.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(customize.system_info, "_memory_gib", lambda: 16.0)
    assert customize.auto_jobs() == 8 and customize.auto_make_opts() == "-j8 -l32"
    monkeypatch.setattr(customize.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(customize.system_info, "_memory_gib", lambda: 4.0)
    assert customize.auto_make_opts() == "-j2 -l4"
    monkeypatch.setattr(customize.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(customize.system_info, "_memory_gib", lambda: 2.0)
    assert customize.auto_make_opts() == "-j1 -l2"


def test_an_explicit_jobs_value_from_a_manifest_is_never_replaced(make_window):
    manifest = InstallManifest()
    manifest.portage.make_opts = "-j2"
    window = make_window(manifest=manifest)
    window._show_page(4)
    assert not window.customize.jobs_auto.isChecked() and window.customize.jobs.value() == 2
    window.customize.save_manifest()
    assert manifest.portage.make_opts == "-j2"
    manifest.portage.make_opts = "-j6 -l8"
    window._show_page(3)
    window._show_page(4)
    window.customize.save_manifest()
    assert manifest.portage.make_opts == "-j6 -l8"  # extra flags survive an untouched round trip
    window.customize.jobs.setValue(3)
    window.customize.save_manifest()
    assert manifest.portage.make_opts == "-j3"


def test_automatic_jobs_are_computed_for_this_machine(make_window, monkeypatch):
    from gentoo_installer.pages import customize

    monkeypatch.setattr(customize.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(customize.system_info, "_memory_gib", lambda: 32.0)
    manifest = InstallManifest()
    manifest.portage.make_opts = ""
    window = make_window(manifest=manifest)
    window._show_page(4)
    assert window.customize.jobs_auto.isChecked() and window.customize.jobs.value() == 12
    window.customize.save_manifest()
    assert manifest.portage.make_opts == ""  # automatic: the installer sizes MAKEOPTS from the same machine


# --- G7: the saved draft ----------------------------------------------------------------------------------------------------------------------------------


def settings_file():
    return QSettings("GentooLive", "Installer")


def test_the_draft_is_private_and_holds_no_secret_or_proxy_password(make_window):
    manifest = InstallManifest()
    manifest.network.proxy = "http://alice:S3cretPass@proxy.corp:3128"
    manifest.accounts.password = "user-password"
    window = make_window(manifest=manifest)
    window.close()
    store = settings_file()
    draft = store.value("draft")
    assert "S3cretPass" not in draft and "user-password" not in draft
    assert json.loads(draft)["network"]["proxy"] == "http://proxy.corp:3128"
    import os
    import stat

    path = store.fileName()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700


@pytest.mark.parametrize("value", [["not", "a", "string"], 42, 3.5, "", "{not json", "[1, 2]", '{"unknown": 1}', '{"storage": 5}'])
def test_a_malformed_draft_never_stops_the_start(value):
    settings_file().setValue("draft", value)
    assert isinstance(load_draft(), InstallManifest)


def test_the_experience_mode_survives_a_restart(make_window):
    window = make_window()
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    window.close()
    reopened = make_window()
    assert reopened.mode.currentData() == "advanced"
    assert "All available controls are shown" in reopened.header_subtitle.text()


def test_guided_mode_hides_the_encryption_card_until_needed(make_window):
    window = make_window()
    window.mode.setCurrentIndex(window.mode.findData("guided"))
    assert not window.storage.encryption.isVisibleTo(window.storage)
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    assert window.storage.encryption.isVisibleTo(window.storage)


def test_blank_secrets_strips_credentials_everywhere():
    data = {"accounts": {"password": "x", "root_password": "y"}, "network": {"proxy": "https://u:p@h:1/", "wifi_password": "z"}, "storage": {"luks_passphrase": "q"}, "list": [{"password": "k"}]}
    cleaned = blank_secrets(data)
    assert cleaned["accounts"] == {"password": "", "root_password": ""}
    assert cleaned["network"] == {"proxy": "https://h:1/", "wifi_password": ""}
    assert cleaned["list"] == [{"password": ""}]


def test_a_redacted_export_can_be_imported_again(make_window, tmp_path, monkeypatch):
    source = InstallManifest()
    source.storage.device = "/dev/sda"
    source.system.desktop = "gnome"
    source.accounts.password = "user-password"
    source.accounts.root_password = "root-password"
    source.network.proxy = "http://alice:S3cretPass@proxy.corp:3128"
    path = tmp_path / "export.json"
    from gentoo_installer.pages.review import save_redacted_copy

    save_redacted_copy(source, path)
    loaded = load_redacted_manifest(path)
    assert loaded.accounts.password == "" and loaded.system.desktop == "gnome"
    assert loaded.network.proxy == "http://alice:<redacted>@proxy.corp:3128"
    window = make_window()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    informed = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text, *a, **k: informed.append(title))
    window.import_settings()
    assert window.manifest.system.desktop == "gnome" and window.manifest.accounts.password == ""
    assert informed == ["Settings imported"]


def test_importing_a_broken_file_explains_it(make_window, tmp_path, monkeypatch):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    window = make_window()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *a, **k: warned.append(title))
    window.import_settings()
    assert warned == ["Cannot import settings"]
    with pytest.raises(ManifestError):
        load_redacted_manifest(path)
