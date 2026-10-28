"""scripts/sync-app.sh (and its scripts/sync-portagehub.sh): imports a commit of one of the user's applications into apps/.

The source is a temporary git repository; nothing here touches the real ones.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/sync-portagehub.sh"
SYNC_APP = ROOT / "scripts/sync-app.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("patch") is None, reason="needs git and patch")

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def run_git(directory: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


def write(base: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A PortageHub-shaped repository with two commits and some untracked local directories."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "master")
    write(
        repo,
        {
            "Cargo.toml": '[package]\nname = "portagehub"\nversion = "0.4.0"\n',
            "Cargo.lock": "# lock one\n",
            "src/main.rs": "fn main() {}\n",
            "src/old.rs": "// goes away\n",
            "dist/install.sh": "#!/bin/sh\n",
            "dist/portagehub.desktop": "[Desktop Entry]\n",
            "dist/gentoo/app-portage/portagehub/portagehub-0.4.0.ebuild": "# upstream's own ebuild\n",
            "packaging/gentoo/sys-kernel/kernelsmith/kernelsmith-9999.ebuild": "# KernelSmith's own ebuild\n",
            "docs/screenshots/home.png": "png",
            "docs/guide.md": "guide\n",
            ".github/workflows/ci.yml": "name: ci\n",
            ".gitignore": "/target\n",
            "README.md": "readme\n",
        },
    )
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "First state")
    (repo / "src/old.rs").unlink()
    write(repo, {"src/new.rs": "// arrives\n", "Cargo.lock": "# lock two\n"})
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "Second state: with a colon")
    # Local-only directories: not tracked, so never in the archive.
    write(repo, {"target/release/portagehub": "binary", "reports/r.md": "r", "research_notes/n.md": "n", "src/untracked.rs": "x"})
    return repo


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    directory = tmp_path / "apps/portagehub"
    write(directory, {"Cargo.toml": "old\n", "src/stale.rs": "stale\n", "docs/old.md": "old docs\n", "target/keep": "build output"})
    return directory


def sync(source: Path, dest: Path, *args: str, patches: Path | None = None) -> subprocess.CompletedProcess[str]:
    patch_dir = patches if patches is not None else dest.parent / "no-patches"
    return subprocess.run(
        ["bash", str(SCRIPT), "--source", str(source), "--dest", str(dest), "--patches", str(patch_dir), *args],
        capture_output=True,
        text=True,
        env=ENV,
    )


def snapshot(repo: Path) -> tuple[str, str, str]:
    """What a change to the source repository would show: HEAD, the work tree status and every file's contents."""
    files = sorted(p for p in repo.rglob("*") if p.is_file() and ".git/" not in str(p))
    return run_git(repo, "rev-parse", "HEAD"), run_git(repo, "status", "--porcelain", "--ignored"), repr([(str(p), p.read_bytes()) for p in files])


def test_head_is_exported_and_local_directories_stay_out(upstream: Path, dest: Path) -> None:
    result = sync(upstream, dest)
    assert result.returncode == 0, result.stderr
    assert (dest / "src/main.rs").read_text() == "fn main() {}\n"
    assert (dest / "src/new.rs").exists()
    assert (dest / "Cargo.lock").read_text() == "# lock two\n"
    assert (dest / "dist/install.sh").exists() and (dest / "dist/portagehub.desktop").exists()
    assert (dest / "README.md").exists()
    for name in (".github", ".gitignore", "dist/gentoo", "reports", "research_notes", "src/untracked.rs", "src/old.rs"):
        assert not (dest / name).exists(), name
    # The old contents are replaced; a build directory is not.
    for name in ("src/stale.rs", "docs/old.md"):
        assert not (dest / name).exists(), name
    assert (dest / "target/keep").read_text() == "build output"
    assert not (dest / "target/release").exists()


def test_upstream_file_records_source_commit_date_and_subject(upstream: Path, dest: Path) -> None:
    assert sync(upstream, dest).returncode == 0
    fields = dict(line.split(": ", 1) for line in (dest / "UPSTREAM").read_text().splitlines())
    assert fields["source"] == str(upstream.resolve())
    assert fields["commit"] == run_git(upstream, "rev-parse", "HEAD")
    assert fields["subject"] == "Second state: with a colon"
    assert fields["date"] == run_git(upstream, "show", "-s", "--format=%ci", "HEAD")
    assert list(fields) == ["source", "commit", "date", "subject"]


def test_source_name_replaces_the_recorded_path(upstream: Path, dest: Path) -> None:
    assert sync(upstream, dest, "--source-name", "/home/someone/PortageHub").returncode == 0
    assert (dest / "UPSTREAM").read_text().splitlines()[0] == "source: /home/someone/PortageHub"


def test_an_older_commit_can_be_named(upstream: Path, dest: Path) -> None:
    first = run_git(upstream, "rev-parse", "HEAD~1")
    result = sync(upstream, dest, first)
    assert result.returncode == 0, result.stderr
    assert (dest / "src/old.rs").exists() and not (dest / "src/new.rs").exists()
    assert (dest / "Cargo.lock").read_text() == "# lock one\n"
    assert f"commit: {first}" in (dest / "UPSTREAM").read_text()
    assert run_git(upstream, "rev-parse", "master") != first, "the source's branch did not move"


