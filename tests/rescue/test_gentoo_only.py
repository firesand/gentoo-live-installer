"""The repair tool supports Gentoo only: any other Linux is listed with one plain sentence, and is never opened, repaired or entered."""

from __future__ import annotations

import pytest

from gentoo_installerd.rescue import api as rescue_api
from gentoo_installerd.rescue.actions import available_actions, build_plan
from gentoo_installerd.rescue.errors import RescueError, Unsupported
from gentoo_installerd.rescue.scan import ScanOptions

from . import lsblk_fixtures as lf
from .flow import mount_through_the_manager, uefi_gentoo_lab

NOT_GENTOO = "Not a Gentoo system - this repair tool only supports Gentoo"
OTHER_SYSTEMS = ["debian", "ubuntu", "fedora", "arch", "opensuse", "alpine", "void", "nixos"]


def scanned(lab) -> dict[str, dict]:
    return {item["device"]: item for item in lab.scanner().scan(ScanOptions()).candidates}


def test_a_system_that_is_not_gentoo_is_listed_by_name_with_the_plain_reason(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)  # Gentoo on the NVMe disk, a Debian on the second disk
    systems = scanned(lab)
    gentoo, debian = systems["/dev/nvme0n1p5"], systems["/dev/sdb1"]
    assert gentoo["supported"] is True and NOT_GENTOO not in gentoo["warnings"]
    assert debian["supported"] is False and debian["warnings"] == [NOT_GENTOO]
    assert debian["os"]["pretty_name"] == "Debian GNU/Linux 13 (trixie)" and debian["os"]["family"] == "generic"  # the person still sees what it is
    assert lab.system.mounted_targets() == []


@pytest.mark.parametrize("distribution", OTHER_SYSTEMS)
def test_every_other_distribution_is_listed_as_not_gentoo(lab_factory, distribution: str) -> None:
    lab = lab_factory(lf.plain_sata_mbr())
    lab.add_tree("/dev/sda1", distribution)
    (candidate,) = lab.scanner().scan(ScanOptions()).candidates
    assert candidate["supported"] is False and candidate["warnings"] == [NOT_GENTOO]
    assert candidate["os"]["family"] == "generic" and candidate["os"]["id"] == distribution.replace("opensuse", "opensuse-tumbleweed")


def test_a_linux_that_says_nothing_about_itself_is_listed_as_not_gentoo(lab_factory) -> None:
    lab = lab_factory(lf.plain_sata_mbr())
    lab.add_tree("/dev/sda1", "unknown")
    (candidate,) = lab.scanner().scan(ScanOptions()).candidates
    assert candidate["supported"] is False and candidate["os"]["pretty_name"] == "Unidentified Linux" and candidate["warnings"] == [NOT_GENTOO]


def test_the_scan_says_when_none_of_the_systems_is_gentoo(lab_factory) -> None:
    lab = lab_factory(lf.plain_sata_mbr())
    lab.add_tree("/dev/sda1", "ubuntu")
    hints = {hint["id"]: hint for hint in lab.scanner().scan(ScanOptions()).hints}
    assert "No Gentoo system was found" in hints["no_gentoo"]["text"]
    mixed, _ = uefi_gentoo_lab(lab_factory)
    assert "no_gentoo" not in {hint["id"] for hint in mixed.scanner().scan(ScanOptions()).hints}  # a Gentoo is there: nothing to explain


def test_a_locked_disk_is_still_pointed_out_when_the_only_system_found_is_not_gentoo(lab_factory) -> None:
    """The Gentoo may be inside the locked volume: the other system must not hide that hint."""
    lab = lab_factory(lf.luks_lvm_ext4())
    lab.add_tree("/dev/nvme0n1p2", "debian")
    hints = {hint["id"] for hint in lab.scanner().scan(ScanOptions()).hints}
    assert {"locked_volumes", "no_gentoo"} <= hints


@pytest.mark.parametrize("by", ["candidate", "device"])
@pytest.mark.parametrize("step", ["dry_run", "mount"])
def test_a_system_that_is_not_gentoo_is_refused_before_anything_is_mounted(lab_factory, by: str, step: str) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager = lab.manager()
    scan = manager.scan(None, ScanOptions())
    chosen = next(item for item in scan["candidates"] if item["device"] == "/dev/sdb1")
    request = {"candidate": chosen["id"], "scan_id": scan["scan_id"]} if by == "candidate" else {"device": "/dev/sdb1"}
    request.update({"dry_run": True} if step == "dry_run" else {"plan_digest": "0" * 64})
    with pytest.raises(RescueError) as err:
        manager.mount(None, request)
    assert err.value.short == "unsupported_system" and err.value.message == NOT_GENTOO
    assert lab.system.mounted_targets() == [] and manager.sessions == {} and not any(lab.system.ro.values())  # the probe mount was undone, no session exists


def test_a_disk_without_any_system_is_refused_with_the_same_sentence(lab_factory) -> None:
    lab = lab_factory(lf.plain_sata_mbr())
    lab.add_tree("/dev/sda1", "data")
    with pytest.raises(RescueError) as err:
        lab.manager().mount(None, {"device": "/dev/sda1", "dry_run": True})
    assert err.value.short == "unsupported_system" and err.value.message == NOT_GENTOO


def test_the_gentoo_next_to_it_is_opened_as_before(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert info["family"] == "gentoo" and "immutable" not in info and manager.session(sid).mode == "ro"
    assert all("/dev/sdb1" not in " ".join(command) for command in lab.system.commands_named("mount")[-3:])  # the other system was only looked at during the scan


def test_a_session_of_another_system_would_still_get_no_repair(lab_factory) -> None:
    """Opening it is refused, but the planner does not rely on that: every repair of a system that is not Gentoo is refused as well."""
    lab, _ = uefi_gentoo_lab(lab_factory)
    session, view = lab.mounted_view("/dev/sdb1")
    manager = lab.manager()
    for action in ("boot.bootloader", "boot.config", "boot.initramfs", "boot.kernel", "boot.esp_fallback", "efi.add", "fstab.fix", "fs.check", "acct.password", "acct.unlock", "pkg.finish", "repair.recommended"):
        with pytest.raises(Unsupported) as err:
            build_plan(manager, session, view, action, {"username": "root"})
        assert err.value.message == NOT_GENTOO, action
    repairs = [spec for spec in available_actions(manager, session, view, []) if not spec.id.startswith("session.")]
    assert repairs and all(not spec.available and spec.reason == NOT_GENTOO for spec in repairs)
    assert not lab.system.chroot_calls


def test_the_daemon_announces_that_it_repairs_gentoo_only(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    caps = rescue_api.RescueApi(lab.manager()).capabilities()
    assert caps["families"] == ["gentoo"] and caps["repairs"] == ["gentoo"]
