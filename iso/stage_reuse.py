#!/usr/bin/env python3
"""Decide whether a finished Catalyst stage1 can be reused, and keep its receipt.

A full ISO build spends hours in livecd-stage1 (about 520 packages). The inputs of that
stage change rarely, while the installer's own sources and the stage2 overlay change all
the time. After every stage1 the builder writes a receipt next to the archive:

    <storedir>/builds/23.0-default/livecd-stage1-amd64-<stamp>.tar.gz.receipt.json

and the next run compares it with the current inputs:

    reuse    nothing that went into stage1 changed: run stage2 only.
    reseed   only the project's own packages changed (installer, HWMonux, PortageHub or
             their ebuilds): unpack the old stage1, let emerge reinstall exactly those
             packages, then run stage2. Packages are never removed this way, which is why
             the specs, the Portage configuration and the pins must be unchanged.
    fresh    anything else, or anything that cannot be proven: build stage1 from stage3.

The module is stdlib only. The CLI is used by iso/builder-entrypoint.sh:

    stage_reuse.py decide  --workspace W --distfiles D --archive A [--policy auto|none|stage1]
    stage_reuse.py receipt --workspace W --distfiles D --archive A --catalyst-version V
                           [--image-id ID] [--build-inputs FILE] > receipt.json
    stage_reuse.py reseed-spec SPEC SEED_SUBPATH      (prints the spec for the reseed run)
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1
RECEIPT_SUFFIX = ".receipt.json"
# Bump this when builder-entrypoint.sh changes how it drives stage1 (catalyst.conf, the
# environment handed to the chroot); the files below cannot see that.
STAGE1_FORMAT = 1
# What decides the content of livecd-stage1 (paths relative to the workspace; a directory
# stands for every file below it). Templates are hashed unrendered, so the job count
# (@JOBS@) never invalidates a stage1.
STAGE1_INPUTS: tuple[str, ...] = (
    "iso/specs/livecd-stage1.spec",
    "iso/portage",
    "iso/pins.env",
    "iso/catalyst-no-loop.py",
    "iso/make_conf_keys.py",
    "iso/Containerfile",
    # The overlay is a stage1 repository. The three packages of the project are compared
    # separately (local_ebuilds); the rest of it is part of the key.
    "packaging/gentoo-overlay/acct-group",
    "packaging/gentoo-overlay/metadata",
    "packaging/gentoo-overlay/profiles",
)
# distfile prefix -> Portage atom of the project's own packages.
LOCAL_PACKAGES: Mapping[str, str] = {
    "gentoo-live-installer": "app-admin/gentoo-live-installer",
    "hwmonux": "app-misc/hwmonux",
    "portagehub": "app-portage/portagehub",
}
OVERLAY = "packaging/gentoo-overlay"


class ReuseError(Exception):
    """The receipt or the archive cannot be used."""


def sha256_file(path: Path) -> str:
    return _digest_file(path, "sha256")


def sha512_file(path: Path) -> str:
    return _digest_file(path, "sha512")


def _digest_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _files_below(workspace: Path, relative: str) -> list[Path]:
    path = workspace / relative
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file())
    return [path] if path.is_file() else []


def compute_stage1_key(workspace: Path, inputs: Iterable[str] = STAGE1_INPUTS) -> str:
    """Hash of everything that decides what livecd-stage1 contains."""
    digest = hashlib.sha256()
    digest.update(f"stage1-format={STAGE1_FORMAT}\n".encode())
    for relative in inputs:
        files = _files_below(workspace, relative)
        if not files:
            raise ReuseError(f"stage1 input is missing: {relative}")
        for path in files:
            name = path.relative_to(workspace).as_posix()
            digest.update(f"{name}\0{sha256_file(path)}\n".encode())
    return digest.hexdigest()


def ebuild_file(workspace: Path, prefix: str) -> Path:
    """The single committed ebuild of one of the project's packages."""
    atom = LOCAL_PACKAGES[prefix]
    category, name = atom.split("/")
    found = sorted((workspace / OVERLAY / category / name).glob(f"{name}-*.ebuild"))
    if len(found) != 1:
        raise ReuseError(
            f"{atom} must have exactly one ebuild in the overlay, found {len(found)}"
            " (remove the old revision in the same change)"
        )
    return found[0]


def package_version(ebuild: Path) -> str:
    """Version of an ebuild file name without the revision (name-X.Y.Z-rN.ebuild gives X.Y.Z)."""
    match = re.fullmatch(r"(?P<name>.+?)-(?P<version>[0-9][^-]*)(?:-r[0-9]+)?\.ebuild", ebuild.name)
    if not match:
        raise ReuseError(f"cannot read a version from {ebuild.name}")
    return match["version"]


