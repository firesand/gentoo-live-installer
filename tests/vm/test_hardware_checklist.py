"""scripts/hardware-checklist.py (H05): the real-hardware checklist is read-only, judged here on made-up machines. The script itself is never run for real."""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hardware-checklist.py"
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("hardware_checklist", SCRIPT)
checklist = importlib.util.module_from_spec(spec)
sys.modules["hardware_checklist"] = checklist
spec.loader.exec_module(checklist)


class FakeMachine:
    """A PC as the checklist sees it: command answers, files, globs, environment. Anything it was not told about is missing."""

    def __init__(self, commands=None, files=None, environ=None):
        self.commands, self.files, self.env = dict(commands or {}), dict(files or {}), dict(environ or {})
        self.asked: list[tuple[str, ...]] = []

    def run(self, argv):
        argv = tuple(argv)
        assert checklist.is_read_only(argv), f"the checklist asked for a command that is not read-only: {argv}"
        self.asked.append(argv)
        return self.commands.get(argv, (127, ""))

    def read(self, path):
        assert path.startswith(checklist.READ_ONLY_PATH_PREFIXES), path
        return self.files.get(path)

    def glob(self, pattern):
        import fnmatch
        assert pattern.startswith(checklist.READ_ONLY_PATH_PREFIXES), pattern
        return sorted(name for name in [*self.files, *self.env.get("globs", [])] if fnmatch.fnmatch(name, pattern))

    def environ(self, name):
        return self.env.get(name, "")


SMI = ("nvidia-smi", "--query-gpu=name,driver_version,memory.total,persistence_mode", "--format=csv,noheader")
LOGINCTL = ("loginctl", "show-session", "3", *checklist.LOGINCTL_PROPERTIES)
JOURNAL = ("journalctl", "-k", "-b", "-p", "err", "--no-pager", "-q")
LSUSB = "Bus 001 Device 004: ID 1532:0091 Razer USA, Ltd Razer Viper\nBus 001 Device 005: ID 0b05:18f3 ASUSTek Computer, Inc. AURA LED Controller\nBus 003 Device 002: ID 054c:0ce6 Sony Corp. DualSense Wireless Controller\n"
VULKAN = "Devices:\n========\nGPU0:\n\tdeviceName         = NVIDIA GeForce RTX 4090\n\tdriverName         = NVIDIA\n"


def good_pc() -> FakeMachine:
    return FakeMachine(
        commands={
            SMI: (0, "NVIDIA GeForce RTX 4090, 595.104.02, 24564 MiB, Enabled\n"),
            LOGINCTL: (0, "Type=wayland\nDesktop=KDE\nState=active\nRemote=no\n"),
            ("vulkaninfo", "--summary"): (0, VULKAN),
            JOURNAL: (0, ""),
            ("df", "--output=avail", "-B1", "/var/tmp"): (0, "Avail\n" + str(200 * 2**30) + "\n"),
            ("lsusb",): (0, LSUSB),
            ("pgrep", "-x", "openrazer-daemon"): (0, "812\n"),
            ("ss", "-ltn"): (0, "State Recv-Q Send-Q Local Address:Port\nLISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"),
            ("systemctl", "is-active", "sshd"): (4, "unknown\n"),
            ("rc-service", "sshd", "status"): (0, " * status: started\n"),
        },
        files={
            "/sys/module/nvidia_drm/parameters/modeset": "Y\n",
            "/proc/driver/nvidia/version": "NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  595.104.02  Release Build\n",
            "/proc/driver/nvidia/params": "ResmanDebugLevel: 4294967295\nPreserveVideoMemoryAllocations: 1\n",
            "/boot/grub/grub.cfg": "submenu 'Advanced' {\nmenuentry 'Safe graphics' {\n\tlinux /vmlinuz ro nomodeset module_blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm,nouveau\n}\n}\n",
            "/sys/firmware/efi/fw_platform_size": "64\n",
            "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c": "\x06\x00\x00\x00\x00",
            "/sys/kernel/security/lockdown": "[none] integrity confidentiality\n",
            "/proc/sys/kernel/tainted": "4096\n",
            "/sys/bus/hid/drivers/razermouse/0003:1532:0091.0004": "",
            "/usr/lib/udev/rules.d/60-openrgb.rules": "",
            "/dev/input/by-id/usb-Sony_Interactive_Entertainment_DualSense_Wireless_Controller-if03-event-joystick": "",
            "/etc/gentoo-live-installer/install.json": json.dumps({"installer": "0.1.0", "init": "openrc", "bootloader": "grub", "kernel": "gentoo-kernel-bin",
                                                                   "hardware": ["Graphics: NVIDIA GeForce RTX 4090 - proprietary driver 595"]}),
        },
        environ={"XDG_SESSION_ID": "3"},
    )


