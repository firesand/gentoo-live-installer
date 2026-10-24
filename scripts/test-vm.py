#!/usr/bin/env python3
"""QEMU end-to-end checks using an explicitly provisioned, key-only SSH channel."""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gentoo_installer.manifest import InstallManifest  # noqa: E402
from vm_qmp import TEST_LUKS_PASSPHRASE, screendump as qmp_screendump, type_luks_passphrase as qmp_type_luks_passphrase  # noqa: E402

VM_ROOT = PROJECT_ROOT / "build" / "vm"
TARGET_DEVICE = "/dev/vda"
TARGET_SERIAL = "GENTOO-VM-DISPOSABLE"
TEST_USERNAME = "vmtest"
TEST_PASSWORD = "vmtest-only-password"
TEST_ROOT_PASSWORD = "vmtest-only-root-password"
SCENARIOS = ("openrc", "systemd", "uefi-luks", "bios")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
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


@dataclass(frozen=True)
class Scenario:
    name: str
    init: str
    firmware: str
    luks: bool = False

    @property
    def hostname(self) -> str:
        return f"gentoo-vm-{self.name}"


SCENARIO_MAP = {
    "openrc": Scenario("openrc", "openrc", "uefi"),
    "systemd": Scenario("systemd", "systemd", "uefi"),
    "uefi-luks": Scenario("uefi-luks", "systemd", "uefi", luks=True),
    "bios": Scenario("bios", "openrc", "bios"),
}


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


def companion_selection(manifest: InstallManifest) -> dict[str, bool]:
    """Read companion choices from the exact install manifest used by the VM."""
    return {
        "hwmonux": bool(manifest.options.install_hwmonux),
        "portagehub": bool(manifest.options.install_portagehub),
    }


def build_manifest(scenario: Scenario, ssh_public_key: str, companions: str = "both", *,
                   build_jobs: int = 2, swap_mib: int = 512) -> InstallManifest:
    if not re.fullmatch(r"(?:ssh-(?:ed25519|rsa)|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]+(?: .*)?", ssh_public_key.strip()):
        raise HarnessError("The SSH public key is missing or invalid")
    if companions not in {"both", "portagehub", "none"}:
        raise HarnessError("Companions must be both, portagehub, or none")
    if not 1 <= build_jobs <= 256 or not 0 <= swap_mib <= 131072:
        raise HarnessError("Build jobs must be 1–256 and target swap must be 0–131072 MiB")
    manifest = InstallManifest()
    manifest.storage.device = TARGET_DEVICE
    manifest.storage.device_id = TARGET_SERIAL
    manifest.storage.firmware = scenario.firmware
    manifest.storage.table = "dos" if scenario.firmware == "bios" else "gpt"
    manifest.storage.swap_mib = swap_mib
    manifest.storage.luks = scenario.luks
    manifest.storage.luks_passphrase = TEST_LUKS_PASSPHRASE if scenario.luks else ""
    manifest.system.init = scenario.init
    manifest.system.stage_variant = f"amd64-{scenario.init}"
    manifest.system.profile = "default/linux/amd64/23.0" + ("/systemd" if scenario.init == "systemd" else "")
    manifest.system.desktop = "minimal"
    manifest.system.profile = "default/linux/amd64/23.0" + ("/systemd" if scenario.init == "systemd" else "")
    manifest.system.kernel = "gentoo-kernel-bin"
    manifest.system.initramfs = "dracut"
    manifest.system.bootloader = "grub"
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


