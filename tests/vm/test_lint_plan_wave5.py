"""scripts/lint-plan.py and scripts/resolve-gate.py in Wave 5 (hardware backlog H03): --hardware, --set, --overlay, the Wave 5 matrix, the baseline.

Everything here is offline: the Portage walk itself is covered by tests/backend/test_target_lint.py; these tests cover what is new around it.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lint = load("lint_plan", "lint-plan.py")
gate = load("resolve_gate", "resolve-gate.py")


def test_settings_become_typed_overrides_in_one_place():
    assert lint.parse_setting("hardware.gpu_driver=nvidia") == ("hardware.gpu_driver", "nvidia")
    assert lint.parse_setting("hardware.nvidia_license_accepted=TRUE") == ("hardware.nvidia_license_accepted", True)
    assert lint.parse_setting("hardware.battery_charge_limit=80") == ("hardware.battery_charge_limit", 80)
    for bad in ("nonsense", "a.b.c=1", "=1"):
        with pytest.raises(ValueError):
            lint.parse_setting(bad)
    assert gate.parse_setting("options.gaming=native") == lint.parse_setting("options.gaming=native"), "resolve-gate uses lint-plan's parser"


def test_the_hardware_plan_comes_from_a_fixture_and_a_missing_fixture_is_an_error():
    cell = next(cell for cell in lint.wave5_cells() if cell.name == "nvidia-pascal-xfce-openrc")
    plan = lint.hardware_plan(cell.hardware, cell.manifest())
    assert "x11-drivers/nvidia-drivers" in plan.all_atoms()
    assert gate.hardware_plan(cell.hardware, cell.manifest()).all_atoms() == plan.all_atoms()
    with pytest.raises(SystemExit, match="No hardware fixture"):
        lint.hardware_plan("no-such-machine", cell.manifest())


def test_a_derived_fixture_can_be_named_too():
    cell = next(cell for cell in lint.wave5_cells() if cell.name == "secureboot-nvidia-kde-systemd")
    assert lint.hardware_plan(cell.hardware, cell.manifest()) is not None


def test_every_wave5_cell_is_a_valid_manifest_with_a_unique_name_and_a_real_fixture():
    names = [cell.name for cell in lint.wave5_cells()]
    assert len(names) == len(set(names))
    fixtures = ROOT / "tests" / "hardware" / "fixtures"
    for cell in lint.wave5_cells():
        cell.manifest()
        assert not cell.hardware or (fixtures / cell.hardware).is_dir() or cell.hardware in ("asus-desktop-rtx4090-secureboot",), cell.name


def test_every_desktop_runs_on_both_init_systems():
    from gentoo_installer.manifest import DESKTOPS

    covered = {(cell.desktop, cell.init) for cell in lint.default_cells() + lint.wave5_cells()}
    for desktop in DESKTOPS:
        for init in ("openrc", "systemd"):
            assert (desktop, init) in covered, f"no lint cell for {desktop} on {init}"


def test_the_wave5_group_leaves_out_the_known_findings_and_everything_includes_the_older_cells():
    wave5 = {cell.name for cell in lint.select_cells("wave5")}
    assert "secureboot-nvidia-kde-systemd" not in wave5 and "i3-systemd" in wave5 and "gaming-native-kde-openrc" in wave5
    assert {cell.name for cell in lint.select_cells("secureboot-nvidia-kde-systemd")} == {"secureboot-nvidia-kde-systemd"}
    everything = {cell.name for cell in lint.select_cells("everything")}
    assert wave5 <= everything and {cell.name for cell in lint.extended_cells()} <= everything
    assert {cell.name for cell in lint.select_cells("all")}.isdisjoint(wave5), "'all' keeps meaning what it meant"


def test_the_baseline_accepts_the_measured_counts_and_names_a_deviation():
    baseline = lint.load_data("desktop-baseline.json")
    assert {name: values for name, values in baseline["counts"].items()} == {
        "kde": [524, 512], "gnome": [578, 579], "xfce": [343, 334], "cinnamon": [485, 474], "mate": [422, 409], "lxqt": [392, 371]}
    assert lint.baseline_problem("kde", "openrc", 524) == "" and lint.baseline_problem("kde", "systemd", 512) == ""
    assert lint.baseline_problem("xfce", "openrc", 343 + 30) == ""
    problem = lint.baseline_problem("xfce", "systemd", 600)
    assert "334" in problem and "600" in problem
    assert lint.baseline_problem("sway", "openrc", 5) == "", "a desktop without a measurement is not judged"


def test_heavy_builds_are_found_by_name_not_by_prefix():
    closure = ["dev-qt/qtwebengine-6.8.2-r1", "dev-qt/qtbase-6.8.2", "www-client/firefox-153.5.1", "app-office/libreoffice-bin-24.8", "dev-libs/openssl-3.4.1-r1"]
    found = lint.heavy_packages(closure)
    assert found == ["dev-qt/qtwebengine-6.8.2-r1 (about 3.0 h)", "www-client/firefox-153.5.1 (about 3.0 h)"]
    assert lint.heavy_packages([]) == []


def test_a_heavy_build_is_a_finding_only_for_a_cell_that_must_not_compile_it():
    tolerant = lint.Report(name="a", heavy=["dev-qt/qtwebengine-6.8.2 (about 3.0 h)"])
    strict = lint.Report(name="b", heavy=["dev-qt/qtwebengine-6.8.2 (about 3.0 h)"], forbid_heavy=True)
    assert tolerant.ok and not strict.ok
    assert "HEAVY" in lint.format_report(strict) and "FINDINGS" in lint.format_report(strict)
    assert "NOTE" in lint.format_report(lint.Report(name="c", notes=["x"]))


def test_a_choice_without_an_effect_is_an_error_not_a_green_cell():
    cell = lint.Cell("gaming", expect_atoms=("games-util/steam-launcher", "sys-apps/flatpak"))
    plan = types.SimpleNamespace(all_atoms=lambda: ["sys-apps/flatpak[abi_x86_32]"])
    rendered = lint.RenderedCell(Path("/t"), Path("/c"), "p", ["x11-misc/foo"], plan)
    errors = lint.expectation_errors(cell, rendered)
    assert len(errors) == 1 and "games-util/steam-launcher is missing from the plan" in errors[0]
    assert lint.expectation_errors(lint.Cell("none"), rendered) == []


def test_an_overlay_becomes_a_repository_next_to_gentoo(tmp_path):
    overlay = tmp_path / "edorp-overlay"
    (overlay / "profiles").mkdir(parents=True)
    (overlay / "profiles" / "repo_name").write_text("edorp\n")
    assert lint.overlay_name(overlay) == "edorp" and lint.overlay_name(tmp_path / "nameless") == "nameless"
    tree = tmp_path / "gentoo"
    (tree / "profiles" / "default/linux/amd64/23.0").mkdir(parents=True)
    cell = lint.Cell("minimal-openrc", "openrc", "minimal")
    rendered = lint.render_cell(cell, tree, tmp_path / "scratch", None, [overlay, ("guru", Path("/overlays/1"))])
    repos = (rendered.config / "etc/portage/repos.conf/repos.conf").read_text()
    assert "[edorp]" in repos and f"location = {overlay.resolve()}" in repos and "masters = gentoo" in repos
    assert "[guru]" in repos and "location = /overlays/1" in repos


def test_the_command_line_hands_hardware_settings_and_overlays_to_every_cell(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(lint, "portage_available", lambda: True)
    monkeypatch.setattr(lint, "find_tree", lambda explicit=None: tmp_path)
    overlay = tmp_path / "edorp"
    (overlay / "profiles").mkdir(parents=True)

    def fake_lint_cell(cell, tree, scratch, **kwargs):
        seen.append((cell, kwargs))
        return lint.Report(name=cell.name)

    monkeypatch.setattr(lint, "lint_cell", fake_lint_cell)
    lines = []
    status = lint.main(["--cells", "nvidia-pascal-xfce-openrc,xfce-openrc", "--hardware", "pascal-desktop-gtx1060", "--set", "hardware.gpu_driver=nvidia",
                        "--overlay", str(overlay), "--baseline", "--fail-on-heavy", "--scratch", str(tmp_path / "s")], out=lines.append)
    assert status == 0
    assert [cell.name for cell, _ in seen] == ["nvidia-pascal-xfce-openrc", "xfce-openrc"]
    for cell, kwargs in seen:
        assert cell.hardware == "pascal-desktop-gtx1060" and ("hardware.gpu_driver", "nvidia") in cell.overrides and cell.forbid_heavy
        assert kwargs["overlays"] == [overlay] and kwargs["baseline"] is True


def test_a_bad_setting_or_overlay_stops_the_command_line_before_any_work(tmp_path, monkeypatch):
    monkeypatch.setattr(lint, "portage_available", lambda: True)
    monkeypatch.setattr(lint, "find_tree", lambda explicit=None: tmp_path)
    lines = []
    assert lint.main(["--cells", "xfce-openrc", "--set", "oops"], out=lines.append) == 2 and "--set needs SECTION.FIELD=VALUE" in lines[-1]
    assert lint.main(["--cells", "xfce-openrc", "--overlay", str(tmp_path / "missing")], out=lines.append) == 2 and "not a repository" in lines[-1]


def test_the_list_shows_the_wave5_cells():
    lines = []
    assert lint.main(["--list"], out=lines.append) == 0
    text = "\n".join(lines)
    assert "nvidia-pascal-xfce-openrc" in text and "sway-systemd" in text


def test_resolve_gate_mounts_every_overlay_read_only_at_the_path_the_configuration_names(tmp_path, monkeypatch):
    overlay = tmp_path / "edorp"
    (overlay / "profiles").mkdir(parents=True)
    (overlay / "profiles" / "repo_name").write_text("edorp\n")
    commands = []

    def fake_docker(*arguments, **kwargs):
        commands.append(arguments)
        return types.SimpleNamespace(returncode=0, stdout="Total: 3 packages\n", stderr="")

    monkeypatch.setattr(gate, "docker", fake_docker)
    lint_module = gate.load_lint()
    monkeypatch.setattr(lint_module, "render_cell", lambda cell, tree, scratch, plan, overlays: types.SimpleNamespace(
        profile="p", atoms=["a/b"], plan=None, config=tmp_path / "config", root=tmp_path))
    (tmp_path / "config/etc/portage").mkdir(parents=True)
    (tmp_path / "config/etc/portage/make.conf").write_text("")
    args = gate.build_parser().parse_args(["--overlay", str(overlay), "--no-binhost", "--tree", str(tmp_path)])
    cell = lint_module.Cell("minimal-openrc", "openrc", "minimal")
    gate.run_cell(lint_module, cell, args, {"openrc": "img", "systemd": "img"}, tmp_path / "out")
    run = next(command for command in commands if command and command[0] == "run")
    assert f"{overlay.resolve()}:/overlays/0:ro" in run


def test_the_data_files_are_valid_json_with_the_keys_the_code_reads():
    assert set(lint.load_data("heavy-builds.json")["hours"]) >= {"dev-qt/qtwebengine", "www-client/firefox"}
    assert json.loads((ROOT / "scripts/lint-plan-data/desktop-baseline.json").read_text())["counts"]["gnome"] == [578, 579]


EMERGE_OUTPUT = """
[binary   R    ] dev-libs/openssl-3.4.1-r1::gentoo
[ebuild  N     ] dev-qt/qtwebengine-6.8.2-r1:6/6.8::gentoo  USE="widgets" 0 KiB
[ebuild  N     ] x11-libs/gtk+-3.24.43:3::gentoo  USE="X" 0 KiB
[ebuild  N     ] www-client/firefox-153.5.1:rapid::gentoo  0 KiB

