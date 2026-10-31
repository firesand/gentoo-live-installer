"""The VM probe of the system tuning (vm_probes.TUNING_COMMAND): what the installed system must show, from the plan the daemon left."""
from __future__ import annotations

import subprocess

import vm_probes
from gentoo_installerd import hwplan

from tests.hardware.builders import disk, manifest, profile, systemd
from tests.vm.helpers import PUBLIC_KEY, load_vm_module

vm = load_vm_module()

GOOD = """sysctl vm/vfs_cache_pressure 50
sysctl vm/dirty_bytes 268435456
sysctl vm/swappiness 150
sysctl vm/page-cluster 0
thp_defrag always defer [defer+madvise] madvise never
thp_ptes 409
sched nvme0n1 0 [kyber] none mq-deadline bfq
sched vda 1 [mq-deadline] none
sched sr0 1 [bfq] none
hda 0
cmdline BOOT_IMAGE=/vmlinuz root=UUID=x ro zswap.enabled=0 quiet
"""


def nvme_plan(**hardware) -> dict:
    machine = profile(disks=(disk("/dev/nvme0n1"),))
    return hwplan.plan(manifest(hardware={"zram": "yes", **hardware}), machine).to_dict()


def test_the_plan_says_what_to_expect_and_an_older_plan_expects_nothing():
    assert vm_probes.tuning_expected(nvme_plan()) == {"zram": True, "schedulers": True, "thp": True, "audio": True}
    assert vm_probes.tuning_expected(nvme_plan(tuning=False)) is None
    old = nvme_plan()
    del old["scalars"]["tuning"]  # the plan of RC3 and older ISOs
    assert vm_probes.tuning_expected(old) is None
    redacted = hwplan.plan(systemd(hardware={"zram": "no"}), profile()).to_dict(redact_files=True)
    assert vm_probes.tuning_expected(redacted) == {"zram": False, "schedulers": True, "thp": True, "audio": True}


def test_a_tuned_system_passes():
    result = vm_probes.parse_tuning_probe(0, GOOD, vm_probes.tuning_expected(nvme_plan()))
    assert result.success, result.error


def test_every_missing_setting_is_named():
    bad = (GOOD.replace("vfs_cache_pressure 50", "vfs_cache_pressure 100").replace("swappiness 150", "swappiness 60")
           .replace("[defer+madvise]", "defer+madvise").replace("[kyber] none", "kyber [none]")
           .replace(" zswap.enabled=0", "").replace("hda 0", "hda 1") + "sysctl_error sysctl: cannot stat /proc/sys/kernel/foo: No such file\n")
    result = vm_probes.parse_tuning_probe(0, bad, vm_probes.tuning_expected(nvme_plan()))
    for words in ("vfs_cache_pressure", "swappiness", "defer+madvise", "nvme0n1 uses the none scheduler", "zswap.enabled=0", "power_save", "cannot stat"):
        assert words in result.error, words
    assert "vda" not in result.error and "sr0" not in result.error  # virtio disks and CD drives have no rule


def test_a_refused_key_marked_with_a_dash_passes_and_an_unknown_one_does_not():
    # The real OpenRC and systemd VMs of RC4: no hardware NMI watchdog, so kernel.nmi_watchdog (written "-kernel...") is refused.
    refused = GOOD + 'sysctl_error sysctl: setting key "kernel.nmi_watchdog", ignoring: Unknown error 524\n'
    assert vm_probes.parse_tuning_probe(0, refused, vm_probes.tuning_expected(nvme_plan())).success
    # The manual-kernel VM of RC4, with 6.18.57: the same key is read-only there, and procps says so without ", ignoring".
    read_only = GOOD + 'sysctl_error sysctl: setting key "kernel.nmi_watchdog": Operation not permitted\n'
    assert vm_probes.parse_tuning_probe(0, read_only, vm_probes.tuning_expected(nvme_plan())).success
    for line in ('sysctl: "net.core.netdev_max_backlg" is an unknown key, ignoring',
                 'sysctl: setting key "vm.vfs_cache_pressure": Invalid argument',
                 'sysctl: setting key "vm.vfs_cache_pressure": Operation not permitted',
                 'sysctl: setting key "kernel.nmi_watchdog": Invalid argument'):
        result = vm_probes.parse_tuning_probe(0, GOOD + f"sysctl_error {line}\n", vm_probes.tuning_expected(nvme_plan()))
        assert not result.success and line.removeprefix("sysctl: ") in result.error, line


def test_without_zram_the_swap_values_are_not_checked():
    expected = vm_probes.tuning_expected(nvme_plan(zram="no"))
    output = GOOD.replace("swappiness 150", "swappiness 60").replace(" zswap.enabled=0", "")
    assert vm_probes.parse_tuning_probe(0, output, expected).success


def test_the_probe_runs_only_for_a_plan_with_tuning_and_the_command_is_valid_shell():
    plan = vm_probes.parse_hardware_plan_file(__import__("json").dumps({"plan": nvme_plan()}))
    names = {probe[0] for probe in vm.installed_system_probes(manifest(), vm.SCENARIO_MAP["defaults"], hardware_plan=plan)}
    assert "tuning" in names
    old = dict(plan, scalars={k: v for k, v in plan["scalars"].items() if k != "tuning"})
    assert "tuning" not in {probe[0] for probe in vm.installed_system_probes(manifest(), vm.SCENARIO_MAP["defaults"], hardware_plan=old)}
    subprocess.run(["sh", "-n", "-c", vm_probes.TUNING_COMMAND], check=True)
