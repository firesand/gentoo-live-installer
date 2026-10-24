"""B2 (wave 4), the side that applies the plan: the limit for the memory-heavy packages only ever lowers the compile jobs.

The plan sizes the limit from what the probe saw, but the setting in force is MAKEOPTS in the new system's make.conf (sized
from the live memory when the repository stage wrote it). package.env replaces MAKEOPTS for the packages it names, so a limit
that is not below that setting would raise those packages.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gentoo_installerd import hwplan

from tests.backend.target_helpers import configurer, read
from tests.hardware.builders import manifest, profile

ENV = "etc/portage/env/installer-memory"
PACKAGE_ENV = "etc/portage/package.env/installer-hardware"


def small_pc_plan():
    """The plan for the machine of the first acceptance run: it limits the heavy packages to one job and writes a zram setup besides."""
    return hwplan.plan(manifest(portage={"make_opts": "-j2"}, storage={"swap_mib": 512}), profile(ram_gib=3.8, threads=2))


def roomy_pc_plan():
    return hwplan.plan(manifest(storage={"swap_mib": 4096}), profile(ram_gib=16, threads=8))   # four jobs for the heavy packages


class MemoryLimitFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.c = configurer(self.root)
        self.notes: list[str] = []
        self.c.notify = self.notes.append

    def make_conf(self, text: str | None) -> None:
        if text is not None:
            (self.root / "etc/portage").mkdir(parents=True, exist_ok=True)
            (self.root / "etc/portage/make.conf").write_text(text)

    def test_a_limit_below_makeopts_is_written(self) -> None:
        self.make_conf("MAKEOPTS='-j8 -l8'\n")
        self.c.write_plan_files(roomy_pc_plan())
        self.assertEqual(read(self.root, ENV).splitlines()[-1], 'MAKEOPTS="-j4"')
        self.assertIn("dev-python/pyside installer-memory", read(self.root, PACKAGE_ENV))
        self.assertEqual(self.notes, [])

    def test_a_limit_that_is_not_below_makeopts_would_raise_the_packages_and_is_left_out(self) -> None:
        for conf in ("MAKEOPTS=-j4\n", "MAKEOPTS='-j2 -l2'\n", "MAKEOPTS=-j1\n"):
            with self.subTest(conf=conf):
                self.make_conf(conf)
                self.c.write_plan_files(roomy_pc_plan())      # the probe saw more memory than the live system has now
                self.assertFalse((self.root / ENV).exists() or (self.root / PACKAGE_ENV).exists())
                self.assertTrue(any("not below" in note and "MAKEOPTS" in note for note in self.notes), self.notes)
                self.notes.clear()

    def test_the_other_files_of_the_plan_are_not_held_back(self) -> None:
        self.make_conf("MAKEOPTS=-j1\n")
        plan = small_pc_plan()
        self.assertIn("/etc/conf.d/zram-init", {path for path, _content, _mode in plan.files})
        self.c.write_plan_files(plan)
        self.assertTrue((self.root / "etc/conf.d/zram-init").is_file())
        self.assertFalse((self.root / ENV).exists())

    def test_nothing_is_known_about_makeopts_so_the_limit_stays(self) -> None:
        for conf in (None, "MAKEOPTS='-l4'\n", "MAKEOPTS=-j\n", "CFLAGS='-O2'\n"):
            with self.subTest(conf=conf):
                self.make_conf(conf)
                self.c.write_plan_files(roomy_pc_plan())
                self.assertTrue((self.root / ENV).is_file() and (self.root / PACKAGE_ENV).is_file())
                (self.root / ENV).unlink()
                (self.root / PACKAGE_ENV).unlink()

    def test_a_preview_writes_nothing(self) -> None:
        dry = configurer(self.root, dry_run=True)
        dry.write_plan_files(roomy_pc_plan())
        self.assertFalse((self.root / "etc").exists())


if __name__ == "__main__":
    unittest.main()
