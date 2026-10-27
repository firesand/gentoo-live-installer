"""The documentation says what the code does: Secure Boot first, working links, real options."""
from __future__ import annotations

from pathlib import Path
import re

import pytest

from .conftest import ROOT, load_script

README = ROOT / "README.md"
USER_GUIDE = ROOT / "docs/USER-GUIDE.md"
ISO_DOC = ROOT / "docs/ISO.md"
BUILD_SCRIPT = (ROOT / "scripts/build-iso.sh").read_text()


def test_secure_boot_is_on_the_first_screen_of_the_readme() -> None:
    """The one step a Secure Boot PC needs (MokManager) and the way out when the PC trusts only Windows, before anything else."""
    first_screen = "\n".join(README.read_text().splitlines()[:25]).replace("**", "")
    for needle in ("Secure Boot", "Enroll key from disk", "GENTOO_EFI", "ENROLL_THIS_KEY_IN_MOKMANAGER.cer", "Allow Microsoft 3rd Party UEFI CA", "BitLocker"):
        assert needle in first_screen, needle


def test_the_disc_carries_the_same_secure_boot_instructions() -> None:
    notice = (ROOT / "iso/cd_overlay/SECURE-BOOT.txt").read_text()
    for needle in ("Secure Boot", "BitLocker", "Disabled", "Apple Silicon", "Enroll key from disk", "GENTOO_EFI",
                   "ENROLL_THIS_KEY_IN_MOKMANAGER.cer", "Allow Microsoft 3rd Party UEFI CA", ".secureboot.txt", "ASUS laptop: F2 (boot menu Esc)"):
        assert needle in notice, needle
    assert max(len(line) for line in notice.splitlines()) <= 80
    assert "SECURE-BOOT.txt" in (ROOT / "iso/README-live.txt").read_text()


def test_an_old_disc_key_is_to_be_removed_not_left() -> None:
    # EAC Linux v2 (2026-10) accepts only enrolled MOKs that Epic recognizes: a leftover disc key would fail it on that PC
    for document in (ROOT / "iso/cd_overlay/SECURE-BOOT.txt", USER_GUIDE):
        text = " ".join(document.read_text().split())
        assert "you can leave it" not in text and "mokutil --delete" in text and "Easy Anti-Cheat Linux v2" in text, document.name


def test_the_names_on_the_blue_screens_are_the_names_the_signer_uses() -> None:
    """MokManager lists the volume by its label and the file by its name: the instructions must say exactly those."""
    signer = load_script("iso/sign-secure-boot.py", "sign_secure_boot")
    for document in (ROOT / "iso/cd_overlay/SECURE-BOOT.txt", README, USER_GUIDE):
        text = document.read_text()
        assert signer.EFI_LABEL in text and signer.CERT_NAME in text, document.name


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
    documented -= {"--flavor", "--previous-manifest", "--reinstall-atoms", "--jobs", "--usepkg-exclude"}
    check_atoms = (ROOT / "iso/check-atoms.py").read_text()  # section 11 documents its options too
    for option in documented:
        assert option in BUILD_SCRIPT or f'"{option}"' in check_atoms, f"{option} is documented but build-iso.sh does not know it"
    for variable in ("JOBS", "LOAD_AVERAGE", "BUILD_MEMORY", "BUILD_MIN_FREE_GIB", "ISO_CACHE_DIR", "ISO_DIST_DIR", "ISO_LOG_DIR", "ISO_SECCOMP", "ISO_KEEP_STAGE1"):
        assert variable in BUILD_SCRIPT and variable in ISO_DOC.read_text()


def test_reuse_auto_is_described_as_it_behaves() -> None:
    """--reuse auto reuses a stage1 that nothing changed and builds stage1 fresh otherwise. The reseed that reinstalls only the changed
    project packages did not reinstall them in a real build: it runs only for BUILD_MODE=reseed inside the container (iso/builder-entrypoint.sh)."""
    usage = BUILD_SCRIPT.split("--reuse MODE", 1)[1].split("--reuse-stage1", 1)[0]
    lines = ISO_DOC.read_text().splitlines()
    row = next(line for line in lines if line.startswith("| `--reuse auto`"))
    reseed = next(line for line in lines if line.startswith("* **reseed**"))
    for name, described in (("the usage text of build-iso.sh", usage), ("the --reuse auto row of docs/ISO.md", row)):
        assert "fresh" in described and "reinstall" not in described, name
    assert "BUILD_MODE=reseed" in reseed and "--reuse auto" in reseed and "not" in reseed
    assert "needs no revision bump" not in ISO_DOC.read_text() and "reuse logic reinstalls it by name" not in ISO_DOC.read_text()


