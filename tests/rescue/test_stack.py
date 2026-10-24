"""Unlock, LVM activation, md assembly and their release (secrets only on stdin)."""

from __future__ import annotations

import json

import pytest

from gentoo_installerd.rescue.errors import RescueError
from gentoo_installerd.rescue.stack import hex_only, mapper_name, parse_export

from . import fakeroot as fr
from . import lsblk_fixtures as lf
from .fakes import LuksSpec, MdSpec, VgSpec, assert_nothing_destructive

PASSPHRASE = "correct horse battery staple"
MAPPER = "rescue-1f2e3d4c5b6a"


def luks_lab(lab_factory, **kwargs):
    lab = lab_factory(lambda system: lf.luks_lvm_ext4(unlocked=bool(system.mapped), activated=system.vgs["vg0"].active), **kwargs)
    lab.system.luks["/dev/nvme0n1p3"] = LuksSpec(fr.LUKS_UUID, PASSPHRASE, tokens=("systemd-tpm2",))
    lab.system.vgs["vg0"] = VgSpec("vg0", "vguuid-0", [f"/dev/mapper/{MAPPER}"], [{"name": "root", "size": 80 << 30}, {"name": "swap", "size": 8 << 30}])
    lab.add_tree("/dev/mapper/vg0-root", "gentoo", firmware="uefi")
    return lab


def test_unlock_sends_the_passphrase_on_stdin_only(lab_factory) -> None:
    lab = luks_lab(lab_factory)
    stack = lab.stack()
    node = lab.graph().find("/dev/nvme0n1p3")
    result = stack.unlock(node, PASSPHRASE)
    command = lab.system.commands_with("cryptsetup", "open")[0]
    assert command == ("cryptsetup", "open", "--type", "luks", "--key-file=-", "/dev/nvme0n1p3", MAPPER)
    call = [c for c in lab.system.calls if c.argv == command][0]
    assert call.stdin == PASSPHRASE and call.secret is True and not call.stdin.endswith("\n")
    assert all(PASSPHRASE not in arg for argv in lab.system.commands for arg in argv)
    assert result["mapper"] == f"/dev/mapper/{MAPPER}" and result["ours"] is True and result["luks"]["version"] == 2
    assert [entry.kind for entry in lab.ledger] == ["luks"] and lab.ledger.entries[0].data["name"] == MAPPER
    assert MAPPER in lab.ctx.owned_maps
    audit = lab.audit.text()
    assert PASSPHRASE not in audit and "<redacted>" in audit


def test_luks_metadata_hints(lab_factory) -> None:
    lab = luks_lab(lab_factory, mem_available_kib=512 * 1024)
    info = lab.stack().luks_info(lab.graph().find("/dev/nvme0n1p3"))
    assert info.version == 2 and info.tokens == ["systemd-tpm2"] and info.pbkdf == "argon2id" and info.memory_kib == 1048576
    assert any("TPM2" in hint for hint in info.hints) and any("memory" in hint for hint in info.hints)


def test_wrong_passphrase_is_a_typed_error_with_a_keyboard_hint(lab_factory) -> None:
    lab = luks_lab(lab_factory)
    stack = lab.stack()
    with pytest.raises(RescueError) as info:
        stack.unlock(lab.graph().find("/dev/nvme0n1p3"), "wrong")
    assert info.value.code == "rescue.bad_passphrase" and "keyboard layout" in info.value.message
    assert lab.ledger.entries == [] and lab.system.mapped == {}
    assert "wrong" not in lab.audit.text().replace("bad_passphrase", "")


@pytest.mark.parametrize("rc,code", [(3, "no_memory"), (4, "not_luks"), (5, "already_open"), (1, "command_failed")])
def test_cryptsetup_exit_codes_are_mapped(lab_factory, rc: int, code: str) -> None:
    lab = luks_lab(lab_factory)
    lab.system.script(("cryptsetup", "open"), "boom", rc=rc)
    with pytest.raises(RescueError) as info:
        lab.stack().unlock(lab.graph().find("/dev/nvme0n1p3"), PASSPHRASE)
    assert info.value.short == code


def test_an_already_open_container_is_adopted_not_owned(lab_factory) -> None:
    lab = luks_lab(lab_factory)
    lab.system.mapped["cryptroot"] = "/dev/nvme0n1p3"
    data = json.loads(lf.luks_lvm_ext4(unlocked=True, mapper="cryptroot"))
    lab.system._lsblk = json.dumps(data)
    result = lab.stack().unlock(lab.graph().find("/dev/nvme0n1p3"), PASSPHRASE)
    assert result["preexisting"] is True and result["ours"] is False and result["mapper"] == "/dev/mapper/cryptroot"
    assert not lab.system.commands_with("cryptsetup", "open") and lab.ledger.entries == []


