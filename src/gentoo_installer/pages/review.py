from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import desktops, diagnostics, layout
from ..manifest import COMPANIONS_ON_MINIMAL, InstallManifest
from ..system_info import DiskInfo, SystemInfo, tools_for_selection
from ..widgets import Card, FlowLayout, Page, wrapped_label
from .boot_section import firmware_secure_boot
from .help import AdvancedSection
from .hardware import CAPACITY_CODES, blocking_findings, hardware_plan, plan_lines
from .system import FAMILY_DESKTOP_SUPPORT, FAMILY_LABELS, UNVERIFIED_FAMILIES, stage_family

LOG = logging.getLogger("gentoo_installer.review")
BOOT_LOADER_NAMES = {"grub": "GRUB", "systemd-boot": "systemd-boot", "efi-stub": "the EFI stub", "none": "your own boot loader"}
# The disc itself starts with Secure Boot on (shim and its own key, iso/sign-secure-boot.py); the system it installs does not, unless it is signed.
SECURE_BOOT_UNSIGNED = ("Secure Boot is on in this PC, and the new system starts with {loader}, which is not signed: the PC will refuse to start it. "
                        "Turn Secure Boot off in the firmware setup before the first start of the new system, "
                        "or go back to Base system and choose systemd-boot with Secure Boot signing.")


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
    if desktops.unverified(system.desktop, family):
        warnings.append(f"Not verified: {system.desktop.upper()} on a {FAMILY_LABELS[family]} stage3 may need extra USE changes during the installation.")
    if system.desktop == "minimal" and options.companions():
        warnings.append(COMPANIONS_ON_MINIMAL)
    if info is not None:
        warnings.extend(storage_capacity(manifest, info)[1])
        if manifest.storage.firmware == "bios" and info.uefi:
            warnings.append("Legacy BIOS was chosen on a computer started in UEFI mode: the installed system boots only through the firmware's CSM/legacy mode.")
        if manifest.storage.firmware == "uefi" and not system.secure_boot and firmware_secure_boot(info) == "enabled":
            warnings.append(SECURE_BOOT_UNSIGNED.format(loader=BOOT_LOADER_NAMES.get(system.bootloader, system.bootloader)))
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


def graphics_driver_text(manifest: InstallManifest, info: SystemInfo | None) -> str:
    """The graphics driver the installation will use, as the planner decided it (what was asked for when nothing was detected)."""
    plan = hardware_plan(manifest, info.hardware) if info is not None and info.hardware else None
    if plan is None:
        return "NVIDIA proprietary driver" if manifest.hardware.gpu_driver == "nvidia" else "Open-source driver"
    if plan.scalars.get("gpu_driver") == "nvidia":
        return f"NVIDIA proprietary driver {plan.scalars.get('nvidia_branch', '')}".strip()
    if manifest.hardware.gpu_driver == "nvidia":
        return "Open-source driver (the NVIDIA driver does not fit this computer or these settings)"
    return "Open-source driver"


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


# --- plain names for the summary ---------------------------------------------------------------------------------

INIT_NAMES = {"openrc": "OpenRC", "systemd": "systemd"}
KERNEL_NAMES = {
    "gentoo-kernel-bin": "Gentoo prebuilt kernel",
    "gentoo-kernel": "Gentoo kernel, built on this computer",
    "manual": "No kernel (you install one by hand)",
    "custom": "Your own kernel configuration",
}
INITRAMFS_NAMES = {"dracut": "Dracut", "ugrd": "ugrd", "none": "None"}
BOOTLOADER_NAMES = {
    "grub": "GRUB", "systemd-boot": "systemd-boot", "refind": "rEFInd", "limine": "Limine",
    "efi-stub": "Direct EFI boot entry", "none": "None (you set up a boot loader yourself)",
}
SECURE_BOOT_NAMES = {"off": "Off", "own-key": "On, signed with your own key", "create": "On, a new key is created for you", "shim": "On, through shim"}
PRIVILEGE_NAMES = {"sudo": "sudo", "doas": "doas", "run0": "run0", "su": "su (the root password)"}
FILESYSTEM_TOOLS = {"ext4": "e2fsprogs", "xfs": "xfsprogs", "btrfs": "btrfs-progs", "f2fs": "f2fs-tools", "vfat": "dosfstools"}

