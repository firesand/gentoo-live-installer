"""Breaking a dependency cycle the way emerge suggests, and the QtWebEngine flag of the binary packages."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gentoo_installerd.configure import (
    CYCLE_BREAK_RECORD, CYCLE_BREAK_USE, MAX_CYCLE_BREAKS, PackageInstallError, cycle_break_suggestion, package_of,
)

from tests.backend.target_helpers import ScriptedRunner, configurer, target_manifest

# What emerge --pretend printed for a source-only KDE install from a stage3 on 2026-10-24.
CYCLE = """\
 * Error: circular dependencies:

(dev-python/docutils-0.23:0/0::gentoo, ebuild scheduled for merge) depends on
 (dev-python/pillow-12.3.0:0/0::gentoo, ebuild scheduled for merge) (buildtime)
  (media-libs/harfbuzz-12.3.2:0/6.0.0::gentoo, ebuild scheduled for merge) (buildtime_slot_op)
   (dev-libs/glib-2.88.2:2/2::gentoo, ebuild scheduled for merge) (buildtime)
    (dev-python/docutils-0.23:0/0::gentoo, ebuild scheduled for merge) (buildtime)

It might be possible to break this cycle
by applying the following change:
- dev-python/pillow-12.3.0 (Change USE: -truetype)

Note that this change can be reverted, once the package has been installed.
"""

ANY_OF = CYCLE.replace("by applying the following change:\n- dev-python/pillow-12.3.0 (Change USE: -truetype)",
                       "by applying any of the following changes:\n- media-libs/harfbuzz-12.3.2 (Change USE: -glib)\n"
                       "- dev-python/pillow-12.3.0 (Change USE: -truetype)")


class SuggestionTests(unittest.TestCase):
    def test_emerges_own_suggestion_is_read(self) -> None:
        self.assertEqual(cycle_break_suggestion(CYCLE), ("dev-python/pillow-12.3.0", ["-truetype"]))

    def test_the_first_of_several_suggestions_is_used(self) -> None:
        self.assertEqual(cycle_break_suggestion(ANY_OF), ("media-libs/harfbuzz-12.3.2", ["-glib"]))

    def test_no_cycle_or_no_suggestion_means_nothing_to_break(self) -> None:
        self.assertIsNone(cycle_break_suggestion("!!! All ebuilds that could satisfy \"x/y\" have been masked."))
        self.assertIsNone(cycle_break_suggestion(CYCLE.split("It might be possible")[0]))
        self.assertIsNone(cycle_break_suggestion(CYCLE.replace("(Change USE: -truetype)", "(Change USE: -tr;ue)")))

    def test_package_names_are_taken_from_versioned_atoms(self) -> None:
        self.assertEqual(package_of("dev-python/pillow-12.3.0"), "dev-python/pillow")
        self.assertEqual(package_of("sys-apps/util-linux-2.42.3-r1"), "sys-apps/util-linux")
        self.assertEqual(package_of("sys-firmware/ipw2100-firmware-1.3-r8"), "sys-firmware/ipw2100-firmware")


class BreakingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runner = ScriptedRunner()
        self.configurer = configurer(self.root, self.runner)
        self.notes: list[str] = []
        self.configurer.notify = self.notes.append
        self.written: list[str] = []
        original = self.configurer.write

        def write(relative, content, mode=0o644):
            self.written.append(f"{relative}: {content}")
            original(relative, content, mode)

        self.configurer.write = write  # type: ignore[method-assign]

    def emerges(self) -> list[str]:
        prefix = f"chroot {self.root} emerge "
        return [line.removeprefix(prefix) for line in self.runner.lines() if line.startswith(prefix)]

    def test_a_cycle_is_broken_for_that_package_only_then_resolved_again(self) -> None:
        self.runner.script(f"chroot {self.root} emerge --pretend", ("fail", CYCLE), "ok")
        self.configurer.pretend(target_manifest(), ["kde-plasma/plasma-meta"])
        self.assertEqual(self.emerges(), ["--pretend --noreplace --verbose=n kde-plasma/plasma-meta", "--oneshot =dev-python/pillow-12.3.0",
                                          "--pretend --noreplace --verbose=n kde-plasma/plasma-meta"])
        self.assertIn(f"{CYCLE_BREAK_USE}: =dev-python/pillow-12.3.0 -truetype\n", self.written)
        self.assertFalse((self.root / CYCLE_BREAK_USE).exists(), "the temporary USE change must not stay")
        self.assertEqual((self.root / CYCLE_BREAK_RECORD).read_text(), "dev-python/pillow\n")
        self.assertTrue(any("Breaking a dependency cycle" in note for note in self.notes))

    def test_the_desktop_stage_rebuilds_the_package_with_the_configured_flags(self) -> None:
        (self.root / CYCLE_BREAK_RECORD).parent.mkdir(parents=True)
        (self.root / CYCLE_BREAK_RECORD).write_text("dev-python/pillow\n")
        self.assertEqual(self.configurer.restore_cycle_breaks(target_manifest()), [])
        self.assertEqual(self.emerges(), ["--oneshot --update --newuse dev-python/pillow"])
        self.assertFalse((self.root / CYCLE_BREAK_RECORD).exists())

    def test_a_failed_rebuild_is_reported_not_fatal(self) -> None:
        (self.root / CYCLE_BREAK_RECORD).parent.mkdir(parents=True)
        (self.root / CYCLE_BREAK_RECORD).write_text("dev-python/pillow\n")
        self.runner.script(f"chroot {self.root} emerge --oneshot", ("fail", " * ERROR: dev-python/pillow-12.3.0::gentoo failed (compile phase):"))
        [problem] = self.configurer.restore_cycle_breaks(target_manifest())
        self.assertIn("emerge --oneshot --newuse dev-python/pillow", problem)

    def test_a_cycle_that_keeps_coming_back_stops_with_the_resolution_error(self) -> None:
        self.runner.script(f"chroot {self.root} emerge --pretend", *[("fail", CYCLE)] * (MAX_CYCLE_BREAKS + 1))
        with self.assertRaises(PackageInstallError) as context:
            self.configurer.pretend(target_manifest(), ["kde-plasma/plasma-meta"])
        self.assertTrue(context.exception.resolution)
        self.assertEqual(sum(line.startswith("--oneshot =") for line in self.emerges()), MAX_CYCLE_BREAKS)

    def test_a_failed_break_build_is_a_resolution_error_and_cleans_up(self) -> None:
        self.runner.script(f"chroot {self.root} emerge --pretend", ("fail", CYCLE))
        self.runner.script(f"chroot {self.root} emerge --oneshot", ("fail", " * ERROR: dev-python/pillow-12.3.0::gentoo failed (compile phase):"))
        with self.assertRaises(PackageInstallError) as context:
            self.configurer.pretend(target_manifest(), ["kde-plasma/plasma-meta"])
        self.assertIn("could not be broken", str(context.exception))
        self.assertFalse((self.root / CYCLE_BREAK_USE).exists())


class QtWebEngineTests(unittest.TestCase):
    def test_bindist_matches_the_binary_packages_in_both_modes_and_the_user_decides_last(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            for binhost in (True, False):
                manifest = target_manifest()
                manifest.portage.binhost = binhost
                self.assertIn("bindist", c.package_use_table(manifest)["dev-qt/qtwebengine"])
            manifest = target_manifest()
            manifest.portage.package_use = {"dev-qt/qtwebengine": ["-bindist"]}
            self.assertEqual(c.package_use_table(manifest)["dev-qt/qtwebengine"], ["-bindist"])