def test_test_only_and_readonly_variants(lab_factory) -> None:
    lab = luks_lab(lab_factory)
    stack = lab.stack()
    node = lab.graph().find("/dev/nvme0n1p3")
    assert stack.unlock(node, PASSPHRASE, test_only=True)["ok"] is True
    assert lab.system.mapped == {} and lab.ledger.entries == []
    stack.unlock(node, PASSPHRASE, readonly=True)
    assert "--readonly" in lab.system.commands_with("cryptsetup", "open")[-1]
    assert lab.ledger.entries[0].data["readonly"] is True


def test_passphrases_with_line_breaks_or_nothing_are_refused(lab_factory) -> None:
    lab = luks_lab(lab_factory)
    stack = lab.stack()
    node = lab.graph().find("/dev/nvme0n1p3")
    for bad in ("two\nlines", "carriage\rreturn", "nul\x00byte"):
        with pytest.raises(RescueError) as info:
            stack.unlock(node, bad)
        assert info.value.short == "bad_request"
    with pytest.raises(RescueError):
        stack.unlock(node, None)


def test_the_live_medium_can_never_be_unlocked(lab_factory) -> None:
    lab = lab_factory(lf.usb_live_hybrid())
    lab.system.live_mounts.append(("/dev/sdb1", "/run/initramfs/live"))
    lab.system.luks["/dev/sdb2"] = LuksSpec("x", "y")
    node = lab.graph().find("/dev/sdb2")
    assert node.live_medium
    with pytest.raises(RescueError) as info:
        lab.stack().unlock(node, "y")
    assert info.value.code == "rescue.live_medium" and not lab.system.commands_with("cryptsetup", "open")


def test_unlocking_a_volume_that_is_not_luks(lab_factory) -> None:
    lab = lab_factory(lf.luks_lvm_ext4())
    with pytest.raises(RescueError) as info:
        lab.stack().unlock(lab.graph().find("/dev/nvme0n1p3"), "x")
    assert info.value.short == "not_luks"


def test_lvm_inventory_ignores_warnings_on_stderr(lab_factory) -> None:
    lab = luks_lab(lab_factory)
    stack = lab.stack()
    assert stack.lvm_inventory() == []  # the PV sits behind a locked container
    stack.unlock(lab.graph().find("/dev/nvme0n1p3"), PASSPHRASE)
    groups = stack.lvm_inventory()
    assert len(groups) == 1 and groups[0].name == "vg0" and groups[0].complete and not groups[0].active
    assert [lv["name"] for lv in groups[0].lvs] == ["root", "swap"]


def test_lvm_sizes_are_asked_for_in_bytes_and_read_the_way_lvm_prints_them(lab_factory) -> None:
    """lvm2 prints '<19.00g' unless told otherwise (units "r" with a suffix is its default): the inventory asks for bytes and must never trip over a size."""
    lab = luks_lab(lab_factory)
    lab.system.vgs["vg0"].lvs[0]["size"] = 19 * (1 << 30) - 4096  # the human style rounds this up to <19.00g
    stack = lab.stack()
    stack.unlock(lab.graph().find("/dev/nvme0n1p3"), PASSPHRASE)
    groups = stack.lvm_inventory()
    assert groups[0].lvs[0]["size"] == 19 * (1 << 30) - 4096
    for name in ("pvs", "lvs"):
        argv = lab.system.commands_named(name)[-1]
        assert argv[argv.index("--units") + 1:argv.index("--units") + 3] == ("b", "--nosuffix"), argv


@pytest.mark.parametrize("printed,expected", [
    ("21474836480", 21474836480), ("<19.00g", 19 << 30), (">2.00t", 2 << 40), ("512.00m", 512 << 20), ("100.00k", 100 << 10), ("4096B", 4096), ("1000.00b", 1000),
    ("", 0), ("unknown", 0), ("<g", 0), (None, 0),
])
def test_an_lv_size_in_any_format_lvm_uses_never_breaks_the_inventory(lab_factory, printed, expected) -> None:
    lab = luks_lab(lab_factory)
    lab.system.mapped[MAPPER] = "/dev/nvme0n1p3"  # the VG is visible
    row = {"lv_name": "root", "vg_name": "vg0", "lv_path": "/dev/vg0/root", "lv_attr": "-wi-a-----", "lv_size": printed, "segtype": "linear", "pool_lv": "", "origin": "", "lv_uuid": "x"}
    lab.system.script("lvs", json.dumps({"report": [{"lv": [row]}]}))
    groups = lab.stack().lvm_inventory()
    assert [lv["size"] for lv in groups[0].lvs] == [expected]


