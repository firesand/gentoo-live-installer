"""The Qt client of the rescue actions, against the real daemon over a fake machine."""

from __future__ import annotations

import socket
import threading

import pytest

from gentoo_installer.rescue_ui.client import Call, RescueClient
from tests.ui.rescue_fakes import (  # noqa: F401  (fixtures)
    CuttingProxy,
    daemon_factory,
    gentoo_machine,
    proxy_factory,
    rescue_lab_factory,
)


class Outcome:
    """Collects what a Call reports so a test can wait for it."""

    def __init__(self, call: Call) -> None:
        self.call = call
        self.events: list[dict] = []
        self.result: dict | None = None
        self.failure: tuple[str, str, dict] | None = None
        self.lost: str | None = None
        self.finished = False
        call.eventReceived.connect(self.events.append)
        call.succeeded.connect(lambda data: setattr(self, "result", data))
        call.failed.connect(lambda code, text, data: setattr(self, "failure", (code, text, data)))
        call.lost.connect(lambda text: setattr(self, "lost", text))
        call.finished.connect(lambda: setattr(self, "finished", True))


def run(qtbot, client: RescueClient, action: str, **params) -> Outcome:
    outcome = Outcome(client.request(action, params))
    qtbot.waitUntil(lambda: outcome.finished, timeout=30000)
    return outcome


def test_the_probe_reports_the_rescue_feature(qtbot, gentoo_machine) -> None:
    daemon, _lab, _tree = gentoo_machine
    client = RescueClient(daemon.path)
    states = []
    probe = client.probe()
    probe.done.connect(states.append)
    qtbot.waitUntil(lambda: bool(states), timeout=15000)
    assert states[0].reachable and states[0].supports("rescue.v1")
    assert client.wait_all()


def test_a_scan_streams_progress_and_ends_with_the_result(qtbot, gentoo_machine) -> None:
    daemon, _lab, _tree = gentoo_machine
    client = RescueClient(daemon.path)
    outcome = run(qtbot, client, "rescue.scan", probe=True)
    kinds = [event["type"] for event in outcome.events]
    assert kinds[-2:] == ["result", "completed"] and "progress" in kinds
    assert outcome.result is not None and {item["device"] for item in outcome.result["candidates"]} == {"/dev/nvme0n1p5", "/dev/sdb1"}
    assert outcome.call.job_id and outcome.call.last_seq == len(outcome.events) and outcome.call.connected
    assert outcome.failure is None and outcome.lost is None


def test_an_engine_error_is_reported_with_its_code_and_a_plain_text(qtbot, gentoo_machine) -> None:
    daemon, _lab, _tree = gentoo_machine
    client = RescueClient(daemon.path)
    outcome = run(qtbot, client, "rescue.mount", candidate="cdeadbeef", scan_id="none", dry_run=True)
    assert outcome.failure is not None and outcome.failure[0] == "rescue.not_found"
    assert "scan again" in outcome.failure[1]
    assert outcome.result is None and outcome.lost is None


def test_the_last_result_is_kept_when_an_error_follows_it(qtbot, gentoo_machine) -> None:
    daemon, lab, _tree = gentoo_machine
    client = RescueClient(daemon.path)
    scan = run(qtbot, client, "rescue.scan", probe=True).result
    assert scan is not None
    candidate = next(item for item in scan["candidates"] if item["device"] == "/dev/nvme0n1p5")
    request = {"candidate": candidate["id"], "scan_id": scan["scan_id"]}
    dry = run(qtbot, client, "rescue.mount", dry_run=True, **request).result
    assert dry is not None
    info = run(qtbot, client, "rescue.mount", plan_digest=dry["mount_plan"]["digest"], **request).result
    assert info is not None
    session = daemon.manager.session(info["session_id"])
    lab.system.fail_umount[session.root + "/efi"] = (32, "umount: target is busy.")
    lab.system.holders[session.root + "/efi"] = "                     USER        PID ACCESS COMMAND\n" + f"{session.root}/efi:      gentoo     4321 ..c.. thunar\n"
    outcome = run(qtbot, client, "rescue.unmount", session_id=info["session_id"])
    assert outcome.failure is not None and outcome.failure[0] == "rescue.busy"
    assert outcome.call.result["busy_holders"][0]["command"] == "thunar"  # the result event came first and is not lost


def test_a_cut_stream_is_lost_not_failed_and_remembers_where_it_was(qtbot, gentoo_machine, proxy_factory) -> None:
    daemon, _lab, _tree = gentoo_machine
    proxy = proxy_factory(daemon)
    proxy.cut("rescue.scan", 3)
    client = RescueClient(proxy.path)
    outcome = run(qtbot, client, "rescue.scan", probe=True)
    assert outcome.lost is not None and outcome.failure is None and outcome.result is None
    assert outcome.call.completed is None and outcome.call.last_seq == 3 and outcome.call.job_id
    # the daemon finished the scan without the window: the events can be fetched again from where the window stopped
    again = run(qtbot, client, "rescue.events", job_id=outcome.call.job_id, after_seq=outcome.call.last_seq)
    assert again.result is not None and again.result["candidates"] and again.events[0]["seq"] == 4


def test_a_daemon_that_is_not_there_is_lost_without_ever_connecting(qtbot, tmp_path, sock_dir) -> None:
    client = RescueClient(str(sock_dir / "nothing-here.sock"))
    outcome = run(qtbot, client, "rescue.scan")
    assert outcome.lost is not None and "not running" in outcome.lost and not outcome.call.connected


def test_abort_ends_a_call_that_the_daemon_never_answers(qtbot, sock_dir) -> None:
    path = str(sock_dir / "silent.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    server.settimeout(10)
    held: list[socket.socket] = []
    threading.Thread(target=lambda: held.append(server.accept()[0]), daemon=True).start()
    try:
        client = RescueClient(path)
        outcome = Outcome(client.request("rescue.scan", {}))
        qtbot.waitUntil(lambda: bool(held), timeout=5000)
        client.abort_all()
        assert client.wait_all(5000)
        qtbot.waitUntil(lambda: outcome.finished, timeout=5000)
        assert outcome.lost is None and outcome.failure is None and outcome.result is None  # an aborted call stays silent
    finally:
        for connection in held:
            connection.close()
        server.close()
