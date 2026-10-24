"""D13 (socket path hazards, one daemon per directory), D7 (daemon logging), leftover reporting and the CLI."""
from __future__ import annotations

import io
import json
import logging
import os
import signal
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd import server as server_module
from gentoo_installerd.server import (
    InstallManager,
    UnixServer,
    configure_logging,
    find_leftovers,
    main,
    serve,
    validate_socket_path,
)

from tests.backend.helpers import FakeInspector
from tests.backend.helpers import manifest as make_manifest
from tests.backend.test_server_support import DaemonCase, finishing, scripted, wait_for

FAKE_GROUP = type("Group", (), {"gr_gid": 4242})()


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="gi-"))
        self.addCleanup(__import__("shutil").rmtree, self.directory, True)


class SocketPathTests(TempCase):
    def test_shared_directories_are_refused(self) -> None:
        for path in ("/tmp/server.sock", "/run/server.sock", "/var/run/server.sock", "/var/tmp/s.sock", "/etc/s.sock", "/dev/shm/s.sock", "/server.sock", "/home/s.sock", "/mnt/s.sock"):
            with self.subTest(path=path), self.assertRaises(ValueError) as caught:
                validate_socket_path(path)
            self.assertIn("dedicated directory", str(caught.exception))

    def test_relative_paths_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            validate_socket_path("run/server.sock")

    def test_a_dedicated_directory_is_accepted(self) -> None:
        self.assertEqual(validate_socket_path(self.directory / "run" / "s.sock"), self.directory / "run" / "s.sock")
        self.assertEqual(validate_socket_path("/run/gentoo-installer/server.sock"), Path("/run/gentoo-installer/server.sock"))

    def test_a_symlink_to_a_shared_directory_is_seen_through(self) -> None:
        link = self.directory / "link"
        link.symlink_to("/tmp")
        with self.assertRaises(ValueError):
            validate_socket_path(link / "s.sock")

    def test_an_existing_regular_file_is_refused_untouched_and_its_directory_keeps_its_mode(self) -> None:
        run = self.directory / "run"
        run.mkdir(mode=0o755)
        run.chmod(0o755)
        victim = run / "server.sock"
        victim.write_text("precious")
        with patch("gentoo_installerd.server.os.chown") as chown, patch("gentoo_installerd.server.grp.getgrnam", return_value=FAKE_GROUP):
            with self.assertRaisesRegex(RuntimeError, "Refusing to replace non-socket path"):
                serve(victim, require_root=False, install_signals=False, state_dir=self.directory / "s", cache_dir=self.directory / "c")
            chown.assert_not_called()
        self.assertEqual(victim.read_text(), "precious")
        self.assertEqual(run.stat().st_mode & 0o777, 0o755)

    def test_a_forbidden_directory_is_never_taken_over(self) -> None:
        with patch("gentoo_installerd.server.os.chown") as chown, patch("gentoo_installerd.server.os.chmod"), self.assertRaises(ValueError):
            serve("/tmp/x-installer.sock", require_root=False, install_signals=False)
        chown.assert_not_called()

    def test_a_stale_socket_file_is_replaced(self) -> None:
        run = self.directory / "run"
        run.mkdir()
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(run / "s.sock"))
        stale.close()
        self.assertTrue((run / "s.sock").exists())
        validate_socket_path(run / "s.sock")  # a leftover socket is fine to replace


