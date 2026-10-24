"""Wave 3 review fixes: secrets that cannot be written to a command's input are refused before any disk is touched (SURR-1)."""

from __future__ import annotations

import json

import pytest

from gentoo_installer.manifest import InstallManifest

LONE_SURROGATE = "caf\udce9-pass-1"  # what a terminal in another character set leaves behind under LANG=C (surrogateescape)


def valid() -> InstallManifest:
    manifest = InstallManifest()
    manifest.storage.device = "/dev/vda"
    manifest.accounts.password = manifest.accounts.root_password = "correct-horse-1"
    return manifest


def test_the_fixture_manifest_is_valid() -> None:
    assert valid().validate() == []


@pytest.mark.parametrize("field", ["password", "root_password"])
def test_an_account_password_that_cannot_be_encoded_is_refused(field: str) -> None:
    manifest = valid()
    setattr(manifest.accounts, field, LONE_SURROGATE)
    problems = manifest.validate_sections()["accounts"]
    assert any("cannot be stored" in problem for problem in problems), problems
    assert any(("user password" if field == "password" else "root password") in problem for problem in problems if "cannot be stored" in problem)


def test_a_luks_passphrase_that_cannot_be_encoded_is_refused() -> None:
    manifest = valid()
    manifest.storage.luks = True
    manifest.storage.luks_passphrase = LONE_SURROGATE
    problems = manifest.validate_sections()["storage"]
    assert any("LUKS passphrase" in problem and "cannot be stored" in problem for problem in problems), problems


def test_the_root_password_is_not_checked_when_root_login_is_off() -> None:
    manifest = valid()
    manifest.accounts.disable_root_login = True
    manifest.accounts.root_password = ""
    assert manifest.validate() == []


def test_a_secret_with_real_non_ascii_letters_is_fine() -> None:
    manifest = valid()
    manifest.accounts.password = "pässwörd-日本-1"
    manifest.storage.luks, manifest.storage.luks_passphrase = True, "Übergrößenträger"
    assert manifest.validate() == []


def test_the_refusal_comes_before_the_json_reaches_the_daemon() -> None:
    """json.dumps escapes the surrogate, so the daemon would receive it intact: only the manifest rule stops it."""
    manifest = valid()
    manifest.accounts.password = LONE_SURROGATE
    assert json.loads(json.dumps(manifest.to_dict()))["accounts"]["password"] == LONE_SURROGATE
    assert manifest.validate()
