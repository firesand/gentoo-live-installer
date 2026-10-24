#!/bin/sh
# The repository's single quality gate: syntax, shell, types, tests, Rust and packaging.
#
# Usage: scripts/check.sh [--full] [--update-baseline]
#
#   (default)          the fast steps, under a minute: Python and shell syntax, shellcheck and
#                      ruff when installed, mypy against scripts/mypy-baseline.txt, and the pytest
#                      suite without the tests marked slow
#   --full             also: the whole pytest suite, cargo test and clippy for apps/portagehub,
#                      the HWMonux smoke run, pkgcheck on a temporary copy of the overlay, the ISO
#                      verifier's tests and scripts/lint-plan.py (each only when it exists)
#   --update-baseline  rewrite scripts/mypy-baseline.txt and scripts/clippy-baseline.txt from the
#                      current tree and exit (after an intended change, review the diff)
#
# Every step runs inside scripts/sandbox.sh (bubblewrap): the host's files are read-only, no disk device exists,
# and there is no network, so no test can change the host system. --full first fetches the Rust crates in a
# sandbox that has the network (into .cache/cargo-home, not ~/.cargo). NO_SANDBOX=1 runs on the host instead
# (only for a system without bubblewrap or user namespaces; the gate then says so).
# Environment: PYTHON (interpreter), SKIP_RUST=1, SKIP_PKGCHECK=1, NO_SANDBOX=1,
# CARGO_FLAGS (default: --locked --offline), CARGO_TARGET_DIR (Rust build directory).
# Exit status: 0 when every step that ran passed, 1 otherwise.
set -eu
cd "$(dirname "$0")/.."

python=${PYTHON:-}
if [ -z "$python" ]; then
    if [ -x .venv/bin/python ]; then python=.venv/bin/python; else python=python3; fi
fi
cargo_flags=${CARGO_FLAGS:---locked --offline}
full=0
update=0
for argument in "$@"; do
    case "$argument" in
        --full) full=1 ;;
        --update-baseline) update=1 ;;
        -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "check.sh: unknown option: $argument" >&2; exit 2 ;;
    esac
done

if [ "${GENTOO_INSTALLER_SANDBOX:-0}" != 1 ]; then
    if [ "${NO_SANDBOX:-0}" = 1 ]; then
        echo "check.sh: NO_SANDBOX=1: the steps run on the host, not in scripts/sandbox.sh" >&2
    elif scripts/sandbox.sh true 2> /dev/null; then
        if [ "$full" -eq 1 ] && [ "${SKIP_RUST:-0}" != 1 ] && command -v cargo > /dev/null 2>&1; then
            echo "==> cargo fetch (portagehub crates, the only step with network)"
            scripts/sandbox.sh --net cargo fetch --locked --quiet --manifest-path apps/portagehub/Cargo.toml || echo "    cargo fetch failed; the cargo steps will say why" >&2
        fi
        exec scripts/sandbox.sh "$0" "$@"
    else
        echo "check.sh: scripts/sandbox.sh cannot run here (bubblewrap or user namespaces missing); set NO_SANDBOX=1 to run on the host" >&2
        exit 1
    fi
fi

failed=""
step() {
    name=$1
    shift
    printf '==> %s\n' "$name"
    if "$@"; then
        printf '    ok\n'
    else
        printf '    FAILED\n'
        failed="$failed
  $name"
    fi
}
skip() { printf '==> %s\n    skipped: %s\n' "$1" "$2"; }

shell_files() {
    find . \( -path ./.git -o -path ./build -o -path ./dist -o -path ./.venv -o -path ./.cache -o -path ./apps/portagehub/target \) \
        -prune -o -type f -name '*.sh' -print | sort
}

step_python_syntax() {
    cache=$(mktemp -d)
    status=0
    PYTHONPYCACHEPREFIX=$cache "$python" -m compileall -q src scripts tests apps/hwmonux || status=1
    rm -rf "$cache"
    return $status
}

step_shell_syntax() {
    status=0
    for file in $(shell_files); do
        if head -n 1 "$file" | grep -q bash; then shell=bash; else shell=sh; fi
        "$shell" -n "$file" || { echo "    $file does not parse" >&2; status=1; }
    done
    return $status
}

step_shellcheck() {
    # shellcheck disable=SC2046
    shellcheck -S warning $(shell_files)
}

step_ruff() {
    # Only the classes of finding that are bugs: syntax errors, undefined names, invalid comparisons.
    ruff check --select E9,F63,F7,F82 src scripts tests apps/hwmonux
}

step_pyflakes() {
    # Advisory: pyflakes also reports style (unused imports), so it cannot fail the gate.
    "$python" -m pyflakes src scripts tests apps/hwmonux || true
}

mypy_findings() {
    # Line numbers stripped so an unrelated edit above a known finding does not look like a new one.
    # shellcheck disable=SC2046
    "$python" -m mypy --config-file pyproject.toml --no-error-summary \
        $(find src scripts apps/hwmonux -name '*.py' | sort) 2>&1 | sed 's/^\([^:]*\):[0-9][0-9]*:/\1:/' | LC_ALL=C sort -u || true
}

step_mypy() {
    findings=$(mktemp)
    mypy_findings > "$findings"
    status=0
    if [ -f scripts/mypy-baseline.txt ]; then
        new=$(LC_ALL=C comm -13 scripts/mypy-baseline.txt "$findings")
        gone=$(LC_ALL=C comm -23 scripts/mypy-baseline.txt "$findings" | wc -l)
        if [ -n "$new" ]; then
            printf '    new mypy findings (not in scripts/mypy-baseline.txt):\n%s\n' "$new" | sed 's/^\([^ ]\)/    \1/'
            status=1
        fi
        [ "$gone" -eq 0 ] || echo "    $gone baseline finding(s) are fixed: run scripts/check.sh --update-baseline to lock that in"
    else
        echo "    no scripts/mypy-baseline.txt: every finding counts"
        [ ! -s "$findings" ] || { sed 's/^/    /' "$findings"; status=1; }
    fi
    rm -f "$findings"
    return $status
}