def by_id(results):
    return {result.id: result for result in results}


def test_a_healthy_pc_passes_every_check_and_asks_only_read_only_questions():
    machine = good_pc()
    results = by_id(checklist.run_checks(machine))
    failing = {key: (r.status, r.detail) for key, r in results.items() if r.status in (checklist.FAIL, checklist.SKIP)}
    assert failing == {}, failing
    assert results["nvidia-smi"].status == checklist.PASS and "595.104.02" in results["nvidia-smi"].detail
    assert results["wayland-session"].detail.startswith("Type=wayland")
    assert results["safe-graphics"].status == checklist.PASS and "Safe graphics" in results["safe-graphics"].detail
    assert results["secure-boot"].status == checklist.RECORD and "SecureBoot disabled" in results["secure-boot"].detail and "lockdown none" in results["secure-boot"].detail
    assert results["install-receipt"].status == checklist.RECORD and "RTX 4090" in results["install-receipt"].detail
    assert results["suspend-resume"].status == results["fixture-recapture"].status == checklist.MANUAL and results["suspend-resume"].steps
    assert all(checklist.is_read_only(argv) for argv in machine.asked) and machine.asked


@pytest.mark.parametrize(("change", "check", "fragment"), [
    (lambda m: m.commands.update({SMI: (0, "NVIDIA GeForce RTX 3060, 595.104.02, 12288 MiB, Enabled\n")}), "nvidia-smi", "expected an RTX 4090"),
    (lambda m: m.commands.update({SMI: (0, "NVIDIA GeForce RTX 4090, 580.178.04, 24564 MiB, Enabled\n")}), "nvidia-smi", "not branch 595"),
    (lambda m: m.commands.update({SMI: (9, "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.")}), "nvidia-smi", "failed"),
    (lambda m: m.files.update({"/sys/module/nvidia_drm/parameters/modeset": "N\n"}), "nvidia-drm-modeset", "modeset=N"),
    (lambda m: m.files.pop("/sys/module/nvidia_drm/parameters/modeset"), "nvidia-drm-modeset", "not loaded"),
    (lambda m: m.files.update({"/proc/driver/nvidia/version": "NVRM version: NVIDIA UNIX x86_64 Kernel Module  595.104.02\n"}), "nvidia-open-modules", "NVRM"),
    (lambda m: m.commands.update({LOGINCTL: (0, "Type=x11\nDesktop=KDE\nState=active\n")}), "wayland-session", "Type=x11"),
    (lambda m: m.commands.update({("vulkaninfo", "--summary"): (0, "deviceName = llvmpipe (LLVM 19.1, 256 bits)\n")}), "vulkan", "llvmpipe"),
    (lambda m: m.commands.update({JOURNAL: (0, "kernel: NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.\n")}), "nvidia-kernel-log", "Xid"),
    (lambda m: m.files.update({"/proc/driver/nvidia/params": "PreserveVideoMemoryAllocations: 0\n"}), "suspend-preparation", "PreserveVideoMemoryAllocations"),
    (lambda m: m.commands.update({("df", "--output=avail", "-B1", "/var/tmp"): (0, "Avail\n" + str(8 * 2**30) + "\n")}), "suspend-preparation", "8 GiB free"),
    (lambda m: m.files.update({"/boot/grub/grub.cfg": "menuentry 'Gentoo' {\n\tlinux /vmlinuz ro\n}\n"}), "safe-graphics", "no entry called Safe graphics"),
    (lambda m: m.files.pop("/sys/bus/hid/drivers/razermouse/0003:1532:0091.0004"), "openrazer-viper", "razermouse kernel driver"),
    (lambda m: m.commands.update({("pgrep", "-x", "openrazer-daemon"): (1, "")}), "openrazer-viper", "daemon is not running"),
    (lambda m: m.files.pop("/usr/lib/udev/rules.d/60-openrgb.rules"), "openrgb-aura", "udev rules are missing"),
    (lambda m: m.files.pop("/dev/input/by-id/usb-Sony_Interactive_Entertainment_DualSense_Wireless_Controller-if03-event-joystick"), "dualsense", "no input node"),
    (lambda m: m.commands.update({("ss", "-ltn"): (0, "State Recv-Q\nLISTEN 0 128 0.0.0.0:631\n")}), "ssh", "nothing listens on port 22"),
    (lambda m: m.commands.update({("rc-service", "sshd", "status"): (3, " * status: stopped\n")}), "ssh", "sshd is not running"),
    (lambda m: m.files.update({"/etc/gentoo-live-installer/install.json": "{broken"}), "install-receipt", "not valid JSON"),
])
def test_each_broken_promise_is_a_named_failure(change, check, fragment):
    machine = good_pc()
    change(machine)
    result = by_id(checklist.run_checks(machine))[check]
    assert result.status in (checklist.FAIL, checklist.SKIP) and fragment in result.detail
    if check != "nvidia-kernel-log":
        assert result.status == checklist.FAIL


