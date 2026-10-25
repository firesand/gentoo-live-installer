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
from tests.hardware.helpers import FIXTURE_NAMES, fixture_dict, untouched_hardware
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

# Wave 5 fields whose control a later step adds; the owner of a control removes the name here (the test then keeps it covered). All have one now.

WAVE5_FIELDS_WITHOUT_CONTROL: set[str] = set()


def test_every_hardware_field_has_a_control_and_every_choice_a_label():
    assert covered_fields() == {field.name for field in dataclasses.fields(Hardware)} - WAVE5_FIELDS_WITHOUT_CONTROL
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
    assert window.manifest.hardware == untouched_hardware(window.manifest)


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
    # Guided: one recommended card with the licence box; the other drivers wait behind "Choose another driver"
    assert tab.recommendation.isVisibleTo(tab) and "GeForce RTX 4090" in tab.recommendation.text() and "open kernel modules" in tab.recommendation.text()
    assert tab.nvidia_license.isVisibleTo(tab) and tab.driver_toggle.isVisibleTo(tab) and not tab.driver_box.isVisibleTo(tab)
    assert tab.gpu_driver.cards["open"].isEnabled() and tab.gpu_driver.value() == "auto"
    offer = nvidia_offer(desktop.manifest, profile_of(desktop.info.hardware))
    assert offer.available and offer.branch == "595" and offer.licence == "NVIDIA-2025" and offer.flavour == "open"
    # no NVIDIA card in this computer: nothing about NVIDIA is shown at all
    radeon = make_window(info=machine("old-bios-sata-radeon"))
    tab = tab_of(radeon)
    assert not tab.recommendation.isVisibleTo(tab) and not tab.driver_toggle.isVisibleTo(tab) and not tab.driver_box.isVisibleTo(tab)
    assert not tab.nvidia_license.isVisibleTo(tab) and not tab.gpu_driver.cards["open"].isVisibleTo(tab)
    assert not nvidia_offer(radeon.manifest, profile_of(radeon.info.hardware)).available
    # an old card that no proprietary branch supports is shown with the reason, and neither NVIDIA card can be picked
    old = machine("asus-desktop-rtx4090")
    old.hardware["gpus"][0]["generation"] = "kepler"
    kepler = make_window(info=old)
    kepler.mode.setCurrentIndex(1)
    tab = tab_of(kepler)
    assert not tab.recommendation.isVisibleTo(tab) and "not supported" in tab.nvidia_reason.text()
    assert not tab.gpu_driver.cards["open"].isEnabled() and not tab.gpu_driver.cards["closed"].isEnabled()
    assert tab.gpu_driver.cards["nouveau"].isEnabled()


def test_maxwell_cards_get_the_long_term_branch(make_window):
    window = make_window(info=machine("intel-rst-hidden-nvme"))
    offer = nvidia_offer(window.manifest, profile_of(window.info.hardware))
    assert offer.available and offer.branch == "580" and "long-term" in offer.label


