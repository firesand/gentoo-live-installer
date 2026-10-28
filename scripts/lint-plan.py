#!/usr/bin/env python3
"""Offline resolver lint for the installer's package configuration.

For every cell of an option matrix (desktop x init system x ...) this renders the Portage
configuration exactly as the installer writes it (``Configurer.configure_portage`` and the
package sets of each install stage), points a scratch ``PORTAGE_CONFIGROOT`` at it and walks
the dependency closure with the Portage API. It reports what ``emerge`` would refuse to do:

* dependencies whose USE dependency (``[flag]``) the configured USE of the dependency does not
  satisfy (``emerge`` would need ``--autounmask-use`` and abort),
* ``REQUIRED_USE`` violations of the chosen package versions,
* atoms with no visible candidate (masked, missing keyword or licence, or not in the tree).

No ``emerge`` is run and nothing outside the scratch directory is written. The walk
approximates Portage's resolver (best visible version only, first viable alternative of an
any-of group) and treats packages of the stage3 as installed, so it is a cheap gate in front
of the authoritative ``emerge --pretend`` check, not a replacement for it.

Usage::

    scripts/lint-plan.py                      # the default matrix on the pinned Gentoo tree
    scripts/lint-plan.py --cells kde-openrc   # one cell
    scripts/lint-plan.py --list               # show the available cells
    scripts/lint-plan.py --cells nvidia-pascal-openrc --hardware pascal-desktop-gtx1060 --set hardware.gpu_driver=nvidia
    scripts/lint-plan.py --cells wave5 --overlay /path/to/edorp-overlay   # the Wave 5 matrix (new desktops, NVIDIA, vendor tools, gaming)
    scripts/lint-plan.py --tree /var/db/repos/gentoo --json report.json

``--hardware NAME`` renders every cell with the hardware plan of the fixture tests/hardware/fixtures/NAME (what the installer would
decide on that machine), ``--set SECTION.FIELD=VALUE`` changes a manifest field in every cell, and ``--overlay DIR`` (repeatable) adds a
repository (the user's overlay "edorp", GURU, the companion overlay) next to ::gentoo. A cell can also name its own fixture, settings
and ``expect_atoms``: atoms that the plan must contain. A choice that the installer accepts but does not act on yet shows up there as
an error instead of a green cell that resolved nothing.

Findings (exit status 1): a dependency that needs a USE change (``emerge`` would print an autounmask suggestion), a REQUIRED_USE
violation, an atom with no visible candidate (masked, or only ``~amd64``), an expected atom missing from the plan. Information: the
package count against the desktop baseline of ``scripts/lint-plan-data/desktop-baseline.json`` (``--baseline``) and the packages
that are known to take hours to build from source (``scripts/lint-plan-data/heavy-builds.json``; ``--fail-on-heavy`` makes them a
finding for cells that must not compile them).

The tree is ``--tree``, ``$GENTOO_TREE``, the snapshot pinned in ``iso/pins.env`` (below
``.cache/iso-review/catalyst/snapshots``), or ``/var/db/repos/gentoo``. A tree without
``metadata/md5-cache`` (the pinned snapshot) needs a few minutes to generate metadata on the
first run; the result is kept in ``$GENTOO_LINT_SCRATCH`` (default: a directory below the
system temporary directory), so later runs are fast.

The stage3 package lists in ``scripts/lint-plan-data`` come from the official stage3 archives:
``tar -tf stage3-amd64-openrc-*.tar.xz | grep -E '^\\./var/db/pkg/[^/]+/[^/]+/$' | sed -E
's#^\\./var/db/pkg/##; s#/$##' | sort > stage3-openrc.txt``. Refresh them when ``iso/pins.env`` moves.

Exit status: 0 when every cell is clean, 1 when a cell has findings, 2 when the lint cannot run.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).resolve().parent / "lint-plan-data"
sys.dont_write_bytecode = True
for _path in (str(ROOT / "src"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

KEYS = ["EAPI", "SLOT", "IUSE", "RDEPEND", "PDEPEND", "DEPEND", "BDEPEND", "IDEPEND", "REQUIRED_USE",
        "KEYWORDS", "LICENSE", "RESTRICT", "PROPERTIES", "DEFINED_PHASES"]
IMPLICIT_ARCH = {"alpha", "amd64", "arm", "arm64", "hppa", "ia64", "loong", "m68k", "mips", "ppc", "ppc64", "riscv",
                 "s390", "sparc", "x86", "prefix", "prefix-guest", "prefix-stack"}


class LintUnavailable(RuntimeError):
    """The lint cannot run here (no Portage API or no Gentoo tree)."""


# --- locating the tree -------------------------------------------------------------------------


def pinned_tree(root: Path = ROOT) -> Path | None:
    """The snapshot named by GENTOO_TREEISH in iso/pins.env, if it was fetched into .cache."""
    try:
        pins = (root / "iso" / "pins.env").read_text()
    except OSError:
        return None
    treeish = next((line.partition("=")[2].strip() for line in pins.splitlines() if line.startswith("GENTOO_TREEISH=")), "")
    if not treeish:
        return None
    for base in (root, *root.parents):
        candidate = base / ".cache/iso-review/catalyst/snapshots" / f"gentoo-{treeish}.tree"
        if candidate.is_dir():
            return candidate
    return None


def find_tree(explicit: str | Path | None = None) -> Path | None:
    candidates: list[Path | None] = [Path(explicit) if explicit else None]
    candidates.append(Path(os.environ["GENTOO_TREE"]) if os.environ.get("GENTOO_TREE") else None)
    candidates.append(pinned_tree())
    candidates.append(Path("/var/db/repos/gentoo"))
    for candidate in candidates:
        if candidate and (candidate / "profiles").is_dir():
            return candidate
    return None


def default_scratch() -> Path:
    if os.environ.get("GENTOO_LINT_SCRATCH"):
        return Path(os.environ["GENTOO_LINT_SCRATCH"])
    return Path(tempfile.gettempdir()) / f"gentoo-installer-lint-{os.getuid()}"


def portage_available() -> bool:
    try:
        import portage  # noqa: F401
    except Exception:  # noqa: BLE001 - any import problem means "cannot lint"
        return False
    return True


# --- option matrix ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    name: str
    init: str = "openrc"
    desktop: str = "kde"
    overrides: tuple[tuple[str, Any], ...] = ()  # ("section.field", value)
    note: str = ""
    known_findings: bool = False  # a limitation outside this tool's scope; only run when named explicitly
    hardware: str = ""  # a fixture of tests/hardware/fixtures: render with the hardware plan of that machine
    expect_atoms: tuple[str, ...] = ()  # atoms the rendered plan must contain (a missing one means the feature has no effect yet)
    forbid_heavy: bool = False  # packages of heavy-builds.json in the closure are findings (a cell that has to install from binary packages)

    def manifest(self):
        from gentoo_installer.manifest import InstallManifest

        manifest = InstallManifest()
        manifest.storage.device = "/dev/vda"
        manifest.accounts.password = "lint-user-password"
        manifest.accounts.root_password = "lint-root-password"
        manifest.system.init = self.init
        manifest.system.stage_variant = f"amd64-{self.init}"
        manifest.system.desktop = self.desktop
        for key, value in self.overrides:
            section, _, name = key.partition(".")
            setattr(getattr(manifest, section), name, value)
        manifest.require_valid()
        return manifest


def parse_setting(text: str) -> tuple[str, Any]:
    """'section.field=value' with true/false and integers converted, as Cell.overrides expects."""
    key, separator, raw = text.partition("=")
    if not separator or key.count(".") != 1:
        raise ValueError(f"--set needs SECTION.FIELD=VALUE, not {text!r}")
    value: Any = {"true": True, "false": False}.get(raw.lower(), raw)
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        value = int(value)
    return key, value


def hardware_plan(fixture: str, manifest: Any) -> Any:
    """The hardware plan the installer would build on the machine of tests/hardware/fixtures/NAME (or of a derived fixture)."""
    from gentoo_installerd import hwplan
    from tests.hardware.helpers import DERIVED, fixture_dict

    if fixture not in DERIVED and not (ROOT / "tests" / "hardware" / "fixtures" / fixture).is_dir():
        raise SystemExit(f"No hardware fixture {fixture!r} in tests/hardware/fixtures")
    return hwplan.plan(manifest, fixture_dict(fixture))


def load_data(name: str, data_dir: Path = DATA) -> dict:
    return json.loads((data_dir / name).read_text())


def baseline_problem(desktop: str, init: str, count: int, baseline: dict | None = None, tolerance: float = 0.15) -> str:
    """'' when the number of packages a desktop pulls in is within ``tolerance`` of the baseline (desktops.md), else why not."""
    table = (baseline if baseline is not None else load_data("desktop-baseline.json"))["counts"]
    if desktop not in table:
        return ""
    expected = table[desktop][0 if init == "openrc" else 1]
    if abs(count - expected) > tolerance * expected:
        return f"{desktop}/{init} pulls in {count} packages, the baseline is {expected} (tolerance {tolerance:.0%}): the plan changed or the tree moved"
    return ""


def heavy_packages(closure: Sequence[str], table: dict | None = None, installed: Collection[str] = frozenset()) -> list[str]:
    """Members of the closure that take hours to compile ('cat/pkg-version (about N h)'); a source-only cell has to build every one of them.

    ``installed`` are the packages the stage3 already has (by category/package): the walk sees a newer version of them in the tree, which no cell has to compile.
    """
    hours = (table if table is not None else load_data("heavy-builds.json"))["hours"]
    found = []
    for cpv in closure:
        name = re.sub(r"-[0-9][0-9A-Za-z._+-]*$", "", cpv)
        if name in hours and name not in installed:
            found.append(f"{cpv} (about {hours[name]} h)")
    return found


def default_cells() -> list[Cell]:
    """Every desktop on both init systems with the installer's defaults (what a GUI user gets)."""
    return [Cell(f"{desktop}-{init}", init, desktop) for desktop in ("kde", "gnome", "xfce", "minimal") for init in ("openrc", "systemd")]


