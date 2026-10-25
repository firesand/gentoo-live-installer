#!/usr/bin/env python3
"""Propose new pins for the AI agents (src/gentoo_rescue_ai/data/pins.json) from the makers' own release data.

Usage: scripts/sandbox.sh --net python3 scripts/bump-ai-agents.py [--write] [--pins FILE]

It reads only small JSON documents (never a binary) and takes the sha256 and the size that the makers publish:
  * Claude Code: https://downloads.claude.ai/claude-code-releases/latest, then <version>/manifest.json, platforms.linux-x64
  * Codex:       the GitHub release API of openai/codex (tag rust-vX.Y.Z), asset codex-x86_64-unknown-linux-musl.tar.gz, its "digest"
  * OpenCode:    the GitHub release API of anomalyco/opencode, asset opencode-linux-x64-baseline.tar.gz, its "digest"
Without --write it prints the difference and changes nothing: the lead reviews it, then runs it again with --write. Run it in
the sandbox with --net; nothing it fetches is executed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable

PINS = Path(__file__).resolve().parent.parent / "src" / "gentoo_rescue_ai" / "data" / "pins.json"
CLAUDE_BASE = "https://downloads.claude.ai/claude-code-releases"
SOURCES = {
    "codex": ("openai/codex", "codex-x86_64-unknown-linux-musl.tar.gz", "rust-v"),
    "opencode": ("anomalyco/opencode", "opencode-linux-x64-baseline.tar.gz", "v"),
}

FetchText = Callable[[str], str]


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "bump-ai-agents", "Accept": "application/json, text/plain"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URLs above
        return response.read().decode("utf-8")


def claude_pin(fetch: FetchText) -> dict[str, Any]:
    version = fetch(f"{CLAUDE_BASE}/latest").strip()
    if not version or any(c not in "0123456789." for c in version):
        raise ValueError(f"unexpected Claude Code version {version!r}")
    manifest = json.loads(fetch(f"{CLAUDE_BASE}/{version}/manifest.json"))
    platform = manifest["platforms"]["linux-x64"]
    return {"version": version, "url": f"{CLAUDE_BASE}/{version}/linux-x64/claude", "kind": "binary", "binary": "claude",
            "sha256": platform["checksum"], "size": int(platform["size"]),
            "source": f"{CLAUDE_BASE}/{version}/manifest.json (platforms.linux-x64.checksum and .size)",
            "license": "proprietary: downloaded by the person from Anthropic, never redistributed (https://code.claude.com/docs/en/legal-and-compliance)"}


def github_pin(name: str, fetch: FetchText) -> dict[str, Any]:
    repo, asset_name, prefix = SOURCES[name]
    release = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases/latest"))
    tag = release["tag_name"]
    if not tag.startswith(prefix):
        raise ValueError(f"unexpected tag {tag!r} for {repo}")
    asset = next((a for a in release["assets"] if a["name"] == asset_name), None)
    if asset is None or not str(asset.get("digest", "")).startswith("sha256:"):
        raise ValueError(f"{repo} {tag} has no asset {asset_name} with a sha256 digest")
    return {"version": tag[len(prefix):], "url": asset["browser_download_url"], "kind": "tar.gz", "binary": name,
            "sha256": asset["digest"].split(":", 1)[1], "size": int(asset["size"]),
            "source": f"GitHub release API for {repo} tag {tag}, asset {asset_name} (the digest field)",
            "license": "Apache-2.0" if name == "codex" else "MIT"}


def proposed(fetch: FetchText) -> dict[str, Any]:
    return {"claude": claude_pin(fetch), "codex": github_pin("codex", fetch), "opencode": github_pin("opencode", fetch)}


def describe(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    lines = []
    for name, pin in new.items():
        before = old.get(name, {})
        if before.get("sha256") == pin["sha256"]:
            lines.append(f"{name}: unchanged ({pin['version']})")
        else:
            lines.append(f"{name}: {before.get('version', '-')} -> {pin['version']}  sha256 {pin['sha256']}  {pin['size']} bytes")
    return lines


def main(argv: list[str] | None = None, *, fetch: FetchText = fetch_text, out: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="update the pins file (otherwise only print the difference)")
    parser.add_argument("--pins", default=str(PINS))
    args = parser.parse_args(argv)
    out = out or sys.stdout
    path = Path(args.pins)
    old = json.loads(path.read_text(encoding="utf-8"))
    try:
        new = proposed(fetch)
    except (ValueError, KeyError, OSError) as exc:
        print(f"bump-ai-agents: {exc}", file=sys.stderr)
        return 1
    for line in describe(old, new):
        print(line, file=out)
    if not args.write:
        print("Nothing was written. Review the lines above, then run again with --write.", file=out)
        return 0
    path.write_text(json.dumps({"_comment": old.get("_comment", ""), **new}, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
