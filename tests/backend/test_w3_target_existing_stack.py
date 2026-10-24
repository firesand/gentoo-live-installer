"""TGT-3 and STOR-3: a root (or /boot, /home, swap) on an existing LVM volume or md array has to boot.

A manual layout may sit on a volume group or array the user activated before the installation. Nothing in the
manifest says so (storage.lvm and storage.raid_level mean "create"), so the installer looks at the device tree:
lvm2 or mdadm must be in the target before dracut runs, the initramfs needs its module and the arguments that
activate the root, and OpenRC needs the services that activate the other volumes.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gentoo_installer import blockdev
from gentoo_installer.manifest import Partition
from gentoo_installerd.configure import ExistingStack
from gentoo_installerd.install import RECEIPT_PATH, InstallError, Installer
from gentoo_installerd.storage import LayoutEntry

from tests.backend.target_helpers import configurer, read, target_manifest
from tests.backend.test_w3_target_fakes import (MD_UUID, ROOT_FS_UUID, SWAP_UUID, LayerRunner, StageMixin, lvm_tree, md_tree, multipath_tree)

LVM_LAYOUT = [LayoutEntry("/dev/mapper/vg0-root", "/", "ext4"), LayoutEntry("/dev/sda2", "/boot", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat"),
              LayoutEntry("/dev/mapper/vg0-home", "/home", "ext4"), LayoutEntry("/dev/mapper/vg0-swap", "", "swap")]
MD_LAYOUT = [LayoutEntry("/dev/md0", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")]
VOLUMES = {"/dev/mapper/vg0-root": ("vg0", "root", "linear"), "/dev/mapper/vg0-home": ("vg0", "home", "linear"), "/dev/mapper/vg0-swap": ("vg0", "swap", "linear")}
ARRAYS = {"/dev/md0": {"uuid": MD_UUID, "level": "raid1", "members": ["/dev/sda3", "/dev/sdb3"]}}


def detect(tree: str, layout, *, volumes=None, arrays=None, resume: str | None = None, dry_run: bool = False) -> tuple[ExistingStack, LayerRunner]:
    runner = LayerRunner(volumes if volumes is not None else VOLUMES, arrays if arrays is not None else ARRAYS)
    with tempfile.TemporaryDirectory() as temporary:
        stack = configurer(temporary, runner, dry_run=dry_run).detect_stack(layout, blockdev.parse_lsblk(tree), resume)
    return stack, runner


class DetectionTests(unittest.TestCase):
    def test_an_lvm_root_is_found_with_its_volume_group_and_name(self) -> None:
        stack, _ = detect(lvm_tree(), LVM_LAYOUT)
        self.assertTrue(stack.lvm and stack.root_lvm)
        self.assertFalse(stack.md or stack.thin)
        self.assertEqual(stack.lvs, ("vg0/root",))     # /home and the swap are not needed to reach the root
        self.assertEqual(stack.unsupported, "")

    def test_the_swap_to_resume_from_is_needed_early_too(self) -> None:
        stack, _ = detect(lvm_tree(), LVM_LAYOUT, resume="/dev/mapper/vg0-swap")
        self.assertEqual(stack.lvs, ("vg0/root", "vg0/swap"))

    def test_thin_volumes_are_noticed(self) -> None:
        stack, _ = detect(lvm_tree(thin=True), [LayoutEntry("/dev/mapper/vg0-root", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")],
                          volumes={"/dev/mapper/vg0-root": ("vg0", "root", "thin")})
        self.assertTrue(stack.lvm and stack.thin)
        self.assertEqual(stack.lvs, ("vg0/root",))

    def test_an_md_root_is_found_with_the_array_uuid_dracut_wants(self) -> None:
        stack, runner = detect(md_tree(), MD_LAYOUT)
        self.assertTrue(stack.md)
        self.assertFalse(stack.lvm)
        self.assertEqual(stack.root_md, "1")
        self.assertEqual(stack.md_uuids, (MD_UUID,))
        self.assertEqual(stack.arrays, (f"ARRAY /dev/md0 metadata=1.2 name=host:0 UUID={MD_UUID}",))
        # the UUID comes from a member, as dracut's own module does it (a firmware-RAID volume has another UUID than its container)
        self.assertIn(("mdadm", "--examine", "--export", "/dev/sda3"), runner.commands)

    def test_a_volume_on_an_array_needs_both(self) -> None:
        stack, _ = detect(md_tree(lvm_on_top=True), [LayoutEntry("/dev/mapper/vg0-root", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")],
                          volumes={"/dev/mapper/vg0-root": ("vg0", "root", "linear")})
        self.assertTrue(stack.lvm and stack.md and stack.root_lvm)
        self.assertEqual(stack.root_md, "1")
        self.assertEqual((stack.lvs, stack.md_uuids), (("vg0/root",), (MD_UUID,)))

    def test_home_alone_on_a_volume_needs_the_tools_but_not_the_early_boot(self) -> None:
        layout = [LayoutEntry("/dev/sda2", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat"), LayoutEntry("/dev/mapper/vg0-home", "/home", "ext4")]
        stack, _ = detect(lvm_tree(), layout)
        self.assertTrue(stack.lvm)
        self.assertFalse(stack.root_lvm)
        self.assertEqual(stack.lvs, ())

    def test_plain_partitions_run_no_lvm_or_mdadm_command(self) -> None:
        tree = lvm_tree()
        stack, runner = detect(tree, [LayoutEntry("/dev/sda2", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")])
        self.assertEqual(stack, ExistingStack())
        self.assertEqual(runner.commands, [])

    def test_a_root_on_multipath_is_reported(self) -> None:
        stack, _ = detect(multipath_tree(), [LayoutEntry("/dev/mapper/mpatha2", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")])
        self.assertIn("mpath", stack.unsupported)
        self.assertIn("LVM", stack.unsupported)     # says what does work

    def test_a_failing_lvs_falls_back_to_the_name_of_the_mapper_device(self) -> None:
        tree = lvm_tree().replace("vg0-root", "my--vg-root")
        layout = [LayoutEntry("/dev/mapper/my--vg-root", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")]
        stack, _ = detect(tree, layout, volumes={})
        self.assertEqual(stack.lvs, ("my-vg/root",))

    def test_names_that_are_not_plain_text_never_reach_the_command_line(self) -> None:
        layout = [LayoutEntry("/dev/mapper/vg0-root", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")]
        stack, _ = detect(lvm_tree(), layout, volumes={"/dev/mapper/vg0-root": ("v$g;x", "root", "linear")})
        self.assertEqual(stack.lvs, ("vg0/root",))     # the name of the mapper device is used instead
        runner = LayerRunner(VOLUMES, {"/dev/md0": {"uuid": "not a uuid", "level": "raid1", "members": ["/dev/sda3"]}})
        with tempfile.TemporaryDirectory() as temporary:
            stack = configurer(temporary, runner).detect_stack(MD_LAYOUT, blockdev.parse_lsblk(md_tree()))
        self.assertEqual(stack.md_uuids, ())
        self.assertTrue(stack.md and stack.root_md == "1")
        runner.arrays["/dev/md0"]["uuid"] = MD_UUID + "\x07"
        with tempfile.TemporaryDirectory() as temporary:
            stack = configurer(temporary, runner).detect_stack(MD_LAYOUT, blockdev.parse_lsblk(md_tree()))
        self.assertEqual((stack.md_uuids, stack.arrays), ((), ()))

    def test_a_preview_looks_at_nothing(self) -> None:
        stack, runner = detect(lvm_tree(), LVM_LAYOUT, dry_run=True)
        self.assertEqual(stack, ExistingStack())
        self.assertEqual(runner.commands, [])

    def test_a_device_the_listing_does_not_know_is_skipped(self) -> None:
        stack, _ = detect(lvm_tree(), [LayoutEntry("/dev/sdz9", "/", "ext4")])
        self.assertEqual(stack, ExistingStack())


class ConfigurerStackTests(unittest.TestCase):
    LVM = ExistingStack(lvm=True, root_lvm=True, lvs=("vg0/root",))
    MD = ExistingStack(md=True, root_md="1", md_uuids=(MD_UUID,), arrays=(f"ARRAY /dev/md0 metadata=1.2 UUID={MD_UUID}",))

    def configure_initramfs(self, m, stack, device="/dev/mapper/vg0-root"):
        runner = LayerRunner()
        runner.respond(f"blkid -s UUID -o value {device}", ROOT_FS_UUID + "\n")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        configurer(root, runner).configure_initramfs(m, device, None, None, stack=stack)
        return root

    def test_the_tools_come_before_the_initramfs(self) -> None:
        c = configurer(tempfile.gettempdir())
        m = target_manifest()
        self.assertEqual(c.kernel_stage_atoms(m, (), None, self.LVM)[:1], ["sys-fs/lvm2"])
        self.assertIn("sys-fs/mdadm", c.kernel_stage_atoms(m, (), None, self.MD))
        self.assertNotIn("sys-fs/lvm2", c.kernel_stage_atoms(m, (), None, self.MD))
        self.assertEqual(c.kernel_stage_atoms(m, (), None, ExistingStack()), c.kernel_stage_atoms(m))

    def test_lvm_roots_get_the_module_and_the_activation_argument(self) -> None:
        root = self.configure_initramfs(target_manifest(storage={"mode": "manual"}), self.LVM)
        self.assertEqual(read(root, "etc/kernel/cmdline"), f"root=UUID={ROOT_FS_UUID} rw rd.lvm.lv=vg0/root\n")
        self.assertIn('add_dracutmodules+=" lvm "', read(root, "etc/dracut.conf.d/gentoo-installer.conf"))
        self.assertEqual(read(root, "etc/default/grub"), "GRUB_CMDLINE_LINUX=rd.lvm.lv=vg0/root\n")

    def test_md_roots_get_the_module_and_the_array_uuid(self) -> None:
        root = self.configure_initramfs(target_manifest(storage={"mode": "manual"}), self.MD, "/dev/md0")
        self.assertEqual(read(root, "etc/kernel/cmdline"), f"root=UUID={ROOT_FS_UUID} rw rd.md.uuid={MD_UUID}\n")
        self.assertIn('add_dracutmodules+=" mdraid "', read(root, "etc/dracut.conf.d/gentoo-installer.conf"))

    def test_the_installers_own_volume_is_not_listed_twice(self) -> None:
        stack = ExistingStack(lvm=True, root_lvm=True, lvs=("gentoo/root",))
        root = self.configure_initramfs(target_manifest(storage={"lvm": True}), stack, "/dev/gentoo/root")
        self.assertEqual(read(root, "etc/kernel/cmdline").split().count("rd.lvm.lv=gentoo/root"), 1)
        self.assertEqual(read(root, "etc/dracut.conf.d/gentoo-installer.conf").count("lvm"), 1)

    def test_the_raid_flag_gets_the_array_uuid_too(self) -> None:
        m = target_manifest(storage={"mode": "manual", "raid_level": "1", "device": "/dev/md0"})
        root = self.configure_initramfs(m, self.MD, "/dev/md0")
        self.assertIn(f"rd.md.uuid={MD_UUID}", read(root, "etc/kernel/cmdline"))
        self.assertEqual(read(root, "etc/dracut.conf.d/gentoo-installer.conf").count("mdraid"), 1)

    def test_no_stack_changes_nothing(self) -> None:
        root = self.configure_initramfs(target_manifest(), ExistingStack(), "/dev/sda3")
        self.assertEqual(read(root, "etc/kernel/cmdline"), f"root=UUID={ROOT_FS_UUID} rw\n")

    def test_volume_support_is_switched_on_before_lvm2_is_built(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            c.prepare_stack_packages(ExistingStack(lvm=True, root_lvm=True))
            self.assertEqual(read(Path(temporary), "etc/portage/package.use/installer-storage"), "sys-fs/lvm2 lvm\n")
            c.prepare_stack_packages(ExistingStack(lvm=True, thin=True))
            self.assertEqual(read(Path(temporary), "etc/portage/package.use/installer-storage"), "sys-fs/lvm2 lvm thin\n")

    def test_an_array_only_stack_writes_no_use_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configurer(temporary).prepare_stack_packages(self.MD)
            self.assertFalse(Path(temporary, "etc/portage/package.use/installer-storage").exists())

    def test_openrc_activates_the_other_volumes_at_boot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = LayerRunner()
            root = Path(temporary)
            for name in ("lvm", "mdraid"):
                (root / "etc/init.d").mkdir(parents=True, exist_ok=True)
                (root / "etc/init.d" / name).write_text("#!/sbin/openrc-run\n")
            configurer(root, runner).finish_stack(target_manifest(), ExistingStack(lvm=True, md=True, arrays=self.MD.arrays))
            self.assertIn(("chroot", temporary, "rc-update", "add", "lvm", "boot"), runner.commands)
            self.assertIn(("chroot", temporary, "rc-update", "add", "mdraid", "boot"), runner.commands)
            self.assertEqual(read(root, "etc/mdadm.conf").splitlines()[1:], list(self.MD.arrays))

    def test_systemd_needs_no_service_for_either(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = LayerRunner()
            m = target_manifest(system={"init": "systemd", "stage_variant": "amd64-systemd", "profile": "default/linux/amd64/23.0/systemd"})
            configurer(temporary, runner).finish_stack(m, ExistingStack(lvm=True, md=True, arrays=self.MD.arrays))
            self.assertEqual(runner.commands, [])
            self.assertTrue(Path(temporary, "etc/mdadm.conf").is_file())

    def test_a_missing_init_script_is_a_warning_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = LayerRunner()
            c = configurer(temporary, runner)
            notes: list[str] = []
            c.notify = notes.append
            c.finish_stack(target_manifest(), ExistingStack(lvm=True))
            self.assertEqual(runner.commands, [])
            self.assertTrue(any("lvm" in note and "not enabled" in note for note in notes))

    def test_the_receipt_says_what_the_root_sits_on(self) -> None:
        c = configurer(tempfile.gettempdir())
        lvm = c.build_receipt(target_manifest(), root_uuid="u", stack=self.LVM)["root"]
        self.assertEqual((lvm["lvm"], lvm["raid"]), (True, ""))
        md = c.build_receipt(target_manifest(), root_uuid="u", stack=self.MD)["root"]
        self.assertEqual((md["lvm"], md["raid"]), (False, "1"))
        plain = c.build_receipt(target_manifest(), root_uuid="u")["root"]
        self.assertEqual((plain["lvm"], plain["raid"]), (False, ""))
        home_only = c.build_receipt(target_manifest(), root_uuid="u", stack=ExistingStack(lvm=True))["root"]
        self.assertFalse(home_only["lvm"])           # the root itself is a plain partition

    def test_the_flags_still_describe_the_volumes_the_installer_creates(self) -> None:
        c = configurer(tempfile.gettempdir())
        root = c.build_receipt(target_manifest(storage={"lvm": True, "raid_level": "1"}), root_uuid="u", stack=ExistingStack())["root"]
        self.assertEqual((root["lvm"], root["raid"]), (True, "1"))


class VerifyBootTests(unittest.TestCase):
    def verify(self, config: str, stack: ExistingStack, m=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "boot/grub").mkdir(parents=True)
            (root / "boot/vmlinuz-1").write_bytes(b"k")
            (root / "boot/initramfs-1.img").write_bytes(b"i")
            (root / "boot/grub/grub.cfg").write_text(config)
            (root / "etc").mkdir()
            (root / "etc/fstab").write_text("UUID=u / ext4 defaults 0 1\n")
            (root / "efi/EFI/Gentoo").mkdir(parents=True)
            (root / "efi/EFI/Gentoo/grubx64.efi").write_bytes(b"g")
            return configurer(root).verify_boot_setup(m or target_manifest(), "/dev/sda3", root_uuid="u", stack=stack)

    def test_the_menu_must_activate_the_volume_or_array_the_root_is_on(self) -> None:
        plain = "menuentry 'g' {\n linux /vmlinuz-1 root=UUID=u ro\n}\n"
        self.assertEqual(self.verify(plain, ExistingStack()), [])
        problems = self.verify(plain, ExistingStack(lvm=True, root_lvm=True, lvs=("vg0/root",)))
        self.assertEqual(len(problems), 1)
        self.assertIn("rd.lvm.lv", problems[0])
        problems = self.verify(plain, ExistingStack(md=True, root_md="1", md_uuids=(MD_UUID,)))
        self.assertIn("rd.md.uuid", problems[0])
        good = "menuentry 'g' {\n linux /vmlinuz-1 root=UUID=u ro rd.lvm.lv=vg0/root rd.md.uuid=x\n}\n"
        self.assertEqual(self.verify(good, ExistingStack(lvm=True, md=True, root_lvm=True, lvs=("vg0/root",), md_uuids=(MD_UUID,))), [])

    def test_a_volume_that_only_holds_home_asks_nothing_of_the_menu(self) -> None:
        plain = "menuentry 'g' {\n linux /vmlinuz-1 root=UUID=u ro\n}\n"
        self.assertEqual(self.verify(plain, ExistingStack(lvm=True)), [])


def manual_manifest(root: str = "/dev/mapper/vg0-root", **storage):
    rows = [Partition("/dev/sda1", "/efi", "vfat", format=False), Partition("/dev/sda2", "/boot", "ext4", format=True), Partition(root, "/", "ext4", format=True)]
    return target_manifest(storage={"mode": "manual", "partitions": rows, "device": "/dev/sda", **storage})


class KernelStageStackTests(StageMixin, unittest.TestCase):
    def prepare(self, tree: str, layout, m=None, volumes=None, arrays=None, init_scripts=("lvm", "mdraid")) -> Installer:
        installer = self.make(m or manual_manifest(), layout=layout, runner_factory=lambda target: LayerRunner(VOLUMES if volumes is None else volumes, ARRAYS if arrays is None else arrays))
        self.runner.respond("lsblk --json", tree)
        for device in ("/dev/mapper/vg0-root", "/dev/md0", "/dev/sda2"):
            self.runner.respond(f"blkid -s UUID -o value {device}", ROOT_FS_UUID + "\n")
        self.runner.respond("blkid -s UUID -o value /dev/mapper/vg0-swap", SWAP_UUID + "\n")
        (self.target / "etc/init.d").mkdir(parents=True)
        for name in init_scripts:
            (self.target / "etc/init.d" / name).write_text("#!/sbin/openrc-run\n")
        return installer

    def test_an_lvm_root_gets_lvm2_before_dracut_builds_the_initramfs(self) -> None:
        installer = self.prepare(lvm_tree(), LVM_LAYOUT)
        installer._kernel()
        lines = self.chroot_lines()
        tools = next(i for i, line in enumerate(lines) if line.startswith("emerge") and "sys-fs/lvm2" in line)
        kernel = next(i for i, line in enumerate(lines) if "sys-kernel/installkernel" in line)
        dracut = next(i for i, line in enumerate(lines) if line.startswith("dracut"))
        self.assertLess(tools, kernel)
        self.assertLess(tools, dracut)
        self.assertEqual(read(self.target, "etc/portage/package.use/installer-storage"), "sys-fs/lvm2 lvm\n")
        self.assertEqual(read(self.target, "etc/kernel/cmdline"), f"root=UUID={ROOT_FS_UUID} rw rd.lvm.lv=vg0/root\n")
        self.assertIn('add_dracutmodules+=" lvm "', read(self.target, "etc/dracut.conf.d/gentoo-installer.conf"))
        self.assertIn("rc-update add lvm boot", lines)
        self.assertLess(lines.index("rc-update add lvm boot"), dracut)

    def test_the_use_flag_is_written_before_lvm2_is_emerged(self) -> None:
        class Checking(LayerRunner):
            def run(inner, argv, **kwargs):
                if "sys-fs/lvm2" in argv:
                    inner.use_at_emerge = (self.target / "etc/portage/package.use/installer-storage").read_text()
                return super().run(argv, **kwargs)

        installer = self.make(manual_manifest(), layout=LVM_LAYOUT, runner_factory=lambda target: Checking(VOLUMES, ARRAYS))
        self.runner.respond("lsblk --json", lvm_tree())
        self.runner.respond("blkid -s UUID -o value /dev/mapper/vg0-root", ROOT_FS_UUID + "\n")
        installer._kernel()
        self.assertEqual(self.runner.use_at_emerge, "sys-fs/lvm2 lvm\n")

    def test_an_md_root_gets_mdadm_the_array_uuid_and_a_config_file(self) -> None:
        installer = self.prepare(md_tree(), MD_LAYOUT, m=manual_manifest("/dev/md0", firmware="uefi"))
        installer._kernel()
        lines = self.chroot_lines()
        self.assertTrue(any(line.startswith("emerge") and "sys-fs/mdadm" in line for line in lines))
        self.assertEqual(read(self.target, "etc/kernel/cmdline"), f"root=UUID={ROOT_FS_UUID} rw rd.md.uuid={MD_UUID}\n")
        self.assertIn(f"UUID={MD_UUID}", read(self.target, "etc/mdadm.conf"))
        self.assertIn("rc-update add mdraid boot", lines)

    def test_the_swap_volume_to_resume_from_is_activated_early(self) -> None:
        m = manual_manifest()
        m.hardware.hibernate = True
        installer = self.prepare(lvm_tree(), LVM_LAYOUT, m=m)
        installer._kernel()
        cmdline = read(self.target, "etc/kernel/cmdline")
        self.assertIn(f"rd.lvm.lv=vg0/root rd.lvm.lv=vg0/swap resume=UUID={SWAP_UUID}", cmdline)

    def test_a_root_the_initramfs_cannot_assemble_stops_the_install_before_any_package_work(self) -> None:
        installer = self.prepare(multipath_tree(), [LayoutEntry("/dev/mapper/mpatha2", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")], m=manual_manifest("/dev/mapper/mpatha2"))
        with self.assertRaisesRegex(InstallError, "mpath"):
            installer._kernel()
        self.assertEqual(self.chroot_lines(), [])
        self.assertFalse((self.target / "etc/kernel/cmdline").exists())

    def test_a_guided_install_does_not_even_look(self) -> None:
        runner = LayerRunner()
        runner.respond("blkid -s UUID -o value /dev/sda3", ROOT_FS_UUID + "\n")
        installer = self.make(target_manifest(), runner)
        installer._kernel()
        self.assertFalse(any(line.startswith("lsblk") for line in self.runner.lines()))
        self.assertFalse(any("lvm" in line or "mdadm" in line for line in self.runner.lines()))

    def test_an_unreadable_device_tree_is_logged_and_the_install_goes_on(self) -> None:
        installer = self.prepare("not json", LVM_LAYOUT)
        installer._kernel()
        self.assertTrue(any("device tree" in event.message for event in self.events if event.type == "log"))
        self.assertEqual(read(self.target, "etc/kernel/cmdline"), f"root=UUID={ROOT_FS_UUID} rw\n")


class LaterStageStackTests(StageMixin, unittest.TestCase):
    def prepare(self, tree: str, layout, modules: str = "") -> Installer:
        installer = self.make(manual_manifest(), layout=layout, runner_factory=lambda target: LayerRunner(VOLUMES, ARRAYS))
        self.runner.respond("lsblk --json", tree)
        self.runner.respond("blkid -s UUID -o value /dev/mapper/vg0-root", ROOT_FS_UUID + "\n")
        self.runner.respond("blkid -s UUID -o value /dev/md0", ROOT_FS_UUID + "\n")
        if modules:
            self.runner.respond("chroot", modules)
        (self.target / "boot/grub").mkdir(parents=True)
        (self.target / "boot/vmlinuz-6.12.0").write_bytes(b"kernel")
        (self.target / "boot/initramfs-6.12.0.img").write_bytes(b"initramfs")
        (self.target / "etc/portage").mkdir(parents=True)
        (self.target / "etc/portage/make.conf").write_text("x\n")
        (self.target / "etc/fstab").write_text(f"UUID={ROOT_FS_UUID} / ext4 defaults 0 1\n")
        (self.target / "efi/EFI/Gentoo").mkdir(parents=True)
        (self.target / "efi/EFI/Gentoo/grubx64.efi").write_bytes(b"grub")
        return installer

    def test_the_receipt_of_an_lvm_root_says_lvm(self) -> None:
        installer = self.prepare(lvm_tree(), LVM_LAYOUT)
        installer._bootloader()
        receipt = json.loads((self.target / RECEIPT_PATH).read_text())
        self.assertEqual((receipt["root"]["lvm"], receipt["root"]["raid"]), (True, ""))

    def test_the_receipt_of_an_md_root_says_its_level(self) -> None:
        installer = self.prepare(md_tree(), MD_LAYOUT)
        installer._bootloader()
        receipt = json.loads((self.target / RECEIPT_PATH).read_text())
        self.assertEqual((receipt["root"]["lvm"], receipt["root"]["raid"]), (False, "1"))

    def grub_config(self, arguments: str) -> None:
        (self.target / "boot/grub/grub.cfg").write_text(f"menuentry 'Gentoo' {{\n linux /vmlinuz-6.12.0 root=UUID={ROOT_FS_UUID} ro {arguments}\n}}\n")

    def test_validation_wants_the_lvm_module_in_the_initramfs(self) -> None:
        installer = self.prepare(lvm_tree(), LVM_LAYOUT, modules="dracut modules:\nbash\nrootfs-block\n")
        self.grub_config("rd.lvm.lv=vg0/root")
        with self.assertRaisesRegex(InstallError, "lacks the lvm module"):
            installer._validate()
        self.assertFalse(installer._bootable)

    def test_validation_wants_the_mdraid_module_in_the_initramfs(self) -> None:
        installer = self.prepare(md_tree(), MD_LAYOUT, modules="dracut modules:\nbash\nrootfs-block\n")
        self.grub_config(f"rd.md.uuid={MD_UUID}")
        with self.assertRaisesRegex(InstallError, "lacks the mdraid module"):
            installer._validate()

    def test_validation_passes_when_the_module_and_the_argument_are_there(self) -> None:
        installer = self.prepare(lvm_tree(), LVM_LAYOUT, modules="dracut modules:\nbash\nlvm\nrootfs-block\n")
        self.grub_config("rd.lvm.lv=vg0/root")
        installer._validate()

    def test_validation_notices_a_menu_without_the_activation_argument(self) -> None:
        installer = self.prepare(lvm_tree(), LVM_LAYOUT, modules="dracut modules:\nlvm\n")
        self.grub_config("")
        with self.assertRaisesRegex(InstallError, "rd.lvm.lv"):
            installer._validate()


if __name__ == "__main__":
    unittest.main()
