"""ACCT: the gaming option's honest wording and its read-only "Check this PC" list.

Nothing here writes, needs root, needs the network or runs a program: the checks read a few files under
``/sys`` and ``/proc`` (the ``root`` argument lets tests point at a fake tree). The wording is the one source of
the GUI card, the text installer and the documentation, so they cannot drift apart. Update the dated status
here when Epic publishes more, and re-read Epic's post before every release.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

STATUS_DATE = "24 October 2026"
EPIC_URL = "https://onlineservices.epicgames.com/news/introducing-easy-anti-cheat-linux-v2"

# What secondary sources report Epic's EAC Linux v2 asks for (Epic's own post could not be read when this was written).
FORTNITE_REQUIREMENTS = (
    "a signed kernel and signed kernel modules, using a key from Epic's list of well-known keys",
    "Secure Boot switched on in the firmware",
    "a TPM 2.0 chip, switched on in the firmware",
    "measured boot (the boot manager records measurements in the TPM)",
    "the IOMMU switched on, without pass-through",
    "kernel lockdown in integrity mode",
    "kexec disabled",
    "the BPF security module",
)

FORTNITE_STATUS = (
    f"As of {STATUS_DATE}, Fortnite is not playable on Linux. On 14 October 2026 Epic announced that it is building a new Easy Anti-Cheat for Linux "
    "(EAC Linux v2) and that Fortnite is coming, but it gave no release date and no list of Linux distributions. "
    "Secondary sources report that the new anti-cheat asks for eight things: " + "; ".join(FORTNITE_REQUIREMENTS) + ". "
    "A Gentoo kernel built on your own computer is not on Epic's list of keys, so a Gentoo install may not qualify even if Fortnite arrives "
    "(this conclusion is our inference from that list). "
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
              f"Fortnite: not playable on Linux today ({STATUS_DATE}). Epic has announced it but gave no date, and this option does not make it work.")
CHECK_BUTTON = "Check this PC (read-only)"
LIVE_NOTE = ("Secure Boot, TPM and IOMMU say something about this computer. The kernel rows describe the installed system's choices and are decided after the installation.")

LIVE_KERNEL_ROWS = {"Kernel lockdown", "BPF security module", "kexec"}
EFI_SECURE_BOOT = "sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"


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
    if found:
        return Row("IOMMU", "yes", "the firmware offers it and the kernel found it")
    return Row("IOMMU", "no", "not found: the firmware tables are missing or it is switched off in the firmware")


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
    rows = [secure_boot(base), tpm(base), iommu(base), measured_boot(base), lockdown(base), bpf_lsm(base), kexec(base)]
    if live:
        rows = [Row(row.name, "later", "decided after the installation") if row.name in LIVE_KERNEL_ROWS else row for row in rows]
    rows.append(Row("Signed kernel and modules", "unknown", "cannot be checked here; a Gentoo kernel built on this computer is probably not on Epic's list of keys (our inference)"))
    return rows


def format_rows(rows: list[Row]) -> str:
    return "\n".join(row.line() for row in rows)
