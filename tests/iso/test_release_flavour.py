"""I5: an ISO with a test login cannot be mistaken for a release, and cannot land in dist/."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from .conftest import ROOT
from .test_build_iso_cli import RELEASE_NAME, TEST_NAME, container_env, option_values

pytestmark = pytest.mark.skipif(os.uname().machine != "x86_64", reason="the builder insists on an x86_64 host")

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq2nzJYQ1vK0cG9bJqvT7mQ0Yd5kX1c8eZqkJ1xW2aB vm-test"


@pytest.fixture()
def key_file(stubs) -> Path:
    path = stubs.tmp / "key.pub"
    path.write_text(PUBLIC_KEY + "\n")
    return path


def test_test_key_needs_its_own_output_directory(stubs, key_file: Path) -> None:
    result = stubs.run("--test-key-file", str(key_file), extra_env={"ISO_DIST_DIR": ""})
    assert result.returncode != 0
    assert "must not use the default dist/" in result.stderr
    assert not stubs.runs() and not [c for c in stubs.calls() if c[0] == "build"]


def test_test_key_cannot_use_the_release_directory_even_when_named(stubs, key_file: Path) -> None:
    result = stubs.run("--test-key-file", str(key_file), extra_env={"ISO_DIST_DIR": str(ROOT / "dist")})
    assert result.returncode != 0 and "which is where releases go" in result.stderr
    result = stubs.run("--test-key-file", str(key_file), extra_env={"ISO_DIST_DIR": str(ROOT / "dist" / ".." / "dist")})
    assert result.returncode != 0


@pytest.mark.parametrize("inside", ["test-only", "test-r18/nested", "a/../b", "./c", "."])
def test_test_key_cannot_use_a_directory_inside_dist_either(stubs, key_file: Path, inside: str) -> None:
    """Whatever publishes dist/ as a tree (tar, rsync, an upload) would ship a test login that sits anywhere below it."""
    # --check stops after the flavour rules and creates nothing, so a missing guard cannot leave directories in dist/
    result = stubs.run("--check", "--test-key-file", str(key_file), extra_env={"ISO_DIST_DIR": str(ROOT / "dist" / inside)})
    assert result.returncode != 0, inside
    assert "dist/ or any directory below it" in result.stderr and "which is where releases go" in result.stderr
    assert not stubs.runs() and not [c for c in stubs.calls() if c[0] == "build"]


def test_a_neighbour_of_dist_is_not_inside_dist(stubs, key_file: Path) -> None:
    """The rule is about the directory tree, not about the spelling: dist-test and a directory whose name only starts with dist are fine."""
    for neighbour in ("dist-test", "distribution", "build/test-dist"):
        result = stubs.run("--check", "--test-key-file", str(key_file), extra_env={"ISO_DIST_DIR": str(ROOT / neighbour)})
        assert result.returncode == 0, (neighbour, result.stderr)
        assert "flavour testkey" in result.stdout


def test_the_messages_name_a_directory_outside_dist_as_the_example(stubs, key_file: Path) -> None:
    result = stubs.run("--test-key-file", str(key_file), extra_env={"ISO_DIST_DIR": ""})
    assert "build/test-dist" in result.stderr and "dist/test-only" not in result.stderr
    assert "outside dist/" in stubs.run("--help").stdout


def test_the_environment_variable_is_a_test_key_too_and_has_the_same_rule(stubs) -> None:
    result = stubs.run(extra_env={"GENTOO_TEST_SSH_PUBLIC_KEY": PUBLIC_KEY, "ISO_DIST_DIR": ""})
    assert result.returncode != 0 and "default dist/" in result.stderr
    stubs.env["STUB_ISO_NAME"] = TEST_NAME
    result = stubs.run(extra_env={"GENTOO_TEST_SSH_PUBLIC_KEY": PUBLIC_KEY})
    assert result.returncode == 0, result.stderr
    assert container_env(stubs.runs()[0])["ISO_FLAVOR"] == "testkey"


def test_test_build_names_and_container_environment(stubs, key_file: Path) -> None:
    stubs.env["STUB_ISO_NAME"] = TEST_NAME
    result = stubs.run("--test-key-file", str(key_file))
    assert result.returncode == 0, result.stderr
    env = container_env(stubs.runs()[0])
    assert env["GENTOO_TEST_SSH_PUBLIC_KEY"] == PUBLIC_KEY and env["ISO_FLAVOR"] == "testkey"
    assert (stubs.tmp / "dist" / TEST_NAME).is_file() and not (stubs.tmp / "dist" / RELEASE_NAME).exists()
    assert "TEST BUILD" in result.stderr


def test_check_reports_the_test_names(stubs, key_file: Path) -> None:
    result = stubs.run("--check", "--test-key-file", str(key_file))
    assert result.returncode == 0
    assert f"flavour testkey -> {TEST_NAME} (volume GENTOO_INSTALLER_TESTKEY)" in result.stdout


def test_release_build_carries_no_key_and_the_plain_names(stubs) -> None:
    stubs.env["STUB_ISO_NAME"] = RELEASE_NAME
    result = stubs.run()
    assert result.returncode == 0
    env = container_env(stubs.runs()[0])
    assert env["GENTOO_TEST_SSH_PUBLIC_KEY"] == "" and env["ISO_FLAVOR"] == "release"
    assert "TEST BUILD" not in result.stderr


def test_manifest_flavour_must_match_the_request(stubs, key_file: Path) -> None:
    """A builder that produced a release-flavoured ISO for a test request (or the reverse) is refused."""
    stubs.env["STUB_ISO_NAME"] = TEST_NAME
    # the stub copies ISO_FLAVOR from the container environment, so lie about the artifact name instead
    result = stubs.run("--test-key-file", str(key_file), extra_env={"STUB_ISO_NAME": RELEASE_NAME})
    assert result.returncode != 0


@pytest.mark.parametrize(
    "content",
    ["-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n",
     "not a key\n", "ecdsa-sha2-nistp256 AAAA key\n", "ssh-ed25519\n", ""],
)
def test_bad_key_files_are_refused(stubs, content: str) -> None:
    path = stubs.tmp / "bad.pub"
    path.write_text(content)
    result = stubs.run("--test-key-file", str(path))
    assert result.returncode != 0 and not stubs.runs()


def test_only_the_first_line_of_the_key_file_is_used(stubs) -> None:
    path = stubs.tmp / "two.pub"
    path.write_text(PUBLIC_KEY + "\n\nssh-rsa AAAAB3NzaC1yc2E other\n")
    stubs.env["STUB_ISO_NAME"] = TEST_NAME
    assert stubs.run("--test-key-file", str(path)).returncode == 0
    assert container_env(stubs.runs()[0])["GENTOO_TEST_SSH_PUBLIC_KEY"] == PUBLIC_KEY


def test_key_file_must_be_a_regular_file(stubs) -> None:
    link = stubs.tmp / "link.pub"
    target = stubs.tmp / "real.pub"
    target.write_text(PUBLIC_KEY + "\n")
    link.symlink_to(target)
    assert stubs.run("--test-key-file", str(link)).returncode != 0
    assert stubs.run("--test-key-file", str(stubs.tmp)).returncode != 0
    assert stubs.run("--test-key-file", str(stubs.tmp / "absent.pub")).returncode != 0