def ovmf_candidates() -> list[tuple[Path, Path]]:
    roots = (
        Path("/usr/share/OVMF"),
        Path("/usr/share/edk2/ovmf"),
        Path("/usr/share/edk2/OvmfX64"),
        Path("/usr/share/edk2/x64"),
        Path("/usr/share/qemu"),
    )
    pairs: list[tuple[Path, Path]] = []
    for root in roots:
        for code_name, vars_name in (
            ("OVMF_CODE.fd", "OVMF_VARS.fd"),
            ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"),
            ("OVMF_CODE.4m.fd", "OVMF_VARS.4m.fd"),
            ("edk2-x86_64-code.fd", "edk2-i386-vars.fd"),
            ("ovmf-x86_64-code.bin", "ovmf-x86_64-vars.bin"),
        ):
            pairs.append((root / code_name, root / vars_name))
    return pairs


def detect_ovmf(candidates: Sequence[tuple[Path, Path]] | None = None) -> OvmfFirmware | None:
    for code, variables in candidates if candidates is not None else ovmf_candidates():
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


def firmware_arguments(scenario: Scenario, firmware: OvmfFirmware | None, run_dir: Path) -> list[str]:
    if scenario.firmware == "bios":
        return []
    if firmware is None:
        raise HarnessError("This scenario requires OVMF; install OVMF_CODE.fd and OVMF_VARS.fd")
    require_under_vm_root(run_dir)
    variables = run_dir / "OVMF_VARS.fd"
    if variables.is_symlink():
        raise HarnessError("Refusing symlink firmware variables")
    if not variables.exists():
        shutil.copyfile(firmware.variables, variables)
    return [
        "-drive", f"if=pflash,format=raw,readonly=on,file={firmware.code}",
        "-drive", f"if=pflash,format=raw,file={variables}",
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
) -> list[str]:
    validate_command_paths(iso, disk, run_dir)
    validate_loopback(ssh_host)
    if not 1 <= ssh_port <= 65535 or memory_mib < 512 or not 1 <= cpus <= 64:
        raise HarnessError("Invalid SSH port, memory size, or CPU count")
    args = [
        qemu_binary,
        "-name", f"gentoo-installer-{scenario.name}",
        "-machine", "q35,accel=kvm:tcg",
        "-m", str(memory_mib),
        "-smp", str(cpus),
        "-drive", f"file={disk},if=none,id=target,format=qcow2",
        "-device", f"virtio-blk-pci,drive=target,serial={TARGET_SERIAL}",
    ]
    if live:
        args.extend(("-drive", f"file={iso},media=cdrom,readonly=on,format=raw"))
    args.extend(firmware_arguments(scenario, firmware, run_dir))
    args.extend([
        "-boot", "order=d,strict=on" if live else "order=c,strict=on",
        "-netdev", f"user,id=net0,hostfwd=tcp:{ssh_host}:{ssh_port}-:22",
        "-device", "virtio-net-pci,netdev=net0",
        "-device", "virtio-rng-pci",
        "-vga", "virtio",
        "-display", "none",
        "-monitor", "none",
        "-chardev", f"socket,id=serial0,path={run_dir / 'serial.sock'},server=on,wait=off",
        "-serial", "chardev:serial0",
        "-qmp", f"unix:{run_dir / 'qmp.sock'},server=on,wait=off",
        "-no-reboot",
    ])
    return args


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
        f"{user}@{args.ssh_host}",
    ]


def ssh_command(args: argparse.Namespace, known_hosts: Path, user: str, remote: str) -> list[str]:
    return [*ssh_base_arguments(args, known_hosts, user), "--", remote]


def parse_install_result(returncode: int, output: str) -> InstallResult:
    events: list[dict] = []
    errors: list[str] = []
    completed: dict | None = None
    for line in ANSI_RE.sub("", output).splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        if event.get("type") == "error":
            errors.append(str(event.get("error") or event.get("message") or "Unknown backend error"))
        if event.get("type") == "completed":
            completed = event
    if completed is None:
        return InstallResult(False, "Backend output has no completed event" + (f" (exit {returncode})" if returncode else ""), events)
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
    except (ValueError, base64.binascii.Error) as exc:
        raise HarnessError(f"GUI artifact {name} was not valid base64") from exc
    if not data:
        raise HarnessError(f"GUI artifact {name} was empty")
    if name.endswith(".png") and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HarnessError(f"GUI artifact {name} was not a PNG image")
    return data


