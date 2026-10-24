from __future__ import annotations

import sys
import threading
import time
import unittest

from gentoo_installerd.runner import DryRunRunner, InstallCancelled, SubprocessRunner, format_command, redact_text, validate_argv


class RedactionTests(unittest.TestCase):
    def test_manifest_and_url_secrets_are_redacted(self) -> None:
        text = 'password swordfish {"wifi_password":"wireless"} https://user:proxysecret@example.test/path'
        clean = redact_text(text, ["swordfish"])
        self.assertNotIn("swordfish", clean)
        self.assertNotIn("wireless", clean)
        self.assertNotIn("proxysecret", clean)
        self.assertIn("<redacted>", clean)

    def test_command_quoting_is_not_shell_execution(self) -> None:
        self.assertEqual(format_command(["printf", "a b"]), "printf 'a b'")
        self.assertEqual(redact_text(format_command(["tool", "secret"]), ["secret"]), "tool <redacted>")

    def test_argv_rejects_empty_and_newlines(self) -> None:
        for argv in ([], ["ok", "bad\narg"], ["ok", ""], ["bad\x00arg"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                validate_argv(argv)

    def test_dry_runner_never_executes(self) -> None:
        logs: list[str] = []
        runner = DryRunRunner(logs.append)
        result = runner.run(["sh", "-c", "exit 99"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(runner.commands, [("sh", "-c", "exit 99")])
        self.assertIn("dry-run", logs[0])


class SubprocessRunnerTests(unittest.TestCase):
    def test_streams_stdout_and_stderr(self) -> None:
        lines: list[str] = []
        runner = SubprocessRunner(lines.append)
        result = runner.run([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"])
        self.assertIn("out", result.stdout)
        self.assertIn("err", result.stdout)
        self.assertTrue(any(line == "out" for line in lines))
        self.assertTrue(any(line == "err" for line in lines))

    def test_sensitive_stdin_is_not_logged(self) -> None:
        lines: list[str] = []
        runner = SubprocessRunner(lines.append)
        result = runner.run([sys.executable, "-c", "import sys; print(sys.stdin.read())"], stdin="my-password\n", secret=True)
        self.assertNotIn("my-password", "\n".join(lines))
        self.assertNotIn("my-password", result.stdout)

    def test_cancellation_terminates_process(self) -> None:
        cancel = threading.Event()
        runner = SubprocessRunner(cancel=cancel)
        timer = threading.Timer(0.15, cancel.set)
        timer.start()
        with self.assertRaises(InstallCancelled):
            runner.run([sys.executable, "-c", "import time; time.sleep(20)"])
        timer.join()


class CleanupRunnerTests(unittest.TestCase):
    def test_cleanup_can_execute_after_job_cancellation(self):
        cancel = threading.Event()
        cancel.set()
        runner = SubprocessRunner(cancel=cancel)
        result = runner.run_cleanup([sys.executable, "-c", "print('released')"])
        self.assertEqual(result.stdout, "released")
        self.assertTrue(cancel.is_set())

    def test_cancel_after_output_eof_still_terminates_process(self):
        cancel = threading.Event()
        runner = SubprocessRunner(cancel=cancel)
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(InstallCancelled):
                runner.run([sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(10)"])
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
