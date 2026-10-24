"""The text-mode boot entries "Repair a system" and "Install in text mode" (interface I7).

Three parts have to agree: the boot menu adds `gentoo.autostart=<tool>`, the profile.d hook
starts that tool on the first console login, and the image contract demands both. This file
tests the menu and its verifier rules; the hook has its own tests further down.
"""
from __future__ import annotations

import json

import pytest

from .conftest import ROOT, load_script

render = load_script("iso/render-config.py", "render_config")
verify = load_script("scripts/verify-iso.py", "verify_iso")

CONTRACT = json.loads((ROOT / "iso/image-contract.json").read_text())
ISO_FILES = {"boot/gentoo", "boot/gentoo.igz", "memtest64.bios", "memtest.efi64"}
REPAIR = "Repair a system (text console)"
TEXT_INSTALL = "Install in text mode"


@pytest.fixture()
def grub_text(pins: dict[str, str]) -> str:
    values = render.token_values(pins, "/build", "2", "4")
    template = (ROOT / "iso/cd_overlay/boot/grub/grub.cfg.in").read_text()
    return render.render_text(template, values, "grub.cfg.in")


@pytest.fixture()
def entries(grub_text: str):
    return {entry.title: entry for entry in verify.parse_grub_cfg(grub_text)[0]}


def check(grub_text: str, pins: dict[str, str]):
    report = verify.Report()
    verify.check_grub(report, grub_text, pins["ISO_VOLID"], pins["ISO_BOOTARGS"].split(), set(ISO_FILES), CONTRACT)
    return report


def failed(report) -> set[str]:
    return {c.id for c in report.errors}


# --- the menu --------------------------------------------------------------------------------


def test_the_menu_offers_a_repair_entry_and_a_text_install_entry(entries) -> None:
    assert REPAIR in entries and TEXT_INSTALL in entries
    repair, install = entries[REPAIR].linux, entries[TEXT_INSTALL].linux
    assert len(repair) == 1 and len(install) == 1
    assert {"nox", "dokeymap", "gentoo.autostart=rescue"} <= set(repair[0].split())
    assert {"nox", "dokeymap", "gentoo.autostart=installer-text"} <= set(install[0].split())


def test_both_entries_ask_for_a_keyboard_layout_because_passwords_are_typed(entries) -> None:
    """A LUKS passphrase or an account password is typed in both tools: the layout is chosen first."""
    for title in (REPAIR, TEXT_INSTALL):
        words = entries[title].linux[0].split()
        assert "dokeymap" in words and "nox" in words, title


def test_only_the_two_text_entries_carry_an_autostart_token(entries) -> None:
    carrying = {title for title, entry in entries.items() if any("gentoo.autostart=" in line for line in entry.linux)}
    assert carrying == {REPAIR, TEXT_INSTALL}


def test_the_default_entry_is_still_the_desktop(entries, grub_text: str) -> None:
    first = verify.parse_grub_cfg(grub_text)[0][0]
    assert first.title == "Gentoo installer and rescue desktop"
    assert not {"nox", "dokeymap"} & set(first.linux[0].split())
    assert "gentoo.autostart" not in first.linux[0]


def test_repair_comes_before_the_plain_shell_and_the_troubleshooting_submenu(grub_text: str) -> None:
    titles = [e.title for e in verify.parse_grub_cfg(grub_text)[0]]
    assert titles.index(REPAIR) < titles.index(TEXT_INSTALL) < titles.index("Text console only (a shell, asks for a keyboard layout)")
    assert titles.index(TEXT_INSTALL) < titles.index("Verbose boot (kernel and initramfs messages)")


def test_the_entries_keep_the_dracut_arguments_in_catalysts_order(entries, pins: dict[str, str]) -> None:
    tail = f"root=live:CDLABEL={pins['ISO_VOLID']} rd.live.dir=/ rd.live.squashimg=image.squashfs cdroot"
    for title in (REPAIR, TEXT_INSTALL):
        line = entries[title].linux[0]
        assert tail in line and line.index(tail) < line.index("nox"), title
        assert line.split()[2:2 + len(pins["ISO_BOOTARGS"].split())] == pins["ISO_BOOTARGS"].split(), "the shared boot arguments come first"
    assert any(line.strip() == "initrd /boot/gentoo.igz" for line in entries[REPAIR].lines)


def test_the_contract_names_exactly_the_values_the_menu_uses(entries) -> None:
    used = {word.split("=", 1)[1] for e in entries.values() for line in e.linux for word in line.split() if word.startswith("gentoo.autostart=")}
    assert used == set(CONTRACT["grub"]["autostart_values"]) == {"rescue", "installer-text"}
    assert {REPAIR, TEXT_INSTALL} == set(CONTRACT["grub"]["required_titles"])


# --- the verifier refuses what would break the console hook ------------------------------------


def test_the_rendered_menu_passes_the_verifier(grub_text: str, pins: dict[str, str]) -> None:
    report = check(grub_text, pins)
    assert report.ok, [c.id + ": " + c.detail for c in report.errors]
    assert "grub.autostart" in {c.id for c in report.checks}


def test_a_menu_without_the_repair_entry_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    start = grub_text.index(f"menuentry '{REPAIR}'")
    end = grub_text.index("menuentry 'Install in text mode'")
    report = check(grub_text[:start] + grub_text[end:], pins)
    assert {"grub.required_titles", "grub.has_gentoo.autostart=rescue", "grub.argument_group"} <= failed(report)


def test_a_menu_without_the_text_install_entry_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    start = grub_text.index("menuentry 'Install in text mode'")
    end = grub_text.index("menuentry 'Text console only")
    report = check(grub_text[:start] + grub_text[end:], pins)
    assert {"grub.required_titles", "grub.has_gentoo.autostart=installer-text"} <= failed(report)


def test_an_autostart_token_on_a_desktop_entry_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    broken = grub_text.replace("cdroot rd.live.ram=1\n", "cdroot rd.live.ram=1 gentoo.autostart=rescue\n", 1)
    assert broken != grub_text
    report = check(broken, pins)
    assert "grub.autostart" in failed(report)
    assert "also the desktop" in next(c.detail for c in report.errors if c.id == "grub.autostart")


def test_an_autostart_value_the_hook_does_not_know_is_refused(grub_text: str, pins: dict[str, str]) -> None:
    broken = grub_text.replace("nox dokeymap gentoo.autostart=rescue", "nox dokeymap gentoo.autostart=repair", 1)
    assert broken != grub_text
    report = check(broken, pins)
    assert "grub.autostart" in failed(report)
    assert "gentoo.autostart=repair" in next(c.detail for c in report.errors if c.id == "grub.autostart")
