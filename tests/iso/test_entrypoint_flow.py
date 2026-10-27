"""The stage steps of the builder (fresh, reseed, publish) with Catalyst replaced by a stub."""
from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from .conftest import ROOT, load_script, run_bash, write_executable
from .test_entrypoint import ENTRYPOINT, reuse, sandbox, stage1, write_receipt  # noqa: F401  (fixtures)

verify = load_script("scripts/verify-iso.py", "verify_iso")
ARCHIVE_NAME = "livecd-stage1-amd64-installer-20261018T124555Z.tar.gz"
# What Catalyst leaves behind: a stage1 tarball whose package database (var/db/pkg) says when each package was built. The stub
# builds one for every atom of RESEED_ATOMS, STUB_BUILT_OFFSET seconds after "now" (a negative offset is a package that was not
# reinstalled).
MAKE_STAGE1 = textwrap.dedent("""
    import io, sys, tarfile, time
    archive, offset, *atoms = sys.argv[1:]
    built = int(time.time()) + int(offset)
    with tarfile.open(archive, "w:gz") as tar:
        for atom in atoms:
            data = f"{built}\\n".encode()
            info = tarfile.TarInfo(f"./var/db/pkg/{atom}-0.1.0-r5/BUILD_TIME")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
""")


def flow(sandbox, body: str, **env: str):
    """Run entrypoint functions after replacing run_catalyst by a recorder."""
    log = sandbox.tmp / "catalyst-calls.log"
    helper = sandbox.tmp / "make-stage1.py"
    helper.write_text(MAKE_STAGE1)
    prelude = f"""
load_environment
STAGE1_ARCHIVE={shlex.quote(str(sandbox.dirs['store'] / 'builds/23.0-default' / ARCHIVE_NAME))}
export EMERGE_DEFAULT_OPTS='--getbinpkg'
mkdir -p "$(dirname "$STAGE1_ARCHIVE")"
new_stage1() {{ python3 {shlex.quote(str(helper))} "$STAGE1_ARCHIVE" "${{STUB_BUILT_OFFSET:-0}}" ${{RESEED_ATOMS:-}}; }}
run_catalyst() {{
	{{ echo "== $1"; echo "env EMERGE_DEFAULT_OPTS=$(printenv EMERGE_DEFAULT_OPTS)"; cat "$BUILD_DIR/catalyst.conf"; if [[ -f $1 ]]; then grep '^source_subpath' "$1"; fi; ls "$(dirname "$STAGE1_ARCHIVE")"; }} >> {shlex.quote(str(log))}
	new_stage1
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
    result, calls = flow(sandbox, 'PLAN=reseed\nRESEED_ATOMS="app-misc/hwmonux app-portage/portagehub"\nbuild_stage1\necho "after: $(printenv EMERGE_DEFAULT_OPTS)"')
    assert result.returncode == 0, result.stderr
    assert "after: --getbinpkg\n" in result.stdout, "stage2 must not inherit the reinstall options"
    assert f"== {sandbox.dirs['build']}/livecd-stage1-reseed.spec" in calls
    assert "source_subpath: 23.0-default/livecd-stage1-amd64-installer-20261018T124555Z-completed" in calls
    # Catalyst passes the environment's EMERGE_DEFAULT_OPTS into the chroot (envvars), so that is where they must be
    assert "env EMERGE_DEFAULT_OPTS=--getbinpkg --reinstall-atoms=app-misc/hwmonux --reinstall-atoms=app-portage/portagehub" in calls
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
    result, _ = flow(sandbox, f'PLAN=reseed\nRESEED_ATOMS="app-misc/hwmonux"\nrun_catalyst() {{ cp "$1" {shlex.quote(str(keep))}; new_stage1; }}\nbuild_stage1')
    assert result.returncode == 0, result.stderr
    original = (sandbox.dirs["build"] / "livecd-stage1.spec").read_text().splitlines()
    reseed = keep.read_text().splitlines()
    differing = [(a, b) for a, b in zip(original, reseed) if a != b]
    assert len(original) == len(reseed) and len(differing) == 1 and differing[0][0].startswith("source_subpath:")


def test_a_reseed_that_reinstalled_nothing_leaves_no_receipt(sandbox) -> None:
    """The first real reseed did exactly this: Catalyst finished, the archive still held the old installer, and the receipt vouched for it."""
    render_specs(sandbox)
    archive = sandbox.dirs["store"] / "builds/23.0-default" / ARCHIVE_NAME
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"previous stage1")
    old_receipt = Path(str(archive) + ".receipt.json")
    old_receipt.write_text("{}")
    result, _calls = flow(sandbox, 'PLAN=reseed\nRESEED_ATOMS="app-admin/gentoo-live-installer app-misc/hwmonux"\nbuild_stage1\necho not reached',
                          STUB_BUILT_OFFSET="-3600")
    assert result.returncode != 0 and "not reached" not in result.stdout
    assert "the reseed did not reinstall app-admin/gentoo-live-installer app-misc/hwmonux" in result.stderr
    assert "app-admin/gentoo-live-installer was built at" in result.stderr and "before the reseed started" in result.stderr
    assert "--reuse none" in result.stderr
    assert not old_receipt.exists(), "no receipt at all: the next build starts from scratch instead of trusting this archive"
    assert not [p for p in archive.parent.iterdir() if "completed" in p.name], "the seed copy is removed even then"


def test_a_reseed_that_left_one_package_old_is_refused_naming_it(sandbox) -> None:
    render_specs(sandbox)
    archive = sandbox.dirs["store"] / "builds/23.0-default" / ARCHIVE_NAME
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"previous stage1")
    # the stub builds hwmonux fresh, but the installer's entry is not in the archive at all
    result, _calls = flow(sandbox, 'PLAN=reseed\nRESEED_ATOMS="app-misc/hwmonux app-admin/gentoo-live-installer"\n'
                                   'new_stage1() { python3 "' + str(sandbox.tmp / "make-stage1.py") + '" "$STAGE1_ARCHIVE" 0 app-misc/hwmonux; }\nbuild_stage1')
    assert result.returncode != 0
    assert "app-admin/gentoo-live-installer is not installed in the new archive" in result.stderr and "app-misc/hwmonux is not" not in result.stderr
    assert not Path(str(archive) + ".receipt.json").exists()


def test_the_proof_applies_to_a_reseed_only(sandbox) -> None:
    render_specs(sandbox)
    result, _calls = flow(sandbox, "PLAN=full\nwrite_catalyst_config\nbuild_stage1", STUB_BUILT_OFFSET="-3600")
    assert result.returncode == 0, result.stderr


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
    # the real signer needs shim, sbsign and mtools (tests/iso/test_secure_boot.py tests it with a fake runner)
    write_executable(
        workspace / "iso/sign-secure-boot.py",
        f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
if os.environ.get("STUB_SIGN_FAIL"):
    print("sign-secure-boot: stub failure", file=sys.stderr)
    sys.exit(1)
open(args[args.index("--cert-out") + 1], "wb").write(b"certificate")
json.dump({{"certificate_sha256": "ab" * 32, "mokmanager_fingerprint": "D1 AB"}}, open(args[args.index("--summary") + 1], "w"))
open(args[args.index("--notice-out") + 1], "w").write("Secure Boot key\\n")
open(os.environ["STUB_SIGN_ARGS"], "w").write(json.dumps(args))
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
        "load_environment; STAGE1_MODE=reuse; STAGE1_ARCHIVE=/none; export STUB_VERIFY_ARGS STUB_SIGN_ARGS; publish_iso",
        WORKSPACE=str(workspace), STUB_VERIFY_ARGS=str(sandbox.tmp / "verify-args.json"), STUB_SIGN_ARGS=str(sandbox.tmp / "sign-args.json"),
        CATALYST_BUILDER_IMAGE="localhost/image:1", **env,
    )
    return result, name


def test_publish_runs_the_verifier_and_writes_every_sidecar(sandbox, publish_workspace: Path) -> None:
    result, name = publish(sandbox, publish_workspace)
    assert result.returncode == 0, result.stderr + result.stdout
    dist = sandbox.dirs["dist"]
    assert {p.name for p in dist.iterdir()} == {name, f"{name}.sha512", f"{name}.blake2b", f"{name}.packages.txt", f"{name}.manifest.json",
                                                f"{name}.secureboot.cer", f"{name}.secureboot.txt"}
    assert (dist / f"{name}.secureboot.cer").read_bytes() == b"certificate"
    manifest = json.loads((dist / f"{name}.manifest.json").read_text())
    assert manifest["flavor"] == "release" and manifest["verified"]["ok"] is True and manifest["stage1_mode"] == "reuse"
    assert manifest["reused_stage1"] is True and manifest["gentoo_repository"]["signature"]["verified"] is True
    assert manifest["packages_list"] == f"{name}.packages.txt" and manifest["volume_id"] == "GENTOO_INSTALLER_AMD64"
    assert manifest["secure_boot"]["certificate_sha256"] == "ab" * 32
    signed = json.loads((sandbox.tmp / "sign-args.json").read_text())
    assert signed[0].endswith(name) and signed[0].startswith(str(sandbox.dirs["store"])), "Catalyst's ISO is signed in place, before it is verified"
    arguments = json.loads((sandbox.tmp / "verify-args.json").read_text())
    assert arguments[0].endswith(name) and arguments[arguments.index("--flavor") + 1] == "release"
    assert arguments[arguments.index("--spec") + 1] == str(sandbox.dirs["build"] / "livecd-stage2.spec")
    assert arguments[arguments.index("--workspace") + 1] == str(publish_workspace)
    assert "--previous-manifest" not in arguments, "a first build has nothing to compare with"
    assert not [p for p in dist.iterdir() if p.name.startswith(".")], "no partial files are left"


def test_an_earlier_manifest_of_the_same_name_feeds_the_size_warning(sandbox, publish_workspace: Path) -> None:
    name = "gentoo-installer-amd64-20261018T124555Z.iso"
    sandbox.dirs["dist"].mkdir(parents=True, exist_ok=True)
    earlier = sandbox.dirs["dist"] / f"{name}.manifest.json"
    earlier.write_text('{"iso": {"size": 1}}')
    result, _ = publish(sandbox, publish_workspace)
    assert result.returncode == 0, result.stderr + result.stdout
    arguments = json.loads((sandbox.tmp / "verify-args.json").read_text())
    assert arguments[arguments.index("--previous-manifest") + 1] == str(earlier)
    # the stub verifier saw the old file; the new build has replaced it afterwards
    assert json.loads(earlier.read_text())["iso"]["size"] > 1


def test_a_failed_secure_boot_signing_publishes_nothing(sandbox, publish_workspace: Path) -> None:
    result, _ = publish(sandbox, publish_workspace, STUB_SIGN_FAIL="1")
    assert result.returncode != 0 and "could not be prepared for Secure Boot" in result.stderr
    assert not (sandbox.tmp / "verify-args.json").exists(), "an unsigned ISO is not even verified"
    assert not sandbox.dirs["dist"].exists() or list(sandbox.dirs["dist"].iterdir()) == []


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
