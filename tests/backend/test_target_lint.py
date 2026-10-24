"""Tests for scripts/lint-plan.py: the offline resolver lint of the installer's package configuration."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("lint_plan", ROOT / "scripts" / "lint-plan.py")
lint = importlib.util.module_from_spec(SPEC)
sys.modules["lint_plan"] = lint
SPEC.loader.exec_module(lint)

needs_portage = unittest.skipUnless(lint.portage_available(), "the Portage Python API is not available")


def make_tree(base: Path, ebuilds: dict[str, str], masks: str = "") -> Path:
    """A tiny Gentoo-style repository; ebuild names are category/package-version."""
    tree = base / "tree"
    profile = tree / "profiles/default/linux/amd64/test"
    profile.mkdir(parents=True)
    (tree / "metadata").mkdir()
    (tree / "metadata/layout.conf").write_text("masters =\nthin-manifests = true\n")
    (tree / "profiles/repo_name").write_text("synth\n")
    (tree / "profiles/arch.list").write_text("amd64\n")
    (tree / "profiles/categories").write_text("app-test\nlib-test\n")
    (tree / "profiles/use.desc").write_text("feature - a feature\nother - other\nalpha - a\nbeta - b\n")
    (profile / "eapi").write_text("8\n")
    (profile / "make.defaults").write_text('ARCH="amd64"\nACCEPT_KEYWORDS="amd64"\nUSERLAND="GNU"\nKERNEL="linux"\nELIBC="glibc"\n'
                                           'IUSE_IMPLICIT="prefix prefix-guest prefix-stack"\n')
    if masks:
        (tree / "profiles/package.mask").write_text(masks)
    for name, text in ebuilds.items():
        category, _, version = name.partition("/")
        package = version.rsplit("-", 1)[0]
        directory = tree / category / package
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{version}.ebuild").write_text('EAPI=8\nSLOT="0"\nKEYWORDS="amd64"\nDESCRIPTION="x"\nLICENSE="GPL-2"\n' + text)
    return tree


@needs_portage
class ClosureWalkTests(unittest.TestCase):
    scratch: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)
        cls.scratch = cls.base / "scratch"
        lint.prepare_environment(cls.scratch)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def lint(self, ebuilds: dict[str, str], atoms: list[str], *, use: str = "", masks: str = "", stage3: set[str] | None = None):
        case = Path(tempfile.mkdtemp(dir=self.base))
        tree = make_tree(case, ebuilds, masks)
        config = case / "config"
        (config / "etc/portage/repos.conf").mkdir(parents=True)
        (config / "etc/portage/repos.conf/synth.conf").write_text(f"[DEFAULT]\nmain-repo = synth\n\n[synth]\nlocation = {tree}\nauto-sync = no\n")
        os.symlink(tree / "profiles/default/linux/amd64/test", config / "etc/portage/make.profile")
        (config / "etc/portage/make.conf").write_text('ACCEPT_LICENSE="*"\n')
        if use:
            (config / "etc/portage/package.use").write_text(use)
        return lint.lint_atoms(config, self.scratch, atoms, stage3 or set(), name="synthetic", repo_name="synth")

    LIB = {"lib-test/lib-1": 'IUSE="feature other"\n'}

    def test_a_consistent_closure_is_clean(self) -> None:
        report = self.lint({**self.LIB, "app-test/app-1": 'RDEPEND="lib-test/lib"\n'}, ["app-test/app"])
        self.assertTrue(report.ok, lint.format_report(report))
        self.assertEqual((report.closure, report.packages), (2, ["app-test/app-1", "lib-test/lib-1"]))

    def test_an_unmet_use_dependency_is_reported_until_the_flag_is_configured(self) -> None:
        ebuilds = {**self.LIB, "app-test/app-1": 'RDEPEND="lib-test/lib[feature]"\n'}
        report = self.lint(ebuilds, ["app-test/app"])
        self.assertFalse(report.ok)
        self.assertEqual(report.violations, [{"by": "app-test/app", "kind": "usedep", "atom": "lib-test/lib[feature]", "dep": "lib-test/lib-1", "unmet": "+feature"}])
        self.assertIn("USE-DEP  app-test/app needs lib-test/lib[feature]", lint.format_report(report))
        self.assertTrue(self.lint(ebuilds, ["app-test/app"], use="lib-test/lib feature\n").ok)

    def test_a_negative_use_dependency_is_checked_too(self) -> None:
        ebuilds = {**self.LIB, "app-test/app-1": 'RDEPEND="lib-test/lib[-feature]"\n'}
        self.assertTrue(self.lint(ebuilds, ["app-test/app"]).ok)
        report = self.lint(ebuilds, ["app-test/app"], use="lib-test/lib feature\n")
        self.assertEqual(report.violations[0]["unmet"], "-feature")

    def test_required_use_is_evaluated_with_the_configured_flags(self) -> None:
        ebuilds = {"app-test/app-1": 'IUSE="alpha beta"\nREQUIRED_USE="alpha? ( beta )"\n'}
        self.assertTrue(self.lint(ebuilds, ["app-test/app"]).ok)
        report = self.lint(ebuilds, ["app-test/app"], use="app-test/app alpha\n")
        self.assertEqual([item["cpv"] for item in report.required_use], ["app-test/app-1"])
        self.assertTrue(self.lint(ebuilds, ["app-test/app"], use="app-test/app alpha beta\n").ok)

    def test_masked_and_missing_atoms_are_unresolved_with_a_reason(self) -> None:
        ebuilds = {**self.LIB, "app-test/app-1": 'RDEPEND="lib-test/lib app-test/ghost"\n'}
        report = self.lint(ebuilds, ["app-test/app"], masks="lib-test/lib\n")
        reasons = {item["atom"]: item["reason"] for item in report.unresolved}
        self.assertIn("masked", reasons["lib-test/lib"])
        self.assertEqual(reasons["app-test/ghost"], "not in the tree")
        self.assertEqual(self.lint({}, ["app-test/absent"]).unresolved[0]["reason"], "not in the tree")

    def test_any_of_groups_pick_the_first_viable_alternative(self) -> None:
        ebuilds = {**self.LIB, "app-test/app-1": 'RDEPEND="|| ( app-test/ghost lib-test/lib )"\n'}
        self.assertTrue(self.lint(ebuilds, ["app-test/app"]).ok)
        broken = {**self.LIB, "app-test/app-1": 'RDEPEND="|| ( app-test/ghost lib-test/lib[feature] )"\n'}
        report = self.lint(broken, ["app-test/app"])
        self.assertEqual([item["kind"] for item in report.violations], ["any-of"])

    def test_conditional_dependencies_follow_the_configured_flags(self) -> None:
        ebuilds = {**self.LIB, "app-test/app-1": 'IUSE="alpha"\nRDEPEND="alpha? ( lib-test/lib[feature] )"\n'}
        self.assertTrue(self.lint(ebuilds, ["app-test/app"]).ok)
        self.assertFalse(self.lint(ebuilds, ["app-test/app"], use="app-test/app alpha\n").ok)

    def test_packages_of_the_stage3_are_assumed_to_be_installed(self) -> None:
        ebuilds = {**self.LIB, "app-test/app-1": 'RDEPEND="lib-test/lib"\n', "app-test/base-1": 'RDEPEND="lib-test/lib[feature]"\n'}
        self.assertFalse(self.lint(ebuilds, ["app-test/base"]).ok)
        report = self.lint(ebuilds, ["app-test/base"], stage3={"app-test/base"})
        self.assertTrue(report.ok)
        self.assertEqual(report.closure, 1)

    def test_report_serialises_for_the_json_output(self) -> None:
        report = self.lint({"app-test/app-1": ""}, ["app-test/app"])
        data = json.loads(json.dumps(report.to_dict()))
        self.assertEqual((data["ok"], data["closure"], data["atoms"]), (True, 1, ["app-test/app"]))


class RenderingTests(unittest.TestCase):
    """The lint renders the installer's real configuration; no Portage is needed for that part."""

    def render(self, cell: lint.Cell):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tree = Path(temporary.name, "tree")
        tree.mkdir()
        return lint.render_cell(cell, tree, Path(temporary.name, "scratch")), Path(temporary.name)

    def test_the_default_matrix_covers_every_desktop_on_both_init_systems(self) -> None:
        cells = lint.default_cells()
        self.assertEqual({(cell.desktop, cell.init) for cell in cells},
                         {(desktop, init) for desktop in ("kde", "gnome", "xfce", "minimal") for init in ("openrc", "systemd")})
        self.assertEqual(len({cell.name for cell in lint.extended_cells()}), len(lint.extended_cells()))
        self.assertEqual([cell.name for cell in lint.select_cells("kde-openrc,xfce-systemd")], ["kde-openrc", "xfce-systemd"])
        with self.assertRaises(SystemExit):
            lint.select_cells("no-such-cell")

    def test_every_cell_is_a_valid_manifest(self) -> None:
        for cell in lint.extended_cells():
            with self.subTest(cell=cell.name):
                self.assertEqual(cell.manifest().validate(), [])

    def test_configuration_comes_from_the_installer_code(self) -> None:
        rendered, base = self.render(lint.Cell("kde-openrc"))
        config = rendered.config / "etc/portage"
        self.assertIn("dev-qt/qtpositioning geoclue", (config / "package.use/installer").read_text())
        self.assertNotIn("CPU_FLAGS_X86", (config / "make.conf").read_text())
        self.assertEqual(rendered.profile, "default/linux/amd64/23.0/desktop/plasma")
        self.assertTrue((config / "make.profile").is_symlink())
        self.assertIn("[gentoo-live-installer]", (config / "repos.conf/repos.conf").read_text())
        self.assertIn("app-misc/liquidctl ~amd64", (config / "package.accept_keywords/gentoo-installer-companions").read_text())
        for atom in ("sys-kernel/installkernel", "sys-kernel/gentoo-kernel-bin", "sys-kernel/dracut", "sys-boot/grub", "kde-plasma/plasma-meta",
                     "app-misc/hwmonux", "app-portage/portagehub", "sys-fs/dosfstools", "app-admin/sudo"):
            self.assertIn(atom, rendered.atoms)

    def test_cells_reflect_their_options(self) -> None:
        cells = {cell.name: cell for cell in lint.extended_cells()}
        rendered, _ = self.render(cells["minimal-systemd-luks"])
        self.assertIn("sys-fs/cryptsetup", rendered.atoms)
        systemd = next(line for line in (rendered.config / "etc/portage/package.use/installer").read_text().splitlines() if line.startswith("sys-apps/systemd "))
        self.assertIn("cryptsetup", systemd.split())
        rendered, _ = self.render(cells["gnome-systemd-dualboot"])
        self.assertIn("sys-boot/os-prober", rendered.atoms)
        self.assertIn("sys-boot/grub mount", (rendered.config / "etc/portage/package.use/installer").read_text())
        rendered, _ = self.render(cells["minimal-openrc-btrfs-nocompanions"])
        self.assertIn("sys-fs/btrfs-progs", rendered.atoms)
        self.assertNotIn("app-misc/hwmonux", rendered.atoms)
        self.assertFalse((rendered.config / "etc/portage/repos.conf/gentoo-live-installer.conf").exists())

    def test_binhost_does_not_change_the_use_configuration(self) -> None:
        binhost, _ = self.render(lint.Cell("a", "openrc", "kde"))
        source, _ = self.render(lint.Cell("b", "openrc", "kde", (("portage.binhost", False),)))
        self.assertEqual((binhost.config / "etc/portage/package.use/installer").read_text(),
                         (source.config / "etc/portage/package.use/installer").read_text())
        self.assertEqual(binhost.atoms, source.atoms)

    def test_stage3_lists_exist_for_both_init_systems(self) -> None:
        for init in ("openrc", "systemd"):
            names = (lint.DATA / f"stage3-{init}.txt").read_text().splitlines()
            self.assertGreater(len(names), 250, init)
            self.assertTrue(all(name.count("/") == 1 for name in names), init)
            self.assertTrue(any(name.startswith("sys-apps/portage-") for name in names), init)
            self.assertTrue(any(name.startswith("sys-libs/glibc-") for name in names), init)
        systemd = (lint.DATA / "stage3-systemd.txt").read_text()
        self.assertIn("sys-apps/systemd-", systemd)
        self.assertNotIn("sys-apps/systemd-", (lint.DATA / "stage3-openrc.txt").read_text().replace("systemd-utils", ""))

    def test_command_line_reports_a_missing_portage_or_tree(self) -> None:
        output: list[str] = []
        self.assertEqual(lint.main(["--list"], output.append), 0)
        self.assertTrue(any(line.startswith("kde-openrc") for line in output))
        output.clear()
        original = lint.find_tree
        lint.find_tree = lambda explicit=None: None
        try:
            self.assertEqual(lint.main([], output.append), 2)
            self.assertTrue("no Gentoo tree found" in output[0] or "not importable" in output[0], output)
        finally:
            lint.find_tree = original

    def test_tree_discovery_prefers_the_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            (tree / "profiles").mkdir()
            self.assertEqual(lint.find_tree(tree), tree)
            self.assertNotEqual(lint.find_tree(tree / "missing"), tree / "missing")

    def test_the_pinned_snapshot_is_found_from_the_pins_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "iso").mkdir()
            (root / "iso/pins.env").write_text("GENTOO_TREEISH=abc123\n")
            self.assertIsNone(lint.pinned_tree(root))
            snapshot = root / ".cache/iso-review/catalyst/snapshots/gentoo-abc123.tree"
            snapshot.mkdir(parents=True)
            self.assertEqual(lint.pinned_tree(root), snapshot)


