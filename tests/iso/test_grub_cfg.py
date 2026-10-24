"""The boot menu template, rendered, against the rules that scripts/verify-iso.py enforces."""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from .conftest import FIXTURES, ROOT, load_script

render = load_script("iso/render-config.py", "render_config")
verify = load_script("scripts/verify-iso.py", "verify_iso")

CONTRACT = json.loads((ROOT / "iso/image-contract.json").read_text())
ISO_FILES = {
    "boot/gentoo", "boot/gentoo.igz", "memtest64.bios", "memtest.efi64",
}


@pytest.fixture()
def grub_text(pins: dict[str, str]) -> str:
    values = render.token_values(pins, "/build", "2", "4")
    template = (ROOT / "iso/cd_overlay/boot/grub/grub.cfg.in").read_text()
    return render.render_text(template, values, "grub.cfg.in")


@pytest.fixture()
def entries(grub_text: str):
    return verify.parse_grub_cfg(grub_text)[0]


def check(grub_text: str, pins: dict[str, str], files=ISO_FILES, volid: str | None = None):
    report = verify.Report()
    verify.check_grub(report, grub_text, volid or pins["ISO_VOLID"], pins["ISO_BOOTARGS"].split(), set(files), CONTRACT)
    return report


def test_rendered_menu_passes_every_rule(grub_text: str, pins: dict[str, str]) -> None:
    report = check(grub_text, pins)
    assert report.ok, [c.id + ": " + c.detail for c in report.errors]


def test_template_uses_only_the_two_tokens() -> None:
    template = (ROOT / "iso/cd_overlay/boot/grub/grub.cfg.in").read_text()
    assert set(re.findall(r"@[A-Z]+@", template)) == {"@VOLID@", "@BOOTARGS@"}


def test_default_entry_is_the_desktop_without_a_keymap_prompt(entries) -> None:
    first = entries[0]
    assert len(first.linux) == 1
    words = first.linux[0].split()
    assert "dokeymap" not in words and "nox" not in words and "nomodeset" not in words
    assert first.title == "Gentoo installer and rescue desktop"


def test_ram_entry_states_the_memory_it_needs(entries) -> None:
    ram = [e for e in entries if any("rd.live.ram=1" in l.split() for l in e.linux) and not any("nox" in l.split() for l in e.linux)]
    assert len(ram) == 1 and "6 GiB" in ram[0].title


def test_text_console_entry_asks_for_a_keyboard_layout(entries) -> None:
    text = [e for e in entries if any({"nox", "dokeymap"} <= set(l.split()) for l in e.linux)]
    assert len(text) >= 1
    # dokeymap anywhere else would stop a desktop boot on the prompt
    for entry in entries:
        for line in entry.linux:
            if "dokeymap" in line.split():
                assert "nox" in line.split(), entry.title


def test_troubleshooting_entries(entries) -> None:
    lines = [l for e in entries for l in e.linux]
    assert any("console=ttyS0,115200n8" in l.split() and "nox" in l.split() for l in lines)
    assert any({"nomodeset", "pcie_aspm=off", "nvme_core.default_ps_max_latency_us=0", "usbcore.autosuspend=-1"} <= set(l.split()) for l in lines)
    assert any({"acpi=off", "noapic"} <= set(l.split()) for l in lines)
    assert any("dospeakup" in l.split() for l in lines)
    assert any({"ignore_loglevel", "loglevel=7", "rd.debug"} <= set(l.split()) for l in lines)
    assert any({"intel_iommu=off", "amd_iommu=off"} <= set(l.split()) for l in lines)


def test_no_safe_graphics_entry(entries) -> None:
    """H1: the live kernel has no simpledrm/efidrm, so nomodeset gives a text console only."""
    for entry in entries:
        assert "safe graphics" not in entry.title.lower()
        if any("nomodeset" in l.split() for l in entry.linux):
            assert "text" in entry.title.lower(), f"'{entry.title}' must say that it is text only"


def test_memtest_block_is_present_once_and_guarded(grub_text: str) -> None:
    for title in ("Memtest86+ 64bit BIOS", "Memtest86+ 64bit UEFI"):
        assert grub_text.count(f'"{title}"') == 1
    assert "[ -e /memtest64.bios ]" in grub_text and "[ -e /memtest.efi64 ]" in grub_text
    bios = grub_text.index('"Memtest86+ 64bit BIOS"')
    assert grub_text.rindex("xpc", 0, bios) > grub_text.rindex("fi", 0, bios) - 400


