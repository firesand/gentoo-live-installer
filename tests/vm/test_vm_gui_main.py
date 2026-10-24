"""The GUI driver's whole run (scripts/vm-gui-install.py main) against the real wizard, offscreen, with a fake daemon.

No VM: the driver runs in this process under Qt's offscreen platform, walks the real InstallerWindow
through its Continue buttons, confirms the real erase dialog and talks to a daemon socket that plays
an installation (or a cancelled one) from a script. What the driver prints is what the harness parses.
"""
from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

import pytest

from gentoo_installerd.events import Event
from gentoo_installerd.server import UnixServer
from gentoo_installer.system_info import DiskInfo, SystemInfo
from tests.vm.helpers import PUBLIC_KEY, load_vm_module
from tests.vm.test_vm_gui_driver import driver

vm = load_vm_module()
pytestmark = pytest.mark.slow


class FakeDaemon:
    """Stands in for InstallManager: plays a script of events, and a cancelled ending on request."""

    def __init__(self, *, cancellable: bool = False) -> None:
        self.events: queue.Queue[Event | None] = queue.Queue()
        self.cancellable = cancellable
        self.started = threading.Event()
        self.manifest = None

    def start(self, manifest, resume=False):
        self.manifest = manifest
        self.started.set()
        self.events.put(Event("stage", stage="storage", data={"status": "completed"}))
        if self.cancellable:
            self.events.put(Event("stage", stage="stage3", data={"status": "running"}))
        else:
            self.events.put(Event("log", message="$ emerge --quiet sys-apps/portage"))
            self.events.put(Event("completed", message="done", data={"success": True}))
            self.events.put(None)
        return self.events

    def request_cancel(self):
        self.events.put(Event("error", error="cancelled", message="Installation cancelled"))
        self.events.put(Event("stage", stage="cleanup", data={"status": "completed"}))
        self.events.put(Event("completed", message="cancelled", data={"success": False}))
        self.events.put(None)