def test_documented_test_iso_name_matches_the_builder(pins: dict[str, str]) -> None:
    expected = pins["ISO_NAME"].replace(".iso", "-TESTKEY.iso")
    assert expected in README.read_text()
    assert "dist/test-only/" + pins["ISO_NAME"] not in README.read_text(), "the old test ISO name must not be documented"
    assert pins["ISO_VOLID"].replace("_AMD64", "_TESTKEY") in README.read_text()


def test_no_document_suggests_a_test_iso_in_or_below_dist() -> None:
    """build-iso.sh refuses a test-key ISO in dist/ and in every directory below it, so no example may suggest one."""
    for document in (README, USER_GUIDE, ISO_DOC, ROOT / "docs/TESTING.md"):
        text = document.read_text()
        for value in re.findall(r"ISO_DIST_DIR=(\S+)", text):
            assert not re.match(r"[\"']?(\./)?dist(/|[\"']?$)", value), f"{document.name} builds a test ISO into {value}"
        assert "dist/test-only" not in text, f"{document.name} still names dist/test-only"


def test_no_document_offers_speech_output_the_disc_does_not_have() -> None:
    """The disc has no speech synthesizer, screen reader or braille software. A document may say so, but must not offer them."""
    for document in (README, USER_GUIDE, ISO_DOC, ROOT / "iso/README-live.txt"):
        text = document.read_text()
        assert document == ISO_DOC or "dospeakup" not in text, document.name
        assert not re.search(r"(?im)^\s*(\|\s*)?accessibility\b", text), f"{document.name} lists an Accessibility boot entry"
        for line in text.splitlines():
            if re.search(r"speech|speak|screen reader|braille", line, re.I):
                assert re.search(r"\b(no|not|cannot|without)\b", line, re.I), f"{document.name} offers speech output: {line.strip()}"


def test_documentation_does_not_promise_what_the_kernel_cannot_do() -> None:
    for document in (README, USER_GUIDE, ISO_DOC):
        text = document.read_text()
        assert not re.search(r"safe[- ]graphics (entry|mode) (works|gives)", text, re.I)
    # H1: the live kernel is built with a generic framebuffer DRM driver, so Safe graphics is a real desktop entry
    assert "no generic framebuffer driver" not in README.read_text() and "Safe graphics" in README.read_text()
    assert "simpledrm" in USER_GUIDE.read_text() and "Safe graphics" in USER_GUIDE.read_text()


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


def test_the_documents_name_every_main_boot_entry() -> None:
    """A renamed menu entry must not leave the README, the guide or the disc's README.txt pointing at a title that is gone."""
    parser = load_script("scripts/verify-iso.py", "verify_iso")
    template = (ROOT / "iso/cd_overlay/boot/grub/grub.cfg.in").read_text()
    titles = [entry.title for entry in parser.parse_grub_cfg(template)[0]]
    ai_rescue = "AI rescue (text console)"  # wave 6: the disc's README must name it; docs/RESCUE-AI.md is the docs owner's page
    assert ai_rescue in titles
    main = [t for t in titles[:6] if t != ai_rescue]
    assert main[0] == "Gentoo installer and rescue desktop" and len(main) == 5
    assert ai_rescue in re.sub(r"\s+", " ", (ROOT / "iso/README-live.txt").read_text()), "iso/README-live.txt does not mention the boot entry 'AI rescue (text console)'"
    documents = {
        "README.md": README.read_text(),
        "docs/USER-GUIDE.md": USER_GUIDE.read_text(),
        "iso/README-live.txt": (ROOT / "iso/README-live.txt").read_text(),
    }
    for title in main:
        short = title if "text console)" in title else title.split(" (")[0]
        for name, text in documents.items():
            assert short in re.sub(r"\s+", " ", text), f"{name} does not mention the boot entry '{short}'"
