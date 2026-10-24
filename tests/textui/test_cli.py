"""The command line: options, exit codes, --manifest automation, --dry-run, --json-events and --attach."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import gentoo_installer
from gentoo_installer.manifest import InstallManifest
from gentoo_installer.textui import cli
from gentoo_installer.textui.follow import Outcome

from tests.textui.support import (
    END, FakeHost, FakeInstaller, Script, failing_job, good_job, inspect_reply, run_main, valid_manifest,
)

VDA_SERIAL = {"/dev/vda": "VDA-SERIAL-0001"}


def write_manifest(tmp_path: Path, manifest: InstallManifest | None = None, *, redact: bool = False, name: str = "manifest.json") -> str:
    path = tmp_path / name
    path.write_text((manifest or valid_manifest()).to_json(redact=redact))
    return str(path)


def serial_installer(installer, **kwargs) -> FakeInstaller:
    return installer(inspect_reply("qemu-q35-virtio", serials=VDA_SERIAL), **kwargs)


def requests_of(fake: FakeInstaller, action: str) -> list[dict]:
    return [request for request in fake.requests if request["action"] == action]


# --- the options -------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("argv, message", [
    (["--yes"], "--yes needs --manifest"),
    (["--dry-run"], "--dry-run needs --manifest"),
    (["--manifest", "m.json", "--confirm-device", "/dev/vda"], "--confirm-device only works together with --yes"),
    (["--resume"], "--resume needs --manifest"),
    (["--attach", "--manifest", "m.json"], "--attach cannot be combined"),
    (["--json-events"], "--json-events needs --yes, --dry-run or --attach"),
    (["--manifest", "m.json", "--json-events"], "--json-events needs"),
])
def test_wrong_combinations_of_options_are_usage_errors(host: FakeHost, argv: list[str], message: str) -> None:
    result = run_main(argv, Script(), host)
    assert result.code == 2 and message in result.err


def test_an_unknown_option_is_a_usage_error_and_help_and_version_work(host: FakeHost) -> None:
    assert run_main(["--bogus"], Script(), host).code == 2
    helped = run_main(["--help"], Script(), host)
    assert helped.code == 0 and "--confirm-device" in helped.out and "exit codes" in helped.out and "gentoo-installer-text --attach" in helped.out
    versioned = run_main(["--version"], Script(), host)
    assert versioned.code == 0 and gentoo_installer.__version__ in versioned.out


# --- the settings file ---------------------------------------------------------------------------------------------------------------


def test_a_missing_or_broken_settings_file_is_exit_2(tmp_path: Path, host: FakeHost) -> None:
    missing = run_main(["--manifest", str(tmp_path / "nope.json"), "--dry-run"], Script(), host)
    assert missing.code == 2 and "Cannot read" in missing.out
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json")
    assert "is not valid JSON" in run_main(["--manifest", str(broken), "--dry-run"], Script(), host).out
    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps({"storage": {"colour": "red"}}))
    result = run_main(["--manifest", str(unknown), "--dry-run"], Script(), host)
    assert result.code == 2 and "Unknown manifest.storage fields: colour" in result.out


def test_invalid_settings_are_listed_and_nothing_is_contacted(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    manifest = valid_manifest()
    manifest.storage.device = "sda"
    manifest.accounts.hostname = "bad host"
    result = run_main(["--manifest", write_manifest(tmp_path, manifest), "--yes", "--confirm-device", "sda", "--socket", fake.path], Script(), host)
    assert result.code == 2 and "Select a real target disk" in result.out and "Hostname must contain only" in result.out
    assert fake.requests == []


def test_a_saved_copy_without_passwords_cannot_be_installed_unattended(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    path = write_manifest(tmp_path, redact=True)
    result = run_main(["--manifest", path, "--yes", "--confirm-device", "/dev/vda", "--socket", fake.path], Script(), host)
    assert result.code == 2 and "Replace the redacted secret" in result.out and fake.requests == []


# --- unattended installation --------------------------------------------------------------------------------------------------------------------


def test_yes_without_confirm_device_is_refused_before_anything_is_contacted(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    result = run_main(["--manifest", write_manifest(tmp_path), "--yes", "--socket", fake.path], Script(), host)
    assert result.code == 3 and "--confirm-device /dev/vda" in result.out and fake.requests == []


def test_a_wrong_confirm_device_is_refused(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    result = run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vdb", "--socket", fake.path], Script(), host)
    assert result.code == 3 and "not the disk in the manifest" in result.out and fake.requests == []
    assert run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda1", "--socket", fake.path], Script(), host).code == 3


def test_an_unattended_installation_runs_to_the_end(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    script = Script()
    result = run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--socket", fake.path], script, host)
    assert result.code == 0 and fake.resumes == [False]
    assert fake.manifests[0] == valid_manifest().to_dict()  # exactly what the file said, secrets included
    assert "The installation finished successfully." in script.text and "[ 40%] Installing the base system" in script.text
    assert not host.called("reboot") and not host.calls  # nobody is asked and nothing restarts by itself


def test_the_resume_flag_is_passed_on(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--resume", "--socket", fake.path], Script(), host)
    assert fake.resumes == [True]


def test_a_failed_unattended_installation_is_exit_1_and_says_how_to_continue(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer, jobs=[failing_job()])
    script = Script()
    result = run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--socket", fake.path], script, host)
    assert result.code == 1
    assert "The installation stopped while: Installing the base system." in script.text
    assert "emerge failed: dev-libs/broken-1.2 could not be built" in script.text and "/var/lib/gentoo-installer/install.log" in script.text
    assert "run the same command with --resume" in script.flat


def test_a_manifest_without_a_disk_identity_is_pinned_from_the_inventory(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    manifest = valid_manifest()
    manifest.storage.device_id = ""
    script = Script()
    result = run_main(["--manifest", write_manifest(tmp_path, manifest), "--yes", "--confirm-device", "/dev/vda", "--socket", fake.path], script, host)
    sent = fake.manifests[0]["storage"]
    assert result.code == 0 and (sent["device_id"], sent["device_id_kind"]) == ("VDA-SERIAL-0001", "")
    assert "pinned to its serial number" in script.flat


def test_naming_a_disk_without_serial_on_the_command_line_approves_its_hardware_path(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = installer(inspect_reply("qemu-q35-virtio"))
    manifest = valid_manifest()
    manifest.storage.device_id = ""
    script = Script()
    result = run_main(["--manifest", write_manifest(tmp_path, manifest), "--yes", "--confirm-device", "/dev/vda", "--socket", fake.path], script, host)
    sent = fake.manifests[0]["storage"]
    assert result.code == 0 and sent["device_id_kind"] == "path" and sent["device_id"].startswith("pci-0000:00:03.0|")
    assert "hardware path, size and model" in script.flat


@pytest.mark.parametrize("device, message", [("/dev/nvme9n1", "was not found"), ("/dev/sdb", "installer medium")])
def test_a_disk_that_cannot_be_identified_or_used_stops_the_run_before_it_starts(tmp_path: Path, installer, host: FakeHost, device: str, message: str) -> None:
    fake = serial_installer(installer)
    manifest = valid_manifest(device)
    manifest.storage.device_id = ""
    result = run_main(["--manifest", write_manifest(tmp_path, manifest), "--yes", "--confirm-device", device, "--socket", fake.path], Script(), host)
    assert result.code == 2 and message in result.out and not requests_of(fake, "start")


def test_an_unreachable_service_is_exit_4(tmp_path: Path, sock_dir: Path, host: FakeHost) -> None:
    result = run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--socket", str(sock_dir / "nobody.sock")], Script(), host)
    assert result.code == 4 and "installer service is not running" in result.out and "rc-service gentoo-installerd" in result.out


def test_a_running_installation_is_not_started_over(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    fake.state["status"] = "running"
    result = run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--socket", fake.path], Script(), host)
    assert result.code == 1 and "already running" in result.out and not requests_of(fake, "start")


def test_json_events_are_the_only_thing_on_standard_output(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    script = Script()
    result = run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--json-events", "--socket", fake.path], script, host)
    events = [json.loads(line) for line in result.out.splitlines()]  # every line of standard output is JSON
    assert result.code == 0 and events[0]["type"] == "status" and events[-1]["type"] == "completed" and events[-1]["data"]["success"] is True
    assert [event["type"] for event in events if "seq" in event] == [event["type"] for event in good_job()]
    assert "The installation finished successfully." in result.err and "Installing" not in result.err  # people-text goes to the other stream


def test_json_events_on_failure_end_in_exit_1_with_the_error_event(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer, jobs=[failing_job()])
    result = run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--json-events", "--socket", fake.path], Script(), host)
    events = [json.loads(line) for line in result.out.splitlines()]
    assert result.code == 1 and [event["type"] for event in events][-2:] == ["error", "completed"] and events[-2]["data"]["code"] == "install.failed"


def test_the_exit_codes_of_the_outcomes() -> None:
    assert [cli.exit_code(Outcome(state)) for state in ("success", "failed", "cancelled", "busy", "invalid", "unreachable", "detached")] == [0, 1, 1, 1, 2, 4, 130]


# --- --dry-run --------------------------------------------------------------------------------------------------------------------------------


def test_a_dry_run_shows_the_plan_and_starts_nothing(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    script = Script()
    result = run_main(["--manifest", write_manifest(tmp_path), "--dry-run", "--socket", fake.path], script, host)
    assert result.code == 0 and "Dry run: nothing was started." in script.text
    assert "ERASE  /dev/vda" in script.text and "CREATE  EFI system partition" in script.text and "Chosen for this computer:" in script.text
    assert not requests_of(fake, "start") and "reboot" not in str(host.calls)


def test_a_dry_run_needs_no_confirmation_and_ignores_yes(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    assert run_main(["--manifest", write_manifest(tmp_path), "--dry-run", "--yes", "--socket", fake.path], Script(), host).code == 0


def test_a_dry_run_can_print_json(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    result = run_main(["--manifest", write_manifest(tmp_path), "--dry-run", "--json-events", "--socket", fake.path], Script(), host)
    data = json.loads(result.out)
    assert result.code == 0 and data["type"] == "dry_run" and data["ok"] is True and data["source"] == "daemon" and data["problems"] == []
    assert any(line.startswith("ERASE  /dev/vda") for line in data["storage_plan"]) and data["hardware_plan"]
    assert "secret" not in result.out and "correct-horse" not in result.out


def test_a_dry_run_reports_problems_with_this_computer(tmp_path: Path, installer, host: FakeHost) -> None:
    reply = inspect_reply("qemu-q35-virtio", serials=VDA_SERIAL)
    reply["disks"][-1]["size"] = 8 * 1024**3 if reply["disks"][-1]["path"] == "/dev/vda" else reply["disks"][-1]["size"]
    for disk in reply["disks"]:
        if disk["path"] == "/dev/vda":
            disk["size"] = 8 * 1024**3
    fake = installer(reply)
    script = Script()
    result = run_main(["--manifest", write_manifest(tmp_path), "--dry-run", "--socket", fake.path], script, host)
    assert result.code == 2 and "The disk is too small for this layout" in script.flat and "cannot be installed as it is" in script.flat


def test_a_dry_run_without_the_service_uses_what_can_be_read_locally(tmp_path: Path, sock_dir: Path, host: FakeHost) -> None:
    host.local_reply = inspect_reply("qemu-q35-virtio", serials=VDA_SERIAL)
    script = Script()
    result = run_main(["--manifest", write_manifest(tmp_path), "--dry-run", "--socket", str(sock_dir / "nobody.sock")], script, host)
    assert result.code == 0 and host.called("local_inspection") and "did not answer" in script.flat and "ERASE  /dev/vda" in script.text


def test_a_dry_run_with_nothing_to_inspect_still_checks_the_settings(tmp_path: Path, sock_dir: Path, host: FakeHost) -> None:
    result = run_main(["--manifest", write_manifest(tmp_path), "--dry-run", "--socket", str(sock_dir / "nobody.sock")], Script(), host)
    assert result.code == 0 and "could not be inspected" in result.out


def test_a_dry_run_of_invalid_settings_is_exit_2(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    manifest = valid_manifest()
    manifest.system.locale = "klingon"
    result = run_main(["--manifest", write_manifest(tmp_path, manifest), "--dry-run", "--socket", fake.path], Script(), host)
    assert result.code == 2 and "Invalid locale" in result.out and fake.requests == []


# --- interactive: the service is not there, a job is running ------------------------------------------------------------------------------------------


def test_without_a_service_the_wizard_offers_to_try_again_and_ends_with_exit_4(sock_dir: Path, host: FakeHost) -> None:
    script = Script([("installer service is not running", ""), ("Try again", "q"), ("Quit the installer?", "y")])
    result = run_main(["--socket", str(sock_dir / "nobody.sock")], script, host)
    script.assert_finished()
    assert result.code == 4 and "rc-service gentoo-installerd" in script.text


def test_a_running_installation_is_offered_for_watching_instead_of_the_questions(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    cli_script = Script([("already running", ""), ("What now", "4")])
    # the job ran before this session, so the service can replay it
    fake.current = fake._numbered(good_job())
    fake.state.update(status="running", seq=len(good_job()), stage="base", progress=40.0)
    result = run_main(["--socket", fake.path], cli_script, host)
    assert result.code == 0 and "An installation is already running on this computer (Installing the base system, 40% done)." in cli_script.flat
    assert not requests_of(fake, "start") and requests_of(fake, "attach")[0]["after_seq"] == 0
    assert "The installation finished successfully." in cli_script.text


# --- --attach ----------------------------------------------------------------------------------------------------------------------------------------------


def test_attach_follows_the_last_job_and_reports_its_result(installer, host: FakeHost, tmp_path: Path) -> None:
    fake = serial_installer(installer)
    run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--socket", fake.path], Script(), host)
    script = Script([("What now", END)])
    result = run_main(["--attach", "--socket", fake.path], script, host)
    assert result.code == 0 and "The installation finished successfully." in script.text and requests_of(fake, "attach")


def test_attach_without_any_job_is_exit_1(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    result = run_main(["--attach", "--socket", fake.path], Script(), host)
    assert result.code == 1 and "nothing to follow" in result.out


def test_attach_to_a_failed_job_is_exit_1_and_resuming_is_not_offered_without_the_settings(installer, host: FakeHost, tmp_path: Path) -> None:
    fake = serial_installer(installer, jobs=[failing_job()])
    run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--socket", fake.path], Script(), host)
    script = Script([("What now", END)])
    result = run_main(["--attach", "--socket", fake.path], script, host)
    assert result.code == 1 and "Resume the installation" not in script.text and "Show the last lines" in script.text


def test_attach_with_json_events_prints_the_events(installer, host: FakeHost, tmp_path: Path) -> None:
    fake = serial_installer(installer)
    run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--socket", fake.path], Script(), host)
    result = run_main(["--attach", "--json-events", "--socket", fake.path], Script(), host)
    events = [json.loads(line) for line in result.out.splitlines()]
    assert result.code == 0 and events[-1]["type"] == "completed"


# --- the console script ---------------------------------------------------------------------------------------------------------------------------------


def test_the_console_script_is_declared() -> None:
    root = Path(gentoo_installer.__file__).resolve().parents[2]
    scripts = tomllib.loads((root / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["gentoo-installer-text"] == "gentoo_installer.textui.cli:main"
    assert scripts["gentoo-installer"] and scripts["gentoo-installerd"] and scripts["gentoo-rescue"]  # the others are kept


def test_the_module_can_be_run_and_needs_no_qt(tmp_path: Path) -> None:
    source = Path(gentoo_installer.__file__).resolve().parents[1]
    env = {"PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"}
    code = "import sys; sys.modules['PySide6'] = None; import gentoo_installer.textui.cli as cli; sys.exit(cli.main(['--version']))"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60, cwd=tmp_path)
    assert done.returncode == 0 and "gentoo-installer-text" in done.stdout, done.stderr
    module = subprocess.run([sys.executable, "-m", "gentoo_installer.textui", "--version"], capture_output=True, text=True, env=env, timeout=60, cwd=tmp_path)
    assert module.returncode == 0 and "gentoo-installer-text" in module.stdout, module.stderr


def test_a_dry_run_can_check_a_saved_copy_whose_passwords_were_left_out(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    result = run_main(["--manifest", write_manifest(tmp_path, redact=True), "--dry-run", "--socket", fake.path], Script(), host)
    assert result.code == 0 and "blanked out" in result.out and "ERASE  /dev/vda" in result.out and not requests_of(fake, "start")


def test_leaving_while_an_installation_runs_does_not_claim_that_nothing_changed(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    fake.current = fake._numbered(good_job())
    fake.state.update(status="running", seq=len(good_job()), stage="base", progress=40.0)
    script = Script([("already running", "q"), ("Quit the installer?", "y")])
    result = run_main(["--socket", fake.path], script, host)
    assert result.code == 130 and "Nothing has been changed" not in script.text and "gentoo-installer-text --attach" in script.text
    assert "You left the installer. The installation keeps running" in script.flat


# --- credentials ----------------------------------------------------------------------------------------------------------------------------------------


def test_the_password_of_a_proxy_is_never_printed(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    manifest = valid_manifest()
    manifest.network.proxy = "http://proxyuser:proxy-secret-9@proxy.example:3128"
    path = write_manifest(tmp_path, manifest)
    script = Script()
    result = run_main(["--manifest", path, "--dry-run", "--socket", fake.path], script, host)
    assert result.code == 0 and "proxy-secret-9" not in result.out + result.err and "proxyuser:<redacted>@proxy.example:3128" in result.out
    as_json = run_main(["--manifest", path, "--dry-run", "--json-events", "--socket", fake.path], Script(), host)
    assert "proxy-secret-9" not in as_json.out + as_json.err
    assert any("proxyuser:<redacted>@" in row["value"] for row in json.loads(as_json.out)["settings"])
    unattended = run_main(["--manifest", path, "--yes", "--confirm-device", "/dev/vda", "--socket", fake.path], Script(), host)
    assert unattended.code == 0 and "proxy-secret-9" not in unattended.out
    assert fake.manifests[0]["network"]["proxy"] == "http://proxyuser:proxy-secret-9@proxy.example:3128"  # the service gets the real value


def test_programs_cannot_run_in_tests() -> None:
    from gentoo_installer.textui.host import Host

    for call in (lambda: Host().loadkeys("us"), lambda: Host().reboot(), lambda: Host().run_interactive(["nmtui"])):
        with pytest.raises(AssertionError, match="real program"):
            call()
