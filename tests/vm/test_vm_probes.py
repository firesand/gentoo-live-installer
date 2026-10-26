"""scripts/vm_probes.py: what the harness accepts as proof about an installed system (receipt, swap, EFI fallback, layout, world, hardware plan)."""
from __future__ import annotations

import copy
import json
import shlex
import types
from pathlib import Path

import pytest

import vm_probes
from gentoo_installer.manifest import InstallManifest, Partition
from gentoo_installerd import hwplan
from gentoo_installerd.configure import Configurer
from tests.hardware.helpers import fixture_dict
from tests.vm.helpers import PUBLIC_KEY, load_vm_module

vm = load_vm_module()
UUID = "0a1b2c3d-1111-2222-3333-444455556666"
DATA = Path(__file__).resolve().parent / "data"  # output of the first real VM runs (the record of what the product really prints)


def manifest_of(name: str, **changes) -> InstallManifest:
    manifest = vm.build_manifest(vm.SCENARIO_MAP[name], PUBLIC_KEY)
    for path, value in changes.items():
        section, key = path.split("__")
        setattr(getattr(manifest, section), key, value)
    return manifest


def receipt_of(manifest: InstallManifest, **kwargs) -> dict:
    """The receipt the product itself would write (Configurer.build_receipt), so the probe is tested against the real format."""
    configurer = Configurer(types.SimpleNamespace(run=lambda *args, **options: None), "/nonexistent", dry_run=True)
    values = {"root_uuid": UUID, "luks_uuid": "feedface-0000-1111-2222-333344445555" if manifest.storage.luks else "",
              "boot_separate": bool(manifest.storage.luks)}
    values.update(kwargs)
    return configurer.build_receipt(manifest, **values)


# --- the install receipt -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["openrc", "systemd", "bios", "uefi-luks", "btrfs-root", "lvm-root", "systemd-boot", "defaults", "usb-portable"])
def test_the_receipt_the_installer_writes_passes_for_every_scenario(name):
    manifest = manifest_of(name)
    kwargs = {"root_filesystem": manifest.storage.filesystem}
    result = vm_probes.parse_receipt_probe(0, json.dumps(receipt_of(manifest, **kwargs)), manifest)
    assert result.success and not result.advisory, result.error


def test_a_missing_receipt_is_a_finding_but_a_wrong_one_is_a_failure():
    manifest = manifest_of("openrc")
    missing = vm_probes.parse_receipt_probe(1, "cat: /etc/gentoo-live-installer/install.json: No such file or directory", manifest)
    assert not missing.success and missing.advisory and "missing" in missing.error
    garbage = vm_probes.parse_receipt_probe(0, "not json", manifest)
    assert not garbage.success and not garbage.advisory and "not JSON" in garbage.error
    assert "not a JSON object" in vm_probes.parse_receipt_probe(0, "[1]", manifest).error


@pytest.mark.parametrize(("change", "fragment"), [
    ({"firmware": "bios"}, "firmware is 'bios'"),
    ({"bootloader": "systemd-boot"}, "bootloader"),
    ({"init": "systemd"}, "init is 'systemd'"),
    ({"kernel": "manual"}, "kernel"),
    ({"profile": "default/linux/amd64/23.0/hardened"}, "profile"),
    ({"esp": "/boot/efi"}, "esp is '/boot/efi'"),
    ({"version": 2}, "version 2"),
    ({"created": "yesterday"}, "created"),
    ({"installer": ""}, "no installer version"),
    ({"hardware": "none"}, "hardware is not a list"),
])
def test_every_field_of_the_receipt_is_compared_with_the_manifest(change, fragment):
    manifest = manifest_of("openrc")
    receipt = receipt_of(manifest, root_filesystem="ext4") | change
    result = vm_probes.parse_receipt_probe(0, json.dumps(receipt), manifest)
    assert not result.success and fragment in result.error


def test_the_root_of_the_receipt_is_compared_too():
    manifest = manifest_of("openrc")
    base = receipt_of(manifest, root_filesystem="ext4")
    for key, value, fragment in (("fstype", "xfs", "root.fstype"), ("uuid", "", "root.uuid"), ("luks_uuid", UUID, "luks_uuid"), ("lvm", True, "root.lvm"), ("raid", "1", "root.raid")):
        receipt = copy.deepcopy(base)
        receipt["root"][key] = value
        result = vm_probes.parse_receipt_probe(0, json.dumps(receipt), manifest)
        assert not result.success and fragment in result.error, key
    receipt = copy.deepcopy(base)
    del receipt["root"]["uuid"]
    assert "root is missing" in vm_probes.parse_receipt_probe(0, json.dumps(receipt), manifest).error
    assert "missing keys" in vm_probes.parse_receipt_probe(0, json.dumps({"version": 1}), manifest).error


