"""The interactive wizard from the first question to the running installation, with scripted input."""

from __future__ import annotations

import pytest

from gentoo_installer.manifest import InstallManifest

from tests.textui.support import CTRL_C, END, FakeHost, FakeInstaller, Script, good_job, inspect_reply, run_main

VDA = {"/dev/vda": "VDA-SERIAL-0001"}
PASSWORD = "correct-horse-1"


def to_disk_step(keyboard: str = "") -> list[tuple[str, str]]:
    """Steps 1 to 3 with the defaults, then the disk menu is on screen."""
    return [("Step 1 of 9: Keyboard", keyboard), ("Step 2 of 9: Network", ""), ("Step 3 of 9: This computer", "")]


def accounts(user: str = "alice", root_mode: str = "1") -> list[tuple[str, str]]:
    return [
        ("Step 7 of 9: Accounts", "mybox"), ("Your user name", user), ("Password for " + user, PASSWORD), ("Type it again", PASSWORD),
        ("How should the administrator", root_mode),
    ]


def region() -> list[tuple[str, str]]:
    return [("Step 8 of 9: Time zone", "5"), ("Choose your time zone", "berlin"), ("Which language", "de_DE")]


def serial_installer(installer, **kwargs) -> FakeInstaller:
    return installer(inspect_reply("qemu-q35-virtio", serials=VDA), **kwargs)