def extended_cells() -> list[Cell]:
    extra = [
        Cell("kde-full-openrc", "openrc", "kde", (("options.kde_apps", "full"),), "complete KDE applications"),
        Cell("kde-full-systemd", "systemd", "kde", (("options.kde_apps", "full"),)),
        Cell("kde-openrc-source", "openrc", "kde", (("portage.binhost", False),), "package.use must not depend on the binhost"),
        Cell("xfce-openrc-doas", "openrc", "xfce", (("accounts.privilege", "doas"),)),
        Cell("gnome-systemd-dualboot", "systemd", "gnome", (("system.dual_boot", True),)),
        Cell("minimal-openrc-btrfs-nocompanions", "openrc", "minimal",
             (("storage.filesystem", "btrfs"), ("options.install_hwmonux", False), ("options.install_portagehub", False),
              ("options.install_gentoo_settings", False), ("options.install_kernelsmith", False))),
        Cell("minimal-openrc-xfs-lvm", "openrc", "minimal", (("storage.filesystem", "xfs"), ("storage.lvm", True))),
        Cell("minimal-systemd-luks", "systemd", "minimal", (("storage.luks", True), ("storage.luks_passphrase", "lint-passphrase"))),
        Cell("minimal-systemd-sdboot", "systemd", "minimal", (("system.bootloader", "systemd-boot"),)),
        Cell("minimal-openrc-sdboot-uki", "openrc", "minimal", (("system.bootloader", "systemd-boot"), ("system.uki", True))),
        Cell("minimal-openrc-efistub", "openrc", "minimal", (("system.bootloader", "efi-stub"),)),
        Cell("minimal-openrc-bios-genkernel-free", "openrc", "minimal", (("storage.firmware", "bios"), ("options.microcode", False))),
        Cell("minimal-openrc-everything", "openrc", "minimal",
             (("options.flatpak", True), ("options.ssh", True), ("options.file_indexing", True), ("system.console_font", "ter-132b"))),
        Cell("xfce-systemd-networkd", "systemd", "xfce", (("network.manager", "systemd-networkd"),)),
        Cell("kde-openrc-dhcpcd", "openrc", "kde", (("network.manager", "dhcpcd"),)),
        Cell("kde-openrc-quiet", "openrc", "kde", (("options.bluetooth", False), ("options.printing", False), ("options.pipewire", False))),
        Cell("minimal-openrc-bluetooth-printing-audio", "openrc", "minimal",
             (("options.bluetooth", True), ("options.printing", True), ("options.pipewire", True)), "graphical-only packages stay out"),
        Cell("minimal-systemd-bluetooth-printing-audio", "systemd", "minimal",
             (("options.bluetooth", True), ("options.printing", True), ("options.pipewire", True))),
        Cell("kde-systemd-sdboot-uki", "systemd", "kde", (("system.bootloader", "systemd-boot"), ("system.uki", True))),
        Cell("minimal-openrc-hardened", "openrc", "minimal", (("system.stage_variant", "amd64-hardened-openrc"),)),
        Cell("minimal-openrc-nomultilib", "openrc", "minimal",
             (("system.multilib", False), ("system.stage_variant", "amd64-nomultilib-openrc"), ("options.install_hwmonux", False),
              ("options.install_portagehub", False), ("options.install_gentoo_settings", False), ("options.install_kernelsmith", False))),
    ]
    return default_cells() + extra