def test_an_encrypted_install_needs_its_luks_uuid_and_a_separate_boot():
    manifest = manifest_of("uefi-luks")
    receipt = receipt_of(manifest, root_filesystem="ext4")
    assert vm_probes.parse_receipt_probe(0, json.dumps(receipt), manifest).success
    assert "luks_uuid" in vm_probes.parse_receipt_probe(0, json.dumps(receipt | {"root": receipt["root"] | {"luks_uuid": ""}}), manifest).error
    assert "separate /boot" in vm_probes.parse_receipt_probe(0, json.dumps(receipt | {"boot_separate": False}), manifest).error


def test_a_manual_install_is_compared_with_its_root_row_and_its_esp_mount():
    manifest = manifest_of("manual-two-disks")
    manifest.storage.partitions = [Partition("/dev/vda1", "/boot/efi", "vfat", 0, True), Partition("/dev/vda2", "/", "xfs", 0, True)]
    receipt = receipt_of(manifest, root_filesystem="xfs")
    assert receipt["esp"] == "/boot/efi"
    assert vm_probes.parse_receipt_probe(0, json.dumps(receipt), manifest).success
    assert "root.fstype" in vm_probes.parse_receipt_probe(0, json.dumps(receipt | {"root": receipt["root"] | {"fstype": "ext4"}}), manifest).error


# --- the command line of the installed kernel ---------------------------------------------------------------------------

# /proc/cmdline of the first real encrypted install (uefi-nvme-luks-openrc)
REAL_LUKS_CMDLINE = ("BOOT_IMAGE=/vmlinuz-6.18.57-gentoo-dist-bin root=UUID=c29723df-cc13-4a09-9106-7fac70da20a9 ro "
                     "rd.luks.uuid=e1d07902-57c2-4bc7-9223-289102723d3f rd.luks.name=e1d07902-57c2-4bc7-9223-289102723d3f=gentoo-root "
                     "console=tty0 console=ttyS0,115200n8")


def test_the_command_line_of_a_real_encrypted_install_passes():
    result = vm.parse_cmdline_probe(0, REAL_LUKS_CMDLINE + "\n", manifest_of("uefi-nvme-luks-openrc"))
    assert result.success, result.error


def test_the_container_must_carry_the_name_the_storage_stage_gave_it():
    manifest = manifest_of("uefi-nvme-luks-openrc")
    renamed = vm.parse_cmdline_probe(0, REAL_LUKS_CMDLINE.replace("=gentoo-root ", "=gentoo-crypt "), manifest)
    assert not renamed.success and "rd.luks.name=<uuid>=gentoo-root" in renamed.error
    unnamed = vm.parse_cmdline_probe(0, REAL_LUKS_CMDLINE.replace("rd.luks.name=e1d07902-57c2-4bc7-9223-289102723d3f=gentoo-root ", ""), manifest)
    assert not unnamed.success and "rd.luks.name=<uuid>=gentoo-root" in unnamed.error


def test_with_lvm_inside_the_container_dracut_names_the_mapping_gentoo_crypt_and_activates_the_volume():
    manifest = manifest_of("uefi-luks-lvm")
    good = REAL_LUKS_CMDLINE.replace("=gentoo-root ", "=gentoo-crypt ") + " rd.lvm.lv=gentoo/root"
    assert vm.parse_cmdline_probe(0, good, manifest).success
    old_name = vm.parse_cmdline_probe(0, REAL_LUKS_CMDLINE + " rd.lvm.lv=gentoo/root", manifest)
    assert not old_name.success and "rd.luks.name=<uuid>=gentoo-crypt" in old_name.error and "gentoo-root" in old_name.error
    no_volume = vm.parse_cmdline_probe(0, good.replace(" rd.lvm.lv=gentoo/root", ""), manifest)
    assert not no_volume.success and "rd.lvm.lv=gentoo/root" in no_volume.error


