"""scripts/bump-ai-agents.py with canned upstream answers (no network)."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

from gentoo_rescue_ai import agents

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bump-ai-agents.py"
spec = importlib.util.spec_from_file_location("bump_ai_agents", SCRIPT)
bump = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(bump)  # type: ignore[union-attr]

SHA = "ab" * 32


def upstream(claude_version: str = "3.0.0", tag: str = "rust-v1.2.3") -> dict[str, str]:
    asset = lambda name: {"name": name, "size": 12345, "digest": f"sha256:{SHA}", "browser_download_url": f"https://github.com/x/y/releases/download/t/{name}"}
    return {
        f"{bump.CLAUDE_BASE}/latest": claude_version + "\n",
        f"{bump.CLAUDE_BASE}/{claude_version}/manifest.json": json.dumps({"version": claude_version, "platforms": {"linux-x64": {"binary": "claude", "checksum": SHA, "size": 99}}}),
        "https://api.github.com/repos/openai/codex/releases/latest": json.dumps({"tag_name": tag, "assets": [asset("codex-x86_64-unknown-linux-musl.tar.gz"), asset("codex-x86_64-unknown-linux-musl-bazel.tar.gz")]}),
        "https://api.github.com/repos/anomalyco/opencode/releases/latest": json.dumps({"tag_name": "v4.5.6", "assets": [asset("opencode-linux-x64-baseline.tar.gz")]}),
    }


def fetch_from(table: dict[str, str]):
    return lambda url: table[url]


def test_without_write_it_only_prints_the_difference(tmp_path) -> None:
    pins = tmp_path / "pins.json"
    pins.write_text(Path(agents.__file__).with_name("data").joinpath("pins.json").read_text())
    before = pins.read_text()
    out = io.StringIO()
    assert bump.main(["--pins", str(pins)], fetch=fetch_from(upstream()), out=out) == 0
    assert pins.read_text() == before
    text = out.getvalue()
    assert "claude: 2.1.310 -> 3.0.0" in text and "codex: 0.164.0 -> 1.2.3" in text and "opencode: 1.19.4 -> 4.5.6" in text and "Nothing was written" in text


def test_write_updates_the_pins_and_the_installer_can_load_them(tmp_path) -> None:
    pins = tmp_path / "pins.json"
    pins.write_text(Path(agents.__file__).with_name("data").joinpath("pins.json").read_text())
    assert bump.main(["--pins", str(pins), "--write"], fetch=fetch_from(upstream()), out=io.StringIO()) == 0
    loaded = agents.load_pins(pins)
    assert loaded["claude"].version == "3.0.0" and loaded["claude"].sha256 == SHA and loaded["claude"].size == 99
    assert loaded["codex"].url.endswith("codex-x86_64-unknown-linux-musl.tar.gz") and "bazel" not in loaded["codex"].url


@pytest.mark.parametrize("break_it", ["version", "digest", "tag"])
def test_an_unexpected_upstream_answer_changes_nothing(tmp_path, break_it) -> None:
    table = upstream()
    if break_it == "version":
        table[f"{bump.CLAUDE_BASE}/latest"] = "<html>oops"
    elif break_it == "digest":
        table["https://api.github.com/repos/openai/codex/releases/latest"] = json.dumps({"tag_name": "rust-v1", "assets": [{"name": "codex-x86_64-unknown-linux-musl.tar.gz", "size": 1}]})
    else:
        table["https://api.github.com/repos/openai/codex/releases/latest"] = json.dumps({"tag_name": "other-1", "assets": []})
    pins = tmp_path / "pins.json"
    pins.write_text(Path(agents.__file__).with_name("data").joinpath("pins.json").read_text())
    before = pins.read_text()
    assert bump.main(["--pins", str(pins), "--write"], fetch=fetch_from(table), out=io.StringIO()) == 1
    assert pins.read_text() == before
