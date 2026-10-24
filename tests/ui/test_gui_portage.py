"""The Portage tab: Guided choices, the grouped More options, and what each control writes to the manifest (PORT-02 to PORT-13)."""

from __future__ import annotations

import dataclasses
import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox

from gentoo_installer import mirrors, licenses
from gentoo_installer.manifest import InstallManifest, Portage
from gentoo_installer.pages import customize, portage_tab
from gentoo_installerd.configure import Resources
from tests.backend.target_helpers import configurer, target_manifest
from tests.hardware.helpers import FIXTURE_NAMES
from tests.ui.test_gui_hardware import machine


def page_of(window):
    window._show_page(4)
    window.customize.tabs.setCurrentIndex(1)
    return window.customize


def reload(window, **portage):
    for name, value in portage.items():
        setattr(window.manifest.portage, name, value)
    window.customize.load_manifest()
    return window.customize


# --- PORT-02: three Guided questions, grouped More options ---------------------------------------------------------------


def test_the_portage_tab_is_still_the_second_tab(make_window):
    window = make_window()
    assert [window.customize.tabs.tabText(i) for i in range(4)] == ["Hardware", "Portage", "Network", "Applications && tools"]


def test_a_visit_changes_nothing_in_the_manifest(make_window):
    window = make_window()
    before = dataclasses.replace(window.manifest.portage), window.manifest.system.world_update
    page = page_of(window)
    for _ in range(2):
        page.save_manifest()
        window._show_page(3)
        window._show_page(4)
    assert (window.manifest.portage, window.manifest.system.world_update) == before
    assert window.manifest.portage == Portage()


def test_guided_shows_the_few_questions_and_hides_the_rest(make_window):
    window = make_window()
    window.mode.setCurrentIndex(0)
    page = page_of(window)
    visible = lambda widget: widget.isVisibleTo(page)  # noqa: E731
    for widget in (page.optimize, page.binhost, page.source_kind, page.license_free, page.license_redistributable, page.jobs_summary):
        assert visible(widget), widget
    assert not page.portage_advanced.is_open()
    for widget in (page.cflags, page.emerge_opts, page.features, page.world_update, page.use_table, page.video_cards):
        assert not visible(widget), widget


def test_advanced_groups_the_rest_under_four_headings_and_nothing_is_lost(make_window):
    window = make_window()
    page = page_of(window)
    window.mode.setCurrentIndex(1)
    assert page.portage_advanced.is_open()
    titles = [box.title() for box in (page.speed_box, page.packages_box, page.licences_box, page.advanced_box)]
    assert titles == ["Speed", "Packages", "Licences", "Advanced"]
    assert page.speed_box.isAncestorOf(page.cflags) and page.packages_box.isAncestorOf(page.use_flags)
    assert page.licences_box.isAncestorOf(page.licenses)
    # EMERGE_DEFAULT_OPTS and FEATURES only in the last group; CXXFLAGS has no visible control
    assert page.advanced_box.isAncestorOf(page.emerge_opts) and page.advanced_box.isAncestorOf(page.features)
    assert not page.cxxflags.isVisibleTo(page)
    page.cflags.setText("-O2 -pipe -g0")
    window.mode.setCurrentIndex(0)
    window.mode.setCurrentIndex(1)
    page.save_manifest()
    assert window.manifest.portage.cflags == "-O2 -pipe -g0"


def test_the_variable_name_is_in_the_tooltip_of_each_advanced_control(make_window):
    page = page_of(make_window())
    for widget, name in ((page.cflags, "COMMON_FLAGS"), (page.rustflags, "RUSTFLAGS"), (page.cpu_flags, "CPU_FLAGS_X86"),
                         (page.licenses, "ACCEPT_LICENSE"), (page.jobs, "MAKEOPTS")):
        assert name in widget.toolTip(), name


def test_the_tab_explains_make_conf(make_window):
    page = page_of(make_window())
    texts = " ".join(label.text() for label in page.tabs.widget(1).findChildren(portage_tab.QLabel))
    assert "make.conf is the main file that tells Portage how to build programs." in texts


