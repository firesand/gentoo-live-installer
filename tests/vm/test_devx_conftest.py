"""The suite's own safety nets (tests/conftest.py), exercised in throw-away pytest runs.

Each case copies the real conftest next to a tiny test file in a temporary
project and runs a separate pytest process, so a regression in the guard shows
up as a failed assertion here instead of a hung test run.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFTEST = ROOT / "tests" / "conftest.py"

pytestmark = pytest.mark.slow


def run_pytest(project: Path, *args: str, env: dict[str, str] | None = None, timeout: int = 60):
    """Run pytest in ``project`` with a clean environment and return (result, seconds)."""
    base = {"PATH": os.environ["PATH"], "HOME": str(project), "PYTHONDONTWRITEBYTECODE": "1"}
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:timeout", *args],
        cwd=project, env={**base, **(env or {})}, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=timeout, check=False)
    return result, time.monotonic() - started


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "conftest.py").write_bytes(CONFTEST.read_bytes())
    return root


def write_test(project: Path, body: str) -> None:
    (project / "tests" / "test_sample.py").write_text(textwrap.dedent(body))


def test_a_hanging_test_fails_with_a_timeout_instead_of_blocking_the_run(project):
    write_test(project, """
        import time

        def test_hangs():
            time.sleep(30)

        def test_after_it_still_runs():
            assert True
    """)
    result, seconds = run_pytest(project, "--timeout=1")
    assert seconds < 20, result.stdout
    assert result.returncode == 1
    assert "Timeout: tests/test_sample.py::test_hangs ran longer than 1s" in result.stdout
    assert "1 failed, 1 passed" in result.stdout


def test_timeout_marker_and_ini_key_are_honoured(project):
    (project / "pytest.ini").write_text("[pytest]\ntimeout = 1\n")
    write_test(project, """
        import time
        import pytest

        def test_ini_limit_applies():
            time.sleep(30)

        @pytest.mark.timeout(0)
        def test_marker_can_disable_the_limit():
            time.sleep(1.5)
    """)
    result, seconds = run_pytest(project)
    assert seconds < 20, result.stdout
    assert "test_ini_limit_applies ran longer than 1s" in result.stdout
    assert "1 failed, 1 passed" in result.stdout


def test_a_leaked_non_daemon_listener_thread_cannot_keep_pytest_alive(project):
    """The tests/ui/test_backend.py failure mode: the accept() thread never gets a client."""
    write_test(project, """
        import socket
        import threading

        def test_listener_never_connected(tmp_path):
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(tmp_path / "s"))
            server.listen(1)
            threading.Thread(target=server.accept, name="serve").start()
            assert False, "simulated regression"
    """)
    result, seconds = run_pytest(project, timeout=40)
    assert seconds < 30, result.stdout
    assert result.returncode == 1  # the failing test's status is kept
    assert "1 failed" in result.stdout
    assert "leaving 1 non-daemon thread(s) running (serve)" in result.stdout


def test_a_clean_run_has_no_leak_warning_and_removes_its_private_temp_dir(project):
    write_test(project, """
        import tempfile
        from pathlib import Path

        def test_private_tempdir():
            path = Path(tempfile.mkdtemp())
            (path / "leaked").write_text("x")
            print("TMP", tempfile.gettempdir())

        def test_second():
            assert Path(tempfile.gettempdir()).is_dir()
    """)
    result, _ = run_pytest(project, "-s")
    assert result.returncode == 0, result.stdout
    assert "non-daemon" not in result.stdout
    tmp = next(line.split(" ", 1)[1] for line in result.stdout.splitlines() if line.startswith("TMP "))
    assert Path(tmp).name.startswith("gi-t-")
    assert not Path(tmp).exists()  # everything a test leaked is gone with the session


def test_temp_dir_never_crosses_a_symlinked_tmpdir(project, tmp_path):
    real = tmp_path / "real-tmp"
    real.mkdir()
    link = tmp_path / "link-tmp"
    link.symlink_to(real)
    write_test(project, """
        import os
        import tempfile

        def test_tempdir_is_symlink_free():
            with tempfile.TemporaryDirectory() as path:
                assert path == os.path.realpath(path), path
    """)
    result, _ = run_pytest(project, env={"TMPDIR": str(link)})
    assert result.returncode == 0, result.stdout


def test_sock_dir_is_short_even_when_the_base_temp_dir_is_deep(project, tmp_path):
    deep = tmp_path / ("d" * 40) / ("e" * 40) / ("f" * 40)
    deep.parent.mkdir(parents=True)
    write_test(project, """
        import os
        import socket

        def test_socket_fits(sock_dir, tmp_path):
            assert len(os.fsencode(tmp_path / "installer-test.sock")) > 107  # the premise
            path = sock_dir / "installer-test.sock"
            assert len(os.fsencode(path)) < 107
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.close()

        def test_listener_times_out_instead_of_blocking(unix_listener):
            path, server = unix_listener("x.sock")
            assert server.gettimeout() == 5
    """)
    result, _ = run_pytest(project, f"--basetemp={deep}")
    assert result.returncode == 0, result.stdout


def test_qt_is_offscreen_even_when_the_environment_asks_for_a_real_platform(project):
    write_test(project, """
        import os

        def test_platform():
            assert os.environ["QT_QPA_PLATFORM"] == "offscreen"
    """)
    result, _ = run_pytest(project, env={"QT_QPA_PLATFORM": "wayland"})
    assert result.returncode == 0, result.stdout
    opted_in, _ = run_pytest(project, env={"QT_QPA_PLATFORM": "wayland", "GENTOO_TEST_QT_PLATFORM": "xcb"})
    assert "AssertionError" in opted_in.stdout  # the override was honoured, not offscreen


def test_the_backend_worker_regression_that_hung_pytest_now_ends_the_run(project):
    """tests/ui/test_backend.py with BackendWorker.run broken: its non-daemon accept() thread used to keep pytest alive forever."""
    root = Path(__file__).resolve().parents[2]
    (project / "tests" / "test_backend.py").write_bytes((root / "tests" / "ui" / "test_backend.py").read_bytes())
    driver = (
        "from gentoo_installer.backend import BackendWorker\n"
        "BackendWorker.run = lambda self: None\n"   # the simulated regression: the client never connects
        "import pytest\n"
        "raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', '-p', 'no:timeout',\n"
        "                               'tests/test_backend.py::test_backend_worker_sends_manifest_and_reads_events']))\n")
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", driver], cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=60, check=False,
                            env={"PATH": os.environ["PATH"], "HOME": str(project), "PYTHONDONTWRITEBYTECODE": "1",
                                 "PYTHONPATH": str(root / "src")})
    assert time.monotonic() - started < 40, result.stdout
    assert result.returncode == 1 and "1 failed" in result.stdout, result.stdout
    assert "non-daemon thread" in result.stdout
