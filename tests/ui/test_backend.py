import json
import queue
import socket
import threading

from gentoo_installer.backend import BackendWorker
from gentoo_installer.manifest import InstallManifest
from gentoo_installerd.server import UnixServer


def test_backend_worker_sends_manifest_and_reads_events(qtbot, tmp_path):
    path = str(tmp_path / "installer.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    received = {}

    def serve():
        connection, _ = server.accept()
        request = json.loads(connection.makefile("r").readline())
        received.update(request)
        connection.sendall(b'{"type":"stage","stage":"preflight","progress":0}\n')
        connection.sendall(b'{"type":"error","message":"failed"}\n')
        connection.sendall(b'{"type":"completed","data":{"success":false},"message":"done"}\n')
        connection.close()
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    worker = BackendWorker(InstallManifest(), path)
    events = []
    worker.eventReceived.connect(events.append)
    worker.start()
    assert worker.wait(5000)
    thread.join(1)
    assert received["action"] == "start"
    assert received["manifest"]["system"]["init"] == "openrc"
    qtbot.waitUntil(lambda: len(events) == 3, timeout=2000)
    assert [event["type"] for event in events] == ["stage", "error", "completed"]


def test_cancel_uses_a_separate_control_connection_and_type_terminal(qtbot, tmp_path):
    path = str(tmp_path / "installer.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(2)
    requests = []

    def serve():
        start, _ = server.accept()
        requests.append(json.loads(start.makefile("r").readline()))
        cancel, _ = server.accept()
        requests.append(json.loads(cancel.makefile("r").readline()))
        start.sendall(b'{"type":"completed","data":{"success":false},"message":"cancelled"}\n')
        cancel.close()
        start.close()
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    worker = BackendWorker(InstallManifest(), path)
    events = []
    worker.eventReceived.connect(events.append)
    worker.start()
    qtbot.waitUntil(lambda: len(requests) == 1, timeout=2000)
    worker.cancel()
    assert worker.wait(5000)
    thread.join(1)
    assert [request["action"] for request in requests] == ["start", "cancel"]
    qtbot.waitUntil(lambda: bool(events), timeout=2000)
    assert events[-1]["type"] == "completed"


def test_cancel_reaches_real_request_handler_without_installer(qtbot, tmp_path):
    class FakeManager:
        def __init__(self):
            self.events = queue.Queue()
            self.cancelled = threading.Event()

        def start(self, manifest, resume=False):
            return self.events

        def request_cancel(self):
            self.cancelled.set()
            self.events.put(None)

    path = str(tmp_path / "installer.sock")
    manager = FakeManager()
    server = UnixServer(path, manager)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    manifest = InstallManifest()
    manifest.storage.device = "/dev/sda"
    manifest.accounts.password = "user-password"
    manifest.accounts.root_password = "root-password"
    worker = BackendWorker(manifest, path)
    events = []
    worker.eventReceived.connect(events.append)
    worker.start()
    qtbot.waitUntil(lambda: any(event.get("type") == "status" for event in events), timeout=2000)
    worker.cancel()
    qtbot.waitUntil(manager.cancelled.is_set, timeout=2000)
    assert worker.wait(5000)
    server.shutdown()
    server.server_close()
    server_thread.join(2)
