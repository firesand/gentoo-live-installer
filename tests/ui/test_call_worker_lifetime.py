"""netclient.CallWorker: a running worker is never destroyed, whatever happens to the widget that started it.

Destroying a running QThread aborts the program. It happened in a full test run (a worker deleted on its answer before
its thread had ended) and could happen to a user who leaves a page while a Wi-Fi scan runs. The stress test runs in a
subprocess, so that such an abort fails this test instead of killing the whole run.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]

STRESS = r"""
import sys, time
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget
from gentoo_installer import netclient

app = QApplication([])
answers = []
for round_ in range(150):
    owner = QWidget()
    for index in range(6):
        worker = netclient.CallWorker(lambda action, payload: (time.sleep(0.001 * (index % 3)), {"ok": True})[1], "x", {})
        worker.answered.connect(netclient.deliver(owner, lambda _a, answer: answers.append(answer)))
        worker.start()
    if round_ % 2:
        owner.deleteLater()  # the widget goes away while its calls run
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
deadline = time.monotonic() + 20
while netclient._LIVE and time.monotonic() < deadline:
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    time.sleep(0.005)
print("live", len(netclient._LIVE), "answers", len(answers))
sys.exit(0 if not netclient._LIVE else 3)
"""


def test_workers_outlive_their_widget_and_are_deleted_only_after_they_ended() -> None:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run([sys.executable, "-c", STRESS], capture_output=True, text=True, timeout=120, env=env, cwd=ROOT)
    assert result.returncode == 0, f"exit {result.returncode} (-6 is an abort):\n{result.stdout}\n{result.stderr[-2000:]}"
    live, answers = result.stdout.split()[1], int(result.stdout.split()[3])
    assert live == "0"
    assert 0 < answers < 150 * 6  # the answers of deleted widgets are dropped, the others arrive


def test_an_answer_for_a_widget_that_is_gone_is_dropped(qapp) -> None:
    from PySide6.QtWidgets import QWidget
    import shiboken6

    from gentoo_installer import netclient

    owner, got = QWidget(), []
    slot = netclient.deliver(owner, got.append)
    slot("first")
    shiboken6.delete(owner)
    slot("second")
    assert got == ["first"]
