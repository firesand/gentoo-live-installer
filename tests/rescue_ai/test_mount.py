"""gentoo-rescue-mount over the rescue engine's lab (fake disks): read-only first, /mnt/gentoo link, encrypted disks, unmount."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from gentoo_installerd.rescue import cli
from gentoo_installerd.rescue.api import RescueApi
from gentoo_installerd.rescue.client import LocalTransport

from gentoo_rescue_ai import mount

from tests.rescue.conftest import lab_factory  # noqa: F401 - the fixture of the engine's own tests
from tests.rescue.flow import uefi_gentoo_lab
from tests.rescue.test_flow_encrypted import PASSPHRASE, encrypted_lab


def run_mount(lab, tmp_path: Path, *argv: str, answers: list[str] | None = None, secrets: list[str] | None = None, interactive: bool = True):
    out, err = io.StringIO(), io.StringIO()
    scripted, hidden = list(answers or []), list(secrets or [])
    console = cli.Console(out=out, err=err, ask=lambda _p: scripted.pop(0) if scripted else "", secret=lambda _p: hidden.pop(0) if hidden else "", interactive=interactive)
    manager = lab.manager(auto_adopt=True)
    link = tmp_path / "mnt" / "gentoo"
    code = mount.main(list(argv), transport=LocalTransport(RescueApi(manager)), console=console, link=str(link), base_dir=str(lab.session_env().base_dir))
    return code, out.getvalue(), err.getvalue(), link


def test_it_mounts_the_one_gentoo_system_read_only_and_links_it_at_mnt_gentoo(lab_factory, tmp_path) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    code, out, err, link = run_mount(lab, tmp_path, answers=["y"])
    assert code == 0, err
    assert "read-only" in out and "session r" in out
    assert link.is_symlink() and os.path.realpath(link) == os.path.realpath(lab.session_env().base_dir) + "/" + os.path.basename(os.readlink(link))
    assert "gentoo-chroot r" in out and "gentoo-rescue-mount --unmount" in out and f"ls {link}/etc" in out
    # nothing was written to the target: no write mount command was issued
    assert not lab.system.commands_with("mount", "rw") and not lab.system.commands_with("mount", "remount")


def test_without_a_yes_nothing_is_mounted_and_the_link_is_not_made(lab_factory, tmp_path) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    code, out, err, link = run_mount(lab, tmp_path, answers=["n"])
    assert not link.exists() and not link.is_symlink()


def test_the_other_systems_are_never_offered(lab_factory, tmp_path) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)  # the lab also holds a Debian
    _, out, _, _ = run_mount(lab, tmp_path, answers=["n"])
    assert "Not a Gentoo system" in out and "Debian" in out
    assert out.count("[c") == 1  # one id: the Gentoo


def test_an_encrypted_system_asks_for_the_passphrase_here_unlocks_activates_and_mounts(lab_factory, tmp_path) -> None:
    lab, _tree, _boot = encrypted_lab(lab_factory)
    code, out, err, link = run_mount(lab, tmp_path, answers=["y", "y"], secrets=[PASSPHRASE])
    assert code == 0, err
    assert "Unlocked as" in out and link.is_symlink()
    assert PASSPHRASE not in out + err


def test_a_wrong_passphrase_stops_with_a_message_and_mounts_nothing(lab_factory, tmp_path) -> None:
    lab, _tree, _boot = encrypted_lab(lab_factory)
    code, out, err, link = run_mount(lab, tmp_path, answers=["y"], secrets=["wrong one"])
    assert not link.exists() and "No Gentoo system was opened" in out
    assert "wrong one" not in out + err


def test_unmount_releases_everything_and_removes_the_link(lab_factory, tmp_path) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    code, _, _, link = run_mount(lab, tmp_path, answers=["y"])
    assert code == 0 and link.is_symlink()
    code, out, err, link = run_mount(lab, tmp_path, "--unmount")
    assert code == 0, err
    assert not link.is_symlink() and "Unmounted" in out


def test_running_it_twice_reuses_the_open_system(lab_factory, tmp_path) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    run_mount(lab, tmp_path, answers=["y"])
    code, out, _, link = run_mount(lab, tmp_path)
    assert code == 0 and "already open" in out and link.is_symlink()


def test_a_real_folder_with_content_at_the_link_path_is_never_removed(tmp_path) -> None:
    link = tmp_path / "gentoo"
    link.mkdir()
    (link / "precious").write_text("x")
    problem = mount.make_link(str(link), "/mnt/rescue/r1")
    assert "not a link or an empty folder" in problem or "could not create" in problem
    assert (link / "precious").exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert mount.make_link(str(empty), "/mnt/rescue/r1") == "" and empty.is_symlink()


def test_it_refuses_to_run_without_root_and_names_the_reason(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert mount.main([]) == cli.EXIT_FAILED
    assert "never goes to an agent" in capsys.readouterr().err


def test_writable_goes_through_the_engines_own_plan_and_confirmation(lab_factory, tmp_path) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    # No typed confirmation is given, so the engine refuses read-write and the system stays read-only.
    code, out, err, link = run_mount(lab, tmp_path, "--writable", answers=["y", "", "", ""])
    assert link.is_symlink() and "(read-only)" in out
    assert not lab.system.commands_with("mount", "remount")


def test_the_base_folder_is_made_walkable_for_the_agents(lab_factory, tmp_path) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    base = Path(lab.session_env().base_dir)
    base.mkdir(parents=True, exist_ok=True)
    base.chmod(0o700)
    code, *_ = run_mount(lab, tmp_path, answers=["y"])
    assert code == 0 and base.stat().st_mode & 0o005 == 0o005
