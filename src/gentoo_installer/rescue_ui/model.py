"""What the repair window knows about the engine's payloads (no Qt, no sockets).

Everything here is a pure function of the dictionaries the ``rescue.*`` actions return (docs/protocol.md,
``gentoo_installerd.rescue``): how a system is described to a person, how findings and actions are ordered and grouped, what a
plan's confirmation block demands, how a running plan is followed from its events, and how a terminal is launched. The widgets
only draw what is computed here, so the rules are tested without a display.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from ..layout import human_size

BACKUP_PHRASE = "I HAVE A BACKUP"  # the words a dangerous plan (and a read-write terminal) asks for; the engine checks the same text
SEVERITY_RANK = {"error": 0, "warn": 1, "info": 2}
LOADER_NAMES = {"grub": "GRUB", "systemd-boot": "systemd-boot", "refind": "rEFInd", "limine": "Limine", "syslinux": "Syslinux", "lilo": "LILO"}
MOUNT_KINDS = {"root": "The system", "boot": "Boot files", "system": "System folders", "data": "Your data (optional)"}

# The groups of the repair page, in the order a person should think about them (what the engine calls an action id on the right).
ACTION_GROUPS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("boot", "Boot loader", "The boot loader is the first program that starts. If it is damaged, the computer does not start.",
     ("boot.bootloader", "boot.config", "boot.esp_fallback")),
    ("startup", "Startup files", "The kernel and the initramfs (startup files) are loaded by the boot loader. The system cannot start without them.",
     ("boot.initramfs", "boot.kernel")),
    ("fstab", "Disk list", "The file /etc/fstab tells the system which disks to open when it starts.", ("fstab.fix",)),
    ("efi", "UEFI boot entries", "The firmware of a UEFI computer keeps its own list of systems to start.",
     ("efi.add", "efi.delete", "efi.order", "efi.next")),
    ("filesystem", "File system check", "Looks for errors in the file system. A check changes nothing.", ("fs.check",)),
    ("accounts", "Passwords and accounts", "Set a new password or unlock an account that cannot log in.", ("acct.password", "acct.unlock")),
    ("packages", "Software packages", "Finish what Portage was doing when the computer stopped.", ("pkg.finish",)),
    ("advanced", "Advanced", "Switch between looking only and being allowed to change files.", ("session.enter_write", "session.leave_write")),
)
RECOMMENDED_ACTION = "repair.recommended"
# Actions that always ask for options before the plan is made (the others use the engine's defaults).
FORM_FIRST = frozenset({"fs.check"})


def event_data(event: dict[str, Any]) -> dict[str, Any]:
    """The ``data`` object of an event ({} when it has none)."""
    value = event.get("data")
    return value if isinstance(value, dict) else {}


def error_details(data: dict[str, Any]) -> dict[str, Any]:
    """The ``details`` object of an error event's data ({} when there is none)."""
    value = data.get("details")
    return value if isinstance(value, dict) else {}


# ---- systems found by the scan ---------------------------------------------------------------------------------------------------
def os_name(info: dict[str, Any]) -> str:
    """'Gentoo Linux' (a rolling release), or the name another system gives itself, such as 'Debian GNU/Linux 12 (bookworm)'; the version is added when the name lacks one."""
    name = str(info.get("pretty_name") or info.get("name") or info.get("id") or "Linux")
    version = str(info.get("version_id") or "")
    if version and version not in name and not any(char.isdigit() for char in name):
        name = f"{name} {version}"
    return name


