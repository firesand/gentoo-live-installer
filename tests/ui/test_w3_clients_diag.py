"""Wave 3 review fixes: the diagnostics bundle shortens what it promises to shorten (DIAG-1), and output that is not UTF-8
cannot freeze the window or hide the disks (DIAG-2)."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

from gentoo_installer import diagnostics, system_info
from gentoo_installer.backend import BackendWorker
from gentoo_installer.pages.progress import DiagnosticsExporter, LogExporter
from tests.hardware.helpers import fixture_path

LSBLK_JSON = Path(__file__).resolve().parents[1] / "rescue" / "data" / "author_pc_lsblk.json"


# --- DIAG-1: serial numbers, WWNs and partition-table ids stay out of the command output -----------------------------------------


IDENTIFYING = {"SERIAL", "WWN", "UUID", "PTUUID", "PARTUUID", "ID", "ID-LINK"}


def bundle_argv(name: str) -> tuple[str, ...]:
    return dict(diagnostics.BUNDLE_COMMANDS)[name]


def test_lsblk_is_asked_for_columns_that_name_no_particular_device() -> None:
    argv = bundle_argv("lsblk")
    assert "--output-all" not in argv and "--output" in argv
    columns = argv[argv.index("--output") + 1].upper().split(",")
    assert not IDENTIFYING & set(columns), IDENTIFYING & set(columns)
    assert {"NAME", "SIZE", "MODEL", "FSTYPE", "PARTTYPE", "MOUNTPOINTS"} <= set(columns)  # what a bug report about a disk needs
    assert "--json" in argv


EFIBOOTMGR = """\
BootCurrent: 0001
Timeout: 1 seconds
BootOrder: 0001,0000,0003
Boot0000* Windows Boot Manager\tHD(1,GPT,5b7c0fa4-8d1e-4c6f-9a31-2e5d7c11aa01,0x800,0x32000)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)WINDOWS
Boot0001* Gentoo\tHD(1,GPT,2cb269e1-071b-4203-8525-6867ac712a83,0x800,0x100000)/File(\\EFI\\gentoo\\grubx64.efi)
Boot0002* Old MBR entry\tHD(2,MBR,0x1234abcd,0x1000,0x2000)/File(\\EFI\\boot\\bootx64.efi)
Boot0003* UEFI: NVMe\tPciRoot(0x0)/Pci(0x1d,0x0)/Pci(0x0,0x0)/NVMe(0x1,6479A71E50000A94)/HD(1,GPT,44e60460-e3eb-4ed3-9f52-d74213468c2a,0x800,0x100000)
Boot0004* UEFI: Network\tPciRoot(0x0)/Pci(0x1c,0x0)/Pci(0x0,0x0)/MAC(a0b1c2d3e4f5,0)/IPv4(0.0.0.00.0.0.0,0,0)
"""


def test_the_boot_entries_lose_partition_guids_disk_signatures_namespace_ids_and_mac_addresses() -> None:
    cleaned = diagnostics.scrub_efibootmgr(EFIBOOTMGR)
    for secret in ("5b7c0fa4-8d1e-4c6f-9a31-2e5d7c11aa01", "2cb269e1-071b-4203-8525-6867ac712a83", "44e60460-e3eb-4ed3-9f52-d74213468c2a",
                   "0x1234abcd", "6479A71E50000A94", "a0b1c2d3e4f5"):
        assert secret not in cleaned, secret
    # what helps a report stays: the entry names, the order, which partition and which file
    assert "BootOrder: 0001,0000,0003" in cleaned and "Boot0001* Gentoo" in cleaned
    assert "HD(1,GPT," in cleaned and "HD(2,MBR," in cleaned and "\\EFI\\gentoo\\grubx64.efi" in cleaned
    assert "Pci(0x1d,0x0)" in cleaned


def test_the_bundle_runs_the_boot_entries_through_that_cleaning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "BUNDLE_COMMANDS", (("efibootmgr", ("efibootmgr", "-v")), ("lsblk", ("lsblk",))))
    monkeypatch.setattr(diagnostics, "run_command", lambda argv, **kw: EFIBOOTMGR if argv[0] == "efibootmgr" else "NAME SIZE\nsda 1T")
    bundle = diagnostics.collect_bundle()
    text = json.dumps(bundle)
    assert "2cb269e1-071b-4203-8525-6867ac712a83" not in text and "Boot0001* Gentoo" in text
    assert bundle["commands"]["lsblk"] == "NAME SIZE\nsda 1T"


def test_the_success_message_promises_what_the_bundle_does(tmp_path: Path) -> None:
    message = DiagnosticsExporter(str(tmp_path / "bundle.json"), lambda: {"format": 1}).work()
    assert "serial numbers and other identifiers are shortened or removed" in message and "passwords are removed" in message


# --- DIAG-2: bytes that are not UTF-8 ---------------------------------------------------------------------------------------------


def test_a_command_that_prints_bytes_that_are_not_utf8_is_still_collected() -> None:
    output = diagnostics.run_command(("printf", r"USB DISK \3512.0"))  # printf is part of coreutils; \351 is Latin-1 "é"
    assert output.startswith("USB DISK ") and output.endswith("2.0")


def faithful_run(raw: bytes):
    """subprocess.run as it behaves for text=True: output that does not decode raises UnicodeDecodeError."""

    def run(argv, *, text=False, errors=None, encoding=None, **kwargs):
        stdout = raw.decode(encoding or "utf-8", errors or "strict") if (text or errors or encoding) else raw
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    return run


LSBLK_WITH_LATIN1 = LSBLK_JSON.read_bytes().replace(b"Samsung", b"USB DISK \xe9 Samsung", 1)


def test_the_disk_list_of_the_window_survives_a_model_name_with_bytes_that_are_not_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    assert b"\xe9" in LSBLK_WITH_LATIN1
    monkeypatch.setattr(system_info.subprocess, "run", faithful_run(LSBLK_WITH_LATIN1))
    roots = system_info._lsblk_roots()
    assert roots, "no disks were found because lsblk printed a byte that is not UTF-8"
    assert any("USB DISK" in root.model for root in roots)


def test_the_live_medium_lookup_survives_odd_bytes_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_info.subprocess, "run", faithful_run(b"/dev/sr0[/live \xe9]\n"))
    assert system_info._mount_sources() == ["/dev/sr0"]


def test_the_console_installers_own_disk_list_survives_it_as_well(monkeypatch: pytest.MonkeyPatch) -> None:
    from gentoo_installer.textui import host as host_module
    from gentoo_installer.textui.host import Host

    class Programs:
        class SubprocessError(Exception):
            pass

        class TimeoutExpired(SubprocessError):
            pass

        run = staticmethod(faithful_run(LSBLK_WITH_LATIN1))
        call = staticmethod(lambda argv, **kw: 0)

    monkeypatch.setattr(host_module, "subprocess", Programs)
    monkeypatch.setenv("GENTOO_HW_ROOT", str(fixture_path("qemu-q35-virtio")))
    reply = Host().local_inspection()
    assert reply["disks"], "no disks were found because lsblk printed a byte that is not UTF-8"


class Recorder(QObject):
    """Collects the (ok, message) a file job reports."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[tuple[bool, str]] = []

    def done(self, ok: bool, message: str) -> None:
        self.results.append((ok, message))


