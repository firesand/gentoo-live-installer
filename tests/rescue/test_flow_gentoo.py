"""End to end on a Gentoo fake: scan, mount, plan, confirm, run, verify, release."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from gentoo_installerd.rescue.errors import RescueError

from . import fakeroot as fr
from . import lsblk_fixtures as lf
from .fakes import assert_nothing_destructive
from .flow import chroot_commands, mount_through_the_manager, plan_and_run, uefi_gentoo_lab, unmount


def test_scan_mount_diagnose_in_one_go(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert info["mode"] == "ro" and info["family"] == "gentoo" and info["state"] == "mounted_ro" and info["path"].endswith(sid)
    assert [m["mountpoint"] for m in info["mounts"]] == ["/", "/efi"]
    actions = {a["id"]: a for a in info["actions"]}
    assert actions["boot.bootloader"]["available"] and actions["boot.initramfs"]["available"] and actions["session.enter_write"]["available"] and not actions["session.leave_write"]["available"]
    assert info["environment"]["uefi"] is True
    assert [f for f in info["findings"] if f["severity"] == "error"] == []
    assert not any(c[0] == "chroot" or c[:1] == ("sh",) for c in lab.system.commands)  # a read-only session runs nothing of the system (review R9)
    assert not lab.system.chroot_calls
    result = unmount(manager, sid)
    assert result["leftovers"] == [] and lab.system.mounted_targets() == [] and not any(lab.system.ro.values())
    assert_nothing_destructive(lab.system)


def test_the_two_step_mount_refuses_a_digest_that_no_longer_matches(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    unmount(manager, sid)
    from gentoo_installerd.rescue.scan import ScanOptions

    scan = manager.scan(None, ScanOptions())
    candidate = [c for c in scan["candidates"] if c["device"] == "/dev/nvme0n1p5"][0]
    with pytest.raises(RescueError) as info:
        manager.mount(None, {"candidate": candidate["id"], "scan_id": scan["scan_id"], "plan_digest": "0" * 64})
    assert info.value.short == "plan_stale"
    assert lab.system.mounted_targets() == []


def test_regenerating_the_menu_runs_in_the_chroot_with_a_clean_environment(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    (tree / "boot/grub/grub.cfg").unlink()
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert any(f["id"] == "D05" for f in info["findings"]) and "boot.config" in info["recommended"]
    plan, result, op = plan_and_run(manager, sid, "boot.config")
    assert plan["risk"] == "medium" and plan["confirm"]["typed"] == "" and plan["confirm"]["review"] is True
    displays = [step["display"] for step in plan["steps"]]
    assert displays[0].startswith("Check that no terminal") and any("blockdev --setrw" in d for d in displays) and any("mount -t proc proc" in d for d in displays)
    assert any(d.startswith("chroot") and "true" in d for d in displays) and displays[-1].endswith("grub-mkconfig -o /boot/grub/grub.cfg")
    assert result["outcome"] == "success" and all(check["ok"] for check in result["post_checks"])
    root, env, command = lab.system.chroot_calls[-1]
    assert command == ("grub-mkconfig", "-o", "/boot/grub/grub.cfg")
    assert env["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin" and env["HOME"] == "/root" and "PYTHONPATH" not in env and "DISPLAY" not in env
    # the program inside the chroot saw the synthesized command line, never the live one (review R3)
    seen = lab.system.chroot_snapshots[-1]["cmdline"]
    assert "root=live" not in seen and "rd.live" not in seen and fr.ROOT_UUID in seen
    assert manager.session(sid).mode == "rw"
    unmount(manager, sid)
    assert (tree / "boot/grub/grub.cfg").read_text().count("menuentry") == 1  # the repair reached the device
    assert_nothing_destructive(lab.system)


def test_a_plan_needs_its_confirmation(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    (tree / "boot/grub/grub.cfg").unlink()
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan = manager.plan({"session_id": sid, "action_id": "boot.initramfs"})
    base = {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"]}
    for request, code in (
        ({**base, "confirm": False}, "needs_confirmation"),
        ({**base, "digest": "f" * 64, "confirm": True}, "plan_mismatch"),
        ({"session_id": sid, "plan_id": "pdeadbe", "digest": plan["digest"], "confirm": True}, "not_found"),
    ):
        with pytest.raises(RescueError) as info:
            manager.run_plan(manager.new_op("run", sid), request)
        assert info.value.short == code
    assert manager.session(sid).mode == "ro" and not lab.system.chroot_calls  # nothing ran


def test_high_risk_plans_need_the_root_device_name_typed(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan = manager.plan({"session_id": sid, "action_id": "boot.bootloader"})
    assert plan["risk"] == "high" and plan["confirm"]["typed"] == "nvme0n1p5"
    with pytest.raises(RescueError) as info:
        manager.run_plan(manager.new_op("run", sid), {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True, "typed": "sda1"})
    assert info.value.short == "needs_confirmation" and info.value.details["expected"] == "nvme0n1p5"
    # the rejected attempt did not consume the plan; the right phrase works
    result = manager.run_plan(manager.new_op("run", sid), {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True, "typed": "nvme0n1p5"})
    assert result["outcome"] in {"success", "warning"}


def test_a_plan_runs_once_and_goes_stale_when_the_session_changes(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    first = manager.plan({"session_id": sid, "action_id": "boot.initramfs"})
    second = manager.plan({"session_id": sid, "action_id": "boot.config"})
    request = {"session_id": sid, "plan_id": first["plan_id"], "digest": first["digest"], "confirm": True}
    manager.run_plan(manager.new_op("run", sid), request)
    with pytest.raises(RescueError) as info:
        manager.run_plan(manager.new_op("run", sid), request)  # single use
    assert info.value.short == "not_found"
    with pytest.raises(RescueError) as info:
        manager.run_plan(manager.new_op("run", sid), {"session_id": sid, "plan_id": second["plan_id"], "digest": second["digest"], "confirm": True})
    assert info.value.short == "plan_stale"  # the first plan changed the session (read-only -> read-write)


def test_an_expired_plan_is_refused(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan = manager.plan({"session_id": sid, "action_id": "boot.config"})
    session = manager.session(sid)
    session.plans._plans[plan["plan_id"]].expires = 1.0
    with pytest.raises(RescueError) as info:
        manager.run_plan(manager.new_op("run", sid), {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True})
    assert info.value.short == "plan_expired"


def test_stateless_rerun_by_digest_for_separate_cli_invocations(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    (tree / "boot/grub/grub.cfg").unlink()
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    shown = manager.plan({"session_id": sid, "action_id": "boot.config"})
    result = manager.run_plan(manager.new_op("run", sid), {"session_id": sid, "action_id": "boot.config", "params": {}, "digest": shown["digest"], "confirm": True})
    assert result["outcome"] == "success"
    again = manager.plan({"session_id": sid, "action_id": "boot.initramfs"})
    with pytest.raises(RescueError) as info:
        manager.run_plan(manager.new_op("run", sid), {"session_id": sid, "action_id": "boot.initramfs", "params": {}, "digest": shown["digest"], "confirm": True})
    assert info.value.short in {"plan_mismatch", "plan_stale"}
    assert again["action"] == "boot.initramfs"


def test_rebuilding_the_initramfs_with_dracut(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, initramfs=False)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert "D02" in [f["id"] for f in info["findings"]] and info["recommended"][0] == "boot.initramfs"
    plan, result, _ = plan_and_run(manager, sid, "boot.initramfs")
    assert ("dracut", "--force", "--regenerate-all") in chroot_commands(lab) and result["outcome"] == "success"
    assert result["post_checks"][0]["ok"] and result["post_checks"][0]["kind"] == "file_min_size"
    unmount(manager, sid)
    assert (tree / f"boot/initramfs-{fr.GENTOO_KVER}.img").stat().st_size >= 1 << 20


def test_initramfs_for_one_kernel_and_a_generic_image(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, _, _ = plan_and_run(manager, sid, "boot.initramfs", {"kernels": [fr.GENTOO_KVER], "generic": True})
    assert ("dracut", "--force", "--no-hostonly", f"/boot/initramfs-{fr.GENTOO_KVER}.img", fr.GENTOO_KVER) in chroot_commands(lab)
    assert any("drivers for many kinds of hardware" in w for w in plan["warnings"])
    with pytest.raises(RescueError) as info:
        manager.plan({"session_id": sid, "action_id": "boot.initramfs", "params": {"kernels": ["--evil; rm -rf /"]}})
    assert info.value.short == "bad_request"
    with pytest.raises(RescueError):
        manager.plan({"session_id": sid, "action_id": "boot.initramfs", "params": {"kernels": ["9.9.9-not-installed"]}})


def test_reinstalling_grub_for_uefi_keeps_the_existing_id_and_backs_up_the_esp(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "boot.bootloader")
    commands = chroot_commands(lab)
    assert ("grub-install", "--target=x86_64-efi", "--efi-directory=/efi", "--bootloader-id=Gentoo", "--recheck") in commands
    assert ("grub-mkconfig", "-o", "/boot/grub/grub.cfg") in commands
    assert not any("--removable" in c for c in commands)  # a fallback loader already exists, so it is not overwritten
    assert result["outcome"] == "success", result
    unmount(manager, sid)
    backups = list((tree / "var/lib/gentoo-rescue/backups").glob("esp-*.tar.gz"))
    assert len(backups) == 1 and oct(backups[0].stat().st_mode & 0o777) == "0o600"
    import tarfile

    with tarfile.open(backups[0]) as archive:
        assert "EFI/Gentoo/grubx64.efi" in archive.getnames() and "EFI/BOOT/BOOTX64.EFI" in archive.getnames()


def test_grub_removable_fallback_when_asked(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan_and_run(manager, sid, "boot.bootloader", {"removable": "yes"})
    assert ("grub-install", "--target=x86_64-efi", "--efi-directory=/efi", "--removable", "--recheck") in chroot_commands(lab)


def test_a_bios_live_session_installs_for_the_fallback_path_without_nvram(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, uefi=False)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert any(f["id"] == "D10" for f in info["findings"])
    plan, result, _ = plan_and_run(manager, sid, "boot.bootloader")
    install = [c for c in chroot_commands(lab) if c[0] == "grub-install"]
    assert install[0][-1] == "--no-nvram" and any("--removable" in c for c in install)
    assert any("cannot store UEFI boot entries" in w for w in plan["warnings"])


def test_when_the_system_cannot_run_its_programs_the_live_grub_is_used(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    lab.system.chroot_failures["true"] = (126, "chroot: failed to run command 'true': Exec format error")
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "boot.bootloader")
    live = [c for c in lab.system.commands if c[:1] == ("grub-install",)]
    session = manager.session(sid)
    assert live and live[0][1] == f"--boot-directory={session.root}/boot" and f"--efi-directory={session.root}/efi" in live[0]
    assert not [c for c in chroot_commands(lab) if c[0] in {"grub-install", "grub-mkconfig"}]
    assert any("cannot run its own programs" in w for w in result["warnings"])
    skipped = [step for step in result["steps"] if step["status"] == "skipped"]
    assert skipped and result["outcome"] in {"success", "warning"}


def test_a_gpt_disk_without_a_bios_boot_partition_is_not_repartitioned(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, uefi=False)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan = manager.plan({"session_id": sid, "action_id": "boot.bootloader", "params": {"firmware": "bios"}})
    failed = [p for p in plan["preconditions"] if not p["ok"]]
    assert failed and "never repartitions" in failed[0]["text"]
    with pytest.raises(RescueError) as info:
        manager.run_plan(manager.new_op("run", sid), {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True, "typed": "nvme0n1p5"})
    assert info.value.short == "precondition_failed"
    assert not [c for c in chroot_commands(lab) if c[0] == "grub-install"]


def test_bios_grub_goes_to_the_roots_disk_only(lab_factory) -> None:
    lab = lab_factory(lf.debian_cloud(), uefi=False)
    lab.add_tree("/dev/vda1", "gentoo", firmware="bios")
    manager, sid, _ = mount_through_the_manager(lab)
    plan, result, _ = plan_and_run(manager, sid, "boot.bootloader")
    assert ("grub-install", "--target=i386-pc", "--recheck", "/dev/vda") in chroot_commands(lab)
    with pytest.raises(RescueError):
        manager.plan({"session_id": sid, "action_id": "boot.bootloader", "params": {"firmware": "bios", "disk": "/dev/sdz"}})


def test_kernel_reinstall_with_the_installkernel_guard(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, kernel_cmdline=False)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "boot.kernel")
    commands = chroot_commands(lab)
    assert ("emerge", "--ask=n", "--config", f"=sys-kernel/gentoo-kernel-bin-6.18.57") in commands
    assert ("dracut", "--force", "--regenerate-all") in commands and commands[-1] == ("grub-mkconfig", "-o", "/boot/grub/grub.cfg")
    displays = " | ".join(step["display"] for step in plan["steps"])
    assert "05-check-chroot.install" in displays and any("synthesized" in w for w in plan["warnings"])
    unmount(manager, sid)
    assert not (tree / "etc/kernel/preinst.d/05-check-chroot.install").exists()  # the override was temporary


def test_kernel_reinstall_validates_the_package(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    for bad in ("sys-kernel/gentoo-kernel-bin-9.9", "--pretend", "sys-apps/portage-3.0", "x; reboot"):
        with pytest.raises(RescueError) as info:
            manager.plan({"session_id": sid, "action_id": "boot.kernel", "params": {"package": bad}})
        assert info.value.short == "bad_request"


def test_systemd_boot_reinstall_and_the_refusal_of_efistub(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, bootctl=True)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, _, _ = plan_and_run(manager, sid, "boot.bootloader", {"loader": "systemd-boot"})
    assert ("bootctl", "--esp-path=/efi", "install") in chroot_commands(lab)
    with pytest.raises(RescueError) as info:
        manager.plan({"session_id": sid, "action_id": "boot.bootloader", "params": {"loader": "efistub"}})
    assert info.value.short == "unsupported_action"
    with pytest.raises(RescueError) as info:
        manager.plan({"session_id": sid, "action_id": "boot.bootloader", "params": {"loader": "lilo"}})
    assert info.value.short == "bad_request"


LIVE_ARGS = "root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.image cdroot"


def test_live_arguments_in_the_sources_are_removed_before_the_menu_is_regenerated(lab_factory) -> None:
    """Regenerating grub.cfg alone would write the live arguments out again from /etc/default/grub (finding D21)."""
    lab, tree = uefi_gentoo_lab(lab_factory)
    defaults = fr.put(tree, "etc/default/grub", f'GRUB_DEFAULT=0\nGRUB_CMDLINE_LINUX="quiet {LIVE_ARGS}"\nGRUB_CMDLINE_LINUX_DEFAULT="splash"\n# GRUB_CMDLINE_LINUX="{LIVE_ARGS}"\n')
    cfg = tree / "boot/grub/grub.cfg"
    cfg.write_text(cfg.read_text().replace("ro\n", f"ro {LIVE_ARGS}\n"))
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert "D21" in {f["id"] for f in info["findings"]}
    plan = manager.plan({"session_id": sid, "action_id": "boot.config"})
    assert '-GRUB_CMDLINE_LINUX="quiet root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.image cdroot"' in plan["diff"] and '+GRUB_CMDLINE_LINUX="quiet"' in plan["diff"]
    assert "-GRUB_CMDLINE_LINUX_DEFAULT" not in plan["diff"] and "-# GRUB_CMDLINE_LINUX" not in plan["diff"]  # lines without a live argument are not part of the change
    assert any("Arguments of the live system" in w for w in plan["warnings"])
    shown = [step["display"] for step in plan["steps"]]
    assert next(i for i, d in enumerate(shown) if d.startswith("write /etc/default/grub without")) < next(i for i, d in enumerate(shown) if "grub-mkconfig" in d)
    plan, result, _ = plan_and_run(manager, sid, "boot.config")
    assert result["outcome"] == "success" and all(check["ok"] for check in result["post_checks"])
    unmount(manager, sid)
    text = defaults.read_text()
    active = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert 'GRUB_CMDLINE_LINUX="quiet"' in active and 'GRUB_CMDLINE_LINUX_DEFAULT="splash"' in active and "root=live" not in active
    assert f'# GRUB_CMDLINE_LINUX="{LIVE_ARGS}"' in text  # a commented-out line is the user's own business
    assert "root=live" not in cfg.read_text() and "rd.live" not in cfg.read_text()  # the regenerated menu does not carry them either
    backups = list((tree / "etc/default").glob("grub.gentoo-rescue-*"))
    assert len(backups) == 1 and LIVE_ARGS in backups[0].read_text()


def test_a_kernel_command_line_file_with_only_live_arguments_gets_the_root_back(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    (tree / "etc/kernel").mkdir(parents=True, exist_ok=True)
    (tree / "etc/kernel/cmdline").write_text(LIVE_ARGS + "\n")
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan_and_run(manager, sid, "boot.config")
    unmount(manager, sid)
    assert (tree / "etc/kernel/cmdline").read_text() == f"root=UUID={fr.ROOT_UUID}\n"


def test_files_without_live_arguments_are_never_rewritten(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    before = (tree / "etc/default/grub").read_text() if (tree / "etc/default/grub").exists() else ""
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan = manager.plan({"session_id": sid, "action_id": "boot.config"})
    assert plan["diff"] == "" and not any("without the live system" in step["display"] for step in plan["steps"])
    plan_and_run(manager, sid, "boot.config")
    unmount(manager, sid)
    assert ((tree / "etc/default/grub").read_text() if (tree / "etc/default/grub").exists() else "") == before
    assert not list((tree / "etc").rglob("*.gentoo-rescue-*"))


def vdb(tree: Path, cpv: str, inherited: str = "") -> None:
    fr.put(tree, f"var/db/pkg/sys-kernel/{cpv}/CATEGORY", "sys-kernel\n")
    if inherited:
        fr.put(tree, f"var/db/pkg/sys-kernel/{cpv}/INHERITED", inherited + "\n")


def test_a_distribution_kernel_from_an_overlay_is_reinstalled_like_the_standard_ones(lab_factory) -> None:
    """The author's real kernel is sys-kernel/cachyos-kernel (overlay CachyOS-kernels): it inherits kernel-install and kernel-build, so emerge --config works for it."""
    kver = "7.3.0-cachyos-dist"
    lab, tree = uefi_gentoo_lab(lab_factory, kernels=[kver], kernel_pkg="cachyos-kernel")
    import shutil

    shutil.rmtree(tree / "var/db/pkg/sys-kernel/cachyos-kernel-7.3.0")
    vdb(tree, "cachyos-kernel-7.3.0-r1", "toolchain-funcs dist-kernel-utils kernel-install kernel-build")  # a revision does not appear in the kernel release
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert {a["id"]: a for a in info["actions"]}["boot.kernel"]["available"] is True
    plan, result, _ = plan_and_run(manager, sid, "boot.kernel")
    assert ("emerge", "--ask=n", "--config", "=sys-kernel/cachyos-kernel-7.3.0-r1") in chroot_commands(lab) and result["outcome"] == "success"