def test_a_risky_compiler_flag_shows_the_handbook_warning(make_window):
    page = page_of(make_window())
    window = page.window()
    window.mode.setCurrentIndex(1)
    assert not page.flags_warning.isVisibleTo(page)
    page.cflags.setText("-O3 -pipe")
    assert page.flags_warning.isVisibleTo(page) and "-O3" in page.flags_warning.text()
    page.cflags.setText("-O2 -pipe -fomit-frame-pointer")
    assert "-fomit-frame-pointer" in page.flags_warning.text()
    page.cflags.setText("-O2 -pipe")
    assert not page.flags_warning.isVisibleTo(page)


def test_cxxflags_from_a_settings_file_survive(make_window):
    window = make_window()
    page = reload(window, cxxflags="-O2 -fno-exceptions")
    page.save_manifest()
    assert window.manifest.portage.cxxflags == "-O2 -fno-exceptions"


# --- PORT-03/04: optimisation and ready-made packages ---------------------------------------------------------------------


def test_only_generic_and_native_are_offered_never_an_optimisation_level(make_window):
    page = page_of(make_window())
    assert [page.optimize.itemData(i) for i in range(page.optimize.count())] == ["generic", "native"]
    assert "any PC" in page.optimize.itemText(0) and "this CPU" in page.optimize.itemText(1)


def test_native_is_saved_and_explained(make_window):
    window = make_window()
    page = page_of(window)
    page.optimize.setCurrentIndex(1)
    page.save_manifest()
    assert window.manifest.portage.optimize == "native"
    assert page.optimize_note.isVisibleTo(page) and "older PC" in page.optimize_note.text()


def test_a_portable_target_disables_native_with_an_explanation(make_window):
    manifest = InstallManifest()
    manifest.hardware.target = "portable"
    window = make_window(manifest=manifest)
    page = page_of(window)
    model = page.optimize.model()
    assert not model.item(1).isEnabled() and model.item(0).isEnabled()
    assert "portable" in page.optimize_note.text().lower()
    page.optimize.setCurrentIndex(1)  # forced (a settings file could do it): the problem is reported, not rendered
    ok, message = page.validate_page()
    assert not ok and "portable" in message


def test_the_binhost_checkbox_is_on_by_default_with_its_help(make_window):
    page = page_of(make_window())
    assert page.binhost.isChecked() and page.binhost.text() == "Download ready-made packages (much faster)"
    assert "Gentoo offers pre-built packages" in page.binhost.toolTip() and "NVIDIA" in page.binhost.toolTip()
    page.binhost.setChecked(False)
    page.save_manifest()
    assert not page.manifest.portage.binhost


def test_the_packages_variant_is_chosen_in_advanced_and_shows_the_detected_choice(make_window):
    page = page_of(make_window(info=machine("asus-desktop-rtx4090")))
    assert [page.binhost_variant.itemData(i) for i in range(3)] == ["auto", "generic", "v3"]
    assert "Automatic (now:" in page.binhost_variant.itemText(0)
    page.binhost_variant.setCurrentIndex(2)
    page.save_manifest()
    assert page.manifest.portage.binhost_variant == "v3"


# --- PORT-05: licences -----------------------------------------------------------------------------------------------------


def test_the_untouched_default_reads_as_free_software_and_is_not_rewritten(make_window):
    window = make_window()
    page = page_of(window)
    assert page.license_free.isChecked() and not page.license_redistributable.isChecked()
    page.save_manifest()
    assert window.manifest.portage.accept_license == "@FREE" and window.manifest.portage.license_groups == []


