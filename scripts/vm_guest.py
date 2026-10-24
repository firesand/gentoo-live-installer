"""Programs the harness runs inside the disposable guest (never on the host).

Each sub-command takes one JSON argument and prints one JSON line:

  decoy-prepare   give an extra disk a GPT, an ext4 filesystem and a marker file, report its sha256
  decoy-check     report whether an extra disk still has the sha256 it had
  partition-manual  give the target disk (ESP or BIOS boot + root) or a data disk (swap + /home) the partitions a
                  manual-mode install is pointed at (nothing is formatted)
  rescue-break    damage the installed system on the target disk, from the live system, so a
                  repair tool has something to repair (grub.cfg, initramfs or the fstab UUID); finds the
                  system inside a btrfs subvolume and mounts a separate /boot from the system's own fstab
  disk-hash       the sha256 of a whole disk, to show later that nothing wrote to it

They are sent as ``python3 -c "$(cat vm_guest.py)" <sub-command> '<json>'`` and use only the
standard library plus the live image's own tools (sgdisk, mkfs.ext4, mount, lsblk). Every disk
is identified by the exact size the harness gave it, re-checked here, and the block device must be
one of the VM's own (virtio, SATA/SCSI, NVMe). The damage functions work on a mounted tree and
are tested on fixture directories.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DISK_RE = re.compile(r"/dev/(?:vd[a-z]|sd[a-z]|nvme[0-9]+n[0-9]+)")
ESP_PARTTYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
BIOS_BOOT_PARTTYPE = "21686148-6449-6e6f-744e-656564454649"
ROOT_FILESYSTEMS = ("ext4", "xfs", "btrfs")
DEAD_UUID = "00000000-dead-beef-0000-000000000000"
BREAK_MODES = ("grub-cfg", "initramfs", "fstab-uuid")
MARKER = "gentoo-installer-harness-decoy"
MOUNTS_FILE = "/proc/mounts"


class GuestError(RuntimeError):
    pass


def run(argv: list[str], *, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, input=input_text, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    if check and result.returncode != 0:
        raise GuestError(f"{' '.join(argv)} failed ({result.returncode}): {result.stdout.strip()[-300:]}")
    return result


def partition_path(disk: str, number: int) -> str:
    return f"{disk}p{number}" if disk[-1].isdigit() else f"{disk}{number}"


def verify_disk(path: str, size: int, runner=run) -> None:
    """The device is one of the VM's own disks, writable, unmounted and exactly ``size`` bytes."""
    if not DISK_RE.fullmatch(path):
        raise GuestError(f"Refusing {path}: not a VM disk name")
    actual = int(runner(["blockdev", "--getsize64", path]).stdout.strip())
    if actual != size:
        raise GuestError(f"Refusing {path}: it has {actual} bytes, the harness created {size}")
    mounts = Path(MOUNTS_FILE).read_text() if Path(MOUNTS_FILE).exists() else ""
    if any(line.split()[0].startswith(path) for line in mounts.splitlines() if line.split()):
        raise GuestError(f"Refusing {path}: something on it is mounted")


