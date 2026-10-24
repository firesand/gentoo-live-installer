"""STOR: the storage, mounts and target (fstab) stages and the questions about the disk they answer."""

from __future__ import annotations

from pathlib import Path

from ..configure import ExistingStack, render_fstab
from ..runner import RunnerError
from .context import InstallError
from .plan import PlanMixin


class StorageStages(PlanMixin):
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

    def _root_device(self) -> str:
        return next(entry.device for entry in self.storage.layout if entry.mountpoint == "/")

    def _uuid_of(self, device: str) -> str:
        if self.dry_run or not device:
            return ""
        try:
            return self.runner.run(("blkid", "-s", "UUID", "-o", "value", device)).stdout.strip().splitlines()[0]
        except (RunnerError, IndexError):
            return ""

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

    def _blkid(self, tag: str, device: str) -> str:
        result = self.runner.run(("blkid", "-s", tag, "-o", "value", device))
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""

    def _write_fstab(self) -> None:
        storage = self.manifest.storage
        uuids: dict[str, str] = {}
        sources: dict[str, str] = {}
        tag = {"label": "LABEL", "partuuid": "PARTUUID"}.get(storage.fstab_ident, "")
        for entry in self.storage.layout:
            if entry.source or entry.device in uuids:
                continue  # a swap file is named by its path, not by the UUID mkswap gave it; one lookup per device
            value = self._blkid("UUID", entry.device)
            if value:
                uuids[entry.device] = value
            if tag:
                # A volume without a label or a partition identifier (an LVM volume, an encrypted mapping) keeps the filesystem UUID.
                found = self._blkid(tag, entry.device)
                if found:
                    sources[entry.device] = f"{tag}={found}"
        discard = bool(getattr(self._plan(), "scalars", {}).get("btrfs_discard_async"))
        text = render_fstab(self.storage.layout, uuids, btrfs_discard=discard, atime=storage.atime, tz_utc=not self.manifest.system.rtc_local,
                            boot_noauto=storage.boot_noauto, sources=sources)
        self.configurer.write("etc/fstab", text)

    def _verify_storage(self) -> bool:
        self.storage.restore(self.manifest, self.checkpoints.load_layout(), self.checkpoints.load_receipt())
        return True
