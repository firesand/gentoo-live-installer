"""scripts/build-iso.sh with a stub docker: naming, limits, logging, signals, disk checks, hardening."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

import pytest

from .conftest import ROOT, write_executable

pytestmark = pytest.mark.skipif(os.uname().machine != "x86_64", reason="the builder insists on an x86_64 host")

RELEASE_NAME = "gentoo-installer-amd64-20261018T124555Z.iso"
TEST_NAME = "gentoo-installer-amd64-20261018T124555Z-TESTKEY.iso"


def option_values(call: list[str], name: str) -> list[str]:
    return [call[i + 1] for i, word in enumerate(call) if word == name and i + 1 < len(call)]


def container_env(call: list[str]) -> dict[str, str]:
    return dict(item.split("=", 1) for item in option_values(call, "--env") if "=" in item)


def test_check_validates_without_side_effects(stubs) -> None:
    fresh = stubs.tmp / "a" / "b"
    result = stubs.run("--check", extra_env={"ISO_CACHE_DIR": str(fresh / "cache"), "ISO_DIST_DIR": str(fresh / "dist")})
    assert result.returncode == 0, result.stderr
    assert "no build started" in result.stdout
    assert "flavour release" in result.stdout and RELEASE_NAME in result.stdout
    assert not fresh.exists() and not (stubs.tmp / "a").exists(), "--check must not create directories"
    assert not [c for c in stubs.calls() if c[0] in ("build", "run", "rm", "stop")]


@pytest.mark.parametrize("arguments", [["--bogus"], ["--reuse", "sometimes"], ["--reuse"], ["--stage1-only", "--stage2-only"],
                                       ["--test-key-file"], ["--verify"], ["--flavor", "gold", "--check"]])
def test_bad_arguments_are_refused(stubs, arguments) -> None:
    assert stubs.run(*arguments).returncode != 0


def test_container_name_is_stable_per_cache_directory(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    assert stubs.run().returncode == 0
    name = option_values(stubs.runs()[0], "--name")[0]
    assert re.fullmatch(r"gentoo-iso-[0-9a-f]{12}", name)
    digest = hashlib.sha256(str(stubs.tmp / "cache").encode()).hexdigest()[:12]
    assert name == f"gentoo-iso-{digest}"
    other = stubs.run(extra_env={"ISO_CACHE_DIR": str(stubs.tmp / "cache2"), "STUB_ISO_NAME": RELEASE_NAME})
    assert other.returncode == 0
    assert option_values(stubs.runs()[1], "--name")[0] != name


@pytest.mark.parametrize(("memory", "ok"), [("8g", True), ("6144m", True), ("12G", True), ("8", False), ("0g", False), ("abc", False), ("8gb", False), ("-1g", False)])
def test_build_memory_is_validated_and_used(stubs, memory: str, ok: bool) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    result = stubs.run(extra_env={"BUILD_MEMORY": memory})
    assert (result.returncode == 0) is ok, result.stderr
    if ok:
        run = stubs.runs()[0]
        assert option_values(run, "--memory") == [memory] and option_values(run, "--memory-swap") == [memory]
    else:
        assert "BUILD_MEMORY" in result.stderr and not stubs.runs()


def test_default_memory_and_cpu_limits(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    assert stubs.run(extra_env={"JOBS": "3"}).returncode == 0
    run = stubs.runs()[0]
    assert option_values(run, "--memory") == ["8g"] and option_values(run, "--cpus") == ["3"]
    env = container_env(run)
    assert env["JOBS"] == "3" and env["LOAD_AVERAGE"] == "6"


def test_the_build_starts_only_while_16_gib_stay_free_for_the_user(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    (stubs.tmp / "meminfo").write_text("MemAvailable: 20971520 kB\n")  # 20 GiB: 8 for the build leaves 12
    result = stubs.run()
    assert result.returncode != 0 and not stubs.runs()
    assert "only 20480 MiB of memory are available" in result.stderr and "16 GiB stay free" in result.stderr
    assert stubs.run(extra_env={"HOST_RESERVE_GIB": "8"}).returncode == 0
    (stubs.tmp / "meminfo").write_text("MemAvailable: 25165824 kB\n")  # 24 GiB: exactly enough
    assert stubs.run().returncode == 0
    assert stubs.run(extra_env={"HOST_RESERVE_GIB": "lots"}).returncode != 0


def test_the_container_leaves_the_reserved_cpus_and_has_the_lowest_weight(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    assert stubs.run(extra_env={"HOST_RESERVE_CPUS": "4"}).returncode == 0
    run = stubs.runs()[0]
    allowed = subprocess.run([str(ROOT / "scripts/low-priority.sh"), "--cpus"], capture_output=True, text=True,
                             env={**os.environ, "HOST_RESERVE_CPUS": "4"}).stdout.strip()
    assert option_values(run, "--cpuset-cpus") == [allowed] and option_values(run, "--cpu-shares") == ["2"]
    assert not any(cpu in allowed.replace("-", ",").split(",") for cpu in "0123")


def test_log_file_is_created_announced_and_filled(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    result = stubs.run()
    assert result.returncode == 0, result.stderr
    logs = sorted((stubs.tmp / "dist/logs").glob("build-*.log"))
    assert len(logs) == 1 and re.fullmatch(r"build-\d{8}T\d{6}Z\.log", logs[0].name)
    assert f"log file: {logs[0]}" in result.stdout
    assert result.stdout.index("log file:") < result.stdout.index("starting Catalyst")
    text = logs[0].read_text()
    assert "stub: image built" in text and "stub: container started" in text


def test_log_directory_can_be_chosen(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    assert stubs.run(extra_env={"ISO_LOG_DIR": str(stubs.tmp / "elsewhere")}).returncode == 0
    assert list((stubs.tmp / "elsewhere").glob("build-*.log"))


def test_a_failed_build_shows_the_tail_and_the_status(stubs) -> None:
    result = stubs.run(extra_env={"STUB_RUN": "fail"})
    assert result.returncode != 0
    assert "failed with status 1" in result.stderr and "build output line 59" in result.stderr
    assert "OOM" not in result.stderr
    assert "artifacts were not accepted" in result.stderr


def test_an_oom_kill_is_explained(stubs) -> None:
    result = stubs.run(extra_env={"STUB_RUN": "fail", "STUB_OOM": "true", "JOBS": "4"})
    assert result.returncode != 0
    assert "OOM-killed near 'Emerging (3 of 9) dev-python/pyside-6.11.2'" in result.stderr
    assert "raise BUILD_MEMORY (now 8g) or lower JOBS (now 4)" in result.stderr


def test_the_container_is_removed_after_the_state_was_read(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    assert stubs.run().returncode == 0
    commands = [c[0] for c in stubs.calls()]
    assert "--rm" not in stubs.runs()[0]
    assert commands.index("inspect") < len(commands) - 1 - commands[::-1].index("rm") + 1
    assert "rm" in commands


def test_existing_container_blocks_a_second_build(stubs) -> None:
    result = stubs.run(extra_env={"STUB_CONTAINER_EXISTS": "1"})
    assert result.returncode != 0 and "docker stop gentoo-iso-" in result.stderr and not stubs.runs()


def test_the_cache_is_locked_against_a_second_build(stubs) -> None:
    import fcntl

    cache = stubs.tmp / "cache"
    with open(cache / ".build.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = stubs.run()
    assert result.returncode != 0 and "another build is using" in result.stderr and not stubs.runs()


def test_sigterm_stops_the_container_and_exits_143(stubs) -> None:
    merged = {**os.environ, **{k: v for k, v in stubs.env.items() if v}, "STUB_RUN": "sleep", "STUB_ISO_NAME": RELEASE_NAME}
    process = subprocess.Popen(["bash", str(ROOT / "scripts/build-iso.sh")], env=merged, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 20
    while time.time() < deadline and not stubs.runs():
        time.sleep(0.05)
    assert stubs.runs(), "the stub container never started"
    time.sleep(0.3)
    process.send_signal(signal.SIGTERM)
    out, err = process.communicate(timeout=30)
    assert process.returncode == 143, (out, err)
    commands = stubs.calls()
    stop = [c for c in commands if c[0] == "stop"]
    assert stop and "120" in stop[0] and stop[0][-1].startswith("gentoo-iso-")
    assert [c for c in commands if c[0] == "rm"]
    assert "log file:" in out


def test_sigint_behaves_like_sigterm(stubs) -> None:
    merged = {**os.environ, **{k: v for k, v in stubs.env.items() if v}, "STUB_RUN": "sleep", "STUB_ISO_NAME": RELEASE_NAME}
    process = subprocess.Popen(["bash", str(ROOT / "scripts/build-iso.sh")], env=merged, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 20
    while time.time() < deadline and not stubs.runs():
        time.sleep(0.05)
    time.sleep(0.3)
    process.send_signal(signal.SIGINT)
    process.communicate(timeout=30)
    assert process.returncode == 130
    assert [c for c in stubs.calls() if c[0] == "stop"]


# --- what is passed to docker -------------------------------------------------------------


def test_hardening_options(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    assert stubs.run().returncode == 0
    run = stubs.runs()[0]
    options = option_values(run, "--security-opt")
    assert "seccomp=unconfined" in options and "apparmor=unconfined" in options
    assert option_values(run, "--cap-add") == ["SYS_ADMIN", "NET_ADMIN", "MKNOD", "SYS_CHROOT"]
    assert "--init" in run and option_values(run, "--pids-limit") == ["4096"]


def test_seccomp_can_be_left_at_dockers_default(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    assert stubs.run(extra_env={"ISO_SECCOMP": "default"}).returncode == 0
    assert not [o for o in option_values(stubs.runs()[0], "--security-opt") if o.startswith("seccomp")]
    assert stubs.run(extra_env={"ISO_SECCOMP": "strict"}).returncode != 0


def test_apparmor_is_only_touched_where_it_exists(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    assert stubs.run(extra_env={"STUB_SECURITY": "[name=seccomp,profile=builtin name=cgroupns]"}).returncode == 0
    assert "apparmor=unconfined" not in option_values(stubs.runs()[0], "--security-opt")


def test_rootless_docker_is_refused(stubs) -> None:
    result = stubs.run(extra_env={"STUB_SECURITY": "[name=seccomp name=rootless]"})
    assert result.returncode != 0 and "rootless" in result.stderr


def test_host_ids_are_handed_to_the_container(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    assert stubs.run().returncode == 0
    env = container_env(stubs.runs()[0])
    assert env["HOST_UID"] == str(os.getuid()) and env["HOST_GID"] == str(os.getgid())


def test_no_dead_binary_package_mount(stubs) -> None:
    """NOBS-12: Catalyst keeps its package cache below its own store."""
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    assert stubs.run().returncode == 0
    mounts = " ".join(option_values(stubs.runs()[0], "--mount"))
    assert "binpkgs" not in mounts
    assert not (stubs.tmp / "cache/binpkgs").exists()
    targets = sorted(re.findall(r"target=(\S+?)(?:,|$| )", mounts))
    assert targets == ["/dist", "/var/cache/cargo", "/var/cache/catalyst/distfiles", "/var/tmp/catalyst", "/workspace"]
    workspace = next(m for m in option_values(stubs.runs()[0], "--mount") if "target=/workspace" in m)
    assert workspace.endswith("readonly")


@pytest.mark.parametrize(
    ("arguments", "mode"),
    [([], "auto"), (["--reuse", "auto"], "auto"), (["--reuse-stage1"], "auto"), (["--reuse", "none"], "full"), (["--reuse", "stage1"], "stage2-only"),
     (["--stage2-only"], "stage2-only"), (["--stage1-only"], "stage1-only")],
)
def test_reuse_options_map_to_build_modes(stubs, arguments, mode) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    result = stubs.run(*arguments)
    assert result.returncode == 0, result.stderr
    assert container_env(stubs.runs()[0])["BUILD_MODE"] == mode


def test_builder_image_is_built_with_the_pinned_arguments(stubs, pins) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    assert stubs.run().returncode == 0
    build = next(c for c in stubs.calls() if c[0] == "build")
    args = option_values(build, "--build-arg")
    assert f"GENTOO_TREEISH={pins['GENTOO_TREEISH']}" in args
    assert f"GENTOO_TREEISH_SIGNER_FPR={pins['GENTOO_TREEISH_SIGNER_FPR']}" in args
    assert container_env(stubs.runs()[0])["BUILDER_IMAGE_ID"] == "sha256:" + "ab" * 32


def test_failed_image_build_stops_everything(stubs) -> None:
    result = stubs.run(extra_env={"STUB_BUILD_STATUS": "1"})
    assert result.returncode != 0 and "image creation failed" in result.stderr and not stubs.runs()


# --- results ------------------------------------------------------------------------------


def test_success_prints_the_artifacts(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    result = stubs.run()
    assert result.returncode == 0, result.stderr
    for fragment in ("verified ISO:", "SHA-512:", "BLAKE2b:", "manifest:", "installed packages:", "build log:"):
        assert fragment in result.stdout


@pytest.mark.parametrize("mode", ["noartifacts", "badmanifest"])
def test_missing_artifacts_or_unverified_manifest_fail(stubs, mode: str) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    result = stubs.run(extra_env={"STUB_RUN": mode})
    assert result.returncode != 0


GPG_STUB = r'''#!__PYTHON__
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["STUB_GPG_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\n")
if "--list-secret-keys" in argv:
    sys.exit(2 if os.environ.get("STUB_GPG_NO_KEY") == "1" else 0)
if "--detach-sign" in argv:
    if os.environ.get("STUB_GPG_SIGN_FAILS") == "1":
        sys.exit(2)
    with open(argv[argv.index("--output") + 1], "w") as signature:
        signature.write("signature of " + argv[-1] + "\n")
    sys.exit(0)
if "--verify" in argv:
    sys.exit(1 if os.environ.get("STUB_GPG_BAD") == "1" else 0)
sys.exit(0)
'''
FINGERPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"


def with_gpg(stubs):
    import sys

    write_executable(stubs.bin / "gpg", GPG_STUB.replace("__PYTHON__", sys.executable))
    stubs.env["STUB_GPG_LOG"] = str(stubs.tmp / "gpg.log")
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME

    def gpg_calls() -> list[list[str]]:
        log = stubs.tmp / "gpg.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return gpg_calls


def test_sign_key_signs_the_checksums_and_the_manifest_on_the_host(stubs) -> None:
    gpg_calls = with_gpg(stubs)
    result = stubs.run("--sign-key", FINGERPRINT)
    assert result.returncode == 0, result.stderr + result.stdout
    dist = stubs.tmp / "dist"
    for suffix in (".sha512", ".manifest.json"):
        assert (dist / f"{RELEASE_NAME}{suffix}.asc").read_text().startswith("signature of "), suffix
    signing = [c for c in gpg_calls() if "--detach-sign" in c]
    assert [c[-1] for c in signing] == [str(dist / f"{RELEASE_NAME}.sha512"), str(dist / f"{RELEASE_NAME}.manifest.json")]
    assert all(option_values(c, "--local-user") == [FINGERPRINT] and "--armor" in c and "--batch" in c for c in signing)
    assert "signatures:" in result.stdout and f"{RELEASE_NAME}.sha512.asc" in result.stdout
    assert FINGERPRINT not in json.dumps(stubs.runs()), "the key must never be handed to the container"


def test_no_signature_without_the_option(stubs) -> None:
    gpg_calls = with_gpg(stubs)
    assert stubs.run().returncode == 0
    assert gpg_calls() == [] and not list((stubs.tmp / "dist").glob("*.asc"))


@pytest.mark.parametrize("value", ["abc", "bob@example.org", "0123456789ABCDEG", "0123456789ABCDEF01234567", "-----"])
def test_sign_key_must_look_like_a_key_id_or_fingerprint(stubs, value: str) -> None:
    with_gpg(stubs)
    result = stubs.run("--sign-key", value)
    assert result.returncode != 0 and "--sign-key must be" in result.stderr and not stubs.runs()


def test_sign_key_without_a_value_is_refused(stubs) -> None:
    with_gpg(stubs)
    assert stubs.run("--sign-key").returncode != 0


@pytest.mark.parametrize("key", ["0x0123456789abcdef", "0123456789ABCDEF", FINGERPRINT, FINGERPRINT.lower()])
def test_sign_key_accepts_long_ids_and_fingerprints(stubs, key: str) -> None:
    with_gpg(stubs)
    assert stubs.run("--check", "--sign-key", key).returncode == 0


def test_a_missing_secret_key_is_found_before_the_build(stubs) -> None:
    with_gpg(stubs)
    result = stubs.run("--sign-key", FINGERPRINT, extra_env={"STUB_GPG_NO_KEY": "1"})
    assert result.returncode != 0 and "no secret key" in result.stderr and not stubs.runs()
    assert stubs.run("--check", "--sign-key", FINGERPRINT, extra_env={"STUB_GPG_NO_KEY": "1"}).returncode != 0


def test_a_test_key_iso_is_never_signed(stubs) -> None:
    with_gpg(stubs)
    key_file = stubs.tmp / "key.pub"
    key_file.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOyxpnq6y2K3rBtYBCPPNjpzV7cXzEvZ0NPIPXhdtJJL test\n")
    result = stubs.run("--sign-key", FINGERPRINT, "--test-key-file", str(key_file))
    assert result.returncode != 0 and "never signed" in result.stderr and not stubs.runs()


def test_a_failed_signature_fails_the_build_step(stubs) -> None:
    with_gpg(stubs)
    failed = stubs.run("--sign-key", FINGERPRINT, extra_env={"STUB_GPG_SIGN_FAILS": "1"})
    assert failed.returncode != 0 and "could not sign" in failed.stderr
    bad = stubs.run("--sign-key", FINGERPRINT, extra_env={"STUB_GPG_BAD": "1"})
    assert bad.returncode != 0 and "does not verify" in bad.stderr


def test_stage1_only_does_not_expect_an_iso(stubs) -> None:
    result = stubs.run("--stage1-only")
    assert result.returncode == 0 and "stage1 and its receipt" in result.stdout


# --- clean ---------------------------------------------------------------------------------


def test_clean_runs_a_cleanup_container_and_no_build(stubs) -> None:
    result = stubs.run("--clean", extra_env={"ISO_KEEP_STAGE1": "3"})
    assert result.returncode == 0, result.stderr
    run = stubs.runs()[0]
    assert option_values(run, "--entrypoint") == ["/bin/bash"] and "--rm" in run
    env = container_env(run)
    assert env["KEEP"] == "3" and env["CLEAN_PACKAGES"] == "0"
    assert "livecd-stage1-amd64-*.tar.gz" in run[-1] and "packages/" in run[-1]
    assert len(stubs.runs()) == 1 and "BUILD_MODE" not in env


def test_clean_packages_also_empties_the_package_cache(stubs) -> None:
    assert stubs.run("--clean-packages").returncode == 0
    assert container_env(stubs.runs()[0])["CLEAN_PACKAGES"] == "1"


# --- disk space ----------------------------------------------------------------------------


def df_rules(*rules) -> str:
    """Rules for the df stub; the directories must exist because df is asked about the nearest existing one."""
    return json.dumps([[needle if needle.startswith("/") else "/" + needle, *rest] for needle, *rest in rules])


@pytest.fixture(autouse=True)
def _existing_directories(stubs) -> None:
    for name in ("cache", "dist"):
        (stubs.tmp / name).mkdir(exist_ok=True)


def test_too_little_space_on_the_cache_filesystem_names_it(stubs) -> None:
    rules = df_rules(["cache", "bigdisk", 10 * 1024 * 1024, "/mnt/big"])
    result = stubs.run("--check", extra_env={"STUB_DF": rules})
    assert result.returncode != 0
    assert "not enough free space on /mnt/big for cache" in result.stderr
    assert "need 45 GiB, have 10 GiB" in result.stdout


def test_lowering_the_floor_lets_the_check_pass(stubs) -> None:
    rules = df_rules(["cache", "bigdisk", 10 * 1024 * 1024, "/mnt/big"])
    assert stubs.run("--check", extra_env={"STUB_DF": rules, "BUILD_MIN_FREE_GIB": "1"}).returncode == 0


def test_cache_and_output_on_one_filesystem_are_summed(stubs) -> None:
    rules = df_rules(["cache", "one", 48 * 1024 * 1024, "/data"], ["dist", "one", 48 * 1024 * 1024, "/data"])
    result = stubs.run("--check", extra_env={"STUB_DF": rules})
    assert result.returncode != 0 and "cache, output" in result.stdout + result.stderr
    ok = df_rules(["cache", "one", 60 * 1024 * 1024, "/data"], ["dist", "one", 60 * 1024 * 1024, "/data"])
    assert stubs.run("--check", extra_env={"STUB_DF": ok}).returncode == 0


def test_output_filesystem_has_its_own_floor(stubs) -> None:
    rules = df_rules(["dist", "small", 2 * 1024 * 1024, "/srv"])
    result = stubs.run("--check", extra_env={"STUB_DF": rules})
    assert result.returncode != 0 and "/srv for output" in result.stderr


def test_docker_storage_is_checked_when_readable_and_only_warned_about_when_not(stubs) -> None:
    root = stubs.tmp / "docker-root"
    root.mkdir()
    low = df_rules(["docker-root", "dockerfs", 1024 * 1024, "/var/lib/docker"])
    result = stubs.run("--check", extra_env={"STUB_DF": low, "STUB_DOCKER_ROOT": str(root)})
    assert result.returncode != 0 and "/var/lib/docker for docker" in result.stderr
    unreadable = stubs.run("--check", extra_env={"STUB_DOCKER_ROOT": str(stubs.tmp / "not-here")})
    assert unreadable.returncode == 0 and "cannot read Docker's storage directory" in unreadable.stderr


def test_nearest_existing_parent_is_used_for_directories_that_do_not_exist_yet(stubs) -> None:
    deep = stubs.tmp / "a/b/c/cache"
    result = stubs.run("--check", extra_env={"ISO_CACHE_DIR": str(deep), "STUB_DF": json.dumps([["/dist", "full", 1, "/full"]])})
    assert result.returncode != 0 and "/full for output" in result.stderr, "dist is on the stubbed small filesystem"
    result = stubs.run("--check", extra_env={"ISO_CACHE_DIR": str(deep)})
    assert result.returncode == 0 and not (stubs.tmp / "a").exists()


# --- verify --------------------------------------------------------------------------------


def test_verify_option_hands_over_to_the_verifier(stubs) -> None:
    result = stubs.run("--verify", str(stubs.tmp / "nothing.iso"), "--flavor", "testkey")
    assert result.returncode == 2 and "no such file" in result.stderr