def test_activating_a_volume_group_by_name_only(lab_factory) -> None:
    lab = luks_lab(lab_factory)
    stack = lab.stack()
    stack.unlock(lab.graph().find("/dev/nvme0n1p3"), PASSPHRASE)
    result = stack.activate_vg("vg0")
    assert lab.system.commands_with("vgchange")[0] == ("vgchange", "-ay", "--nohints", "vg0")
    assert result["preexisting"] is False and [lv["name"] for lv in result["lvs"]] == ["root", "swap"] and all(lv["active"] for lv in result["lvs"])
    assert "vg0" in lab.ctx.owned_vgs and [entry.kind for entry in lab.ledger] == ["luks", "vg"]
    assert not [argv for argv in lab.system.commands if "--activationmode" in argv]
    again = stack.activate_vg("vg0")
    assert again["preexisting"] is True and len(lab.system.commands_with("vgchange")) == 1


def test_an_already_active_volume_group_is_left_alone(lab_factory) -> None:
    lab = luks_lab(lab_factory)
    lab.system.mapped[MAPPER] = "/dev/nvme0n1p3"
    lab.system.vgs["vg0"].active = True
    result = lab.stack().activate_vg("vg0")
    assert result["preexisting"] is True and not lab.system.commands_with("vgchange")
    assert lab.ledger.entries == []


def test_duplicate_incomplete_and_thin_volume_groups_are_refused(lab_factory) -> None:
    lab = luks_lab(lab_factory)
    lab.system.mapped[MAPPER] = "/dev/nvme0n1p3"
    lab.system.vgs["vg0"].pvs.append("/dev/mapper/" + MAPPER)
    # a second VG with the same name but another UUID (cloned disk)
    lab.system.vgs["vg0-clone"] = VgSpec("vg0", "vguuid-OTHER", [f"/dev/mapper/{MAPPER}"], [{"name": "root"}])
    with pytest.raises(RescueError) as info:
        lab.stack().activate_vg("vg0")
    assert info.value.short == "duplicate_vg"
    del lab.system.vgs["vg0-clone"]
    lab.system.vgs["vg0"].missing = 1
    with pytest.raises(RescueError) as info:
        lab.stack().activate_vg("vg0")
    assert info.value.short == "degraded"
    lab.system.vgs["vg0"].missing = 0
    lab.system.vgs["vg0"].lvs.append({"name": "pool", "segtype": "thin-pool"})
    lab2 = lab.stack()
    lab2.host = type(lab.host)(lab.host_root, which=lambda name: None)
    with pytest.raises(RescueError) as info:
        lab2.activate_vg("vg0")
    assert info.value.short == "unsupported_fs" and "thin_check" in info.value.message
    with pytest.raises(RescueError):
        lab.stack().activate_vg("nope")
    assert not lab.system.commands_with("vgchange")


def test_release_deactivates_what_sits_above_what_we_opened(lab_factory) -> None:
    lab = luks_lab(lab_factory)
    stack = lab.stack()
    stack.unlock(lab.graph().find("/dev/nvme0n1p3"), PASSPHRASE)
    # udev (or the user) activated the VG after the unlock; release must still deactivate it before closing the container
    lab.system.vgs["vg0"].active = True
    for entry in reversed(lab.ledger.entries):
        stack.release(entry.kind, entry.data)
    commands = lab.system.commands
    assert commands.index(("vgchange", "-an", "--nohints", "vg0")) < commands.index(("cryptsetup", "close", MAPPER))
    assert lab.system.mapped == {} and lab.system.vgs["vg0"].active is False


def test_release_stops_arrays_we_assembled(lab_factory) -> None:
    lab = lab_factory(lambda s: lf.md_raid1_under_lvm() if s.md["x"].assembled else lf.md_raid1_unassembled())
    lab.system.md["x"] = MdSpec("aaaa1111:bbbb2222:cccc3333:44445555", ["/dev/sdb1", "/dev/sdc1"], name="host:data", device="/dev/md127")
    stack = lab.stack()
    stack.assemble_md(["aaaa1111-bbbb-2222-cccc-333344445555"])
    stack.release("md", lab.ledger.entries[0].data)
    assert lab.system.commands_with("mdadm", "--stop") == [("mdadm", "--stop", "/dev/md127")]
    assert lab.system.md["x"].assembled is False


