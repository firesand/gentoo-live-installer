"""PORT: the stage3 stage (download, verify, unpack)."""

from __future__ import annotations

from typing import Any

from gentoo_installer.mirrors import mirror_list
from gentoo_installer.stagechoice import effective_stage_variant

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
        # The chosen mirrors in order (else the one mirror), then the fallbacks: the first is tried first, the others when it fails.
        portage, system = self.manifest.portage, self.manifest.system
        servers = mirror_list(portage.mirror, portage.mirrors, portage.mirror_fallbacks)
        if servers[1:]:
            extra["fallback_mirrors"] = tuple(servers[1:])
        # The Desktop stage3 for a graphical system, the Standard one for Minimal (Handbook), unless the user chose Hardened or No multilib.
        variant = effective_stage_variant(system.stage_variant, system.desktop, system.init, system.multilib, system.stage_auto)
        if variant != system.stage_variant:
            self.log(f"Using the {variant} stage3 that matches the chosen desktop")
        self._stage3_archive = self.stage3.install(
            servers[0],
            variant,
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
