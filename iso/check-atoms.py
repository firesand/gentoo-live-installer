#!/usr/bin/env python3
"""Check every package atom of the live image against a Gentoo tree (hardware item I02).

    iso/check-atoms.py --tree /path/to/pinned/gentoo [--workspace DIR] [--guru DIR] [--extra FILE]

Run it on every bump of GENTOO_TREEISH in iso/pins.env (the checklist is in docs/ISO.md, section 11).
It reads, from the workspace:

  * livecd/packages and boot/kernel/gentoo/sources of the Catalyst specs,
  * the dependency strings (RDEPEND, DEPEND, BDEPEND, PDEPEND) of the project's own ebuilds,
  * the atoms of iso/portage/package.use and iso/portage/package.accept_keywords,
  * --extra FILE: one atom per line (the harness passes the atoms of the hardware plan).

and fails (exit status 1) when an atom
  * is not in the tree and not in the companion overlay (packaging/gentoo-overlay),
  * has no version that is stable on amd64 ("drops to ~amd64" after a pin bump), unless
    iso/portage/package.accept_keywords names it (the image accepts ~amd64 for that atom on purpose),
  * is pinned to an exact version (=cat/pkg-1.2) that no longer exists or is not stable,
  * is vendored into the overlay although the tree has it now (delete the copy),
  * names a USE flag in package.use that no version of the package declares.

--guru DIR (a ::guru checkout) adds a warning when a vendored package has a newer version there.
Dependencies of dependencies are not followed: scripts/resolve-gate.py does that.
Nothing is written; the tree is only read.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
import re
import sys
from typing import Iterable, Sequence

ATOM = re.compile(r"^(?P<op>[<>=~!]*)(?P<name>[a-z0-9][a-z0-9+_.-]*/[A-Za-z0-9][A-Za-z0-9+_.-]*?)(?P<version>-[0-9][0-9A-Za-z._]*(?:-r[0-9]+)?)?(?P<slot>:[^\[\s]*)?(?P<use>\[[^\]]*\])?\*?$")
LOCAL_PACKAGES = ("app-admin/gentoo-live-installer", "app-misc/hwmonux", "app-portage/portagehub", "acct-group/gentoo-installer")


@dataclasses.dataclass
class Finding:
    atom: str
    problem: str
    severity: str = "error"
    source: str = ""

    def __str__(self) -> str:
        where = f" ({self.source})" if self.source else ""
        return f"[{self.severity.upper():5}] {self.atom}{where}: {self.problem}"


def parse_atom(text: str) -> tuple[str, str, str] | None:
    """(operator, category/name, version) of an atom, or None when it is not a package atom."""
    match = ATOM.match(text)
    if not match:
        return None
    return match.group("op"), match.group("name"), (match.group("version") or "")[1:]


def spec_list(text: str, key: str) -> list[str]:
    """Values of `key:` in a Catalyst spec (the same rules as scripts/verify-iso.py parse_spec)."""
    values: list[str] = []
    collecting = False
    for line in text.splitlines():
        stripped = re.sub(r"\s*#.*$", "", line)
        if not stripped.strip():
            continue
        if line[:1] in (" ", "\t"):
            if collecting:
                values.append(stripped.strip())
            continue
        name, _, rest = stripped.partition(":")
        collecting = name.strip() == key
        if collecting and rest.strip():
            values.extend(rest.split())
    return values


def spec_value(text: str, key: str) -> str:
    found = spec_list(text, key)
    return found[0] if found else ""


def dependency_atoms(depend: str) -> list[str]:
    result = []
    for token in re.findall(r"[^\s()]+", depend):
        if token.endswith("?") or token in ("||", "^^", "??") or token.startswith("!"):
            continue
        if token.startswith("$"):
            continue
        result.append(token)
    return result


def ebuild_dependencies(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    atoms: list[str] = []
    for variable in ("RDEPEND", "DEPEND", "BDEPEND", "PDEPEND"):
        for match in re.finditer(rf'^{variable}\+?="(.*?)"', text, re.M | re.S):
            atoms += dependency_atoms(match.group(1))
    return atoms


def versions(directory: Path) -> dict[str, Path]:
    """version string (with revision) -> ebuild, 9999 live ebuilds left out."""
    found: dict[str, Path] = {}
    for path in sorted(directory.glob("*.ebuild")):
        version = path.stem[len(directory.name) + 1:]
        if version.startswith("9999") or re.fullmatch(r"9+", version.split("-")[0]):
            continue
        found[version] = path
    return found


def keywords(path: Path) -> set[str]:
    """Every word of every KEYWORDS="..." assignment (conditional ones, indented, included)."""
    words: set[str] = set()
    for match in re.finditer(r'^\s*KEYWORDS="([^"]*)"', path.read_text(encoding="utf-8", errors="replace"), re.M):
        words |= set(match.group(1).split())
    return words


def is_stable(path: Path) -> bool:
    return "amd64" in keywords(path)


def is_testing(path: Path) -> bool:
    return "~amd64" in keywords(path)


def iuse(directory: Path) -> set[str]:
    flags: set[str] = set()
    for path in directory.glob("*.ebuild"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r'^\s*IUSE\+?="([^"]*)"', text, re.M | re.S):
            flags |= {flag.lstrip("+-") for flag in match.group(1).split()}
        for match in re.finditer(r"^\s*IUSE\+?=\(([^)]*)\)", text, re.M | re.S):
            flags |= {flag.lstrip("+-").strip("\"'") for flag in match.group(1).split()}
        # USE_EXPAND flags are listed by their expanded names in the ebuild text
        flags |= set(re.findall(r"\b(?:grub_platforms|python_targets|python_single_target|video_cards|input_devices)_\w+", text))
    return flags


def accepted_testing(workspace: Path) -> set[str]:
    """Atoms (category/name) that iso/portage/package.accept_keywords accepts as ~amd64."""
    accepted: set[str] = set()
    base = workspace / "iso/portage/package.accept_keywords"
    for path in (sorted(base.iterdir()) if base.is_dir() else [base] if base.is_file() else []):
        for line in path.read_text().splitlines():
            line = line.split("#")[0].split()
            if line:
                parsed = parse_atom(line[0])
                if parsed:
                    accepted.add(parsed[1])
    return accepted


def collect(workspace: Path, extra: Iterable[str]) -> list[tuple[str, str]]:
    """(atom, where it came from) for every atom the image depends on."""
    atoms: list[tuple[str, str]] = []
    stage1 = (workspace / "iso/specs/livecd-stage1.spec").read_text()
    stage2 = (workspace / "iso/specs/livecd-stage2.spec").read_text()
    atoms += [(a, "livecd-stage1.spec") for a in spec_list(stage1, "livecd/packages")]
    atoms += [(a, "livecd-stage2.spec") for a in spec_list(stage2, "livecd/unmerge") if parse_atom(a)]
    for key in ("boot/kernel/gentoo/sources",):
        value = spec_value(stage2, key)
        if value:
            atoms.append((value, "livecd-stage2.spec"))
    overlay = workspace / "packaging/gentoo-overlay"
    for atom in LOCAL_PACKAGES:
        directory = overlay / atom
        for ebuild in sorted(directory.glob("*.ebuild")):
            atoms += [(a, f"{ebuild.name}") for a in ebuild_dependencies(ebuild)]
    for path in sorted((workspace / "iso/portage/package.use").glob("*")):
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                atoms.append((line.split()[0], f"package.use/{path.name}"))
    atoms += [(a.strip(), "--extra") for a in extra if a.strip() and not a.lstrip().startswith("#")]
    return atoms


def vendored(workspace: Path) -> list[str]:
    """category/name of every package of the overlay that is not the project's own."""
    overlay = workspace / "packaging/gentoo-overlay"
    found = []
    for directory in sorted(overlay.glob("*/*")):
        name = f"{directory.parent.name}/{directory.name}"
        if directory.is_dir() and list(directory.glob("*.ebuild")) and name not in LOCAL_PACKAGES:
            found.append(name)
    return found


