"""A complete fake lab for the harness: fake qemu, qemu-img, ssh and ssh-keygen plus a guest rule table.

Nothing here starts a real VM or connects anywhere. ``main()``/``ScenarioRunner`` run their
real orchestration (processes, sockets, files, timeouts) against scripts from ``tests/vm/fakes``.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import stat
import sys
import types
from pathlib import Path

import vm_probes
from gentoo_installer.layout import luks_mapper_name
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


WORLD_CLEAN = "\nCalculating dependencies... done!\n\nNothing to merge; quitting.\n"


def scenario_manifest(vm, scenario_name: str, firmware: str | None = None):
    """The manifest the harness builds for the scenario (on the other firmware when ``firmware`` says so)."""
    import dataclasses

    scenario = vm.SCENARIO_MAP[scenario_name]
    if firmware and firmware != scenario.firmware:
        scenario = dataclasses.replace(scenario, firmware=firmware)
    return vm.build_manifest(scenario, PUBLIC_KEY)


def receipt_for(vm, scenario_name: str, firmware: str | None = None) -> str:
    """The install receipt the product itself writes (Configurer.build_receipt) for the scenario's manifest."""
    from gentoo_installerd.configure import Configurer

    manifest = scenario_manifest(vm, scenario_name, firmware)
    configurer = Configurer(types.SimpleNamespace(run=lambda *args, **options: None), "/nonexistent", dry_run=True)
    receipt = configurer.build_receipt(
        manifest, root_uuid="0a1b2c3d-1111-2222-3333-444455556666", luks_uuid="feedface-0000-1111-2222-333344445555" if manifest.storage.luks else "",
        root_filesystem=manifest.storage.filesystem if manifest.storage.mode == "erase" else "ext4", boot_separate=bool(manifest.storage.luks))
    return json.dumps(receipt, indent=2) + "\n"


OPENRC_SSHD = " * status: started\n---\n            sshd |      default\n"
SYSTEMD_SSHD = "active\n---\nenabled\n"
NETWORK_STATUS_ANSWER = json.dumps({"events": [{"type": "result", "version": 1, "data": {
    "online": True, "mirror": True, "default_route": True, "dns": True, "wifi_present": False, "wifi_enabled": False, "rfkill_hard": False,
    "devices": [{"name": "ens3", "kind": "ethernet", "state": "connected", "driver": "virtio_net", "connection": "Wired connection 1",
                 "ipv4": ["10.0.2.15/24"], "ipv6": []}], "problem": "", "message": ""}}]}) + "\n"


def sshd_t(manifest) -> str:
    """What ``sshd -T`` prints on a machine installed from this manifest (Gentoo's PAM drop-in turns passwords into keyboard-interactive)."""
    password = bool(manifest.accounts.ssh_password_login)
    return ("port 22\nusepam yes\npermitrootlogin no\npubkeyauthentication yes\npasswordauthentication no\n"
            f"kbdinteractiveauthentication {'yes' if password else 'no'}\npermitemptypasswords no\n")


GUEST_TOOLS = ("app-emulation/qemu-guest-agent", "app-emulation/spice-vdagent", "app-emulation/open-vm-tools", "app-emulation/virtualbox-guest-additions")


def hardware_plan_for(vm, scenario_name: str, firmware: str | None = None) -> dict:
    """The hardware plan of the scenario's manifest on a QEMU guest (the product's own planner)."""
    from gentoo_installerd import hwplan
    from tests.hardware.helpers import fixture_dict

    return hwplan.plan(scenario_manifest(vm, scenario_name, firmware), fixture_dict("qemu-q35-virtio")).to_dict()


def plan_record(plan: dict) -> dict:
    """The file the daemon keeps in its state directory (Installer._save_plan): the plan inside a record bound to the manifest."""
    return {"version": 1, "fingerprint": "63308222627f71c26c7e4297df94f4a403047b31bdafd17ca9bcd8034fc04757",
            "created": "2026-10-24T11:09:05.376Z", "plan": plan}


def boot_flag_report(vm, scenario_name: str, firmware: str | None = None, *, flagged: bool = True) -> dict:
    """What the boot-flag probe's program prints in a guest whose disk is (or is not) flagged: the first sector is one parted and sgdisk really wrote."""
    gpt = scenario_manifest(vm, scenario_name, firmware).storage.table == "gpt"
    image = ("gpt-pmbr-boot" if flagged else "gpt-no-flag") if gpt else ("dos-root-active" if flagged else "dos-no-flag")
    partition = 3 if gpt else 2  # BIOS boot, swap, root on GPT; swap, root on DOS
    sector = (Path(__file__).resolve().parent / "data" / f"mbr-{image}.bin").read_bytes()
    return {"holder": "/", "source": f"/dev/vda{partition}", "partition": partition, "disk": "/dev/vda", **vm_probes.decode_mbr(sector)}


