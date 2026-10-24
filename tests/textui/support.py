"""Fakes for the console installer tests: a scripted terminal, a fake live system and a fake installer service.

Nothing here touches the machine the tests run on: the terminal is a script, ``FakeHost`` replaces
every program and system file, and ``FakeInstaller`` is a small server on a Unix socket in a
temporary directory that speaks the daemon protocol (docs/protocol.md).
"""

from __future__ import annotations

import io
import json
import socket
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

from gentoo_installer import blockdev
from gentoo_installer.manifest import InstallManifest, ManifestError
from gentoo_installer.textui.host import Host

from tests.hardware.helpers import fixture_dict

CTRL_C = object()  # an answer: the user presses Ctrl-C
END = object()  # an answer: the input ends (Ctrl-D)
DROP = object()  # a reply item: the service closes the connection at this point
ALL_TOOLS = {
    "lsblk": True, "findmnt": True, "blkid": True, "sgdisk": True, "parted": True, "partprobe": True, "udevadm": True,
    "wipefs": True, "mkfs.ext4": True, "mkfs.xfs": True, "mkfs.btrfs": True, "mkfs.vfat": True, "mkswap": True, "swapon": True,
    "swapoff": True, "mount": True, "umount": True, "tar": True, "gpg": True, "gpgv": True, "cryptsetup": True,
}


# --- the terminal ----------------------------------------------------------------------------------------------


class ScriptExhausted(AssertionError):
    """The program asked for input and the script has no answer left."""


class _Input:
    def __init__(self, script: Script) -> None:
        self.script = script
        self.mark = 0

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        script = self.script
        text = script.out.getvalue()
        shown, self.mark = text[self.mark:], len(text)
        if not script.steps:
            raise ScriptExhausted(f"The program wants more input than the script has.\n--- printed since the last answer ---\n{shown}\n--- whole session ---\n{text}")
        expected, answer = script.steps.pop(0)
        script.answered.append(answer if isinstance(answer, str) else repr(answer))
        if expected and expected not in shown:
            raise AssertionError(f"Expected {expected!r} before the answer {answer!r}.\n--- printed since the last answer ---\n{shown}\n--- whole session ---\n{text}")
        if answer is CTRL_C:
            raise KeyboardInterrupt
        if answer is END:
            return ""
        assert isinstance(answer, str)
        return answer + "\n"


class Script:
    """A terminal for one test. ``steps`` are (expected text, answer) pairs.

    The expected text must appear in what the program printed since the previous answer ("" skips the
    check); then the answer is "typed". ``CTRL_C`` and ``END`` are special answers."""

    def __init__(self, steps: Iterable[tuple[str, Any]] = ()) -> None:
        self.steps: list[tuple[str, Any]] = list(steps)
        self.out = io.StringIO()
        self.stdin = _Input(self)
        self.answered: list[str] = []

    @property
    def text(self) -> str:
        return self.out.getvalue()

    @property
    def flat(self) -> str:
        """The transcript with every run of white space (including line breaks) made one space, to find sentences."""
        return " ".join(self.text.split())

    def assert_finished(self) -> None:
        assert not self.steps, f"The program stopped asking before the script ended; unused: {self.steps}\n{self.text}"


# --- the live system ----------------------------------------------------------------------------------------------


