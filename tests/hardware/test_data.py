from __future__ import annotations

import json
from pathlib import Path

import pytest

from gentoo_installer import hardware_data as data


@pytest.mark.parametrize(("name", "device_id", "generation", "guess"), [
    ("AD102 [GeForce RTX 4090]", "2684", "ampere_plus", False),
    ("GB202 [GeForce RTX 5090]", "2b85", "blackwell", False),
    ("GB10 [GB10]", "2e12", "blackwell", False),
    ("GA102 [GeForce RTX 3090]", "2204", "ampere_plus", False),
    ("GA100 [A100 SXM4 40GB]", "20b0", "ampere_plus", False),  # the name wins: the id is among the Turing ones
    ("TU117M [GeForce GTX 1650 Ti Mobile]", "1f95", "turing", False),
    ("TU116 [GeForce GTX 1660 SUPER]", "21c4", "turing", False),
    ("GH100 [H100 PCIe]", "2331", "ampere_plus", False),
    ("GP104 [GeForce GTX 1080]", "1b80", "maxwell-volta", False),
    ("GM107 [GeForce GTX 750 Ti]", "1380", "maxwell-volta", False),
    ("GV100 [TITAN V]", "1d81", "maxwell-volta", False),
    ("GK104 [GeForce GTX 680]", "1180", "kepler", False),
    ("GF100 [GeForce GTX 480]", "06c0", "fermi", False),
    ("GT218 [GeForce 8400 GS Rev. 3]", "10c3", "older", False),
    ("G98 [GeForce 8400 GS]", "06e4", "older", False),
    ("NV43 [GeForce 6600]", "0141", "older", False),
    # Not in pci.ids: the id range decides, flagged as a guess.
    ("", "2d04", "blackwell", True),
    ("", "2684", "ampere_plus", True),
    ("", "1f95", "turing", True),
    ("", "1c82", "maxwell-volta", True),
    ("", "0fc6", "older", True),
    ("Tesla K80", "102d", "older", True),
])
def test_classify_nvidia(name, device_id, generation, guess):
    assert data.classify_nvidia(name, device_id) == (generation, guess)


@pytest.mark.parametrize(("generation", "branch"), [
    ("blackwell", "595"), ("ampere_plus", "595"), ("turing", "595"), ("turing+", "595"), ("maxwell-volta", "580"), ("kepler", ""), ("fermi", ""), ("older", ""), ("", ""),
])
def test_nvidia_branch_by_generation(generation, branch):
    assert data.nvidia_branch_for(generation) == branch


@pytest.mark.parametrize(("name", "driver", "family"), [
    ("Navi 22 [Radeon RX 6700/6700 XT/6750 XT / 6800M/6850M XT]", "amdgpu", "gcn3+"),
    ("Polaris 10", "amdgpu", "gcn3+"),
    ("Vega 10 XT [Radeon PRO SSG]", "amdgpu", "gcn3+"),
    ("Lucienne", "amdgpu", "gcn3+"),
    ("Tahiti PRO GL [FirePro Series]", "radeon", "gcn1-2"),
    ("Pitcairn XT [Radeon HD 7870 GHz Edition]", "radeon", "gcn1-2"),
    ("Bonaire XT [Radeon HD 7790/8770 / R7 360 / R9 260/360 OEM]", "radeon", "gcn1-2"),
    ("Hawaii PRO [Radeon R9 290/390]", "amdgpu", "gcn1-2"),
    ("Kaveri [Radeon R6 Graphics]", "radeon", "gcn1-2"),
    ("Cedar [Radeon HD 5000/6000/7350/8350 Series]", "radeon", "terascale"),
    ("Cypress PRO [Radeon HD 5850]", "radeon", "terascale"),
    ("Turks [Radeon HD 7650A/7670A]", "radeon", "terascale"),
    ("RV710 [Radeon HD 4550]", "radeon", "terascale"),
    ("RS780L [Radeon 3000]", "radeon", "terascale"),
    ("RS880M [Mobility Radeon HD 4100]", "radeon", "terascale"),
    ("R600 [Radeon HD 2900 PRO/XT]", "radeon", "terascale"),
    ("Wrestler [Radeon HD 6250]", "radeon", "terascale"),
    ("RV350 [Radeon 9600]", "radeon", "r300"),
    ("R420 [Radeon X800 AGP Series]", "radeon", "r300"),
    ("RV515/M52 [Mobility Radeon X1300]", "radeon", "r300"),
    ("R580 [Radeon X1900 GT]", "radeon", "r300"),
    ("RV280 [Radeon 9200 PRO]", "radeon", "older"),
    ("", "amdgpu", "gcn3+"),
    ("", "radeon", ""),
    ("", "", ""),
])
def test_classify_amd(name, driver, family):
    assert data.classify_amd(name, driver) == family


