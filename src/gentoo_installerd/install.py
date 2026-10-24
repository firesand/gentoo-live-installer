from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from gentoo_installer.manifest import InstallManifest

from . import __version__
from .configure import GRUB_EFI_FILES, RECEIPT_PATH, Configurer, ExistingStack, PackageInstallError, grub_efi_target, live_input_problems, render_fstab
from .events import ErrorCode, Event, EventSink
from .hwplan import PLAN_SCHEMA, HardwarePlan, blocking_issues
from .hwplan import plan as plan_hardware
from .inspect import Discovery, Inspection
from .proxy import proxy_environment
from .runner import DryRunRunner, InstallCancelled, Runner, RunnerError, SubprocessRunner, redact_text
from .stage3 import Stage3Manager
from .storage import LayoutEntry, StorageManager

LOG = logging.getLogger(__name__)

STAGES = (
    "preflight",
    "storage",
    "stage3",
    "mounts",
    "repository",
    "portage",
    "kernel",
    "target",
    # Essential packages and services, then the bootloader, so the target is
    # bootable before the long, failure-prone desktop stage runs.
    "base",
    "bootloader",
    "desktop",
    "validation",
    "cleanup",
)

# Share of the overall progress bar per stage (sums to 100). The desktop stage is
# by far the longest for a full desktop and is empty for "minimal".
STAGE_WEIGHTS: dict[str, int] = {
    "preflight": 2,
    "storage": 8,
    "stage3": 12,
    "mounts": 1,
    "repository": 5,
    "portage": 2,
    "kernel": 15,
    "target": 2,
    "base": 12,
    "bootloader": 5,
    "desktop": 31,
    "validation": 3,
    "cleanup": 2,
}
assert tuple(STAGE_WEIGHTS) == STAGES and sum(STAGE_WEIGHTS.values()) == 100


def stage_weights(desktop: str = "") -> dict[str, int]:
    """Stage weights for a manifest's desktop choice (still summing to 100)."""
    weights = dict(STAGE_WEIGHTS)
    if desktop == "minimal":
        weights["base"] += weights["desktop"] - 1
        weights["desktop"] = 1
    return weights


# "emerge --verbose" announces every package as ">>> Emerging (3 of 10) cat/pkg-1.2::repo".
EMERGE_PROGRESS = re.compile(r"^>>> Emerging(?: binary)? \((\d+) of (\d+)\)")
LOG_DIRECTORY_IN_TARGET = "var/log/gentoo-installer"


# More than this and TLS certificates, signatures and Portage timestamps start to disagree with the mirror.
CLOCK_TOLERANCE_SECONDS = 600


def describe_clock_offset(seconds: float) -> str:
    """"20 minutes", "5 hours" or "3 days" for a clock that is off by ``seconds``."""
    minutes = round(seconds / 60)
    if minutes < 120:
        return f"{max(minutes, 1)} minutes"
    hours = round(seconds / 3600)
    return f"{hours} hours" if hours < 48 else f"{round(seconds / 86400)} days"


