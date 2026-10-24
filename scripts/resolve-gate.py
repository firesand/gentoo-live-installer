#!/usr/bin/env python3
"""Resolver gate: ``emerge --pretend`` of the installer's package sets inside a stage3 container.

``scripts/lint-plan.py`` walks the dependency closure offline with the Portage API, which is an
approximation. This runs the real resolver. For every cell it renders the Portage configuration
exactly as the installer writes it (lint-plan's ``render_cell``, optionally with the hardware plan
of a fixture machine), starts a throw-away container from the official stage3 image of the cell's
init system with the Gentoo tree bind-mounted read-only, uses the rendered configuration as
``/etc/portage`` and runs::

    emerge --pretend --noreplace [--getbinpkg] <every atom of the kernel, base, bootloader and desktop stages>

A cell passes when emerge exits 0. Outside the report directory nothing on the host is written;
each container is removed when it ends. The default tree is the host's /var/db/repos/gentoo, the
closest stand-in for the snapshot emerge-webrsync gives a new installation today.

Usage::

    scripts/resolve-gate.py                         # the default cells, binary host as the manifest says
    scripts/resolve-gate.py --cells kde-openrc --hardware amd-laptop-sof
    scripts/resolve-gate.py --cells all --jobs 3 --out build/resolve-gate/run1

Needs Docker; membership of the docker group is equivalent to root. The systemd cells need a
systemd stage3 image: --systemd-image NAME, or --import-systemd-seed TARBALL to import a stage3
archive whose signed .DIGESTS file verifies against the Gentoo release key.

Exit status: 0 when every cell resolves, 1 when a cell does not, 2 when the gate cannot run.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
for _path in (str(ROOT / "src"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

CONTAINER_TREE = Path("/var/db/repos/gentoo")
DEFAULT_IMAGES = {"openrc": "gentoo/stage3:amd64-openrc", "systemd": "gentoo/stage3:amd64-systemd"}
RELEASE_KEY = Path("/usr/share/openpgp-keys/gentoo-release.asc")
# make.conf lines lint-plan adds for its own scratch directories; the container uses its own.
SCRATCH_KEYS = ("PORTAGE_TMPDIR", "PORTAGE_DEPCACHEDIR", "DISTDIR", "PKGDIR")
# Lines of a failed emerge that say why; the full output is kept in the cell's log.
REASON_RE = re.compile(
    r"^(!!! .*|emerge: there are no (ebuilds|binary packages).*|The following (USE|REQUIRED_USE|keyword|license|mask) changes.*"
    r"|\s*- .*\(masked by: .*|\[blocks [Bb]\] .*|\s*The following REQUIRED_USE flag constraints are unsatisfied:.*"
    r"|\s*(any-of|exactly-one-of|at-most-one-of)\b.*|\(dependency required by .*)$")


def load_lint() -> Any:
    spec = importlib.util.spec_from_file_location("lint_plan", ROOT / "scripts" / "lint-plan.py")
    if spec is None or spec.loader is None:
        raise SystemExit("Cannot load scripts/lint-plan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["lint_plan"] = module
    spec.loader.exec_module(module)
    return module


def hardware_plan(fixture: str, manifest: Any) -> Any:
    """The hardware plan the installer would build on the fixture machine."""
    from gentoo_installer import hardware
    from gentoo_installerd import hwplan

    root = ROOT / "tests" / "hardware" / "fixtures" / fixture
    if not root.is_dir():
        raise SystemExit(f"No hardware fixture {fixture!r} in tests/hardware/fixtures")
    return hwplan.plan(manifest, hardware.probe(root).to_dict())


def docker(*args: str, timeout: float | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("docker", *args), text=True, capture_output=capture, timeout=timeout, check=False)


def image_exists(name: str) -> bool:
    return docker("image", "inspect", name).returncode == 0


def verify_seed(tarball: Path) -> None:
    """The stage3 archive matches its .DIGESTS file and that file carries a valid Gentoo release signature."""
    digests = tarball.with_name(tarball.name + ".DIGESTS")
    if not digests.is_file() or not RELEASE_KEY.is_file():
        raise SystemExit(f"Cannot verify {tarball.name}: need {digests.name} and {RELEASE_KEY}")
    with tempfile.TemporaryDirectory() as home:
        keyring = Path(home) / "release.gpg"
        subprocess.run(("gpg", "--homedir", home, "--batch", "--quiet", "--dearmor", "--output", str(keyring), str(RELEASE_KEY)), check=True)
        verified = subprocess.run(("gpgv", "--homedir", home, "--keyring", str(keyring), str(digests)), capture_output=True, text=True, check=False)
    if verified.returncode != 0:
        raise SystemExit(f"The signature of {digests.name} does not verify:\n{verified.stderr.strip()}")
    text = digests.read_text()
    match = re.search(r"# BLAKE2B HASH\n([0-9a-f]{128})\s+" + re.escape(tarball.name) + r"\n", text)
    if not match:
        raise SystemExit(f"{digests.name} has no BLAKE2B line for {tarball.name}")
    digest = hashlib.blake2b()
    with tarball.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != match.group(1):
        raise SystemExit(f"{tarball.name} does not match the BLAKE2B hash in {digests.name}")


def import_seed(tarball: Path) -> str:
    stamp = re.search(r"(\d{8}T\d{6}Z)", tarball.name)
    name = f"gentoo-installer-stage3:amd64-systemd-{stamp.group(1).lower() if stamp else 'seed'}"
    if image_exists(name):
        return name
    verify_seed(tarball)
    result = docker("import", str(tarball), name, timeout=1800)
    if result.returncode != 0:
        raise SystemExit(f"docker import failed: {result.stderr.strip()}")
    return name


@dataclass
class CellResult:
    name: str
    image: str
    profile: str = ""
    binhost: bool = False
    hardware: str = ""
    atoms: list[str] = field(default_factory=list)
    returncode: int | None = None
    seconds: float = 0.0
    reasons: list[str] = field(default_factory=list)
    log: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.error


def container_config(rendered: Any) -> Path:
    """Drop lint-plan's host scratch paths from make.conf; the container keeps Portage's defaults."""
    make_conf = rendered.config / "etc/portage/make.conf"
    kept = [line for line in make_conf.read_text().splitlines() if not line.split("=", 1)[0].strip() in SCRATCH_KEYS]
    make_conf.write_text("\n".join(kept) + "\n")
    return rendered.config / "etc/portage"


def run_cell(lint: Any, cell: Any, args: argparse.Namespace, images: dict[str, str], out: Path) -> CellResult:
    manifest = cell.manifest()
    image = images[cell.init]
    result = CellResult(cell.name, image, binhost=manifest.portage.binhost and not args.no_binhost, hardware=args.hardware or "")
    scratch = out / "cells" / cell.name
    try:
        plan = hardware_plan(args.hardware, manifest) if args.hardware else None
        rendered = lint.render_cell(cell, CONTAINER_TREE, out / "render", plan)
    except Exception as exc:  # noqa: BLE001 - a broken cell must not stop the others
        result.error = f"Could not render the installer configuration: {type(exc).__name__}: {exc}"
        return result
    result.profile, result.atoms = rendered.profile, list(rendered.atoms)
    portage_dir = container_config(rendered)
    scratch.mkdir(parents=True, exist_ok=True)
    # The installer's own options (Configurer.emerge/pretend; EMERGE_DEFAULT_OPTS from its make.conf):
    # the gate predicts the real install instead of being stricter than it.
    command = ["emerge", "--pretend", "--noreplace", "--color=n", "--nospinner", "--verbose=n"]
    command.append("--getbinpkg" if result.binhost else "--getbinpkg=n")
    command.extend(result.atoms)
    (scratch / "command.txt").write_text(" ".join(command) + "\n")
    name = f"gentoo-resolve-gate-{os.getpid()}-{cell.name}"
    docker_args = [
        "run", "--rm", "--name", name, "--memory", "4g", "--cpus", "4", "--pids-limit", "4096",
        "--volume", f"{args.tree}:{CONTAINER_TREE}:ro",
        "--volume", f"{portage_dir}:/etc/portage:ro",
        # The rendered overlay and companion bundle live below the report and the repository.
        "--volume", f"{out}:{out}:ro", "--volume", f"{ROOT}:{ROOT}:ro",
    ]
    if not result.binhost:
        docker_args += ["--network", "none"]
    docker_args += [image, *command]
    started = time.monotonic()
    try:
        completed = docker(*docker_args, timeout=args.timeout)
        result.returncode = completed.returncode
        output = completed.stdout + completed.stderr
    except subprocess.TimeoutExpired as exc:
        docker("rm", "--force", name)
        result.error = f"emerge did not finish within {args.timeout:.0f} s"
        output = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
    result.seconds = round(time.monotonic() - started, 1)
    log = scratch / "emerge.log"
    log.write_text(output)
    result.log = str(log)
    if not result.ok:
        result.reasons = list(dict.fromkeys(line.rstrip() for line in output.splitlines() if REASON_RE.match(line)))[:40]
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cells", default="default", help="'default', 'all' or comma-separated cell names (see scripts/lint-plan.py --list)")
    parser.add_argument("--tree", type=Path, default=Path("/var/db/repos/gentoo"), help="Gentoo tree to resolve against (read-only)")
    parser.add_argument("--hardware", default="", help="build the hardware plan from tests/hardware/fixtures/NAME")
    parser.add_argument("--no-binhost", action="store_true", help="resolve source-only even when the manifest uses the binary host")
    parser.add_argument("--openrc-image", default=DEFAULT_IMAGES["openrc"])
    parser.add_argument("--systemd-image", default=DEFAULT_IMAGES["systemd"])
    parser.add_argument("--import-systemd-seed", type=Path, help="import this verified stage3-amd64-systemd archive as the systemd image")
    parser.add_argument("--jobs", type=int, default=2, help="containers at the same time")
    parser.add_argument("--timeout", type=float, default=1800.0, help="seconds per cell")
    parser.add_argument("--out", type=Path, help="report directory (default build/resolve-gate/<UTC time>)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if shutil.which("docker") is None or docker("info", timeout=60).returncode != 0:
        print("resolve-gate: Docker is not available", file=sys.stderr)
        return 2
    if not (args.tree / "profiles").is_dir():
        print(f"resolve-gate: {args.tree} is not a Gentoo tree", file=sys.stderr)
        return 2
    args.tree = args.tree.resolve()
    lint = load_lint()
    cells = lint.select_cells(args.cells)
    images = {"openrc": args.openrc_image, "systemd": args.systemd_image}
    if args.import_systemd_seed:
        images["systemd"] = import_seed(args.import_systemd_seed.resolve())
    for init in sorted({cell.init for cell in cells}):
        if not image_exists(images[init]):
            print(f"resolve-gate: image {images[init]} for the {init} cells is missing (pull it, or use --import-systemd-seed)", file=sys.stderr)
            return 2
    out = (args.out or ROOT / "build" / "resolve-gate" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())).resolve()
    out.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        results = list(pool.map(lambda cell: run_cell(lint, cell, args, images, out), cells))
    for result in results:
        mode = "binhost" if result.binhost else "source"
        status = "ok" if result.ok else "FAILED"
        print(f"[{result.name}] {status} ({mode}, {result.profile}, {len(result.atoms)} atoms, {result.seconds:.0f}s) {result.log}")
        for line in ([result.error] if result.error else []) + result.reasons:
            print(f"    {line}")
    report = {"tree": str(args.tree), "hardware": args.hardware, "images": images, "cells": [vars(r) | {"ok": r.ok} for r in results]}
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"resolve-gate: {sum(r.ok for r in results)}/{len(results)} cells resolve; report {out / 'report.json'}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
