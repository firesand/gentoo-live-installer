"""D11 (cancel kills the whole process tree), D12 (no hang on inherited pipes), timeouts and abnormal exits.

Every helper process is started by these tests and killed again in a cleanup, identified by
the pid files it writes, never by name.
"""
from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd import runner as runner_module
from gentoo_installerd.runner import CommandTimeout, InstallCancelled, ProcessTree, SubprocessRunner, scan_processes

# A grandchild that leaves the command's session (like portage's pid-ns-init) and sleeps.
GRANDCHILD = """
    import os, sys, time
    os.setsid()
    open(sys.argv[1], 'w').write(str(os.getpid()))
    time.sleep(120)
"""

# A process that holds on to the inherited output pipe and sleeps.
HOLDER = """
    import os, sys, time
    open(sys.argv[1], 'w').write(str(os.getpid()))
    time.sleep(120)
"""


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class ProcessCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="runner-processes-"))
        self.started: list[Path] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for pidfile in self.started:
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        shutil.rmtree(self.directory, ignore_errors=True)

    def script(self, name: str, code: str) -> Path:
        path = self.directory / name
        path.write_text(textwrap.dedent(code))
        return path

    def pidfile(self, name: str) -> Path:
        path = self.directory / name
        self.started.append(path)
        return path

    def pid_of(self, path: Path) -> int:
        self.assertTrue(wait_until(lambda: path.exists() and path.read_text().strip().isdigit()), f"{path.name} was not written")
        return int(path.read_text())

    def cancelling_sink(self, cancel: threading.Event):
        """A log sink that sets ``cancel`` as soon as the command printed 'ready'."""

        def sink(line: str) -> None:
            if line == "ready":
                cancel.set()

        return sink