def test_the_two_radio_choices_write_the_documented_lines(make_window):
    window = make_window()
    page = page_of(window)
    page.license_redistributable.setChecked(True)
    page.save_manifest()
    assert window.manifest.portage.accept_license == "-* @FREE @BINARY-REDISTRIBUTABLE"
    assert window.manifest.portage.license_groups == ["@FREE", "@BINARY-REDISTRIBUTABLE"]
    assert page.license_checks["@BINARY-REDISTRIBUTABLE"].isChecked() and not page.license_checks["@EULA"].isChecked()
    page.license_free.setChecked(True)
    page.save_manifest()
    assert window.manifest.portage.accept_license == "-* @FREE" and window.manifest.portage.license_groups == ["@FREE"]
    assert page.license_free.isChecked() and not page.license_redistributable.isChecked()


def test_group_checkboxes_and_the_custom_state(make_window):
    window = make_window()
    page = page_of(window)
    page.license_checks["@FREE"].setChecked(False)
    page.license_checks["@EULA"].setChecked(True)
    assert page.licenses.text() == "-* @EULA"
    assert not page.license_free.isChecked() and not page.license_redistributable.isChecked()
    assert page.license_custom_note.isVisibleTo(page)
    page.license_checks["@FREE"].setChecked(True)
    page.license_checks["@EULA"].setChecked(False)
    assert page.licenses.text() == "-* @FREE" and page.license_free.isChecked()


def test_a_licence_for_one_package_is_saved_and_checked(make_window):
    window = make_window()
    page = page_of(window)
    page.package_license.setPlainText("games-util/steam-launcher ValveSteamLicense")
    page.save_manifest()
    assert window.manifest.portage.package_license == {"games-util/steam-launcher": ["ValveSteamLicense"]}
    page.package_license.setPlainText("not an atom X")
    assert page.validate_page()[0] is False


def test_settings_that_only_list_groups_show_them(make_window):
    window = make_window()
    page = reload(window, license_groups=["@FREE", "@BINARY-REDISTRIBUTABLE"])
    assert page.licenses.text() == "-* @FREE @BINARY-REDISTRIBUTABLE" and page.license_redistributable.isChecked()
    page.save_manifest()
    assert window.manifest.portage.accept_license == "@FREE"  # untouched


# --- PORT-06: download sources --------------------------------------------------------------------------------------------


def test_automatic_is_the_default_source(make_window):
    window = make_window()
    page = page_of(window)
    assert page.source_kind.currentData() == "automatic"
    assert not page.mirror_list.isVisibleTo(page) and not page.mirror.isVisibleTo(page)
    page.save_manifest()
    assert (window.manifest.portage.mirror, window.manifest.portage.mirrors) == ("https://distfiles.gentoo.org", [])


def test_a_regional_choice_is_ordered_and_written_to_the_manifest(make_window):
    window = make_window()
    page = page_of(window)
    page.source_kind.setCurrentIndex(page.source_kind.findData("country"))
    assert page.mirror_list.isVisibleTo(page) and page.mirror_list.count() == len(mirrors.MIRRORS)
    assert page.validate_page() == (False, "Tick at least one mirror, or choose Automatic.")
    first, second = 3, 9
    for row in (first, second):
        page.mirror_list.item(row).setCheckState(Qt.CheckState.Checked)
    chosen = page.checked_mirrors()
    assert len(chosen) == 2
    page.mirror_list.setCurrentRow(second)
    page.mirror_up.click()  # move the second chosen one up by one
    page.save_manifest()
    assert window.manifest.portage.mirrors == page.checked_mirrors() and window.manifest.portage.mirror == window.manifest.portage.mirrors[0]
    assert all(url.startswith("https://") for url in window.manifest.portage.mirrors)
    # loading it again shows the same order
    page.load_manifest()
    assert page.source_kind.currentData() == "country" and page.checked_mirrors() == window.manifest.portage.mirrors


def test_moving_a_mirror_changes_the_order_that_is_written(make_window):
    window = make_window()
    page = page_of(window)
    page.source_kind.setCurrentIndex(page.source_kind.findData("country"))
    for row in (0, 1):
        page.mirror_list.item(row).setCheckState(Qt.CheckState.Checked)
    before = page.checked_mirrors()
    page.mirror_list.setCurrentRow(0)
    page.mirror_down.click()
    assert page.checked_mirrors() == list(reversed(before))


