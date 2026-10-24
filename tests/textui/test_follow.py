"""Following an installation: progress, reconnecting with attach{after_seq}, Ctrl-C and the outcome."""

from __future__ import annotations

import io
import json
from typing import Any, Iterator

from gentoo_installer.textui.console import Console
from gentoo_installer.textui.daemon import Daemon
from gentoo_installer.textui.follow import DEFAULT_LOG_PATH, InstallFollower, JsonSink, Outcome, TextSink

from tests.textui.support import CTRL_C, FakeHost, FakeInstaller, Script, completed, error, failing_job, good_job, log, progress, stage, valid_manifest


def start_request(resume: bool = False) -> dict[str, Any]:
    return {"action": "start", "manifest": valid_manifest().to_dict(), "resume": resume}


def follower_for(fake: FakeInstaller, host: FakeHost, steps=(), *, interactive: bool = True, quiet_until: int = 0, daemon: Daemon | None = None):
    script = Script(steps)
    console = Console(script.stdin, script.out, environ={})
    daemon = daemon or Daemon(fake.path)
    return InstallFollower(daemon, console, host, TextSink(console), interactive=interactive, quiet_until=quiet_until), script


def actions(fake: FakeInstaller) -> list[str]:
    return [request["action"] for request in fake.requests]


# --- the picture on the screen ---------------------------------------------------------------------------------------------


def test_the_run_ends_with_a_successful_outcome(installer, host: FakeHost) -> None:
    fake = installer()
    follower, script = follower_for(fake, host)
    outcome = follower.run(start_request())
    assert outcome.ok and outcome.state == "success" and outcome.percent == 100.0 and outcome.last_seq == len(good_job())
    assert outcome.log_path == "/var/lib/gentoo-installer/install.log"
    lines = script.text.splitlines()
    assert lines[0] == "The installation has started."
    assert "[  0%] Checking the installation environment" in lines and "[ 40%] Installing the base system" in lines
    assert lines.count("[ 55%]   Package 1 of 2") == 1  # one line per package, not one per event


def test_stage_percentages_never_go_backwards_and_the_download_is_reported_in_steps(installer, host: FakeHost) -> None:
    fake = installer(jobs=[[stage("stage3", 10), progress("stage3", 11, "x", downloaded=1, total=100), progress("stage3", 12, "x", downloaded=5, total=100),
                            progress("stage3", 13, "x", downloaded=20, total=100), progress("stage3", 9, "x", downloaded=60, total=100), completed(True)]])
    follower, script = follower_for(fake, host)
    follower.run(start_request())
    assert script.text.count("Downloaded") == 3  # 1% (first), 20%, 60%: a line every ten percent
    assert "[  9%]" not in script.text and "[ 13%]   Downloaded 60 B of 100 B" in script.text


def test_log_lines_are_shortened_to_the_screen_and_errors_are_coloured_only_on_a_terminal(installer, host: FakeHost) -> None:
    fake = installer(jobs=[[log("x" * 200), log("error: it broke"), log("warning: careful"), completed(True)]])
    follower, script = follower_for(fake, host)
    follower.run(start_request())
    assert max(len(line) for line in script.text.splitlines()) <= 78 and "\x1b" not in script.text


def test_colour_is_used_for_errors_on_a_colour_terminal(installer, host: FakeHost) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    fake = installer(jobs=[[log("error: it broke"), log("fine"), completed(True)]])
    out = Tty()
    console = Console(io.StringIO(), out, environ={"TERM": "linux"})
    InstallFollower(Daemon(fake.path), console, host, TextSink(console)).run(start_request())
    assert "\x1b[31merror: it broke\x1b[0m" in out.getvalue() and "\x1b[31mfine" not in out.getvalue()


def test_a_status_warning_and_degraded_parts_end_up_in_the_outcome(installer, host: FakeHost) -> None:
    warning = {"type": "status", "message": "Unmounting failed after the installation. Run: umount -R /mnt/gentoo", "data": {"warning": True}}
    job = [stage("desktop", 80), progress("desktop", 85, "Installed with warnings", degraded=["NVIDIA driver"], bootable=True), warning,
           completed(True, degraded=["a printer driver"], warnings=["cleanup failed"])]
    fake = installer(jobs=[job])
    follower, script = follower_for(fake, host)
    outcome = follower.run(start_request())
    assert outcome.ok and outcome.degraded == ["NVIDIA driver", "a printer driver"]
    assert outcome.warnings == ["Unmounting failed after the installation. Run: umount -R /mnt/gentoo", "cleanup failed"]
    assert "Warning: Unmounting failed after the installation." in script.flat


