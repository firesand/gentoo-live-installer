#!/usr/bin/env python3
"""Render the Catalyst specs, the Portage settings and the boot-menu overlay.

builder-entrypoint.sh runs this once per build:

    render-config.py BUILD_DIR WORKSPACE JOBS LOAD_AVERAGE

The values of the @TOKENS@ come from the environment (iso/pins.env is sourced into it
by the entrypoint, and the image flavour has already been applied to ISO_NAME and
ISO_VOLID). Besides the three templates that always existed it renders everything
below iso/cd_overlay, so the boot menu and the live volume label can never disagree:

    iso/specs/livecd-stage1.spec   -> BUILD_DIR/livecd-stage1.spec
    iso/specs/livecd-stage2.spec   -> BUILD_DIR/livecd-stage2.spec
    iso/portage/make.conf          -> BUILD_DIR/portage/make.conf (+ make.conf.extra)
    iso/README-live.txt            -> BUILD_DIR/README-live.txt
    iso/cd_overlay/**              -> BUILD_DIR/cd_overlay/** (a name ending in .in is
                                      rendered and loses the suffix, others are copied)

    render-config.py --get-make-conf-value KEY FILE   prints one assignment's value.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sys
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_conf_keys  # noqa: E402

TOKEN_RE = re.compile(r"@[A-Z][A-Z0-9_]*@")
VOLID_RE = re.compile(r"[A-Z0-9_]{1,32}")
# The boot arguments are pasted into GRUB's "linux" lines, so no shell or GRUB syntax.
BOOTARG_RE = re.compile(r"[A-Za-z0-9_.=,:/+-]+")
ASSIGNMENT_RE = re.compile(r'([A-Z_]+)="([^"]*)"')


class RenderError(Exception):
    """A template or a value cannot be used."""


def token_values(env: Mapping[str, str], build_dir: str, jobs: str, load_average: str) -> dict[str, str]:
    """Return the token map; every value is validated."""
    try:
        values = {
            "@BUILD_DIR@": str(build_dir),
            "@TREEISH@": env["GENTOO_TREEISH"],
            "@VERSION_STAMP@": env["VERSION_STAMP"],
            "@STAGE3_BASENAME@": env["STAGE3_BASENAME"],
            "@ISO_NAME@": env["ISO_NAME"],
            "@VOLID@": env["ISO_VOLID"],
            "@BOOTARGS@": env["ISO_BOOTARGS"],
            "@JOBS@": str(jobs),
            "@LOAD_AVERAGE@": str(load_average),
        }
    except KeyError as exc:
        raise RenderError(f"missing setting {exc.args[0]} (iso/pins.env)") from exc
    if not VOLID_RE.fullmatch(values["@VOLID@"]):
        raise RenderError("ISO_VOLID must be 1 to 32 characters of A-Z, 0-9 and _")
    words = values["@BOOTARGS@"].split()
    if not words or not all(BOOTARG_RE.fullmatch(word) for word in words):
        raise RenderError("ISO_BOOTARGS must be plain kernel arguments separated by spaces")
    if "dokeymap" in words:
        raise RenderError("dokeymap blocks every boot on a keymap prompt; only the text console entry may use it")
    values["@BOOTARGS@"] = " ".join(words)
    if not re.fullmatch(r"[1-9][0-9]*", values["@JOBS@"]):
        raise RenderError("JOBS must be a positive integer")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", values["@LOAD_AVERAGE@"]):
        raise RenderError("LOAD_AVERAGE must be numeric")
    return values


def render_text(text: str, values: Mapping[str, str], source: str = "<text>") -> str:
    """Replace every token and refuse to leave an unexpanded one behind."""
    for token, value in values.items():
        text = text.replace(token, value)
    leftover = TOKEN_RE.search(text)
    if leftover:
        raise RenderError(f"unexpanded token {leftover.group(0)} in {source}")
    return text


def render_file(source: Path, destination: Path, values: Mapping[str, str]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_text(source.read_text(encoding="utf-8"), values, str(source)), encoding="utf-8")
    shutil.copymode(source, destination)


def extract_assignments(text: str, keys: tuple[str, ...] = make_conf_keys.BUILD_KEYS) -> dict[str, str]:
    """Return {key: value} for the wanted KEY="value" lines, each exactly once."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = ASSIGNMENT_RE.fullmatch(line.strip())
        if not match or match[1] not in keys:
            continue
        if match[1] in found:
            raise RenderError(f"duplicate {match[1]} in Portage make.conf")
        found[match[1]] = match[2]
    missing = [key for key in keys if key not in found]
    if missing:
        raise RenderError(f"missing Portage settings: {' '.join(missing)}")
    return found


def render_make_conf_extra(assignments: Mapping[str, str]) -> str:
    return (
        "# Portage settings preserved across Catalyst make.conf rewrites.\n"
        + "".join(f'{key}="{assignments[key]}"\n' for key in make_conf_keys.BUILD_KEYS)
    )


def render_all(workspace: Path, build: Path, values: Mapping[str, str]) -> None:
    iso = workspace / "iso"
    render_file(iso / "specs/livecd-stage1.spec", build / "livecd-stage1.spec", values)
    render_file(iso / "specs/livecd-stage2.spec", build / "livecd-stage2.spec", values)
    render_file(iso / "portage/make.conf", build / "portage/make.conf", values)
    render_file(iso / "README-live.txt", build / "README-live.txt", values)
    assignments = extract_assignments((build / "portage/make.conf").read_text(encoding="utf-8"))
    (build / "portage/make.conf.extra").write_text(render_make_conf_extra(assignments), encoding="utf-8")
    overlay = iso / "cd_overlay"
    for source in sorted(p for p in overlay.rglob("*") if p.is_file()):
        relative = source.relative_to(overlay)
        if source.name.endswith(".in"):
            render_file(source, build / "cd_overlay" / relative.with_name(source.name[:-3]), values)
        else:
            destination = build / "cd_overlay" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) == 3 and args[0] == "--get-make-conf-value":
            assignments = extract_assignments(Path(args[2]).read_text(encoding="utf-8"), (args[1],))
            print(assignments[args[1]])
            return 0
        if len(args) != 4:
            print(__doc__, file=sys.stderr)
            return 2
        build, workspace, jobs, load_average = args
        values = token_values(os.environ, build, jobs, load_average)
        render_all(Path(workspace), Path(build), values)
    except (RenderError, OSError) as exc:
        print(f"render-config: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