def test_a_missing_device_or_tool_is_skipped_not_failed():
    machine = good_pc()
    machine.commands[("lsusb",)] = (0, "Bus 001 Device 002: ID 8087:0033 Intel Corp.\n")
    machine.commands.pop(("vulkaninfo", "--summary"))
    machine.commands[JOURNAL] = (1, "Hint: You are currently not seeing messages from other users and the system.")
    machine.env.pop("XDG_SESSION_ID")
    machine.files.pop("/etc/gentoo-live-installer/install.json")
    results = by_id(checklist.run_checks(machine))
    for key in ("openrazer-viper", "openrgb-aura", "dualsense", "vulkan", "nvidia-kernel-log", "wayland-session", "install-receipt"):
        assert results[key].status == checklist.SKIP, (key, results[key])
    assert checklist.main([], machine, out=lambda text: None) == 0, "skips are not failures"


def test_the_exit_status_is_1_when_a_check_fails_and_the_formats_render(capsys):
    machine = good_pc()
    machine.files["/sys/module/nvidia_drm/parameters/modeset"] = "N\n"
    lines: list[str] = []
    assert checklist.main([], machine, out=lines.append) == 1
    assert "[FAIL  ] nvidia_drm modeset=Y" in lines[0] and "MANUAL" in lines[0]
    markdown: list[str] = []
    checklist.main(["--markdown"], good_pc(), out=markdown.append)
    assert markdown[0].startswith("| Check | Result | Detail |") and "| PASS |" in markdown[0]
    payload: list[str] = []
    checklist.main(["--json"], good_pc(), out=payload.append)
    assert {item["id"] for item in json.loads(payload[0])} >= {"nvidia-smi", "suspend-resume", "fixture-recapture"}


def test_a_check_that_crashes_does_not_hide_the_others():
    def broken(machine):
        raise RuntimeError("boom")

    results = checklist.run_checks(good_pc(), (broken, checklist.check_modeset))
    assert [r.status for r in results] == [checklist.SKIP, checklist.PASS] and "boom" in results[0].detail


