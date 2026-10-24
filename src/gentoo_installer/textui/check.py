"""Check one answer against the manifest's own rules.

The manifest knows what a valid hostname, time zone, locale or passphrase is (``validate_sections``).
Instead of copying those rules, a changed value is put into a scratch manifest whose other fields
are valid, and whatever the manifest then complains about belongs to that value.
"""

from __future__ import annotations

from typing import Callable

from ..manifest import InstallManifest

SCRATCH_PASSWORD = "scratch-pass-1"


def scratch_manifest() -> InstallManifest:
    """A manifest that validates cleanly, to be changed in one place."""
    manifest = InstallManifest()
    manifest.storage.device = "/dev/sda"
    manifest.accounts.password = manifest.accounts.root_password = SCRATCH_PASSWORD
    return manifest


def field_errors(section: str, change: Callable[[InstallManifest], None]) -> list[str]:
    """What ``validate_sections()[section]`` says after ``change`` is applied to a clean manifest."""
    manifest = scratch_manifest()
    change(manifest)
    return manifest.validate_sections()[section]
