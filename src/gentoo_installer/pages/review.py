from __future__ import annotations

import functools
import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from .. import diagnostics, layout
from ..manifest import InstallManifest
from ..system_info import DiskInfo, SystemInfo, tools_for_selection
from ..widgets import Card, FlowLayout, Page, wrapped_label
from .hardware import CAPACITY_CODES, blocking_findings, hardware_plan, plan_lines
from .system import FAMILY_DESKTOP_SUPPORT, FAMILY_LABELS, UNVERIFIED_FAMILIES, stage_family

LOG = logging.getLogger("gentoo_installer.review")


def disk_description(disk: DiskInfo | None, path: str) -> str:
    """'/dev/sda (Samsung SSD 870, 500.0 GiB, serial …123456)' for the review and the dialogs."""
    if disk is None:
        return path
    return f"{disk.path} ({disk.model}, {disk.size_text}, {disk.identity_text})"


def required_confirmations(manifest: InstallManifest, info: SystemInfo | None) -> list[str]:
    """Device paths the user must type: the target disk, plus every other disk
    that a manual layout puts a partition on."""
    wanted = [manifest.storage.device]
    if manifest.storage.mode == "manual" and info is not None:
        for partition in manifest.storage.partitions:
            found = info.find_partition(partition.device)
            parent = found.parent if found is not None else ""
            if parent and parent not in wanted:
                wanted.append(parent)
    return wanted


def format_lines(manifest: InstallManifest, info: SystemInfo | None) -> list[str]:
    """One line per manual row: FORMAT or KEEP, where it goes, which disk it is on
    and what is on it today."""
    lines = []
    for partition in manifest.storage.partitions:
        found = info.find_partition(partition.device) if info is not None else None
        disk = info.find_disk(found.parent) if info is not None and found is not None else None
        target = "swap" if partition.filesystem == "swap" else (partition.mountpoint or "(not mounted)")
        where = f" on {disk.model} {disk.identity_text}" if disk is not None else ""
        now = f", now {found.describe()}" if found is not None else ""
        verb = "FORMAT" if partition.format else "KEEP  "
        lines.append(f"{verb}  {partition.device} → {target} ({partition.filesystem}){where}{now}")
    return lines


def disk_warning(manifest: InstallManifest, info: SystemInfo | None) -> str:
    storage = manifest.storage
    disk = info.find_disk(storage.device) if info is not None else None
    described = disk_description(disk, storage.device)
    if storage.mode == "erase":
        text = f"All existing data on {described} will be permanently erased"
        if disk is not None:
            text += f" (today: {disk.contents_text})"
        text += ". Other disks will not be modified."
        if disk is not None and disk.active_volumes:
            text += f" It currently holds active volumes ({'; '.join(disk.active_volumes)}) that will be removed with it."
        return text
    lines = format_lines(manifest, info)
    heading = f"Manual installation on {described}: only the partitions marked FORMAT are changed; KEEP partitions keep their data."
    return heading + ("\n" + "\n".join(lines) if lines else "")


def manifest_warnings(manifest: InstallManifest, info: SystemInfo | None = None) -> list[str]:
    """Things worth knowing that do not stop the installation."""
    extra = getattr(manifest, "warnings", None)
    warnings: list[str] = list(extra()) if callable(extra) else []
    system, options = manifest.system, manifest.options
    family = stage_family(system.stage_variant)
    if family in UNVERIFIED_FAMILIES and system.desktop in {"gnome", "xfce"} and system.desktop in FAMILY_DESKTOP_SUPPORT[family]:
        warnings.append(f"Not verified: {system.desktop.upper()} on a {FAMILY_LABELS[family]} stage3 may need extra USE changes during the installation.")
    if system.desktop == "minimal" and (options.install_hwmonux or options.install_portagehub):
        warnings.append("HWMonux and PortageHub are graphical applications and cannot start on a minimal system; they only add build time and disk space there.")
    if info is not None:
        warnings.extend(storage_capacity(manifest, info)[1])
        if manifest.storage.firmware == "bios" and info.uefi:
            warnings.append("Legacy BIOS was chosen on a computer started in UEFI mode: the installed system boots only through the firmware's CSM/legacy mode.")
        if manifest.storage.device_id_kind == "path":
            warnings.append("The target disk has no serial number; it is recognised by its hardware path, size and model.")
        for finding in info.warnings:
            if finding.get("severity") in {"warning", "error"} and finding.get("message"):
                warnings.append(str(finding["message"]))
    return list(dict.fromkeys(warnings))


