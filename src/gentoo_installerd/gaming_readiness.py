"""ACCT: the gaming option's honest wording and its read-only "Check this PC" list.

Nothing here writes, needs root, needs the network or runs a program: the checks read a few files under
``/sys`` and ``/proc`` (the ``root`` argument lets tests point at a fake tree). The wording is the one source of
the GUI card, the text installer and the documentation, so they cannot drift apart. Update the dated status
here when Epic publishes more, and re-read Epic's post before every release.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct

STATUS_DATE = "27 October 2026"
EPIC_URL = "https://onlineservices.epicgames.com/news/introducing-easy-anti-cheat-linux-v2"

# What secondary sources report Epic's EAC Linux v2 asks for (Epic's own post could not be read when this was written).
FORTNITE_REQUIREMENTS = (
    "a signed kernel and signed kernel modules, built by an organization whose key Epic recognizes",
    "Secure Boot switched on in the firmware",
    "a TPM 2.0 chip, switched on in the firmware",
    "measured boot (the boot manager records measurements in the TPM)",
    "the IOMMU switched on, without pass-through",
    "kernel lockdown in integrity mode",
    "kexec disabled",
    "the BPF security module",
)

# The keys secondary sources (xenospectrum, Phoronix, 14 October 2026) report Epic recognizes so far.
RECOGNIZED_KEYS = ("Canonical (Ubuntu)", "Fedora", "Universal Blue (Bazzite, Bluefin, Aurora)", "Debian", "SUSE", "Rocky Linux", "the Microsoft UEFI CA")

FORTNITE_STATUS = (
    f"As of {STATUS_DATE}, Fortnite is not playable on Linux. On 14 October 2026 Epic announced that it is building a new Easy Anti-Cheat for Linux "
    "(EAC Linux v2) and that Fortnite is coming, but it gave no release date. "
    "Secondary sources report that EAC Linux v2 asks for eight things: " + "; ".join(FORTNITE_REQUIREMENTS) + ". "
    "The keys it recognizes so far are those of " + ", ".join(RECOGNIZED_KEYS[:-1]) + " and " + RECOGNIZED_KEYS[-1] + "; when a computer starts "
    "through shim, every key enrolled in its MOK list must be one of them. Gentoo is not on that list, and a kernel built on your own computer "
    "cannot be, so an installed Gentoo cannot run games that need EAC Linux v2. "
    "This option sets up an ordinary Linux gaming system: Steam or Heroic, graphics libraries and game controller access. "
    "It does not make Fortnite work, and nobody can promise that it will. "
    f"Source: Epic Online Services, 14 October 2026, {EPIC_URL}. Check it again before you rely on this."
)

GAMING_MODES_TEXT = {
    "off": "No gaming setup (default)",
    "flatpak": "Quick: Steam and Heroic from Flathub (downloaded after the first start; needs the network then)",
    "native": "Native Gentoo: Steam from Valve and 32-bit graphics libraries built on this computer",
}
BUILD_TIME_NOTE = ("Native Gentoo builds 32-bit graphics libraries from source (Mesa, LLVM and more) and takes a long time, often hours. "
                   "It needs a stage3 with 32-bit support (multilib), an outside software repository (steam-overlay), some testing (~amd64) packages "
                   "and a download from Valve.")
NATIVE_NEEDS_MULTILIB = "Native Gentoo gaming needs a multilib stage3 (32-bit support); this installation uses a no-multilib one. Use the quick Flatpak mode instead."
NEEDS_DESKTOP = "Gaming needs a desktop. Choose a desktop on the System page to use it."
ACCEPT_EXTERNAL_TEXT = "I accept the outside repository, the testing (~amd64) packages and the download of Steam from Valve"
CARD_INTRO = ("Gaming (optional): sets up Steam or Heroic, graphics libraries and game controller support. "
              f"Fortnite: not playable on Linux today ({STATUS_DATE}). Games that need Easy Anti-Cheat Linux v2 will not run on Gentoo: Epic accepts "
              "only kernels signed by organizations on its list, and Gentoo is not on it.")
CHECK_BUTTON = "Check this PC (read-only)"
LIVE_NOTE = ("Secure Boot, TPM, IOMMU and the MOK list say something about this computer. The kernel rows describe the installed system's choices "
             "and are decided after the installation.")

LIVE_KERNEL_ROWS = {"Kernel lockdown", "BPF security module", "kexec"}
EFI_SECURE_BOOT = "sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
EFI_MOK_LIST = "sys/firmware/efi/efivars/MokListRT-605dab50-e046-4300-abb6-3dd810dd8b23"  # shim's copy of the MOK list, readable without root
DISC_KEY_NAME = "Gentoo installer live disc"  # the start of the name iso/sign-secure-boot.py gives the key of each disc
COMMON_NAME_OID = b"\x06\x03\x55\x04\x03"


@dataclass(frozen=True)
class Row:
    name: str
    state: str  # "yes" | "no" | "unknown" | "later" (decided after the installation)
    detail: str

    def line(self) -> str:
        mark = {"yes": "[x]", "no": "[ ]", "unknown": "[?]", "later": "[-]"}[self.state]
        return f"{mark} {self.name}: {self.detail}"


def _text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return None


def secure_boot(root: Path) -> Row:
    if not (root / "sys/firmware/efi").is_dir():
        return Row("Secure Boot", "no", "this computer started in BIOS mode, which has no Secure Boot")
    try:
        data = (root / EFI_SECURE_BOOT).read_bytes()
    except OSError:
        return Row("Secure Boot", "unknown", "the firmware does not say")
    if data and data[-1] == 1:
        return Row("Secure Boot", "yes", "switched on in the firmware")
    return Row("Secure Boot", "no", "switched off in the firmware")


def tpm(root: Path) -> Row:
    major = _text(root / "sys/class/tpm/tpm0/tpm_version_major")
    if major == "2" or (root / "dev/tpmrm0").exists():
        return Row("TPM 2.0", "yes", "found")
    if major is not None:
        return Row("TPM 2.0", "no", f"a TPM version {major} chip was found")
    return Row("TPM 2.0", "no", "none found (it may be switched off in the firmware: fTPM or PTT)")


def iommu(root: Path) -> Row:
    directory = root / "sys/class/iommu"
    try:
        found = any(directory.iterdir())
    except OSError:
        found = False
    if not found:
        return Row("IOMMU", "no", "not found: the firmware tables are missing or it is switched off in the firmware")
    if any(_text(path) == "identity" for path in (root / "sys/kernel/iommu_groups").glob("*/type")):
        return Row("IOMMU", "no", "on, but in pass-through mode (iommu=pt or iommu.passthrough=1 on the kernel command line)")
    return Row("IOMMU", "yes", "the firmware offers it and the kernel found it")


def _common_name(der: bytes) -> str:
    """The subject's common name of a DER certificate (it follows the issuer's), or "" when it has none."""
    names = []
    start = der.find(COMMON_NAME_OID)
    while start >= 0 and start + 7 <= len(der):
        length = der[start + 6]
        if length < 0x80:
            names.append(der[start + 7:start + 7 + length].decode("utf-8", errors="replace"))
        start = der.find(COMMON_NAME_OID, start + 1)
    return names[1] if len(names) > 1 else (names[0] if names else "")


def mok_names(data: bytes) -> list[str]:
    """One name per entry of an EFI signature list such as MokListRT (without the 4 attribute bytes of efivarfs)."""
    names: list[str] = []
    offset = 0
    while offset + 28 <= len(data):
        list_size, header_size, entry_size = struct.unpack_from("<III", data, offset + 16)
        if list_size < 28 or offset + list_size > len(data) or entry_size <= 16:
            break
        first = offset + 28 + header_size
        for entry in range(first, offset + list_size - entry_size + 1, entry_size):
            names.append(_common_name(data[entry + 16:entry + entry_size]) or "an entry without a name (a file hash)")
        offset += list_size
    return names


def mok(root: Path) -> Row:
    name = "Machine Owner Keys (MOK)"
    try:
        data = (root / EFI_MOK_LIST).read_bytes()
    except OSError:
        return Row(name, "unknown", "cannot be read here: this session did not start through shim")
    names = mok_names(data[4:])
    disc = next((key for key in names if key.startswith(DISC_KEY_NAME)), "")
    if disc:
        return Row(name, "no", f"this disc's key is enrolled ({disc}). EAC Linux v2 accepts only keys Epic recognizes: remove it once you no longer "
                               "need the disc (sudo mokutil --delete with its .cer file)")
    if names:
        return Row(name, "unknown", "enrolled: " + "; ".join(names[:4]) + ("; and more" if len(names) > 4 else "")
                   + ". EAC Linux v2 accepts only keys Epic recognizes")
    return Row(name, "yes", "none enrolled")


def measured_boot(root: Path) -> Row:
    if (root / "sys/kernel/security/tpm0/binary_bios_measurements").exists():
        return Row("Measured boot", "yes", "the firmware records boot measurements in the TPM")
    return Row("Measured boot", "unknown", "cannot be seen from here")


def lockdown(root: Path) -> Row:
    text = _text(root / "sys/kernel/security/lockdown")
    if text is None:
        return Row("Kernel lockdown", "unknown", "this kernel does not say")
    mode = next((word.strip("[]") for word in text.split() if word.startswith("[")), "")
    return Row("Kernel lockdown", "yes" if mode in {"integrity", "confidentiality"} else "no", f"mode: {mode or 'unknown'}")


def bpf_lsm(root: Path) -> Row:
    text = _text(root / "sys/kernel/security/lsm")
    if text is None:
        return Row("BPF security module", "unknown", "this kernel does not say")
    return Row("BPF security module", "yes" if "bpf" in text.split(",") else "no", "active" if "bpf" in text.split(",") else "not in the active list")


def kexec(root: Path) -> Row:
    text = _text(root / "proc/sys/kernel/kexec_load_disabled")
    if text is None:
        return Row("kexec", "unknown", "this kernel does not say")
    return Row("kexec", "yes" if text == "1" else "no", "disabled" if text == "1" else "enabled")


def check_this_pc(root: str | Path = "/", *, live: bool = True) -> list[Row]:
    """The readiness list, read-only. In the live session the kernel rows are 'decided after installation'."""
    base = Path(root)
    rows = [secure_boot(base), tpm(base), iommu(base), measured_boot(base), mok(base), lockdown(base), bpf_lsm(base), kexec(base)]
    if live:
        rows = [Row(row.name, "later", "decided after the installation") if row.name in LIVE_KERNEL_ROWS else row for row in rows]
    rows.append(Row("Signed kernel and modules", "no", "Gentoo is not on Epic's list of recognized keys, and a kernel built on this computer cannot be"))
    return rows


def format_rows(rows: list[Row]) -> str:
    return "\n".join(row.line() for row in rows)
