"""Client side of docs/protocol.md: BackendWorker options and the blocking helpers."""

from __future__ import annotations

import os
import socket

import pytest

from gentoo_installer import daemon_client
from gentoo_installer.backend import BackendWorker
from gentoo_installer.manifest import InstallManifest


def status_reply(**data):
    return [{"type": "status", "data": {"status": "idle", **data}}]


# --- BackendWorker request building -------------------------------------------------


def test_default_request_is_a_plain_start_without_resume():
    worker = BackendWorker(InstallManifest(), "/nonexistent")
    request = worker.build_request()
    assert request["action"] == "start" and request["version"] == 1
    assert "resume" not in request
    assert request["manifest"]["system"]["init"] == "openrc"


def test_resume_attach_and_custom_requests():
    manifest = InstallManifest()
    assert BackendWorker(manifest, "/x", resume=True).build_request()["resume"] is True
    attach = BackendWorker(None, "/x", attach=True, after_seq=17).build_request()
    assert attach == {"version": 1, "action": "attach", "after_seq": 17}
    custom = BackendWorker(None, "/x", request={"action": "rescue.scan"}).build_request()
    assert custom == {"version": 1, "action": "rescue.scan"}


def test_start_without_manifest_reports_a_clear_error(qtbot, tmp_path):
    worker = BackendWorker(None, str(tmp_path / "none.sock"))
    errors = []
    worker.connectionFailed.connect(errors.append)
    worker.start()
    assert worker.wait(3000)
    qtbot.waitUntil(lambda: bool(errors), timeout=1000)
    assert "manifest" in errors[0]


def test_worker_streams_attach_events_until_completed(qtbot, fake_daemon):
    daemon = fake_daemon(lambda request: [
        {"type": "stage", "stage": "storage", "seq": 3, "data": {"status": "completed"}},
        {"type": "completed", "seq": 4, "data": {"success": False}},
        {"type": "log", "message": "never delivered"},
    ])
    worker = BackendWorker(None, daemon.path, attach=True, after_seq=2)
    events = []
    worker.eventReceived.connect(events.append)
    worker.start()
    assert worker.wait(5000)
    qtbot.waitUntil(lambda: len(events) == 2, timeout=2000)
    assert daemon.requests[0] == {"version": 1, "action": "attach", "after_seq": 2}
    assert [event["type"] for event in events] == ["stage", "completed"]


def test_missing_socket_explains_how_to_start_the_service(qtbot, tmp_path):
    worker = BackendWorker(InstallManifest(), str(tmp_path / "missing.sock"))
    errors = []
    worker.connectionFailed.connect(errors.append)
    worker.start()
    assert worker.wait(5000)
    qtbot.waitUntil(lambda: bool(errors), timeout=1000)
    assert "rc-service gentoo-installerd" in errors[0]
    assert "[Errno" not in errors[0]


# --- cancel ----------------------------------------------------------------------------


def cancel_with(qtbot, fake_daemon, handler, **kwargs):
    daemon = fake_daemon(handler)
    worker = BackendWorker(InstallManifest(), daemon.path, **kwargs)
    with qtbot.waitSignal(worker.cancelAcknowledged, timeout=5000) as blocker:
        worker.cancel()
    return daemon, blocker.args


def test_cancel_reads_the_daemon_reply(qtbot, fake_daemon):
    daemon, args = cancel_with(qtbot, fake_daemon, lambda request: [
        {"type": "status", "message": "Cancellation requested", "data": {"status": "cancelling"}}])
    assert args == [True, "Cancellation requested"]
    assert daemon.requests == [{"version": 1, "action": "cancel"}]


def test_cancel_refusal_is_reported_with_the_daemon_message(qtbot, fake_daemon):
    _, args = cancel_with(qtbot, fake_daemon, lambda request: [
        {"type": "error", "error": "No installation is running", "data": {"code": "install.not_running"}}])
    assert args[0] is False
    assert args[1] == "No installation is running"


def test_cancel_without_reply_is_not_acknowledged(qtbot, fake_daemon):
    _, args = cancel_with(qtbot, fake_daemon, lambda request: [])
    assert args[0] is False
    assert "without confirming" in args[1]


def test_cancel_request_can_be_replaced(qtbot, fake_daemon):
    daemon, _ = cancel_with(
        qtbot, fake_daemon, lambda request: status_reply(), cancel_request={"action": "rescue.cancel", "session": "s1"})
    assert daemon.requests == [{"version": 1, "action": "rescue.cancel", "session": "s1"}]


