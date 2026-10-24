"""The Hardware tab, hardware defaults, and the hardware parts of Readiness and Review (offscreen Qt, fixture machines)."""

from __future__ import annotations

import copy
import dataclasses
from datetime import datetime

import pytest
from PySide6.QtWidgets import QCheckBox, QComboBox, QMessageBox

from gentoo_installer import app as app_module
from gentoo_installer.daemon_client import DaemonState
from gentoo_installer.manifest import HARDWARE_CHOICES, Hardware, InstallManifest
from gentoo_installer.pages import dialogs, hardware as hardware_page
from gentoo_installer.pages.hardware import HardwareTab, covered_fields, issue_section, nvidia_offer, plan_lines, resolved_text
from gentoo_installer.pages.review import blocking_problems, storage_plan_text
from gentoo_installer.system_info import DiskInfo, SystemInfo, profile_of
from tests.hardware.helpers import FIXTURE_NAMES, fixture_dict
from tests.ui.conftest import sample_info

GIB = 1024**3


def machine(name: str, **overrides) -> SystemInfo:
    """A SystemInfo whose hardware is one of the recorded machines (the disk list stays the usual test disk).

    The findings of the probe travel in the inspect reply as ``warnings``, next to ``hardware``."""
    hardware = fixture_dict(name)
    return sample_info(hardware=hardware, warnings=copy.deepcopy(hardware["warnings"]), **overrides)


def readiness_window(make_window, info: SystemInfo):
    """A window that has been told about ``info``. The first inspection runs in development mode, which replaces
    the daemon state of the object it is given, so the state is put back and the result shown again."""
    state = info.daemon
    window = make_window(info=info)
    info.daemon = state
    window.apply_info(info)
    return window


def tab_of(window) -> HardwareTab:
    window._show_page(4)
    window.customize.show_hardware()
    return window.customize.hardware


def combo(tab: HardwareTab, name: str) -> QComboBox:
    widget = tab.controls[name]
    assert isinstance(widget, QComboBox)
    return widget


def pick(widget: QComboBox, value: str) -> None:
    """Choose an item the way a user does (activated is only sent for a user's choice)."""
    index = widget.findData(value)
    assert index >= 0, (value, [widget.itemData(i) for i in range(widget.count())])
    widget.setCurrentIndex(index)
    widget.activated.emit(index)


# --- every manifest.hardware field has a control -----------------------------------------------------------------------------------


def test_every_hardware_field_has_a_control_and_every_choice_a_label():
    assert covered_fields() == {field.name for field in dataclasses.fields(Hardware)}
    for spec in hardware_page.SPECS:
        if spec.kind == "choice" and spec.name in HARDWARE_CHOICES:
            assert {value for value, _ in spec.choices} == HARDWARE_CHOICES[spec.name], spec.name


def test_the_tab_is_the_first_one_of_the_customization_page(make_window):
    window = make_window()
    assert window.customize.tabs.tabText(0) == "Hardware" and window.customize.tabs.widget(0) is window.customize.hardware


# --- nothing is rewritten by looking ------------------------------------------------------------------------------------------------


EXPLICIT = dict(target="portable", gpu_driver="nvidia", nvidia_branch="590", nvidia_license_accepted=True, nvk="yes", prime="off", cpu_flags_mode="manual",
                power="tlp", thermald="no", fingerprint=True, fwupd=True, guest_tools="none", zram="yes", zram_percent=35, hibernate=True, trim="weekly",
                luks_discard=True, sof_firmware="no", printer_drivers=["hplip"], hidpi="dpi:130", removable_fallback="if-missing", pipewire_alsa=True)


@pytest.mark.parametrize("name", [None, *FIXTURE_NAMES])
def test_visiting_the_tab_never_changes_loaded_values(make_window, name):
    manifest = InstallManifest()
    manifest.hardware = Hardware(**EXPLICIT)
    window = make_window(manifest=manifest, info=machine(name) if name else sample_info())
    before = copy.deepcopy(window.manifest.hardware)
    for _ in range(2):
        tab_of(window)
        window.customize.save_manifest()
        window._show_page(3)
        window._show_page(4)
    assert window.manifest.hardware == before == Hardware(**EXPLICIT)


