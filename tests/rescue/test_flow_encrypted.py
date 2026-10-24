"""A laptop with full disk encryption: LUKS2 under LVM, a separate /boot and ESP. From a locked disk to a repaired system and back to a locked disk, through the manager."""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_installerd.rescue.errors import RescueError
from gentoo_installerd.rescue.scan import ScanOptions

from . import fakeroot as fr
from . import lsblk_fixtures as lf
from .fakes import LuksSpec, VgSpec, assert_nothing_destructive
from .flow import plan_and_run, unmount

PASSPHRASE = "correct horse battery staple"
MAPPER = "rescue-1f2e3d4c5b6a"
ROOT_LV = "/dev/mapper/vg0-root"


def encrypted_lab(lab_factory, *, initramfs: bool = True):
    lab = lab_factory(lambda s: lf.luks_lvm_ext4(unlocked=bool(s.mapped), activated=s.vgs["vg0"].active))
    lab.system.luks["/dev/nvme0n1p3"] = LuksSpec(fr.LUKS_UUID, PASSPHRASE)
    lab.system.vgs["vg0"] = VgSpec("vg0", "vguuid-0", [f"/dev/mapper/{MAPPER}"], [{"name": "root", "size": 80 << 30}, {"name": "swap", "size": 8 << 30}])
    fstab = (f"UUID={fr.ROOT_UUID}\t/\text4\tnoatime\t0 1\nUUID={fr.BOOT_UUID}\t/boot\text4\tnoatime\t0 2\nUUID={fr.ESP_UUID}\t/efi\tvfat\tumask=0077\t0 2\n"
             f"UUID={fr.SWAP_UUID}\tnone\tswap\tsw\t0 0\n")
    tree = lab.add_tree(ROOT_LV, "gentoo", firmware="uefi", separate_boot=True, fstab=fstab, kernel_cmdline=False, crypttab=f"cryptroot UUID={fr.LUKS_UUID} none luks\n")
    (tree / "efi").mkdir(exist_ok=True)
    (tree / "boot").mkdir(exist_ok=True)
    fr.put(tree, f"usr/lib/modules/{fr.GENTOO_KVER}/modules.dep", "kernel/fs/ext4/ext4.ko:\n")  # the module tree lives in the root file system, the kernel image in the separate /boot
    boot = lab.tmp / "trees" / "boot"
    fr.kernel_files(boot, fr.GENTOO_KVER, initramfs=initramfs, prefix="vmlinuz-", initrd_prefix="initramfs-", modules=False)
    fr.put(boot, "grub/grub.cfg", fr.grub_cfg(fr.GENTOO_KVER, kernel="vmlinuz").replace("/boot/", "/"))
    lab.system.attach("/dev/nvme0n1p2", boot)
    lab.system.attach("/dev/nvme0n1p1", fr.esp_tree(lab.tmp / "trees" / "esp", "gentoo"))
    return lab, tree, boot


def test_a_locked_disk_is_listed_but_nothing_is_guessed(lab_factory) -> None:
    from gentoo_installerd.rescue.scan import ScanOptions

    lab, _tree, _boot = encrypted_lab(lab_factory)
    manager = lab.manager()
    scan = manager.scan(None, ScanOptions())
    assert scan["candidates"] == [] and "locked_volumes" in {hint["id"] for hint in scan["hints"]}
    assert [(item["device"], item["state"], item["version"]) for item in scan["stacks"]["luks"]] == [("/dev/nvme0n1p3", "locked", 2)]
    assert not lab.system.commands_with("cryptsetup", "open") and lab.system.mapped == {}