def error_code(exc: BaseException) -> str:
    """The data.code of a failure: an exception that names its own (StorageError: storage.*) keeps it."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code.startswith(("storage.", "install.", "proto.")) else ErrorCode.FAILED


def tools_for_selection(manifest: InstallManifest) -> set[str]:
    """Programs of the live system that this configuration will run, so a missing one is found before any disk is touched.

    Signing tools are not among them: Secure Boot images are signed inside the new system, after
    app-crypt/sbsigntools was installed there.
    """
    storage, system = manifest.storage, manifest.system
    tools: set[str] = set()
    if storage.firmware == "uefi":
        tools.add("mkfs.vfat")
    if storage.filesystem in {"xfs", "btrfs"}:
        tools.add("mkfs." + storage.filesystem)
    if storage.filesystem == "btrfs" and storage.mode == "erase":
        tools.add("btrfs")  # subvolumes and the swap file of an encrypted install
    for partition in storage.partitions:
        if partition.format and partition.filesystem in {"ext4", "xfs", "btrfs", "vfat"}:
            tools.add("mkfs." + partition.filesystem)
    if storage.swap_mib or any(p.filesystem == "swap" for p in storage.partitions):
        tools.update(("swapon", "swapoff"))
    if storage.luks:
        tools.add("cryptsetup")
    if storage.lvm:
        tools.update(("pvcreate", "vgcreate", "lvcreate", "pvs", "vgchange"))
    if storage.raid_level:
        tools.add("mdadm")
    if system.bootloader == "efi-stub":
        tools.add("efibootmgr")
    return tools


class InstallError(RuntimeError):
    pass


class CheckpointStore:
    """Completed stages of one manifest, persisted so that a failed or cancelled install can resume.

    The fingerprint is the hash of the *redacted* manifest, so it covers which
    secrets are set but not their values. ``secrets`` (name -> value) adds a
    salted scrypt digest per secret group to the file, never the secret
    itself, so a resume notices a changed passphrase or password.
    """

    def __init__(self, path: str | Path, fingerprint: str, secrets: Mapping[str, str] | None = None) -> None:
        self.path = Path(path)
        self.fingerprint = fingerprint
        self.completed: set[str] = set()
        self.secrets = {name: value for name, value in (secrets or {}).items() if value}
        self.changed_secrets: set[str] = set()
        self._stored_secrets: dict[str, str] | None = None

    @staticmethod
    def _digest(salt: bytes, value: str) -> str:
        if hasattr(hashlib, "scrypt"):
            return hashlib.scrypt(value.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()
        return hashlib.pbkdf2_hmac("sha256", value.encode(), salt, 300_000, dklen=32).hex()

    def _secrets_record(self, refresh: bool) -> dict[str, str]:
        """Digests to persist.

        Stored digests are kept until the ``target`` stage (which applies the account and
        Wi-Fi secrets) completes, so a changed secret keeps being noticed until it is applied.
        """
        record = self._stored_secrets
        if record is not None and record.get("salt") and not refresh:
            return record
        salt_hex = record["salt"] if record and record.get("salt") else os.urandom(16).hex()
        salt = bytes.fromhex(salt_hex)
        return {"salt": salt_hex, **{name: self._digest(salt, value) for name, value in self.secrets.items()}}

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallError(f"Checkpoint state is unreadable: {exc}") from exc
        if raw.get("fingerprint") != self.fingerprint or raw.get("version") != 1 or not isinstance(raw.get("completed"), list):
            raise InstallError("Checkpoint state does not match this manifest")
        if any(stage not in STAGES for stage in raw["completed"]):
            raise InstallError("Checkpoint state contains an unknown stage")
        self.completed = set(raw["completed"])
        stored = raw.get("secrets")
        # Files without the key (older versions) are accepted: nothing to compare against.
        if isinstance(stored, dict) and isinstance(stored.get("salt"), str):
            try:
                salt = bytes.fromhex(stored["salt"])
            except ValueError as exc:
                raise InstallError("Checkpoint secret digests are unreadable") from exc
            self._stored_secrets = {key: value for key, value in stored.items() if isinstance(value, str)}
            self.changed_secrets = {name for name, value in self.secrets.items() if stored.get(name) != self._digest(salt, value)}

    def is_complete(self, stage: str, verifier: Callable[[], bool] | None = None) -> bool:
        return stage in self.completed and verifier is not None and verifier()

    def complete(self, stage: str) -> None:
        if stage not in STAGES or stage in {"preflight", "cleanup"}:
            return
        self.completed.add(stage)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        record = self._secrets_record(refresh=stage == "target")
        payload = {"version": 1, "fingerprint": self.fingerprint, "completed": sorted(self.completed), "secrets": record}
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
        self._stored_secrets = record
        if stage == "target":
            self.changed_secrets = set()

    def save_layout(self, layout: list[LayoutEntry], receipt: dict | None = None) -> None:
        path = self.path.with_name("layout.json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"fingerprint": self.fingerprint, "layout": [entry.__dict__ for entry in layout], "receipt": receipt}, indent=2) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)

    def load_layout(self) -> list[LayoutEntry]:
        try:
            raw = json.loads(self.path.with_name("layout.json").read_text())
            if not isinstance(raw, dict) or raw.get("fingerprint") != self.fingerprint:
                raise ValueError("layout does not match this manifest")
            # options and source arrived later than the other fields; a file without them is a plain layout.
            return [LayoutEntry(str(item["device"]), str(item["mountpoint"]), str(item["filesystem"]), bool(item["format"]),
                                str(item.get("options", "")), str(item.get("source", ""))) for item in raw["layout"]]
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise InstallError(f"Saved storage layout is invalid: {exc}") from exc

    def load_receipt(self) -> dict:
        try:
            raw = json.loads(self.path.with_name("layout.json").read_text())
            if raw.get("fingerprint") != self.fingerprint or not isinstance(raw.get("receipt"), dict):
                raise ValueError("missing or mismatched storage identity receipt")
            return raw["receipt"]
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise InstallError(f"Cannot safely resume storage: {exc}") from exc


class InstallBusyError(InstallError):
    """Another installation (a daemon job or a command-line run) already holds the state directory."""


class _LogFile:
    """Append-only text log with no user-space buffer.

    A failed write leaves nothing queued for a later retry, so a full or failing
    disk can never make the log grow in memory or replay stale data.
    """

    def __init__(self, path: Path) -> None:
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        self.closed = False

    def write(self, text: str) -> int:
        data = text.encode("utf-8", "replace")
        if os.write(self._fd, data) != len(data):
            raise OSError(errno.ENOSPC, "Short write to the install log")
        return len(text)

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            os.close(self._fd)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def manifest_secrets(manifest: InstallManifest) -> tuple[str, ...]:
    """Every secret value of a manifest, for redaction (empty values are dropped)."""
    values = [manifest.accounts.password, manifest.accounts.root_password, manifest.storage.luks_passphrase, manifest.network.wifi_password]
    if manifest.network.proxy:
        try:
            values.append(urlsplit(manifest.network.proxy).password or "")
        except ValueError:
            pass
    return tuple(value for value in values if value)


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


class Installer:
    def __init__(
        self,
        manifest: InstallManifest,
        *,
        runner: Runner | None = None,
        target_root: str | Path = "/mnt/gentoo",
        state_dir: str | Path = "/var/lib/gentoo-installer",
        cache_dir: str | Path = "/var/tmp/gentoo-installer",
        dry_run: bool = False,
        resume: bool = False,
        emit: EventSink | None = None,
        cancel: threading.Event | None = None,
        inspector: Discovery | None = None,
        storage: StorageManager | None = None,
        stage3: Stage3Manager | None = None,
        configurer: Configurer | None = None,
        allow_unverified_disk: bool = False,
        live_root: str | Path = "/",
    ) -> None:
        manifest.require_valid()
        self.manifest = manifest
        self.dry_run = dry_run
        self.resume = resume
        self.target_root = Path(target_root)
        self.state_dir = Path(state_dir)
        self.cancel = cancel or threading.Event()
        self.job_id = uuid.uuid4().hex
        self._event_sink = emit or (lambda event: None)
        self.secrets = manifest_secrets(manifest)
        # Progress and stage bookkeeping: every stage/progress event carries the overall percentage.
        self.stage_weights = stage_weights(manifest.system.desktop)
        self._offsets: dict[str, int] = {}
        done = 0
        for name in STAGES:
            self._offsets[name] = done
            done += self.stage_weights[name]
        self._stage = ""
        self._overall = 0.0
        self._stage_fraction = 0.0
        self._outcome = ""
        # Persistent log. A log that cannot be written never stops the installation.
        self.log_path = self.state_dir / "install.log"
        self._log_file: _LogFile | None = None
        self._log_failed = False
        self._log_dropped = 0
        self._sink_failures = 0
        self._lock_fd: int | None = None
        fingerprint = hashlib.sha256(manifest.to_json(redact=True).encode()).hexdigest()
        if not dry_run:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.state_dir.chmod(0o700)
            _write_private(self.state_dir / "manifest.json", redact_text(manifest.to_json(redact=True), self.secrets) + "\n")
            try:
                self._log_file = _LogFile(self.log_path)
            except OSError as exc:
                self._log_failed = True
                LOG.warning("Cannot open %s: %s", self.log_path, exc)
            self._write_log_record("created", message="Install job created", version=__version__, pid=os.getpid(), manifest_sha256=fingerprint, resume=resume)
        self.runner = runner or (DryRunRunner(self.log, self.secrets) if dry_run else SubprocessRunner(self.log, self.cancel, self.secrets))
        secret_groups = {
            "luks": manifest.storage.luks_passphrase,
            "accounts": manifest.accounts.password + "\0" + manifest.accounts.root_password,
            "wifi": manifest.network.wifi_password,
        }
        self.checkpoints = CheckpointStore(self.state_dir / "checkpoints.json", fingerprint, secret_groups)
        self.inspector = inspector or Discovery(self.runner)
        self.storage = storage or StorageManager(self.runner, self.target_root, allow_unverified_disk=allow_unverified_disk)
        self.stage3 = stage3 or Stage3Manager(self.runner, cache_dir, cancel=self.cancel)
        self.configurer = configurer or Configurer(self.runner, self.target_root, dry_run=dry_run)
        self.inspection: Inspection | None = None
        self.hardware_plan: HardwarePlan | None = None
        self._plan_attempted = False
        # Where the live system's zone, keymap and locale lists are looked up (a test points it elsewhere).
        self.live_root = Path(live_root)
        self._cleanup_done = False
        self._failed = False
        # The bootloader stage completed: the disk boots, whatever happens to the stages after it.
        self._bootable = False
        self._stage3_archive: Path | None = None
        # Texts for completed.data.warnings: things that went wrong after the installation itself succeeded.
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ events and log

    def emit(self, event_type: str, stage: str = "", message: str = "", progress: float | None = None, **data: object) -> None:
        """Publish an event and record it in the install log.

        For ``stage`` and ``progress`` events ``progress`` is the *stage-local* percentage (0-100);
        the event carries the monotonic overall percentage and ``data.stage_progress``.
        """
        error = str(data.pop("error", ""))
        if event_type != "completed":
            stage = stage or self._stage
        overall = progress
        if event_type in {"stage", "progress"}:
            local = self._local_progress(event_type, progress, data.get("status"))
            overall = self._overall_progress(stage, local)
            if local is not None:
                data["stage_progress"] = round(local, 1)
        elif event_type == "completed" and progress is not None:
            self._overall = max(self._overall, progress)
        if message:
            message = redact_text(message, self.secrets)
        if error:
            error = redact_text(error, self.secrets)
        event = Event(event_type, job_id=self.job_id, stage=stage, message=message, progress=overall, data={key: value for key, value in data.items() if value is not None}, error=error)
        self._record(event)
        self._deliver(event)

    def log(self, message: str) -> None:
        self.emit("log", message=message)
        if message.startswith(">>> Emerging"):
            self._note_package_progress(message)

    def _deliver(self, event: Event) -> None:
        """Hand an event to the sink; a failing sink never stops an installation."""
        try:
            self._event_sink(event)
        except Exception:
            self._sink_failures += 1
            if self._sink_failures in (1, 100, 10000):
                LOG.exception("Event sink failed (%d failures so far); the installation continues", self._sink_failures)

    def _record(self, event: Event) -> None:
        extra = {key: event.data[key] for key in ("status", "code", "retryable", "stage_progress", "success", "resumable") if key in event.data}
        self._write_log_record(event.type, stage=event.stage, message=event.message, progress=event.progress, error=event.error, **extra)

    def _write_log_record(self, record_type: str, **fields: object) -> None:
        """Append one JSON line to the install log. Never raises: the log is a diagnostic, not a precondition."""
        handle = self._log_file
        if handle is None or handle.closed:
            return
        record: dict[str, object] = {"ts": _timestamp(), "job_id": self.job_id, "type": record_type}
        record.update({key: value for key, value in fields.items() if value not in (None, "")})
        try:
            lines = ""
            if self._log_dropped:
                # Something was lost: restart on a fresh line (the last write may be torn) and say so.
                lines = "\n" + json.dumps({"ts": _timestamp(), "job_id": self.job_id, "type": "log", "message": f"{self._log_dropped} install log records could not be written"}) + "\n"
            handle.write(lines + json.dumps(record, ensure_ascii=False, default=str) + "\n")
            handle.flush()
            self._log_dropped = 0
        except Exception as exc:
            self._log_dropped += 1
            if not self._log_failed:
                self._log_failed = True
                message = f"WARNING: the install log cannot be written ({exc}); the installation continues without a complete persistent log"
                LOG.warning(message)
                self._deliver(Event("log", job_id=self.job_id, stage=self._stage, message=message, data={"warning": "install_log_unwritable"}))

    def _local_progress(self, event_type: str, progress: float | None, status: object) -> float | None:
        if event_type == "stage":
            progress = {"running": 0.0, "completed": 100.0, "skipped": 100.0}.get(str(status), progress)
        if progress is None:
            return None
        return min(max(float(progress), 0.0), 100.0)

    def _overall_progress(self, stage: str, local: float | None) -> float:
        """Overall percentage after ``stage`` reached ``local`` percent; never goes backwards."""
        if stage == "cleanup" and self._outcome not in ("", "completed"):
            return self._overall  # releasing mounts after a failure or a cancel is not progress
        weight = self.stage_weights.get(stage)
        if weight is not None and local is not None:
            self._overall = max(self._overall, round(self._offsets[stage] + weight * local / 100.0, 2))
        return self._overall

    def _note_package_progress(self, line: str) -> None:
        """Turn ">>> Emerging (3 of 10) ..." into smooth progress inside the long package stages."""
        match = EMERGE_PROGRESS.match(line)
        if not match or not self._stage:
            return
        index, total = int(match.group(1)), int(match.group(2))
        if not 0 < index <= total:
            return
        self._stage_fraction = max(self._stage_fraction, (index - 1) / total)
        self.emit("progress", self._stage, f"Package {index} of {total}", self._stage_fraction * 100, package=index, packages=total)

    # --------------------------------------------------------------------- run

    def run(self) -> bool:
        success = False
        cancelled = False
        locked = False
        try:
            self._acquire_run_lock()
            locked = True
            if self.resume and not self.dry_run:
                self.checkpoints.load()
                self._reconcile_secrets()
            self._execute()
            success = True
        except InstallCancelled as exc:
            cancelled = True
            self._failed = True
            self._report_failure(exc, str(exc), ErrorCode.CANCELLED, error="cancelled")
        except InstallBusyError as exc:
            self._failed = True
            self._report_failure(exc, str(exc), ErrorCode.BUSY, retryable=False)
        except Exception as exc:
            self._failed = True
            clean = redact_text(str(exc), self.secrets)
            self.log(f"Installation failed: {clean}")
            self._report_failure(exc, clean, error_code(exc))
        finally:
            self._outcome = "completed" if success else "cancelled" if cancelled else "failed"
            problem = ""
            try:
                if locked:
                    self._cleanup()
            except Exception as exc:
                problem = redact_text(str(exc), self.secrets)
            if problem and success:
                # Every stage worked; only releasing the disks failed. The system is installed, so say what to do.
                warning = (f"{problem} The installation itself is complete. Before you restart, release the disks by hand: "
                           f"run 'umount -R {self.target_root}' and 'swapoff -a', then 'cryptsetup close' or 'vgchange -an' for an encrypted or LVM root.")
                self.warnings.append(warning)
                self.emit("status", "cleanup", warning, warning=True)
            elif problem:
                self._failed = True
                self.emit("error", stage="cleanup", message=problem, error=problem, code=ErrorCode.CLEANUP_FAILED, retryable=True,
                          resumable=bool(self._resumable()), bootable=self._bootable)
            message = "Installation cancelled" if cancelled else "Installation completed successfully" if success else "Installation failed"
            self.emit("completed", message=message, progress=100.0 if success else None, success=success,
                      resumable=bool(self._resumable()) if not success else None, bootable=self._bootable,
                      degraded=list(self.degraded), warnings=list(self.warnings))
            LOG.info("Install job %s finished: %s", self.job_id, "success" if success else self._outcome)
            self._release_run_lock()
            if self._log_file:
                try:
                    self._log_file.close()
                except Exception:
                    pass
        return success

    def _report_failure(self, exc: BaseException, message: str, code: str, *, error: str | None = None, retryable: bool = True) -> None:
        """The error event of a failed job: always with the stage, a machine-readable code and what can still be done."""
        self.emit("error", stage=self._stage or "preflight", message=message, error=message if error is None else error, code=code,
                  retryable=retryable, resumable=bool(self._resumable()), bootable=self._bootable or bool(getattr(exc, "bootable", False)))

    def _resumable(self) -> bool | None:
        """Whether a later run with resume=True can continue (storage was prepared and checkpointed)."""
        return True if "storage" in self.checkpoints.completed and not self.dry_run else None

    def _acquire_run_lock(self) -> None:
        """One installation per state directory, across daemon jobs and command-line runs."""
        if self.dry_run:
            return
        fd = os.open(self.state_dir / "install.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise InstallBusyError("Another installation is already running") from exc
            raise
        self._lock_fd = fd

    def _release_run_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            finally:
                self._lock_fd = None

    def _reconcile_secrets(self) -> None:
        """A resume must not silently keep a secret the user has since changed."""
        changed = self.checkpoints.changed_secrets
        if "luks" in changed:
            raise InstallError("The LUKS passphrase differs from the one this installation was created with; resume with the original passphrase")
        if changed & {"accounts", "wifi"}:
            self.log("Account or Wi-Fi secrets changed since the checkpoint; the target stage will be repeated to apply them")
            self.checkpoints.completed.discard("target")

    def _save_checkpoint(self, name: str) -> None:
        try:
            self.checkpoints.complete(name)
        except OSError as exc:
            self.log(f"WARNING: could not save the checkpoint for {name} ({exc}); a resume will repeat this stage")

    def _execute(self) -> None:
        steps: list[tuple[str, Callable[[], None], Callable[[], bool] | None]] = [
            ("preflight", self._preflight, None),
            ("storage", self._storage, self._verify_storage),
            ("stage3", self._stage3, lambda: self._target_file("etc/gentoo-release")),
            ("mounts", self._mounts, None),
            ("repository", self._repository, lambda: self._target_file("var/db/repos/gentoo/metadata")),
            ("portage", self._portage, lambda: self._target_file("etc/portage/make.conf")),
            ("kernel", self._kernel, self._verify_kernel),
            ("target", self._target, lambda: self._target_file("etc/fstab")),
            ("base", self._base, None),
            ("bootloader", self._bootloader, self._verify_bootloader),
            ("desktop", self._desktop, None),
            ("validation", self._validate, None),
        ]
        for name, operation, verifier in steps:
            self._check_cancel()
            self._stage = name
            self._stage_fraction = 0.0
            if self.resume and not self.dry_run and name == "storage":
                if "storage" not in self.checkpoints.completed:
                    raise InstallError("No completed storage checkpoint; resume will not recreate or format storage")
                self._verify_storage()
                self.emit("stage", name, "Restored verified storage without formatting", status="skipped")
                continue
            if self.resume and self.checkpoints.is_complete(name, verifier):
                self.emit("stage", name, f"Verified checkpoint; skipping {name}", status="skipped")
                self._stage_done(name)
                continue
            self.emit("stage", name, f"Starting {name}", status="running")
            operation()
            if not self.dry_run:
                self._save_checkpoint(name)
            self._stage_done(name)
            self.emit("stage", name, f"Completed {name}", status="completed")

    def _stage_done(self, name: str) -> None:
        if name == "bootloader":
            self._bootable = True
        elif name == "stage3":
            self._discard_stage3_archive()

    def _preflight(self) -> None:
        if self.target_root.resolve() == Path("/") or not self.target_root.is_absolute():
            raise InstallError("Target root must be an absolute, non-root mount directory")
        if any(path.is_symlink() for path in (self.target_root, *self.target_root.parents)):
            raise InstallError("Target root must not contain symlinks")
        if isinstance(self.inspector, Discovery):
            # Probe the mirror the install will really download from, through the proxy the user configured.
            self.inspector.configure(mirror=self.manifest.portage.mirror, proxy=self.manifest.network.proxy)
        self.inspection = self.inspector.inspect()
        if self.manifest.storage.firmware == "uefi" and not self.dry_run and not self.inspection.uefi:
            raise InstallError(
                "The installation is set to UEFI, but this PC was started in legacy BIOS (CSM) mode. "
                "Restart the installer in UEFI mode, or choose Legacy BIOS in the storage settings"
            )
        for warning in self.inspection.warnings:
            self.log(f"Preflight warning: {warning}")
        # Structured findings decide (severity "error" blocks); no message wording is matched.
        hardware = getattr(self.inspection, "hardware", None) or None
        fatal = [
            *self.inspection.blocking_errors(),
            *(issue.message for issue in blocking_issues(self.manifest, hardware)),
            *live_input_problems(self.manifest, root=self.live_root, runner=self.runner),
            *self._clock_problems(),
        ]
        fatal = list(dict.fromkeys(fatal))
        if fatal and self.dry_run:
            # A preview cannot know the real machine's network, tools or disks: report, never block.
            for error in fatal:
                self.log(f"[dry-run] preflight finding (fatal in a real run): {error}")
            fatal = []
        if fatal:
            raise InstallError("Preflight failed: " + "; ".join(fatal))
        missing = sorted(tool for tool in tools_for_selection(self.manifest) if self.inspection.tools.get(tool) is False)
        if missing and self.dry_run:
            self.log("[dry-run] selected configuration needs tools missing on this machine: " + ", ".join(missing))
        elif missing:
            raise InstallError("Selected configuration needs missing tools: " + ", ".join(missing))
        if self.manifest.system.kernel == "manual":
            self.log("Manual kernel selected: installation will stop at the kernel stage with interactive instructions")
        plan = self._plan()
        setup = list(plan.summary) if plan is not None else []
        for line in setup:
            self.log(f"Hardware setup: {line}")
        for note in plan.notes if plan is not None else []:
            self.log(f"Hardware note: {note}")
        # Decisions that surprise (a driver that was asked for but does not fit this PC) are part of the result, not only of the log.
        for issue in plan.warnings if plan is not None else []:
            if issue.severity in ("warning", "error") and issue.message not in self.warnings:
                self.warnings.append(issue.message)
        # The probe's full result went to the client with the inspect reply; the event keeps the summary.
        found = self.inspection.to_dict()
        found.pop("hardware", None)
        self.emit("progress", "preflight", "System readiness checked", 5.0, inspection=found, hardware_setup=setup)

    def _clock_problems(self) -> list[str]:
        """A clock that is far off breaks certificate and signature checks, so say so before anything is changed."""
        if self.dry_run or self.inspection is None:
            return []
        # skew_seconds is the mirror's time minus this computer's clock (Discovery asked the mirror; None when it was unreachable).
        skew = (getattr(self.inspection, "clock", None) or {}).get("skew_seconds")
        if not isinstance(skew, (int, float)) or abs(skew) <= CLOCK_TOLERANCE_SECONDS:
            return []
        return [f"The system clock is off by {describe_clock_offset(abs(skew))}. Set the date or connect to the network so it syncs, then try again."]

    def _storage(self) -> None:
        # What will happen to which disk goes into the log before anything is changed. A plan is
        # information: failing to build one never stops the installation (prepare() does the checking).
        plan = None
        try:
            plan = self.storage.describe(self.manifest, None if self.dry_run else self.storage.inventory())
            for line in plan.lines():
                self.log(f"Storage plan: {line}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"Storage plan unavailable: {exc}")
        layout = self.storage.prepare(self.manifest, dry_run=self.dry_run)
        for note in self.storage.notes:
            self.log(f"Storage note: {note}")
        if not self.dry_run:
            self.checkpoints.save_layout(layout, self.storage.snapshot())
        self.emit("progress", "storage", "Storage layout prepared", data={"layout": [entry.__dict__ for entry in layout], "plan": plan.to_dict() if plan else None})

    # --- stage helpers -----------------------------------------------------------------

    @property
    def degraded(self) -> list[str]:
        """Optional pieces that could not be installed; the installation still finishes."""
        return self.__dict__.setdefault("_degraded", [])

    # --- the hardware plan -----------------------------------------------------------------

    def _plan(self) -> HardwarePlan | None:
        """What this machine needs on top of the manifest (microcode, firmware, drivers, power, ...).

        Decided once per job from the hardware probe of the inspection, or restored on a resume so
        that the stages after the interruption follow the decisions of the stages before it. Every
        installation has a plan: without probe data it is the generic one, which still decides
        microcode and firmware from the manifest options.
        """
        if self.hardware_plan is None and not self._plan_attempted:
            self._plan_attempted = True
            self.hardware_plan = self._restore_plan()
            if self.hardware_plan is None:
                self.hardware_plan = self._make_plan()
                if self.hardware_plan is not None:
                    self._save_plan(self.hardware_plan)
        return self.hardware_plan

    def _make_plan(self) -> HardwarePlan | None:
        hardware = getattr(self.inspection, "hardware", None) or None
        try:
            return plan_hardware(self.manifest, hardware)
        except Exception as exc:  # noqa: BLE001 - a planning bug must not stop an install that can go on without it
            self.log(f"WARNING: the hardware-specific setup could not be planned ({exc}); continuing with the generic setup")
        if hardware is not None:
            try:
                return plan_hardware(self.manifest, None)
            except Exception:  # noqa: BLE001
                pass
        self.degraded.append("The hardware-specific setup (microcode, firmware, drivers) could not be planned and was skipped")
        return None

    @property
    def plan_path(self) -> Path:
        return self.state_dir / "hardware-plan.json"

    def _save_plan(self, plan: HardwarePlan) -> None:
        """Keep the decision (private, bound to this manifest) so a resume does not decide again.

        The file is on the live system only and holds the plan's file contents; the copy that goes
        to the installed system keeps just their paths and hashes.
        """
        if self.dry_run:
            return
        record = {"version": 1, "fingerprint": self.checkpoints.fingerprint, "created": _timestamp(), "plan": plan.to_dict()}
        try:
            _write_private(self.plan_path, json.dumps(record, indent=2) + "\n")
        except OSError as exc:
            self.log(f"WARNING: the hardware-specific setup could not be saved ({exc}); a resume will decide it again")

    def _restore_plan(self) -> HardwarePlan | None:
        """The plan an earlier run of this job decided, or None when this is not a resume or none was saved."""
        if not self.resume or self.dry_run:
            return None
        try:
            raw = json.loads(self.plan_path.read_text())
        except FileNotFoundError:
            self.log("No saved hardware-specific setup was found; deciding it again")
            return None
        except (OSError, ValueError) as exc:
            raise InstallError(f"The saved hardware-specific setup cannot be read ({exc}); start the installation again") from exc
        plan = raw.get("plan") if isinstance(raw, dict) else None
        if not isinstance(plan, dict) or raw.get("version") != 1 or raw.get("fingerprint") != self.checkpoints.fingerprint or plan.get("schema") != PLAN_SCHEMA:
            raise InstallError("The saved hardware-specific setup does not belong to this installation; start the installation again")
        self.log("Using the hardware-specific setup that was decided when this installation started")
        return HardwarePlan.from_dict(plan)

    def _root_device(self) -> str:
        return next(entry.device for entry in self.storage.layout if entry.mountpoint == "/")

    def _uuid_of(self, device: str) -> str:
        if self.dry_run or not device:
            return ""
        try:
            return self.runner.run(("blkid", "-s", "UUID", "-o", "value", device)).stdout.strip().splitlines()[0]
        except (RunnerError, IndexError):
            return ""

    def _stage3(self) -> None:
        if self.dry_run:
            self.log("Would discover, signature-verify, hash-verify, download, and extract the selected official stage3")
            return
        self.target_root.mkdir(parents=True, exist_ok=True)
        self.stage3.notify = self.log
        # Mount points are not leftovers, and neither is the installer's own swap file (made inside the new root before this stage).
        allowed = [entry.mountpoint for entry in self.storage.layout if entry.mountpoint] + [entry.source for entry in self.storage.layout if entry.source]
        extra: dict[str, Any] = {"allowed_existing": allowed}
        if self.manifest.network.proxy:
            extra["proxy"] = self.manifest.network.proxy
        if self.manifest.portage.mirror_fallbacks:
            extra["fallback_mirrors"] = tuple(self.manifest.portage.mirror_fallbacks)
        self._stage3_archive = self.stage3.install(
            self.manifest.portage.mirror,
            self.manifest.system.stage_variant,
            self.target_root,
            lambda current, total: self.emit("progress", "stage3", "Downloading stage3", current * 100 / total if total else None, downloaded=current, total=total),
            **extra,
        )

    def _discard_stage3_archive(self) -> None:
        """The unpacked stage3 is checkpointed, so the archive can go: the live system keeps its cache in memory."""
        archive, self._stage3_archive = self._stage3_archive, None
        if archive is None or self.dry_run:
            return
        try:
            self.stage3.discard(archive)
        except Exception as exc:  # noqa: BLE001 - a leftover archive costs memory, it must not fail the installation
            self.log(f"The downloaded stage3 archive could not be removed ({exc})")

    def _mounts(self) -> None:
        if not self.dry_run:
            for directory in ("dev", "proc", "sys", "run", "etc"):
                self.configurer.path(directory).mkdir(parents=True, exist_ok=True)
            try:
                resolv = Path("/etc/resolv.conf").read_text()
            except OSError as exc:
                raise InstallError(f"The live system has no readable /etc/resolv.conf to copy into the new system ({exc}); connect to a network first") from exc
            self.configurer.write("etc/resolv.conf", resolv)
        mounts = [
            (("mount", "--rbind", "/dev", str(self.configurer.path("dev"))), self.configurer.path("dev")),
            (("mount", "--types", "proc", "proc", str(self.configurer.path("proc"))), self.configurer.path("proc")),
            (("mount", "--rbind", "/sys", str(self.configurer.path("sys"))), self.configurer.path("sys")),
            (("mount", "--rbind", "/run", str(self.configurer.path("run"))), self.configurer.path("run")),
        ]
        for command, path in mounts:
            mounted = False
            if not self.dry_run:
                source = self.storage.mount_source(path)
                mounted = bool(source)
                if mounted:
                    host_path = Path("/") / path.name
                    expected = self.storage.mount_source(host_path)
                    if not expected or source != expected:
                        raise InstallError(f"Unrelated virtual filesystem mounted at {path}")
            if not mounted:
                self.runner.run(command)
            if not self.dry_run and path not in self.storage.owned_mounts:
                self.storage.owned_mounts.append(path)
        self.runner.run(("mount", "--make-rprivate", str(self.configurer.path("dev"))))
        self.runner.run(("mount", "--make-rprivate", str(self.configurer.path("sys"))))
        self.runner.run(("mount", "--make-rprivate", str(self.configurer.path("run"))))

    def _repository(self) -> None:
        self.configurer.notify = self.log
        # make.conf first: emerge-webrsync reads GENTOO_MIRRORS from it, and a proxy has to be
        # known before the first download.
        self.configurer.configure_portage(self.manifest, self._plan())
        environment = proxy_environment(self.manifest.network.proxy)
        if self.manifest.portage.rsync_mirror:
            self.configurer.chroot(("emaint", "sync", "--auto"), env=environment)
        else:
            self.configurer.chroot(("emerge-webrsync",), env=environment)

    def _portage(self) -> None:
        self.configurer.notify = self.log
        plan = self._plan()
        self.configurer.configure_portage(self.manifest, plan)
        self.configurer.chroot(("eselect", "profile", "set", self.manifest.effective_profile()))
        # Resolve everything the later stages will emerge now: an impossible combination of
        # options fails here, in minutes and with the disk still reusable, not hours later.
        required = [*self.configurer.kernel_stage_atoms(self.manifest, self.storage.layout, plan),
                    *self.configurer.kernel_atoms(self.manifest), *self.configurer.essential_atoms(self.manifest, plan),
                    *self.configurer.bootloader_atoms(self.manifest), *self.configurer.desktop_atoms(self.manifest, plan).required]
        try:
            self.configurer.pretend(self.manifest, list(dict.fromkeys(required)))
        except PackageInstallError as exc:
            raise InstallError(str(exc)) from exc

    def _kernel(self) -> None:
        plan = self._plan()
        self.configurer.notify = self.log
        root = self._root_device()
        stack = self._storage_stack()
        if stack.unsupported:
            raise InstallError(stack.unsupported)
        self.configurer.configure_initramfs(self.manifest, root, self.storage._stack.get("luks_device"), plan, resume_device=self._resume_device(), stack=stack)
        kernel = self.manifest.system.kernel
        if kernel == "manual":
            # "Manual kernel" is a command-line option: the graphical installer cannot pause for it.
            if self.resume and self._verify_kernel():
                self.log("Verified manually installed kernel and initramfs")
                return
            raise InstallError(f"Manual kernel selected. After cleanup, remount the saved target filesystems at {self.target_root}, chroot into that directory, install the kernel and initramfs under /boot, then resume")
        # Dracut needs the target's storage utilities when it builds the image.
        # Installing them in the later packages stage would produce an unusable
        # encrypted/LVM/RAID root image (or fail generation altogether). Firmware
        # packages also supply microcode and dracut configuration, and the filesystem
        # userland lets the installed system check its own partitions; later --noreplace skips them.
        self.configurer.prepare_stack_packages(stack)
        batch = self.configurer.kernel_stage_atoms(self.manifest, self.storage.layout, plan, stack)
        if batch:
            self.configurer.emerge(self.manifest, ("--noreplace", *batch))
        self.configurer.finish_stack(self.manifest, stack)
        # systemd (systemd-utils on OpenRC) is already installed in its stage3, so --noreplace would keep the
        # build without the flags the installer wrote: cryptsetup for the initramfs, boot and ukify for
        # systemd-boot and a Unified Kernel Image. One rebuild, and a stop right here if a tool is still missing.
        rebuild = self.configurer.flag_rebuild(self.manifest, plan)
        if rebuild is not None:
            atom, expected = rebuild
            self.configurer.emerge(self.manifest, ("--update", "--newuse", "--oneshot", atom))
            for relative, message in expected:
                if not self.dry_run and not self.configurer.path(relative).exists():
                    raise InstallError(message)
        self.configurer.emerge(self.manifest, ("--noreplace", "sys-kernel/installkernel", f"sys-kernel/{kernel}"))
        # Files and out-of-tree drivers from the hardware plan must exist before the initramfs is built.
        self.configurer.write_plan_files(plan)
        driver_atoms = [str(atom) for atom in getattr(plan, "driver_atoms", None) or []]
        if driver_atoms:
            try:
                self.configurer.emerge(self.manifest, ("--noreplace", *driver_atoms))
            except RunnerError as exc:
                self.degraded.append(f"Hardware drivers ({', '.join(driver_atoms)}) could not be installed: {str(exc)[-200:]}")
                self.log("WARNING: " + self.degraded[-1])
        if self.manifest.system.initramfs == "dracut":
            self.configurer.emerge(self.manifest, ("--noreplace", "sys-kernel/dracut"))
            self.configurer.chroot(("dracut", "--force", "--regenerate-all"))

    def _resume_device(self) -> str | None:
        """The swap partition to resume from when hibernation is allowed (None when it is not).

        The kernel only looks for a hibernation image where resume= points, so without a swap partition the
        option cannot work: a swap file (the encrypted install's) cannot be named this way.
        """
        if not self.manifest.hardware.hibernate:
            return None
        swap = next((entry.device for entry in self.storage.layout if entry.filesystem == "swap" and not entry.source), None)
        if swap is None:
            raise InstallError("Hibernation is on, but this installation has no swap partition to resume from. "
                               "Add a swap partition as large as the memory, or turn hibernation off.")
        return swap

    def _storage_stack(self) -> ExistingStack:
        """The LVM volumes and md arrays under the layout that the installer did not create (manual storage only).

        A guided install makes its own stack from the manifest flags. A manual layout may sit on a volume group or
        an array that was activated before the installation; it is looked up once, in the live device tree.
        """
        stack: ExistingStack | None = self.__dict__.get("_existing_stack")
        if stack is None:
            disks = None
            if self.manifest.storage.mode == "manual" and not self.dry_run:
                disks = self.storage.inventory()
                if disks is None:
                    self.log("WARNING: the device tree could not be read, so the LVM volumes and RAID arrays under the layout were not looked for")
            stack = self.configurer.detect_stack(self.storage.layout, disks, self._resume_device())
            self.__dict__["_existing_stack"] = stack
        return stack

    def _target(self) -> None:
        self.configurer.notify = self.log
        self.configurer.configure_identity(self.manifest)
        self.configurer.configure_accounts(self.manifest)
        self.configurer.configure_network(self.manifest)
        self._write_fstab()
        self.configurer.configure_privileges(self.manifest)

    def _write_fstab(self) -> None:
        uuids: dict[str, str] = {}
        for entry in self.storage.layout:
            if entry.source or entry.device in uuids:
                continue  # a swap file is named by its path, not by the UUID mkswap gave it; one lookup per device
            result = self.runner.run(("blkid", "-s", "UUID", "-o", "value", entry.device))
            if result.stdout.strip():
                uuids[entry.device] = result.stdout.strip().splitlines()[0]
        discard = bool(getattr(self._plan(), "scalars", {}).get("btrfs_discard_async"))
        self.configurer.write("etc/fstab", render_fstab(self.storage.layout, uuids, btrfs_discard=discard))

    def _base(self) -> None:
        """Essential packages and services: after this the system can be managed from a console."""
        self.configurer.notify = self.log
        try:
            self.configurer.install_essential(self.manifest, self._plan())
        except PackageInstallError as exc:
            raise InstallError(str(exc)) from exc
        self.configurer.configure_network_late(self.manifest)
        self.configurer.configure_services(self.manifest, self._plan(), phase="base")

    def _bootloader(self) -> None:
        self.configurer.notify = self.log
        self.configurer.emerge(self.manifest, ("--noreplace", *self.configurer.bootloader_atoms(self.manifest)))
        root = self._root_device()
        efi = next((entry.device for entry in self.storage.layout if entry.mountpoint in {"/efi", "/boot/efi"}), None)
        self.configurer.configure_bootloader(self.manifest, root, efi, self._plan())
        self.configurer.write_receipt(self.manifest, root, self.storage.layout, self.storage._stack.get("luks_device"), self._plan(), stack=self._storage_stack())

    def _desktop(self) -> None:
        """Desktop and optional packages, installed after the bootloader.

        The system is already bootable here, so a failure of an optional package is reported as a
        warning (``degraded``); only a desktop environment that cannot be installed fails the stage,
        flagged ``bootable`` so the front end can offer to reboot into the base system.
        """
        self.configurer.notify = self.log
        plan = self._plan()
        try:
            result = self.configurer.install_desktop(self.manifest, plan)
        except PackageInstallError as exc:
            self.log(f"The base system is installed and bootable; only the desktop failed: {exc}")
            error = InstallError(f"{exc} The base system is installed and bootable; reboot to use it or retry the desktop installation.")
            error.bootable = True  # type: ignore[attr-defined]
            raise error from exc
        warnings = [*result.degraded, *self.configurer.configure_services(self.manifest, plan, phase="desktop")]
        self.configurer.configure_desktop_session(self.manifest)
        warnings.extend(self.configurer.run_plan_steps(plan))
        # Only binary packages can leave the installed USE flags different from make.conf, so source
        # installs skip the (minutes-long) check.
        drift = self.configurer.world_drift(self.manifest) if self.manifest.portage.binhost else []
        if drift:
            # Visible in install.log; the next @world update will rebuild these with the configured USE.
            self.log(f"NOTE: {len(drift)} installed package(s) differ from the configured USE flags: {', '.join(drift[:12])}"
                     + (", ..." if len(drift) > 12 else ""))
        if warnings:
            self.degraded.extend(warnings)
            for warning in warnings:
                self.log("WARNING: " + warning)
            self.emit("progress", "desktop", "Installed with warnings", degraded=warnings, bootable=True)

    def _validate(self) -> None:
        cfg = self.configurer
        try:
            for path in ("etc/fstab", "etc/portage/make.conf"):
                cfg.chroot(("test", "-s", f"/{path}"))
            cfg.chroot(("getent", "passwd", self.manifest.accounts.username))
            stack = self._storage_stack()
            problems = cfg.verify_boot_setup(self.manifest, self._root_device(), root_uuid=self._uuid_of(self._root_device()), plan=self._plan(), stack=stack)
            modules = cfg.initramfs_modules()
            if modules is not None:
                storage = self.manifest.storage
                for wanted, needed in (("crypt", storage.luks), ("lvm", storage.lvm or stack.early_lvm), ("mdraid", bool(storage.raid_level) or stack.early_md)):
                    if needed and wanted not in modules:
                        problems.append(f"The initramfs lacks the {wanted} module, so the root filesystem could not be opened at boot")
            if problems:
                raise InstallError("The installed system failed its final checks: " + "; ".join(problems))
        except InstallCancelled:
            raise
        except Exception:
            # The bootloader stage set this flag. A system that does not pass its final checks is not one the
            # clients may call bootable: they would offer "Restart" and hide "Start over".
            self._bootable = False
            raise
        if self.manifest.system.desktop != "minimal":
            self.log("Desktop packages installed; graphical session will be available after reboot")
        # Last: nothing after this point needs name resolution inside the target.
        cfg.finalize_network(self.manifest)

    def _cleanup(self) -> None:
        if self._cleanup_done:
            return
        self._stage = "cleanup"
        self.emit("stage", "cleanup", "Cleaning up installer-owned mounts", status="running")
        self._preserve_install_log()
        if not self.dry_run:
            self.configurer.cleanup_secrets()  # signing keys staged on the live system never outlive the job
        self.storage.cleanup()
        self._cleanup_done = True
        self.emit("stage", "cleanup", "Cleanup completed", status="completed")

    def _target_holds_system(self) -> bool:
        """The target filesystem is mounted and a stage3 was extracted into it."""
        return os.path.ismount(self.target_root) and (self.target_root / "etc/gentoo-release").is_file()

    def _preserve_install_log(self) -> None:
        """Leave a copy of the (already redacted) install log in the new system, before it is unmounted.

        Best effort and never fatal; it also runs after a failure or a cancellation, which is
        when the log is needed most.
        """
        if self.dry_run or self._log_file is None:
            return
        try:
            if not self._target_holds_system():
                return
            self._log_file.flush()
            directory = self.target_root / LOG_DIRECTORY_IN_TARGET
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
            _chown_root(directory)
            trailer = json.dumps({"ts": _timestamp(), "job_id": self.job_id, "type": "copy", "message": f"Copied into the new system before unmounting; job outcome: {self._outcome or 'unknown'}"}) + "\n"
            temporary = directory / "install.log.tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "wb") as out:
                if self.log_path.exists():
                    with self.log_path.open("rb") as source:
                        shutil.copyfileobj(source, out, 1 << 20)
                out.write(trailer.encode())
                out.flush()
                os.fsync(out.fileno())
            _chown_root(temporary)
            os.replace(temporary, directory / "install.log")
            _write_private(directory / "manifest.json", redact_text(self.manifest.to_json(redact=True), self.secrets) + "\n")
            if self.hardware_plan is not None:
                # For whoever supports this system later: what was decided, with the paths and hashes of the files but not their contents.
                plan = {"version": 1, "created": _timestamp(), "plan": self.hardware_plan.to_dict(redact_files=True)}
                _write_private(directory / "hardware-plan.json", json.dumps(plan, indent=2) + "\n")
            self._write_log_record("cleanup", message=f"Copied the install log to /{LOG_DIRECTORY_IN_TARGET}/install.log in the new system")
        except Exception as exc:
            LOG.warning("Could not copy the install log into the new system: %s", exc)
            self._write_log_record("cleanup", message=f"Could not copy the install log into the new system: {exc}")

    def _verify_storage(self) -> bool:
        self.storage.restore(self.manifest, self.checkpoints.load_layout(), self.checkpoints.load_receipt())
        return True

    def _verify_kernel(self) -> bool:
        boot = self.configurer.path("boot")
        kernel = any(p.is_file() and p.stat().st_size for pattern in ("vmlinuz-*", "kernel-*") for p in boot.glob(pattern))
        initramfs = self.manifest.system.initramfs == "none" or any(p.is_file() and p.stat().st_size for pattern in ("initramfs*", "initrd*") for p in boot.glob(pattern))
        return bool(kernel and initramfs)

    def _verify_bootloader(self) -> bool:
        efi = self.configurer.efi_mountpoint(self.manifest).lstrip("/")
        if self.manifest.system.uki and not self._target_file(f"{efi}/EFI/Linux/gentoo.efi"):
            return False
        if not self._target_file(RECEIPT_PATH):
            return False
        if self.manifest.system.bootloader == "efi-stub":
            return "Gentoo" in self.runner.run(("efibootmgr",)).stdout
        installed, removable = GRUB_EFI_FILES[grub_efi_target(self._plan())]
        if self.manifest.system.bootloader == "grub" and self.manifest.storage.firmware == "uefi" \
                and not (self._target_file(f"{efi}/EFI/Gentoo/{installed}") or self._target_file(f"{efi}/EFI/BOOT/{removable}")):
            return False
        path = "boot/grub/grub.cfg" if self.manifest.system.bootloader == "grub" else f"{efi}/loader/entries/gentoo.conf"
        return self._target_file(path)

    def _target_file(self, path: str) -> bool:
        target = self.configurer.path(path)
        return self.dry_run or target.exists()

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise InstallCancelled("Installation cancelled")