def test_a_custom_address_must_be_https(make_window):
    window = make_window()
    page = page_of(window)
    page.source_kind.setCurrentIndex(page.source_kind.findData("custom"))
    assert page.mirror.isVisibleTo(page)
    page.mirror.setText("http://mirror.example/gentoo")
    ok, message = page.validate_page()
    assert not ok and "https://" in message and "http://" in message
    page.mirror.setText("https://mirror.example/gentoo/")
    assert page.validate_page() == (True, "")
    assert window.manifest.portage.mirror == "https://mirror.example/gentoo" and window.manifest.portage.mirrors == []
    page.mirror.clear()
    assert page.validate_page()[0] is False


def test_an_old_settings_file_with_a_mirror_shows_it_as_custom(make_window):
    window = make_window()
    page = reload(window, mirror="https://mirror.example/gentoo")
    assert page.source_kind.currentData() == "custom" and page.mirror.text() == "https://mirror.example/gentoo"
    page = reload(window, mirror="https://distfiles.gentoo.org", mirrors=["https://example.invalid/gentoo", mirrors.MIRRORS[2].url])
    assert page.source_kind.currentData() == "country"
    assert page.checked_mirrors() == ["https://example.invalid/gentoo", mirrors.MIRRORS[2].url]  # an unknown one is kept, first


# --- PORT-07: the package tree ---------------------------------------------------------------------------------------------


def test_the_web_snapshot_is_the_default_and_rsync_is_a_choice(make_window):
    window = make_window()
    page = page_of(window)
    assert page.sync_snapshot.isChecked() and not page.rsync_mirror.isEnabled()
    page.sync_rsync.setChecked(True)
    assert page.rsync_mirror.isEnabled()
    page.save_manifest()
    assert (window.manifest.portage.rsync_mode, window.manifest.portage.rsync_mirror) == ("sync", "")
    page.rsync_mirror.setText("rsync://rsync.example/gentoo-portage")
    page.save_manifest()
    assert window.manifest.portage.rsync_mirror == "rsync://rsync.example/gentoo-portage"
    page.sync_snapshot.setChecked(True)
    page.save_manifest()
    assert (window.manifest.portage.rsync_mode, window.manifest.portage.rsync_mirror) == ("snapshot", "")


def test_an_https_rsync_server_is_refused(make_window):
    page = page_of(make_window())
    page.sync_rsync.setChecked(True)
    page.rsync_mirror.setText("https://git.example/gentoo.git")
    ok, message = page.validate_page()
    assert not ok and "rsync://" in message


# --- PORT-09: the USE flag browser -----------------------------------------------------------------------------------------


def test_the_use_browser_lists_popular_flags_and_searches(make_window):
    page = page_of(make_window())
    names = [page.use_table.item(row, 0).text() for row in range(page.use_table.rowCount())]
    assert "kde" in names and "wayland" in names
    page.use_search.setText("dvd")
    names = [page.use_table.item(row, 0).text() for row in range(page.use_table.rowCount())]
    assert names[0] == "dvd" and all("dvd" in name.lower() or "dvd" in page.use_table.item(row, 1).text().lower() for row, name in enumerate(names))


def pick_state(page, flag: str, state: str) -> None:
    for row in range(page.use_table.rowCount()):
        if page.use_table.item(row, 0).text() == flag:
            combo = page.use_table.cellWidget(row, 2)
            assert isinstance(combo, QComboBox)
            combo.setCurrentIndex(combo.findData(state))
            return
    raise AssertionError(flag)


