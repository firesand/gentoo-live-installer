"""T1 / NOBS-13: unattended LUKS unlock of the installed system."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from gentoo_installer.manifest import InstallManifest
from tests.vm.helpers import PUBLIC_KEY, load_vm_module

vm = load_vm_module()

PROMPTS = {
    "systemd-cryptsetup": "Please enter passphrase for disk root (gentoo-root): (press TAB for no echo)",
    "systemd-short": "Enter passphrase for /dev/vda2:",
    "dracut-70crypt": "Password (/dev/disk/by-uuid/0a1b2c3d-1111-2222-3333-444455556666) [1/3]:",
    "dracut-70crypt-luks-name": "Password (luks-0a1b2c3d) [1/3]:",
}
NOISE = ["Booting a command list", "random: crng init done", "systemd[1]: Reached target Password Entry Dispatch Wait."]


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_prompt_regex_matches_both_unlock_paths(name):
    assert re.search(vm.LUKS_PROMPT_RE, PROMPTS[name], re.IGNORECASE)


@pytest.mark.parametrize("line", NOISE)
def test_prompt_regex_ignores_boot_noise(line):
    assert not re.search(vm.LUKS_PROMPT_RE, line, re.IGNORECASE)


def test_luks_scenarios_get_a_serial_console_and_others_do_not():
    luks = vm.build_manifest(vm.SCENARIO_MAP["uefi-luks"], PUBLIC_KEY)
    assert luks.system.kernel_cmdline_extra == "console=tty0 console=ttyS0,115200n8"
    assert luks.validate() == []
    assert vm.serial_console_configured(luks.system.kernel_cmdline_extra)
    assert vm.build_manifest(vm.SCENARIO_MAP["openrc"], PUBLIC_KEY).system.kernel_cmdline_extra == ""


@pytest.mark.parametrize(("cmdline", "expected"), [
    ("console=tty0 console=ttyS0,115200n8", True),
    ("console=ttyS0", True),
    ("quiet console=ttyS0 loglevel=3", True),
    ("console=tty0", False),
    ("console=ttyS01", False),
    ("", False),
])
def test_serial_console_detection_looks_at_whole_arguments(cmdline, expected):
    assert vm.serial_console_configured(cmdline) is expected


def test_serial_wait_for_ignores_text_before_the_given_offset():
    channel = vm.SerialChannel(Path("/nonexistent"), Path("/nonexistent.log"))
    channel.buffer = "Please enter passphrase for disk root (gentoo-root):"
    first = channel.wait_for(vm.LUKS_PROMPT_RE, 1)
    assert first == len(channel.buffer)
    assert channel.last_match.startswith("passphrase for disk root")
    with pytest.raises(vm.HarnessError):
        channel.wait_for(vm.LUKS_PROMPT_RE, 1, since=first)  # the same prompt must not match twice
    channel.buffer += "\nPassword (/dev/disk/by-uuid/x) [2/3]:"
    assert channel.wait_for(vm.LUKS_PROMPT_RE, 1, since=first) == len(channel.buffer)


class FakeSerial:
    def __init__(self, text=""):
        self.buffer = text
        self.last_match = ""
        self.sent = []

    def wait_for(self, pattern, timeout, since=0):
        match = re.compile(pattern, re.IGNORECASE).search(self.buffer, since)
        if not match:
            raise vm.HarnessError("Timed out waiting for LUKS passphrase prompt")
        self.last_match = match.group(0)
        return match.end()

    def send_line(self, value):
        self.sent.append(value)


def test_unlock_answers_each_new_prompt_until_ssh_is_up():
    serial = FakeSerial("Password (/dev/disk/by-uuid/x) [1/3]:")
    answers = iter([False, True])

    def ssh_ready(_seconds):
        ready = next(answers)
        if not ready:
            serial.buffer += "\nPassword (/dev/disk/by-uuid/x) [2/3]:"  # dracut asks again
        return ready

    receipt = vm.unlock_over_serial(serial, "pw", 30, ssh_ready)
    assert serial.sent == ["pw", "pw"]
    assert receipt["method"] == "serial" and receipt["attempts"] == 2
    assert "[2/3]" in receipt["matched_text"]


def test_unlock_gives_up_after_the_bounded_number_of_attempts():
    serial = FakeSerial("Password (/dev/disk/by-uuid/x) [1/3]:")

    def ssh_ready(_seconds):
        serial.buffer += "\nPassword (/dev/disk/by-uuid/x) [n/3]:"
        return False

    with pytest.raises(vm.HarnessError, match="did not come up after 3"):
        vm.unlock_over_serial(serial, "pw", 30, ssh_ready, attempts=3)
    assert serial.sent == ["pw", "pw", "pw"]


def test_a_slow_boot_after_an_accepted_passphrase_is_not_an_unlock_failure():
    serial = FakeSerial("Please enter passphrase for disk root (gentoo-root):")
    receipt = vm.unlock_over_serial(serial, "pw", 30, lambda _seconds: False, reprompt_wait=1)
    assert serial.sent == ["pw"]  # no second prompt: the caller's SSH wait takes over
    assert receipt["attempts"] == 1


def test_missing_prompt_is_reported_instead_of_typing_blindly():
    serial = FakeSerial("OVMF BdsDxe: loading Boot0008 Gentoo")
    with pytest.raises(vm.HarnessError, match="passphrase prompt"):
        vm.unlock_over_serial(serial, "pw", 1, lambda _seconds: True)
    assert serial.sent == []


# --- keystroke fallback ------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_screen_watcher_waits_for_changes_then_a_stable_screen():
    clock = Clock()
    watcher = vm.ScreenWatcher(min_changes=3, stable_seconds=8, min_elapsed=20, clock=clock)
    for index in range(3):  # firmware, GRUB, kernel messages
        clock.now += 2
        assert watcher.observe(f"screen-{index}") is False
    for _ in range(3):
        clock.now += 2
        assert watcher.observe("screen-2") is False  # stable, but for less than 8 s and under 20 s in
    clock.now += 8
    assert watcher.observe("screen-2") is True
    watcher.rearm()
    assert watcher.observe("screen-2") is False  # a try needs the screen to sit still again
    clock.now += 8
    assert watcher.observe("screen-2") is True


def test_screen_watcher_never_fires_on_a_screen_that_keeps_repainting():
    clock = Clock()
    watcher = vm.ScreenWatcher(clock=clock)
    for index in range(100):  # the GRUB countdown repaints every second
        clock.now += 1
        assert watcher.observe(f"tick-{index}") is False


class FakeGuest:
    def __init__(self, frames):
        self.frames = list(frames)
        self.typed = 0
        self.dumps = 0

    def screendump(self, output):
        frame = self.frames[min(self.dumps, len(self.frames) - 1)]
        self.dumps += 1
        Path(output).write_bytes(frame.encode())
        return output

    def type_test_luks_passphrase(self):
        self.typed += 1


def test_keystroke_unlock_types_only_after_the_screen_settles(tmp_path):
    clock = Clock()
    frames = ["ovmf", "grub-5", "grub-4", "kernel-1", "kernel-2"] + ["prompt"] * 40
    guest = FakeGuest(frames)
    receipt = vm.unlock_over_qmp(guest, "pw", 300, lambda _s: True, tmp_path,
                                 sleep=clock.sleep, clock=clock)
    assert receipt == {"method": "qmp", "attempts": 1}
    assert guest.typed == 1
    assert (tmp_path / "luks-prompt-observation.ppm").read_bytes() == b"prompt"
    assert guest.dumps >= 10  # it kept watching instead of typing at the GRUB menu


def test_keystroke_unlock_gives_up_when_the_screen_never_settles(tmp_path):
    clock = Clock()
    guest = FakeGuest([f"tick-{n}" for n in range(1000)])
    with pytest.raises(vm.HarnessError, match="never settled"):
        vm.unlock_over_qmp(guest, "pw", 60, lambda _s: True, tmp_path, sleep=clock.sleep, clock=clock)
    assert guest.typed == 0


def test_keystroke_unlock_retries_a_bounded_number_of_times(tmp_path):
    clock = Clock()
    guest = FakeGuest(["a", "b", "c"] + ["prompt"] * 500)
    with pytest.raises(vm.HarnessError, match="did not come up after 2"):
        vm.unlock_over_qmp(guest, "pw", 10_000, lambda _s: False, tmp_path, attempts=2,
                           sleep=clock.sleep, clock=clock)
    assert guest.typed == 2


# --- the runner's choice of method ------------------------------------------------------------

def luks_runner(tmp_path, method, cmdline):
    runner = object.__new__(vm.ScenarioRunner)
    runner.args = SimpleNamespace(luks_unlock=method, luks_timeout=5, boot_timeout=10)
    runner.run_dir = tmp_path
    manifest = vm.build_manifest(vm.SCENARIO_MAP["uefi-luks"], PUBLIC_KEY)
    manifest.system.kernel_cmdline_extra = cmdline
    manifest.save(tmp_path / "manifest.json", redact=False)
    runner.wait_for_ssh = lambda *args, **kwargs: None
    return runner


class FakeTarget:
    process = None

    def __init__(self, tmp_path):
        self.serial = FakeSerial("Password (/dev/disk/by-uuid/x) [1/3]:")
        self.serial_log = tmp_path / "serial-target.log"
        self.serial_log.write_text("boot log\n")
        self.typed = 0

    def screendump(self, output):
        Path(output).write_bytes(b"x")
        return output

    def type_test_luks_passphrase(self):
        self.typed += 1


def test_serial_method_answers_on_serial_and_writes_a_receipt(tmp_path):
    runner = luks_runner(tmp_path, "serial", "console=tty0 console=ttyS0,115200n8")
    target = FakeTarget(tmp_path)
    receipt = runner.unlock_luks(target)
    assert target.serial.sent == [vm.TEST_LUKS_PASSPHRASE]
    assert receipt["method"] == "serial" and receipt["requested"] == "serial"
    saved = vm.json.loads((tmp_path / "luks-unlock-receipt.json").read_text())
    assert saved["serial_log_sha256"] == vm.hashlib.sha256(b"boot log\n").hexdigest()
    assert "Password (" in saved["matched_text"] and saved["timestamp"]


def test_serial_request_without_a_serial_console_falls_back_to_keystrokes(tmp_path, monkeypatch, capsys):
    runner = luks_runner(tmp_path, "serial", "")
    target = FakeTarget(tmp_path)
    calls = []
    monkeypatch.setattr(vm, "unlock_over_qmp", lambda *args, **kwargs: calls.append(args) or {"method": "qmp", "attempts": 1})
    receipt = runner.unlock_luks(target)
    assert calls and target.serial.sent == []
    assert receipt["method"] == "qmp" and receipt["requested"] == "serial"
    assert "no console=ttyS0" in receipt["note"]
    assert "typing the passphrase through QMP" in capsys.readouterr().out


def test_external_method_leaves_the_unlock_to_the_operator(tmp_path, capsys):
    runner = luks_runner(tmp_path, "external", "")
    target = FakeTarget(tmp_path)
    receipt = runner.unlock_luks(target)
    assert receipt["method"] == "external" and target.serial.sent == [] and target.typed == 0
    assert (tmp_path / "luks-unlock-method.txt").is_file()


def test_luks_unlock_choices(tmp_path):
    base = ["--scenario", "uefi-luks", "--iso", str(tmp_path / "x.iso")]
    assert vm.build_parser().parse_args(base).luks_unlock == "serial"
    assert vm.build_parser().parse_args(base + ["--luks-unlock", "qmp"]).luks_unlock == "qmp"