def test_lvm_without_encryption_needs_its_volume_activated_too():
    manifest = manifest_of("lvm-root")
    plain = "BOOT_IMAGE=/vmlinuz-6.18.57-gentoo-dist-bin root=UUID=c29723df-cc13-4a09-9106-7fac70da20a9 ro"
    assert "rd.lvm.lv=gentoo/root" in vm.parse_cmdline_probe(0, plain, manifest).error
    assert vm.parse_cmdline_probe(0, plain + " rd.lvm.lv=gentoo/root", manifest).success
    assert vm.parse_cmdline_probe(0, plain, manifest_of("openrc")).success  # a plain install has nothing to activate


def test_a_manual_layout_is_not_held_to_the_names_of_the_guided_one():
    manifest = manifest_of("uefi-nvme-luks-openrc", storage__mode="manual")
    assert vm.parse_cmdline_probe(0, REAL_LUKS_CMDLINE.replace("=gentoo-root ", "=my-own-name "), manifest).success


# --- swap ----------------------------------------------------------------------------------------------------------

MIB = 1 << 20


def swap_text(*rows: tuple[str, str, int]) -> str:
    return "".join(f"{name} {kind} {size}\n" for name, kind, size in rows)


def test_a_plain_install_keeps_accepting_any_swap_of_the_right_size():
    manifest = manifest_of("openrc")
    assert vm_probes.parse_swap_probe(0, swap_text(("/dev/vda3", "partition", 512 * MIB - 4096)), manifest).success
    assert not vm_probes.parse_swap_probe(0, swap_text(("/dev/zram0", "partition", 512 * MIB)), manifest).success  # zram does not count
    assert not vm_probes.parse_swap_probe(0, "", manifest).success
    assert vm_probes.parse_swap_probe(0, "/dev/vda3 536866816\n", manifest).success  # an older swapon prints no type


def test_an_encrypted_install_must_swap_to_a_file_inside_the_encrypted_root():
    manifest = manifest_of("uefi-luks")
    assert vm_probes.parse_swap_probe(0, swap_text(("/swapfile", "file", 512 * MIB)), manifest).success
    clear = vm_probes.parse_swap_probe(0, swap_text(("/dev/vda3", "partition", 512 * MIB)), manifest)
    assert not clear.success and "raw swap partition" in clear.error and "/dev/vda3" in clear.error
    both = vm_probes.parse_swap_probe(0, swap_text(("/swapfile", "file", 512 * MIB), ("/dev/vda3", "partition", 512 * MIB)), manifest)
    assert not both.success and "raw swap partition" in both.error
    assert "no active swap" in vm_probes.parse_swap_probe(0, swap_text(("/dev/zram0", "partition", 8 * MIB)), manifest).error
    assert vm_probes.parse_swap_probe(0, swap_text(("/swapfile", "file", 512 * MIB), ("/dev/zram0", "partition", 8 * MIB)), manifest).success


def test_no_swap_is_fine_when_none_was_asked_for():
    manifest = manifest_of("uefi-luks", storage__swap_mib=0)
    assert vm_probes.parse_swap_probe(0, "", manifest).success


# --- the removable EFI fallback --------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "changes", "expected"), [
    ("openrc", {}, True),
    ("systemd-boot", {}, True),
    ("bios", {}, False),
    ("openrc", {"hardware__removable_fallback": "never"}, False),
    ("openrc", {"system__bootloader": "efi-stub"}, False),
])
def test_which_installs_must_leave_the_fallback_boot_file(name, changes, expected):
    assert vm_probes.expects_efi_fallback(manifest_of(name, **changes)) is expected


def test_a_shared_esp_of_a_manual_install_is_not_guaranteed_a_fallback():
    manifest = manifest_of("manual-two-disks")
    assert vm_probes.expects_efi_fallback(manifest)
    manifest.storage.partitions[0].format = False
    assert not vm_probes.expects_efi_fallback(manifest)


def test_the_fallback_is_looked_for_on_the_esp_where_the_installer_mounted_it_with_sudo():
    guided = vm_probes.efi_fallback_command(manifest_of("openrc"))
    assert guided.startswith("sudo -S -p '' python3 -c ") and shlex.split(guided)[-1] == "/efi/EFI/BOOT/BOOTX64.EFI"
    manual = manifest_of("manual-two-disks")
    manual.storage.partitions[0].mountpoint = "/boot/efi"
    assert shlex.split(vm_probes.efi_fallback_command(manual))[-1] == "/boot/efi/EFI/BOOT/BOOTX64.EFI"


