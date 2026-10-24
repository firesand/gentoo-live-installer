"""Wave 3 review fixes: a start that a repair session blocked is not 'another installation' (BUSY-1), and the text installer
believes what the job's own events say about resuming (RESUME-1)."""

from __future__ import annotations

from typing import Any

from gentoo_installer.textui.conclude import conclude, describe
from gentoo_installer.textui.console import Console
from gentoo_installer.textui.daemon import Daemon
from gentoo_installer.textui.follow import InstallFollower, Outcome, TextSink

from tests.textui.support import FakeHost, FakeInstaller, Script, completed, error, failing_job, log, stage, valid_manifest


def start_request(resume: bool = False) -> dict[str, Any]:
    return {"action": "start", "manifest": valid_manifest().to_dict(), "resume": resume}


def follow(fake: FakeInstaller, host: FakeHost, request: dict[str, Any] | None = None) -> Outcome:
    script = Script()
    console = Console(script.stdin, script.out, environ={})
    return InstallFollower(Daemon(fake.path), console, host, TextSink(console), interactive=False).run(request or start_request())


REASON = "A repair session holds /dev/sda3; unmount it first"


# --- BUSY-1 ---------------------------------------------------------------------------------------------------------------------------


def blocked_job() -> list[dict[str, Any]]:
    """What the daemon sends when the repair module's busy check refuses a start: install.busy, marked as a refusal."""
    return [error(REASON, "install.busy", "", retryable=False, blocked=True), completed(False, REASON)]


def test_a_start_blocked_by_a_repair_session_is_its_own_state(installer, host: FakeHost) -> None:
    fake = installer(jobs=[blocked_job()])
    outcome = follow(fake, host)
    assert outcome.state == "blocked" and outcome.code == "install.busy" and outcome.message == REASON


def test_a_second_start_while_a_job_runs_is_still_busy(installer, host: FakeHost) -> None:
    fake = installer()
    fake.state["status"] = "running"
    assert follow(fake, host).state == "busy"


def test_the_text_installer_says_what_blocks_it_and_does_not_offer_to_follow_a_job_that_does_not_exist(installer, host: FakeHost) -> None:
    script = Script([("What now", "1")])
    console = Console(script.stdin, script.out, environ={})
    fake = installer(jobs=[blocked_job()])
    outcome = follow(fake, host)
    assert conclude(console, host, Daemon(fake.path), outcome) == "again"
    text = script.flat
    assert "Another installation is already running" not in text and "Follow the running installation" not in text
    assert "The installation cannot start" in text and REASON in text
    assert "Go back to the summary" in text


def test_describe_a_blocked_start(installer, host: FakeHost) -> None:
    script = Script()
    describe(Console(script.stdin, script.out, environ={}), Outcome("blocked", REASON))
    assert "cannot start" in script.flat and REASON in script.flat and "nothing was started" in script.flat.lower()
    assert "disk may be" not in script.flat


# --- RESUME-1 -------------------------------------------------------------------------------------------------------------------------


def test_a_job_that_says_it_cannot_be_resumed_is_believed_over_an_earlier_jobs_leftovers(installer, host: FakeHost) -> None:
    """Job A failed after storage and left its checkpoints; job B (other settings) fails in preflight and says resumable=false."""
    fake = installer(jobs=[[stage("preflight", 0), error("The system clock is off by 2 hours", "install.failed", "preflight", resumable=False),
                            completed(False, resumable=False)]])
    fake.resumable_override = True  # what the status reads from A's files
    outcome = follow(fake, host)
    assert outcome.state == "failed" and outcome.resumable is False


def test_a_job_that_stopped_before_storage_finished_is_not_resumable_either(installer, host: FakeHost) -> None:
    fake = installer(jobs=[[stage("storage", 5), error("boom", resumable=False), completed(False)]])
    fake.resumable_override = True
    assert follow(fake, host).resumable is False


def test_the_status_is_asked_when_the_events_do_not_say(installer, host: FakeHost) -> None:
    """A service that predates the key sends neither in the error nor in the completed event."""
    fake = installer(jobs=[[stage("storage", 5), error("boom"), completed(False)]])
    fake.resumable_override = True
    assert follow(fake, host).resumable is True


def test_a_crash_of_the_service_after_storage_can_still_be_resumed(installer, host: FakeHost) -> None:
    """The service's crash path always writes resumable=false; this job finished storage, so its own checkpoints are what the status reads."""
    fake = installer(jobs=[[stage("preflight", 0), stage("storage", 2), stage("storage", 10, "completed"), stage("base", 40),
                            log("Traceback"), error("Installer crashed: boom", "install.failed", "base", resumable=False), completed(False)]])
    fake.resumable_override = True
    assert follow(fake, host).resumable is True


def test_a_resumed_run_that_skipped_storage_and_crashed_can_be_resumed_again(installer, host: FakeHost) -> None:
    fake = installer(jobs=[[stage("preflight", 0), stage("storage", 2, "skipped"), stage("kernel", 40),
                            error("Installer crashed: boom", "install.failed", "kernel", resumable=False), completed(False)]])
    fake.resumable_override = True
    assert follow(fake, host).resumable is True


def test_the_menu_after_such_a_failure_does_not_offer_to_resume(installer, host: FakeHost) -> None:
    fake = installer(jobs=[failing_job_before_storage()])
    fake.resumable_override = True
    outcome = follow(fake, host)
    script = Script([("What now", "1")])
    console = Console(script.stdin, script.out, environ={})
    assert conclude(console, host, Daemon(fake.path), outcome) == "again"
    assert "Resume the installation" not in script.text and "Go back to the summary" in script.text


def failing_job_before_storage() -> list[dict[str, Any]]:
    return [stage("preflight", 0), error("Preflight failed: no network", "install.failed", "preflight", resumable=False), completed(False, resumable=False)]


def test_a_failure_after_storage_with_resumable_true_is_unchanged(installer, host: FakeHost) -> None:
    assert follow(installer(jobs=[failing_job()]), host).resumable is True
