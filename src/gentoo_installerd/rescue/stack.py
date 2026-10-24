"""Unlock LUKS, activate LVM volume groups and assemble md arrays, exactly as the user chose (docs: rescue never guesses).

Secrets travel only on stdin (``secret=True``) and are never stored: the passphrase is a local
variable of ``unlock``. Everything opened is recorded in the ledger so release can undo it top
down, and objects that were already active when rescue started are left alone.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .devices import DeviceGraph, DeviceNode, extract_json
from .errors import RescueError
from .hostenv import Host
from .ledger import Ledger
from .runner import RescueRunner
from .safety import PolicyContext

CRYPT_EXIT = {2: "bad_passphrase", 3: "no_memory", 4: "not_luks", 5: "already_open"}
TOKEN_HINTS = {
    "systemd-tpm2": "This volume has a TPM2 token. If it unlocks by itself at boot you may not know the passphrase: use the recovery key you created.",
    "systemd-fido2": "This volume has a FIDO2 token. If you do not remember a passphrase, use the recovery key you created.",
    "systemd-recovery": "This volume has a recovery key slot; the recovery key works as a passphrase here.",
    "clevis": "This volume is bound with Clevis (network or TPM); type a passphrase or recovery key.",
}


def hex_only(value: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", value).lower()


def mapper_name(uuid: str) -> str:
    return "rescue-" + hex_only(uuid)[:12].ljust(12, "0")


@dataclass
class LuksInfo:
    device: str
    version: int
    uuid: str
    tokens: list[str] = field(default_factory=list)
    pbkdf: str = ""
    memory_kib: int = 0
    hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"device": self.device, "version": self.version, "uuid": self.uuid, "tokens": self.tokens, "pbkdf": self.pbkdf, "memory_kib": self.memory_kib, "hints": self.hints}


@dataclass
class VolumeGroup:
    name: str
    uuid: str
    pvs: list[str]
    complete: bool
    active: bool
    lvs: list[dict[str, Any]] = field(default_factory=list)
    duplicate: bool = False
    thin: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "uuid": self.uuid, "complete": self.complete, "active": self.active, "pvs": self.pvs, "lvs": self.lvs, "duplicate": self.duplicate, "thin": self.thin}


@dataclass
class MdArray:
    uuid: str
    level: str
    expected: int
    members: list[str]
    name: str = ""
    metadata: str = ""
    assembled: bool = False
    device: str = ""
    state: str = "not_assembled"

    @property
    def degraded(self) -> bool:
        return len(self.members) < self.expected

    def to_dict(self) -> dict[str, Any]:
        return {"uuid": self.uuid, "level": self.level, "devices": self.expected, "members": self.members, "name": self.name, "metadata": self.metadata, "degraded": self.degraded,
                "state": self.state, "device": self.device}


def parse_export(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        key, eq, value = line.partition("=")
        if eq and re.fullmatch(r"[A-Z_0-9]+", key):
            result[key] = value
    return result


def _report_rows(text: str, key: str) -> list[dict[str, Any]]:
    try:
        document = extract_json(text)
    except ValueError:
        return []
    rows: list[dict[str, Any]] = []
    for report in document.get("report", []) if isinstance(document, dict) else []:
        found = report.get(key) if isinstance(report, dict) else None
        if isinstance(found, list):
            rows.extend(row for row in found if isinstance(row, dict))
    return rows


LVM_SIZE_UNITS = {"b": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5, "e": 6}
LVM_SIZE_RE = re.compile(r"^[<>]?(\d+(?:\.\d+)?)([bkmgtpe]?)$", re.IGNORECASE)


def lvm_bytes(value: object) -> int:
    """A size printed by ``lvs``: bare bytes (``--units b --nosuffix``) or lvm2's default style, rounded with a suffix ('<19.00g'); 0 for anything else."""
    match = LVM_SIZE_RE.match(str(value or "").strip())
    if match is None:
        return 0
    number, unit = match.groups()
    scale = 1024 ** LVM_SIZE_UNITS.get(unit.lower(), 0)
    return int(number) * scale if number.isdigit() else int(float(number) * scale)