def parse_lsblk_snapshot(output: str) -> dict[str, dict[str, str]]:
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
        if isinstance(path, str) and path.startswith("/dev/vda"):
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
    disk = flattened.get("/dev/vda")
    partitions = [row for path, row in flattened.items() if path != "/dev/vda" and row["type"] == "part"]
    if not disk or disk["type"] != "disk" or not partitions:
        raise HarnessError("lsblk did not report /dev/vda and its partition children")
    if any(not row["uuid"] or not row["fstype"] for row in partitions):
        raise HarnessError("A /dev/vda partition had an empty filesystem UUID or type")
    if not disk["pttype"] or not disk["ptuuid"]:
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
    success = (returncode == 0 and storage_completed and cleanup_completed and stage3_started == 1 and len(errors) == 1 and
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
            plan.append((f"disabled-{name}-absent", package_check,
                         lambda code, output, name=name: parse_companion_absent(name, code, output)))
    return plan


def parse_static_network_probe(returncode: int, output: str, expected: dict[str, object]) -> ProbeResult:
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


class SerialChannel:
    def __init__(self, path: Path, log: Path) -> None:
        self.path = path
        self.log = log
        self.socket: socket.socket | None = None
        self.buffer = ""
        self.condition = threading.Condition()
        self.closed = False
        self.thread: threading.Thread | None = None

    def connect(self, timeout: int = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(0.5)
                client.connect(str(self.path))
                self.socket = client
                self.thread = threading.Thread(target=self._reader, daemon=True)
                self.thread.start()
                return
            except OSError:
                if 'client' in locals():
                    client.close()
                time.sleep(0.2)
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

    def wait_for(self, pattern: str, timeout: int) -> None:
        regex = re.compile(pattern, re.IGNORECASE)
        deadline = time.monotonic() + timeout
        with self.condition:
            while not regex.search(self.buffer):
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
        for name in ("serial.sock", "qmp.sock"):
            (self.run_dir / name).unlink(missing_ok=True)
        with self.log.open("ab") as stream:
            stream.write((f"$ {shlex.join(self.argv)}\n").encode())
            stream.flush()
            try:
                self.process = subprocess.Popen(
                    self.argv, stdin=subprocess.DEVNULL, stdout=stream,
                    stderr=subprocess.STDOUT, start_new_session=True,
                )
            except OSError as exc:
                raise HarnessError(f"Could not start QEMU: {exc}") from exc
        (self.run_dir / f"qemu-{self.label}.pid").write_text(f"{self.process.pid}\n")
        self.serial = SerialChannel(self.run_dir / "serial.sock", self.serial_log)
        self.serial.connect()

    def wait(self, timeout: int) -> int:
        if self.process is None:
            raise HarnessError("QEMU was not started")
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise HarnessError(f"VM did not shut down within {timeout}s") from None

    def qmp(self, command: str) -> None:
        path = self.run_dir / "qmp.sock"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(3)
                client.connect(str(path))
                stream = client.makefile("rwb")
                stream.readline()
                for request in ({"execute": "qmp_capabilities"}, {"execute": command}):
                    stream.write(json.dumps(request).encode() + b"\r\n")
                    stream.flush()
                    stream.readline()
        except OSError:
            pass

    def screendump(self, output: Path) -> Path:
        return qmp_screendump(self.run_dir / "qmp.sock", output)

    def type_test_luks_passphrase(self) -> None:
        qmp_type_luks_passphrase(self.run_dir / "qmp.sock", TEST_LUKS_PASSPHRASE)

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


class ScenarioRunner:
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

    def vm_arguments(self, live: bool) -> list[str]:
        return qemu_command(
            self.scenario, iso=self.iso, disk=self.disk, run_dir=self.run_dir, live=live,
            ssh_host=self.args.ssh_host, ssh_port=self.args.ssh_port,
            memory_mib=self.args.memory, cpus=self.args.cpus,
            qemu_binary=self.args.qemu_binary, firmware=self.firmware,
        )

    def start_vm(self, live: bool) -> VMProcess:
        label = "live" if live else "target"
        vm = VMProcess(self.vm_arguments(live), self.run_dir, label)
        self.vms.append(vm)
        vm.start()
        return vm

    def ssh(self, user: str, remote: str, log_name: str, timeout: int, *, stdin: bytes | None = None) -> tuple[int, Path]:
        known_hosts = self.run_dir / ("known_hosts.live" if user == self.args.ssh_user else "known_hosts.target")
        log = self.run_dir / log_name
        self.logs.append(log)
        code = run_logged(ssh_command(self.args, known_hosts, user, remote), log, timeout, stdin=stdin)
        return code, log

    def wait_for_ssh(self, user: str, phase: str, vm: VMProcess) -> None:
        deadline = time.monotonic() + self.args.boot_timeout
        log = self.run_dir / f"ssh-{phase}-wait.log"
        command = ssh_command(self.args, self.run_dir / f"known_hosts.{phase}", user, "true")
        while time.monotonic() < deadline:
            if vm.process is not None and vm.process.poll() is not None:
                raise HarnessError(f"QEMU exited with status {vm.process.returncode} before SSH became ready; see {vm.log} and {vm.serial_log}")
            code = run_logged(command, log, min(20, self.args.command_timeout))
            if code == 0:
                return
            time.sleep(3)
        raise HarnessError(f"SSH did not become ready within {self.args.boot_timeout}s; see {log}")

    def gui_environment(self) -> dict[str, str]:
        timeout = min(getattr(self.args, "boot_timeout", 900), 120)
        return poll_gui_environment(
            lambda attempt, command_timeout: self.ssh(
                self.args.ssh_user,
                shlex.join(("python3", "-c", GUI_DISPLAY_PROBE)),
                f"gui-environment-attempt-{attempt:02d}.log", command_timeout,
            ),
            timeout=timeout, command_timeout=self.args.command_timeout, run_dir=self.run_dir,
        )

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
        command = "lsblk --json --output PATH,TYPE,UUID,FSTYPE,PARTUUID,PARTTYPE,PTTYPE,PTUUID /dev/vda"
        code, log = self.ssh(self.args.ssh_user, command, f"{name}-lsblk.log", self.args.command_timeout)
        if code != 0:
            raise HarnessError(f"Could not capture {name} storage identity; see {log}")
        snapshot = parse_lsblk_snapshot(log.read_text(errors="replace"))
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
        guard = "from pathlib import Path; import stat; p=Path('/sys/class/block/vda/serial'); p=p if p.exists() else Path('/sys/class/block/vda/device/serial'); assert p.read_text().strip() == 'GENTOO-VM-DISPOSABLE'; assert stat.S_ISBLK(Path('/dev/vda').stat().st_mode)"
        code, log = self.ssh(self.args.ssh_user, shlex.join(("python3", "-c", guard)),
                             "resume-disk-guard.log", self.args.command_timeout)
        if code != 0:
            raise HarnessError(f"Resume disk guard failed; see {log}")
        before = self.capture_storage_snapshot("before-resume")
        remote_manifest = "/run/gentoo-installer-vm-manifest.json"
        command = shlex.join(("gentoo-installerd", "--manifest", remote_manifest, "--yes", "--resume"))
        code, log = self.ssh(self.args.ssh_user, command, "resume.log", self.args.install_timeout)
        output = log.read_text(errors="replace")
        # Capture post-resume identities even when the daemon reports failure.
        after = self.capture_storage_snapshot("after-resume")
        report = parse_resume_regression(code, output, before, after)
        (self.run_dir / "resume-result.json").write_text(json.dumps(report, indent=2) + "\n")
        if not report["success"]:
            raise HarnessError(f"Resume regression failed: {report['error']}; see {log} and {self.run_dir / 'resume-result.json'}")
        return report

    def install(self, *, cancel_at_stage3: bool = False) -> None:
        guard = "from pathlib import Path; import stat; p=Path('/sys/class/block/vda/serial'); p=p if p.exists() else Path('/sys/class/block/vda/device/serial'); assert p.read_text().strip() == 'GENTOO-VM-DISPOSABLE'; assert stat.S_ISBLK(Path('/dev/vda').stat().st_mode)"
        code, log = self.ssh(self.args.ssh_user, shlex.join(("python3", "-c", guard)), "guest-disk-guard.log", self.args.command_timeout)
        if code != 0:
            raise HarnessError(f"Guest does not expose the expected disposable disk; see {log}")
        manifest = build_manifest(
            self.scenario, self.public_key, getattr(self.args, "companions", "both"),
            build_jobs=getattr(self.args, "build_jobs", 2), swap_mib=getattr(self.args, "swap_mib", 512),
        )
        manifest_path = self.run_dir / "manifest.json"
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
        if getattr(self.args, "install_mode", "cli") == "gui":
            driver = PROJECT_ROOT / "scripts/vm-gui-install.py"
            code, log = self.ssh(self.args.ssh_user, "cat > /run/vm-gui-install.py", "gui-upload.log", self.args.command_timeout, stdin=driver.read_bytes())
            if code != 0:
                raise HarnessError(f"Could not upload the GUI driver; see {log}")
            setup = f"chown gentoo:gentoo-installer {remote_manifest} && chmod 600 {remote_manifest} && chmod 644 /run/vm-gui-install.py"
            code, log = self.ssh(self.args.ssh_user, setup, "gui-access.log", self.args.command_timeout)
            if code != 0:
                raise HarnessError(f"Could not prepare the non-root GUI session; see {log}")
            environment = self.gui_environment()
            env_args = [f"{key}={value}" for key, value in environment.items()]
            output_dir = "/tmp/gentoo-vm-gui-cancel" if cancel_at_stage3 else "/tmp/gentoo-vm-gui"
            command = shlex.join(("runuser", "-u", "gentoo", "--", "env", *env_args,
                                  "python3", "/run/vm-gui-install.py", remote_manifest,
                                  "--output", output_dir, *( ("--cancel-at-stage3",) if cancel_at_stage3 else () )))
        else:
            command = shlex.join(("gentoo-installerd", "--manifest", remote_manifest, "--yes"))
        code, log = self.ssh(self.args.ssh_user, command, "install.log", self.args.install_timeout)
        output = log.read_text(errors="replace")
        if cancel_at_stage3:
            cancel_result = parse_cancel_regression(code, output)
            (self.run_dir / "cancel-result.json").write_text(json.dumps(cancel_result, indent=2) + "\n")
            if not cancel_result["success"]:
                raise HarnessError(f"GUI cancellation regression failed: {cancel_result['error']}; see {log} and {self.run_dir / 'cancel-result.json'}")
            result = parse_install_result(code, output)
        else:
            result = parse_install_result(code, output)
            (self.run_dir / "install-result.json").write_text(json.dumps(asdict(result), indent=2) + "\n")
        if getattr(self.args, "install_mode", "cli") == "gui":
            artifacts = ("review.png", "result.png", "gui.log")
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
            validate_install_outcome(
                result, log, gui=getattr(self.args, "install_mode", "cli") == "gui",
            )

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
                "if line.split() and line.split()[0].startswith(('/dev/vda','/dev/mapper/gentoo','/dev/gentoo/'))]; "
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

    def verify(self) -> list[ProbeResult]:
        expected_init = "systemd" if self.scenario.init == "systemd" else "init"
        probes: list[tuple[str, str, Callable[[int, str], ProbeResult]]] = [
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
        static_probe = static_network_probe(manifest)
        if static_probe is not None:
            command, parser = static_probe
            probes.append(("network-static", command, parser))
        results: list[ProbeResult] = []
        for name, command, parser in probes:
            code, log = self.ssh(TEST_USERNAME, command, f"verify-{name}.log", self.args.command_timeout)
            result = parser(code, log.read_text(errors="replace"))
            results.append(result)
        return results

    @staticmethod
    def parse_portage(returncode: int, output: str) -> ProbeResult:
        result = parse_probe_result("portage", returncode, output)
        if result.success and "sys-apps/portage" not in result.output:
            return ProbeResult("portage", False, result.output, "Portage query returned no sys-apps/portage match")
        return result

    def run(self) -> dict:
        result = {"scenario": self.scenario.name, "success": False, "run_dir": str(self.run_dir),
                  "companions": None, "companion_probes": [], "disabled_companion_probes": [],
                  "probes": [], "resume": None, "cancel": None}
        try:
            validate_command_paths(self.iso, self.disk, self.run_dir)
            if self.disk.exists():
                raise HarnessError("Refusing to overwrite an existing virtual disk")
            code = run_logged(
                (self.args.qemu_img, "create", "-f", "qcow2", str(self.disk), f"{self.args.disk_size}G"),
                self.run_dir / "qemu-img.log", self.args.command_timeout,
            )
            if code != 0:
                raise HarnessError(f"qemu-img could not create the disposable disk; see {self.run_dir / 'qemu-img.log'}")
            live = self.start_vm(live=True)
            self.wait_for_ssh(self.args.ssh_user, "live", live)
            if self.args.cancel_then_resume:
                self.install(cancel_at_stage3=True)
                result["cancel"] = self.verify_cancel_cleanup()
                result["resume"] = self.check_resume()
            else:
                self.install()
            saved_manifest = InstallManifest.load(self.run_dir / "manifest.json")
            result["companions"] = companion_selection(saved_manifest)
            if self.args.check_resume:
                result["resume"] = self.check_resume()
            code, log = self.ssh(self.args.ssh_user, "poweroff", "shutdown-live.log", self.args.command_timeout)
            if code not in (0, 255):
                raise HarnessError(f"Could not request live-environment shutdown; see {log}")
            live.wait(self.args.shutdown_timeout)
            live.cleanup()

            target = self.start_vm(live=False)
            if self.scenario.luks:
                if self.args.luks_unlock == "serial":
                    if target.serial is None:
                        raise HarnessError("Serial LUKS unlock requested but QEMU serial channel is unavailable")
                    target.serial.wait_for(r"passphrase.*(?:for|:)|password.*luks", self.args.luks_timeout)
                    target.serial.send_line(TEST_LUKS_PASSPHRASE)
                else:
                    self.prepare_external_luks_unlock(target)
            self.wait_for_ssh(TEST_USERNAME, "target", target)
            probes = self.verify()
            result["probes"] = [asdict(probe) for probe in probes]
            result["companion_probes"] = [asdict(probe) for probe in probes if probe.name in {"hwmonux", "portagehub"}]
            result["disabled_companion_probes"] = [asdict(probe) for probe in probes if probe.name.startswith("disabled-")]
            failures = [f"{probe.name}: {probe.error}" for probe in probes if not probe.success]
            if failures:
                raise HarnessError("Target verification failed: " + "; ".join(failures))
            result["success"] = True
            return result
        except Exception as exc:
            result["error"] = str(exc)
            raise
        finally:
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
            if result["success"] and not self.args.keep:
                for name in ("disk.qcow2", "OVMF_VARS.fd"):
                    (self.run_dir / name).unlink(missing_ok=True)
            (self.run_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")


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
    firmware = detect_ovmf()
    if any(scenario.firmware == "uefi" for scenario in scenarios) and firmware is None:
        raise HarnessError("OVMF was not found; UEFI scenarios cannot run (the bios scenario does not need OVMF)")
    return firmware


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--scenario", choices=SCENARIOS)
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--ssh-host", help="Loopback address of the QEMU SSH forward (normally 127.0.0.1)")
    parser.add_argument("--ssh-port", type=positive_int, help="Unused host TCP port forwarded to guest SSH")
    parser.add_argument("--ssh-private-key", type=Path, help="Ephemeral key already authorized in the test ISO")
    parser.add_argument("--ssh-user", default="root", help="Live ISO SSH user (must be root)")
    parser.add_argument("--disk-size", type=positive_int, default=40, help="Disposable qcow2 size in GiB (default: 40)")
    parser.add_argument("--memory", type=positive_int, default=4096, help="VM memory in MiB (default: 4096)")
    parser.add_argument("--cpus", type=positive_int, default=2)
    parser.add_argument("--build-jobs", type=positive_int, default=2,
                        help="Target Portage build jobs, 1–256 (default: 2)")
    parser.add_argument("--swap-mib", type=int, default=512,
                        help="Swap on the disposable target disk, 0–131072 MiB (default: 512)")
    parser.add_argument("--boot-timeout", type=positive_int, default=900)
    parser.add_argument("--install-timeout", type=positive_int, default=21600)
    parser.add_argument("--shutdown-timeout", type=positive_int, default=120)
    parser.add_argument("--command-timeout", type=positive_int, default=120)
    parser.add_argument("--luks-timeout", type=positive_int, default=180)
    parser.add_argument("--luks-unlock", choices=("serial", "external"), default="serial",
                        help="Unlock a LUKS test VM via serial (default) or operator-controlled VGA/QMP")
    parser.add_argument("--check-resume", action="store_true",
                        help="Rerun the successful install with --resume and verify storage identities remain unchanged")
    parser.add_argument("--cancel-then-resume", action="store_true",
                        help="In GUI mode, cancel at stage3, verify cleanup, then resume on the same guarded disk")
    parser.add_argument("--keep", action="store_true", help="Keep the disk and firmware copy after success (the manifest is always retained)")
    parser.add_argument("--install-mode", choices=("cli", "gui"), default="cli", help="Drive the real backend CLI or non-root graphical installer")
    parser.add_argument("--companions", choices=("both", "portagehub", "none"), default="both",
                        help="Install HWMonux and PortageHub (both), only PortageHub, or neither")
    parser.add_argument("--qemu-binary", default="qemu-system-x86_64")
    parser.add_argument("--qemu-img", default="qemu-img")
    parser.add_argument("--ssh-binary", default="ssh")
    parser.add_argument("--ssh-keygen", default="ssh-keygen")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cancel_then_resume and args.check_resume:
        parser.error("--cancel-then-resume cannot be combined with --check-resume")
    if args.cancel_then_resume and args.install_mode != "gui":
        parser.error("--cancel-then-resume requires --install-mode gui")
    if args.cancel_then_resume and args.ssh_user != "root":
        parser.error("--cancel-then-resume requires --ssh-user root")
    scenarios = list(SCENARIO_MAP.values()) if args.all else [SCENARIO_MAP[args.scenario]]
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
        except (HarnessError, OSError) as exc:
            failures += 1
            print(f"FAIL {scenario.name}: {exc}", file=sys.stderr)
            for log in (runner.run_dir / "install.log", runner.run_dir / "serial-target.log", runner.run_dir / "qemu-live.log"):
                excerpt = tail(log)
                if excerpt:
                    print(f"--- {log} (last lines) ---\n{excerpt}", file=sys.stderr)
            print(f"Artifacts retained at {runner.run_dir}", file=sys.stderr)
        except KeyboardInterrupt:
            print(f"Interrupted; harness-owned QEMU processes cleaned up. Artifacts: {runner.run_dir}", file=sys.stderr)
            return 130
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