WAVE5_DESKTOP_ATOMS = {"cinnamon": "gnome-extra/cinnamon", "mate": "mate-base/mate", "lxqt": "lxqt-base/lxqt-meta", "gnome-light": "gnome-base/gnome-light",
                       "sway": "gui-wm/sway", "i3": "x11-wm/i3"}
NVIDIA_ON = (("hardware.gpu_driver", "nvidia"), ("hardware.nvidia_license_accepted", True))


def wave5_cells() -> list[Cell]:
    """The Wave 5 matrix: every new desktop on both inits, NVIDIA on each generation, vendor tools, gaming.

    Each cell lists the atoms the plan has to contain (``expect_atoms``), so a choice the installer accepts but does not act on yet
    fails with "missing from the plan" instead of resolving nothing. Overlay atoms (asusctl, supergfxctl) need ``--overlay``.
    """
    cells: list[Cell] = []
    for desktop, atom in WAVE5_DESKTOP_ATOMS.items():
        for init in ("openrc", "systemd"):
            cells.append(Cell(f"{desktop}-{init}", init, desktop, expect_atoms=(atom,), forbid_heavy=True, note="backlog B14: the desktop registry"))
    cells += [
        Cell("nvidia-rtx4090-kde-openrc", "openrc", "kde", NVIDIA_ON, "the user's PC: open kernel modules, branch 595", hardware="asus-desktop-rtx4090",
             expect_atoms=("x11-drivers/nvidia-drivers",)),
        Cell("nvidia-rtx4090-kde-systemd", "systemd", "kde", NVIDIA_ON, hardware="asus-desktop-rtx4090", expect_atoms=("x11-drivers/nvidia-drivers",)),
        Cell("nvidia-pascal-xfce-openrc", "openrc", "xfce", NVIDIA_ON, "Pascal: closed 580, newer branches masked (the mask must still leave a visible 580)",
             hardware="pascal-desktop-gtx1060", expect_atoms=("x11-drivers/nvidia-drivers",)),
        Cell("nvidia-blackwell-kde-openrc", "openrc", "kde", NVIDIA_ON, "Blackwell: 595 open only", hardware="blackwell-desktop-rtx5090",
             expect_atoms=("x11-drivers/nvidia-drivers",)),
        Cell("nvidia-hybrid-laptop-gnome-systemd", "systemd", "gnome", NVIDIA_ON, "Intel + NVIDIA laptop: PRIME offload", hardware="thinkpad-intel-nvidia-hybrid",
             expect_atoms=("x11-drivers/nvidia-drivers",)),
        Cell("nouveau-kepler-xfce-openrc", "openrc", "xfce", NVIDIA_ON, "Kepler: no proprietary driver, nouveau", hardware="kepler-desktop-gtx680"),
        Cell("secureboot-nvidia-kde-systemd", "systemd", "kde", NVIDIA_ON, "NVIDIA under Secure Boot: refused until backlog B07 step 2, so not part of 'wave5'",
             known_findings=True, hardware="asus-desktop-rtx4090-secureboot"),
        Cell("asus-rog-tools-kde-openrc", "openrc", "kde", NVIDIA_ON + (("hardware.devices", {"asus_tools": True}),), "asusctl and supergfxctl from the edorp overlay (--overlay)",
             hardware="asus-rog-zephyrus-intel-nvidia-mux", expect_atoms=("sys-power/asusctl", "sys-power/supergfxctl")),
        Cell("razer-devices-kde-openrc", "openrc", "kde", (("hardware.devices", {"openrazer": True, "razer_frontend": True}),), "OpenRazer and a front end",
             hardware="razer-blade-intel-nvidia", expect_atoms=("sys-apps/openrazer",)),
        Cell("openrgb-solaar-kde-openrc", "openrc", "kde", (("hardware.devices", {"openrgb": True, "solaar": True, "game_rules": True}),), "Aura, Logitech, controllers",
             hardware="asus-desktop-rtx4090", expect_atoms=("app-misc/openrgb",)),
        Cell("gaming-native-kde-openrc", "openrc", "kde", (("options.gaming", "native"), ("options.gaming_accept_external", True)) + NVIDIA_ON,
             "Steam from the steam-overlay; multilib", hardware="asus-desktop-rtx4090", expect_atoms=("games-util/steam-launcher",)),
        Cell("gaming-flatpak-kde-systemd", "systemd", "kde", (("options.gaming", "flatpak"),), "Steam as a Flatpak", hardware="asus-desktop-rtx4090",
             expect_atoms=("sys-apps/flatpak",)),
    ]
    return cells


