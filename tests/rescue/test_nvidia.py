"""Hardware R01: NVIDIA diagnostics and repair actions in the chroot (a fake root with a mismatched driver is reported; each action gives the expected commands)."""

from __future__ import annotations

from pathlib import Path

from . import fakeroot as fr
from .fakes import tree_digest
from .flow import chroot_commands, mount_through_the_manager, plan_and_run, uefi_gentoo_lab, unmount

DEFAULTS = 'GRUB_DEFAULT=0\nGRUB_CMDLINE_LINUX_DEFAULT="quiet nvidia-drm.modeset=1 nvidia-drm.fbdev=1"\nGRUB_CMDLINE_LINUX=""\n'
TOOLS = {"emerge", "dracut", "grub-mkconfig"}


def opened(lab_factory, cmdline: str | None = None, **nvidia):
    lab, tree = uefi_gentoo_lab(lab_factory)
    fr.nvidia(tree, **nvidia)
    fr.put(tree, "etc/default/grub", DEFAULTS)
    if cmdline is not None:
        fr.put(tree, "etc/kernel/cmdline", cmdline)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    return lab, tree, manager, sid, info


def ids(info) -> dict[str, dict]:
    return {finding["id"]: finding for finding in info["findings"]}


def available(info) -> dict[str, bool]:
    return {spec["id"]: spec["available"] for spec in info["actions"] if spec["id"].startswith("gfx.")}


def repairs(lab) -> list[tuple[str, ...]]:
    return [command for command in chroot_commands(lab) if command[0] in TOOLS]


def test_a_system_without_nvidia_reports_nothing_and_offers_no_repair(lab_factory) -> None:
    lab, _tree = uefi_gentoo_lab(lab_factory)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert not [f for f in info["findings"] if f["id"].startswith("N")]
    assert not any(available(info).values())
    unmount(manager, sid)


def test_a_matching_driver_is_fine_except_for_the_fbdev_argument(lab_factory) -> None:
    lab, _tree, manager, sid, info = opened(lab_factory)
    assert set(ids(info)) & {"N01", "N02", "N03"} == set()
    assert ids(info)["N04"]["severity"] == "warn" and ids(info)["N04"]["actions"] == ["gfx.nvidia_fbdev_off"]
    assert "/etc/default/grub" in ids(info)["N04"]["evidence"]
    unmount(manager, sid)


def test_a_module_built_for_another_driver_version_is_reported(lab_factory) -> None:
    lab, _tree, manager, sid, info = opened(lab_factory, driver="580.142.00", module="580.126.18")
    finding = ids(info)["N02"]
    assert finding["severity"] == "warn" and "580.142.00" in finding["detail"] and any("580.126.18" in line for line in finding["evidence"])
    assert finding["actions"] == ["gfx.nvidia_rebuild", "gfx.nouveau_switch"]
    unmount(manager, sid)


def test_a_kernel_without_the_module_is_reported(lab_factory) -> None:
    lab, _tree, manager, sid, info = opened(lab_factory, module=None)
    finding = ids(info)["N01"]
    assert fr.GENTOO_KVER in finding["detail"] and "no NVIDIA kernel module" in " ".join(finding["evidence"])
    assert available(info) == {"gfx.nvidia_rebuild": True, "gfx.nvidia_fbdev_off": True, "gfx.nouveau_switch": True}
    unmount(manager, sid)


def test_nouveau_blocked_without_a_driver_is_reported(lab_factory) -> None:
    lab, _tree, manager, sid, info = opened(lab_factory, driver="", module=None, blacklist=True, video_cards="nvidia")
    finding = ids(info)["N03"]
    assert finding["actions"] == ["gfx.nouveau_switch"] and any("blacklist nouveau" in line for line in finding["evidence"])
    assert available(info)["gfx.nvidia_rebuild"] is False  # nothing to rebuild
    unmount(manager, sid)


def test_rebuilding_the_modules_runs_module_rebuild_then_the_initramfs(lab_factory) -> None:
    lab, _tree, manager, sid, info = opened(lab_factory, driver="580.142.00", module="580.126.18")
    plan, result, _ = plan_and_run(manager, sid, "gfx.nvidia_rebuild")
    assert result["outcome"] == "success"
    assert repairs(lab) == [("emerge", "--ask=n", "--oneshot", "@module-rebuild"), ("dracut", "--force", "--regenerate-all")]
    assert plan["needs"]["network"] is False  # the distfile is in /var/cache/distfiles
    unmount(manager, sid)


