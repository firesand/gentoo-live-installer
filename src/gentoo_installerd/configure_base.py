"""CORE: the Configurer's shared plumbing (target paths, files, chroot) and the install receipt."""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence, TYPE_CHECKING

from gentoo_installer import __version__ as INSTALLER_VERSION
from gentoo_installer.manifest import InstallManifest, System

from .runner import accepts_keyword, CommandResult, Runner, RunnerError
from .storage import LayoutEntry

if TYPE_CHECKING:
    from .configure_fstab import ExistingStack


class ConfigurationError(RuntimeError):
    pass


RECEIPT_PATH = "etc/gentoo-live-installer/install.json"


def shell_value(value: str) -> str:
    return shlex.quote(value)


# --- build parallelism from the machine that runs the install -----------------------------------


@dataclass(frozen=True)
class Resources:
    cpus: int
    mem_total: int  # bytes
    mem_available: int  # bytes


def machine_resources(meminfo: str | Path = "/proc/meminfo") -> Resources:
    """CPU count and memory of this machine; unreadable values degrade to a small safe machine."""
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    total = available = 0
    try:
        for line in Path(meminfo).read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in {"MemTotal", "MemAvailable"} and rest.split():
                value = int(rest.split()[0]) * 1024
                if key == "MemTotal":
                    total = value
                else:
                    available = value
    except (OSError, ValueError):
        pass
    return Resources(max(1, cpus), total, available or total)


def _plan_list(plan: object | None, name: str) -> list:
    return list(getattr(plan, name, None) or [])


def _plan_scalar(plan: object | None, name: str, default: Any = None) -> Any:
    """A resolved choice of the hardware plan (HardwarePlan.scalars), or ``default`` without a plan."""
    scalars = getattr(plan, "scalars", None)
    return scalars.get(name, default) if isinstance(scalars, dict) else default


