"""iso/stage_reuse.py: when may a finished stage1 be reused, reseeded or must it be rebuilt."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from .conftest import ROOT, load_script

reuse = load_script("iso/stage_reuse.py", "stage_reuse")
PACKAGES = list(reuse.LOCAL_PACKAGES)


def sha512(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def current(**override) -> "reuse.Current":
    base = dict(
        stage1_key="key-1",
        local_inputs={p: f"src-{p}" for p in PACKAGES},
        local_ebuilds={p: {"file": f"{p}-1-r1.ebuild", "sha256": f"eb-{p}"} for p in PACKAGES},
        catalyst_version="Catalyst 4.2.0",
    )
    base.update(override)
    return reuse.Current(**base)


def receipt(archive: bytes = b"stage1", **override) -> dict:
    base = {
        "schema_version": 1, "filename": "livecd-stage1-amd64-x.tar.gz", "size": len(archive), "sha512": sha512(archive),
        "catalyst_version": "Catalyst 4.2.0", "builder_image_id": "sha256:1", "stage1_key": "key-1",
        "local_inputs": {p: f"src-{p}" for p in PACKAGES},
        "local_ebuilds": {p: {"file": f"{p}-1-r1.ebuild", "sha256": f"eb-{p}"} for p in PACKAGES},
        "build_inputs_sha256": {}, "created": "2026-10-24T00:00:00Z",
    }
    base.update(override)
    return base


GOOD_ARCHIVE = reuse.Archive(True, False, sha512(b"stage1"))
CASES = [
    # (name, receipt, current, archive, action, reason fragment)
    ("identical keys reuse", receipt(), current(), GOOD_ARCHIVE, "reuse", "no input"),
    ("no receipt", None, current(), GOOD_ARCHIVE, "fresh", "no receipt"),
    ("missing archive", receipt(), current(), None, "fresh", "missing"),
    ("symlinked archive", receipt(), current(), reuse.Archive(False, True, ""), "fresh", "plain file"),
    ("archive hash differs", receipt(), current(), reuse.Archive(True, False, sha512(b"other")), "fresh", "sha512"),
    ("spec, portage or pins changed", receipt(), current(stage1_key="key-2"), GOOD_ARCHIVE, "fresh", "specs"),
    ("catalyst upgraded", receipt(), current(catalyst_version="Catalyst 4.3.0"), GOOD_ARCHIVE, "fresh", "Catalyst"),
    ("unknown schema", receipt(schema_version=2), current(), GOOD_ARCHIVE, "fresh", "schema_version"),
    ("receipt without sha", receipt(sha512="short"), current(), GOOD_ARCHIVE, "fresh", "sha512"),
    ("receipt of another shape", {"schema_version": 1}, current(), GOOD_ARCHIVE, "fresh", "unusable"),
]


@pytest.mark.parametrize(("name", "old", "new", "archive", "action", "fragment"), CASES, ids=[c[0] for c in CASES])
def test_decisions(name, old, new, archive, action, fragment) -> None:
    decision = reuse.decide(old, new, archive)
    assert decision.action == action, decision
    assert fragment in decision.reason, decision


def test_changed_source_of_one_package_reseeds_only_that_package() -> None:
    new = current(local_inputs={**{p: f"src-{p}" for p in PACKAGES}, "hwmonux": "other"})
    decision = reuse.decide(receipt(), new, GOOD_ARCHIVE)
    assert decision.action == "reseed"
    assert decision.reinstall == ("app-misc/hwmonux",)


def test_changed_ebuild_or_files_dir_reseeds_even_without_a_revision_bump() -> None:
    ebuilds = {p: {"file": f"{p}-1-r1.ebuild", "sha256": f"eb-{p}"} for p in PACKAGES}
    ebuilds["gentoo-live-installer"]["sha256"] = "changed init script"
    decision = reuse.decide(receipt(), current(local_ebuilds=ebuilds), GOOD_ARCHIVE)
    assert decision.action == "reseed" and decision.reinstall == ("app-admin/gentoo-live-installer",)


def test_all_three_packages_changed() -> None:
    decision = reuse.decide(receipt(), current(local_inputs={p: "x" for p in PACKAGES}), GOOD_ARCHIVE)
    assert decision.action == "reseed"
    assert set(decision.reinstall) == set(reuse.LOCAL_PACKAGES.values())


def test_stage1_changes_win_over_package_changes() -> None:
    """A changed spec means packages may have to disappear, which a reseed cannot do."""
    new = current(stage1_key="key-2", local_inputs={p: "x" for p in PACKAGES})
    assert reuse.decide(receipt(), new, GOOD_ARCHIVE).action == "fresh"


def test_a_receipt_that_forgot_a_package_is_not_trusted() -> None:
    old = receipt()
    del old["local_inputs"]["portagehub"]
    assert reuse.decide(old, current(), GOOD_ARCHIVE).action == "fresh"


# --- the files on disk -----------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A copy of the inputs that decide stage1, in a temporary directory."""
    target = tmp_path / "ws"
    for relative in (*reuse.STAGE1_INPUTS, "packaging/gentoo-overlay"):
        source = ROOT / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)
    return target


