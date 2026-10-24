#!/usr/bin/env python3
"""QEMU end-to-end checks using an explicitly provisioned, key-only SSH channel.

Installs Gentoo into disposable virtual machines (different disk buses, sector sizes, firmware,
network cards, laptop identities), checks that the installed system boots, and can damage an
installed system to test the rescue tools. Every disk is a file below build/vm/ and the only host
access of a VM is a loopback SSH forward. See docs/TESTING.md.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import dataclasses
import functools
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gentoo_installer.manifest import InstallManifest, Partition  # noqa: E402
import vm_guard  # noqa: E402
import vm_guest  # noqa: E402
import vm_hw  # noqa: E402
from vm_hw import VMHardware  # noqa: E402
from vm_qmp import (  # noqa: E402
    SUN_PATH_MAX, TEST_LUKS_PASSPHRASE, connect_unix, screen_digest, screendump as qmp_screendump,
    type_luks_passphrase as qmp_type_luks_passphrase,
)

VM_ROOT = PROJECT_ROOT / "build" / "vm"
TARGET_DEVICE = "/dev/vda"
TARGET_SERIAL = "GENTOO-VM-DISPOSABLE"
TEST_USERNAME = "vmtest"
TEST_PASSWORD = "vmtest-only-password"
TEST_ROOT_PASSWORD = "vmtest-only-root-password"
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# The passphrase prompt of both unlock paths, matched case-insensitively:
#   systemd-cryptsetup: "Please enter passphrase for disk root (gentoo-root):"
#   dracut 70crypt:     "Password (/dev/disk/by-uuid/...) [1/3]:"
LUKS_PROMPT_RE = r"(?:passphrase|password)\b[^\r\n]{0,200}:"
# The installed kernel has no serial console unless the manifest asks for one.
# ttyS0 comes last so /dev/console, where both prompts are asked, is the serial port.
SERIAL_CONSOLE_ARGUMENTS = "console=tty0 console=ttyS0,115200n8"
SOCKET_NAMES = ("serial.sock", "qmp.sock")
GUI_DISPLAY_PROBE = r"""
import json, os, pwd, re, stat
from pathlib import Path
uid = pwd.getpwnam('gentoo').pw_uid
display_re = re.compile(r'^(?::[0-9]+(?:\.[0-9]+)?|unix:[0-9]+(?:\.[0-9]+)?)$')
for proc in Path('/proc').glob('[0-9]*'):
    try:
        status = (proc / 'status').read_text()
        uids = next(line.split()[1:] for line in status.splitlines() if line.startswith('Uid:'))
        if int(uids[0]) != uid:
            continue
        command = (proc / 'cmdline').read_bytes().split(b'\0')
        if not any(Path(part.decode(errors='ignore')).name == 'xfce4-session' for part in command if part):
            continue
        values = dict(item.split(b'=', 1) for item in (proc / 'environ').read_bytes().split(b'\0') if b'=' in item)
        env = {key.decode(): value.decode(errors='strict') for key, value in values.items()}
        display = env.get('DISPLAY', '')
        authority = env.get('XAUTHORITY', '/home/gentoo/.Xauthority')
        auth = Path(authority)
        if not display_re.fullmatch(display):
            continue
        if not auth.is_absolute() or not auth.is_file() or not os.access(auth, os.R_OK):
            continue
        print(json.dumps({'type': 'gui_environment', 'environment': {
            key: env[key] for key in ('DISPLAY', 'XAUTHORITY', 'XDG_RUNTIME_DIR',
                                      'DBUS_SESSION_BUS_ADDRESS', 'XDG_CURRENT_DESKTOP',
                                      'XDG_SESSION_TYPE') if key in env
        } | {'DISPLAY': display, 'XAUTHORITY': str(auth)}}))
        raise SystemExit(0)
    except (OSError, ValueError, StopIteration):
        continue
raise SystemExit('No readable local XFCE display and Xauthority found for gentoo')
"""


class HarnessError(RuntimeError):
    pass


class TerminationRequested(KeyboardInterrupt):
    """SIGTERM or SIGHUP, raised in the main thread so the finally blocks stop QEMU."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"terminated by signal {signum}")
        self.signum = signum


def install_termination_handlers() -> dict[int, Any]:
    """Turn SIGTERM/SIGHUP into TerminationRequested; return the previous handlers."""
    def handler(signum: int, _frame: object) -> None:
        raise TerminationRequested(signum)

    previous: dict[int, Any] = {}
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name)
        previous[number] = signal.signal(number, handler)
    return previous


def _bind_prctl() -> Any:
    """libc's prctl, bound here in the parent: the pre-exec hook runs between fork and exec and must not load anything."""
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True).prctl
    except Exception:
        return None


_PRCTL = _bind_prctl()


def die_with_parent(parent_pid: int | None = None) -> None:
    """preexec hook: QEMU gets SIGKILL when the harness dies without running its cleanup.

    PR_SET_PDEATHSIG follows the thread that started QEMU, which is the main
    thread. ``parent_pid`` closes the race where the harness already died
    before the flag was set.
    """
    if _PRCTL is None:
        return
    try:
        _PRCTL(1, int(signal.SIGKILL), 0, 0, 0)  # PR_SET_PDEATHSIG
        if parent_pid is not None and os.getppid() != parent_pid:
            os._exit(1)
    except Exception:
        pass


@dataclass(frozen=True)
class Scenario:
    """One VM test: how the machine looks (``hardware``) and what is installed on it.

    The first five fields and every default below reproduce the original scenarios exactly;
    the rest are the hardware and configuration dimensions of the wider matrix (docs/TESTING.md).
    """
    name: str
    init: str
    firmware: str
    luks: bool = False
    # Keep every InstallManifest default (the untouched GUI path) instead of
    # the fast minimal install the other scenarios use.
    defaults: bool = False
    table: str = ""                  # "" = dos on BIOS, gpt on UEFI
    filesystem: str = "ext4"
    bootloader: str = "grub"
    lvm: bool = False
    hw_target: str = "this"          # manifest.hardware.target: "this" machine or a "portable" install
    guest_tools: str = "none"        # "none" keeps the validated scenarios free of hypervisor tooling
    hardware: VMHardware = VMHardware()
    decoy: bool = False              # extra disks are decoys: marker + checksum before, unchanged after
    manual: bool = False             # partition the disk first and install in manual mode
    reinstall: bool = False          # install a second time over the first installation
    expect_refusal: str = ""         # regex the installer's refusal must match (nothing may be written)
    disk_gib: int = 0                # 0 = use --disk-size
    in_all: bool = True              # run by --all (the fast, validated scenarios)
    note: str = ""

    @property
    def hostname(self) -> str:
        return f"gentoo-vm-{self.name}"

    @property
    def resolved_table(self) -> str:
        return self.table or ("dos" if self.firmware == "bios" else "gpt")


def _hw(**changes) -> VMHardware:
    return VMHardware(**changes)


SCENARIO_MAP = {
    "openrc": Scenario("openrc", "openrc", "uefi"),
    "systemd": Scenario("systemd", "systemd", "uefi"),
    "uefi-luks": Scenario("uefi-luks", "systemd", "uefi", luks=True),
    "bios": Scenario("bios", "openrc", "bios"),
    # The untouched GUI defaults install the full default desktop (hours): run it explicitly.
    "defaults": Scenario("defaults", "openrc", "uefi", defaults=True, in_all=False,
                         note="every InstallManifest default; the GUI must detect firmware, table and disk"),
    # --- the wider matrix: each runs on request until it has passed once on real QEMU ---------------
    "bios-gpt": Scenario("bios-gpt", "openrc", "bios", table="gpt", in_all=False,
                         note="SeaBIOS + GPT: BIOS-boot partition and grub i386-pc on a legacy PC"),
    "openrc-luks": Scenario("openrc-luks", "openrc", "uefi", luks=True, in_all=False,
                            note="OpenRC (the default init) with dracut's non-systemd passphrase prompt"),
    "uefi-fresh-nvram": Scenario("uefi-fresh-nvram", "openrc", "uefi", in_all=False,
                                 hardware=_hw(ovmf_vars="fresh-on-target"),
                                 note="boots the installed disk with empty NVRAM: needs the removable fallback"),
    "decoy-disk": Scenario("decoy-disk", "openrc", "uefi", decoy=True, in_all=False,
                           hardware=_hw(extra_disks=(256,)),
                           note="a second disk with data that must survive the installation untouched"),
    "nvme-uefi": Scenario("nvme-uefi", "openrc", "uefi", in_all=False,
                          hardware=_hw(disk_bus="nvme", nic="e1000e", vga="std"),
                          note="NVMe naming (p1/p2), Intel NIC, standard VGA"),
    "sata-bios": Scenario("sata-bios", "openrc", "bios", in_all=False,
                          hardware=_hw(disk_bus="sata", nic="rtl8139", vga="qxl", machine="pc"),
                          note="an old PC: i440FX, SATA (sda), Realtek NIC, QXL display"),
    "usb-portable": Scenario("usb-portable", "openrc", "uefi", hw_target="portable", in_all=False,
                             hardware=_hw(disk_bus="usb", removable=True, ovmf_vars="fresh-on-target"),
                             note="a removable USB install that must boot on firmware that never saw it"),
    "serial-less": Scenario("serial-less", "openrc", "uefi", in_all=False,
                            hardware=_hw(disk_serial=False),
                            note="a disk without serial number: approved by its by-path identity"),
    "uefi-4kn": Scenario("uefi-4kn", "systemd", "uefi", in_all=False,
                         hardware=_hw(disk_bus="nvme", sector_size=4096),
                         note="NVMe with 4096-byte sectors"),
    "laptop-smbios": Scenario("laptop-smbios", "openrc", "uefi", guest_tools="auto", in_all=False,
                              hardware=_hw(smbios="laptop-lenovo", cpu="Haswell"),
                              note="notebook DMI strings and chassis type, a named CPU model"),
    "vmware-guest": Scenario("vmware-guest", "openrc", "uefi", guest_tools="auto", in_all=False,
                             hardware=_hw(smbios="vmware", vga="vmware", nic="e1000e"),
                             note="hypervisor detection from VMware's DMI strings (run with --memory 2048)"),
    "manual-two-disks": Scenario("manual-two-disks", "openrc", "uefi", manual=True, decoy=True, in_all=False,
                                 hardware=_hw(extra_disks=(256,)),
                                 note="manual partitioning of one disk next to a disk with data that must survive"),
    "reinstall": Scenario("reinstall", "openrc", "uefi", reinstall=True, in_all=False,
                          note="installs twice onto the same disk (stale signatures at reused offsets)"),
    "small-disk": Scenario("small-disk", "openrc", "uefi", disk_gib=6, in_all=False,
                           expect_refusal=r"(?i)too small|at least|capacity|not enough",
                           note="the installer must refuse a disk that is too small before it writes anything"),
    "btrfs-root": Scenario("btrfs-root", "openrc", "uefi", filesystem="btrfs", in_all=False),
    "xfs-root": Scenario("xfs-root", "systemd", "uefi", filesystem="xfs", in_all=False),
    "lvm-root": Scenario("lvm-root", "openrc", "uefi", lvm=True, in_all=False),
    "systemd-boot": Scenario("systemd-boot", "systemd", "uefi", bootloader="systemd-boot", in_all=False),
}
SCENARIOS = tuple(SCENARIO_MAP)
# What --all runs: the fast scenarios whose installs have passed on real QEMU.
ALL_SCENARIOS = tuple(name for name, scenario in SCENARIO_MAP.items() if scenario.in_all)
# Manifest fields the GUI determines from the machine it runs on. In --gui-detect mode the harness
# leaves them at the wizard's defaults and asserts what the GUI chose.
DETECTED_FIELDS = ("storage.device", "storage.device_id", "storage.device_id_kind", "storage.firmware", "storage.table")


@dataclass(frozen=True)
class OvmfFirmware:
    code: Path
    variables: Path


@dataclass
class InstallResult:
    success: bool
    error: str
    events: list[dict]


@dataclass
class ProbeResult:
    name: str
    success: bool
    output: str
    error: str = ""
    # An advisory probe records a finding without failing the scenario (unless --strict-probes).
    advisory: bool = False


def companion_selection(manifest: InstallManifest) -> dict[str, bool]:
    """Read companion choices from the exact install manifest used by the VM."""
    return {
        "hwmonux": bool(manifest.options.install_hwmonux),
        "portagehub": bool(manifest.options.install_portagehub),
    }


def manual_partitions(device: str, firmware: str) -> list[Partition]:
    """What `vm_guest.py partition-manual` creates on ``device``, as manual-mode manifest rows."""
    rows = [Partition(device=vm_guest.partition_path(device, 2), mountpoint="/", filesystem="ext4", format=True)]
    if firmware == "uefi":
        rows.insert(0, Partition(device=vm_guest.partition_path(device, 1), mountpoint="/efi", filesystem="vfat", format=True))
    return rows


def build_manifest(scenario: Scenario, ssh_public_key: str, companions: str = "both", *,
                   build_jobs: int | None = None, swap_mib: int | None = None,
                   device: str = TARGET_DEVICE, device_id: str = TARGET_SERIAL,
                   device_id_kind: str = "") -> InstallManifest:
    """Build the scenario manifest; None resource values keep the scenario's own default.

    ``device``, ``device_id`` and ``device_id_kind`` name the disk the guard resolved in the
    guest ("path" ids for a disk without a serial number); the defaults are the original
    /dev/vda with its serial.
    """
    if not re.fullmatch(r"(?:ssh-(?:ed25519|rsa)|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]+(?: .*)?", ssh_public_key.strip()):
        raise HarnessError("The SSH public key is missing or invalid")
    if companions not in {"both", "portagehub", "none"}:
        raise HarnessError("Companions must be both, portagehub, or none")
    if (build_jobs is not None and not 1 <= build_jobs <= 256) or (swap_mib is not None and not 0 <= swap_mib <= 131072):
        raise HarnessError("Build jobs must be 1–256 and target swap must be 0–131072 MiB")
    manifest = InstallManifest()
    manifest.storage.device = device
    manifest.storage.device_id = device_id
    manifest.storage.device_id_kind = device_id_kind
    if scenario.defaults:
        # Only the disposable disk, test credentials and probe access differ
        # from what an untouched GUI run would send.
        manifest.accounts.hostname = scenario.hostname
        manifest.accounts.username = TEST_USERNAME
        manifest.accounts.password = TEST_PASSWORD
        manifest.accounts.root_password = TEST_ROOT_PASSWORD
        manifest.accounts.ssh_authorized_keys = [ssh_public_key.strip()]
        manifest.options.ssh = True
        manifest.options.install_hwmonux = companions == "both"
        manifest.options.install_portagehub = companions in {"both", "portagehub"}
        if build_jobs is not None:
            manifest.portage.make_opts = f"-j{build_jobs}"
        if swap_mib is not None:
            manifest.storage.swap_mib = swap_mib
        manifest.require_valid()
        return manifest
    build_jobs = 2 if build_jobs is None else build_jobs
    swap_mib = 512 if swap_mib is None else swap_mib
    manifest.storage.firmware = scenario.firmware
    manifest.storage.table = scenario.resolved_table
    manifest.storage.filesystem = scenario.filesystem
    manifest.storage.lvm = scenario.lvm
    if scenario.manual:
        manifest.storage.mode = "manual"
        manifest.storage.partitions = manual_partitions(device, scenario.firmware)
    manifest.storage.swap_mib = swap_mib
    manifest.storage.luks = scenario.luks
    manifest.storage.luks_passphrase = TEST_LUKS_PASSPHRASE if scenario.luks else ""
    manifest.system.init = scenario.init
    manifest.system.stage_variant = f"amd64-{scenario.init}"
    manifest.system.profile = "default/linux/amd64/23.0" + ("/systemd" if scenario.init == "systemd" else "")
    manifest.system.desktop = "minimal"
    manifest.system.kernel = "gentoo-kernel-bin"
    manifest.system.initramfs = "dracut"
    manifest.system.bootloader = scenario.bootloader
    if scenario.luks:
        # Without it the unlock prompt only exists on the emulated VGA screen.
        manifest.system.kernel_cmdline_extra = SERIAL_CONSOLE_ARGUMENTS
    manifest.hardware.target = scenario.hw_target
    manifest.hardware.guest_tools = scenario.guest_tools
    manifest.portage.binhost = True
    manifest.portage.features = ["getbinpkg"]
    manifest.portage.make_opts = f"-j{build_jobs}"
    manifest.accounts.hostname = scenario.hostname
    manifest.accounts.username = TEST_USERNAME
    manifest.accounts.full_name = "Disposable VM test user"
    manifest.accounts.password = TEST_PASSWORD
    manifest.accounts.root_password = TEST_ROOT_PASSWORD
    manifest.accounts.disable_root_login = True
    manifest.accounts.ssh_authorized_keys = [ssh_public_key.strip()]
    manifest.options.install_hwmonux = companions == "both"
    manifest.options.install_portagehub = companions in {"both", "portagehub"}
    manifest.options.ssh = True
    manifest.options.pipewire = False
    manifest.options.bluetooth = False
    manifest.options.printing = False
    manifest.options.microcode = False
    manifest.options.chrony = False
    manifest.options.cron = False
    if scenario.name == "bios":
        # QEMU user networking reserves these guest/router/DNS addresses.
        # Exercise the generated static Ethernet profile as well as DHCP.
        manifest.network.dhcp = False
        manifest.network.address = "10.0.2.15/24"
        manifest.network.gateway = "10.0.2.2"
        manifest.network.dns = ["10.0.2.3"]
    manifest.require_valid()
    return manifest


