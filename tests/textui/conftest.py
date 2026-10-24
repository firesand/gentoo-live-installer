"""Fixtures of the console installer tests."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator

import pytest

from gentoo_installer.textui import host as host_module

from tests.textui.support import FakeHost, FakeInstaller


class _NoSubprocess:
    """Stands in for ``subprocess`` inside textui.host: a test that reaches a real program has a bug.

    Its exception classes are its own, so the AssertionError below is not swallowed by ``except subprocess.SubprocessError``."""

    class SubprocessError(Exception):
        pass

    class TimeoutExpired(SubprocessError):
        pass

    @staticmethod
    def run(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a test tried to run a real program: {args}")

    call = run


@pytest.fixture(autouse=True)
def never_run_programs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_module, "subprocess", _NoSubprocess)


@pytest.fixture
def host(tmp_path: Path) -> FakeHost:
    home = tmp_path / "home"
    home.mkdir()
    return FakeHost(home)


@pytest.fixture
def installer(sock_dir: Path) -> Iterator[Callable[..., FakeInstaller]]:
    """Factory for fake installer services; every one is closed at the end of the test."""
    made: list[FakeInstaller] = []

    def make(*args: object, **kwargs: object) -> FakeInstaller:
        fake = FakeInstaller(sock_dir / f"fake-{len(made)}.sock", *args, **kwargs)  # type: ignore[arg-type]
        made.append(fake)
        return fake

    yield make
    for fake in made:
        fake.close()