def sha256_device(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(4 << 20):
            digest.update(chunk)
    return digest.hexdigest()


# --- decoys ------------------------------------------------------------------------------------------

def decoy_prepare(args: dict, runner=run) -> dict:
    path, size = args["path"], int(args["size"])
    if not str(args.get("role", "")).startswith("decoy"):
        raise GuestError("Only decoy disks are prepared")
    verify_disk(path, size, runner)
    runner(["sgdisk", "--zap-all", path])
    runner(["sgdisk", "--new=1:0:0", "--typecode=1:8300", "--change-name=1:decoy", path])
    runner(["partprobe", path], check=False)
    runner(["udevadm", "settle", "--timeout=15"], check=False)
    partition = partition_path(path, 1)
    runner(["mkfs.ext4", "-q", "-F", "-L", "DECOY", partition])
    mount_point = tempfile.mkdtemp(prefix="harness-decoy-")
    runner(["mount", partition, mount_point])
    try:
        marker = Path(mount_point) / "do-not-touch.txt"
        marker.write_text(f"{MARKER}\n" + "precious data\n" * 100)
        runner(["sync"])
    finally:
        runner(["umount", mount_point], check=False)
        os.rmdir(mount_point)
    return {"path": path, "partition": partition, "size": size, "sha256": sha256_device(path)}


def decoy_check(args: dict, runner=run) -> dict:
    path, size = args["path"], int(args["size"])
    verify_disk(path, size, runner)
    digest = sha256_device(path)
    return {"path": path, "size": size, "sha256": digest, "unchanged": digest == args["sha256"]}


# --- a layout for manual-mode installs ---------------------------------------------------------------

def partition_manual(args: dict, runner=run) -> dict:
    """The partitions a manual-mode install is pointed at; nothing is formatted here.

    The target disk gets a GPT with an EFI System Partition (UEFI) or a BIOS-boot partition, then one partition for
    the rest (1 = ESP or BIOS boot, 2 = root). A data disk (role ``data-N``) gets a swap partition and one for /home
    (1 = swap, 2 = home), so a layout can reach across two disks.
    """
    path, size, firmware, role = args["path"], int(args["size"]), args.get("firmware", "uefi"), str(args.get("role", ""))
    if role.startswith("data"):
        verify_disk(path, size, runner)
        runner(["sgdisk", "--zap-all", path])
        runner(["sgdisk", "--new=1:0:+512MiB", "--typecode=1:8200", "--change-name=1:swap", path])
        runner(["sgdisk", "--new=2:0:0", "--typecode=2:8300", "--change-name=2:home", path])
        runner(["partprobe", path], check=False)
        runner(["udevadm", "settle", "--timeout=15"], check=False)
        return {"path": path, "swap": partition_path(path, 1), "home": partition_path(path, 2)}
    if firmware not in {"uefi", "bios"}:
        raise GuestError(f"Unknown firmware {firmware!r}")
    if not role.startswith("target"):
        raise GuestError("Only the target disk and data disks are partitioned for manual mode")
    verify_disk(path, size, runner)
    runner(["sgdisk", "--zap-all", path])
    first = ("--new=1:0:+512MiB", "--typecode=1:EF00", "--change-name=1:EFI") if firmware == "uefi" else (
        "--new=1:0:+3MiB", "--typecode=1:EF02", "--change-name=1:BIOS")
    runner(["sgdisk", *first, path])
    runner(["sgdisk", "--new=2:0:0", "--typecode=2:8300", "--change-name=2:root", path])
    runner(["partprobe", path], check=False)
    runner(["udevadm", "settle", "--timeout=15"], check=False)
    return {"path": path, "boot": partition_path(path, 1), "root": partition_path(path, 2)}


def disk_hash(args: dict, runner=run) -> dict:
    """The checksum of the whole disk (it must be a disk of this VM, unmounted)."""
    path, size = args["path"], int(args["size"])
    verify_disk(path, size, runner)
    return {"path": path, "size": size, "sha256": sha256_device(path)}


# --- breaking an installed system -----------------------------------------------------------------

def choose_partitions(lsblk: dict, disk: str) -> dict:
    """The root (and EFI System) partition of the installation on ``disk``, from ``lsblk --json`` output.

    The root is the largest partition with a root-capable filesystem. An encrypted or LVM root
    is reported as unsupported instead of guessed at.
    """
    nodes = [node for node in lsblk.get("blockdevices", []) if node.get("path") == disk]
    if not nodes:
        raise GuestError(f"lsblk does not list {disk}")
    partitions = nodes[0].get("children") or []
    if any((child.get("fstype") or "") in ("crypto_LUKS", "LVM2_member", "linux_raid_member") for child in partitions):
        raise GuestError("The installation uses LUKS, LVM or RAID: breaking it is not supported yet")
    roots = [child for child in partitions if (child.get("fstype") or "") in ROOT_FILESYSTEMS]
    if not roots:
        raise GuestError(f"No ext4, XFS or Btrfs root partition found on {disk}")
    root = max(roots, key=lambda child: int(child.get("size") or 0))
    esp = next((child for child in partitions if (child.get("parttype") or "").lower() == ESP_PARTTYPE), None)
    return {"root": root["path"], "root_fstype": root.get("fstype"), "esp": esp["path"] if esp else ""}


SPEC_KEYS = {"UUID": "uuid", "LABEL": "label", "PARTUUID": "partuuid", "PARTLABEL": "partlabel"}
LSBLK_COLUMNS = "PATH,TYPE,SIZE,FSTYPE,PARTTYPE,LABEL,UUID,PARTUUID,PARTLABEL"
SYSTEM_SUBVOLUMES = ("root", "@", "@root", "ROOT", "rootfs")


def read_fstab(system: Path) -> list[tuple[str, str]]:
    """(device field, mount point) of every entry of the system's /etc/fstab."""
    try:
        text = (system / "etc" / "fstab").read_text()
    except OSError:
        return []
    entries = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and not fields[0].startswith("#"):
            entries.append((fields[0], fields[1]))
    return entries


def resolve_spec(spec: str, partitions: list[dict]) -> str:
    """The partition (of ``partitions``, the disk under test) that an fstab device field names; '' when none or several do."""
    kind, _, value = spec.partition("=")
    if kind in SPEC_KEYS and value:
        key = SPEC_KEYS[kind]
        wanted = value.strip("\"'").lower()
        found = [part for part in partitions if str(part.get(key) or "").lower() == wanted]
        return str(found[0]["path"]) if len(found) == 1 else ""
    if spec.startswith("/dev/") and any(part.get("path") == spec for part in partitions):
        return spec
    return ""


def find_system_root(mounted: Path) -> Path:
    """The directory of the installed system inside a mounted file system: the mount itself, or the btrfs subvolume that holds /etc/fstab.

    The top level of a btrfs file system lists its subvolumes (root, home ...) as directories, so a
    system in a subvolume is edited through that directory.
    """
    if (mounted / "etc" / "fstab").is_file():
        return mounted
    found = sorted(child for child in mounted.iterdir() if child.is_dir() and not child.is_symlink() and (child / "etc" / "fstab").is_file())
    if len(found) == 1:
        return found[0]
    named = [child for child in found if child.name in SYSTEM_SUBVOLUMES]
    if len(named) == 1:
        return named[0]
    raise GuestError("No installed system was found on the root partition" + (f": several directories have an /etc/fstab ({', '.join(child.name for child in found)})" if found else ""))


def boot_partition(system: Path, root_device: str, partitions: list[dict]) -> str:
    """The separate /boot partition the system's own fstab names, if it is one of the disk's partitions."""
    for spec, target in read_fstab(system):
        if target == "/boot":
            device = resolve_spec(spec, partitions)
            if device and device != root_device:
                return device
    return ""


def initramfs_images(boot: Path) -> list[Path]:
    """Every initramfs image below /boot (at most three levels down, for layouts that keep one directory per kernel)."""
    found: list[Path] = []
    for directory, subdirectories, names in os.walk(boot):
        depth = len(Path(directory).relative_to(boot).parts)
        if depth >= 3:
            subdirectories[:] = []
        for name in names:
            path = Path(directory) / name
            if (name.startswith("initramfs") or name.startswith("initrd")) and path.is_file():
                found.append(path)
    return sorted(found)


def break_root_tree(mode: str, root: Path) -> dict:
    """Damage a mounted installation. Returns what was changed; raises if there was nothing to break."""
    if mode == "grub-cfg":
        # Gentoo and Debian keep it in boot/grub, Fedora and SUSE in boot/grub2.
        configs = [root / name for name in ("boot/grub/grub.cfg", "boot/grub2/grub.cfg") if (root / name).is_file()]
        if not configs:
            raise GuestError("boot/grub/grub.cfg is missing: this installation has nothing to break")
        size = sum(config.stat().st_size for config in configs)
        for config in configs:
            config.unlink()
        return {"removed": [str(config.relative_to(root)) for config in configs], "bytes": size}
    if mode == "initramfs":
        images = initramfs_images(root / "boot")
        if not images:
            raise GuestError("No initramfs image in /boot: this installation has nothing to break")
        for image in images:
            image.unlink()
        return {"removed": [str(image.relative_to(root)) for image in images]}
    if mode == "fstab-uuid":
        fstab = root / "etc" / "fstab"
        lines = fstab.read_text().splitlines()
        changed: list[str] = []
        for index, line in enumerate(lines):
            fields = line.split()
            if len(fields) >= 2 and fields[1] == "/" and fields[0].partition("=")[0] in SPEC_KEYS and "=" in fields[0]:
                changed.append(fields[0])
                lines[index] = line.replace(fields[0], f"UUID={DEAD_UUID}", 1)
        if not changed:
            raise GuestError("etc/fstab has no UUID=, LABEL= or PARTUUID= entry for /: this installation has nothing to break")
        fstab.write_text("\n".join(lines) + "\n")
        return {"replaced": changed, "with": f"UUID={DEAD_UUID}"}
    raise GuestError(f"Unknown break mode {mode!r}")


def rescue_break(args: dict, runner=run) -> dict:
    """Damage the system on the guarded disk. Its root is found by size and file system; a btrfs subvolume and a separate /boot are handled."""
    mode, path, size = args["mode"], args["path"], int(args["size"])
    if mode not in BREAK_MODES:
        raise GuestError(f"Unknown break mode {mode!r}")
    verify_disk(path, size, runner)
    lsblk = json.loads(runner(["lsblk", "--json", "--bytes", "--paths", "--output", LSBLK_COLUMNS]).stdout)
    layout = choose_partitions(lsblk, path)
    partitions = next((node.get("children") or [] for node in lsblk.get("blockdevices", []) if node.get("path") == path), [])
    mount_point = tempfile.mkdtemp(prefix="harness-root-")
    runner(["mount", layout["root"], mount_point])
    mounted = [mount_point]
    proof: dict = {}
    extra: dict = {}
    try:
        system = find_system_root(Path(mount_point))
        boot = boot_partition(system, layout["root"], partitions)
        if boot:
            boot_dir = system / "boot"
            boot_dir.mkdir(exist_ok=True)
            runner(["mount", boot, str(boot_dir)])
            mounted.append(str(boot_dir))
        extra = {"boot_partition": boot, "system": str(system.relative_to(mount_point)) if system != Path(mount_point) else ""}
        changes = break_root_tree(mode, system)
        runner(["sync"])
        if mode == "fstab-uuid":
            # Booting may still succeed with a wrong fstab, so the defect is proven here, read-only.
            check = runner(["findmnt", "--verify", "--tab-file", str(system / "etc" / "fstab")], check=False)
            proof = {"findmnt_exit": check.returncode, "findmnt_output": check.stdout[-800:]}
    finally:
        for target in reversed(mounted):
            runner(["umount", target], check=False)
        os.rmdir(mount_point)
    return {"mode": mode, **layout, **extra, **changes, **proof}


COMMANDS = {"decoy-prepare": decoy_prepare, "decoy-check": decoy_check, "partition-manual": partition_manual,
            "rescue-break": rescue_break, "disk-hash": disk_hash}


def main(argv: list[str]) -> int:
    try:
        report = COMMANDS[argv[0]](json.loads(argv[1]))
    except (GuestError, KeyError, ValueError, OSError) as exc:
        print(json.dumps({"type": "vm_guest", "ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps({"type": "vm_guest", "ok": True, **report}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