def known_limitation_cells() -> list[Cell]:
    """Combinations that cannot resolve today because the published profiles have no desktop settings.

    KDE on these stage3 families is refused by the manifest itself (it needs a Standard or Desktop
    stage3), so only the desktops the manifest still allows, with a warning, are listed.
    """
    note = "profile without desktop settings (no-multilib/hardened have no desktop variant)"
    return [
        Cell("xfce-openrc-nomultilib", "openrc", "xfce", (("system.multilib", False), ("system.stage_variant", "amd64-nomultilib-openrc")), note, True),
    ]


def select_cells(spec: str) -> list[Cell]:
    if spec in {"default", ""}:
        return default_cells()
    if spec == "all":
        return extended_cells()
    if spec == "wave5":
        return [cell for cell in wave5_cells() if not cell.known_findings]
    if spec == "everything":
        return extended_cells() + [cell for cell in wave5_cells() if not cell.known_findings]
    available = {cell.name: cell for cell in [*extended_cells(), *known_limitation_cells(), *wave5_cells()]}
    chosen = []
    for name in (part.strip() for part in spec.split(",") if part.strip()):
        if name not in available:
            raise SystemExit(f"Unknown cell {name!r}; use --list to see the available cells")
        chosen.append(available[name])
    return chosen


# --- rendering the installer's configuration -----------------------------------------------------


@dataclass
class RenderedCell:
    root: Path  # the target root the Configurer wrote into
    config: Path  # scratch PORTAGE_CONFIGROOT
    profile: str
    atoms: list[str]
    plan: Any = None  # the hardware plan the configuration was rendered with


def load_stage3(init: str, data_dir: Path = DATA) -> set[str]:
    """Category/package names that the official stage3 for this init system already contains."""
    import portage

    names: set[str] = set()
    path = data_dir / f"stage3-{init}.txt"
    if not path.is_file():
        return names
    for line in path.read_text().splitlines():
        split = portage.versions.catpkgsplit(line.strip())
        if split:
            names.add(f"{split[0]}/{split[1]}")
    return names


def stage_atoms(configurer, manifest, plan=None) -> list[str]:
    """Every atom the install stages emerge, in order (kernel, base, bootloader, desktop)."""
    desktop = configurer.desktop_atoms(manifest, plan)
    layout = _layout_for(manifest)
    atoms = [*configurer.kernel_stage_atoms(manifest, layout, plan), *configurer.kernel_atoms(manifest),
             *configurer.essential_atoms(manifest, plan), *configurer.bootloader_atoms(manifest), *desktop.everything()]
    if manifest.system.init == "systemd" and manifest.storage.luks:
        atoms.append("sys-apps/systemd")
    return list(dict.fromkeys(atoms))