@pytest.fixture
def lab(tmp_path, monkeypatch, sock_dir, qapp):
    """Everything the driver needs from its surroundings, faked."""
    monkeypatch.setattr(driver.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("DISPLAY", ":0")
    xauth = tmp_path / "xauth"
    xauth.write_text("x")
    monkeypatch.setenv("XAUTHORITY", str(xauth))
    monkeypatch.setattr(driver, "guard_problems", lambda spec: [])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # Lab.machine() is the whole machine: the GUI must not ask the test server's real
    # `inspect` handler, which would read this host's disks and firmware instead.
    monkeypatch.setenv("GENTOO_INSTALLER_DEV", "1")

    class Lab:
        output = tmp_path / "out"
        socket = str(sock_dir / "d.sock")
        servers: list = []
        daemons: list = []

        def machine(self, *, uefi=True, network=True, disks=None):
            info = SystemInfo(architecture="x86_64", memory_gib=8, uefi=uefi, network=network, synchronized_clock=True,
                              missing_tools=[], disks=disks if disks is not None else [
                                  DiskInfo("/dev/vda", "QEMU", 40 * 2**30, serial=vm.TARGET_SERIAL)])
            monkeypatch.setattr("gentoo_installer.app.inspect_system", lambda: info)

        def manifest(self, scenario="openrc") -> Path:
            path = tmp_path / "manifest.json"
            vm.build_manifest(vm.SCENARIO_MAP[scenario], PUBLIC_KEY).save(path, redact=False)
            return path

        def plan(self, **plan) -> Path:
            path = tmp_path / "plan.json"
            path.write_text(json.dumps(plan))
            return path

        def serve(self, daemon: FakeDaemon):
            self.daemons.append(daemon)
            server = UnixServer(self.socket, daemon)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers.append((server, thread))
            return server

        def run(self, *arguments: str, plan: dict | None = None) -> int:
            """Run the driver; the plan defaults to what the harness sends for a pre-filled run."""
            if "--plan" not in arguments:
                plan = plan if plan is not None else {"guard": {"disks": []}, "detect": False,
                                                      "target": {"path": "/dev/vda", "serial": vm.TARGET_SERIAL}}
                arguments = (*arguments, "--plan", str(self.plan(**plan)))
            return driver.main([str(self.manifest_path), "--output", str(self.output), "--socket", self.socket,
                                "--navigation-timeout", "20", "--idle-timeout", "20", "--readiness-timeout", "4", *arguments])

    lab = Lab()
    lab.servers, lab.daemons = [], []
    yield lab
    for daemon in lab.daemons:
        daemon.events.put(None)  # end every event stream, so the GUI's worker thread finishes before the window goes away
    for server, thread in lab.servers:
        server.shutdown()
        server.server_close()
        thread.join(2)


def events_of(capsys) -> list[dict]:
    out = []
    for line in capsys.readouterr().out.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def test_a_prefilled_install_walks_all_pages_confirms_the_erase_dialog_and_reports_success(lab, capsys):
    lab.machine()
    lab.manifest_path = lab.manifest()
    daemon = FakeDaemon()
    lab.serve(daemon)
    assert lab.run() == 0
    events = events_of(capsys)
    navigation = next(event for event in events if event["type"] == "gui_navigation")
    assert navigation["manifest_matches"] is True and navigation["target"] == "/dev/vda" and navigation["detect"] is False
    result = next(event for event in events if event["type"] == "gui_result")
    assert result["success"] is True and result["reboot_enabled"] is True
    assert [e["type"] for e in events].count("error") == 0
    assert daemon.manifest.storage.device == "/dev/vda" and daemon.manifest.accounts.hostname == "gentoo-vm-openrc"
    for artifact in ("review.png", "result.png", "gui.log"):
        assert (lab.output / artifact).stat().st_size > 0


def test_detect_mode_lets_the_gui_choose_and_writes_down_what_it_chose(lab, capsys):
    lab.machine()
    lab.manifest_path = lab.manifest("defaults")
    expected = vm.build_manifest(vm.SCENARIO_MAP["defaults"], PUBLIC_KEY).to_dict(redact=False)
    overrides = vm.manifest_overrides(expected, __import__("gentoo_installer.manifest", fromlist=["InstallManifest"]).InstallManifest().to_dict(redact=False))
    plan = lab.plan(guard={"disks": []}, detect=True, overrides=overrides, target={"path": "/dev/vda", "by_path": []},
                    detected={"storage.device": "/dev/vda", "storage.device_id": vm.TARGET_SERIAL, "storage.device_id_kind": "",
                              "storage.firmware": "uefi", "storage.table": "gpt", "system.stage_variant": None})
    lab.serve(FakeDaemon())
    assert lab.run("--plan", str(plan)) == 0
    detected = json.loads((lab.output / "detected.json").read_text())
    assert detected["storage.firmware"] == "uefi" and detected["storage.device"] == "/dev/vda"
    assert detected["preselected"] in ("", None, "/dev/vda")  # what the GUI had selected before the driver chose
    navigation = next(event for event in events_of(capsys) if event["type"] == "gui_navigation")
    assert navigation["detect"] is True


def test_detect_mode_approves_a_disk_without_a_serial_number_like_a_user(lab, capsys):
    lab.machine(disks=[DiskInfo("/dev/vda", "QEMU", 64 * 2**30, by_path="pci-0000:00:04.0")])
    lab.manifest_path = lab.manifest("defaults")
    expected = vm.build_manifest(vm.SCENARIO_MAP["defaults"], PUBLIC_KEY).to_dict(redact=False)
    overrides = vm.manifest_overrides(expected, __import__("gentoo_installer.manifest", fromlist=["InstallManifest"]).InstallManifest().to_dict(redact=False))
    plan = lab.plan(guard={"disks": []}, detect=True, overrides=overrides, target={"path": "/dev/vda", "by_path": ["pci-0000:00:04.0"]},
                    detected={"storage.device": "/dev/vda", "storage.device_id": None, "storage.device_id_kind": "path",
                              "storage.firmware": "uefi", "storage.table": "gpt", "system.stage_variant": None})
    daemon = FakeDaemon()
    lab.serve(daemon)
    assert lab.run("--plan", str(plan)) == 0, [event for event in events_of(capsys) if event["type"] == "error"]
    assert daemon.manifest.storage.device_id_kind == "path"
    assert daemon.manifest.storage.device_id.startswith("pci-0000:00:04.0|")


def test_a_gui_that_detects_the_wrong_firmware_fails_the_run_with_the_difference(lab, capsys):
    lab.machine(uefi=False)  # a BIOS live system ...
    lab.manifest_path = lab.manifest("openrc")  # ... installed with a UEFI scenario's manifest
    # The scenario says UEFI, the machine is BIOS: the readiness check refuses before any page is touched.
    lab.serve(FakeDaemon())
    assert lab.run() == 1
    errors = [event for event in events_of(capsys) if event["type"] == "error"]
    assert errors and "firmware does not match the live VM firmware" in errors[0]["error"]
    assert not (lab.output / "review.png").exists()


def test_a_page_that_refuses_to_continue_ends_the_run_with_its_message(lab, capsys, monkeypatch):
    lab.machine()
    lab.manifest_path = lab.manifest()
    lab.serve(FakeDaemon())
    from gentoo_installer.pages.accounts import AccountsPage
    monkeypatch.setattr(AccountsPage, "validate_page", lambda self: (False, "The user password is far too short."))
    assert lab.run() == 1
    errors = [event for event in events_of(capsys) if event["type"] == "error"]
    assert errors[0]["error"] == "The user password is far too short." and errors[0]["dialog"] == "Check this step"


def test_missing_network_is_retried_until_the_readiness_timeout_then_reported_with_the_rows(lab, capsys):
    lab.machine(network=False)
    lab.manifest_path = lab.manifest()
    lab.serve(FakeDaemon())
    assert lab.run("--readiness-timeout", "1") == 1
    [error] = [event for event in events_of(capsys) if event["type"] == "error"]
    assert "network is not reachable" in error["error"] and "after 1s" in error["error"]
    assert "Internet connection" not in error["error"] and '"network"' in error["error"]  # the readiness rows are included


def test_a_silent_backend_ends_with_the_watchdog_status(lab, capsys):
    lab.machine()
    lab.manifest_path = lab.manifest()

    class Silent(FakeDaemon):
        def start(self, manifest, resume=False):
            self.started.set()
            return self.events  # never sends anything

    lab.serve(Silent())
    assert lab.run("--idle-timeout", "1") == driver.WATCHDOG_STATUS
    errors = [event for event in events_of(capsys) if event["type"] == "error"]
    assert errors[-1]["error"] == "Watchdog: no backend event for 1s" or "Watchdog" in errors[-1]["error"]


def test_the_cancel_flow_clicks_cancel_when_stage3_starts_and_expects_a_clean_stop(lab, capsys):
    lab.machine()
    lab.manifest_path = lab.manifest()
    lab.serve(FakeDaemon(cancellable=True))
    assert lab.run("--cancel-at-stage3") == 0
    result = next(event for event in events_of(capsys) if event["type"] == "gui_result")
    assert result["success"] is False and result["cancel_requested"] is True
    assert result["storage_completed"] is True and result["stage3_started"] == 1
