"""GUI log file, last-resort exception handler and the diagnostics bundle.

The installer starts from an autostart entry, so a traceback in a Qt slot would
vanish into a session log nobody reads, and the daemon's own log is root-only.
This module gives users on unusual hardware something to attach to a report:
a rotating GUI log and a single JSON file with redacted settings, the hardware
view, daemon state and read-only command output.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import re
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Callable

LOG = logging.getLogger("gentoo_installer")
LOG_NAME = "gui.log"
LOG_BYTES = 1_000_000
LOG_BACKUPS = 3
# The credentials of an address end at the LAST "@" before the first "/", "?" or "#", as in the manifest (a proxy is accepted
# with a user name that is an e-mail address, or an "@" in the password): http://john@corp.com:secret@proxy:3128
USERINFO_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^\s/?#]*)@")
BUNDLE_COMMANDS = (
    ("uname", ("uname", "-a")),
    ("cmdline", ("cat", "/proc/cmdline")),
    ("lsblk", ("lsblk", "--output-all", "--json")),
    ("findmnt", ("findmnt", "--list")),
    ("lspci", ("lspci", "-nnk")),
    ("lsusb", ("lsusb",)),
    ("efibootmgr", ("efibootmgr", "-v")),
    ("memory", ("free", "-m")),
    ("dmesg", ("dmesg", "--level=err,warn,crit,alert,emerg")),
)


def redact_url_userinfo(value: str) -> str:
    """scheme://user:password@host -> scheme://user:<redacted>@host."""

    def hide(match: re.Match[str]) -> str:
        user = match["userinfo"].partition(":")[0]
        return f"{match['scheme']}{user}:<redacted>@" if user else f"{match['scheme']}<redacted>@"

    return USERINFO_RE.sub(hide, value)


def strip_url_userinfo(value: str) -> str:
    """Remove credentials from a URL completely (used for the saved draft)."""
    return USERINFO_RE.sub(lambda m: m["scheme"], value)


def scrub_text(text: str, secrets: list[str]) -> str:
    """Replace every known secret (and URL credentials) in free text."""
    for secret in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return redact_url_userinfo(text)


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "gentoo-installer"


def log_path() -> Path:
    return cache_dir() / LOG_NAME


def setup_gui_logging() -> Path | None:
    """Log to a private rotating file. Returns its path, or None if it cannot be created."""
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            path.touch(mode=0o600)
        path.chmod(0o600)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for existing in list(LOG.handlers):
        if getattr(existing, "_gentoo_gui_log", False):
            LOG.removeHandler(existing)
            existing.close()
    handler._gentoo_gui_log = True  # type: ignore[attr-defined]
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.info("GUI started: python %s on %s", platform.python_version(), platform.platform())
    return path


def tail_file(path: Path, lines: int = 300) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in handle.readlines()[-lines:]]
    except OSError:
        return []


# --- exceptions -----------------------------------------------------------------------------

_reentrant = threading.local()
_dialog_budget = {"left": 3}


def format_exception(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> str:
    return "".join(traceback.format_exception(exc_type, exc, tb))


def handle_exception(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
    *,
    show: Callable[[str, Path | None], None] | None = None,
) -> str:
    """Log an unhandled exception and tell the user where the details are."""
    text = format_exception(exc_type, exc, tb)
    LOG.error("Unhandled exception:\n%s", text)
    if issubclass(exc_type, KeyboardInterrupt):
        return text
    if show is not None and not getattr(_reentrant, "active", False) and _dialog_budget["left"] > 0:
        _dialog_budget["left"] -= 1
        _reentrant.active = True
        try:
            show(text, log_path())
        except Exception:  # noqa: BLE001 - the handler of last resort must not raise
            LOG.exception("Could not show the error dialog")
        finally:
            _reentrant.active = False
    return text


def install_excepthook(show: Callable[[str, Path | None], None] | None = None) -> Callable[..., Any]:
    """Route uncaught exceptions (main thread, worker threads) to the GUI log
    and an error dialog. Returns the previous sys.excepthook."""
    previous = sys.excepthook
    _dialog_budget["left"] = 3

    def hook(exc_type, exc, tb):
        handle_exception(exc_type, exc, tb, show=show)

    def thread_hook(args):
        handle_exception(args.exc_type, args.exc_value, args.exc_traceback, show=None)

    sys.excepthook = hook
    threading.excepthook = thread_hook
    return previous


# --- the bundle --------------------------------------------------------------------------------


def run_command(argv: tuple[str, ...], timeout: float = 6.0, limit: int = 60_000) -> str:
    """Read-only helper command; never raises, output is size-limited."""
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(not available: {exc})"
    output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr.strip() and not result.stdout.strip() else "")
    return output[-limit:]


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    return value