def package_database(plan: dict) -> str:
    """What the package check prints on a machine where every package of the plan was installed (and nothing else)."""
    wanted = {atom for field in ("early_atoms", "driver_atoms", "required_atoms", "optional_atoms") for atom in plan.get(field, [])}
    return "".join(f"{'installed' if atom in wanted else 'absent'} {atom}\n" for atom in ("sys-firmware/intel-microcode", *GUEST_TOOLS))


def layout_output(vm, scenario_name: str) -> str:
    """What findmnt and swapon print on a machine laid out exactly as the scenario's manual manifest says."""
    manifest = vm.build_manifest(vm.SCENARIO_MAP[scenario_name], PUBLIC_KEY, data_disks=[("/dev/vdb", "GENTOO-VM-EXTRA-1")])
    rows = manifest.storage.partitions
    mounts = "".join(f"{row.mountpoint} {row.device}\n" for row in rows if row.mountpoint)
    swaps = "".join(f"{row.device}\n" for row in rows if row.filesystem == "swap")
    return mounts + "---\n" + swaps


def guest_rules(vm, scenario_name: str, *, guard: str = "vm_disk_guard", install: list | None = None,
                companions: bool | str = True, extra: list | None = None, guard_options: dict | bool = True,
                swap_mib: int = 512, firmware: str | None = None, sudo: bool = False, boot_flag: bool = True) -> list[dict]:
    """Rules for a guest that installs and boots successfully, in the order the harness needs them.

    ``companions`` is True (both are installed and answer), "portagehub" (HWMonux is absent) or False (neither answers).
    ``sudo`` gives the test user a working sudo: the probes that need it answer instead of ending up as findings,
    so a ``--strict-probes`` run of a good guest can pass. ``boot_flag`` says whether the BIOS disk carries its boot flag.
    """
    scenario = vm.SCENARIO_MAP[scenario_name]
    plan = hardware_plan_for(vm, scenario_name, firmware)
    preset = vm.vm_hw.SMBIOS_PRESETS[scenario.hardware.smbios]
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
        {"user": "^root$", "match": r"(?:decoy-prepare|decoy-check|partition-manual|disk-hash) '\{", "guest_tool": True},
        {"user": "^root$", "match": r"^cat /var/log/gentoo-installerd\.log$", "stdout": "daemon stderr\n"},
        {"user": "^root$", "match": r"^cat /var/lib/gentoo-installer/hardware-plan\.json$", "stdout": json.dumps(plan_record(plan), indent=2) + "\n"},
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
    if scenario.luks:  # the names configure_initramfs gives a guided root: gentoo-root, or gentoo-crypt when LVM sits inside the container
        cmdline += f" rd.luks.uuid={uuid} rd.luks.name={uuid}={luks_mapper_name(scenario_manifest(vm, scenario_name, firmware).storage)} console=tty0 console=ttyS0,115200n8"
    if scenario.lvm:
        cmdline += " rd.lvm.lv=gentoo/root"
    rules += [
        {"user": "^vmtest$", "match": r"^test -d /sys/firmware/efi", "stdout": scenario.firmware + "\n"},
        {"user": "^vmtest$", "match": r"^cat /proc/cmdline$", "stdout": cmdline + "\n"},
        {"user": "^vmtest$", "match": r"^findmnt -no SOURCE /$",
         "stdout": ("/dev/mapper/gentoo-root" if scenario.luks or scenario.lvm else "/dev/vda2") + "\n"},
        {"user": "^vmtest$", "match": r"^swapon --noheadings --bytes", "stdout": (f"/swapfile file {swap_mib * 2**20}\n" if scenario.luks
                                                                          else f"/dev/vda3 partition {swap_mib * 2**20 - 4096}\n")},
        {"user": "^vmtest$", "match": r"^cat /etc/gentoo-live-installer/install\.json$", "stdout": receipt_for(vm, scenario_name, firmware)},
        {"user": "^vmtest$", "match": r"^cat /sys/class/dmi/id/sys_vendor", "stdout": f"{preset.manufacturer}\n{preset.chassis_type}\n"},
        {"user": "^vmtest$", "match": r"BOOTX64\.EFI$", "stdout": json.dumps({"path": "/efi/EFI/BOOT/BOOTX64.EFI", "size": 120832, "magic": "MZ"}) + "\n"},
        {"user": "^vmtest$", "match": r"emerge --color=n -pvuDN @world$", "stdout": WORLD_CLEAN},
        {"user": "^vmtest$", "match": r"^findmnt -rno TARGET,SOURCE", "stdout": layout_output(vm, scenario_name) if scenario.manual else ""},
        {"user": "^vmtest$", "match": r"^sh -c .* sh sys-firmware/intel-microcode", "stdout": package_database(plan)},
        # Wave 5 (I13): sshd on the installed system, network_status in the live system
        {"user": "^vmtest$", "match": r"^(?:rc-service sshd status|systemctl is-active sshd)", "stdout": SYSTEMD_SSHD if scenario.init == "systemd" else OPENRC_SSHD},
        {"user": "^vmtest$", "match": r"sshd -T$", "stdout": sshd_t(scenario_manifest(vm, scenario_name, firmware))},
        {"user": "^root$", "match": r"network_status", "stdout": NETWORK_STATUS_ANSWER},
    ]
    desktop = scenario_manifest(vm, scenario_name, firmware).system.desktop
    if desktop != "minimal":  # Wave 5 (H04): the display manager, the greeter, a session file, and PipeWire's pactl
        import vm_session

        started = "active\n" if scenario.init == "systemd" else " * status: started\n"
        sessions = f"/usr/share/xsessions:\n{vm_session.DESKTOPS[desktop]['session']}.desktop\n"
        rules += [{"user": "^vmtest$", "match": r"/usr/share/xsessions", "stdout": f"{sessions}\n---\n{started}\n---\nsystemd\nXorg\nsddm\nbash\n"},
                  {"user": "^vmtest$", "match": r"pactl info", "stdout": "Server Name: PulseAudio (on PipeWire 1.6.8)\n"}]
    if companions == "portagehub":  # HWMonux is switched off: its absence is what the probe looks for
        rules.append({"user": "^vmtest$", "match": r"app-misc.*glob.*hwmonux-", "stdout": "absent\n"})
    rules.append({"user": "^vmtest$", "match": r"/var/db/pkg/", "stdout": "plasma-meta\n"})
    guided = scenario_manifest(vm, scenario_name, firmware).storage
    if guided.firmware == "bios" and guided.mode == "erase":
        rules.append({"user": "^vmtest$", "match": vm_probes.BOOT_FLAG_MARK, "stdout": json.dumps(boot_flag_report(vm, scenario_name, firmware, flagged=boot_flag)) + "\n"})
    if sudo:
        locked = scenario_manifest(vm, scenario_name, firmware).accounts.disable_root_login
        rules += [
            {"user": "^vmtest$", "match": r"^sudo -S -p '' true$", "stdout": ""},
            {"user": "^vmtest$", "match": r"^sudo -S -p '' passwd -S root$", "stdout": f"root {'L' if locked else 'P'} 2026-10-24 0 99999 7 -1\n"},
        ]
    if companions:
        if companions != "portagehub":
            rules.append({"user": "^vmtest$", "match": r"^hwmonux --dump$", "stdout": "vm-host\nSensor Value Min Max\n"})
        rules.append({"user": "^vmtest$", "match": r"^portagehub --self-test$", "stdout": "repos: 1\ncatalog: 2\nvdb: 3\n"})
    rules.append({"user": "^vmtest$", "match": "nmcli", "stdout": STATIC_NETWORK + "\n"})
    return rules


PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()


def gui_rules(*, detected: dict | None = None, cancel: bool = False, success: bool = True) -> list[dict]:
    """Rules for the graphical install: the driver's uploads, the live XFCE session, the driver run and its artifacts."""
    out = "/tmp/gentoo-vm-gui-cancel" if cancel else "/tmp/gentoo-vm-gui"
    events = [{"type": "stage", "stage": "storage", "data": {"status": "completed"}},
              {"type": "completed", "data": {"success": success}, "message": "done"},
              {"type": "gui_result", "success": success, "reboot_enabled": success}]
    rules = [
        {"user": "^root$", "match": r"^cat > /run/vm-gui-install\.py$", "stdin_to": "vm-gui-install.py"},
        {"user": "^root$", "match": r"^cat > /run/vm_guard\.py$", "stdin_to": "vm_guard.py"},
        {"user": "^root$", "match": r"^cat > /run/vm-gui-plan\.json$", "stdin_to": "vm-gui-plan.json"},
        {"user": "^root$", "match": r"^chown gentoo:gentoo-installer ", "rc": 0},
        {"user": "^root$", "match": r"xfce4-session", "stdout": json.dumps({"type": "gui_environment", "environment": {
            "DISPLAY": ":0", "XAUTHORITY": "/home/gentoo/.Xauthority"}}) + "\n"},
        {"user": "^root$", "match": r"^runuser -u gentoo -- env ", "events": events},
        {"user": "^root$", "match": rf"^base64 -w0 -- {out}/(?:review|result)\.png$", "stdout": PNG},
        {"user": "^root$", "match": rf"^base64 -w0 -- {out}/gui\.log$", "stdout": base64.b64encode(b"gui log\n").decode()},
        {"user": "^root$", "match": rf"^base64 -w0 -- {out}/detected\.json$",
         "stdout": base64.b64encode(json.dumps(detected or {}).encode()).decode()},
        {"user": "^root$", "match": r"^cat /var/lib/gentoo-installer/install\.log$", "stdout": "daemon log\n"},
    ]
    return rules


class Lab:
    """Fake binaries, a VM root under tmp_path and helpers to run ``vm.main`` against them."""

    def __init__(self, vm, tmp_path: Path, monkeypatch) -> None:
        self.vm = vm
        self.root = tmp_path / "build" / "vm"
        self.bin = tmp_path / "bin"
        self.bin.mkdir(parents=True)
        monkeypatch.setattr(vm, "VM_ROOT", self.root)
        monkeypatch.setattr(vm.ScenarioRunner, "SSH_RETRY_SECONDS", 0.2)
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
