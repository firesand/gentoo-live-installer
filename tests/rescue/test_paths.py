"""Hostile-target path handling on real temporary trees (review R1, R2)."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import Path

import pytest

from gentoo_installerd.rescue import paths
from gentoo_installerd.rescue.errors import RescueError
from gentoo_installerd.rescue.paths import RootFS

MODES = [pytest.param(False, id="walker"), pytest.param(True, id="openat2", marks=pytest.mark.skipif(not paths.openat2_supported(), reason="openat2 unavailable"))]


@pytest.fixture(params=MODES)
def use_openat2(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    """(root, outside): a fake target root and a directory that must stay untouched."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "etc").mkdir(parents=True)
    (root / "usr/lib").mkdir(parents=True)
    outside.mkdir()
    (outside / "canary").write_text("LIVE-SYSTEM\n")
    (root / "etc/passwd").write_text("planted-in-root\n")
    (root / "etc/os-release").write_text('ID=gentoo\n')
    return root, outside


def fs_for(root: Path, use_openat2: bool, **kwargs: object) -> RootFS:
    return RootFS(root, use_openat2=use_openat2, **kwargs)  # type: ignore[arg-type]


def test_reads_regular_files_and_lists_directories(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, _ = tree
    with fs_for(root, use_openat2) as fs:
        assert fs.read_text("/etc/os-release") == "ID=gentoo\n"
        assert fs.read_text("etc/os-release") == "ID=gentoo\n"
        assert fs.listdir("/etc") == ["os-release", "passwd"]
        assert fs.is_dir("/usr/lib") and fs.is_file("/etc/passwd")
        assert not fs.exists("/nope")
        assert fs.resolve("/") == "/"
        assert fs.resolve("/etc/./os-release") == "/etc/os-release"


@pytest.mark.parametrize("target", ["//etc/passwd", "///etc/passwd", "/etc/passwd", "/../etc/passwd", "/../../../../etc/passwd", "/./etc//passwd"])
def test_absolute_symlink_text_is_relative_to_the_target_root(tree: tuple[Path, Path], use_openat2: bool, target: str) -> None:
    """A symlink whose text starts with '//' must not land on the live /etc/passwd (review R1)."""
    root, _ = tree
    (root / "evil").symlink_to(target)
    live = Path("/etc/passwd").read_text()
    with fs_for(root, use_openat2) as fs:
        assert fs.read_text("/evil") == "planted-in-root\n"
        assert fs.read_text("/evil") != live
        assert fs.resolve("/evil") == "/etc/passwd"


def test_symlink_to_a_live_file_that_the_root_does_not_have(tree: tuple[Path, Path], use_openat2: bool) -> None:
    """If the target has no such file the answer is ENOENT, never a read of the live file."""
    root, _ = tree
    (root / "etc/passwd").unlink()
    (root / "evil").symlink_to("//etc/passwd")
    with fs_for(root, use_openat2) as fs, pytest.raises(FileNotFoundError):
        fs.read_text("/evil")


def test_dotdot_cannot_leave_the_root(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, outside = tree
    (root / "a").mkdir()
    with fs_for(root, use_openat2) as fs:
        assert fs.read_text("a/../../etc/passwd") == "planted-in-root\n"
        assert fs.read_text("/../../etc/passwd") == "planted-in-root\n"
        assert fs.read_text("/usr/../../../etc/passwd") == "planted-in-root\n"
        assert fs.resolve("/usr/lib/../../../..") == "/"
        with pytest.raises(FileNotFoundError):
            fs.read_text("/../outside/canary")


def test_relative_symlink_climbing_out_is_clamped(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, outside = tree
    (root / "usr/lib/escape").symlink_to("../../../../" + str(outside).lstrip("/") + "/canary")
    (root / "usr/lib/up").symlink_to("../../../../../../../..")
    with fs_for(root, use_openat2) as fs:
        with pytest.raises(FileNotFoundError):
            fs.read_text("/usr/lib/escape")
        assert fs.resolve("/usr/lib/up") == "/"
        assert fs.read_text("/usr/lib/up/etc/os-release") == "ID=gentoo\n"


def test_absolute_path_to_a_live_file_is_not_followed(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, outside = tree
    (root / "link").symlink_to(outside / "canary")  # absolute path of a file outside the root
    with fs_for(root, use_openat2) as fs, pytest.raises(FileNotFoundError):
        fs.read_text("/link")


def test_symlink_chains_and_directory_symlinks(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, _ = tree
    (root / "a").symlink_to("/b")
    (root / "b").symlink_to("/c")
    (root / "c").symlink_to("/etc/os-release")
    (root / "lib64").symlink_to("usr/lib")
    (root / "usr/lib/data").write_text("x")
    (root / "outer").symlink_to("/usr")
    with fs_for(root, use_openat2) as fs:
        assert fs.read_text("/a") == "ID=gentoo\n"
        assert fs.resolve("/a") == "/etc/os-release"
        assert fs.read_text("/lib64/data") == "x"
        assert fs.resolve("/outer/lib/data") == "/usr/lib/data"
        assert fs.resolve("/lib64/../../etc") == "/etc"


def test_symlink_loops_are_bounded(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, _ = tree
    (root / "l1").symlink_to("/l2")
    (root / "l2").symlink_to("l1")
    (root / "self").symlink_to("self")
    with fs_for(root, use_openat2) as fs:
        for name in ("/l1", "/self"):
            with pytest.raises(RescueError) as info:
                fs.read_text(name)
            assert info.value.code == "rescue.symlink_loop"
        with pytest.raises(RescueError):
            fs.resolve("/l1")


def test_nul_bytes_and_empty_symlinks_are_rejected(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, _ = tree
    with fs_for(root, use_openat2) as fs:
        with pytest.raises(RescueError):
            fs.read_text("/etc/pass\x00wd")
        with pytest.raises(RescueError):
            fs.resolve("/etc/pass\x00wd")


def test_final_symlink_not_followed_by_lstat_and_readlink(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, _ = tree
    (root / "link").symlink_to("//etc/passwd")
    with fs_for(root, use_openat2) as fs:
        assert stat.S_ISLNK(fs.lstat("/link").st_mode)
        assert stat.S_ISREG(fs.stat("/link").st_mode)
        assert fs.readlink("/link") == "//etc/passwd"
        assert fs.is_symlink("/link") and not fs.is_symlink("/etc/passwd")
        with pytest.raises(OSError):
            fs.readlink("/etc/passwd")


def test_resolve_of_missing_paths_keeps_the_target_semantics(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, _ = tree
    (root / "etc/resolv.conf").symlink_to("../run/systemd/resolve/stub-resolv.conf")
    with fs_for(root, use_openat2) as fs:
        assert fs.resolve("/etc/resolv.conf", missing_ok=True) == "/run/systemd/resolve/stub-resolv.conf"
        with pytest.raises(FileNotFoundError):
            fs.resolve("/etc/resolv.conf")
        with pytest.raises(FileNotFoundError):
            fs.resolve("/missing/../x", missing_ok=True)


def test_not_regular_files_are_refused_without_blocking(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, _ = tree
    os.mkfifo(root / "etc/pipe")
    with fs_for(root, use_openat2) as fs:
        with pytest.raises(RescueError) as info:
            fs.read_text("/etc/pipe")
        assert info.value.code == "rescue.not_regular"
        with pytest.raises(IsADirectoryError):
            fs.read_text("/etc")


def test_read_limit(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, _ = tree
    (root / "big").write_bytes(b"a" * 5000)
    with fs_for(root, use_openat2) as fs:
        assert len(fs.read_bytes("/big", 100)) == 100
        assert len(fs.read_bytes("/big")) == 5000


def test_which_prefers_distribution_directories(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, _ = tree
    for directory in ("usr/bin", "usr/local/sbin", "usr/sbin"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "usr/local/sbin/grub-install").write_text("trojan")
    (root / "usr/local/sbin/grub-install").chmod(0o755)
    (root / "usr/sbin/grub-install").write_text("real")
    (root / "usr/sbin/grub-install").chmod(0o755)
    (root / "usr/bin/plain").write_text("not executable")
    (root / "usr/bin/plain").chmod(0o644)
    (root / "sbin").symlink_to("usr/sbin")  # merged-usr layout
    with fs_for(root, use_openat2) as fs:
        assert fs.which("grub-install") == "/usr/sbin/grub-install"
        assert fs.which("plain") is None
        assert fs.which("absent") is None
        with pytest.raises(ValueError):
            fs.which("../bin/sh")


def test_which_follows_target_symlinks_inside_the_root(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, _ = tree
    (root / "usr/bin").mkdir(parents=True, exist_ok=True)
    (root / "usr/bin/real").write_text("x")
    (root / "usr/bin/real").chmod(0o755)
    (root / "usr/bin/alias").symlink_to("/usr/bin/real")
    with fs_for(root, use_openat2) as fs:
        assert fs.which("alias") == "/usr/bin/real"


def test_exe_arch_reads_the_elf_header(tree: tuple[Path, Path], use_openat2: bool) -> None:
    root, _ = tree
    (root / "usr/bin").mkdir(parents=True, exist_ok=True)

    def elf(machine: int) -> bytes:
        header = bytearray(64)
        header[:4] = b"\x7fELF"
        header[4] = 2
        header[5] = 1
        header[18:20] = machine.to_bytes(2, "little")
        return bytes(header)

    with fs_for(root, use_openat2) as fs:
        assert fs.exe_arch() == ""
        (root / "usr/bin/env").write_bytes(elf(0x3E))
        assert fs.exe_arch() == "x86_64"
        (root / "usr/bin/env").write_bytes(elf(0xB7))
        assert fs.exe_arch() == "aarch64"
        (root / "usr/bin/env").write_bytes(b"#!/bin/sh\n")
        assert fs.exe_arch() == ""


def test_openat2_failure_falls_back_to_the_walker(tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = tree

    def boom(*_args: object, **_kwargs: object) -> int:
        raise OSError(errno.ENOSYS, "no openat2")

    monkeypatch.setattr(paths, "_syscall_openat2", boom)
    with RootFS(root) as fs:
        assert fs.read_text("/etc/os-release") == "ID=gentoo\n"
        assert fs._use_openat2 is False


@pytest.mark.skipif(not paths.openat2_supported(), reason="openat2 unavailable")
def test_openat2_eagain_is_retried(tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = tree
    real = paths._syscall_openat2
    calls = {"n": 0}

    def flaky(*args: object, **kwargs: object) -> int:
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(errno.EAGAIN, "raced")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(paths, "_syscall_openat2", flaky)
    with RootFS(root, use_openat2=True) as fs:
        assert fs.read_text("/etc/os-release") == "ID=gentoo\n"
    assert calls["n"] == 3


# ---- mount points (review R2) -------------------------------------------------------------
def test_mount_destination_accepts_plain_directories(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    (root / "home").mkdir()
    (root / "boot/efi").mkdir(parents=True)
    with RootFS(root) as fs:
        assert fs.mount_destination("/home") == str(root.resolve()) + "/home"
        assert fs.mount_destination("/boot/efi") == str(root.resolve()) + "/boot/efi"
        assert fs.mount_destination("/") == str(root.resolve())


def test_mount_destination_follows_in_root_links_only(tree: tuple[Path, Path]) -> None:
    root, outside = tree
    (root / "srv").mkdir()
    (root / "data").symlink_to("/srv")  # fine: inside the root
    (root / "hostile").symlink_to(str(outside))  # absolute path of a live directory -> not in the root
    with RootFS(root) as fs:
        assert fs.mount_destination("/data") == str(root.resolve()) + "/srv"
        with pytest.raises(FileNotFoundError):
            fs.mount_destination("/hostile")  # would otherwise mount over the live directory


def test_mount_destination_dotdot_cannot_reach_live_mnt(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    with RootFS(root) as fs:
        with pytest.raises(FileNotFoundError):
            fs.mount_destination("/../../../../mnt")
        assert fs.resolve("/etc/../../../..") == "/"


@pytest.mark.parametrize("name", ["proc", "sys", "dev", "run"])
def test_mount_destination_rejects_virtual_filesystem_areas(tree: tuple[Path, Path], name: str) -> None:
    root, _ = tree
    (root / name / "sub").mkdir(parents=True)
    (root / "var").mkdir()
    (root / "var/alias").symlink_to(f"/{name}")
    with RootFS(root) as fs:
        for point in (f"/{name}", f"/{name}/sub", "/var/alias"):
            with pytest.raises(RescueError) as info:
                fs.mount_destination(point)
            assert info.value.code == "rescue.path_escape"


def test_mount_destination_rejects_files(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    with RootFS(root) as fs, pytest.raises(NotADirectoryError):
        fs.mount_destination("/etc/passwd")


# ---- writes ------------------------------------------------------------------------------
def test_write_atomic_keeps_mode_makes_a_backup_and_checks_the_digest(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    target = root / "etc/fstab"
    target.write_text("old\n")
    target.chmod(0o640)
    old_digest = hashlib.sha256(b"old\n").hexdigest()
    with RootFS(root, writable=True) as fs:
        result = fs.write_atomic("/etc/fstab", b"new\n", expect_sha256=old_digest, backup_suffix=".gentoo-rescue-20261024T000000Z")
    assert target.read_text() == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    backup = root / "etc/fstab.gentoo-rescue-20261024T000000Z"
    assert backup.read_text() == "old\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert result.before_sha256 == old_digest and result.after_sha256 == hashlib.sha256(b"new\n").hexdigest()
    assert result.backup == "/etc/fstab.gentoo-rescue-20261024T000000Z"
    assert not [name for name in os.listdir(root / "etc") if name.startswith(".fstab.rescue")]


def test_write_atomic_refuses_when_the_file_changed_since_the_plan(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    target = root / "etc/fstab"
    target.write_text("edited in the terminal\n")
    with RootFS(root, writable=True) as fs, pytest.raises(RescueError) as info:
        fs.write_atomic("/etc/fstab", b"new\n", expect_sha256=hashlib.sha256(b"old\n").hexdigest())
    assert info.value.code == "rescue.file_changed"
    assert target.read_text() == "edited in the terminal\n"


def test_write_atomic_never_follows_a_symlink_out_of_the_root(tree: tuple[Path, Path]) -> None:
    root, outside = tree
    (root / "etc/fstab").symlink_to(outside / "canary")
    with RootFS(root, writable=True) as fs, pytest.raises(FileNotFoundError):
        fs.write_atomic("/etc/fstab", b"pwned\n")
    assert (outside / "canary").read_text() == "LIVE-SYSTEM\n"
    # a '//' link resolves inside the root and replaces the in-root file instead
    (root / "etc/fstab").unlink()
    (root / "etc/fstab").symlink_to("//etc/hosts")
    (root / "etc/hosts").write_text("127.0.0.1 x\n")
    with RootFS(root, writable=True) as fs:
        fs.write_atomic("/etc/fstab", b"through-link\n")
    assert (root / "etc/hosts").read_text() == "through-link\n"
    assert (root / "etc/fstab").is_symlink()


def test_write_atomic_creates_files_and_parents_only_when_asked(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    with RootFS(root, writable=True) as fs:
        result = fs.write_atomic("/etc/kernel/cmdline", b"root=UUID=x\n", mode=0o644, parents=True)
        assert result.created and result.before_sha256 == ""
        with pytest.raises(FileNotFoundError):
            fs.write_atomic("/etc/deeper/than/that", b"x")
        with pytest.raises(FileNotFoundError):
            fs.write_atomic("/etc/nothere", b"x", create=False)
    assert (root / "etc/kernel/cmdline").read_text() == "root=UUID=x\n"


def test_read_only_view_cannot_write(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    with RootFS(root) as fs:
        for call in (lambda: fs.write_atomic("/x", b""), lambda: fs.mkdir("/x"), lambda: fs.remove("/etc/passwd"), lambda: fs.touch("/x")):
            with pytest.raises(RescueError) as info:
                call()
            assert info.value.code == "rescue.read_only"


def test_remove_and_touch_and_mkdir(tree: tuple[Path, Path]) -> None:
    root, outside = tree
    (root / "link").symlink_to(outside / "canary")
    with RootFS(root, writable=True) as fs:
        assert fs.remove("/link")  # removes the link itself, not what it points at
        assert (outside / "canary").exists()
        assert not fs.remove("/link")
        with pytest.raises(IsADirectoryError):
            fs.remove("/etc")
        fs.touch("/.autorelabel")
        assert (root / ".autorelabel").exists()
        assert fs.mkdir("/new/deep", parents=True) == "/new/deep"
        assert (root / "new/deep").is_dir()
        with pytest.raises(FileExistsError):
            fs.mkdir("/etc/passwd")


def test_free_bytes_and_sha(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    with RootFS(root) as fs:
        assert fs.free_bytes("/") > 0
        assert fs.sha256("/etc/os-release") == hashlib.sha256(b"ID=gentoo\n").hexdigest()
