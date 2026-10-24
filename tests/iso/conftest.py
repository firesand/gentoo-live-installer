"""Shared helpers for the ISO build tests (specs, overlay, scripts, packaging)."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import textwrap
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
# The pinned Gentoo tree (read-only). Tests that need it are skipped without it.
PINNED_TREE = Path(os.environ["ISO_TREE"]) if os.environ.get("ISO_TREE") else None


def load_script(path: str, name: str) -> ModuleType:
    """Import a script whose file name is not a valid module name (render-config.py)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up by name while the class is being created.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def pins() -> dict[str, str]:
    verify = load_script("scripts/verify-iso.py", "verify_iso")
    return verify.load_pins(ROOT / "iso/pins.env")


@pytest.fixture(scope="session")
def pinned_tree() -> Path:
    if PINNED_TREE is None or not PINNED_TREE.is_dir():
        pytest.skip("set ISO_TREE to the pinned Gentoo tree to check atoms, flags and licences")
    return PINNED_TREE


def write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def run_bash(script: str, *, env: dict[str, str] | None = None, cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=merged, cwd=cwd, timeout=timeout, check=False)


# --- stubs for docker and df, used by the tests of scripts/build-iso.sh --------------------

DOCKER_STUB = r'''#!__PYTHON__
"""Stand-in for docker: records its arguments and plays a configurable build."""
import hashlib, json, os, pathlib, signal, sys, time

argv = sys.argv[1:]
env = os.environ
with open(env["STUB_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\n")
state = pathlib.Path(env["STUB_STATE"])
state.mkdir(parents=True, exist_ok=True)


def option(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


command = argv[0] if argv else ""
if command == "info":
    fmt = option("--format", "")
    if "ServerVersion" in fmt:
        print("27.0.0")
    elif "SecurityOptions" in fmt:
        print(env.get("STUB_SECURITY", "[name=seccomp,profile=builtin name=apparmor]"))
    elif "DockerRootDir" in fmt:
        print(env.get("STUB_DOCKER_ROOT", ""))
    sys.exit(int(env.get("STUB_INFO_STATUS", "0")))
if command == "build":
    print("stub: image built")
    sys.exit(int(env.get("STUB_BUILD_STATUS", "0")))
if command == "image":
    print("sha256:" + "ab" * 32)
    sys.exit(0)
if command == "inspect":
    name = argv[-1]
    marker = state / ("container-" + name)
    if "--type" in argv:
        sys.exit(0 if marker.exists() or env.get("STUB_CONTAINER_EXISTS") == "1" else 1)
    if "OOMKilled" in option("--format", ""):
        print(env.get("STUB_OOM", "false"))
    sys.exit(0)
if command in ("stop", "rm"):
    name = argv[-1]
    if command == "stop":
        (state / ("stop-" + name)).write_text("stop")
    else:
        (state / ("container-" + name)).unlink(missing_ok=True)
    sys.exit(0)
if command == "run":
    name = option("--name", "unnamed")
    marker = state / ("container-" + name)
    if "--rm" not in argv:
        marker.write_text("1")
    mounts = {}
    for index, word in enumerate(argv):
        if word == "--mount":
            fields = dict(part.split("=", 1) for part in argv[index + 1].split(",") if "=" in part)
            mounts[fields["target"]] = fields["source"]
    mode = env.get("STUB_RUN", "ok")
    envs = {w.split("=", 1)[0]: w.split("=", 1)[1] for i, w in enumerate(argv) if i and argv[i - 1] == "--env" and "=" in w}
    if option("--entrypoint"):
        print("stub: cleanup ran")
        sys.exit(int(env.get("STUB_CLEAN_STATUS", "0")))
    print("stub: container started, mode " + envs.get("BUILD_MODE", "?"))
    if mode == "sleep":
        def stop(signum, frame):
            sys.exit(143)
        signal.signal(signal.SIGTERM, stop)
        for _ in range(400):
            if (state / ("stop-" + name)).exists():
                sys.exit(143)
            time.sleep(0.05)
        sys.exit(99)
    if envs.get("BUILD_MODE") == "stage1-only" and mode == "ok":
        sys.exit(0)
    if mode in ("fail", "oom"):
        for n in range(1, 4):
            print(f"Emerging ({n} of 9) dev-python/pyside-6.11.2")
        for n in range(60):
            print(f"build output line {n}")
        sys.exit(1)
    if mode == "ok" or mode == "badmanifest" or mode == "noartifacts":
        if mode == "noartifacts":
            sys.exit(0)
        flavor = envs.get("ISO_FLAVOR", "release")
        iso_name = env["STUB_ISO_NAME"]
        dist = pathlib.Path(mounts["/dist"])
        data = b"stub iso for " + iso_name.encode()
        (dist / iso_name).write_bytes(data)
        (dist / (iso_name + ".sha512")).write_text(hashlib.sha512(data).hexdigest() + "  " + iso_name + "\n")
        (dist / (iso_name + ".blake2b")).write_text(hashlib.blake2b(data).hexdigest() + "  " + iso_name + "\n")
        (dist / (iso_name + ".packages.txt")).write_text("sys-fs/lvm2-2.03.39\n")
        manifest = {"iso": {"name": iso_name}, "flavor": flavor, "test_ssh_enabled": flavor == "testkey", "verified": {"ok": mode != "badmanifest"}}
        (dist / (iso_name + ".manifest.json")).write_text(json.dumps(manifest))
        sys.exit(0)
sys.exit(0)
'''