@pytest.mark.parametrize("name", [None, *FIXTURE_NAMES])
def test_default_choices_stay_on_auto_after_a_visit(make_window, name):
    window = make_window(info=machine(name) if name else sample_info())
    tab_of(window)
    window.customize.save_manifest()
    assert window.manifest.hardware == Hardware()


def test_without_detection_the_tab_says_so_and_keeps_automatic(make_window):
    window = make_window()
    tab = tab_of(window)
    assert "could not be detected" in tab.summary.text()
    assert not tab.plan_card.isVisibleTo(tab)
    assert combo(tab, "zram").currentData() == "auto"


# --- what the tab shows for real machines ---------------------------------------------------------------------------------------------


def test_the_summary_line_and_the_plan_come_from_the_detected_machine(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    tab = tab_of(window)
    assert "Intel Core i9-14900K" in tab.summary.text() and "RTX 4090" in tab.summary.text()
    assert tab.plan_card.isVisibleTo(tab)
    assert any("Intel microcode" in line for line in plan_lines(hardware_page.hardware_plan(window.manifest, window.info.hardware)))
    assert "Intel microcode" in tab.plan_text.text()


def test_the_nvidia_driver_is_offered_only_where_the_planner_says_it_fits(make_window):
    desktop = make_window(info=machine("asus-desktop-rtx4090"))
    tab = tab_of(desktop)
    assert tab.gpu_driver.cards["nvidia"].isVisibleTo(tab) and tab.gpu_driver.cards["nvidia"].isEnabled()
    offer = nvidia_offer(desktop.manifest, profile_of(desktop.info.hardware))
    assert offer.available and offer.branch == "595" and offer.licence == "NVIDIA-2025"
    # no NVIDIA card in this computer: the choice is not shown at all
    radeon = make_window(info=machine("old-bios-sata-radeon"))
    tab = tab_of(radeon)
    assert not tab.gpu_driver.cards["nvidia"].isVisibleTo(tab)
    assert not nvidia_offer(radeon.manifest, profile_of(radeon.info.hardware)).available
    # an old card that no proprietary branch supports is shown with the reason, but cannot be picked
    old = machine("asus-desktop-rtx4090")
    old.hardware["gpus"][0]["generation"] = "kepler"
    kepler = make_window(info=old)
    tab = tab_of(kepler)
    assert tab.gpu_driver.cards["nvidia"].isVisibleTo(tab) and not tab.gpu_driver.cards["nvidia"].isEnabled()
    assert "not supported" in tab.nvidia_reason.text()


def test_maxwell_cards_get_the_long_term_branch(make_window):
    window = make_window(info=machine("intel-rst-hidden-nvme"))
    offer = nvidia_offer(window.manifest, profile_of(window.info.hardware))
    assert offer.available and offer.branch == "580" and "long-term" in offer.label


def test_nvidia_needs_the_licence_checkbox_and_a_distribution_kernel(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    tab = tab_of(window)
    assert not tab.nvidia_license.isVisibleTo(tab)
    tab.gpu_driver.set_value("nvidia")
    assert tab.nvidia_license.isVisibleTo(tab) and "NVIDIA-2025" in tab.nvidia_license.text()
    assert "licence" in " ".join(tab.problems())
    tab.nvidia_license.setChecked(True)
    assert tab.problems() == []
    window.customize.save_manifest()
    assert (window.manifest.hardware.gpu_driver, window.manifest.hardware.nvidia_license_accepted) == ("nvidia", True)
    assert window.customize.validate_page() == (True, "")
    # switching back to the open driver forgets the licence
    tab.gpu_driver.set_value("open")
    window.customize.save_manifest()
    assert (window.manifest.hardware.gpu_driver, window.manifest.hardware.nvidia_license_accepted) == ("open", False)


def test_nvidia_with_secure_boot_is_not_available_and_says_why(make_window):
    manifest = InstallManifest()
    manifest.system.secure_boot = True
    window = make_window(manifest=manifest, info=machine("asus-desktop-rtx4090"))
    tab = tab_of(window)
    assert not tab.gpu_driver.cards["nvidia"].isEnabled() and "Secure Boot" in tab.nvidia_reason.text()


def test_a_loaded_nvidia_choice_that_cannot_work_here_is_refused_not_replaced(make_window):
    manifest = InstallManifest()
    manifest.hardware.gpu_driver = "nvidia"
    manifest.hardware.nvidia_license_accepted = True
    window = make_window(manifest=manifest, info=machine("old-bios-sata-radeon"))
    tab = tab_of(window)
    assert tab.gpu_driver.value() == "nvidia"
    valid, message = window.customize.validate_page()
    assert not valid and "NVIDIA" in message
    window.customize.save_manifest()
    assert window.manifest.hardware.gpu_driver == "nvidia"


def test_prime_is_shown_for_hybrid_laptops_only(make_window):
    hybrid = tab_of(make_window(info=machine("thinkpad-intel-nvidia-hybrid")))
    assert hybrid.row_shown("prime")
    assert "Automatic (install the switching helpers)" in combo(hybrid, "prime").itemText(0)
    assert not tab_of(make_window(info=machine("asus-desktop-rtx4090"))).row_shown("prime")
    assert not tab_of(make_window(info=machine("amd-laptop-sof"))).row_shown("prime")


def test_power_rows_follow_the_machine_and_the_experience(make_window):
    desktop = make_window(info=machine("asus-desktop-rtx4090"))
    tab = tab_of(desktop)
    assert not tab.row_shown("power") and not tab.row_shown("thermald")
    desktop.mode.setCurrentIndex(1)  # Advanced
    assert tab.advanced and not tab.row_shown("power")  # a desktop has no battery to manage
    laptop = make_window(info=machine("amd-laptop-sof"))
    tab = tab_of(laptop)
    assert tab.row_shown("power") and "Automatic (power-profiles-daemon)" in combo(tab, "power").itemText(0)
    assert not tab.row_shown("thermald")  # an AMD processor has no thermald
    assert tab_of(make_window(info=machine("thinkpad-intel-nvidia-hybrid"))).row_shown("fingerprint")
    assert not tab_of(make_window(info=machine("amd-laptop-sof"))).row_shown("fingerprint")


def test_guest_tools_only_in_a_virtual_machine(make_window):
    vm = tab_of(make_window(info=machine("qemu-q35-virtio")))
    assert vm.row_shown("guest_tools") and vm.cards["vm"].isVisibleTo(vm)
    assert not tab_of(make_window(info=machine("asus-desktop-rtx4090"))).cards["vm"].isVisibleTo(tab_of(make_window(info=machine("asus-desktop-rtx4090"))))


def test_the_guided_view_hides_expert_rows_unless_they_are_set(make_window):
    window = make_window(info=machine("amd-laptop-sof"))
    tab = tab_of(window)
    for name in ("trim", "sof_firmware", "cpu_flags_mode", "pipewire_alsa", "hibernate", "removable_fallback", "zram_percent"):
        assert not tab.row_shown(name), name
    window.mode.setCurrentIndex(1)
    for name in ("trim", "sof_firmware", "cpu_flags_mode", "pipewire_alsa", "hibernate", "removable_fallback"):
        assert tab.row_shown(name), name
    window.mode.setCurrentIndex(0)
    window.manifest.hardware.trim = "off"
    tab.load_manifest()
    assert tab.row_shown("trim")  # a value that differs from the default is never hidden


def test_hibernation_is_blocked_with_disk_encryption(make_window):
    manifest = InstallManifest()
    manifest.storage.luks = True
    window = make_window(manifest=manifest, info=machine("amd-laptop-sof"))
    window.mode.setCurrentIndex(1)
    tab = tab_of(window)
    hibernate = tab.controls["hibernate"]
    assert isinstance(hibernate, QCheckBox) and not hibernate.isEnabled() and "encryption" in hibernate.toolTip()
    assert tab.row_shown("luks_discard")


def test_portable_is_a_choice_of_its_own_and_changes_the_plan(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    tab = tab_of(window)
    assert tab.target.value() == "this"
    tab.target.set_value("portable")
    assert "Intel and AMD microcode" in tab.plan_text.text()
    window.customize.save_manifest()
    assert window.manifest.hardware.target == "portable"


def test_a_removable_target_disk_suggests_portable(make_window):
    info = machine("asus-desktop-rtx4090")
    info.hardware["disks"][0]["hotplug"] = True
    info.hardware["disks"][0]["removable"] = True
    manifest = InstallManifest()
    manifest.storage.device = info.hardware["disks"][0]["path"]
    window = make_window(manifest=manifest, info=info)
    assert "portable" in tab_of(window).advice.text()


def test_choices_reach_the_manifest_and_change_the_plan(make_window):
    window = make_window(info=machine("amd-laptop-sof"))
    window.mode.setCurrentIndex(1)
    tab = tab_of(window)
    pick(combo(tab, "power"), "tlp")
    assert "TLP" in tab.plan_text.text()
    pick(combo(tab, "zram"), "yes")
    assert tab.row_shown("zram_percent")
    spin = tab.controls["zram_percent"]
    spin.setValue(30)
    tab.printer_checks["hplip"].setChecked(True)
    pick(combo(tab, "hidpi"), "custom")
    tab.controls["hidpi_custom"].setValue(168)
    window.customize.save_manifest()
    hardware = window.manifest.hardware
    assert (hardware.power, hardware.zram, hardware.zram_percent, hardware.printer_drivers, hardware.hidpi) == ("tlp", "yes", 30, ["hplip"], "dpi:168")
    assert hardware.trim == "auto" and hardware.target == "this"  # untouched controls keep their value


def test_a_custom_value_is_shown_flagged_and_kept(make_window):
    manifest = InstallManifest()
    manifest.hardware.hidpi = "dpi:130"
    manifest.hardware.nvidia_branch = "590"
    window = make_window(manifest=manifest, info=machine("asus-desktop-rtx4090"))
    window.mode.setCurrentIndex(1)
    tab = tab_of(window)
    assert combo(tab, "hidpi").currentData() == "custom" and tab.controls["hidpi_custom"].value() == 130
    assert "590" in tab.nvidia_branch.currentText()
    window.customize.save_manifest()
    assert (window.manifest.hardware.hidpi, window.manifest.hardware.nvidia_branch) == ("dpi:130", "590")


def test_the_automatic_item_names_what_automatic_means_here(make_window):
    window = make_window(info=machine("qemu-q35-virtio"))
    window.mode.setCurrentIndex(1)
    tab = tab_of(window)
    assert combo(tab, "zram").itemText(0) == "Automatic (on)"  # 4 GiB of memory
    assert combo(tab, "trim").itemText(0).startswith("Automatic")
    assert resolved_text("power", {"power": "tlp"}) == "TLP" and resolved_text("hidpi", {"hidpi": "dpi:144"}) == "144 dpi" and resolved_text("power", {}) == ""


def test_the_planners_cross_checks_are_sorted_by_code():
    assert issue_section("hw.bios_4kn") == "storage" and issue_section("hw.st_live_medium") == "storage" and issue_section("hw.fw_other_type") == "storage"
    assert issue_section("hw.net_wifi_needs_nm") == "network" and issue_section("hw.nvidia_kernel") == "hardware" and issue_section("hw.hibernate_luks") == "hardware"


def test_hardware_errors_block_the_customization_page_and_name_the_tab(make_window):
    manifest = InstallManifest()
    manifest.hardware.hibernate = True
    manifest.storage.luks = True
    window = make_window(manifest=manifest, info=machine("amd-laptop-sof"))
    window._show_page(4)
    window.customize.tabs.setCurrentIndex(2)
    valid, message = window.customize.validate_page()
    assert not valid and "Hibernation" in message
    assert window.customize.tabs.currentWidget() is window.customize.hardware


def test_hardware_problems_found_late_lead_back_to_the_hardware_tab(make_window, monkeypatch):
    manifest = InstallManifest()
    manifest.hardware.hibernate = True
    manifest.storage.luks = True
    manifest.storage.luks_passphrase = "long-enough-passphrase"
    info = machine("amd-laptop-sof")
    manifest.storage.device = info.hardware["disks"][0]["path"]
    window = make_window(manifest=manifest, info=info)
    window.manifest.accounts.password = window.manifest.accounts.root_password = "long-password"
    window._show_page(5)
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *a, **k: shown.append((title, text)))
    window.accounts.validate_page = lambda: (True, "")
    window.next.click()
    assert shown and shown[0][0] == "Installation settings" and "Hibernation" in shown[0][1]
    assert window.current == 4 and window.customize.tabs.currentWidget() is window.customize.hardware


# --- defaults from the detected machine --------------------------------------------------------------------------------------------------


def test_the_first_inspection_seeds_what_the_machine_suggests(make_window):
    window = make_window(info=machine("qemu-q35-virtio"))
    options, storage = window.manifest.options, window.manifest.storage
    assert options.microcode is False  # the hypervisor owns the microcode
    assert options.bluetooth is False  # no radio in a virtual machine
    assert storage.swap_mib == 2048  # 4 GiB of memory with compressed RAM swap
    assert window.manifest.hardware == Hardware()  # the hardware choices themselves stay on automatic


def test_explicit_values_are_never_replaced_by_the_seeding(make_window):
    manifest = InstallManifest()
    manifest.storage.swap_mib = 1024
    manifest.options.microcode = True
    window = make_window(manifest=manifest, info=machine("qemu-q35-virtio"))
    assert window.manifest.storage.swap_mib == 1024  # not the factory default: kept
    assert window.manifest.options.microcode is False  # the factory default True is seeded (microcode makes no sense in a VM)


def test_a_manifest_from_the_command_line_keeps_its_firmware(make_window):
    manifest = InstallManifest()
    manifest.storage.firmware = "bios"
    window = make_window(manifest=manifest, info=machine("qemu-q35-virtio"), firmware_explicit=True)
    assert window.manifest.storage.firmware == "bios"
    other = make_window(manifest=InstallManifest(), info=machine("old-bios-sata-radeon"))
    assert other.manifest.storage.firmware == "bios"  # the live firmware of a fresh manifest


def test_check_again_corrects_the_seeding_until_the_user_changes_the_value(make_window):
    first = machine("qemu-q35-virtio")
    window = make_window(info=first)
    assert window.manifest.storage.swap_mib == 2048
    bigger = machine("asus-desktop-rtx4090")
    window.apply_info(bigger)
    assert window.manifest.storage.swap_mib == 8192  # 47 GiB of memory: the seeding was corrected
    window.manifest.storage.swap_mib = 3000  # the user chose
    window.apply_info(first)
    assert window.manifest.storage.swap_mib == 3000


def test_no_detection_data_seeds_nothing(make_window):
    window = make_window(info=sample_info())
    assert window.manifest.storage.swap_mib == 4096 and window.manifest.options.microcode is True


def test_imported_settings_are_not_seeded_afterwards(make_window, tmp_path, monkeypatch):
    window = make_window(info=machine("qemu-q35-virtio"))
    other = InstallManifest()
    other.options.microcode = True
    other.storage.swap_mib = 4096
    path = tmp_path / "settings.json"
    other.save(path)
    monkeypatch.setattr(app_module.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), "")))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    window.import_settings()
    window.apply_info(machine("qemu-q35-virtio"))
    assert window.manifest.options.microcode is False  # a new inspection seeds again, but never undoes an edit it did not make
    assert window._seeded  # and keeps track of it


