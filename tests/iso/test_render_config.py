"""iso/render-config.py: specs, make.conf, README and the boot menu come from one set of values."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

import pytest

from .conftest import ROOT, load_script

render = load_script("iso/render-config.py", "render_config")
keys = load_script("iso/make_conf_keys.py", "make_conf_keys")
verify = load_script("scripts/verify-iso.py", "verify_iso")


@pytest.fixture()
def env(pins: dict[str, str]) -> dict[str, str]:
    return dict(pins)


@pytest.fixture()
def rendered(tmp_path: Path, env: dict[str, str]) -> Path:
    values = render.token_values(env, str(tmp_path / "build"), "4", "8")
    render.render_all(ROOT, tmp_path / "build", values)
    return tmp_path / "build"


def test_every_template_renders_without_leftover_tokens(rendered: Path) -> None:
    for path in rendered.rglob("*"):
        if path.is_file():
            assert not re.search(r"@[A-Z][A-Z0-9_]*@", path.read_text(encoding="utf-8")), path


def test_spec_and_boot_menu_share_volume_id_and_arguments(rendered: Path, pins: dict[str, str]) -> None:
    stage2 = verify.parse_spec((rendered / "livecd-stage2.spec").read_text())
    assert stage2["livecd/volid"] == pins["ISO_VOLID"]
    assert stage2["livecd/bootargs"] == pins["ISO_BOOTARGS"].split()
    grub = (rendered / "cd_overlay/boot/grub/grub.cfg").read_text()
    for line in grub.splitlines():
        if line.strip().startswith("linux ") and "memtest" not in line:
            assert f"root=live:CDLABEL={pins['ISO_VOLID']}" in line
            assert pins["ISO_BOOTARGS"] in line
        if line.strip().startswith("search "):
            assert line.strip().endswith(pins["ISO_VOLID"])
    assert (rendered / "cd_overlay/SECURE-BOOT-OFF.txt").read_text().startswith("SECURE BOOT MUST BE OFF")
    assert "SECURE-BOOT-OFF.txt" in (rendered / "README-live.txt").read_text()


def test_paths_and_job_counts_are_substituted(rendered: Path) -> None:
    stage2 = verify.parse_spec((rendered / "livecd-stage2.spec").read_text())
    assert stage2["livecd/overlay"] == f"{rendered}/cd_overlay"
    assert stage2["livecd/readme"] == f"{rendered}/README-live.txt"
    assert stage2["livecd/fsops"] == ["-j", "4", "-Q", "20"]
    make_conf = (rendered / "portage/make.conf").read_text()
    assert 'MAKEOPTS="-j4"' in make_conf and 'NINJAOPTS="-j4 -l8"' in make_conf


def test_make_conf_extra_has_every_key_once_in_order(rendered: Path) -> None:
    lines = [l for l in (rendered / "portage/make.conf.extra").read_text().splitlines() if l and not l.startswith("#")]
    assert [l.split("=")[0] for l in lines] == list(keys.BUILD_KEYS)


def test_emerge_options_have_one_definition_without_job_counts(rendered: Path) -> None:
    text = (rendered / "portage/make.conf").read_text()
    assert len(re.findall(r"^EMERGE_DEFAULT_OPTS=", text, re.M)) == 1
    value = render.extract_assignments(text)["EMERGE_DEFAULT_OPTS"]
    assert "--jobs" not in value and "--load-average" not in value
    # The three local packages and their group are always built from source.
    for atom in ("app-admin/gentoo-live-installer", "app-misc/hwmonux", "app-portage/portagehub", "acct-group/gentoo-installer"):
        assert f"--usepkg-exclude={atom}" in value
    assert not re.search(r"^USE=", text, re.M), "Catalyst writes USE from livecd/use; a USE line here is dead"


def test_get_make_conf_value_cli(rendered: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "iso/render-config.py"), "--get-make-conf-value", "VIDEO_CARDS", str(rendered / "portage/make.conf")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0 and "amdgpu" in result.stdout


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"ISO_VOLID": "lower_case"}, "ISO_VOLID"),
        ({"ISO_VOLID": "X" * 33}, "ISO_VOLID"),
        ({"ISO_BOOTARGS": "nodhcp secureconsole dokeymap"}, "dokeymap"),
        ({"ISO_BOOTARGS": "nodhcp; reboot"}, "ISO_BOOTARGS"),
        ({"ISO_BOOTARGS": 'quiet "x"'}, "ISO_BOOTARGS"),
        ({"ISO_BOOTARGS": "$(id)"}, "ISO_BOOTARGS"),
        ({"ISO_BOOTARGS": ""}, "ISO_BOOTARGS"),
    ],
)
def test_bad_values_are_refused(env: dict[str, str], tmp_path: Path, change: dict[str, str], message: str) -> None:
    env.update(change)
    with pytest.raises(render.RenderError, match=message):
        render.token_values(env, str(tmp_path), "2", "4")


def test_missing_pin_is_named(env: dict[str, str], tmp_path: Path) -> None:
    del env["ISO_VOLID"]
    with pytest.raises(render.RenderError, match="ISO_VOLID"):
        render.token_values(env, str(tmp_path), "2", "4")


def test_unexpanded_token_is_an_error() -> None:
    with pytest.raises(render.RenderError, match="@UNKNOWN@"):
        render.render_text("a @VOLID@ b @UNKNOWN@", {"@VOLID@": "X"})


def test_duplicate_or_missing_make_conf_key_is_an_error() -> None:
    complete = "".join(f'{key}="x"\n' for key in keys.BUILD_KEYS)
    assert render.extract_assignments(complete)["FEATURES"] == "x"
    with pytest.raises(render.RenderError, match="duplicate"):
        render.extract_assignments(complete + 'FEATURES="y"\n')
    with pytest.raises(render.RenderError, match="missing"):
        render.extract_assignments(complete.replace('MAKEOPTS="x"\n', ""))


def test_cd_overlay_files_without_suffix_are_copied_unchanged(rendered: Path) -> None:
    source = ROOT / "iso/cd_overlay/SECURE-BOOT-OFF.txt"
    assert (rendered / "cd_overlay/SECURE-BOOT-OFF.txt").read_bytes() == source.read_bytes()
    assert not (rendered / "cd_overlay/boot/grub/grub.cfg.in").exists()