# --- failures ------------------------------------------------------------------------------------------------------------------------


def test_a_failure_reports_the_error_the_stage_and_whether_it_can_be_resumed(installer, host: FakeHost) -> None:
    fake = installer(jobs=[failing_job()])
    follower, script = follower_for(fake, host)
    outcome = follower.run(start_request())
    assert (outcome.state, outcome.code, outcome.stage) == ("failed", "install.failed", "base")
    assert outcome.message == "emerge failed: dev-libs/broken-1.2 could not be built" and outcome.resumable and not outcome.bootable and outcome.retryable
    assert "Error: emerge failed: dev-libs/broken-1.2 could not be built (Installing the base system)" in script.flat


def test_resumable_comes_from_the_status_when_the_events_do_not_say(installer, host: FakeHost) -> None:
    fake = installer(jobs=[[stage("storage", 5), error("boom", resumable=False), completed(False)]])
    fake.resumable_override = True
    follower, _ = follower_for(fake, host)
    assert follower.run(start_request()).resumable is True


def test_a_cancelled_installation_is_not_a_failure_state(installer, host: FakeHost) -> None:
    fake = installer(jobs=[[stage("storage", 5), error("Installation cancelled", "install.cancelled", resumable=True), completed(False, "Installation cancelled", resumable=True)]])
    outcome = follower_for(fake, host)[0].run(start_request())
    assert outcome.state == "cancelled" and outcome.resumable


def test_the_data_bootable_flag_is_passed_on(installer, host: FakeHost) -> None:
    fake = installer(jobs=[failing_job(bootable=True)])
    outcome = follower_for(fake, host)[0].run(start_request())
    assert outcome.state == "failed" and outcome.bootable


def test_a_second_start_is_busy_and_a_bad_manifest_is_invalid(installer, host: FakeHost) -> None:
    fake = installer()
    fake.state["status"] = "running"
    assert follower_for(fake, host)[0].run(start_request()).state == "busy"
    fake.state["status"] = "idle"
    bad = {"action": "start", "manifest": {"storage": {"device": "not a device"}}}
    outcome = follower_for(fake, host)[0].run(bad)
    assert outcome.state == "invalid" and "Select a real target disk" in outcome.message


def test_a_service_that_closes_the_connection_without_a_word_is_a_failure(installer, host: FakeHost) -> None:
    fake = installer()
    fake.refuse_start = True
    outcome = follower_for(fake, host)[0].run(start_request())
    assert outcome.state == "failed" and outcome.code == "proto.closed"


def test_without_a_service_the_outcome_is_unreachable_after_a_few_tries(sock_dir, host: FakeHost) -> None:
    console = Console(Script().stdin, Script().out, environ={})
    follower = InstallFollower(Daemon(str(sock_dir / "nobody.sock")), console, host, TextSink(console))
    outcome = follower.run(start_request())
    assert outcome.state == "unreachable" and host.slept == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0]


# --- reconnecting ------------------------------------------------------------------------------------------------------------------------


def test_a_lost_connection_re_attaches_after_the_last_event_it_saw(installer, host: FakeHost) -> None:
    fake = installer()
    fake.drop_after = 7
    follower, script = follower_for(fake, host)
    outcome = follower.run(start_request())
    assert outcome.ok and outcome.last_seq == len(good_job())
    attaches = [request for request in fake.requests if request["action"] == "attach"]
    assert [request["after_seq"] for request in attaches] == [7]
    assert actions(fake).count("start") == 1
    assert "still running. Reconnecting" in script.flat
    assert script.text.count("Downloading and verifying Gentoo") == 1  # nothing shown twice
    assert host.slept == [1.0]