step_pytest() {
    if [ "$full" -eq 1 ]; then
        PYTHONPATH=src QT_QPA_PLATFORM=offscreen "$python" -m pytest -q -p no:cacheprovider
    else
        PYTHONPATH=src QT_QPA_PLATFORM=offscreen "$python" -m pytest -q -p no:cacheprovider -m 'not slow'
    fi
}

step_cargo_test() {
    # shellcheck disable=SC2086
    cargo test --quiet --manifest-path apps/portagehub/Cargo.toml $cargo_flags
}

clippy_warnings() {
    # shellcheck disable=SC2086
    cargo clippy --manifest-path apps/portagehub/Cargo.toml $cargo_flags --all-targets --message-format=short 2>&1 \
        | grep -c ': warning:' || true
}

step_cargo_clippy() {
    if [ ! -f scripts/clippy-baseline.txt ]; then
        # shellcheck disable=SC2086
        cargo clippy --manifest-path apps/portagehub/Cargo.toml $cargo_flags --all-targets -- -D warnings
        return
    fi
    baseline=$(cat scripts/clippy-baseline.txt)
    count=$(clippy_warnings)
    echo "    $count clippy warning(s), baseline $baseline"
    if [ "$count" -gt "$baseline" ]; then
        echo "    clippy found more warnings than scripts/clippy-baseline.txt allows (run cargo clippy to see them)" >&2
        return 1
    fi
    [ "$count" -ge "$baseline" ] || echo "    fewer than the baseline: run scripts/check.sh --update-baseline to lock that in"
}

step_hwmonux() {
    QT_QPA_PLATFORM=offscreen "$python" apps/hwmonux/hwmonux.py --dump > /dev/null
}

step_pkgcheck() {
    # pkgcheck writes caches and may touch the repository: work on a copy, with private XDG directories.
    work=$(mktemp -d)
    cp -r packaging/gentoo-overlay "$work/overlay"
    status=0
    HOME=$work XDG_CACHE_HOME=$work/cache XDG_CONFIG_HOME=$work/config XDG_DATA_HOME=$work/data \
        pkgcheck scan --repo "$work/overlay" --exit error -k=-MissingManifest,-MissingUri || status=1
    rm -rf "$work"
    return $status
}

step_iso_verifier() {
    "$python" -m py_compile scripts/verify-iso.py &&
        PYTHONPATH=src QT_QPA_PLATFORM=offscreen "$python" -m pytest -q -p no:cacheprovider -k 'verify_iso or verify-iso'
}

step_lint_plan() {
    "$python" scripts/lint-plan.py
}

if [ "$update" -eq 1 ]; then
    mypy_findings > scripts/mypy-baseline.txt
    echo "wrote scripts/mypy-baseline.txt ($(wc -l < scripts/mypy-baseline.txt) findings)"
    if [ "${SKIP_RUST:-0}" != 1 ] && command -v cargo > /dev/null 2>&1; then
        clippy_warnings > scripts/clippy-baseline.txt
        echo "wrote scripts/clippy-baseline.txt ($(cat scripts/clippy-baseline.txt) warnings)"
    fi
    exit 0
fi

step "python syntax" step_python_syntax
step "shell syntax" step_shell_syntax
if command -v shellcheck > /dev/null 2>&1; then step "shellcheck" step_shellcheck; else skip "shellcheck" "not installed"; fi
if command -v ruff > /dev/null 2>&1; then
    step "ruff" step_ruff
elif "$python" -c 'import pyflakes' 2> /dev/null; then
    step "pyflakes (advisory)" step_pyflakes
else
    skip "ruff / pyflakes" "neither is installed"
fi
if "$python" -c 'import mypy' 2> /dev/null; then step "mypy against the baseline" step_mypy; else skip "mypy" "not installed"; fi
if [ "$full" -eq 1 ]; then step "pytest (everything)" step_pytest; else step "pytest (fast subset)" step_pytest; fi

if [ "$full" -eq 1 ]; then
    if [ "${SKIP_RUST:-0}" = 1 ] || ! command -v cargo > /dev/null 2>&1; then
        skip "cargo test / clippy" "SKIP_RUST=1 or cargo is not installed"
    else
        step "cargo test (portagehub)" step_cargo_test
        if cargo clippy --version > /dev/null 2>&1; then step "cargo clippy (portagehub)" step_cargo_clippy; else skip "cargo clippy" "clippy is not installed"; fi
    fi
    step "hwmonux --dump" step_hwmonux
    if [ "${SKIP_PKGCHECK:-0}" = 1 ] || ! command -v pkgcheck > /dev/null 2>&1; then
        skip "pkgcheck (overlay copy)" "SKIP_PKGCHECK=1 or pkgcheck is not installed"
    else
        step "pkgcheck (overlay copy)" step_pkgcheck
    fi
    if [ -f scripts/verify-iso.py ]; then step "ISO verifier tests" step_iso_verifier; else skip "ISO verifier tests" "scripts/verify-iso.py does not exist yet"; fi
    if [ -f scripts/lint-plan.py ]; then step "lint-plan" step_lint_plan; else skip "lint-plan" "scripts/lint-plan.py does not exist yet"; fi
else
    echo "(fast mode: run scripts/check.sh --full for the whole suite, Rust, pkgcheck and the other slow steps)"
fi

if [ -n "$failed" ]; then
    printf '\nFAILED:%s\n' "$failed"
    exit 1
fi
printf '\nall checks passed\n'
