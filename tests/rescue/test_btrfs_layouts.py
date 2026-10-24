"""btrfs layouts: Arch/Ubuntu @, Fedora root, openSUSE default snapshot (before and after a rollback), Timeshift (review R10)."""

from __future__ import annotations

import json
from pathlib import Path

from gentoo_installerd.rescue import btrfs
from gentoo_installerd.rescue.scan import ScanOptions

from . import fakeroot as fr
from . import lsblk_fixtures as lf


def btrfs_disk(root_label: str = "pool") -> str:
    kids = [lf.part("vda", 1, 100 * lf.GiB, 254, 0, fstype="btrfs", uuid=fr.ROOT_UUID, label=root_label, parttype=lf.LINUX_FS, pttype="gpt")]
    return lf.lsblk(lf.node("vda", "disk", 100 * lf.GiB, maj_min="254:0", pttype="gpt", ptuuid="ffffffff-0000-4000-8000-000000000001", serial="GENTOO-VM-DISPOSABLE", children=kids))


def scan(lab):
    return lab.scanner().scan(ScanOptions())


def setup(lab, top: Path, subvolumes: dict[int, str], listing: str, default: str = "ID 5 (FS_TREE)") -> None:
    lab.system.attach("/dev/vda1", top)
    lab.system.subvolumes["/dev/vda1"] = {5: "", **subvolumes}
    lab.system.btrfs_text["/dev/vda1"] = listing
    lab.system.btrfs_default["/dev/vda1"] = default


def test_plain_btrfs_root_is_the_top_level(lab_factory, tmp_path: Path) -> None:
    lab = lab_factory(btrfs_disk())
    top = fr.make_root(tmp_path / "top", "gentoo")
    setup(lab, top, {}, "")
    result = scan(lab)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate["rootflags"] == "" and candidate["subvol"]["id"] == 5 and candidate["kind"] == "root" and candidate["os"]["id"] == "gentoo"
    mount = lab.system.commands_named("mount")[0]
    assert "subvolid=5" in mount[4].split(",") and "rescue=nologreplay" in mount[4].split(",")


def test_fedora_root_and_home_subvolumes(lab_factory, tmp_path: Path) -> None:
    lab = lab_factory(btrfs_disk("fedora"))
    top = tmp_path / "top"
    fr.make_root(top / "root", "fedora", btrfs=True)
    fr.put(top, "home/user/file.txt", "hello")
    setup(lab, top, {256: "root", 257: "home"}, "ID 256 gen 20 top level 5 path root\nID 257 gen 21 top level 5 path home\n")
    result = scan(lab)
    assert [(item["os"]["id"], item["rootflags"], item["subvol"]["path"]) for item in result.candidates] == [("fedora", "subvolid=256", "root")]


def test_arch_and_ubuntu_at_layout(lab_factory, tmp_path: Path) -> None:
    lab = lab_factory(btrfs_disk())
    top = tmp_path / "top"
    fr.make_root(top / "@", "arch")
    fr.put(top, "@home/u/x", "x")
    fr.put(top, "@log/x", "x")
    listing = "".join(f"ID {256 + i} gen 9 top level 5 path {name}\n" for i, name in enumerate(["@", "@home", "@log", "@pkg", "@.snapshots"]))
    (top / "@pkg").mkdir()
    (top / "@.snapshots").mkdir()
    setup(lab, top, {256: "@", 257: "@home", 258: "@log", 259: "@pkg", 260: "@.snapshots"}, listing)
    result = scan(lab)
    assert [(item["os"]["id"], item["subvol"]["path"], item["rootflags"]) for item in result.candidates] == [("arch", "@", "subvolid=256")]


def opensuse_top(tmp_path: Path) -> tuple[Path, dict[int, str], str]:
    top = tmp_path / "top"
    fr.make_root(top / "@/.snapshots/1/snapshot", "opensuse")
    fr.make_root(top / "@/.snapshots/2/snapshot", "opensuse")
    (top / "@/boot/grub2/x86_64-efi").mkdir(parents=True)
    ids = {256: "@", 257: "@/.snapshots", 258: "@/.snapshots/1/snapshot", 259: "@/.snapshots/2/snapshot", 260: "@/home", 261: "@/boot/grub2/x86_64-efi"}
    listing = "".join(f"ID {i} gen 30 top level 5 path {path}\n" for i, path in ids.items())
    return top, ids, listing


def test_opensuse_default_snapshot_subvolume_is_the_writable_root(lab_factory, tmp_path: Path) -> None:
    """The ACTIVE root is the default subvolume @/.snapshots/N/snapshot, not a read-only snapshot (review R10)."""
    lab = lab_factory(btrfs_disk())
    top, ids, listing = opensuse_top(tmp_path)
    setup(lab, top, ids, listing, default="ID 258 gen 30 top level 257 path @/.snapshots/1/snapshot")
    by_path = {item["subvol"]["path"]: item for item in scan(lab).candidates}
    active = by_path["@/.snapshots/1/snapshot"]
    assert active["kind"] == "root" and active["subvol"]["default"] is True and active["subvol"]["read_only"] is False
    assert by_path["@/.snapshots/2/snapshot"]["kind"] == "snapshot"
    assert any("read-only history" in warning for warning in by_path["@/.snapshots/2/snapshot"]["warnings"])
    assert active["rootflags"] == "subvolid=258" and active["os"]["id"] == "opensuse-tumbleweed"


