from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from gentoo_installer.manifest import InstallManifest

from .configure import RECEIPT_PATH, Configurer, PackageInstallError, render_fstab
from .events import Event, EventSink
from .inspect import Discovery, Inspection
from .proxy import proxy_environment
from .runner import DryRunRunner, InstallCancelled, Runner, RunnerError, SubprocessRunner, redact_text
from .stage3 import Stage3Manager
from .storage import LayoutEntry, StorageManager


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


class InstallError(RuntimeError):
    pass


class CheckpointStore:
    def __init__(self, path: str | Path, fingerprint: str) -> None:
        self.path = Path(path)
        self.fingerprint = fingerprint
        self.completed: set[str] = set()

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

    def is_complete(self, stage: str, verifier: Callable[[], bool] | None = None) -> bool:
        return stage in self.completed and verifier is not None and verifier()

    def complete(self, stage: str) -> None:
        if stage not in STAGES or stage in {"preflight", "cleanup"}:
            return
        self.completed.add(stage)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "fingerprint": self.fingerprint, "completed": sorted(self.completed)}, indent=2) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)

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
            return [LayoutEntry(str(item["device"]), str(item["mountpoint"]), str(item["filesystem"]), bool(item["format"])) for item in raw["layout"]]
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
        self.secrets = (manifest.accounts.password, manifest.accounts.root_password, manifest.storage.luks_passphrase, manifest.network.wifi_password)
        self._log_file = None
        if not dry_run:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.state_dir.chmod(0o700)
            manifest.save(self.state_dir / "manifest.json", redact=True)
            self._log_file = (self.state_dir / "install.log").open("a", encoding="utf-8")
            self._log_file.write(json.dumps({"job_id": self.job_id, "event": "created"}) + "\n")
            self._log_file.flush()
        self.runner = runner or (DryRunRunner(self.log) if dry_run else SubprocessRunner(self.log, self.cancel, self.secrets))
        fingerprint = hashlib.sha256(manifest.to_json(redact=True).encode()).hexdigest()
        self.checkpoints = CheckpointStore(self.state_dir / "checkpoints.json", fingerprint)
        self.inspector = inspector or Discovery(self.runner)
        self.storage = storage or StorageManager(self.runner, self.target_root)
        self.stage3 = stage3 or Stage3Manager(self.runner, cache_dir, cancel=self.cancel)
        self.configurer = configurer or Configurer(self.runner, self.target_root, dry_run=dry_run)
        self.inspection: Inspection | None = None
        self._cleanup_done = False
        self._failed = False

    def emit(self, event_type: str, stage: str = "", message: str = "", progress: float | None = None, **data: object) -> None:
        error = str(data.pop("error", ""))
        event = Event(event_type, job_id=self.job_id, stage=stage, message=message, progress=progress, data={key: value for key, value in data.items() if value is not None}, error=error)
        self._event_sink(event)

    def log(self, message: str) -> None:
        clean = redact_text(message, self.secrets)
        self.emit("log", message=clean)
        if self._log_file and not self._log_file.closed:
            self._log_file.write(json.dumps({"job_id": self.job_id, "message": clean}) + "\n")
            self._log_file.flush()

    def run(self) -> bool:
        success = False
        cancelled = False
        try:
            if self.resume and not self.dry_run:
                self.checkpoints.load()
            self._execute()
            success = True
        except InstallCancelled as exc:
            cancelled = True
            self._failed = True
            self.emit("error", message=str(exc), error="cancelled", retryable=False)
        except Exception as exc:
            self._failed = True
            clean = redact_text(str(exc), self.secrets)
            self.log(f"Installation failed: {clean}")
            self.emit("error", message=clean, error=clean, retryable=True)
        finally:
            try:
                self._cleanup()
            except Exception as exc:
                success = False
                self._failed = True
                clean = redact_text(str(exc), self.secrets)
                self.emit("error", stage="cleanup", message=clean, error=clean, retryable=True)
            message = "Installation cancelled" if cancelled else "Installation completed successfully" if success else "Installation failed"
            self.emit("completed", message=message, progress=100.0 if success else None, success=success)
            if self._log_file:
                self._log_file.close()
        return success

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
        for index, (name, operation, verifier) in enumerate(steps):
            self._check_cancel()
            progress = index / (len(STAGES) - 1) * 100
            if self.resume and not self.dry_run and name == "storage":
                if "storage" not in self.checkpoints.completed:
                    raise InstallError("No completed storage checkpoint; resume will not recreate or format storage")
                self._verify_storage()
                self.emit("stage", name, "Restored verified storage without formatting", progress, status="skipped")
                continue
            if self.resume and self.checkpoints.is_complete(name, verifier):
                self.emit("stage", name, f"Verified checkpoint; skipping {name}", progress, status="skipped")
                continue
            self.emit("stage", name, f"Starting {name}", progress, status="running")
            operation()
            if not self.dry_run:
                self.checkpoints.complete(name)
            self.emit("stage", name, f"Completed {name}", (index + 1) / (len(STAGES) - 1) * 100, status="completed")

    def _preflight(self) -> None:
        if self.target_root.resolve() == Path("/") or not self.target_root.is_absolute():
            raise InstallError("Target root must be an absolute, non-root mount directory")
        if any(path.is_symlink() for path in (self.target_root, *self.target_root.parents)):
            raise InstallError("Target root must not contain symlinks")
        self.inspection = self.inspector.inspect()
        if self.manifest.storage.firmware == "uefi" and not self.dry_run and not self.inspection.uefi:
            raise InstallError("The live system was not booted in UEFI mode")
        fatal = [error for error in self.inspection.errors if not error.startswith("At least 2 GiB")]
        if fatal:
            raise InstallError("Preflight failed: " + "; ".join(fatal))
        selected_tools: set[str] = set()
        if self.manifest.storage.firmware == "uefi":
            selected_tools.add("mkfs.vfat")
        if self.manifest.storage.filesystem in {"xfs", "btrfs"}:
            selected_tools.add("mkfs." + self.manifest.storage.filesystem)
        for partition in self.manifest.storage.partitions:
            if partition.format and partition.filesystem in {"ext4", "xfs", "btrfs", "vfat"}:
                selected_tools.add("mkfs." + partition.filesystem)
        if self.manifest.storage.swap_mib or any(p.filesystem == "swap" for p in self.manifest.storage.partitions):
            selected_tools.update(("swapon", "swapoff"))
        if self.manifest.storage.luks:
            selected_tools.add("cryptsetup")
        if self.manifest.storage.lvm:
            selected_tools.update(("pvcreate", "vgcreate", "lvcreate", "pvs", "vgchange"))
        if self.manifest.storage.raid_level:
            selected_tools.add("mdadm")
        if self.manifest.system.secure_boot:
            selected_tools.update(("sbsign", "sbverify"))
        if self.manifest.system.bootloader == "efi-stub":
            selected_tools.add("efibootmgr")
        missing = sorted(tool for tool in selected_tools if self.inspection.tools.get(tool) is False)
        if missing:
            raise InstallError("Selected configuration needs missing tools: " + ", ".join(missing))
        if self.manifest.system.kernel == "manual":
            self.log("Manual kernel selected: installation will stop at the kernel stage with interactive instructions")
        self.emit("progress", "preflight", "System readiness checked", 5.0, inspection=self.inspection.to_dict())

    def _storage(self) -> None:
        layout = self.storage.prepare(self.manifest, dry_run=self.dry_run)
        if not self.dry_run:
            self.checkpoints.save_layout(layout, self.storage.snapshot())
        self.emit("progress", "storage", "Storage layout prepared", data={"layout": [entry.__dict__ for entry in layout]})

    # --- stage helpers -----------------------------------------------------------------

    @property
    def degraded(self) -> list[str]:
        """Optional pieces that could not be installed; the installation still finishes."""
        return self.__dict__.setdefault("_degraded", [])

    def _plan(self) -> object | None:
        """The hardware plan (gentoo_installerd.hwplan.HardwarePlan) that _preflight builds, if any."""
        return getattr(self, "hardware_plan", None)

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
        extra: dict[str, Any] = {"allowed_existing": [entry.mountpoint for entry in self.storage.layout if entry.mountpoint]}
        if self.manifest.network.proxy:
            extra["proxy"] = self.manifest.network.proxy
        if self.manifest.portage.mirror_fallbacks:
            extra["fallback_mirrors"] = tuple(self.manifest.portage.mirror_fallbacks)
        self.stage3.install(
            self.manifest.portage.mirror,
            self.manifest.system.stage_variant,
            self.target_root,
            lambda current, total: self.emit("progress", "stage3", "Downloading stage3", current * 100 / total if total else None, downloaded=current, total=total),
            **extra,
        )

    def _mounts(self) -> None:
        if not self.dry_run:
            for directory in ("dev", "proc", "sys", "run", "etc"):
                self.configurer.path(directory).mkdir(parents=True, exist_ok=True)
            resolv = Path("/etc/resolv.conf").read_text()
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
        self.configurer.configure_initramfs(self.manifest, root, self.storage._stack.get("luks_device"), plan)
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
        batch = self.configurer.kernel_stage_atoms(self.manifest, self.storage.layout, plan)
        if batch:
            self.configurer.emerge(self.manifest, ("--noreplace", *batch))
        if self.manifest.system.init == "systemd" and self.manifest.storage.luks:
            # systemd is already installed in its stage3, so --noreplace would
            # retain the build without systemd-cryptsetup in the initramfs.
            self.configurer.emerge(self.manifest, ("--update", "--newuse", "--oneshot", "sys-apps/systemd"))
            if not self.dry_run and not self.configurer.path("usr/lib/systemd/systemd-cryptsetup").is_file():
                raise InstallError("systemd-cryptsetup is missing after enabling systemd[cryptsetup]")
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

    def _target(self) -> None:
        self.configurer.notify = self.log
        self.configurer.configure_identity(self.manifest)
        self.configurer.configure_accounts(self.manifest)
        self.configurer.configure_network(self.manifest)
        uuids: dict[str, str] = {}
        for entry in self.storage.layout:
            result = self.runner.run(("blkid", "-s", "UUID", "-o", "value", entry.device))
            if result.stdout.strip():
                uuids[entry.device] = result.stdout.strip().splitlines()[0]
        self.configurer.write("etc/fstab", render_fstab(self.storage.layout, uuids))
        self.configurer.configure_privileges(self.manifest)

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
        self.configurer.write_receipt(self.manifest, root, self.storage.layout, self.storage._stack.get("luks_device"), self._plan())

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
        drift = self.configurer.world_drift(self.manifest)
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
        for path in ("etc/fstab", "etc/portage/make.conf"):
            cfg.chroot(("test", "-s", f"/{path}"))
        cfg.chroot(("getent", "passwd", self.manifest.accounts.username))
        problems = cfg.verify_boot_setup(self.manifest, self._root_device(), root_uuid=self._uuid_of(self._root_device()))
        modules = cfg.initramfs_modules()
        if modules is not None:
            for wanted, needed in (("crypt", self.manifest.storage.luks), ("lvm", self.manifest.storage.lvm)):
                if needed and wanted not in modules:
                    problems.append(f"The initramfs lacks the {wanted} module, so the root filesystem could not be opened at boot")
        if problems:
            raise InstallError("The installed system failed its final checks: " + "; ".join(problems))
        self._keep_install_log()
        if self.manifest.system.desktop != "minimal":
            self.log("Desktop packages installed; graphical session will be available after reboot")
        # Last: nothing after this point needs name resolution inside the target.
        cfg.finalize_network(self.manifest)

    def _keep_install_log(self) -> None:
        """Leave the (already redacted) log and manifest on the new system for later troubleshooting."""
        if self.dry_run:
            return
        for name in ("install.log", "manifest.json"):
            source = self.state_dir / name
            try:
                if source.is_file():
                    self.configurer.write_bytes(f"var/log/gentoo-installer/{name}", source.read_bytes(), 0o600)
            except OSError as exc:
                self.log(f"Could not keep {name} on the new system: {exc}")

    def _cleanup(self) -> None:
        if self._cleanup_done:
            return
        self.emit("stage", "cleanup", "Cleaning up installer-owned mounts", 99.0, status="running")
        self.storage.cleanup()
        self._cleanup_done = True
        self.emit("stage", "cleanup", "Cleanup completed", 100.0, status="completed")

    def _verify_storage(self) -> bool:
        self.storage.restore(self.manifest, self.checkpoints.load_layout(), self.checkpoints.load_receipt())
        return True

    def _verify_mounts(self) -> bool:
        try:
            paths = [self.configurer.path(name) for name in ("dev", "proc", "sys", "run")]
            for path in paths:
                result = self.runner.run(("findmnt", "--mountpoint", str(path), "--noheadings", "--output", "SOURCE"))
                if not result.stdout.strip():
                    return False
            for path in paths:
                if path not in self.storage.owned_mounts:
                    self.storage.owned_mounts.append(path)
            return True
        except Exception as exc:
            self.log(f"Virtual mount checkpoint not reusable: {exc}")
            return False

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
        if self.manifest.system.bootloader == "grub" and self.manifest.storage.firmware == "uefi" \
                and not (self._target_file(f"{efi}/EFI/Gentoo/grubx64.efi") or self._target_file(f"{efi}/EFI/BOOT/BOOTX64.EFI")):
            return False
        path = "boot/grub/grub.cfg" if self.manifest.system.bootloader == "grub" else f"{efi}/loader/entries/gentoo.conf"
        return self._target_file(path)

    def _target_file(self, path: str) -> bool:
        target = self.configurer.path(path)
        return self.dry_run or target.exists()

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise InstallCancelled("Installation cancelled")