@pytest.fixture()
def distfiles(tmp_path: Path, workspace: Path) -> Path:
    directory = tmp_path / "distfiles"
    directory.mkdir()
    for prefix in reuse.LOCAL_PACKAGES:
        version = reuse.package_version(reuse.ebuild_file(workspace, prefix))
        (directory / f"{prefix}-{version}.tar.zst").write_bytes(f"archive of {prefix}".encode())
    return directory


def test_stage1_key_is_stable_and_follows_the_inputs(workspace: Path) -> None:
    key = reuse.compute_stage1_key(workspace)
    assert key == reuse.compute_stage1_key(workspace)
    (workspace / "iso/pins.env").write_text((workspace / "iso/pins.env").read_text() + "# changed\n")
    assert reuse.compute_stage1_key(workspace) != key


@pytest.mark.parametrize(
    "relative",
    ["iso/specs/livecd-stage1.spec", "iso/portage/make.conf", "iso/portage/package.use/live-installer",
     "iso/catalyst-no-loop.py", "iso/Containerfile", "packaging/gentoo-overlay/acct-group/gentoo-installer/gentoo-installer-0.ebuild"],
)
def test_stage1_key_changes_with_each_stage1_input(workspace: Path, relative: str) -> None:
    key = reuse.compute_stage1_key(workspace)
    path = workspace / relative
    path.write_text(path.read_text() + "\n# changed\n")
    assert reuse.compute_stage1_key(workspace) != key


@pytest.mark.parametrize(
    "relative",
    ["iso/specs/livecd-stage2.spec", "iso/fsscript-stage2.sh", "iso/root_overlay/etc/motd", "iso/cd_overlay/boot/grub/grub.cfg.in",
     "iso/README-live.txt", "iso/builder-entrypoint.sh", "scripts/build-iso.sh", "iso/image-contract.json"],
)
def test_stage2_only_files_do_not_touch_the_stage1_key(relative: str) -> None:
    assert not any(relative == i or relative.startswith(i + "/") for i in reuse.STAGE1_INPUTS), relative


def test_job_count_is_not_part_of_the_key(workspace: Path) -> None:
    """The templates are hashed unrendered, so --jobs never invalidates a stage1."""
    text = (workspace / "iso/portage/make.conf").read_text()
    assert "@JOBS@" in text


def test_archive_names_follow_the_committed_ebuilds(workspace: Path, distfiles: Path) -> None:
    inputs = reuse.local_inputs(workspace, distfiles)
    assert set(inputs) == set(PACKAGES)
    # an old archive of another version in the shared distfiles cache is ignored
    (distfiles / "hwmonux-0.9.tar.zst").write_bytes(b"old")
    assert reuse.local_inputs(workspace, distfiles) == inputs
    (distfiles / "portagehub-0.4.0.tar.zst").unlink()
    with pytest.raises(reuse.ReuseError, match="missing"):
        reuse.local_inputs(workspace, distfiles)