def test_it_re_attaches_again_when_the_second_connection_drops_too(installer, host: FakeHost) -> None:
    fake = installer()
    fake.drop_after = 4
    fake.attach_drops = [3]
    outcome = follower_for(fake, host)[0].run(start_request())
    assert outcome.ok
    assert [request["after_seq"] for request in fake.requests if request["action"] == "attach"] == [4, 7]


def test_a_service_that_stays_quiet_is_asked_and_the_follower_re_attaches(installer, host: FakeHost) -> None:
    fake = installer()
    fake.stall_at = 5
    fake.stall_seconds = 0.8
    daemon = Daemon(fake.path)
    daemon.read_timeout = 0.3
    follower, script = follower_for(fake, host, daemon=daemon)
    outcome = follower.run(start_request())
    assert outcome.ok and [request["after_seq"] for request in fake.requests if request["action"] == "attach"] == [5]


def test_when_the_job_is_over_one_more_attach_fetches_the_missed_end(installer, host: FakeHost) -> None:
    fake = installer()
    fake.drop_after = 3
    fake.state.update(status="completed")  # it finished while this program was away
    fake.current = []
    follower, _ = follower_for(fake, host)
    outcome = follower.run(start_request())
    assert outcome.ok  # start (3 events, dropped), the status said "not running", attach delivered the rest
    assert actions(fake).count("attach") == 1


def test_an_old_service_without_attach_is_waited_for(installer, host: FakeHost) -> None:
    fake = installer()
    fake.features = ["install.v1"]
    fake.actions = ["inspect", "status", "cancel", "start"]
    fake.drop_after = 4
    polls = {"count": 0}
    original = fake.respond

    def respond(request):
        if request.get("action") == "status":
            polls["count"] += 1
            if polls["count"] >= 3:
                fake.state.update(status="completed")
        return original(request)

    fake.respond = respond  # type: ignore[method-assign]
    follower, script = follower_for(fake, host)
    outcome = follower.run(start_request())
    assert outcome.ok and "cannot send the missed progress again" in script.flat and "attach" not in actions(fake)
    assert all(delay == 5.0 for delay in host.slept)


def test_attaching_to_a_job_that_was_never_started_is_an_error(installer, host: FakeHost) -> None:
    fake = installer()
    follower, _ = follower_for(fake, host)
    outcome = follower.run({"action": "attach", "after_seq": 0})
    assert outcome.state == "failed" and "No installation has run" in outcome.message


def test_attaching_early_shows_the_history_in_short(installer, host: FakeHost) -> None:
    fake = installer(jobs=[[stage("storage", 5), *[log(f"line {n}") for n in range(40)], progress("base", 50, "x", package=3, packages=9), stage("base", 60), completed(True)]])
    follower_for(fake, host)[0].run(start_request())  # the job exists on the service now
    follower, script = follower_for(fake, host, quiet_until=len(fake.current))
    outcome = follower.run({"action": "attach", "after_seq": 0})
    assert outcome.ok
    assert "28 earlier log lines are not shown" in script.text and "line 39" in script.text and "line 27" not in script.text
    assert "Package 3 of 9" not in script.text and "Preparing disks and filesystems" in script.text


# --- Ctrl-C while watching ----------------------------------------------------------------------------------------------------------------