def split_candidates(found: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(the systems that can be opened, the other systems that are only named).

    An engine that says nothing about support opens every system, as it always did. A snapshot of a system that is not Gentoo adds nothing to the
    list of the other systems: the system itself is named already.
    """
    openable = [item for item in found if item.get("supported", True) is not False]
    others = [item for item in found if item.get("supported", True) is False and item.get("kind") != "snapshot"]
    return openable, others


def parent_disk(devices: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    """The disk that holds a partition. lsblk names make this a prefix match: sda2 -> sda, nvme0n1p5 -> nvme0n1, mmcblk0p1 -> mmcblk0."""
    name = path.rsplit("/", 1)[-1]
    best: dict[str, Any] | None = None
    for node in devices:
        if node.get("type") != "disk":
            continue
        disk_name = str(node.get("path", "")).rsplit("/", 1)[-1]
        if disk_name and name != disk_name and name.startswith(disk_name) and re.fullmatch(r"p?\d+", name[len(disk_name):]):
            if best is None or len(disk_name) > len(str(best["path"])):
                best = node
    return best


def find_device(devices: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    return next((node for node in devices if node.get("path") == path), None)


def stack_kinds(candidate: dict[str, Any]) -> set[str]:
    """Which layers lie between the disk and the file system: 'luks', 'lvm', 'md' (the scan labels them 'luks:/dev/mapper/..')."""
    kinds = set()
    for label in candidate.get("stack", []):
        prefix = str(label).split(":", 1)[0]
        if prefix in {"luks", "lvm", "md"}:
            kinds.add(prefix)
    return kinds


@dataclass
class SystemRow:
    """One installed system as a person should see it."""

    id: str
    title: str
    lines: list[str]
    warnings: list[str]
    snapshot: bool = False
    encrypted: bool = False
    device: str = ""
    supported: bool = True  # False: not a Gentoo system, listed but never opened


def describe_system(candidate: dict[str, Any], devices: list[dict[str, Any]]) -> SystemRow:
    os_info = candidate.get("os") or {}
    device = str(candidate.get("device", ""))
    stack = candidate.get("stack") or [device]
    bottom = str(stack[0]) if stack else device
    disk = parent_disk(devices, bottom)
    part = find_device(devices, bottom)
    where = []
    if disk is not None:
        model = str(disk.get("model") or "").strip() or "Disk"
        where.append(f"On the disk {model} ({human_size(int(disk.get('size') or 0))})")
    else:
        where.append("On disk")
    where.append(f"partition {bottom}" + (f" ({human_size(int(part['size']))})" if part and part.get("size") else ""))
    kinds = stack_kinds(candidate)
    facts = [f"File system: {candidate.get('fstype') or 'unknown'}", "Encrypted (LUKS)" if "luks" in kinds else "Not encrypted"]
    if "lvm" in kinds:
        facts.append("LVM volume")
    if "md" in kinds:
        facts.append("RAID array")
    boot = candidate.get("boot") or {}
    loader = str(boot.get("loader") or "")
    if loader:
        facts.append("Boot loader: " + LOADER_NAMES.get(loader, loader))
    kernels = [item for item in boot.get("kernels", []) if item.get("image")]
    facts.append(f"{len(kernels)} kernel{'s' if len(kernels) != 1 else ''}")
    snapshot = candidate.get("kind") == "snapshot"
    if snapshot:
        facts.append("Snapshot (read-only history)")
    return SystemRow(
        id=str(candidate.get("id", "")), title=os_name(os_info), lines=[", ".join(where), " · ".join(facts)],
        warnings=[str(item) for item in candidate.get("warnings", [])], snapshot=snapshot, encrypted="luks" in kinds, device=device, supported=candidate.get("supported", True) is not False,
    )


@dataclass
class LockedVolume:
    device: str
    size: str
    version: int
    hints: list[str]


def locked_volumes(scan: dict[str, Any]) -> list[LockedVolume]:
    """Encrypted volumes that are still locked, with what the scan learned about them (LUKS version, tokens, memory)."""
    info = {str(item.get("device")): item for item in (scan.get("stacks") or {}).get("luks", [])}
    found = []
    for node in scan.get("devices", []):
        if node.get("role") != "luks_locked" or node.get("live_medium"):
            continue
        extra = info.get(str(node.get("path")), {})
        found.append(LockedVolume(str(node["path"]), human_size(int(node.get("size") or 0)), int(extra.get("version") or 1), [str(h) for h in extra.get("hints", [])]))
    return found


def inactive_groups(scan: dict[str, Any]) -> list[dict[str, Any]]:
    return [group for group in (scan.get("stacks") or {}).get("vgs", []) if not group.get("active")]


def unassembled_arrays(scan: dict[str, Any]) -> list[dict[str, Any]]:
    return [array for array in (scan.get("stacks") or {}).get("arrays", []) if array.get("state") == "not_assembled"]


def environment_text(environment: dict[str, Any]) -> str:
    """'Started in UEFI mode · Secure Boot is off · online' for the top of the first page."""
    parts = ["Started in UEFI mode" if environment.get("uefi") else "Started in legacy BIOS mode"]
    if environment.get("uefi") and environment.get("secure_boot") is not None:
        parts.append("Secure Boot is " + ("on" if environment["secure_boot"] else "off"))
    network = environment.get("network")
    if isinstance(network, dict):
        parts.append("online" if network.get("ip") else "offline")
    return " · ".join(parts)


def disk_overview(devices: list[dict[str, Any]]) -> list[str]:
    """One line per disk for the 'all disks' list: model, size and what each partition looks like."""
    lines = []
    for disk in devices:
        if disk.get("type") != "disk" or disk.get("live_medium"):
            continue
        parts = []
        for node in devices:
            if node.get("type") in {"part", "lvm", "crypt"} and parent_disk(devices, str(node.get("path"))) is disk:
                parts.append(f"{str(node['path']).rsplit('/', 1)[-1]}: {node.get('fstype') or node.get('role') or 'unknown'} {human_size(int(node.get('size') or 0))}")
        lines.append(f"{disk.get('path')}  {str(disk.get('model') or '').strip()}  {human_size(int(disk.get('size') or 0))}" + (f"  —  {'; '.join(parts)}" if parts else ""))
    return lines


# ---- mounting ---------------------------------------------------------------------------------------------------------------------
@dataclass
class MountRow:
    mountpoint: str
    device: str
    fstype: str
    kind: str
    optional: bool = False
    selected: bool = True
    note: str = ""


def mount_rows(plan: dict[str, Any]) -> list[MountRow]:
    """The root first, then the other file systems the system's fstab lists (data volumes are optional and off by default)."""
    root = plan.get("root") or {}
    rows = [MountRow("/", str(root.get("device", "")), str(root.get("fstype", "")), MOUNT_KINDS["root"],
                     note=("subvolume " + str(root["rootflags"]).split("=", 1)[-1]) if root.get("rootflags") else "")]
    for item in plan.get("items", []):
        tier = str(item.get("tier_name", "data"))
        rows.append(MountRow(str(item["mountpoint"]), str(item["device"]), str(item.get("fstype", "")), MOUNT_KINDS.get(tier, tier), tier == "data", bool(item.get("selected", True)),
                             "the folder is made when repairs are enabled" if item.get("needs_mkdir") else ""))
    return rows


@dataclass
class MountChoice:
    """Everything that decides the mount plan. The dry run and the real mount must send exactly the same request (the digest covers it)."""

    candidate: str
    scan_id: str
    allow_write: bool = True
    include_tier4: list[str] = field(default_factory=list)
    overrides: list[dict[str, str]] = field(default_factory=list)

    def request(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate, "scan_id": self.scan_id, "allow_write": self.allow_write, "include_tier4": list(self.include_tier4),
            "overrides": [dict(item) for item in self.overrides],
        }


@dataclass
class OpenSystem:
    """The system that is open for repair: the reply of ``rescue.mount``, kept up to date after each repair."""

    info: dict[str, Any]

    @property
    def session_id(self) -> str:
        return str(self.info.get("session_id", ""))

    @property
    def root_device(self) -> str:
        return str((self.info.get("root") or {}).get("device", ""))

    @property
    def title(self) -> str:
        return os_name(self.info.get("os") or {})

    @property
    def mode(self) -> str:
        return str(self.info.get("mode", "ro"))

    @property
    def writable(self) -> bool:
        return self.mode == "rw"

    @property
    def allow_write(self) -> bool:
        return bool(self.info.get("allow_write", True))

    @property
    def findings(self) -> list[dict[str, Any]]:
        return list(self.info.get("findings", []))

    @property
    def recommended(self) -> list[str]:
        return list(self.info.get("recommended", []))

    @property
    def actions(self) -> list[dict[str, Any]]:
        return list(self.info.get("actions", []))

    @property
    def audit_path(self) -> str:
        return str(self.info.get("audit", ""))

    @property
    def kernels(self) -> list[str]:
        return [str(item["version"]) for item in (self.info.get("boot") or {}).get("kernels", []) if item.get("image")]

    @property
    def devices(self) -> list[str]:
        """The devices the system is made of (for the file system check): the root and the other mounted file systems."""
        found = [self.root_device]
        found += [str(item.get("device")) for item in self.info.get("mounts", []) if item.get("kind") == "mount"]
        return list(dict.fromkeys(device for device in found if device.startswith("/dev/")))

    def merge(self, **updates: Any) -> None:
        self.info.update({key: value for key, value in updates.items() if value is not None})


# ---- diagnosis --------------------------------------------------------------------------------------------------------------------
def sorted_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(findings, key=lambda item: SEVERITY_RANK.get(str(item.get("severity")), 3))


def finding_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"error": 0, "warn": 0, "info": 0}
    for item in findings:
        severity = str(item.get("severity"))
        if severity in counts:
            counts[severity] += 1
    return counts


