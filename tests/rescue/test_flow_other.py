"""Debian family, Fedora family, and the systems that are only detected and diagnosed (Arch, openSUSE, generic)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gentoo_installerd.rescue.errors import RescueError

from . import fakeroot as fr
from . import lsblk_fixtures as lf
from .fakes import assert_nothing_destructive
from .flow import chroot_commands, mount_through_the_manager, plan_and_run, uefi_gentoo_lab, unmount

DEBIAN_ENV = {"DEBIAN_FRONTEND": "noninteractive", "DEBCONF_NONINTERACTIVE_SEEN": "true"}


# ---- Debian and Ubuntu -------------------------------------------------------------------------------------------------------------
def debian_lab(lab_factory, family: str = "debian", **options):
    options.setdefault("esp_mount", "/boot/efi")
    return uefi_gentoo_lab(lab_factory, family, **options)


def test_debian_family_is_identified_and_offers_repairs(lab_factory) -> None:
    lab, _ = debian_lab(lab_factory)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert info["family"] == "debian" and info["os"]["id"] == "debian"
    actions = {a["id"]: a for a in info["actions"]}
    assert all(actions[name]["available"] for name in ("boot.bootloader", "boot.config", "boot.initramfs", "boot.kernel", "fstab.fix") if name != "fstab.fix")
    assert [f for f in info["findings"] if f["severity"] == "error"] == []


def test_debian_initramfs_uses_initramfs_tools_in_a_clean_noninteractive_environment(lab_factory) -> None:
    lab, tree = debian_lab(lab_factory, initramfs=False)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert "D02" in [f["id"] for f in info["findings"]]
    plan, result, _ = plan_and_run(manager, sid, "boot.initramfs")
    assert ("update-initramfs", "-c", "-k", "all") in chroot_commands(lab)
    assert all(env.get("DEBIAN_FRONTEND") == "noninteractive" for _r, env, cmd in lab.system.chroot_calls if cmd[0] == "update-initramfs")
    assert result["outcome"] == "success"
    plan, _, _ = plan_and_run(manager, sid, "boot.initramfs", {"kernels": [fr.DEBIAN_KVER]})
    assert ("update-initramfs", "-u", "-k", fr.DEBIAN_KVER) in chroot_commands(lab)


def test_debian_generic_initramfs_is_temporary_configuration(lab_factory) -> None:
    lab, tree = debian_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "boot.initramfs", {"generic": True})
    displays = [step["display"] for step in plan["steps"]]
    assert any("zz-rescue-generic" in d for d in displays)
    unmount(manager, sid)
    assert not (tree / "etc/initramfs-tools/conf.d/zz-rescue-generic").exists()


def test_debian_boot_menu_is_update_grub(lab_factory) -> None:
    lab, tree = debian_lab(lab_factory)
    (tree / "boot/grub/grub.cfg").unlink()
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan_and_run(manager, sid, "boot.config")
    assert ("update-grub",) in chroot_commands(lab)


def test_debian_secure_boot_chain_is_preserved_by_grub_install(lab_factory) -> None:
    lab, tree = debian_lab(lab_factory, "ubuntu")
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "boot.bootloader")
    install = [c for c in chroot_commands(lab) if c[0] == "grub-install"]
    assert install[0] == ("grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi", "--bootloader-id=ubuntu", "--recheck", "--uefi-secure-boot")
    assert ("update-grub",) in chroot_commands(lab)


def test_debian_without_shim_chain_gets_no_secure_boot_flag(lab_factory) -> None:
    lab, tree = debian_lab(lab_factory, shim=False, firmware="uefi")
    import shutil
    shutil.rmtree(tree / "usr/lib/shim", ignore_errors=True)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan_and_run(manager, sid, "boot.bootloader")
    assert not any("--uefi-secure-boot" in c for c in chroot_commands(lab) if c[0] == "grub-install")


def test_debian_secure_boot_flag_is_only_used_when_grub_install_knows_it(lab_factory) -> None:
    lab, tree = debian_lab(lab_factory, secure_boot_flag=False)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, _, _ = plan_and_run(manager, sid, "boot.bootloader")
    assert not any("--uefi-secure-boot" in c for c in chroot_commands(lab) if c[0] == "grub-install")
    assert any("does not know --uefi-secure-boot" in w for w in plan["warnings"])


def test_debian_can_reinstall_the_packages_when_online(lab_factory) -> None:
    lab, _ = debian_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "boot.bootloader", {"method": "packages"})
    apt = [c for c in chroot_commands(lab) if c[0] == "apt-get"][0]
    assert apt[:4] == ("apt-get", "install", "--reinstall", "-y") and apt[-3:] == ("grub-efi-amd64", "grub-efi-amd64-signed", "shim-signed") and "Dpkg::Options::=--force-confold" in apt
    assert plan["needs"]["network"] is True
    assert any("policy-rc.d" in step["display"] for step in plan["steps"])


def test_network_dependent_plans_refuse_to_start_offline(lab_factory) -> None:
    lab, _ = debian_lab(lab_factory)
    manager = lab.manager(network_check=lambda: {"ip": False, "dns": False})
    from gentoo_installerd.rescue.scan import ScanOptions

    scan = manager.scan(None, ScanOptions())
    cand = [c for c in scan["candidates"] if c["device"] == "/dev/nvme0n1p5"][0]
    dry = manager.mount(None, {"candidate": cand["id"], "scan_id": scan["scan_id"], "dry_run": True})
    info = manager.mount(None, {"candidate": cand["id"], "scan_id": scan["scan_id"], "plan_digest": dry["mount_plan"]["digest"]})
    sid = info["session_id"]
    plan = manager.plan({"session_id": sid, "action_id": "boot.kernel"})
    with pytest.raises(RescueError) as err:
        manager.run_plan(manager.new_op("run", sid), {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True, "typed": plan["confirm"]["typed"]})
    assert err.value.short == "needs_network" and "no route" in err.value.message
    assert manager.session(sid).mode == "ro"


def test_debian_kernel_reinstall_wraps_apt_with_policy_rc_and_rebuilds(lab_factory) -> None:
    lab, tree = debian_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "boot.kernel")
    commands = chroot_commands(lab)
    apt = [c for c in commands if c[0] == "apt-get"][0]
    assert f"linux-image-{fr.DEBIAN_KVER}" in apt and ("update-initramfs", "-u", "-k", fr.DEBIAN_KVER) in commands and commands[-1] == ("update-grub",)
    unmount(manager, sid)
    assert not (tree / "usr/sbin/policy-rc.d").exists()


def test_debian_finish_interrupted_packages(lab_factory) -> None:
    lab, _ = debian_lab(lab_factory, half_configured=True)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert "D18" in [f["id"] for f in info["findings"]]
    plan_and_run(manager, sid, "pkg.finish")
    commands = chroot_commands(lab)
    assert ("dpkg", "--configure", "-a") in commands and any(c[:3] == ("apt-get", "-f", "install") for c in commands)
    clean, _ = debian_lab(lab_factory)
    manager2, sid2, _ = mount_through_the_manager(clean, candidate_device="/dev/nvme0n1p5")
    with pytest.raises(RescueError):
        manager2.plan({"session_id": sid2, "action_id": "pkg.finish"})


def test_debian_bios_install_goes_to_the_disk(lab_factory) -> None:
    lab = lab_factory(lf.debian_cloud(), uefi=False)
    lab.add_tree("/dev/vda1", "debian", firmware="bios")
    manager, sid, _ = mount_through_the_manager(lab)
    plan_and_run(manager, sid, "boot.bootloader")
    commands = chroot_commands(lab)
    assert ("grub-install", "--target=i386-pc", "--recheck", "/dev/vda") in commands and ("update-grub",) in commands


def test_pop_os_with_systemd_boot_is_not_given_grub_actions(lab_factory) -> None:
    lab, tree = debian_lab(lab_factory, "pop", bootloader="systemd-boot", grub_cfg=False)
    esp = lab.system.device_trees["/dev/nvme0n1p1"]
    import shutil
    shutil.rmtree(esp / "EFI/Pop_OS-1234", ignore_errors=True)
    fr.put(esp, "EFI/systemd/systemd-bootx64.efi", size=1 << 20)
    fr.put(esp, "loader/loader.conf", "default Pop_OS-current\n")
    (tree / "boot/grub").mkdir(parents=True, exist_ok=True)
    for stray in (tree / "boot/grub").glob("*"):
        stray.unlink()
    (tree / "boot/grub").rmdir()
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    actions = {a["id"]: a for a in info["actions"]}
    assert actions["boot.config"]["available"] is False and "not GRUB" in actions["boot.config"]["reason"]
    plan, _, _ = plan_and_run(manager, sid, "boot.bootloader")
    assert ("bootctl", "--esp-path=/boot/efi", "update") in chroot_commands(lab)
    assert not [c for c in chroot_commands(lab) if c[0] == "grub-install"]


# ---- Fedora and RHEL ------------------------------------------------------------------------------------------------------------------------
def fedora_lab(lab_factory, family: str = "fedora", **options):
    options.setdefault("esp_mount", "/boot/efi")
    return uefi_gentoo_lab(lab_factory, family, **options)


def test_fedora_dracut_and_bls_entries(lab_factory) -> None:
    lab, tree = fedora_lab(lab_factory, initramfs=False)
    import shutil
    shutil.rmtree(tree / "boot/loader")
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert {"D02", "D05"} <= {f["id"] for f in info["findings"]}
    plan_and_run(manager, sid, "boot.initramfs")
    plan, result, _ = plan_and_run(manager, sid, "boot.config")
    commands = chroot_commands(lab)
    assert ("dracut", "--force", "--regenerate-all") in commands
    assert ("kernel-install", "add", fr.FEDORA_KVER, f"/usr/lib/modules/{fr.FEDORA_KVER}/vmlinuz") in commands
    assert ("grub2-mkconfig", "-o", "/boot/grub2/grub.cfg") in commands
    assert any("autorelabel" in step["display"] for step in plan["steps"]) and any("SELinux" in w for w in plan["warnings"])
    unmount(manager, sid)
    assert (tree / "boot/loader/entries").glob("*.conf").__next__().exists() and (tree / ".autorelabel").exists()


def test_boot_entries_made_in_the_chroot_never_carry_the_live_command_line(lab_factory) -> None:
    """kernel-install falls back to /proc/cmdline; there the live system's root=live:... would end up in the repaired system's boot entry (review R3)."""
    import shutil

    lab, tree = fedora_lab(lab_factory)
    shutil.rmtree(tree / "boot/loader")
    assert not (tree / "etc/kernel/cmdline").exists()
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, _result, _ = plan_and_run(manager, sid, "boot.config")
    assert any("Boot entries" in check["text"] and "root=UUID=" in check["text"] for check in plan["preconditions"])  # the reviewer sees which command line the new entries get
    seen = [snapshot["cmdline"] for snapshot in lab.system.chroot_snapshots]
    assert seen and all("root=live" not in text and "rd.live" not in text and "CDLABEL" not in text for text in seen)
    unmount(manager, sid)
    entries = sorted((tree / "boot/loader/entries").glob("*.conf"))
    assert entries, "kernel-install added no boot entry"
    for entry in entries:
        text = entry.read_text()
        assert f"options root=UUID={fr.ROOT_UUID} ro" in text, text
        assert "root=live" not in text and "rd.live" not in text and "CDLABEL" not in text and "cdroot" not in text


def test_live_arguments_in_a_boot_entry_are_removed_and_nothing_else_is_touched(lab_factory) -> None:
    lab, tree = fedora_lab(lab_factory)
    entry = next((tree / "boot/loader/entries").glob("*.conf"))
    original = entry.read_text()
    entry.write_text(original.replace("ro rootflags=subvol=root", "ro root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.image quiet"))
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert "D21" in {f["id"] for f in info["findings"]}
    plan, result, _ = plan_and_run(manager, sid, "boot.config")
    assert result["outcome"] == "success" and f"-options root=UUID={fr.ROOT_UUID} ro root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.image quiet" in plan["diff"]
    unmount(manager, sid)
    lines = entry.read_text().splitlines()
    assert f"options root=UUID={fr.ROOT_UUID} ro quiet" in lines and "title Fedora Linux (" + fr.FEDORA_KVER + ")" in lines and "grub_class fedora" in lines
    assert len(list((tree / "boot/loader/entries").glob("*.gentoo-rescue-*"))) == 1  # the entry as it was found is kept


def test_fedora_bootloader_comes_from_packages_never_from_grub2_install(lab_factory) -> None:
    lab, _ = fedora_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "boot.bootloader")
    commands = chroot_commands(lab)
    assert ("dnf", "reinstall", "-y", "shim-x64", "grub2-efi-x64") in commands and not any(c[0] == "grub2-install" for c in commands)
    entry = [c for c in lab.system.commands if c[:2] == ("efibootmgr", "--create")][0]
    assert entry[-2:] == ("--loader", "\\EFI\\fedora\\shimx64.efi") and "Fedora" in entry
    assert plan["needs"]["network"] is True


def test_rhel_adds_grub2_common_and_uses_its_own_directory(lab_factory) -> None:
    lab, _ = fedora_lab(lab_factory, "rhel")
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan_and_run(manager, sid, "boot.bootloader")
    assert ("dnf", "reinstall", "-y", "shim-x64", "grub2-efi-x64", "grub2-common") in chroot_commands(lab)
    entry = [c for c in lab.system.commands if c[:2] == ("efibootmgr", "--create")][0]
    assert entry[-1] == "\\EFI\\redhat\\shimx64.efi" and "Red Hat Enterprise Linux" in entry


def test_fedora_bios_install_uses_grub2_install(lab_factory) -> None:
    lab = lab_factory(lf.debian_cloud(), uefi=False)
    lab.add_tree("/dev/vda1", "fedora", firmware="bios")
    manager, sid, _ = mount_through_the_manager(lab)
    plan_and_run(manager, sid, "boot.bootloader", {"firmware": "bios"})
    assert ("grub2-install", "--target=i386-pc", "--recheck", "/dev/vda") in chroot_commands(lab)


def test_fedora_grub_cfg_location_follows_the_esp_file(lab_factory) -> None:
    lab, tree = fedora_lab(lab_factory, "rhel")
    esp = lab.system.device_trees["/dev/nvme0n1p1"]
    fr.put(esp, "EFI/redhat/grub.cfg", "set default=0\n" + "menuentry 'x' {\n}\n" * 400)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan_and_run(manager, sid, "boot.config")
    assert ("grub2-mkconfig", "-o", "/boot/efi/EFI/redhat/grub.cfg") in chroot_commands(lab)


def test_fedora_kernel_reinstall(lab_factory) -> None:
    lab, _ = fedora_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan_and_run(manager, sid, "boot.kernel")
    commands = chroot_commands(lab)
    assert ("dnf", "reinstall", "-y", "--setopt=strict=0", "kernel-core", "kernel-modules-core", "kernel-modules") in commands
    assert ("dracut", "--force", "--regenerate-all") in commands and ("grub2-mkconfig", "-o", "/boot/grub2/grub.cfg") in commands


# ---- systems that are only detected and diagnosed -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("family,label", [("arch", "arch"), ("opensuse", "suse"), ("alpine", "generic"), ("void", "generic"), ("nixos", "generic")])
def test_unsupported_families_are_diagnosed_but_never_repaired(lab_factory, family: str, label: str) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory, family, esp_mount="/efi")
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert info["family"] == label and info["findings"] is not None
    write_actions = [a for a in info["actions"] if a["id"] not in {"session.enter_write", "session.leave_write"}]
    assert write_actions and all(a["available"] is False and a["reason"] for a in write_actions)
    for name in ("boot.bootloader", "boot.initramfs", "fstab.fix", "fs.check", "efi.add", "acct.password"):
        with pytest.raises(RescueError) as err:
            manager.plan({"session_id": sid, "action_id": name, "params": {"username": "root"}})
        assert err.value.short == "unsupported_action"
    assert manager.query({"session_id": sid, "query": "diag.summary"})["data"]["findings"] is not None
    assert not lab.system.chroot_calls
    unmount(manager, sid)


def test_image_based_systems_get_their_own_recovery_advice(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory, "ostree", esp_mount="/boot/efi")
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert info["immutable"] == "ostree"
    with pytest.raises(RescueError) as err:
        manager.plan({"session_id": sid, "action_id": "boot.bootloader"})
    assert "rpm-ostree rollback" in err.value.message
    micro, _ = uefi_gentoo_lab(lab_factory, "microos", esp_mount="/boot/efi")
    manager2, sid2, _ = mount_through_the_manager(micro, candidate_device="/dev/nvme0n1p5")
    with pytest.raises(RescueError) as err:
        manager2.plan({"session_id": sid2, "action_id": "boot.config"})
    assert "transactional-update rollback" in err.value.message


def test_a_foreign_architecture_gets_no_chroot(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory, "aarch64", esp_mount="/boot/efi")
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert info["arch"] == "aarch64" and any("aarch64 system" in w for w in manager.scan(None, __import__("gentoo_installerd.rescue.scan", fromlist=["ScanOptions"]).ScanOptions())["candidates"][0]["warnings"])
    with pytest.raises(RescueError) as err:
        manager.plan({"session_id": sid, "action_id": "boot.initramfs"})
    assert "aarch64" in err.value.message and not lab.system.chroot_calls


def test_the_chroot_can_be_refused_in_an_inspect_only_session(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, info = mount_through_the_manager(lab, allow_write=False, candidate_device="/dev/nvme0n1p5")
    assert all(a["available"] is False for a in info["actions"] if a["id"] != "session.leave_write")
    with pytest.raises(RescueError) as err:
        manager.plan({"session_id": sid, "action_id": "boot.config"})
    assert err.value.short == "needs_write"
    assert all("noexec" in c[4].split(",") for c in lab.system.commands_named("mount")[1:3] if c[1] == "-t")
