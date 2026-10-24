"""Wave 4 (B4): follow-ups of the Wave 3 review for the daemon.

* the resolve gate of the portage stage knows the LVM and RAID tools of a manual layout (TGT-3),
* a crashed job reports truthfully whether it can be resumed (RESUME-1).
"""

from __future__ import annotations

import unittest

from tests.backend.target_helpers import read
from tests.backend.test_server_support import DaemonCase, ScriptedInstaller, scripted, types
from tests.backend.test_w3_target_existing_stack import ARRAYS, LVM_LAYOUT, MD_LAYOUT, VOLUMES, manual_manifest
from tests.backend.test_w3_target_fakes import LayerRunner, StageMixin, lvm_tree, md_tree


class ResolveGateTests(StageMixin, unittest.TestCase):
    def gate(self, tree: str, layout, m=None) -> list[str]:
        installer = self.make(m or manual_manifest(), layout=layout, runner_factory=lambda target: LayerRunner(VOLUMES, ARRAYS))
        self.runner.respond("lsblk --json", tree)
        installer._portage()
        return [line for line in self.chroot_lines() if line.startswith("emerge") and "--pretend" in line]

    def test_lvm2_of_an_existing_volume_group_is_resolved_before_any_disk_time_is_spent(self) -> None:
        pretend = self.gate(lvm_tree(), LVM_LAYOUT)
        self.assertTrue(any("sys-fs/lvm2" in line for line in pretend), pretend)
        # and with the USE flag the kernel stage will build it with: lvm2[-lvm] has no lvm command
        self.assertEqual(read(self.target, "etc/portage/package.use/installer-storage"), "sys-fs/lvm2 lvm\n")

    def test_mdadm_of_an_existing_array_is_resolved_too(self) -> None:
        pretend = self.gate(md_tree(), MD_LAYOUT, manual_manifest("/dev/md0", firmware="uefi"))
        self.assertTrue(any("sys-fs/mdadm" in line for line in pretend), pretend)

    def test_plain_partitions_ask_the_tree_for_nothing_extra(self) -> None:
        installer = self.make()
        installer._portage()
        self.assertFalse([line for line in self.chroot_lines() if "--pretend" in line and ("sys-fs/lvm2" in line or "sys-fs/mdadm" in line)])
        self.assertFalse((self.target / "etc/portage/package.use/installer-storage").exists())

    def test_the_stack_found_here_is_the_one_the_kernel_stage_uses(self) -> None:
        installer = self.make(manual_manifest(), layout=LVM_LAYOUT, runner_factory=lambda target: LayerRunner(VOLUMES, ARRAYS))
        self.runner.respond("lsblk --json", lvm_tree())
        installer._portage()
        looked = [command for command in self.runner.commands if command[:1] == ("lvs",)]
        installer._storage_stack()
        self.assertEqual([command for command in self.runner.commands if command[:1] == ("lvs",)], looked)  # asked once for the whole job


def crashing(resumable):
    def script(installer):
        installer.stage("kernel")
        raise RuntimeError("kaboom")

    return type("Crashing", (ScriptedInstaller,), {"script": staticmethod(script), "instances": [], "_resumable": resumable})


class CrashPathTests(DaemonCase):
    """InstallManager._run: whatever happened inside the installer, the job ends with an error and a completed event that tell the truth."""

    def setUp(self) -> None:
        self.start_daemon()

    def crash(self, resumable) -> list[dict]:
        self.use_installer(crashing(resumable))
        with self.assertLogs("gentoo_installerd.server", level="ERROR"):
            events = self.call(self.start_request())
        self.assertEqual(types(events)[-2:], ["error", "completed"])
        return events

    def test_a_job_that_crashed_after_the_storage_stage_can_be_resumed(self) -> None:
        events = self.crash(lambda installer: True)
        self.assertIs(events[-2]["data"]["resumable"], True)
        self.assertIs(events[-1]["data"]["resumable"], True)

    def test_a_job_that_crashed_before_that_cannot(self) -> None:
        events = self.crash(lambda installer: None)
        self.assertIs(events[-2]["data"]["resumable"], False)
        self.assertIs(events[-1]["data"]["resumable"], False)

    def test_a_job_whose_state_cannot_be_read_is_reported_as_not_resumable_and_the_crash_is_not_hidden(self) -> None:
        def broken(installer):
            raise OSError("state directory is gone")

        events = self.crash(broken)
        self.assertIs(events[-2]["data"]["resumable"], False)
        self.assertIn("kaboom", events[-2]["error"])
        self.assertIs(events[-1]["data"]["success"], False)

    def test_an_installer_that_does_not_say_is_not_resumable(self) -> None:
        def script(installer):
            raise RuntimeError("kaboom")

        self.use_installer(scripted(script))
        with self.assertLogs("gentoo_installerd.server", level="ERROR"):
            events = self.call(self.start_request())
        self.assertIs(events[-2]["data"]["resumable"], False)
        self.assertIs(events[-1]["data"]["resumable"], False)

    def test_a_job_that_finishes_without_saying_so_carries_no_resumable_flag(self) -> None:
        self.use_installer(scripted(lambda installer: (installer.stage("preflight"), True)[1]))
        events = self.call(self.start_request())
        self.assertNotIn("resumable", events[-1]["data"])


if __name__ == "__main__":
    unittest.main()
