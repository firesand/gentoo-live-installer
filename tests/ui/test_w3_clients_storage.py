"""Wave 3 review fixes on the storage page and the start: a consent belongs to the partition or disk it was given for (CONSENT-1),
and a different disk under the same name is not silently taken for the one the user chose (IDENT-1)."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from gentoo_installer.manifest import InstallManifest, Partition
from gentoo_installer.pages import progress as progress_module
from gentoo_installer.system_info import DiskInfo, PartitionInfo
from tests.ui.test_gui_recovery import FakeWorker
from tests.ui.test_gui_storage_safety import GIB, ask, data_disk, info_of, laptop, select

OVERWRITE = "Install over the existing system?"


def system_disk(path="/dev/sdb", serial="D2", **kwargs):
    """A second disk that holds a system somebody cares about."""
    return DiskInfo(path, "Work disk", 500 * GIB, serial=serial, transport="sata",
                    partitions=[PartitionInfo(f"{path}1", 480 * GIB, "ext4", label="work-gentoo", uuid=f"uuid-of-{path[5:]}1", parent=path)], **kwargs)


def kept_root_window(make_window, monkeypatch, *, info=None):
    FakeWorker.instances = []
    monkeypatch.setattr(progress_module, "BackendWorker", FakeWorker)
    window = make_window(info=info or info_of(laptop(), system_disk()))
    storage = window.manifest.storage
    storage.mode, storage.device, storage.device_id = "manual", "/dev/nvme0n1", "S5Y1NX0T123456"
    storage.partitions = [Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False), Partition("/dev/nvme0n1p3", "/", "ext4", format=False)]
    return window


def use_root(window, device):
    window.manifest.storage.partitions = [Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False), Partition(device, "/", "ext4", format=False)]


def fail_and_return(window):
    """The run ends without success and the user presses 'Back to settings'."""
    window.progress.handle_event({"type": "error", "message": "boom", "error": "boom", "data": {"retryable": True}})
    window.progress.handle_event({"type": "completed", "data": {"success": False}})
    window._worker_stopped()
    window._return_to_settings()


# --- CONSENT-1: the answer 'Yes, install over the system' is for one partition -------------------------------------------------------------


def test_the_consent_given_for_one_root_does_not_cover_another_kept_root(make_window, monkeypatch):
    window = kept_root_window(make_window, monkeypatch)
    shown = ask(monkeypatch, {OVERWRITE: QMessageBox.StandardButton.Yes})
    window.start_installation()
    assert [title for title, _ in shown] == [OVERWRITE] and window.manifest.storage.overwrite_existing_system is True
    started = len(FakeWorker.instances)
    fail_and_return(window)
    assert window.manifest.storage.overwrite_existing_system is True, "the settings of the run stay as they were, so that 'Resume' still matches"

    use_root(window, "/dev/sdb1")  # another partition, with another system on it
    shown = ask(monkeypatch, {})  # the answer is No this time
    window.start_installation()
    assert [title for title, _ in shown] == [OVERWRITE], "the question is asked again for the new root"
    assert "/dev/sdb1" in shown[0][1]
    assert window.manifest.storage.overwrite_existing_system is False
    assert len(FakeWorker.instances) == started and not window.install_started, "nothing was sent to the service"


def test_saying_yes_for_the_second_root_is_remembered_for_that_root(make_window, monkeypatch):
    window = kept_root_window(make_window, monkeypatch)
    shown = ask(monkeypatch, {OVERWRITE: QMessageBox.StandardButton.Yes})
    window.start_installation()
    fail_and_return(window)
    use_root(window, "/dev/sdb1")
    window.start_installation()
    assert [title for title, _ in shown] == [OVERWRITE, OVERWRITE] and window.manifest.storage.overwrite_existing_system is True
    fail_and_return(window)
    shown.clear()
    window.start_installation()  # the same root again: no second question
    assert shown == [] and window.install_started


def test_a_consent_from_a_draft_or_an_imported_file_is_not_taken_for_granted(make_window, monkeypatch):
    window = kept_root_window(make_window, monkeypatch)
    window.manifest.storage.overwrite_existing_system = True  # loaded from the saved draft, or from 'Import settings'
    shown = ask(monkeypatch, {})
    window.start_installation()
    assert [title for title, _ in shown] == [OVERWRITE]
    assert not FakeWorker.instances and window.manifest.storage.overwrite_existing_system is False


def test_a_resume_keeps_the_consent_of_the_run_it_continues(make_window, monkeypatch):
    window = kept_root_window(make_window, monkeypatch)
    window.manifest.storage.overwrite_existing_system = True  # the draft of the run that is resumed after a restart
    shown = ask(monkeypatch, {})
    window._show_page(6)
    window.review.resume_check.setVisible(True)
    window.review.resume_check.setChecked(True)
    window.start_installation()
    assert shown == [] and FakeWorker.instances[-1].kwargs == {"resume": True}
    assert window.manifest.storage.overwrite_existing_system is True


def test_the_same_partition_name_on_another_filesystem_is_asked_about_again(make_window, monkeypatch):
    window = kept_root_window(make_window, monkeypatch)
    shown = ask(monkeypatch, {OVERWRITE: QMessageBox.StandardButton.Yes})
    window.start_installation()
    fail_and_return(window)
    swapped = info_of(laptop(), system_disk())
    next(part for part in swapped.disks[0].partitions if part.path == "/dev/nvme0n1p3").uuid = "another-filesystem"  # reformatted, or another disk
    window.apply_info(swapped)
    shown.clear()
    window.start_installation()
    assert [title for title, _ in shown] == [OVERWRITE]


def test_a_formatted_root_never_asks(make_window, monkeypatch):
    window = kept_root_window(make_window, monkeypatch)
    window.manifest.storage.partitions[1].format = True
    shown = ask(monkeypatch, {"Format these partitions?": QMessageBox.StandardButton.Yes})
    window.start_installation()
    assert [title for title, _ in shown] == ["Format these partitions?"]


# --- CONSENT-1: the box 'switch these volumes off' is for one disk ------------------------------------------------------------------------------


def lvm_disk(path, serial):
    disk = DiskInfo(path, "Old disk", 500 * GIB, serial=serial, transport="sata", pttype="gpt",
                    partitions=[PartitionInfo(f"{path}1", 400 * GIB, "LVM2_member", parent=path)])
    disk.signatures = ["LVM2_member"]
    return disk


def test_the_box_ticked_for_one_disk_is_not_ticked_for_the_next(make_window):
    window = make_window(info=info_of(lvm_disk("/dev/sda", "A1"), lvm_disk("/dev/sdb", "B1")))
    page = select(window, "/dev/sda")
    page.release_holders.setChecked(True)
    assert page.validate_page() == (True, "") and window.manifest.storage.release_holders is True
    select(window, "/dev/sdb")
    assert page.holders_card.isVisibleTo(page) and not page.release_holders.isChecked()
    valid, message = page.validate_page()
    assert not valid and "still in use" in message
    assert window.manifest.storage.release_holders is False


def test_a_rescan_of_the_same_disk_keeps_the_box_ticked(make_window):
    window = make_window(info=info_of(lvm_disk("/dev/sda", "A1")))
    page = select(window, "/dev/sda")
    page.release_holders.setChecked(True)
    window.apply_info(info_of(lvm_disk("/dev/sda", "A1")))
    assert page.release_holders.isChecked() and page.validate_page() == (True, "")


def test_going_back_to_the_page_keeps_the_consent_of_the_disk_that_is_chosen(make_window):
    window = make_window(info=info_of(lvm_disk("/dev/sda", "A1"), lvm_disk("/dev/sdb", "B1")))
    page = select(window, "/dev/sda")
    page.release_holders.setChecked(True)
    page.save_manifest()
    page.load_manifest()
    assert page.disk_group.value() == "/dev/sda" and page.release_holders.isChecked()


def test_a_loaded_consent_survives_the_first_choice_of_a_disk(make_window):
    manifest = InstallManifest()
    manifest.storage.release_holders = True
    window = make_window(manifest=manifest, info=info_of(lvm_disk("/dev/sda", "A1"), lvm_disk("/dev/sdb", "B1")))
    page = select(window, "/dev/sda")
    assert page.release_holders.isChecked() and page.validate_page() == (True, "")


# --- IDENT-1: another disk under the same name -------------------------------------------------------------------------------------------------


def stick(serial, **kwargs):
    return DiskInfo("/dev/sdb", "USB stick", 64 * GIB, serial=serial, transport="usb", partitions=[PartitionInfo("/dev/sdb1", 60 * GIB, "ext4", label="Photo backup", parent="/dev/sdb")], **kwargs)


def test_a_different_disk_under_the_selected_name_is_not_silently_taken(make_window):
    window = make_window(info=info_of(stick("STICK-A-0001"), data_disk("/dev/sdc", "C1")))
    page = select(window, "/dev/sdb")
    assert page.validate_page() == (True, "")
    assert (window.manifest.storage.device, window.manifest.storage.device_id) == ("/dev/sdb", "STICK-A-0001")

    window.apply_info(info_of(stick("STICK-B-0002"), data_disk("/dev/sdc", "C1")))  # a rescan finds another disk at /dev/sdb
    assert page.disk_group.value() == "", "the selection is dropped"
    assert page.disk_notice.isVisibleTo(page) and "/dev/sdb" in page.disk_notice.text() and "not the same disk" in page.disk_notice.text()
    valid, message = page.validate_page()
    assert not valid and "/dev/sdb" in message and "not the same disk" in message
    assert window.manifest.storage.device_id == "" and window.manifest.storage.device == "", "no stale pin, and no pin of the new disk either"

    select(window, "/dev/sdb")  # the user looks at the new disk and chooses it
    assert page.validate_page() == (True, "") and window.manifest.storage.device_id == "STICK-B-0002"
    assert not page.disk_notice.isVisibleTo(page)


def test_a_rescan_that_finds_the_same_disk_changes_nothing(make_window):
    window = make_window(info=info_of(stick("STICK-A-0001")))
    page = select(window, "/dev/sdb")
    page.validate_page()
    window.apply_info(info_of(stick("STICK-A-0001")))
    assert page.disk_group.value() == "/dev/sdb" and not page.disk_notice.isVisibleTo(page)
    assert window.manifest.storage.device_id == "STICK-A-0001"


def test_going_back_and_forward_does_not_select_the_other_disk_again(make_window):
    window = make_window(info=info_of(stick("STICK-A-0001")))
    page = select(window, "/dev/sdb")
    page.validate_page()
    window.apply_info(info_of(stick("STICK-B-0002")))
    page.load_manifest()  # what showing the page again does
    assert page.disk_group.value() == ""
    window.apply_info(info_of(stick("STICK-B-0002")))  # and another rescan
    assert page.disk_group.value() == ""


def test_a_choice_that_was_not_saved_yet_is_not_judged_by_the_old_pin(make_window):
    window = make_window(info=info_of(stick("STICK-A-0001"), data_disk("/dev/sdc", "C1")))
    page = select(window, "/dev/sdb")
    page.validate_page()  # pinned to the stick
    select(window, "/dev/sdc")  # the user moves on to another disk; the manifest is only written when the page is left
    window.apply_info(info_of(stick("STICK-A-0001"), data_disk("/dev/sdc", "C1")))
    assert page.disk_group.value() == "/dev/sdc" and not page.disk_notice.isVisibleTo(page)


def test_a_disk_without_a_serial_number_is_recognised_by_its_path_size_and_model(make_window):
    def virtual(model):
        return DiskInfo("/dev/vdb", model, 64 * GIB, by_path="pci-0000:00:05.0", partitions=[PartitionInfo("/dev/vdb1", 60 * GIB, "ext4", parent="/dev/vdb")])

    window = make_window(info=info_of(virtual("QEMU HARDDISK")))
    page = select(window, "/dev/vdb")
    page.approve_path.setChecked(True)
    assert page.validate_page() == (True, "") and window.manifest.storage.device_id_kind == "path"
    window.apply_info(info_of(virtual("QEMU HARDDISK")))
    assert page.disk_group.value() == "/dev/vdb"
    window.apply_info(info_of(virtual("Some other model")))
    assert page.disk_group.value() == "" and page.disk_notice.isVisibleTo(page)


def test_the_consents_of_the_old_disk_do_not_pass_to_the_new_one(make_window):
    def holder(serial):
        disk = lvm_disk("/dev/sdb", serial)
        return disk

    window = make_window(info=info_of(holder("OLD-0001")))
    page = select(window, "/dev/sdb")
    page.release_holders.setChecked(True)
    assert page.validate_page() == (True, "")
    window.apply_info(info_of(holder("NEW-0002")))
    select(window, "/dev/sdb")
    assert not page.release_holders.isChecked() and window.manifest.storage.release_holders is False
    assert not page.validate_page()[0]


def test_a_first_scan_selects_the_disk_of_a_loaded_manifest_as_before(make_window):
    manifest = InstallManifest()
    manifest.storage.device, manifest.storage.device_id = "/dev/sdb", "STICK-A-0001"
    window = make_window(manifest=manifest, info=info_of(stick("STICK-A-0001")))
    assert window.storage.disk_group.value() == "/dev/sdb"