OVMF_ROOTS = (
    Path("/usr/share/OVMF"),
    Path("/usr/share/edk2/ovmf"),
    Path("/usr/share/edk2/OvmfX64"),
    Path("/usr/share/edk2-ovmf"),
    Path("/usr/share/edk2/x64"),
    Path("/usr/share/qemu"),
)


def ovmf_candidates(secure: bool = False) -> list[tuple[Path, Path]]:
    """(code, variables) file pairs OVMF packages install; ``secure`` selects the Secure Boot builds."""
    names = (
        (("OVMF_CODE.secboot.fd", "OVMF_VARS.secboot.fd"),
         ("OVMF_CODE_4M.secboot.fd", "OVMF_VARS_4M.secboot.fd"),
         ("OVMF_CODE.secboot.4m.fd", "OVMF_VARS.4m.fd"),
         ("edk2-x86_64-secure-code.fd", "edk2-i386-vars.fd"))
        if secure else
        (("OVMF_CODE.fd", "OVMF_VARS.fd"),
         ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"),
         ("OVMF_CODE.4m.fd", "OVMF_VARS.4m.fd"),
         ("edk2-x86_64-code.fd", "edk2-i386-vars.fd"),
         ("ovmf-x86_64-code.bin", "ovmf-x86_64-vars.bin"))
    )
    return [(root / code_name, root / vars_name) for root in OVMF_ROOTS for code_name, vars_name in names]


def detect_ovmf(candidates: Sequence[tuple[Path, Path]] | None = None, *, secure: bool = False) -> OvmfFirmware | None:
    for code, variables in candidates if candidates is not None else ovmf_candidates(secure):
        if code.is_file() and variables.is_file():
            return OvmfFirmware(code.resolve(), variables.resolve())
    return None


def require_under_vm_root(path: Path) -> Path:
    resolved = path.resolve()
    root = VM_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise HarnessError(f"Refusing runtime path outside {root}: {resolved}")
    return resolved


def validate_command_paths(iso: Path, disk: Path, run_dir: Path) -> None:
    for path in (iso, disk, run_dir):
        if any(char in str(path) for char in (",", "\n", "\r", "\x00")):
            raise HarnessError("QEMU paths cannot contain commas or control characters")
    if disk.is_symlink() or run_dir.is_symlink():
        raise HarnessError("Virtual disk and run directory must not be symlinks")
    if not iso.is_file() or iso.suffix.lower() != ".iso":
        raise HarnessError(f"--iso must name an existing ISO file: {iso}")
    require_under_vm_root(run_dir)
    require_under_vm_root(disk)
    if disk.parent.resolve() != run_dir.resolve() or disk.name != "disk.qcow2":
        raise HarnessError("The virtual disk must be run_dir/disk.qcow2")
    if disk.exists() and not disk.is_file():
        raise HarnessError(f"Refusing non-file virtual disk: {disk}")


def extra_disk_path(run_dir: Path, index: int) -> Path:
    """Where the index-th (1-based) extra disk lives: always a regular file inside the run directory."""
    return run_dir / f"extra-{index}.img"


def extra_disk_serial(index: int) -> str:
    return f"GENTOO-VM-EXTRA-{index}"


def firmware_arguments(scenario: Scenario, firmware: OvmfFirmware | None, run_dir: Path, *,
                       vars_mode: str = "persist", vars_template: Path | None = None) -> list[str]:
    """pflash drives for a UEFI scenario; the writable variable store is a per-run copy.

    ``vars_template`` replaces the firmware package's pristine variable store as the copy's
    source (a store with enrolled keys, for Secure Boot tests). ``vars_mode`` "readonly" maps
    the store read-only, so the firmware cannot save boot entries (efibootmgr fails).
    """
    if scenario.firmware == "bios":
        return []
    if firmware is None:
        raise HarnessError("This scenario requires OVMF; install OVMF_CODE.fd and OVMF_VARS.fd")
    require_under_vm_root(run_dir)
    variables = run_dir / "OVMF_VARS.fd"
    if variables.is_symlink():
        raise HarnessError("Refusing symlink firmware variables")
    if not variables.exists():
        shutil.copyfile(vars_template or firmware.variables, variables)
    store = "if=pflash,format=raw,readonly=on," if vars_mode == "readonly" else "if=pflash,format=raw,"
    return [
        "-drive", f"if=pflash,format=raw,readonly=on,file={firmware.code}",
        "-drive", f"{store}file={variables}",
    ]


def qemu_command(
    scenario: Scenario,
    *,
    iso: Path,
    disk: Path,
    run_dir: Path,
    live: bool,
    ssh_host: str,
    ssh_port: int,
    memory_mib: int,
    cpus: int,
    qemu_binary: str = "qemu-system-x86_64",
    firmware: OvmfFirmware | None = None,
    hardware: VMHardware | None = None,
    nonce: str = "",
    cd_first: bool = False,
    vars_template: Path | None = None,
) -> list[str]:
    """The complete QEMU command line. Every optional argument keeps the original machine.

    ``hardware`` overrides the scenario's machine shape, ``nonce`` is the per-run value the disk
    guard reads back from the guest's SMBIOS, ``cd_first`` gives the live CD boot priority over
    an installed disk (a second live boot of the same disk), ``vars_template`` seeds the
    variable store from a given file.
    """
    validate_command_paths(iso, disk, run_dir)
    validate_loopback(ssh_host)
    if not 1 <= ssh_port <= 65535 or memory_mib < 512 or not 1 <= cpus <= 64:
        raise HarnessError("Invalid SSH port, memory size, or CPU count")
    hw = hardware if hardware is not None else scenario.hardware
    try:
        hw.require_valid()
    except ValueError as exc:
        raise HarnessError(str(exc)) from exc
    args = [
        qemu_binary,
        "-name", f"gentoo-installer-{scenario.name}",
        *vm_hw.machine_arguments(hw),
        "-m", str(memory_mib),
        "-smp", str(cpus),
        "-drive", f"file={disk},if=none,id=target,format=qcow2",
        *vm_hw.disk_device_arguments(hw, "target", TARGET_SERIAL if hw.disk_serial else "", 0),
    ]
    for index, _ in enumerate(hw.extra_disks, start=1):
        extra = extra_disk_path(run_dir, index)
        if any(char in str(extra) for char in (",", "\n", "\r", "\x00")) or extra.is_symlink():
            raise HarnessError("Extra disks must be plain files inside the run directory")
        args.extend(("-drive", f"file={extra},if=none,id=extra{index},format=raw",
                     *vm_hw.disk_device_arguments(hw, f"extra{index}", extra_disk_serial(index), index)))
    if live:
        if cd_first:
            args.extend(("-drive", f"file={iso},if=none,id=cd0,media=cdrom,readonly=on,format=raw",
                         "-device", "ide-cd,drive=cd0,bootindex=0"))
        else:
            args.extend(("-drive", f"file={iso},media=cdrom,readonly=on,format=raw"))
    args.extend(firmware_arguments(scenario, firmware, run_dir, vars_mode=hw.ovmf_vars, vars_template=vars_template))
    args.extend([
        "-boot", "order=d,strict=on" if live else "order=c,strict=on",
        "-netdev", f"user,id=net0,hostfwd=tcp:{ssh_host}:{ssh_port}-:22",
        *vm_hw.nic_arguments(hw),
        "-device", "virtio-rng-pci",
        "-vga", hw.vga,
        "-display", "none",
        "-monitor", "none",
        *vm_hw.smbios_arguments(hw, run_dir, nonce),
        "-chardev", f"socket,id=serial0,path={socket_argument(run_dir, SOCKET_NAMES[0])},server=on,wait=off",
        "-serial", "chardev:serial0",
        "-qmp", f"unix:{socket_argument(run_dir, SOCKET_NAMES[1])},server=on,wait=off",
        "-no-reboot",
    ])
    return args


def socket_argument(run_dir: Path, name: str) -> str:
    """Absolute socket path for the QEMU argv, or the bare name when it would not fit sun_path.

    The absolute path stays in the argv whenever it fits because operator
    tooling identifies the VM by it; VMProcess runs QEMU in ``run_dir`` so the
    bare name resolves there for deep checkouts.
    """
    absolute = run_dir / name
    return str(absolute) if len(os.fsencode(absolute)) <= SUN_PATH_MAX else name


def validate_loopback(host: str) -> str:
    if host == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise HarnessError("--ssh-host must be localhost or a numeric loopback address") from exc
    if not address.is_loopback or address.version != 4:
        raise HarnessError("Only an IPv4 loopback --ssh-host is supported by the QEMU port forward")
    return host


def ssh_base_arguments(args: argparse.Namespace, known_hosts: Path, user: str) -> list[str]:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise HarnessError(f"Invalid SSH username: {user}")
    return [
        args.ssh_binary,
        # Ignore the invoking user's ssh_config (ControlMaster, ProxyJump, Host * rules).
        "-F", "/dev/null",
        "-i", str(args.ssh_private_key),
        "-p", str(args.ssh_port),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "ConnectTimeout=10",
        "-o", "ConnectionAttempts=1",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        # No "Permanently added ..." line in output that probes compare exactly.
        "-o", "LogLevel=ERROR",
        # Notice a dead peer, but tolerate a guest that thrashes for many minutes under
        # memory pressure (the r18 OpenRC stall): 30 s x 60 = 30 minutes of silence.
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=60",
        f"{user}@{args.ssh_host}",
    ]


def ssh_command(args: argparse.Namespace, known_hosts: Path, user: str, remote: str) -> list[str]:
    return [*ssh_base_arguments(args, known_hosts, user), "--", remote]


UNKNOWN_FIELDS_RE = re.compile(r"Unknown (\w+) fields?: ([\w, ]+)")


def parse_install_result(returncode: int, output: str) -> InstallResult:
    events: list[dict] = []
    errors: list[str] = []
    stray: list[str] = []
    completed: dict | None = None
    for line in ANSI_RE.sub("", output).splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                stray.append(line.strip())
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        if event.get("type") == "error":
            errors.append(str(event.get("error") or event.get("message") or "Unknown backend error"))
        if event.get("type") == "completed":
            completed = event
    if completed is None:
        reason = "Backend output has no completed event" + (f" (exit {returncode})" if returncode else "")
        if stray:  # the daemon refused to start: its message is the cause
            reason += f": {stray[-1][:300]}"
            unknown = UNKNOWN_FIELDS_RE.search(stray[-1])
            if unknown:
                reason += (f" (the ISO does not know the manifest field(s) {unknown.group(2).strip()} that this harness sends: "
                           "rebuild the test ISO from the same tree)")
        return InstallResult(False, reason, events)
    success = completed.get("data", {}).get("success") is True
    if returncode != 0:
        errors.append(f"Backend exited with status {returncode}")
    if not success:
        errors.append("Backend completed event did not report success")
    return InstallResult(returncode == 0 and success, "; ".join(dict.fromkeys(errors)), events)


def validate_install_outcome(result: InstallResult, log: Path, *, gui: bool = False) -> None:
    """Report backend failure before checking GUI-only success indicators."""
    if not result.success:
        raise HarnessError(f"Installation failed: {result.error}; see {log}")
    if gui:
        gui_results = [event for event in result.events if event.get("type") == "gui_result"]
        if len(gui_results) != 1 or gui_results[0].get("success") is not True or gui_results[0].get("reboot_enabled") is not True:
            raise HarnessError(f"GUI did not report one successful ready state; see {log}")


