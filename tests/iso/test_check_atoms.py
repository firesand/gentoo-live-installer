"""Hardware I02: iso/check-atoms.py fails when an atom of the image drops out of the stable tree."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from .conftest import ROOT, load_script

atoms = load_script("iso/check-atoms.py", "check_atoms")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


def ebuild(tree: Path, atom: str, version: str, keywords: str, iuse: str = "") -> None:
    name = atom.split("/")[1]
    write(tree / atom / f"{name}-{version}.ebuild", f'EAPI=8\nIUSE="{iuse}"\nKEYWORDS="{keywords}"\n')


@pytest.fixture()
def world(tmp_path: Path):
    """A tiny workspace and tree: stage1 with three packages, a kernel, one use line, one local package."""
    workspace, tree = tmp_path / "ws", tmp_path / "tree"
    write(tree / "profiles/repo_name", "gentoo\n")
    write(workspace / "iso/specs/livecd-stage1.spec", """
        livecd/packages:
        \tapp-misc/stable
        \t=sys-fs/pinned-1.0-r1
        \tapp-misc/testing
        \tapp-misc/missing
        """)
    write(workspace / "iso/specs/livecd-stage2.spec", """
        livecd/unmerge:
        \tnet-misc/netifrc
        boot/kernel/gentoo/sources: sys-kernel/gentoo-kernel
        """)
    write(workspace / "iso/portage/package.use/live", "app-misc/stable fancy\n")
    write(workspace / "iso/portage/package.accept_keywords/live", "")
    local = workspace / "packaging/gentoo-overlay/app-admin/gentoo-live-installer"
    write(local / "gentoo-live-installer-0.1.0.ebuild", 'EAPI=8\nRDEPEND="\n\tapp-misc/stable\n\tfoo? ( app-misc/depends )\n"\n')
    ebuild(tree, "app-misc/stable", "1.0", "~alpha amd64", "+fancy")
    ebuild(tree, "app-misc/depends", "1", "amd64")
    ebuild(tree, "sys-fs/pinned", "1.0-r1", "amd64")
    ebuild(tree, "app-misc/testing", "2.0", "~amd64")
    ebuild(tree, "net-misc/netifrc", "1", "amd64")
    ebuild(tree, "sys-kernel/gentoo-kernel", "6.18.57", "amd64")
    return workspace, tree


def problems(findings, severity="error"):
    return {f.atom: f.problem for f in findings if f.severity == severity}


def test_a_missing_package_and_a_package_that_dropped_to_testing_fail(world) -> None:
    workspace, tree = world
    found = problems(atoms.check(workspace, tree))
    assert set(found) == {"app-misc/testing", "app-misc/missing"}
    assert "not in the tree" in found["app-misc/missing"]
    assert "~amd64" in found["app-misc/testing"] and "stable" in found["app-misc/testing"]


def test_testing_is_fine_when_the_image_accepts_the_keyword_on_purpose(world) -> None:
    workspace, tree = world
    write(workspace / "iso/portage/package.accept_keywords/live", "app-misc/testing ~amd64\n")
    findings = atoms.check(workspace, tree)
    assert "app-misc/testing" not in problems(findings)
    assert "accepted by iso/portage/package.accept_keywords" in problems(findings, "info")["app-misc/testing"]


def test_an_exact_pin_must_exist_and_be_stable(world) -> None:
    workspace, tree = world
    (tree / "sys-fs/pinned/pinned-1.0-r1.ebuild").unlink()
    ebuild(tree, "sys-fs/pinned", "1.1", "amd64")
    assert "does not exist any more" in problems(atoms.check(workspace, tree))["=sys-fs/pinned-1.0-r1"]
    ebuild(tree, "sys-fs/pinned", "1.0-r1", "~amd64")
    assert "not stable" in problems(atoms.check(workspace, tree))["=sys-fs/pinned-1.0-r1"]


def test_dependencies_of_the_project_ebuilds_and_the_kernel_are_checked(world) -> None:
    workspace, tree = world
    (tree / "app-misc/depends/depends-1.ebuild").write_text('KEYWORDS="~amd64"\n')
    (tree / "sys-kernel/gentoo-kernel/gentoo-kernel-6.18.57.ebuild").write_text('KEYWORDS="~amd64"\n')
    found = problems(atoms.check(workspace, tree))
    assert "app-misc/depends" in found and "sys-kernel/gentoo-kernel" in found


def test_conditional_keywords_and_live_ebuilds_are_read_correctly(world) -> None:
    workspace, tree = world
    write(tree / "app-misc/missing/missing-9999.ebuild", 'KEYWORDS=""\n')
    assert "only live" in problems(atoms.check(workspace, tree))["app-misc/missing"]
    write(tree / "app-misc/missing/missing-1.ebuild", 'if [[ x ]]; then\n\tKEYWORDS="~amd64"\nelse\n\tKEYWORDS="amd64"\nfi\n')
    assert "app-misc/missing" not in problems(atoms.check(workspace, tree))


def test_an_unknown_use_flag_is_reported(world) -> None:
    workspace, tree = world
    write(workspace / "iso/portage/package.use/live", "app-misc/stable -fancy nonsense\n")
    found = problems(atoms.check(workspace, tree))
    assert any("nonsense" in v for v in found.values()) and not any("fancy is not" in v for v in found.values())


def test_a_vendored_copy_must_go_when_the_tree_has_the_package(world) -> None:
    workspace, tree = world
    vendored = workspace / "packaging/gentoo-overlay/gnome-extra/iio-sensor-proxy"
    write(vendored / "iio-sensor-proxy-3.9.ebuild", 'KEYWORDS="amd64"\n')
    assert "gnome-extra/iio-sensor-proxy" not in problems(atoms.check(workspace, tree))
    ebuild(tree, "gnome-extra/iio-sensor-proxy", "3.9", "amd64")
    assert "delete the vendored copy" in problems(atoms.check(workspace, tree))["gnome-extra/iio-sensor-proxy"]


def test_a_newer_guru_version_is_a_warning(world, tmp_path: Path) -> None:
    workspace, tree = world
    write(workspace / "packaging/gentoo-overlay/gnome-extra/iio-sensor-proxy/iio-sensor-proxy-3.9.ebuild", 'KEYWORDS="amd64"\n')
    guru = tmp_path / "guru"
    ebuild(guru, "gnome-extra/iio-sensor-proxy", "3.9", "~amd64")
    ebuild(guru, "gnome-extra/iio-sensor-proxy", "3.10", "~amd64")
    warnings = problems(atoms.check(workspace, tree, guru=guru), "warning")
    assert "3.10" in warnings["gnome-extra/iio-sensor-proxy"]


def test_extra_atoms_of_the_hardware_plan_are_checked(world) -> None:
    workspace, tree = world
    found = problems(atoms.check(workspace, tree, extra=["# comment", "sys-power/power-profiles-daemon", ""]))
    assert "sys-power/power-profiles-daemon" in found


def test_command_line_exit_status(world, tmp_path: Path) -> None:
    workspace, tree = world
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "iso/check-atoms.py"), "--workspace", str(workspace), *a], capture_output=True, text=True, check=False)
    failed = run("--tree", str(tree))
    assert failed.returncode == 1 and "[ERROR] app-misc/missing" in failed.stdout and "2 errors" in failed.stdout
    assert run("--tree", str(tmp_path / "nothing")).returncode == 2
    (workspace / "iso/specs/livecd-stage1.spec").write_text("livecd/packages:\n\tapp-misc/stable\n")
    ok = run("--tree", str(tree), "--json", str(tmp_path / "out.json"))
    assert ok.returncode == 0 and "0 errors" in ok.stdout and (tmp_path / "out.json").read_text().strip() == "[]"


# --- the real image against the pinned tree ------------------------------------------------------------


def test_every_atom_of_the_image_is_stable_in_the_pinned_tree(pinned_tree: Path) -> None:
    errors = [str(f) for f in atoms.check(ROOT, pinned_tree) if f.severity == "error"]
    assert not errors, "\n".join(errors)


def test_the_atoms_of_the_look_and_of_safe_graphics_are_collected() -> None:
    names = {atoms.parse_atom(a)[1] for a, _ in atoms.collect(ROOT, [])}
    assert {"x11-themes/arc-theme", "x11-themes/papirus-icon-theme", "media-fonts/fira-sans", "www-client/firefox-bin", "sys-kernel/gentoo-kernel"} <= names
    assert "sys-kernel/gentoo-kernel-bin" not in names