def test_two_ebuilds_of_one_package_are_refused(workspace: Path) -> None:
    directory = workspace / "packaging/gentoo-overlay/app-admin/gentoo-live-installer"
    shutil.copy(next(directory.glob("*.ebuild")), directory / "gentoo-live-installer-0.1.0-r4.ebuild")
    with pytest.raises(reuse.ReuseError, match="exactly one ebuild"):
        reuse.local_ebuilds(workspace)


def test_package_version_parsing() -> None:
    assert reuse.package_version(Path("gentoo-live-installer-0.1.0-r5.ebuild")) == "0.1.0"
    assert reuse.package_version(Path("hwmonux-1.0.0.ebuild")) == "1.0.0"
    with pytest.raises(reuse.ReuseError):
        reuse.package_version(Path("garbage.ebuild"))


def run_cli(*arguments: str):
    return subprocess.run([sys.executable, str(ROOT / "iso/stage_reuse.py"), *arguments], capture_output=True, text=True, check=False)


def test_cli_round_trip(workspace: Path, distfiles: Path, tmp_path: Path) -> None:
    archive = tmp_path / "builds" / "livecd-stage1-amd64-x.tar.gz"
    archive.parent.mkdir()
    archive.write_bytes(b"stage1 tarball")
    common = ["--workspace", str(workspace), "--distfiles", str(distfiles), "--archive", str(archive), "--catalyst-version", "Catalyst 4.2.0"]

    first = run_cli("decide", *common)
    assert first.returncode == 0 and json.loads(first.stdout)["action"] == "fresh"
    stage = run_cli("decide", *common, "--policy", "stage1")
    assert stage.returncode == 3 and "no receipt" in stage.stderr

    made = run_cli("receipt", *common)
    assert made.returncode == 0, made.stderr
    Path(str(archive) + ".receipt.json").write_text(made.stdout)
    stored = json.loads(made.stdout)
    assert stored["schema_version"] == 1 and stored["sha512"] == hashlib.sha512(b"stage1 tarball").hexdigest()

    assert json.loads(run_cli("decide", *common).stdout)["action"] == "reuse"
    assert run_cli("decide", *common, "--policy", "stage1").returncode == 0
    assert json.loads(run_cli("decide", *common, "--policy", "none").stdout)["action"] == "fresh"

    (distfiles / "hwmonux-1.0.0.tar.zst").write_bytes(b"new hwmonux sources")
    reseed = json.loads(run_cli("decide", *common).stdout)
    assert reseed["action"] == "reseed" and reseed["reinstall"] == ["app-misc/hwmonux"]
    assert run_cli("decide", *common, "--policy", "stage1").returncode == 3

    archive.write_bytes(b"tampered")
    assert json.loads(run_cli("decide", *common).stdout)["action"] == "fresh"
    archive.unlink()
    archive.symlink_to(tmp_path / "elsewhere")
    assert json.loads(run_cli("decide", *common).stdout)["action"] == "fresh"


def test_broken_receipt_file_means_fresh(workspace: Path, distfiles: Path, tmp_path: Path) -> None:
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"x")
    Path(str(archive) + ".receipt.json").write_text("{ not json")
    result = run_cli("decide", "--workspace", str(workspace), "--distfiles", str(distfiles), "--archive", str(archive))
    assert json.loads(result.stdout)["action"] == "fresh"


def test_reseed_spec_replaces_only_the_source() -> None:
    spec = (ROOT / "iso/specs/livecd-stage1.spec").read_text()
    out = reuse.render_reseed_spec(spec, "23.0-default/livecd-stage1-amd64-x-completed")
    assert "source_subpath: 23.0-default/livecd-stage1-amd64-x-completed\n" in out
    assert "source_subpath: 23.0-default/@STAGE3_BASENAME@" not in out
    assert out.replace("23.0-default/livecd-stage1-amd64-x-completed", "23.0-default/@STAGE3_BASENAME@") == spec
    with pytest.raises(reuse.ReuseError):
        reuse.render_reseed_spec("target: x\n", "y")
