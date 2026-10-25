"""gentoo-rescue-ai: a failed safety check stops the start unless the person types yes."""

from __future__ import annotations

from pathlib import Path

from gentoo_rescue_ai import launcher

from .test_launcher import Scripted


def start(tmp_path: Path, answers: list[str], problems: list[str]):
    prompts = Scripted(answers)
    commands: list[list[str]] = []
    code = launcher.main(["shell"], prompts=prompts, run=lambda c: commands.append(list(c)) or 0, net=lambda a, p: {"ok": True, "online": True}, home=str(tmp_path / "h"),
                         password_file=str(tmp_path / "pw"), require_root=False, preflight=lambda: problems)
    return code, commands, prompts


def test_a_failed_check_stops_the_start(tmp_path) -> None:
    code, commands, prompts = start(tmp_path, ["no"], ["rescue-ai is in the group wheel"])
    assert code == 1 and commands == [] and "group wheel" in prompts.text


def test_typing_yes_starts_anyway_and_a_clean_check_asks_nothing(tmp_path) -> None:
    code, commands, prompts = start(tmp_path, ["yes", "", "", "n"], ["x"])
    assert code == 0 and commands and "NOT kept away" in prompts.asked[0]
    code, commands, prompts = start(tmp_path, ["", "n"], [])
    assert code == 0 and not any("NOT kept away" in asked for asked in prompts.asked)
