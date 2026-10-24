from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QMessageBox

from gentoo_installer.app import InstallerWindow, bounded_window_height
from gentoo_installer.manifest import InstallManifest, Partition
from gentoo_installerd.events import Event
from gentoo_installer.system_info import DiskInfo, PartitionInfo, SystemInfo
from tests.vm.helpers import PUBLIC_KEY, load_vm_module


@pytest.fixture
def info():
    return SystemInfo(
        architecture="x86_64",
        memory_gib=16,
        uefi=True,
        disks=[DiskInfo("/dev/sda", "Virtual disk", 64 * 1024**3, serial="test-disk")],
        network=True,
        synchronized_clock=True,
        missing_tools=[],
    )


@pytest.fixture
def window(qtbot, monkeypatch, info, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("gentoo_installer.app.inspect_system", lambda: info)
    widget = InstallerWindow(InstallManifest(), str(tmp_path / "backend.sock"), background_inspection=False)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def populate(window):
    window.storage.disk_group.set_value("/dev/sda")
    window.storage.save_manifest()
    window.accounts.password.setText("user-password")
    window.accounts.password_confirm.setText("user-password")
    window.accounts.root_password.setText("root-password")
    window.accounts.root_confirm.setText("root-password")
    for page in window.pages[:6]:
        page.save_manifest()


@pytest.mark.parametrize(
    ("requested", "available", "expected"),
    [
        (790, 772, 724),  # 800px XFCE desktop with its top panel.
        (700, 772, 700),  # Do not expand an intentionally smaller window.
        (790, 680, 632),  # Leave room for the title bar and panel.
        (790, 600, 560),  # The minimum stays at 560 on very short screens.
    ],
)
def test_initial_window_height_fits_available_desktop(requested, available, expected):
    assert bounded_window_height(requested, available) == expected


def test_window_keeps_resizable_minimum_and_clamps_to_screen(window):
    screen = window.screen()
    assert window.minimumHeight() == 560
    assert window.minimumWidth() == 800
    assert window.maximumHeight() > window.minimumHeight()
    assert window.height() <= max(560, screen.availableGeometry().height() - 48)


def test_every_page_constructs_and_choice_cards_do_not_recurse(window):
    for index in range(7):
        window._show_page(index)
        assert window.stack.currentIndex() == index
    window.system.init_group.set_value("systemd")
    assert window.system.init_group.value() == "systemd"
    assert window.system.stage.currentData().endswith("systemd")
    window.system.desktop_group.set_value("gnome")
    assert "/desktop/gnome/" in window.system.profile.currentText()
    window.system.desktop_group.set_value("minimal")
    assert "/desktop/" not in window.system.profile.currentText()


def test_system_profiles_are_published_for_current_init_and_choices(window):
    supported = {
        "default/linux/amd64/23.0",
        "default/linux/amd64/23.0/systemd",
        "default/linux/amd64/23.0/desktop",
        "default/linux/amd64/23.0/desktop/systemd",
        "default/linux/amd64/23.0/desktop/plasma",
        "default/linux/amd64/23.0/desktop/plasma/systemd",
        "default/linux/amd64/23.0/desktop/gnome",
        "default/linux/amd64/23.0/desktop/gnome/systemd",
        "default/linux/amd64/23.0/hardened",
        "default/linux/amd64/23.0/hardened/systemd",
        "default/linux/amd64/23.0/no-multilib",
        "default/linux/amd64/23.0/no-multilib/systemd",
        "default/linux/amd64/23.0/no-multilib/hardened",
        "default/linux/amd64/23.0/no-multilib/hardened/systemd",
    }
    page = window.system
    for init in ("openrc", "systemd"):
        page.init_group.set_value(init)
        for prefix in ("amd64", "amd64-desktop", "amd64-nomultilib", "amd64-hardened"):
            variant = f"{prefix}-{init}"
            page.stage.setCurrentIndex(page.stage.findData(variant))
            for desktop in ("kde", "gnome", "xfce", "minimal"):
                page.desktop_group.set_value(desktop)
                assert page.profile.currentText() in supported, (variant, desktop, page.profile.currentText())

    page.init_group.set_value("systemd")
    page.stage.setCurrentIndex(page.stage.findData("amd64-hardened-systemd"))
    assert page.profile.currentText() == "default/linux/amd64/23.0/hardened/systemd"
    page.stage.setCurrentIndex(page.stage.findData("amd64-nomultilib-systemd"))
    page.desktop_group.set_value("kde")
    assert page.profile.currentText() == "default/linux/amd64/23.0/no-multilib/systemd"


def test_default_gui_manifest_selects_kde_profile_for_amd64_openrc():
    manifest = InstallManifest()
    assert manifest.system.stage_variant == "amd64-openrc"
    assert manifest.system.init == "openrc"
    assert manifest.system.desktop == "kde"
    assert manifest.system.profile == "default/linux/amd64/23.0/desktop/plasma"


def test_kernel_guidance_explains_automatic_kernel_dracut_requirement(window):
    guidance = window.system.kernel_guidance.text()
    assert "distribution kernels require Dracut" in guidance
    assert "Manual kernel configuration" not in guidance
    assert [window.system.kernel.itemData(i) for i in range(window.system.kernel.count())] == ["gentoo-kernel-bin", "gentoo-kernel"]
    assert [window.system.initramfs.itemData(i) for i in range(window.system.initramfs.count())] == ["dracut"]


def test_guided_choices_reach_a_valid_review(window):
    populate(window)
    assert window.manifest.validate() == []
    window._show_page(6)
    window.review.confirmation.setText("/dev/sda")
    assert window.review.validate_page() == (True, "")
    assert window.review.summary.rowCount() >= 8


def test_continue_buttons_traverse_full_wizard_before_starting_install(window, monkeypatch):
    populate(window)
    reached = []
    monkeypatch.setattr(window, "start_installation", lambda: reached.append(window.current))
    assert window.current == 0
    for expected_page in range(1, 7):
        window.next.click()
        assert window.current == expected_page
        reached.append(expected_page)
    assert window.manifest.storage.device == "/dev/sda"
    assert window.manifest.storage.device_id == "test-disk"
    assert window.manifest.validate() == []
    window.review.confirmation.setText("/dev/sda")
    window.next.click()
    assert reached[-1] == 6


@pytest.mark.parametrize("scenario_name", ["openrc", "systemd", "uefi-luks", "bios", "defaults"])
def test_vm_harness_manifests_survive_real_wizard_roundtrip(qtbot, monkeypatch, tmp_path, scenario_name):
    harness = load_vm_module()
    scenario = harness.SCENARIO_MAP[scenario_name]
    manifest = harness.build_manifest(scenario, PUBLIC_KEY)
    expected_manifest = manifest.to_dict(redact=False)
    info = SystemInfo(
        architecture="x86_64", memory_gib=16, uefi=scenario.firmware == "uefi",
        disks=[DiskInfo("/dev/vda", "QEMU", 32 * 1024**3, serial="GENTOO-VM-DISPOSABLE")],
        network=True, synchronized_clock=True, missing_tools=[],
    )
    monkeypatch.setattr("gentoo_installer.app.inspect_system", lambda: info)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / scenario_name))
    widget = InstallerWindow(manifest, str(tmp_path / "backend.sock"), background_inspection=False)
    qtbot.addWidget(widget)
    widget.show()
    started = []
    monkeypatch.setattr(widget, "start_installation", lambda: started.append(True))

    for expected_page in range(1, 7):
        widget.next.click()
        assert widget.current == expected_page, (scenario_name, expected_page)
    assert widget.manifest.to_dict(redact=False) == expected_manifest
    assert widget.manifest.accounts.disable_root_login is (not scenario.defaults)  # the defaults scenario keeps root login
    assert widget.manifest.accounts.root_password == harness.TEST_ROOT_PASSWORD
    widget.review.confirmation.setText("/dev/vda")
    widget.next.click()
    assert started == [True]


