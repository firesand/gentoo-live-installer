#!/bin/sh
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
cargo test --manifest-path apps/portagehub/Cargo.toml
python3 apps/hwmonux/hwmonux.py --dump >/dev/null