class CancelKillsTheTreeTests(ProcessCase):
    def leader(self, pidfile: Path, *, ignore_sigterm: bool) -> list[str]:
        grandchild = self.script("grandchild.py", GRANDCHILD)
        leader = self.script("leader.py", f"""
            import os, signal, subprocess, sys, time
            {"signal.signal(signal.SIGTERM, signal.SIG_IGN)" if ignore_sigterm else ""}
            subprocess.Popen([sys.executable, {str(grandchild)!r}, sys.argv[1]])
            while not (os.path.exists(sys.argv[1]) and open(sys.argv[1]).read()):
                time.sleep(0.01)
            print('ready', flush=True)
            time.sleep(120)
        """)
        return [sys.executable, str(leader), str(pidfile)]

    def test_a_grandchild_in_its_own_session_dies_with_the_cancelled_command(self) -> None:
        pidfile = self.pidfile("grandchild.pid")
        cancel = threading.Event()
        runner = SubprocessRunner(self.cancelling_sink(cancel), cancel)
        started = time.monotonic()
        with self.assertRaises(InstallCancelled):
            runner.run(self.leader(pidfile, ignore_sigterm=False))
        grandchild = self.pid_of(pidfile)
        self.assertTrue(wait_until(lambda: not alive(grandchild)), "the process in the other session survived the cancel")
        self.assertLess(time.monotonic() - started, 15)

    def test_a_command_that_ignores_sigterm_is_killed_after_the_grace_period(self) -> None:
        pidfile = self.pidfile("grandchild.pid")
        cancel = threading.Event()
        runner = SubprocessRunner(self.cancelling_sink(cancel), cancel)
        started = time.monotonic()
        with patch.object(runner_module, "CANCEL_GRACE_SECONDS", 1.0), self.assertRaises(InstallCancelled):
            runner.run(self.leader(pidfile, ignore_sigterm=True))
        self.assertGreaterEqual(time.monotonic() - started, 1.0, "SIGKILL was sent before the grace period was over")
        self.assertTrue(wait_until(lambda: not alive(self.pid_of(pidfile))))

    def test_a_command_that_handles_sigterm_exits_without_sigkill(self) -> None:
        marker = self.directory / "terminated"
        script = self.script("polite.py", f"""
            import signal, sys, time
            def stop(*_):
                open({str(marker)!r}, 'w').write('term')
                sys.exit(0)
            signal.signal(signal.SIGTERM, stop)
            print('ready', flush=True)
            time.sleep(120)
        """)
        cancel = threading.Event()
        runner = SubprocessRunner(self.cancelling_sink(cancel), cancel)
        started = time.monotonic()
        with self.assertRaises(InstallCancelled):
            runner.run([sys.executable, str(script)])
        self.assertEqual(marker.read_text(), "term")
        self.assertLess(time.monotonic() - started, 10)

    def test_stragglers_are_killed_even_when_the_leader_dies_first(self) -> None:
        # The leader dies on SIGTERM at once, which re-parents the grandchild to init: the
        # tree must have been recorded before the first signal was sent.
        pidfile = self.pidfile("grandchild.pid")
        cancel = threading.Event()
        runner = SubprocessRunner(self.cancelling_sink(cancel), cancel)
        with self.assertRaises(InstallCancelled):
            runner.run(self.leader(pidfile, ignore_sigterm=False))
        self.assertTrue(wait_until(lambda: not alive(self.pid_of(pidfile))))

    def test_cancel_is_independent_of_the_leader_once_it_has_exited(self) -> None:
        # D12: the leader is gone, a descendant holds the pipe open, and cancel must still be honoured.
        pidfile = self.pidfile("holder.pid")
        holder = self.script("holder.py", HOLDER)
        launcher = self.script("launcher.py", f"""
            import subprocess, sys
            subprocess.Popen([sys.executable, {str(holder)!r}, sys.argv[1]])
            print('started', flush=True)
        """)
        cancel = threading.Event()
        runner = SubprocessRunner(cancel=cancel)
        timer = threading.Timer(0.4, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with patch.object(runner_module, "EOF_GRACE_SECONDS", 30.0), self.assertRaises(InstallCancelled):
                runner.run([sys.executable, str(launcher), str(pidfile)])
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(wait_until(lambda: not alive(self.pid_of(pidfile))))


class InheritedPipeTests(ProcessCase):
    def test_a_descendant_holding_the_pipe_does_not_hang_the_runner(self) -> None:
        pidfile = self.pidfile("holder.pid")
        holder = self.script("holder.py", HOLDER)
        launcher = self.script("launcher.py", f"""
            import subprocess, sys
            subprocess.Popen([sys.executable, {str(holder)!r}, sys.argv[1]])
            print('started', flush=True)
        """)
        lines: list[str] = []
        runner = SubprocessRunner(lines.append)
        started = time.monotonic()
        with patch.object(runner_module, "EOF_GRACE_SECONDS", 0.5):
            result = runner.run([sys.executable, str(launcher), str(pidfile)])
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(result.stdout, "started")
        self.assertTrue(any("left background processes" in line for line in lines))
        self.assertTrue(wait_until(lambda: not alive(self.pid_of(pidfile))), "the descendant keeping the pipe open was not terminated")

    def test_output_that_is_still_draining_after_the_exit_is_not_cut_off(self) -> None:
        # A slow log sink must not make the grace period swallow the end of a finished command's output.
        seen: list[str] = []

        def slow_sink(line: str) -> None:
            seen.append(line)
            if line.isdigit():
                time.sleep(0.001)

        runner = SubprocessRunner(slow_sink)
        with patch.object(runner_module, "EOF_GRACE_SECONDS", 0.3):
            result = runner.run([sys.executable, "-c", "[print(i) for i in range(800)]; print('last')"])
        self.assertEqual(result.stdout.splitlines()[-1], "last")
        self.assertIn("799", seen)


class TimeoutAndAbnormalExitTests(ProcessCase):
    def test_timeout_kills_the_tree_and_reports_it(self) -> None:
        pidfile = self.pidfile("grandchild.pid")
        grandchild = self.script("grandchild.py", GRANDCHILD)
        parent = self.script("parent.py", f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, {str(grandchild)!r}, sys.argv[1]])
            time.sleep(120)
        """)
        runner = SubprocessRunner()
        started = time.monotonic()
        with patch.object(runner_module, "CANCEL_GRACE_SECONDS", 1.0), self.assertRaises(CommandTimeout) as caught:
            runner.run([sys.executable, str(parent), str(pidfile)], timeout=0.8)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(caught.exception.returncode, 124)
        self.assertIn("timed out", str(caught.exception))
        self.assertTrue(wait_until(lambda: not alive(self.pid_of(pidfile))))

    def test_a_raising_log_sink_still_kills_the_whole_group(self) -> None:
        pidfile = self.pidfile("grandchild.pid")
        grandchild = self.script("grandchild.py", GRANDCHILD)
        leader = self.script("leader.py", f"""
            import os, subprocess, sys, time
            subprocess.Popen([sys.executable, {str(grandchild)!r}, sys.argv[1]])
            while not (os.path.exists(sys.argv[1]) and open(sys.argv[1]).read()):
                time.sleep(0.01)
            print('ready', flush=True)
            time.sleep(120)
        """)

        def broken_sink(line: str) -> None:
            if line == "ready":
                raise OSError(28, "No space left on device")

        runner = SubprocessRunner(broken_sink)
        started = time.monotonic()
        with self.assertRaises(OSError):
            runner.run([sys.executable, str(leader), str(pidfile)])
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(wait_until(lambda: not alive(self.pid_of(pidfile))))

    def test_cleanup_commands_ignore_the_cancel_flag(self) -> None:
        cancel = threading.Event()
        cancel.set()
        result = SubprocessRunner(cancel=cancel).run_cleanup([sys.executable, "-c", "print('ok')"], timeout=20)
        self.assertEqual(result.stdout, "ok")


class ProcessTreeTests(unittest.TestCase):
    def fake_proc(self, entries: dict[int, tuple[int, int, int, str]]) -> Path:
        root = Path(tempfile.mkdtemp(prefix="fake-proc-"))
        self.addCleanup(shutil.rmtree, root, True)
        for pid, (ppid, pgrp, session, state) in entries.items():
            (root / str(pid)).mkdir()
            fields = [state, str(ppid), str(pgrp), str(session)] + ["0"] * 15 + [str(pid * 10)]
            (root / str(pid) / "stat").write_text(f"{pid} (odd ) name) " + " ".join(fields) + "\n")
        return root

    def test_the_tree_follows_parents_groups_and_sessions(self) -> None:
        root = self.fake_proc({
            100: (1, 100, 100, "S"),    # the command
            101: (100, 100, 100, "S"),  # child in the group
            102: (101, 102, 102, "S"),  # grandchild in a new session (pid-ns-init)
            103: (102, 102, 102, "S"),  # its child
            104: (1, 100, 100, "S"),    # re-parented straggler still in the group
            105: (1, 105, 105, "S"),    # unrelated
            106: (100, 100, 100, "Z"),  # zombie child
        })
        tree = ProcessTree(100, proc_root=root)
        self.assertEqual(sorted(tree._members), [100, 101, 102, 103, 104, 106])
        self.assertEqual(sorted(tree.alive()), [100, 101, 102, 103, 104])
        self.assertNotIn(105, tree._members)

    def test_scan_parses_command_names_with_spaces_and_parentheses(self) -> None:
        root = self.fake_proc({7: (1, 7, 7, "R")})
        table = scan_processes(root)
        self.assertEqual((table[7].ppid, table[7].pgrp, table[7].session, table[7].state), (1, 7, 7, "R"))

    def test_a_reused_pid_is_not_mistaken_for_a_member(self) -> None:
        root = self.fake_proc({100: (1, 100, 100, "S"), 101: (100, 100, 100, "S")})
        tree = ProcessTree(100, proc_root=root)
        self.assertIn(101, tree._members)
        # pid 101 exits and the number is reused by an unrelated process with another start time
        (root / "101" / "stat").write_text("101 (new) S 1 101 101 " + " ".join(["0"] * 15) + " 999999\n")
        tree.refresh()
        self.assertNotIn(101, tree.alive())


if __name__ == "__main__":
    unittest.main()
