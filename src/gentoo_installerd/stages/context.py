"""CORE: what the install stages share (InstallError, the stage context and small file helpers)."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gentoo_installer.manifest import InstallManifest

    from ..configure import Configurer
    from ..hwplan import HardwarePlan
    from ..inspect import Discovery, Inspection
    from ..install import CheckpointStore
    from ..runner import Runner
    from ..stage3 import Stage3Manager
    from ..storage import StorageManager


class InstallError(RuntimeError):
    pass


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _chown_root(path: Path | str) -> None:
    """Files the installer writes into the target belong to root, not to the live daemon's group."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            os.chown(path, 0, 0)
        except OSError:
            pass


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` (mode 0600, root-owned) atomically."""
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    _chown_root(temporary)
    os.replace(temporary, path)


class StageContext:
    """What every stage can rely on: the attributes and the helpers that the Installer in install.py provides."""

    if TYPE_CHECKING:
        manifest: InstallManifest
        dry_run: bool
        resume: bool
        target_root: Path
        state_dir: Path
        cancel: threading.Event
        runner: Runner
        checkpoints: CheckpointStore
        inspector: Discovery
        storage: StorageManager
        stage3: Stage3Manager
        configurer: Configurer
        inspection: Inspection | None
        hardware_plan: HardwarePlan | None
        live_root: Path
        warnings: list[str]
        _plan_attempted: bool
        _stage3_archive: Path | None

        def emit(self, event_type: str, stage: str = "", message: str = "", progress: float | None = None, **data: object) -> None: ...
        def log(self, message: str) -> None: ...
        @property
        def degraded(self) -> list[str]: ...
        def _target_file(self, path: str) -> bool: ...