def _layout_for(manifest):
    from gentoo_installerd.storage import LayoutEntry

    layout = [LayoutEntry("/dev/vda2", "/", manifest.storage.filesystem)]
    if manifest.storage.firmware == "uefi":
        layout.append(LayoutEntry("/dev/vda1", "/efi", "vfat"))
    return layout


def overlay_name(path: Path) -> str:
    """The repository name of an overlay directory (profiles/repo_name), else its directory name."""
    try:
        name = (path / "profiles" / "repo_name").read_text().strip()
    except OSError:
        name = ""
    return name or path.name


def render_cell(cell: Cell, tree: Path, scratch: Path, plan=None, overlays: Sequence[Path] = ()) -> RenderedCell:
    """Write the installer's Portage configuration for the cell and build a scratch config root from it.

    Without a plan the cell gets the generic plan an installation builds when nothing was probed
    (Installer._plan: hwplan.plan(manifest, None)), so firmware and microcode are configured as on
    a real machine of unknown vendor.
    """
    from gentoo_installerd import hwplan
    from gentoo_installerd.configure import Configurer, Resources
    from gentoo_installerd.runner import DryRunRunner

    manifest = cell.manifest()
    if plan is None:
        plan = hwplan.plan(manifest, None)
    work = scratch / "cells" / cell.name
    if work.exists():
        shutil.rmtree(work)
    target = work / "target"
    bundle = work / "bundle"
    (bundle / "distfiles").mkdir(parents=True)
    (bundle / "overlay").symlink_to(ROOT / "packaging" / "gentoo-overlay", target_is_directory=True)
    for archive in ("hwmonux-1.0.0.tar.zst", "portagehub-0.4.0.tar.zst", "gentoo-settings-0.1.0.tar.zst", "kernelsmith-0.1.0.tar.zst"):
        (bundle / "distfiles" / archive).write_bytes(b"")
    target.mkdir()
    configurer = Configurer(DryRunRunner(), target, companion_bundle=bundle)
    configurer.resources = lambda: Resources(8, 16 * 1024**3, 12 * 1024**3)  # deterministic MAKEOPTS
    configurer.configure_portage(manifest, plan)
    configurer.configure_companion_overlay(manifest)
    atoms = stage_atoms(configurer, manifest, plan)
    atoms = list(dict.fromkeys([*atoms, *(str(atom) for atom in getattr(plan, "driver_atoms", None) or [])]))  # the drivers the kernel stage emerges on their own

    config = work / "config"
    shutil.copytree(target / "etc" / "portage", config / "etc" / "portage", symlinks=True)
    profile = manifest.effective_profile()
    repos = ["[DEFAULT]", "main-repo = gentoo", "", "[gentoo]", f"location = {tree}", "auto-sync = no", ""]
    overlay = target / "var/db/repos/gentoo-live-installer"
    if overlay.is_dir():
        repos.extend(("[gentoo-live-installer]", f"location = {overlay}", "masters = gentoo", "auto-sync = no", ""))
    for extra in overlays:  # a directory, or (name, location) when the location is where a container will see it
        name, location = extra if isinstance(extra, tuple) else (overlay_name(Path(extra)), Path(extra).resolve())
        repos.extend((f"[{name}]", f"location = {location}", "masters = gentoo", "auto-sync = no", ""))
    (config / "etc/portage/repos.conf").mkdir(parents=True, exist_ok=True)
    for stale in (config / "etc/portage/repos.conf").iterdir():
        stale.unlink()
    (config / "etc/portage/repos.conf/repos.conf").write_text("\n".join(repos))
    make_profile = config / "etc/portage/make.profile"
    if make_profile.is_symlink() or make_profile.exists():
        make_profile.unlink()
    make_profile.symlink_to(tree / "profiles" / profile)
    # Keep Portage's scratch state out of the system directories and make the config self-contained.
    with (config / "etc/portage/make.conf").open("a") as handle:
        handle.write("\n".join((f'PORTAGE_TMPDIR="{scratch / "tmp"}"', f'PORTAGE_DEPCACHEDIR="{scratch / "depcache"}"',
                                f'DISTDIR="{scratch / "distfiles"}"', f'PKGDIR="{scratch / "pkgdir"}"', "")))
    return RenderedCell(target, config, profile, atoms, plan)


# --- the closure walk --------------------------------------------------------------------------------


@dataclass
class Report:
    name: str
    profile: str = ""
    atoms: list[str] = field(default_factory=list)
    closure: int = 0
    packages: list[str] = field(default_factory=list)  # the closure, as category/package-version
    violations: list[dict] = field(default_factory=list)
    required_use: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0
    heavy: list[str] = field(default_factory=list)  # closure members that take hours to build from source
    notes: list[str] = field(default_factory=list)  # information: the baseline comparison, heavy builds that are tolerated
    forbid_heavy: bool = False

    @property
    def ok(self) -> bool:
        return not (self.violations or self.required_use or self.unresolved or self.errors or (self.forbid_heavy and self.heavy))

    def to_dict(self) -> dict:
        return {"name": self.name, "profile": self.profile, "ok": self.ok, "closure": self.closure, "atoms": self.atoms,
                "packages": self.packages, "violations": self.violations, "required_use": self.required_use, "unresolved": self.unresolved,
                "errors": self.errors, "seconds": round(self.seconds, 1), "heavy": self.heavy, "notes": self.notes}