def check(workspace: Path, tree: Path, extra: Sequence[str] = (), guru: Path | None = None) -> list[Finding]:
    findings: list[Finding] = []
    overlay = workspace / "packaging/gentoo-overlay"
    accepted = accepted_testing(workspace)
    own = set(LOCAL_PACKAGES) | set(vendored(workspace))
    seen: set[tuple[str, str]] = set()
    use_flags: dict[str, set[str]] = {}
    for atom, source in collect(workspace, extra):
        parsed = parse_atom(atom)
        if parsed is None:
            continue
        operator, name, version = parsed
        key = (atom, source.split("/")[0])
        if key in seen:
            continue
        seen.add(key)
        if "/" not in name:
            continue
        # virtuals and the project's own packages are checked the same way, but a local package has no keywords
        if name in LOCAL_PACKAGES:
            continue
        directory = tree / name
        if not directory.is_dir():
            if name in own and (overlay / name).is_dir():
                continue
            findings.append(Finding(atom, "is not in the tree and not in the companion overlay", source=source))
            continue
        available = versions(directory)
        if not available:
            findings.append(Finding(atom, "has only live (9999) ebuilds in the tree", source=source))
            continue
        if operator == "=" and version:
            path = available.get(version)
            if path is None:
                findings.append(Finding(atom, f"the pinned version does not exist any more (the tree has {', '.join(sorted(available))})", source=source))
            elif not is_stable(path):
                findings.append(Finding(atom, "the pinned version is not stable on amd64", source=source))
            continue
        stable = [v for v, p in available.items() if is_stable(p)]
        if not stable:
            testing = [v for v, p in available.items() if is_testing(p)]
            if name in accepted and testing:
                findings.append(Finding(atom, f"~amd64 only ({', '.join(testing)}), accepted by iso/portage/package.accept_keywords", "info", source))
            else:
                findings.append(Finding(atom, "no version is stable on amd64 (it dropped to ~amd64 or is masked): the build would fail; "
                                              "pick another package or accept the keyword on purpose", source=source))
    for line_source, atom, flags in package_use(workspace):
        parsed = parse_atom(atom)
        if parsed is None or parsed[1] in LOCAL_PACKAGES or parsed[1] in own:
            continue
        directory = tree / parsed[1]
        if not directory.is_dir():
            continue
        declared = use_flags.setdefault(parsed[1], iuse(directory))
        for flag in flags:
            bare = flag.lstrip("-")
            if bare not in declared and not re.match(r"(grub_platforms|python_|video_cards|input_devices)", bare):
                findings.append(Finding(atom, f"USE flag {bare} is not declared by any version", source=line_source))
    for name in vendored(workspace):
        if (tree / name).is_dir():
            findings.append(Finding(name, "is in the tree now: delete the vendored copy from packaging/gentoo-overlay", source="companion overlay"))
        if guru is not None and (guru / name).is_dir():
            ours = set(versions(overlay / name))
            newer = sorted(v for v in versions(guru / name) if v not in ours and _newer(v, ours))
            if newer:
                findings.append(Finding(name, f"::guru has newer versions: {', '.join(newer)}", "warning", "companion overlay"))
    return findings