def test_nvidia_needs_the_licence_checkbox_and_a_distribution_kernel(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    tab = tab_of(window)
    assert tab.nvidia_license.isVisibleTo(tab) and "NVIDIA-2025" in tab.nvidia_license.text() and not tab.nvidia_license.isChecked()
    assert tab.problems() == []                                  # Automatic without the tick is just nouveau
    tab.gpu_driver.set_value("open")                             # choosing the NVIDIA card by hand needs the tick
    assert "licence" in " ".join(tab.problems())
    tab.nvidia_license.setChecked(True)
    assert tab.problems() == []
    window.customize.save_manifest()
    assert (window.manifest.hardware.gpu_driver, window.manifest.hardware.nvidia_modules, window.manifest.hardware.nvidia_license_accepted) == ("nvidia", "open", True)
    assert window.customize.validate_page() == (True, "")
    # switching back to the open driver forgets the licence
    tab.gpu_driver.set_value("nouveau")
    window.customize.save_manifest()
    assert (window.manifest.hardware.gpu_driver, window.manifest.hardware.nvidia_license_accepted) == ("nouveau", False)


def test_ticking_the_licence_under_automatic_is_all_a_newcomer_does(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    tab = tab_of(window)
    assert "once you accept its licence" in " ".join(plan_lines(hardware_page.hardware_plan(tab.working_manifest(), window.info.hardware)))
    tab.nvidia_license.setChecked(True)
    window.customize.save_manifest()
    hardware = window.manifest.hardware
    assert (hardware.gpu_driver, hardware.nvidia_modules, hardware.nvidia_license_accepted) == ("auto", "auto", True)
    plan = hardware_page.hardware_plan(window.manifest, window.info.hardware)
    assert plan.driver_atoms == ["x11-drivers/nvidia-drivers"] and plan.scalars["nvidia_flavour"] == "open"
    assert "NVIDIA proprietary driver 595" in tab.plan_text.text()


def test_nvidia_with_secure_boot_is_not_available_and_says_why(make_window):
    manifest = InstallManifest()
    manifest.system.secure_boot = True
    window = make_window(manifest=manifest, info=machine("asus-desktop-rtx4090"))
    window.mode.setCurrentIndex(1)
    tab = tab_of(window)
    assert not tab.gpu_driver.cards["open"].isEnabled() and not tab.gpu_driver.cards["closed"].isEnabled()
    assert "Secure Boot" in tab.nvidia_reason.text() and "unsigned" not in tab.nvidia_reason.text().replace("no signature", "") or "signature" in tab.nvidia_reason.text()
    assert not tab.recommendation.isVisibleTo(tab) and not tab.nvidia_license.isVisibleTo(tab)


def test_a_loaded_nvidia_choice_that_cannot_work_here_is_refused_not_replaced(make_window):
    manifest = InstallManifest()
    manifest.hardware.gpu_driver = "nvidia"
    manifest.hardware.nvidia_license_accepted = True
    window = make_window(manifest=manifest, info=machine("old-bios-sata-radeon"))
    tab = tab_of(window)
    assert tab.gpu_driver.value() == "open" and tab.driver_box.isVisibleTo(tab)     # a loaded choice is never hidden
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
    window.accounts.validate_page = lambda: (True, "")
    window.next.click()
    assert "Hibernation" in window.customize.problem_label.text()
    assert window.current == 4 and window.customize.tabs.currentWidget() is window.customize.hardware


# --- defaults from the detected machine --------------------------------------------------------------------------------------------------


def test_the_first_inspection_seeds_what_the_machine_suggests(make_window):
    window = make_window(info=machine("qemu-q35-virtio"))
    options, storage = window.manifest.options, window.manifest.storage
    assert options.microcode is False  # the hypervisor owns the microcode
    assert options.bluetooth is False  # no radio in a virtual machine
    assert (storage.swap_choice, storage.swap_mib) == ("recommended", 4096)  # 4 GiB of memory: the Handbook size
    assert window.manifest.hardware == untouched_hardware(window.manifest)  # the hardware choices themselves stay on automatic


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
    assert window.manifest.storage.swap_mib == 4096
    bigger = machine("asus-desktop-rtx4090")
    window.apply_info(bigger)
    assert window.manifest.storage.swap_mib == 16384  # 47 GiB of memory (Handbook: 16 GiB above 32 GiB): the seeding was corrected
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


# --- the applications tab and the findings list say what the hardware decides --------------------------------------------------------------


def test_firmware_that_the_hardware_needs_is_explained_next_to_the_tick_box(make_window):
    manifest = InstallManifest()
    manifest.options.firmware = False
    window = make_window(manifest=manifest, info=machine("amd-laptop-sof"))
    window._show_page(4)
    window.customize.tabs.setCurrentIndex(3)
    note = window.customize.hardware_note
    assert note.isVisibleTo(window.customize) and "Linux firmware is installed even though it is not ticked" in note.text() and "Wi-Fi" in note.text()
    window.manifest.options.firmware = True
    window.customize.load_manifest()
    assert "Linux firmware is installed even though" not in window.customize.hardware_note.text()


def test_microcode_is_explained_in_a_virtual_machine_and_for_a_portable_disk(make_window):
    window = make_window(info=machine("qemu-q35-virtio"))
    window.manifest.options.microcode = True
    window._show_page(4)
    window.customize.refresh_placeholders()
    assert "virtual machine" in window.customize.hardware_note.text()
    portable = InstallManifest()
    portable.hardware.target = "portable"
    window = make_window(manifest=portable, info=machine("asus-desktop-rtx4090"))
    window._show_page(4)
    assert "both Intel and AMD" in window.customize.hardware_note.text()


def test_findings_of_the_service_checks_are_named_for_what_they_are_about():
    from gentoo_installer.pages.readiness import warning_area

    assert warning_area("preflight.disks") == "Storage" and warning_area("preflight.network") == "Network" and warning_area("preflight.something") == "Installer check"
    assert warning_area("hw.st_no_disk") == "Storage" and warning_area("hw.gpu_hybrid") == "Graphics" and warning_area("hw.odd_thing") == "Hardware"


def test_a_detection_that_could_not_read_everything_says_which_parts(make_window):
    info = machine("asus-desktop-rtx4090")
    info.hardware["errors"] = ["disks: lsblk failed", "usb: no permission", "disks: again"]
    window = make_window(info=info)
    row = window.readiness.rows["hardware"]
    assert row.status == "warning" and "could not read: disks, usb" in row.detail.text() and "RTX 4090" in row.detail.text()


def test_a_failed_detection_is_reported_with_its_reason_and_the_wizard_goes_on(make_window):
    from gentoo_installer import system_info

    info = sample_info()
    system_info.merge_inspection(info, {"architecture": "x86_64", "hardware_error": "OSError: sysfs is not readable"})
    window = make_window(info=info)
    window.apply_info(info)
    row = window.readiness.rows["hardware"]
    assert row.status == "warning" and "sysfs is not readable" in row.detail.text() and "Automatic" in row.detail.text()
    assert window.readiness.validate_page() == (True, "")


# --- keyboard only ------------------------------------------------------------------------------------------------------------------------


def test_every_control_of_the_hardware_tab_can_be_reached_with_the_keyboard(make_window):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox

    window = make_window(info=machine("thinkpad-intel-nvidia-hybrid"))
    window.mode.setCurrentIndex(1)
    tab = tab_of(window)
    tab.gpu_driver.set_value("nvidia")  # shows the licence and the version too
    window.show()
    controls = [w for kind in (QComboBox, QAbstractButton, QAbstractSpinBox) for w in tab.findChildren(kind) if w.isVisibleTo(tab) and w.isEnabled()]
    assert len(controls) > 15
    unreachable = [w.accessibleName() or w.objectName() or type(w).__name__ for w in controls if w.focusPolicy() == Qt.FocusPolicy.NoFocus]
    # the cards of a choice group are one tab stop (the arrow keys move inside it): only the selected card has focus
    unreachable = [name for name in unreachable if name not in {card.accessibleName() for card in [*tab.target.cards.values(), *tab.gpu_driver.cards.values()]}]
    assert unreachable == []


def test_the_first_control_of_the_customization_page_is_a_choice_card(make_window):
    window = make_window(info=machine("thinkpad-intel-nvidia-hybrid"))
    window._show_page(4)
    window.show()
    window.customize.focus_first()
    focused = window.focusWidget()
    assert focused is not None and focused is window.customize.hardware.target.cards["this"]


def test_choosing_the_nvidia_driver_never_ticks_the_licence_for_the_user(make_window):
    manifest = InstallManifest()
    manifest.hardware.nvidia_license_accepted = True  # from an old settings file: the driver itself is not chosen there
    window = make_window(manifest=manifest, info=machine("asus-desktop-rtx4090"))
    tab = tab_of(window)
    assert tab.nvidia_license.isChecked()  # shown as loaded
    tab.gpu_driver.set_value("open")
    assert not tab.nvidia_license.isChecked()
    assert any("licence" in problem for problem in tab.problems())


# --- the summary table of the review ----------------------------------------------------------------------------------------------------------


def summary_rows(window) -> dict[str, str]:
    return {label: value for _, label, value in window.review.rows()}


def test_the_review_says_what_encryption_covers(make_window):
    manifest = InstallManifest()
    manifest.storage.luks = True
    manifest.storage.luks_passphrase = "long-enough-passphrase"
    window = make_window(manifest=manifest)
    window._show_page(6)
    assert summary_rows(window)["Encryption"] == manifest.storage.encryption_summary() != "LUKS enabled"
    assert "swap is never stored unencrypted" in summary_rows(window)["Encryption"]


def test_the_review_names_the_target_and_the_graphics_driver(make_window):
    manifest = InstallManifest()
    manifest.hardware.gpu_driver, manifest.hardware.nvidia_license_accepted = "nvidia", True
    window = make_window(manifest=manifest, info=machine("asus-desktop-rtx4090"))
    window._show_page(6)
    rows = summary_rows(window)
    assert rows["Installed for"] == "This computer" and rows["Graphics driver"] == "NVIDIA proprietary driver 595"
    window.manifest.hardware.target = "portable"
    window.manifest.hardware.gpu_driver, window.manifest.hardware.nvidia_license_accepted = "auto", False
    window._show_page(5)
    window._show_page(6)
    rows = summary_rows(window)
    assert rows["Installed for"].startswith("A portable disk") and rows["Graphics driver"] == "Open-source driver"
    unfit = make_window(manifest=manifest, info=machine("old-bios-sata-radeon"))
    unfit.manifest.hardware.target = "this"
    unfit.manifest.hardware.gpu_driver = "nvidia"
    unfit._show_page(6)
    assert "does not fit" in summary_rows(unfit)["Graphics driver"]
    plain = make_window()
    plain._show_page(6)
    assert summary_rows(plain)["Graphics driver"] == "Open-source driver"



# --- G01: the NVIDIA driver cards ----------------------------------------------------------------------------------------------------------


def card_texts(tab: HardwareTab) -> dict[str, tuple[bool, bool, str]]:
    """key -> (shown, enabled, title) of the driver cards."""
    return {key: (card.isVisibleTo(tab), card.isEnabled(), card.title_label.text()) for key, card in tab.gpu_driver.cards.items()}


def test_guided_shows_one_recommended_card_and_the_expander_opens_the_rest(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    tab = tab_of(window)
    assert tab.recommendation.text().startswith("Recommended for NVIDIA GeForce RTX 4090: the NVIDIA driver (version 595) with open kernel modules")
    assert not tab.driver_box.isVisibleTo(tab) and not tab.nvidia_branch.isVisibleTo(tab) and not tab.extras_box.isVisibleTo(tab)
    tab.driver_toggle.setChecked(True)
    shown = card_texts(tab)
    assert [key for key, (visible, _enabled, _title) in shown.items() if visible] == ["auto", "open", "closed", "nouveau", "basic"]
    assert all(enabled for _visible, enabled, _title in shown.values())
    assert shown["closed"][2] == "NVIDIA, closed kernel modules (version 580)"


def test_a_pascal_card_gets_the_closed_modules_and_the_open_ones_are_refused(make_window):
    window = make_window(info=machine("intel-rst-hidden-nvme"))
    tab = tab_of(window)
    assert "driver 580" in tab.recommendation.text() and "closed kernel modules" in tab.recommendation.text() and "last NVIDIA driver" in tab.recommendation.text()
    tab.driver_toggle.setChecked(True)
    shown = card_texts(tab)
    assert shown["closed"][1] and not shown["open"][1]
    assert "open kernel modules need" in tab.gpu_driver.cards["open"].toolTip()


def test_a_blackwell_card_only_has_the_open_modules(make_window):
    info = machine("asus-desktop-rtx4090")
    info.hardware["gpus"][0]["generation"] = "blackwell"
    tab = tab_of(make_window(info=info))
    tab.driver_toggle.setChecked(True)
    shown = card_texts(tab)
    assert shown["open"][1] and not shown["closed"][1]
    assert "Blackwell" in tab.gpu_driver.cards["closed"].toolTip() and "open" in tab.gpu_driver.cards["closed"].toolTip()


def test_a_kepler_card_is_left_to_nouveau_in_every_view(make_window):
    info = machine("asus-desktop-rtx4090")
    info.hardware["gpus"][0]["generation"] = "kepler"
    for index in (0, 1):
        window = make_window(info=info)
        window.mode.setCurrentIndex(index)
        tab = tab_of(window)
        shown = card_texts(tab)
        assert not shown["open"][1] and not shown["closed"][1] and shown["nouveau"][1] and shown["auto"][1]
        assert "too old" in tab.gpu_driver.cards["open"].toolTip() or "not supported" in tab.gpu_driver.cards["open"].toolTip()


def test_advanced_lists_every_driver_and_greys_the_branches_that_are_gone(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    window.mode.setCurrentIndex(1)
    tab = tab_of(window)
    assert tab.driver_box.isVisibleTo(tab) and not tab.driver_toggle.isVisibleTo(tab)
    assert [key for key, (visible, *_rest) in card_texts(tab).items() if visible] == ["auto", "open", "closed", "nouveau", "basic"]
    labels = {tab.nvidia_branch.itemData(i): (tab.nvidia_branch.itemText(i), tab.nvidia_branch.model().item(i).isEnabled()) for i in range(tab.nvidia_branch.count())}
    assert set(labels) == {"auto", "595", "580", "470", "390"}                  # 535 is not listed
    assert labels["595"][1] and labels["580"][1] and not labels["470"][1] and not labels["390"][1]
    assert "not offered" in labels["470"][0]


def test_basic_display_is_a_card_and_sway_cannot_use_it(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    window.mode.setCurrentIndex(1)
    tab = tab_of(window)
    tab.gpu_driver.set_value("basic")
    window.customize.save_manifest()
    assert window.manifest.hardware.gpu_driver == "basic" and not window.manifest.hardware.nvidia_license_accepted
    assert "Basic display" in tab.plan_text.text() or "basic display" in tab.plan_text.text().lower()
    window.manifest.system.desktop = "sway"
    tab.refresh()
    assert not tab.gpu_driver.cards["basic"].isEnabled() or tab.gpu_driver.value() == "basic"


def test_the_addons_and_the_second_licence_are_advanced_and_cuda_needs_its_tick(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    tab = tab_of(window)
    assert not tab.extras_box.isVisibleTo(tab)
    window.mode.setCurrentIndex(1)
    assert tab.extras_box.isVisibleTo(tab) and not tab.cuda_license.isVisibleTo(tab)
    tab.extra_checks["cuda"].setChecked(True)
    tab.extra_checks["nvtop"].setChecked(True)
    assert tab.cuda_license.isVisibleTo(tab)
    assert any("CUDA" in problem for problem in tab.problems())                    # impossible without the second tick
    tab.cuda_license.setChecked(True)
    window.customize.save_manifest()
    hardware = window.manifest.hardware
    assert hardware.nvidia_extras == ["cuda", "nvtop"] and hardware.nvidia_cuda_license_accepted


def test_the_wave5_power_and_boot_choices_have_controls_that_write_what_was_picked(make_window):
    window = make_window(info=machine("thinkpad-amd-laptop"))
    window.mode.setCurrentIndex(1)
    tab = tab_of(window)
    pick(combo(tab, "battery_charge_limit"), "80")
    pick(combo(tab, "lid_action"), "ignore")
    pick(combo(tab, "signed_modules"), "off")
    window.customize.save_manifest()
    hardware = window.manifest.hardware
    assert (hardware.battery_charge_limit, hardware.lid_action, hardware.signed_modules) == (80, "ignore", "off")
    assert tab.row_shown("battery_charge_limit") and tab.row_shown("lid_action") and not tab.row_shown("nvidia_rtd3") and not tab.row_shown("vendor_workarounds")
    desktop = tab_of(make_window(info=machine("asus-desktop-rtx4090")))
    assert not desktop.row_shown("battery_charge_limit") and not desktop.row_shown("lid_action")


def test_the_safe_graphics_and_power_saving_rows_show_only_with_the_nvidia_driver(make_window):
    manifest = InstallManifest()
    manifest.hardware.nvidia_license_accepted = True
    window = make_window(manifest=manifest, info=machine("asus-desktop-rtx4090"))
    window.mode.setCurrentIndex(1)
    tab = tab_of(window)
    assert tab.row_shown("safe_graphics_entry") and not tab.row_shown("nvidia_rtd3") and not tab.row_shown("nvk")
    other = tab_of(make_window(info=machine("asus-desktop-rtx4090")))
    assert not other.row_shown("safe_graphics_entry") and other.row_shown("nvk") or not other.advanced


# --- G02: the Devices card ----------------------------------------------------------------------------------------------------------------


def test_the_users_pc_shows_razer_openrgb_and_controller_lines(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    tab = tab_of(window)
    card = tab.devices
    assert card.isVisibleTo(tab) and card.visible_keys() == ["openrazer", "openrgb", "game_rules"]
    assert card.lines["openrazer"].check.isChecked() and not card.lines["openrgb"].check.isChecked() and not card.lines["game_rules"].check.isChecked()
    assert "Razer" in card.lines["openrazer"].sentence.text() and "built on this PC" in card.lines["openrazer"].detail.text()
    assert "testing" in card.lines["game_rules"].detail.text().lower() or "one tested version" in card.lines["game_rules"].detail.text()
    assert not card.lines["openrazer"].detail.text().startswith("Found:")          # the evidence is for Advanced


def test_a_plain_virtual_machine_shows_no_devices_card(make_window):
    window = make_window(info=machine("qemu-q35-virtio"))
    tab = tab_of(window)
    assert not tab.devices.isVisibleTo(tab) and tab.devices.visible_keys() == []
    assert not tab_of(make_window()).devices.isVisibleTo(tab)                      # nothing detected at all


def test_guided_shows_at_most_five_lines_and_advanced_all_with_what_was_found(make_window):
    window = make_window(info=machine("arc-ultra-convertible"))
    tab = tab_of(window)
    assert len(tab.devices.visible_keys()) <= 5
    window.mode.setCurrentIndex(1)
    keys = tab.devices.visible_keys()
    assert "wacom" in keys and tab.devices.lines["wacom"].detail.text().startswith("Found:")
    info = machine("thinkpad-amd-laptop")
    info.hardware["peripherals"]["usb_devices"] = [{"vendor_id": "1532", "product_id": "0091", "role": "razer"}]
    thinkpad = make_window(info=info)
    thinkpad.mode.setCurrentIndex(0)
    guided = tab_of(thinkpad)
    assert "thinkfan" not in guided.devices.visible_keys() and guided.devices.more.isVisibleTo(guided)
    thinkpad.mode.setCurrentIndex(1)
    assert "thinkfan" in guided.devices.visible_keys() and not guided.devices.more.isVisibleTo(guided)


def test_the_asus_laptop_card_says_plainly_where_the_tools_come_from(make_window):
    window = make_window(info=machine("asus-rog-laptop"))
    window.mode.setCurrentIndex(1)
    tab = tab_of(window)
    line = tab.devices.lines["asus_tools"]
    assert not line.check.isChecked()
    assert "unofficial" in line.detail.text() and "GURU" in line.detail.text() and "edorp" in line.detail.text()
    assert "supergfxctl" in line.sentence.text()                                     # a hybrid laptop


def test_only_the_lines_the_user_clicked_are_written_to_the_manifest(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    tab = tab_of(window)
    window.customize.save_manifest()
    assert window.manifest.hardware.devices == {}                                    # looking changes nothing
    tab.devices.lines["openrgb"].check.setChecked(True)
    tab.devices.lines["openrazer"].check.setChecked(False)
    window.customize.save_manifest()
    assert window.manifest.hardware.devices == {"openrgb": True, "openrazer": False}
    again = tab_of(window)
    assert again.devices.lines["openrgb"].check.isChecked() and not again.devices.lines["openrazer"].check.isChecked()
    assert window.manifest.validate_sections()["hardware"] == []


def test_a_device_the_secure_boot_setting_would_refuse_is_greyed_with_a_sentence(make_window):
    manifest = InstallManifest()
    manifest.system.secure_boot = True
    window = make_window(manifest=manifest, info=machine("asus-desktop-rtx4090"))
    line = tab_of(window).devices.lines["openrazer"]
    assert not line.check.isEnabled() and not line.check.isChecked() and "unsigned" in line.sentence.text()


def test_the_devices_the_user_chose_reach_the_plan(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    tab = tab_of(window)
    tab.devices.lines["openrgb"].check.setChecked(True)
    window.customize.save_manifest()
    plan = hardware_page.hardware_plan(window.manifest, window.info.hardware)
    assert "app-misc/openrgb" in plan.optional_atoms and "sys-apps/openrazer" in plan.optional_atoms and {"plugdev", "i2c"} <= set(plan.groups)


def test_the_licence_box_and_the_saved_licence_agree_after_leaving_and_returning_to_automatic(make_window):
    manifest = InstallManifest()
    manifest.hardware.nvidia_license_accepted = True
    window = make_window(info=machine("asus-desktop-rtx4090"), manifest=manifest)
    tab = tab_of(window)
    tab.gpu_driver.set_value("nouveau")
    tab.gpu_driver.set_value("auto")
    window.customize.save_manifest()
    assert tab.nvidia_license.isChecked() is False
    assert window.manifest.hardware.nvidia_license_accepted is False