def _shorten(value: Any) -> Any:
    return "…" + value[-6:] if isinstance(value, str) and value else value


def describe_system_info(info: Any) -> dict[str, Any]:
    """SystemInfo as plain data; serial numbers and WWNs are shortened to their tails, partition-table ids and
    the names of saved network connections are removed."""
    data = _plain(info) if info is not None else {}
    data.pop("roots", None)  # the lsblk tree behind `disks`: the same facts with full serial numbers and UUIDs
    for disk in data.get("disks", []):
        for key in ("serial", "wwid"):
            if disk.get(key):
                disk[key] = _shorten(disk[key])
        disk["by_path"] = disk.get("by_path", "")
        disk["partitions"] = [{k: v for k, v in part.items() if k not in {"uuid", "partuuid"}} for part in disk.get("partitions", [])]
    hardware = data.get("hardware")
    if isinstance(hardware, dict):
        for disk in hardware.get("disks", []) if isinstance(hardware.get("disks"), list) else []:
            if isinstance(disk, dict):
                for key in ("serial", "wwn"):
                    disk[key] = _shorten(disk.get(key, ""))
                disk["ptuuid"] = ""
        if isinstance(hardware.get("network_profiles"), list):
            hardware["network_profiles"] = ["<name removed>"] * len(hardware["network_profiles"])
    daemon = data.get("daemon")
    if isinstance(daemon, dict):
        daemon["features"] = sorted(daemon.get("features", []))
        daemon["actions"] = sorted(daemon.get("actions", []))
    return data


def collect_bundle(
    *,
    manifest: Any = None,
    info: Any = None,
    socket_path: str = "",
    visible_log: str = "",
    reason: str = "",
    daemon_document: Callable[[], dict[str, Any] | None] | None = None,
    commands: bool = True,
) -> dict[str, Any]:
    secrets: list[str] = []
    manifest_data: Any = None
    if manifest is not None:
        manifest_data = manifest.to_dict(redact=True)
        network = manifest_data.get("network", {})
        if isinstance(network.get("proxy"), str):
            network["proxy"] = redact_url_userinfo(network["proxy"])
        secrets = [
            manifest.accounts.password, manifest.accounts.root_password,
            manifest.storage.luks_passphrase, manifest.network.wifi_password,
        ]
    try:
        from PySide6 import __version__ as pyside_version
    except ImportError:
        pyside_version = ""
    bundle: dict[str, Any] = {
        "format": 1,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reason": reason,
        "gui": {"python": platform.python_version(), "pyside6": pyside_version, "log": str(log_path())},
        "platform": {"machine": platform.machine(), "kernel": platform.release()},
        "manifest": manifest_data,
        "system_info": describe_system_info(info),
        "gui_log_tail": tail_file(log_path()),
        "install_log_tail": visible_log.splitlines()[-500:],
    }
    if daemon_document is not None:
        try:
            bundle["daemon"] = daemon_document()
        except Exception as exc:  # noqa: BLE001 - a bundle with a missing part is still useful
            bundle["daemon"] = {"error": str(exc)}
    if commands:
        bundle["commands"] = {name: run_command(argv) for name, argv in BUNDLE_COMMANDS}
    text = scrub_text(json.dumps(bundle, default=str), secrets)
    return json.loads(text)


def default_bundle_name() -> str:
    return f"gentoo-installer-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.json"


def write_bundle(path: str | Path, bundle: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    target.chmod(0o600)
    return target