def test_md_assembly_uses_a_private_read_only_config(lab_factory) -> None:
    lab = lab_factory(lambda s: lf.md_raid1_under_lvm() if s.md["x"].assembled else lf.md_raid1_unassembled())
    lab.system.md["x"] = MdSpec("aaaa1111:bbbb2222:cccc3333:44445555", ["/dev/sdb1", "/dev/sdc1"], name="host:data", device="/dev/md127")
    stack = lab.stack()
    inventory = stack.md_inventory()
    assert len(inventory) == 1 and inventory[0].level == "raid1" and not inventory[0].degraded and inventory[0].state == "not_assembled"
    result = stack.assemble_md(["aaaa1111-bbbb-2222-cccc-333344445555"])
    conf = lab.state_dir / "sessions" / "pool.mdadm.conf"
    text = conf.read_text()
    assert text.startswith("AUTO +all\n") and "ARRAY /dev/md/data" in text and "aaaa1111:bbbb2222" in text
    assert oct(conf.stat().st_mode & 0o777) == "0o600"
    assert lab.system.commands_with("mdadm", "--assemble")[0] == ("mdadm", "--assemble", "--scan", "--readonly", f"--config={conf}")
    assert result["arrays"][0]["device"] == "/dev/md127" and result["arrays"][0]["preexisting"] is False
    assert lab.ledger.entries[0].kind == "md" and "/dev/md127" in lab.ctx.owned_md
    assert stack.assemble_md(["aaaa1111-bbbb-2222-cccc-333344445555"])["arrays"][0]["preexisting"] is True
    assert len(lab.system.commands_with("mdadm", "--assemble")) == 1


def test_degraded_arrays_need_an_explicit_confirmation(lab_factory) -> None:
    lab = lab_factory(lambda s: lf.md_raid1_under_lvm() if s.md["x"].assembled else lf.md_raid1_unassembled())
    lab.system.md["x"] = MdSpec("aaaa1111:bbbb2222:cccc3333:44445555", ["/dev/sdb1", "/dev/sdc1"], name="host:data", devices=3)
    with pytest.raises(RescueError) as info:
        lab.stack().assemble_md(["aaaa1111-bbbb-2222-cccc-333344445555"])
    assert info.value.short == "degraded" and not lab.system.commands_with("mdadm", "--assemble")
    lab.stack().assemble_md(["aaaa1111-bbbb-2222-cccc-333344445555"], degraded_ok=True)
    assert "--run" in lab.system.commands_with("mdadm", "--assemble")[0]
    with pytest.raises(RescueError) as info:
        lab.stack().assemble_md(["ffffffff-0000-0000-0000-000000000000"])
    assert info.value.short == "not_found"


def test_imsm_container_lines_are_kept_together() -> None:
    from gentoo_installerd.rescue.stack import StackManager

    text = (
        "ARRAY metadata=imsm UUID=0a1b2c3d:11111111:22222222:33333333\n"
        "ARRAY /dev/md/Volume0 container=0a1b2c3d:11111111:22222222:33333333 member=0 UUID=ffff0000:aaaa1111:bbbb2222:cccc3333\n"
        "ARRAY /dev/md/other metadata=1.2 UUID=99999999:88888888:77777777:66666666 name=h:o\n"
    )
    lines = StackManager._select_array_lines(text, {hex_only("ffff0000:aaaa1111:bbbb2222:cccc3333")})
    assert len(lines) == 2 and "metadata=imsm" in lines[0] and "Volume0" in lines[1]
    assert not any("other" in line for line in lines)


def test_helpers() -> None:
    assert mapper_name("1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b") == MAPPER
    assert hex_only("AA:bb-CC") == "aabbcc"
    assert parse_export("MD_LEVEL=raid1\nnoise\nMD_UUID=a:b\n") == {"MD_LEVEL": "raid1", "MD_UUID": "a:b"}


def test_stack_operations_never_issue_destructive_commands(lab_factory) -> None:
    lab = luks_lab(lab_factory)
    stack = lab.stack()
    stack.unlock(lab.graph().find("/dev/nvme0n1p3"), PASSPHRASE)
    stack.activate_vg("vg0")
    for entry in reversed(lab.ledger.entries):
        stack.release(entry.kind, entry.data)
    assert_nothing_destructive(lab.system)