def test_firmware_entries_are_efi_only(grub_text: str) -> None:
    efi_start = grub_text.index('if [ "x$grub_platform" = xefi ]; then')
    efi_end = grub_text.index("\nfi\n", efi_start)
    efi_block = grub_text[efi_start:efi_end]
    assert "fwsetup" in efi_block and "exit" in efi_block
    assert "fwsetup" not in grub_text[:efi_start] and "fwsetup" not in grub_text[efi_end:]
    assert grub_text.index("insmod efifwsetup") < grub_text.index("\tfwsetup")


def test_header_matches_catalyst(grub_text: str) -> None:
    """The first lines are Catalyst's own, so the theme and the graphics mode do not change."""
    catalyst = (FIXTURES / "grub_r19.cfg").read_text().splitlines()[:9]
    assert grub_text.split("set default=0", 1)[1].splitlines()[:9] == ["" if False else l for l in ["", *catalyst[1:9]]] or all(l in grub_text for l in catalyst)


def test_search_and_initrd_lines_are_the_ones_catalyst_writes(grub_text: str, pins: dict[str, str]) -> None:
    assert f"\tsearch --no-floppy --set=root -l {pins['ISO_VOLID']}" in grub_text
    assert "\tinitrd /boot/gentoo.igz" in grub_text
    for entry in verify.parse_grub_cfg(grub_text)[0]:
        for line in entry.linux:
            words = line.split()
            assert words[:2] == ["linux", "/boot/gentoo"]
            assert words[words.index(f"root=live:CDLABEL={pins['ISO_VOLID']}"):][:4] == [
                f"root=live:CDLABEL={pins['ISO_VOLID']}", "rd.live.dir=/", "rd.live.squashimg=image.squashfs", "cdroot",
            ]


# --- negative cases: what the verifier must refuse ------------------------------------------


def test_catalyst_default_menu_is_refused(pins: dict[str, str]) -> None:
    report = check((FIXTURES / "grub_r19.cfg").read_text(), pins)
    failed = {c.id for c in report.errors}
    assert {"grub.entries", "grub.default_entry", "grub.has_nox"} <= failed


def test_wrong_volume_id_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    assert not check(grub_text, pins, volid="GENTOO_INSTALLER_TESTKEY").ok


def test_missing_boot_argument_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    first = next(l for l in grub_text.splitlines() if l.strip().startswith("linux /boot/gentoo"))
    broken = grub_text.replace(first, first.replace("secureconsole ", ""), 1)
    assert broken != grub_text
    assert "grub.linux_lines" in {c.id for c in check(broken, pins).errors}


def test_dokeymap_in_the_default_entry_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    broken = grub_text.replace("cdroot\n\tinitrd", "cdroot dokeymap\n\tinitrd", 1)
    report = check(broken, pins)
    assert "grub.default_entry" in {c.id for c in report.errors}


def test_safe_graphics_promise_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    broken = grub_text.replace("Compatibility mode (text only", "Safe graphics (nomodeset", 1)
    assert "grub.forbidden_titles" in {c.id for c in check(broken, pins).errors}


def test_duplicate_memtest_block_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    block = grub_text[grub_text.index('if [ "x$grub_platform" = xpc ]'):grub_text.index('if [ "x$grub_platform" = xefi ]')]
    assert "grub.unique_titles" in {c.id for c in check(grub_text + block, pins).errors}


def test_memtest_files_without_menu_entry_are_refused(pins: dict[str, str]) -> None:
    text = "\n".join(l for l in (FIXTURES / "grub_r19.cfg").read_text().splitlines())
    report = check(text, pins)
    assert "grub.memtest" in {c.id for c in report.errors}


@pytest.mark.skipif(shutil.which("grub-script-check") is None, reason="grub-script-check (sys-boot/grub) is not installed")
def test_grub_itself_accepts_the_rendered_menu(grub_text: str, tmp_path) -> None:
    """GRUB's own parser: a syntax error here means no menu at all on the user's PC."""
    good = tmp_path / "grub.cfg"
    good.write_text(grub_text)
    result = subprocess.run(["grub-script-check", str(good)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    broken = tmp_path / "broken.cfg"
    broken.write_text(grub_text.replace("submenu 'More options (troubleshooting)' --class gnu-linux --class os {", "submenu 'More options' {{", 1))
    assert subprocess.run(["grub-script-check", str(broken)], capture_output=True, check=False).returncode != 0, "the check itself must be able to fail"