class StackManager:
    """Operations on the storage layers between a disk and a mountable file system."""

    def __init__(self, runner: RescueRunner, ctx: PolicyContext, ledger: Ledger, graph_provider: Callable[[], DeviceGraph], host: Host,
                 settle: Callable[[], None] = lambda: None) -> None:
        self.runner = runner
        self.ctx = ctx
        self.ledger = ledger
        self.graph_provider = graph_provider
        self.host = host
        self.settle = settle

    # ---- LUKS -------------------------------------------------------------------------------
    def luks_info(self, node: DeviceNode) -> LuksInfo:
        if self.runner.execute(("cryptsetup", "isLuks", node.path)).returncode != 0:
            raise RescueError("not_luks", f"{node.path} is not a LUKS volume", device=node.path)
        info = LuksInfo(node.path, int(node.fsver) if node.fsver.isdigit() else 1, node.uuid)
        if info.version == 2:
            result = self.runner.execute(("cryptsetup", "luksDump", "--dump-json-metadata", node.path))
            if result.returncode == 0:
                try:
                    self._read_luks2(extract_json(result.stdout), info)
                except (ValueError, json.JSONDecodeError):
                    pass
        return info

    def _read_luks2(self, meta: dict[str, Any], info: LuksInfo) -> None:
        for token in (meta.get("tokens") or {}).values():
            kind = str(token.get("type", ""))
            if kind and kind not in info.tokens:
                info.tokens.append(kind)
                if kind in TOKEN_HINTS:
                    info.hints.append(TOKEN_HINTS[kind])
        for slot in (meta.get("keyslots") or {}).values():
            kdf = slot.get("kdf") or {}
            info.pbkdf = info.pbkdf or str(kdf.get("type", ""))
            info.memory_kib = max(info.memory_kib, int(kdf.get("memory") or 0))
        total, available = self.host.meminfo()
        if info.memory_kib and available and info.memory_kib * 1024 > available:
            info.hints.append(f"The key derivation needs about {info.memory_kib // 1024} MiB of memory; only {available // (1024 * 1024)} MiB are free. Close applications first.")

    def unlock(self, node: DeviceNode, passphrase: str | None, *, readonly: bool = False, test_only: bool = False, keyfile: str = "") -> dict[str, Any]:
        """Open a LUKS container as ``rescue-<uuid>``; the passphrase is used once, on stdin."""
        self._refuse_live(node)
        if passphrase is not None and any(ch in passphrase for ch in "\n\r\x00"):
            raise RescueError("bad_request", "A passphrase typed here cannot contain line breaks")
        if passphrase is None and not keyfile:
            raise RescueError("bad_request", "A passphrase or a key file is required")
        info = self.luks_info(node)
        name = mapper_name(node.uuid or node.partuuid or node.kname)
        graph = self.graph_provider()
        children = [child for child in graph.children_of(graph.find(node.path)) if child.type == "crypt"]
        if children and not test_only:
            existing = children[0]
            ours = self.ledger.find("luks", name=existing.name) is not None
            return {"mapper": existing.path, "ours": ours, "preexisting": not ours, "luks": info.to_dict()}
        self.ctx.devices.add(node.path)
        argv = ["cryptsetup", "open", "--type", "luks", f"--key-file={keyfile or '-'}"]
        if readonly:
            argv.append("--readonly")
        if test_only:
            argv.append("--test-passphrase")
        argv.append(node.path)
        if not test_only:
            argv.append(name)
        result = self.runner.execute(argv, stdin=passphrase if not keyfile else None, secret=bool(passphrase), timeout=120)
        if result.returncode != 0:
            code = CRYPT_EXIT.get(result.returncode, "command_failed")
            details: dict[str, Any] = {"rc": result.returncode, "device": node.path}
            if code == "no_memory":
                details["required_kib"] = info.memory_kib
            message = {
                "bad_passphrase": "The passphrase did not unlock the volume. Check the keyboard layout: the live system may use a different one than the installed system's boot prompt.",
                "no_memory": "Not enough memory to unlock this volume (its key derivation needs a lot of RAM).",
                "not_luks": f"{node.path} is not a LUKS volume.",
                "already_open": f"{node.path} is already open under another name.",
            }.get(code, f"Unlocking {node.path} failed (exit code {result.returncode}).")
            raise RescueError(code, message, **details)
        if test_only:
            return {"test_only": True, "ok": True, "luks": info.to_dict()}
        self.ledger.add("luks", name=name, device=node.path, ours=True, readonly=readonly)
        self.ctx.owned_maps.add(name)
        self.settle()
        return {"mapper": f"/dev/mapper/{name}", "ours": True, "preexisting": False, "luks": info.to_dict()}

    def _refuse_live(self, node: DeviceNode) -> None:
        if node.live_medium or self.ctx.devices.is_live(node.path):
            raise RescueError("live_medium", f"{node.path} is the medium the live system runs from; it is hidden and can never be modified", device=node.path)

    # ---- LVM ------------------------------------------------------------------------------------
    def lvm_inventory(self) -> list[VolumeGroup]:
        pvs = self.runner.execute(("pvs", "--reportformat", "json", "--units", "b", "--nosuffix", "-o", "pv_name,pv_uuid,vg_name,vg_uuid,vg_pv_count,vg_missing_pv_count,pv_size"), timeout=60)
        lvs = self.runner.execute(("lvs", "--reportformat", "json", "--units", "b", "--nosuffix", "-o", "lv_name,vg_name,lv_path,lv_attr,lv_size,segtype,pool_lv,origin,lv_uuid"), timeout=60)
        groups: dict[str, VolumeGroup] = {}
        names_seen: dict[str, set[str]] = {}
        for row in _report_rows(pvs.stdout, "pv") if pvs.returncode == 0 else []:
            vg = str(row.get("vg_name") or "")
            if not vg:
                continue
            names_seen.setdefault(vg, set()).add(str(row.get("vg_uuid") or ""))
            key = f"{vg}\0{row.get('vg_uuid')}"
            group = groups.setdefault(key, VolumeGroup(vg, str(row.get("vg_uuid") or ""), [], str(row.get("vg_missing_pv_count") or "0") in {"0", ""}, False))
            group.pvs.append(str(row.get("pv_name") or ""))
        for row in _report_rows(lvs.stdout, "lv") if lvs.returncode == 0 else []:
            vg = str(row.get("vg_name") or "")
            for group in groups.values():
                if group.name == vg:
                    attr = str(row.get("lv_attr") or "")
                    active = len(attr) > 4 and attr[4] == "a"
                    group.active = group.active or active
                    segtype = str(row.get("segtype") or "")
                    group.thin = group.thin or "thin-pool" in segtype
                    group.lvs.append({"name": row.get("lv_name"), "path": row.get("lv_path"), "size": lvm_bytes(row.get("lv_size")), "segtype": segtype, "active": active})
        for group in groups.values():
            group.duplicate = len(names_seen.get(group.name, ())) > 1
        return list(groups.values())

    def activate_vg(self, name: str) -> dict[str, Any]:
        groups = [group for group in self.lvm_inventory() if group.name == name]
        if not groups:
            raise RescueError("not_found", f"Volume group {name} was not found", vg=name)
        if len(groups) > 1 or groups[0].duplicate:
            raise RescueError("duplicate_vg", f"Two volume groups are called {name} (cloned disks?). LVM cannot tell them apart without renaming one, which changes metadata; do that in the terminal.", vg=name)
        group = groups[0]
        self._refuse_live_pvs(group)
        if not group.complete:
            raise RescueError("degraded", f"Volume group {name} is missing physical volumes", vg=name)
        if group.thin and not self.host.which("thin_check"):
            raise RescueError("unsupported_fs", f"Volume group {name} uses thin provisioning, which needs thin_check; this live system does not have it", reason="missing_tool thin_check", vg=name)
        if group.active:
            return {"vg": name, "preexisting": True, "lvs": group.lvs}
        self.runner.run(("vgchange", "-ay", "--nohints", name), timeout=120)
        self.ledger.add("vg", name=name, ours=True)
        self.ctx.owned_vgs.add(name)
        self.settle()
        refreshed = [item for item in self.lvm_inventory() if item.name == name]
        return {"vg": name, "preexisting": False, "lvs": refreshed[0].lvs if refreshed else group.lvs}

    def _refuse_live_pvs(self, group: VolumeGroup) -> None:
        graph = self.graph_provider()
        for pv in group.pvs:
            try:
                if graph.find(pv).live_medium:
                    raise RescueError("live_medium", f"{pv} belongs to the live medium", device=pv)
            except LookupError:
                continue

    # ---- md RAID --------------------------------------------------------------------------------------
    def md_inventory(self, graph: DeviceGraph | None = None) -> list[MdArray]:
        graph = graph or self.graph_provider()
        arrays: dict[str, MdArray] = {}
        for node in graph:
            if node.role != "md_member" or node.live_medium:
                continue
            result = self.runner.execute(("mdadm", "--examine", "--export", node.path), timeout=30)
            fields = parse_export(result.stdout) if result.returncode == 0 else {}
            uuid = hex_only(fields.get("MD_UUID", ""))
            if not uuid:
                continue
            array = arrays.setdefault(uuid, MdArray(uuid, fields.get("MD_LEVEL", ""), int(fields.get("MD_DEVICES") or 0), [], fields.get("MD_NAME", ""), fields.get("MD_METADATA", "")))
            array.members.append(node.path)
        for array in arrays.values():
            member_keys = {graph.find(path).key for path in array.members if path}
            for node in graph:
                if node.type.startswith("raid") and node.parents and set(node.parents) <= member_keys:
                    array.assembled, array.device, array.state = True, node.path, self._md_state(node.path)
        return list(arrays.values())

    def _md_state(self, device: str) -> str:
        name = device.rsplit("/", 1)[-1]
        text = self.host.read(f"/sys/block/{name}/md/array_state").strip()
        return text or "active"

    def assemble_md(self, uuids: list[str], *, degraded_ok: bool = False) -> dict[str, Any]:
        wanted = {hex_only(item) for item in uuids}
        arrays = {array.uuid: array for array in self.md_inventory()}
        missing = wanted - set(arrays)
        if missing:
            raise RescueError("not_found", f"RAID array {', '.join(sorted(missing))} was not found", uuids=sorted(missing))
        chosen = [arrays[item] for item in sorted(wanted)]
        pending = [array for array in chosen if not array.assembled]
        for array in pending:
            if array.degraded and not degraded_ok:
                raise RescueError("degraded", f"RAID array {array.name or array.uuid} has {len(array.members)} of {array.expected} members. Assembling it degraded is only safe if you confirm.", uuid=array.uuid)
        result: list[dict[str, Any]] = [{**array.to_dict(), "preexisting": True} for array in chosen if array.assembled]
        if pending:
            members = sorted({member for array in pending for member in array.members})
            scan = self.runner.run(("mdadm", "--examine", "--scan", *members), timeout=30)
            lines = self._select_array_lines(scan.stdout, {array.uuid for array in pending})
            conf = Path(self.ctx.mdadm_conf)
            conf.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write("AUTO +all\n" + "\n".join(lines) + "\n")
            argv = ["mdadm", "--assemble", "--scan", "--readonly", f"--config={conf}"]
            if any(array.degraded for array in pending):
                argv.append("--run")
            self.runner.run(argv, timeout=120)
            self.settle()
            for array in self.md_inventory():
                if array.uuid in {item.uuid for item in pending} and array.assembled:
                    self.ledger.add("md", device=array.device, uuid=array.uuid, ours=True)
                    self.ctx.owned_md.add(array.device)
                    self.ctx.owned_md.add(os.path.realpath(array.device))  # host-path: an md device node
                    result.append({**array.to_dict(), "preexisting": False})
        return {"arrays": result}

    @staticmethod
    def _select_array_lines(text: str, wanted: set[str]) -> list[str]:
        """ARRAY lines of the chosen arrays, keeping the device names mdadm printed (review R17)."""
        entries = []
        for line in text.splitlines():
            if not line.startswith("ARRAY"):
                continue
            uuid = re.search(r"\bUUID=(\S+)", line)
            container = re.search(r"\bcontainer=(\S+)", line)
            entries.append((line.strip(), hex_only(uuid.group(1)) if uuid else "", hex_only(container.group(1)) if container else ""))
        containers = {cont for _line, uuid, cont in entries if uuid in wanted and cont}
        selected = [line for line, uuid, cont in entries if uuid in wanted or cont in wanted or uuid in containers]
        return list(dict.fromkeys(selected))

    # ---- release ------------------------------------------------------------------------------------------
    def release(self, entry_kind: str, entry_data: dict[str, Any]) -> None:
        """Undo one acquisition (the ledger removes the entry afterwards)."""
        if entry_kind == "md":
            self.ctx.owned_md.add(entry_data["device"])
            self.runner.run(("mdadm", "--stop", entry_data["device"]), timeout=60)
        elif entry_kind == "vg":
            self.ctx.owned_vgs.add(entry_data["name"])
            self.runner.run(("vgchange", "-an", "--nohints", entry_data["name"]), timeout=60)
        elif entry_kind == "luks":
            self._release_luks(entry_data)

    def _release_luks(self, data: dict[str, Any]) -> None:
        name = data["name"]
        pvs = self.runner.execute(("pvs", "--reportformat", "json", "-o", "pv_name,vg_name"), timeout=60)
        for row in _report_rows(pvs.stdout, "pv") if pvs.returncode == 0 else []:
            if str(row.get("pv_name")) == f"/dev/mapper/{name}" and row.get("vg_name"):
                vg = str(row["vg_name"])
                self.ctx.owned_vgs.add(vg)  # udev activated it because we opened the container: it sits above something we opened
                self.runner.execute(("vgchange", "-an", "--nohints", vg), timeout=60)
        self.ctx.owned_maps.add(name)
        self.runner.run(("cryptsetup", "close", name), timeout=60)
