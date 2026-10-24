"""Static diagnosis: one case per finding, healthy systems stay quiet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gentoo_installerd.rescue import efi
from gentoo_installerd.rescue.diagnose import diagnose, recommended_actions

from . import fakeroot as fr
from . import lsblk_fixtures as lf


def lab_for(lab_factory, family: str = "gentoo", lsblk: str | None = None, root: str = "/dev/nvme0n1p5", esp: bool = True, **opts):
    lab = lab_factory(lsblk or lf.nvme_dualboot(), **{k: opts.pop(k) for k in ("uefi",) if k in opts})
    opts.setdefault("firmware", "uefi")
    tree = lab.add_tree(root, family, **opts)
    if esp:
        lab.system.attach("/dev/nvme0n1p1", fr.esp_tree(lab.tmp / "trees" / "esp", family, shim=family in {"debian", "ubuntu"}))
    return lab, tree


def findings_of(lab, root: str = "/dev/nvme0n1p5", **kwargs):
    session, view = lab.mounted_view(root)
    return diagnose(view, **kwargs)


def ids(findings) -> list[str]:
    return [finding.id for finding in findings]


def severities(findings, level: str) -> list[str]:
    return [finding.id for finding in findings if finding.severity == level]


def test_healthy_gentoo_has_no_errors(lab_factory) -> None:
    lab, _ = lab_for(lab_factory)
    findings = findings_of(lab)
    assert severities(findings, "error") == [], [f.to_dict() for f in findings]


@pytest.mark.parametrize("family,kwargs", [("debian", {}), ("ubuntu", {}), ("fedora", {"btrfs": True}), ("arch", {"esp_mount": "/efi"})])
def test_healthy_systems_of_other_families_are_quiet(lab_factory, family: str, kwargs: dict) -> None:
    lab, _ = lab_for(lab_factory, family, **kwargs)
    # the fakes mount their ESP below /boot/efi or /boot; give them the right directory
    findings = findings_of(lab)
    assert "D01" not in ids(findings) and "D02" not in ids(findings) and "D03" not in ids(findings) and "D04" not in ids(findings), [f.to_dict() for f in findings]


def test_d01_no_kernel(lab_factory) -> None:
    lab, _ = lab_for(lab_factory, kernel_image=False)
    findings = findings_of(lab)
    d01 = [f for f in findings if f.id == "D01"][0]
    assert d01.severity == "error" and d01.actions == ["boot.kernel"]


def test_d02_missing_initramfs_and_d03_truncated(lab_factory) -> None:
    lab, _ = lab_for(lab_factory, initramfs=False)
    findings = findings_of(lab)
    assert [f.actions for f in findings if f.id == "D02"] == [["boot.initramfs"]]
    lab2, _ = lab_for(lab_factory, initramfs="truncated", **{})
    assert "D03" in ids(findings_of(lab2))


def test_d04_kernel_without_module_tree(lab_factory) -> None:
    lab, tree = lab_for(lab_factory, kernels=[fr.GENTOO_KVER, "6.12.0-gentoo-dist-bin"])
    import shutil
    shutil.rmtree(tree / "usr/lib/modules/6.12.0-gentoo-dist-bin")
    findings = findings_of(lab)
    assert [f.title for f in findings if f.id == "D04"] == ["Kernel 6.12.0-gentoo-dist-bin has no module tree"]


def test_d05_missing_empty_and_menuless_grub_cfg(lab_factory) -> None:
    lab, tree = lab_for(lab_factory)
    (tree / "boot/grub/grub.cfg").unlink()
    assert [f.actions for f in findings_of(lab) if f.id == "D05"] == [["boot.config"]]
    lab2, tree2 = lab_for(lab_factory)
    (tree2 / "boot/grub/grub.cfg").write_text("# empty\n")
    assert any("empty" in f.title for f in findings_of(lab2) if f.id == "D05")
    lab3, tree3 = lab_for(lab_factory)
    (tree3 / "boot/grub/grub.cfg").write_text("set default=0\n" * 20)
    assert any("lists no system" in f.title for f in findings_of(lab3) if f.id == "D05")


def test_d05_fedora_without_boot_entries(lab_factory) -> None:
    lab, tree = lab_for(lab_factory, "fedora", btrfs=True, grub_cfg=False)
    import shutil
    shutil.rmtree(tree / "boot/loader")
    assert any(f.id == "D05" and "no boot entries" in f.title.lower() for f in findings_of(lab))


def test_d05_no_loader_at_all(lab_factory) -> None:
    lab, tree = lab_for(lab_factory, bootloader="none", esp=False)
    assert any(f.id == "D05" and "No boot loader" in f.title and f.actions == ["boot.bootloader"] for f in findings_of(lab))


def test_d06_grub_cfg_names_a_root_that_does_not_exist(lab_factory) -> None:
    lab, tree = lab_for(lab_factory, root_uuid=fr.DEAD_UUID)
    # the filesystem on the device keeps its real UUID; only grub.cfg refers to the dead one
    findings = findings_of(lab)
    d06 = [f for f in findings if f.id == "D06"]
    assert d06 and fr.DEAD_UUID in d06[0].detail and set(d06[0].actions) == {"fstab.fix", "boot.config"}


def test_d21_live_arguments_in_boot_configuration(lab_factory) -> None:
    lab, tree = lab_for(lab_factory)
    cfg = tree / "boot/grub/grub.cfg"
    cfg.write_text(cfg.read_text().replace("ro\n", "ro root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.image\n"))
    (tree / "etc/kernel/cmdline").write_text("quiet cdroot root=live:LABEL=X\n")
    found = [f for f in findings_of(lab) if f.id == "D21"]
    assert len(found) >= 2 and all(f.severity == "error" and f.actions == ["boot.config"] for f in found)


def test_ordinary_kernel_arguments_are_not_a_live_leak(lab_factory) -> None:
    """A healthy system that boots to the console (nox) or keeps rd.debug / dokeymap must not get a D21 error and a 'Fix everything recommended'."""
    lab, tree = lab_for(lab_factory)
    (tree / "etc/kernel/cmdline").write_text(f"root=UUID={fr.ROOT_UUID} ro nox rd.debug dokeymap\n")
    fr.put(tree, "etc/default/grub", 'GRUB_CMDLINE_LINUX="nogui nodhcp dospeakup"\n')
    cfg = tree / "boot/grub/grub.cfg"
    cfg.write_text(cfg.read_text().replace("ro\n", "ro nox rd.debug\n"))
    findings = findings_of(lab)
    assert "D21" not in ids(findings), [f.to_dict() for f in findings if f.id == "D21"]
    assert "boot.config" not in recommended_actions(findings)


def test_d07_dead_fstab_entries(lab_factory) -> None:
    text = fr.fstab_text(esp=fr.DEAD_UUID, esp_mount="/efi", swap=None, home=fr.DEAD_UUID)
    lab, _ = lab_for(lab_factory, fstab=text)
    d07 = [f for f in findings_of(lab) if f.id == "D07"]
    assert d07[0].severity == "error" and d07[0].actions == ["fstab.fix"] and len(d07[0].evidence) == 2


def test_d07_type_mismatch_and_unstable_names(lab_factory) -> None:
    text = f"UUID={fr.ROOT_UUID} / xfs defaults 0 1\n/dev/nvme0n1p1 /efi vfat defaults 0 2\n"
    lab, _ = lab_for(lab_factory, fstab=text)
    titles = [f.title for f in findings_of(lab) if f.id == "D07"]
    assert any("wrong file system type" in t for t in titles) and any("can change" in t for t in titles)


def test_d08_esp_empty_without_loader_and_without_fallback(lab_factory) -> None:
    lab, _ = lab_for(lab_factory, esp=False)
    (lab.tmp / "trees" / "esp0").mkdir(parents=True)
    lab.system.attach("/dev/nvme0n1p1", lab.tmp / "trees" / "esp0")
    assert any(f.id == "D08" and "empty" in f.title for f in findings_of(lab))
    other, _ = lab_for(lab_factory, esp=False)
    foreign = other.tmp / "trees" / "esp1"
    fr.put(foreign, "EFI/Microsoft/Boot/bootmgfw.efi", size=1 << 20)
    other.system.attach("/dev/nvme0n1p1", foreign)
    d08 = [f for f in findings_of(other) if f.id == "D08"]
    assert d08 and "no boot loader" in d08[0].title.lower() and d08[0].actions == ["boot.bootloader"]
    nofallback, _ = lab_for(lab_factory, esp=False)
    nofallback.system.attach("/dev/nvme0n1p1", fr.esp_tree(nofallback.tmp / "trees" / "esp", "gentoo", fallback=False))
    d08 = [f for f in findings_of(nofallback) if f.id == "D08"]
    assert d08 and d08[0].severity == "warn" and "boot.esp_fallback" in d08[0].actions


def test_d08_esp_that_cannot_be_mounted(lab_factory) -> None:
    lab, _ = lab_for(lab_factory, esp=False)
    lab.system.fail_mount["/dev/nvme0n1p1"] = "mount: wrong fs type, bad option, bad superblock"
    findings = findings_of(lab)
    m01 = [f for f in findings if f.id == "M01"]
    assert len(m01) == 1 and m01[0].severity == "error" and m01[0].actions == ["fs.check"] and "damaged vfat file system" in m01[0].detail
    assert "D08" not in ids(findings)  # one finding for one cause: the failed mount is already reported


def test_d09_gpt_disk_without_bios_boot_partition(lab_factory) -> None:
    lab, tree = lab_for(lab_factory, esp=False, firmware="bios")
    import shutil
    (tree / "boot/grub/i386-pc").mkdir(parents=True)
    findings = findings_of(lab)
    d09 = [f for f in findings if f.id == "D09"]
    assert d09 and "never repartitions" in d09[0].detail and d09[0].actions == []
    cloud = lab_factory(lf.debian_cloud(), name="cloud")
    t = cloud.add_tree("/dev/vda1", "debian", firmware="bios")
    (t / "boot/grub/i386-pc").mkdir(parents=True)
    assert "D09" not in ids(findings_of(cloud, "/dev/vda1"))


def test_d10_firmware_mismatch(lab_factory) -> None:
    lab, _ = lab_for(lab_factory, uefi=False)
    d10 = [f for f in findings_of(lab) if f.id == "D10"]
    assert d10 and "legacy BIOS mode" in d10[0].detail and d10[0].actions == ["boot.bootloader"]
    bios, tree = lab_for(lab_factory, esp=False, firmware="bios", uefi=True)
    (tree / "boot/grub/i386-pc").mkdir(parents=True)
    d10 = [f for f in findings_of(bios) if f.id == "D10"]
    assert d10 and "BIOS/CSM" in d10[0].detail


def test_d11_nvram_has_no_entry_for_this_system(lab_factory) -> None:
    lab, _ = lab_for(lab_factory)
    session, view = lab.mounted_view("/dev/nvme0n1p5")
    partuuid = view.graph.find("/dev/nvme0n1p1").partuuid
    text = f"BootCurrent: 0001\nBootOrder: 0001\nBoot0001* Windows Boot Manager\tHD(1,GPT,{partuuid},0x800,0x32000)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n"
    state = efi.parse_efibootmgr(text)
    found = [f for f in diagnose(view, efi_state=state) if f.id == "D11"]
    assert found and found[0].actions == ["efi.add", "boot.bootloader"]
    good = efi.parse_efibootmgr(text + f"Boot0002* Gentoo\tHD(1,GPT,{partuuid},0x800,0x100000)/File(\\efi\\GENTOO\\GRUBX64.EFI)\n")
    assert [f for f in diagnose(view, efi_state=good) if f.id == "D11"] == []
    dead = efi.parse_efibootmgr(text + f"Boot0002* Gentoo\tHD(1,GPT,{partuuid},0x800,0x100000)/File(\\EFI\\Gentoo\\gone.efi)\n")
    assert "no longer exist" in [f for f in diagnose(view, efi_state=dead) if f.id == "D11"][0].detail


def test_d12_degraded_view_d14_accounts_d18_pending_work(lab_factory) -> None:
    lab, tree = lab_for(lab_factory)
    (tree / "etc/passwd").write_text("")
    session, view = lab.mounted_view("/dev/nvme0n1p5")
    assert "D14" in ids(diagnose(view))
    deb, _ = lab_for(lab_factory, "debian", half_configured=True)
    assert [f.evidence for f in findings_of(deb) if f.id == "D18"] == [["half-configured: grub-efi-amd64"]]
    arch, atree = lab_for(lab_factory, "arch")
    (atree / "var/lib/pacman").mkdir(parents=True, exist_ok=True)
    (atree / "var/lib/pacman/db.lck").write_text("")
    assert any(f.id == "D18" and "db.lck" in f.evidence[0] for f in findings_of(arch))
    gentoo, gtree = lab_for(lab_factory)
    (gtree / "var/cache/edb").mkdir(parents=True, exist_ok=True)
    (gtree / "var/cache/edb/mtimedb").write_text(json.dumps({"resume": {"mergelist": [["ebuild", "/", "dev-lang/python-3.14", "merge"]]}}))
    assert any(f.id == "D18" for f in findings_of(gentoo))


def test_d17_cloned_root_uuid(lab_factory) -> None:
    lab = lab_factory(lf.cloned_uuids())
    lab.add_tree("/dev/sda1", "alpine")
    session, view = lab.mounted_view("/dev/sda1")
    assert "D17" in ids(diagnose(view))


def test_d20_hardware_changed_is_only_reported_when_the_user_says_so(lab_factory) -> None:
    lab, _ = lab_for(lab_factory)
    session, view = lab.mounted_view("/dev/nvme0n1p5")
    assert "D20" not in ids(diagnose(view))
    assert [f.actions for f in diagnose(view, hardware_changed=True) if f.id == "D20"] == [["boot.initramfs", "boot.bootloader", "efi.add"]]


def test_recommendations_follow_the_repair_order(lab_factory) -> None:
    lab, tree = lab_for(lab_factory, initramfs=False, fstab=fr.fstab_text(esp=fr.DEAD_UUID, esp_mount="/efi", swap=None))
    (tree / "boot/grub/grub.cfg").unlink()
    (tree / "boot/grub/x86_64-efi").mkdir()  # GRUB is still installed
    findings = findings_of(lab)
    assert recommended_actions(findings)[:3] == ["fstab.fix", "boot.initramfs", "boot.config"]


def test_findings_serialize() -> None:
    from gentoo_installerd.rescue.diagnose import Finding

    assert Finding("D99", "info", "t", "d", ["e"], ["a"]).to_dict() == {"id": "D99", "severity": "info", "title": "t", "detail": "d", "evidence": ["e"], "actions": ["a"]}


def test_efi_parser_and_case_insensitive_fat_lookup() -> None:
    text = (
        "BootNext: 0003\nBootCurrent: 0001\nTimeout: 1 seconds\nBootOrder: 0001,0000,0003\n"
        "Boot0000* Windows Boot Manager\tHD(1,GPT,aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee,0x800,0x32000)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)WINDOWS.........x...B.C.D.O.B.J.E.C.T.=.{9dea862c}\n"
        "Boot0001* debian\tHD(1,GPT,aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee,0x800,0x100000)/File(\\EFI\\debian\\shimx64.efi)\n"
        "Boot0003  UEFI: PXE IPv4 Intel(R) Ethernet\tPciRoot(0x0)/Pci(0x1c,0x0)/Pci(0x0,0x0)/MAC(001122334455,1)/IPv4(0.0.0.00.0.0.0,0,0)..BO\n"
    )
    state = efi.parse_efibootmgr(text)
    assert state.current == "0001" and state.next == "0003" and state.order == ["0001", "0000", "0003"] and state.timeout == "1 seconds"
    assert [(e.number, e.active, e.label) for e in state.entries] == [("0000", True, "Windows Boot Manager"), ("0001", True, "debian"), ("0003", False, "UEFI: PXE IPv4 Intel(R) Ethernet")]
    assert state.entry("0001").loader == "\\EFI\\debian\\shimx64.efi" and state.entry("0001").partuuid == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" and state.entry("0001").partition == 1
    assert state.entry("0000").loader == "\\EFI\\Microsoft\\Boot\\bootmgfw.efi"
    assert state.entry("0003").loader == "" and state.entry("0003").partuuid == ""
    listing = {"/boot/efi": ["EFI"], "/boot/efi/EFI": ["Debian", "BOOT"], "/boot/efi/EFI/Debian": ["SHIMX64.EFI"]}
    assert efi.esp_file_exists("\\EFI\\debian\\shimx64.efi", lambda path: listing.get(path, []), "/boot/efi")
    assert not efi.esp_file_exists("\\EFI\\debian\\grubx64.efi", lambda path: listing.get(path, []), "/boot/efi")
    bad = efi.parse_efibootmgr("EFI variables are not supported on this system.\n", 2)
    assert bad.usable is False and "not supported" in bad.error
    assert efi.sanitize_label("Gentoo; rm -rf /") == "Gentoo rm -rf" and efi.sanitize_label("a;b") == "ab"
    assert efi.loader_for_entry("/EFI/BOOT/BOOTX64.EFI") == "\\EFI\\BOOT\\BOOTX64.EFI"
