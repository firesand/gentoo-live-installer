"""gentoo-rescue-do and the approver: the rules of the Security design, section by section, without root."""

from __future__ import annotations

import io
import json
import os
import socket
import threading
from pathlib import Path

import pytest

from gentoo_rescue_ai import approver, do_client

PASSWORD = "correct-horse-battery"
ME = os.getuid()


class FakeConsole:
    """The approver's terminal: scripted answers, a record of everything shown."""

    def __init__(self, answers: list[str | None]) -> None:
        self.answers = list(answers)
        self.shown: list[str] = []
        self.prompts: list[str] = []
        self.entered = 0
        self.left = 0

    def enter(self) -> None:
        self.entered += 1

    def leave(self) -> None:
        self.left += 1

    def show(self, text: str) -> None:
        self.shown.append(text)

    def read_secret(self, prompt: str, timeout: float) -> str | None:
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else None


class Service:
    def __init__(self, tmp: Path, sock_dir: Path, answers: list[str | None], allowed: set[int] | None = None, runner=None) -> None:
        self.console = FakeConsole(answers)
        self.log = tmp / "do.log"
        self.audit = approver.Audit(str(self.log))
        kwargs = {"runner": runner} if runner else {}
        self.approver = approver.Approver(self.console, PASSWORD, self.audit, {ME} if allowed is None else allowed, **kwargs)
        self.path = str(sock_dir / "do.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(4)
        self.stop = False
        self.thread = threading.Thread(target=self.approver.serve, args=(self.listener, lambda: self.stop), daemon=True)
        self.thread.start()

    def ask(self, argv: list[str], cwd: str | None = None) -> tuple[int, bytes, bytes, list[str]]:
        out, err, notes = io.BytesIO(), io.BytesIO(), []
        code = do_client.request(self.path, argv, cwd if cwd is not None else str(Path.home()), out, err, notes.append)
        return code, out.getvalue(), err.getvalue(), notes

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def close(self) -> None:
        self.stop = True
        self.thread.join(5)
        self.listener.close()


@pytest.fixture
def make(tmp_path: Path, sock_dir: Path):
    services: list[Service] = []

    def build(answers: list[str | None], **kwargs) -> Service:
        service = Service(tmp_path, sock_dir, answers, **kwargs)
        services.append(service)
        return service

    yield build
    for service in services:
        service.close()


def test_the_right_password_typed_on_the_console_runs_exactly_one_command(make, tmp_path) -> None:
    service = make([PASSWORD])
    code, out, err, notes = service.ask(["/bin/echo", "hello", "root"], cwd=str(tmp_path))
    assert (code, out) == (0, b"hello root\n")
    assert any("phone cannot approve" in note for note in notes)
    assert service.console.entered == 1 and service.console.left == 1
    kinds = [record["event"] for record in service.records()]
    assert kinds == ["request", "answer", "exit"]
    assert PASSWORD not in service.log.read_text()
    assert oct(service.log.stat().st_mode & 0o777) == "0o600"


def test_the_screen_shows_the_exact_argv_and_folder_and_not_the_password(make, tmp_path) -> None:
    service = make([PASSWORD])
    service.ask(["/bin/echo", "a b", "x"], cwd=str(tmp_path))
    prompt = service.console.prompts[0]
    assert "/bin/echo 'a b' x" in prompt and str(tmp_path) in prompt and "ONE command" in prompt
    assert PASSWORD not in prompt


@pytest.mark.parametrize("typed", ["wrong-password!", "", None, PASSWORD + " "])
def test_a_wrong_empty_or_missing_password_refuses_that_request_and_does_not_retry(make, tmp_path, typed) -> None:
    service = make([typed, PASSWORD])
    code, out, _, notes = service.ask(["/bin/echo", "never"], cwd=str(tmp_path))
    assert code == do_client.EXIT_REFUSED and out == b""
    assert len(service.console.prompts) == 1  # no second chance inside one request
    assert [r["event"] for r in service.records()] == ["request", "answer"]
    assert service.console.left == 1


def test_an_approval_never_carries_over_to_the_next_request(make, tmp_path) -> None:
    service = make([PASSWORD, None])
    assert service.ask(["/bin/echo", "one"], cwd=str(tmp_path))[0] == 0
    code, out, _, _ = service.ask(["/bin/echo", "two"], cwd=str(tmp_path))
    assert code == do_client.EXIT_REFUSED and out == b""
    assert len(service.console.prompts) == 2


def test_a_caller_that_is_not_rescue_ai_is_refused_without_touching_the_console(make, tmp_path) -> None:
    service = make([PASSWORD], allowed={ME + 12345})
    code, out, _, notes = service.ask(["/bin/echo", "x"], cwd=str(tmp_path))
    assert code == do_client.EXIT_REFUSED and out == b""
    assert service.console.entered == 0 and service.console.prompts == []
    assert service.records()[0]["event"] == "refused"


@pytest.mark.parametrize("bad", [{"argv": []}, {"argv": "ls", "cwd": "/"}, {"argv": ["ls", 3], "cwd": "/"}, {"argv": ["a\0b"], "cwd": "/"}, {"argv": ["ls"], "cwd": "relative"},
                                  {"argv": [""], "cwd": "/"}, {"argv": ["./x"], "cwd": "/"}, {"argv": ["ls"], "cwd": "/nonexistent-folder"}, [1, 2]])
def test_a_malformed_request_is_refused_before_anyone_is_asked(make, bad) -> None:
    service = make([PASSWORD])
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(service.path)
    conn.sendall(json.dumps(bad).encode() + b"\n")
    reply = conn.makefile().readline()
    assert json.loads(reply)["event"] == "denied"
    assert service.console.prompts == []


def test_garbage_that_is_not_json_is_refused(make) -> None:
    service = make([PASSWORD])
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(service.path)
    conn.sendall(b"\x00\xffnot json\n")
    assert json.loads(conn.makefile().readline())["event"] == "denied"


def test_a_folder_below_the_target_is_refused_even_through_a_symlink(make, tmp_path, monkeypatch) -> None:
    target = tmp_path / "mnt-gentoo"
    (target / "etc").mkdir(parents=True)
    link = tmp_path / "innocent"
    link.symlink_to(target / "etc")
    monkeypatch.setattr(approver, "TARGET_PREFIXES", (str(target),))
    service = make([PASSWORD])
    for folder in (target, target / "etc", link):
        code, _, _, notes = service.ask(["/bin/echo", "x"], cwd=str(folder))
        assert code == do_client.EXIT_REFUSED and any("target disk" in note for note in notes)
    assert service.console.prompts == []


def test_a_program_that_is_not_an_absolute_path_or_a_bare_name_is_refused(make, tmp_path) -> None:
    service = make([PASSWORD])
    for program in ("./evil", "bin/evil", "/no/such/program"):
        assert service.ask([program], cwd=str(tmp_path))[0] == do_client.EXIT_REFUSED
    assert service.console.prompts == []


def test_there_is_no_shell_and_the_environment_is_clean(make, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "leak-me")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    service = make([PASSWORD, PASSWORD])
    code, out, _, _ = service.ask(["/usr/bin/env"], cwd=str(tmp_path))
    environment = dict(line.split("=", 1) for line in out.decode().splitlines())
    assert code == 0 and environment == approver.CLEAN_ENV
    code, out, _, _ = service.ask(["/bin/echo", "$HOME; id `id` > x"], cwd=str(tmp_path))
    assert out == b"$HOME; id `id` > x\n" and not (tmp_path / "x").exists()


def test_stdin_of_the_command_is_not_connected_and_its_status_comes_back(make, tmp_path) -> None:
    service = make([PASSWORD, PASSWORD])
    assert service.ask(["/bin/sh", "-c", "exit 3"], cwd=str(tmp_path))[0] == 3
    code, out, _, _ = service.ask(["/bin/cat"], cwd=str(tmp_path))
    assert (code, out) == (0, b"")  # /dev/null, not the caller's terminal


def test_an_escape_sequence_in_the_command_cannot_redraw_the_approval_screen() -> None:
    peer = approver.Peer(7, 1000, 1000, "rescue-ai")
    argv = ["/bin/echo", "\x1b[2J\x1b[HApproved by the person\r", "e‮txt", "\x07\x00"[:1]]
    text = approver.prompt_text(peer, argv, "/home/rescue-ai\x1b[31m")
    for bad in ("\x1b", "\r", "\x07", "‮"):
        assert bad not in text.replace("\n==================== gentoo-rescue-do ====================\n", "")
    assert "\\x1b[2J" in text and "\\u202e" in text


def test_an_unlogged_request_never_runs(make, tmp_path) -> None:
    service = make([PASSWORD])
    service.approver.audit = approver.Audit(str(tmp_path / "missing" / "dir" / "ro" / "do.log"))
    os.makedirs(tmp_path / "missing", mode=0o500)
    code, out, _, notes = service.ask(["/bin/echo", "x"], cwd=str(tmp_path))
    os.chmod(tmp_path / "missing", 0o700)
    assert code == do_client.EXIT_REFUSED and out == b"" and service.console.prompts == []


def test_a_caller_that_left_before_approval_does_not_get_its_command_run(tmp_path) -> None:
    ran: list[list[str]] = []
    console = FakeConsole([PASSWORD])
    service = approver.Approver(console, PASSWORD, approver.Audit(str(tmp_path / "log")), {ME}, runner=lambda argv, cwd, send: ran.append(argv) or 0)
    server, client = socket.socketpair()
    client.sendall(json.dumps({"argv": ["/bin/echo", "x"], "cwd": str(tmp_path)}).encode() + b"\n")
    client.close()
    service.handle(server)
    assert ran == []


def test_the_password_must_be_a_private_root_file_and_long_enough(tmp_path) -> None:
    path = tmp_path / "do-password"
    path.write_text("secret-secret\n")
    path.chmod(0o600)
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):  # not owned by root
            approver.read_password(str(path))
    with pytest.raises(ValueError):
        approver.Approver(FakeConsole([]), "short", approver.Audit(str(tmp_path / "l")), {ME})
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        approver.read_password(str(path))