def test_rebuilding_without_the_distfile_needs_the_network_and_can_remerge(lab_factory) -> None:
    lab, _tree, manager, sid, info = opened(lab_factory, distfile=False)
    plan = manager.plan({"session_id": sid, "action_id": "gfx.nvidia_rebuild", "params": {"mode": "remerge"}})
    assert plan["needs"]["network"] is True
    assert [step["display"] for step in plan["steps"] if "emerge" in step["display"]][-1].endswith("emerge --ask=n --oneshot x11-drivers/nvidia-drivers")
    unmount(manager, sid)


def test_removing_fbdev_edits_the_sources_and_rebuilds_the_menu(lab_factory) -> None:
    lab, tree, manager, sid, info = opened(lab_factory)
    _plan, result, _ = plan_and_run(manager, sid, "gfx.nvidia_fbdev_off")
    assert result["outcome"] == "success"
    unmount(manager, sid)
    text = (tree / "etc/default/grub").read_text()
    assert 'GRUB_CMDLINE_LINUX_DEFAULT="quiet nvidia-drm.modeset=1"' in text and "fbdev" not in text  # only the named argument goes
    assert list((tree / "etc/default").glob("grub.gentoo-rescue-*"))  # the old file is kept beside it
    assert [c for c in repairs(lab) if c[0] != "dracut"] == [("grub-mkconfig", "-o", "/boot/grub/grub.cfg")]


def test_fbdev_off_is_not_offered_when_nothing_sets_it(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    fr.nvidia(tree)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert available(info)["gfx.nvidia_fbdev_off"] is False
    unmount(manager, sid)


def test_switching_to_nouveau_needs_no_network_and_removes_every_nvidia_setting(lab_factory) -> None:
    lab, tree, manager, sid, info = opened(lab_factory, cmdline=f"root=UUID={fr.ROOT_UUID} ro quiet nvidia-drm.fbdev=1 modprobe.blacklist=nouveau\n", blacklist=True)
    plan = manager.plan({"session_id": sid, "action_id": "gfx.nouveau_switch"})
    assert plan["risk"] == "high" and plan["needs"]["network"] is False
    assert '+VIDEO_CARDS="nouveau"' in plan["diff"] and '-VIDEO_CARDS="nvidia"' in plan["diff"]
    assert any("Mesa" in warning for warning in plan["warnings"])
    _plan, result, _ = plan_and_run(manager, sid, "gfx.nouveau_switch")
    assert result["outcome"] == "success"
    unmount(manager, sid)
    make_conf = (tree / "etc/portage/make.conf").read_text()
    assert 'VIDEO_CARDS="nouveau"' in make_conf and 'COMMON_FLAGS="-O2 -pipe"' in make_conf and 'CPU_FLAGS_X86="aes avx2"' in make_conf
    blacklist = (tree / "etc/modprobe.d/blacklist-nouveau.conf").read_text()
    assert "# switched off by the Gentoo Live repair tool: blacklist nouveau" in blacklist and "# keep nouveau away" in blacklist
    grub = (tree / "etc/default/grub").read_text()
    assert "nvidia-drm" not in grub and 'GRUB_CMDLINE_LINUX_DEFAULT="quiet"' in grub
    assert (tree / "etc/kernel/cmdline").read_text() == f"root=UUID={fr.ROOT_UUID} ro quiet\n"
    assert repairs(lab) == [("emerge", "--ask=n", "--unmerge", "x11-drivers/nvidia-drivers"), ("dracut", "--force", "--regenerate-all"), ("grub-mkconfig", "-o", "/boot/grub/grub.cfg")]


def test_switching_is_refused_when_the_kernel_has_no_nouveau(lab_factory) -> None:
    lab, _tree, manager, sid, info = opened(lab_factory, nouveau=False)
    assert available(info)["gfx.nouveau_switch"] is False
    reason = next(spec["reason"] for spec in info["actions"] if spec["id"] == "gfx.nouveau_switch")
    assert "no nouveau driver" in reason
    unmount(manager, sid)


def test_planning_and_diagnosing_run_nothing_and_change_nothing(lab_factory) -> None:
    lab, tree, manager, sid, info = opened(lab_factory, blacklist=True)
    root = Path(manager.session(sid).root)
    before = tree_digest(root)
    for action in ("gfx.nvidia_rebuild", "gfx.nvidia_fbdev_off", "gfx.nouveau_switch"):
        manager.plan({"session_id": sid, "action_id": action})
    assert not repairs(lab) and tree_digest(root) == before
    unmount(manager, sid)
