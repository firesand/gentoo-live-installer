"""iso/builder-entrypoint.sh and its libraries, run outside the container on fixture trees."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import sys
import time

import pytest

from .conftest import ROOT, load_script, run_bash, write_executable

ENTRYPOINT = ROOT / "iso/builder-entrypoint.sh"
verify = load_script("scripts/verify-iso.py", "verify_iso")
reuse = load_script("iso/stage_reuse.py", "stage_reuse")


@pytest.fixture()
def sandbox(tmp_path: Path):
    """Directories and stub commands so that entrypoint functions can run as an ordinary user."""
    bin_dir = tmp_path / "bin"
    log = tmp_path / "commands.log"
    for name in ("chown", "ebuild", "catalyst"):
        write_executable(bin_dir / name, f'#!/bin/sh\necho "{name} $*" >> {shlex.quote(str(log))}\nif [ "{name}" = catalyst ] && [ "$1" = --version ]; then echo "Catalyst 4.2.0"; fi\nexit 0\n')
    dirs = {name: tmp_path / name for name in ("build", "store", "distfiles", "dist", "cargo")}
    for path in dirs.values():
        path.mkdir()
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "WORKSPACE": str(ROOT),
        "BUILD_DIR": str(dirs["build"]),
        "CATALYST_STORE": str(dirs["store"]),
        "DISTDIR": str(dirs["distfiles"]),
        "DIST": str(dirs["dist"]),
        "CARGO_CACHE": str(dirs["cargo"]),
    }

    class Sandbox:
        def __init__(self) -> None:
            self.env, self.dirs, self.log, self.tmp = env, dirs, log, tmp_path

        def run(self, body: str, **extra: str) -> subprocess.CompletedProcess:
            return run_bash(f"source {shlex.quote(str(ENTRYPOINT))}\n{body}", env={**env, **extra})

        def commands(self) -> list[str]:
            return self.log.read_text().splitlines() if self.log.exists() else []

        def archives(self) -> None:
            for prefix in reuse.LOCAL_PACKAGES:
                version = reuse.package_version(reuse.ebuild_file(ROOT, prefix))
                (dirs["distfiles"] / f"{prefix}-{version}.tar.zst").write_bytes(f"archive of {prefix}".encode())

    return Sandbox()


def test_unknown_build_mode_exits_non_zero() -> None:
    result = subprocess.run(["bash", str(ENTRYPOINT)], capture_output=True, text=True, env={**os.environ, "BUILD_MODE": "turbo", "WORKSPACE": str(ROOT)}, check=False)
    assert result.returncode != 0 and "unknown BUILD_MODE 'turbo'" in result.stderr


def test_sourcing_the_entrypoint_does_not_start_a_build(sandbox) -> None:
    result = sandbox.run("declare -F main >/dev/null && echo defined")
    assert result.returncode == 0 and result.stdout.strip() == "defined"
    assert not any(sandbox.dirs["build"].iterdir())


@pytest.mark.parametrize(
    ("atom", "version"),
    [("app-admin/gentoo-live-installer", "0.1.0"), ("app-misc/hwmonux", "1.0.0"), ("app-portage/portagehub", "0.4.0")],
)
def test_versions_come_from_the_committed_ebuilds(sandbox, atom: str, version: str) -> None:
    result = sandbox.run(f"package_version {atom}")
    assert result.returncode == 0 and result.stdout.strip() == version


def test_two_ebuilds_of_one_package_stop_the_build(sandbox, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    overlay = workspace / "packaging/gentoo-overlay/app-misc/hwmonux"
    overlay.mkdir(parents=True)
    (overlay / "hwmonux-1.0.0.ebuild").write_text("")
    (overlay / "hwmonux-1.0.0-r1.ebuild").write_text("")
    result = sandbox.run("package_version app-misc/hwmonux", WORKSPACE=str(workspace))
    assert result.returncode != 0 and "needs exactly one ebuild" in result.stderr


def test_flavours_follow_the_test_key(sandbox) -> None:
    release = sandbox.run("load_environment; echo $ISO_FLAVOR $ISO_NAME $ISO_VOLID")
    assert release.stdout.split() == ["release", "gentoo-installer-amd64-20261018T124555Z.iso", "GENTOO_INSTALLER_AMD64"]
    test = sandbox.run("load_environment; echo $ISO_FLAVOR $ISO_NAME $ISO_VOLID", GENTOO_TEST_SSH_PUBLIC_KEY="ssh-ed25519 AAAA k")
    assert test.stdout.split() == ["testkey", "gentoo-installer-amd64-20261018T124555Z-TESTKEY.iso", "GENTOO_INSTALLER_TESTKEY"]


def test_flavour_and_key_must_agree(sandbox) -> None:
    result = sandbox.run("load_environment", ISO_FLAVOR="release", GENTOO_TEST_SSH_PUBLIC_KEY="ssh-ed25519 AAAA k")
    assert result.returncode != 0 and "does not match the test key" in result.stderr
    result = sandbox.run("load_environment", ISO_FLAVOR="testkey")
    assert result.returncode != 0


@pytest.mark.parametrize(("var", "value"), [("JOBS", "0"), ("JOBS", "two"), ("LOAD_AVERAGE", "x")])
def test_bad_numbers_are_refused(sandbox, var: str, value: str) -> None:
    assert sandbox.run("load_environment", **{var: value}).returncode != 0


# --- catalyst.conf --------------------------------------------------------------------------


def catalyst_conf(sandbox, plan: str, extra: str = "") -> str:
    result = sandbox.run(
        f"load_environment; EMERGE_DEFAULT_OPTS='--getbinpkg'; PLAN={plan}; write_catalyst_config {shlex.quote(extra)}; cat \"$BUILD_DIR/catalyst.conf\"",
        JOBS="3",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_full_build_keeps_the_stage1_chroot_for_stage2(sandbox) -> None:
    assert 'options = ["seedcache", "pkgcache"]' in catalyst_conf(sandbox, "full")


@pytest.mark.parametrize("plan", ["stage2-only", "reseed", "stage1-only"])
def test_reuse_paths_never_use_a_retained_chroot(sandbox, plan: str) -> None:
    """A stale chroot directory would win over the archive that was checked against its receipt."""
    text = catalyst_conf(sandbox, plan)
    assert 'options = ["pkgcache"]' in text and "seedcache" not in text


def test_catalyst_conf_values(sandbox) -> None:
    text = catalyst_conf(sandbox, "full")
    assert "jobs = 3" in text and "load-average = 6" in text
    assert 'target_pkgdir = "/var/cache/binpkgs"' in text
    assert 'envvars = { SOURCE_DATE_EPOCH = "1792327555", EMERGE_DEFAULT_OPTS = "--getbinpkg" }' in text
    assert 'repo_openpgp_key_path = ""' in text


def test_reseed_adds_exactly_the_reinstall_options(sandbox) -> None:
    text = catalyst_conf(sandbox, "reseed", "--reinstall-atoms=app-misc/hwmonux --reinstall-atoms=app-portage/portagehub")
    assert 'EMERGE_DEFAULT_OPTS = "--getbinpkg --reinstall-atoms=app-misc/hwmonux --reinstall-atoms=app-portage/portagehub"' in text


# --- stage1 reuse planning ---------------------------------------------------------------------


def plan(sandbox, mode: str) -> subprocess.CompletedProcess:
    result = sandbox.run(
        "load_environment; resolve_build_plan && echo \"$PLAN $STAGE1_MODE [$RESEED_ATOMS]\"",
        BUILD_MODE=mode,
    )
    # the decision is also logged ("==> Stage1 decision: ..."); the tests compare the result line
    result.stdout = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return result


def write_receipt(sandbox, archive: Path) -> None:
    done = subprocess.run(
        [sys.executable, str(ROOT / "iso/stage_reuse.py"), "receipt", "--workspace", str(ROOT), "--distfiles", str(sandbox.dirs["distfiles"]),
         "--archive", str(archive), "--catalyst-version", "Catalyst 4.2.0"],
        capture_output=True, text=True, check=True,
    )
    Path(str(archive) + ".receipt.json").write_text(done.stdout)


@pytest.fixture()
def stage1(sandbox) -> Path:
    sandbox.archives()
    archive = sandbox.dirs["store"] / "builds/23.0-default/livecd-stage1-amd64-installer-20261018T124555Z.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"stage1")
    return archive


def test_full_and_stage1_only_need_no_decision(sandbox) -> None:
    assert plan(sandbox, "full").stdout.strip() == "full fresh []"
    assert plan(sandbox, "stage1-only").stdout.strip() == "stage1-only fresh []"


def test_auto_without_a_receipt_builds_everything(sandbox, stage1: Path) -> None:
    assert plan(sandbox, "auto").stdout.strip() == "full fresh []"


def test_auto_with_a_matching_receipt_runs_stage2_only(sandbox, stage1: Path) -> None:
    write_receipt(sandbox, stage1)
    assert plan(sandbox, "auto").stdout.strip() == "stage2-only reuse []"
    assert plan(sandbox, "stage2-only").stdout.strip() == "stage2-only reuse []"


def test_changed_local_sources_reseed_only_those_packages(sandbox, stage1: Path) -> None:
    write_receipt(sandbox, stage1)
    version = reuse.package_version(reuse.ebuild_file(ROOT, "portagehub"))
    (sandbox.dirs["distfiles"] / f"portagehub-{version}.tar.zst").write_bytes(b"new sources")
    # auto never reseeds (a real reseed kept the old package); only an explicit reseed does
    assert plan(sandbox, "auto").stdout.strip() == "full fresh []"
    assert plan(sandbox, "reseed").stdout.strip() == "reseed reseed [app-portage/portagehub]"
    refused = plan(sandbox, "stage2-only")
    assert refused.returncode != 0 and "cannot run stage2 only" in refused.stderr


def test_stage2_only_without_a_receipt_is_an_error_and_auto_falls_back(sandbox, stage1: Path) -> None:
    refused = plan(sandbox, "stage2-only")
    assert refused.returncode != 0 and "no receipt" in refused.stderr
    explicit = plan(sandbox, "reseed")
    assert explicit.returncode != 0 and "needs a usable earlier stage1" in explicit.stderr


def test_a_tampered_archive_is_never_reused(sandbox, stage1: Path) -> None:
    write_receipt(sandbox, stage1)
    stage1.write_bytes(b"tampered")
    assert plan(sandbox, "auto").stdout.strip() == "full fresh []"
    assert plan(sandbox, "stage2-only").returncode != 0


# --- test access -----------------------------------------------------------------------------


def prepare_overlay(sandbox) -> None:
    root_overlay = sandbox.dirs["build"] / "root_overlay/etc"
    root_overlay.mkdir(parents=True)
    (root_overlay / "issue").write_text("issue text\n")
    (root_overlay / "motd").write_text("motd text\n")
    (sandbox.dirs["build"] / "livecd-stage2.spec").write_text("livecd/rcadd:\n\tdbus|default\n\tgentoo-installerd|default\n\tchronyd|default\n")


def test_test_access_is_configured_and_announced(sandbox) -> None:
    prepare_overlay(sandbox)
    result = sandbox.run("configure_test_access", GENTOO_TEST_SSH_PUBLIC_KEY="ssh-ed25519 AAAA key")
    assert result.returncode == 0, result.stderr
    build = sandbox.dirs["build"] / "root_overlay"
    keys = build / "root/.ssh/authorized_keys"
    assert keys.read_text() == "ssh-ed25519 AAAA key\n"
    assert stat.S_IMODE(keys.stat().st_mode) == 0o600 and stat.S_IMODE(keys.parent.stat().st_mode) == 0o700
    assert (build / "etc/gentoo-installer-vm-test").exists()
    assert "PermitRootLogin prohibit-password" in (build / "etc/ssh/sshd_config.d/90-installer-vm-test.conf").read_text()
    for name in ("issue", "motd"):
        text = (build / "etc" / name).read_text()
        assert "TEST BUILD" in text.splitlines()[1] and text.endswith(f"{name} text\n")
    spec = (sandbox.dirs["build"] / "livecd-stage2.spec").read_text()
    assert "\tgentoo-installerd|default\n\tsshd|default\n" in spec


def test_without_a_key_nothing_changes(sandbox) -> None:
    prepare_overlay(sandbox)
    assert sandbox.run("configure_test_access").returncode == 0
    root = sandbox.dirs["build"]
    assert not (root / "root_overlay/root").exists() and "sshd" not in (root / "livecd-stage2.spec").read_text()
    assert "TEST BUILD" not in (root / "root_overlay/etc/issue").read_text()


@pytest.mark.parametrize("key", ["not a key", "ssh-ed25519 AAAA\nssh-rsa BBBB", "-----BEGIN OPENSSH PRIVATE KEY-----"])
def test_bad_test_keys_stop_the_build(sandbox, key: str) -> None:
    prepare_overlay(sandbox)
    result = sandbox.run("configure_test_access", GENTOO_TEST_SSH_PUBLIC_KEY=key)
    assert result.returncode != 0 and "one public key line" in result.stderr


def test_the_rcadd_needle_is_in_the_real_spec(pins: dict[str, str]) -> None:
    """configure_test_access inserts sshd after this exact line of the rendered spec."""
    text = (ROOT / "iso/specs/livecd-stage2.spec").read_text()
    assert "\tgentoo-installerd|default\n" in text


# --- I10: the overlay is root-owned and has sane modes ------------------------------------------


def test_overlay_copies_lose_the_builders_ownership_and_modes_are_normalized(sandbox, tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "etc/init.d").mkdir(parents=True)
    (source / "usr/local/bin").mkdir(parents=True)
    (source / "etc/a.conf").write_text("x")
    (source / "etc/secret").write_text("y")
    (source / "etc/init.d/svc").write_text("#!/bin/sh\n")
    (source / "usr/local/bin/tool").write_text("#!/bin/sh\n")
    os.chmod(source / "etc", 0o700)
    os.chmod(source / "etc/a.conf", 0o600)
    os.chmod(source / "etc/secret", 0o666)
    os.chmod(source / "etc/init.d/svc", 0o750)
    os.chmod(source / "usr/local/bin/tool", 0o4755)
    result = sandbox.run(
        f"source {ROOT}/iso/lib/overlay.sh; copy_tree_without_owner {shlex.quote(str(source))} {tmp_path}/copy; normalize_tree {tmp_path}/copy",
    )
    assert result.returncode == 0, result.stderr
    copy = tmp_path / "copy"
    assert stat.S_IMODE((copy / "etc").stat().st_mode) == 0o755
    assert stat.S_IMODE((copy / "etc/a.conf").stat().st_mode) == 0o644
    assert stat.S_IMODE((copy / "etc/secret").stat().st_mode) == 0o644
    assert stat.S_IMODE((copy / "etc/init.d/svc").stat().st_mode) == 0o755
    assert stat.S_IMODE((copy / "usr/local/bin/tool").stat().st_mode) == 0o755, "set-id bits are dropped"
    assert any(line == f"chown -R 0:0 -- {copy}" for line in sandbox.commands()), sandbox.commands()


def test_prepare_portage_builds_the_inputs_of_both_stages(sandbox) -> None:
    sandbox.archives()
    result = sandbox.run("load_environment; prepare_portage; echo \"$EMERGE_DEFAULT_OPTS\"; echo $GENTOO_INSTALLER_MAKE_CONF_EXTRA")
    assert result.returncode == 0, result.stderr
    build = sandbox.dirs["build"]
    lines = result.stdout.splitlines()
    assert "--usepkg-exclude=app-misc/hwmonux" in lines[0] and "--jobs" not in lines[0]
    assert lines[1] == str(build / "portage/make.conf.extra")
    for name in ("livecd-stage1.spec", "livecd-stage2.spec", "README-live.txt", "fsscript-stage2.sh", "portage/make.conf", "portage/make.conf.extra",
                 "cd_overlay/boot/grub/grub.cfg", "cd_overlay/SECURE-BOOT-OFF.txt", "root_overlay/etc/lvm/lvmlocal.conf", "overlay/profiles/repo_name"):
        assert (build / name).is_file(), name
    shipped = build / "root_overlay/usr/local/share/gentoo-installer"
    assert (shipped / "overlay/app-misc/hwmonux/hwmonux-1.0.0-r1.ebuild").is_file()
    assert sorted(p.name for p in (shipped / "distfiles").iterdir()) == sorted(p.name for p in sandbox.dirs["distfiles"].iterdir())
    ebuild_calls = [c for c in sandbox.commands() if c.startswith("ebuild ")]
    assert len(ebuild_calls) == 4 and all(c.endswith(" manifest") for c in ebuild_calls)
    for path in build.rglob("*"):
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        assert not mode & 0o022, f"{path} is writable by group or others"
        assert not mode & 0o7000, path
    assert os.access(build / "root_overlay/etc/acpi/actions/powerbtn.sh", os.X_OK) and os.access(build / "fsscript-stage2.sh", os.X_OK)
    assert not os.access(build / "root_overlay/etc/motd", os.X_OK)
    assert any("chown -R 0:0" in c for c in sandbox.commands())
    # the spec that was rendered is the one Catalyst reads
    stage2 = verify.parse_spec((build / "livecd-stage2.spec").read_text())
    assert stage2["livecd/overlay"] == f"{build}/cd_overlay"


# --- cleanup after a failure ------------------------------------------------------------------


def test_exit_hands_the_outputs_back_to_the_user(sandbox) -> None:
    result = sandbox.run("trap on_exit EXIT; (exit 3)", HOST_UID="1234", HOST_GID="5678")
    assert "failed with status 3" in result.stderr
    chowns = [c for c in sandbox.commands() if c.startswith("chown")]
    assert chowns == [f"chown -R 1234:5678 {sandbox.dirs['dist']} {sandbox.dirs['distfiles']} {sandbox.dirs['cargo']}"]
    assert "catalyst" not in chowns[0] and str(sandbox.dirs["store"]) not in chowns[0], "the Catalyst store holds chroots and stays root-owned"


def test_exit_without_host_ids_does_not_chown(sandbox) -> None:
    sandbox.run("trap on_exit EXIT; true")
    assert not [c for c in sandbox.commands() if c.startswith("chown")]
    sandbox.run("trap on_exit EXIT; true", HOST_UID="abc", HOST_GID="5")
    assert not [c for c in sandbox.commands() if c.startswith("chown")]


# --- signals (I6) ------------------------------------------------------------------------------


def start_run_child(tmp_path: Path, body: str, env: dict[str, str] | None = None):
    flag = tmp_path / "started"
    child = write_executable(
        tmp_path / "fake-catalyst.py",
        f"""#!{sys.executable}