# --- Readiness ----------------------------------------------------------------------------------------------------------------------------


def test_readiness_shows_a_row_for_the_computer(make_window):
    window = make_window(info=machine("thinkpad-intel-nvidia-hybrid"))
    row = window.readiness.rows["hardware"]
    assert row.status == "ok" and "i7-11800H" in row.detail.text() and "RTX A5000" in row.detail.text()
    assert window.readiness.hardware_hint.isVisibleTo(window.readiness)
    undetected = make_window(info=sample_info())
    assert undetected.readiness.rows["hardware"].status in {"unknown", "info"}


def test_warnings_are_listed_errors_first_with_the_fix_the_daemon_offers(make_window):
    info = machine("amd-laptop-sof", daemon=DaemonState("running", features=frozenset({"hardware.v1"}), actions=frozenset({"sync_time", "reboot_firmware"})))
    window = readiness_window(make_window, info)
    page = window.readiness
    rows = [page.notes_box.itemAt(i).widget() for i in range(page.notes_box.count())]
    severities = [row.status for row in rows]
    assert severities == sorted(severities, key=lambda status: {"error": 0, "warning": 1}.get(status, 2))
    boot = next(row for row in rows if "Secure Boot is turned on" in row.detail.text())
    assert boot.action is not None and boot.action.text() == "Restart into firmware settings"
    clock = next(row for row in rows if "not been checked against the network" in row.detail.text())
    assert clock.action is not None and clock.action.text() == "Set clock from network"
    charger = next(row for row in rows if "charger" in row.detail.text())
    assert charger.action is None and "Connect the charger" in charger.detail.text()


