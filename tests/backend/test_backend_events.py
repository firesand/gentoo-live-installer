"""The install events of interface I1: every error carries stage, code and what can still be done; completed says what the result is."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from gentoo_installerd import server
from gentoo_installerd.configure import Configurer
from gentoo_installerd.install import InstallBusyError, InstallError, Installer
from gentoo_installerd.runner import DryRunRunner, InstallCancelled
from gentoo_installerd.storage import StorageError, StorageManager

from tests.backend.helpers import FakeInspector, FakeRunner, manifest


def installer(tmp_path: Path, **kwargs) -> tuple[Installer, list]:
    events: list = []
    kwargs.setdefault("runner", DryRunRunner())
    kwargs.setdefault("dry_run", True)
    result = Installer(manifest(), target_root=tmp_path / "target", state_dir=tmp_path / "state", inspector=FakeInspector(), emit=events.append, **kwargs)
    return result, events


def errors(events: list) -> list:
    return [event for event in events if event.type == "error"]


def completed(events: list):
    found = [event for event in events if event.type == "completed"]
    assert len(found) == 1
    return found[0]


def fail_in(inst: Installer, stage: str, exc: BaseException) -> None:
    def raiser() -> None:
        raise exc

    setattr(inst, f"_{stage}" if stage != "validation" else "_validate", raiser)


class TestErrorEvents:
    def test_a_failed_stage_names_itself_and_says_what_can_be_done(self, tmp_path):
        inst, events = installer(tmp_path)
        fail_in(inst, "kernel", InstallError("the kernel did not build"))
        assert not inst.run()
        error = errors(events)[0]
        assert error.stage == "kernel" and error.message == "the kernel did not build"
        assert error.data == {"code": "install.failed", "retryable": True, "resumable": False, "bootable": False}

    def test_a_storage_error_keeps_its_own_code(self, tmp_path):
        inst, events = installer(tmp_path)
        fail_in(inst, "storage", StorageError("The disk is too small", "storage.capacity"))
        assert not inst.run()
        error = errors(events)[0]
        assert error.stage == "storage" and error.data["code"] == "storage.capacity"

    def test_a_cancel_is_retryable_and_says_so(self, tmp_path):
        cancel = threading.Event()
        inst, events = installer(tmp_path, cancel=cancel)
        fail_in(inst, "kernel", InstallCancelled("Installation cancelled"))
        assert not inst.run()
        error = errors(events)[0]
        assert error.error == "cancelled" and error.data["code"] == "install.cancelled" and error.data["retryable"] is True
        assert completed(events).message == "Installation cancelled"

    def test_a_second_job_is_refused_before_any_stage_with_a_stage_to_show(self, tmp_path):
        inst, events = installer(tmp_path)

        def busy() -> None:
            raise InstallBusyError("Another installation is already running")

        inst._acquire_run_lock = busy  # type: ignore[method-assign]
        assert not inst.run()
        error = errors(events)[0]
        assert error.stage == "preflight" and error.data["code"] == "install.busy"
        assert error.data["retryable"] is False and error.data["resumable"] is False

    def test_a_failure_before_the_first_stage_still_names_one(self, tmp_path):
        inst, events = installer(tmp_path, resume=True, dry_run=False, runner=FakeRunner())
        (tmp_path / "state" / "checkpoints.json").write_text("{not json")
        assert not inst.run()
        assert errors(events)[0].stage == "preflight"

    def test_failing_after_the_bootloader_means_the_disk_still_boots(self, tmp_path):
        inst, events = installer(tmp_path)
        fail_in(inst, "desktop", InstallError("the desktop did not build"))
        assert not inst.run()
        assert errors(events)[0].data["bootable"] is True
        assert completed(events).data["bootable"] is True and completed(events).data["success"] is False

    def test_failing_before_the_bootloader_means_it_does_not(self, tmp_path):
        inst, events = installer(tmp_path)
        fail_in(inst, "base", InstallError("the base system did not build"))
        assert not inst.run()
        assert errors(events)[0].data["bootable"] is False and completed(events).data["bootable"] is False

    def test_an_error_that_says_it_is_bootable_is_believed(self, tmp_path):
        inst, events = installer(tmp_path)
        exc = InstallError("only the desktop failed")
        exc.bootable = True  # type: ignore[attr-defined]
        fail_in(inst, "kernel", exc)
        assert not inst.run()
        assert errors(events)[0].data["bootable"] is True

    def test_a_resumable_failure_says_so(self, tmp_path):
        inst, events = installer(tmp_path)
        inst.dry_run = False  # resumable means: storage was prepared and checkpointed, which a preview never does
        inst.checkpoints.completed.add("storage")
        assert inst._resumable() is True
        fail_in(inst, "kernel", InstallError("no network"))
        inst.dry_run = True
        assert not inst.run()
        assert errors(events)[0].data["resumable"] is False  # a preview is never resumable


class TestCompletedEvent:
    def test_success_reports_a_bootable_system_with_nothing_degraded(self, tmp_path):
        inst, events = installer(tmp_path)
        assert inst.run()
        done = completed(events)
        assert done.data == {"success": True, "bootable": True, "degraded": [], "warnings": []}
        assert done.progress == 100.0

    def test_optional_parts_that_failed_are_listed(self, tmp_path):
        inst, events = installer(tmp_path)

        def desktop() -> None:
            inst.degraded.append("The NVIDIA driver could not be built; the open driver is used")

        inst._desktop = desktop  # type: ignore[method-assign]
        assert inst.run()
        assert completed(events).data["degraded"] == ["The NVIDIA driver could not be built; the open driver is used"]

    def test_a_failed_job_lists_what_degraded_before_it_failed(self, tmp_path):
        inst, events = installer(tmp_path)
        inst.degraded.append("sof-firmware could not be installed")
        fail_in(inst, "validation", InstallError("the final check failed"))
        assert not inst.run()
        assert completed(events).data["degraded"] == ["sof-firmware could not be installed"]


class TestCleanupSecrets:
    def test_staged_signing_keys_are_removed_when_the_job_is_cleaned_up(self, tmp_path):
        calls: list[str] = []

        class Recording(Configurer):
            def cleanup_secrets(self) -> None:
                calls.append("cleanup_secrets")

        runner = FakeRunner()
        inst, _ = installer(tmp_path, runner=runner, dry_run=False, storage=StorageManager(runner), configurer=Recording(runner, tmp_path / "target"))
        inst._cleanup()
        assert calls == ["cleanup_secrets"]
        inst._log_file.close()

    def test_a_preview_leaves_the_secrets_of_a_real_job_alone(self, tmp_path):
        calls: list[str] = []

        class Recording(Configurer):
            def cleanup_secrets(self) -> None:
                calls.append("cleanup_secrets")

        runner = FakeRunner()
        inst, _ = installer(tmp_path, runner=runner, dry_run=True, configurer=Recording(runner, tmp_path / "target", dry_run=True))
        inst._cleanup()
        assert calls == []

    def test_the_keys_go_even_when_the_unmount_fails(self, tmp_path):
        calls: list[str] = []

        class Recording(Configurer):
            def cleanup_secrets(self) -> None:
                calls.append("cleanup_secrets")

        runner = FakeRunner()
        runner.failures["umount"] = 1
        storage = StorageManager(runner, cleanup_retries=1)
        storage.owned_mounts = [tmp_path / "target"]
        inst, _ = installer(tmp_path, runner=runner, dry_run=False, storage=storage, configurer=Recording(runner, tmp_path / "target"))
        with pytest.raises(StorageError):
            inst._cleanup()
        assert calls == ["cleanup_secrets"]
        inst._log_file.close()


class TestUnverifiedDisk:
    def test_the_installer_accepts_the_flag_and_defaults_to_refusing(self, tmp_path):
        assert installer(tmp_path)[0].storage.allow_unverified_disk is False
        assert installer(tmp_path, allow_unverified_disk=True)[0].storage.allow_unverified_disk is True

    def test_only_the_command_line_has_the_flag(self):
        assert server.build_parser().parse_args(["--allow-unverified-disk"]).allow_unverified_disk is True
        assert server.build_parser().parse_args([]).allow_unverified_disk is False

    def test_the_daemon_refuses_the_flag_without_a_manifest(self, monkeypatch):
        monkeypatch.setattr(server.os, "geteuid", lambda: 0)
        assert server.main(["--allow-unverified-disk", "--socket", "/nonexistent/dir/s.sock"]) == 2

    def test_the_flag_reaches_the_installer_of_a_command_line_run(self, tmp_path, monkeypatch):
        seen: dict = {}

        class Fake:
            def __init__(self, manifest, **kwargs):
                seen.update(kwargs)
                self.cancel = threading.Event()

            def run(self):
                return True

        monkeypatch.setattr(server, "Installer", Fake)
        path = tmp_path / "manifest.json"
        path.write_text(manifest().to_json())
        for flag in (False, True):
            server.run_cli(path, dry_run=True, target_root=tmp_path / "t", state_dir=tmp_path / "s", cache_dir=tmp_path / "c", resume=False, allow_unverified_disk=flag)
            assert seen["allow_unverified_disk"] is flag

    def test_a_client_of_the_socket_can_never_set_it(self, tmp_path, monkeypatch):
        seen: dict = {}

        class Fake:
            job_id = "job"
            stage_weights: dict = {}

            def __init__(self, manifest, **kwargs):
                seen.update(kwargs)

            def run(self):
                return True

        monkeypatch.setattr(server, "Installer", Fake)
        manager = server.InstallManager(tmp_path / "t", tmp_path / "s", tmp_path / "c")
        manager.start(manifest())
        manager.thread.join(5)
        assert "allow_unverified_disk" not in seen
