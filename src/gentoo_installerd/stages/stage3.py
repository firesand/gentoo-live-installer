"""PORT: the stage3 stage (download, verify, unpack)."""

from __future__ import annotations

from typing import Any

from .storage import StorageStages


class Stage3Stage(StorageStages):
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