STEP_STORAGE, STEP_SYSTEM, STEP_CUSTOM, STEP_ACCOUNTS, STEP_OTHER = "storage", "system", "customization", "accounts", "other"
# Labels of manifest.summary() that core_rows() covers with plainer words; anything else it returns is listed under "Other settings".
LEGACY_SUMMARY_LABELS = {"Disk", "Firmware", "Encryption", "Base", "Desktop", "Kernel", "Bootloader", "Portage", "Network", "Account", "Companion apps"}


@dataclass(frozen=True)
class ReviewSource:
    """One wizard step the review lists: its key (where core_rows puts rows), its place in the wizard and the page whose summary_rows() it asks."""

    key: str
    index: int
    title: str
    page: QWidget | None = None


def filesystem_tools(manifest: InstallManifest) -> str:
    storage = manifest.storage
    names = {storage.filesystem} if storage.mode == "erase" else {row.filesystem for row in storage.partitions if row.filesystem not in {"swap", "none"}}
    if storage.firmware == "uefi":
        names.add("vfat")
    if storage.luks:
        names.add("ext4")  # the separate /boot
    packages = sorted({FILESYSTEM_TOOLS[name] for name in names if name in FILESYSTEM_TOOLS})
    if storage.luks:
        packages.append("cryptsetup")
    if storage.lvm:
        packages.append("lvm2")
    if storage.raid_level:
        packages.append("mdadm")
    return ", ".join(packages)


def swap_text(manifest: InstallManifest, info: SystemInfo | None = None) -> str:
    """The swap the installation really gets: a "recommended" or "hibernate" choice is sized from this PC's memory (STOR-04)."""
    storage = manifest.storage
    if storage.mode != "erase":
        return "As set in your partition rows"
    kib = int(info.memory_gib * 1024 * 1024) if info is not None else 0
    swap = layout.effective_swap_mib(storage, kib)
    if swap:
        where = "a swap file" if layout.swap_in_root(layout.resolve_swap(storage, kib)) else "a swap partition"
        return f"{swap:,} MiB ({where})"
    if storage.swap_kind == "zram" and storage.swap_choice != "none":
        return "Compressed memory (zram), no swap on the disk"
    return "None"


def root_login_text(manifest: InstallManifest) -> str:
    accounts = manifest.accounts
    if accounts.root_locked():
        return "Locked: nobody logs in as root; use " + PRIVILEGE_NAMES.get(accounts.privilege, accounts.privilege) + " from your account"
    return "Uses your own password" if accounts.root_mode == "same" else "Has a password of its own"


def hardware_lines(manifest: InstallManifest, info: SystemInfo | None) -> list[tuple[str, str]]:
    """Microcode, firmware and sound-firmware lines from the hardware plan (nothing without detection data)."""
    plan = hardware_plan(manifest, info.hardware) if info is not None and info.hardware else None
    if plan is None:
        return []
    scalars = plan.scalars
    rows: list[tuple[str, str]] = []
    microcode = scalars.get("microcode")
    if microcode:
        rows.append(("CPU microcode", {"intel": "Intel", "amd": "AMD", "both": "Intel and AMD", "none": "Not installed"}.get(str(microcode), str(microcode))))
    rows.append(("Hardware firmware", "Installed (linux-firmware)" if manifest.options.firmware or scalars.get("firmware_forced") else "Not installed"))
    if scalars.get("sof_firmware") in ("yes", "no"):
        rows.append(("Sound firmware (SOF)", "Installed" if scalars["sof_firmware"] == "yes" else "Not needed"))
    return rows


