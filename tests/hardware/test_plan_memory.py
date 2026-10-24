"""B2 (wave 4): the plan limits the compile jobs of the few packages that need far more memory per job than MAKEOPTS assumes.

The first VM acceptance run built dev-python/pyside with -j2 on a 4 GiB machine (3.8 GiB of memory, a 512 MiB swap
file) and the kernel killed the compiler. The planner is the one place that decides what the hardware allows, so it
writes the limit as a package.env entry for a short list of packages; configure.py only writes the plan's files.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

from gentoo_installer import recommend
from gentoo_installer.manifest import ATOM_RE
from gentoo_installerd import configure, hwplan

from tests.backend.target_helpers import configurer
from tests.hardware.builders import manifest, profile

ENV_FILE = "/etc/portage/env/installer-memory"
PACKAGE_ENV = "/etc/portage/package.env/installer-hardware"
TREE = Path(os.environ["GENTOO_PINNED_TREE"]) if os.environ.get("GENTOO_PINNED_TREE") else None
needs_tree = pytest.mark.skipif(TREE is None or not TREE.is_dir(), reason="set GENTOO_PINNED_TREE to the pinned ::gentoo snapshot")


def planned(ram_gib: float, threads: int = 8, **sections):
    return hwplan.plan(manifest(**sections), profile(ram_gib=ram_gib, threads=threads))


def files_of(result) -> dict[str, str]:
    return {path: content for path, content, _mode in result.files}


def listed(package_env: str) -> list[tuple[str, str]]:
    return [(fields[0], fields[1]) for fields in (line.split() for line in package_env.splitlines() if line.strip() and not line.startswith("#"))]


# -- the formula -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("total_gib", "swap_mib", "jobs"), [
    (0, 0, 1),              # nothing known
    (3.8, 512, 1),          # the 4 GiB VM of the acceptance run
    (7.7, 0, 1),            # an 8 GiB PC reports about 7.7 GiB
    (7.7, 4096, 2),
    (15.5, 4096, 4),
    (46.7, 8192, 12),       # 48 GiB
    (62, 8192, 16),
])
def test_four_gib_of_memory_or_eight_of_swap_buy_one_job_of_the_heavy_packages(total_gib, swap_mib, jobs):
    assert recommend.memory_heavy_jobs(int(total_gib * recommend.KIB_PER_GIB), swap_mib) == jobs


@pytest.mark.parametrize(("text", "jobs"), [
    ("-j4", 4), ("-j4 -l8", 4), ("--jobs=6", 6), ("-j 3", 3), ("--jobs 5 -l2", 5), ("-j2 -j7", 7),   # the last one wins, as in make
    ("", None), ("-l4", None), ("-j", None), ("-j0", None), ("--jobs -l4", None), ("--jobserver-auth=3,4", None), ("-jfoo", None),
])
def test_the_jobs_of_a_makeopts_text(text, jobs):
    assert recommend.make_opts_jobs(text) == jobs


# -- the plan ----------------------------------------------------------------------------------------------------------

def test_the_machine_of_the_acceptance_run_gets_one_job_for_pyside():
    result = planned(3.8, threads=2, portage={"make_opts": "-j2"}, storage={"swap_mib": 512})
    files = files_of(result)
    assert files[ENV_FILE].splitlines()[-1] == 'MAKEOPTS="-j1"'
    assert listed(files[PACKAGE_ENV]) == [(atom, "installer-memory") for atom in hwplan.MEMORY_HEAVY_ATOMS]
    assert ("dev-python/pyside", "installer-memory") in listed(files[PACKAGE_ENV])
    assert result.scalars["memory_heavy_jobs"] == 1
    assert any("one compile job" in line for line in result.summary)
    assert any("get 1 compile job when" in note and "With 3.8 GiB of memory" in note for note in result.notes)


def test_a_roomier_machine_keeps_more_jobs_but_fewer_than_the_machine_wide_setting():
    result = planned(16, threads=8, storage={"swap_mib": 4096})            # MAKEOPTS would be -j8; four jobs of 4 GiB fit
    assert files_of(result)[ENV_FILE].splitlines()[-1] == 'MAKEOPTS="-j4"'
    assert result.scalars["memory_heavy_jobs"] == 4
    assert not any("one compile job" in line for line in result.summary)    # only the one-job case is worth a line in the summary


def test_no_limit_is_written_when_the_machine_wide_setting_is_not_higher():
    for kwargs in ({"ram_gib": 8, "threads": 2}, {"ram_gib": 64, "threads": 8}, {"ram_gib": 2, "threads": 1}):
        result = planned(storage={"swap_mib": 4096}, **kwargs)
        assert ENV_FILE not in files_of(result) and PACKAGE_ENV not in files_of(result), kwargs
        assert "memory_heavy_jobs" not in result.scalars


def test_the_machine_wide_setting_is_the_one_the_user_typed_when_there_is_one():
    assert ENV_FILE not in files_of(planned(3.8, threads=8, portage={"make_opts": "-j1"}))                # already one job
    assert files_of(planned(16, threads=8, portage={"make_opts": "-j16"}))[ENV_FILE].endswith('MAKEOPTS="-j4"\n')
    assert files_of(planned(16, threads=8, portage={"make_opts": "-l4"}))[ENV_FILE].endswith('MAKEOPTS="-j4"\n')      # no -j at all: unlimited


def test_the_swap_that_counts_is_the_one_the_installation_creates():
    small = planned(7.7, threads=8, portage={"make_opts": "-j8"}, storage={"swap_mib": 0})
    assert files_of(small)[ENV_FILE].endswith('MAKEOPTS="-j1"\n')
    larger = planned(7.7, threads=8, portage={"make_opts": "-j8"}, storage={"swap_mib": 4096})
    assert files_of(larger)[ENV_FILE].endswith('MAKEOPTS="-j2"\n')


def test_an_undetected_machine_gets_no_limit():
    result = hwplan.plan(manifest(), None)
    assert ENV_FILE not in files_of(result)


def test_the_plan_survives_its_state_file():
    result = planned(3.8, threads=2, portage={"make_opts": "-j2"}, storage={"swap_mib": 512})
    again = hwplan.HardwarePlan.from_dict(result.to_dict())
    assert files_of(again) == files_of(result)
    redacted = result.to_dict(redact_files=True)
    assert {item["path"] for item in redacted["files"]} >= {ENV_FILE, PACKAGE_ENV}   # the support copy keeps paths and hashes


# -- the list ----------------------------------------------------------------------------------------------------------

def test_the_list_is_short_and_made_of_atoms_without_a_version():
    assert 3 <= len(hwplan.MEMORY_HEAVY_ATOMS) <= 8
    assert len(set(hwplan.MEMORY_HEAVY_ATOMS)) == len(hwplan.MEMORY_HEAVY_ATOMS)
    for atom in hwplan.MEMORY_HEAVY_ATOMS:
        assert ATOM_RE.fullmatch(atom) and not re.search(r"-\d", atom), atom
    assert "dev-python/pyside" in hwplan.MEMORY_HEAVY_ATOMS


# -- configure.py applies it -------------------------------------------------------------------------------------------

def test_configure_and_the_planner_agree_on_the_names():
    assert configure.MEMORY_ENV_FILE == ENV_FILE.lstrip("/")
    assert configure.PACKAGE_ENV_FILE == PACKAGE_ENV.lstrip("/")


def test_the_written_files_are_what_the_configurer_reads_back():
    result = planned(3.8, threads=2, portage={"make_opts": "-j2"}, storage={"swap_mib": 512})
    with tempfile.TemporaryDirectory() as temporary:
        c = configurer(temporary)
        c.write_plan_files(result)
        (Path(temporary) / "etc/portage/make.conf").write_text("MAKEOPTS='-j4 -l4'\n")
        assert c.compile_jobs("dev-python/pyside-6.11.2") == 1       # its own limit
        assert c.compile_jobs("dev-qt/qtwebengine-6.11.2-r1") == 1
        assert c.compile_jobs("app-misc/hwmonux-1.0.0-r1") == 4      # everything else follows MAKEOPTS
        # a limit above MAKEOPTS cannot raise the jobs of a package
        (Path(temporary) / "etc/portage/env/installer-memory").write_text('MAKEOPTS="-j9"\n')
        assert c.compile_jobs("dev-python/pyside-6.11.2") == 4


# -- evidence from the pinned tree (opt-in, like test_tree_evidence.py) ---------------------------------------------------

# atom -> (a package that pulls it in, a regex its ebuilds must match): the installer's package sets reach every atom of the list
REACHED_BY = {
    "dev-qt/qtwebengine": ("kde-plasma/kdeplasma-addons", r"qtwebengine"),       # plasma-meta -> kdeplasma-addons[webengine]
    "llvm-core/llvm": ("media-libs/mesa", r"llvm-core/llvm:"),                    # mesa[llvm], which every graphical desktop has
    "llvm-core/clang": ("dev-python/pyside", r"llvm-core/clang:"),                # pyside needs clang to generate its bindings
    "net-libs/webkit-gtk": ("gnome-extra/yelp", r"net-libs/webkit-gtk"),          # gnome-core-apps -> yelp
}
# atom -> (a package, a regex its ebuilds must match): why the atom needs more memory per job than the rest
HEAVY_BECAUSE = {
    "dev-qt/qtwebengine": ("dev-qt/qtwebengine", r"CHECKREQS_MEMORY=.*makeopts_jobs"),
    "llvm-core/llvm": ("llvm-core/llvm", r"very high memory consumption while linking"),
    "net-libs/webkit-gtk": ("net-libs/webkit-gtk", r'append-ldflags .*--no-keep-memory'),
}
META_CHAIN = (("kde-plasma/plasma-meta", "kde-plasma/kdeplasma-addons"), ("gnome-base/gnome", "gnome-base/gnome-core-apps"),
              ("gnome-base/gnome-core-apps", "gnome-extra/yelp"))


def ebuild_texts(atom: str) -> list[str]:
    category, name = atom.split("/")
    return [path.read_text() for path in (TREE / category / name).glob("*.ebuild")]


@needs_tree
def test_every_heavy_atom_is_in_the_tree_with_a_stable_amd64_ebuild():
    for atom in hwplan.MEMORY_HEAVY_ATOMS:
        texts = ebuild_texts(atom)
        assert texts, f"{atom} is not in the pinned tree"
        assert any(re.search(r'^\s*KEYWORDS="[^"]*(?<![~-])\bamd64\b', text, re.M) for text in texts), f"{atom} has no stable amd64 ebuild"


@needs_tree
def test_every_heavy_atom_is_reached_by_a_package_set_and_is_heavy():
    for atom in hwplan.MEMORY_HEAVY_ATOMS:
        if atom == "dev-python/pyside":
            continue    # HWMonux's own ebuild (packaging/gentoo-overlay) and the acceptance run, checked below
        package, pattern = REACHED_BY[atom]
        assert any(re.search(pattern, text) for text in ebuild_texts(package)), f"{package} no longer pulls in {atom}"
        if atom in HEAVY_BECAUSE:
            package, pattern = HEAVY_BECAUSE[atom]
            assert any(re.search(pattern, text) for text in ebuild_texts(package)), f"the ebuilds of {package} no longer say that {atom} needs much memory"
    for parent, child in META_CHAIN:
        assert any(child.split("/")[1] in text for text in ebuild_texts(parent)), f"{parent} no longer includes {child}"
    overlay = Path(__file__).resolve().parents[2] / "packaging/gentoo-overlay/app-misc/hwmonux"
    assert any("dev-python/pyside" in path.read_text() for path in overlay.glob("*.ebuild"))