def test_the_fallback_probe_wants_a_non_empty_efi_program():
    good = json.dumps({"path": "/efi/EFI/BOOT/BOOTX64.EFI", "size": 123456, "magic": "MZ"})
    assert vm_probes.parse_efi_fallback_probe(0, good + "\n").success
    assert "empty" in vm_probes.parse_efi_fallback_probe(0, json.dumps({"path": "p", "size": 0, "magic": ""})).error
    assert "does not start with MZ" in vm_probes.parse_efi_fallback_probe(0, json.dumps({"path": "p", "size": 10, "magic": "#!"})).error
    missing = vm_probes.parse_efi_fallback_probe(1, json.dumps({"path": "p", "error": "No such file"}))
    assert not missing.success and "not readable" in missing.error and not missing.advisory
    assert "no JSON" in vm_probes.parse_efi_fallback_probe(0, "hello").error


def test_a_broken_sudo_is_reported_as_the_reason_not_as_a_missing_file():
    result = vm_probes.parse_efi_fallback_probe(1, "sudo: a password is required")
    assert not result.success and result.advisory and "cannot use sudo" in result.error


OVMF_LOG = (
    '\x1b[2J\x1b[01;01H[=3h\x1b[2J\r\n'
    'BdsDxe: failed to load Boot0001 "UEFI Misc Device" from PciRoot(0x0)/Pci(0x2,0x0): Not Found\r\n'
    'BdsDxe: loading Boot0002 "UEFI QEMU HARDDISK QM00001 " from PciRoot(0x0)/Pci(0x7,0x0)/HD(1,GPT,AAAA,0x800,0x200000)/\\EFI\\BOOT\\BOOTX64.EFI\r\n'
    'BdsDxe: starting Boot0002 "UEFI QEMU HARDDISK QM00001 " from PciRoot(0x0)/Pci(0x7,0x0)/HD(1,GPT,AAAA,0x800,0x200000)/\\EFI\\BOOT\\BOOTX64.EFI\r\n'
)


def test_the_serial_log_shows_which_boot_option_the_firmware_started():
    choices = vm_probes.firmware_boot_choices(OVMF_LOG)
    assert [choice["number"] for choice in choices] == ["0002"] and "QEMU HARDDISK" in choices[0]["name"]
    assert vm_probes.booted_through_fallback(OVMF_LOG)
    registered = OVMF_LOG.replace("\\EFI\\BOOT\\BOOTX64.EFI", "\\EFI\\Gentoo\\grubx64.efi")
    assert not vm_probes.booted_through_fallback(registered) and not vm_probes.booted_through_fallback("")


# --- manual layouts --------------------------------------------------------------------------------------------------------

LAYOUT_ROWS = [Partition("/dev/vda1", "/efi", "vfat"), Partition("/dev/vda2", "/", "ext4"), Partition("/dev/vdb1", "/home", "ext4"), Partition("/dev/vdb2", "", "swap")]
LAYOUT_OUTPUT = "/ /dev/vda2\n/efi /dev/vda1\n/home /dev/vdb1\n/proc proc\n---\n/dev/vdb2\n"


def test_every_row_of_a_manual_layout_must_be_where_the_manifest_put_it():
    assert vm_probes.parse_layout_probe(0, LAYOUT_OUTPUT, LAYOUT_ROWS).success
    swapped = LAYOUT_OUTPUT.replace("/home /dev/vdb1", "/home /dev/vda2")
    result = vm_probes.parse_layout_probe(0, swapped, LAYOUT_ROWS)
    assert not result.success and "/home is mounted from /dev/vda2, the layout says /dev/vdb1" in result.error
    no_swap = vm_probes.parse_layout_probe(0, LAYOUT_OUTPUT.replace("/dev/vdb2\n", ""), LAYOUT_ROWS)
    assert "swap on /dev/vdb2 is not active" in no_swap.error
    assert "nothing" in vm_probes.parse_layout_probe(0, "/ /dev/vda2\n---\n", LAYOUT_ROWS[:3]).error


def test_a_btrfs_subvolume_source_is_compared_by_its_device():
    rows = [Partition("/dev/vda2", "/", "btrfs")]
    assert vm_probes.parse_layout_probe(0, "/ /dev/vda2[/@]\n---\n", rows).success


# --- the world -------------------------------------------------------------------------------------------------------------

