"""Cleanup (S12) and the installer's storage stage: plan logging, receipts, resume."""

from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

from gentoo_installerd import storage as storage_module
from gentoo_installerd.install import Installer
from gentoo_installerd.runner import RunnerError
from gentoo_installerd.storage import StorageError

from tests.backend.helpers import FakeInspector, FakeRunner, manifest
from tests.backend.test_storage_identity import erase
from tests.backend.test_storage_support import FakeDisk, GiB, StatefulRunner, make_manager


class Flaky(FakeRunner):
    """A FakeRunner whose commands fail a set number of times before they succeed."""

    def __init__(self, budget: dict[str, int]) -> None:
        super().__init__()
        self.budget = dict(budget)

    def run(self, argv, **kwargs):
        text = " ".join(argv)
        for prefix in list(self.budget):
            if text.startswith(prefix) and self.budget[prefix] > 0:
                self.budget[prefix] -= 1
                self.commands.append(tuple(argv))
                raise RunnerError(tuple(argv), 32, "target is busy")
        return super().run(argv, **kwargs)


def manager_with(runner, tmp_path, **kwargs):
    return make_manager(runner, tmp_path, **kwargs)


class TestCleanup:
    def test_a_busy_mount_is_retried_with_backoff_before_giving_up(self, tmp_path):
        sleeps: list[float] = []
        runner = Flaky({f"umount -R {tmp_path}/target": 2})
        manager = manager_with(runner, tmp_path, sleep=sleeps.append)
        manager.owned_mounts = [tmp_path / "target"]
        manager.cleanup()
        assert [c for c in runner.commands if c[0] == "umount"] == [("umount", "-R", f"{tmp_path}/target")] * 3
        assert sleeps == [0.5, 1.0]
        assert manager.owned_mounts == []

    def test_after_the_last_retry_the_error_is_raised_and_the_resource_kept_for_another_attempt(self, tmp_path):
        runner = Flaky({"umount": 99})
        manager = manager_with(runner, tmp_path)
        manager.owned_mounts = [tmp_path / "target"]
        with pytest.raises(StorageError, match="Cleanup failed") as caught:
            manager.cleanup()
        assert caught.value.code == "storage.cleanup"
        assert manager.owned_mounts == [tmp_path / "target"]
        runner.budget.clear()
        manager.cleanup()
        assert manager.owned_mounts == []

    def test_a_virtual_filesystem_falls_back_to_a_lazy_unmount_after_sync(self, tmp_path):
        runner = Flaky({f"umount -R {tmp_path}/target/dev": 3})
        manager = manager_with(runner, tmp_path)
        manager.owned_mounts = [tmp_path / "target", tmp_path / "target" / "dev"]
        manager.cleanup()
        text = [" ".join(c) for c in runner.commands]
        assert text.count(f"umount -R {tmp_path}/target/dev") == 3
        assert text.index("sync") < text.index(f"umount -R -l {tmp_path}/target/dev") < text.index(f"umount -R {tmp_path}/target")
        assert manager.owned_mounts == []

    @pytest.mark.parametrize("name", ["", "home"])
    def test_the_target_root_and_data_mounts_are_never_unmounted_lazily(self, tmp_path, name):
        path = tmp_path / "target" / name if name else tmp_path / "target"
        runner = Flaky({f"umount -R {path}": 99})
        manager = manager_with(runner, tmp_path)
        manager.owned_mounts = [path]
        with pytest.raises(StorageError):
            manager.cleanup()
        assert not any("-l" in c for c in runner.commands)
        assert not any(c[0] == "sync" for c in runner.commands)

    def processes(self, tmp_path, *, root: Path) -> None:
        proc = tmp_path / "proc"
        for pid, name, where in ((4242, "gpg-agent", root), (4343, "dirmngr", root), (999, "sshd", tmp_path / "elsewhere")):
            (proc / str(pid)).mkdir(parents=True, exist_ok=True)
            (proc / str(pid) / "comm").write_text(name + "\n")
            (proc / str(pid) / "root").symlink_to(where)
        (proc / "self").mkdir(exist_ok=True)

    def test_processes_left_inside_the_chroot_are_reported_in_the_error(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir(exist_ok=True)
        self.processes(tmp_path, root=target)
        runner = Flaky({"umount": 99})
        manager = manager_with(runner, tmp_path)
        manager.owned_mounts = [target]
        with pytest.raises(StorageError, match=f"processes still inside {target}: 4242:gpg-agent, 4343:dirmngr") as caught:
            manager.cleanup()
        assert "sshd" not in str(caught.value)

    def test_leftover_chroot_processes_are_asked_to_exit_then_killed_and_others_are_left_alone(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir(exist_ok=True)
        self.processes(tmp_path, root=target)
        sent: list[tuple[int, int]] = []
        sleeps: list[float] = []
        manager = manager_with(FakeRunner(), tmp_path, kill=lambda pid, sig: sent.append((pid, sig)), sleep=sleeps.append)
        manager.owned_mounts = [target]
        manager.cleanup()
        assert sent == [(4242, signal.SIGTERM), (4343, signal.SIGTERM), (4242, signal.SIGKILL), (4343, signal.SIGKILL)]
        assert sleeps and sleeps[0] <= 3.0

    def test_nothing_is_signalled_when_no_mount_is_owned(self, tmp_path):
        sent: list[tuple[int, int]] = []
        manager = manager_with(FakeRunner(), tmp_path, kill=lambda pid, sig: sent.append((pid, sig)))
        manager.cleanup()
        assert sent == []

    def test_the_cleanup_order_is_mounts_swap_volume_groups_then_mappers(self, tmp_path):
        runner = FakeRunner()
        manager = manager_with(runner, tmp_path)
        manager.owned_mounts = [tmp_path / "target"]
        manager.owned_swaps = ["/dev/vda2"]
        manager.owned_vgs = ["gentoo"]
        manager.owned_maps = ["gentoo-root"]
        manager.cleanup()
        assert [c[0] for c in runner.commands] == ["umount", "swapoff", "vgchange", "cryptsetup"]


def storage_installer(tmp_path: Path, runner, *, dry_run: bool = False, **changes) -> Installer:
    m = manifest()
    m.storage.device = "/dev/vda"
    m.storage.device_id = "SER"
    for name, value in changes.items():
        setattr(m.storage, name, value)
    storage = make_manager(runner, tmp_path)
    return Installer(m, runner=runner, storage=storage, target_root=tmp_path / "target", state_dir=tmp_path / "state", cache_dir=tmp_path / "cache", dry_run=dry_run, inspector=FakeInspector(), emit=lambda event: EVENTS.append(event))


EVENTS: list = []


class TestStorageStage:
    def setup_method(self):
        EVENTS.clear()

    def test_the_plan_is_logged_before_anything_is_changed_and_travels_with_the_progress_event(self, tmp_path):
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1", model="QEMU HARDDISK", partitions=[{"num": 1, "size": 10 * GiB, "fstype": "ntfs", "label": "Windows"}])])
        planned_before_the_first_write: list[bool] = []
        runner.on("sgdisk --zap-all", lambda r, argv: planned_before_the_first_write.append(any(e.message.startswith("Storage plan:") for e in EVENTS)))
        installer = storage_installer(tmp_path, runner)
        installer._storage()
        assert planned_before_the_first_write == [True]
        logs = [event.message for event in EVENTS if event.type == "log"]
        plan_lines = [line for line in logs if line.startswith("Storage plan:")]
        assert plan_lines[0].startswith("Storage plan: ERASE  /dev/vda (QEMU HARDDISK, 80.0 GiB, serial …SER")
        assert "today: 1 partition: ntfs 10.0 GiB “Windows”" in plan_lines[0]
        progress = next(event for event in EVENTS if event.type == "progress" and event.stage == "storage")
        payload = progress.data["data"]
        assert [entry["mountpoint"] for entry in payload["layout"]] == ["/efi", "/", ""]
        assert payload["plan"]["disks"][0]["path"] == "/dev/vda" and payload["plan"]["disks"][0]["action"] == "erase"

    def test_a_dry_run_plan_needs_no_inventory(self, tmp_path):
        runner = FakeRunner()
        installer = storage_installer(tmp_path, runner, dry_run=True)
        installer._storage()
        assert not any(c[0] == "lsblk" for c in runner.commands)
        assert any(event.message.startswith("Storage plan: ERASE  /dev/vda: all existing data") for event in EVENTS if event.type == "log")

    def test_notes_are_written_to_the_install_log(self, tmp_path):
        runner = StatefulRunner([FakeDisk("/dev/vda", 34 * GiB, serial="SER", wwn="WWN-1")])
        installer = storage_installer(tmp_path, runner)
        installer._storage()
        assert any(event.message.startswith("Storage note: The system will get only") for event in EVENTS if event.type == "log")

    def test_a_storage_error_keeps_its_machine_readable_code(self, tmp_path):
        runner = StatefulRunner([FakeDisk("/dev/vda", 8 * GiB, serial="SER", wwn="WWN-1")])
        installer = storage_installer(tmp_path, runner)
        with pytest.raises(StorageError) as caught:
            installer._storage()
        assert caught.value.code == "storage.capacity"

    def test_resume_gets_btrfs_options_back_through_the_checkpoint_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage_module, "fstab_supports_layout_fields", lambda: (True, True))
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        installer = storage_installer(tmp_path, runner, filesystem="btrfs")
        installer._storage()
        saved = json.loads((tmp_path / "state" / "layout.json").read_text())
        assert saved["receipt"]["version"] == 2 and any(entry["options"] == "subvol=@home,compress=zstd:3" for entry in saved["receipt"]["entries"])
        installer.storage.cleanup()
        runner.commands.clear()
        resumed = storage_installer(tmp_path, runner, filesystem="btrfs")
        resumed.checkpoints.load_layout()
        resumed._verify_storage()
        target = str(tmp_path / "target")
        assert ("mount", "-o", "subvol=@home,compress=zstd:3", "/dev/vda3", target + "/home") in runner.commands
        assert [entry.options for entry in resumed.storage.layout if entry.mountpoint == "/home"] == ["subvol=@home,compress=zstd:3"]

    def test_resume_explains_what_to_do_when_an_assembled_volume_is_gone(self, tmp_path):
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        installer = storage_installer(tmp_path, runner)
        installer._storage()
        installer.storage.cleanup()
        runner.fail["blkid -s UUID -o value /dev/vda3"] = (2, "")
        resumed = storage_installer(tmp_path, runner)
        with pytest.raises(StorageError, match="is missing or has no filesystem signature.*mdadm --assemble --scan, vgchange -ay"):
            resumed._verify_storage()

    def test_unreadable_resolver_configuration_is_a_clear_error(self, tmp_path, monkeypatch):
        from gentoo_installerd.install import InstallError

        installer = storage_installer(tmp_path, FakeRunner())
        real_read_text = Path.read_text

        def read_text(self, *args, **kwargs):
            if str(self) == "/etc/resolv.conf":
                raise FileNotFoundError("/etc/resolv.conf")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
        with pytest.raises(InstallError, match="no readable /etc/resolv.conf.*connect to a network first"):
            installer._mounts()