def test_on_off_and_default_map_to_use_tokens(make_window):
    window = make_window()
    page = page_of(window)
    page.use_search.setText("gtk")
    pick_state(page, "gtk", "off")
    page.use_search.setText("kde")
    pick_state(page, "kde", "on")
    page.save_manifest()
    assert window.manifest.portage.use_flags == ["-gtk", "kde"]
    page.use_search.setText("gtk")
    pick_state(page, "gtk", "default")
    page.save_manifest()
    assert window.manifest.portage.use_flags == ["kde"]
    page.use_search.setText("")
    assert page.use_table.cellWidget([page.use_table.item(r, 0).text() for r in range(page.use_table.rowCount())].index("kde"), 2).currentData() == "on"


def test_the_expert_field_and_the_browser_stay_in_step(make_window):
    window = make_window()
    page = reload(window, use_flags=["-gtk", "alsa"])
    names = {page.use_table.item(r, 0).text(): page.use_table.cellWidget(r, 2).currentData() for r in range(page.use_table.rowCount())}
    assert names["gtk"] == "off" and names["alsa"] == "on"
    page.use_flags.setPlainText("-gtk\nalsa\nkde")
    assert {page.use_table.item(r, 0).text(): page.use_table.cellWidget(r, 2).currentData() for r in range(page.use_table.rowCount())}["kde"] == "on"


def test_a_custom_flag_is_added_and_a_bad_one_is_explained(make_window):
    window = make_window()
    page = page_of(window)
    window.mode.setCurrentIndex(1)
    page.use_custom.setText("-*")
    page.use_add.click()
    assert page.use_error.isVisibleTo(page) and "switch off every default feature" in page.use_error.text()
    assert window.manifest.portage.use_flags == [] and page.use_flags.toPlainText() == ""
    page.use_custom.setText("my-flag")
    page.use_add.click()
    page.save_manifest()
    assert window.manifest.portage.use_flags == ["my-flag"]
    assert not page.use_error.isVisibleTo(page) or page.use_error.text() == ""


def test_star_typed_in_the_expert_field_is_refused_with_the_handbooks_reason(make_window):
    page = page_of(make_window())
    page.use_flags.setPlainText("-* kde")
    ok, message = page.validate_page()
    assert not ok and "switch off every default feature" in message and "Handbook" in message


# --- PORT-10: MAKEOPTS ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("cpus", "memory_gib", "swap_mib"), [(32, 16, 0), (4, 4, 0), (2, 2, 0), (24, 48, 0), (16, 8, 8192), (64, 256, 0)])
def test_the_displayed_jobs_equal_what_the_daemon_installs(make_window, monkeypatch, cpus, memory_gib, swap_mib, tmp_path):
    monkeypatch.setattr(customize.os, "cpu_count", lambda: cpus)
    monkeypatch.setattr(customize.system_info, "_memory_gib", lambda: float(memory_gib))
    window = make_window()
    window.manifest.storage.swap_mib = swap_mib
    page = page_of(window)
    page.load_manifest()
    daemon = configurer(tmp_path)
    daemon.resources = lambda: Resources(cpus, memory_gib * 1024**3, memory_gib * 1024**3)
    manifest = target_manifest(storage={"swap_mib": swap_mib})
    effective = daemon.effective_make_opts(manifest)
    jobs = int(effective.split()[0][2:])
    assert f"Using {jobs} build jobs" in page.jobs_summary.text()
    assert f"your PC has {cpus} threads and {memory_gib} GiB RAM" in page.jobs_summary.text()
    assert portage_tab.auto_make_opts(swap_mib) == effective


def test_the_load_limit_has_its_own_control_and_is_preserved(make_window):
    window = make_window()
    page = reload(window, make_opts="-j6 -l8")
    assert not page.jobs_auto.isChecked() and (page.jobs.value(), page.load_limit.value()) == (6, 8)
    page.save_manifest()
    assert window.manifest.portage.make_opts == "-j6 -l8"
    page.load_limit.setValue(0)
    page.save_manifest()
    assert window.manifest.portage.make_opts == "-j6"
    page.load_limit.setValue(12)
    page.jobs.setValue(4)
    page.save_manifest()
    assert window.manifest.portage.make_opts == "-j4 -l12"
    page.jobs_auto.setChecked(True)
    page.save_manifest()
    assert window.manifest.portage.make_opts == "" and not page.jobs.isEnabled() and not page.load_limit.isEnabled()


