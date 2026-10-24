"""fstab parsing, resolution and the repair proposals (review R13)."""

from __future__ import annotations

import pytest

from gentoo_installerd.rescue.devices import build_graph
from gentoo_installerd.rescue.fstab import (
    Fstab, apply_choices, decode_octal, encode_octal, parse_crypttab, propose_fixes, resolve_all, resolve_entry, unified_diff, uuid_spec,
)

from . import fakeroot as fr
from . import lsblk_fixtures as lf

SAMPLE = """# /etc/fstab
UUID=998f7697-b591-4cb9-a8e7-d1c52c798ac7\t/\text4\tnoatime\t0 1
UUID=ABCD-1234 /efi vfat umask=0077 0 2

/dev/mapper/vg0-swap none swap sw 0 0
tmpfs /tmp tmpfs defaults,nosuid 0 0
server:/export /mnt/nfs nfs defaults 0 0
/dev/sda3\t/media/my\\040disk\tntfs3\tdefaults,noauto\t0\t0
"""


def test_round_trip_is_byte_exact_and_octal_escapes_decode() -> None:
    fstab = Fstab.parse(SAMPLE)
    assert fstab.render() == SAMPLE
    assert Fstab.parse(SAMPLE.rstrip("\n")).render() == SAMPLE.rstrip("\n")
    assert Fstab.parse("").render() == ""
    mount = next(entry for entry in fstab.entries if "disk" in entry.file)
    assert mount.file == "/media/my disk"
    assert decode_octal(r"a\040b\011c\134d") == "a b\tc\\d"
    assert encode_octal("a b\\c") == "a\\040b\\134c"


def test_missing_trailing_fields_get_their_defaults() -> None:
    fstab = Fstab.parse("UUID=x /data ext4\nUUID=y /var ext4 noatime\n")
    first, second = fstab.entries
    assert (first.opts, first.dump, first.passno) == ("defaults", "0", "0")
    assert (second.opts, second.dump, second.passno) == ("noatime", "0", "0")


def test_minimal_edits_keep_the_layout() -> None:
    fstab = Fstab.parse(SAMPLE)
    efi = fstab.entries[1]
    fstab.replace_spec(efi.lineno, "UUID=FFFF-0000")
    swap = fstab.entries[2]
    fstab.add_option(swap.lineno, "nofail")
    text = fstab.render()
    assert "UUID=FFFF-0000 /efi vfat umask=0077 0 2" in text
    assert "/dev/mapper/vg0-swap none swap sw,nofail 0 0" in text
    assert text.count("\n") == SAMPLE.count("\n")
    assert unified_diff(SAMPLE, text).startswith("--- a/etc/fstab\n+++ b/etc/fstab\n")
    fstab.add_option(swap.lineno, "nofail")  # idempotent
    assert fstab.render() == text


def test_comment_out_keeps_the_original_line_visible() -> None:
    fstab = Fstab.parse(SAMPLE)
    fstab.comment_out(fstab.entries[1].lineno)
    text = fstab.render()
    assert "# UUID=ABCD-1234 /efi vfat umask=0077 0 2" in text
    assert [entry.file for entry in Fstab.parse(text).entries if entry.file == "/efi"] == []


def test_spec_kinds_resolve_against_the_graph() -> None:
    graph = build_graph(lf.nvme_dualboot())
    text = (
        f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.ESP_UUID.lower()} /efi vfat defaults 0 2\nLABEL=Windows /win ntfs3 ro 0 0\n"
        f"PARTUUID={graph.find('/dev/nvme0n1p6').partuuid} none swap sw 0 0\n/dev/nvme0n1p5 /alt ext4 defaults 0 0\n/dev/disk/by-uuid/{fr.ROOT_UUID} /again ext4 defaults 0 0\n"
        "/dev/disk/by-label/Windows /w2 ntfs3 defaults 0 0\nUUID=00000000-dead /gone ext4 defaults 0 0\n"
    )
    results = {entry.entry.file: entry for entry in resolve_all(Fstab.parse(text), graph)}
    assert results["/"].status == "resolved" and results["/"].device.name == "nvme0n1p5" and results["/"].kind == "uuid"
    assert results["/efi"].status == "resolved" and results["/efi"].device.name == "nvme0n1p1"  # FAT volume ids compare case-insensitively
    assert results["/win"].status == "resolved" and results["/win"].kind == "label"
    assert results["none"].device.name == "nvme0n1p6" and results["none"].kind == "partuuid"
    assert results["/alt"].unstable_name and results["/alt"].status == "resolved"
    assert results["/again"].device.name == "nvme0n1p5"
    assert results["/w2"].device.name == "nvme0n1p3"
    assert results["/gone"].status == "unresolved" and "UUID" in results["/gone"].reason