# --- the safety rules ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ("efibootmgr",), ("lsusb",), ("ss", "-ltn"), ("pgrep", "-x", "openrazer-daemon"), ("systemctl", "is-active", "sshd"), ("rc-service", "sshd", "status"),
    ("df", "--output=avail", "-B1", "/var/tmp"), ("vulkaninfo", "--summary"), JOURNAL, SMI, LOGINCTL,
])
def test_the_exact_read_only_forms_are_allowed(argv):
    assert checklist.is_read_only(argv)


@pytest.mark.parametrize("argv", [
    (), ("sudo", "nvidia-smi"), ("efibootmgr", "-B", "-b", "0001"), ("efibootmgr", "-n", "0002"), ("systemctl", "restart", "sshd"), ("systemctl", "is-active", "sshd; reboot"),
    ("rc-service", "sshd", "stop"), ("rc-service", "sshd", "restart"), ("pgrep", "-x", "foo bar"), ("pgrep", "-f", "x"), ("nvidia-smi", "-pm", "1"),
    ("nvidia-smi", "-pl", "300"), ("loginctl", "terminate-session", "3"), ("loginctl", "show-session", "3"), ("journalctl", "--vacuum-size=1M"),
    ("lsusb", "-v"), ("df", "--output=avail", "-B1", "relative"), ("openrgb", "--list-devices"), ("rm", "-rf", "/"), ("sh", "-c", "true"), ("bash",),
])
def test_everything_else_is_refused(argv):
    assert not checklist.is_read_only(argv)


def test_the_real_machine_refuses_to_run_or_read_what_is_not_read_only():
    machine = checklist.SystemMachine()
    for argv in (("efibootmgr", "-B"), ("rm", "-rf", "/tmp/x"), ("systemctl", "stop", "sshd")):
        with pytest.raises(ValueError, match="not a read-only command"):
            machine.run(argv)
    for path in ("/etc/shadow", "/home/edo/.ssh/id_ed25519", "/root/x", "/var/log/messages"):
        with pytest.raises(ValueError, match="outside the places"):
            machine.read(path)
    with pytest.raises(ValueError):
        machine.glob("/home/*")


def test_the_source_has_no_shell_no_sudo_no_writes_and_one_subprocess_call():
    source = SCRIPT.read_text()
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    names = []
    for call in calls:
        function = call.func
        names.append(ast.unparse(function))
        for keyword in call.keywords:
            assert not (keyword.arg == "shell" and getattr(keyword.value, "value", False)), "shell=True"
    assert names.count("subprocess.run") == 1, "all commands go through SystemMachine.run, which checks them first"
    for forbidden in ("os.system", "os.popen", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "shutil.rmtree", "os.remove",
                      "os.unlink", "os.rename", "os.chmod", "os.write", "Path.write_text", "Path.write_bytes", "os.makedirs", "os.mkdir"):
        assert forbidden not in names, forbidden
    opens = [call for call in calls if ast.unparse(call.func) == "open"]
    assert not opens, "files are read through Path.read_text in SystemMachine.read only"
    assert not [name for name in names if name.endswith((".write_text", ".write_bytes", ".touch", ".unlink", ".mkdir"))]
    program_names = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    for word in ("sudo", "pkexec", "doas", "su"):
        assert word not in program_names, f"{word} appears as a string"


def test_every_command_a_check_asks_for_passes_the_rules_even_on_a_machine_where_everything_fails():
    machine = FakeMachine()
    results = checklist.run_checks(machine)
    assert all(argv and checklist.is_read_only(argv) for argv in machine.asked)
    assert not any("the check itself failed" in r.detail for r in results), [r for r in results if "the check itself failed" in r.detail]


def test_the_manual_steps_name_their_commands():
    results = by_id(checklist.run_checks(good_pc()))
    text = " ".join(results["suspend-resume"].steps + results["fixture-recapture"].steps)
    assert "systemctl suspend" in text and "nvidia-smi" in text and "capture-hardware-fixture.py" in text
    assert "openrgb --list-devices" in " ".join(results["openrgb-aura"].steps), "OpenRGB scans the SMBus: left to the user"
