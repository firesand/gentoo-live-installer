"""STOR-10: the manual editor offered "none (do not mount)" and the manifest rejected every such row with 'Invalid mountpoint: '.

A partition that is not listed is never touched, so a row that does nothing has no purpose: the editor no
longer offers it, and a hand-written or old row says what to do instead of ending at a colon.
"""

from __future__ import annotations

from gentoo_installer.manifest import Partition
from gentoo_installer.pages.manual_storage import FILESYSTEMS, ManualLayoutEditor

from tests.backend.test_storage_manifest import manual, storage_errors

ROOT = Partition("/dev/vda2", "/", "ext4")


class TestEveryRowTheEditorCanMakeIsAccepted:
    def editor_row(self, qapp, choice: str, mount: str = "/data") -> Partition:
        editor = ManualLayoutEditor()
        editor.add_row(Partition("/dev/vda3", mount, "ext4", format=True))
        fs = editor.table.cellWidget(0, 2)
        fs.setCurrentIndex(fs.findData(choice))
        [row] = editor.rows()
        return row

    def test_the_offered_filesystems_are_the_ones_the_manifest_knows(self, qapp):
        editor = ManualLayoutEditor()
        editor.add_row()
        fs = editor.table.cellWidget(0, 2)
        offered = [fs.itemData(i) for i in range(fs.count())]
        assert offered == [value for value, _ in FILESYSTEMS]
        assert "none" not in offered

    def test_each_choice_validates_next_to_a_root(self, qapp):
        editor = ManualLayoutEditor()
        editor.add_row()
        fs = editor.table.cellWidget(0, 2)
        for index in range(fs.count()):
            choice = fs.itemData(index)
            row = self.editor_row(qapp, choice)
            assert storage_errors(manual(ROOT, row)) == [], (choice, row)

    def test_a_swap_row_has_no_mountpoint(self, qapp):
        assert self.editor_row(qapp, "swap").mountpoint == ""


class TestRowsThatCannotWork:
    def test_a_none_row_says_what_to_do_instead_of_ending_at_a_colon(self):
        errors = storage_errors(manual(ROOT, Partition("/dev/vda3", "", "none", format=False)))
        assert errors and not any(error.rstrip().endswith(":") for error in errors), errors
        assert any("/dev/vda3" in error and "remove the row" in error.lower() for error in errors), errors

    def test_the_same_for_a_none_row_with_a_mountpoint(self):
        errors = storage_errors(manual(ROOT, Partition("/dev/vda3", "/data", "none", format=False)))
        assert any("remove the row" in error.lower() for error in errors), errors

    def test_an_empty_mountpoint_names_the_partition_and_the_way_out(self):
        errors = storage_errors(manual(ROOT, Partition("/dev/vda3", "", "ext4")))
        assert any("Invalid mountpoint" in error and "/dev/vda3" in error and "/home" in error for error in errors), errors
        assert not any(error.rstrip().endswith(":") for error in errors), errors

    def test_a_bad_mountpoint_is_still_reported_as_it_was(self):
        assert any("Invalid mountpoint: /data dir" in error for error in storage_errors(manual(ROOT, Partition("/dev/vda3", "/data dir", "ext4"))))

    def test_swap_and_the_real_filesystems_are_unchanged(self):
        rows = (ROOT, Partition("/dev/vda3", "", "swap"), Partition("/dev/vda4", "/data", "xfs"), Partition("/dev/vda5", "/home", "btrfs"))
        assert storage_errors(manual(*rows)) == []

    def test_an_old_draft_with_a_none_row_loads_into_the_editor_without_failing(self, qapp):
        editor = ManualLayoutEditor()
        editor.set_rows([Partition("/dev/vda2", "/", "ext4"), Partition("/dev/vda3", "", "none", format=False)])
        rows = editor.rows()
        assert [row.device for row in rows] == ["/dev/vda2", "/dev/vda3"]
        assert rows[1].filesystem != "none"  # the row shows as a normal one the user has to fill in or remove
        assert any("Invalid mountpoint" in error and "/dev/vda3" in error for error in storage_errors(manual(*rows)))