class FakeHost(Host):
    """No program is started and no system file is read."""

    def __init__(self, home: Path) -> None:
        super().__init__()
        self.home = home
        self.calls: list[tuple[Any, ...]] = []
        self.slept: list[float] = []
        self.keymap_result: tuple[bool, str] = (True, "")
        self.reboot_result: tuple[bool, str] = (True, "")
        self.interactive_status = 0
        self.zones = ["Africa/Cairo", "America/New_York", "America/Sao_Paulo", "Asia/Jakarta", "Asia/Tokyo", "Europe/Berlin", "Europe/London", "Europe/Paris", "UTC"]
        self.locale_list = ["en_US.UTF-8", "de_DE.UTF-8", "en_GB.UTF-8", "fr_FR.UTF-8", "id_ID.UTF-8", "pt_BR.UTF-8"]
        self.keymap_names = {"us", "uk", "de-latin1", "de-latin1-nodeadkeys", "fr", "dvorak", "ru"}
        self.by_path: dict[str, str] = {}
        self.local_reply: dict[str, Any] = {}
        self.clock = 1_800_000_000.0

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def monotonic(self) -> float:
        return self.clock

    def now(self) -> float:
        return self.clock

    def loadkeys(self, keymap: str) -> tuple[bool, str]:
        self.calls.append(("loadkeys", keymap))
        return self.keymap_result

    def reboot(self) -> tuple[bool, str]:
        self.calls.append(("reboot",))
        return self.reboot_result

    def run_interactive(self, argv: Sequence[str]) -> int:
        self.calls.append(("run", tuple(argv)))
        return self.interactive_status

    def home_dir(self) -> Path:
        return self.home

    def console_keymaps(self) -> set[str]:
        return set(self.keymap_names)

    def timezones(self) -> list[str]:
        return list(self.zones)

    def locales(self) -> list[str]:
        return list(self.locale_list)

    def by_path_names(self) -> dict[str, str]:
        return dict(self.by_path)

    def local_inspection(self) -> dict[str, Any]:
        self.calls.append(("local_inspection",))
        return dict(self.local_reply)

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == name]


# --- what the service says about the computer ----------------------------------------------------------------------