def selection_problems(manifest: InstallManifest, info: SystemInfo | None) -> list[str]:
    """Blocking problems that only show with the final selection."""
    problems = []
    if info is not None:
        # Judged against the tool map of the last inspection with the *current* selection;
        # the daemon's own missing_for_selection was computed for whatever was chosen
        # when the inspection ran, so it is not trusted here.
        missing = [tool for tool in tools_for_selection(manifest) if info.tools.get(tool) is False]
        if missing:
            problems.append("The live system lacks tools that your choices need: " + ", ".join(sorted(set(missing))) + ". Change the filesystem, encryption or bootloader choice, or use a newer installer image.")
    return problems


@functools.lru_cache(maxsize=1)
def layout_features() -> tuple[bool, bool]:
    """(Btrfs subvolumes, swap file): what the daemon in this build can really create, so the preview does not promise more."""
    try:
        from gentoo_installerd import storage as daemon_storage

        probe = getattr(daemon_storage, "fstab_supports_layout_fields", None)
        if callable(probe):
            subvolumes, swap_file = probe()
            return bool(subvolumes), bool(swap_file)
    except Exception:  # noqa: BLE001 - a preview must never stop the review
        LOG.exception("Could not ask the installer service code which layouts it can create")
    return True, True


def storage_plan(manifest: InstallManifest, info: SystemInfo | None) -> layout.StoragePlan | None:
    """What the installation will do to every disk it touches (None when the plan cannot be built)."""
    subvolumes, swap_file = layout_features()
    try:
        return layout.plan_storage(manifest, info.block_devices() if info is not None else None, btrfs_subvolumes=subvolumes, swap_file=swap_file)
    except Exception:  # noqa: BLE001
        LOG.exception("The storage plan could not be built")
        return None


def storage_plan_text(manifest: InstallManifest, info: SystemInfo | None) -> str:
    plan = storage_plan(manifest, info)
    return "\n".join(plan.lines()) if plan is not None else ""


def storage_plan_details(manifest: InstallManifest, info: SystemInfo | None) -> list[str]:
    """The plan's lines for a confirmation dialog: without the first ERASE line the dialog already says in its own words."""
    plan = storage_plan(manifest, info)
    if plan is None:
        return []
    lines = plan.lines()
    return lines[1:] if manifest.storage.mode == "erase" and lines and lines[0].startswith("ERASE") else lines


def storage_capacity(manifest: InstallManifest, info: SystemInfo | None) -> tuple[list[str], list[str]]:
    """(errors, warnings) about the room the chosen desktop gets, with the daemon's own arithmetic (layout.capacity_problems)."""
    storage = manifest.storage
    if info is None:
        return [], []
    if storage.mode == "erase":
        disk = info.find_disk(storage.device)
        return layout.capacity_problems(manifest, disk.size, disk.log_sec) if disk is not None else ([], [])
    root = next((row for row in storage.partitions if row.mountpoint == "/"), None)
    part = info.find_partition(root.device) if root is not None else None
    if part is None:
        return [], []
    minimum, recommended = layout.minimum_root_mib(manifest) * layout.MIB, layout.recommended_root_mib(manifest) * layout.MIB
    desktop = "a minimal system" if manifest.system.desktop == "minimal" else manifest.system.desktop.upper()
    if part.size < minimum:
        return [f"The root partition {part.path} is {layout.human_size(part.size)}, but {desktop} needs at least {layout.human_size(minimum)}. "
                "Choose a larger partition or a lighter desktop."], []
    if part.size < recommended:
        return [], [f"The root partition {part.path} is {layout.human_size(part.size)}; {recommended // layout.GIB} GiB or more is recommended so updates and builds have room."]
    return [], []


