"""After the installation: restart, resume, the log, starting over."""

from __future__ import annotations

import stat

from gentoo_installer.textui.conclude import conclude, describe, tell_how_to_resume
from gentoo_installer.textui.console import Console
from gentoo_installer.textui.daemon import Daemon
from gentoo_installer.textui.follow import Outcome

from tests.textui.support import (
    FakeHost, FakeInstaller, Script, failing_job, good_job, inspect_reply, run_main, walk_to_review,
)

VDA = {"/dev/vda": "VDA-SERIAL-0001"}


def serial_installer(installer, **kwargs) -> FakeInstaller:
    return installer(inspect_reply("qemu-q35-virtio", serials=VDA), **kwargs)


# --- through the whole program ---------------------------------------------------------------------------------------------------


def test_a_failed_installation_can_be_resumed_without_erasing_the_disk_again(installer, host: FakeHost) -> None:
    fake = serial_installer(installer, jobs=[failing_job(), good_job()])
    script = Script([
        *walk_to_review(), ("Step 9 of 9", "/dev/vda"),
        ("The installation stopped while: Installing the base system.", "1"),  # Resume the installation
        ("The installation finished successfully.", "4"),  # Quit
    ])
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 0
    assert fake.resumes == [False, True] and fake.manifests[0] == fake.manifests[1]  # the same settings, so the service accepts the resume
    assert "Resume the installation" in script.text and "emerge failed: dev-libs/broken-1.2 could not be built" in script.text
    assert "The installation log is on this computer: /var/lib/gentoo-installer/install.log" in script.flat
    assert not host.called("reboot")


def test_the_log_can_be_read_and_saved_after_a_failure(installer, host: FakeHost) -> None:
    fake = serial_installer(installer, jobs=[failing_job()])
    script = Script([
        *walk_to_review(), ("Step 9 of 9", "/dev/vda"),
        ("What now", "3"), ("What now", "4"), ("What now", "5"),  # show the log, save it, quit
    ])
    result = run_main(["--socket", fake.path], script, host)
    assert result.code == 1
    assert "The last 2 lines of the installation log:" in script.text and "last line" in script.text
    saved = sorted(host.home.glob("gentoo-installer-*.log"))
    assert len(saved) == 1 and stat.S_IMODE(saved[0].stat().st_mode) == 0o600
    assert saved[0].read_text() == "\n".join(fake.log_lines) + "\n"
    assert "Saved 2 log lines to" in script.flat and saved[0].name in script.text.replace("\n", "")


def test_a_system_that_can_start_is_offered_a_restart_even_though_something_failed(installer, host: FakeHost) -> None:
    fake = serial_installer(installer, jobs=[failing_job(bootable=True)])
    script = Script([*walk_to_review(), ("Step 9 of 9", "/dev/vda"), ("far enough to start", "2")])  # 1 resume, 2 restart
    result = run_main(["--socket", fake.path], script, host)
    assert result.code == 1 and host.called("reboot") == [("reboot",)]
    assert "Restart the computer now" in script.text and "The new system was installed far enough to start." in script.flat


def test_starting_over_goes_back_to_the_summary_and_the_typed_confirmation(installer, host: FakeHost) -> None:
    fake = serial_installer(installer, jobs=[failing_job(), failing_job(resumable=False), ])
    script = Script([
        *walk_to_review(), ("Step 9 of 9", "/dev/vda"),
        ("The installation stopped", "2"),  # go back to the summary
        ("An earlier installation stopped after the disk was prepared", "n"),
        ("type the disk name exactly", "/dev/vda"),
        ("The installation stopped", "4"),  # quit
    ])
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 1 and fake.resumes == [False, False]


def test_an_earlier_stopped_installation_can_be_resumed_from_the_summary(installer, host: FakeHost) -> None:
    fake = serial_installer(installer, jobs=[failing_job(), good_job()])
    script = Script([
        *walk_to_review(), ("Step 9 of 9", "/dev/vda"),
        ("The installation stopped", "2"),
        ("Resume it?", "y"),
        ("continue the installation on /dev/vda (nothing is erased again)", "/dev/vda"),
        ("The installation finished", "4"),
    ])
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 0 and fake.resumes == [False, True]