def test_manual_partition_rows_do_not_duplicate_on_navigation(window):
    window.info.disks[0].partitions = [
        PartitionInfo("/dev/sda1", 512 * 1024**2, "vfat", parttype="c12a7328-f81f-11d2-ba4b-00a0c93ec93b", parent="/dev/sda"),
        PartitionInfo("/dev/sda2", 40 * 1024**3, "ext4", parent="/dev/sda"),
    ]
    window.apply_info(window.info)
    window.manifest.storage.device = "/dev/sda"
    window.manifest.storage.mode = "manual"
    window.manifest.storage.partitions = [
        Partition(device="/dev/sda1", mountpoint="/efi", filesystem="vfat"),
        Partition(device="/dev/sda2", mountpoint="/"),
    ]
    window._show_page(2)
    window._show_page(1)
    window._show_page(2)
    assert window.storage.partitions.rowCount() == 2
    window.storage.mode.setCurrentIndex(1)
    assert window.storage.validate_page() == (True, "")


def test_password_mismatch_is_explained(window):
    window.accounts.password.setText("first-password")
    window.accounts.password_confirm.setText("other-password")
    valid, message = window.accounts.validate_page()
    assert not valid
    assert "do not match" in message


def test_progress_displays_real_events(window):
    window._show_page(7)
    window.progress.handle_event({"event": "stage", "stage": "stage3", "progress": 35, "message": "Verifying archive"})
    assert window.progress.progress.value() == 35
    assert "Gentoo" in window.progress.stage.text()
    window.progress.handle_event({"event": "log", "message": "gpgv: good signature"})
    assert "good signature" in window.progress.log.toPlainText()
    window.progress.handle_event({"event": "completed", "message": "Target checks passed"})
    assert window.progress.reboot.isVisible()
    assert window.progress.progress.value() == 100