NOTHING_TO_DO = "\nThese are the packages that would be merged, in order:\n\nCalculating dependencies... done!\nDependency resolution took 11.38 s (backtrack: 0/20).\n\nNothing to merge; quitting.\n"
UPDATES = (
    "[ebuild     U  ] sys-libs/zlib-1.3.1-r1 [1.3.1] USE=\"minizip -static-libs\" 1,345 KiB\n"
    "[binary   R    ] dev-libs/foo-1.0 USE=\"+bar -baz\" 120 KiB\n"
    "[ebuild  N     ] x11-libs/new-1.0 USE=\"a\" 10 KiB\n"
    "[ebuild   R    ] dev-libs/drift-2.0 USE=\"b\" 10 KiB\n"
    "\nTotal: 4 packages (1 upgrade, 1 new, 2 reinstalls), Size of downloads: 2,000 KiB\n")


def test_a_world_with_nothing_to_do_is_clean():
    resolve, drift = vm_probes.parse_world_probe(0, NOTHING_TO_DO)
    assert resolve.name == "world-resolve" and resolve.success and not resolve.advisory
    assert drift.name == "world-drift" and drift.success and not drift.advisory and "0 upgrades, 0 new, 0 reinstalls" in drift.output
    assert drift.data["counts"] == {"upgrades": 0, "new": 0, "reinstalls": 0}


def test_updates_and_reinstalls_resolve_and_are_information_only():
    """A tree synced at install time is newer than the stage3: Portage wants upgrades and reinstalls the installer did not cause."""
    resolve, drift = vm_probes.parse_world_probe(0, UPDATES)
    assert resolve.success
    assert vm_probes.summarize_world(UPDATES) == {"new": ["x11-libs/new-1.0"], "upgrade": ["sys-libs/zlib-1.3.1-r1"], "rebuild": ["dev-libs/foo-1.0", "dev-libs/drift-2.0"]}
    assert drift.name == "world-drift" and drift.success and not drift.advisory and drift.error == ""  # never a finding, never a failure
    assert drift.data == {"counts": {"upgrades": 1, "new": 1, "reinstalls": 2}, "upgrades": ["sys-libs/zlib-1.3.1-r1"], "new": ["x11-libs/new-1.0"],
                          "reinstalls": ["dev-libs/foo-1.0", "dev-libs/drift-2.0"]}
    assert "1 upgrades, 1 new, 2 reinstalls" in drift.output and "dev-libs/drift-2.0" in drift.output and "information only" in drift.output


def test_the_world_of_the_first_real_run_resolves_and_its_drift_is_listed():
    """tests/vm/data/emerge-world-*.log is the output of 'emerge -pvuDN @world' in the first real VM install: 4 upgrades, 3 new packages, 5 reinstalls."""
    text = (DATA / "emerge-world-uefi-nvme-luks-openrc.log").read_text()
    groups = vm_probes.summarize_world(text)
    assert (len(groups["upgrade"]), len(groups["new"]), len(groups["rebuild"])) == (4, 3, 5)
    resolve, drift = vm_probes.parse_world_probe(0, text)
    assert resolve.name == "world-resolve" and resolve.success and not resolve.advisory
    assert drift.success and not drift.advisory and drift.error == ""
    assert drift.data["counts"] == {"upgrades": 4, "new": 3, "reinstalls": 5}
    assert drift.data["upgrades"] == ["app-misc/ca-certificates-20260816.3.131.1-1", "sys-libs/timezone-data-2026e-1", "net-misc/curl-8.23.0-1",
                                      "dev-python/ensurepip-pip-26.2.1-r1-1"]
    assert drift.data["reinstalls"] == ["app-arch/zstd-1.5.7-r1-10", "net-misc/iputils-20250605-r4-1", "sys-devel/gettext-0.23.2-7",
                                        "app-crypt/libb2-0.98.1-r3-14", "app-portage/portage-utils-0.97.1-1"]  # no ::repo and no slot
    assert drift.data["new"] == ["net-libs/nghttp2-1.70.0-3", "net-libs/nghttp3-1.18.0-1", "net-libs/ngtcp2-1.25.0-1"]