@dataclass
class ActionGroup:
    key: str
    title: str
    help: str
    actions: list[dict[str, Any]]


def group_actions(actions: list[dict[str, Any]]) -> list[ActionGroup]:
    """The engine's catalogue as the groups of the page; 'Fix everything recommended' is shown separately and not part of a group."""
    by_id = {str(item.get("id")): item for item in actions if item.get("id") != RECOMMENDED_ACTION}
    groups = []
    used: set[str] = set()
    for key, title, help_text, ids in ACTION_GROUPS:
        members = [by_id[name] for name in ids if name in by_id]
        used.update(name for name in ids if name in by_id)
        if members:
            groups.append(ActionGroup(key, title, help_text, members))
    rest = [item for name, item in by_id.items() if name not in used]
    if rest:
        groups.append(ActionGroup("other", "Other", "", rest))
    return groups


def action_by_id(actions: list[dict[str, Any]], action_id: str) -> dict[str, Any] | None:
    return next((item for item in actions if item.get("id") == action_id), None)


@dataclass
class Param:
    name: str
    type: str
    label: str
    choices: list[str] = field(default_factory=list)
    default: Any = None
    required: bool = False


def params_of(spec: dict[str, Any]) -> list[Param]:
    found = []
    for item in spec.get("params", []):
        found.append(Param(str(item["name"]), str(item.get("type", "text")), str(item.get("label", item["name"])), [str(c) for c in item.get("choices", [])], item.get("default"),
                           bool(item.get("required", False))))
    return found


