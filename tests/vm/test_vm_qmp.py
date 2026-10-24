import json
import socket
import threading
from pathlib import Path

import pytest

from scripts import vm_qmp
from scripts.vm_qmp import KEY_EVENT_DELAY_SECONDS, TEST_LUKS_PASSPHRASE, screendump, type_luks_passphrase


def serve_qmp(path, on_execute):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def run():
        connection, _ = listener.accept()
        with connection:
            stream = connection.makefile("rwb")
            stream.write(b'{"QMP":{"version":{}}}\r\n')
            stream.flush()
            while line := stream.readline():
                request = json.loads(line)
                result = on_execute(request)
                stream.write(json.dumps({"return": result}).encode() + b"\r\n")
                stream.flush()
                if request.get("execute") != "qmp_capabilities":
                    break
        listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_qmp_screendump_uses_output_path_and_checks_created_image(tmp_path, sock_dir):
    socket_path = sock_dir / "qmp.sock"
    output = tmp_path / "screen.ppm"
    requests = []

    def execute(request):
        requests.append(request)
        if request.get("execute") == "screendump":
            Path(request["arguments"]["filename"]).write_bytes(b"P6\n1 1\n255\n\0\0\0")
        return {}

    thread = serve_qmp(socket_path, execute)
    assert screendump(socket_path, output) == output.resolve()
    thread.join(timeout=2)
    assert output.read_bytes().startswith(b"P6")
    assert requests[1]["execute"] == "screendump"
    assert requests[1]["arguments"]["filename"] == str(output.resolve())


def test_qmp_luks_helper_paces_each_fixed_test_key_before_enter(monkeypatch, tmp_path):
    requests = []
    delays = []
    monkeypatch.setattr(vm_qmp, "execute", lambda _socket, command, arguments: requests.append((command, arguments)) or {})
    monkeypatch.setattr(vm_qmp.time, "sleep", delays.append)

    type_luks_passphrase(tmp_path / "qmp.sock", TEST_LUKS_PASSPHRASE)

    expected = ["minus" if char == "-" else char for char in TEST_LUKS_PASSPHRASE] + ["ret"]
    assert len(requests) == len(expected)
    for (command, arguments), keycode in zip(requests, expected, strict=True):
        assert command == "input-send-event"
        events = arguments["events"]
        assert len(events) == 2
        assert [event["type"] for event in events] == ["key", "key"]
        assert [event["data"]["key"]["data"] for event in events] == [keycode, keycode]
        assert [event["data"]["down"] for event in events] == [True, False]
    assert delays == [KEY_EVENT_DELAY_SECONDS] * len(TEST_LUKS_PASSPHRASE)
    assert requests[-1][1]["events"][0]["data"]["key"]["data"] == "ret"


def test_qmp_luks_helper_rejects_chars_outside_fixed_keymap(tmp_path):
    with pytest.raises(ValueError, match="fixed test"):
        type_luks_passphrase(tmp_path / "nonexistent.sock", "secret!")