def local_inputs(workspace: Path, distfiles: Path) -> dict[str, str]:
    """sha256 of the reproducible source archive of each of the project's packages.

    The archive names follow the committed ebuilds (${P}.tar.zst), so an old archive left
    in the distfiles cache by an earlier version is never mistaken for the current one.
    """
    result: dict[str, str] = {}
    for prefix in LOCAL_PACKAGES:
        archive = distfiles / f"{prefix}-{package_version(ebuild_file(workspace, prefix))}.tar.zst"
        if not archive.is_file():
            raise ReuseError(f"source archive is missing: {archive}")
        result[prefix] = sha256_file(archive)
    return result


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(f"{path.relative_to(root).as_posix()}\0{sha256_file(path)}\n".encode())
    return digest.hexdigest()


def local_ebuilds(workspace: Path) -> dict[str, dict[str, str]]:
    """The committed package of each local atom: ebuild name (with revision) and tree hash.

    The hash covers the whole package directory (ebuild, files/, metadata.xml), because a
    changed init script or desktop entry changes what the package installs.
    """
    result: dict[str, dict[str, str]] = {}
    for prefix in LOCAL_PACKAGES:
        ebuild = ebuild_file(workspace, prefix)
        result[prefix] = {"file": ebuild.name, "sha256": _tree_digest(ebuild.parent)}
    return result


@dataclasses.dataclass(frozen=True)
class Current:
    """The inputs of this run."""

    stage1_key: str
    local_inputs: Mapping[str, str]
    local_ebuilds: Mapping[str, Mapping[str, str]]
    catalyst_version: str = ""


@dataclasses.dataclass(frozen=True)
class Archive:
    """What the filesystem says about the stage1 archive (None for a missing one)."""

    is_regular: bool
    is_symlink: bool
    sha512: str


@dataclasses.dataclass(frozen=True)
class Decision:
    action: str  # "reuse", "reseed" or "fresh"
    reason: str
    reinstall: tuple[str, ...] = ()  # Portage atoms to reinstall on a reseed

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), sort_keys=True)


def check_receipt(receipt: Any) -> dict[str, Any]:
    """Return the receipt or raise ReuseError when it is not one we can trust."""
    if not isinstance(receipt, dict):
        raise ReuseError("the receipt is not a JSON object")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ReuseError(f"unsupported receipt schema_version {receipt.get('schema_version')!r}")
    for key, kind in (("filename", str), ("sha512", str), ("stage1_key", str), ("local_inputs", dict), ("local_ebuilds", dict)):
        if not isinstance(receipt.get(key), kind):
            raise ReuseError(f"the receipt has no valid {key}")
    if not re.fullmatch(r"[0-9a-f]{128}", receipt["sha512"]):
        raise ReuseError("the receipt has no valid sha512")
    return receipt


def decide(receipt: Mapping[str, Any] | None, current: Current, archive: Archive | None) -> Decision:
    """Pick reuse, reseed or fresh. Everything unproven means fresh."""
    if receipt is None:
        return Decision("fresh", "there is no receipt of an earlier stage1")
    try:
        receipt = check_receipt(dict(receipt))
    except ReuseError as exc:
        return Decision("fresh", f"unusable receipt: {exc}")
    if archive is None or not archive.is_regular or archive.is_symlink:
        return Decision("fresh", "the stage1 archive is missing or not a plain file")
    if archive.sha512 != receipt["sha512"]:
        return Decision("fresh", "the stage1 archive does not match its receipt (sha512)")
    if receipt["stage1_key"] != current.stage1_key:
        return Decision("fresh", "the stage1 specs, Portage configuration or pins changed")
    if current.catalyst_version and receipt.get("catalyst_version") not in ("", None, current.catalyst_version):
        return Decision("fresh", "the Catalyst version changed")
    changed: list[str] = []
    for prefix in LOCAL_PACKAGES:
        old_source = receipt["local_inputs"].get(prefix)
        old_ebuild = receipt["local_ebuilds"].get(prefix) or {}
        new_ebuild = current.local_ebuilds.get(prefix) or {}
        if old_source is None or "sha256" not in old_ebuild:
            return Decision("fresh", f"the receipt does not describe {prefix}")
        if old_source != current.local_inputs.get(prefix) or old_ebuild.get("sha256") != new_ebuild.get("sha256"):
            changed.append(prefix)
    if not changed:
        return Decision("reuse", "no input of stage1 changed")
    atoms = tuple(LOCAL_PACKAGES[prefix] for prefix in changed)
    return Decision("reseed", "only " + ", ".join(changed) + " changed", atoms)