DF_STUB = r'''#!__PYTHON__
"""Stand-in for df -Pk PATH: the filesystem is chosen by STUB_DF, a JSON list of
[path substring, filesystem, available KiB, mount point]; the first match wins."""
import json, os, sys

path = sys.argv[-1]
rules = json.loads(os.environ.get("STUB_DF", "[]"))
fs, avail, mount = "stubfs", 10**10, "/"
for needle, name, kib, where in rules:
    if needle in path:
        fs, avail, mount = name, kib, where
        break
print("Filesystem 1024-blocks Used Available Capacity Mounted on")
print(f"{fs} {avail + 1000} 1000 {avail} 1% {mount}")
'''


@pytest.fixture()
def stubs(tmp_path: Path):
    """A directory with stub docker and df first on PATH, plus the environment that drives them."""
    bin_dir = tmp_path / "stub-bin"
    write_executable(bin_dir / "docker", DOCKER_STUB.replace("__PYTHON__", sys.executable))
    write_executable(bin_dir / "df", DF_STUB.replace("__PYTHON__", sys.executable))
    log = tmp_path / "docker.log"
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 33554432 kB\nMemAvailable: 33554432 kB\n")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_STATE": str(tmp_path / "docker-state"),
        "PROC_MEMINFO": str(meminfo),
        "ISO_CACHE_DIR": str(tmp_path / "cache"),
        "ISO_DIST_DIR": str(tmp_path / "dist"),
        "HOME": str(tmp_path / "home"),
    }
    for variable in ("JOBS", "LOAD_AVERAGE", "BUILD_MEMORY", "BUILD_MIN_FREE_GIB", "ISO_SECCOMP", "ISO_LOG_DIR", "GENTOO_TEST_SSH_PUBLIC_KEY",
                     "ISO_KEEP_STAGE1", "ISO_DIST_MIN_FREE_GIB", "DOCKER_MIN_FREE_GIB"):
        env[variable] = ""  # overridden per test; empty means "use the default" below

    class Stubs:
        def __init__(self) -> None:
            self.env = env
            self.log = log
            self.tmp = tmp_path
            self.bin = bin_dir

        def calls(self) -> list[list[str]]:
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines()]

        def runs(self) -> list[list[str]]:
            return [c for c in self.calls() if c[:1] == ["run"]]

        def run(self, *args: str, extra_env: dict[str, str] | None = None, timeout: int = 60, input_text: str | None = None) -> subprocess.CompletedProcess:
            merged = {k: v for k, v in os.environ.items()}
            merged.update({k: v for k, v in self.env.items() if v != ""})
            for variable, value in (extra_env or {}).items():
                if value == "":
                    merged.pop(variable, None)
                else:
                    merged[variable] = value
            return subprocess.run(["bash", str(ROOT / "scripts/build-iso.sh"), *args], capture_output=True, text=True, env=merged,
                                  timeout=timeout, check=False, input=input_text)

    return Stubs()

