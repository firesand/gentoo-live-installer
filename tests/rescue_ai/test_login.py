"""gentoo-rescue-login: URL scraping on recorded-style outputs (see data/README), the QR code, and the rule that the agent reads the code itself."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from gentoo_rescue_ai import login

DATA = Path(__file__).parent / "data"
COLUMNS = {"claude_auth_login_wrapped": 46}
RECORDED = sorted(path.stem for path in DATA.glob("*.expect"))


@pytest.mark.parametrize("name", RECORDED)
def test_the_login_address_is_found_in_each_recorded_output(name) -> None:
    agent = name.split("_")[0]
    expected = (DATA / f"{name}.expect").read_text().strip()
    text = (DATA / f"{name}.txt").read_text(encoding="utf-8")
    assert login.find_login_url(text, COLUMNS.get(name, 80), agent) == expected


@pytest.mark.parametrize("name", [path.stem for path in sorted(DATA.glob("no_url_*.txt"))])
def test_an_output_without_a_login_address_gives_none_and_never_raises(name) -> None:
    assert login.find_login_url((DATA / f"{name}.txt").read_text(), 80, "claude") is None


@pytest.mark.parametrize("text", ["", "\x1b[", "https://", "https://\n", "http://insecure.example/login", "\x00\x01\x02", "x" * 100000, "https://" + "a" * 5000 + "/login"])
def test_odd_input_is_never_a_crash(text) -> None:
    assert login.find_login_url(text, 80, "codex") is None


def test_an_address_on_an_unrelated_host_is_not_taken_for_claudes_login() -> None:
    assert login.find_login_url("visit https://evil.example/oauth/authorize now\n", 80, "claude") is None
    assert login.find_login_url("visit https://evil.example/oauth/authorize now\n", 80, "opencode") == "https://evil.example/oauth/authorize"


def test_qr_text_calls_qrencode_with_the_address_as_an_argument_not_as_shell_text() -> None:
    seen = {}

    def run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="██ QR ██\n", stderr="")

    url = "https://x.example/a?b=1&c=$(id)"
    assert login.qr_text(url, which=lambda name: "/usr/bin/qrencode", run=run) == "██ QR ██\n"
    assert seen["argv"] == ["/usr/bin/qrencode", "-t", "ANSIUTF8", "-m", "2", url] and not seen["kwargs"].get("shell")
    assert login.qr_text(url, which=lambda name: None) is None
    assert login.qr_text(url, which=lambda name: "/q", run=lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="e")) is None


def fake_agent(tmp_path: Path, name: str, script: str) -> Path:
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def run_login(tmp_path: Path, agent: str, script: str, stdin_bytes: bytes = b"", qr=lambda url: "<QR of " + url + ">\n"):
    """Run login() against a fake agent. Returns (status, screen text, the pipe for the agent's stdin)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    fake = fake_agent(tmp_path, agent, script)
    stdin_r, stdin_w = os.pipe()
    os.write(stdin_w, stdin_bytes)
    os.close(stdin_w)
    screen_r, screen_w = os.pipe()
    out = []
    old = dict(login.LOGIN_COMMANDS)
    login.LOGIN_COMMANDS[agent] = [str(fake)]
    try:
        status = login.login(agent, home=str(home), stdin_fd=stdin_r, out_fd=screen_w, say=login.Say(screen_w), qr=qr)
    finally:
        login.LOGIN_COMMANDS.update(old)
        os.close(screen_w)
        while chunk := os.read(screen_r, 65536):
            out.append(chunk)
        os.close(screen_r)
        os.close(stdin_r)
    return status, b"".join(out).decode(errors="replace").replace("\r\n", "\n"), home


def test_the_agent_reads_the_pasted_code_itself_and_the_helper_never_does(tmp_path) -> None:
    got = tmp_path / "got"
    script = f'echo "Use the url below:"; echo "https://claude.com/cai/oauth/authorize?state=s1"; printf "Paste code here > "; read code; echo "$code" > {got}; echo; echo logged in\n'
    status, screen, _ = run_login(tmp_path, "claude", script, stdin_bytes=b"SECRET-LOGIN-CODE-123\n")
    assert status == 0
    assert got.read_text().strip() == "SECRET-LOGIN-CODE-123"  # the agent got the code from the terminal itself
    assert "SECRET-LOGIN-CODE-123" not in screen  # and it never went through the helper (the agent does not echo it here)
    assert "<QR of https://claude.com/cai/oauth/authorize?state=s1>" in screen and "Phone: scan this" in screen
    assert screen.count("<QR of") == 1  # drawn once, not at every redraw


def test_the_helper_leaves_no_credential_or_log_behind(tmp_path) -> None:
    script = 'echo "https://claude.com/cai/oauth/authorize?state=s1"; read code; echo done\n'
    _, _, home = run_login(tmp_path, "claude", script, stdin_bytes=b"TOKEN-xyz\n")
    assert not [p for p in home.rglob("*") if p.is_file() and b"TOKEN-xyz" in p.read_bytes()]
    source = Path(login.__file__).read_text()
    assert not re.search(r"(?<![\w.])open\(", source) and "write_text" not in source and "sys.stdin" not in source  # nothing is written to disk, stdin is never read


def test_scraping_that_fails_soft_still_shows_everything_and_points_to_the_manual_way(tmp_path) -> None:
    status, screen, _ = run_login(tmp_path, "claude", 'echo "something changed: no address here"; exit 3\n')
    assert status == 3
    assert "something changed" in screen and "could not find the login address" in screen and "ANTHROPIC_API_KEY" in screen
    assert "<QR" not in screen


def test_without_qrencode_the_address_is_still_printed(tmp_path) -> None:
    _, screen, _ = run_login(tmp_path, "codex", 'echo "1. open"; echo "   https://auth.openai.com/codex/device"; echo "2. code ABCD-1234"\n', qr=lambda url: None)
    assert "qrencode is not available" in screen and screen.count("https://auth.openai.com/codex/device") >= 2


def test_codex_device_auth_shows_a_qr_for_the_link_and_leaves_the_code_to_the_phone(tmp_path) -> None:
    script = "cat <<'EOF'\n" + (DATA / "codex_device_auth.txt").read_text() + "EOF\nsleep 0.2\n"
    status, screen, _ = run_login(tmp_path, "codex", script)
    assert status == 0 and "<QR of https://auth.openai.com/codex/device>" in screen and "ABCD-12345" in screen  # the code is the agent's own output, shown as it is


def test_the_commands_are_the_documented_logins() -> None:
    assert login.LOGIN_COMMANDS == {"claude": ["claude", "auth", "login"], "codex": ["codex", "login", "--device-auth"], "opencode": ["opencode", "auth", "login"]}


def test_login_refuses_root_and_missing_agents(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert login.main(["claude"]) == 1 and "run it as rescue-ai" in capsys.readouterr().err
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert login.main(["claude"], home=str(tmp_path)) == 1 and "gentoo-rescue-agents install claude" in capsys.readouterr().err
    assert login.main(["emacs"], home=str(tmp_path)) == 2


def test_api_key_fallbacks_never_ask_for_a_claude_key(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    from gentoo_rescue_ai import agents

    for name in ("claude", "opencode", "codex"):
        binary = tmp_path / ".local" / "bin" / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("x")
        agents.state_file(name, tmp_path).parent.mkdir(parents=True, exist_ok=True)
        agents.state_file(name, tmp_path).write_text('{"version": "1", "binary": "%s"}' % binary)
    assert login.main(["claude", "--api-key"], home=str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "read -rs ANTHROPIC_API_KEY" in out and "never sees it" in out
    assert login.main(["opencode", "--api-key"], home=str(tmp_path)) == 0 and "/connect" in capsys.readouterr().out


def test_codex_api_key_goes_to_codex_stdin_only(tmp_path, monkeypatch) -> None:
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["input"] = argv, kwargs.get("input")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(login.subprocess, "run", fake_run)
    assert login.api_key_codex(str(tmp_path), prompt_key=lambda p: " sk-test \n") == 0
    assert seen["argv"] == ["codex", "login", "--with-api-key"] and seen["input"] == "sk-test\n" and "sk-test" not in " ".join(seen["argv"])