def blocking_problems(manifest: InstallManifest, info: SystemInfo | None) -> list[tuple[str, str]]:
    """(section, message) of everything that stops the installation and only shows with the final selection."""
    found: list[tuple[str, str]] = [("storage", message) for message in selection_problems(manifest, info)]
    if info is not None:
        found += [("storage", message) for message in storage_capacity(manifest, info)[0]]
        for section in ("storage", "network", "hardware"):
            found += [(section, message) for message in blocking_findings(manifest, info.hardware, section, skip=CAPACITY_CODES)]
    unique: list[tuple[str, str]] = []
    for section, message in found:
        if all(message != other for _, other in unique):
            unique.append((section, message))
    return unique


def save_redacted_copy(manifest: InstallManifest, path: str | Path) -> None:
    """Write the settings without any secret (and without proxy credentials), mode 0600."""
    data = manifest.to_dict(redact=True)
    network = data.get("network", {})
    if isinstance(network.get("proxy"), str):
        network["proxy"] = diagnostics.redact_url_userinfo(network["proxy"])
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    target.chmod(0o600)


class ReviewPage(Page):
    exportRequested = Signal()
    importRequested = Signal()

    def __init__(self, manifest: InstallManifest, info: SystemInfo | None = None):
        super().__init__("Review your installation", "Nothing has been changed yet. Check your choices carefully—especially the target disk—before starting.")
        self.manifest = manifest
        self.info = info
        summary_card = Card("Installation summary")
        self.summary = QTableWidget(0, 2)
        self.summary.setHorizontalHeaderLabels(["Setting", "Selection"])
        self.summary.verticalHeader().setVisible(False)
        self.summary.horizontalHeader().setStretchLastSection(True)
        self.summary.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.summary.setWordWrap(True)
        self.summary.setMinimumHeight(350)
        summary_card.content.addWidget(self.summary)
        self.content.addWidget(summary_card)

        plan_card = Card("What will happen to your disks", "Nothing is changed before you confirm below.")
        self.storage_plan = wrapped_label("", selectable=True)
        self.storage_plan.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.storage_plan.setAccessibleName("Storage plan")
        plan_card.content.addWidget(self.storage_plan)
        self.content.addWidget(plan_card)
        self.hardware_card = Card("Set-up for this computer", "Chosen from the hardware that was found. Change it on the Hardware tab of the Customization step.")
        self.hardware_plan = wrapped_label("", selectable=True)
        self.hardware_plan.setAccessibleName("Hardware set-up")
        self.hardware_card.content.addWidget(self.hardware_plan)
        self.content.addWidget(self.hardware_card)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.warning.setStyleSheet("background: #fff1f2; color: #8d2636; border: 1px solid #f0c5cb; border-radius: 9px; padding: 14px;")
        self.content.addWidget(self.warning)
        self.notes = wrapped_label("", muted=True, selectable=True)
        self.notes.hide()
        self.content.addWidget(self.notes)
        self.resume_check = QCheckBox("Resume the interrupted installation instead of erasing the disk again")
        self.resume_check.setToolTip("An earlier installation stopped after the disk was prepared. Resuming needs exactly the same settings, and re-uses the disk as it is.")
        self.resume_check.hide()
        self.content.addWidget(self.resume_check)
        self.confirm_card = Card("Final confirmation", "Type the target device path exactly to enable installation. This is the last step before disk changes begin.")
        self.confirm_form = QFormLayout()
        self.confirmation = QLineEdit()
        self.confirmation.setClearButtonEnabled(True)
        self.confirmation.setAccessibleName("Target disk confirmation")
        self.confirm_form.addRow("Target disk", self.confirmation)
        self.extra_confirmations: list[tuple[str, QLineEdit]] = []
        self.confirm_card.content.addLayout(self.confirm_form)
        self.content.addWidget(self.confirm_card)
        buttons = FlowLayout()
        export = QPushButton("Save a redacted copy of these settings")
        export.clicked.connect(self._export)
        buttons.addWidget(export)
        importer = QPushButton("Import settings…")
        importer.clicked.connect(self.importRequested)
        buttons.addWidget(importer)
        self.content.addLayout(buttons)
        self.advance_on_enter(self.confirmation)

    def set_info(self, info: SystemInfo) -> None:
        self.info = info

    def _rebuild_confirmations(self, wanted: list[str]) -> None:
        for _, edit in self.extra_confirmations:
            self.confirm_form.removeRow(edit)
        self.extra_confirmations = []
        for path in wanted[1:]:
            edit = QLineEdit()
            edit.setPlaceholderText(path)
            edit.setAccessibleName(f"Confirmation for {path}")
            self.confirm_form.addRow(f"Also changes {path}", edit)
            self.extra_confirmations.append((path, edit))

    def load_manifest(self) -> None:
        storage = self.manifest.storage
        values = self.manifest.summary()
        disk = self.info.find_disk(storage.device) if self.info is not None else None
        rich = []
        for name, value in values:
            if name == "Disk" and disk is not None:
                value = f"{disk.describe()} · {storage.mode} · {storage.filesystem}"
            rich.append((name, value))
        if storage.mode == "erase":
            rich.append(("Swap", f"{storage.swap_mib} MiB" if storage.swap_mib else "None"))
        rich.append(("Region", f"{self.manifest.system.locale} · {self.manifest.system.timezone} · keyboard {self.manifest.system.keyboard_layout} (console {self.manifest.system.keymap})"))
        self.summary.setRowCount(len(rich))
        for row, (name, value) in enumerate(rich):
            self.summary.setItem(row, 0, QTableWidgetItem(name))
            self.summary.setItem(row, 1, QTableWidgetItem(value))
        self.summary.resizeRowsToContents()
        self.warning.setText(disk_warning(self.manifest, self.info))
        self.storage_plan.setText(storage_plan_text(self.manifest, self.info))
        hardware = self.info.hardware if self.info is not None else {}
        lines = plan_lines(hardware_plan(self.manifest, hardware)) if hardware else []
        self.hardware_plan.setText("\n".join("• " + line for line in lines))
        self.hardware_card.setVisible(bool(lines))
        notes = manifest_warnings(self.manifest, self.info)
        self.notes.setText("Good to know:\n• " + "\n• ".join(notes) if notes else "")
        self.notes.setVisible(bool(notes))
        wanted = required_confirmations(self.manifest, self.info)
        self.confirmation.setPlaceholderText(storage.device)
        self.confirmation.clear()
        self._rebuild_confirmations(wanted)
        daemon = self.info.daemon if self.info is not None else None
        resumable = bool(daemon is not None and daemon.reachable and daemon.status.get("resumable") and not daemon.job_running)
        self.resume_check.setVisible(resumable)
        self.resume_check.setChecked(False)

    def resume_requested(self) -> bool:
        return self.resume_check.isVisibleTo(self) and self.resume_check.isChecked()

    def validate_page(self) -> tuple[bool, str]:
        wanted = required_confirmations(self.manifest, self.info)
        typed = [self.confirmation.text().strip()] + [edit.text().strip() for _, edit in self.extra_confirmations]
        for expected, got in zip(wanted, typed + [""] * len(wanted)):
            if got != expected:
                return False, f"Type {expected} exactly to confirm the target disk."
        problems = blocking_problems(self.manifest, self.info)
        if problems:
            return False, problems[0][1]
        errors = self.manifest.validate()
        return (False, errors[0]) if errors else (True, "")

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save installation settings", "gentoo-installation.json", "JSON files (*.json)")
        if path:
            save_redacted_copy(self.manifest, path)