import pathlib, signal, sys, time
signal.signal(signal.SIGTERM, lambda *a: (pathlib.Path({str(tmp_path / 'got-term')!r}).write_text('term'), sys.exit(7)))
signal.signal(signal.SIGINT, lambda *a: (pathlib.Path({str(tmp_path / 'got-int')!r}).write_text('int'), sys.exit(8)))
pathlib.Path({str(flag)!r}).write_text('1')
{body}
""",
    )
    script = f"source {ROOT}/iso/lib/run.sh; run_child {shlex.quote(str(child))}; echo status=$?"
    process = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, **(env or {})})
    deadline = time.time() + 10
    while time.time() < deadline and not flag.exists():
        time.sleep(0.02)
    assert flag.exists(), "the fake Catalyst never started"
    return process


def test_sigterm_reaches_the_child_and_the_entrypoint_exits_143(tmp_path: Path) -> None:
    process = start_run_child(tmp_path, "time.sleep(60)")
    started = time.time()
    process.send_signal(signal.SIGTERM)
    out, err = process.communicate(timeout=10)
    assert time.time() - started < 5
    assert process.returncode == 143, (out, err)
    assert (tmp_path / "got-term").read_text() == "term"


def test_sigint_reaches_the_child_and_exits_130(tmp_path: Path) -> None:
    process = start_run_child(tmp_path, "time.sleep(60)")
    process.send_signal(signal.SIGINT)
    process.communicate(timeout=10)
    assert process.returncode == 130 and (tmp_path / "got-int").read_text() == "int"


def test_a_child_that_ignores_the_signal_is_killed_eventually(tmp_path: Path) -> None:
    body = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)"
    process = start_run_child(tmp_path, body, {"RUN_CHILD_GRACE_TENTHS": "10"})
    process.send_signal(signal.SIGTERM)
    out, err = process.communicate(timeout=20)
    assert process.returncode == 143


def test_normal_exit_status_is_returned(tmp_path: Path) -> None:
    process = start_run_child(tmp_path, "sys.exit(5)")
    out, _ = process.communicate(timeout=10)
    assert "status=5" in out