def test_binary_packages_ignored_for_their_use_do_not_fail_the_world():
    """tests/vm/data/emerge-world-uefi-luks-systemd.log: the world resolves, and Portage adds a '!!!' notice that it builds
    nghttp2 from source because no binary package has its USE flags."""
    text = (DATA / "emerge-world-uefi-luks-systemd.log").read_text()
    assert "!!! The following binary packages have been ignored due to non matching USE:" in text
    resolve, drift = vm_probes.parse_world_probe(0, text)
    assert resolve.success and resolve.error == ""
    assert drift.data["counts"] == {"upgrades": 4, "new": 3, "reinstalls": 5}
    changed = "\n!!! The following binary packages have been ignored due to changed dependencies:\n\n     app-misc/foo-1.0::gentoo\n"
    assert vm_probes.parse_world_probe(0, NOTHING_TO_DO + changed)[0].success
    still_an_error = text + "\n!!! Section 'gentoo' in repos.conf has location attribute set to nonexistent directory: '/var/db/repos/gentoo'\n"
    resolve, _drift = vm_probes.parse_world_probe(0, still_an_error)
    assert not resolve.success and "Portage reported errors (!!!)" in resolve.error


def test_drift_is_information_even_when_the_world_does_not_resolve():
    resolve, drift = vm_probes.parse_world_probe(1, "!!! Problem resolving dependencies for @world\n[ebuild   R    ] dev-libs/drift-2.0 USE=\"b\" 10 KiB\n")
    assert not resolve.success and not resolve.advisory
    assert drift.success and not drift.advisory and drift.data["reinstalls"] == ["dev-libs/drift-2.0"]
    _resolve, nothing = vm_probes.parse_world_probe(137, "Calculating dependencies ...")
    assert nothing.success and nothing.data["counts"] == {"upgrades": 0, "new": 0, "reinstalls": 0} and "status 137" in nothing.output


@pytest.mark.parametrize("text", [
    "!!! Multiple package instances within a single package slot have been pulled\n!!! into the dependency graph, resulting in a slot conflict:\n",
    "[blocks B      ] <app-misc/foo-1.0 (\"<app-misc/foo-1.0\" is soft blocking app-misc/bar-2.0)\n",
    "The following USE changes are necessary to proceed:\n#required by dev-qt/qtbase, required by @world\n>=media-libs/libpng-1.6 -apng\n",
    "WARNING: One or more updates/rebuilds have been skipped due to a dependency conflict:\n",
    "!!! Problem resolving dependencies for @world\n",
    "The following mask changes are necessary to proceed:\n",
    "  dev-qt/qtbase REQUIRED_USE=\"|| ( gles2-only opengl )\"\n",
])
def test_resolution_problems_fail_the_world(text):
    resolve, _drift = vm_probes.parse_world_probe(1, text)
    assert not resolve.success and not resolve.advisory and resolve.error


def test_a_nonzero_exit_without_a_known_message_still_fails_the_world():
    resolve, _drift = vm_probes.parse_world_probe(137, "Calculating dependencies ...")
    assert not resolve.success and "status 137" in resolve.error


def test_a_broken_sudo_is_not_a_verdict_about_the_world():
    [only] = vm_probes.parse_world_probe(1, "sudo: a password is required\n")
    assert only.name == "world-resolve" and only.advisory and "cannot use sudo" in only.error
    assert vm_probes.WORLD_COMMAND.startswith("sudo -S -p ''") and "emerge --color=n -pvuDN @world" in vm_probes.WORLD_COMMAND


# --- a part the installer could not install ------------------------------------------------------------------------------------

def test_a_missing_companion_is_explained_by_what_the_installer_said_about_it():
    """The first real run: 'hwmonux: Probe exited with status 127' and, three probes away, the installer's own words about the package."""
    missing = vm_probes.ProbeResult("hwmonux", False, "bash: line 1: hwmonux: command not found", "Probe exited with status 127")
    reason = "app-misc/hwmonux could not be installed: dev-python/pyside-6.11.2 was killed because the machine ran out of memory"
    explained = vm_probes.explain_missing_part(missing, ["app-emulation/qemu-guest-agent failed", reason])
    assert explained.error == f"Probe exited with status 127 (the installer reported: {reason})" and explained.output == missing.output
    assert vm_probes.explain_missing_part(missing, ["app-emulation/qemu-guest-agent failed"]).error == "Probe exited with status 127"
    assert vm_probes.explain_missing_part(missing, []).error == "Probe exited with status 127"
    fine = vm_probes.ProbeResult("hwmonux", True, "ok")
    assert vm_probes.explain_missing_part(fine, [reason]).error == ""
    other = vm_probes.ProbeResult("network", False, "", "no route")
    assert vm_probes.explain_missing_part(other, [reason]).error == "no route"
    hub = vm_probes.ProbeResult("portagehub", False, "", "Probe exited with status 127")
    assert "app-portage/portagehub failed" in vm_probes.explain_missing_part(hub, ["app-portage/portagehub failed to build"]).error


