"""The console installer against the real daemon server (gentoo_installerd.server) with a scripted installer.

The fake service of the other tests is only as good as our reading of docs/protocol.md; here the real
socket server, the real event buffer and the real ``attach`` answer. No disk, no network and no real
installation: the Installer class is replaced by a script that emits events.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import patch

import pytest

from gentoo_installerd.events import Event
from gentoo_installerd.server import ActionRegistry, InstallManager, UnixServer

from gentoo_installer.textui.console import Console
from gentoo_installer.textui.daemon import Daemon
from gentoo_installer.textui.follow import InstallFollower, TextSink

from tests.textui.support import FakeHost, Script, run_main, valid_manifest

Script_ = Callable[["ScriptedInstaller"], bool]


class ScriptedInstaller:
    """Stands in for gentoo_installerd.install.Installer: run() emits events and decides the result."""

    script: Script_

    def __init__(self, manifest: Any, **kwargs: Any) -> None:
        self.emit = kwargs["emit"]
        self.cancel: threading.Event = kwargs["cancel"]
        self.resume = kwargs.get("resume", False)
        self.job_id = uuid.uuid4().hex
        self.secrets: tuple[str, ...] = ()

    def run(self) -> bool:
        return type(self).script(self)

    def stage(self, name: str, progress: float, status: str = "running") -> None:
        self.emit(Event("stage", job_id=self.job_id, stage=name, message=f"{status} {name}", progress=progress, data={"status": status}))

    def log(self, text: str) -> None:
        self.emit(Event("log", job_id=self.job_id, message=text))

    def finish(self, success: bool, **data: Any) -> bool:
        self.emit(Event("completed", job_id=self.job_id, message="done", data={"success": success, **data}))
        return success


class RealDaemon:
    def __init__(self, directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.directory = directory
        self.path = str(directory / "real.sock")
        self.manager = InstallManager(directory / "target", directory / "state", directory / "cache")
        self.server = UnixServer(self.path, self.manager, ActionRegistry())
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.monkeypatch = monkeypatch

    def run_job(self, script: Script_) -> None:
        installer = type("Scripted", (ScriptedInstaller,), {"script": staticmethod(script)})
        patcher = patch("gentoo_installerd.server.Installer", installer)
        patcher.start()
        self.monkeypatch.setattr(type(self), "_patcher", patcher, raising=False)

    def close(self) -> None:
        patcher = getattr(self, "_patcher", None)
        if patcher is not None:
            patcher.stop()
        self.manager.shutdown(5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture
def real(sock_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RealDaemon]:
    daemon = RealDaemon(sock_dir, monkeypatch)
    yield daemon
    daemon.close()


def write_manifest(tmp_path: Path) -> str:
    path = tmp_path / "manifest.json"
    path.write_text(valid_manifest().to_json())
    return str(path)


def follower_for(real: RealDaemon, host: FakeHost, daemon: Daemon | None = None, steps=()) -> tuple[InstallFollower, Script]:
    script = Script(steps)
    console = Console(script.stdin, script.out, environ={})
    return InstallFollower(daemon or Daemon(real.path), console, host, TextSink(console)), script


def request() -> dict[str, Any]:
    return {"action": "start", "manifest": valid_manifest().to_dict(), "resume": False}


def good_script(installer: ScriptedInstaller) -> bool:
    installer.stage("preflight", 0)
    installer.stage("storage", 0)
    installer.log("Partitioning")
    installer.stage("storage", 100, "completed")
    installer.stage("base", 0)
    installer.log(">>> Emerging (1 of 1) sys-apps/foo-1.0::gentoo")
    installer.stage("base", 100, "completed")
    return installer.finish(True)


def test_an_unattended_install_against_the_real_server(real: RealDaemon, tmp_path: Path, host: FakeHost) -> None:
    real.run_job(good_script)
    script = Script()
    result = run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--socket", real.path], script, host)
    assert result.code == 0, script.text
    assert "The installation has started." in script.text and "Preparing disks and filesystems" in script.text and ">>> Emerging (1 of 1)" in script.text
    assert "The installation finished successfully." in script.text


def test_json_events_from_the_real_server_carry_sequence_numbers(real: RealDaemon, tmp_path: Path, host: FakeHost) -> None:
    real.run_job(good_script)
    result = run_main(["--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--json-events", "--socket", real.path], Script(), host)
    events = [json.loads(line) for line in result.out.splitlines()]
    numbers = [event["seq"] for event in events if "seq" in event]
    assert result.code == 0 and numbers == sorted(numbers) and numbers[0] == 1 and events[-1]["type"] == "completed"


def test_the_service_advertises_what_the_console_installer_needs(real: RealDaemon) -> None:
    state = Daemon(real.path).probe()
    assert state.reachable and state.supports("attach.v1") and state.supports("install.v1") and state.job_status == "idle"


class LosingTheConnection(Daemon):
    """The first stream breaks after ``count`` events, as if the cable were pulled; ``then`` runs at that moment and ``on_attach`` when the next connection is made."""

    def __init__(self, path: str, count: int, then: Callable[[], None], on_attach: Callable[[], None] = lambda: None) -> None:
        super().__init__(path)
        self.count, self.then, self.on_attach, self.broken = count, then, on_attach, False

    def stream(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        if self.broken:
            self.on_attach()
            yield from super().stream(request)
            return
        self.broken = True
        for number, event in enumerate(super().stream(request), 1):
            yield event
            if number == self.count:  # break right after this event, without waiting for the next one
                self.then()
                raise ConnectionResetError("connection lost")


def test_a_lost_connection_is_re_attached_to_the_running_job_and_misses_nothing(real: RealDaemon, host: FakeHost) -> None:
    gap, reattached = threading.Event(), threading.Event()

    def script(installer: ScriptedInstaller) -> bool:
        installer.stage("preflight", 0)
        installer.stage("storage", 0)
        installer.log("before the break")
        gap.wait(10)  # the connection is lost here ...
        for number in range(30):
            installer.log(f"while nobody watched {number}")
        reattached.wait(10)  # ... and the job is still running when the follower asks for its status
        installer.stage("base", 50)
        return installer.finish(True)

    real.run_job(script)
    daemon = LosingTheConnection(real.path, 4, gap.set, reattached.set)
    follower, screen = follower_for(real, host, daemon)
    outcome = follower.run(request())
    assert outcome.ok, screen.text
    assert "still running. Reconnecting" in screen.flat
    assert "while nobody watched 0" in screen.text and "while nobody watched 29" in screen.text
    assert screen.text.count("before the break") == 1 and screen.text.count("Preparing disks and filesystems") == 1
    assert outcome.last_seq == 35  # nothing lost and nothing twice: 2 stages + 1 + 30 log lines + 1 stage + the end


def test_cancelling_from_the_ctrl_c_menu_stops_the_real_job(real: RealDaemon, host: FakeHost) -> None:
    def script(installer: ScriptedInstaller) -> bool:
        installer.stage("storage", 0)
        while not installer.cancel.wait(0.02):
            pass
        installer.emit(Event("error", job_id=installer.job_id, stage="storage", message="Installation cancelled", error="cancelled", data={"code": "install.cancelled", "retryable": True, "resumable": False}))
        return installer.finish(False, resumable=False)

    real.run_job(script)
    started = threading.Event()

    class Impatient(LosingTheConnection):
        def stream(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
            if not self.broken:
                self.broken = True
                for number, event in enumerate(Daemon.stream(self, request), 1):
                    yield event
                    if number == 2:  # the "started" status and the first stage
                        started.set()
                        raise KeyboardInterrupt
                return
            yield from Daemon.stream(self, request)

    follower, screen = follower_for(real, host, Impatient(real.path, 0, lambda: None), [("What do you want to do", "3"), ("Really cancel", "y")])
    outcome = follower.run(request())
    assert started.is_set() and outcome.state == "cancelled", screen.text
    assert "Cancellation requested" in screen.text


def test_the_status_of_a_failed_job_is_what_the_follower_reports(real: RealDaemon, host: FakeHost) -> None:
    def script(installer: ScriptedInstaller) -> bool:
        installer.stage("storage", 0)
        installer.stage("storage", 100, "completed")
        installer.emit(Event("error", job_id=installer.job_id, stage="base", message="emerge died", error="emerge died", data={"code": "install.failed", "retryable": True, "resumable": True}))
        return installer.finish(False, resumable=True)

    real.run_job(script)
    follower, _ = follower_for(real, host)
    outcome = follower.run(request())
    assert (outcome.state, outcome.stage, outcome.message, outcome.resumable) == ("failed", "base", "emerge died", True)
    deadline = time.monotonic() + 5
    while real.manager.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    status = Daemon(real.path).status()
    assert status["status"] == "failed" and status["error"] == "emerge died"


# --- the real inspect reply -------------------------------------------------------------------------------------------------------------------------


def test_the_real_inspect_reply_is_understood(real: RealDaemon, monkeypatch: pytest.MonkeyPatch) -> None:
    """handle_inspect with a prepared inventory (no network, no programs): the reply has the shape the daemon really sends."""
    from gentoo_installer import blockdev, hardware
    from gentoo_installerd import server
    from gentoo_installerd.inspect import Inspection

    from tests.hardware.helpers import fixture_dict
    from tests.textui.support import ALL_TOOLS, device_number
    from gentoo_installer.textui.inventory import MachineView

    profile = hardware.HardwareProfile.from_dict(fixture_dict("qemu-q35-virtio"))
    vda = blockdev.BlockDevice(name="vda", path="/dev/vda", type="disk", size=40 * 1024**3, model="", serial="REAL-SERIAL-1", maj_min=device_number("vda"), transport="virtio")
    usb = blockdev.BlockDevice(name="sdb", path="/dev/sdb", type="disk", size=16 * 1024**3, model="USB DISK 3.0", maj_min=device_number("sdb"), transport="usb", removable=True, hotplug=True,
                               children=(blockdev.BlockDevice(name="sdb1", path="/dev/sdb1", type="part", size=3 * 1024**3, maj_min=device_number("sdb1"), fstype="iso9660"),))

    class Discovery:
        def __init__(self, runner, **kwargs) -> None:
            pass

        def inspect(self) -> Inspection:
            return Inspection("x86_64", 4 * 1024**3, True, True, dict(ALL_TOOLS), [vda, usb], [], [], [], ["/dev/sdb", "/dev/sdb1"])

    monkeypatch.setattr(server, "Discovery", Discovery)
    monkeypatch.setattr(hardware, "probe", lambda *args, **kwargs: profile)
    reply = Daemon(real.path).inspect(proxy="http://proxy.example:3128")
    view = MachineView(reply)
    assert view.uefi is True and view.network is True and view.profile.machine.hypervisor == "qemu"
    entries = {entry.disk.path: entry for entry in view.disk_entries()}
    assert entries["/dev/vda"].usable and entries["/dev/vda"].disk.transport == "virtio"  # the to_dict() spelling survived the trip
    assert not entries["/dev/sdb"].usable and "installer medium" in entries["/dev/sdb"].problem
    assert {finding.code for finding in view.findings} >= {"hw.vm_detected"}
