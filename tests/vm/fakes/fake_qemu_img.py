#!/usr/bin/env python3
"""A stand-in for qemu-img.

  create -f FORMAT PATH SIZE              a small placeholder file
  create -f qcow2 -b BASE -F FMT PATH     the same, plus PATH.backing naming the base image
  info --output=json PATH                 {"format": "qcow2", "virtual-size": $FAKE_QEMU_IMG_VSIZE}
"""
import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
if os.environ.get("FAKE_QEMU_IMG_LOG"):
    with open(os.environ["FAKE_QEMU_IMG_LOG"], "a") as log:
        log.write(json.dumps(argv) + "\n")
if os.environ.get("FAKE_QEMU_IMG_FAIL"):
    print(os.environ["FAKE_QEMU_IMG_FAIL"], file=sys.stderr)
    raise SystemExit(1)
if argv[:1] == ["create"]:
    backing = argv[argv.index("-b") + 1] if "-b" in argv else ""
    path = argv[-1] if backing else next(
        item for item in argv[1:] if not item.startswith("-") and item not in {"qcow2", "raw"} and "/" in item)
    target = Path(path)
    if target.exists():
        print(f"qemu-img: Could not open '{path}': file exists", file=sys.stderr)
        raise SystemExit(1)
    target.write_bytes(b"placeholder disk image\n")
    if backing:
        Path(path + ".backing").write_text(backing)
    raise SystemExit(0)
if argv[:1] == ["info"]:
    print(json.dumps({"format": os.environ.get("FAKE_QEMU_IMG_FORMAT", "qcow2"),
                      "virtual-size": int(os.environ.get("FAKE_QEMU_IMG_VSIZE", 21474836480))}))
    raise SystemExit(0)
print("fake qemu-img: unsupported command", file=sys.stderr)
raise SystemExit(2)