def test_failure_event_then_completed_failure_never_offers_reboot(window):
    page = window.progress
    emitted = []
    page.finished.connect(emitted.append)
    for backend_event in (
        Event("error", message="simulated stage failed", error="simulated stage failed"),
        Event("completed", message="Installation failed", data={"success": False}),
    ):
        page.handle_event(json.loads(backend_event.to_json()))
    assert emitted == [False]
    assert page.stage.text() == "Installation needs attention"
    assert page.detail.text() == "simulated stage failed"
    assert page.reboot.isHidden()


def test_cleanup_events_do_not_overwrite_terminal_failure_state(window):
    page = window.progress
    emitted = []
    page.finished.connect(emitted.append)
    events = (
        Event("error", message="kernel USE dependency failed", error="kernel USE dependency failed"),
        Event("stage", stage="cleanup", message="Cleanup completed", progress=100,
              data={"status": "completed"}),
        Event("completed", message="Installation failed", data={"success": False}),
    )
    for backend_event in events:
        page.handle_event(json.loads(backend_event.to_json()))
    assert emitted == [False]
    assert page.stage.text() == "Installation needs attention"
    assert page.detail.text() == "kernel USE dependency failed"
    assert page.progress.value() == 0
    assert page.reboot.isHidden()


def test_netifrc_interface_selection_reaches_manifest(window):
    populate(window)
    window.customize.network_manager.setCurrentIndex(window.customize.network_manager.findData("netifrc"))
    window.customize.interface.setText("enp1s0")
    window.customize.save_manifest()
    assert window.manifest.network.interface == "enp1s0"
    assert window.manifest.validate() == []


def test_close_persists_no_passwords(window):
    populate(window)
    window.close()
    settings = QSettings("GentooLive", "Installer")
    draft = settings.value("draft")
    assert "user-password" not in draft
    assert "root-password" not in draft
    assert json.loads(draft)["accounts"]["password"] == ""


def test_close_requests_cancel_and_waits_for_worker_to_stop(window, monkeypatch, qtbot):
    monkeypatch.setattr(InstallerWindow, "_confirm_close_during_install", lambda self: True)
    calls = []
    window.progress.stop = lambda: calls.append("cancel")
    window.install_started = True
    window.close()
    assert window.isVisible()
    assert calls == ["cancel"]
    assert window.install_started
    window._worker_stopped()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=1000)


def test_progress_stop_requests_cancel_without_waiting_on_qthread(window):
    class RunningWorker:
        def __init__(self):
            self.cancelled = False

        def isRunning(self):
            return True

        def cancel(self):
            self.cancelled = True

        def wait(self, *_args):
            raise AssertionError("GUI must not block waiting for the worker thread")

    worker = RunningWorker()
    window.progress.worker = worker
    window.progress.stop()
    assert worker.cancelled
