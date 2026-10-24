"""A complete fake lab for the harness: fake qemu, qemu-img, ssh and ssh-keygen plus a guest rule table.

Nothing here starts a real VM or connects anywhere. ``main()``/``ScenarioRunner`` run their
real orchestration (processes, sockets, files, timeouts) against scripts from ``tests/vm/fakes``.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import sys
from pathlib import Path

from tests.vm.helpers import PUBLIC_KEY

FAKES = Path(__file__).resolve().parent / "fakes"
EVENT_STREAM = [
    {"type": "stage", "stage": "storage", "data": {"status": "completed"}},
    {"type": "stage", "stage": "packages", "data": {"status": "completed"}},
    {"type": "completed", "data": {"success": True}, "message": "done"},
]
FAILED_STREAM = [
    {"type": "stage", "stage": "storage", "data": {"status": "completed"}},
    {"type": "error", "error": "emerge failed: simulated"},
    {"type": "completed", "data": {"success": False}, "message": "failed"},
]
RESUME_STREAM = [
    {"type": "stage", "stage": "storage", "data": {"status": "skipped"}},
    {"type": "completed", "data": {"success": True}, "message": "done"},
]


def lsblk_snapshot(disk: str = "/dev/vda") -> str:
    """lsblk --json for an installed disk: a table, an ESP and a root filesystem."""
    sep = "p" if disk[-1].isdigit() else ""
    return json.dumps({"blockdevices": [{"path": disk, "type": "disk", "uuid": None, "fstype": None, "partuuid": None,
                                         "parttype": None, "pttype": "gpt", "ptuuid": "table-id", "children": [
        {"path": f"{disk}{sep}1", "type": "part", "uuid": "AAAA-AAAA", "fstype": "vfat", "partuuid": "p1",
         "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "pttype": None, "ptuuid": None},
        {"path": f"{disk}{sep}2", "type": "part", "uuid": "root-uuid", "fstype": "ext4", "partuuid": "p2",
         "parttype": "0fc63daf-8483-4772-8e79-3d69d8477de4", "pttype": None, "ptuuid": None}]}]})


NETWORK = "2: eth0    inet 10.0.2.15/24 brd 10.0.2.255 scope global eth0\ndefault via 10.0.2.2 dev eth0\n"
STATIC_NETWORK = json.dumps({
    "active": ["Installer wired static:eth0"], "method": "manual", "addresses": ["10.0.2.15/24"],
    "gateway": "10.0.2.2", "dns": ["10.0.2.3"]})


def wrapper(path: Path, script: Path) -> Path:
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def guest_rules(vm, scenario_name: str, *, guard: str = "vm_disk_guard", install: list | None = None,
                companions: bool = True, extra: list | None = None, guard_options: dict | bool = True,
                swap_mib: int = 512) -> list[dict]:
    """Rules for a guest that installs and boots successfully, in the order the harness needs them."""
    scenario = vm.SCENARIO_MAP[scenario_name]
    init = "systemd" if scenario.init == "systemd" else "init"
    target_ready = {"user": "^vmtest$", "match": "^true$"}
    if scenario.luks:
        target_ready["requires"] = "fake-unlocked"
    rules: list[dict] = list(extra or [])
    rules += [
        {"user": "^root$", "match": "^true$", "rc": 0},
        target_ready,
        {"user": "^vmtest$", "match": "^true$", "rc": 255},
        {"user": "^root$", "match": guard, "guard": guard_options},
        {"user": "^root$", "match": r"decoy-(?:prepare|check) '\{", "guest_tool": True},
        {"user": "^root$", "match": r"^cat > /run/gentoo-installer-vm-manifest\.json$", "stdin_to": "manifest.json"},
        {"user": "^root$", "match": r"^gentoo-installerd --manifest \S+ --yes$", "events": install or EVENT_STREAM},
        {"user": "^root$", "match": r"^gentoo-installerd --manifest \S+ --yes --resume$", "events": RESUME_STREAM},
        {"user": "^root$", "match": r"^lsblk --json --output PATH,TYPE,UUID", "stdout": lsblk_snapshot()},
        {"user": "^root$", "match": "^poweroff$", "shutdown_vm": "live"},
        {"user": "^vmtest$", "match": r"^ps -p 1 -o comm=$", "stdout": init + "\n"},
        {"user": "^vmtest$", "match": r"^hostname$", "stdout": scenario.hostname + "\n"},
        {"user": "^vmtest$", "match": r"^id -un$", "stdout": "vmtest\n"},
        {"user": "^vmtest$", "match": r"ip -4 -o address show", "stdout": NETWORK},
        {"user": "^vmtest$", "match": r"^getent ahostsv4", "stdout": "151.101.1.1 STREAM distfiles.gentoo.org\n"},
        {"user": "^vmtest$", "match": r"^emerge --search", "stdout": "* sys-apps/portage\n"},
    ]
    uuid = "0a1b2c3d-1111-2222-3333-444455556666"
    cmdline = f"BOOT_IMAGE=/vmlinuz-6.12 root=UUID={uuid} ro quiet"
    if scenario.luks:
        cmdline += f" rd.luks.uuid={uuid} rd.luks.name={uuid}=gentoo-root console=tty0 console=ttyS0,115200n8"
    rules += [
        {"user": "^vmtest$", "match": r"^test -d /sys/firmware/efi", "stdout": scenario.firmware + "\n"},
        {"user": "^vmtest$", "match": r"^cat /proc/cmdline$", "stdout": cmdline + "\n"},
        {"user": "^vmtest$", "match": r"^findmnt -no SOURCE /$",
         "stdout": ("/dev/mapper/gentoo-root" if scenario.luks else "/dev/vda2") + "\n"},
        {"user": "^vmtest$", "match": r"^swapon ", "stdout": f"/dev/vda3 partition {swap_mib * 2**20 - 4096}\n"},
    ]
    if companions:
        rules += [
            {"user": "^vmtest$", "match": r"^hwmonux --dump$", "stdout": "vm-host\nSensor Value Min Max\n"},
            {"user": "^vmtest$", "match": r"^portagehub --self-test$", "stdout": "repos: 1\ncatalog: 2\nvdb: 3\n"},
        ]
    rules.append({"user": "^vmtest$", "match": "nmcli", "stdout": STATIC_NETWORK + "\n"})
    return rules


class Lab:
    """Fake binaries, a VM root under tmp_path and helpers to run ``vm.main`` against them."""

    def __init__(self, vm, tmp_path: Path, monkeypatch) -> None:
        self.vm = vm
        self.root = tmp_path / "build" / "vm"
        self.bin = tmp_path / "bin"
        self.bin.mkdir(parents=True)
        monkeypatch.setattr(vm, "VM_ROOT", self.root)
        self.iso = tmp_path / "installer.iso"
        self.iso.write_bytes(b"iso")
        self.key = tmp_path / "id_test"
        self.key.write_text("not a real key\n")
        self.qemu = wrapper(self.bin / "fake-qemu", FAKES / "fake_qemu.py")
        self.qemu_img = wrapper(self.bin / "fake-qemu-img", FAKES / "fake_qemu_img.py")
        self.ssh = wrapper(self.bin / "fake-ssh", FAKES / "fake_ssh.py")
        self.ssh_keygen = self.bin / "fake-ssh-keygen"
        self.ssh_keygen.write_text(f'#!/bin/sh\necho "{PUBLIC_KEY}"\n')
        self.ssh_keygen.chmod(0o755)
        self.rules_path = tmp_path / "rules.json"
        self.ssh_log = tmp_path / "ssh.log"
        self.qemu_config_path = tmp_path / "qemu.json"
        self.qemu_img_log = tmp_path / "qemu-img.log"
        self.port = free_port()
        self.qemu_config: dict = {"exit_after": None}
        self.rules: list[dict] = []
        for name, value in (("FAKE_SSH_RULES", self.rules_path), ("FAKE_SSH_LOG", self.ssh_log),
                            ("FAKE_QEMU_CONFIG", self.qemu_config_path), ("FAKE_QEMU_IMG_LOG", self.qemu_img_log)):
            monkeypatch.setenv(name, str(value))
        fake_firmware_dir = tmp_path / "ovmf"
        fake_firmware_dir.mkdir()
        (fake_firmware_dir / "code.fd").write_bytes(b"code")
        (fake_firmware_dir / "vars.fd").write_bytes(b"vars")
        self.firmware = vm.OvmfFirmware(fake_firmware_dir / "code.fd", fake_firmware_dir / "vars.fd")
        monkeypatch.setattr(vm, "detect_ovmf", lambda candidates=None, **_: self.firmware)
        self.write()

    def write(self) -> None:
        self.rules_path.write_text(json.dumps(self.rules))
        self.qemu_config_path.write_text(json.dumps(self.qemu_config))

    def argv(self, *arguments: str) -> list[str]:
        return [
            "--iso", str(self.iso), "--ssh-host", "127.0.0.1", "--ssh-port", str(self.port),
            "--ssh-private-key", str(self.key), "--qemu-binary", str(self.qemu), "--qemu-img", str(self.qemu_img),
            "--ssh-binary", str(self.ssh), "--ssh-keygen", str(self.ssh_keygen),
            "--boot-timeout", "10", "--command-timeout", "20", "--shutdown-timeout", "10",
            "--install-timeout", "60", "--luks-timeout", "10", *arguments,
        ]

    def run(self, *arguments: str) -> int:
        self.write()
        return self.vm.main(self.argv(*arguments))

    def run_dirs(self) -> list[Path]:
        return sorted(path for path in self.root.iterdir() if path.is_dir()) if self.root.exists() else []

    def run_dir(self) -> Path:
        [only] = self.run_dirs()
        return only

    def result(self) -> dict:
        return json.loads((self.run_dir() / "result.json").read_text())

    def ssh_calls(self) -> list[dict]:
        if not self.ssh_log.exists():
            return []
        return [json.loads(line) for line in self.ssh_log.read_text().splitlines()]


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1].split()[0] != "Z"
    except OSError:
        return False
