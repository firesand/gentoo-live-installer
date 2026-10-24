"""I7: the Gentoo tree is accepted only when its commit carries the pinned developer's signature."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from .conftest import ROOT

VERIFY = ROOT / "iso/verify-snapshot.sh"


def test_pins_name_the_commit_and_its_signer(pins: dict[str, str]) -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", pins["GENTOO_TREEISH"])
    assert re.fullmatch(r"[0-9A-F]{40}", pins["GENTOO_TREEISH_SIGNER_FPR"])
    assert re.fullmatch(r"[A-Z0-9_]{1,32}", pins["ISO_VOLID"])
    assert pins["ISO_NAME"].endswith(".iso")


def test_containerfile_verifies_the_tree_and_has_no_download_pipeline() -> None:
    text = (ROOT / "iso/Containerfile").read_text()
    assert "sec-keys/openpgp-keys-gentoo-developers" in text and "sec-keys/openpgp-keys-gentoo-auth" in text
    assert not re.search(r"curl[^\n]*\\\n(?:[^\n]*\\\n)*[^\n]*\|\s*tar", text), "no curl-to-tar pipeline"
    assert "codeload" in text and "-o /tmp/bootstrap/gentoo.tar.gz" in text, "the bootstrap download goes to a file"
    verify = text.index("verify-gentoo-snapshot /tmp/gentoo.git")
    archive = text.index("git -C /tmp/gentoo.git archive")
    assert text.index("emerge") < verify < archive, "the tree is replaced only after the signature was checked"
    assert "ARG GENTOO_TREEISH_SIGNER_FPR" in text and "dev-libs/libisoburn" in text
    assert "COPY verify-snapshot.sh" in text


def test_every_build_verifies_the_signature_before_catalyst_uses_the_tree() -> None:
    text = (ROOT / "iso/builder-entrypoint.sh").read_text()
    function = text[text.index("prepare_snapshot() {"):text.index("# write_catalyst_config")]
    verify = function.index("verify-snapshot.sh")
    assert function.index("rev-parse") < verify < function.index("git -C \"${repo}\" archive") < function.index("catalyst -c")
    assert '"${GENTOO_TREEISH_SIGNER_FPR}"' in function


# --- the verifier with throwaway keys ------------------------------------------------------------

needs_tools = pytest.mark.skipif(shutil.which("gpg") is None or shutil.which("git") is None, reason="gpg and git are needed")


@pytest.fixture()
def signed_repo(tmp_path: Path):
    home = tmp_path / "g"
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

    def run(*argv: str, cwd: Path | None = None, check: bool = True):
        return subprocess.run(argv, capture_output=True, text=True, env=env, cwd=cwd, check=check)

    run("gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "", "--quick-generate-key", "Test Signer <t@example.org>", "ed25519", "sign", "never")
    listing = run("gpg", "--list-keys", "--with-colons").stdout
    fingerprint = next(l.split(":")[9] for l in listing.splitlines() if l.startswith("fpr:"))
    keyring = tmp_path / "keys.asc"
    keyring.write_text(run("gpg", "--armor", "--export").stdout)
    repo = tmp_path / "repo"
    run("git", "init", "-q", str(repo))
    for key, value in (("user.email", "t@example.org"), ("user.name", "T"), ("user.signingkey", fingerprint)):
        run("git", "config", key, value, cwd=repo)
    (repo / "f").write_text("a")
    run("git", "add", "f", cwd=repo)
    run("git", "commit", "-q", "-S", "-m", "signed", cwd=repo)
    signed = run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
    (repo / "f").write_text("b")
    run("git", "commit", "-q", "-a", "-m", "unsigned", cwd=repo)
    unsigned = run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()

    def verify(commit: str, signer: str, result: Path | None = None, keys: Path | str | None = None):
        argv = [str(VERIFY), str(repo), commit, signer] + ([str(result)] if result else [])
        environment = {**os.environ, "GENTOO_OPENPGP_KEYS": str(keys or keyring), "GIT_CONFIG_GLOBAL": "/dev/null"}
        environment.pop("GNUPGHOME", None)
        return subprocess.run(argv, capture_output=True, text=True, env=environment, check=False)

    yield type("Repo", (), {"verify": staticmethod(verify), "signed": signed, "unsigned": unsigned, "fingerprint": fingerprint, "repo": repo, "run": staticmethod(run)})
    subprocess.run(["gpgconf", "--kill", "all"], env=env, capture_output=True, check=False)


@needs_tools
def test_a_commit_signed_by_the_pinned_key_is_accepted(signed_repo, tmp_path: Path) -> None:
    result = signed_repo.verify(signed_repo.signed, signed_repo.fingerprint, tmp_path / "result.json")
    assert result.returncode == 0, result.stderr
    import json

    data = json.loads((tmp_path / "result.json").read_text())
    assert data["verified"] is True and data["commit"] == signed_repo.signed and data["signer_fpr"] == signed_repo.fingerprint


@needs_tools
def test_the_fingerprint_may_be_written_in_lower_case_with_spaces(signed_repo) -> None:
    spaced = " ".join(signed_repo.fingerprint[i:i + 4] for i in range(0, 40, 4)).lower()
    assert signed_repo.verify(signed_repo.signed, spaced).returncode == 0


@needs_tools
def test_a_commit_signed_by_somebody_else_is_refused(signed_repo) -> None:
    result = signed_repo.verify(signed_repo.signed, "EE7058CE6C1EFBC45BB8874DFBE5925C5B02CE58")
    assert result.returncode != 0 and "not by the pinned" in result.stderr


@needs_tools
def test_an_unsigned_commit_is_refused(signed_repo) -> None:
    result = signed_repo.verify(signed_repo.unsigned, signed_repo.fingerprint)
    assert result.returncode != 0 and "missing or invalid" in result.stderr


@needs_tools
def test_a_tampered_commit_is_refused(signed_repo) -> None:
    """Rewriting the content of a signed commit invalidates its signature."""
    run, repo = signed_repo.run, signed_repo.repo
    raw = run("git", "cat-file", "commit", signed_repo.signed, cwd=repo).stdout
    forged = raw.replace("signed\n", "forged message\n", 1)
    assert forged != raw
    result = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin", "--literally"], input=forged, capture_output=True, text=True, cwd=repo, check=False)
    assert result.returncode == 0, result.stderr
    assert signed_repo.verify(result.stdout.strip(), signed_repo.fingerprint).returncode != 0


@needs_tools
def test_unknown_signing_key_is_refused(signed_repo, tmp_path: Path) -> None:
    empty = tmp_path / "other.asc"
    empty.write_text("")
    result = signed_repo.verify(signed_repo.signed, signed_repo.fingerprint, keys=empty)
    assert result.returncode != 0


@pytest.mark.parametrize(
    "arguments",
    [["repo", "short", "A" * 40], ["repo", "0" * 40, "XYZ"], ["repo", "0" * 40], ["/nonexistent", "0" * 40, "A" * 40]],
)
def test_bad_arguments_are_refused(arguments: list[str]) -> None:
    result = subprocess.run([str(VERIFY), *arguments], capture_output=True, text=True, check=False)
    assert result.returncode != 0 and result.stderr.startswith("verify-snapshot:")