def test_the_whole_interactive_installation(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    script = Script([
        ("Step 1 of 9: Keyboard", "4"),  # German
        ("Test the layout", "yz@"), ("match the keys", ""),
        ("Step 2 of 9: Network", ""),
        ("Step 3 of 9: This computer", ""),
        ("Step 4 of 9: Disk", "1"),
        ("Step 5 of 9", "3"),  # Btrfs
        ("Encrypt the system disk", "y"), ("Passphrase", "disk-passphrase-1"), ("Type the passphrase again", "disk-passphrase-1"),
        ("Step 6 of 9", "3"),  # Xfce
        ("Which init system", "2"),  # systemd
        ("Step 7 of 9: Accounts", "mybox"), ("Your user name", "alice"), ("Password for alice", PASSWORD), ("Type it again", PASSWORD),
        ("How should the administrator", "3"),  # root login off
        ("Step 8 of 9: Time zone", "5"), ("Choose your time zone", "berlin"), ("Which language", "de_DE"),
        ("Step 9 of 9: Review and start", "/dev/vda"),
        ("The installation has started", ""),  # Enter = restart now
    ])
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 0, script.text
    sent = fake.manifests[0]
    assert fake.resumes == [False]
    assert sent["system"]["keymap"] == "de-latin1" and sent["system"]["keyboard_layout"] == "de" and sent["system"]["keyboard_variant"] == ""
    assert sent["storage"]["device"] == "/dev/vda" and sent["storage"]["device_id"] == "VDA-SERIAL-0001" and sent["storage"]["device_id_kind"] == ""
    assert sent["storage"]["mode"] == "erase" and sent["storage"]["filesystem"] == "btrfs" and sent["storage"]["firmware"] == "uefi"
    assert sent["storage"]["luks"] is True and sent["storage"]["luks_passphrase"] == "disk-passphrase-1"
    assert sent["system"]["desktop"] == "xfce" and sent["system"]["init"] == "systemd" and sent["system"]["stage_variant"] == "amd64-systemd"
    assert sent["accounts"]["hostname"] == "mybox" and sent["accounts"]["username"] == "alice" and sent["accounts"]["password"] == PASSWORD
    assert sent["accounts"]["disable_root_login"] is True
    assert sent["system"]["timezone"] == "Europe/Berlin" and sent["system"]["locale"] == "de_DE.UTF-8"
    InstallManifest.from_dict(sent).require_valid()
    assert host.called("loadkeys") == [("loadkeys", "de-latin1")] and host.called("reboot")
    assert [request["action"] for request in fake.requests].count("start") == 1
    for secret in (PASSWORD, "disk-passphrase-1"):
        assert secret not in script.text  # nothing secret is ever printed


def test_the_progress_shows_the_step_the_percentage_and_the_log(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    script = Script([
        *to_disk_step(), ("Step 4 of 9: Disk", "1"), ("Step 5 of 9", ""), ("Encrypt", ""), ("Step 6 of 9", ""), ("init system", ""),
        *accounts(), *region(), ("Step 9 of 9", "/dev/vda"), ("The installation finished", "4"),
    ])
    result = run_main(["--socket", fake.path], script, host)
    assert result.code == 0
    text = script.text
    assert "[  2%] Preparing disks and filesystems" in text and "[ 40%] Installing the base system" in text
    assert "Downloaded 200.0 MiB of 200.0 MiB" in text and "Package 2 of 2" in text
    assert ">>> Emerging (1 of 2) sys-apps/foo-1.0::gentoo" in text
    assert ">>> /usr/bin/foo" not in text and "--- /usr/share/foo" not in text  # file listings are not shown
    assert "The installation finished successfully." in text and not host.called("reboot")


def test_the_first_requests_check_the_service_and_the_computer(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    run_main(["--socket", fake.path], Script([("", END)]), host)
    assert [request["action"] for request in fake.requests[:3]] == ["status", "capabilities", "inspect"]


# --- going back -------------------------------------------------------------------------------------------------------------


def run_until_input_ends(installer, host: FakeHost, steps: list[tuple[str, object]]):
    """Run the wizard with ``steps`` followed by the end of the input; returns (script, result, fake service)."""
    fake = serial_installer(installer)
    script = Script([*steps, ("", END)])
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 130
    assert not [request for request in fake.requests if request["action"] == "start"]
    return script, result, fake


def test_the_first_step_says_there_is_nothing_before_it(installer, host: FakeHost) -> None:
    script, _, _ = run_until_input_ends(installer, host, [("Step 1 of 9: Keyboard", "b"), ("This is the first step", "")])
    assert script.text.count("Step 1 of 9: Keyboard") == 2


def test_back_goes_to_the_previous_step_from_every_early_step(installer, host: FakeHost) -> None:
    run_until_input_ends(installer, host, [
        ("Step 1 of 9: Keyboard", ""), ("Step 2 of 9: Network", "b"),
        ("Step 1 of 9: Keyboard", ""), ("Step 2 of 9: Network", ""), ("Step 3 of 9: This computer", "b"),
        ("Step 2 of 9: Network", ""), ("Step 3 of 9: This computer", ""), ("Step 4 of 9: Disk", "b"),
        ("Step 3 of 9: This computer", ""), ("Step 4 of 9: Disk", "1"), ("Step 5 of 9", "b"),
        ("Step 4 of 9: Disk", "1"), ("Step 5 of 9", ""),
    ])


def test_back_inside_a_step_goes_to_the_previous_question_of_that_step(installer, host: FakeHost) -> None:
    run_until_input_ends(installer, host, [
        *to_disk_step(), ("Step 4 of 9: Disk", "1"),
        ("Which filesystem", ""), ("Encrypt the system disk", "<"),  # back to the filesystem question, not to the disk
        ("Which filesystem", "2"), ("Encrypt the system disk", ""),
        ("Step 6 of 9", ""), ("Which init system", "b"),  # back to the desktop question, not to step 5
        ("Which desktop", "2"), ("Which init system", ""),
        ("Step 7 of 9", "mybox"), ("Your user name", "<"),  # back to the host name
        ("Computer name", "mybox"), ("Your user name", "alice"), ("Password for alice", PASSWORD), ("Type it again", "<"),  # retype the first password
        ("Password for alice", PASSWORD), ("Type it again", PASSWORD), ("How should the administrator", "b"),
        ("Keep the password", "<"), ("Your user name", "bob"), ("Keep the password", ""), ("How should the administrator", ""),
        ("Step 8 of 9", "b"),
        ("Step 7 of 9", ""), ("Your user name", ""), ("Keep the password", ""), ("How should the administrator", ""),
        ("Step 8 of 9", "5"), ("Choose your time zone", "paris"), ("Which language", "<"), ("Where are you", ""), ("Choose your time zone", ""), ("Which language", ""),
        ("Step 9 of 9", "b"), ("Step 8 of 9", ""), ("Choose your time zone", ""), ("Which language", ""),
    ])


def test_change_a_setting_from_the_review_then_walk_forward_again(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    script = Script([
        *to_disk_step(), ("Step 4 of 9: Disk", "1"), ("Step 5 of 9", ""), ("Encrypt", ""), ("Step 6 of 9", ""), ("init system", ""),
        *accounts(), *region(),
        ("Step 9 of 9", "c"), ("Which setting do you want to change", "6"),  # Desktop and init system
        ("Step 6 of 9", "4"), ("init system", ""),
        ("Step 7 of 9", ""), ("Your user name", ""), ("Keep the password", ""), ("How should the administrator", ""),
        ("Step 8 of 9", "5"), ("Choose your time zone", ""), ("Which language", ""),
        ("no desktop (text mode)", "/dev/vda"), ("The installation finished", "4"),
    ])
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 0
    assert fake.manifests[0]["system"]["desktop"] == "minimal"
    assert fake.manifests[0]["options"]["install_hwmonux"] is False and fake.manifests[0]["options"]["install_portagehub"] is False


# --- the typed confirmation -----------------------------------------------------------------------------------------------------


def walk_to_review() -> list[tuple[str, str]]:
    return [*to_disk_step(), ("Step 4 of 9: Disk", "1"), ("Step 5 of 9", ""), ("Encrypt", ""), ("Step 6 of 9", ""), ("init system", ""), *accounts(), *region()]


def test_a_wrong_confirmation_is_refused_three_times_and_nothing_starts(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    script = Script([*walk_to_review(), ("Step 9 of 9", "/dev/vdb"), ("That is not /dev/vda", "vda"), ("That is not /dev/vda", "yes"), ("Nothing has been changed", "")])
    result = run_main(["--socket", fake.path], script, host)
    assert result.code == 3
    assert "typed wrongly too often" in script.text
    assert not [request for request in fake.requests if request["action"] == "start"]


def test_the_confirmation_must_match_exactly_and_empty_input_is_just_asked_again(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    script = Script([*walk_to_review(), ("Step 9 of 9", "/dev/vda "), ("", ""), ("", " /dev/vda"), ("The installation finished", "4")])
    result = run_main(["--socket", fake.path], script, host)
    assert result.code == 0  # surrounding spaces are not part of a device name


def test_ctrl_c_during_the_questions_asks_and_then_quits_with_130(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    script = Script([("Step 1 of 9", CTRL_C), ("Quit the installer?", "n"), ("Your choice", ""), ("Step 2 of 9", CTRL_C), ("Quit the installer?", "y")])
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 130 and "Nothing has been changed on your disks." in script.text
    assert not [request for request in fake.requests if request["action"] == "start"]


def test_the_end_of_the_input_stops_the_installer(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    result = run_main(["--socket", fake.path], Script([("Step 1 of 9", ""), ("Step 2 of 9", END)]), host)
    assert result.code == 130


@pytest.mark.parametrize("answer", ["q", "quit"])
def test_q_in_a_menu_asks_before_leaving(installer, host: FakeHost, answer: str) -> None:
    fake = serial_installer(installer)
    script = Script([("Step 1 of 9", ""), ("Step 2 of 9", answer), ("Quit the installer?", "y")])
    assert run_main(["--socket", fake.path], script, host).code == 130