def test_the_socket_is_made_for_rescue_ai_only_and_replaces_a_stale_one(sock_dir) -> None:
    path = str(sock_dir / "d" / "do.sock")
    os.makedirs(os.path.dirname(path))
    Path(path).write_text("stale")
    listener = approver.make_listener(path, group="no-such-group-for-test")
    try:
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    finally:
        listener.close()


def test_the_client_says_so_when_the_approver_is_not_running(tmp_path) -> None:
    notes: list[str] = []
    assert do_client.request(str(tmp_path / "none.sock"), ["/bin/true"], "/", io.BytesIO(), io.BytesIO(), notes.append) == do_client.EXIT_UNAVAILABLE
    assert "not running" in notes[0]


def test_root_is_told_to_run_the_command_directly(monkeypatch, capsys) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert do_client.main(["ls"]) == do_client.EXIT_USAGE
    assert "root already" in capsys.readouterr().err


def test_the_vt_console_reads_a_secret_with_echo_off_and_times_out(tmp_path) -> None:
    import pty
    import termios

    master, slave = pty.openpty()
    console = approver.VtConsole(9, device=str(tmp_path / "nope"))
    console._fd = slave
    try:
        os.write(master, b"stale typing\n")
        timer = threading.Timer(0.3, lambda: os.write(master, b"hunter22\n"))
        timer.start()
        # Anything typed before the question is thrown away; the answer typed after it is returned.
        typed = console.read_secret("Password: ", 5.0)
        assert typed == "hunter22"
        assert termios.tcgetattr(slave)[3] & termios.ECHO  # the terminal is put back as it was
        assert console.read_secret("Password: ", 0.2) is None
    finally:
        os.close(master)
        os.close(slave)


def test_a_terminal_that_is_not_a_private_root_device_is_refused(tmp_path) -> None:
    device = tmp_path / "tty9"
    device.write_text("")
    with pytest.raises(OSError):
        approver.VtConsole(9, device=str(device)).secure()