def core_rows(manifest: InstallManifest, info: SystemInfo | None = None) -> dict[str, list[tuple[str, str]]]:
    """The settings every installation has, in plain words, grouped by the wizard step that changes them."""
    storage, system, accounts, options, portage = manifest.storage, manifest.system, manifest.accounts, manifest.options, manifest.portage
    disk = info.find_disk(storage.device) if info is not None else None
    disk_text = f"{disk.describe() if disk is not None else storage.device} · {storage.mode} · {storage.filesystem}"
    rows: dict[str, list[tuple[str, str]]] = {STEP_STORAGE: [], STEP_SYSTEM: [], STEP_CUSTOM: [], STEP_ACCOUNTS: [], STEP_OTHER: []}
    rows[STEP_STORAGE] += [
        ("Disk", disk_text),
        ("Boot mode", "UEFI" if storage.firmware == "uefi" else "Legacy BIOS"),
        ("Encryption", storage.encryption_summary()),
        ("Swap", swap_text(manifest, info)),
    ]
    tools = filesystem_tools(manifest)
    if tools:
        rows[STEP_STORAGE].append(("Filesystem tools", tools))
    rows[STEP_STORAGE].append(("Installed for", "A portable disk (other computers)" if manifest.hardware.target == "portable" else "This computer"))
    boot = BOOTLOADER_NAMES.get(system.bootloader, system.bootloader) + (" (other systems are added to the menu)" if system.dual_boot and system.bootloader == "grub" else "")
    rows[STEP_SYSTEM] += [
        ("Init system", INIT_NAMES.get(system.init, system.init)),
        ("Base", f"{system.stage_variant} · {manifest.effective_profile()}"),
        ("Kernel", KERNEL_NAMES.get(system.kernel, system.kernel)
         + ({"newest": " · newest series", "longterm": " · long-term series"}.get(system.kernel_series, "")
            if system.kernel in {"gentoo-kernel-bin", "gentoo-kernel"} else "")),
        ("Initramfs", INITRAMFS_NAMES.get(system.initramfs, system.initramfs)),
        ("Desktop", system.desktop.upper() if system.desktop != "minimal" else "Minimal (no desktop)"),
        ("Boot menu", boot),
        ("Secure Boot", SECURE_BOOT_NAMES.get(system.secure_boot_mode, system.secure_boot_mode)),
    ]
    companions = ", ".join(options.companions())
    rows[STEP_CUSTOM] += [
        ("Graphics driver", graphics_driver_text(manifest, info)),
        *hardware_lines(manifest, info),
        ("Software source", "Binary packages preferred" if portage.binhost else "Build from source"),
        ("Network", {"networkmanager": "NetworkManager", "dhcpcd": "dhcpcd", "systemd-networkd": "systemd-networkd"}.get(manifest.network.manager, manifest.network.manager)),
        ("Remote login (SSH)", "On" if options.ssh else "Off"),
        ("Companion apps", companions or "Not installed"),
    ]
    variant = f" ({system.keyboard_variant})" if system.keyboard_variant else ""
    rows[STEP_ACCOUNTS] += [
        ("Account", f"{accounts.username}@{accounts.hostname}"),
        ("Administrator tool", PRIVILEGE_NAMES.get(accounts.privilege, accounts.privilege)),
        ("Groups", ", ".join(accounts.groups) or "None"),
        ("Root login", root_login_text(manifest)),
        ("Language", system.locale),
        ("Time zone", system.timezone),
        ("Keyboard", f"{system.keyboard_layout}{variant} · console {system.keymap}"),
    ]
    rows[STEP_OTHER] += [(label, value) for label, value in manifest.summary() if label not in LEGACY_SUMMARY_LABELS]
    return rows


