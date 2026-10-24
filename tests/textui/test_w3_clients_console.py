"""Wave 3 review fixes in the console: typed numbers that are not numbers, and input the terminal cannot decode."""

from __future__ import annotations

from typing import Any

import pytest

from gentoo_installer.textui.console import Console, Option

from tests.textui.test_console import console_for


# --- CONSOLE-1: a key like ² (left of the 1 on a French keyboard) must not end the wizard -----------------------------------------------


@pytest.mark.parametrize("typed", ["²", "³", "¹", "①", "⑩", "12²", "½"])
def test_a_menu_ignores_characters_that_look_like_digits_but_are_not_numbers(typed: str) -> None:
    console, script = console_for([("", typed), ("", "2")])
    assert console.menu([Option("one"), Option("two")]).index == 1
    assert "Please type one of the numbers" in script.text
    script.assert_finished()


@pytest.mark.parametrize("typed", ["²", "³", "¹", "①"])
def test_a_long_list_ignores_them_too(typed: str) -> None:
    console, script = console_for([("", typed), ("", "2")])
    assert console.pick(["alpha", "beta", "gamma"]) == 1
    script.assert_finished()


def test_a_number_written_with_other_decimal_digits_still_works() -> None:
    console, _ = console_for([("", "٢")])  # ARABIC-INDIC DIGIT TWO: int() reads it, so the menu does too
    assert console.menu([Option("one"), Option("two")]).index == 1


def test_a_very_long_number_is_just_a_wrong_choice() -> None:
    console, script = console_for([("", "9" * 5000), ("", "1"), ("", "9" * 5000), ("", "1")])
    assert console.menu([Option("one")]).index == 0  # int() refuses 5000 digits; that must not end the wizard
    assert console.pick(["alpha", "beta"]) == 0
    assert "Please type one of the numbers" in script.text and "Please type a number from 1 to 2" in script.text


# --- SURR-1: input the terminal's character set cannot be decoded asks again ---------------------------------------------------------


class UndecodableInput:
    """A standard input whose first line holds bytes that are not UTF-8 (strict decoding raises, as under en_US.UTF-8)."""

    def __init__(self, *lines: Any) -> None:
        self.lines = list(lines)

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        item = self.lines.pop(0)
        if item == "BAD":
            raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")
        return item + "\n"


def console_with(*lines: Any) -> tuple[Console, Any]:
    import io

    out = io.StringIO()
    return Console(UndecodableInput(*lines), out, environ={}), out


def test_a_password_with_undecodable_bytes_is_asked_again() -> None:
    console, out = console_with("BAD", "correct-horse-1")
    assert console.secret("Password") == "correct-horse-1"
    assert "could not be read as text" in out.getvalue() and "UTF-8" in out.getvalue()
    assert "correct-horse" not in out.getvalue()


def test_an_ordinary_answer_with_undecodable_bytes_is_asked_again() -> None:
    console, out = console_with("BAD", "alice")
    assert console.ask("Your user name") == "alice"
    assert "could not be read as text" in out.getvalue()


def test_a_menu_with_undecodable_bytes_asks_again() -> None:
    console, out = console_with("BAD", "1")
    assert console.menu([Option("one")]).index == 0
    assert "could not be read as text" in out.getvalue()


def test_input_that_never_decodes_ends_the_program_instead_of_looping() -> None:
    from gentoo_installer.textui.console import InputClosed

    console, out = console_with(*["BAD"] * 30)
    with pytest.raises(InputClosed):
        console.ask("Your user name")
    assert out.getvalue().count("could not be read as text") < 30


def test_a_terminal_that_sends_another_character_set_gets_its_echo_back_and_a_second_try() -> None:
    import io
    import os
    import pty
    import termios
    import threading
    import time

    master, slave = pty.openpty()
    try:
        stdin = os.fdopen(slave, "r", closefd=False, encoding="utf-8", errors="strict")  # as sys.stdin is under en_US.UTF-8
        out = io.StringIO()
        console = Console(stdin, out, environ={})
        result: list[str] = []
        thread = threading.Thread(target=lambda: result.append(console.secret("Password")), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while termios.tcgetattr(slave)[3] & termios.ECHO and time.monotonic() < deadline:
            time.sleep(0.01)
        os.write(master, b"caf\xe9-pass\n")  # Latin-1 "é"
        deadline = time.monotonic() + 5
        while "could not be read as text" not in out.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "could not be read as text" in out.getvalue()
        deadline = time.monotonic() + 5
        while termios.tcgetattr(slave)[3] & termios.ECHO and time.monotonic() < deadline:
            time.sleep(0.01)  # asked again: the echo is off for the second try
        os.write(master, b"correct-horse-1\n")
        thread.join(5)
        assert result == ["correct-horse-1"]
        assert termios.tcgetattr(slave)[3] & termios.ECHO
    finally:
        os.close(master)
        os.close(slave)
