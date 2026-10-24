"""Terminal input and output of the console installer.

Everything the installer prints or asks goes through ``Console``. The same code runs on a
text console, over a serial line, over SSH and, with scripted input, in the tests:

* plain numbered menus and prompts, wrapped to at most 78 columns; long output pauses after
  each screenful on a real terminal;
* ANSI colour only when standard output is a terminal, TERM is not "dumb" and NO_COLOR is not
  set (https://no-color.org); no cursor movement at all;
* ``b`` or ``<`` goes back one step, ``?`` explains the question, ``q`` and Ctrl-C ask before
  leaving the installer;
* passwords are read with the terminal echo switched off.
"""

from __future__ import annotations

import os
import sys
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence, TextIO

BACK = "<"
BACK_WORDS = frozenset({"b", "back", BACK})
QUIT_WORDS = frozenset({"q", "quit", "exit"})
HELP_WORDS = frozenset({"?", "h", "help"})
YES_WORDS = frozenset({"y", "yes"})
NO_WORDS = frozenset({"n", "no"})

NBSP = "\u00a0"  # keeps "b = back" together when a line is wrapped; printed as a plain space
# Typographic characters that the fonts of a Linux text console often lack, and what to print instead.
ASCII_FALLBACK = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2026": "...", "\u2013": "-", "\u2014": "-", "\u00b7": "-", "\u2192": "->", NBSP: " "})

SGR = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34", "cyan": "36"}


class GoBack(Exception):
    """The user asked to return to the previous question."""


class Quit(Exception):
    """The user confirmed that they want to leave the installer."""


class InputClosed(Exception):
    """Standard input ended: nobody can answer any more."""


@dataclass(frozen=True)
class Option:
    """One numbered line of a menu."""

    label: str
    detail: str = ""  # shown below the label, wrapped


@dataclass(frozen=True)
class Selection:
    """The answer to a menu: a numbered option (``index``) or a command letter (``command``)."""

    index: int = -1
    command: str = ""


def is_tty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return False


def _int_from(environ: Mapping[str, str], name: str, fallback: int) -> int:
    try:
        value = int(environ.get(name, ""))
    except ValueError:
        return fallback
    return value if value > 0 else fallback


