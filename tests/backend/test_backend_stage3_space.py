"""The stage3 archive and the live system's memory: room is checked before the download, the archive goes once it is unpacked."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from gentoo_installerd.install import Installer
from gentoo_installerd.stage3 import DOWNLOAD_MARGIN_BYTES, MIB, UNKNOWN_ARCHIVE_BYTES, UNPACKED_STAGE3_BYTES, Stage3Archive, Stage3Error, Stage3Manager, free_bytes

from tests.backend.helpers import FakeInspector, FakeRunner, manifest
from tests.backend.test_stage3 import MIRROR, PATH, VARIANT, VerifiedRunner

NAME = Path(PATH).name


class Setup:
    """A stage3 manager that has a signed index, a verified checksum and a tiny archive to unpack."""

    def __init__(self, tmp_path: Path, announced: int | None = 300 * MIB) -> None:
        self.root = tmp_path
        source = tmp_path / "source.tar"
        with tarfile.open(source, "w") as tar:
            payload = b"stage3-data"
            info = tarfile.TarInfo("etc/gentoo-release")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        self.data = source.read_bytes()
        keyring = tmp_path / "keyring.asc"
        keyring.write_text("test keyring")
        self.archive = Stage3Archive(PATH, announced, f"{MIRROR}/{PATH}", f"{MIRROR}/{PATH}.DIGESTS", f"{MIRROR}/{PATH}.DIGESTS")
        self.manager = Stage3Manager(VerifiedRunner(f"{hashlib.sha512(self.data).hexdigest()}  {NAME}\n"), tmp_path / "cache", keyring)
        self.manager.discover = lambda mirror, variant: self.archive  # type: ignore[method-assign]
        self.manager._fetch_bytes = lambda url: b"clearsigned"  # type: ignore[method-assign]
        self.downloads: list[str] = []
        self.manager._download = self.download  # type: ignore[method-assign]
        self.free: dict[str, int] = {}
        self.manager.free_bytes = lambda path: self.free["cache" if "cache" in str(path) else "target"]
        self.target = tmp_path / "target"

    def download(self, url, destination, progress=None) -> None:
        self.downloads.append(url)
        destination.write_bytes(self.data)

    def install(self) -> Path:
        return self.manager.install(MIRROR, VARIANT, self.target)


class TestFreeSpaceBeforeTheDownload:
    def test_a_download_that_does_not_fit_in_memory_is_refused_before_it_starts(self, tmp_path):
        s = Setup(tmp_path)
        s.free = {"cache": 200 * MIB, "target": 100 * 1024 * MIB}
        with pytest.raises(Stage3Error, match=r"not enough free space to download the stage3 archive: about 364 MiB are needed in .*cache, but only 200 MiB are free\. "
                                              r"The live system keeps this folder in memory"):
            s.install()
        assert s.downloads == [] and not s.target.exists()

    def test_a_target_that_cannot_hold_the_system_is_refused_before_the_download_too(self, tmp_path):
        s = Setup(tmp_path)
        s.free = {"cache": 10 * 1024 * MIB, "target": 3000 * MIB}
        with pytest.raises(Stage3Error, match=r"not enough free space to unpack the stage3 into the new system: about 4096 MiB are needed in .*target, "
                                              r"but only 3000 MiB are free\. Choose a larger disk"):
            s.install()
        assert s.downloads == []

    def test_with_enough_room_the_stage3_is_installed(self, tmp_path):
        s = Setup(tmp_path)
        s.free = {"cache": 300 * MIB + DOWNLOAD_MARGIN_BYTES, "target": UNPACKED_STAGE3_BYTES}  # exactly enough
        s.install()
        assert (s.target / "etc/gentoo-release").read_bytes() == b"stage3-data" and len(s.downloads) == 1

    def test_an_archive_that_was_verified_before_needs_no_room_for_a_download(self, tmp_path):
        s = Setup(tmp_path, announced=None)
        s.manager._secure_cache_dir()
        (s.manager.cache_dir / NAME).write_bytes(s.data)
        s.free = {"cache": 0, "target": UNPACKED_STAGE3_BYTES}
        s.install()
        assert s.downloads == []

    def test_a_partial_download_only_needs_room_for_the_rest(self, tmp_path):
        s = Setup(tmp_path, announced=300 * MIB)
        s.manager._secure_cache_dir()
        (s.manager.cache_dir / (NAME + ".part")).write_bytes(b"x" * (100 * MIB))
        s.free = {"cache": 200 * MIB + DOWNLOAD_MARGIN_BYTES, "target": UNPACKED_STAGE3_BYTES}
        s.install()
        s.free["cache"] -= 1
        with pytest.raises(Stage3Error, match="not enough free space to download"):
            s.manager.install(MIRROR, VARIANT, s.target)

    def test_an_unannounced_size_is_assumed_to_be_large(self, tmp_path):
        s = Setup(tmp_path, announced=None)
        s.free = {"cache": UNKNOWN_ARCHIVE_BYTES + DOWNLOAD_MARGIN_BYTES - 1, "target": UNPACKED_STAGE3_BYTES}
        with pytest.raises(Stage3Error, match="not enough free space to download"):
            s.install()

    def test_free_space_is_read_from_the_nearest_existing_directory(self, tmp_path):
        assert free_bytes(tmp_path / "does" / "not" / "exist") == free_bytes(tmp_path) > 0


class TestRemovingTheArchive:
    def test_the_archive_its_checksums_and_leftovers_are_removed(self, tmp_path):
        s = Setup(tmp_path)
        s.free = {"cache": 10 * 1024 * MIB, "target": 10 * 1024 * MIB}
        archive = s.install()
        cache = s.manager.cache_dir
        (cache / (NAME + ".part")).write_bytes(b"x")
        assert archive.is_file() and (cache / (NAME + ".DIGESTS")).is_file()
        s.manager.discard(archive)
        assert not archive.exists() and not (cache / (NAME + ".part")).exists() and not (cache / (NAME + ".DIGESTS")).exists()
        s.manager.discard(archive)  # already gone: fine

    def test_nothing_outside_the_cache_is_ever_removed(self, tmp_path):
        s = Setup(tmp_path)
        victim = tmp_path / "keep.tar.xz"
        victim.write_bytes(b"precious")
        with pytest.raises(Stage3Error, match="outside the stage3 cache"):
            s.manager.discard(victim)
        assert victim.read_bytes() == b"precious"


class RecordingStage3:
    def __init__(self, archive: Path) -> None:
        self.archive = archive
        self.discarded: list[Path] = []
        self.notify = lambda message: None

    def install(self, *args, **kwargs) -> Path:
        return self.archive

    def discard(self, archive: Path) -> None:
        self.discarded.append(archive)


class TestInstallerRemovesTheArchiveAfterTheCheckpoint:
    def installer(self, tmp_path, **kwargs) -> tuple[Installer, RecordingStage3]:
        stage3 = RecordingStage3(tmp_path / "cache" / "stage3.tar.xz")
        inst = Installer(manifest(), runner=FakeRunner(), target_root=tmp_path / "target", state_dir=tmp_path / "state", cache_dir=tmp_path / "cache",
                         inspector=FakeInspector(), stage3=stage3, emit=lambda event: None, **kwargs)  # type: ignore[arg-type]
        return inst, stage3

    def test_the_archive_goes_after_the_stage_is_checkpointed_and_not_before(self, tmp_path):
        inst, stage3 = self.installer(tmp_path)
        order: list[str] = []
        real_save = inst._save_checkpoint
        inst._save_checkpoint = lambda name: (order.append(f"checkpoint {name}"), real_save(name))[1]  # type: ignore[method-assign]
        stage3.discard = lambda archive: order.append("discard")  # type: ignore[method-assign]
        for name in ("_preflight", "_storage", "_mounts", "_repository", "_portage", "_kernel", "_target", "_base", "_bootloader", "_desktop", "_validate"):
            setattr(inst, name, lambda: None)
        inst._stage3 = lambda: setattr(inst, "_stage3_archive", stage3.archive)  # type: ignore[method-assign]
        inst._execute()
        assert order.index("checkpoint stage3") < order.index("discard") < order.index("checkpoint mounts")
        inst._log_file.close()

    def test_a_failed_stage3_keeps_the_archive_for_the_next_try(self, tmp_path):
        inst, stage3 = self.installer(tmp_path)

        def failing() -> None:
            raise RuntimeError("unpacking failed")

        inst._stage3 = failing  # type: ignore[method-assign]
        inst._preflight = lambda: None  # type: ignore[method-assign]
        inst._storage = lambda: None  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            inst._execute()
        assert stage3.discarded == []
        inst._log_file.close()

    def test_a_preview_has_no_archive_to_remove(self, tmp_path):
        inst, stage3 = self.installer(tmp_path, dry_run=True)
        inst._stage_done("stage3")
        assert stage3.discarded == []

    def test_a_cache_that_cannot_be_cleaned_never_fails_the_installation(self, tmp_path):
        inst, stage3 = self.installer(tmp_path)
        logs: list[str] = []
        inst.log = logs.append  # type: ignore[method-assign]

        def broken(archive: Path) -> None:
            raise OSError("read-only cache")

        stage3.discard = broken  # type: ignore[method-assign]
        inst._stage3_archive = stage3.archive
        inst._stage_done("stage3")
        assert any("could not be removed (read-only cache)" in line for line in logs)
        inst._log_file.close()
