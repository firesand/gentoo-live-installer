"""The author's own computer, rebuilt from what it really looks like (anonymized): a Gentoo on a btrfs subvolume, three CachyOS kernels from an overlay, GRUB on an ESP at
/boot/efi, seven drives and a firmware with a real efibootmgr 18 entry list.

Every number below was read from the machine: the sizes in /boot, the dracut and installkernel settings, the fstab, the kernel command line. The test shows that the
program works for the machine it was written for, and (by the false alarms it caught while it was written) that the checks do not depend on tidy fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gentoo_installerd.rescue.errors import RescueError

from . import fakeroot as fr
from . import lsblk_fixtures as lf
from .flow import chroot_commands, mount_through_the_manager, plan_and_run, unmount

DATA = Path(__file__).parent / "data"
ROOT_DEVICE = "/dev/nvme0n1p3"
ROOT_UUID = "de5412a3-678a-49a2-8e1c-25bcb37d0834"
ESP_UUID = "ACB7-F14B"
ESP_PARTUUID = "4d4b688a-2baf-4c6d-8ee8-1ffd5a4e7174"
KERNELS = {"7.2.10-cachyos-dist": (17178624, 26433380, "7.2.10"), "7.2.11-cachyos-dist": (17178624, 26678209, "7.2.11"), "7.3.0-cachyos-dist": (17313792, 26370692, "7.3.0-r1")}
LATEST = "7.3.0-cachyos-dist"
CMDLINE = "ibt=off pcie_aspm=off intel_pstate=passive kernel.split_lock_mitigate=0 mitigations=off nvidia-drm.fbdev=1 nvidia-drm.modeset=1 reboot=acpi acpi=force"
GRUB_DEFAULTS = f'GRUB_DISTRIBUTOR="Gentoo"\nGRUB_CMDLINE_LINUX_DEFAULT="{CMDLINE}"\nGRUB_DISABLE_LINUX_PARTUUID=false\n'
GRUB_CFG = ("### BEGIN /etc/grub.d/10_linux ###\n" + "".join(
    f"menuentry 'Gentoo GNU/Linux, with Linux {kver}' --class gentoo --class gnu-linux --class os $menuentry_id_option 'gnulinux-{kver}-advanced-{ROOT_UUID}' {{\n"
    f"\tinsmod part_gpt\n\tinsmod btrfs\n\tsearch --no-floppy --fs-uuid --set=root {ROOT_UUID}\n\techo\t'Loading Linux {kver} ...'\n"
    f"\tlinux\t/root/boot/vmlinuz-{kver} root=UUID={ROOT_UUID} ro rootflags=subvol=root {CMDLINE}\n"
    f"\techo\t'Loading initial ramdisk ...'\n\tinitrd\t/root/boot/amd-uc.img /root/boot/intel-uc.img /root/boot/initramfs-{kver}.img\n}}\n" for kver in KERNELS) + "### END /etc/grub.d/10_linux ###\n")
DRACUT_VENDOR = 'hostonly="yes"\nhostonly_mode="sloppy"\nhostonly_cmdline="no"\nukify="no"\n'
INSTALL_CONF = "# This file is managed by sys-kernel/installkernel\nlayout=grub\ninitrd_generator=dracut\nuki_generator=none\n"
EFIBOOTMGR = {
    "entries": {
        "0001": {"label": "Gentoo", "loader": "\\EFI\\gentoo\\grubx64.efi", "active": True, "partuuid": ESP_PARTUUID, "part": 1, "disk": "/dev/nvme0n1"},
        "0004": {"label": "UEFI OS", "loader": "\\EFI\\BOOT\\BOOTX64.EFI", "active": True, "partuuid": ESP_PARTUUID, "part": 1, "disk": "/dev/nvme0n1", "data": "0000424f"},
    },
    "order": ["0001", "0004"], "current": "", "next": "", "usable": True,
}


def author_lab(lab_factory, tmp_path: Path, *, btrfs_default: str = "ID 256 gen 4000 top level 5 path root"):
    """The machine as it is seen from a live medium: nothing is mounted, the root is the subvolume 'root' of a btrfs file system."""
    data = json.loads(lf.author_pc())
    for disk in data["blockdevices"]:
        for part in disk.get("children", []):
            part["mountpoints"] = []
    lab = lab_factory(json.dumps(data))
    top = tmp_path / "pool-top"
    root = fr.make_root(top / "root", "gentoo", firmware="uefi", esp_mount="/boot/efi", kernels=list(KERNELS), kernel_pkg="cachyos-kernel", kernel_cmdline=False, root_uuid=ROOT_UUID,
                        fstab=(DATA / "author_pc_fstab.txt").read_text(), grub_cfg=False)
    import shutil

    shutil.rmtree(root / "boot")
    shutil.rmtree(root / "var/db/pkg/sys-kernel")
    for kver, (image, initramfs, pkgversion) in KERNELS.items():
        fr.put(root, f"boot/vmlinuz-{kver}", size=image)
        fr.put(root, f"boot/initramfs-{kver}.img", size=initramfs)
        fr.put(root, f"boot/System.map-{kver}", size=11_400_000)
        fr.put(root, f"boot/config-{kver}", size=294_000)
        fr.put(root, f"usr/lib/modules/{kver}/modules.dep", "kernel/fs/btrfs/btrfs.ko:\n")
        fr.put(root, f"usr/src/linux-{kver}/include/config/kernel.release", kver + "\n")
        fr.put(root, f"var/db/pkg/sys-kernel/cachyos-kernel-{pkgversion}/INHERITED", "toolchain-funcs linux-info dist-kernel-utils kernel-install kernel-build optfeature\n")
    fr.put(root, "boot/amd-uc.img", size=306_176)
    fr.put(root, "boot/intel-uc.img", size=29_737_984)
    (root / "boot/kernel-backup").mkdir(mode=0o700)
    (root / "boot/kernel-configs").mkdir(mode=0o700)
    fr.put(root, "boot/grub/grub.cfg", GRUB_CFG)
    (root / "boot/grub/x86_64-efi").mkdir(parents=True)
    fr.put(root, "var/db/pkg/sys-kernel/installkernel-68-r1/USE", "abi_x86_64 amd64 dracut elibc_glibc grub kernel_linux\n")
    fr.put(root, "var/db/pkg/sys-kernel/dracut-112/CATEGORY", "sys-kernel\n")
    fr.put(root, "etc/default/grub", GRUB_DEFAULTS)
    fr.put(root, "usr/lib/dracut/dracut.conf.d/01-gentoo.conf", DRACUT_VENDOR)
    fr.put(root, "usr/lib/kernel/install.conf", INSTALL_CONF)
    (root / "boot/efi").mkdir(exist_ok=True)
    lab.system.attach(ROOT_DEVICE, top)
    lab.system.subvolumes[ROOT_DEVICE] = {5: "", 256: "root"}
    lab.system.btrfs_text[ROOT_DEVICE] = "ID 256 gen 4000 top level 5 path root\n"
    lab.system.btrfs_default[ROOT_DEVICE] = btrfs_default
    esp = fr.esp_tree(tmp_path / "esp", "gentoo", dirname="gentoo")
    fr.put(esp, "EFI/BOOT/BOOTX64.EFI", size=2_600_000)
    lab.system.attach("/dev/nvme0n1p1", esp)
    for device in ("/dev/sdb1", "/dev/sdc1", "/dev/nvme2n1p2", "/dev/nvme1n1p1"):
        lab.add_plain_tree(device, {"data/readme.txt": "data"})
    lab.system.nvram.update(json.loads(json.dumps(EFIBOOTMGR)))
    return lab, root, esp


def mount(lab):
    return mount_through_the_manager(lab, candidate_device=ROOT_DEVICE)


def test_the_machine_is_found_as_one_gentoo_with_its_three_kernels(lab_factory, tmp_path: Path) -> None:
    lab, _root, _esp = author_lab(lab_factory, tmp_path)
    result = lab.scanner().scan(__import__("gentoo_installerd.rescue.scan", fromlist=["ScanOptions"]).ScanOptions())
    found = {item["device"]: item for item in result.candidates}
    assert list(found) == [ROOT_DEVICE]  # six other drives hold data only (and the NTFS one is never mounted)
    candidate = found[ROOT_DEVICE]
    assert (candidate["os"]["id"], candidate["fstype"], candidate["rootflags"], candidate["subvol"]["path"], candidate["init"]) == ("gentoo", "btrfs", "subvolid=256", "root", "openrc")
    assert sorted(k["version"] for k in candidate["boot"]["kernels"]) == sorted(KERNELS)  # kernel-backup and kernel-configs are directories, not kernels
    assert candidate["fstab"]["dead"] == 0
    assert "/dev/sda2" not in {argv[-2] for argv in lab.system.commands_named("mount")}


def test_the_mount_plan_follows_the_real_fstab(lab_factory, tmp_path: Path) -> None:
    lab, _root, _esp = author_lab(lab_factory, tmp_path)
    manager, sid, info = mount(lab)
    assert [(m["mountpoint"], m["device"]) for m in info["mounts"]] == [("/", ROOT_DEVICE), ("/boot/efi", "/dev/nvme0n1p1")]  # the ESP; the data drives are not mounted unless asked for
    session = manager.session(sid)
    items = {item.mountpoint: item for item in session.plan.items}
    assert {mp for mp, item in items.items() if not item.selected} == {"/mnt/steam-library", "/mnt/BackupData", "/mnt/Extern2", "/mnt/Extern3", "/mnt/ExTern"}
    assert items["/mnt/ExTern"].fstype == "ntfs" and items["/mnt/ExTern"].tier == 4 and session.plan.unresolved == []  # offered when asked for, never mounted on its own
    assert not [w for w in session.plan.warnings if "/dev/shm" in w or "portage" in w]  # tmpfs lines of fstab are not devices and not a problem
    root_mount = [argv for argv in lab.system.commands_named("mount") if argv[-1] == session.root][0]
    assert root_mount[2] == "btrfs" and "subvolid=256" in root_mount[4].split(",") and "ro" in root_mount[4].split(",")
    unmount(manager, sid)
    assert not any(lab.system.ro.values())


def test_a_healthy_machine_has_no_findings_that_need_repair(lab_factory, tmp_path: Path) -> None:
    lab, _root, _esp = author_lab(lab_factory, tmp_path)
    manager, sid, info = mount(lab)
    assert [(f["id"], f["title"]) for f in info["findings"] if f["severity"] == "error"] == []
    assert info["recommended"] == []
    actions = {a["id"]: a for a in info["actions"]}
    assert actions["boot.kernel"]["available"] and actions["boot.bootloader"]["available"] and actions["repair.recommended"]["available"] is False
    entries = manager.query({"session_id": sid, "query": "efi.list"})["data"]
    assert [(e["number"], e["label"], e["loader"]) for e in entries["entries"]] == [("0001", "Gentoo", "\\EFI\\gentoo\\grubx64.efi"), ("0004", "UEFI OS", "\\EFI\\BOOT\\BOOTX64.EFI")]
    assert entries["order"] == ["0001", "0004"]
    boot = manager.query({"session_id": sid, "query": "boot.inventory"})["data"]
    assert boot["loader"] == "grub" and boot["esp"]["mountpoint"] == "/boot/efi" and "gentoo" in boot["esp"]["dirs"]
    unmount(manager, sid)


def test_reinstalling_the_kernel_uses_the_overlay_package_and_no_guard(lab_factory, tmp_path: Path) -> None:
    lab, _root, _esp = author_lab(lab_factory, tmp_path)
    manager, sid, _info = mount(lab)
    plan = manager.plan({"session_id": sid, "action_id": "boot.kernel"})
    commands = [step["argv"] for step in plan["steps"] if step.get("argv")]
    assert ["emerge", "--ask=n", "--config", "=sys-kernel/cachyos-kernel-7.3.0-r1"] in commands
    shown = " ".join(step["display"] for step in plan["steps"])
    assert "05-check-chroot" not in shown  # dracut's vendor file says hostonly_cmdline=no and the layout is grub: installkernel has nothing to refuse
    assert plan["risk"] == "high" and plan["confirm"]["typed"] == "nvme0n1p3"
    unmount(manager, sid)


def test_boot_loader_repair_reuses_the_gentoo_directory_and_the_boot_efi_mount(lab_factory, tmp_path: Path) -> None:
    lab, _root, esp = author_lab(lab_factory, tmp_path)
    manager, sid, _info = mount(lab)
    plan, result, _ = plan_and_run(manager, sid, "boot.bootloader")
    assert result["outcome"] == "success", result
    install = [c for c in chroot_commands(lab) if c[0] == "grub-install"][0]
    assert install == ("grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi", "--bootloader-id=gentoo", "--recheck")  # the existing lowercase directory, the ESP where fstab puts it
    assert ("grub-mkconfig", "-o", "/boot/grub/grub.cfg") in chroot_commands(lab)
    assert not [w for w in plan["warnings"] if "fallback" in w.lower()]  # EFI/BOOT/BOOTX64.EFI exists already
    unmount(manager, sid)
    assert (esp / "EFI/gentoo/grubx64.efi").exists()


def test_a_damaged_machine_gets_one_plan_with_only_what_is_broken(lab_factory, tmp_path: Path) -> None:
    lab, root, esp = author_lab(lab_factory, tmp_path)
    (root / "boot/grub/grub.cfg").unlink()
    (root / f"boot/initramfs-{LATEST}.img").write_bytes(b"")  # an interrupted write of the newest initramfs
    (esp / "EFI/gentoo/grubx64.efi").unlink()
    (root / "boot/kernel-backup").rmdir()
    manager, sid, info = mount(lab)
    errors = {f["id"] for f in info["findings"] if f["severity"] == "error"}
    assert {"D03", "D08"} <= errors and "D02" not in errors  # the incomplete initramfs and the missing loader; the two older kernels are fine
    plan = manager.plan({"session_id": sid, "action_id": "repair.recommended"})
    assert plan["params"]["kernels"] == [LATEST]
    dracut = [step["display"] for step in plan["steps"] if "dracut" in step["display"]]
    assert len(dracut) == 1 and dracut[0].endswith(f"dracut --force /boot/initramfs-{LATEST}.img {LATEST}")
    plan, result, _ = plan_and_run(manager, sid, "repair.recommended")
    assert result["outcome"] == "success" and all(check["ok"] for check in result["post_checks"])
    unmount(manager, sid)
    assert (root / f"boot/initramfs-{LATEST}.img").stat().st_size > 1 << 20 and (root / "boot/grub/grub.cfg").stat().st_size > 1024
    assert (esp / "EFI/gentoo/grubx64.efi").exists()


def test_a_later_process_knows_the_btrfs_subvolume_of_the_root(lab_factory, tmp_path: Path) -> None:
    """Every gentoo-rescue command is a new process in --local mode; the command line made for the chroot must still say rootflags=subvol=root."""
    from gentoo_installerd.rescue.session import session_from_state

    lab, _root, _esp = author_lab(lab_factory, tmp_path)
    manager, sid, _info = mount(lab)
    session = manager.session(sid)
    expected = session.derive_cmdline().text
    assert expected == f"{CMDLINE} root=UUID={ROOT_UUID} ro rootflags=subvol=root"  # the arguments of /etc/default/grub plus what grub-mkconfig adds: what the machine really booted with
    adopted = session_from_state(lab.session_env(), json.loads(session.state_path.read_text()))
    assert adopted.derive_cmdline().text == expected
    unmount(manager, sid)