def prepare_environment(scratch: Path) -> None:
    """Point Portage at scratch state. Must happen before Portage reads its configuration."""
    for name in ("tmp", "depcache", "distfiles", "pkgdir", "root"):
        (scratch / name).mkdir(parents=True, exist_ok=True)
    os.environ["PORTAGE_TMPDIR"] = str(scratch / "tmp")
    os.environ["PORTAGE_DEPCACHEDIR"] = str(scratch / "depcache")
    os.environ["DISTDIR"] = str(scratch / "distfiles")
    os.environ["PKGDIR"] = str(scratch / "pkgdir")


def lint_atoms(config_root: Path, scratch: Path, atoms: Sequence[str], stage3: set[str], *, name: str = "",
               profile: str = "", build_deps: bool = False, repo_name: str = "gentoo") -> Report:
    """Walk the dependency closure of ``atoms`` under the Portage configuration in ``config_root``."""
    import portage
    from portage.dep import Atom, check_required_use, use_reduce

    report = Report(name=name, profile=profile, atoms=list(atoms))
    started = time.monotonic()
    trees = portage.create_trees(config_root=str(config_root), target_root=str(scratch / "root"))
    root = list(trees)[0]
    base = trees[root]["vartree"].settings
    portdb = trees[root]["porttree"].dbapi
    cache: dict[str, tuple[dict, set[str], set[str]]] = {}

    def info(cpv: str):
        if cpv not in cache:
            metadata = dict(zip(KEYS, portdb.aux_get(cpv, KEYS)))
            metadata["repository"] = portdb.aux_get(cpv, ["repository"])[0] or repo_name
            settings = portage.config(clone=base)
            settings.setcpv(cpv, mydb=metadata)
            use = set(settings["PORTAGE_USE"].split())
            iuse = {flag.lstrip("+-") for flag in metadata["IUSE"].split()}
            cache[cpv] = (metadata, use, iuse)
        return cache[cpv]

    def best(atom: Atom) -> str | None:
        try:
            match = portdb.xmatch("bestmatch-visible", atom.without_use if atom.use else atom)
        except Exception:  # noqa: BLE001
            return None
        return match or None

    def why_invisible(atom: Atom) -> str:
        try:
            candidates = portdb.xmatch("match-all", atom.without_use if atom.use else atom)
        except Exception:  # noqa: BLE001
            candidates = []
        if not candidates:
            return "not in the tree"
        newest = candidates[-1]
        try:
            reasons = portage.getmaskingstatus(newest, settings=base, portdb=portdb)
        except Exception:  # noqa: BLE001
            reasons = []
        return f"{newest} is masked ({', '.join(reasons) or 'unknown reason'})"

    def usedep_problems(atom: Atom, cpv: str) -> str:
        if not atom.use:
            return ""
        _, use, iuse = info(cpv)
        problems = []
        for flag in atom.use.enabled:
            if flag not in iuse:
                if flag not in atom.use.missing_enabled:
                    problems.append(f"+{flag} (not in IUSE)")
            elif flag not in use:
                problems.append(f"+{flag}")
        for flag in atom.use.disabled:
            if flag not in iuse:
                if flag not in atom.use.missing_disabled:
                    problems.append(f"-{flag} (not in IUSE)")
            elif flag in use:
                problems.append(f"-{flag}")
        return ",".join(problems)

    def alternatives(group: Any, use: set[str]) -> list[Atom]:
        found: list[Atom] = []
        items = group if isinstance(group, list) else [group]
        index = 0
        while index < len(items):
            item = items[index]
            if item == "||":  # nested any-of: take its first branch
                branch = items[index + 1]
                found.extend(alternatives(branch[0] if branch else [], use))
                index += 2
                continue
            if isinstance(item, list):
                found.extend(alternatives(item, use))
            elif isinstance(item, Atom) and not item.blocker:
                found.append(item.evaluate_conditionals(use) if item.use else item)
            index += 1
        return found

    def process(tree: list, use: set[str], by: str) -> list[Atom]:
        found: list[Atom] = []
        index = 0
        while index < len(tree):
            item = tree[index]
            if item == "||":
                group = tree[index + 1]
                index += 2
                chosen: list[Atom] | None = None
                failures: list[list[str]] = []
                for alternative in group:
                    atoms_ = alternatives(alternative, use)
                    if not atoms_:
                        chosen = []
                        break
                    reasons = []
                    for atom_ in atoms_:
                        cpv_ = best(atom_)
                        if not cpv_:
                            reasons.append(f"{atom_}: {why_invisible(atom_)}")
                        elif usedep_problems(atom_, cpv_):
                            reasons.append(f"{atom_}: {usedep_problems(atom_, cpv_)}")
                    if not reasons:
                        chosen = atoms_
                        break
                    failures.append(reasons)
                if chosen is None:
                    report.violations.append({"by": by, "kind": "any-of", "detail": failures})
                else:
                    found.extend(chosen)
                continue
            if isinstance(item, list):
                found.extend(process(item, use, by))
            elif isinstance(item, Atom) and not item.blocker:
                atom_ = item.evaluate_conditionals(use) if item.use else item
                cpv_ = best(atom_)
                if not cpv_:
                    report.unresolved.append({"atom": str(atom_), "by": by, "reason": why_invisible(atom_)})
                else:
                    problems = usedep_problems(atom_, cpv_)
                    if problems:
                        report.violations.append({"by": by, "kind": "usedep", "atom": str(atom_), "dep": cpv_, "unmet": problems})
                    found.append(atom_)
            index += 1
        return found

    closure: dict[str, str] = {}
    queue: list[tuple[str, str]] = [(atom, "<installer>") for atom in atoms]
    dep_keys = ["RDEPEND", "PDEPEND"] + (["DEPEND", "BDEPEND", "IDEPEND"] if build_deps else [])
    while queue:
        atom_text, by = queue.pop(0)
        try:
            atom = Atom(atom_text)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"invalid atom {atom_text!r}: {exc}")
            continue
        cpv = best(atom)
        if not cpv:
            report.unresolved.append({"atom": atom_text, "by": by, "reason": why_invisible(atom)})
            continue
        cp = portage.versions.cpv_getkey(cpv)
        if cp in closure:
            continue
        closure[cp] = cpv
        metadata, use, iuse = info(cpv)
        required_use = metadata.get("REQUIRED_USE", "")
        if required_use.strip():
            def is_valid(flag: str, iuse=iuse) -> bool:
                return flag in iuse or flag.startswith(("kernel_", "elibc_", "userland_")) or flag in IMPLICIT_ARCH
            try:
                if not check_required_use(required_use, use, is_valid, eapi=metadata["EAPI"]):
                    report.required_use.append({"cpv": cpv, "required_use": required_use, "by": by})
            except Exception as exc:  # noqa: BLE001
                report.required_use.append({"cpv": cpv, "error": str(exc), "by": by})
        if cp in stage3 and not build_deps:
            continue  # installed in the stage3: its own dependencies are satisfied
        for key in dep_keys:
            text = metadata.get(key, "")
            if not text.strip():
                continue
            try:
                parsed = use_reduce(text, uselist=use, masklist=[], matchall=False, excludeall=[], is_src_uri=False,
                                    eapi=metadata["EAPI"], opconvert=False, flat=False, is_valid_flag=None, token_class=Atom)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{cpv}: cannot parse {key}: {exc}")
                continue
            for dependency in process(parsed, use, cp):
                queue.append((str(dependency.without_use) if dependency.use else str(dependency), cp))
    report.closure = len(closure)
    report.packages = sorted(closure.values())
    report.seconds = time.monotonic() - started
    return report