def lsblk_disk(disk: dict[str, Any], *, serial: str | None = None, mounted: bool = False) -> dict[str, Any]:
    """A hardware-profile disk as the service reports it in ``data.disks`` (BlockDevice.to_dict() keys)."""
    partitions = disk["partitions"] or ([{"name": disk["name"] + "1", "size": disk["size"] // 2, "fstype": "ext4", "label": "", "parttype": ""}] if mounted else [])
    children = tuple(
        blockdev.BlockDevice(
            name=part["name"], path="/dev/" + part["name"], type="part", size=part["size"], fstype=part["fstype"], label=part["label"],
            parttype=part["parttype"], mountpoints=("/mnt/data",) if mounted else (),
        )
        for part in partitions
    )
    node = blockdev.BlockDevice(
        name=disk["name"], path=disk["path"], type="disk", size=disk["size"], model=disk["model"],
        serial=disk["serial"] if serial is None else serial, wwn=disk["wwn"], transport=disk["tran"] or disk["bus"],
        removable=disk["removable"], hotplug=disk["hotplug"], ro=disk["ro"], log_sec=disk["log_sec"] or 512, phy_sec=disk["phy_sec"] or 512,
        maj_min=f"{8 if disk['name'].startswith('sd') else 252}:{abs(hash(disk['name'])) % 200 * 16}", pttype=disk["pttype"], children=children,
    )
    return node.to_dict()


def inspect_reply(
    machine: str = "qemu-q35-virtio", *, network: bool | None = True, serials: dict[str, str] | None = None, mounted: Sequence[str] = (),
    extra_findings: Sequence[dict[str, str]] = (), tools: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """The ``inspect`` reply for one of the captured test machines (tests/hardware/fixtures)."""
    hardware = fixture_dict(machine)
    serials = serials or {}
    for item in hardware["disks"]:  # the daemon's lsblk and its hardware profile come from the same probe
        if item["path"] in serials:
            item["serial"] = serials[item["path"]]
    disks = [lsblk_disk(disk, serial=serials.get(disk["path"]), mounted=disk["path"] in mounted) for disk in hardware["disks"]]
    live = [disk["path"] for disk in hardware["disks"] if disk["live_medium"]]
    live += [f"{path}{index}" for path in list(live) for index in (1, 2)]
    reply: dict[str, Any] = {
        "architecture": "x86_64",
        "memory_bytes": hardware["memory"]["total_kib"] * 1024,
        "uefi": hardware["firmware"]["mode"] == "uefi",
        "tools": dict(ALL_TOOLS if tools is None else tools),
        "disks": disks,
        "errors": [],
        "warnings": [*hardware["warnings"], *extra_findings],
        "live_medium": live,
        "hardware": hardware,
    }
    if network is not None:
        reply["network"] = network
    return reply


# --- events -----------------------------------------------------------------------------------------------------------------


def stage(name: str, progress: float, status: str = "running", **data: Any) -> dict[str, Any]:
    return {"type": "stage", "stage": name, "message": f"{status} {name}", "progress": progress, "data": {"status": status, "stage_progress": 0 if status == "running" else 100, **data}}


def progress(name: str, percent: float, message: str = "", **data: Any) -> dict[str, Any]:
    return {"type": "progress", "stage": name, "message": message, "progress": percent, "data": data}


def log(text: str) -> dict[str, Any]:
    return {"type": "log", "message": text}


def error(message: str, code: str = "install.failed", stage_name: str = "base", **data: Any) -> dict[str, Any]:
    return {"type": "error", "stage": stage_name, "message": message, "error": message, "data": {"code": code, "retryable": True, **data}}


def completed(success: bool, message: str = "", **data: Any) -> dict[str, Any]:
    return {"type": "completed", "message": message or ("Installation completed successfully" if success else "Installation failed"), "data": {"success": success, **data}}


def good_job() -> list[dict[str, Any]]:
    return [
        stage("preflight", 0), stage("preflight", 2, "completed"),
        stage("storage", 2), progress("storage", 6, "Storage layout prepared"), stage("storage", 10, "completed"),
        stage("stage3", 10), progress("stage3", 14, "Downloading stage3", downloaded=50 * 1024**2, total=200 * 1024**2),
        progress("stage3", 22, "Downloading stage3", downloaded=200 * 1024**2, total=200 * 1024**2), stage("stage3", 22, "completed"),
        stage("base", 40), log(">>> Emerging (1 of 2) sys-apps/foo-1.0::gentoo"), log(">>> /usr/bin/foo"), log("--- /usr/share/foo"),
        progress("base", 55, "Package 1 of 2", package=1, packages=2), log(">>> Emerging (2 of 2) sys-apps/bar-2.0::gentoo"),
        progress("base", 70, "Package 2 of 2", package=2, packages=2), stage("base", 70, "completed"),
        stage("cleanup", 98), stage("cleanup", 100, "completed"),
        completed(True),
    ]


def failing_job(*, resumable: bool = True, bootable: bool = False) -> list[dict[str, Any]]:
    return [
        stage("preflight", 0), stage("storage", 2), stage("storage", 10, "completed"), stage("base", 40),
        log("emerge: dev-libs/broken failed"), error("emerge failed: dev-libs/broken-1.2 could not be built", resumable=resumable, bootable=bootable),
        completed(False, resumable=resumable),
    ]


# --- the installer service ------------------------------------------------------------------------------------------------


class FakeInstaller:
    """A server on a Unix socket that speaks the installer protocol (docs/protocol.md), one request per connection."""

    def __init__(self, path: Path, reply: dict[str, Any] | None = None, jobs: Sequence[Sequence[dict[str, Any]]] | None = None) -> None:
        self.path = str(path)
        self.reply = reply if reply is not None else inspect_reply()
        self.jobs: list[list[dict[str, Any]]] = [list(job) for job in (jobs or [good_job()])]
        self.features = ["install.v1", "attach.v1", "hardware.v1"]
        self.actions = ["inspect", "status", "cancel", "start", "capabilities", "attach", "log", "sync_time", "set_time"]
        self.state: dict[str, Any] = {"status": "idle", "seq": 0, "stage": "", "progress": None, "resumable": False, "log_path": "", "error": ""}
        self.requests: list[dict[str, Any]] = []
        self.manifests: list[dict[str, Any]] = []
        self.resumes: list[bool] = []
        self.drop_after: int | None = None  # close the connection after this many job events of the next start
        self.attach_drops: list[int] = []  # for the next attaches: close after this many events
        self.log_lines = ["2026-10-24T12:00:00Z log [storage] first line", "2026-10-24T12:00:01Z log [base] last line"]
        self.action_replies: dict[str, dict[str, Any]] = {"sync_time": {"ok": True, "message": "The clock was set from the network."}, "set_time": {"ok": True, "message": "The clock was set."}}
        self.inspect_error: str = ""
        self.cancelled = False
        self.current: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = False
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(8)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- socket plumbing

    def _serve(self) -> None:
        while not self._stop:
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        connection.settimeout(5)
        try:
            with connection, connection.makefile("rwb") as stream:
                line = stream.readline()
                if not line:
                    return
                request = json.loads(line)
                for event in self.respond(request):
                    if event is DROP:
                        return
                    stream.write(json.dumps(event).encode() + b"\n")
                    stream.flush()
        except OSError:
            pass

    def close(self) -> None:
        self._stop = True
        try:
            self._server.close()
        except OSError:
            pass

    # -- the protocol

    def respond(self, request: dict[str, Any]) -> list[Any]:
        with self._lock:
            self.requests.append(request)
            action = request.get("action")
            if action == "status":
                return [{"type": "status", "data": dict(self.state)}]
            if action == "capabilities":
                return [{"type": "result", "message": "capabilities", "data": {"protocol": 1, "features": self.features, "actions": self.actions}}]
            if action == "inspect":
                if self.inspect_error:
                    return [error(self.inspect_error, "proto.internal", ""), completed(False)]
                return [{"type": "status", "message": "Inspection completed", "data": self.reply}, completed(True, "Inspection completed")]
            if action == "start":
                return self._start(request)
            if action == "attach":
                return self._attach(request)
            if action == "log":
                return [{"type": "result", "message": "install log", "data": {"lines": self.log_lines}}, completed(True, lines=len(self.log_lines))]
            if action == "cancel":
                self.cancelled = True
                return [{"type": "status", "message": "Cancellation requested", "data": {"status": "cancelling"}}]
            if action in self.action_replies:
                return [{"type": "result", "data": self.action_replies[action]}, completed(True)]
            return [error(f"Unsupported action: {action}", "proto.unsupported_action", ""), completed(False)]

    def _numbered(self, script: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**event, "seq": number, "job_id": "job-1"} for number, event in enumerate(script, 1)]

    def _start(self, request: dict[str, Any]) -> list[Any]:
        raw = request.get("manifest")
        try:
            InstallManifest.from_dict(raw).require_valid()  # type: ignore[arg-type]
        except ManifestError as exc:
            return [error(str(exc), "install.invalid_manifest", ""), completed(False)]
        if self.state["status"] == "running":
            return [error("An installation is already running", "install.busy", "", retryable=False), completed(False)]
        self.manifests.append(raw)  # type: ignore[arg-type]
        self.resumes.append(bool(request.get("resume")))
        script = self.jobs.pop(0) if len(self.jobs) > 1 else self.jobs[0]
        self.current = self._numbered(script)
        self.state.update(status="running", seq=len(self.current), stage="", progress=None)
        out: list[Any] = [{"type": "status", "job_id": "job-1", "message": "Installation started", "data": {"status": "running", "job_id": "job-1"}}]
        if self.drop_after is not None:
            out.extend(self.current[: self.drop_after])
            out.append(DROP)
            self.drop_after = None
            return out
        out.extend(self.current)
        self._finish()
        return out

    def _attach(self, request: dict[str, Any]) -> list[Any]:
        if not self.current:
            return [error("No installation has run in this session", "install.not_running", ""), completed(False)]
        after = int(request.get("after_seq", 0))
        events: list[Any] = [event for event in self.current if event["seq"] > after]
        if self.attach_drops:
            events = events[: self.attach_drops.pop(0)]
            return [*events, DROP]
        self._finish()
        return events

    def _finish(self) -> None:
        done = next((event for event in self.current if event["type"] == "completed"), None)
        if done is None:
            return
        success = done["data"].get("success") is True
        failure = next((event for event in self.current if event["type"] == "error"), None)
        cancelled = failure is not None and failure["data"].get("code") == "install.cancelled"
        self.state.update(
            status="completed" if success else "cancelled" if cancelled else "failed", resumable=bool(done["data"].get("resumable")),
            error=failure["message"] if failure else "", log_path="/var/lib/gentoo-installer/install.log",
        )


class Result:
    """What one run of the command did."""

    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err


def run_main(argv: Sequence[str], script: Script, host: FakeHost, *, environ: dict[str, str] | None = None) -> Result:
    """Run ``gentoo-installer-text`` against the scripted terminal. Standard error is captured separately."""
    from gentoo_installer.textui.cli import main

    err = io.StringIO()
    code = main(list(argv), stdin=script.stdin, stdout=script.out, stderr=err, environ={"TERM": "dumb", **(environ or {})}, host=host)  # type: ignore[arg-type]
    return Result(code, script.text, err.getvalue())
