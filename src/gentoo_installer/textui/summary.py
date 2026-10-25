"""The summary shown before an installation starts (and by ``--dry-run``).

It joins everything that is known without touching a disk: the settings, what the hardware plan
decided for this computer (``hwplan.plan``), what will happen to every disk (``layout.plan_storage``),
and the problems and advice from the manifest, the hardware checks and the recommendations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import desktops, keymaps, layout, recommend
from ..diagnostics import redact_url_userinfo
from ..manifest import InstallManifest
from .console import Console
from .inventory import MachineView

MAX_ADVICE = 5
INIT_NAMES = {"openrc": "OpenRC", "systemd": "systemd"}
# Advice about choices the text installer does not offer.
ONLY_ELSEWHERE_FIELDS = frozenset({"hardware.gpu_driver", "hardware.prime", "hardware.cpu_flags_mode", "options.install_hwmonux"})
ONLY_ELSEWHERE = "(The text installer cannot change this: use the graphical installer or a settings file.)"
# HWMonux needs PySide6, and the binary host has no matching PySide6 package, so it is compiled on every installation.
HWMONUX_LOW_MEMORY = "HWMonux needs PySide6, which is compiled from source: about 40 minutes and up to 4 GiB of memory per compile job."
HWMONUX_MEMORY_GIB = 8  # a computer with less memory than this gets the note
HWMONUX_ADVICE = "rec.hwmonux_low_memory"  # the same advice from recommend.recommend(), when it gives one


@dataclass
class Summary:
    settings: list[tuple[str, str]] = field(default_factory=list)
    hardware: list[str] = field(default_factory=list)  # what the installer decided for this computer
    storage: list[str] = field(default_factory=list)  # what happens to every disk
    notes: list[str] = field(default_factory=list)  # worth knowing
    advice: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # these stop the installation

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "settings": [{"name": name, "value": value} for name, value in self.settings],
            "hardware_plan": self.hardware, "storage_plan": self.storage,
            "notes": self.notes, "advice": self.advice, "problems": self.problems,
        }


def keyboard_label(manifest: InstallManifest) -> str:
    system = manifest.system
    for preset in keymaps.PRESETS:
        if (preset.console, preset.layout, preset.variant) == (system.keymap, system.keyboard_layout, system.keyboard_variant):
            return preset.label
    variant = f" ({system.keyboard_variant})" if system.keyboard_variant else ""
    return f"{system.keyboard_layout}{variant}"


def hwmonux_note(manifest: InstallManifest, view: MachineView) -> str:
    """What the PySide6 build of HWMonux costs, for a computer with little memory that is going to get HWMonux ('' otherwise).

    The advice of recommend.recommend() is used when it gives one; otherwise the memory of this computer decides.
    It only informs: nothing here leaves HWMonux out."""
    if not manifest.options.install_hwmonux:
        return ""
    try:
        for item in recommend.recommend(view.hardware, manifest):
            if item.code == HWMONUX_ADVICE:
                return item.message
    except Exception:  # noqa: BLE001 - advice only
        pass
    return HWMONUX_LOW_MEMORY if 0 < view.memory_gib < HWMONUX_MEMORY_GIB else ""


def settings_rows(manifest: InstallManifest, view: MachineView) -> list[tuple[str, str]]:
    storage, system, accounts = manifest.storage, manifest.system, manifest.accounts
    entry = view.find_disk(storage.device) if view.roots else None
    disk = layout.disk_description(entry.disk) if entry is not None else storage.device
    if storage.device_id_kind == "path":
        disk += " - recognised by its hardware path, size and model"
    swap_mib = layout.effective_swap_mib(storage, int(view.memory_gib * 1024 * 1024))
    swap = f"{swap_mib} MiB swap" if swap_mib else "no swap"
    desktop = "no desktop (text mode)" if system.desktop == "minimal" else desktops.label(system.desktop)
    root = "root login turned off (use sudo)" if accounts.disable_root_login else "root has a password"
    return [
        ("Disk", f"{disk}. ALL DATA ON IT WILL BE ERASED." if storage.mode == "erase" else f"{disk}, your own partitions (manual mode)"),
        ("Boot mode", f"{'UEFI' if storage.firmware == 'uefi' else 'Legacy BIOS'}, {storage.table.upper()} partition table"),
        ("Filesystem", f"{storage.filesystem}, {swap}") if storage.mode == "erase" else ("Partitions", f"{len(storage.partitions)} chosen by you, listed below"),
        ("Encryption", storage.encryption_summary()),
        ("System", f"{INIT_NAMES.get(system.init, system.init)} with {desktop}; {'binary packages' if manifest.portage.binhost else 'built from source'}"),
        ("Boot loader", f"{system.bootloader}, kernel {system.kernel}"),
        ("Account", f"{accounts.username} on a computer named {accounts.hostname}; {root}"),
        ("Region", f"{system.timezone}, {system.locale}; keyboard {keyboard_label(manifest)} (console {system.keymap})"),
        ("Network", manifest.network.manager + (f", proxy {redact_url_userinfo(manifest.network.proxy)}" if manifest.network.proxy else "")),
    ]


def build_summary(manifest: InstallManifest, view: MachineView, *, require_identity: bool = False) -> Summary:
    """Everything worth reading before the installation starts. Never raises: an advisory part that fails is reported as a note.

    ``require_identity``: the disk must already be pinned to a serial number, WWN or approved hardware path (the daemon insists)."""
    summary = Summary(settings=settings_rows(manifest, view))
    problems, notes = summary.problems, summary.notes
    problems.extend(manifest.validate())
    if require_identity and manifest.storage.device and not manifest.storage.device_id:
        problems.append(f"{manifest.storage.device} is not identified yet: it needs a serial number, a WWN or your approval of its hardware path. Go back to the disk step and choose it again.")
    extra = getattr(manifest, "warnings", None)  # InstallManifest.warnings() (contract I4) may not exist yet
    if callable(extra):
        notes.extend(str(text) for text in extra())
    # What the hardware plan decided, and the cross-checks between the settings and this computer.
    try:
        from gentoo_installerd import hwplan  # pure module (no daemon-only imports)

        plan = hwplan.plan(manifest, view.hardware)
        summary.hardware = list(plan.summary)
        notes.extend(issue.message for issue in plan.warnings)
        blocking = hwplan.blocking_issues(manifest, view.hardware)
        problems.extend(issue.message for issue in blocking)
        blocked = {issue.message for issue in blocking}
        notes.extend(issue.message for issue in hwplan.validate_plan(manifest, view.hardware) if issue.message not in blocked)
    except Exception as exc:  # noqa: BLE001 - the preview is advice, it must never stop the wizard
        notes.append(f"The hardware plan could not be worked out here ({type(exc).__name__}: {exc}). The installer service will work it out again before it starts.")
    # What happens to each disk.
    try:
        plan_of_storage = layout.plan_storage(manifest, view.roots)
        summary.storage = [line for line in plan_of_storage.lines() if not line.startswith(("Warning: ", "Problem: "))]
        problems.extend(plan_of_storage.errors)
        notes.extend(plan_of_storage.warnings)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"The disk plan could not be worked out here ({type(exc).__name__}: {exc}).")
    missing = view.missing_tools(manifest)
    if missing:
        problems.append("The live system lacks programs that these settings need: " + ", ".join(missing) + ". Change the filesystem or encryption choice, or use a newer installer disc.")
    problems.extend(finding.message for finding in view.blocking)
    for finding in view.findings:
        if finding.severity == "warning":
            notes.append(finding.message)
    try:
        given = recommend.recommend(view.hardware, manifest)[:MAX_ADVICE]
    except Exception:  # noqa: BLE001
        given = []
    summary.advice = [item.message + (f" {ONLY_ELSEWHERE}" if item.field in ONLY_ELSEWHERE_FIELDS else "") for item in given]
    note = hwmonux_note(manifest, view)
    if note and note not in [item.message for item in given]:  # said once: the advice may already carry it
        notes.append(note)
    summary.problems = list(dict.fromkeys(problems))
    summary.notes = [note for note in dict.fromkeys(notes) if note not in summary.problems]
    return summary


def render_summary(console: Console, summary: Summary) -> None:
    """Print the summary: settings, hardware plan, disk plan, then problems and notes."""
    lines: list[str] = []

    def add(text: str = "", *, indent: int = 0, hanging: int | None = None) -> None:
        lines.extend(console.wrap(text, indent=indent, hanging=hanging))

    def heading(text: str, *styles: str) -> None:
        lines.append(console.style(text, *styles))

    for name, value in summary.settings:
        lines.extend(console.kv_lines(name, value, label_width=12))
    if summary.hardware:
        add()
        heading("Chosen for this computer:", "bold")
        for item in summary.hardware:
            add(f"- {item}", indent=2, hanging=4)
    if summary.storage:
        add()
        heading("What will happen to the disks:", "bold")
        for item in summary.storage:
            add(item, indent=2, hanging=9)
    if summary.advice:
        add()
        heading("Advice for this computer:", "bold")
        for item in summary.advice:
            add(f"- {item}", indent=2, hanging=4)
    if summary.notes:
        add()
        heading("Good to know:", "bold")
        for item in summary.notes:
            add(f"- {item}", indent=2, hanging=4)
    if summary.problems:
        add()
        heading("These problems stop the installation until they are fixed:", "red", "bold")
        for item in summary.problems:
            add(f"- {item}", indent=2, hanging=4)
    console.show_lines(lines)