def test_a_diffstat_says_what_changed(upstream: Path, dest: Path) -> None:
    result = sync(upstream, dest)
    out = result.stdout
    assert "src/new.rs (new)" in out
    assert "src/stale.rs (removed)" in out
    assert "Cargo.toml" in out
    assert "files changed" in out and "insertions(+)" in out and "deletions(-)" in out
    # A second run changes nothing but the (identical) UPSTREAM file: no files are listed.
    again = sync(upstream, dest)
    assert again.returncode == 0
    assert "no change" in again.stdout and "files changed" not in again.stdout


def test_an_unknown_commit_is_refused_and_nothing_is_touched(upstream: Path, dest: Path) -> None:
    before = {p: p.read_bytes() for p in sorted(dest.rglob("*")) if p.is_file()}
    result = sync(upstream, dest, "0123456789abcdef0123456789abcdef01234567")
    assert result.returncode == 1
    assert "unknown commit" in result.stderr
    assert {p: p.read_bytes() for p in sorted(dest.rglob("*")) if p.is_file()} == before
    assert not (dest / "UPSTREAM").exists()


def test_a_source_that_is_not_a_repository_is_refused(tmp_path: Path, dest: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = sync(plain, dest)
    assert result.returncode == 1 and "not a git repository" in result.stderr
    assert (dest / "src/stale.rs").exists()
    missing = sync(tmp_path / "missing", dest)
    assert missing.returncode == 1 and "not a directory" in missing.stderr


def test_the_source_repository_is_not_changed(upstream: Path, dest: Path) -> None:
    before = snapshot(upstream)
    for args in ((), ("HEAD~1",), ("nonsense",)):
        sync(upstream, dest, *args)
    assert snapshot(upstream) == before


def test_patches_are_checked_against_the_new_tree(upstream: Path, dest: Path, tmp_path: Path) -> None:
    patches = tmp_path / "files"
    patches.mkdir()
    good = "--- a/src/main.rs\n+++ b/src/main.rs\n@@ -1 +1,2 @@\n fn main() {}\n+// patched\n"
    (patches / "0001-good.patch").write_text(good)
    result = sync(upstream, dest, patches=patches)
    assert result.returncode == 0, result.stderr
    assert "patch applies: 0001-good.patch" in result.stdout
    assert (dest / "src/main.rs").read_text() == "fn main() {}\n", "the check does not apply the patch"

    (patches / "0002-stale.patch").write_text("--- a/src/main.rs\n+++ b/src/main.rs\n@@ -1 +1 @@\n-fn other() {}\n+fn another() {}\n")
    result = sync(upstream, dest, patches=patches)
    assert result.returncode == 3
    assert "PATCH DOES NOT APPLY: 0002-stale.patch" in result.stderr
    assert (dest / "UPSTREAM").exists(), "the tree is updated; the exit status says the patches need attention"


def test_usage_errors() -> None:
    assert subprocess.run(["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True).returncode == 2
    assert subprocess.run(["bash", str(SCRIPT), "--source"], capture_output=True, text=True).returncode == 2
    assert subprocess.run(["bash", str(SCRIPT), "a", "b"], capture_output=True, text=True).returncode == 2
    helped = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert helped.returncode == 0 and "Usage: scripts/sync-app.sh APP" in helped.stdout and "set -Eeuo" not in helped.stdout
    assert "scripts/sync-portagehub.sh is `sync-app.sh portagehub`" in helped.stdout
    for argv in ([], ["bogus"], ["--source", "x"]):
        assert subprocess.run(["bash", str(SYNC_APP), *argv], capture_output=True, text=True).returncode == 2, argv


@pytest.mark.parametrize(("app", "name"), [("gentoo-settings", "Gentoo Settings"), ("kernelsmith", "KernelSmith"), ("portagehub", "PortageHub")])
def test_each_application_is_named_and_its_packaging_and_screenshots_stay_out(upstream: Path, tmp_path: Path, app: str, name: str) -> None:
    dest = tmp_path / "apps" / app
    result = subprocess.run(["bash", str(SYNC_APP), app, "--source", str(upstream), "--dest", str(dest), "--patches", str(tmp_path / "none")],
                            capture_output=True, text=True, env=ENV)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(f"{name} {run_git(upstream, 'rev-parse', 'HEAD')[:12]} Second state")
    assert (dest / "docs/guide.md").is_file() and not (dest / "docs/screenshots").exists()
    assert not (dest / "packaging/gentoo").exists() and not (dest / "dist/gentoo").exists()
    recorded = (dest / "UPSTREAM").read_text().splitlines()[0]
    assert recorded == ("source: https://github.com/firesand/kernelsmith.git" if app == "kernelsmith" else f"source: {upstream}")


def test_the_default_patches_are_next_to_each_ebuild() -> None:
    text = SYNC_APP.read_text()
    for package in ("app-portage/portagehub", "app-admin/gentoo-settings", "sys-kernel/kernelsmith"):
        assert f"package={package}" in text