def receipt_path(archive: Path) -> Path:
    return archive.with_name(archive.name + RECEIPT_SUFFIX)


def load_receipt(path: Path) -> dict[str, Any] | None:
    """Read a receipt; None when there is none. A broken file raises ReuseError."""
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ReuseError(f"cannot read {path}: {exc}") from exc
    return check_receipt(data)


def archive_state(path: Path) -> Archive | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return Archive(False, True, "")
    if not stat.S_ISREG(info.st_mode):
        return Archive(False, False, "")
    return Archive(True, False, sha512_file(path))


def build_receipt(
    archive: Path,
    current: Current,
    *,
    image_id: str = "",
    build_inputs: Mapping[str, str] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": SCHEMA_VERSION,
        "filename": archive.name,
        "size": archive.stat().st_size,
        "sha512": sha512_file(archive),
        "catalyst_version": current.catalyst_version,
        "builder_image_id": image_id,
        "stage1_key": current.stage1_key,
        "local_inputs": dict(current.local_inputs),
        "local_ebuilds": {key: dict(value) for key, value in current.local_ebuilds.items()},
        "build_inputs_sha256": dict(build_inputs or {}),
        "created": stamp,
    }


def render_reseed_spec(stage1_spec: str, seed_subpath: str) -> str:
    """Return the stage1 spec with source_subpath pointing at the previous stage1."""
    lines = stage1_spec.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.startswith("source_subpath:")]
    if len(hits) != 1:
        raise ReuseError("the stage1 spec must contain exactly one source_subpath line")
    lines[hits[0]] = f"source_subpath: {seed_subpath}\n"
    return "".join(lines)


def current_state(workspace: Path, distfiles: Path, catalyst_version: str = "") -> Current:
    return Current(
        compute_stage1_key(workspace),
        local_inputs(workspace, distfiles),
        local_ebuilds(workspace),
        catalyst_version,
    )


def _cmd_decide(args: argparse.Namespace) -> int:
    workspace, archive_path = Path(args.workspace), Path(args.archive)
    if args.policy == "none":
        print(Decision("fresh", "reuse was switched off (--reuse none)").to_json())
        return 0
    try:
        current = current_state(workspace, Path(args.distfiles), args.catalyst_version)
        receipt = load_receipt(receipt_path(archive_path))
    except ReuseError as exc:
        decision = Decision("fresh", f"unusable receipt: {exc}")
    else:
        decision = decide(receipt, current, archive_state(archive_path))
    print(decision.to_json())
    if args.policy == "stage1" and decision.action != "reuse":
        print(f"stage_reuse: cannot run stage2 only: {decision.reason}", file=sys.stderr)
        return 3
    return 0


def _cmd_receipt(args: argparse.Namespace) -> int:
    current = current_state(Path(args.workspace), Path(args.distfiles), args.catalyst_version)
    build_inputs = None
    if args.build_inputs:
        build_inputs = json.loads(Path(args.build_inputs).read_text(encoding="utf-8"))
    receipt = build_receipt(Path(args.archive), current, image_id=args.image_id, build_inputs=build_inputs)
    sys.stdout.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return 0


def _cmd_reseed_spec(args: argparse.Namespace) -> int:
    sys.stdout.write(render_reseed_spec(Path(args.spec).read_text(encoding="utf-8"), args.seed_subpath))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stage_reuse.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("decide", "receipt"):
        p = sub.add_parser(name)
        p.add_argument("--workspace", required=True)
        p.add_argument("--distfiles", required=True)
        p.add_argument("--archive", required=True)
        p.add_argument("--catalyst-version", default="")
        if name == "decide":
            p.add_argument("--policy", choices=("auto", "none", "stage1"), default="auto")
        else:
            p.add_argument("--image-id", default="")
            p.add_argument("--build-inputs")
    p = sub.add_parser("reseed-spec")
    p.add_argument("spec")
    p.add_argument("seed_subpath")
    args = parser.parse_args(argv)
    handlers = {"decide": _cmd_decide, "receipt": _cmd_receipt, "reseed-spec": _cmd_reseed_spec}
    try:
        return handlers[args.command](args)
    except (ReuseError, OSError, ValueError) as exc:
        print(f"stage_reuse: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