def test_without_the_action_the_row_explains_what_to_do(make_window):
    info = machine("amd-laptop-sof", daemon=DaemonState("running", features=frozenset({"attach.v1"})))
    window = readiness_window(make_window, info)
    page = window.readiness
    rows = [page.notes_box.itemAt(i).widget() for i in range(page.notes_box.count())]
    assert all(row.action is None for row in rows)
    boot = next(row for row in rows if "Secure Boot is turned on" in row.detail.text())
    assert "firmware settings" in boot.detail.text() and "F2" in boot.detail.text()


def test_a_button_of_a_finding_starts_the_daemon_action_and_shows_the_answer(make_window, monkeypatch, fake_daemon):
    def handler(request):
        if request["action"] == "status":
            return [{"type": "status", "data": {"status": "idle"}}, {"type": "completed", "data": {"success": True}}]
        if request["action"] == "sync_time":
            return [{"type": "result", "data": {"ok": True, "message": "The clock was set from the network."}}, {"type": "completed", "data": {"success": True}}]
        return [{"type": "completed", "data": {"success": True}}]

    daemon = fake_daemon(handler)
    info = machine("amd-laptop-sof", daemon=DaemonState("running", features=frozenset({"hardware.v1"}), actions=frozenset({"sync_time"})))
    window = readiness_window(make_window, info)
    window.socket_path = daemon.path
    page = window.readiness
    row = next(page.notes_box.itemAt(i).widget() for i in range(page.notes_box.count()) if "not been checked" in page.notes_box.itemAt(i).widget().detail.text())
    window.refresh_readiness = lambda: None  # the answer is what matters here
    assert row.action is not None
    row.action.click()
    deadline = 0
    from PySide6.QtWidgets import QApplication
    while not page.action_note.text() and deadline < 300:
        QApplication.processEvents()
        deadline += 1
        import time
        time.sleep(0.01)
    assert page.action_note.text() == "The clock was set from the network."
    assert [r["action"] for r in daemon.requests if r["action"] != "status"] == ["sync_time"]