def test_pseudo_network_and_bind_entries_are_skipped() -> None:
    graph = build_graph(lf.plain_sata_mbr())
    results = resolve_all(Fstab.parse(SAMPLE + "/srv/data /mnt/data none bind 0 0\noverlay /x overlay defaults 0 0\n//nas/share /nas cifs defaults 0 0\n"), graph)
    skipped = {item.entry.file: item.kind for item in results if item.status == "skipped"}
    assert skipped == {"/tmp": "pseudo", "/mnt/nfs": "network", "/mnt/data": "bind", "/x": "pseudo", "/nas": "network"}


def test_cloned_uuids_are_ambiguous_never_guessed() -> None:
    graph = build_graph(lf.cloned_uuids())
    result = resolve_entry(Fstab.parse(f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\n").entries[0], graph)
    assert result.status == "ambiguous" and len(result.devices) == 2


def test_btrfs_members_sharing_a_uuid_resolve_to_the_filesystem() -> None:
    graph = build_graph(lf.btrfs_multi_device())
    result = resolve_entry(Fstab.parse(f"UUID={fr.ROOT_UUID} / btrfs defaults 0 0\n").entries[0], graph)
    assert result.status == "resolved" and len(result.devices) == 2


def test_type_mismatch_is_reported_but_ext_family_is_compatible() -> None:
    graph = build_graph(lf.plain_sata_mbr())
    wrong = resolve_entry(Fstab.parse(f"UUID={fr.ROOT_UUID} / xfs defaults 0 1\n").entries[0], graph)
    assert wrong.status == "resolved" and "xfs" in wrong.type_mismatch and "ext4" in wrong.type_mismatch
    for kind in ("ext2", "ext3", "ext4", "auto"):
        assert not resolve_entry(Fstab.parse(f"UUID={fr.ROOT_UUID} / {kind} defaults 0 1\n").entries[0], graph).type_mismatch


def test_lvm_and_mapper_names() -> None:
    graph = build_graph(lf.luks_lvm_ext4(unlocked=True, activated=True))
    text = "/dev/mapper/vg0-root / ext4 defaults 0 1\n/dev/vg0/swap none swap sw 0 0\n/dev/vg0/missing /data ext4 defaults 0 2\n"
    results = {item.entry.file: item for item in resolve_all(Fstab.parse(text), graph)}
    assert results["/"].device.name == "vg0-root"
    assert results["none"].device.name == "vg0-swap"
    assert results["/data"].status == "unresolved" and "not active" in results["/data"].reason
    dashed = build_graph(lf.ubuntu_lvm())
    assert resolve_entry(Fstab.parse("/dev/ubuntu-vg/ubuntu-lv / ext4 defaults 0 1\n").entries[0], dashed).device.name == "ubuntu--vg-ubuntu--lv"


def test_crypttab_maps_a_mapper_entry_to_the_container() -> None:
    crypttab = parse_crypttab(f"# comment\ncryptroot UUID={fr.LUKS_UUID} none luks,discard\nbadline\n")
    assert crypttab[0].name == "cryptroot" and crypttab[0].keyfile == "" and crypttab[0].options == "luks,discard"
    entry = Fstab.parse("/dev/mapper/cryptroot / ext4 defaults 0 1\n").entries[0]
    locked = resolve_entry(entry, build_graph(lf.luks_lvm_ext4()), crypttab=crypttab)
    assert locked.status == "needs_unlock" and locked.container == "/dev/nvme0n1p3"
    # once unlocked under our own name the entry resolves to that mapping
    graph = build_graph(lf.luks_btrfs(unlocked=True))
    crypttab = parse_crypttab(f"cryptroot UUID={fr.LUKS_UUID} none luks\n")
    unlocked = resolve_entry(entry, graph, crypttab=crypttab)
    assert unlocked.status == "resolved" and unlocked.device.name == "rescue-1f2e3d4c5b6a"


def test_keyfile_crypttab_entries_expose_the_keyfile_path() -> None:
    entry = parse_crypttab("data UUID=abc /etc/keys/data.key luks\n")[0]
    assert entry.keyfile == "/etc/keys/data.key"


def test_by_id_links_use_the_injected_resolver() -> None:
    graph = build_graph(lf.plain_sata_mbr())
    entry = Fstab.parse("/dev/disk/by-id/ata-Samsung_SSD-part6 /home ext4 defaults 0 2\n").entries[0]
    assert resolve_entry(entry, graph).status == "unresolved"
    found = resolve_entry(entry, graph, dev_links=lambda path: "/dev/sda6" if path.endswith("part6") else None)
    assert found.status == "resolved" and found.device.name == "sda6"


def test_quoted_values_resolve_like_unquoted_ones() -> None:
    """fstab(5) allows UUID="..." and blkid prints it that way; the quotes are not part of the value."""
    graph = build_graph(lf.nvme_dualboot())
    partuuid = graph.find("/dev/nvme0n1p5").partuuid
    text = (
        f'UUID="{fr.ROOT_UUID}" / ext4 defaults 0 1\nUUID=\'{fr.ESP_UUID}\' /efi vfat defaults 0 2\nLABEL="Windows" /win ntfs3 ro 0 0\n'
        f'PARTUUID="{partuuid}" /alt ext4 defaults 0 0\nUUID="{fr.ROOT_UUID} /broken ext4 defaults 0 0\n'
    )
    results = {entry.entry.file: entry for entry in resolve_all(Fstab.parse(text), graph)}
    assert results["/"].status == "resolved" and results["/"].device.name == "nvme0n1p5" and results["/"].kind == "uuid"
    assert results["/efi"].status == "resolved" and results["/efi"].device.name == "nvme0n1p1"
    assert results["/win"].status == "resolved" and results["/win"].device.name == "nvme0n1p3" and results["/win"].kind == "label"
    assert results["/alt"].status == "resolved" and results["/alt"].kind == "partuuid"
    assert results["/broken"].status == "unresolved"  # a lone quote is not a pair: nothing is guessed from it


def test_a_quoted_crypttab_source_finds_its_container() -> None:
    crypttab = parse_crypttab(f'cryptroot UUID="{fr.LUKS_UUID}" none luks\n')
    entry = Fstab.parse("/dev/mapper/cryptroot / ext4 defaults 0 1\n").entries[0]
    locked = resolve_entry(entry, build_graph(lf.luks_lvm_ext4()), crypttab=crypttab)
    assert locked.status == "needs_unlock" and locked.container == "/dev/nvme0n1p3"


def test_by_id_and_by_path_names_resolve_through_the_live_systems_links_by_default(monkeypatch) -> None:
    """Nobody passes a link resolver, so the default one must be the real one: the live system's own /dev/disk links."""
    from gentoo_installerd.rescue import fstab as fstab_module

    asked: list[str] = []

    def live_links(path: str) -> str | None:
        asked.append(path)
        return {"/dev/disk/by-id/ata-Samsung_SSD-part6": "/dev/sda6", "/dev/disk/by-path/pci-0000:00:17.0-ata-1-part1": "/dev/sda1"}.get(path)

    monkeypatch.setattr(fstab_module, "default_dev_link_resolver", live_links)
    graph = build_graph(lf.plain_sata_mbr())
    text = "/dev/disk/by-id/ata-Samsung_SSD-part6 /home ext4 defaults 0 2\n/dev/disk/by-path/pci-0000:00:17.0-ata-1-part1 /boot ext4 defaults 0 2\n/dev/disk/by-id/gone /x ext4 defaults 0 2\n"
    results = {item.entry.file: item for item in resolve_all(Fstab.parse(text), graph)}
    assert results["/home"].status == "resolved" and results["/home"].device.name == "sda6" and results["/home"].kind == "by_id"
    assert results["/boot"].status == "resolved" and results["/boot"].device.name == "sda1" and results["/boot"].kind == "by_path"
    assert results["/x"].status == "unresolved" and "/dev/disk/by-id/gone" in results["/x"].reason
    assert "/dev/disk/by-id/ata-Samsung_SSD-part6" in asked


def test_the_default_link_resolver_follows_a_link_and_knows_a_missing_one(tmp_path) -> None:
    from gentoo_installerd.rescue.fstab import default_dev_link_resolver

    disk = tmp_path / "sda6"
    disk.write_text("")
    (tmp_path / "by-id-link").symlink_to(disk)
    assert default_dev_link_resolver(str(tmp_path / "by-id-link")) == str(disk)
    assert default_dev_link_resolver(str(tmp_path / "no-such-link")) is None


# ---- proposals ---------------------------------------------------------------------------------------------------
def fedora_like_graph():
    return build_graph(lf.fedora_cloud_btrfs())


def test_dead_root_entry_is_replaced_by_the_mounted_filesystem() -> None:
    graph = build_graph(lf.plain_sata_mbr())
    fstab = Fstab.parse(f"UUID={fr.DEAD_UUID} / ext4 defaults 0 1\nUUID={fr.SWAP_UUID} none swap sw 0 0\n")
    root = graph.find("/dev/sda1")
    proposals = propose_fixes(fstab, resolve_all(fstab, graph), graph, root_node=root)
    assert len(proposals) == 1
    assert proposals[0].selected == f"UUID={fr.ROOT_UUID}" and proposals[0].default_action == "replace"
    assert "mounted" in proposals[0].selected_why


def test_dead_swap_with_a_single_unused_swap_area_is_preselected() -> None:
    graph = build_graph(lf.plain_sata_mbr())
    fstab = Fstab.parse(f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.DEAD_UUID} none swap sw 0 0\n")
    proposals = propose_fixes(fstab, resolve_all(fstab, graph), graph, root_node=graph.find("/dev/sda1"))
    assert proposals[0].selected == f"UUID={fr.SWAP_UUID}"
    assert "swap" in proposals[0].selected_why


def test_two_candidate_swap_areas_are_not_preselected() -> None:
    import json
    data = json.loads(lf.plain_sata_mbr())
    extra = lf.part("sda", 7, 2 * lf.GiB, 8, 0, fstype="swap", uuid="11111111-2222-3333-4444-555555555555", parttype="0x82", pttype="dos")
    data["blockdevices"][0]["children"].append(extra)
    graph = build_graph(json.dumps(data))
    fstab = Fstab.parse(f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.DEAD_UUID} none swap sw 0 0\n")
    proposal = propose_fixes(fstab, resolve_all(fstab, graph), graph, root_node=graph.find("/dev/sda1"))[0]
    assert proposal.selected == "" and len(proposal.candidates) == 2 and proposal.default_action == "nofail"


def test_single_esp_on_the_systems_disk_is_preselected_only_when_it_has_an_efi_directory() -> None:
    graph = fedora_like_graph()
    fstab = Fstab.parse(f"UUID={fr.ROOT_UUID} / btrfs defaults 0 0\nUUID={fr.DEAD_UUID} /boot/efi vfat umask=0077 0 2\n")
    root = graph.find("/dev/vda4")
    resolved = resolve_all(fstab, graph)
    good = propose_fixes(fstab, resolved, graph, root_node=root)[0]
    assert good.selected == f"UUID={fr.ESP_UUID}" and "EFI system partition" in good.selected_why
    esp = graph.find("/dev/vda2")
    bad = propose_fixes(fstab, resolved, graph, root_node=root, esp_has_efi_dir={esp.key: False})[0]
    assert bad.selected == "" and bad.default_action == "nofail"


def test_a_backup_partition_is_never_preselected_as_home() -> None:
    """Same filesystem, same disk and a label word score high, but only root, swap and ESP may be preselected."""
    graph = build_graph(lf.plain_sata_mbr())
    fstab = Fstab.parse(f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.DEAD_UUID} /home ext4 defaults 0 2\n")
    proposal = propose_fixes(fstab, resolve_all(fstab, graph), graph, root_node=graph.find("/dev/sda1"))[0]
    assert proposal.selected == "" and proposal.default_action == "nofail"
    assert proposal.candidates and proposal.candidates[0].device == "/dev/sda6" and proposal.candidates[0].score >= 80
    # the candidates are still offered so the user may pick one deliberately


def test_candidates_exclude_devices_that_other_entries_already_use() -> None:
    graph = build_graph(lf.plain_sata_mbr())
    fstab = Fstab.parse(f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.HOME_UUID} /home ext4 defaults 0 2\nUUID={fr.DEAD_UUID} /data ext4 defaults 0 2\n")
    proposal = propose_fixes(fstab, resolve_all(fstab, graph), graph, root_node=graph.find("/dev/sda1"))[0]
    assert proposal.mountpoint == "/data" and proposal.candidates == []


def test_label_match_scores_high_but_stays_a_suggestion() -> None:
    graph = build_graph(lf.plain_sata_mbr())
    fstab = Fstab.parse(f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nLABEL=HOME /home ext4 defaults 0 2\n")
    proposal = propose_fixes(fstab, resolve_all(fstab, graph), graph, root_node=graph.find("/dev/sda1"))[0]
    assert proposal.candidates[0].device == "/dev/sda6" and proposal.selected == ""
    assert any("same label" in reason for reason in proposal.candidates[0].reasons)


def test_type_mismatch_gets_a_keep_proposal() -> None:
    graph = build_graph(lf.plain_sata_mbr())
    fstab = Fstab.parse(f"UUID={fr.ROOT_UUID} / xfs defaults 0 1\n")
    proposal = propose_fixes(fstab, resolve_all(fstab, graph), graph, root_node=graph.find("/dev/sda1"))[0]
    assert proposal.status == "type_mismatch" and proposal.default_action == "keep"


def test_apply_choices_validates_replacements() -> None:
    graph = build_graph(lf.plain_sata_mbr())
    fstab = Fstab.parse(f"UUID={fr.DEAD_UUID} / ext4 defaults 0 1\nUUID={fr.DEAD_UUID} /data ext4 defaults 0 2\nUUID={fr.DEAD_UUID} /old ext4 defaults 0 2\n/dev/sda6 /home ext4 defaults 0 2\n")
    updated = apply_choices(fstab, {1: {"action": "replace", "spec": f"UUID={fr.ROOT_UUID}"}, 2: {"action": "nofail"}, 3: {"action": "comment"}, 4: {"action": "unstable_to_uuid"}}, graph)
    text = updated.render()
    assert f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1" in text
    assert "/data ext4 defaults,nofail 0 2" in text
    assert "# UUID=" + fr.DEAD_UUID + " /old ext4 defaults 0 2" in text
    assert f"UUID={fr.HOME_UUID} /home ext4" in text
    assert fstab.render().count(fr.DEAD_UUID) == 3  # the original is untouched
    for bad in ({"action": "replace", "spec": "UUID=00000000-0000-0000-0000-000000000000"}, {"action": "replace", "spec": "/dev/sda1"},
                {"action": "replace", "spec": "UUID=1; rm -rf /"}, {"action": "explode"}):
        with pytest.raises(ValueError):
            apply_choices(fstab, {1: bad}, graph)
    with pytest.raises(ValueError):
        apply_choices(fstab, {99: {"action": "nofail"}}, graph)


def test_uuid_spec_prefers_the_filesystem_uuid() -> None:
    graph = build_graph(lf.plain_sata_mbr())
    assert uuid_spec(graph.find("/dev/sda1")) == f"UUID={fr.ROOT_UUID}"
    assert uuid_spec(graph.find("/dev/sda2")).startswith("PARTUUID=")