def asks_first(spec: dict[str, Any], *, hardware_changed: bool = False) -> bool:
    """Whether the options come before the plan: required values (a user name, a boot entry) or a choice that should be made on purpose."""
    if spec.get("id") in FORM_FIRST:
        return True
    if hardware_changed and spec.get("id") == "boot.initramfs":
        return True
    return any(param.required for param in params_of(spec))


# ---- plans and their confirmation ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Confirmation:
    """What a plan's ``confirm`` block demands before ``rescue.run``: a click, a review, the root device's name, a phrase."""

    risk: str = "info"
    required: bool = False
    review: bool = False
    typed: str = ""
    phrase: str = ""

    @classmethod
    def of(cls, plan: dict[str, Any]) -> "Confirmation":
        block = plan.get("confirm") or {}
        return cls(str(block.get("risk", plan.get("risk", "info"))), bool(block.get("required", False)), bool(block.get("review", False)), str(block.get("typed", "")), str(block.get("phrase", "")))

    def satisfied(self, reviewed: bool, typed: str, phrase: str) -> bool:
        if not self.required:
            return True
        if self.review and not reviewed:
            return False
        if self.typed and typed != self.typed:
            return False
        if self.phrase and phrase != self.phrase:
            return False
        return True


def failed_preconditions(plan: dict[str, Any]) -> list[str]:
    return [str(item.get("text", "")) for item in plan.get("preconditions", []) if not item.get("ok", True)]


def secrets_ready(plan: dict[str, Any], values: dict[str, tuple[str, str]]) -> bool:
    """Every secret the plan needs (a new password) was typed twice, identically, and is not empty or multi-line."""
    for name in plan.get("secrets", []):
        first, second = values.get(name, ("", ""))
        if not first or first != second or any(char in first for char in "\n\r\0"):
            return False
    return True


# Steps that only prepare the system (switch it to read-write, mount virtual file systems) are shown in a quieter style.
PREPARATION_GROUPS = frozenset({"enter-write", "leave-write", "virtual", "guard"})
SMOKE_STEP = "smoke"  # the check that the system can run its own programs


def is_preparation(step: dict[str, Any]) -> bool:
    return step.get("group") in PREPARATION_GROUPS or step.get("id") == SMOKE_STEP
CONDITION_TEXTS = {"chroot_ok": "if the system's programs run", "chroot_broken": "only if they do not"}


# ---- following a run ----------------------------------------------------------------------------------------------------------------
@dataclass
class StepState:
    id: str
    text: str
    status: str = "pending"  # pending | running | done | failed | skipped
    atomic: bool = True
    cleanup: bool = False
    rc: int | None = None
    error: str = ""
    when: str = ""
    group: str = ""


