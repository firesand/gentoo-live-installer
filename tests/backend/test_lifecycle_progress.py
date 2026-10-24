"""D15: one weighted, monotonic progress value for the whole installation."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gentoo_installerd import install as install_module
from gentoo_installerd.events import Event
from gentoo_installerd.install import STAGE_WEIGHTS, STAGES, InstallError, Installer, stage_weights
from gentoo_installerd.runner import DryRunRunner

from tests.backend.helpers import FakeInspector, manifest


def dry_run(m=None, **kwargs):
    events: list[Event] = []
    temporary = tempfile.TemporaryDirectory()
    installer = Installer(m or manifest(), runner=DryRunRunner(), target_root=Path(temporary.name, "t"), state_dir=Path(temporary.name, "s"), dry_run=True, inspector=FakeInspector(), emit=events.append, **kwargs)
    return installer, events, temporary


class StageWeightTests(unittest.TestCase):
    def test_the_weights_cover_every_stage_and_sum_to_100(self) -> None:
        self.assertEqual(tuple(STAGE_WEIGHTS), STAGES)
        self.assertEqual(sum(STAGE_WEIGHTS.values()), 100)
        self.assertEqual(sum(stage_weights("minimal").values()), 100)

    def test_a_minimal_system_has_almost_no_desktop_stage(self) -> None:
        weights = stage_weights("minimal")
        self.assertEqual(weights["desktop"], 1)
        self.assertGreater(weights["base"], STAGE_WEIGHTS["base"])
        self.assertEqual(stage_weights("kde"), STAGE_WEIGHTS)

    def test_the_installer_uses_the_weights_of_its_manifest(self) -> None:
        m = manifest()
        m.system.desktop = "minimal"
        installer, _events, temporary = dry_run(m)
        with temporary:
            self.assertEqual(installer.stage_weights, stage_weights("minimal"))


class ProgressTests(unittest.TestCase):
    def test_progress_never_decreases_and_reaches_100_only_at_the_end(self) -> None:
        installer, events, temporary = dry_run()
        with temporary:
            self.assertTrue(installer.run())
        values = [event.progress for event in events if event.progress is not None]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[0], 0.0)
        completed = next(event for event in events if event.type == "completed")
        self.assertEqual(completed.progress, 100.0)
        validation_done = next(event for event in events if event.type == "stage" and event.stage == "validation" and event.data["status"] == "completed")
        self.assertLess(validation_done.progress, 100.0)
        cleanup = [event for event in events if event.stage == "cleanup" and event.type == "stage"]
        self.assertEqual(cleanup[-1].progress, 100.0)
        self.assertTrue(all(event.progress >= validation_done.progress for event in cleanup))

    def test_stage_and_progress_events_carry_the_overall_value_and_the_local_one(self) -> None:
        installer, events, temporary = dry_run()
        with temporary:
            installer.run()
        for event in events:
            if event.type in {"stage", "progress"}:
                self.assertIsNotNone(event.progress, event)
        started = next(event for event in events if event.type == "stage" and event.stage == "stage3" and event.data["status"] == "running")
        finished = next(event for event in events if event.type == "stage" and event.stage == "stage3" and event.data["status"] == "completed")
        self.assertEqual((started.data["stage_progress"], finished.data["stage_progress"]), (0.0, 100.0))
        self.assertEqual(finished.progress - started.progress, STAGE_WEIGHTS["stage3"])

    def test_a_download_percentage_is_stage_local_and_cannot_hijack_the_bar(self) -> None:
        installer, events, temporary = dry_run()
        with temporary:
            installer._stage = "stage3"
            installer.emit("stage", "stage3", "Starting stage3", status="running")
            installer.emit("progress", "stage3", "Downloading stage3", 50.0, downloaded=5, total=10)
            installer.emit("progress", "stage3", "Downloading stage3", 100.0, downloaded=10, total=10)
        running, half, full = (event for event in events if event.type in {"stage", "progress"})
        before = sum(STAGE_WEIGHTS[name] for name in STAGES[: STAGES.index("stage3")])
        self.assertEqual(running.progress, before)
        self.assertEqual(half.progress, before + STAGE_WEIGHTS["stage3"] * 0.5)
        self.assertEqual(full.progress, before + STAGE_WEIGHTS["stage3"])  # 100% of the download is the end of its stage, not of the bar
        self.assertEqual((half.data["stage_progress"], full.data["stage_progress"]), (50.0, 100.0))
        self.assertEqual((half.data["downloaded"], half.data["total"]), (5, 10))

    def test_a_late_low_value_never_lowers_the_bar(self) -> None:
        installer, events, temporary = dry_run()
        with temporary:
            installer.emit("stage", "kernel", "Completed kernel", status="completed")
            high = events[-1].progress
            installer.emit("progress", "storage", "late storage note", 3.0)
            installer.emit("progress", "", "no stage at all", 10.0)
        self.assertEqual([event.progress for event in events][1:], [high, high])

    def test_package_counters_from_emerge_move_the_bar_inside_a_long_stage(self) -> None:
        installer, events, temporary = dry_run()
        with temporary:
            installer._stage = "desktop"
            installer.log(">>> Emerging (1 of 10) kde-plasma/plasma-meta-6.4::gentoo")
            installer.log(">>> Emerging (5 of 10) dev-qt/qtbase-6.9::gentoo")
            installer.log(">>> Emerging binary (6 of 10) dev-qt/qtdeclarative-6.9::gentoo")
            installer.log(">>> Emerging (3 of 10) dev-libs/late-1::gentoo")  # a restarted counter must not go back
            installer.log("  * ordinary line")
        updates = [event for event in events if event.type == "progress"]
        self.assertEqual([(event.data["package"], event.data["packages"]) for event in updates], [(1, 10), (5, 10), (6, 10), (3, 10)])
        self.assertEqual([event.data["stage_progress"] for event in updates], [0.0, 40.0, 50.0, 50.0])
        self.assertEqual([event.message for event in updates][:2], ["Package 1 of 10", "Package 5 of 10"])
        values = [event.progress for event in updates]
        self.assertEqual(values, sorted(values))
        self.assertEqual(updates[2].stage, "desktop")

    def test_nonsense_package_counters_are_ignored(self) -> None:
        installer, events, temporary = dry_run()
        with temporary:
            installer._stage = "base"
            installer.log(">>> Emerging (0 of 4) x")
            installer.log(">>> Emerging (9 of 4) x")
            installer.log(">>> Emerging (a of b) x")
        self.assertFalse([event for event in events if event.type == "progress"])

    def test_log_events_do_not_carry_progress_but_carry_the_stage(self) -> None:
        installer, events, temporary = dry_run()
        with temporary:
            installer._stage = "kernel"
            installer.log("plain line")
        self.assertIsNone(events[0].progress)
        self.assertEqual(events[0].stage, "kernel")

    def test_error_events_name_the_stage_and_a_machine_readable_code(self) -> None:
        installer, events, temporary = dry_run()
        with temporary:
            installer._kernel = lambda: (_ for _ in ()).throw(InstallError("dracut exploded"))  # type: ignore[method-assign]
            self.assertFalse(installer.run())
        error = next(event for event in events if event.type == "error" and event.stage != "cleanup")
        self.assertEqual(error.stage, "kernel")
        self.assertEqual(error.data["code"], "install.failed")
        self.assertIs(error.data["retryable"], True)
        self.assertIn("dracut exploded", error.error)
        self.assertEqual(error.message, error.error)

    def test_the_manual_kernel_stop_is_reported_at_the_kernel_stage(self) -> None:
        m = manifest()
        m.system.kernel = "manual"
        installer, events, temporary = dry_run(m)
        with temporary:
            self.assertFalse(installer.run())
        error = next(event for event in events if event.type == "error")
        self.assertEqual(error.stage, "kernel")


if __name__ == "__main__":
    unittest.main()
