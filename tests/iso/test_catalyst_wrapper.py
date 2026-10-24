"""iso/catalyst-no-loop.py patches private parts of Catalyst 4.2; these tests pin its behaviour.

The wrapper's most expensive failure so far was a build without bounded ninja jobs, so its
environment and make.conf handling are checked against the real Catalyst classes. The
tests are skipped for other Catalyst releases on purpose: an upgrade must be reviewed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from .conftest import ROOT, load_script

catalyst = pytest.importorskip("catalyst")
if not str(catalyst.__version__).startswith("4.2"):
    pytest.skip(f"the wrapper was written for Catalyst 4.2.x, found {catalyst.__version__}; review it first", allow_module_level=True)

from catalyst.base.stagebase import StageBase  # noqa: E402
from catalyst.support import CatalystError  # noqa: E402

wrapper = load_script("iso/catalyst-no-loop.py", "catalyst_no_loop")
render = load_script("iso/render-config.py", "render_config")
keys = load_script("iso/make_conf_keys.py", "make_conf_keys")


def run_patched(exercise):
    """Call wrapper.main() with a fake catalyst_main that runs `exercise` while the patches are active."""
    result = {}

    def fake_main(arguments):
        result["value"] = exercise()

    original = wrapper.catalyst_main
    wrapper.catalyst_main = fake_main
    try:
        wrapper.main([])
    finally:
        wrapper.catalyst_main = original
    return result["value"]


def stage(**settings):
    base = {"subarch": "amd64", "jobs": 4}
    base.update(settings)
    return SimpleNamespace(settings=base, env={})


@pytest.mark.parametrize(
    ("settings", "expected_ninja", "expected_make"),
    [
        ({"jobs": 4, "load-average": 8}, "-j4 -l8", "-j4 -l8"),
        ({"jobs": 2}, "-j2", "-j2"),
        ({"load-average": 1.5, "jobs": None}, "-j1 -l1.5", "-l1.5"),
    ],
)
def test_ninja_is_bounded_without_a_jobserver(settings, expected_ninja, expected_make) -> None:
    if settings.get("jobs") is None:
        settings = {k: v for k, v in settings.items() if k != "jobs"}
    fake = stage(**settings)
    if "jobs" not in settings:
        fake.settings.pop("jobs")
    run_patched(lambda: StageBase.setup_environment(fake))
    assert fake.env["NINJAOPTS"] == expected_ninja
    assert fake.env["MAKEOPTS"] == expected_make
    assert fake.env["clst_basearch"] == "amd64"


def test_without_job_settings_ninja_still_gets_one_job() -> None:
    fake = SimpleNamespace(settings={"subarch": "amd64"}, env={})
    run_patched(lambda: StageBase.setup_environment(fake))
    assert fake.env["NINJAOPTS"] == "-j1"


def test_jobserver_fifo_branch_is_untouched_apart_from_basearch() -> None:
    fifo = stage(**{"jobserver-fifo": "/tmp/fifo", "load-average": 3})
    plain = stage(**{"jobserver-fifo": "/tmp/fifo", "load-average": 3})
    original = StageBase.setup_environment
    original(plain)
    run_patched(lambda: StageBase.setup_environment(fifo))
    assert {k: v for k, v in fifo.env.items() if k != "clst_basearch"} == plain.env
    assert fifo.env["clst_basearch"] == "amd64"


@pytest.mark.parametrize("jobs", [0, "4;bad", "nan", "", "-1", True])
def test_invalid_job_counts_are_refused(jobs) -> None:
    fake = stage(jobs=jobs)
    with pytest.raises(CatalystError):
        run_patched(lambda: StageBase.setup_environment(fake))


def test_only_amd64_is_supported() -> None:
    for settings in ({"subarch": "arm64"}, {}):
        fake = SimpleNamespace(settings=dict(settings, jobs=2), env={})
        with pytest.raises(CatalystError, match="amd64"):
            run_patched(lambda: StageBase.setup_environment(fake))
    fake = stage()
    fake.env["clst_basearch"] = "arm64"

    def exercise():
        # setup_environment of Catalyst does not reset env, the wrapper must notice the conflict
        return StageBase.setup_environment(fake)

    with pytest.raises(CatalystError, match="conflicts"):
        run_patched(exercise)


def test_patches_are_restored_after_an_exception() -> None:
    originals = {name: getattr(StageBase, name) for name in ("set_snapshot", "bind", "write_make_conf", "setup_environment")}

    def failing(arguments):
        raise RuntimeError("boom")

    original = wrapper.catalyst_main
    wrapper.catalyst_main = failing
    try:
        with pytest.raises(RuntimeError):
            wrapper.main([])
    finally:
        wrapper.catalyst_main = original
    for name, method in originals.items():
        assert getattr(StageBase, name) is method, name


# --- make.conf handling -------------------------------------------------------------------


def extra_file(tmp_path: Path, content: str | None = None) -> Path:
    """Write the file the entrypoint writes, from the real iso/portage/make.conf."""
    if content is None:
        env = {"GENTOO_TREEISH": "0" * 40, "VERSION_STAMP": "x", "STAGE3_BASENAME": "x", "ISO_NAME": "x.iso",
               "ISO_VOLID": "X", "ISO_BOOTARGS": "nodhcp"}
        values = render.token_values(env, str(tmp_path), "4", "8")
        template = (ROOT / "iso/portage/make.conf").read_text()
        content = render.render_make_conf_extra(render.extract_assignments(render.render_text(template, values)))
    path = tmp_path / "make.conf.extra"
    path.write_text(content)
    return path


def read_extra(path: Path | str):
    os.environ["GENTOO_INSTALLER_MAKE_CONF_EXTRA"] = str(path)
    try:
        return wrapper.read_make_conf_extra()
    finally:
        del os.environ["GENTOO_INSTALLER_MAKE_CONF_EXTRA"]


def test_the_wrapper_accepts_exactly_what_the_renderer_writes(tmp_path: Path) -> None:
    values = read_extra(extra_file(tmp_path))
    assert list(values) == list(keys.BUILD_KEYS)
    assert values["MAKEOPTS"] == "-j4" and values["NINJAOPTS"] == "-j4 -l8"


def test_both_sides_use_one_key_list() -> None:
    """Drift guard: a key added to the renderer but not to the wrapper (or the reverse) fails here."""
    source = (ROOT / "iso/catalyst-no-loop.py").read_text()
    assert "make_conf_keys.BUILD_KEYS" in source
    assert not re.search(r'"ACCEPT_LICENSE"|"PYTHON_SINGLE_TARGET"', source), "the wrapper must not carry its own copy of the key list"
    assert set(keys.LIVE_KEYS) <= set(keys.BUILD_KEYS)
    template = (ROOT / "iso/portage/make.conf").read_text()
    for key in keys.BUILD_KEYS:
        assert len(re.findall(rf"^{key}=", template, re.M)) == 1, key
    # ...and the other way round: nothing in the template is silently dropped by Catalyst.
    compiler_flags = {"COMMON_FLAGS", "CFLAGS", "CXXFLAGS", "FCFLAGS", "FFLAGS"}
    assigned = set(re.findall(r"^([A-Z_]+)=", template, re.M))
    assert assigned - compiler_flags == set(keys.BUILD_KEYS)


@pytest.mark.parametrize(
    "line",
    [
        'FOO="x"',                       # unknown key
        'MAKEOPTS="-j2"\nMAKEOPTS="-j3"',  # duplicate
        'MAKEOPTS="$(id)"',
        'MAKEOPTS="`id`"',
        'MAKEOPTS=-j2',                   # not quoted
        'MAKEOPTS="-j2" # trailing',
    ],
)
def test_read_make_conf_extra_rejects_unsafe_content(tmp_path: Path, line: str) -> None:
    with pytest.raises(CatalystError):
        read_extra(extra_file(tmp_path, line + "\n"))


def test_read_make_conf_extra_rejects_bad_files(tmp_path: Path) -> None:
    good = extra_file(tmp_path)
    with pytest.raises(CatalystError):
        read_extra("relative/path")
    link = tmp_path / "link"
    link.symlink_to(good)
    with pytest.raises(CatalystError):
        read_extra(link)
    big = tmp_path / "big"
    big.write_text('MAKEOPTS="-j2"\n' + "# " + "x" * 20000 + "\n")
    with pytest.raises(CatalystError):
        read_extra(big)
    with pytest.raises(CatalystError):
        read_extra(tmp_path)  # a directory
    assert wrapper.read_make_conf_extra() == {}


def make_conf_stage(tmp_path: Path):
    chroot = tmp_path / "chroot"
    (chroot / "etc/portage").mkdir(parents=True, exist_ok=True)
    fake = SimpleNamespace(settings={
        "chroot_path": str(chroot), "make_conf": "/etc/portage/make.conf", "options": [], "catalyst_use": [],
        "target_distdir": "/var/cache/catalyst/distfiles", "target_pkgdir": "/var/cache/binpkgs",
        "spec_prefix": "livecd", "use": ["networkmanager"],
    }, env={})
    return fake, chroot / "etc/portage/make.conf"


def test_build_chroot_gets_every_setting_and_the_live_image_only_the_safe_ones(tmp_path: Path) -> None:
    os.environ["GENTOO_INSTALLER_MAKE_CONF_EXTRA"] = str(extra_file(tmp_path))
    try:
        fake, path = make_conf_stage(tmp_path)

        def exercise():
            StageBase.write_make_conf(fake, setup=True)
            build = path.read_text()
            StageBase.write_make_conf(fake, setup=False)
            return build, path.read_text()

        build, live = run_patched(exercise)
    finally:
        del os.environ["GENTOO_INSTALLER_MAKE_CONF_EXTRA"]
    for key in keys.BUILD_KEYS:
        assert re.search(rf'^{key}="', build, re.M), f"{key} missing from the build make.conf"
    assert re.search(r'^FEATURES=".*\bbuildpkg\b', build, re.M)
    assert re.search(r'^DISTDIR="/var/cache/catalyst/distfiles"', build, re.M), "Catalyst's own DISTDIR is part of the build configuration"
    for key in ("MAKEOPTS", "NINJAOPTS", "EMERGE_DEFAULT_OPTS", "ACCEPT_LICENSE", "DISTDIR", "PKGDIR"):
        assert not re.search(rf"^{key}=", live, re.M), f"{key} must not be in the live image"
    assert "buildpkg" not in live
    for key in keys.LIVE_KEYS:
        assert re.search(rf'^{key}="', live, re.M), f"{key} missing from the live make.conf"
    assert re.search(r'^USE=".*networkmanager', live, re.M), "USE still comes from the spec"
    assert 'FEATURES="binpkg-logs protect-owned parallel-fetch strict"' in live


def test_live_make_conf_leaves_emerge_jobs_to_autoconfig(tmp_path: Path) -> None:
    """livecd-tools adds --jobs=$(nproc) only when make.conf has no EMERGE_DEFAULT_OPTS with --jobs."""
    os.environ["GENTOO_INSTALLER_MAKE_CONF_EXTRA"] = str(extra_file(tmp_path))
    try:
        fake, path = make_conf_stage(tmp_path)
        live = run_patched(lambda: (StageBase.write_make_conf(fake, setup=False), path.read_text())[1])
    finally:
        del os.environ["GENTOO_INSTALLER_MAKE_CONF_EXTRA"]
    assert not re.search(r"EMERGE_DEFAULT_OPTS=.*--jobs", live)


def test_without_extra_settings_catalyst_output_is_unchanged(tmp_path: Path) -> None:
    fake, path = make_conf_stage(tmp_path / "patched")
    ours = run_patched(lambda: (StageBase.write_make_conf(fake, setup=True), path.read_text())[1])
    plain_fake, plain_path = make_conf_stage(tmp_path / "plain")
    StageBase.write_make_conf(plain_fake, setup=True)
    assert ours.replace(str(tmp_path / "patched"), "") == plain_path.read_text().replace(str(tmp_path / "plain"), "")


# --- directory snapshot -------------------------------------------------------------------


def snapshot_tree(tmp_path: Path, commit: str = "a" * 40, receipt: dict | None = None) -> SimpleNamespace:
    tree = tmp_path / "snapshots" / f"gentoo-{commit}.tree"
    (tree / "profiles").mkdir(parents=True)
    (tree / "metadata").mkdir()
    (tree / "profiles/repo_name").write_text("gentoo\n")
    (tree / "metadata/layout.conf").write_text("masters =\n")
    (tree / ".catalyst-snapshot.json").write_text(json.dumps(receipt or {"repo_name": "gentoo", "treeish": commit}))
    return SimpleNamespace(settings={"repo_name": "gentoo", "snapshot_treeish": commit, "storedir": str(tmp_path)}, snapshot=None)


def test_directory_snapshot_is_accepted_and_selected(tmp_path: Path) -> None:
    fake = snapshot_tree(tmp_path)
    wrapper.set_directory_snapshot(fake)
    assert fake.snapshot == str(tmp_path / "snapshots" / ("gentoo-" + "a" * 40 + ".tree"))


@pytest.mark.parametrize(
    "receipt",
    [{"repo_name": "gentoo", "treeish": "b" * 40}, {"repo_name": "other", "treeish": "a" * 40}, ["not", "a", "dict"]],
)
def test_mismatching_receipt_is_refused(tmp_path: Path, receipt) -> None:
    with pytest.raises(CatalystError):
        wrapper.set_directory_snapshot(snapshot_tree(tmp_path, receipt=receipt))


def test_symlinked_or_moving_snapshot_is_refused(tmp_path: Path) -> None:
    fake = snapshot_tree(tmp_path)
    tree = Path(tmp_path / "snapshots" / ("gentoo-" + "a" * 40 + ".tree"))
    moved = tree.with_name("real")
    tree.rename(moved)
    tree.symlink_to(moved)
    with pytest.raises(CatalystError, match="missing or unsafe"):
        wrapper.set_directory_snapshot(fake)
    fake.settings["snapshot_treeish"] = "master"
    with pytest.raises(CatalystError, match="full pinned git commit"):
        wrapper.set_directory_snapshot(fake)