def parse_gui_environment(returncode: int, output: str) -> dict[str, str]:
    if returncode != 0:
        raise HarnessError("Could not inspect the live desktop session: " + output.strip()[-1200:])
    for line in reversed(output.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "gui_environment" or not isinstance(event.get("environment"), dict):
            continue
        environment = event["environment"]
        display = environment.get("DISPLAY", "")
        authority = environment.get("XAUTHORITY", "")
        if not isinstance(display, str) or not re.fullmatch(r"(?::[0-9]+(?:\.[0-9]+)?|unix:[0-9]+(?:\.[0-9]+)?)", display):
            raise HarnessError("The guest desktop reported an unsupported DISPLAY")
        if not isinstance(authority, str) or not authority.startswith("/"):
            raise HarnessError("The guest desktop did not report an absolute XAUTHORITY path")
        allowed = {"DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                   "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE"}
        return {key: value for key, value in environment.items()
                if key in allowed and isinstance(value, str) and "\x00" not in value}
    raise HarnessError("The guest did not report a usable desktop environment")


def poll_gui_environment(probe: Callable[[int, int], tuple[int, Path]], *, timeout: int,
                         command_timeout: int, run_dir: Path, retry_interval: float = 2.0) -> dict[str, str]:
    """Wait for the live XFCE session, preserving each probe and a summary."""
    deadline = time.monotonic() + timeout
    attempts: list[str] = []
    number = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if number and remaining < 1:
            time.sleep(remaining)
            break
        number += 1
        probe_timeout = min(command_timeout, max(1, int(remaining)))
        try:
            code, log = probe(number, probe_timeout)
            output = log.read_text(errors="replace")
            environment = parse_gui_environment(code, output)
            attempts.append(f"attempt {number}: ready ({log.name})")
            (run_dir / "gui-environment.log").write_text("\n".join(attempts) + "\n")
            return environment
        except (HarnessError, OSError) as exc:
            attempts.append(f"attempt {number}: {exc}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(retry_interval, remaining))
    summary = "\n".join(attempts)
    (run_dir / "gui-environment.log").write_text(summary + "\n")
    raise HarnessError(f"No usable XFCE DISPLAY/XAUTHORITY appeared within {timeout}s; see {run_dir / 'gui-environment.log'}\n{summary[-1200:]}")


def decode_gui_artifact(name: str, payload: str) -> bytes:
    try:
        data = base64.b64decode(payload.strip(), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HarnessError(f"GUI artifact {name} was not valid base64") from exc
    if not data:
        raise HarnessError(f"GUI artifact {name} was empty")
    if name.endswith(".png") and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HarnessError(f"GUI artifact {name} was not a PNG image")
    return data


BIOS_BOOT_PARTTYPE = "21686148-6449-6e6f-744e-656564454649"


def parse_lsblk_snapshot(output: str, disk: str = TARGET_DEVICE) -> dict[str, dict[str, str]]:
    """Partition identities of ``disk`` and its partitions from ``lsblk --json`` output."""
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"lsblk returned invalid JSON: {exc}") from exc
    devices = payload.get("blockdevices")
    if not isinstance(devices, list):
        raise HarnessError("lsblk JSON had no blockdevices array")
    flattened: dict[str, dict[str, str]] = {}

    def visit(node: dict) -> None:
        path = node.get("path")
        if isinstance(path, str) and path.startswith(disk):
            flattened[path] = {key: str(node.get(key) or "") for key in
                               ("type", "uuid", "fstype", "partuuid", "parttype", "pttype", "ptuuid")}
        children = node.get("children") or []
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    visit(child)

    for device in devices:
        if isinstance(device, dict):
            visit(device)
    whole = flattened.get(disk)
    partitions = [row for path, row in flattened.items() if path != disk and row["type"] == "part"]
    if not whole or whole["type"] != "disk" or not partitions:
        raise HarnessError(f"lsblk did not report {disk} and its partition children")
    # The BIOS-boot partition (EF02) carries raw GRUB code: no filesystem, so no UUID or type.
    if any((not row["uuid"] or not row["fstype"]) and row["parttype"].lower() != BIOS_BOOT_PARTTYPE for row in partitions):
        raise HarnessError(f"A {disk} partition had an empty filesystem UUID or type")
    if not whole["pttype"] or not whole["ptuuid"]:
        raise HarnessError("lsblk did not report the disposable disk's partition table identity")
    return dict(sorted(flattened.items()))


def destructive_runner_commands(output: str) -> list[str]:
    destructive: list[str] = []
    for line in ANSI_RE.sub("", output).splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "log":
            continue
        command_text = event.get("message", "")
        if not isinstance(command_text, str) or not command_text.startswith("$ "):
            continue
        command_text = command_text[2:].strip()
        try:
            argv = shlex.split(command_text)
        except ValueError:
            destructive.append(command_text)
            continue
        if not argv:
            continue
        executable = Path(argv[0]).name
        is_destructive = (executable.startswith("mkfs.") or executable in {"mkswap", "sgdisk", "wipefs", "pvcreate", "vgcreate", "lvcreate"}
                          or (executable == "parted" and "mklabel" in argv[1:])
                          or (executable == "mdadm" and "--create" in argv[1:])
                          or (executable == "cryptsetup" and any(arg.lower() == "luksformat" for arg in argv[1:])))
        if is_destructive:
            destructive.append(command_text)
    return destructive


def parse_resume_regression(returncode: int, output: str, before: dict[str, dict[str, str]],
                            after: dict[str, dict[str, str]]) -> dict:
    install = parse_install_result(returncode, output)
    stages = [event for event in install.events if event.get("type") == "stage" and event.get("stage") == "storage"]
    storage_skipped = any(event.get("data", {}).get("status") == "skipped" for event in stages)
    destructive = destructive_runner_commands(output)
    mismatches = []
    for device in sorted(set(before) | set(after)):
        old, new = before.get(device), after.get(device)
        if old is None or new is None or any(old.get(key) != new.get(key) for key in ("uuid", "fstype", "partuuid", "pttype", "ptuuid")):
            mismatches.append(device)
    success = install.success and storage_skipped and not destructive and not mismatches
    reasons = []
    if not install.success:
        reasons.append(install.error or "resume completed event did not report success")
    if not storage_skipped:
        reasons.append("resume did not report storage as skipped/restored")
    if destructive:
        reasons.append("destructive storage commands were recorded: " + ", ".join(destructive))
    if mismatches:
        reasons.append("disk identity changed for: " + ", ".join(mismatches))
    return {"success": success, "error": "; ".join(reasons), "storage_skipped": storage_skipped,
            "destructive_commands": destructive, "identity_mismatches": mismatches,
            "before": before, "after": after, "events": install.events}


def parse_cancel_regression(returncode: int, output: str) -> dict:
    events = []
    for line in ANSI_RE.sub("", output).splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    stages = [event for event in events if event.get("type") == "stage"]
    storage_completed = any(event.get("stage") == "storage" and event.get("data", {}).get("status") == "completed" for event in stages)
    cleanup_completed = any(event.get("stage") == "cleanup" and event.get("data", {}).get("status") == "completed" for event in stages)
    stage3_started = sum(event.get("stage") == "stage3" and event.get("data", {}).get("status") == "running" for event in stages)
    errors = [event for event in events if event.get("type") == "error"]
    completions = [event for event in events if event.get("type") == "completed"]
    gui = [event for event in events if event.get("type") == "gui_result"]
    success = (returncode == 0 and storage_completed and cleanup_completed and stage3_started == 1 and
               len(errors) == 1 and errors[0].get("error") == "cancelled" and
               len(completions) == 1 and completions[0].get("data", {}).get("success") is False and
               len(gui) == 1 and gui[0].get("success") is False and
               gui[0].get("cancel_requested") is True and gui[0].get("reboot_enabled") is False and
               gui[0].get("stage3_started") == 1)
    reasons = []
    if returncode != 0:
        reasons.append(f"GUI driver exited with status {returncode}")
    if not storage_completed:
        reasons.append("storage completion was not observed before cancellation")
    if not cleanup_completed:
        reasons.append("installer cleanup did not report completion")
    if stage3_started != 1:
        reasons.append(f"expected one stage3 start event, got {stage3_started}")
    if len(errors) != 1 or (errors and errors[0].get("error") != "cancelled"):
        reasons.append(f"expected exactly one cancelled error event, got {len(errors)} error event(s)")
    if len(completions) != 1 or (completions and completions[0].get("data", {}).get("success") is not False):
        reasons.append("backend did not report exactly one unsuccessful completion")
    if len(gui) != 1 or (gui and (gui[0].get("success") is not False or gui[0].get("cancel_requested") is not True)):
        reasons.append("GUI did not report the requested cancellation as unsuccessful")
    return {"success": success, "error": "; ".join(reasons), "events": events}


def parse_cancel_cleanup(returncode: int, output: str) -> dict:
    try:
        payload = json.loads(output.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        payload = {}
    mounts = payload.get("target_mounts", [])
    mappings = payload.get("leftover_root_stack", [])
    swaps = payload.get("target_swaps", [])
    success = returncode == 0 and not mounts and not mappings and not swaps
    reasons = []
    if returncode != 0:
        reasons.append(f"guest cleanup check exited with status {returncode}")
    if mounts:
        reasons.append("target mount(s) remain: " + ", ".join(mounts))
    if mappings:
        reasons.append("active root stack mapping(s) remain: " + ", ".join(mappings))
    if swaps:
        reasons.append("target swap(s) remain active: " + ", ".join(swaps))
    if not payload:
        reasons.append("guest cleanup check returned no JSON evidence")
    return {"success": success, "error": "; ".join(reasons), "target_mounts": mounts,
            "leftover_root_stack": mappings, "target_swaps": swaps}


def parse_probe_result(name: str, returncode: int, output: str, expected: str | None = None) -> ProbeResult:
    clean = ANSI_RE.sub("", output).strip()
    if returncode != 0:
        return ProbeResult(name, False, clean, f"Probe exited with status {returncode}")
    if expected is not None and clean != expected:
        return ProbeResult(name, False, clean, f"Expected {expected!r}")
    return ProbeResult(name, True, clean)


def parse_network_result(returncode: int, output: str) -> ProbeResult:
    result = parse_probe_result("network", returncode, output)
    if not result.success:
        return result
    address_ok = re.search(r"\binet\s+(?!127\.)[0-9a-fA-F:.]+/\d+", result.output) is not None
    route_ok = re.search(r"(?m)^default(?:\s|$)", result.output) is not None
    if not address_ok or not route_ok:
        return ProbeResult("network", False, result.output, "No non-loopback IPv4 address and default route")
    return result


def parse_hwmonux_dump(returncode: int, output: str) -> ProbeResult:
    result = parse_probe_result("hwmonux", returncode, output)
    if not result.success:
        return result
    lines = result.output.splitlines()
    if len(lines) < 2 or not lines[0].strip() or not all(header in lines[1] for header in ("Value", "Min", "Max")):
        return ProbeResult("hwmonux", False, result.output, "--dump returned no valid sensor report header")
    return result


def parse_portagehub_self_test(returncode: int, output: str) -> ProbeResult:
    result = parse_probe_result("portagehub", returncode, output)
    if not result.success:
        return result
    if not all(marker in result.output for marker in ("repos:", "catalog:", "vdb:")):
        return ProbeResult("portagehub", False, result.output,
                           "--self-test did not report repos, catalog, and installed package database")
    return result


def parse_companion_absent(name: str, returncode: int, output: str) -> ProbeResult:
    result = parse_probe_result(f"disabled-{name}-absent", returncode, output)
    if result.success and result.output.strip() != "absent":
        return ProbeResult(result.name, False, result.output,
                           f"Disabled companion {name} is present in the installed package database")
    return result


def companion_probe_plan(manifest: InstallManifest) -> list[tuple[str, str, Callable[[int, str], ProbeResult]]]:
    """Build runtime/absence checks from the saved manifest's exact choices."""
    selected = companion_selection(manifest)
    plan: list[tuple[str, str, Callable[[int, str], ProbeResult]]] = []
    for name, (category, command, parser) in {
        "hwmonux": ("app-misc", "hwmonux --dump", parse_hwmonux_dump),
        "portagehub": ("app-portage", "portagehub --self-test", parse_portagehub_self_test),
    }.items():
        if selected[name]:
            plan.append((name, command, parser))
        else:
            package_check = (
                "python3 -c " + shlex.quote(
                    "from pathlib import Path; import sys; "
                    f"present=bool(list(Path('/var/db/pkg/{category}').glob('{name}-*'))); "
                    "print('present' if present else 'absent'); sys.exit(bool(present))"
                )
            )
            plan.append((f"disabled-{name}-absent", package_check, functools.partial(parse_companion_absent, name)))
    return plan


DESKTOP_PACKAGES = {
    "kde": ("kde-plasma", "plasma-meta", "sddm"),
    "gnome": ("gnome-base", "gnome", "gdm"),
    "xfce": ("xfce-base", "xfce4-meta", "lightdm"),
}


def desktop_probe_plan(manifest: InstallManifest) -> list[tuple[str, str, Callable[[int, str], ProbeResult]]]:
    """Check that the selected desktop and its display manager were installed and enabled."""
    if manifest.system.desktop not in DESKTOP_PACKAGES:
        return []
    category, package, manager = DESKTOP_PACKAGES[manifest.system.desktop]
    installed = f"ls -d /var/db/pkg/{category}/{package}-[0-9]*"
    if manifest.system.init == "openrc":
        expected = shlex.quote(f'DISPLAYMANAGER="{manager}"')
        enabled = f"test -e /etc/runlevels/default/display-manager && grep -qxF {expected} /etc/conf.d/display-manager"
    else:
        enabled = f"systemctl is-enabled {manager}.service"
    return [("desktop", f"{installed} && {enabled}", lambda code, output: parse_probe_result("desktop", code, output))]


def parse_static_network_probe(returncode: int, output: str, expected: Mapping[str, object]) -> ProbeResult:
    name = "network-static"
    if returncode != 0:
        return ProbeResult(name, False, output.strip(), f"Read-only NetworkManager probe exited with status {returncode}")
    try:
        observed = json.loads(output.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return ProbeResult(name, False, output.strip(), "NetworkManager probe returned no JSON evidence")
    if not isinstance(observed, dict):
        return ProbeResult(name, False, output.strip(), "NetworkManager probe evidence is not a JSON object")
    active = observed.get("active", [])
    addresses = observed.get("addresses", [])
    dns = observed.get("dns", [])
    if (not isinstance(active, list) or not all(isinstance(value, str) for value in active)
            or not isinstance(addresses, list) or not all(isinstance(value, str) for value in addresses)
            or not isinstance(dns, list) or not all(isinstance(value, str) for value in dns)):
        return ProbeResult(name, False, json.dumps(observed, sort_keys=True),
                           "NetworkManager probe returned fields with invalid types")
    profile_active = any(entry.split(":", 1)[0] == "Installer wired static" for entry in active)
    valid = (
        profile_active
        and observed.get("method") == "manual"
        and addresses == [expected["address"]]
        and observed.get("gateway") == expected["gateway"]
        and dns == expected["dns"]
    )
    error = ""
    if not profile_active:
        error = "Installer wired static is not an active NetworkManager connection"
    elif observed.get("method") != "manual":
        error = "Static profile ipv4.method is not manual"
    elif addresses != [expected["address"]]:
        error = "Static profile address does not match the saved manifest"
    elif observed.get("gateway") != expected["gateway"]:
        error = "Static profile gateway does not match the saved manifest"
    elif dns != expected["dns"]:
        error = "Static profile DNS does not match the saved manifest"
    return ProbeResult(name, valid, json.dumps(observed, sort_keys=True), error)


def static_network_probe(manifest: InstallManifest) -> tuple[str, Callable[[int, str], ProbeResult]] | None:
    network = manifest.network
    if network.manager != "networkmanager" or network.dhcp:
        return None
    expected = {"address": network.address, "gateway": network.gateway, "dns": list(network.dns)}
    script = r'''import json, subprocess
def values(*args):
    result = subprocess.run(["nmcli", *args], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
active = values("-t", "-f", "NAME,DEVICE", "connection", "show", "--active")
profile = "Installer wired static"
print(json.dumps({
    "active": active,
    "method": (values("-g", "ipv4.method", "connection", "show", "id", profile) or [""])[0],
    "addresses": values("-g", "ipv4.addresses", "connection", "show", "id", profile),
    "gateway": (values("-g", "ipv4.gateway", "connection", "show", "id", profile) or [""])[0],
    "dns": values("-g", "ipv4.dns", "connection", "show", "id", profile),
}))'''
    command = shlex.join(("python3", "-c", script))
    return command, lambda code, output: parse_static_network_probe(code, output, expected)


# --- the live system on this hardware (--live-only) ---------------------------------------------------

# Ask the installer daemon, as the GUI would, what it sees of this machine.
INSPECT_SNIPPET = r"""
import json, socket
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(60)
client.connect('/run/gentoo-installer/server.sock')
client.sendall(json.dumps({'version': 1, 'action': 'inspect'}).encode() + b'\n')
for line in client.makefile('r'):
    event = json.loads(line)
    if event.get('type') == 'status' and event.get('data'):
        print(json.dumps({'type': 'live_inspection', 'data': event['data']}))
    if event.get('type') in ('completed', 'error'):
        if event.get('type') == 'error':
            print(json.dumps({'type': 'live_inspection_error', 'error': event.get('error') or event.get('message')}))
        break
"""


def parse_live_inspection(returncode: int, output: str) -> dict:
    """The daemon's inspection payload from the INSPECT_SNIPPET output."""
    for line in reversed(ANSI_RE.sub("", output).splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "live_inspection" and isinstance(event.get("data"), dict):
            return event["data"]
        if isinstance(event, dict) and event.get("type") == "live_inspection_error":
            raise HarnessError(f"The installer daemon could not inspect the live system: {event.get('error')}")
    raise HarnessError("The installer daemon returned no inspection" + (f" (exit {returncode})" if returncode else ""))


def live_inspection_problems(data: dict, *, firmware: str, memory_mib: int, target_size: int) -> list[str]:
    """What the installer sees of the machine must be what the VM is: firmware, memory, network, the target disk."""
    problems = []
    if data.get("architecture") not in {"x86_64", "amd64"}:
        problems.append(f"the installer sees architecture {data.get('architecture')!r}")
    if bool(data.get("uefi")) != (firmware == "uefi"):
        problems.append(f"the installer detects {'UEFI' if data.get('uefi') else 'BIOS'} firmware but the VM runs {firmware.upper()}")
    seen = int(data.get("memory_bytes") or 0)
    if seen < memory_mib * (1 << 20) * 0.6:
        problems.append(f"the installer sees {seen >> 20} MiB of memory in a {memory_mib} MiB VM")
    if not data.get("network"):
        problems.append("the live system has no working network on this machine's NIC (the installer cannot download anything)")
    raw_disks = data.get("disks")
    disks: list = raw_disks if isinstance(raw_disks, list) else []
    if not any(isinstance(disk, dict) and int(disk.get("size") or 0) == target_size and not disk.get("mountpoints") for disk in disks):
        problems.append(f"no unmounted disk of {target_size} bytes is offered: the installer does not see the VM's disk")
    missing = [tool for tool, present in (data.get("tools") or {}).items() if not present and tool in REQUIRED_LIVE_TOOLS]
    if missing:
        problems.append("required tools are missing from the live system: " + ", ".join(sorted(missing)))
    return problems


REQUIRED_LIVE_TOOLS = ("lsblk", "findmnt", "blkid", "sgdisk", "parted", "partprobe", "udevadm", "mkfs.ext4", "mkswap", "mount", "umount")


def lookup_path(data: Any, path: str) -> Any:
    """A dotted path into JSON: keys, and numbers for list items ('disks.0.size')."""
    for part in path.split("."):
        if isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            raise KeyError(path)
    return data


def check_expectations(data: dict, expected: dict) -> list[str]:
    """Compare dotted paths of the inspection with expected values; a string 're:...' is a regular expression."""
    problems = []
    for path, wanted in sorted(expected.items()):
        try:
            found = lookup_path(data, path)
        except KeyError:
            problems.append(f"{path}: not reported by the installer")
            continue
        if isinstance(wanted, str) and wanted.startswith("re:"):
            if not re.search(wanted[3:], str(found)):
                problems.append(f"{path}: {found!r} does not match {wanted[3:]!r}")
        elif found != wanted:
            problems.append(f"{path}: expected {wanted!r}, the installer reports {found!r}")
    return problems


# --- probes of the installed system (all pure parsers, tested without a VM) ------------------------

LIVE_ARGUMENT_RE = re.compile(r"root=live:\S*|rd\.live\.\S+|\S*CDLABEL=\S*|\S*rd\.live\S*")
STABLE_ROOT_RE = re.compile(r"root=(?:UUID=|PARTUUID=|LABEL=|PARTLABEL=|/dev/mapper/|/dev/disk/by-)\S+")
UNSTABLE_ROOT_RE = re.compile(r"root=/dev/(?:vd|sd|hd|nvme|mmcblk|xvd)\S*")
PROBE_STDIN = "stdin"
# What a machine that cannot boot shows on its serial console or screen (rescue evidence).
BROKEN_BOOT_MARKERS = ("grub>", "grub rescue>", "error: file", "error: unknown filesystem", "Kernel panic",
                       "VFS: Unable to mount root", "Entering emergency mode", "UEFI Interactive Shell",
                       "No bootable device", "Boot failed")
RESCUE_BREAKS = ("none", "grub-cfg", "initramfs", "fstab-uuid")


def parse_cmdline_probe(returncode: int, output: str, manifest: InstallManifest) -> ProbeResult:
    """The installed kernel's command line must survive another disk order, another machine and an encrypted root."""
    result = parse_probe_result("cmdline", returncode, output)
    if not result.success:
        return result
    tokens = result.output.split()
    problems: list[str] = []
    roots = [token for token in tokens if token.startswith("root=")]
    if not any(STABLE_ROOT_RE.fullmatch(token) for token in roots):
        problems.append("no stable root= argument (UUID=, PARTUUID=, LABEL= or a /dev/mapper name)")
    if any(UNSTABLE_ROOT_RE.fullmatch(token) for token in roots):
        problems.append("root= names a kernel device (it breaks when the disks are enumerated in another order)")
    if manifest.storage.luks and not any(token.startswith("rd.luks.uuid=") for token in tokens):
        problems.append("the encrypted root has no rd.luks.uuid= argument")
    for wanted in manifest.system.kernel_cmdline_extra.split():
        if wanted not in tokens:
            problems.append(f"the requested argument {wanted} is missing")
    leaked = LIVE_ARGUMENT_RE.findall(result.output)
    if leaked:
        problems.append("arguments of the live system leaked into the installed kernel: " + ", ".join(leaked))
    return ProbeResult("cmdline", not problems, result.output, "; ".join(problems))


def parse_swap_probe(returncode: int, output: str, manifest: InstallManifest) -> ProbeResult:
    """A swap partition of the requested size must be active (zram swap does not count)."""
    result = parse_probe_result("swap", returncode, output)
    if not result.success:
        return result
    wanted = manifest.storage.swap_mib * (1 << 20)
    if manifest.storage.mode != "erase" or wanted == 0:
        return ProbeResult("swap", True, result.output, "")
    sizes = []
    for line in result.output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and not fields[0].startswith("/dev/zram") and fields[-1].isdigit():
            sizes.append(int(fields[-1]))
    if not any(size >= wanted * 0.9 for size in sizes):
        return ProbeResult("swap", False, result.output, f"no active swap of about {manifest.storage.swap_mib} MiB")
    return ProbeResult("swap", True, result.output, "")


def parse_root_source_probe(returncode: int, output: str, manifest: InstallManifest) -> ProbeResult:
    result = parse_probe_result("root-source", returncode, output)
    if not result.success:
        return result
    source = re.sub(r"\[.*\]$", "", result.output.strip())  # Btrfs adds the subvolume: /dev/vda2[/@]
    if not source.startswith("/dev/"):
        return ProbeResult("root-source", False, result.output, "the root filesystem is not on a block device")
    if (manifest.storage.luks or manifest.storage.lvm) and not source.startswith("/dev/mapper/"):
        return ProbeResult("root-source", False, result.output, "an encrypted or LVM root must be mounted from /dev/mapper")
    return ProbeResult("root-source", True, result.output, "")


def parse_firmware_probe(returncode: int, output: str, expected: str) -> ProbeResult:
    """The installed system must have booted in the firmware mode it was installed for."""
    return parse_probe_result("firmware", returncode, output, expected)


def parse_dmi_probe(returncode: int, output: str, preset: vm_hw.SmbiosPreset) -> ProbeResult:
    """The VM must really present the DMI identity the scenario asked for (a harness self-check)."""
    result = parse_probe_result("dmi", returncode, output)
    if not result.success:
        return result
    lines = result.output.splitlines()
    problems = []
    if lines[:1] != [preset.manufacturer]:
        problems.append(f"sys_vendor is {lines[:1]!r}, expected {preset.manufacturer!r}")
    if preset.chassis_type and lines[1:2] != [str(preset.chassis_type)]:
        problems.append(f"chassis_type is {lines[1:2]!r}, expected {preset.chassis_type}: QEMU did not apply the SMBIOS chassis file")
    return ProbeResult("dmi", not problems, result.output, "; ".join(problems))


def parse_receipt_probe(returncode: int, output: str, manifest: InstallManifest) -> ProbeResult:
    """/etc/gentoo-live-installer/install.json (docs/contracts.md): absent is advisory, wrong is a failure."""
    if returncode != 0 or not output.strip():
        return ProbeResult("receipt", False, output.strip(), "the install receipt is missing", advisory=True)
    try:
        receipt = json.loads(output)
    except json.JSONDecodeError as exc:
        return ProbeResult("receipt", False, output.strip()[:300], f"the install receipt is not JSON: {exc}")
    problems = []
    if receipt.get("firmware") != manifest.storage.firmware:
        problems.append(f"firmware {receipt.get('firmware')!r} != {manifest.storage.firmware!r}")
    if receipt.get("init") != manifest.system.init:
        problems.append(f"init {receipt.get('init')!r} != {manifest.system.init!r}")
    root = receipt.get("root") if isinstance(receipt.get("root"), dict) else {}
    if not manifest.storage.luks and root.get("fstype") not in (None, manifest.storage.filesystem):
        problems.append(f"root.fstype {root.get('fstype')!r} != {manifest.storage.filesystem!r}")
    if bool(root.get("luks_uuid")) != bool(manifest.storage.luks):
        problems.append("root.luks_uuid does not match the encryption choice")
    return ProbeResult("receipt", not problems, json.dumps(receipt, sort_keys=True)[:600], "; ".join(problems))


def parse_sudo_probe(returncode: int, output: str) -> ProbeResult:
    result = parse_probe_result("sudo", returncode, output)
    return ProbeResult("sudo", result.success, result.output,
                       "" if result.success else "the new user cannot use sudo with their password", advisory=True)


def parse_root_lock_probe(returncode: int, output: str, manifest: InstallManifest) -> ProbeResult:
    result = parse_probe_result("root-lock", returncode, output)
    if not result.success:
        return ProbeResult("root-lock", False, result.output, "root's password status could not be read (needs sudo)", advisory=True)
    fields = result.output.split()
    expected = "L" if manifest.accounts.disable_root_login else "P"
    if len(fields) < 2 or fields[0] != "root" or fields[1] != expected:
        return ProbeResult("root-lock", False, result.output,
                           f"root's password status is {fields[1:2]!r}, expected {expected!r}")
    return ProbeResult("root-lock", True, result.output, "")


def installed_system_probes(manifest: InstallManifest, scenario: Scenario) -> list[tuple]:
    """Probes of what the installer produced, beyond 'it boots': (name, command, parser[, stdin])."""
    password = (TEST_PASSWORD + "\n").encode()
    probes: list[tuple] = [
        ("firmware", "test -d /sys/firmware/efi && echo uefi || echo bios",
         lambda c, o: parse_firmware_probe(c, o, manifest.storage.firmware)),
        ("cmdline", "cat /proc/cmdline", lambda c, o: parse_cmdline_probe(c, o, manifest)),
        ("root-source", "findmnt -no SOURCE /", lambda c, o: parse_root_source_probe(c, o, manifest)),
        ("swap", "swapon --noheadings --bytes --show=NAME,TYPE,SIZE", lambda c, o: parse_swap_probe(c, o, manifest)),
        ("receipt", "cat /etc/gentoo-live-installer/install.json", lambda c, o: parse_receipt_probe(c, o, manifest)),
        ("sudo", "sudo -S -p '' true", parse_sudo_probe, password),
        ("root-lock", "sudo -S -p '' passwd -S root", lambda c, o: parse_root_lock_probe(c, o, manifest), password),
    ]
    preset = vm_hw.SMBIOS_PRESETS[scenario.hardware.smbios]
    if preset.manufacturer:
        probes.append(("dmi", "cat /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/chassis_type",
                       lambda c, o: parse_dmi_probe(c, o, preset)))
    return probes


# --- result files (TD-N6): a summary in the JSON, the full event stream in a sidecar ----------------

def summarize_events(events: list[dict]) -> dict:
    """Counts, final stage states, errors and the last log lines of an installer event stream."""
    types: dict[str, int] = {}
    stages: dict[str, str] = {}
    errors: list[str] = []
    logs: list[str] = []
    completed = None
    for event in events:
        kind = str(event.get("type"))
        types[kind] = types.get(kind, 0) + 1
        if kind == "stage":
            stages[str(event.get("stage"))] = str((event.get("data") or {}).get("status", ""))
        elif kind == "error":
            errors.append(str(event.get("error") or event.get("message") or "")[:500])
        elif kind == "log":
            logs.append(str(event.get("message", ""))[:300])
        elif kind == "completed":
            completed = event.get("data")
    return {"events": len(events), "types": types, "stages": stages, "errors": errors[:20],
            "completed": completed, "last_log": logs[-50:]}


def write_events(path: Path, events: list[dict]) -> Path:
    path.write_text("".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events))
    return path


def slim_report(report: dict, sidecar: Path) -> dict:
    """The report without its event stream (written to ``sidecar``) plus a summary of it."""
    events = report.get("events") or []
    write_events(sidecar, events)
    slim = {key: value for key, value in report.items() if key != "events"}
    slim["event_summary"] = summarize_events(events)
    slim["events_file"] = sidecar.name
    return slim


def choose_by_path(target: dict) -> str:
    """The by-path link name recorded as the identity of a disk without a serial number."""
    names = [name for name in target.get("by_path", []) if "-part" not in name]
    preferred = sorted((name for name in names if name.startswith("pci-")), key=lambda name: (len(name), name))
    if preferred:
        return preferred[0]
    if names:
        return sorted(names, key=lambda name: (len(name), name))[0]
    raise HarnessError("The disk has no /dev/disk/by-path link, so it has no identity without a serial number")


def tail(path: Path, lines: int = 25) -> str:
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def run_logged(argv: Sequence[str], log: Path, timeout: int, *, stdin: bytes | None = None) -> int:
    log.with_suffix(log.suffix + ".command.json").write_text(json.dumps(list(argv), indent=2) + "\n")
    with log.open("ab") as stream:
        try:
            result = subprocess.run(
                list(argv), input=stdin, stdout=stream, stderr=subprocess.STDOUT,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            stream.write(f"\nHarness timeout after {timeout}s\n".encode())
            raise HarnessError(f"Command timed out after {timeout}s; see {log}") from None
        except OSError as exc:
            raise HarnessError(f"Could not execute {argv[0]}: {exc}") from exc
    return result.returncode


def serial_console_configured(cmdline: str) -> bool:
    """Whether a kernel command line sends /dev/console to ttyS0 (where the serial socket listens)."""
    return re.search(r"(?:^|\s)console=ttyS0(?:[,\s]|$)", cmdline) is not None


def unlock_over_serial(serial: "SerialChannel", passphrase: str, timeout: int,
                       ssh_ready: Callable[[int], bool], *, attempts: int = 3, settle: int = 60,
                       reprompt_wait: float = 5.0) -> dict:
    """Answer the LUKS prompt on the serial console, and again whenever it is shown again.

    A wrong or garbled answer makes dracut ask again ("[2/3]"). Every new prompt past the
    previous match is answered, up to ``attempts`` times, until SSH comes up. When the
    answer was not followed by another prompt the passphrase was accepted and the boot is
    merely slow, so the caller's normal SSH wait takes over.
    """
    deadline = time.monotonic() + timeout
    offset = serial.wait_for(LUKS_PROMPT_RE, max(1, int(deadline - time.monotonic())))
    matched = serial.last_match
    for attempt in range(1, attempts + 1):
        serial.send_line(passphrase)
        if ssh_ready(settle):
            return {"method": "serial", "attempts": attempt, "matched_text": matched}
        try:
            offset = serial.wait_for(LUKS_PROMPT_RE, max(1, int(reprompt_wait)), since=offset)
        except HarnessError:
            return {"method": "serial", "attempts": attempt, "matched_text": matched}
        matched = serial.last_match
    raise HarnessError(f"The installed system did not come up after {attempts} LUKS passphrase answers on the serial console")


class ScreenWatcher:
    """Heuristic for 'the guest is sitting at a prompt', from successive screendump digests.

    Keystrokes sent before the kernel is up land in the GRUB menu, so the passphrase is only
    typed once the screen has changed several times (firmware, GRUB, kernel messages) and
    then stopped changing. GRUB's countdown repaints every second, which keeps it from
    looking idle. This is a heuristic: the serial console is the reliable way.
    """

    def __init__(self, *, min_changes: int = 3, stable_seconds: float = 8.0, min_elapsed: float = 20.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.min_changes = min_changes
        self.stable_seconds = stable_seconds
        self.min_elapsed = min_elapsed
        self.clock = clock
        self.started = clock()
        self.changes = 0
        self.digest = ""
        self.changed_at = self.started

    def observe(self, digest: str) -> bool:
        now = self.clock()
        if digest != self.digest:
            self.digest = digest
            self.changes += 1
            self.changed_at = now
            return False
        return (self.changes >= self.min_changes and now - self.started >= self.min_elapsed
                and now - self.changed_at >= self.stable_seconds)

    def rearm(self) -> None:
        """After typing: the screen has to sit still for ``stable_seconds`` again before the next try."""
        self.changes = max(self.changes, self.min_changes)
        self.changed_at = self.clock()


def unlock_over_qmp(guest: "VMProcess", passphrase: str, timeout: int, ssh_ready: Callable[[int], bool],
                    run_dir: Path, *, attempts: int = 3, settle: int = 60, poll: float = 2.0,
                    watcher: ScreenWatcher | None = None,
                    sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic) -> dict:
    """Type the test passphrase as keystrokes once the screen looks like an idle prompt.

    Used when the installed kernel has no serial console. Evidence screenshots are kept:
    ``luks-prompt-observation.ppm`` (the stable screen that triggered typing) and
    ``luks-after-typing.ppm`` (the screen after the last answer).
    """
    watcher = watcher or ScreenWatcher(clock=clock)
    deadline = clock() + timeout
    frame = run_dir / "luks-screen-current.ppm"
    for attempt in range(1, attempts + 1):
        while True:
            try:
                guest.screendump(frame)
                ready = watcher.observe(screen_digest(frame))
            except (OSError, RuntimeError, ValueError):
                ready = False  # QMP is not up yet, or the display is mid-modeset
            if ready:
                break
            if clock() >= deadline:
                raise HarnessError(f"The screen never settled at a LUKS passphrase prompt within {timeout}s; "
                                   f"see {frame}")
            sleep(poll)
        shutil.copyfile(frame, run_dir / "luks-prompt-observation.ppm")
        guest.type_test_luks_passphrase()
        if ssh_ready(settle):
            return {"method": "qmp", "attempts": attempt}
        try:
            guest.screendump(run_dir / "luks-after-typing.ppm")
        except (OSError, RuntimeError, ValueError):
            pass
        watcher.rearm()
    raise HarnessError(f"The installed system did not come up after {attempts} LUKS passphrase keystroke answers")


class SerialChannel:
    def __init__(self, path: Path, log: Path, process: subprocess.Popen | None = None) -> None:
        self.path = path
        self.log = log
        self.process = process
        self.socket: socket.socket | None = None
        self.buffer = ""
        self.last_match = ""
        self.condition = threading.Condition()
        self.closed = False
        self.thread: threading.Thread | None = None

    def connect(self, timeout: int = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise HarnessError(f"QEMU exited with status {self.process.returncode} before the serial socket "
                                   f"appeared; see the qemu-*.log next to {self.path}")
            try:
                self.socket = connect_unix(self.path, timeout=0.5)
            except OSError:
                time.sleep(0.2)
                continue
            self.thread = threading.Thread(target=self._reader, daemon=True)
            self.thread.start()
            return
        raise HarnessError(f"Could not connect to VM serial socket {self.path}")

    def _reader(self) -> None:
        assert self.socket is not None
        with self.log.open("ab") as stream:
            while not self.closed:
                try:
                    data = self.socket.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                stream.write(data)
                stream.flush()
                with self.condition:
                    self.buffer += data.decode(errors="replace")
                    self.condition.notify_all()

    def wait_for(self, pattern: str, timeout: int, since: int = 0) -> int:
        """Wait for a match at or after buffer offset ``since``; return the offset just past it.

        The buffer accumulates for the whole boot, so a caller that answers a prompt more than
        once passes the previous return value to skip text it has already answered.
        """
        regex = re.compile(pattern, re.IGNORECASE)
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                match = regex.search(self.buffer, since)
                if match:
                    self.last_match = match.group(0)
                    return match.end()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HarnessError(f"Timed out waiting for LUKS passphrase prompt; see {self.log}")
                self.condition.wait(min(remaining, 0.5))

    def send_line(self, value: str) -> None:
        if self.socket is None:
            raise HarnessError("Serial console is not connected")
        try:
            self.socket.sendall(value.encode() + b"\n")
        except OSError as exc:
            raise HarnessError(f"Could not unlock the test LUKS disk over serial: {exc}") from exc

    def close(self) -> None:
        self.closed = True
        if self.socket is not None:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.socket.close()
        if self.thread is not None:
            self.thread.join(timeout=2)


class VMProcess:
    def __init__(self, argv: Sequence[str], run_dir: Path, label: str) -> None:
        self.argv = list(argv)
        self.run_dir = run_dir
        self.label = label
        self.process: subprocess.Popen | None = None
        self.serial: SerialChannel | None = None
        self.log = run_dir / f"qemu-{label}.log"
        self.serial_log = run_dir / f"serial-{label}.log"

    def start(self) -> None:
        for name in SOCKET_NAMES:
            (self.run_dir / name).unlink(missing_ok=True)
        with self.log.open("ab") as stream:
            stream.write((f"$ {shlex.join(self.argv)}\n").encode())
            stream.flush()
            try:
                # cwd=run_dir so a bare socket name (deep checkouts) resolves there; the
                # preexec hook makes the kernel kill QEMU if the harness itself is killed.
                self.process = subprocess.Popen(
                    self.argv, stdin=subprocess.DEVNULL, stdout=stream,
                    stderr=subprocess.STDOUT, start_new_session=True, cwd=self.run_dir,
                    preexec_fn=functools.partial(die_with_parent, os.getpid()),
                )
            except OSError as exc:
                raise HarnessError(f"Could not start QEMU: {exc}") from exc
        (self.run_dir / f"qemu-{self.label}.pid").write_text(f"{self.process.pid}\n")
        self.serial = SerialChannel(self.run_dir / SOCKET_NAMES[0], self.serial_log, self.process)
        self.serial.connect()

    def wait(self, timeout: int) -> int:
        if self.process is None:
            raise HarnessError("QEMU was not started")
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise HarnessError(f"VM did not shut down within {timeout}s") from None

    def qmp(self, command: str) -> None:
        path = self.run_dir / SOCKET_NAMES[1]
        try:
            with connect_unix(path, timeout=3) as client:
                stream = client.makefile("rwb")
                stream.readline()
                for request in ({"execute": "qmp_capabilities"}, {"execute": command}):
                    stream.write(json.dumps(request).encode() + b"\r\n")
                    stream.flush()
                    stream.readline()
        except OSError:
            pass

    def screendump(self, output: Path) -> Path:
        return qmp_screendump(self.run_dir / SOCKET_NAMES[1], output)

    def type_test_luks_passphrase(self) -> None:
        qmp_type_luks_passphrase(self.run_dir / SOCKET_NAMES[1], TEST_LUKS_PASSPHRASE)

    def cleanup(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.qmp("quit")
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.serial is not None:
            self.serial.close()


def manifest_overrides(expected: dict, defaults: dict, ignore: Sequence[str] = DETECTED_FIELDS) -> dict:
    """What a person typing into the wizard would change: every field that differs from the
    wizard's defaults, except the ones the GUI determines from the machine (``ignore``)."""
    overrides: dict = {}
    for section, values in expected.items():
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if f"{section}.{key}" not in ignore and defaults.get(section, {}).get(key) != value:
                overrides.setdefault(section, {})[key] = value
    return overrides


class ScenarioRunner:
    # Defaults for runners built without __init__ (unit tests) or by tools that never ran a guard.
    target_device = TARGET_DEVICE
    guard_report: dict | None = None
    installed = False
    nonce = ""
    SSH_RETRY_SECONDS = 3.0  # pause between SSH readiness probes

    def __init__(self, args: argparse.Namespace, scenario: Scenario, public_key: str, firmware: OvmfFirmware | None) -> None:
        self.args = args
        self.scenario = scenario
        self.public_key = public_key
        self.firmware = firmware
        VM_ROOT.mkdir(parents=True, exist_ok=True)
        VM_ROOT.chmod(0o700)
        self.run_dir = Path(tempfile.mkdtemp(prefix=f"{scenario.name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-", dir=VM_ROOT))
        self.run_dir.chmod(0o700)
        self.disk = self.run_dir / "disk.qcow2"
        self.iso = args.iso.resolve()
        self.vms: list[VMProcess] = []
        self.logs: list[Path] = []
        self.nonce = secrets.token_hex(8)

    # --- machine -----------------------------------------------------------------------------------

    def vm_arguments(self, live: bool) -> list[str]:
        return qemu_command(
            self.scenario, iso=self.iso, disk=self.disk, run_dir=self.run_dir, live=live,
            ssh_host=self.args.ssh_host, ssh_port=self.args.ssh_port,
            memory_mib=self.args.memory, cpus=self.args.cpus,
            qemu_binary=self.args.qemu_binary, firmware=self.firmware,
            nonce=self.nonce, cd_first=live and self.installed,
            vars_template=getattr(self.args, "ovmf_vars_file", None),
        )

    def start_vm(self, live: bool) -> VMProcess:
        label = "live" if live else "target"
        if not live and self.scenario.hardware.ovmf_vars == "fresh-on-target":
            self.reset_firmware_variables()
        (self.run_dir / f"known_hosts.{label}").unlink(missing_ok=True)  # every boot generates its own SSH host keys
        vm = VMProcess(self.vm_arguments(live), self.run_dir, label)
        self.vms.append(vm)
        vm.start()
        return vm

    def reset_firmware_variables(self) -> None:
        """Boot the installed disk with an empty NVRAM, as on firmware that never saw this installation."""
        variables = self.run_dir / "OVMF_VARS.fd"
        if variables.exists():
            variables.replace(self.run_dir / "OVMF_VARS.live.fd")  # kept: which entries did the installer register?

    def disk_sizes(self) -> list[int]:
        """Exact byte sizes of the target and the extra disks (see vm_guard.unusual_sizes)."""
        cached = self.__dict__.get("_disk_sizes")
        if cached is None:
            gib = self.scenario.disk_gib or getattr(self.args, "disk_size", 40)
            salt = int(self.nonce[:8], 16) if self.nonce else 0
            cached = vm_guard.unusual_sizes([gib * 1024, *self.scenario.hardware.extra_disks], salt)
            self.__dict__["_disk_sizes"] = cached
        return cached

    def create_disks(self) -> None:
        sizes = self.disk_sizes()
        log = self.run_dir / "qemu-img.log"
        images = [(self.disk, "qcow2", sizes[0])]
        images += [(extra_disk_path(self.run_dir, index), "raw", size) for index, size in enumerate(sizes[1:], start=1)]
        for path, image_format, size in images:
            code = run_logged((self.args.qemu_img, "create", "-f", image_format, str(path), str(size)),
                              log, self.args.command_timeout)
            if code != 0:
                raise HarnessError(f"qemu-img could not create the disposable disk {path.name}; see {log}")

    # --- remote access -----------------------------------------------------------------------------

    def ssh(self, user: str, remote: str, log_name: str, timeout: int, *, stdin: bytes | None = None) -> tuple[int, Path]:
        known_hosts = self.run_dir / ("known_hosts.live" if user == self.args.ssh_user else "known_hosts.target")
        log = self.run_dir / log_name
        self.logs.append(log)
        code = run_logged(ssh_command(self.args, known_hosts, user, remote), log, timeout, stdin=stdin)
        return code, log

    def wait_for_ssh(self, user: str, phase: str, vm: VMProcess, timeout: int | None = None) -> None:
        limit = timeout or self.args.boot_timeout
        deadline = time.monotonic() + limit
        log = self.run_dir / f"ssh-{phase}-wait.log"
        command = ssh_command(self.args, self.run_dir / f"known_hosts.{phase}", user, "true")
        while time.monotonic() < deadline:
            if vm.process is not None and vm.process.poll() is not None:
                raise HarnessError(f"QEMU exited with status {vm.process.returncode} before SSH became ready; see {vm.log} and {vm.serial_log}")
            code = run_logged(command, log, min(20, self.args.command_timeout))
            if code == 0:
                return
            time.sleep(min(self.SSH_RETRY_SECONDS, max(0.0, deadline - time.monotonic())))
        raise HarnessError(f"SSH did not become ready within {limit}s; see {log}")

    def gui_environment(self) -> dict[str, str]:
        timeout = min(getattr(self.args, "boot_timeout", 900), getattr(self.args, "gui_timeout", 120))
        return poll_gui_environment(
            lambda attempt, command_timeout: self.ssh(
                self.args.ssh_user,
                shlex.join(("python3", "-c", GUI_DISPLAY_PROBE)),
                f"gui-environment-attempt-{attempt:02d}.log", command_timeout,
            ),
            timeout=timeout, command_timeout=self.args.command_timeout, run_dir=self.run_dir,
        )

    def guest_tool(self, command: str, payload: dict, log_name: str) -> dict:
        """Run a vm_guest.py sub-command in the live guest; returns its report or raises."""
        source = (PROJECT_ROOT / "scripts" / "vm_guest.py").read_text()
        code, log = self.ssh(self.args.ssh_user, shlex.join(("python3", "-c", source, command, json.dumps(payload))),
                             log_name, self.args.command_timeout)
        output = log.read_text(errors="replace") if log.exists() else ""
        for line in reversed(output.strip().splitlines()):
            try:
                report = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(report, dict) and report.get("type") == "vm_guest":
                if not report.get("ok"):
                    raise HarnessError(f"{command} failed in the guest: {report.get('error')}; see {log}")
                return report
        raise HarnessError(f"{command} returned no report (exit {code}); see {log}")

    # --- the disposable-disk guard (TD-N2) -----------------------------------------------------------

    def guard_spec(self, *, blank: bool | None) -> vm_guard.GuardSpec:
        hardware = self.scenario.hardware
        facts = hardware.facts
        sizes = self.disk_sizes()
        common = {"model": facts["model"], "tran": facts["tran"], "pci_vendors": list(facts["pci_vendors"]),
                  "log_sec": hardware.sector_size}
        disks = [vm_guard.DiskExpectation(
            role="target", size=sizes[0], serial=TARGET_SERIAL if hardware.disk_serial else "",
            removable=True if hardware.removable else None, blank=blank, **common)]
        for index in range(1, len(sizes)):
            disks.append(vm_guard.DiskExpectation(role=f"decoy-{index}", size=sizes[index],
                                                  serial=extra_disk_serial(index), **common))
        return vm_guard.GuardSpec(disks=disks, nonce=self.nonce)

    def run_guard(self, log_name: str, *, blank: bool | None) -> dict:
        """Prove in the guest that the disk about to be erased is this run's disk and nothing else is attached."""
        spec = self.guard_spec(blank=blank)
        code, log = self.ssh(self.args.ssh_user, shlex.join(vm_guard.guest_command(spec)), log_name, self.args.command_timeout)
        output = log.read_text(errors="replace") if log.exists() else ""
        try:
            report = vm_guard.parse_report(code, output)
        except ValueError as exc:
            raise HarnessError(f"Guest does not expose the expected disposable disk: {exc}; see {log}") from None
        (self.run_dir / f"{Path(log_name).stem}.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        if not report["ok"]:
            raise HarnessError("Disposable-disk guard refused: " + "; ".join(report["problems"]) + f"; see {log}")
        self.target_device = report["target"]["path"]
        self.guard_report = report
        return report

    def disk_identity(self) -> tuple[str, str, str]:
        """(device, device_id, device_id_kind) for the manifest, from what the guard saw."""
        target = (self.guard_report or {}).get("target")
        if not target:
            return TARGET_DEVICE, TARGET_SERIAL, ""
        if self.scenario.hardware.disk_serial:
            return target["path"], TARGET_SERIAL, ""
        return target["path"], f"{choose_by_path(target)}|{target['size']}|{target['model']}", "path"

    # --- decoys ------------------------------------------------------------------------------------

    def prepare_decoys(self) -> dict:
        """Give every extra disk a GPT, a filesystem and a marker, and remember its checksum."""
        report = self.run_guard("guest-decoy-guard.log", blank=True)
        prepared: dict = {}
        for role, disk in report["disks"].items():
            if role.startswith("decoy"):
                prepared[role] = self.guest_tool("decoy-prepare", {"role": role, "path": disk["path"], "size": disk["size"]},
                                                 f"decoy-prepare-{role}.log")
        (self.run_dir / "decoys.json").write_text(json.dumps(prepared, indent=2, sort_keys=True) + "\n")
        return prepared

    def check_decoys(self, prepared: dict, stage: str) -> None:
        for role, disk in prepared.items():
            check = self.guest_tool("decoy-check", {"path": disk["path"], "size": disk["size"], "sha256": disk["sha256"]},
                                    f"decoy-check-{stage}-{role}.log")
            if not check["unchanged"]:
                raise HarnessError(f"The decoy disk {role} ({disk['path']}) changed {stage}: the installer wrote to a disk it was not given")

    def check_decoy_images(self, prepared: dict) -> None:
        """After every VM has stopped: the raw image file is byte for byte what the guest prepared."""
        for index, role in enumerate(sorted(prepared), start=1):
            digest = hashlib.sha256()
            with extra_disk_path(self.run_dir, index).open("rb") as stream:
                while chunk := stream.read(4 << 20):
                    digest.update(chunk)
            if digest.hexdigest() != prepared[role]["sha256"]:
                raise HarnessError(f"The decoy image {extra_disk_path(self.run_dir, index).name} changed during the run")

    # --- LUKS --------------------------------------------------------------------------------------

    def unlock_luks(self, target: VMProcess) -> dict:
        """Unlock the encrypted target boot and write luks-unlock-receipt.json.

        ``serial`` answers on the serial socket and needs ``console=ttyS0`` on the installed
        kernel command line (the harness adds it to LUKS manifests); without it no prompt can
        appear there, so the keystroke method takes over. ``qmp`` types the fixed test
        passphrase through the emulated keyboard. ``external`` leaves it to an operator.
        """
        requested = getattr(self.args, "luks_unlock", "serial")
        cmdline = InstallManifest.load(self.run_dir / "manifest.json").system.kernel_cmdline_extra
        method, note = requested, ""
        if requested == "serial" and not serial_console_configured(cmdline):
            method = "qmp"
            note = "the installed kernel command line has no console=ttyS0, so no prompt can appear on the serial socket"
            print(f"LUKS unlock: {note}; typing the passphrase through QMP instead", flush=True)
        timeout = getattr(self.args, "luks_timeout", 180)

        def ssh_ready(seconds: int) -> bool:
            try:
                self.wait_for_ssh(TEST_USERNAME, "target", target, timeout=seconds)
            except HarnessError:
                if target.process is not None and target.process.poll() is not None:
                    raise  # QEMU is gone: waiting longer cannot help
                return False
            return True

        if method == "external":
            self.prepare_external_luks_unlock(target)
            receipt: dict = {"method": "external"}
        elif method == "serial":
            if target.serial is None:
                raise HarnessError("Serial LUKS unlock requested but QEMU serial channel is unavailable")
            try:
                receipt = unlock_over_serial(target.serial, TEST_LUKS_PASSPHRASE, timeout, ssh_ready)
            except HarnessError as exc:
                if "passphrase prompt" in str(exc):
                    raise HarnessError(f"{exc}. If the serial log shows no kernel messages, the installed kernel does not have "
                                       f"console=ttyS0: check that system.kernel_cmdline_extra ({cmdline!r}) reached "
                                       "/etc/kernel/cmdline and GRUB_CMDLINE_LINUX") from None
                raise
            receipt["serial_log_sha256"] = hashlib.sha256(target.serial_log.read_bytes()).hexdigest()
        else:
            receipt = unlock_over_qmp(target, TEST_LUKS_PASSPHRASE, timeout, ssh_ready, self.run_dir)
        receipt.update(requested=requested, note=note, timestamp=datetime.now(timezone.utc).isoformat())
        (self.run_dir / "luks-unlock-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        return receipt

    def prepare_external_luks_unlock(self, target: VMProcess) -> None:
        screenshot = target.screendump(self.run_dir / "luks-unlock-screen.ppm")
        instructions = (
            "LUKS unlock method: external operator-controlled VGA via QMP.\n"
            f"QMP socket: {self.run_dir / 'qmp.sock'}\n"
            f"Initial screendump: {screenshot}\n"
            "Inspect the screendump. When the passphrase prompt is visible, you may run:\n"
            f"  python3 {PROJECT_ROOT / 'scripts/vm-qmp.py'} type-test-luks-passphrase {self.run_dir / 'qmp.sock'}\n"
            "That helper sends only the fixed test passphrase and Enter. SSH readiness is polled concurrently "
            f"for up to {self.args.boot_timeout}s."
        )
        (self.run_dir / "luks-unlock-method.txt").write_text(instructions + "\n")
        print(instructions, flush=True)

    def capture_storage_snapshot(self, name: str) -> dict[str, dict[str, str]]:
        command = ("lsblk --json --output PATH,TYPE,UUID,FSTYPE,PARTUUID,PARTTYPE,PTTYPE,PTUUID "
                   + shlex.quote(self.target_device))
        code, log = self.ssh(self.args.ssh_user, command, f"{name}-lsblk.log", self.args.command_timeout)
        if code != 0:
            raise HarnessError(f"Could not capture {name} storage identity; see {log}")
        snapshot = parse_lsblk_snapshot(log.read_text(errors="replace"), self.target_device)
        (self.run_dir / f"{name}-storage-identities.json").write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
        return snapshot

    def capture_failure_screenshots(self) -> list[dict[str, str]]:
        """Capture active guests before cleanup, with each QMP call bounded to five seconds."""
        captures: list[dict[str, str]] = []
        for vm in self.vms:
            process = vm.process
            if process is None or process.poll() is not None:
                continue
            output = self.run_dir / f"failure-{vm.label}.ppm"
            try:
                path = vm.screendump(output)
                captures.append({"vm": vm.label, "path": str(path), "success": "true"})
            except (OSError, RuntimeError, ValueError) as exc:
                captures.append({"vm": vm.label, "success": "false", "error": str(exc)})
        (self.run_dir / "failure-screenshots.json").write_text(json.dumps(captures, indent=2) + "\n")
        return captures

    def check_resume(self) -> dict:
        if self.args.ssh_user != "root":
            raise HarnessError("--check-resume requires root SSH to rerun the daemon with the original manifest")
        self.run_guard("resume-disk-guard.log", blank=False)
        before = self.capture_storage_snapshot("before-resume")
        remote_manifest = "/run/gentoo-installer-vm-manifest.json"
        command = shlex.join(("gentoo-installerd", "--manifest", remote_manifest, "--yes", "--resume"))
        code, log = self.ssh(self.args.ssh_user, command, "resume.log", self.args.install_timeout)
        output = log.read_text(errors="replace")
        # Capture post-resume identities even when the daemon reports failure.
        after = self.capture_storage_snapshot("after-resume")
        report = parse_resume_regression(code, output, before, after)
        report = slim_report(report, self.run_dir / "resume-events.jsonl")
        (self.run_dir / "resume-result.json").write_text(json.dumps(report, indent=2) + "\n")
        if not report["success"]:
            raise HarnessError(f"Resume regression failed: {report['error']}; see {log} and {self.run_dir / 'resume-result.json'}")
        return report

    def gui_detect_enabled(self) -> bool:
        """Whether the GUI must detect firmware, partition table and disk itself (TD-N1).

        On by default for the untouched-defaults scenario, whose point is that nothing is
        pre-filled; ``--gui-detect`` / ``--no-gui-detect`` decide for every other scenario.
        """
        choice = getattr(self.args, "gui_detect", None)
        return self.scenario.defaults if choice is None else bool(choice)

    def gui_plan(self, manifest: InstallManifest, spec: vm_guard.GuardSpec) -> dict:
        detect = self.gui_detect_enabled()
        plan: dict = {"guard": json.loads(spec.to_json()), "detect": detect, "target": (self.guard_report or {}).get("target"),
                      "overrides": {}, "detected": {}}
        if detect:
            plan["overrides"] = manifest_overrides(manifest.to_dict(redact=False), InstallManifest().to_dict(redact=False))
            storage = manifest.storage
            table = self.scenario.table or ("gpt" if storage.firmware == "uefi" else None)
            plan["detected"] = {"storage.device": storage.device, "storage.device_id": storage.device_id,
                                "storage.device_id_kind": storage.device_id_kind, "storage.firmware": storage.firmware,
                                "storage.table": table}
        return plan

    def install(self, *, cancel_at_stage3: bool = False, reinstall: bool = False) -> dict | None:
        """Install onto the guarded disk; returns the refusal report for a scenario that must be refused."""
        self.run_guard("guest-disk-guard.log", blank=not reinstall)
        device, device_id, device_id_kind = self.disk_identity()
        if self.scenario.manual and not reinstall:
            disk = self.guard_report["target"]  # type: ignore[index]
            self.guest_tool("partition-manual", {"role": "target", "path": disk["path"], "size": disk["size"],
                                                 "firmware": self.scenario.firmware}, "partition-manual.log")
        swap_mib = getattr(self.args, "swap_mib", None)
        if reinstall:  # a different layout over the first one: stale signatures at reused offsets
            swap_mib = (512 if swap_mib is None else swap_mib) + 256
        manifest = build_manifest(
            self.scenario, self.public_key, getattr(self.args, "companions", "both"),
            build_jobs=getattr(self.args, "build_jobs", None), swap_mib=swap_mib,
            device=device, device_id=device_id, device_id_kind=device_id_kind,
        )
        manifest_path = self.run_dir / "manifest.json"
        if reinstall and manifest_path.exists():
            manifest_path.replace(self.run_dir / "manifest-first.json")
        manifest.save(manifest_path, redact=False)
        selection = companion_selection(InstallManifest.load(manifest_path))
        (self.run_dir / "install.log.command-metadata.json").write_text(json.dumps({
            "manifest": str(manifest_path), "companions": selection,
        }, indent=2) + "\n")
        remote_manifest = "/run/gentoo-installer-vm-manifest.json"
        code, log = self.ssh(
            self.args.ssh_user, f"cat > {remote_manifest}", "ssh-upload.log",
            self.args.command_timeout, stdin=manifest_path.read_bytes(),
        )
        if code != 0:
            raise HarnessError(f"Could not upload the manifest; see {log}")
        gui = getattr(self.args, "install_mode", "cli") == "gui"
        if gui:
            output_dir = "/tmp/gentoo-vm-gui-cancel" if cancel_at_stage3 else "/tmp/gentoo-vm-gui"
            command = self.prepare_gui_driver(manifest, remote_manifest, output_dir, cancel_at_stage3)
        else:
            command = shlex.join(("gentoo-installerd", "--manifest", remote_manifest, "--yes"))
        code, log = self.ssh(self.args.ssh_user, command, "install-2.log" if reinstall else "install.log",
                             self.args.install_timeout)
        output = log.read_text(errors="replace")
        if cancel_at_stage3:
            cancel_result = slim_report(parse_cancel_regression(code, output), self.run_dir / "cancel-events.jsonl")
            (self.run_dir / "cancel-result.json").write_text(json.dumps(cancel_result, indent=2) + "\n")
            if not cancel_result["success"]:
                raise HarnessError(f"GUI cancellation regression failed: {cancel_result['error']}; see {log} and {self.run_dir / 'cancel-result.json'}")
        else:
            result = parse_install_result(code, output)
            write_events(self.run_dir / "events.jsonl", result.events)
            (self.run_dir / "install-result.json").write_text(json.dumps({
                "success": result.success, "error": result.error, "event_summary": summarize_events(result.events),
                "events_file": "events.jsonl"}, indent=2) + "\n")
            if self.scenario.expect_refusal and not reinstall:
                return self.check_refusal(code, output, log)
        if gui:
            artifacts = ["review.png", "result.png", "gui.log"] + (["detected.json"] if self.gui_detect_enabled() else [])
            for name in artifacts:
                remote_path = f"{output_dir}/{name}"
                code, artifact_log = self.ssh(
                    self.args.ssh_user,
                    f"base64 -w0 -- {shlex.quote(remote_path)}",
                    f"gui-{name}.b64", self.args.command_timeout,
                )
                if code != 0:
                    raise HarnessError(f"Could not retrieve GUI artifact {name}; see {artifact_log}")
                data = decode_gui_artifact(name, artifact_log.read_text(errors="strict"))
                local_name = f"cancel-gui-{name}" if cancel_at_stage3 else f"gui-{name}"
                local_path = self.run_dir / local_name
                local_path.write_bytes(data)
                self.logs.append(local_path)
            code, daemon_log = self.ssh(
                self.args.ssh_user, "cat /var/lib/gentoo-installer/install.log",
                "daemon.log", self.args.command_timeout,
            )
            if code != 0 or not daemon_log.stat().st_size:
                raise HarnessError(f"Could not retrieve the daemon log; see {daemon_log}")
        if not cancel_at_stage3:
            validate_install_outcome(result, log, gui=gui)
            self.installed = True
        return None

    def prepare_gui_driver(self, manifest: InstallManifest, remote_manifest: str, output_dir: str,
                           cancel_at_stage3: bool) -> str:
        """Upload the GUI driver, the guard module and the plan; return the command that runs the driver."""
        uploads = (("/run/vm-gui-install.py", (PROJECT_ROOT / "scripts/vm-gui-install.py").read_bytes(), "gui-upload.log"),
                   ("/run/vm_guard.py", (PROJECT_ROOT / "scripts/vm_guard.py").read_bytes(), "gui-guard-upload.log"),
                   ("/run/vm-gui-plan.json",
                    (json.dumps(self.gui_plan(manifest, self.guard_spec(blank=None)), indent=2) + "\n").encode(),
                    "gui-plan-upload.log"))
        for remote_path, data, log_name in uploads:
            code, log = self.ssh(self.args.ssh_user, f"cat > {remote_path}", log_name, self.args.command_timeout, stdin=data)
            if code != 0:
                raise HarnessError(f"Could not upload {remote_path}; see {log}")
        # The manifest and the plan hold the test credentials: readable by the GUI user only.
        setup = (f"chown gentoo:gentoo-installer {remote_manifest} /run/vm-gui-plan.json && "
                 f"chmod 600 {remote_manifest} /run/vm-gui-plan.json && chmod 644 /run/vm-gui-install.py /run/vm_guard.py")
        code, log = self.ssh(self.args.ssh_user, setup, "gui-access.log", self.args.command_timeout)
        if code != 0:
            raise HarnessError(f"Could not prepare the non-root GUI session; see {log}")
        environment = self.gui_environment()
        env_args = [f"{key}={value}" for key, value in environment.items()]
        return shlex.join(("runuser", "-u", "gentoo", "--", "env", *env_args,
                           "python3", "/run/vm-gui-install.py", remote_manifest,
                           "--output", output_dir, "--plan", "/run/vm-gui-plan.json",
                           *(("--cancel-at-stage3",) if cancel_at_stage3 else ())))

    def check_refusal(self, code: int, output: str, log: Path) -> dict:
        """The installer must have refused this disk, said why, run nothing destructive and left it blank."""
        result = parse_install_result(code, output)
        problems = []
        if result.success:
            problems.append("the installer accepted a disk it should have refused")
        elif not re.search(self.scenario.expect_refusal, result.error):
            problems.append(f"the refusal {result.error!r} does not match {self.scenario.expect_refusal!r}")
        destructive = destructive_runner_commands(output)
        if destructive:
            problems.append("destructive commands ran before the refusal: " + ", ".join(destructive))
        try:
            self.run_guard("refusal-disk-guard.log", blank=True)
        except HarnessError as exc:
            problems.append(f"the disk is no longer blank: {exc}")
        report = {"success": not problems, "error": "; ".join(problems), "refusal": result.error,
                  "destructive_commands": destructive}
        (self.run_dir / "refusal-result.json").write_text(json.dumps(report, indent=2) + "\n")
        if problems:
            raise HarnessError("Refusal check failed: " + "; ".join(problems) + f"; see {log}")
        return report

    def verify_cancel_cleanup(self) -> dict:
        if self.args.ssh_user != "root":
            raise HarnessError("--cancel-then-resume requires root SSH for guest cleanup checks and resume")
        check = (
            "python3 -c " + shlex.quote(
                "from pathlib import Path; import json, sys; "
                "mounts=[fields[4].replace('\\\\040',' ') for line in Path('/proc/self/mountinfo').read_text().splitlines() "
                "if len(fields := line.split()) > 4 and (fields[4] == '/mnt/gentoo' or fields[4].startswith('/mnt/gentoo/'))]; "
                "left=[p for p in ('/dev/mapper/gentoo-root','/dev/gentoo/root') if Path(p).exists()]; "
                "swaps=[line.split()[0] for line in Path('/proc/swaps').read_text().splitlines()[1:] "
                f"if line.split() and line.split()[0].startswith(({self.target_device!r},'/dev/mapper/gentoo','/dev/gentoo/'))]; "
                "print(json.dumps({'target_mounts':mounts,'leftover_root_stack':left,'target_swaps':swaps})); "
                "sys.exit(bool(mounts or left or swaps))"
            )
        )
        code, log = self.ssh(self.args.ssh_user, check, "cancel-cleanup.log", self.args.command_timeout)
        output = log.read_text(errors="replace")
        report = parse_cancel_cleanup(code, output)
        report["output"] = output
        (self.run_dir / "cancel-cleanup-result.json").write_text(json.dumps(report, indent=2) + "\n")
        if not report["success"]:
            raise HarnessError(f"Cancellation cleanup check failed: {report['error']}; see {log}")
        return report

    def verify(self, label: str = "") -> list[ProbeResult]:
        """Run every probe of the installed system; ``label`` keeps the logs of a second round apart."""
        expected_init = "systemd" if self.scenario.init == "systemd" else "init"
        probes: list[tuple] = [
            ("init", "ps -p 1 -o comm=", lambda c, o: parse_probe_result("init", c, o, expected_init)),
            ("hostname", "hostname", lambda c, o: parse_probe_result("hostname", c, o, self.scenario.hostname)),
            ("user", "id -un", lambda c, o: parse_probe_result("user", c, o, TEST_USERNAME)),
            ("network", "ip -4 -o address show scope global up; ip -4 route show default", parse_network_result),
            ("dns", "getent ahostsv4 distfiles.gentoo.org", lambda c, o: parse_probe_result("dns", c, o)),
            ("portage", "emerge --search --quiet portage", self.parse_portage),
        ]
        manifest_path = self.run_dir / "manifest.json"
        if not manifest_path.is_file():
            raise HarnessError(f"Saved install manifest is missing; cannot derive companion probes: {manifest_path}")
        manifest = InstallManifest.load(manifest_path)
        probes.extend(companion_probe_plan(manifest))
        probes.extend(desktop_probe_plan(manifest))
        static_probe = static_network_probe(manifest)
        if static_probe is not None:
            command, parser = static_probe
            probes.append(("network-static", command, parser))
        probes.extend(installed_system_probes(manifest, self.scenario))
        results: list[ProbeResult] = []
        for name, command, parser, *rest in probes:
            code, log = self.ssh(TEST_USERNAME, command, f"verify-{name}{label}.log", self.args.command_timeout,
                                 stdin=rest[0] if rest else None)
            result = parser(code, log.read_text(errors="replace"))
            results.append(result)
        return results

    @staticmethod
    def parse_portage(returncode: int, output: str) -> ProbeResult:
        result = parse_probe_result("portage", returncode, output)
        if result.success and "sys-apps/portage" not in result.output:
            return ProbeResult("portage", False, result.output, "Portage query returned no sys-apps/portage match")
        return result

    def power_off_live(self, live: VMProcess) -> None:
        code, log = self.ssh(self.args.ssh_user, "poweroff", "shutdown-live.log", self.args.command_timeout)
        if code not in (0, 255):
            raise HarnessError(f"Could not request live-environment shutdown; see {log}")
        live.wait(self.args.shutdown_timeout)
        live.cleanup()

    def boot_and_verify(self, result: dict, key: str = "probes", label: str = "") -> VMProcess:
        """Boot the installed disk alone, unlock it if needed, and run every probe of the installed system."""
        target = self.start_vm(live=False)
        if self.scenario.luks:
            result["luks_unlock"] = self.unlock_luks(target)
        self.wait_for_ssh(TEST_USERNAME, "target", target)
        probes = self.verify(label)
        strict = getattr(self.args, "strict_probes", False)
        result[key] = [asdict(probe) for probe in probes]
        result["companion_probes"] = [asdict(probe) for probe in probes if probe.name in {"hwmonux", "portagehub"}]
        result["disabled_companion_probes"] = [asdict(probe) for probe in probes if probe.name.startswith("disabled-")]
        result["advisory_findings"] = [asdict(probe) for probe in probes if not probe.success and probe.advisory]
        for probe in probes:
            if not probe.success and probe.advisory:
                print(f"WARNING {self.scenario.name}: advisory probe {probe.name} failed: {probe.error}", file=sys.stderr)
        failures = [f"{probe.name}: {probe.error}" for probe in probes if not probe.success and (strict or not probe.advisory)]
        if failures:
            raise HarnessError("Target verification failed: " + "; ".join(failures))
        return target

    # --- rescue (TD-N4): damage an installed system from the live ISO, show it is broken, repair it ---------

    def boot_live(self) -> VMProcess:
        live = self.start_vm(live=True)
        self.wait_for_ssh(self.args.ssh_user, "live", live)
        return live

    def stop_vm(self, guest: VMProcess) -> None:
        """Press the power button, then make sure the guest is gone."""
        guest.qmp("system_powerdown")
        if guest.process is not None:
            try:
                guest.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        guest.cleanup()

    def attach_target_image(self) -> None:
        """Use an existing disk image as the target through a copy-on-write overlay: the image itself is never written."""
        image = Path(self.args.target_image)
        if image.is_symlink() or not image.is_file() or image.suffix.lower() not in {".qcow2", ".img", ".raw"}:
            raise HarnessError(f"--target-image must name a regular .qcow2, .img or .raw file: {image}")
        image = image.resolve()
        if self.run_dir in image.parents or any(char in str(image) for char in (",", "\n", "\r", "\x00")):
            raise HarnessError("--target-image must be outside the run directory and have no commas in its path")
        info_log = self.run_dir / "qemu-img-info.log"
        if run_logged((self.args.qemu_img, "info", "--output=json", str(image)), info_log, self.args.command_timeout) != 0:
            raise HarnessError(f"qemu-img could not read {image}; see {info_log}")
        try:
            info = json.loads(info_log.read_text())
            image_format, size = str(info["format"]), int(info["virtual-size"])
        except (ValueError, KeyError, TypeError) as exc:
            raise HarnessError(f"qemu-img info returned no usable format and size for {image}: {exc}") from None
        if image_format not in {"qcow2", "raw"}:
            raise HarnessError(f"--target-image has format {image_format}; only qcow2 and raw images are supported")
        code = run_logged((self.args.qemu_img, "create", "-f", "qcow2", "-b", str(image), "-F", image_format, str(self.disk)),
                          self.run_dir / "qemu-img.log", self.args.command_timeout)
        if code != 0:
            raise HarnessError(f"qemu-img could not create the overlay; see {self.run_dir / 'qemu-img.log'}")
        status = image.stat()
        self.__dict__["_base_image"] = (image, status.st_size, status.st_mtime_ns)
        self.__dict__["_disk_sizes"] = [size]
        manifest = Path(getattr(self.args, "target_manifest", None) or image.parent / "manifest.json")
        if not manifest.is_file():
            raise HarnessError(f"The manifest the image was installed with is needed for the probes: {manifest} (use --target-manifest)")
        shutil.copyfile(manifest, self.run_dir / "manifest.json")
        self.installed = True

    def check_base_image_untouched(self) -> bool:
        image, size, mtime = self.__dict__["_base_image"]
        status = image.stat()
        if (status.st_size, status.st_mtime_ns) != (size, mtime):
            raise HarnessError(f"The base image {image} changed during the run: it must only ever be a read-only backing file")
        return True

    def break_installed_system(self, mode: str) -> dict:
        """In the running live system: damage the installed system on the guarded disk."""
        report = self.run_guard("rescue-disk-guard.log", blank=False)
        disk = report["target"]
        details = self.guest_tool("rescue-break", {"mode": mode, "path": disk["path"], "size": disk["size"]}, "rescue-break.log")
        (self.run_dir / "rescue-break.json").write_text(json.dumps(details, indent=2, sort_keys=True) + "\n")
        return details

    def prove_broken(self, mode: str, details: dict) -> dict:
        """The damage must be real: the machine does not come up (or, for the fstab, findmnt rejects it)."""
        if mode == "fstab-uuid":
            # A wrong fstab does not always stop the boot, so the proof is the guest's own verification.
            if details.get("findmnt_exit", 0) == 0:
                raise HarnessError("findmnt --verify accepts the damaged fstab: the break did not take effect")
            return {"proof": "findmnt --verify", "exit": details["findmnt_exit"], "output": details.get("findmnt_output", "")}
        timeout = getattr(self.args, "broken_timeout", 120)
        target = self.start_vm(live=False)
        try:
            self.wait_for_ssh(TEST_USERNAME, "broken", target, timeout=timeout)
        except HarnessError as exc:
            reason = str(exc)
        else:
            raise HarnessError(f"The system still boots after breaking {mode}: a repair test on it would prove nothing")
        evidence: dict = {"proof": "no SSH", "reason": reason, "timeout": timeout}
        try:
            evidence["screenshot"] = target.screendump(self.run_dir / "broken-boot.ppm").name
        except (OSError, RuntimeError, ValueError) as exc:
            evidence["screenshot_error"] = str(exc)
        excerpt = tail(target.serial_log, 40)
        (self.run_dir / "broken-boot-serial.txt").write_text(excerpt + "\n")
        evidence["markers"] = [marker for marker in BROKEN_BOOT_MARKERS if marker in excerpt]
        target.cleanup()
        return evidence

    def run_repair(self, command: str) -> dict:
        """Run the repair command as root in the live system; {device} stands for the guarded target disk."""
        remote = command.replace("{device}", shlex.quote(self.target_device))
        code, log = self.ssh(self.args.ssh_user, remote, "rescue-command.log", getattr(self.args, "rescue_timeout", 1800))
        report = {"command": remote, "exit": code, "log": log.name}
        if code != 0:
            raise HarnessError(f"The rescue command failed with exit status {code}; see {log}")
        return report

    def rescue_cycle(self, mode: str, command: str, live: VMProcess | None = None) -> dict:
        """Break the installed system, show it is broken, repair it. Leaves no VM running."""
        rescue: dict = {"break": mode, "command": command or None, "disk": str(self.disk)}
        live = live or self.boot_live()
        if mode != "none":
            rescue["details"] = self.break_installed_system(mode)
            self.power_off_live(live)
            live = None
            rescue["broken"] = self.prove_broken(mode, rescue["details"])
        if command:
            live = live or self.boot_live()
            rescue["repair"] = self.run_repair(command)
        if live is not None:
            self.power_off_live(live)
        return rescue

    def run(self) -> dict:
        result = {"scenario": self.scenario.name, "success": False, "run_dir": str(self.run_dir),
                  "companions": None, "companion_probes": [], "disabled_companion_probes": [],
                  "probes": [], "resume": None, "cancel": None, "luks_unlock": None,
                  "decoys": None, "refusal": None, "rescue": None, "live": None, "hardware": asdict(self.scenario.hardware),
                  "firmware": self.scenario.firmware}
        mode = getattr(self.args, "rescue_break", "none")
        command = getattr(self.args, "rescue_command", None) or ""
        try:
            validate_command_paths(self.iso, self.disk, self.run_dir)
            if self.disk.exists():
                raise HarnessError("Refusing to overwrite an existing virtual disk")
            if getattr(self.args, "target_image", None):
                return self.run_on_existing_image(result, mode, command)
            if getattr(self.args, "live_only", False):
                return self.run_live_smoke(result)
            self.create_disks()
            live = self.boot_live()
            decoys = self.prepare_decoys() if self.scenario.decoy else None
            result["decoys"] = decoys
            if self.args.cancel_then_resume:
                self.install(cancel_at_stage3=True)
                result["cancel"] = self.verify_cancel_cleanup()
                result["resume"] = self.check_resume()
            else:
                refusal = self.install()
                if refusal is not None:  # the installer refused the disk, as this scenario requires
                    result["refusal"] = refusal
                    self.power_off_live(live)
                    result["success"] = True
                    return result
                if self.scenario.reinstall:
                    self.install(reinstall=True)
            saved_manifest = InstallManifest.load(self.run_dir / "manifest.json")
            result["companions"] = companion_selection(saved_manifest)
            if self.args.check_resume:
                result["resume"] = self.check_resume()
            if decoys:
                self.check_decoys(decoys, "during the installation")
            self.power_off_live(live)
            rescuing = mode != "none" or bool(command)
            target = self.boot_and_verify(result, "probes_before_break" if rescuing else "probes",
                                          "-before-break" if rescuing else "")
            if rescuing:
                self.stop_vm(target)
                result["rescue"] = self.rescue_cycle(mode, command)
                if command:
                    target = self.boot_and_verify(result)
            if decoys:
                target.cleanup()  # every VM is stopped: the image files can be compared safely
                self.check_decoy_images(decoys)
            result["success"] = True
            return result
        except BaseException as exc:
            result["error"] = str(exc)
            if isinstance(exc, KeyboardInterrupt):
                result["interrupted"] = getattr(exc, "signum", int(signal.SIGINT))
            raise
        finally:
            self.finish_run(result)

    def run_live_smoke(self, result: dict) -> dict:
        """Boot the live ISO on this machine shape and check what the installer sees; install nothing."""
        self.create_disks()
        live = self.boot_live()
        guard = self.run_guard("guest-disk-guard.log", blank=True)
        live_result: dict = {"guard": {"device": guard["target"]["path"], "size": guard["target"]["size"]}}
        result["live"] = live_result
        code, log = self.ssh(self.args.ssh_user, shlex.join(("python3", "-c", INSPECT_SNIPPET)), "live-inspect.log",
                             self.args.command_timeout)
        inspection = parse_live_inspection(code, log.read_text(errors="replace"))
        (self.run_dir / "live-inspection.json").write_text(json.dumps(inspection, indent=2, sort_keys=True) + "\n")
        live_result["inspection"] = {key: inspection.get(key) for key in ("architecture", "memory_bytes", "uefi", "network", "errors")}
        problems = live_inspection_problems(inspection, firmware=self.scenario.firmware, memory_mib=self.args.memory,
                                            target_size=self.disk_sizes()[0])
        expectations = getattr(self.args, "live_expect", None)
        if expectations is not None:
            problems += check_expectations(inspection, json.loads(Path(expectations).read_text()))
        if self.scenario.hardware.vga != "none":
            try:
                live_result["gui"] = {"environment": sorted(self.gui_environment())}
            except HarnessError as exc:
                problems.append(f"the graphical live session did not start on this display adapter: {exc}")
        live_result["problems"] = problems
        self.power_off_live(live)
        if problems:
            raise HarnessError("The live system does not work on this machine: " + "; ".join(problems))
        result["success"] = True
        return result

    def run_on_existing_image(self, result: dict, mode: str, command: str) -> dict:
        """Boot, damage, repair and verify a disk that was installed earlier (or by another distribution's installer)."""
        self.attach_target_image()
        live = self.boot_live()
        self.run_guard("guest-disk-guard.log", blank=False)
        result["companions"] = companion_selection(InstallManifest.load(self.run_dir / "manifest.json"))
        result["rescue"] = rescue = self.rescue_cycle(mode, command, live)
        if mode == "none" and not command:
            rescue["note"] = "no break and no repair requested: the image was only booted and verified"
        self.boot_and_verify(result)
        rescue["base_image_unchanged"] = self.check_base_image_untouched()
        result["success"] = True
        return result

    def finish_run(self, result: dict) -> None:
        saved_manifest_path = self.run_dir / "manifest.json"
        if saved_manifest_path.is_file():
            try:
                saved_manifest = InstallManifest.load(saved_manifest_path)
                result["companions"] = companion_selection(saved_manifest)
            except Exception as exc:
                result["companion_metadata_error"] = f"Could not read saved manifest metadata: {exc}"
                if result["success"]:
                    result["success"] = False
                    result["error"] = result["companion_metadata_error"]
        if not result["success"]:
            try:
                result["failure_screenshots"] = self.capture_failure_screenshots()
            except OSError as exc:
                result["failure_screenshots_error"] = str(exc)
        for vm in reversed(self.vms):
            vm.cleanup()
        for name in ("serial.sock", "qmp.sock", "qemu-live.pid", "qemu-target.pid"):
            (self.run_dir / name).unlink(missing_ok=True)
        keep = self.args.keep or bool(result.get("rescue"))  # a rescue run exists to leave a disk behind
        if result["success"] and not keep:
            for path in [self.run_dir / "OVMF_VARS.fd", self.run_dir / "OVMF_VARS.live.fd", self.disk,
                         *self.run_dir.glob("extra-*.img")]:
                path.unlink(missing_ok=True)
        result["disk"] = {"device": self.target_device, "bytes": self.disk_sizes()[0]}
        (self.run_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")


def find_stale_qemu(vm_root: Path | None = None, proc_root: Path = Path("/proc")) -> list[tuple[Path, int]]:
    """(pid file, pid) of live QEMU processes that earlier harness runs left behind.

    A pid file only counts when /proc/<pid> is a qemu-system process owned by this user whose
    command line names that run directory and the harness's ``-name gentoo-installer-*``, so a
    recycled pid never matches an unrelated process.
    """
    root = (vm_root or VM_ROOT)
    found: list[tuple[Path, int]] = []
    for pid_file in sorted(root.glob("*/qemu-*.pid")):
        try:
            pid = int(pid_file.read_text().strip())
            process = proc_root / str(pid)
            argv = [part.decode(errors="replace") for part in (process / "cmdline").read_bytes().split(b"\0") if part]
            owner = process.stat().st_uid
        except (OSError, ValueError):
            continue
        run_dir = str(pid_file.parent)
        named = any(arg.startswith("gentoo-installer-") for arg in argv)
        if (argv and Path(argv[0]).name.startswith("qemu-system") and named
                and any(run_dir in arg for arg in argv) and owner == os.getuid()):
            found.append((pid_file, pid))
    return found


def cleanup_stale(vm_root: Path | None = None, proc_root: Path = Path("/proc"), *,
                  kill: Callable[[int, int], None] = os.kill, sleep: Callable[[float], None] = time.sleep,
                  grace: float = 10.0) -> list[int]:
    """Terminate stale harness QEMU processes (SIGTERM, then SIGKILL after ``grace``)."""
    stopped: list[int] = []
    for pid_file, pid in find_stale_qemu(vm_root, proc_root):
        try:
            kill(pid, signal.SIGTERM)
            waited = 0.0
            while (proc_root / str(pid)).exists() and waited < grace:
                sleep(0.5)
                waited += 0.5
            if (proc_root / str(pid)).exists():
                kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        pid_file.unlink(missing_ok=True)
        stopped.append(pid)
    return stopped


def public_key_from_private(ssh_keygen: str, private_key: Path, timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            (ssh_keygen, "-y", "-f", str(private_key)), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"Could not derive the SSH public key: {exc}") from exc
    if result.returncode != 0:
        raise HarnessError(f"Could not derive the SSH public key: {result.stderr.strip()}")
    return result.stdout.strip()


def check_port(host: str, port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((host, port))
    except OSError as exc:
        raise HarnessError(f"SSH forward endpoint {host}:{port} is unavailable: {exc}") from exc


def preflight(args: argparse.Namespace, scenarios: Sequence[Scenario]) -> OvmfFirmware | None:
    if not args.ssh_host or not args.ssh_port or not args.ssh_private_key:
        raise HarnessError("Refusing to run without --ssh-host, --ssh-port, and --ssh-private-key (a key-only test control channel)")
    if args.ssh_user != "root":
        raise HarnessError("The live environment must permit root SSH for the real backend CLI; use --ssh-user root")
    if args.ssh_port > 65535:
        raise HarnessError("--ssh-port must be between 1 and 65535")
    args.ssh_host = validate_loopback(args.ssh_host)
    args.ssh_private_key = args.ssh_private_key.resolve()
    if not args.ssh_private_key.is_file():
        raise HarnessError(f"SSH private key does not exist: {args.ssh_private_key}")
    args.iso = args.iso.resolve()
    if not args.iso.is_file() or args.iso.suffix.lower() != ".iso":
        raise HarnessError(f"--iso must name an existing ISO file: {args.iso}")
    for attribute in ("qemu_binary", "qemu_img", "ssh_binary", "ssh_keygen"):
        if shutil.which(getattr(args, attribute)) is None:
            raise HarnessError(f"Required executable not found: {getattr(args, attribute)}")
    check_port(args.ssh_host, args.ssh_port)
    template = getattr(args, "ovmf_vars_file", None)
    if template is not None and not template.is_file():
        raise HarnessError(f"--ovmf-vars-file does not exist: {template}")
    secure = any(scenario.hardware.secure_boot for scenario in scenarios)
    firmware = detect_ovmf(secure=True) if secure else detect_ovmf()
    if any(scenario.firmware == "uefi" for scenario in scenarios) and firmware is None:
        raise HarnessError("The Secure Boot build of OVMF was not found (OVMF_CODE.secboot.fd)" if secure else
                           "OVMF was not found; UEFI scenarios cannot run (the bios scenario does not need OVMF)")
    return firmware


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def disk_size_mib(value: str) -> int:
    """'256M', '2G' or a bare number of GiB, as MiB."""
    match = re.fullmatch(r"([0-9]+)([MmGg]?)", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("use a size such as 256M or 2G")
    number = int(match.group(1))
    return number if match.group(2).upper() == "M" else number * 1024


def apply_overrides(scenario: Scenario, args: argparse.Namespace) -> Scenario:
    """The scenario with the command line's hardware options applied (an option left out keeps the scenario's own)."""
    changes: dict = {}
    for option in ("disk_bus", "sector_size", "nic", "vga", "cpu", "machine", "smbios", "ovmf_vars", "rtc"):
        value = getattr(args, option, None)
        if value is not None:
            changes[option] = value
    if getattr(args, "no_disk_serial", False):
        changes["disk_serial"] = False
    if getattr(args, "removable_disk", False):
        changes["removable"] = True
    if getattr(args, "secure_boot", False):
        changes["secure_boot"] = True
    if getattr(args, "extra_disk", None):
        changes["extra_disks"] = tuple(args.extra_disk)
    return dataclasses.replace(scenario, firmware=getattr(args, "firmware", None) or scenario.firmware,
                               hardware=dataclasses.replace(scenario.hardware, **changes))


def rescue_problems(scenario: Scenario, args: argparse.Namespace) -> list[str]:
    """Rescue combinations that cannot work with this scenario."""
    problems = []
    if args.target_image is not None and (scenario.decoy or scenario.reinstall or scenario.expect_refusal):
        problems.append("--target-image cannot be combined with a decoy, reinstall or refusal scenario")
    if args.target_image is not None and args.install_mode == "gui":
        problems.append("--target-image does not install, so --install-mode gui has nothing to drive")
    return [f"{scenario.name}: {problem}" for problem in problems]


def hardware_problems(scenario: Scenario, install_mode: str) -> list[str]:
    """Combinations that cannot work, found before anything starts."""
    problems = scenario.hardware.problems()
    if scenario.firmware == "bios" and scenario.hardware.secure_boot:
        problems.append("--secure-boot needs UEFI firmware")
    if scenario.firmware == "bios" and scenario.hardware.ovmf_vars != "persist":
        problems.append("--ovmf-vars only applies to UEFI firmware")
    if scenario.hardware.vga == "none" and install_mode == "gui":
        problems.append("--vga none leaves the graphical installer without a display; use --install-mode cli")
    if scenario.luks and scenario.hardware.vga == "none":
        problems.append("--vga none leaves no screen for the keystroke LUKS unlock; add console=ttyS0 and use the serial unlock")
    return [f"{scenario.name}: {problem}" for problem in problems]


def list_scenarios() -> str:
    lines = [f"{'scenario':<18} {'init':<8} {'fw':<5} {'in --all':<9} machine and notes"]
    for name, scenario in SCENARIO_MAP.items():
        hw = scenario.hardware
        shape = f"{hw.disk_bus}{'/4Kn' if hw.sector_size == 4096 else ''}{'' if hw.disk_serial else '/no-serial'}, {hw.nic}, {hw.vga}"
        extras = [text for text in (f"smbios={hw.smbios}" if hw.smbios != "qemu" else "", f"cpu={hw.cpu}" if hw.cpu else "",
                                    f"machine={hw.machine}" if hw.machine != "q35" else "", "luks" if scenario.luks else "",
                                    f"+{len(hw.extra_disks)} extra disk(s)" if hw.extra_disks else "",
                                    "fresh NVRAM on the target boot" if hw.ovmf_vars == "fresh-on-target" else "") if text]
        lines.append(f"{name:<18} {scenario.init:<8} {scenario.firmware:<5} {'yes' if scenario.in_all else 'no':<9} "
                     f"{shape}{'; ' + ', '.join(extras) if extras else ''}{' - ' + scenario.note if scenario.note else ''}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--scenario", choices=SCENARIOS)
    selection.add_argument("--all", action="store_true", help="Run the fast scenarios (see docs/TESTING.md)")
    selection.add_argument("--cleanup-stale", action="store_true",
                           help="Stop QEMU processes left behind by an earlier harness run (found through build/vm/*/qemu-*.pid) and exit")
    selection.add_argument("--list-scenarios", action="store_true", help="Print the scenario catalogue and exit")
    parser.add_argument("--iso", type=Path, help="The (test-key) live ISO to boot")
    parser.add_argument("--ssh-host", help="Loopback address of the QEMU SSH forward (normally 127.0.0.1)")
    parser.add_argument("--ssh-port", type=positive_int, help="Unused host TCP port forwarded to guest SSH")
    parser.add_argument("--ssh-private-key", type=Path, help="Ephemeral key already authorized in the test ISO")
    parser.add_argument("--ssh-user", default="root", help="Live ISO SSH user (must be root)")
    parser.add_argument("--disk-size", type=positive_int, default=40, help="Disposable qcow2 size in GiB (default: 40)")
    parser.add_argument("--memory", type=positive_int, default=4096, help="VM memory in MiB (default: 4096)")
    parser.add_argument("--cpus", type=positive_int, default=2)
    parser.add_argument("--build-jobs", type=positive_int, default=None,
                        help="Target Portage build jobs, 1–256 (default: 2; the defaults scenario keeps the GUI default)")
    parser.add_argument("--swap-mib", type=int, default=None,
                        help="Swap on the disposable target disk, 0–131072 MiB (default: 512; the defaults scenario keeps the GUI default)")
    parser.add_argument("--boot-timeout", type=positive_int, default=900)
    parser.add_argument("--install-timeout", type=positive_int, default=21600)
    parser.add_argument("--shutdown-timeout", type=positive_int, default=120)
    parser.add_argument("--command-timeout", type=positive_int, default=120)
    parser.add_argument("--gui-timeout", type=positive_int, default=120,
                        help="Seconds to wait for the live XFCE session after SSH is up (raise it when QEMU falls back to TCG)")
    parser.add_argument("--luks-timeout", type=positive_int, default=180)
    parser.add_argument("--luks-unlock", choices=("serial", "qmp", "external"), default="serial",
                        help="Unlock a LUKS test VM on the serial console (default; falls back to qmp when the installed "
                             "kernel has no console=ttyS0), by typing the test passphrase through QMP, or by an operator")
    parser.add_argument("--check-resume", action="store_true",
                        help="Rerun the successful install with --resume and verify storage identities remain unchanged")
    parser.add_argument("--cancel-then-resume", action="store_true",
                        help="In GUI mode, cancel at stage3, verify cleanup, then resume on the same guarded disk")
    parser.add_argument("--keep", action="store_true", help="Keep the disk and firmware copy after success (the manifest is always retained)")
    parser.add_argument("--install-mode", choices=("cli", "gui"), default="cli", help="Drive the real backend CLI or non-root graphical installer")
    parser.add_argument("--companions", choices=("both", "portagehub", "none"), default="both",
                        help="Install HWMonux and PortageHub (both), only PortageHub, or neither")
    parser.add_argument("--gui-detect", action=argparse.BooleanOptionalAction, default=None,
                        help="GUI mode: leave firmware, partition table and disk to the GUI's own detection and assert what "
                             "it chose (default: on for the defaults scenario only)")
    parser.add_argument("--strict-probes", action="store_true",
                        help="Count advisory probe findings (sudo, root lock, install receipt) as failures")
    parser.add_argument("--live-only", action="store_true",
                        help="Only boot the live ISO on this machine shape and check what the installer sees of it "
                             "(firmware, memory, network, disk, the graphical session); nothing is installed")
    parser.add_argument("--live-expect", type=Path, metavar="FILE",
                        help="With --live-only: a JSON object of dotted paths into the installer's inspection and the values "
                             "they must have ('disks.0.model': 're:QEMU')")
    rescue = parser.add_argument_group(
        "rescue", "Test repairing a system that does not boot (docs/TESTING.md). These runs keep the disk.")
    rescue.add_argument("--rescue-break", choices=RESCUE_BREAKS, default="none",
                        help="After a verified install, boot the live ISO again, damage the installed system (grub.cfg, the "
                             "initramfs images or the root UUID in fstab) and show that it no longer boots")
    rescue.add_argument("--rescue-command", metavar="COMMAND",
                        help="Run this as root in the live system to repair it ({device} is the target disk), then boot the "
                             "disk again and verify it")
    rescue.add_argument("--broken-timeout", type=positive_int, default=120,
                        help="Seconds a damaged system gets to come up before it counts as broken (default: 120)")
    rescue.add_argument("--rescue-timeout", type=positive_int, default=1800,
                        help="Seconds the rescue command may run (default: 1800)")
    rescue.add_argument("--target-image", type=Path, metavar="PATH",
                        help="Use an existing qcow2/raw image (a kept run's disk.qcow2, or another distribution's) instead of "
                             "installing; it is only read, changes go to an overlay")
    rescue.add_argument("--target-manifest", type=Path, metavar="PATH",
                        help="Manifest the --target-image was installed with (default: manifest.json beside it)")
    hardware = parser.add_argument_group(
        "machine shape", "Override the scenario's own hardware; an option left out keeps it (docs/TESTING.md)")
    hardware.add_argument("--firmware", choices=("uefi", "bios"), help="Boot with UEFI (OVMF) or BIOS (SeaBIOS)")
    hardware.add_argument("--disk-bus", choices=vm_hw.DISK_BUSES, help="How the target disk is attached")
    hardware.add_argument("--no-disk-serial", action="store_true",
                          help="Give the target disk no serial number (virtio only); the install is approved by its by-path identity")
    hardware.add_argument("--sector-size", type=int, choices=vm_hw.SECTOR_SIZES, help="Logical and physical sector size of the disks")
    hardware.add_argument("--removable-disk", action="store_true", help="The USB target disk reports itself removable")
    hardware.add_argument("--nic", choices=vm_hw.NICS, help="Network card model")
    hardware.add_argument("--vga", choices=vm_hw.VGAS, help="Display adapter")
    hardware.add_argument("--cpu", metavar="MODEL", help="QEMU CPU model, for example Haswell, EPYC or host")
    hardware.add_argument("--machine", choices=vm_hw.MACHINES, help="Chipset: q35 (default) or the older i440FX 'pc'")
    hardware.add_argument("--smbios", choices=tuple(vm_hw.SMBIOS_PRESETS),
                          help="DMI identity: a laptop or desktop vendor, or a hypervisor's strings")
    hardware.add_argument("--extra-disk", action="append", type=disk_size_mib, metavar="SIZE",
                          help="Attach another empty raw disk of this size (256M, 2G; repeatable)")
    hardware.add_argument("--secure-boot", action="store_true", help="Use OVMF's Secure Boot build (setup mode unless the variable store enrols keys)")
    hardware.add_argument("--ovmf-vars", choices=vm_hw.OVMF_VARS_MODES,
                          help="persist: both boots share one NVRAM; fresh-on-target: the installed disk boots with an empty one; "
                               "readonly: the firmware cannot save boot entries")
    hardware.add_argument("--ovmf-vars-file", type=Path, help="Seed the NVRAM from this file instead of the firmware package's pristine one")
    hardware.add_argument("--rtc", choices=vm_hw.RTC_MODES, help="Hardware clock in UTC (default) or local time")
    parser.add_argument("--qemu-binary", default="qemu-system-x86_64")
    parser.add_argument("--qemu-img", default="qemu-img")
    parser.add_argument("--ssh-binary", default="ssh")
    parser.add_argument("--ssh-keygen", default="ssh-keygen")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Exit status: 0 all passed, 1 a scenario failed, 2 usage/preflight, 128+N killed by signal N."""
    previous = install_termination_handlers()
    try:
        return _main(argv)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cleanup_stale:
        stopped = cleanup_stale()
        print(f"Stopped {len(stopped)} stale QEMU process(es)" + (": " + ", ".join(map(str, stopped)) if stopped else ""))
        return 0
    if args.list_scenarios:
        print(list_scenarios())
        return 0
    if args.iso is None:
        parser.error("--iso is required")
    if args.cancel_then_resume and args.check_resume:
        parser.error("--cancel-then-resume cannot be combined with --check-resume")
    if args.cancel_then_resume and args.install_mode != "gui":
        parser.error("--cancel-then-resume requires --install-mode gui")
    if args.cancel_then_resume and args.ssh_user != "root":
        parser.error("--cancel-then-resume requires --ssh-user root")
    if args.live_only and (args.cancel_then_resume or args.check_resume or args.rescue_break != "none" or args.rescue_command
                           or args.target_image):
        parser.error("--live-only installs nothing, so it cannot be combined with install or rescue options")
    if args.live_expect is not None and not args.live_only:
        parser.error("--live-expect only applies to --live-only")
    rescuing = args.rescue_break != "none" or args.rescue_command or args.target_image
    if rescuing:
        if args.all:
            parser.error("rescue options need one --scenario, not --all")
        if args.cancel_then_resume or args.check_resume:
            parser.error("rescue options cannot be combined with --cancel-then-resume or --check-resume")
        if args.target_image is None and (args.target_manifest is not None):
            parser.error("--target-manifest only applies to --target-image")
        if SCENARIO_MAP[args.scenario].luks:
            parser.error("rescue runs support unencrypted installs only")
        args.keep = True  # the whole point of a rescue run is the disk it leaves behind
    names = ALL_SCENARIOS if args.all else (args.scenario,)
    scenarios = [apply_overrides(SCENARIO_MAP[name], args) for name in names]
    problems = [problem for scenario in scenarios for problem in
                (*hardware_problems(scenario, args.install_mode), *rescue_problems(scenario, args))]
    if problems:
        parser.error("; ".join(problems))
    try:
        firmware = preflight(args, scenarios)
        public_key = public_key_from_private(args.ssh_keygen, args.ssh_private_key)
        build_manifest(scenarios[0], public_key, args.companions,
                       build_jobs=args.build_jobs, swap_mib=args.swap_mib)
    except HarnessError as exc:
        parser.exit(2, f"test-vm.py: error: {exc}\n")
    failures = 0
    for scenario in scenarios:
        runner = ScenarioRunner(args, scenario, public_key, firmware)
        try:
            print(f"RUN {scenario.name}: logs in {runner.run_dir}", flush=True)
            result = runner.run()
            if not result.get("success"):
                raise HarnessError(f"Scenario result marked unsuccessful: {result.get('error', 'unknown error')}")
            print(f"PASS {scenario.name}: result {runner.run_dir / 'result.json'}", flush=True)
        except Exception as exc:  # a bug in one scenario must not hide the others' results
            failures += 1
            detail = str(exc) if isinstance(exc, (HarnessError, OSError)) else f"{type(exc).__name__}: {exc}"
            print(f"FAIL {scenario.name}: {detail}", file=sys.stderr)
            for log in (runner.run_dir / "install.log", runner.run_dir / "serial-target.log", runner.run_dir / "qemu-live.log"):
                excerpt = tail(log)
                if excerpt:
                    print(f"--- {log} (last lines) ---\n{excerpt}", file=sys.stderr)
            print(f"Artifacts retained at {runner.run_dir}", file=sys.stderr)
        except KeyboardInterrupt as exc:
            signum = getattr(exc, "signum", int(signal.SIGINT))
            print(f"Interrupted (signal {signum}); harness-owned QEMU processes cleaned up. Artifacts: {runner.run_dir}",
                  file=sys.stderr)
            return 128 + signum
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
