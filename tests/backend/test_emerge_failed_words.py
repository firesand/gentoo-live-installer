"""A failed emerge is told in words: what failed and what to do, never Portage's asyncio noise at exit.

The first Wave 6 VM matrix lost its network in the middle of the kernel stage. The install stopped with
"Command failed (1): chroot /mnt/gentoo emerge ...: -222' coro=<BuildLogger._main() done ...": the real cause, Portage's
"!!! Couldn't download 'gnu-efi-4.0.4.tar.gz'" after failed name lookups, was above the last lines of the output.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_installer.manifest import InstallManifest
from gentoo_installerd.configure_packages import EmergeFailed, explain_emerge_failure
from gentoo_installerd.runner import CommandTimeout, RunnerError

from .target_helpers import ScriptedRunner, configurer

REAL = (Path(__file__).parent / "data/emerge-kernel-no-network.txt").read_text()


def test_the_recorded_no_network_output_is_explained() -> None:
    diagnosis = explain_emerge_failure(REAL)
    assert diagnosis.summary.startswith("download failed for gnu-efi-4.0.4.tar.gz (sys-boot/gnu-efi-4.0.4)")
    assert "failed to build" not in diagnosis.summary, "nothing was built: the download failed"
    assert "the internet cannot be reached" in diagnosis.summary
    assert diagnosis.transient and "coro=" not in diagnosis.summary and "Task was destroyed" not in diagnosis.summary


def test_name_lookups_that_fail_without_a_download_line_still_say_no_internet() -> None:
    text = "Resolving distfiles.gentoo.org... failed: Temporary failure in name resolution.\nwget: unable to resolve host address\n"
    diagnosis = explain_emerge_failure(text)
    assert "the internet cannot be reached" in diagnosis.summary and diagnosis.transient


def test_the_old_fetch_line_is_still_read() -> None:
    assert "download failed for app-misc/foo-1.0 (check the network connection)" in explain_emerge_failure("!!! Fetch failed for app-misc/foo-1.0, continuing...\n").summary


def test_emerge_raises_the_words_and_keeps_the_output(tmp_path) -> None:
    runner = ScriptedRunner()
    runner.script("*emerge", ("fail", REAL, 1))
    target = configurer(tmp_path, runner)
    with pytest.raises(EmergeFailed) as raised:
        target.emerge(InstallManifest(), ("--noreplace", "sys-kernel/gentoo-kernel-bin"))
    message = str(raised.value)
    assert message.startswith("Installing packages failed: download failed for gnu-efi-4.0.4.tar.gz")
    assert "check the internet connection, then try again" in message and "coro=" not in message
    assert isinstance(raised.value, RunnerError) and raised.value.output == REAL and raised.value.returncode == 1
    assert raised.value.diagnosis.transient


def test_a_timeout_keeps_its_own_words(tmp_path) -> None:
    runner = ScriptedRunner()
    runner.script("*emerge", CommandTimeout(("emerge",), 60.0, "half a build"))
    with pytest.raises(CommandTimeout):
        configurer(tmp_path, runner).emerge(InstallManifest(), ("--noreplace", "app-misc/foo"))
