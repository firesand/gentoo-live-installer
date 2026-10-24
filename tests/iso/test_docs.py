"""The documentation says what the code does: Secure Boot first, working links, real options."""
from __future__ import annotations

from pathlib import Path
import re

import pytest

from .conftest import ROOT

README = ROOT / "README.md"
USER_GUIDE = ROOT / "docs/USER-GUIDE.md"
ISO_DOC = ROOT / "docs/ISO.md"
BUILD_SCRIPT = (ROOT / "scripts/build-iso.sh").read_text()


def test_secure_boot_is_on_the_first_screen_of_the_readme() -> None:
    first_screen = "\n".join(README.read_text().splitlines()[:25])
    assert "Secure Boot" in first_screen and "must be off" in first_screen.lower().replace("**", "")


def test_the_disc_carries_the_same_secure_boot_instructions() -> None:
    notice = (ROOT / "iso/cd_overlay/SECURE-BOOT-OFF.txt").read_text()
    for needle in ("Secure Boot", "BitLocker", "Disabled", "Apple Silicon"):
        assert needle in notice
    assert max(len(line) for line in notice.splitlines()) <= 80
    assert "SECURE-BOOT-OFF.txt" in (ROOT / "iso/README-live.txt").read_text()


@pytest.mark.parametrize("document", [README, USER_GUIDE, ISO_DOC])
def test_relative_links_resolve(document: Path) -> None:
    text = document.read_text()
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        assert (document.parent / target).exists(), f"{document.name} links to {target}, which does not exist"
    for anchor in re.findall(r"\]\(#([^)]+)\)", text):
        headings = {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-") for h in re.findall(r"^#+ (.*)$", text, re.M)}
        assert anchor in headings, f"{document.name} links to #{anchor}"


def test_every_documented_option_and_variable_exists_in_the_script() -> None:
    documented = set(re.findall(r"`(--[a-z0-9-]+)", ISO_DOC.read_text()))
    documented -= {"--flavor", "--previous-manifest", "--reinstall-atoms", "--jobs"}
    for option in documented:
        assert option in BUILD_SCRIPT, f"{option} is documented but build-iso.sh does not know it"
    for variable in ("JOBS", "LOAD_AVERAGE", "BUILD_MEMORY", "BUILD_MIN_FREE_GIB", "ISO_CACHE_DIR", "ISO_DIST_DIR", "ISO_LOG_DIR", "ISO_SECCOMP", "ISO_KEEP_STAGE1"):
        assert variable in BUILD_SCRIPT and variable in ISO_DOC.read_text()


def test_documented_test_iso_name_matches_the_builder(pins: dict[str, str]) -> None:
    expected = pins["ISO_NAME"].replace(".iso", "-TESTKEY.iso")
    assert expected in README.read_text()
    assert "dist/test-only/" + pins["ISO_NAME"] not in README.read_text(), "the old test ISO name must not be documented"
    assert pins["ISO_VOLID"].replace("_AMD64", "_TESTKEY") in README.read_text()


def test_documentation_does_not_promise_what_the_kernel_cannot_do() -> None:
    for document in (README, USER_GUIDE, ISO_DOC):
        text = document.read_text()
        assert not re.search(r"safe[- ]graphics (entry|mode) (works|gives)", text, re.I)
    assert "no generic framebuffer driver" in README.read_text()
    assert "simpledrm" in USER_GUIDE.read_text()


def test_hardware_facts_in_the_readme_match_the_image_contract() -> None:
    text = README.read_text()
    assert "bootia32.efi" in text and "bootx64.efi" in text
    import json

    contract = json.loads((ROOT / "iso/image-contract.json").read_text())
    assert "efi/boot/bootia32.efi" in contract["iso"]["required_files"], "the 32-bit UEFI loader claim is enforced"


def test_cheat_sheet_commands_name_tools_that_are_on_the_disc() -> None:
    contract_commands = set(__import__("json").loads((ROOT / "iso/image-contract.json").read_text())["squashfs"]["commands"])
    guide = USER_GUIDE.read_text()
    for tool in ("cryptsetup", "mdadm", "smartctl", "nvme", "efibootmgr", "xfs_repair", "ddrescue", "testdisk", "hdparm", "lshw", "dmidecode", "lspci", "lsusb", "ntfsfix"):
        if re.search(rf"`{tool}\b|\b{tool}\b", guide):
            assert tool in contract_commands or tool == "testdisk", tool
