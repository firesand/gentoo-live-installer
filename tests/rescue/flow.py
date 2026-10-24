"""Drive the manager the way the daemon API does: scan, mount, plan, run, unmount."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gentoo_installerd.rescue.scan import ScanOptions

from . import fakeroot as fr
from . import lsblk_fixtures as lf

if TYPE_CHECKING:
    from .lab import Lab


def uefi_gentoo_lab(lab_factory, family: str = "gentoo", root_device: str = "/dev/nvme0n1p5", **options: Any):
    """A UEFI machine: Windows + Gentoo on one NVMe disk sharing the ESP, plus a second disk with a Debian on it (a system the tool lists but never opens)."""
    data = json.loads(lf.nvme_dualboot())
    data["blockdevices"].append(lf.sata_disk("b", 1, 500 * lf.GiB, [lf.part("sdb", 1, 500 * lf.GiB, 8, 1, fstype="ext4", uuid="22222222-2222-4222-8222-222222222222", parttype=lf.LINUX_FS, pttype="gpt")]))
    lab = lab_factory(json.dumps(data), uefi=options.pop("uefi", True))
    options.setdefault("firmware", "uefi")
    tree = lab.add_tree(root_device, family, **options)
    lab.system.attach("/dev/nvme0n1p1", fr.esp_tree(lab.tmp / "trees" / "esp", family))
    lab.add_tree("/dev/sdb1", "debian")
    return lab, tree


def boot_efi_lab(lab_factory, *, efi_dir: bool = True, loaders: bool = True):
    """A Gentoo on an LVM volume with a separate /boot partition: the ESP is mounted at /boot/efi, a directory that lives on that /boot partition."""
    lab = lab_factory(lf.ubuntu_lvm())
    fstab = f"/dev/mapper/ubuntu--vg-ubuntu--lv / ext4 defaults 0 1\nUUID={fr.BOOT_UUID} /boot ext4 defaults 0 2\nUUID={fr.ESP_UUID} /boot/efi vfat umask=0077 0 1\n"
    tree = lab.add_tree("/dev/mapper/ubuntu--vg-ubuntu--lv", "gentoo", firmware="uefi", separate_boot=True, fstab=fstab)
    (tree / "boot").mkdir(exist_ok=True)  # empty: the real /boot is the partition
    fr.put(tree, f"usr/lib/modules/{fr.GENTOO_KVER}/modules.dep", "kernel/fs/ext4/ext4.ko:\n")
    boot = lab.tmp / "trees" / "boot"
    fr.kernel_files(boot, fr.GENTOO_KVER, prefix="vmlinuz-", modules=False)
    fr.put(boot, "grub/grub.cfg", fr.grub_cfg(fr.GENTOO_KVER, kernel="vmlinuz", initrd="initramfs").replace("/boot/", "/"))
    if efi_dir:
        (boot / "efi").mkdir(parents=True, exist_ok=True)
    lab.system.attach("/dev/sda2", boot)
    lab.system.attach("/dev/sda1", fr.esp_tree(lab.tmp / "trees" / "esp", "gentoo", loaders=loaders, fallback=loaders))
    return lab, tree, boot


def mount_through_the_manager(lab, *, allow_write: bool = True, candidate_device: str | None = None, **extra: Any):
    """scan -> dry run -> mount, exactly as a client would; returns (manager, session id, mount result)."""
    manager = lab.manager()
    scan = manager.scan(None, ScanOptions())
    candidates = [c for c in scan["candidates"] if candidate_device is None or c["device"] == candidate_device]
    chosen = candidates[0]
    request = {"candidate": chosen["id"], "scan_id": scan["scan_id"], "allow_write": allow_write, **extra}
    dry = manager.mount(None, {**request, "dry_run": True})
    info = manager.mount(None, {**request, "plan_digest": dry["mount_plan"]["digest"]})
    return manager, info["session_id"], info


def plan_and_run(manager, sid: str, action: str, params: dict[str, Any] | None = None, *, secrets: dict[str, str] | None = None, typed: bool = True, **run_extra: Any):
    plan = manager.plan({"session_id": sid, "action_id": action, "params": params or {}})
    confirm = plan["confirm"]
    request = {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True, "typed": confirm["typed"] if typed else "", "phrase": confirm["phrase"] if typed else ""}
    if secrets:
        request["secrets"] = secrets
    request.update(run_extra)
    op = manager.new_op("run", sid)
    result = manager.run_plan(op, request)
    return plan, result, op


def chroot_commands(lab) -> list[tuple[str, ...]]:
    return [command for _root, _env, command in lab.system.chroot_calls]


def unmount(manager, sid: str, **extra: Any) -> dict[str, Any]:
    return manager.unmount(None, {"session_id": sid, **extra})


def with_live_mounts(lab: Lab) -> Lab:
    """Make the fake lsblk show what the fake kernel has mounted, as the real one does (the fixtures print a fixed list)."""
    static = lab.system._lsblk
    assert isinstance(static, str)

    def current(system: Any) -> str:
        document = json.loads(static)
        mounts: dict[str, list[str]] = {}
        for target, record in system.mounts.items():
            if not record.bind:
                mounts.setdefault(record.device, []).append(target)

        def walk(nodes: list[dict[str, Any]]) -> None:
            for node in nodes:
                node["mountpoints"] = [*node.get("mountpoints", []), *mounts.get(node["path"], [])]
                walk(node.get("children", []))

        walk(document["blockdevices"])
        return json.dumps(document)

    lab.system._lsblk = current
    return lab
