"""Read-only log access (review R9, R12): files only, a fixed catalogue, no program of the system is run by default.

``logs.tail`` resolves the requested path inside the system and only serves files of the catalogue below /var/log (and
their rotated or compressed copies). Authentication databases and keys are never served. The journal is binary: its raw
files are listed here, and the system's own ``journalctl`` is only run after an explicit per-query confirmation.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import re
import zlib
from typing import Any

from .errors import RescueError
from .hostenv import Host
from .paths import RootFS
from .runner import RescueRunner
from .view import SessionView

CATALOGUE = (
    "messages", "syslog", "kern.log", "dmesg", "boot.log", "boot", "rc.log", "emerge.log", "pacman.log", "dpkg.log", "apt/history.log", "apt/term.log", "dnf.log", "dnf.rpm.log",
    "dnf.librepo.log", "yum.log", "zypp/history", "Xorg.0.log", "auth.log", "secure", "daemon.log", "cloud-init.log", "portage/elog/summary.log", "user.log", "errors.log",
)
DENIED_NAMES = re.compile(r"(?:^|/)(?:shadow|gshadow|passwd|btmp|wtmp|lastlog|faillog|.*_key|.*\.key|.*\.pem)(?:$|[.-])")
ROTATED_RE = re.compile(r"^(?P<base>.+?)(?:[.-](?:\d+|\d{8}))?(?:\.(?:gz|xz|bz2|zst))?$")
MAX_TAIL_BYTES = 256 * 1024
MAX_DECOMPRESSED = 8 * 1024 * 1024
JOURNAL_DIRS = ("/var/log/journal", "/run/log/journal")


def _canonical_log(fs: RootFS, path: str) -> str:
    """The in-root canonical path of a requested log, or a RescueError when it is not a catalogued log."""
    try:
        canonical = fs.resolve(path)
    except (OSError, RescueError) as exc:
        raise RescueError("not_found", f"{path} was not found in the system", path=path) from exc
    if not canonical.startswith("/var/log/") or DENIED_NAMES.search(canonical):
        raise RescueError("safety_violation", f"{path} is not one of the logs rescue shows", path=path)
    relative = canonical[len("/var/log/"):]
    match = ROTATED_RE.match(relative)
    base = match.group("base") if match else relative
    if relative not in CATALOGUE and base not in CATALOGUE and not any(relative.startswith(prefix) for prefix in ("journal/",)):
        raise RescueError("safety_violation", f"{path} is not one of the logs rescue shows", path=path)
    return canonical


def list_sources(view: SessionView) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    with RootFS(view._session.root) as fs:
        for name in CATALOGUE:
            directory, _, leaf = name.rpartition("/")
            folder = "/var/log" + (f"/{directory}" if directory else "")
            for entry in fs.listdir_or(folder):
                if entry == leaf or (entry.startswith(leaf + ".") or entry.startswith(leaf + "-")):
                    path = f"{folder}/{entry}"
                    try:
                        info = fs.stat(path)
                    except (OSError, RescueError):
                        continue
                    files.append({"path": path, "size": info.st_size, "mtime": int(info.st_mtime), "compressed": path.endswith((".gz", ".xz", ".bz2", ".zst"))})
        journal = []
        for directory in JOURNAL_DIRS:
            for machine in fs.listdir_or(directory):
                entries = [n for n in fs.listdir_or(f"{directory}/{machine}") if n.endswith((".journal", ".journal~"))]
                if entries:
                    journal.append({"path": f"{directory}/{machine}", "files": len(entries)})
    return {"files": sorted(files, key=lambda item: item["path"]), "journal": journal, "persistent_journal": any(item["path"].startswith("/var/") for item in journal)}


def _decompress(path: str, data: bytes) -> bytes:
    if path.endswith(".gz"):
        engine: Any = zlib.decompressobj(31)
    elif path.endswith(".xz"):
        engine = lzma.LZMADecompressor()
    elif path.endswith(".bz2"):
        engine = bz2.BZ2Decompressor()
    elif path.endswith(".zst"):
        try:
            from compression import zstd  # Python 3.14

            engine = zstd.ZstdDecompressor()
        except ImportError as exc:
            raise RescueError("tool_missing", "Reading .zst logs needs Python 3.14 or the zstd program") from exc
    else:
        return data
    try:
        out = engine.decompress(data, MAX_DECOMPRESSED)
    except (OSError, EOFError, lzma.LZMAError, zlib.error, ValueError) as exc:
        raise RescueError("command_failed", f"{path} could not be decompressed: {exc}") from exc
    return out


def tail(view: SessionView, path: str, lines: int = 200, offset: int = 0) -> dict[str, Any]:
    """The last ``lines`` lines (skipping ``offset`` lines from the end) of a catalogued log."""
    lines = max(1, min(int(lines), 2000))
    with RootFS(view._session.root) as fs:
        canonical = _canonical_log(fs, path)
        if canonical.endswith((".gz", ".xz", ".bz2", ".zst")):
            raw, size = fs.tail_bytes(canonical, 32 * 1024 * 1024)
            text = _decompress(canonical, raw).decode("utf-8", "replace")
        else:
            raw, size = fs.tail_bytes(canonical, MAX_TAIL_BYTES * 4)
            text = raw.decode("utf-8", "replace")
    all_lines = text.splitlines()
    end = len(all_lines) - max(0, int(offset))
    chunk = all_lines[max(0, end - lines):end]
    body = "\n".join(chunk)
    return {"path": canonical, "lines": chunk, "text": body[-MAX_TAIL_BYTES:], "size": size, "more": end - lines > 0, "next_offset": offset + len(chunk)}


def pstore(host: Host) -> dict[str, Any]:
    """Kernel crash records the live kernel preserved from the previous boot (read-only, never deleted)."""
    entries = []
    base = host.path("/sys/fs/pstore")
    if base.is_dir():
        for item in sorted(base.iterdir()):
            try:
                if item.is_file():
                    entries.append({"name": item.name, "size": item.stat().st_size, "text": item.read_text(errors="replace")[-16384:]})
            except OSError:
                continue
    return {"entries": entries, "note": "Records of a kernel crash survive only when the firmware or ramoops preserved them."}


def live_dmesg(runner: RescueRunner, lines: int = 300) -> dict[str, Any]:
    result = runner.execute(("dmesg", "--level=err,crit,alert,emerg,warn"), timeout=20)
    text = result.stdout.splitlines()[-lines:]
    return {"lines": text, "note": "These are the messages of this live session (this hardware), not of the system that failed to boot."}


def journal(view: SessionView, runner: RescueRunner, *, confirm_exec: bool, boot: int = 0, priority: str = "", kernel: bool = False, lines: int = 200) -> dict[str, Any]:
    """Run the system's own journalctl as root after an explicit confirmation (the live image has none; review R9)."""
    if not confirm_exec:
        raise RescueError("needs_confirmation", "Reading the journal runs the system's own journalctl program as root. Confirm that you trust this system.", missing="confirm_exec")
    if not view.can_run_programs:
        raise RescueError("needs_write", "Programs of this system cannot run here (inspection-only session, other architecture or image-based system)")
    if not view.which("journalctl"):
        raise RescueError("tool_missing", "This system has no journalctl")
    if not view._session.ensure_virtual():
        raise RescueError("chroot_broken", "The system could not be prepared to run programs")
    if priority and not re.fullmatch(r"[0-7]|emerg|alert|crit|err|warning|notice|info|debug", priority):
        raise RescueError("bad_request", "Invalid journal priority")
    argv = ["journalctl", "--directory=/var/log/journal", "--no-pager", "--output=short-iso", "-b", str(int(boot)), "-n", str(max(1, min(int(lines), 2000)))]
    if priority:
        argv += ["-p", priority]
    if kernel:
        argv.append("-k")
    result = runner.chroot(argv, tools={"journalctl"}, timeout=120, check=False)
    return {"lines": result.stdout.splitlines()[-2000:], "rc": result.returncode}