class ConfigurerBase:
    if TYPE_CHECKING:
        # Calls from one domain into another. The implementations live in the module of the team that owns them; the
        # declarations are here so that the type checker sees them from every mixin. Keep them in step with the owner's signature.
        def filesystem_atoms(self, manifest: InstallManifest, layout: Sequence[LayoutEntry] = ()) -> list[str]: ...  # configure_fstab
        def storage_tool_atoms(self, manifest: InstallManifest, stack: ExistingStack | None = None) -> list[str]: ...  # configure_fstab
        def resolve_console_keymap(self, manifest: InstallManifest) -> str: ...  # configure_system
        def luks_prompt_uses_console_keymap(self, manifest: InstallManifest) -> bool: ...  # configure_system
        def wants_cron(self, manifest: InstallManifest) -> bool: ...  # configure_system
        def service_installed(self, name: str, init: str) -> bool: ...  # configure_system
        def early_boot_atoms(self, manifest: InstallManifest, plan: object | None = None) -> list[str]: ...  # configure_boot
        def bootloader_atoms(self, manifest: InstallManifest) -> list[str]: ...  # configure_boot

    def __init__(self, runner: Runner, target_root: str | Path, *, dry_run: bool = False,
                 companion_bundle: str | Path = "/usr/local/share/gentoo-installer",
                 secret_dir: str | Path = "/run/gentoo-installer-sb",
                 live_network_dir: str | Path = "/etc/NetworkManager/system-connections",
                 plan: object | None = None) -> None:
        self.runner = runner
        self.root = Path(target_root)
        self.dry_run = dry_run
        self.companion_bundle = Path(companion_bundle)
        self.secret_dir = Path(secret_dir)  # where the live system keeps signing material
        self.secret_chroot_dir = "/run/gentoo-installer-sb"  # the same directory as seen from the chroot
        self.live_network_dir = Path(live_network_dir)
        self.hardware_plan = plan
        self.notify: Callable[[str], None] = lambda message: None
        self.sleep: Callable[[float], None] = time.sleep
        self.resources: Callable[[], Resources] = machine_resources
        # The daemon's runner takes root_identity=True so chrooted tools run as root:root and do
        # not inherit the live service's group, and capture=False so the output of a long emerge
        # is streamed to the log instead of being kept in memory; other runners (tests) do not
        # know these keywords.
        self._root_identity = accepts_keyword(runner.run, "root_identity")
        self._can_skip_capture = accepts_keyword(runner.run, "capture")

    # ----- paths and files ------------------------------------------------------------------

    def path(self, relative: str | Path) -> Path:
        value = str(relative)
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ConfigurationError("Invalid target path")
        pure = PurePosixPath(value)
        if ".." in pure.parts:
            raise ConfigurationError(f"Target path escapes installation root: {value}")
        candidate = self.root / value.lstrip("/")
        root_resolved = self.root.resolve()
        resolved = candidate.resolve()
        if os.path.commonpath((str(root_resolved), str(resolved))) != str(root_resolved):
            raise ConfigurationError(f"Target path escapes installation root: {value}")
        return resolved

    @staticmethod
    def _as_root() -> bool:
        return hasattr(os, "geteuid") and os.geteuid() == 0

    def _mkdirs(self, directory: Path, mode: int = 0o755) -> None:
        """Create missing parent directories as root:root, never with the live service's group."""
        missing: list[Path] = []
        current = directory
        while not current.exists() and current != current.parent:
            missing.append(current)
            current = current.parent
        for created in reversed(missing):
            created.mkdir(mode=mode)
            created.chmod(mode)
            if self._as_root():
                os.chown(created, 0, 0)

    def write(self, relative: str | Path, content: str, mode: int = 0o644) -> None:
        self.write_bytes(relative, content.encode("utf-8"), mode)

    def write_bytes(self, relative: str | Path, data: bytes, mode: int = 0o644) -> None:
        path = self.path(relative)
        if self.dry_run:
            return
        self._mkdirs(path.parent)
        path = self.path(relative)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fchmod(handle.fileno(), mode)
                if self._as_root():
                    os.fchown(handle.fileno(), 0, 0)
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def read_text(self, relative: str | Path) -> str:
        path = self.path(relative)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def set_shell_variable(self, relative: str | Path, key: str, value: str) -> None:
        """Replace an active KEY=... assignment in a shell-style config file, or append one."""
        old = "" if self.dry_run else self.read_text(relative)
        kept = [line for line in old.splitlines() if not re.match(rf"\s*{re.escape(key)}=", line)]
        kept.append(f"{key}={shell_value(value)}")
        self.write(relative, "\n".join(line for line in kept).strip("\n") + "\n")

    def chroot(self, argv: Sequence[str], *, stdin: str | None = None, secret: bool = False,
               env: Mapping[str, str] | None = None, capture: bool = False) -> CommandResult:
        """Run ``argv`` inside the target. Its output is only logged unless ``capture`` asks to read it back."""
        extra: dict[str, Any] = {}
        if env:
            extra["env"] = env
        if self._root_identity:
            extra["root_identity"] = True
        if self._can_skip_capture and not capture:
            extra["capture"] = False
        return self.runner.run(("chroot", str(self.root), *argv), stdin=stdin, secret=secret, **extra)

    # ----- Portage configuration ------------------------------------------------------------------

    def _plan(self, plan: object | None) -> object | None:
        return plan if plan is not None else self.hardware_plan

    @staticmethod
    def systemd_package(system: System) -> str:
        """The stage3 package that carries bootctl, ukify, the systemd-boot images and (on systemd) the cryptsetup helper."""
        return "sys-apps/systemd" if system.init == "systemd" else "sys-apps/systemd-utils"

    @staticmethod
    def efi_mountpoint(manifest: InstallManifest) -> str:
        if manifest.storage.mode == "manual" and any(part.mountpoint == "/boot/efi" for part in manifest.storage.partitions):
            return "/boot/efi"
        return "/efi"

    # ----- receipt and verification -------------------------------------------------------------------

    def build_receipt(self, manifest: InstallManifest, *, root_uuid: str = "", luks_uuid: str = "",
                      root_filesystem: str = "", boot_separate: bool = False, plan: object | None = None,
                      stack: ExistingStack | None = None) -> dict:
        system, storage = manifest.system, manifest.storage
        return {
            "version": 1,
            "installer": INSTALLER_VERSION,
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "firmware": storage.firmware,
            "bootloader": system.bootloader,
            "bootloader_id": "Gentoo",
            "esp": self.efi_mountpoint(manifest) if storage.firmware == "uefi" else "",
            "boot_separate": boot_separate,
            "init": system.init,
            "kernel": system.kernel,
            "initramfs": system.initramfs,
            "uki": system.uki,
            "secure_boot": system.secure_boot,
            "profile": manifest.effective_profile(),
            "root": {"fstype": root_filesystem or storage.filesystem, "uuid": root_uuid, "luks_uuid": luks_uuid,
                     "lvm": storage.lvm or (stack is not None and stack.root_lvm), "raid": storage.raid_level or (stack.root_md if stack is not None else "")},
            "cmdline_file": "/etc/kernel/cmdline",
            "hardware": [str(line) for line in _plan_list(self._plan(plan), "summary")],
        }

    def write_receipt(self, manifest: InstallManifest, root_device: str, layout: Sequence[LayoutEntry] = (),
                      luks_device: str | None = None, plan: object | None = None, stack: ExistingStack | None = None) -> dict:
        """Record what was installed (no secrets) for later repair tools: /etc/gentoo-live-installer/install.json."""
        def uuid_of(device: str | None) -> str:
            if self.dry_run or not device:
                return ""
            try:
                return self.runner.run(("blkid", "-s", "UUID", "-o", "value", device)).stdout.strip().splitlines()[0]
            except (RunnerError, IndexError):
                return ""

        root_entry = next((entry for entry in layout if entry.mountpoint == "/"), None)
        receipt = self.build_receipt(
            manifest, root_uuid=uuid_of(root_device), luks_uuid=uuid_of(luks_device) if manifest.storage.luks else "",
            root_filesystem=root_entry.filesystem if root_entry else "",
            boot_separate=any(entry.mountpoint == "/boot" for entry in layout), plan=plan, stack=stack)
        self.write(RECEIPT_PATH, json.dumps(receipt, indent=2) + "\n", 0o644)
        return receipt