def test_the_help_for_jobs_mentions_memory(make_window):
    page = page_of(make_window())
    notes = [note.note_text for note in page.tabs.widget(1).findChildren(portage_tab.HelpNote)]
    assert any("2 GiB of memory" in text for text in notes)


# --- PORT-11: the world update moved here -------------------------------------------------------------------------------------


def test_the_system_page_no_longer_has_the_world_update_checkbox(make_window):
    window = make_window()
    assert not hasattr(window.system, "world_update")


def test_the_world_update_policy_and_depclean_live_in_the_portage_tab(make_window):
    window = make_window()
    page = page_of(window)
    assert [page.world_update.itemData(i) for i in range(3)] == ["auto", "now", "later"]
    assert page.world_update.currentData() == "later" and not page.depclean.isChecked()  # the manifest's default stays
    page.world_update.setCurrentIndex(page.world_update.findData("auto"))
    page.depclean.setChecked(True)
    page.save_manifest()
    assert window.manifest.system.world_update == "auto" and window.manifest.portage.depclean
    assert page.advanced_box.isAncestorOf(page.world_update)
    notes = " ".join(note.note_text for note in page.tabs.widget(1).findChildren(portage_tab.HelpNote))
    assert "skipping it is fine" in notes or "skipping it is fine" in page.world_update.parentWidget().findChild(portage_tab.HelpNote).note_text


def test_a_world_update_from_a_settings_file_is_shown(make_window):
    window = make_window()
    window.manifest.system.world_update = "now"
    page = reload(window)
    assert page.world_update.currentData() == "now"
    page.save_manifest()
    assert window.manifest.system.world_update == "now"


# --- PORT-13: VIDEO_CARDS ---------------------------------------------------------------------------------------------------

EXPECTED = {  # what the hardware plan chooses for each recorded machine, as tokens the checklist knows
    "asus-desktop-rtx4090": None, "qemu-q35-virtio": None,
}


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_the_checklist_is_preselected_from_detection(make_window, name):
    window = make_window(info=machine(name))
    page = page_of(window)
    detected = page.detected_video_cards()
    assert page.checked_video_cards() == [card for card in page.video_boxes if card in detected]
    assert set(detected) <= set(page.video_boxes)
    page.save_manifest()
    assert window.manifest.portage.video_cards == ""  # looking at the detected choice writes nothing


def test_an_amd_machine_preselects_its_driver():
    from gentoo_installer.pages.hardware import hardware_plan
    from tests.hardware.helpers import fixture_dict

    manifest = InstallManifest()
    manifest.portage.binhost = False
    plan = hardware_plan(manifest, fixture_dict("amd-laptop-sof"))
    assert plan is not None and any(card in plan.make_conf.get("VIDEO_CARDS", "") for card in ("amdgpu", "radeonsi"))


def test_ticking_a_driver_writes_it_and_an_unknown_name_is_rejected(make_window):
    window = make_window()
    page = page_of(window)
    page.video_boxes["amdgpu"].setChecked(True)
    page.video_boxes["radeonsi"].setChecked(True)
    assert page.video_cards.text() == "amdgpu radeonsi"
    page.save_manifest()
    assert window.manifest.portage.video_cards == "amdgpu radeonsi"
    page.video_cards.setText("amdgpu nvidiaa")
    ok, message = page.validate_page()
    assert not ok and "nvidiaa" in message
    page.video_cards.setText("nvidia nvk")
    assert page.checked_video_cards() == ["nvidia", "nvk"]
    assert page.validate_page() == (True, "")


# --- every Portage field has a control ----------------------------------------------------------------------------------------