def test_cancel_with_a_dead_socket_is_reported(qtbot, tmp_path):
    worker = BackendWorker(InstallManifest(), str(tmp_path / "gone.sock"))
    with qtbot.waitSignal(worker.cancelAcknowledged, timeout=5000) as blocker:
        worker.cancel()
    assert blocker.args[0] is False
    assert "installer service" in blocker.args[1]


# --- ping -------------------------------------------------------------------------------


def test_ping_a_missing_socket_is_not_running(tmp_path):
    state = daemon_client.ping(str(tmp_path / "missing.sock"))
    assert state.kind == "not-running"
    assert "rc-service gentoo-installerd" in state.message
    assert not state.reachable


def test_ping_a_stale_socket(tmp_path):
    path = tmp_path / "stale.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.close()  # the socket file stays, nothing listens
    state = daemon_client.ping(str(path))
    assert state.kind == "stale"
    assert "restart" in state.message.lower()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_ping_without_permission(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "server.sock").touch()
    locked.chmod(0)
    try:
        state = daemon_client.ping(str(locked / "server.sock"))
    finally:
        locked.chmod(0o700)
    assert state.kind == "no-access"
    assert "gentoo-installer group" in state.message


def test_ping_a_live_daemon_returns_its_status(fake_daemon):
    daemon = fake_daemon(lambda request: status_reply(status="running", stage="packages", seq=42, resumable=True))
    state = daemon_client.ping(daemon.path)
    assert state.kind == "running" and state.reachable
    assert state.job_running
    assert state.status["stage"] == "packages"


def test_capabilities_of_an_old_daemon_are_empty_not_an_error(fake_daemon):
    daemon = fake_daemon(lambda request: [
        {"type": "error", "error": "Action must be inspect, start, status, or cancel", "message": "x"}])
    assert daemon_client.capabilities(daemon.path) == (frozenset(), frozenset())
    state = daemon_client.probe(daemon.path)
    assert not state.supports("attach.v1")


def test_probe_reports_features_and_actions(fake_daemon):
    def handler(request):
        if request["action"] == "capabilities":
            return [{"type": "result", "data": {"protocol": 1, "features": ["install.v1", "attach.v1"], "actions": ["log", "attach"]}}]
        return status_reply()

    state = daemon_client.probe(fake_daemon(handler).path)
    assert state.supports("attach.v1") and state.supports("log")
    assert not state.supports("rescue.v1")


# --- log -----------------------------------------------------------------------------------


def test_fetch_log_collects_result_lines_in_order(fake_daemon):
    daemon = fake_daemon(lambda request: [
        {"type": "result", "data": {"lines": ["one", "two"]}},
        {"type": "result", "data": {"lines": ["three"]}},
        {"type": "completed", "data": {"success": True}},
    ])
    assert list(daemon_client.fetch_log(daemon.path, tail=3)) == ["one", "two", "three"]
    assert daemon.requests[0] == {"version": 1, "action": "log", "tail": 3}


def test_fetch_log_from_an_old_daemon_raises_a_daemon_error(fake_daemon):
    daemon = fake_daemon(lambda request: [{"type": "error", "error": "Action must be inspect, start, status, or cancel"}])
    with pytest.raises(daemon_client.DaemonError):
        list(daemon_client.fetch_log(daemon.path))


def test_request_cancel_helper(fake_daemon):
    daemon = fake_daemon(lambda request: [{"type": "status", "message": "Cancellation requested", "data": {"status": "cancelling"}}])
    assert daemon_client.request_cancel(daemon.path) == (True, "Cancellation requested")


def test_diagnostics_from_a_daemon_without_the_action_is_none(fake_daemon):
    daemon = fake_daemon(lambda request: [{"type": "error", "error": "unsupported"}])
    assert daemon_client.fetch_diagnostics(daemon.path) is None


def test_diagnostics_json_lines_are_joined(fake_daemon):
    daemon = fake_daemon(lambda request: [
        {"type": "log", "message": '{"dmesg": ["a",'},
        {"type": "log", "message": ' "b"]}'},
        {"type": "completed", "data": {"success": True}},
    ])
    assert daemon_client.fetch_diagnostics(daemon.path) == {"dmesg": ["a", "b"]}