def test_set_time_asks_for_the_time_and_sends_it_in_utc(make_window, monkeypatch):
    window = make_window()
    sent = []
    monkeypatch.setattr(app_module, "ask_utc_time", lambda parent: "2026-10-24 12:00:00")
    monkeypatch.setattr(window, "sync_clock", lambda action, payload=None: sent.append((action, payload)))
    window.run_hardware_action("set_time")
    assert sent == [("set_time", {"utc": "2026-10-24 12:00:00"})]
    sent.clear()
    monkeypatch.setattr(app_module, "ask_utc_time", lambda parent: "")
    window.run_hardware_action("set_time")
    assert sent == []  # cancelled


def test_restarting_into_the_firmware_settings_asks_first(make_window, monkeypatch):
    window = make_window()
    sent = []
    monkeypatch.setattr(window, "sync_clock", lambda action, payload=None: sent.append(action))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    window.run_hardware_action("reboot_firmware")
    assert sent == []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.run_hardware_action("reboot_firmware")
    assert sent == ["reboot_firmware"]


def test_the_time_dialog_converts_the_chosen_zone_to_utc():
    assert dialogs.utc_text(datetime(2026, 10, 24, 14, 30, 5), "Europe/Berlin") == "2026-10-24 12:30:05"
    assert dialogs.utc_text(datetime(2026, 1, 5, 9, 0, 0), "America/New_York") == "2026-01-05 14:00:00"
    with pytest.raises(ValueError):
        dialogs.utc_text(datetime(2026, 1, 1), "Nowhere/Land")


