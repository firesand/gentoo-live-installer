#!/usr/bin/env python3
"""Run Catalyst builds using a verified, unpacked repository snapshot.

CLI arguments are passed unchanged to catalyst.main.main. The entrypoint must
publish snapshots/REPO-COMMIT.tree from the authenticated pinned git archive,
then write .catalyst-snapshot.json containing repo_name and treeish inside it.
This receipt identifies the producer's verified extraction; it is not an
independent cryptographic authentication of arbitrary directory contents.

Only StageBase is adapted. Catalyst's separate snapshot target still creates
its normal .sqfs artifact. No loop-device fallback or device access is added.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from catalyst.base.stagebase import StageBase
from catalyst.main import main as catalyst_main
from catalyst.support import CatalystError, get_repo_name


def read_make_conf_extra() -> str:
    """Read literal builder settings that Catalyst otherwise discards."""
    filename = os.environ.get("GENTOO_INSTALLER_MAKE_CONF_EXTRA")
    if not filename:
        return ""
    allowed = {
        "ACCEPT_LICENSE", "GRUB_PLATFORMS", "INPUT_DEVICES", "VIDEO_CARDS",
        "PYTHON_TARGETS", "PYTHON_SINGLE_TARGET", "MAKEOPTS", "NINJAOPTS", "FEATURES",
        "EMERGE_DEFAULT_OPTS",
    }
    path = Path(filename)
    try:
        if not path.is_absolute():
            raise ValueError("path must be absolute")
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise ValueError("expected a small regular file")
            content = stream.read(16385)
        seen: set[str] = set()
        lines = []
        for line in content.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.fullmatch(r'([A-Z_]+)="([A-Za-z0-9_ ./,:=+*?@%-]*)"', line)
            if not match or match[1] not in allowed or match[1] in seen:
                raise ValueError(f"unsupported or duplicate literal assignment: {line!r}")
            seen.add(match[1])
            lines.append(line)
        return "\n# Installer builder settings preserved after Catalyst generation.\n" + "\n".join(lines) + "\n"
    except (OSError, UnicodeError, ValueError) as exc:
        raise CatalystError(f"Invalid builder make.conf settings {path}: {exc}") from exc


def set_directory_snapshot(stage: StageBase, treeish: str | None = None) -> None:
    repo_name = stage.settings["repo_name"]
    commit = treeish if treeish is not None else stage.settings["snapshot_treeish"]
    if not isinstance(repo_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", repo_name):
        raise CatalystError("Invalid snapshot repository name")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise CatalystError("Directory snapshots require the full pinned git commit, not a moving reference")
    tree = Path(stage.settings["storedir"], "snapshots", f"{repo_name}-{commit}.tree")
    if not tree.is_absolute() or tree.is_symlink() or not tree.is_dir():
        raise CatalystError(f"Verified directory snapshot is missing or unsafe: {tree}")
    receipt = tree / ".catalyst-snapshot.json"
    name_file = tree / "profiles/repo_name"
    layout = tree / "metadata/layout.conf"
    for path in (receipt, name_file, layout):
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(tree.resolve()):
            raise CatalystError(f"Invalid directory snapshot metadata: {path}")
    try:
        if receipt.stat().st_size > 4096:
            raise ValueError("oversized identity receipt")
        identity = json.loads(receipt.read_text(encoding="utf-8"))
        if not isinstance(identity, dict) or identity.get("repo_name") != repo_name or identity.get("treeish") != commit:
            raise ValueError("identity receipt does not match the requested repository and commit")
        if name_file.read_text(encoding="utf-8").strip() != repo_name or get_repo_name(str(tree)) != repo_name:
            raise ValueError("repository metadata does not match the requested name")
    except (OSError, UnicodeError, ValueError) as exc:
        raise CatalystError(f"Invalid directory snapshot {tree}: {exc}") from exc
    # Catalyst already supports directory repositories: set_repos() reads the
    # metadata directly, and bind() selects a regular bind instead of ro,loop.
    # This Portage RepoConfig API expects a string location, not pathlib.Path.
    stage.snapshot = str(tree)


def protect_snapshot_mounts(stage: StageBase) -> None:
    """Make only the snapshot and its duplicate bind mounts read-only."""
    sources = {str(Path(stage.snapshot))}
    targets: set[str] = set()
    # Catalyst may bind the first repository mount again below ROOT. Follow
    # those exact source/target links without touching unrelated repositories,
    # package caches, or other writable build mounts.
    changed = True
    while changed:
        changed = False
        for entry in stage.mount.values():
            if not entry["enable"] or str(Path(entry["source"])) not in sources:
                continue
            target = str(Path(stage.settings["chroot_path"] + str(entry["target"])))
            if target not in targets:
                targets.add(target)
                sources.add(target)
                changed = True
    if not targets:
        raise CatalystError("No validated snapshot bind mount was found")
    for target in sorted(targets):
        try:
            subprocess.run(["mount", "-o", "remount,bind,ro", "--", target], check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise CatalystError(f"Cannot protect snapshot bind mount {target}: {exc}") from exc


def main(argv: list[str] | None = None) -> None:
    extra = read_make_conf_extra()
    original = StageBase.set_snapshot
    original_bind = StageBase.bind
    original_write_make_conf = StageBase.write_make_conf
    original_setup_environment = StageBase.setup_environment

    def bind_readonly_snapshot(stage: StageBase) -> None:
        original_bind(stage)
        protect_snapshot_mounts(stage)

    def write_make_conf(stage: StageBase, setup: bool = True) -> None:
        original_write_make_conf(stage, setup=setup)
        if extra:
            # Catalyst rewrites this file both before emerge and at cleanup.
            destination = Path(stage.settings["chroot_path"] + stage.settings["make_conf"])
            with destination.open("a", encoding="utf-8") as stream:
                stream.write(extra)

    def setup_environment(stage: StageBase) -> None:
        original_setup_environment(stage)
        # Catalyst's distkernel helper selects its kernel image from
        # clst_basearch, but this Catalyst release never derives that variable
        # from subarch.  The project currently builds amd64 images only.
        if stage.settings.get("subarch") != "amd64":
            raise CatalystError("Installer Catalyst wrapper supports amd64 only")
        if stage.env.get("clst_basearch") not in (None, "amd64"):
            raise CatalystError("Catalyst base architecture conflicts with amd64 build")
        stage.env["clst_basearch"] = "amd64"
        if "jobserver-fifo" not in stage.settings:
            # This Catalyst version exports an empty NINJAOPTS without a FIFO.
            # It overrides make.conf and prevents ninja-utils from deriving
            # MAKEOPTS, leaving bare ninja free to use every host CPU.
            jobs = str(stage.settings.get("jobs", 1))
            load = stage.settings.get("load-average")
            if not re.fullmatch(r"[1-9][0-9]*", jobs):
                raise CatalystError("Ninja jobs must be a positive integer")
            options = [f"-j{jobs}"]
            if load is not None:
                if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", str(load)):
                    raise CatalystError("Ninja load average must be nonnegative and numeric")
                options.append(f"-l{load}")
            stage.env["NINJAOPTS"] = " ".join(options)

    StageBase.set_snapshot = set_directory_snapshot
    StageBase.bind = bind_readonly_snapshot
    StageBase.write_make_conf = write_make_conf
    StageBase.setup_environment = setup_environment
    try:
        catalyst_main(sys.argv[1:] if argv is None else argv)
    finally:
        StageBase.set_snapshot = original
        StageBase.bind = original_bind
        StageBase.write_make_conf = original_write_make_conf
        StageBase.setup_environment = original_setup_environment


if __name__ == "__main__":
    main()