def preview_texts(manifest: InstallManifest, info: SystemInfo | None) -> list[tuple[str, str]]:
    """(title, text) of the read-only file previews; empty when the planner or the daemon code is not available."""
    try:
        from gentoo_installerd import preview
        from gentoo_installerd.configure import preview_files

        plan = hardware_plan(manifest, info.hardware) if info is not None and info.hardware else None
        files = preview_files(manifest, plan, storage_plan=storage_plan(manifest, info), hardware=info.hardware if info is not None else None)
    except Exception:  # noqa: BLE001 - a preview must never stop the review
        LOG.exception("The file previews could not be built")
        return []
    groups: list[tuple[str, list[str]]] = [
        (preview.PARTITIONS, [preview.PARTITIONS]),
        ("/etc/fstab", [preview.FSTAB]),
        ("make.conf and package.use", [preview.MAKE_CONF, preview.PACKAGE_USE, preview.PACKAGE_USE_HARDWARE]),
        ("binrepos.conf", [preview.BINREPOS]),
    ]
    result = []
    for title, names in groups:
        present = [name for name in names if name in files]
        if not present:
            continue
        if len(present) == 1:
            result.append((title, files[present[0]]))
        else:
            result.append((title, "\n".join(f"# ===== {name} =====\n{files[name]}" for name in present)))
    return result