Total: 412 packages (410 new, 2 reinstalls), Size of downloads: 1,201,334 KiB
"""


def test_a_finished_emerge_is_judged_for_heavy_builds_and_the_baseline():
    lint_module = gate.load_lint()
    cell = lint_module.Cell("kde-openrc", "openrc", "kde", forbid_heavy=True)
    result = gate.CellResult("kde-openrc", "img", returncode=0)
    gate.judge_output(result, EMERGE_OUTPUT, cell, lint_module)
    assert result.total == 412
    assert result.heavy == ["dev-qt/qtwebengine-6.8.2-r1 (about 3.0 h)", "www-client/firefox-153.5.1 (about 3.0 h)"], "binary packages and small compiles are not heavy"
    assert not result.ok, "a cell that must not compile for hours fails when it would"
    assert result.notes == ["412 packages in all, near the kde baseline"]
    small = gate.CellResult("kde-openrc", "img", returncode=0)
    gate.judge_output(small, EMERGE_OUTPUT.replace("Total: 412", "Total: 150"), cell, lint_module)
    assert "524" in small.notes[0] and "150" in small.notes[0], small.notes
    tolerant = gate.CellResult("kde-openrc", "img", returncode=0)
    gate.judge_output(tolerant, EMERGE_OUTPUT, lint_module.Cell("kde-openrc", "openrc", "kde"), lint_module)
    assert tolerant.ok and tolerant.heavy


def test_resolve_gate_takes_the_heavy_flag_for_every_cell():
    args = gate.build_parser().parse_args(["--fail-on-heavy"])
    assert args.fail_on_heavy is True and gate.build_parser().parse_args([]).fail_on_heavy is False