class InterruptedOnce(Daemon):
    """The first stream gives ``count`` events and then the user presses Ctrl-C."""

    def __init__(self, path: str, count: int) -> None:
        super().__init__(path)
        self.count = count
        self.interrupted = False

    def stream(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        if self.interrupted:
            yield from super().stream(request)
            return
        self.interrupted = True
        for number, event in enumerate(super().stream(request)):
            if number == self.count:
                raise KeyboardInterrupt
            yield event


def test_ctrl_c_can_keep_watching(installer, host: FakeHost) -> None:
    fake = installer()
    follower, script = follower_for(fake, host, [("What do you want to do", "1")], daemon=InterruptedOnce(fake.path, 6))
    outcome = follower.run(start_request())
    assert outcome.ok and "cancel" not in actions(fake)
    assert [request["after_seq"] for request in fake.requests if request["action"] == "attach"] == [5]


def test_ctrl_c_can_stop_watching_and_leave_the_installation_running(installer, host: FakeHost) -> None:
    fake = installer()
    fake.drop_after = 5  # keep the job "running" on the service
    follower, script = follower_for(fake, host, [("What do you want to do", "2")], daemon=InterruptedOnce(fake.path, 3))
    outcome = follower.run(start_request())
    assert outcome.state == "detached" and fake.state["status"] == "running" and "cancel" not in actions(fake)


def test_ctrl_c_can_cancel_after_a_second_question(installer, host: FakeHost) -> None:
    fake = installer()
    follower, script = follower_for(fake, host, [("What do you want to do", "3"), ("Really cancel the installation?", "y")], daemon=InterruptedOnce(fake.path, 4))
    outcome = follower.run(start_request())
    assert fake.cancelled and "cancel" in actions(fake) and outcome.ok  # the fake finishes its job anyway
    assert "Cancellation requested" in script.text


def test_a_cancel_that_is_not_confirmed_changes_nothing(installer, host: FakeHost) -> None:
    fake = installer()
    follower, _ = follower_for(fake, host, [("What do you want to do", "3"), ("Really cancel", "n")], daemon=InterruptedOnce(fake.path, 4))
    follower.run(start_request())
    assert not fake.cancelled


def test_without_somebody_to_ask_ctrl_c_just_stops_watching(installer, host: FakeHost) -> None:
    fake = installer()
    follower, _ = follower_for(fake, host, interactive=False, daemon=InterruptedOnce(fake.path, 4))
    assert follower.run(start_request()).state == "detached"


def test_the_quit_note_stops_claiming_that_nothing_changed_once_the_installation_runs(installer, host: FakeHost) -> None:
    fake = installer()
    follower, script = follower_for(fake, host, [("What do you want to do", CTRL_C), ("Quit the installer?", "y")], daemon=InterruptedOnce(fake.path, 3))
    assert follower.console.quit_note == "Nothing has been changed on your disks."
    outcome = follower.run(start_request())
    assert outcome.state == "detached" and "keeps running in the background" in script.text and "gentoo-installer-text --attach" in script.text
    assert "Nothing has been changed" not in script.text
    assert "keeps running in the background" in follower.console.quit_note  # still true after leaving


def test_after_the_end_the_note_matches_the_result(installer, host: FakeHost) -> None:
    follower, _ = follower_for(installer(), host)
    follower.run(start_request())
    assert follower.console.quit_note == ""
    failing, _ = follower_for(installer(jobs=[failing_job()]), host)
    failing.run(start_request())
    assert "may be partly installed" in failing.console.quit_note


# --- the machine-readable stream --------------------------------------------------------------------------------------------------------------


def test_the_json_sink_prints_every_event_unchanged(installer, host: FakeHost) -> None:
    fake = installer()
    out = io.StringIO()
    console = Console(io.StringIO(), io.StringIO(), environ={})
    outcome = InstallFollower(Daemon(fake.path), console, host, JsonSink(out, console), interactive=False).run(start_request())
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    assert outcome.ok and events[-1]["type"] == "completed" and events[0]["type"] == "status"
    assert [event["seq"] for event in events if "seq" in event] == list(range(1, len(good_job()) + 1))
    assert any(event["type"] == "log" and event["message"].startswith(">>> /usr/bin/foo") for event in events)  # nothing is filtered


def test_an_outcome_knows_whether_it_is_ok() -> None:
    assert Outcome("success").ok and not Outcome("failed").ok and DEFAULT_LOG_PATH.endswith("install.log")


def test_build_output_cannot_drive_the_terminal(installer, host: FakeHost) -> None:
    fake = installer(jobs=[[log("\x1b[32;01m>>>\x1b[0m Emerging (1 of 1) dev-libs/foo\r\x1b[2K50% done"), error("\x1b[31mbad\x1b[0m thing"), completed(False)]])
    follower, script = follower_for(fake, host)
    outcome = follower.run(start_request())
    assert "\x1b" not in script.text and "50% done" in script.text and "Emerging (1 of 1)" not in script.text  # the \r kept only what follows it
    assert "Error: bad thing" in script.text and outcome.message == "\x1b[31mbad\x1b[0m thing"  # the Outcome keeps the raw text; printing cleans it