class SingleDaemonTests(TempCase):
    def test_a_second_daemon_on_the_same_directory_is_refused_and_the_first_keeps_working(self) -> None:
        path = self.directory / "run" / "s.sock"
        servers: list[UnixServer] = []

        class RecordingServer(UnixServer):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                servers.append(self)

        first_done = threading.Event()
        with patch("gentoo_installerd.server.UnixServer", RecordingServer), patch("gentoo_installerd.server.grp.getgrnam", return_value=FAKE_GROUP), patch("gentoo_installerd.server.os.chown"):
            first = threading.Thread(target=lambda: (serve(path, require_root=False, install_signals=False, state_dir=self.directory / "s", cache_dir=self.directory / "c"), first_done.set()), daemon=True)
            first.start()
            self.assertTrue(wait_for(lambda: servers and path.exists()))
            try:
                with self.assertRaisesRegex(RuntimeError, "Another gentoo-installerd is already serving this socket"):
                    serve(path, require_root=False, install_signals=False, state_dir=self.directory / "s2", cache_dir=self.directory / "c2")
                # the first daemon still owns the socket path and answers
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(5)
                    client.connect(str(path))
                    client.sendall(b'{"version":1,"action":"status"}\n')
                    self.assertEqual(json.loads(client.makefile("rb").readline())["data"]["status"], "idle")
            finally:
                servers[0].shutdown()
            self.assertTrue(first_done.wait(10))
        self.assertFalse(path.exists())

    def test_the_lock_is_released_when_the_daemon_stops(self) -> None:
        directory = self.directory / "run"
        directory.mkdir()
        lock = server_module.acquire_server_lock(directory)
        with self.assertRaisesRegex(RuntimeError, "Another gentoo-installerd"):
            server_module.acquire_server_lock(directory)
        os.close(lock)
        os.close(server_module.acquire_server_lock(directory))


class MainTests(TempCase):
    def run_main(self, *extra: str, geteuid: int = 0, group: object | Exception = FAKE_GROUP) -> int:
        args = ["--socket", str(self.directory / "run" / "s.sock"), "--state-dir", str(self.directory / "s"), "--cache-dir", str(self.directory / "c"), "--target-root", str(self.directory / "t"), *extra]
        getgrnam = {"side_effect": group} if isinstance(group, Exception) else {"return_value": group}
        with patch("os.geteuid", return_value=geteuid), patch("gentoo_installerd.server.grp.getgrnam", **getgrnam), patch("gentoo_installerd.server.os.chown"):
            return main(args)

    def test_a_missing_socket_group_is_logged_as_an_error_and_returns_1(self) -> None:
        with self.assertLogs("gentoo_installerd", level="INFO") as logs:
            self.assertEqual(self.run_main(group=KeyError("gentoo-installer")), 1)
        text = "\n".join(logs.output)
        self.assertIn("ERROR:gentoo_installerd.server:gentoo-installerd: Required socket access group 'gentoo-installer' does not exist", text)

    def test_the_startup_line_names_the_version_socket_group_state_and_pid(self) -> None:
        with self.assertLogs("gentoo_installerd", level="INFO") as logs:
            self.run_main(group=KeyError("gentoo-installer"))
        line = next(line for line in logs.output if "gentoo-installerd 0.1.0 socket=" in line)
        for expected in (f"socket={self.directory}/run/s.sock", "group=gentoo-installer", f"state={self.directory}/s", f"cache={self.directory}/c", f"pid={os.getpid()}"):
            self.assertIn(expected, line)

    def test_a_socket_path_in_a_shared_directory_is_an_error_not_a_traceback(self) -> None:
        with self.assertLogs("gentoo_installerd", level="ERROR") as logs:
            self.assertEqual(self.run_main("--socket", "/tmp/x.sock"), 1)
        self.assertIn("dedicated directory", "\n".join(logs.output))

    def test_a_runtime_error_returns_1(self) -> None:
        victim = self.directory / "run" / "s.sock"
        victim.parent.mkdir()
        victim.write_text("file")
        with self.assertLogs("gentoo_installerd", level="ERROR"):
            self.assertEqual(self.run_main(), 1)
        self.assertEqual(victim.read_text(), "file")

    def test_not_being_root_is_logged_and_returns_1(self) -> None:
        with self.assertLogs("gentoo_installerd", level="ERROR") as logs:
            self.assertEqual(self.run_main(geteuid=1000), 1)
        self.assertIn("must run as root", "\n".join(logs.output))

    def test_log_file_receives_the_log_lines(self) -> None:
        log_file = self.directory / "daemon.log"
        self.run_main("--log-file", str(log_file), group=KeyError("gentoo-installer"))
        text = log_file.read_text()
        self.assertIn("gentoo-installerd 0.1.0 socket=", text)
        self.assertIn("ERROR", text)
        self.assertRegex(text.splitlines()[0], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO gentoo_installerd\.server: ")

    def test_logs_go_to_stderr_by_default(self) -> None:
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            self.run_main(group=KeyError("gentoo-installer"))
        self.assertIn("Required socket access group", stderr.getvalue())

    def test_no_rescue_is_passed_on(self) -> None:
        with patch("gentoo_installerd.server.serve") as serve_mock:
            self.run_main("--no-rescue")
        self.assertIs(serve_mock.call_args.kwargs["rescue"], False)
        self.assertEqual(serve_mock.call_args.kwargs["options"].rescue_root, "/mnt/rescue")
        with patch("gentoo_installerd.server.serve") as serve_mock:
            self.run_main()
        self.assertIs(serve_mock.call_args.kwargs["rescue"], True)

    def test_logging_is_undone_after_main(self) -> None:
        package_logger = logging.getLogger("gentoo_installerd")
        before = list(package_logger.handlers), package_logger.level
        self.run_main(group=KeyError("gentoo-installer"))
        self.assertEqual((list(package_logger.handlers), package_logger.level), before)

    def test_a_cli_install_is_cancelled_by_sigterm_and_cleans_up(self) -> None:
        manifest_path = self.directory / "manifest.json"
        manifest_path.write_text(make_manifest().to_json())
        started = threading.Event()
        cleaned = threading.Event()
        real = server_module.Installer

        def factory(*args, **kwargs):
            installer = real(*args, **kwargs)
            installer.inspector = FakeInspector()
            original_cleanup = installer._cleanup

            def blocking_preflight() -> None:
                started.set()
                installer.cancel.wait(20)
                installer._check_cancel()

            installer._preflight = blocking_preflight  # type: ignore[method-assign]
            installer._cleanup = lambda: (cleaned.set(), original_cleanup())[1]  # type: ignore[method-assign]
            return installer

        def deliver() -> None:
            if started.wait(10) and callable(signal.getsignal(signal.SIGTERM)):
                os.kill(os.getpid(), signal.SIGTERM)

        thread = threading.Thread(target=deliver, daemon=True)
        before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        output = io.StringIO()
        thread.start()
        with patch.object(server_module, "Installer", factory), patch("os.geteuid", return_value=0), patch("sys.stdout", output):
            result = main(["--manifest", str(manifest_path), "--yes", "--target-root", str(self.directory / "t"), "--state-dir", str(self.directory / "s"), "--cache-dir", str(self.directory / "c")])
        thread.join(10)
        self.assertEqual(result, 1)
        self.assertTrue(cleaned.is_set())
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[-1]["type"], "completed")
        self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)), before)


