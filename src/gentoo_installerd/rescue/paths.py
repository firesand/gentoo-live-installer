"""Reading and writing paths inside a foreign root filesystem (review R1, R2).

Everything below a mounted target treats the target's own symlinks as hostile
text. ``Path.resolve()``, ``os.path.realpath()`` and ``PurePosixPath`` string
logic must never be applied to a path taken from a target (an absolute or
``//``-prefixed symlink would land on the live system), so this module is the
only place that resolves such paths:

* ``open_fd`` asks the kernel to resolve with ``openat2(RESOLVE_IN_ROOT |
  RESOLVE_NO_MAGICLINKS)`` relative to the root directory fd and falls back to
  the component walker below when the syscall is unavailable;
* the walker resolves one component at a time with ``dir_fd`` and
  ``O_NOFOLLOW``. Absolute symlink targets restart at the root, ``..`` pops an
  fd stack and can never leave the root, loops are bounded. It also produces
  the canonical in-root path that mount points, ESP files and DNS binds need.

``RootFS`` objects hold a directory fd of the session root, so create a new one
after the root was unmounted and mounted again (the fd refers to the old
mount). Callers pass in-root paths such as ``/etc/os-release`` and never see a
``pathlib`` object of the target.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import os
import secrets
import stat
from collections import deque
from dataclasses import dataclass
from typing import Iterator, Literal

from .errors import RescueError

O_PATH = getattr(os, "O_PATH", 0o10000000)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0o2000000)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0o400000)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0o200000)

SYS_OPENAT2 = 437  # identical on every architecture since Linux 5.6
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_IN_ROOT = 0x10
_OPENAT2_UNAVAILABLE = {errno.ENOSYS, errno.EPERM, errno.EINVAL, errno.EOPNOTSUPP}

MAX_LINKS = 40
DEFAULT_READ_LIMIT = 1024 * 1024
# Search order for programs inside a target: the distribution's own directories first (review R21).
PROGRAM_DIRS = ("usr/sbin", "usr/bin", "sbin", "bin", "usr/local/sbin", "usr/local/bin")

_ELF_MACHINES = {0x3E: "x86_64", 0x03: "i386", 0xB7: "aarch64", 0x28: "arm", 0xF3: "riscv", 0x15: "ppc64", 0x16: "s390x", 0x08: "mips"}


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]


_libc: ctypes.CDLL | None = None


def _syscall_openat2(dirfd: int, path: bytes, flags: int, mode: int, resolve: int) -> int:
    """Raw openat2(2); returns the fd or raises OSError (errno preserved)."""
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
    how = _OpenHow(flags, mode, resolve)
    result = _libc.syscall(ctypes.c_long(SYS_OPENAT2), ctypes.c_long(dirfd), ctypes.c_char_p(path), ctypes.byref(how), ctypes.c_size_t(ctypes.sizeof(how)))
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path.decode("utf-8", "replace"))
    return int(result)


def openat2_supported() -> bool:
    """Whether this kernel resolves paths with RESOLVE_IN_ROOT (Linux 5.6 or newer)."""
    try:
        fd = _syscall_openat2(-100, b"/", os.O_RDONLY | O_DIRECTORY | O_CLOEXEC, 0, RESOLVE_IN_ROOT | RESOLVE_NO_MAGICLINKS)  # AT_FDCWD
    except OSError:
        return False
    os.close(fd)
    return True


def split_components(text: str) -> list[str]:
    """Split a path on '/' ourselves: '//x', '/x' and 'x' are the same, '.' and '' vanish."""
    return [part for part in text.split("/") if part not in ("", ".")]


def _check_text(path: str) -> None:
    if "\x00" in path:
        raise RescueError("bad_request", "Path contains a NUL byte")


@dataclass
class _Hit:
    dir_fd: int  # the directory holding `name` (or the directory itself when name is None)
    name: str | None
    names: tuple[str, ...]  # canonical components that exist
    tail: tuple[str, ...]  # components that do not exist (only when missing_ok)
    st: os.stat_result | None


@dataclass(frozen=True)
class WriteResult:
    path: str
    before_sha256: str
    after_sha256: str
    backup: str = ""
    created: bool = False


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RootFS:
    """Chroot-semantics access to a mounted target root (the only door to target paths)."""

    def __init__(self, root: str | os.PathLike[str], *, writable: bool = False, use_openat2: bool | None = None, max_links: int = MAX_LINKS) -> None:
        # The session root is a path the daemon chose (/mnt/rescue/<sid>), never target text.
        self.root = os.path.realpath(os.fspath(root))  # host-path: daemon-chosen session root
        self.writable = writable
        self.max_links = max_links
        self._use_openat2 = (use_openat2 if use_openat2 is not None else True) and hasattr(ctypes, "CDLL")
        self._force_openat2 = use_openat2 is True
        self._fd = os.open(self.root, O_PATH | O_DIRECTORY | O_CLOEXEC)

    @classmethod
    def _anchored(cls, fd: int, root: str, parent: RootFS) -> RootFS:
        child = cls.__new__(cls)
        child.root = root
        child.writable = parent.writable
        child.max_links = parent.max_links
        child._use_openat2 = parent._use_openat2
        child._force_openat2 = parent._force_openat2
        child._fd = fd
        return child

    def sub(self, path: str) -> RootFS:
        """A RootFS whose root is a directory of this one (resolved in-root, anchored by fd, not by path)."""
        canonical = self.resolve(path)
        fd = self.open_fd(path, O_PATH | O_DIRECTORY)
        return self._anchored(fd, self.root if canonical == "/" else self.root + canonical, self)

    # ---- lifecycle -------------------------------------------------------
    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> RootFS:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - safety net for leaked objects
        with contextlib.suppress(Exception):
            self.close()

    # ---- the walker ------------------------------------------------------
    @contextlib.contextmanager
    def _walked(self, path: str, *, follow_last: bool = True, missing_ok: bool = False) -> Iterator[_Hit]:
        opened: list[int] = []
        try:
            yield self._walk(path, follow_last, missing_ok, opened)
        finally:
            for fd in opened:
                with contextlib.suppress(OSError):
                    os.close(fd)

    def _walk(self, path: str, follow_last: bool, missing_ok: bool, opened: list[int]) -> _Hit:
        _check_text(path)
        pending: deque[str] = deque(split_components(path))
        stack: list[int] = [self._fd]
        names: list[str] = []
        links = 0

        def pop() -> None:
            names.pop()
            fd = stack.pop()
            os.close(fd)
            opened.remove(fd)

        while pending:
            comp = pending.popleft()
            if comp == "..":
                if names:  # at the root '..' stays at the root
                    pop()
                continue
            dir_fd = stack[-1]
            try:
                st = os.stat(comp, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                if missing_ok and ".." not in pending:
                    return _Hit(dir_fd, None, tuple(names), (comp, *pending), None)
                raise
            is_last = not pending
            if stat.S_ISLNK(st.st_mode) and (follow_last or not is_last):
                links += 1
                if links > self.max_links:
                    raise RescueError("symlink_loop", f"Too many symbolic links while resolving {path}", path=path)
                target = os.readlink(comp, dir_fd=dir_fd)
                if not target:
                    raise FileNotFoundError(errno.ENOENT, "empty symbolic link", comp)
                if target.startswith("/"):  # absolute: relative to the target root, never the live one
                    while names:
                        pop()
                pending.extendleft(reversed(split_components(target)))
                continue
            if is_last:
                return _Hit(dir_fd, comp, (*names, comp), (), st)
            if not stat.S_ISDIR(st.st_mode):
                raise NotADirectoryError(errno.ENOTDIR, "not a directory", comp)
            fd = os.open(comp, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC, dir_fd=dir_fd)
            opened.append(fd)
            stack.append(fd)
            names.append(comp)
        return _Hit(stack[-1], None, tuple(names), (), None)

    # ---- queries ---------------------------------------------------------
    def resolve(self, path: str, *, follow_last: bool = True, missing_ok: bool = False) -> str:
        """Canonical in-root path ('/usr/lib/os-release'; '/' for the root itself)."""
        with self._walked(path, follow_last=follow_last, missing_ok=missing_ok) as hit:
            return "/" + "/".join((*hit.names, *hit.tail))

    def host_path(self, path: str, *, missing_ok: bool = False) -> str:
        """The live-system path of a target path, composed from the canonical components only."""
        canonical = self.resolve(path, missing_ok=missing_ok)
        return self.root if canonical == "/" else self.root + canonical

    def stat(self, path: str, *, follow: bool = True) -> os.stat_result:
        with self._walked(path, follow_last=follow) as hit:
            return hit.st if hit.st is not None else os.fstat(hit.dir_fd)

    def lstat(self, path: str) -> os.stat_result:
        return self.stat(path, follow=False)

    def exists(self, path: str, *, follow: bool = True) -> bool:
        try:
            self.stat(path, follow=follow)
        except (OSError, RescueError):
            return False
        return True

    def is_dir(self, path: str) -> bool:
        try:
            return stat.S_ISDIR(self.stat(path).st_mode)
        except (OSError, RescueError):
            return False

    def is_file(self, path: str) -> bool:
        try:
            return stat.S_ISREG(self.stat(path).st_mode)
        except (OSError, RescueError):
            return False

    def is_symlink(self, path: str) -> bool:
        try:
            return stat.S_ISLNK(self.lstat(path).st_mode)
        except (OSError, RescueError):
            return False

    def readlink(self, path: str) -> str:
        """The raw text of a symbolic link (never resolved by the caller)."""
        with self._walked(path, follow_last=False) as hit:
            if hit.name is None or hit.st is None or not stat.S_ISLNK(hit.st.st_mode):
                raise OSError(errno.EINVAL, "not a symbolic link", path)
            return os.readlink(hit.name, dir_fd=hit.dir_fd)

    # ---- opening ---------------------------------------------------------
    def open_fd(self, path: str, flags: int = os.O_RDONLY, *, follow_last: bool = True) -> int:
        """Open a path with chroot semantics; the caller owns the fd."""
        _check_text(path)
        flags |= O_CLOEXEC
        if self._use_openat2:
            try:
                return self._openat2(path, flags if follow_last else flags | O_NOFOLLOW)
            except OSError as exc:
                if exc.errno not in _OPENAT2_UNAVAILABLE or self._force_openat2:
                    raise
                self._use_openat2 = False
        with self._walked(path, follow_last=follow_last) as hit:
            if hit.name is None:
                return os.open(".", flags, dir_fd=hit.dir_fd)
            return os.open(hit.name, flags | O_NOFOLLOW, dir_fd=hit.dir_fd)

    def _openat2(self, path: str, flags: int) -> int:
        encoded = os.fsencode(path) or b"."
        for _attempt in range(8):  # EAGAIN: a rename raced the lookup
            try:
                return _syscall_openat2(self._fd, encoded, flags, 0, RESOLVE_IN_ROOT | RESOLVE_NO_MAGICLINKS)
            except OSError as exc:
                if exc.errno == errno.EAGAIN:
                    continue
                if exc.errno == errno.ELOOP and not flags & O_NOFOLLOW:
                    raise RescueError("symlink_loop", f"Too many symbolic links while resolving {path}", path=path) from exc
                raise
        raise OSError(errno.EAGAIN, "openat2 kept racing", path)

    def read_bytes(self, path: str, limit: int = DEFAULT_READ_LIMIT) -> bytes:
        fd = self.open_fd(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                raise IsADirectoryError(errno.EISDIR, "is a directory", path)
            if not stat.S_ISREG(st.st_mode):
                raise RescueError("not_regular", f"{path} is not a regular file", path=path)
            chunks: list[bytes] = []
            remaining = limit
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 1 << 16))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def tail_bytes(self, path: str, count: int) -> tuple[bytes, int]:
        """(the last ``count`` bytes of a regular file, its size) without reading the rest."""
        fd = self.open_fd(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                raise IsADirectoryError(errno.EISDIR, "is a directory", path)
            if not stat.S_ISREG(st.st_mode):
                raise RescueError("not_regular", f"{path} is not a regular file", path=path)
            start = max(0, st.st_size - count)
            chunks = []
            offset = start
            while offset < st.st_size:
                chunk = os.pread(fd, min(1 << 16, st.st_size - offset), offset)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
            return b"".join(chunks), st.st_size
        finally:
            os.close(fd)

    def read_text(self, path: str, limit: int = DEFAULT_READ_LIMIT) -> str:
        return self.read_bytes(path, limit).decode("utf-8", errors="replace")

    def read_text_or(self, path: str, default: str = "", limit: int = DEFAULT_READ_LIMIT) -> str:
        try:
            return self.read_text(path, limit)
        except (OSError, RescueError):
            return default

    def sha256(self, path: str) -> str:
        return sha256_bytes(self.read_bytes(path, 64 * DEFAULT_READ_LIMIT))

    def listdir(self, path: str) -> list[str]:
        fd = self.open_fd(path, os.O_RDONLY | O_DIRECTORY)
        try:
            return sorted(os.listdir(fd))
        finally:
            os.close(fd)

    def listdir_or(self, path: str) -> list[str]:
        try:
            return self.listdir(path)
        except (OSError, RescueError):
            return []

    def free_bytes(self, path: str = "/") -> int:
        fd = self.open_fd(path, os.O_RDONLY | O_DIRECTORY)
        try:
            info = os.fstatvfs(fd)
        finally:
            os.close(fd)
        return info.f_bavail * info.f_frsize

    def which(self, name: str, dirs: tuple[str, ...] = PROGRAM_DIRS) -> str | None:
        """Canonical in-root path of an executable regular file, searched in the distribution's directories first."""
        if "/" in name or name in ("", ".", ".."):
            raise ValueError("A program name has no slashes")
        for directory in dirs:
            try:
                canonical = self.resolve(f"/{directory}/{name}")
                st = self.stat(canonical)
            except (OSError, RescueError):
                continue
            if stat.S_ISREG(st.st_mode) and st.st_mode & 0o111:
                return canonical
        return None

    def exe_arch(self) -> str:
        """CPU architecture of the target's programs from an ELF header ('' when unknown)."""
        for candidate in ("/usr/bin/env", "/bin/sh", "/usr/bin/sh", "/bin/busybox"):
            try:
                header = self.read_bytes(candidate, 20)
            except (OSError, RescueError):
                continue
            if len(header) >= 20 and header[:4] == b"\x7fELF":
                order: Literal["little", "big"] = "little" if header[5] == 1 else "big"
                return _ELF_MACHINES.get(int.from_bytes(header[18:20], order), "unknown")
        return ""

    # ---- mount points (review R2) ---------------------------------------
    def mount_destination(self, mountpoint: str, reserved: tuple[str, ...] = ("proc", "sys", "dev", "run")) -> str:
        """Live-system path of an existing directory a filesystem may be mounted on.

        The caller must call this immediately before every mount. The target's symlinks are
        followed inside the root; the result is rejected when it is the root's virtual-mount
        area, not a plain directory, or when the live path still contains a symlink.
        """
        canonical = self.resolve(mountpoint)
        parts = split_components(canonical)
        if parts and parts[0] in reserved:
            raise RescueError("path_escape", f"{mountpoint} resolves to /{'/'.join(parts)}, which is reserved for virtual filesystems", mountpoint=mountpoint)
        host = self.root if not parts else self.root + canonical
        info = os.lstat(host)  # host-path: composed from the session root and canonical names
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "mount point is not a directory", mountpoint)
        if os.path.realpath(host) != host:  # host-path: our own composed string, must be symlink free
            raise RescueError("path_escape", f"Mount point {mountpoint} contains a symbolic link", mountpoint=mountpoint)
        return host

    def virtual_destination(self, name: str) -> str:
        """Host path of $ROOT/<name> for a virtual file system (proc, sys, dev, run): a real directory, never a link."""
        info = self.lstat("/" + name)
        if not stat.S_ISDIR(info.st_mode):
            raise RescueError("path_escape", f"/{name} in the target is not a plain directory (it may be a symbolic link)", mountpoint=name)
        return self.root + "/" + name

    def file_destination(self, path: str) -> str:
        """Host path of an existing regular file that a file may be bind-mounted onto (resolv.conf, /proc/cmdline)."""
        canonical = self.resolve(path)
        parts = split_components(canonical)
        host = self.root + canonical
        info = os.lstat(host)  # host-path: composed from the session root and canonical names
        if not stat.S_ISREG(info.st_mode) or not parts:
            raise RescueError("path_escape", f"{path} is not a regular file", mountpoint=path)
        if os.path.realpath(host) != host:  # host-path: our own composed string, must be symlink free
            raise RescueError("path_escape", f"{path} contains a symbolic link", mountpoint=path)
        return host

    # ---- writing (executor only; needs writable=True) ---------------------
    def _need_writable(self) -> None:
        if not self.writable:
            raise RescueError("read_only", "This view of the system is read-only")

    def _dir_for_write(self, hit: _Hit, *, parents: bool, opened: list[int]) -> tuple[int, str]:
        """(directory fd, final name) for creating or replacing an entry."""
        if not hit.tail:
            if hit.name is None:
                raise IsADirectoryError(errno.EISDIR, "path is a directory", "/".join(hit.names))
            return hit.dir_fd, hit.name
        current = hit.dir_fd
        tail = list(hit.tail)
        if len(tail) > 1 and not parents:
            raise FileNotFoundError(errno.ENOENT, "parent directory does not exist", "/".join((*hit.names, *tail)))
        for comp in tail[:-1]:
            os.mkdir(comp, 0o755, dir_fd=current)
            fd = os.open(comp, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC, dir_fd=current)
            opened.append(fd)
            current = fd
        return current, tail[-1]

    def mkdir(self, path: str, mode: int = 0o755, *, parents: bool = False) -> str:
        self._need_writable()
        with self._walked(path, follow_last=True, missing_ok=True) as hit:
            if not hit.tail:
                if hit.name is not None and not stat.S_ISDIR(hit.st.st_mode if hit.st else 0):
                    raise FileExistsError(errno.EEXIST, "exists and is not a directory", path)
                return "/" + "/".join(hit.names)
            opened: list[int] = []
            try:
                dir_fd, name = self._dir_for_write(hit, parents=parents, opened=opened)
                os.mkdir(name, mode, dir_fd=dir_fd)
            finally:
                for fd in opened:
                    os.close(fd)
            return "/" + "/".join((*hit.names, *hit.tail))

    def write_atomic(
        self,
        path: str,
        data: bytes,
        *,
        mode: int | None = None,
        uid: int | None = None,
        gid: int | None = None,
        expect_sha256: str | None = None,
        backup_suffix: str = "",
        create: bool = True,
        parents: bool = False,
    ) -> WriteResult:
        """Replace (or create) a file atomically, keeping owner, mode and xattrs of the old one.

        ``expect_sha256`` is the digest of the content the plan was built from; a different current
        content aborts with ``file_changed`` so edits made meanwhile are never overwritten (review
        R7). The new content is read back and verified.
        """
        self._need_writable()
        with self._walked(path, follow_last=True, missing_ok=True) as hit:
            opened: list[int] = []
            try:
                dir_fd, name = self._dir_for_write(hit, parents=parents, opened=opened)
                canonical = "/" + "/".join((*hit.names, *hit.tail))
                return self._replace(dir_fd, name, canonical, data, mode, uid, gid, expect_sha256, backup_suffix, create)
            finally:
                for fd in opened:
                    os.close(fd)

    def _replace(self, dir_fd: int, name: str, canonical: str, data: bytes, mode: int | None, uid: int | None, gid: int | None, expect: str | None, backup_suffix: str, create: bool) -> WriteResult:
        old_fd = -1
        old: bytes | None = None
        old_stat: os.stat_result | None = None
        try:
            old_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | O_CLOEXEC, dir_fd=dir_fd)
        except FileNotFoundError:
            if not create:
                raise
        if old_fd >= 0:
            try:
                old_stat = os.fstat(old_fd)
                if not stat.S_ISREG(old_stat.st_mode):
                    raise RescueError("not_regular", f"{canonical} is not a regular file", path=canonical)
                old = b"".join(iter(lambda: os.read(old_fd, 1 << 16), b""))
            except BaseException:
                os.close(old_fd)
                raise
        before = sha256_bytes(old) if old is not None else ""
        if expect is not None and expect != before:
            if old_fd >= 0:
                os.close(old_fd)
            raise RescueError("file_changed", f"{canonical} changed after the plan was made; review the plan again", path=canonical, expected=expect, actual=before)
        final_mode = mode if mode is not None else (stat.S_IMODE(old_stat.st_mode) if old_stat else 0o644)
        final_uid = uid if uid is not None else (old_stat.st_uid if old_stat else 0)
        final_gid = gid if gid is not None else (old_stat.st_gid if old_stat else 0)
        tmp = f".{name}.rescue-{os.getpid()}-{secrets.token_hex(4)}"
        backup_name = ""
        tmp_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600, dir_fd=dir_fd)
        try:
            _write_all(tmp_fd, data)
            os.fchmod(tmp_fd, final_mode)
            with contextlib.suppress(PermissionError):  # unprivileged test runs keep their own ids
                os.fchown(tmp_fd, final_uid, final_gid)
            if old_fd >= 0:
                _copy_xattrs(old_fd, tmp_fd)
            os.fsync(tmp_fd)
            if backup_suffix and old is not None:
                backup_name = name + backup_suffix
                bfd = os.open(backup_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600, dir_fd=dir_fd)
                try:
                    _write_all(bfd, old)
                    os.fsync(bfd)
                finally:
                    os.close(bfd)
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dir_fd)
            raise
        finally:
            os.close(tmp_fd)
            if old_fd >= 0:
                os.close(old_fd)
        _fsync_dir(dir_fd)
        check_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | O_CLOEXEC, dir_fd=dir_fd)
        try:
            after = sha256_bytes(b"".join(iter(lambda: os.read(check_fd, 1 << 16), b"")))
        finally:
            os.close(check_fd)
        if after != sha256_bytes(data):
            raise RescueError("command_failed", f"Read-back of {canonical} does not match what was written", path=canonical)
        return WriteResult(canonical, before, after, canonical + backup_suffix if backup_name else "", old is None)

    def copy_in_root(self, src: str, dst: str, *, mode: int | None = None, expect_sha256: str | None = None, backup_suffix: str = "") -> WriteResult:
        """Copy one regular file to another path of the same root."""
        data = self.read_bytes(src, 256 * DEFAULT_READ_LIMIT)
        return self.write_atomic(dst, data, mode=mode, expect_sha256=expect_sha256, backup_suffix=backup_suffix)

    def touch(self, path: str, mode: int = 0o644) -> str:
        self._need_writable()
        if self.exists(path):
            return self.resolve(path)
        return self.write_atomic(path, b"", mode=mode, uid=0, gid=0, create=True).path

    def remove(self, path: str, *, missing_ok: bool = True) -> bool:
        self._need_writable()
        try:
            with self._walked(path, follow_last=False) as hit:
                if hit.name is None:
                    raise IsADirectoryError(errno.EISDIR, "refusing to remove a directory", path)
                if stat.S_ISDIR(hit.st.st_mode if hit.st else 0):
                    raise IsADirectoryError(errno.EISDIR, "refusing to remove a directory", path)
                os.unlink(hit.name, dir_fd=hit.dir_fd)
                return True
        except FileNotFoundError:
            if missing_ok:
                return False
            raise


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _copy_xattrs(src_fd: int, dst_fd: int) -> None:
    try:
        names = os.listxattr(src_fd)
    except OSError:
        return
    for attr in names:
        try:
            os.setxattr(dst_fd, attr, os.getxattr(src_fd, attr))
        except OSError:
            continue


def _fsync_dir(dir_fd: int) -> None:
    fd = os.open(".", os.O_RDONLY | O_DIRECTORY | O_CLOEXEC, dir_fd=dir_fd)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
