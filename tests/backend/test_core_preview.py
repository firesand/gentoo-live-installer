"""CORE-06: configure.preview_files returns the text the daemon writes (golden), for every file it previews."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gentoo_installer import layout
from gentoo_installerd import preview
from gentoo_installerd.configure import Configurer, preview_files, render_fstab
from gentoo_installerd.runner import DryRunRunner
from gentoo_installerd.storage import LayoutEntry

from tests.backend.target_helpers import FakePlan, target_manifest


class PreviewTests(unittest.TestCase):
    def written(self, manifest, plan=None) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configurer = Configurer(DryRunRunner(), root)
            configurer.configure_portage(manifest, plan)
            return {
                preview.MAKE_CONF: (root / "etc/portage/make.conf").read_text(),
                preview.PACKAGE_USE: (root / "etc/portage/package.use/installer").read_text(),
                **({preview.BINREPOS: (root / preview.BINREPOS.lstrip("/")).read_text()} if (root / preview.BINREPOS.lstrip("/")).exists() else {}),
                **({preview.PACKAGE_USE_HARDWARE: (root / "etc/portage/package.use/installer-hardware").read_text()}
                   if (root / "etc/portage/package.use/installer-hardware").exists() else {}),
            }

    def test_portage_files_equal_what_the_daemon_writes(self) -> None:
        manifest = target_manifest(portage={"make_opts": "-j6", "binhost": True, "use_flags": ["X"]}, options={"install_hwmonux": True})
        plan = FakePlan()
        plan.make_conf = {"VIDEO_CARDS": "amdgpu radeonsi"}
        plan.package_use = {"media-libs/mesa": ["vulkan"]}
        shown = preview_files(manifest, plan)
        expected = self.written(manifest, plan)
        self.assertEqual({name: shown[name] for name in expected}, expected)
        self.assertIn(preview.BINREPOS, shown)
        self.assertEqual(set(shown), set(expected))

    def test_no_binhost_means_no_binrepos_preview(self) -> None:
        manifest = target_manifest(portage={"make_opts": "-j2", "binhost": False})
        self.assertNotIn(preview.BINREPOS, preview_files(manifest))
        self.assertEqual(preview_files(manifest)[preview.MAKE_CONF], self.written(manifest)[preview.MAKE_CONF])

    def test_without_a_plan_make_opts_are_sized_like_the_daemon(self) -> None:
        manifest = target_manifest(portage={"make_opts": ""})
        with tempfile.TemporaryDirectory() as directory:
            configurer = Configurer(DryRunRunner(), directory)
            self.assertIn(configurer.effective_make_opts(manifest), preview_files(manifest)[preview.MAKE_CONF])

    def test_fstab_and_partition_table_come_from_the_storage_plan(self) -> None:
        manifest = target_manifest(storage={"mode": "erase", "device": "/dev/sda", "filesystem": "btrfs", "firmware": "uefi", "swap_mib": 4096})
        plan = layout.plan_storage(manifest, None)
        shown = preview_files(manifest, storage_plan=plan)
        table = shown[preview.PARTITIONS]
        self.assertIn("Mount point", table.splitlines()[0])
        self.assertIn("EFI system partition", table)
        self.assertIn("/dev/sda", table)
        fstab = shown[preview.FSTAB]
        self.assertIn("subvol=@,", fstab)
        self.assertIn("subvol=@home,", fstab)
        self.assertIn(preview.FSTAB_UUID_PLACEHOLDER, fstab)

    def test_fstab_equals_the_daemon_renderer_for_the_same_entries(self) -> None:
        manifest = target_manifest()
        entries = [LayoutEntry("/dev/sda1", "/efi", "vfat"), LayoutEntry("/dev/sda2", "/", "ext4")]
        uuids = {"/dev/sda1": "AAAA-BBBB", "/dev/sda2": "1111-2222"}
        shown = preview_files(manifest, layout=entries, uuids=uuids)
        self.assertEqual(shown[preview.FSTAB], render_fstab(entries, uuids))

    def test_the_preview_touches_nothing(self) -> None:
        manifest = target_manifest()
        preview_files(manifest)
        self.assertFalse(Path("/nonexistent-preview-root").exists())


if __name__ == "__main__":
    unittest.main()