class LoggingTests(TempCase):
    def test_journald_gets_priority_prefixes_on_every_line(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {"JOURNAL_STREAM": "8:12345"}), patch("sys.stderr", stderr):
            undo = configure_logging()
            try:
                logging.getLogger("gentoo_installerd.test").error("first\nsecond")
                logging.getLogger("gentoo_installerd.test").info("fine")
            finally:
                undo()
        self.assertEqual(stderr.getvalue().splitlines(), ["<3>gentoo_installerd.test: first", "<3>second", "<6>gentoo_installerd.test: fine"])

    def test_an_unwritable_log_file_is_reported_and_stderr_logging_still_works(self) -> None:
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            undo = configure_logging(self.directory / "missing-dir" / "daemon.log")
            try:
                logging.getLogger("gentoo_installerd.test").warning("still here")
            finally:
                undo()
        self.assertIn("Cannot open the log file", stderr.getvalue())
        self.assertIn("still here", stderr.getvalue())


class LeftoverTests(TempCase):
    def test_mounts_below_the_target_root_and_installer_mappings_are_reported(self) -> None:
        mountinfo = self.directory / "mountinfo"
        mountinfo.write_text(
            "36 35 98:0 / / rw - ext4 /dev/sda1 rw\n"
            "40 36 0:5 / /mnt/gentoo rw - ext4 /dev/sda3 rw\n"
            "41 40 0:6 / /mnt/gentoo/proc rw - proc proc rw\n"
            "42 40 0:7 / /mnt/gentoo/mnt/with\\040space rw - ext4 /dev/sdb1 rw\n"
            "43 36 0:8 / /mnt/gentoo-other rw - ext4 /dev/sdc1 rw\n"
            "44 36 0:9 / /mnt/rescue/r1 rw - ext4 /dev/sdd1 rw\n"
        )
        mapper = self.directory / "mapper"
        mapper.mkdir()
        for name in ("control", "gentoo-root", "live-rw", "vg0-home"):
            (mapper / name).touch()
        found = find_leftovers(Path("/mnt/gentoo"), mountinfo=mountinfo, mapper_dir=mapper)
        self.assertEqual(found, ["mounted: /mnt/gentoo", "mounted: /mnt/gentoo/proc", "mounted: /mnt/gentoo/mnt/with space", f"device-mapper device: {mapper}/gentoo-root"])

    def test_nothing_left_is_an_empty_report_and_unreadable_files_do_not_break_it(self) -> None:
        self.assertEqual(find_leftovers(Path("/mnt/gentoo"), mountinfo=self.directory / "absent", mapper_dir=self.directory / "absent-too"), [])

    def test_trailing_slash_and_nested_targets(self) -> None:
        mountinfo = self.directory / "mountinfo"
        mountinfo.write_text("40 36 0:5 / /var/tmp/target rw - ext4 /dev/sda3 rw\n41 40 0:6 / /var/tmp/target/sys rw - sysfs sysfs rw\n")
        self.assertEqual(find_leftovers(Path("/var/tmp/target/"), mountinfo=mountinfo, mapper_dir=self.directory), ["mounted: /var/tmp/target", "mounted: /var/tmp/target/sys"])


