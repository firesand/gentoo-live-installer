"""Static hardware tables and pure classifiers shared by the probe and the planner.

Nothing here touches the machine. Everything that depends on device or board
names is a table or a rule in this file, so supporting a new GPU generation or
hypervisor is a data change with a test next to it. Anything the running kernel
can tell us directly (the bound driver, the battery, the sysfs DMI strings) is
preferred over these tables by the callers.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping

# ---------------------------------------------------------------------------
# PCI
# ---------------------------------------------------------------------------

# Vendor id (lower-case hex, no 0x) -> GPU vendor key.
GPU_VENDORS: dict[str, str] = {
    "10de": "nvidia",
    "1002": "amd",
    "8086": "intel",
    "1af4": "virtio",
    "1b36": "qxl",
    "15ad": "vmware",
    "80ee": "virtualbox",
    "1234": "bochs",
    "1013": "cirrus",
    "1414": "hyperv",
    "1ab8": "parallels",
    "1a03": "aspeed",
    "102b": "matrox",
}
# Paravirtual display adapters: no vendor driver choice, never "hybrid".
VIRTUAL_GPU_VENDORS = frozenset({"virtio", "qxl", "vmware", "virtualbox", "bochs", "cirrus", "hyperv", "parallels"})
# Server BMC graphics: the kernel driver plus Xorg modesetting is enough.
BMC_GPU_VENDORS = frozenset({"aspeed", "matrox"})

# PCI class (first four hex digits of 0xCCSSPP) -> storage controller kind.
STORAGE_CLASSES: dict[str, str] = {
    "0100": "scsi",
    "0101": "ide",
    "0104": "raid",
    "0105": "ata",
    "0106": "ahci",
    "0107": "sas",
    "0108": "nvme",
    "0180": "other",
    "0805": "sd-host",
}

# Hardware-vendor PCI ids used by a few heuristics.
VENDOR_INTEL = "8086"
VENDOR_BROADCOM = "14e4"
VENDOR_APPLE = "106b"
VIRTIO_VENDOR = "1af4"

# Bound drivers that mean "Intel Volume Management Device" (NVMe stays visible).
VMD_DRIVERS = frozenset({"vmd"})
# Bound-driver name patterns of the Sound Open Firmware stack (needs sof-firmware).
SOF_DRIVER_RE = re.compile(r"(?:^|[-_])sof(?:[-_]|$)", re.I)


def gpu_vendor(vendor_id: str) -> str:
    return GPU_VENDORS.get(vendor_id.lower().removeprefix("0x"), "other")


# ---------------------------------------------------------------------------
# GPU generations
# ---------------------------------------------------------------------------

# NVIDIA chip names in pci.ids start with the codename: AD102, GA104, TU117M, GP108M ...
_NVIDIA_CHIP_RE = re.compile(r"^\s*(?P<chip>[A-Z]{1,3}\d{1,4})", re.I)
_NVIDIA_TURING_PLUS = ("TU", "GA", "AD", "GH", "GB")
_NVIDIA_MAXWELL_VOLTA = ("GM", "GP", "GV")
_NVIDIA_KEPLER = ("GK",)
_NVIDIA_FERMI = ("GF",)

# Generation labels the planner understands.
NVIDIA_GENERATIONS = ("turing+", "maxwell-volta", "kepler", "fermi", "older", "")


def nvidia_generation_by_id(device_id: str) -> str:
    """Coarse generation from the PCI device id, used when pci.ids has no name.

    NVIDIA allocated increasing id ranges per architecture: everything from
    0x1e00 up is Turing or newer, 0x1340-0x1dff is Maxwell, Pascal and Volta.
    Below that is Kepler or older, which share one outcome (no proprietary
    branch in the tree), so they are not told apart here.
    """
    try:
        value = int(device_id.lower().removeprefix("0x"), 16)
    except ValueError:
        return ""
    if value >= 0x1E00:
        return "turing+"
    if value >= 0x1340:
        return "maxwell-volta"
    return "older"


def classify_nvidia(name: str, device_id: str = "") -> tuple[str, bool]:
    """Return (generation, guessed) for an NVIDIA GPU.

    The pci.ids name ("AD102 [GeForce RTX 4090]") is authoritative. A device
    that is not in the database falls back to the id range and is flagged as
    guessed so the planner can warn instead of trusting it.
    """
    match = _NVIDIA_CHIP_RE.match(name or "")
    if match:
        chip = match.group("chip").upper()
        prefix = re.match(r"[A-Z]+", chip).group(0)  # type: ignore[union-attr]
        if prefix in _NVIDIA_TURING_PLUS:
            return "turing+", False
        if prefix in _NVIDIA_MAXWELL_VOLTA:
            return "maxwell-volta", False
        if prefix in _NVIDIA_KEPLER:
            return "kepler", False
        if prefix in _NVIDIA_FERMI:
            return "fermi", False
        if prefix in {"G", "GT", "NV", "C", "MCP"}:
            return "older", False
    generation = nvidia_generation_by_id(device_id)
    return generation, bool(generation)


_AMD_GCN12 = re.compile(r"\b(Tahiti|Pitcairn|Cape Verde|Oland|Hainan|Bonaire|Hawaii|Kaveri|Kabini|Mullins|Godavari|Temash|Beema)\b", re.I)
_AMD_TERASCALE = re.compile(
    r"\b(?:Cedar|Redwood|Juniper|Cypress|Hemlock|Barts|Turks|Caicos|Cayman|Antilles|Palm|Sumo2?|SuperSumo|Wrestler|Ontario|Zacate|"
    r"Llano|Trinity|Richland)\b|\b(?:RV[67]\d\d|RS[78]\d\d|R6\d\d)", re.I)
_AMD_R300 = re.compile(r"\b(?:R[345]\d\d|RV[345]\d\d|RS[46]\d\d)", re.I)
# Radeon 7000-9200 (R100/R200 class): no driver in current Mesa.
_AMD_OLDER = re.compile(r"\b(?:Rage|Mach\d+|R100|R200|RV100|RV200|RV250|RV280|RS100|RS200|RS250|Radeon (?:VE|IGP|7\d{3}|8\d{3}|9[0-2]\d\d))\b", re.I)
# Integrated (APU) code names; everything else AMD is treated as discrete.
_AMD_APU = re.compile(
    r"\b(?:Kaveri|Kabini|Mullins|Godavari|Carrizo|Stoney|Bristol Ridge|Raven|Picasso|Renoir|Cezanne|Lucienne|Barcelo|Rembrandt|"
    r"Phoenix\d?|Raphael|Mendocino|Hawk Point|Strix|Krackan|Sarlak|Van Gogh|Vangogh|Dali|Wrestler|Ontario|Zacate|Llano|Sumo|"
    r"Trinity|Richland|Beema|Temash|Granite Ridge|Mero)\b|Radeon\s+(?:Vega\s+(?:Series|Mobile)|\d{3}M\b)", re.I)


def classify_amd(name: str, driver: str = "") -> str:
    """Return the AMD GPU family: gcn3+, gcn1-2, terascale, r300, older or '' when unknown."""
    name = name or ""
    if _AMD_GCN12.search(name):
        return "gcn1-2"
    if _AMD_TERASCALE.search(name):
        return "terascale"
    if _AMD_R300.search(name):
        return "r300"
    if _AMD_OLDER.search(name):
        return "older"
    if name or driver == "amdgpu":
        # Fiji, Tonga, Polaris, Vega, Navi and the APUs after Kaveri.
        return "gcn3+"
    return ""


def amd_is_integrated(name: str) -> bool:
    return bool(_AMD_APU.search(name or ""))


_INTEL_GEN3 = re.compile(r"\b(?:82915|82945|82G33|82G35|82Q35|82Q33|945G|945GM|Pineview|Cedarview|GMA\s*3\d{3}|Atom Processor [DN][2-5][0-9x]{2,3})", re.I)
_INTEL_GEN4_5 = re.compile(r"\b(?:82Q963|82G965|82946|82G41|G41|G45|GM45|GL40|Q45|Q43|Eaglelake|Cantiga|Ironlake|Arrandale|Clarkdale|4 Series|Core Processor Integrated Graphics|Mobile 4 Series)", re.I)
_INTEL_GEN6_7 = re.compile(r"\b(Sandy ?Bridge|Ivy ?Bridge|Haswell|Valleyview|Bay ?Trail|2nd Generation Core|3rd Gen Core|4th Gen Core|Atom Processor Z36|Atom Processor E3800)", re.I)
_INTEL_DISCRETE = re.compile(r"\b(DG[12]|Arc|BMG|Battlemage|Alchemist|Iris Xe MAX|Data Center GPU|Flex)\b", re.I)


def classify_intel_gpu(name: str) -> str:
    """Return gen3, gen4-5, gen6-7 or gen8+ (default for anything newer or unknown)."""
    name = name or ""
    if _INTEL_GEN3.search(name):
        return "gen3"
    if _INTEL_GEN4_5.search(name):
        return "gen4-5"
    if _INTEL_GEN6_7.search(name):
        return "gen6-7"
    return "gen8+"


def intel_is_discrete(name: str) -> bool:
    return bool(_INTEL_DISCRETE.search(name or ""))


# ---------------------------------------------------------------------------
# NVIDIA proprietary driver branches (verified against the pinned Portage tree)
# ---------------------------------------------------------------------------

NVIDIA_ATOM = "x11-drivers/nvidia-drivers"
# Branch -> what the tree offers. The licence is a table, not read at install
# time: the Portage tree is not on the live image. tests/hardware/test_tree_evidence.py
# re-checks every row against the pinned snapshot.
NVIDIA_BRANCHES: dict[str, dict[str, object]] = {
    "595": {
        "generations": ("turing+",),
        "license": "NVIDIA-2025",
        "mask": "",  # newest stable branch: nothing to hold back
        "label": "595 (current, open kernel modules)",
    },
    "580": {
        "generations": ("maxwell-volta", "turing+"),
        "license": "NVIDIA-2025",
        # Keep later @world updates on the long-term branch that still
        # supports Maxwell, Pascal and Volta.
        "mask": ">=x11-drivers/nvidia-drivers-581",
        "label": "580 (long-term, last branch for Maxwell, Pascal and Volta)",
    },
}


def nvidia_branch_for(generation: str) -> str:
    """Default proprietary branch for a GPU generation, '' when none is offered."""
    if generation == "turing+":
        return "595"
    if generation == "maxwell-volta":
        return "580"
    return ""


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------

# Valid values of CPU_FLAGS_X86 (profiles/desc/cpu_flags_x86.desc in the pinned tree).
CPU_FLAGS_X86_VALUES = frozenset("""
3dnow 3dnowext aes amx_bf16 amx_int8 amx_tile avx avx2 avx512_4fmaps avx512_4vnniw avx512_bf16 avx512_bitalg
avx512_fp16 avx512_vbmi2 avx512_vnni avx512_vp2intersect avx512_vpopcntdq avx512bw avx512cd avx512dq avx512er
avx512f avx512ifma avx512pf avx512vbmi avx512vl avx_vnni bmi1 bmi2 f16c fma3 fma4 mmx mmxext padlock pclmul popcnt
rdrand sha sse sse2 sse3 sse4_1 sse4_2 sse4a ssse3 vpclmulqdq xop
""".split())

# /proc/cpuinfo flag -> CPU_FLAGS_X86 value where the names differ (see the
# square-bracket hints in cpu_flags_x86.desc). Flags with identical names map to themselves.
_CPUINFO_RENAMES: dict[str, tuple[str, ...]] = {
    "fma": ("fma3",),
    "pclmulqdq": ("pclmul",),
    "pni": ("sse3",),
    "sha_ni": ("sha",),
    "abm": ("popcnt",),
    "phe": ("padlock",),
    "sse": ("sse", "mmxext"),
}


def cpuinfo_to_cpu_flags(flags: Iterable[str]) -> list[str]:
    """CPU_FLAGS_X86 values implied by /proc/cpuinfo flags (fallback when cpuid2cpuflags is absent)."""
    present = set(flags)
    result: set[str] = set()
    for flag in present:
        for value in _CPUINFO_RENAMES.get(flag, (flag,)):
            if value in CPU_FLAGS_X86_VALUES:
                result.add(value)
    if "mmxext" in present:
        result.add("mmxext")
    if "popcnt" in present:
        result.add("popcnt")
    return sorted(result)


# x86-64 micro-architecture levels (psABI): the cpuinfo flag names of each level.
_LEVEL_FLAGS = (
    ("cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3"),
    ("avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave"),
    ("avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"),
)


def x86_64_level(flags: Iterable[str]) -> int:
    """Highest x86-64 level (1-4) whose required flags are all present; 0 without long mode."""
    present = set(flags)
    if "lm" not in present:
        return 0
    level = 1
    for index, required in enumerate(_LEVEL_FLAGS, start=2):
        if all(flag in present for flag in required):
            level = index
        else:
            break
    return level


_CPU_VENDORS = {"GenuineIntel": "intel", "AuthenticAMD": "amd", "HygonGenuine": "hygon"}


def cpu_vendor(vendor_id: str) -> str:
    """intel | amd | hygon | other, or '' when the vendor string is empty."""
    vendor_id = (vendor_id or "").strip()
    return _CPU_VENDORS.get(vendor_id, "other") if vendor_id else ""


# ---------------------------------------------------------------------------
# Machine: chassis and hypervisor
# ---------------------------------------------------------------------------

# SMBIOS chassis types.
CHASSIS_LAPTOP = frozenset({8, 9, 10, 11, 14, 31})
CHASSIS_TABLET = frozenset({30, 32})
CHASSIS_SERVER = frozenset({17, 23, 25, 28, 29})
CHASSIS_NAMES: dict[int, str] = {
    1: "Other", 2: "Unknown", 3: "Desktop", 4: "Low Profile Desktop", 5: "Pizza Box", 6: "Mini Tower", 7: "Tower",
    8: "Portable", 9: "Laptop", 10: "Notebook", 11: "Hand Held", 12: "Docking Station", 13: "All in One",
    14: "Sub Notebook", 15: "Space-saving", 16: "Lunch Box", 17: "Main Server Chassis", 23: "Rack Mount Chassis",
    24: "Sealed-case PC", 25: "Multi-system chassis", 28: "Blade", 29: "Blade Enclosure", 30: "Tablet",
    31: "Convertible", 32: "Detachable", 33: "IoT Gateway", 34: "Embedded PC", 35: "Mini PC", 36: "Stick PC",
}


def form_factor(chassis_type: int | None, has_battery: bool, hypervisor: str) -> str:
    """desktop | laptop | tablet | server | vm | other, from the chassis type and the battery.

    Many laptops report chassis 1, 2 or 3, so a system battery counts as a
    laptop unless the chassis says server. A hypervisor always wins.
    """
    if hypervisor:
        return "vm"
    if chassis_type in CHASSIS_TABLET:
        return "tablet"
    if chassis_type in CHASSIS_LAPTOP:
        return "laptop"
    if chassis_type in CHASSIS_SERVER:
        return "server"
    if has_battery:
        return "laptop"
    return "desktop" if chassis_type is not None else "other"


# (sys_vendor substring, product_name substring, bios_vendor substring) -> hypervisor.
# A blank pattern matches anything; the first matching row wins. Substrings are
# compared case-insensitively.
HYPERVISOR_DMI: tuple[tuple[str, str, str, str], ...] = (
    ("innotek", "", "", "virtualbox"),
    ("", "virtualbox", "", "virtualbox"),
    ("vmware", "", "", "vmware"),
    ("", "vmware", "", "vmware"),
    ("microsoft corporation", "virtual machine", "", "hyperv"),
    ("parallels", "", "", "parallels"),
    ("xen", "hvm domu", "", "xen"),
    ("freebsd", "bhyve", "", "bhyve"),
    ("", "bhyve", "", "bhyve"),
    ("qemu", "", "", "qemu"),
    ("", "standard pc (", "", "qemu"),
    ("", "kvm", "", "kvm"),
    ("red hat", "", "", "kvm"),
    ("openstack", "", "", "kvm"),
    ("google", "google compute engine", "", "kvm"),
    ("amazon ec2", "", "", "kvm"),
    ("digitalocean", "", "", "kvm"),
    # BIOS-vendor-only rows are weak evidence: classify_hypervisor ignores them unless the CPU reports a hypervisor.
    ("", "", "xen", "xen"),
    ("", "", "bhyve", "bhyve"),
    ("", "", "amazon ec2", "kvm"),
    ("", "", "seabios", "qemu"),
    ("", "", "edk ii", "qemu"),
)
# lscpu "Hypervisor vendor:" value -> hypervisor, used when DMI is generic.
HYPERVISOR_VENDOR_NAMES: tuple[tuple[str, str], ...] = (
    ("vmware", "vmware"),
    ("microsoft", "hyperv"),
    ("xen", "xen"),
    ("oracle", "virtualbox"),
    ("vbox", "virtualbox"),
    ("parallels", "parallels"),
    ("bhyve", "bhyve"),
    ("kvm", "kvm"),
    ("qemu", "qemu"),
)


def classify_hypervisor(sys_vendor: str, product_name: str, bios_vendor: str, *, cpu_flag: bool = False,
                        lscpu_vendor: str = "", sysfs_type: str = "") -> tuple[str, list[str]]:
    """Return (name, evidence). name is '' on bare metal."""
    vendor, product, bios = sys_vendor.lower(), product_name.lower(), bios_vendor.lower()
    for want_vendor, want_product, want_bios, name in HYPERVISOR_DMI:
        if not (want_vendor or want_product or want_bios):
            continue
        if (not want_vendor or want_vendor in vendor) and (not want_product or want_product in product) and (not want_bios or want_bios in bios):
            # BIOS-only rows are weak: only trust them when the CPU also reports a hypervisor
            # or the vendor/product strings agree, so a bare-metal board with an EDK II
            # build is not mistaken for QEMU.
            if want_bios and not (want_vendor or want_product) and not cpu_flag:
                continue
            return name, [f"dmi:{name}"]
    evidence: list[str] = []
    if sysfs_type:
        evidence.append("sysfs:" + sysfs_type)
        return ("xen" if sysfs_type.lower().startswith("xen") else sysfs_type.lower()), evidence
    lscpu = lscpu_vendor.lower()
    for fragment, name in HYPERVISOR_VENDOR_NAMES:
        if fragment in lscpu:
            return name, ["lscpu:" + lscpu_vendor.strip()]
    if cpu_flag:
        return "other", ["cpu_flag"]
    return "", []


# ---------------------------------------------------------------------------
# Peripherals
# ---------------------------------------------------------------------------

# USB vendor ids of fingerprint readers libfprint supports in bulk. A device
# only counts when it also exposes a vendor-specific (0xff) interface, which
# separates readers from the same vendors' touchpads and hubs.
FINGERPRINT_USB_VENDORS = frozenset({"06cb", "138a", "27c6", "147e", "1c7a", "08ff", "04f3", "2808", "10a5"})
USB_CLASS_VENDOR_SPECIFIC = "ff"
USB_CLASS_PRINTER = "07"
USB_CLASS_WIRELESS = "e0"

GAME_CONTROLLER_RE = re.compile(r"controller|dualsense|dualshock|gamepad|joystick|x-box|xbox|8bitdo|joy-con", re.I)
TOUCHPAD_RE = re.compile(r"touchpad|trackpad|synaptics|elan.*(?:tp|pad)|alps|cirque", re.I)
TOUCHSCREEN_RE = re.compile(r"touchscreen|touch screen|goodix|wacom.*touch|ilitek|egalax|elan.*touch|atmel.*touch|hid.*touch", re.I)

# Drivers that are interface-specific ethernet over USB or phones (tethering).
TETHERING_DRIVERS = frozenset({"rndis_host", "cdc_ether", "ipheth", "cdc_ncm"})
WWAN_DRIVERS = frozenset({"qmi_wwan", "cdc_mbim", "mhi_net"})
# Interface names that are never a physical adapter.
VIRTUAL_NIC_PREFIXES = ("docker", "veth", "virbr", "br-", "tun", "tap", "dummy", "tailscale", "wg", "vboxnet", "vmnet", "zt", "ifb", "bond", "sit", "ip6tnl", "ip_vti", "gre", "erspan", "nlmon", "podman", "cni", "flannel", "cali", "kube", "lxc", "lxd", "incus")

# Block device names that are never installation targets.
IGNORED_DISK_RE = re.compile(r"^(?:loop|ram|zram|nbd|sr|fd|dm-|md|rbd|nullb)\d*$|^mmcblk\d+(?:boot\d+|rpmb)$")

# ---------------------------------------------------------------------------
# Minimum disk sizes (GiB) by desktop, from the measured closure of the default sets.
# ---------------------------------------------------------------------------

# desktop -> (minimum root, recommended root) with the binhost; source builds add BUILD_EXTRA_GIB.
ROOT_SIZES_GIB: dict[str, tuple[int, int]] = {
    "minimal": (8, 15),
    "xfce": (14, 22),
    "gnome": (18, 28),
    "kde": (24, 40),
}
BUILD_EXTRA_GIB: dict[str, int] = {"minimal": 10, "xfce": 12, "gnome": 14, "kde": 16}


def distinct(values: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication."""
    return list(dict.fromkeys(values))


def merged(*mappings: Mapping[str, Iterable[str]]) -> dict[str, list[str]]:
    """Merge atom -> flags mappings, keeping first-seen flag order."""
    result: dict[str, list[str]] = {}
    for mapping in mappings:
        for atom, flags in mapping.items():
            result[atom] = distinct([*result.get(atom, []), *flags])
    return result
