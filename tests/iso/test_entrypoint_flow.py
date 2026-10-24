"""The stage steps of the builder (fresh, reseed, publish) with Catalyst replaced by a stub."""
from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from .conftest import ROOT, load_script, run_bash, write_executable
from .test_entrypoint import ENTRYPOINT, reuse, sandbox, stage1, write_receipt  # noqa: F401  (fixtures)

verify = load_script("scripts/verify-iso.py", "verify_iso")
ARCHIVE_NAME = "livecd-stage1-amd64-installer-20261018T124555Z.tar.gz"


def flow(sandbox, body: str, **env: str):
    """Run entrypoint functions after replacing run_catalyst by a recorder."""
    log = sandbox.tmp / "catalyst-calls.log"
    prelude = f"""
load_environment
STAGE1_ARCHIVE={shlex.quote(str(sandbox.dirs['store'] / 'builds/23.0-default' / ARCHIVE_NAME))}
EMERGE_DEFAULT_OPTS='--getbinpkg'
mkdir -p "$(dirname "$STAGE1_ARCHIVE")"
run_catalyst() {{
	{{ echo "== $1"; cat "$BUILD_DIR/catalyst.conf"; if [[ -f $1 ]]; then grep '^source_subpath' "$1"; fi; ls "$(dirname "$STAGE1_ARCHIVE")"; }} >> {shlex.quote(str(log))}
	printf 'new stage1 %s' "$RANDOM" > "$STAGE1_ARCHIVE"
}}
"""
    result = sandbox.run(prelude + body, **env)
    return result, (log.read_text() if log.exists() else "")


def render_specs(sandbox) -> None:
    """The reseed run needs the rendered stage1 spec in BUILD_DIR."""
    sandbox.archives()
    done = sandbox.run("load_environment; prepare_portage")
    assert done.returncode == 0, done.stderr


def test_a_fresh_stage1_writes_a_receipt_and_drops_the_old_one(sandbox) -> None:
    render_specs(sandbox)
    archive = sandbox.dirs["store"] / "builds/23.0-default" / ARCHIVE_NAME
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"old")
    stale = Path(str(archive) + ".receipt.json")
    stale.write_text("{}")
    result, calls = flow(sandbox, "PLAN=full\nwrite_catalyst_config\nbuild_stage1")
    assert result.returncode == 0, result.stderr
    assert calls.count("== ") == 1 and f"== {sandbox.dirs['build']}/livecd-stage1.spec" in calls
    receipt = json.loads(stale.read_text())
    assert receipt["schema_version"] == 1 and receipt["sha512"] == hashlib.sha512(archive.read_bytes()).hexdigest()
    assert receipt["filename"] == ARCHIVE_NAME and set(receipt["local_inputs"]) == set(reuse.LOCAL_PACKAGES)


def test_stage1_that_produced_no_archive_is_an_error(sandbox) -> None:
    render_specs(sandbox)
    result = sandbox.run("load_environment; STAGE1_ARCHIVE=/nonexistent/x.tar.gz; record_stage1_receipt")
    assert result.returncode != 0 and "without" in result.stderr


def test_a_reseed_starts_from_a_copy_and_reinstalls_only_the_changed_packages(sandbox) -> None:
    render_specs(sandbox)
    archive = sandbox.dirs["store"] / "builds/23.0-default" / ARCHIVE_NAME
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"previous stage1")
    result, calls = flow(sandbox, 'PLAN=reseed\nRESEED_ATOMS="app-misc/hwmonux app-portage/portagehub"\nbuild_stage1')
    assert result.returncode == 0, result.stderr
    assert f"== {sandbox.dirs['build']}/livecd-stage1-reseed.spec" in calls
    assert "source_subpath: 23.0-default/livecd-stage1-amd64-installer-20261018T124555Z-completed" in calls
    assert 'EMERGE_DEFAULT_OPTS = "--getbinpkg --reinstall-atoms=app-misc/hwmonux --reinstall-atoms=app-portage/portagehub"' in calls
    assert 'options = ["pkgcache"]' in calls and "seedcache" not in calls
    assert "livecd-stage1-amd64-installer-20261018T124555Z-completed.tar.gz" in calls, "the seed copy exists while Catalyst runs"
    names = sorted(p.name for p in archive.parent.iterdir())
    assert not [n for n in names if "completed" in n], "the seed copy is removed afterwards"
    # stage2 must not inherit the reinstall options
    conf = (sandbox.dirs["build"] / "catalyst.conf").read_text()
    assert "--reinstall-atoms" not in conf
    receipt = json.loads(Path(str(archive) + ".receipt.json").read_text())
    assert receipt["sha512"] == hashlib.sha512(archive.read_bytes()).hexdigest()
    assert archive.read_bytes() != b"previous stage1"