def lint_cell(cell: Cell, tree: Path, scratch: Path | None = None, *, plan=None, build_deps: bool = False, overlays: Sequence[Path] = (),
              baseline: bool = False) -> Report:
    """Render and lint one cell of the matrix.

    ``plan`` (or the cell's own ``hardware`` fixture) is the hardware plan the configuration is rendered with; ``overlays`` are
    further repositories; ``baseline`` compares the desktop's package count with desktop-baseline.json.
    """
    if not portage_available():
        raise LintUnavailable("the Portage Python API is not importable")
    scratch = scratch or default_scratch()
    prepare_environment(scratch)
    if plan is None and cell.hardware:
        plan = hardware_plan(cell.hardware, cell.manifest())
    rendered = render_cell(cell, tree, scratch, plan, overlays)
    stage3 = load_stage3(cell.init)
    report = lint_atoms(rendered.config, scratch, rendered.atoms, stage3, name=cell.name, profile=rendered.profile, build_deps=build_deps)
    report.forbid_heavy = cell.forbid_heavy
    report.errors += expectation_errors(cell, rendered)
    report.heavy = heavy_packages(report.packages, installed=stage3)
    if report.heavy:
        report.notes.append("builds that take hours without a binary package: " + ", ".join(report.heavy))
    if baseline and cell.desktop in load_data("desktop-baseline.json")["counts"]:
        report.notes.append(desktop_baseline_note(cell, rendered, tree, scratch, stage3, report))
    return report


def expectation_errors(cell: Cell, rendered: RenderedCell) -> list[str]:
    """Atoms a cell requires but the rendered plan lacks: the choice is accepted and has no effect (yet)."""
    have = {atom.split("[", 1)[0].split("::", 1)[0] for atom in rendered.atoms}
    plan_atoms = [str(atom).split("[", 1)[0] for atom in (rendered.plan.all_atoms() if rendered.plan is not None and hasattr(rendered.plan, "all_atoms") else [])]
    have.update(plan_atoms)
    return [f"{atom} is missing from the plan: the installer accepts this choice but does not act on it yet" for atom in cell.expect_atoms if atom not in have]