@pytest.mark.parametrize(("name", "integrated"), [
    ("Lucienne", True), ("Renoir [Radeon Vega Series / Radeon Vega Mobile Series]", True), ("Rembrandt [Radeon 680M]", True),
    ("Phoenix1", True), ("Raphael", True), ("Cezanne [Radeon Vega Series / Radeon Vega Mobile Series]", True),
    ("Navi 22 [Radeon RX 6700/6700 XT/6750 XT / 6800M/6850M XT]", False), ("Navi 33 [Radeon RX 7600M XT]", False),
    ("Polaris 22 XT [Radeon RX Vega M GH]", False), ("Baffin [Polaris11]", False), ("", False),
])
def test_amd_integrated(name, integrated):
    assert data.amd_is_integrated(name) is integrated


@pytest.mark.parametrize(("name", "generation"), [
    ("Alder Lake-S GT1 [UHD Graphics 770]", "gen8+"),
    ("TigerLake-LP GT2 [Iris Xe Graphics]", "gen8+"),
    ("HD Graphics 630", "gen8+"),
    ("Skylake GT2 [HD Graphics 520]", "gen8+"),
    ("Haswell-ULT Integrated Graphics Controller", "gen6-7"),
    ("3rd Gen Core processor Graphics Controller", "gen6-7"),
    ("2nd Generation Core Processor Family Integrated Graphics Controller", "gen6-7"),
    ("Atom Processor Z36xxx/Z37xxx Series Graphics & Display", "gen6-7"),
    ("Core Processor Integrated Graphics Controller", "gen4-5"),
    ("Mobile 4 Series Chipset Integrated Graphics Controller", "gen4-5"),
    ("82G33/G31 Express Integrated Graphics Controller", "gen3"),
    ("82945G/GZ Integrated Graphics Controller", "gen3"),
    ("Atom Processor D4xx/D5xx/N4xx/N5xx Integrated Graphics Controller", "gen3"),
    ("Atom Processor D2000/N2000 Series Integrated Graphics Controller", "gen3"),
    ("", "gen8+"),
])
def test_classify_intel(name, generation):
    assert data.classify_intel_gpu(name) == generation


@pytest.mark.parametrize(("name", "discrete"), [
    ("DG2 [Arc A770]", True), ("BMG G21 [Arc B580]", True), ("DG1 [Iris Xe MAX Graphics]", True),
    ("Alder Lake-S GT1 [UHD Graphics 770]", False), ("TigerLake-LP GT2 [Iris Xe Graphics]", False),
])
def test_intel_discrete(name, discrete):
    assert data.intel_is_discrete(name) is discrete