def test_the_reseed_spec_differs_from_the_stage1_spec_only_in_its_source(sandbox) -> None:
    render_specs(sandbox)
    archive = sandbox.dirs["store"] / "builds/23.0-default" / ARCHIVE_NAME
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"x")
    keep = sandbox.tmp / "kept-spec"
    result, _ = flow(sandbox, f'PLAN=reseed\nRESEED_ATOMS="app-misc/hwmonux"\nrun_catalyst() {{ cp "$1" {shlex.quote(str(keep))}; printf y > "$STAGE1_ARCHIVE"; }}\nbuild_stage1')
    assert result.returncode == 0, result.stderr
    original = (sandbox.dirs["build"] / "livecd-stage1.spec").read_text().splitlines()
    reseed = keep.read_text().splitlines()
    differing = [(a, b) for a, b in zip(original, reseed) if a != b]
    assert len(original) == len(reseed) and len(differing) == 1 and differing[0][0].startswith("source_subpath:")


# --- publishing ------------------------------------------------------------------------------


@pytest.fixture()
def publish_workspace(tmp_path: Path) -> Path:
    """A workspace whose verifier is a stub, so that publish_iso can run without a real ISO."""
    workspace = tmp_path / "ws"
    shutil.copytree(ROOT / "iso", workspace / "iso", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "packaging", workspace / "packaging")
    (workspace / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/build-iso.sh", workspace / "scripts/build-iso.sh")
    write_executable(
        workspace / "scripts/verify-iso.py",
        f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
if os.environ.get("STUB_VERIFY_FAIL"):
    print("FAIL stub")
    sys.exit(1)
json.dump({{"ok": True, "flavor": args[args.index("--flavor") + 1], "checks": []}}, open(args[args.index("--json") + 1], "w"))
open(args[args.index("--packages-out") + 1], "w").write("sys-fs/lvm2-2.03.39\\n")
open(os.environ["STUB_VERIFY_ARGS"], "w").write(json.dumps(args))
""",
    )
    return workspace


def publish(sandbox, workspace: Path, **env: str):
    sandbox.archives()
    built_dir = sandbox.dirs["store"] / "builds/23.0-default"
    built_dir.mkdir(parents=True)
    name = "gentoo-installer-amd64-20261018T124555Z.iso"
    with open(built_dir / name, "wb") as stream:
        stream.truncate(10_000_001)
    for tool in ("isoinfo",):
        write_executable(sandbox.tmp / "bin" / tool, "#!/bin/sh\nexit 0\n")
    sandbox.dirs["build"].mkdir(exist_ok=True)
    (sandbox.dirs["build"] / "livecd-stage2.spec").write_text("livecd/rcadd:\n\tdbus|default\n")
    (sandbox.dirs["build"] / "gentoo-signature.json").write_text('{"verified": true, "signer_fpr": "' + "A" * 40 + '"}')
    result = sandbox.run(
        "load_environment; STAGE1_MODE=reuse; STAGE1_ARCHIVE=/none; export STUB_VERIFY_ARGS; publish_iso",
        WORKSPACE=str(workspace), STUB_VERIFY_ARGS=str(sandbox.tmp / "verify-args.json"), CATALYST_BUILDER_IMAGE="localhost/image:1", **env,
    )
    return result, name


def test_publish_runs_the_verifier_and_writes_every_sidecar(sandbox, publish_workspace: Path) -> None:
    result, name = publish(sandbox, publish_workspace)
    assert result.returncode == 0, result.stderr + result.stdout
    dist = sandbox.dirs["dist"]
    assert {p.name for p in dist.iterdir()} == {name, f"{name}.sha512", f"{name}.blake2b", f"{name}.packages.txt", f"{name}.manifest.json"}
    manifest = json.loads((dist / f"{name}.manifest.json").read_text())
    assert manifest["flavor"] == "release" and manifest["verified"]["ok"] is True and manifest["stage1_mode"] == "reuse"
    assert manifest["reused_stage1"] is True and manifest["gentoo_repository"]["signature"]["verified"] is True
    assert manifest["packages_list"] == f"{name}.packages.txt" and manifest["volume_id"] == "GENTOO_INSTALLER_AMD64"
    arguments = json.loads((sandbox.tmp / "verify-args.json").read_text())
    assert arguments[0].endswith(name) and arguments[arguments.index("--flavor") + 1] == "release"
    assert arguments[arguments.index("--spec") + 1] == str(sandbox.dirs["build"] / "livecd-stage2.spec")
    assert arguments[arguments.index("--workspace") + 1] == str(publish_workspace)
    assert not [p for p in dist.iterdir() if p.name.startswith(".")], "no partial files are left"


def test_a_failed_verification_publishes_nothing(sandbox, publish_workspace: Path) -> None:
    result, name = publish(sandbox, publish_workspace, STUB_VERIFY_FAIL="1")
    assert result.returncode != 0 and "failed the image checks" in result.stderr
    assert list(sandbox.dirs["dist"].iterdir()) == []


def test_a_tiny_iso_is_never_published(sandbox, publish_workspace: Path) -> None:
    sandbox.archives()
    built_dir = sandbox.dirs["store"] / "builds/23.0-default"
    built_dir.mkdir(parents=True)
    (built_dir / "gentoo-installer-amd64-20261018T124555Z.iso").write_bytes(b"x" * 100)
    result = sandbox.run("load_environment; publish_iso", WORKSPACE=str(publish_workspace))
    assert result.returncode != 0 and "implausibly small" in result.stderr
