"""The Runner keywords timeout, capture and root_identity: who passes them, and who must not break without them."""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_installerd.configure import Configurer
from gentoo_installerd.runner import CommandResult, RunnerError, accepts_keyword, validate_argv
from gentoo_installerd.storage import CLEANUP_TIMEOUT_SECONDS, StorageError

from tests.backend.helpers import FakeRunner, manifest
from tests.backend.test_storage_support import StatefulRunner, make_manager


class OldRunner:
    """A runner from before the newer keywords: passing capture or root_identity would raise TypeError."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv, *, stdin=None, secret=False, env=None, cwd=None) -> CommandResult:
        args = validate_argv(argv)
        self.commands.append(args)
        return CommandResult(args, 0, "")


class TestAcceptsKeyword:
    def test_detects_explicit_keywords_only(self) -> None:
        assert accepts_keyword(FakeRunner().run, "capture")
        assert accepts_keyword(FakeRunner().run, "timeout")
        assert not accepts_keyword(OldRunner().run, "capture")
        assert accepts_keyword(OldRunner().run, "env")
        assert not accepts_keyword(lambda argv, **kwargs: None, "capture")  # **kwargs is not a promise


class TestChroot:
    def test_commands_in_the_target_run_as_root_and_stream_their_output(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        Configurer(runner, tmp_path).chroot(("emerge", "--version"))
        assert runner.options == [{"timeout": None, "capture": False, "root_identity": True}]

    def test_output_is_captured_only_when_the_caller_reads_it(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.respond("chroot", "something")
        configurer = Configurer(runner, tmp_path)
        assert configurer.chroot(("lsinitrd", "-m", "/boot/x"), capture=True).stdout == "something"
        assert runner.options[-1]["capture"] is True

    def test_the_two_readers_of_chroot_output_ask_for_it(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        configurer = Configurer(runner, tmp_path)
        configurer.world_drift(manifest())
        assert runner.options[-1]["capture"] is True
        (tmp_path / "boot").mkdir()
        (tmp_path / "boot/initramfs-6.12.img").write_bytes(b"x")
        configurer.initramfs_modules()
        assert runner.options[-1]["capture"] is True

    def test_a_runner_without_the_keywords_still_works(self, tmp_path: Path) -> None:
        runner = OldRunner()
        Configurer(runner, tmp_path).chroot(("true",))
        assert runner.commands == [("chroot", str(tmp_path), "true")]

    def test_a_failed_streamed_command_still_explains_itself(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.failures["chroot"] = 1
        with pytest.raises(RunnerError, match="injected failure"):
            Configurer(runner, tmp_path).chroot(("emerge", "x"))


class TestStorageCleanup:
    def test_every_release_command_is_bounded_in_time(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        manager = make_manager(runner, tmp_path)
        manager.owned_mounts = [tmp_path / "target"]
        manager.owned_swaps = ["/dev/vda2"]
        manager.owned_vgs = ["gentoo"]
        manager.owned_maps = ["gentoo-root"]
        manager.cleanup()
        assert [c[0] for c in runner.commands] == ["umount", "swapoff", "vgchange", "cryptsetup"]
        assert [option["timeout"] for option in runner.options] == [CLEANUP_TIMEOUT_SECONDS] * 4
        assert CLEANUP_TIMEOUT_SECONDS == 120

    def test_a_busy_encrypted_volume_is_explained_in_plain_words(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.failures["cryptsetup close"] = 5
        manager = make_manager(runner, tmp_path)
        manager.owned_maps = ["gentoo-root"]
        with pytest.raises(StorageError, match=r"cryptsetup close failed \(5: device already exists or is busy\)") as caught:
            manager.cleanup()
        assert caught.value.code == "storage.cleanup"

    def test_a_cryptsetup_failure_while_preparing_gets_its_own_code(self, tmp_path: Path) -> None:
        from tests.backend.test_storage_identity import erase
        from tests.backend.test_storage_support import FakeDisk, GiB

        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")], fail={"cryptsetup luksFormat": (3, "")})
        manager = make_manager(runner, tmp_path)
        with pytest.raises(StorageError, match=r"cryptsetup luksFormat failed \(3: out of memory") as caught:
            manager.prepare(erase(luks=True, luks_passphrase="long enough passphrase"))
        assert caught.value.code == "storage.cryptsetup"