def test_from_a_locked_disk_to_a_repaired_system_and_back(lab_factory) -> None:
    lab, tree, boot = encrypted_lab(lab_factory, initramfs=False)
    manager = lab.manager()
    manager.scan(None, ScanOptions())

    unlocked = manager.unlock(None, {"device": "/dev/nvme0n1p3", "passphrase": PASSPHRASE})
    assert unlocked["mapper"] == f"/dev/mapper/{MAPPER}" and unlocked["ours"] is True and unlocked["new_candidates"] == []  # the volume group is not active yet (no automatic activation)
    activated = manager.activate(None, {"kind": "lvm", "vg": "vg0"})
    assert [item["device"] for item in activated["new_candidates"]] == [ROOT_LV]
    candidate = activated["new_candidates"][0]
    assert candidate["stack"] == ["/dev/nvme0n1p3", f"luks:/dev/mapper/{MAPPER}", "lvm:vg0-root"] and candidate["os"]["id"] == "gentoo"

    request = {"candidate": candidate["id"], "scan_id": None}
    scan_id = next(reversed(manager._scans))
    request["scan_id"] = scan_id
    dry = manager.mount(None, {**request, "dry_run": True})
    assert [(item["mountpoint"], item["device"]) for item in dry["mount_plan"]["items"]] == [("/boot", "/dev/nvme0n1p2"), ("/efi", "/dev/nvme0n1p1")]
    info = manager.mount(None, {**request, "plan_digest": dry["mount_plan"]["digest"]})
    sid = info["session_id"]
    session = manager.session(sid)
    assert len(session.depends) == 2  # the session knows it stands on the volume group and the unlocked volume
    assert {f["id"] for f in info["findings"] if f["severity"] == "error"} == {"D02"}  # the kernel in the separate /boot has no initramfs
    assert "D05" not in {f["id"] for f in info["findings"]}  # the menu and the loader on the other partitions were seen

    plan, result, _ = plan_and_run(manager, sid, "boot.initramfs")
    assert result["outcome"] == "success"
    seen = [snapshot["cmdline"] for snapshot in lab.system.chroot_snapshots]
    assert seen and all(f"rd.luks.uuid={fr.LUKS_UUID}" in text and "rd.lvm.vg=vg0" in text and f"root=UUID={fr.ROOT_UUID}" in text and "root=live" not in text for text in seen)  # what dracut sees as /proc/cmdline

    left = unmount(manager, sid)
    assert left["leftovers"] == [] and any("luks" in item for item in left["stack"]) and any("vg" in item for item in left["stack"])
    assert lab.system.mapped == {} and lab.system.vgs["vg0"].active is False and lab.system.mounted_targets() == [] and not any(lab.system.ro.values())
    assert (boot / f"initramfs-{fr.GENTOO_KVER}.img").stat().st_size > 1 << 20  # written to the real /boot partition, not to the root
    assert not (tree / "boot" / f"initramfs-{fr.GENTOO_KVER}.img").exists()
    assert_nothing_destructive(lab.system)
    assert manager.pool_ledger.entries == [] and not manager.pool_path.exists()


def test_a_second_session_keeps_the_stack_open_until_the_last_one_leaves(lab_factory) -> None:
    lab, _tree, _boot = encrypted_lab(lab_factory)
    manager = lab.manager()
    manager.scan(None, ScanOptions())
    manager.unlock(None, {"device": "/dev/nvme0n1p3", "passphrase": PASSPHRASE})
    activated = manager.activate(None, {"kind": "lvm", "vg": "vg0"})
    candidate = activated["new_candidates"][0]
    scan_id = next(reversed(manager._scans))
    request = {"candidate": candidate["id"], "scan_id": scan_id}
    first = manager.mount(None, {**request, "plan_digest": manager.mount(None, {**request, "dry_run": True})["mount_plan"]["digest"]})
    released = manager.unmount(None, {"session_id": first["session_id"]})
    assert released["stack"] != [] and lab.system.mapped == {}
    with pytest.raises(RescueError) as err:  # the stack was released with the last session; a new mount needs it again
        manager.mount(None, {**request, "dry_run": True})
    assert err.value.short == "not_found"


def test_a_wrong_passphrase_changes_nothing_and_a_right_one_is_never_logged(lab_factory) -> None:
    lab, _tree, _boot = encrypted_lab(lab_factory)
    manager = lab.manager()
    for attempt in range(3):
        with pytest.raises(RescueError) as err:
            manager.unlock(None, {"device": "/dev/nvme0n1p3", "passphrase": f"wrong-{attempt}"})
        assert err.value.short == "bad_passphrase" and "keyboard layout" in err.value.message
    assert lab.system.mapped == {} and manager.pool_ledger.entries == []
    manager.unlock(None, {"device": "/dev/nvme0n1p3", "passphrase": PASSPHRASE})
    text = manager.audit.text("jsonl")
    assert PASSPHRASE not in text and "wrong-0" not in text and all(PASSPHRASE not in " ".join(argv) for argv in lab.system.commands)
    manager.unmount(None, {})
    assert lab.system.mapped == {}


def test_a_later_process_builds_the_same_command_line_for_the_encrypted_root(lab_factory) -> None:
    lab, _tree, _boot = encrypted_lab(lab_factory, initramfs=False)
    manager = lab.manager()
    manager.scan(None, ScanOptions())
    manager.unlock(None, {"device": "/dev/nvme0n1p3", "passphrase": PASSPHRASE})
    candidate = manager.activate(None, {"kind": "lvm", "vg": "vg0"})["new_candidates"][0]
    request = {"candidate": candidate["id"], "scan_id": next(reversed(manager._scans))}
    sid = manager.mount(None, {**request, "plan_digest": manager.mount(None, {**request, "dry_run": True})["mount_plan"]["digest"]})["session_id"]
    expected = manager.session(sid).derive_cmdline().text
    later = lab.manager(auto_adopt=True)  # another process: it only has the state files
    adopted = later.session(sid)
    assert adopted.derive_cmdline().text == expected and f"rd.luks.uuid={fr.LUKS_UUID}" in expected and "rd.lvm.vg=vg0" in expected
    plan_and_run(later, sid, "boot.initramfs")
    assert all(f"rd.luks.uuid={fr.LUKS_UUID}" in snapshot["cmdline"] for snapshot in lab.system.chroot_snapshots)
    assert later.unmount(None, {"session_id": sid})["leftovers"] == []