def test_a_restart_that_fails_is_explained_and_the_menu_stays(installer, host: FakeHost) -> None:
    host.reboot_result = (False, "Failed to connect to bus")
    fake = serial_installer(installer)
    script = Script([*walk_to_review(), ("Step 9 of 9", "/dev/vda"), ("What now", ""), ("could not be restarted automatically", "4")])
    result = run_main(["--socket", fake.path], script, host)
    assert result.code == 0 and "Type: reboot" in script.flat and "Failed to connect to bus" in script.flat


def test_the_end_of_the_input_at_the_last_menu_keeps_the_result_of_the_installation(installer, host: FakeHost) -> None:
    from tests.textui.support import END

    fake = serial_installer(installer)
    result = run_main(["--socket", fake.path], Script([*walk_to_review(), ("Step 9 of 9", "/dev/vda"), ("What now", END)]), host)
    assert result.code == 0
    failed = serial_installer(installer, jobs=[failing_job()])
    assert run_main(["--socket", failed.path], Script([*walk_to_review(), ("Step 9 of 9", "/dev/vda"), ("What now", END)]), host).code == 1


# --- conclude() on its own ---------------------------------------------------------------------------------------------------------------


def make(host: FakeHost, fake: FakeInstaller, steps=()):
    script = Script(steps)
    return Console(script.stdin, script.out, environ={}), script


def test_describe_tells_success_degraded_parts_and_warnings(installer, host: FakeHost) -> None:
    console, script = make(host, installer())
    describe(console, Outcome("success", degraded=["NVIDIA driver"], warnings=["Unmount failed"]))
    assert "The installation finished successfully." in script.text and "- NVIDIA driver" in script.text and "Warning: Unmount failed" in script.text
    assert "Remove the installation medium" in script.flat


def test_describe_each_kind_of_ending(installer, host: FakeHost) -> None:
    console, script = make(host, installer())
    for state in ("cancelled", "busy", "invalid", "detached", "unreachable", "failed"):
        describe(console, Outcome(state, "the reason", stage="kernel"))
    text = script.flat
    for heading in ("was cancelled while: Installing firmware and the kernel", "Another installation is already running", "refused the settings",
                    "You stopped watching", "Lost contact with the installer service", "The installation stopped while: Installing firmware and the kernel"):
        assert heading in text, heading
    assert "gentoo-installer-text --attach" in text and text.count("the reason") == 6


def test_the_unattended_hint_for_resuming(installer, host: FakeHost) -> None:
    console, script = make(host, installer())
    tell_how_to_resume(console, Outcome("failed", resumable=False))
    assert script.text == ""
    tell_how_to_resume(console, Outcome("failed", resumable=True))
    assert "--resume" in script.text


def test_a_running_installation_can_be_followed_from_the_menu(installer, host: FakeHost) -> None:
    fake = installer()
    console, script = make(host, fake, [("What now", "1")])
    assert conclude(console, host, Daemon(fake.path), Outcome("busy", "An installation is already running")) == "attach"
    console, script = make(host, fake, [("What now", "1")])
    assert conclude(console, host, Daemon(fake.path), Outcome("detached")) == "attach"


def test_resume_is_only_offered_when_the_settings_are_known(installer, host: FakeHost) -> None:
    fake = installer()
    console, script = make(host, fake, [("What now", "q"), ("Quit the installer?", "y")])
    assert conclude(console, host, Daemon(fake.path), Outcome("failed", "x", resumable=True), can_resume=False) == "done"
    assert "Resume the installation" not in script.text
    console, script = make(host, fake, [("What now", "1")])
    assert conclude(console, host, Daemon(fake.path), Outcome("failed", "x", resumable=True)) == "resume"


def test_an_invalid_manifest_only_offers_to_go_back(installer, host: FakeHost) -> None:
    fake = installer()
    console, script = make(host, fake, [("What now", "1")])
    assert conclude(console, host, Daemon(fake.path), Outcome("invalid", "bad")) == "again"
    assert "Starting over erases the disk again" not in script.text


def test_the_log_commands_survive_a_service_without_a_log(installer, host: FakeHost) -> None:
    fake = installer()
    fake.log_lines = []
    console, script = make(host, fake, [("What now", "2"), ("What now", "3"), ("What now", "q"), ("", "y")])
    conclude(console, host, Daemon(fake.path), Outcome("failed", "x"))
    assert "There is no installation log yet." in script.text and "Saved 0 log lines" in script.flat
    assert [path.read_text() for path in host.home.glob("gentoo-installer-*.log")] == [""]