def test_the_time_dialog_validates_and_returns_utc(qtbot):
    dialog = dialogs.SetTimeDialog()
    qtbot.addWidget(dialog)
    dialog.zone.setCurrentText("Asia/Tokyo")
    dialog.when.setDateTime(dialog.when.dateTime().fromString("2026-10-24 21:00:00", "yyyy-MM-dd HH:mm:ss"))
    assert dialog.value() == "2026-10-24 12:00:00"
    dialog.zone.setCurrentText("Nowhere/Land")
    dialog._accept()
    assert dialog.problem.isVisibleTo(dialog) and "Unknown time zone" in dialog.problem.text()


# --- Review -------------------------------------------------------------------------------------------------------------------------------


def test_review_shows_the_hardware_setup_and_the_storage_plan(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    window.manifest.storage.device = "/dev/sda"
    window._show_page(6)
    review = window.review
    assert review.hardware_card.isVisibleTo(review) and "Intel microcode" in review.hardware_plan.text()
    text = review.storage_plan.text()
    assert "ERASE" in text and "/dev/sda" in text and "CREATE" in text and "EFI system partition" in text
    assert text == storage_plan_text(window.manifest, window.info)


def test_review_without_detection_has_no_hardware_card_but_still_the_storage_plan(make_window):
    window = make_window()
    window.manifest.storage.device = "/dev/sda"
    window._show_page(6)
    assert not window.review.hardware_card.isVisibleTo(window.review) and "ERASE" in window.review.storage_plan.text()


def test_the_erase_confirmation_lists_what_will_be_created(make_window, monkeypatch):
    window = make_window()
    window.manifest.storage.device = "/dev/sda"
    seen = {}

    def warning(parent, title, text, buttons, default):
        seen.update(title=title, text=text)
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "warning", warning)
    window.start_installation()
    assert seen["title"] == "Erase the selected disk?"
    assert "It will be set up like this:" in seen["text"] and "EFI system partition" in seen["text"] and "root" in seen["text"]


def test_a_disk_too_small_for_the_desktop_is_refused_before_the_start(make_window):
    window = make_window(info=sample_info(disks=[DiskInfo("/dev/sda", "Small disk", 20 * GIB, serial="s")]))
    window.manifest.storage.device = "/dev/sda"
    problems = blocking_problems(window.manifest, window.info)
    assert problems and problems[0][0] == "storage" and "too small" in problems[0][1]
    window.manifest.system.desktop = "minimal"
    assert blocking_problems(window.manifest, window.info) == []
    window.manifest.accounts.password = window.manifest.accounts.root_password = "long-password"
    window._show_page(6)
    window.review.confirmation.setText("/dev/sda")
    assert window.review.validate_page() == (True, "")
