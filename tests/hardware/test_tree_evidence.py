"""Verify what the planner emits against the pinned Gentoo tree.

Opt-in: set GENTOO_PINNED_TREE to a checkout of the ::gentoo snapshot the
installer targets (the tests skip without it, and without Portage). For every
plan of every fixture and scenario the test builds a Portage configuration from
the plan (ACCEPT_LICENSE stays @FREE, ACCEPT_KEYWORDS=amd64), asks Portage
whether each atom is visible, that every USE flag exists and that REQUIRED_USE
holds. The static checks (licence table, CPU flag names, init script names) read
the tree directly.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from gentoo_installer import hardware_data as data
from gentoo_installerd import hwplan

from tests.hardware.builders import gpu, manifest, profile, systemd
from tests.hardware.helpers import FIXTURE_NAMES, fixture_dict
from tests.hardware.test_plan_golden import SCENARIOS, scenario_manifest

TREE = Path(os.environ["GENTOO_PINNED_TREE"]) if os.environ.get("GENTOO_PINNED_TREE") else None
needs_tree = pytest.mark.skipif(TREE is None or not TREE.is_dir(), reason="set GENTOO_PINNED_TREE to the pinned ::gentoo snapshot")


def has_portage() -> bool:
    return subprocess.run([sys.executable, "-c", "import portage"], capture_output=True).returncode == 0


def make_conf(m, result) -> str:
    lines = [f"ACCEPT_LICENSE={shlex.quote(m.portage.accept_license)}", 'ACCEPT_KEYWORDS="amd64"']
    lines += [f"{key}={shlex.quote(value)}" for key, value in result.make_conf.items()]
    return "\n".join(lines) + "\n"


def case_for(case_id: str, m, machine) -> dict:
    result = hwplan.plan(m, machine)
    return {"id": case_id, "profile": m.system.profile, "make_conf": make_conf(m, result), "package_use": result.package_use,
            "package_license": result.package_license, "package_mask": result.package_mask, "atoms": result.all_atoms(), "plan": result.to_dict()}


def cases() -> list[dict]:
    out = []
    for name in FIXTURE_NAMES:
        machine = fixture_dict(name)
        for scenario in SCENARIOS:
            out.append(case_for(f"{name}/{scenario}", scenario_manifest(scenario, machine), machine))
    laptop = profile(form="laptop", vendor="intel", wifi=True, bluetooth=True, audio_vendors=("8086",), needs_sof=True,
                     gpus=(gpu("intel", "gen8+", integrated=True, boot=True), gpu("nvidia", "maxwell-volta")))
    amd = profile(form="laptop", vendor="amd", gpus=(gpu("amd", "gcn1-2"),))
    extras = {
        "tlp-minimal-dhcpcd": (manifest(system={"desktop": "minimal", "profile": "default/linux/amd64/23.0"}, network={"manager": "dhcpcd"}), laptop),
        "tlp-kde": (manifest(hardware={"power": "tlp"}), laptop),
        "printers-fwupd-fingerprint": (manifest(hardware={"printer_drivers": ["gutenprint", "hplip"], "fwupd": True, "fingerprint": True, "pipewire_alsa": True}), laptop),
        "nvidia-580-turing-gnome": (manifest(system={"desktop": "gnome", "kernel": "gentoo-kernel", "profile": "default/linux/amd64/23.0/desktop/gnome"},
                                             hardware={"gpu_driver": "nvidia", "nvidia_license_accepted": True, "nvidia_branch": "580"}),
                                    profile(gpus=(gpu("nvidia", "turing+"),))),
        "gcn-source": (manifest(portage={"binhost": False}), amd),
        "terascale-source": (manifest(portage={"binhost": False}), profile(vendor="amd", gpus=(gpu("amd", "terascale"),))),
        "r300-source": (manifest(portage={"binhost": False}), profile(vendor="amd", gpus=(gpu("amd", "r300"),))),
        "gen3-source": (manifest(portage={"binhost": False}), profile(gpus=(gpu("intel", "gen3", integrated=True),))),
        "virtualbox": (manifest(), profile(hypervisor="virtualbox", gpus=(gpu("virtualbox"),))),
        "virtualbox-minimal": (manifest(system={"desktop": "minimal", "profile": "default/linux/amd64/23.0"}), profile(hypervisor="virtualbox", gpus=(gpu("virtualbox"),))),
        "zram-systemd": (systemd(), profile(ram_gib=4)),
        "cron-less-trim": (manifest(options={"cron": False}), profile()),
        "portable-everything": (manifest(hardware={"target": "portable", "fwupd": True}), profile()),
    }
    for case_id, (m, machine) in extras.items():
        out.append(case_for(case_id, m, machine))
    return out


@needs_tree
@pytest.mark.skipif(not has_portage(), reason="the Portage Python API is not importable")
def test_every_atom_use_flag_and_licence_resolves_against_the_pinned_tree(tmp_path):
    requests = cases()
    scratch = tmp_path / "portage"
    process = subprocess.run([sys.executable, str(Path(__file__).with_name("tree_check.py")), str(TREE), str(scratch)], input=json.dumps(requests),
                             capture_output=True, text=True, timeout=1800)
    assert process.returncode == 0, process.stderr[-3000:]
    results = {entry["id"]: entry["atoms"] for entry in json.loads(process.stdout)}
    seen: set[str] = set()
    problems: list[str] = []
    for request in requests:
        for atom, outcome in results[request["id"]].items():
            seen.add(atom)
            if outcome["visible"] is None:
                problems.append(f"{request['id']}: {atom} is not installable under ACCEPT_LICENSE=@FREE and stable keywords: {outcome['why']}")
                continue
            if "amd64" not in outcome["keywords"].split():
                problems.append(f"{request['id']}: {atom}-{outcome['visible']} is not stable on amd64")
            if outcome["unknown_flags"]:
                problems.append(f"{request['id']}: {atom} has no USE flag(s) {outcome['unknown_flags']}")
            if not outcome["required_use_ok"]:
                problems.append(f"{request['id']}: REQUIRED_USE of {atom}-{outcome['visible']} fails with {outcome['enabled']}")
    assert not problems, "\n".join(problems)
    assert {hwplan.A_NVIDIA, hwplan.A_TLP, hwplan.A_PPD, hwplan.A_HPLIP, hwplan.A_ZRAM_GENERATOR, hwplan.A_VBOX_GA, hwplan.A_INTEL_UCODE, hwplan.A_SOF} <= seen
    nvidia_580 = results["nvidia-580-turing-gnome"][hwplan.A_NVIDIA]
    assert nvidia_580["visible"].startswith("nvidia-drivers-580.")        # the mask pins the long-term branch
    assert results["asus-desktop-rtx4090/nvidia-this"][hwplan.A_NVIDIA]["visible"].startswith("nvidia-drivers-595.")
    assert "kernel-open" in nvidia_580["enabled"]


@needs_tree
def test_the_nvidia_branch_table_matches_the_tree():
    ebuilds = sorted((TREE / "x11-drivers/nvidia-drivers").glob("nvidia-drivers-*.ebuild"))
    for branch, info in data.NVIDIA_BRANCHES.items():
        stable = [e for e in ebuilds if e.name.startswith(f"nvidia-drivers-{branch}.") and re.search(r'^KEYWORDS="[^"]*(?<![~-])\bamd64\b', e.read_text(), re.M)]
        assert stable, f"no stable amd64 ebuild for branch {branch}"
        text = stable[-1].read_text()
        assert str(info["license"]) in re.search(r'LICENSE="([^"]*)"', text).group(1).split()
        assert re.search(rf'SLOT="0/\$\{{PV%%\.\*\}}"', text)
        if info["mask"]:
            assert str(info["mask"]) == f">={data.NVIDIA_ATOM}-{int(branch) + 1}"
    # Auto picks the newest stable ebuild for branch 595 (it is not masked): a newer stable branch in a later snapshot must be noticed here.
    stable_branches = {re.match(r"nvidia-drivers-(\d+)\.", e.name).group(1) for e in ebuilds if re.search(r'^KEYWORDS="[^"]*(?<![~-])\bamd64\b', e.read_text(), re.M)}
    assert max(map(int, stable_branches)) == max(map(int, data.NVIDIA_BRANCHES)), \
        f"stable NVIDIA branches {sorted(stable_branches)} are newer than the planner's table {sorted(data.NVIDIA_BRANCHES)}"


@needs_tree
def test_the_licences_the_plans_name_are_real_and_everything_else_is_free():
    groups = {}
    for line in (TREE / "profiles/license_groups").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, _, members = line.partition(" ")
            groups[name] = members.split()

    def expand(name: str) -> set[str]:
        out: set[str] = set()
        for member in groups.get(name, []):
            out |= expand(member[1:]) if member.startswith("@") else {member}
        return out

    free = expand("FREE")
    for atom, licence in (("sys-kernel/linux-firmware", "linux-fw-redistributable"), ("sys-firmware/intel-microcode", "intel-ucode"), (data.NVIDIA_ATOM, "NVIDIA-2025")):
        assert (TREE / "licenses" / licence).exists(), licence
        assert licence not in free, f"{licence} is free: the package.license line would be needless"
    assert "BSD" in free        # sys-firmware/sof-firmware needs no licence line


@needs_tree
def test_cpu_flag_names_match_the_trees_description_and_the_renames_are_the_documented_ones():
    desc = (TREE / "profiles/desc/cpu_flags_x86.desc").read_text()
    names = {line.split(" - ")[0] for line in desc.splitlines() if " - " in line and not line.startswith("#")}
    assert names == set(data.CPU_FLAGS_X86_VALUES)
    for line in desc.splitlines():
        match = re.match(r"(\w+) - .*\(\[([^\]]+)\]( or \[([^\]]+)\])? in cpuinfo", line)
        if not match:
            continue
        flag = match.group(1)
        for cpuinfo_flag in filter(None, (match.group(2), match.group(4))):
            assert flag in data.cpuinfo_to_cpu_flags([cpuinfo_flag]), f"{cpuinfo_flag} should enable {flag}"


SERVICE_EVIDENCE = {
    # OpenRC service -> (atom, regex that must match the ebuild)
    "power-profiles-daemon": ("sys-power/power-profiles-daemon", r'newinitd\s+"[^"]*"\s+power-profiles-daemon'),
    "tlp": ("sys-power/tlp", r'newinitd\s+"[^"]*tlp\.init"\s+tlp\b'),
    "tlp-pd": ("sys-power/tlp", r'newinitd\s+"[^"]*tlp-pd\.init"\s+tlp-pd'),
    "thermald": ("sys-power/thermald", r'doinitd\s+"\$\{FILESDIR\}"/thermald'),
    "switcheroo-control": ("sys-power/switcheroo-control", r'newinitd\s+"\$\{FILESDIR\}"/\$\{PN\}-init\.d\s+\$\{PN\}'),
    "qemu-guest-agent": ("app-emulation/qemu-guest-agent", r'newinitd\s+"[^"]*"\s+qemu-guest-agent'),
    "spice-vdagent": ("app-emulation/spice-vdagent", r'newinitd\s+"[^"]*"\s+"\$\{PN\}"'),
    "vmware-tools": ("app-emulation/open-vm-tools", r'newinitd\s+"[^"]*open-vm-tools\.initd"\s+vmware-tools'),
    "virtualbox-guest-additions": ("app-emulation/virtualbox-guest-additions", r"newinitd\s+.*\$\{PN\}"),
    "cronie": ("sys-process/cronie", r'newinitd\s+"[^"]*"\s+\$\{PN\}'),
    "zram-init": ("sys-block/zram-init", r"rc-update add zram-init default"),
    # systemd units that the ebuild installs by name
    "vmtoolsd": ("app-emulation/open-vm-tools", r"vmtoolsd\.service"),
    "vgauthd": ("app-emulation/open-vm-tools", r"vgauthd\.service"),
}


@needs_tree
@pytest.mark.parametrize("service", sorted(SERVICE_EVIDENCE))
def test_service_names_exist_in_the_packages_ebuilds(service):
    atom, pattern = SERVICE_EVIDENCE[service]
    category, package = atom.split("/")
    texts = [path.read_text() for path in (TREE / category / package).glob("*.ebuild")]
    assert any(re.search(pattern, text) for text in texts), f"{service} is not installed by {atom}"


@needs_tree
def test_every_service_the_planner_emits_has_evidence_or_is_a_known_upstream_unit():
    upstream_units = {"fstrim.timer", "spice-vdagentd.socket", "qemu-guest-agent", "power-profiles-daemon", "switcheroo-control", "thermald", "tlp", "tlp-pd",
                      "virtualbox-guest-additions", "cronie", "cups", "cupsd"}
    emitted: set[str] = set()
    for request in cases():
        plan = request["plan"]
        emitted |= {name for name, _level in plan["services"] + plan["optional_services"]}
    unknown = emitted - set(SERVICE_EVIDENCE) - upstream_units
    assert not unknown, f"services without evidence: {sorted(unknown)}"


def test_the_planners_atom_constants_are_plain_category_package_atoms():
    atoms = [value for name, value in vars(hwplan).items() if name.startswith("A_")]
    assert len(atoms) >= 20 and all(re.fullmatch(r"[a-z0-9-]+/[A-Za-z0-9_+.-]+", atom) for atom in atoms)
    assert len(set(atoms)) == len(atoms)


def test_licence_names_the_plan_can_emit_are_these_three():
    emitted: set[str] = set()
    for name in FIXTURE_NAMES:
        machine = fixture_dict(name)
        for scenario in SCENARIOS:
            for items in hwplan.plan(scenario_manifest(scenario, machine), machine).package_license.values():
                emitted |= set(items)
    assert emitted <= {"linux-fw-redistributable", "intel-ucode", "NVIDIA-2025"}