class DaemonLogTests(DaemonCase):
    def setUp(self) -> None:
        self.start_daemon()

    def test_the_daemon_log_names_the_requests_but_never_carries_the_manifest(self) -> None:
        seen: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: seen.append(record.getMessage())  # type: ignore[method-assign]
        logger = logging.getLogger("gentoo_installerd")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, previous)
        self.use_installer(scripted(lambda installer: finishing(installer)))
        self.call(self.start_request())
        self.call({"action": "status"})
        joined = "\n".join(seen)
        self.assertNotIn("user-password", joined)
        self.assertNotIn("root-password", joined)
        self.assertIn("Installation started by uid", joined)
        self.assertIn("Request start from pid", joined)


class LeftoverStatusTests(DaemonCase):
    def setUp(self) -> None:
        self.start_daemon()

    def test_status_reports_leftovers_while_idle_and_not_while_running(self) -> None:
        with patch.object(server_module, "find_leftovers", return_value=["mounted: /mnt/gentoo"]):
            self.assertEqual(self.call({"action": "status"})[0]["data"]["leftovers"], ["mounted: /mnt/gentoo"])
            release = threading.Event()
            self.use_installer(scripted(lambda installer: (release.wait(10), finishing(installer))[1]))
            starter = self.connect()
            client = starter.__enter__()
            try:
                client.sendall((json.dumps({"version": 1, **self.start_request()}) + "\n").encode())
                self.assertTrue(wait_for(lambda: self.manager.is_running()))
                self.assertEqual(self.call({"action": "status"})[0]["data"]["leftovers"], [])
            finally:
                release.set()
                starter.__exit__(None, None, None)

    def test_serve_logs_what_an_earlier_run_left_behind(self) -> None:
        path = self.directory / "run" / "s.sock"
        servers: list[UnixServer] = []

        class RecordingServer(UnixServer):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                servers.append(self)

        done = threading.Event()
        with patch("gentoo_installerd.server.UnixServer", RecordingServer), patch("gentoo_installerd.server.grp.getgrnam", return_value=FAKE_GROUP), patch("gentoo_installerd.server.os.chown"), patch.object(server_module, "find_leftovers", return_value=["mounted: /mnt/gentoo/proc"]), self.assertLogs("gentoo_installerd.server", level="INFO") as logs:
            thread = threading.Thread(target=lambda: (serve(path, require_root=False, install_signals=False, state_dir=self.directory / "s2", cache_dir=self.directory / "c2"), done.set()), daemon=True)
            thread.start()
            self.assertTrue(wait_for(lambda: servers and path.exists()))
            servers[0].shutdown()
            self.assertTrue(done.wait(10))
        text = "\n".join(logs.output)
        self.assertIn("earlier installation left behind: mounted: /mnt/gentoo/proc", text)
        self.assertIn("Listening on", text)


if __name__ == "__main__":
    unittest.main()