class Console:
    """All terminal input and output in one place."""

    MAX_COLUMNS = 80

    def __init__(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        color: bool | None = None,
        width: int | None = None,
        rows: int | None = None,
    ) -> None:
        self.stdin: TextIO = stdin if stdin is not None else sys.stdin
        self.out: TextIO = stdout if stdout is not None else sys.stdout
        self.environ: Mapping[str, str] = os.environ if environ is None else environ
        self.tty_in = is_tty(self.stdin)
        self.tty_out = is_tty(self.out)
        if color is None:
            color = self.tty_out and not self.environ.get("NO_COLOR") and self.environ.get("TERM", "") != "dumb"
        self.color = bool(color)
        columns, lines = self._screen_size()
        self.width = width if width else max(40, min(columns, self.MAX_COLUMNS) - 2)
        self.rows = rows if rows else lines
        # input() gives line editing (history, arrow keys) on a real terminal when readline is loaded.
        self._line_editing = self.stdin is sys.stdin and self.out is sys.stdout and self.tty_in and self.tty_out
        # What a confirmed Ctrl-C means right now; the installer changes it once a disk is touched.
        self.quit_note = "Nothing has been changed on your disks."

    # --- the screen ---------------------------------------------------------------------

    def _screen_size(self) -> tuple[int, int]:
        columns = lines = 0
        if self.tty_out:
            try:
                size = os.get_terminal_size(self.out.fileno())
                columns, lines = size.columns, size.lines
            except (OSError, ValueError, AttributeError):
                pass  # a serial line often cannot tell
        return columns or _int_from(self.environ, "COLUMNS", 80), lines or _int_from(self.environ, "LINES", 24)

    def style(self, text: str, *names: str) -> str:
        if not self.color or not names:
            return text
        return f"\x1b[{';'.join(SGR[name] for name in names)}m{text}\x1b[0m"

    def _write(self, text: str) -> None:
        text = text.translate(ASCII_FALLBACK)
        try:
            self.out.write(text)
        except UnicodeEncodeError:  # a terminal that cannot show a character: show "?" instead of failing
            encoding = getattr(self.out, "encoding", None) or "ascii"
            self.out.write(text.encode(encoding, "replace").decode(encoding))
        self.out.flush()

    def say(self, text: str = "") -> None:
        self._write(text + "\n")

    def wrap(self, text: str, *, indent: int = 0, hanging: int | None = None) -> list[str]:
        """``text`` wrapped to the screen; every newline in it starts a new paragraph."""
        first = " " * indent
        later = " " * (indent if hanging is None else hanging)
        lines: list[str] = []
        for paragraph in text.split("\n"):
            if not paragraph.strip():
                lines.append("")
                continue
            # Never break at a hyphen: people copy commands and device names from the screen.
            lines.extend(textwrap.wrap(paragraph, self.width, initial_indent=first, subsequent_indent=later, break_on_hyphens=False) or [""])
        return lines

    def para(self, text: str, *, indent: int = 0, hanging: int | None = None) -> None:
        for line in self.wrap(text, indent=indent, hanging=hanging):
            self.say(line)

    def heading(self, text: str) -> None:
        self.say()
        self.say(self.style(text, "bold"))
        self.say(self.style("=" * min(len(text), self.width), "dim"))

    def good(self, text: str) -> None:
        self.para(self.style("OK: ", "green") + text, hanging=4)

    def note(self, text: str) -> None:
        self.para(self.style("Note: ", "cyan") + text, hanging=6)

    def warn(self, text: str) -> None:
        self.para(self.style("Warning: ", "yellow") + text, hanging=9)

    def error(self, text: str) -> None:
        self.para(self.style("Error: ", "red", "bold") + text, hanging=7)

    def kv_lines(self, label: str, value: str, *, label_width: int = 14) -> list[str]:
        """'  Disk          /dev/sda (...)' with the value wrapped under itself."""
        head = f"  {label:<{label_width}} "
        lines = textwrap.wrap(value, max(20, self.width - len(head)), break_on_hyphens=False) or [""]
        return [head + lines[0], *(" " * len(head) + line for line in lines[1:])]

    def kv(self, label: str, value: str, *, label_width: int = 14) -> None:
        for line in self.kv_lines(label, value, label_width=label_width):
            self.say(line)

    def show_lines(self, lines: Sequence[str]) -> None:
        """Print ``lines``; on a real terminal pause after each screenful so nothing scrolls away unread."""
        page = max(8, self.rows - 4)
        if not (self.tty_in and self.tty_out) or len(lines) <= page:
            for line in lines:
                self.say(line)
            return
        for number, line in enumerate(lines):
            if number and number % page == 0:
                answer = self._read_line("-- more: Enter = continue, q = skip the rest -- ").strip().lower()
                if answer in QUIT_WORDS:
                    self.say("...")
                    return
            self.say(line)

    def numbered(self, labels: Sequence[str], *, start: int = 1) -> None:
        """Numbered items; in two columns when there are many short ones."""
        numbers = [f"{start + offset:>3}) {label}" for offset, label in enumerate(labels)]
        half = (len(numbers) + 1) // 2
        column = (self.width - 2) // 2
        if len(numbers) > 10 and all(len(item) <= column for item in numbers):
            for row in range(half):
                left = numbers[row]
                right = numbers[row + half] if row + half < len(numbers) else ""
                self.say((left.ljust(column) + "  " + right).rstrip())
            return
        for item in numbers:
            self.para(item, hanging=5)

    # --- reading --------------------------------------------------------------------------

    def _read_once(self, prompt: str, secret: bool) -> str:
        if secret and self.tty_in:
            return self._read_hidden(prompt)
        if self._line_editing and not secret:
            try:
                return input(prompt)
            except EOFError:
                raise InputClosed from None
        self._write(prompt)
        line = self.stdin.readline()
        if line == "":
            raise InputClosed
        line = line.rstrip("\r\n")
        if not self.tty_in:
            # Scripted or piped input is not echoed by a terminal: show it, so logs read like a session.
            self._write(("" if secret else line) + "\n")
        return line

    def _read_hidden(self, prompt: str) -> str:
        """A line typed without echo (the terminal's own echo is switched off while it is read)."""
        self._write(prompt)
        try:
            import termios
        except ImportError:  # pragma: no cover - not on Linux
            termios = None  # type: ignore[assignment]
        saved = None
        fd = -1
        if termios is not None:
            try:
                fd = self.stdin.fileno()
                saved = termios.tcgetattr(fd)
                hidden = list(saved)
                hidden[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSADRAIN, hidden)
            except (termios.error, OSError, ValueError, AttributeError):
                saved = None
        try:
            line = self.stdin.readline()
        finally:
            if saved is not None and termios is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
                except (termios.error, OSError):
                    pass
            self._write("\n")
        if line == "":
            raise InputClosed
        return line.rstrip("\r\n")

    def _read_line(self, prompt: str, *, secret: bool = False) -> str:
        """One line from the user. Ctrl-C asks whether to quit; the end of the input raises InputClosed."""
        while True:
            try:
                return self._read_once(prompt, secret)
            except KeyboardInterrupt:
                self._write("\n")
                if self.confirm_quit():
                    raise Quit from None

    def confirm_quit(self) -> bool:
        """Ask whether to leave. A second Ctrl-C, or the end of the input, counts as yes."""
        self.para(self.quit_note)
        try:
            answer = self._read_once("Quit the installer? [y/N] ", False)
        except (KeyboardInterrupt, InputClosed):
            self._write("\n")
            return True
        return answer.strip().lower() in YES_WORDS

    def _help(self, text: str) -> None:
        self.para(text or "Answer the question, or use the commands shown. b goes back, q quits.")

    # --- prompts ----------------------------------------------------------------------------

    def ask(
        self,
        prompt: str,
        *,
        default: str = "",
        help: str = "",
        validate: Callable[[str], str] | None = None,
        allow_empty: bool = False,
        back: bool = True,
    ) -> str:
        """A line of text. ``<`` goes back, ``?`` shows the help, Enter keeps the default.

        ``validate`` returns what is wrong with an answer ("" when it is fine); the question is
        then asked again."""
        full = f"{prompt} [{default}]: " if default else f"{prompt}: "
        while True:
            text = self._read_line(full).strip()
            if text == BACK and back:
                raise GoBack
            if text == "?":
                self._help(help)
                continue
            if not text:
                if default:
                    text = default
                elif not allow_empty:
                    self.say("Please type an answer." + (" ('<' goes back, '?' explains.)" if back else " ('?' explains.)"))
                    continue
            problem = validate(text) if validate and (text or not allow_empty) else ""
            if problem:
                self.warn(problem)
                continue
            return text

    def secret(self, prompt: str, *, help: str = "", back: bool = True) -> str:
        """A password: nothing is shown while it is typed, and spaces count."""
        while True:
            text = self._read_line(f"{prompt} (nothing is shown while you type): ", secret=True)
            if text == BACK and back:
                raise GoBack
            if text == "?":
                self._help(help)
                continue
            return text

    def confirm(self, question: str, *, default: bool | None = None, back: bool = True, help: str = "") -> bool:
        hint = "[Y/n]" if default is True else "[y/N]" if default is False else "[y/n]"
        while True:
            text = self._read_line(f"{question} {hint} ").strip().lower()
            if text in BACK_WORDS and back:
                raise GoBack
            if text in HELP_WORDS:
                self._help(help)
                continue
            if text in YES_WORDS:
                return True
            if text in NO_WORDS:
                return False
            if not text and default is not None:
                return default
            self.say("Please answer y (yes) or n (no).")

    def pause(self, prompt: str = "Press Enter to continue") -> None:
        """Wait for Enter; ``b`` goes back and ``q`` asks whether to quit."""
        while True:
            text = self._read_line(f"{prompt} (b = back, q = quit): ").strip().lower()
            if text in BACK_WORDS:
                raise GoBack
            if text in QUIT_WORDS:
                if self.confirm_quit():
                    raise Quit
                continue
            return

    def menu(
        self,
        options: Sequence[Option],
        *,
        title: str = "",
        default: int | None = None,
        commands: Mapping[str, str] | None = None,
        back: bool = True,
        help: str = "",
        prompt: str = "Your choice",
    ) -> Selection:
        """A numbered menu. Returns the chosen option or one of the extra ``commands`` (letter -> label).

        ``b`` raises GoBack (unless ``back`` is false) and ``q`` asks whether to quit."""
        lines = self.wrap(title) if title else []
        for number, option in enumerate(options, 1):
            lines.extend(self.wrap(f"{number:>2}) {option.label}", hanging=4))
            if option.detail:
                lines.extend(self.wrap(option.detail, indent=7))
        legend = [f"{letter} = {label}" for letter, label in (commands or {}).items()]
        legend.extend(["b = back"] if back else [])
        legend.extend(["q = quit", "? = help"])
        lines.extend(self.wrap(", ".join(item.replace(" ", NBSP) for item in legend)))
        self.show_lines(lines)  # a long menu pauses after each screenful on a real terminal
        suffix = f" [{default + 1}]" if default is not None else ""
        while True:
            text = self._read_line(f"{prompt}{suffix}: ").strip().lower()
            if text in BACK_WORDS and back:
                raise GoBack
            if text in QUIT_WORDS:
                if self.confirm_quit():
                    raise Quit
                continue
            if text in HELP_WORDS:
                self._help(help)
                continue
            if commands and text in commands:
                return Selection(command=text)
            if not text and default is not None:
                return Selection(index=default)
            if text.isdigit() and 1 <= int(text) <= len(options):
                return Selection(index=int(text) - 1)
            self.say("Please type one of the numbers or letters shown." if commands else "Please type one of the numbers shown.")

    def pick(
        self,
        items: Sequence[str],
        *,
        title: str = "",
        default: int | None = None,
        help: str = "",
        back: bool = True,
    ) -> int:
        """Choose from a long list: type a number, part of a name to narrow the list, n/p for the next/previous page.

        Returns the index in ``items``."""
        if title:
            self.para(title)
        shown = list(range(len(items)))
        page_size = max(10, (self.rows - 9) * 2)
        offset = 0
        while True:
            window = shown[offset:offset + page_size]
            self.numbered([items[index] for index in window], start=1)
            more = len(shown) - offset - len(window)
            hints = []
            if more > 0:
                hints.append(f"n = next page ({more} more)")
            if offset:
                hints.append("p = previous page")
            hints.extend(["b = back"] if back else [])
            self.para("Type a number, or part of a name to narrow the list." + (" " + ", ".join(item.replace(" ", NBSP) for item in hints) if hints else ""))
            suffix = f" [Enter = {items[default]}]" if default is not None else ""
            text = self._read_line(f"Your choice{suffix}: ").strip()
            word = text.lower()
            if word in BACK_WORDS and back:
                raise GoBack
            if word in HELP_WORDS:
                self._help(help)
                continue
            if not text:
                if default is not None:
                    return default
                self.say("Type a number or part of a name.")
                continue
            if text.isdigit():
                number = int(text)
                if 1 <= number <= len(window):
                    return window[number - 1]
                self.say(f"Please type a number from 1 to {len(window)}.")
                continue
            if word == "n" and more > 0:
                offset += page_size
                continue
            if word == "p" and offset:
                offset = max(0, offset - page_size)
                continue
            matches = [index for index, label in enumerate(items) if word in label.lower()]
            if not matches:
                self.say(f"Nothing matches \"{text}\". Try another part of the name.")
                continue
            if len(matches) == 1:
                self.say(f"-> {items[matches[0]]}")
                return matches[0]
            shown, offset = matches, 0
            self.say(f"{len(matches)} entries match \"{text}\":")


def clip(text: str, width: int) -> str:
    """``text`` cut to ``width`` characters, ending in '...' when something was removed."""
    return text if len(text) <= width else text[: max(0, width - 3)] + "..."


def plain(value: Any) -> str:
    """Text for a value that may be None."""
    return "" if value is None else str(value)
