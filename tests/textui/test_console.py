"""Console: prompts, colour rules, wrapping, Ctrl-C and back navigation."""

from __future__ import annotations

import io
import os
import pty
import threading
import time

import pytest

from gentoo_installer.textui.console import Console, GoBack, InputClosed, Option, Quit

from tests.textui.support import CTRL_C, END, Script


class FakeTerminal(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def console_for(steps, *, environ=None, **kwargs) -> tuple[Console, Script]:
    script = Script(steps)
    return Console(script.stdin, script.out, environ=environ or {}, **kwargs), script


# --- colour -----------------------------------------------------------------------------------------------


def test_colour_is_used_only_on_a_terminal_without_no_color() -> None:
    tty = Console(io.StringIO(), FakeTerminal(), environ={"TERM": "linux"})
    assert tty.color and tty.style("x", "red") == "\x1b[31mx\x1b[0m"
    assert Console(io.StringIO(), FakeTerminal(), environ={"TERM": "linux", "NO_COLOR": "1"}).style("x", "red") == "x"
    assert Console(io.StringIO(), FakeTerminal(), environ={"TERM": "dumb"}).style("x", "bold") == "x"
    assert Console(io.StringIO(), FakeTerminal(), environ={}).style("x", "bold") == "x"  # a terminal that does not say what it is
    assert Console(io.StringIO(), io.StringIO(), environ={"TERM": "linux"}).style("x", "bold") == "x"  # not a terminal
    assert Console(io.StringIO(), FakeTerminal(), environ={}, color=False).style("x", "bold") == "x"


def test_an_empty_no_color_variable_does_not_switch_colour_off() -> None:
    assert Console(io.StringIO(), FakeTerminal(), environ={"TERM": "linux", "NO_COLOR": ""}).color


def test_no_escape_sequences_leave_the_console_without_colour() -> None:
    console, script = console_for([])
    console.heading("Title")
    console.error("bad")
    console.warn("careful")
    console.good("fine")
    assert "\x1b" not in script.text


# --- layout --------------------------------------------------------------------------------------------------


def test_text_is_wrapped_to_78_columns_on_an_80_column_screen() -> None:
    console, script = console_for([], environ={"COLUMNS": "80", "LINES": "24"})
    console.para("word " * 60)
    console.warn("longer " * 30)
    console.kv("Disk", "a very long value " * 10)
    lines = script.text.splitlines()
    assert lines and max(len(line) for line in lines) <= 78


def test_a_narrow_screen_still_gets_readable_lines() -> None:
    console, script = console_for([], environ={"COLUMNS": "30"})
    console.para("word " * 30)
    assert max(len(line) for line in script.text.splitlines()) <= 40


def test_menus_fit_80_columns() -> None:
    console, script = console_for([("", "1")], environ={"COLUMNS": "80"})
    console.menu([Option("A fairly long option label " * 3, "and a detail text " * 8)], commands={"r": "scan for disks again", "n": "connect to Wi-Fi"})
    assert max(len(line) for line in script.text.splitlines()) <= 78


def test_a_stream_that_cannot_show_a_character_gets_a_question_mark() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    console = Console(io.StringIO(), stream, environ={})
    console.say("Windows partition “Déjà vu”…")
    stream.flush()
    text = raw.getvalue().decode("ascii")
    assert text.startswith('Windows partition "D?j? vu"...')


# --- ask ------------------------------------------------------------------------------------------------------


def test_ask_returns_the_default_on_enter_and_the_text_otherwise() -> None:
    console, script = console_for([("Hostname [gentoo]", ""), ("", "  mybox  ")])
    assert console.ask("Hostname", default="gentoo") == "gentoo"
    assert console.ask("Hostname", default="gentoo") == "mybox"
    script.assert_finished()


def test_ask_asks_again_until_the_answer_is_valid() -> None:
    console, script = console_for([("", "bad name"), ("", "good-name")])
    assert console.ask("Name", validate=lambda text: "No spaces, please." if " " in text else "") == "good-name"
    assert "Warning: No spaces, please." in script.text


def test_ask_does_not_accept_an_empty_answer_without_a_default() -> None:
    console, script = console_for([("", ""), ("", "x")])
    assert console.ask("Name") == "x"
    assert "Please type an answer" in script.text


def test_ask_can_allow_an_empty_answer() -> None:
    console, _ = console_for([("", "")])
    assert console.ask("Optional", allow_empty=True) == ""


def test_less_than_goes_back_and_question_mark_explains() -> None:
    console, script = console_for([("", "?"), ("", "<")])
    with pytest.raises(GoBack):
        console.ask("Name", help="Letters only.")
    assert "Letters only." in script.text


def test_back_can_be_switched_off_for_the_first_question() -> None:
    console, _ = console_for([("", "<")])
    assert console.ask("Name", back=False) == "<"


# --- secret -----------------------------------------------------------------------------------------------------


def test_a_secret_is_never_echoed_into_the_transcript_and_keeps_its_spaces() -> None:
    console, script = console_for([("", " my secret pass ")])
    assert console.secret("Password", hint="Use at least 8 characters.") == " my secret pass "
    assert "secret" not in script.text and "Use at least 8 characters. Nothing is shown while you type." in script.text
    assert "Password: " in script.text  # the prompt itself is short


def test_secret_less_than_goes_back() -> None:
    console, _ = console_for([("", "<")])
    with pytest.raises(GoBack):
        console.secret("Password")


def test_a_terminal_password_is_read_with_the_echo_switched_off() -> None:
    import termios

    master, slave = pty.openpty()
    try:
        stdin = os.fdopen(slave, "r", closefd=False)
        console = Console(stdin, io.StringIO(), environ={})
        assert console.tty_in
        result: list[str] = []
        thread = threading.Thread(target=lambda: result.append(console.secret("Password")), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while termios.tcgetattr(slave)[3] & termios.ECHO and time.monotonic() < deadline:
            time.sleep(0.01)  # the program switches the echo off before it waits for the typing
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        os.write(master, b"hunter2 secret\n")
        thread.join(5)
        assert result == ["hunter2 secret"]
        assert termios.tcgetattr(slave)[3] & termios.ECHO  # and back on afterwards
        os.set_blocking(master, False)
        try:
            echoed = os.read(master, 1000)
        except BlockingIOError:
            echoed = b""
        assert b"hunter2" not in echoed  # the terminal did not show what was typed
    finally:
        os.close(master)
        os.close(slave)


# --- confirm / menu / pick ----------------------------------------------------------------------------------------


def test_confirm_understands_yes_no_default_and_back() -> None:
    console, _ = console_for([("[Y/n]", ""), ("", "n"), ("", "maybe"), ("", "YES"), ("", "b")])
    assert console.confirm("Go on?", default=True) is True
    assert console.confirm("Go on?", default=True) is False
    assert console.confirm("Go on?") is True  # "maybe" is not an answer; "YES" is
    with pytest.raises(GoBack):
        console.confirm("Go on?")


def test_menu_returns_numbers_and_commands_and_rejects_other_input() -> None:
    console, script = console_for([("", "7"), ("", "2"), ("", "r"), ("[1]", "")])
    options = [Option("one"), Option("two", "details")]
    assert console.menu(options).index == 1
    selection = console.menu(options, commands={"r": "scan again"})
    assert selection.command == "r" and selection.index == -1
    assert console.menu(options, default=0).index == 0
    assert "Please type one of the numbers" in script.text and "r = scan again" in script.text


def test_menu_back_and_quit() -> None:
    console, script = console_for([("", "b"), ("", "q"), ("", "n"), ("", "1"), ("", "q"), ("", "y")])
    options = [Option("one")]
    with pytest.raises(GoBack):
        console.menu(options)
    assert console.menu(options).index == 0  # q, "no", then 1
    with pytest.raises(Quit):
        console.menu(options)
    assert "Quit the installer?" in script.text and "Nothing has been changed on your disks." in script.text


def test_pick_by_number_search_and_page() -> None:
    items = [f"Entry {n:02d}" for n in range(1, 61)] + ["Special case"]
    console, script = console_for([("", "n"), ("", "1"), ("", "special"), ("", "entry 5"), ("", "3")])
    first = console.pick(items)
    assert items[first] == "Entry 27"  # page 2, entry 1: a page holds 26 entries (two columns of 13) on a 24-row screen
    assert items[console.pick(items)] == "Special case"  # a single match is taken
    assert items[console.pick(items)] == "Entry 52"  # "entry 5" matches Entry 50 to 59; number 3 of that list
    assert "next page" in script.text and "10 entries match" in script.text


def test_pick_uses_the_default_and_reports_a_search_without_hits() -> None:
    console, script = console_for([("", "zzz"), ("", "")])
    assert console.pick(["alpha", "beta"], default=1) == 1
    assert 'Nothing matches "zzz"' in script.text


def test_pick_back() -> None:
    console, _ = console_for([("", "<")])
    with pytest.raises(GoBack):
        console.pick(["a", "b"])


def test_pause_goes_back_or_continues() -> None:
    console, _ = console_for([("Press Enter", ""), ("", "b")])
    console.pause()
    with pytest.raises(GoBack):
        console.pause()


# --- Ctrl-C and the end of the input ----------------------------------------------------------------------------------


def test_ctrl_c_asks_before_quitting_and_no_keeps_the_question() -> None:
    console, script = console_for([("", CTRL_C), ("Quit the installer? [y/N]", "n"), ("Name", "alice")])
    assert console.ask("Name") == "alice"
    script.assert_finished()


def test_ctrl_c_and_yes_quits() -> None:
    console, _ = console_for([("", CTRL_C), ("", "y")])
    with pytest.raises(Quit):
        console.ask("Name")


def test_a_second_ctrl_c_counts_as_yes() -> None:
    console, _ = console_for([("", CTRL_C), ("", CTRL_C)])
    with pytest.raises(Quit):
        console.ask("Name")


def test_the_end_of_the_input_is_reported() -> None:
    console, _ = console_for([("", END)])
    with pytest.raises(InputClosed):
        console.ask("Name")


def test_the_quit_note_can_change_with_what_has_happened() -> None:
    console, script = console_for([("", CTRL_C), ("", "y")])
    console.quit_note = "The installation keeps running."
    with pytest.raises(Quit):
        console.ask("Name")
    assert "The installation keeps running." in script.text


# --- long output on a real terminal ----------------------------------------------------------------------------------------


def test_long_output_pauses_after_each_screenful_on_a_terminal() -> None:
    out = FakeTerminal()
    stdin = io.StringIO("\n\n\n")
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    console = Console(stdin, out, environ={"TERM": "linux"}, rows=12, color=False)
    console.show_lines([f"line {n}" for n in range(30)])  # 8 lines per screen: it pauses after 8, 16 and 24
    assert out.getvalue().count("-- more") == 3 and "line 29" in out.getvalue()


def test_output_is_not_paused_when_it_is_piped() -> None:
    console, script = console_for([])
    console.show_lines([f"line {n}" for n in range(100)])
    assert "-- more" not in script.text


# --- text from the outside -----------------------------------------------------------------------------------------------------------------------


def test_sanitize_removes_escape_sequences_and_control_characters() -> None:
    from gentoo_installer.textui.console import sanitize

    assert sanitize("\x1b[32;01m>>>\x1b[0m Emerging (1 of 2)") == ">>> Emerging (1 of 2)"
    assert sanitize("label\x1b]0;evil title\x07 after\x1b[2J\x1b[H!") == "label after!"
    assert sanitize("progress 10%\rprogress 55%") == "progress 55%"
    assert sanitize("a\tb\x00c\x7fd\x9be") == "a bcde"
    assert sanitize("first\nsecond") == "first\nsecond" and sanitize("plain text 100%") == "plain text 100%"


def test_nothing_from_a_disk_label_or_a_log_line_can_drive_the_terminal() -> None:
    console, script = console_for([])
    console.para("Partition \x1b[2Jlabelled \x1b]0;pwned\x07here")
    console.kv("Disk", "\x1b[31mred\x1b[0m disk")
    console.numbered(["one\x1b[1m", "two"])
    assert "\x1b" not in script.text and "Partition labelled here" in script.text and "red disk" in script.text
    from gentoo_installer.textui.console import clip

    assert clip("\x1b[31m" + "x" * 100, 20) == "x" * 17 + "..."