def test_every_portage_field_has_a_control_that_round_trips(make_window):
    window = make_window()
    page = page_of(window)
    values = dict(optimize="native", binhost=False, binhost_url="https://example.org/packages/", binhost_variant="generic", mirror="https://mirror.example/gentoo",
                  rsync_mode="sync", rsync_mirror="rsync://rsync.example/gentoo-portage", make_opts="-j3 -l5", cflags="-O2 -pipe -g0", rustflags="-C debuginfo=0",
                  emerge_opts="--ask=n", features=["ccache"], accept_license="-* @FREE @EULA", license_groups=["@FREE", "@EULA"],
                  use_flags=["-gtk", "kde"], package_use={"app-editors/vim": ["python"]}, cpu_flags="aes avx2", video_cards="amdgpu radeonsi",
                  depclean=True, package_license={"x/y": ["Z"]})
    window.manifest.system.world_update = "now"
    reload(window, **values)
    page.save_manifest()
    saved = window.manifest.portage
    for name, value in values.items():
        assert getattr(saved, name) == value, name
    assert window.manifest.validate_sections()["portage"] == [], window.manifest.validate_sections()["portage"]
    # fields with a list of mirrors
    reload(window, mirrors=[mirrors.MIRRORS[0].url, mirrors.MIRRORS[4].url], mirror=mirrors.MIRRORS[0].url)
    page.save_manifest()
    assert saved.mirrors == [mirrors.MIRRORS[0].url, mirrors.MIRRORS[4].url]
    assert {field.name for field in dataclasses.fields(Portage)} - set(values) - {"mirrors", "mirror_fallbacks", "profile_override", "cxxflags"} == set()  # no visible control: kept as loaded


def test_fields_without_a_control_keep_their_values(make_window):
    window = make_window()
    reload(window, profile_override="default/linux/amd64/23.0/desktop/gnome", mirror_fallbacks=["https://mirror.example/gentoo"])
    page = window.customize
    page.save_manifest()
    assert window.manifest.portage.profile_override == "default/linux/amd64/23.0/desktop/gnome"
    assert window.manifest.portage.mirror_fallbacks == ["https://mirror.example/gentoo"]


# --- PORT-06: the optional speed test --------------------------------------------------------------------------------------


def test_test_speed_is_offered_only_while_online_and_shows_the_answers(make_window, monkeypatch):
    offline = make_window(info=machine("qemu-q35-virtio"))
    page = page_of(offline)
    page.source_kind.setCurrentIndex(page.source_kind.findData("country"))
    online_info = machine("qemu-q35-virtio")
    online_info.network = True
    page.set_info(online_info)
    assert page.mirror_test.isVisibleTo(page)
    online_info.network = False
    page.set_info(online_info)
    assert not page.mirror_test.isVisibleTo(page)
    # the answers (measured elsewhere) are shown next to the names; nothing else changes
    urls = [str(page.mirror_list.item(row).data(Qt.ItemDataRole.UserRole)) for row in range(page.mirror_list.count())]
    page._show_speeds({urls[0]: 0.123, urls[1]: None})
    assert page.mirror_list.item(0).text().endswith("[123 ms]") and page.mirror_list.item(1).text().endswith("[no answer]")
    page._show_speeds({urls[0]: 0.2})
    assert page.mirror_list.item(0).text().count("[") == 1 and page.checked_mirrors() == []


def test_the_detected_processor_features_can_be_filled_in_and_edited(make_window):
    window = make_window(info=machine("asus-desktop-rtx4090"))
    page = page_of(window)
    window.mode.setCurrentIndex(1)
    assert page.cpu_flags_detect.isVisibleTo(page) and page.cpu_flags.text() == ""
    page.cpu_flags_detect.click()
    detected = page.detected_cpu_flags()
    assert detected and page.cpu_flags.text().split() == detected
    page.cpu_flags.setText(page.cpu_flags.text() + " sse4_2")
    page.save_manifest()
    assert window.manifest.portage.cpu_flags.split()[-1] == "sse4_2"
    nothing = make_window()
    assert not page_of(nothing).cpu_flags_detect.isVisibleTo(nothing.customize)
