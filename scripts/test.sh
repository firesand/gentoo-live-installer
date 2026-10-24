#!/bin/sh
# The repository's tests: Python (pytest), PortageHub (cargo) and the HWMonux smoke run.
#
# Usage: scripts/test.sh [pytest arguments...]      e.g. scripts/test.sh -k manifest -x
#        scripts/check.sh [--full]                   adds syntax, types, clippy, pkgcheck (see that script)
#
# Environment: PYTHON (the interpreter; default .venv/bin/python, else python3), SKIP_RUST=1,
# SKIP_HWMONUX=1, CARGO_FLAGS (default: --locked). A hung test fails after 120 s (pyproject.toml).
set -eu
cd "$(dirname "$0")/.."
python=${PYTHON:-}
if [ -z "$python" ]; then
    if [ -x .venv/bin/python ]; then
        python=.venv/bin/python
    else
        python=python3
    fi
fi
PYTHONPATH=src QT_QPA_PLATFORM=offscreen "$python" -m pytest "$@"
if [ "${SKIP_RUST:-0}" != 1 ]; then
    # shellcheck disable=SC2086
    cargo test --manifest-path apps/portagehub/Cargo.toml ${CARGO_FLAGS:---locked}
fi
if [ "${SKIP_HWMONUX:-0}" != 1 ]; then
    # hwmonux imports PySide6 at module level, so it must run under the same interpreter.
    QT_QPA_PLATFORM=offscreen "$python" apps/hwmonux/hwmonux.py --dump > /dev/null
fi
