"""Fixtures for the VM-harness contract tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_host_time_zone(monkeypatch):
    """A window built here must not pick up the time zone of the developer's computer (the Personalize page reads the live session's)."""
    monkeypatch.setattr("gentoo_installer.pages.accounts.detect_timezone", lambda root="/": "")