class ReviewPage(Page):
    exportRequested = Signal()
    importRequested = Signal()
    editRequested = Signal(int)  # the wizard step (index) whose settings the user wants to change
    readyChanged = Signal(bool)  # the typed disk names now match (True) or no longer do

    START_HINT = "Type the disk name to enable"

    def __init__(self, manifest: InstallManifest, info: SystemInfo | None = None):
        super().__init__("Review your installation", "Nothing has been changed yet. Check your choices carefully—especially the target disk—before starting.")
        self.manifest = manifest
        self.info = info
        self.sources: list[ReviewSource] = []
        self._rows: list[tuple[str, str, str]] = []
        self._ready = False
        # Directly under the title: what will be erased, and the typed confirmation that unlocks Start.
        self.warning = QLabel()
        self.warning.setObjectName("eraseWarning")
        self.warning.setWordWrap(True)
        self.warning.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.warning.setStyleSheet("background: #fff1f2; color: #8d2636; border: 1px solid #f0c5cb; border-radius: 9px; padding: 14px;")
        self.content.addWidget(self.warning)
        self.confirm_card = Card("Final confirmation", "Type the target device path exactly to enable installation. This is the last step before disk changes begin.")
        self.confirm_form = QFormLayout()
        self.confirmation = QLineEdit()
        self.confirmation.setClearButtonEnabled(True)
        self.confirmation.setAccessibleName("Target disk confirmation")
        self.confirm_form.addRow("Target disk", self.confirmation)
        self.extra_confirmations: list[tuple[str, QLineEdit]] = []
        self.confirm_card.content.addLayout(self.confirm_form)
        self.content.addWidget(self.confirm_card)
        self.confirmation.textChanged.connect(self._confirmation_edited)
        self.resume_check = QCheckBox("Resume the interrupted installation instead of erasing the disk again")
        self.resume_check.setToolTip("An earlier installation stopped after the disk was prepared. Resuming needs exactly the same settings, and re-uses the disk as it is.")
        self.resume_check.hide()
        self.content.addWidget(self.resume_check)

        # One card per wizard step, each with an Edit link; no scroll area of its own (the page scrolls).
        self.groups = QVBoxLayout()
        self.groups.setSpacing(14)
        self.content.addLayout(self.groups)

        plan_card = Card("What will happen to your disks", "Nothing is changed before you confirm above.")
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
        self.notes = wrapped_label("", muted=True, selectable=True)
        self.notes.hide()
        self.content.addWidget(self.notes)

        self.previews_box = QVBoxLayout()
        self.previews_box.setSpacing(6)
        self.content.addLayout(self.previews_box)
        self.previews: list[AdvancedSection] = []
        self._advanced = False

        buttons = FlowLayout()
        export = QPushButton("Save a redacted copy of these settings")
        export.clicked.connect(self._export)
        buttons.addWidget(export)
        importer = QPushButton("Import settings…")
        importer.clicked.connect(self.importRequested)
        buttons.addWidget(importer)
        self.content.addLayout(buttons)
        self.advance_on_enter(self.confirmation)

    # --- the Review protocol (docs/contracts.md) ---------------------------------------------------------------

    def set_sources(self, sources: list[ReviewSource]) -> None:
        """The wizard steps to list, in order; each page's summary_rows() is asked when the review loads."""
        self.sources = list(sources)

    def rows(self) -> list[tuple[str, str, str]]:
        """[(step title, label, value)] as the review shows them (a label that a page and the core both give is listed once)."""
        return list(self._rows)

    def _collect_rows(self) -> list[tuple[str, str, str]]:
        core = core_rows(self.manifest, self.info)
        collected: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for source in self.sources:
            own = list(source.page.summary_rows()) if isinstance(source.page, Page) else []
            overridden = {label for label, _ in own}
            merged = [(label, value) for label, value in core.get(source.key, []) if label not in overridden] + own
            for label, value in merged:
                if label in seen:
                    continue
                seen.add(label)
                collected.append((source.title, label, value))
        collected += [("Other settings", label, value) for label, value in core[STEP_OTHER] if label not in seen]
        return collected

    def _rebuild_groups(self) -> None:
        while self.groups.count():
            item = self.groups.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        by_title: dict[str, list[tuple[str, str]]] = {}
        for title, label, value in self._rows:
            by_title.setdefault(title, []).append((label, value))
        index_of = {source.title: source.index for source in self.sources}
        for title, entries in by_title.items():
            card = Card()
            card.setAccessibleName(title)
            header = QHBoxLayout()
            heading = QLabel(title)
            heading.setObjectName("cardTitle")
            header.addWidget(heading, 1)
            if title in index_of:
                edit = QPushButton("Edit")
                edit.setObjectName("linkButton")
                edit.setAccessibleName(f"Edit {title}")
                edit.clicked.connect(lambda _=False, step=index_of[title]: self.editRequested.emit(step))
                header.addWidget(edit)
            card.content.addLayout(header)
            grid = QGridLayout()
            grid.setHorizontalSpacing(18)
            grid.setVerticalSpacing(6)
            grid.setColumnStretch(1, 1)
            for row, (label, value) in enumerate(entries):
                name = QLabel(label)
                name.setProperty("muted", True)
                grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
                grid.addWidget(wrapped_label(value, selectable=True), row, 1)
            card.content.addLayout(grid)
            self.groups.addWidget(card)

    def _rebuild_previews(self) -> None:
        for section in self.previews:
            section.hide()
            self.previews_box.removeWidget(section)
            section.setParent(None)
            section.deleteLater()
        self.previews = []
        for title, text in preview_texts(self.manifest, self.info):
            section = AdvancedSection("review", title)
            body = wrapped_label(text, selectable=True)
            body.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
            body.setAccessibleName(f"{title} (read only)")
            section.add_widget(body)
            section.set_advanced(self._advanced)
            self.previews_box.addWidget(section)
            self.previews.append(section)

    def set_advanced(self, advanced: bool) -> None:
        self._advanced = advanced
        for section in self.previews:
            section.set_advanced(advanced)

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
            edit.textChanged.connect(self._confirmation_edited)
            self.confirm_form.addRow(f"Also changes {path}", edit)
            self.extra_confirmations.append((path, edit))

    def confirmation_matches(self) -> bool:
        """The target disk (and every other disk a manual layout changes) was typed exactly."""
        wanted = required_confirmations(self.manifest, self.info)
        typed = [self.confirmation.text().strip()] + [edit.text().strip() for _, edit in self.extra_confirmations]
        return len(typed) >= len(wanted) and all(got == expected for expected, got in zip(wanted, typed))

    def _confirmation_edited(self) -> None:
        ready = self.confirmation_matches()
        if ready != self._ready:
            self._ready = ready
            self.readyChanged.emit(ready)

    def load_manifest(self) -> None:
        storage = self.manifest.storage
        self._rows = self._collect_rows()
        self._rebuild_groups()
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
        self._confirmation_edited()
        daemon = self.info.daemon if self.info is not None else None
        resumable = bool(daemon is not None and daemon.reachable and daemon.status.get("resumable") and not daemon.job_running)
        self.resume_check.setVisible(resumable)
        self.resume_check.setChecked(False)
        self._rebuild_previews()


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