class RunTracker:
    """Follows one ``rescue.run`` from its events (also after a reconnect: events with a sequence number already seen are ignored).

    The plan lists the steps in advance; the engine reports each as running, completed, failed or skipped, and says for a running step
    whether it is atomic. A step that is not atomic can leave the system half-written when it is killed: while one runs, stopping
    and closing are refused. The final ``result`` event holds the outcome, the post-checks and the next hints.
    """

    def __init__(self, plan: dict[str, Any]) -> None:
        self.steps: list[StepState] = []
        self._index: dict[str, StepState] = {}
        for cleanup, key in ((False, "steps"), (True, "cleanup")):
            for item in plan.get(key, []):
                self._add(StepState(str(item.get("id", "")), str(item.get("display", "")), atomic=bool(item.get("atomic", True)), cleanup=cleanup, when=str(item.get("when", "")),
                                    group=str(item.get("group", ""))))
        self.log: list[str] = []
        self.notes: list[str] = []
        self.job_id = ""
        self.last_seq = 0
        self.result: dict[str, Any] | None = None
        self.error: dict[str, Any] | None = None
        self.completed = False
        self.success: bool | None = None

    def _add(self, state: StepState) -> StepState:
        self.steps.append(state)
        self._index[state.id] = state
        return state

    def feed(self, event: dict[str, Any]) -> bool:
        """Apply one event; False when it was a duplicate (already seen before a reconnect)."""
        seq = int(event.get("seq") or 0)
        if seq:
            if seq <= self.last_seq:
                return False
            self.last_seq = seq
        if event.get("job_id") and not self.job_id:
            self.job_id = str(event["job_id"])
        kind = event.get("type")
        data = event_data(event)
        if kind == "stage" and data.get("step"):
            self._step_event(event, data)
        elif kind == "stage" and event.get("stage") == "rollback":
            self.notes.append(str(event.get("message", "")))
        elif kind == "log":
            self.log.append(str(event.get("message", "")))
        elif kind == "result" and "outcome" in data:
            self.result = data
        elif kind == "error":
            self.error = event
        elif kind == "completed":
            self.completed = True
            self.success = bool(data.get("success", True))
        return True

    def _step_event(self, event: dict[str, Any], data: dict[str, Any]) -> None:
        step_id = str(data["step"])
        state = self._index.get(step_id) or self._add(StepState(step_id, str(event.get("message", ""))))
        status = str(data.get("status", ""))
        if status == "running":
            state.status = "running"
            state.atomic = bool(data.get("atomic", state.atomic))
        elif status == "completed":
            state.status = "done"
            state.rc = data.get("rc")
        elif status == "failed":
            state.status, state.rc, state.error = "failed", data.get("rc"), str(data.get("error", ""))
        elif status == "skipped":
            state.status = "skipped"

    @property
    def running(self) -> StepState | None:
        return next((step for step in self.steps if step.status == "running"), None)

    @property
    def non_atomic_running(self) -> bool:
        step = self.running
        return step is not None and not step.atomic

    @property
    def percent(self) -> int:
        main = [step for step in self.steps if not step.cleanup] or self.steps
        if not main:
            return 0
        finished = sum(1 for step in main if step.status in {"done", "failed", "skipped"})
        return int(round(100 * finished / len(main)))

    @property
    def outcome(self) -> str:
        return str((self.result or {}).get("outcome", ""))

    def failed_steps(self) -> list[StepState]:
        return [step for step in self.steps if step.status == "failed"]


# ---- the terminal --------------------------------------------------------------------------------------------------------------------
def terminal_argv(socket_path: str, session_id: str, root_device: str, *, writable: bool = False, host: bool = False, sudo: bool = False) -> list[str]:
    """The command that opens a terminal inside the repaired system: ``gentoo-rescue shell`` hands the daemon's PTY to the terminal window.

    The device name is typed for the person (the window asked them to), so the command never waits for a question the window
    already asked. Read-write terminals also carry the backup phrase that the window collected.
    """
    argv = ["gentoo-rescue", "--socket", socket_path, "shell", session_id, "--typed", root_device.rsplit("/", 1)[-1]]
    if host:
        argv.append("--host")  # inspect-only sessions: a shell of the live system in the mount folder, nothing of the system runs
    elif writable:
        argv += ["--writable", "--i-have-a-backup"]
    return (["sudo", *argv]) if sudo else argv


HOLD_TEXT = "The terminal tool has ended. Press Enter to close this window."


def shell_line(argv: list[str]) -> str:
    """The command plus a pause, so an error message stays readable after the tool ends."""
    return f"{shlex.join(argv)}; echo; echo {shlex.quote(HOLD_TEXT)}; read -r _unused"


def terminal_emulator_argv(emulator: str, title: str, line: str) -> list[str]:
    """Arguments for the terminal programs a live system may have; xfce4-terminal is the one on the disc."""
    if emulator == "xfce4-terminal":
        return ["--title", title, "-x", "sh", "-c", line]
    if emulator == "konsole":
        return ["-p", f"tabtitle={title}", "-e", "sh", "-c", line]
    return ["-e", "sh", "-c", line]


TERMINAL_EMULATORS = ("xfce4-terminal", "x-terminal-emulator", "konsole", "xterm")
