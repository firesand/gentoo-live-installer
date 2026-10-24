"""iso/write-manifest.py: the manifest next to every ISO."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from .conftest import ROOT, load_script

writer = load_script("iso/write-manifest.py", "write_manifest")

OLD_KEYS = {"schema_version", "iso", "source_date_epoch", "test_ssh_enabled", "builder_image", "builder_base_image", "catalyst_version",
            "stage3", "gentoo_repository", "source_archives", "build_inputs_sha256"}


def make_env(pins: dict[str, str]) -> dict[str, str]:
    return {**pins, "CATALYST_BUILDER_IMAGE": "localhost/image:1", "BUILDER_IMAGE": pins["BUILDER_IMAGE"]}


def manifest(tmp_path: Path, pins: dict[str, str], *, flavor="release", stage1_mode="fresh", receipt=None, verified=None, signature=None):
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"iso bytes")
    distfiles = tmp_path / "distfiles"
    distfiles.mkdir(exist_ok=True)
    (distfiles / "a-1.tar.zst").write_bytes(b"archive")
    return writer.build_manifest(
        iso=iso, workspace=ROOT, env=make_env(pins), catalyst_version="Catalyst 4.2.0", flavor=flavor,
        volume_id="GENTOO_INSTALLER_AMD64", verified=verified, signature=signature, stage1_mode=stage1_mode,
        stage1_receipt=receipt, builder_image_id="sha256:1", distfiles=distfiles, packages_list="x.iso.packages.txt",
    )


def test_old_fields_are_kept_and_new_ones_added(tmp_path: Path, pins) -> None:
    data = manifest(tmp_path, pins)
    assert OLD_KEYS <= set(data) and data["schema_version"] == 1
    assert {"flavor", "volume_id", "verified", "reused_stage1", "stage1_mode", "stage1_receipt_sha256", "builder_image_id", "packages_list"} <= set(data)
    assert data["iso"]["sha512"] == hashlib.sha512(b"iso bytes").hexdigest() and data["iso"]["size"] == 9
    assert data["source_archives"] == {"a-1.tar.zst": hashlib.sha256(b"archive").hexdigest()}
    assert data["gentoo_repository"]["commit"] == pins["GENTOO_TREEISH"] and data["catalyst_version"] == "Catalyst 4.2.0"
    assert "iso/pins.env" in data["build_inputs_sha256"] and "scripts/verify-iso.py" in data["build_inputs_sha256"]
    assert not [k for k in data["build_inputs_sha256"] if "__pycache__" in k or k.endswith(".pyc")]


def test_flavour_and_test_ssh_flag_agree(tmp_path: Path, pins) -> None:
    assert manifest(tmp_path, pins, flavor="release")["test_ssh_enabled"] is False
    assert manifest(tmp_path, pins, flavor="testkey")["test_ssh_enabled"] is True


def test_stage1_reuse_is_recorded(tmp_path: Path, pins) -> None:
    receipt = tmp_path / "r.json"
    receipt.write_text("{}")
    fresh = manifest(tmp_path, pins)
    assert fresh["reused_stage1"] is False and fresh["stage1_receipt_sha256"] is None
    for mode in ("reuse", "reseed"):
        data = manifest(tmp_path, pins, stage1_mode=mode, receipt=receipt)
        assert data["reused_stage1"] is True and data["stage1_mode"] == mode
        assert data["stage1_receipt_sha256"] == hashlib.sha256(b"{}").hexdigest()


def test_verification_and_signature_are_embedded(tmp_path: Path, pins) -> None:
    data = manifest(tmp_path, pins, verified={"ok": True, "flavor": "release", "checks": [{"id": "x"}]}, signature={"verified": True, "signer_fpr": "A" * 40})
    assert data["verified"]["ok"] is True and data["gentoo_repository"]["signature"]["signer_fpr"] == "A" * 40
    unverified = manifest(tmp_path, pins)
    assert unverified["gentoo_repository"]["signature"] == {"verified": False}


def test_command_line_writes_the_file_atomically(tmp_path: Path, pins) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / pins["ISO_NAME"]).write_bytes(b"iso")
    distfiles = tmp_path / "distfiles"
    distfiles.mkdir()
    verified = tmp_path / "v.json"
    verified.write_text(json.dumps({"ok": True}))
    env = {**make_env(pins), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "iso/write-manifest.py"), "--dist", str(dist), "--workspace", str(ROOT), "--flavor", "release", "--volid", "X",
         "--verified", str(verified), "--distfiles", str(distfiles), "--stage1-mode", "reuse"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert result.returncode == 0, result.stderr
    written = json.loads((dist / (pins["ISO_NAME"] + ".manifest.json")).read_text())
    assert written["verified"] == {"ok": True} and written["iso"]["name"] == pins["ISO_NAME"]
    assert not list(dist.glob(".*partial"))
