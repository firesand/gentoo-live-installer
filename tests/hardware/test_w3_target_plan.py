"""Wave 3 target findings that live in the planner and the recommendations (TGT-5, TGT-6, TGT-7)."""

from __future__ import annotations

import pytest

from gentoo_installerd import hwplan

from tests.hardware.builders import manifest, profile


def uefi_32():
    machine = profile()
    machine["firmware"]["efi_bits"] = 32
    return machine


# -- TGT-7: 32-bit UEFI firmware ---------------------------------------------------------------------------------

@pytest.mark.parametrize(("bootloader", "uki", "multilib", "severity"), [
    ("grub", False, True, "warning"),            # i386-efi GRUB is installed
    ("grub", False, False, "warning"),
    ("systemd-boot", False, True, "warning"),    # a multilib stage3 builds systemd-bootia32.efi
    ("systemd-boot", False, False, "error"),     # no -m32 toolchain, no ia32 image for bootctl
    ("efi-stub", False, True, "error"),          # efibootmgr would register a 64-bit kernel the firmware cannot load
    ("efi-stub", True, True, "error"),
    ("grub", True, True, "error"),               # ukify builds the x64 stub
    ("systemd-boot", True, True, "error"),
])
def test_32_bit_uefi_blocks_the_boot_setups_that_cannot_start(bootloader, uki, multilib, severity):
    m = manifest(system={"bootloader": bootloader, "uki": uki, "multilib": multilib})
    found = {issue.code: issue for issue in hwplan.validate_plan(m, uefi_32())}
    assert found["hw.fw_uefi32"].severity == severity
    assert ("hw.fw_uefi32" in {issue.code for issue in hwplan.blocking_issues(m, uefi_32())}) is (severity == "error")


def test_the_message_names_what_is_wrong_and_what_works():
    efi_stub = hwplan.validate_plan(manifest(system={"bootloader": "efi-stub"}), uefi_32())[0].message
    assert "EFI stub" in efi_stub and "GRUB" in efi_stub and "systemd-boot" in efi_stub and "legacy BIOS" in efi_stub
    no_multilib = next(i for i in hwplan.validate_plan(manifest(system={"bootloader": "systemd-boot", "multilib": False}), uefi_32()) if i.code == "hw.fw_uefi32").message
    assert "GRUB" in no_multilib and "legacy BIOS" in no_multilib
    assert "choose systemd-boot" not in no_multilib.lower() and "or systemd-boot" not in no_multilib   # systemd-boot is the problem here
    uki = next(i for i in hwplan.validate_plan(manifest(system={"bootloader": "grub", "uki": True}), uefi_32()) if i.code == "hw.fw_uefi32").message
    assert "Unified Kernel Image" in uki


def test_legacy_bios_and_64_bit_firmware_have_no_such_finding():
    for m, machine in ((manifest(system={"bootloader": "efi-stub"}), profile()),
                       (manifest(storage={"firmware": "bios"}, system={"bootloader": "grub"}), profile(firmware="bios"))):
        assert "hw.fw_uefi32" not in {issue.code for issue in hwplan.validate_plan(m, machine)}


# -- TGT-6: the console font hint ----------------------------------------------------------------------------------

def panel(width, height, dpi):
    from gentoo_installer import hardware as hw

    return hw.DisplayInfo(connector="eDP-1", internal=True, width=width, height=height, dpi=dpi)


def test_a_dense_panel_seeds_the_console_font():
    from gentoo_installer import recommend

    machine = profile(form="laptop", displays=(panel(3840, 2160, 290),))
    m = manifest()
    recommend.apply_defaults(m, machine)
    assert m.system.console_font == "ter-132b"
    assert not [error for error in m.validate() if "font" in error.lower()]


def test_a_normal_panel_keeps_the_kernels_font():
    from gentoo_installer import recommend

    for machine in (profile(displays=(panel(1920, 1080, 110),)), profile(), profile(displays=(panel(2560, 1440, 109),))):
        m = manifest()
        recommend.apply_defaults(m, machine)
        assert m.system.console_font == ""


def test_the_users_font_and_a_portable_install_are_left_alone():
    from gentoo_installer import recommend

    machine = profile(displays=(panel(3840, 2160, 290),))
    chosen = manifest(system={"console_font": "lat9w-16"})
    recommend.apply_defaults(chosen, machine)
    assert chosen.system.console_font == "lat9w-16"
    portable = manifest(hardware={"target": "portable"})   # the disk will meet other screens
    recommend.apply_defaults(portable, machine)
    assert portable.system.console_font == ""


