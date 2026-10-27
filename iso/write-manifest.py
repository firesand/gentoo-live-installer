#!/usr/bin/env python3
"""Write <iso>.manifest.json next to a finished ISO.

Called by iso/builder-entrypoint.sh after scripts/verify-iso.py accepted the image. The
manifest records what went into the ISO (pins, source archives, hashes of every build
input) and what was checked. The fields of schema_version 1 that existed before are
unchanged; the newer ones are additive: flavor, volume_id, verified, reused_stage1,
stage1_mode, stage1_receipt_sha256, builder_image_id, gentoo_repository.signature,
packages_list, secure_boot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

SCHEMA_VERSION = 1
# Everything in these places (and these files) is an input of the build.
INPUT_TREES = ("iso", "packaging")
INPUT_FILES = ("scripts/build-iso.sh", "scripts/verify-iso.py")


def digest(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def build_inputs(workspace: Path) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for tree in INPUT_TREES:
        for path in sorted((workspace / tree).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                inputs[str(path.relative_to(workspace))] = digest(path, "sha256")
    for name in INPUT_FILES:
        path = workspace / name
        if path.is_file():
            inputs[name] = digest(path, "sha256")
    return inputs


def read_json(path: str | None) -> dict[str, Any] | None:
    if not path or not Path(path).is_file():
        return None
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def build_manifest(
    *,
    iso: Path,
    workspace: Path,
    env: Mapping[str, str],
    catalyst_version: str,
    flavor: str,
    volume_id: str,
    verified: Mapping[str, Any] | None,
    signature: Mapping[str, Any] | None,
    stage1_mode: str,
    stage1_receipt: Path | None,
    builder_image_id: str,
    distfiles: Path,
    packages_list: str | None,
    secure_boot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    archives = {path.name: digest(path, "sha256") for path in sorted(distfiles.glob("*.tar.zst"))}
    receipt_sha = digest(stage1_receipt, "sha256") if stage1_receipt and stage1_receipt.is_file() else None
    repository: dict[str, Any] = {
        "url": env["GENTOO_REPOSITORY"],
        "commit": env["GENTOO_TREEISH"],
        "verification": "exact commit ID, Git object connectivity and OpenPGP signature of the pinned developer key",
        "signature": dict(signature) if signature else {"verified": False},
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "iso": {
            "name": iso.name,
            "size": iso.stat().st_size,
            "sha512": digest(iso, "sha512"),
            "blake2b": digest(iso, "blake2b"),
        },
        "flavor": flavor,
        "volume_id": volume_id,
        "source_date_epoch": int(env["SOURCE_DATE_EPOCH"]),
        "test_ssh_enabled": flavor == "testkey",
        "builder_image": env.get("CATALYST_BUILDER_IMAGE", ""),
        "builder_base_image": env.get("BUILDER_IMAGE", ""),
        "builder_image_id": builder_image_id,
        "catalyst_version": catalyst_version,
        "stage3": {
            "url": env["STAGE3_URL_BASE"] + "/" + env["STAGE3_BASENAME"],
            "sha512": env["STAGE3_SHA512"],
            "blake2b": env["STAGE3_BLAKE2B"],
            "signed_digests_verified": True,
        },
        "gentoo_repository": repository,
        "source_archives": archives,
        "build_inputs_sha256": build_inputs(workspace),
        "reused_stage1": stage1_mode in ("reuse", "reseed"),
        "stage1_mode": stage1_mode,
        "stage1_receipt_sha256": receipt_sha,
        "verified": dict(verified) if verified else {"flavor": flavor, "checks": []},
        "packages_list": packages_list,
        # iso/sign-secure-boot.py: the certificate MokManager enrolls (the private key no longer exists)
        "secure_boot": dict(secure_boot) if secure_boot else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--catalyst-version", default="")
    parser.add_argument("--flavor", required=True, choices=("release", "testkey"))
    parser.add_argument("--volid", required=True)
    parser.add_argument("--verified")
    parser.add_argument("--signature")
    parser.add_argument("--stage1-mode", choices=("fresh", "reuse", "reseed"), default="fresh")
    parser.add_argument("--stage1-receipt", type=Path)
    parser.add_argument("--builder-image-id", default="")
    parser.add_argument("--distfiles", required=True, type=Path)
    parser.add_argument("--packages")
    parser.add_argument("--secure-boot", help="the summary JSON of iso/sign-secure-boot.py")
    args = parser.parse_args(argv)
    iso_name = os.environ["ISO_NAME"]
    iso = args.dist / iso_name
    manifest = build_manifest(
        iso=iso,
        workspace=args.workspace,
        env=os.environ,
        catalyst_version=args.catalyst_version,
        flavor=args.flavor,
        volume_id=args.volid,
        verified=read_json(args.verified),
        signature=read_json(args.signature),
        stage1_mode=args.stage1_mode,
        stage1_receipt=args.stage1_receipt,
        builder_image_id=args.builder_image_id,
        distfiles=args.distfiles,
        packages_list=Path(args.packages).name if args.packages else None,
        secure_boot=read_json(args.secure_boot),
    )
    temporary = args.dist / f".{iso_name}.manifest.partial"
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.dist / f"{iso_name}.manifest.json")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyError as exc:
        print(f"write-manifest: missing setting {exc}", file=sys.stderr)
        raise SystemExit(1)