@pytest.mark.parametrize("failure", [ValueError("boom"), UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid"), RuntimeError("odd"), KeyError("k")])
def test_a_file_job_that_fails_in_any_way_reports_instead_of_leaving_the_thread(tmp_path: Path, failure: Exception) -> None:
    """An exception that leaves QThread.run() freezes PySide6 6.11 on Python 3.14, so run() is called directly here."""

    def collect() -> dict:
        raise failure

    job = DiagnosticsExporter(str(tmp_path / "bundle.json"), collect)
    recorder = Recorder()
    job.done.connect(recorder.done)
    job.run()
    assert len(recorder.results) == 1
    ok, message = recorder.results[0]
    assert not ok and "bundle.json" in message


def test_a_log_job_that_fails_in_an_odd_way_reports_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gentoo_installer.pages import progress

    monkeypatch.setattr(progress.daemon_client, "fetch_log", lambda *a, **k: (_ for _ in ()).throw(TypeError("odd")))
    job = LogExporter("/nonexistent.sock", str(tmp_path / "x.log"), "visible")
    recorder = Recorder()
    job.done.connect(recorder.done)
    job.run()
    assert recorder.results and recorder.results[0][0] is False


def test_the_backend_worker_reports_an_unexpected_error_instead_of_leaving_the_thread(unix_listener) -> None:  # type: ignore[no-untyped-def]
    path, _server = unix_listener("worker.sock")  # connecting works; writing the request does not
    worker = BackendWorker(None, path, request={"action": "status", "bad": object()})  # json cannot write an object()
    failures: list[str] = []
    worker.connectionFailed.connect(failures.append)
    worker.run()
    assert failures and "unexpected" in failures[0].lower()


@pytest.fixture
def restore_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)


def test_an_exception_in_a_worker_thread_never_opens_a_dialog(restore_hooks: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PySide hands an exception that leaves QThread.run() to sys.excepthook in that thread; a dialog built there kills the GUI."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    shown: list[str] = []
    diagnostics.install_excepthook(show=lambda text, path: shown.append(text))

    def worker() -> None:
        try:
            raise RuntimeError("in a worker")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())  # type: ignore[arg-type]

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(5)
    assert shown == []
    try:
        raise RuntimeError("in the main thread")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())  # type: ignore[arg-type]
    assert len(shown) == 1 and "in the main thread" in shown[0]