@pytest.mark.parametrize(("vendor", "product", "bios", "flag", "lscpu", "expected"), [
    ("QEMU", "Standard PC (Q35 + ICH9, 2009)", "EDK II", True, "KVM", "qemu"),
    ("QEMU", "Standard PC (i440FX + PIIX, 1996)", "SeaBIOS", False, "", "qemu"),
    ("innotek GmbH", "VirtualBox", "innotek GmbH", True, "KVM", "virtualbox"),
    ("VMware, Inc.", "VMware Virtual Platform", "Phoenix Technologies LTD", True, "VMware", "vmware"),
    ("VMware, Inc.", "VMware20,1", "VMware, Inc.", True, "VMware", "vmware"),
    ("Microsoft Corporation", "Virtual Machine", "Microsoft Corporation", True, "Microsoft", "hyperv"),
    ("Xen", "HVM domU", "Xen", True, "Xen", "xen"),
    ("Parallels Software International Inc.", "Parallels Virtual Platform", "Parallels Software International Inc.", True, "", "parallels"),
    ("FreeBSD", "BHYVE", "BHYVE", True, "", "bhyve"),
    ("Amazon EC2", "t3.micro", "Amazon EC2", True, "KVM", "kvm"),
    ("Red Hat", "KVM", "SeaBIOS", True, "KVM", "kvm"),
    ("Unknown Cloud", "Instance", "BIOS", True, "KVM", "kvm"),
    ("Unknown Cloud", "Instance", "BIOS", True, "", "other"),
    ("ASUS", "System Product Name", "American Megatrends Inc.", False, "", ""),
    ("Microsoft Corporation", "Surface Pro 7", "Microsoft Corporation", False, "", ""),
    ("Google", "Link", "coreboot", False, "", ""),
    # A bare-metal board that ships EDK II firmware must not look like QEMU without a hypervisor flag.
    ("Some Vendor", "Mini PC", "EDK II", False, "", ""),
])
def test_classify_hypervisor(vendor, product, bios, flag, lscpu, expected):
    assert data.classify_hypervisor(vendor, product, bios, cpu_flag=flag, lscpu_vendor=lscpu)[0] == expected


def test_hypervisor_from_sysfs_type():
    assert data.classify_hypervisor("", "", "", sysfs_type="xen")[0] == "xen"


@pytest.mark.parametrize(("chassis", "battery", "hypervisor", "expected"), [
    (3, False, "", "desktop"), (4, False, "", "desktop"), (7, False, "", "desktop"), (13, False, "", "desktop"), (35, False, "", "desktop"),
    (8, True, "", "laptop"), (9, True, "", "laptop"), (10, True, "", "laptop"), (14, True, "", "laptop"), (31, True, "", "laptop"),
    (30, True, "", "tablet"), (32, True, "", "tablet"),
    (1, True, "", "laptop"), (2, True, "", "laptop"), (3, True, "", "laptop"),  # laptops that report a desktop chassis
    (1, False, "", "desktop"), (None, False, "", "other"),
    (17, False, "", "server"), (23, True, "", "server"), (28, False, "", "server"),
    (1, False, "qemu", "vm"), (10, True, "vmware", "vm"),
])
def test_form_factor(chassis, battery, hypervisor, expected):
    assert data.form_factor(chassis, battery, hypervisor) == expected


def test_x86_64_levels():
    v1 = "fpu lm sse2 mmx".split()
    v2 = v1 + "cx16 lahf_lm popcnt sse4_1 sse4_2 ssse3".split()
    v3 = v2 + "avx avx2 bmi1 bmi2 f16c fma abm movbe xsave".split()
    v4 = v3 + "avx512f avx512bw avx512cd avx512dq avx512vl".split()
    assert [data.x86_64_level(flags) for flags in (v1, v2, v3, v4)] == [1, 2, 3, 4]
    assert data.x86_64_level(["fpu", "sse2"]) == 0  # no long mode
    assert data.x86_64_level(v3 + ["avx512f"]) == 3  # a partial AVX-512 set is still level 3
    assert data.x86_64_level(v1 + ["avx", "avx2"]) == 1  # levels are cumulative


def test_cpuinfo_flag_mapping_uses_the_renames_in_the_gentoo_description():
    flags = "fpu sse sse2 pni ssse3 fma pclmulqdq sha_ni abm popcnt avx avx2 aes rdrand avx512f avx512_vnni xyz".split()
    assert data.cpuinfo_to_cpu_flags(flags) == sorted(
        {"sse", "mmxext", "sse2", "sse3", "ssse3", "fma3", "pclmul", "sha", "popcnt", "avx", "avx2", "aes", "rdrand", "avx512f", "avx512_vnni"})


@pytest.mark.parametrize(("vendor_id", "expected"), [
    ("GenuineIntel", "intel"), ("AuthenticAMD", "amd"), ("HygonGenuine", "hygon"), ("CentaurHauls", "other"), ("", ""), (" ", ""),
])
def test_cpu_vendor(vendor_id, expected):
    assert data.cpu_vendor(vendor_id) == expected


@pytest.mark.parametrize("name", ["loop0", "ram0", "zram0", "sr0", "fd0", "dm-3", "md127", "nbd1", "mmcblk0boot0", "mmcblk1rpmb"])
def test_ignored_disks(name):
    assert data.IGNORED_DISK_RE.match(name)


