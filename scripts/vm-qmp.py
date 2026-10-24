#!/usr/bin/env python3
"""Operator-controlled QMP tools for a disposable VM test run."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from vm_qmp import TEST_LUKS_PASSPHRASE, screendump, type_luks_passphrase


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    dump = subparsers.add_parser("screendump", help="Save the current guest VGA screen")
    dump.add_argument("socket", type=Path)
    dump.add_argument("output", type=Path)
    unlock = subparsers.add_parser("type-test-luks-passphrase", help="Type only the harness's disposable test passphrase")
    unlock.add_argument("socket", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "screendump":
            print(screendump(args.socket, args.output))
        else:
            type_luks_passphrase(args.socket, TEST_LUKS_PASSPHRASE)
            print("Sent the harness test passphrase and Enter via QMP.")
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"vm-qmp.py: error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