# --- the hardware plan -----------------------------------------------------------------------------------------------------

def plan_dict(name: str, manifest: InstallManifest) -> dict:
    return hwplan.plan(manifest, fixture_dict(name)).to_dict()


def test_the_plan_for_a_qemu_guest_has_no_intel_microcode_and_the_guest_agent():
    manifest = manifest_of("laptop-smbios", options__microcode=True)  # microcode switched on: a virtual machine must still skip it
    plan = plan_dict("qemu-q35-virtio", manifest)
    assert vm_probes.INTEL_MICROCODE not in vm_probes.plan_atoms(plan)
    assert "app-emulation/qemu-guest-agent" in plan["optional_atoms"]
    assert vm_probes.hardware_plan_problems(plan, manifest, "qemu") == []


def test_the_plan_for_a_vmware_guest_installs_the_vmware_tools():
    manifest = manifest_of("vmware-guest")
    plan = plan_dict("vmware-guest", manifest)
    assert vm_probes.hardware_plan_problems(plan, manifest, "vmware") == []
    assert "app-emulation/open-vm-tools" in plan["optional_atoms"]


def test_guest_tools_that_were_switched_off_must_not_be_in_the_plan():
    manifest = manifest_of("laptop-smbios", hardware__guest_tools="none")
    plan = plan_dict("qemu-q35-virtio", manifest_of("laptop-smbios"))  # a plan made as if they were on
    assert "guest tools are switched off" in " ".join(vm_probes.hardware_plan_problems(plan, manifest, "qemu"))


def test_a_plan_with_microcode_or_without_the_expected_tool_is_reported():
    manifest = manifest_of("laptop-smbios")
    plan = plan_dict("qemu-q35-virtio", manifest)
    plan["early_atoms"] = [*plan["early_atoms"], vm_probes.INTEL_MICROCODE]
    plan["optional_atoms"] = [atom for atom in plan["optional_atoms"] if atom != "app-emulation/qemu-guest-agent"]
    problems = " ".join(vm_probes.hardware_plan_problems(plan, manifest, "qemu"))
    assert "Intel microcode in a virtual machine" in problems and "does not list app-emulation/qemu-guest-agent" in problems
    assert "schema" in " ".join(vm_probes.hardware_plan_problems({"schema": 3}, manifest, "qemu"))


def test_a_portable_install_may_carry_intel_microcode():
    manifest = manifest_of("usb-portable")
    plan = {"schema": 1, "early_atoms": [vm_probes.INTEL_MICROCODE], "optional_atoms": []}
    assert vm_probes.hardware_plan_problems(plan, manifest, "qemu") == []


def test_the_package_check_asks_the_package_database_about_each_atom():
    command = shlex.split(vm_probes.package_check_command(["sys-firmware/intel-microcode", "app-emulation/qemu-guest-agent"]))
    assert command[:2] == ["sh", "-c"] and command[-2:] == ["sys-firmware/intel-microcode", "app-emulation/qemu-guest-agent"]
    assert "/var/db/pkg/${atom}" in command[2]


def test_the_installed_system_must_match_the_plan():
    manifest = manifest_of("laptop-smbios")
    plan = plan_dict("qemu-q35-virtio", manifest)
    good = "absent sys-firmware/intel-microcode\ninstalled app-emulation/qemu-guest-agent\n"
    assert vm_probes.parse_hardware_probe(0, good, plan, manifest).success
    microcode = vm_probes.parse_hardware_probe(0, good.replace("absent sys-firm", "installed sys-firm"), plan, manifest)
    assert not microcode.success and "Intel microcode is installed" in microcode.error
    missing = vm_probes.parse_hardware_probe(0, good.replace("installed app-emulation", "absent app-emulation"), plan, manifest)
    assert not missing.success and "qemu-guest-agent" in missing.error and "no problem" in missing.error


def test_an_optional_package_that_failed_is_fine_when_the_installer_said_so():
    manifest = manifest_of("laptop-smbios")
    plan = plan_dict("qemu-q35-virtio", manifest)
    output = "absent sys-firmware/intel-microcode\nabsent app-emulation/qemu-guest-agent\n"
    assert vm_probes.parse_hardware_probe(0, output, plan, manifest, ["Optional package app-emulation/qemu-guest-agent failed to build"]).success


