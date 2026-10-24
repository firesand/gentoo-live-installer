"""STOR review fixes: hibernation needs a swap partition, one swap size for every reader, the preflight tools, the fstab preview."""

from __future__ import annotations

import pytest

from gentoo_installer import layout
from gentoo_installer.manifest import InstallManifest
from gentoo_installerd import hwplan
from gentoo_installerd.install import tools_for_selection
from gentoo_installerd.storage import StorageError

from tests.backend.test_storage_identity import erase, vda
from tests.backend.test_storage_support import StatefulRunner, make_manager
from tests.hardware.builders import manifest as hw_manifest
from tests.hardware.builders import profile


@pytest.mark.parametrize("changes", [
    {"swap_kind": "file", "swap_choice": "hibernate", "swap_mib": 16384},
    {"swap_kind": "auto", "swap_choice": "custom", "swap_mib": 16384, "luks": True, "luks_passphrase": "long-enough-pass"},
    {"swap_kind": "zram"},
    {"swap_kind": "partition", "swap_choice": "none"},
])
def test_hibernation_without_a_swap_partition_is_refused_before_any_disk_command(tmp_path, changes):
    m = erase(**changes)
    m.hardware.hibernate = True
    runner = StatefulRunner([vda()])
    with pytest.raises(StorageError, match="Hibernation needs a swap partition"):
        make_manager(runner, tmp_path).prepare(m)
    assert not any(c[0] in {"sgdisk", "parted", "wipefs", "cryptsetup", "mkfs.ext4", "mkswap"} for c in runner.commands)


def test_hibernation_with_a_swap_partition_is_accepted():
    m = erase(swap_kind="partition", swap_choice="hibernate")
    assert layout.swap_kind_problem(m.storage, True) == ""
    assert layout.swap_kind_problem(erase(swap_kind="file").storage, False) == ""


def test_the_swap_size_follows_the_choice_and_the_kind():
    s = erase(swap_choice="hibernate", swap_kind="partition", swap_mib=4096).storage
    assert layout.effective_swap_mib(s, 16 * 1024 * 1024) > 16 * 1024
    assert layout.effective_swap_partition_mib(s, 16 * 1024 * 1024) == layout.effective_swap_mib(s, 16 * 1024 * 1024)
    f = erase(swap_choice="recommended", swap_kind="file").storage
    assert layout.effective_swap_mib(f, 16 * 1024 * 1024) == 8192 and layout.effective_swap_partition_mib(f, 16 * 1024 * 1024) == 0
    assert layout.effective_swap_mib(erase(swap_kind="zram", swap_mib=4096).storage) == 0
    assert layout.effective_swap_mib(erase(swap_choice="none", swap_mib=4096).storage) == 0


def test_hwplan_sees_the_resolved_hibernation_swap():
    """An applied rec.hibernate_swap only sets swap_choice; the plan must size the swap from it, not from the stale swap_mib."""
    machine = profile(ram_gib=16)
    m = hw_manifest(hardware={"hibernate": True}, storage={"swap_mib": 4096, "swap_choice": "hibernate", "swap_kind": "partition"})
    assert "hw.hibernate_swap_small" not in {i.code for i in hwplan.validate_plan(m, machine)}
    f = hw_manifest(hardware={"hibernate": True}, storage={"swap_mib": 20480, "swap_choice": "custom", "swap_kind": "file"})
    assert "hw.hibernate_swap_small" in {i.code for i in hwplan.validate_plan(f, machine) if i.severity == "error"}


def test_the_preflight_asks_for_f2fs_and_blockdev_tools():
    m = InstallManifest()
    m.storage.filesystem = "f2fs"
    assert {"mkfs.f2fs", "blockdev"} <= tools_for_selection(m)
    m = InstallManifest()
    m.storage.swap_choice, m.storage.swap_mib = "none", 4096
    assert not {"swapon", "swapoff"} & tools_for_selection(m)
