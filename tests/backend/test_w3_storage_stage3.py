"""The stage3 stage: Cancel during the unpacking (STOR-8) and the target-emptiness warning (STOR-9)."""

from __future__ import annotations

import io
import tarfile
import threading
from pathlib import Path

import pytest

from gentoo_installerd.runner import InstallCancelled
from gentoo_installerd.stage3 import Stage3Error, Stage3Manager

from tests.backend.helpers import FakeRunner
from tests.backend.test_storage_identity import vda
from tests.backend.test_storage_stage import storage_installer
from tests.backend.test_storage_support import StatefulRunner

MEMBERS = 300


def make_archive(path: Path, members: int = MEMBERS) -> Path:
    with tarfile.open(path, "w") as tar:
        directory = tarfile.TarInfo("etc")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        tar.addfile(directory)
        for number in range(members):
            info = tarfile.TarInfo(f"etc/file{number:04d}")
            info.size = 2
            tar.addfile(info, io.BytesIO(b"ok"))
    return path


def extracted(target: Path) -> int:
    return len(list(target.glob("etc/file*")))


class CancelAfter:
    """Stands in for Stage3Manager._checked_member and sets the cancel event on its n-th call (the header pass is the first MEMBERS + 1 calls)."""

    def __init__(self, cancel: threading.Event, calls: int) -> None:
        self.cancel, self.calls, self.seen = cancel, calls, 0
        self.real = Stage3Manager._checked_member

    def __call__(self, member, root):
        self.seen += 1
        if self.seen == self.calls:
            self.cancel.set()
        return self.real(member, root)


class TestCancelWhileUnpacking:
    """STOR-8: extract() was one long call with no cancellation point; on a slow target Cancel took minutes to be noticed."""

    def manager(self, tmp_path) -> Stage3Manager:
        return Stage3Manager(FakeRunner(), tmp_path / "cache", cancel=threading.Event())

    def test_a_cancel_that_is_already_set_extracts_nothing(self, tmp_path):
        archive = make_archive(tmp_path / "stage3.tar")
        manager = self.manager(tmp_path)
        manager.cancel.set()
        with pytest.raises(InstallCancelled):
            manager.extract(archive, tmp_path / "target")
        assert extracted(tmp_path / "target") == 0

    def test_a_cancel_while_the_headers_are_read_extracts_nothing(self, tmp_path):
        archive = make_archive(tmp_path / "stage3.tar")
        manager = self.manager(tmp_path)
        manager._checked_member = CancelAfter(manager.cancel, 5)  # type: ignore[method-assign,assignment]
        with pytest.raises(InstallCancelled):
            manager.extract(archive, tmp_path / "target")
        assert extracted(tmp_path / "target") == 0

    def test_a_cancel_in_the_middle_of_the_unpacking_stops_it_at_the_next_member(self, tmp_path):
        archive = make_archive(tmp_path / "stage3.tar")
        manager = self.manager(tmp_path)
        manager._checked_member = CancelAfter(manager.cancel, MEMBERS + 1 + 50)  # type: ignore[method-assign,assignment]
        with pytest.raises(InstallCancelled):
            manager.extract(archive, tmp_path / "target")
        count = extracted(tmp_path / "target")
        assert 0 < count < MEMBERS // 2, count  # a few dozen members were written, not all 300

    def test_cancel_is_not_turned_into_an_extraction_failure(self, tmp_path):
        archive = make_archive(tmp_path / "stage3.tar")
        manager = self.manager(tmp_path)
        manager.cancel.set()
        with pytest.raises(InstallCancelled) as caught:
            manager.extract(archive, tmp_path / "target")
        assert not isinstance(caught.value, Stage3Error)

    def test_an_archive_that_is_not_cancelled_still_unpacks_completely(self, tmp_path):
        archive = make_archive(tmp_path / "stage3.tar")
        manager = self.manager(tmp_path)
        manager.extract(archive, tmp_path / "target")
        assert extracted(tmp_path / "target") == MEMBERS


class RecordingStage3:
    """What the install stage hands to the stage3 manager, checked against the real emptiness rule."""

    def __init__(self) -> None:
        self.notify = lambda message: None
        self.allowed: list[str] = []
        self.leftovers: list[str] = []

    def install(self, mirror, variant, target_root, progress=None, **kwargs) -> Path:
        self.allowed = list(kwargs["allowed_existing"])
        self.leftovers = Stage3Manager.unexpected_root_entries(Path(target_root), self.allowed)
        return Path(target_root) / "archive"


class TestEmptinessWarning:
    """STOR-9: the swap file of an encrypted install is the installer's own file, not something that was on the target."""

    def stage(self, tmp_path, *created: str, **changes) -> RecordingStage3:
        runner = StatefulRunner([vda()])
        installer = storage_installer(tmp_path, runner, luks=True, luks_passphrase="long enough passphrase", **changes)
        installer._storage()
        target = tmp_path / "target"
        for name in ("lost+found", "boot", "efi", *created):
            (target / name).mkdir(exist_ok=True)
        recorder = RecordingStage3()
        installer.stage3 = recorder  # type: ignore[assignment]
        installer._stage3()
        return recorder

    def test_the_swap_file_of_an_encrypted_install_is_allowed(self, tmp_path):
        recorder = self.stage(tmp_path, "swapfile")
        assert recorder.leftovers == [], recorder.allowed
        assert "/swapfile" in recorder.allowed

    def test_the_btrfs_swap_subvolume_is_allowed(self, tmp_path):
        recorder = self.stage(tmp_path, "swap", filesystem="btrfs")
        assert recorder.leftovers == [], recorder.allowed

    def test_something_that_really_was_there_is_still_reported(self, tmp_path):
        recorder = self.stage(tmp_path, "swapfile", "old-system-file")
        assert recorder.leftovers == ["old-system-file"]

    def test_no_swap_no_swap_entry(self, tmp_path):
        recorder = self.stage(tmp_path, swap_mib=0)
        assert "/swapfile" not in recorder.allowed and recorder.leftovers == []