def test_the_newest_installed_kernel_is_the_one_reinstalled(lab_factory) -> None:
    kernels = ["6.9.12-gentoo-dist", "6.10.1-gentoo-dist"]
    lab, tree = uefi_gentoo_lab(lab_factory, kernels=kernels)
    import shutil

    shutil.rmtree(tree / "var/db/pkg/sys-kernel")
    for cpv in ("gentoo-kernel-6.9.12", "gentoo-kernel-6.10.1", "gentoo-kernel-6.11.2"):  # 6.11.2 was merged but its image is gone from /boot
        vdb(tree, cpv)
    fr.put(tree, "var/db/pkg/sys-kernel/installkernel-68-r1/USE", "dracut grub\n")
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan = manager.plan({"session_id": sid, "action_id": "boot.kernel", "params": {"mode": "remerge"}})
    chosen = [arg for step in plan["steps"] for arg in step.get("argv", []) if arg.startswith("=sys-kernel/")]
    assert chosen == ["=sys-kernel/gentoo-kernel-6.10.1"]  # not 6.9.12 (it sorts last as text) and not 6.11.2 (nothing of it is in /boot)


def test_other_packages_of_the_kernel_category_are_not_kernels(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, genkernel=True)
    import shutil

    shutil.rmtree(tree / "var/db/pkg/sys-kernel")
    for cpv in ("linux-firmware-20261019", "dracut-112", "installkernel-68-r1", "linux-headers-6.18"):
        vdb(tree, cpv, "toolchain-funcs optfeature")
    vdb(tree, "scx-1.1.2", "cargo kernel-install-helper")  # a similar name is not the eclass
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    with pytest.raises(RescueError) as err:
        manager.plan({"session_id": sid, "action_id": "boot.kernel"})
    assert err.value.short == "unsupported_action" and "genkernel" in err.value.message


def test_portage_versions_are_ordered_like_a_person_would() -> None:
    from gentoo_installerd.rescue.adapters.gentoo import version_key

    ordered = ["6.9.12", "6.10.0_rc1", "6.10.0", "6.10.0-r1", "6.10.0_p1", "6.10.1", "6.11.2", "7.0.0"]
    assert sorted(reversed(ordered), key=version_key) == ordered