def test_defaults_for_names_the_font_as_a_change_without_touching_the_manifest():
    from gentoo_installer import recommend

    m = manifest()
    changes = recommend.defaults_for(profile(displays=(panel(3840, 2160, 290),)), m)
    assert changes["system.console_font"] == "ter-132b" and m.system.console_font == ""


# -- TGT-5: fonts for the script of the chosen language --------------------------------------------------------------

CJK = ["media-fonts/noto-cjk"]
NOTO = ["media-fonts/noto"]


def font_atoms(locale, desktop):
    m = manifest(system={"locale": locale, "desktop": desktop, "profile": "default/linux/amd64/23.0" if desktop == "minimal" else ""})
    return [atom for atom in hwplan.plan(m, profile()).optional_atoms if atom.startswith("media-fonts/")]


@pytest.mark.parametrize(("locale", "desktop", "expected"), [
    ("ja_JP.UTF-8", "kde", CJK), ("ja_JP.UTF-8", "gnome", CJK), ("ja_JP.UTF-8", "xfce", CJK),
    ("zh_CN.UTF-8", "kde", CJK), ("zh_TW.UTF-8", "gnome", CJK), ("zh_HK.UTF-8", "xfce", CJK), ("ko_KR.UTF-8", "kde", CJK), ("yue_HK.UTF-8", "gnome", CJK),
    ("ar_EG.UTF-8", "gnome", NOTO), ("he_IL.UTF-8", "xfce", NOTO), ("fa_IR.UTF-8", "gnome", NOTO), ("th_TH.UTF-8", "xfce", NOTO),
    ("hi_IN.UTF-8", "gnome", NOTO), ("bn_BD.UTF-8", "xfce", NOTO), ("ta_IN.UTF-8", "gnome", NOTO), ("km_KH.UTF-8", "xfce", NOTO), ("ka_GE.UTF-8", "gnome", NOTO),
    ("ar_EG.UTF-8", "kde", []),          # the KDE package set brings media-fonts/noto along
    ("en_US.UTF-8", "kde", []), ("en_GB.UTF-8", "gnome", []), ("de_DE.UTF-8", "xfce", []), ("pt_BR.UTF-8", "kde", []), ("tr_TR.UTF-8", "gnome", []),
    ("ru_RU.UTF-8", "gnome", []), ("uk_UA.UTF-8", "xfce", []), ("el_GR.UTF-8", "gnome", []), ("vi_VN.UTF-8", "xfce", []),   # Latin, Cyrillic, Greek: the default fonts cover them
    ("ja_JP.UTF-8", "minimal", []), ("ar_EG.UTF-8", "minimal", []),      # a console install has no use for them
    ("C", "kde", []), ("C.UTF-8", "gnome", []), ("POSIX", "xfce", []),
    ("de_DE@euro", "gnome", []), ("ja_JP.eucJP", "xfce", CJK),
])
def test_the_script_of_the_locale_gets_a_font(locale, desktop, expected):
    assert font_atoms(locale, desktop) == expected


def test_the_summary_says_which_fonts_were_added():
    m = manifest(system={"locale": "ja_JP.UTF-8"})
    result = hwplan.plan(m, profile())
    assert any(line.startswith("Fonts:") and "Chinese, Japanese" in line for line in result.summary)
    assert any("media-fonts/noto-cjk" in note for note in result.notes)
    assert not any(line.startswith("Fonts:") for line in hwplan.plan(manifest(), profile()).summary)


def test_fonts_are_optional_so_a_failed_download_only_degrades():
    result = hwplan.plan(manifest(system={"locale": "ja_JP.UTF-8"}), profile())
    assert "media-fonts/noto-cjk" in result.optional_atoms and "media-fonts/noto-cjk" not in result.required_atoms
    assert "media-fonts/noto-cjk" in result.all_atoms()


def test_the_default_locale_changes_no_plan():
    assert hwplan.plan(manifest(), profile()).optional_atoms == hwplan.plan(manifest(system={"locale": "de_DE.UTF-8"}), profile()).optional_atoms


def test_the_script_of_a_locale_is_decided_on_the_language_alone():
    from gentoo_installer import keyboard_data

    expected = {"ja_JP.UTF-8": "cjk", "zh_CN": "cjk", "ko_KR.EUC-KR": "cjk", "yue_HK.UTF-8": "cjk", "ar_SA.UTF-8": "complex", "sat_IN": "complex",
                "ckb_IQ.UTF-8": "complex", "bo_CN.UTF-8": "complex", "dz_BT": "complex", "de_DE@euro": "", "ku_TR.UTF-8": "", "C": "", "a": "", "": ""}
    assert {locale: keyboard_data.locale_script(locale) for locale in expected} == expected
    assert keyboard_data.locale_language("pt_BR.UTF-8") == "pt" and keyboard_data.locale_language("sat_IN") == "sat"