@pytest.mark.parametrize("name", ["sda", "nvme0n1", "vda", "mmcblk0", "xvda", "hda", "mmcblk0p1"])
def test_offered_disks(name):
    assert name == "mmcblk0p1" or not data.IGNORED_DISK_RE.match(name)


def test_merged_keeps_order_and_deduplicates():
    assert data.merged({"a/b": ["x", "y"]}, {"a/b": ["y", "z"], "c/d": ["q"]}) == {"a/b": ["x", "y", "z"], "c/d": ["q"]}
    assert data.distinct(["b", "a", "b"]) == ["b", "a"]


# -- the oracle: every id of NVIDIA's own list of supported cards (a slice of the 615.78.08 driver's supported-gpus.json) -------

SLICE = json.loads((Path(__file__).parent / "data" / "nvidia-supported-gpus-slice.json").read_text())["gpus"]


def _outcome(generation: str) -> str:
    """What a generation means for the driver choice: open (595), maxwell-volta (580) or legacy (nouveau only)."""
    if generation in data.OPEN_MODULE_GENERATIONS:
        return "open"
    return "maxwell-volta" if generation == "maxwell-volta" else "legacy"


def _expected(row: dict) -> str:
    return {"": "open", "580": "maxwell-volta"}.get(row["branch"], "legacy")


def test_the_slice_is_not_empty_and_covers_every_branch():
    assert len(SLICE) > 400
    assert {row["branch"] for row in SLICE} >= {"", "580", "470", "390", "367"}


@pytest.mark.parametrize("row", SLICE, ids=lambda row: row["id"])
def test_every_listed_card_gets_the_branch_nvidia_lists_for_it(row):
    named, _ = data.classify_nvidia(row["name"], row["id"])
    guessed, guess = data.classify_nvidia("", row["id"])
    assert _outcome(named) == _expected(row), (row, named)
    assert _outcome(guessed) == _expected(row), (row, guessed)
    assert guess is True


def test_blackwell_ids_are_blackwell():
    blackwell = [row for row in SLICE if row["name"].startswith("GB")]
    assert len(blackwell) > 20
    assert all(data.classify_nvidia(row["name"], row["id"]) == ("blackwell", False) for row in blackwell)
    assert all(data.classify_nvidia("", row["id"])[0] == "blackwell" for row in blackwell)


def test_ids_the_pci_database_does_not_know_yet_are_still_sorted_by_range():
    unnamed = [row for row in SLICE if not row["name"] and row["branch"] == ""]
    assert unnamed  # the newest Blackwell boards
    assert all(data.classify_nvidia("", row["id"])[0] in data.OPEN_MODULE_GENERATIONS for row in unnamed)


def test_kepler_boards_in_the_grid_range_are_legacy_either_way():
    # Three GRID Kepler boards (branch 367) are listed apart from the Kepler 470 branch; both end in nouveau here.
    grid = [row for row in SLICE if row["branch"] == "367"]
    assert len(grid) == 3
    assert all(_outcome(data.classify_nvidia(row["name"], row["id"])[0]) == "legacy" for row in grid)


@pytest.mark.parametrize(("name", "slot", "integrated"), [
    ("Meteor Lake-P [Intel Arc Graphics]", "0000:00:02.0", True),
    ("Lunar Lake [Intel Arc Graphics 130V / 140V]", "0000:00:02.0", True),
    ("Arrow Lake-P [Intel Graphics]", "0000:00:02.0", True),
    ("Meteor Lake-P [Intel Arc Graphics]", "", True),   # no slot: the name has no model, so it is the processor's graphics
    ("DG2 [Arc A770]", "0000:03:00.0", False),
    ("BMG G21 [Arc B580]", "0000:03:00.0", False),
    ("DG2 [Arc A770]", "", False),
    ("Arc Pro A40", "0000:03:00.0", False),
    ("DG1 [Iris Xe MAX Graphics]", "0000:03:00.0", False),
    ("UHD Graphics 770", "0000:00:02.0", True),
])
def test_intel_arc_graphics_of_the_processor_is_integrated(name, slot, integrated):
    assert (not data.intel_is_discrete(name, slot)) is integrated