def package_use(workspace: Path) -> list[tuple[str, str, list[str]]]:
    rows = []
    for path in sorted((workspace / "iso/portage/package.use").glob("*")):
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                atom, *flags = line.split()
                rows.append((f"package.use/{path.name}", atom, flags))
    return rows


def _newer(version: str, ours: set[str]) -> bool:
    def numeric(v: str) -> list[int]:
        return [int(p) for p in re.findall(r"\d+", re.sub(r"-r\d+$", "", v))]

    return all(numeric(version) > numeric(o) for o in ours) if ours else True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tree", required=True, type=Path, help="the pinned Gentoo tree (read only)")
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--guru", type=Path, help="a ::guru checkout, to look for newer vendored versions")
    parser.add_argument("--extra", type=Path, help="a file with one more atom per line (the atoms of the hardware plan)")
    parser.add_argument("--json", type=Path, help="write the findings as JSON")
    args = parser.parse_args(argv)
    if not (args.tree / "profiles/repo_name").is_file():
        print(f"{args.tree} is not a Gentoo tree (no profiles/repo_name)", file=sys.stderr)
        return 2
    extra = args.extra.read_text().splitlines() if args.extra else []
    findings = check(args.workspace, args.tree, extra, args.guru)
    for finding in findings:
        print(finding)
    errors = [f for f in findings if f.severity == "error"]
    if args.json:
        args.json.write_text(json.dumps([dataclasses.asdict(f) for f in findings], indent=2) + "\n")
    print(f"{len(errors)} errors, {len([f for f in findings if f.severity == 'warning'])} warnings, {len([f for f in findings if f.severity == 'info'])} notes")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
