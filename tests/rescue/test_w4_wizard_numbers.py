"""The repair console's numbered prompts: keys that look like digits (² is next to the 1 on a French keyboard) and numbers far too long to be a choice."""

from __future__ import annotations

import io

import pytest

from gentoo_installerd.rescue import cli
from gentoo_installerd.rescue.api import RescueApi
from gentoo_installerd.rescue.client import LocalTransport

from .flow import uefi_gentoo_lab


class Run:
    """One invocation of the repair tool against a lab, with scripted answers (the same way tests/rescue/test_cli.py drives it)."""

    def __init__(self, lab) -> None:
        self.lab = lab

    def __call__(self, *argv: str, answers: list[str] | None = None, interactive: bool = False):
        out, err = io.StringIO(), io.StringIO()
        scripted = list(answers or [])
        console = cli.Console(out=out, err=err, ask=lambda _p: scripted.pop(0) if scripted else "", secret=lambda _p: "", interactive=interactive)
        transport = LocalTransport(RescueApi(self.lab.manager(auto_adopt=True)))
        code = cli.main(list(argv), transport=transport, console=console)
        return code, out.getvalue(), err.getvalue()


def prepared(lab_factory):
    lab, tree = uefi_gentoo_lab(lab_factory)
    return lab, tree, Run(lab)


TOO_LONG = pytest.param("9" * 5000, id="5000-digits")  # int() refuses more than 4300 digits
LOOKALIKES = ["²", "³", "¹", "①", "⑩", "12²", "½", TOO_LONG]


@pytest.mark.parametrize("typed", LOOKALIKES)
def test_the_system_menu_asks_again_instead_of_ending(lab_factory, typed: str) -> None:
    lab, _, run = prepared(lab_factory)
    code, out, err = run("wizard", answers=[typed, "q"], interactive=True)
    assert code == 0, err
    assert "Please type one of the numbers shown." in out and not lab.system.mounted_targets()


@pytest.mark.parametrize("typed", LOOKALIKES)
def test_the_log_menu_goes_back_to_the_repair_menu(lab_factory, typed: str) -> None:
    lab, _, run = prepared(lab_factory)
    code, out, err = run("wizard", answers=["1", "y", "9", typed, "u"], interactive=True)
    assert code == 0, err
    assert "Which log?" in out and "Unmounted. You can remove the live medium" in out and not lab.system.mounted_targets()


def test_a_number_written_with_other_decimal_digits_is_still_a_number(lab_factory) -> None:
    lab, _, run = prepared(lab_factory)
    code, out, err = run("wizard", answers=["١", "y", "u"], interactive=True)  # ARABIC-INDIC DIGIT ONE: int() reads it, so the menu does too
    assert code == 0, err
    assert "Mounted at" in out and "Unmounted." in out


def test_typed_number_reads_plain_numbers_only() -> None:
    assert cli.typed_number("12") == 12 and cli.typed_number("٣") == 3 and cli.typed_number("007") == 7
    for text in ("", "-1", "1.5", " 1", "²", "①", "9" * 10, "9" * 5000):
        assert cli.typed_number(text) is None, text


@pytest.mark.parametrize("line", ["²", "³", "①", "-1", "1.5", "", TOO_LONG])
def test_choose_refuses_a_line_number_that_is_not_a_number(line: str) -> None:
    """--choose LINE=ACTION: the engine turns the line into an int, so a key like ² would end the request there."""
    with pytest.raises(ValueError, match="--choose needs LINE=ACTION"):
        cli.parse_choices([f"{line}=nofail"])


def test_choose_hands_the_engine_plain_line_numbers() -> None:
    assert cli.parse_choices(["٣=nofail", "05=replace:UUID=abc"]) == {"3": {"action": "nofail"}, "5": {"action": "replace", "spec": "UUID=abc"}}