def desktop_baseline_note(cell: Cell, rendered: RenderedCell, tree: Path, scratch: Path, stage3: set[str], full: Report) -> str:
    """Walk the desktop's own atoms only (what desktops.md counted) and compare with the baseline."""
    from gentoo_installerd.configure import Configurer
    from gentoo_installerd.runner import DryRunRunner

    manifest = cell.manifest()
    atoms = list(Configurer(DryRunRunner(), rendered.root).desktop_atoms(manifest, rendered.plan).required)
    own = lint_atoms(rendered.config, scratch, atoms, stage3, name=cell.name + "-desktop", profile=rendered.profile)
    problem = baseline_problem(cell.desktop, cell.init, own.closure)
    return f"desktop packages: {own.closure} (whole cell {full.closure})" + (f"; BASELINE: {problem}" if problem else "; within the baseline")


# --- command line ------------------------------------------------------------------------------------


def format_report(report: Report, verbose: bool = False) -> str:
    status = "ok" if report.ok else "FINDINGS"
    lines = [f"[{report.name}] {status}: closure={report.closure} usedep/any-of={len(report.violations)} "
             f"required_use={len(report.required_use)} unresolved={len(report.unresolved)} errors={len(report.errors)} "
             f"({report.seconds:.1f}s, {report.profile})"]
    for item in report.violations:
        if item["kind"] == "usedep":
            lines.append(f"  USE-DEP  {item['by']} needs {item['atom']} but {item['dep']} has {item['unmet']} off/on")
        else:
            lines.append(f"  ANY-OF   {item['by']}: no alternative satisfies {json.dumps(item['detail'])[:300]}")
    for item in report.required_use:
        lines.append(f"  REQUIRED_USE  {item['cpv']} (via {item['by']}): {item.get('error') or item['required_use'][:200]}")
    for item in report.unresolved:
        lines.append(f"  UNRESOLVED  {item['atom']} (via {item['by']}): {item['reason']}")
    for error in report.errors:
        lines.append(f"  ERROR  {error}")
    for note in report.notes:
        lines.append(f"  NOTE  {note}")
    if report.forbid_heavy and report.heavy:
        lines.append("  HEAVY  " + ", ".join(report.heavy))
    if verbose:
        lines.append("  atoms: " + " ".join(report.atoms))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lint-plan.py", description="Offline resolver lint for the installer's package configuration")
    parser.add_argument("--tree", help="Gentoo repository to resolve against")
    parser.add_argument("--cells", default="default", help="'default', 'all' or a comma-separated list of cell names")
    parser.add_argument("--list", action="store_true", help="list the available cells and exit")
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    parser.add_argument("--scratch", type=Path, help="scratch directory (default: $GENTOO_LINT_SCRATCH or a temporary directory)")
    parser.add_argument("--build-deps", action="store_true", help="also walk build-time dependencies (much larger closure)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print the atoms of every cell")
    parser.add_argument("--hardware", default="", metavar="FIXTURE", help="render with the hardware plan of tests/hardware/fixtures/FIXTURE (every cell)")
    parser.add_argument("--set", action="append", default=[], metavar="SECTION.FIELD=VALUE",
                        help="change a manifest field in every cell, e.g. hardware.gpu_driver=nvidia (true/false/numbers are converted)")
    parser.add_argument("--overlay", action="append", default=[], type=Path, metavar="DIR",
                        help="add this repository (edorp, GURU, a snapshot of an overlay) next to ::gentoo; repeatable")
    parser.add_argument("--baseline", action="store_true", help="also compare the desktops' package counts with scripts/lint-plan-data/desktop-baseline.json")
    parser.add_argument("--fail-on-heavy", action="store_true", help="a package of heavy-builds.json in any cell's closure is a finding")
    return parser


def main(argv: Iterable[str] | None = None, out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.list:
        for cell in [*extended_cells(), *known_limitation_cells(), *wave5_cells()]:
            out(f"{cell.name:42} {cell.init:8} {cell.desktop:12} {cell.note}")
        return 0
    if not portage_available():
        out("lint-plan: the Portage Python API (sys-apps/portage) is not importable here")
        return 2
    tree = find_tree(args.tree)
    if tree is None:
        out("lint-plan: no Gentoo tree found; pass --tree or set GENTOO_TREE")
        return 2
    scratch = args.scratch or default_scratch()
    out(f"lint-plan: tree {tree}, scratch {scratch}")
    reports: list[Report] = []
    try:
        overrides = tuple(parse_setting(item) for item in args.set)
    except ValueError as exc:
        out(f"lint-plan: {exc}")
        return 2
    for overlay in args.overlay:
        if not (overlay / "profiles").is_dir():
            out(f"lint-plan: {overlay} is not a repository (no profiles directory)")
            return 2
    for cell in select_cells(args.cells):
        cell = dataclasses.replace(cell, overrides=cell.overrides + overrides, hardware=args.hardware or cell.hardware,
                                   forbid_heavy=cell.forbid_heavy or args.fail_on_heavy)
        report = lint_cell(cell, tree, scratch, build_deps=args.build_deps, overlays=args.overlay, baseline=args.baseline)
        reports.append(report)
        out(format_report(report, args.verbose))
    if args.json:
        args.json.write_text(json.dumps({"tree": str(tree), "cells": [report.to_dict() for report in reports]}, indent=1) + "\n")
    failing = [report.name for report in reports if not report.ok]
    out(f"lint-plan: {len(reports) - len(failing)}/{len(reports)} cells clean" + (f"; findings in: {', '.join(failing)}" if failing else ""))
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
