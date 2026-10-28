"""Fixtures for the VM-harness contract tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_host_time_zone(monkeypatch):
    """A window built here must not pick up the time zone of the developer's computer (the Personalize page reads the live session's)."""
    monkeypatch.setattr("gentoo_installer.pages.accounts.detect_timezone", lambda root="/": "")


@pytest.fixture(autouse=True)
def no_host_memory_reading(monkeypatch):
    """The harness refuses to start a VM while the host lacks free memory (scripts/host_share.py); a contract test must
    not depend on how busy the developer's computer is, so the reading is "unknown", which is never refused."""
    monkeypatch.setattr("host_share.available_mib", lambda meminfo=None: None)