@needs_portage
class RealTreeTests(unittest.TestCase):
    """The installer's real option matrix against a real Gentoo tree.

    Opt-in because it needs a repository and, on a tree without a metadata cache, minutes of metadata
    generation: GENTOO_LINT_REAL=1 (optionally GENTOO_TREE=... and GENTOO_LINT_SCRATCH=...).
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not os.environ.get("GENTOO_LINT_REAL"):
            raise unittest.SkipTest("set GENTOO_LINT_REAL=1 to lint the installer against a real Gentoo tree")
        cls.tree = lint.find_tree()
        if cls.tree is None:
            raise unittest.SkipTest("no Gentoo tree found")
        cls.scratch = lint.default_scratch()

    def test_every_default_and_common_combination_resolves(self) -> None:
        failures = []
        for cell in lint.extended_cells():
            report = lint.lint_cell(cell, self.tree, self.scratch)
            if not report.ok:
                failures.append(lint.format_report(report))
        self.assertEqual(failures, [], "\n".join(failures))

    def test_the_original_failures_would_be_caught(self) -> None:
        cell = lint.Cell("kde-full-openrc", "openrc", "kde", (("options.kde_apps", "full"),))
        rendered = lint.render_cell(cell, self.tree, self.scratch)
        make_conf = rendered.config / "etc/portage/make.conf"
        make_conf.write_text(make_conf.read_text() + "CPU_FLAGS_X86=''\n")
        report = lint.lint_atoms(rendered.config, self.scratch, rendered.atoms, lint.load_stage3("openrc"), name=cell.name)
        self.assertTrue(any("opencv" in item["cpv"] for item in report.required_use), lint.format_report(report))


if __name__ == "__main__":
    unittest.main()