def test_opensuse_after_a_rollback_the_other_snapshot_becomes_the_root(lab_factory, tmp_path: Path) -> None:
    lab = lab_factory(btrfs_disk())
    top, ids, listing = opensuse_top(tmp_path)
    setup(lab, top, ids, listing, default="ID 259 gen 31 top level 257 path @/.snapshots/2/snapshot")
    by_path = {item["subvol"]["path"]: item for item in scan(lab).candidates}
    assert by_path["@/.snapshots/2/snapshot"]["kind"] == "root" and by_path["@/.snapshots/2/snapshot"]["subvol"]["default"] is True
    assert by_path["@/.snapshots/1/snapshot"]["kind"] == "snapshot"


def test_a_read_only_default_subvolume_is_flagged(lab_factory, tmp_path: Path) -> None:
    lab = lab_factory(btrfs_disk())
    top, ids, listing = opensuse_top(tmp_path)
    setup(lab, top, ids, listing, default="ID 258 gen 30 top level 257 path @/.snapshots/1/snapshot")
    lab.system.btrfs_ro[("/dev/vda1", "@/.snapshots/1/snapshot")] = True
    by_path = {item["subvol"]["path"]: item for item in scan(lab).candidates}
    assert by_path["@/.snapshots/1/snapshot"]["subvol"]["read_only"] is True
    assert any("read-only" in warning for warning in by_path["@/.snapshots/1/snapshot"]["warnings"])


def test_timeshift_snapshots_are_snapshot_candidates(lab_factory, tmp_path: Path) -> None:
    lab = lab_factory(btrfs_disk())
    top = tmp_path / "top"
    fr.make_root(top / "@", "debian")
    fr.make_root(top / "timeshift-btrfs/snapshots/2024-10-01_00-00-01/@", "debian")
    fr.put(top, "@home/u/x", "x")
    ids = {256: "@", 257: "@home", 258: "timeshift-btrfs/snapshots/2024-10-01_00-00-01/@"}
    setup(lab, top, ids, "".join(f"ID {i} gen 9 top level 5 path {p}\n" for i, p in ids.items()))
    kinds = {item["subvol"]["path"]: item["kind"] for item in scan(lab).candidates}
    assert kinds == {"@": "root", "timeshift-btrfs/snapshots/2024-10-01_00-00-01/@": "snapshot"}


def test_the_text_listing_is_used_when_json_is_unavailable(lab_factory, tmp_path: Path) -> None:
    lab = lab_factory(btrfs_disk())
    top = tmp_path / "top"
    fr.make_root(top / "root", "fedora", btrfs=True)
    setup(lab, top, {256: "root"}, "ID 256 gen 20 top level 5 path root\n")
    lab.system.btrfs_json = False
    result = scan(lab)
    assert [item["subvol"]["path"] for item in result.candidates] == ["root"]
    assert lab.system.commands_with("btrfs", "subvolume", "list", "-a")  # the fallback ran


def test_container_storage_below_a_system_is_not_a_root(lab_factory, tmp_path: Path) -> None:
    lab = lab_factory(btrfs_disk())
    top = tmp_path / "top"
    fr.make_root(top / "@", "debian")
    fr.make_root(top / "@/var/lib/docker/btrfs/subvolumes/abc", "alpine")
    ids = {256: "@", 257: "@/var/lib/docker/btrfs/subvolumes/abc"}
    setup(lab, top, ids, "".join(f"ID {i} gen 9 top level 5 path {p}\n" for i, p in ids.items()))
    assert [item["subvol"]["path"] for item in scan(lab).candidates] == ["@"]


def test_parsers() -> None:
    text = "ID 256 gen 12 cgen 7 parent 5 top level 5 otime 2024-10-01 00:00:01 path <FS_TREE>/@\nID 257 gen 9 top level 256 path @/home dir\n"
    assert [(sv.id, sv.path) for sv in btrfs.parse_list_text(text)] == [(256, "@"), (257, "@/home dir")]  # a path may contain spaces
    doc = json.dumps({"subvolume-list": [{"id": 5, "path": "@x", "top_level": 5}, {"id": 256, "path": "<FS_TREE>/root"}]})
    assert [(sv.id, sv.path) for sv in btrfs.parse_list_json("warning\n" + doc)] == [(5, "@x"), (256, "root")]
    assert btrfs.parse_default("ID 5 (FS_TREE)") == 5
    assert btrfs.parse_default("ID 258 gen 30 top level 257 path @/.snapshots/1/snapshot") == 258
    assert btrfs.parse_default("garbage") == 5
    assert btrfs.Subvolume(1, "@/.snapshots/12/snapshot").is_snapshot and not btrfs.Subvolume(1, "@/home").is_snapshot