def test_guest_tools_that_are_off_must_not_be_installed():
    manifest = manifest_of("openrc")  # guest_tools none
    output = "absent sys-firmware/intel-microcode\ninstalled app-emulation/qemu-guest-agent\n"
    result = vm_probes.parse_hardware_probe(0, output, {"schema": 1}, manifest)
    assert not result.success and "switched off" in result.error


def test_the_plan_file_is_recognised_by_its_schema():
    assert vm_probes.parse_hardware_plan_file(json.dumps({"schema": 1, "early_atoms": []}))["schema"] == 1
    assert vm_probes.parse_hardware_plan_file("") is None and vm_probes.parse_hardware_plan_file("[1]") is None
    assert vm_probes.parse_hardware_plan_file(json.dumps({"early_atoms": []})) is None


def test_the_state_file_the_daemon_really_wrote_is_read_and_checked_against_the_machine():
    """tests/vm/data/hardware-plan-*.json is the file of a real run (a QEMU guest, uefi-nvme-luks-openrc): {version, fingerprint, created, plan}."""
    text = (DATA / "hardware-plan-uefi-nvme-luks-openrc.json").read_text()
    assert set(json.loads(text)) == {"version", "fingerprint", "created", "plan"}  # the fixture is the daemon's record, not a bare plan
    plan = vm_probes.parse_hardware_plan_file(text)
    assert plan is not None and plan["schema"] == 1 and plan["early_atoms"] == ["sys-kernel/linux-firmware"]
    assert plan["summary"][0].endswith("no microcode")
    assert vm_probes.hardware_plan_problems(plan, manifest_of("uefi-nvme-luks-openrc"), "qemu") == []


def test_a_plan_inside_the_daemons_record_is_judged_like_a_bare_one():
    record = {"version": 1, "fingerprint": "0" * 64, "created": "2026-10-24T11:09:05.376Z",
              "plan": {"schema": 1, "early_atoms": [vm_probes.INTEL_MICROCODE], "optional_atoms": []}}
    plan = vm_probes.parse_hardware_plan_file(json.dumps(record))
    assert plan == record["plan"]
    assert "Intel microcode in a virtual machine" in " ".join(vm_probes.hardware_plan_problems(plan, manifest_of("openrc"), "qemu"))


@pytest.mark.parametrize("text", [
    json.dumps({"version": 1, "fingerprint": "f", "created": "x", "plan": {"early_atoms": []}}),   # a record whose plan has no schema
    json.dumps({"version": 1, "plan": "not a plan"}), json.dumps({"version": 1, "plan": [1]}), json.dumps({"version": 1}),
])
def test_a_record_without_a_real_plan_is_not_a_plan(text):
    assert vm_probes.parse_hardware_plan_file(text) is None


# The empty-NVRAM boot of the first Wave 6 matrix (2026-10-26): OVMF makes its own option for the disk, with the device path only.
OVMF_LOG_DEVICE_ONLY = (
    'BdsDxe: loading Boot0001 "UEFI Misc Device" from PciRoot(0x0)/Pci(0x2,0x0)\r\n'
    'BdsDxe: starting Boot0001 "UEFI Misc Device" from PciRoot(0x0)/Pci(0x2,0x0)\r\n'
)


def test_a_device_path_without_a_file_is_the_removable_fallback():
    assert vm_probes.booted_through_fallback(OVMF_LOG_DEVICE_ONLY)
    nvme = OVMF_LOG_DEVICE_ONLY.replace("PciRoot(0x0)/Pci(0x2,0x0)", "PciRoot(0x0)/Pci(0x2,0x0)/NVMe(0x1,00-00-00-00-00-00-00-00)")
    assert vm_probes.booted_through_fallback(nvme)
    # an entry the installer registered names its file, and the firmware's own option for a partition is not the fallback either
    assert not vm_probes.booted_through_fallback(OVMF_LOG_DEVICE_ONLY.replace('"UEFI Misc Device" from PciRoot(0x0)/Pci(0x2,0x0)',
                                                                             '"Gentoo" from HD(1,GPT,AAAA,0x800,0x200000)/\\EFI\\Gentoo\\grubx64.efi'))
    assert not vm_probes.booted_through_fallback('BdsDxe: starting Boot0003 "UEFI Shell" from Fv(7CB8BDC9)/FvFile(7C04A583)\r\n')
