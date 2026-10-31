"""System tuning of the hardware plan (hwplan_tuning.py): CachyOS's settings, adapted to the init, kernel, power manager, form factor and desktop."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from gentoo_installerd import hwplan, hwplan_tuning as tuning

from tests.backend.target_helpers import configurer
from tests.hardware.builders import manifest, profile, systemd

ALL_FILES = {tuning.SYSCTL_FILE, tuning.IOSCHED_FILE, tuning.THP_FILE, tuning.WIFI_FILE, tuning.AUDIO_TLP_FILE, tuning.AUDIO_RULES_FILE,
             tuning.AUDIO_SCRIPT, tuning.TOUCHPAD_X11_FILE, tuning.TOUCHPAD_SWAY_FILE, tuning.WATCHDOG_FILE}
UDEV_PAIR = re.compile(r'[A-Z_]+(\{[^}]+\})?(==|!=|=|\+=)"[^"]*"')


def files_of(result) -> dict[str, tuple[str, int]]:
    return {path: (content, mode) for path, content, mode in result.files}


def tuning_files(result) -> set[str]:
    return set(files_of(result)) & ALL_FILES


@pytest.fixture
def berlin(monkeypatch):
    monkeypatch.setattr(tuning, "zone_countries", lambda path=tuning.ZONE_TAB: {"Europe/Berlin": "DE", "Asia/Jakarta": "ID"})


def test_it_is_on_by_default_and_both_inits_get_the_same_files_applied_by_their_own_services():
    machine = profile(form="laptop")
    openrc, sysd = hwplan.plan(manifest(), machine), hwplan.plan(systemd(), machine)
    assert manifest().hardware.tuning is True
    assert openrc.scalars["power"] == sysd.scalars["power"] == "ppd"  # KDE's profile switcher: so audio goes through udev, not TLP
    assert tuning_files(openrc) == tuning_files(sysd) == {tuning.SYSCTL_FILE, tuning.IOSCHED_FILE, tuning.THP_FILE, tuning.AUDIO_RULES_FILE,
                                                           tuning.AUDIO_SCRIPT, tuning.WATCHDOG_FILE}
    assert {path: files_of(openrc)[path] for path in tuning_files(openrc)} == {path: files_of(sysd)[path] for path in tuning_files(sysd)}
    assert any("applied by OpenRC's sysctl service" in line for line in openrc.summary)
    assert any("applied by OpenRC's systemd-tmpfiles-setup service" in line for line in openrc.summary)
    assert any("applied by systemd-sysctl" in line for line in sysd.summary)
    assert any(line.endswith("(applied by systemd-tmpfiles-setup)") for line in sysd.summary)
    assert openrc.scalars["tuning"] == sysd.scalars["tuning"] == "on"


def test_one_switch_turns_all_of_it_off():
    m = manifest(hardware={"tuning": False, "zram": "yes"})
    result = hwplan.plan(m, profile(form="laptop", wifi=True))
    assert tuning_files(result) == set() and result.scalars["tuning"] == "off"
    assert tuning.A_REGDB not in result.optional_atoms and "zswap.enabled=0" not in result.kernel_cmdline
    assert not any(line.startswith("Tuning:") for line in result.summary)


@pytest.mark.parametrize("zram", ["yes", "no"])
def test_the_swap_settings_follow_zram(zram):
    result = hwplan.plan(manifest(hardware={"zram": zram}), profile())
    text = files_of(result)[tuning.SYSCTL_FILE][0]
    assert ("vm.swappiness = 150" in text) == ("vm.page-cluster = 0" in text) == (zram == "yes")
    assert ("zswap.enabled=0" in result.kernel_cmdline) == (zram == "yes")


@pytest.mark.parametrize("kernel", ["gentoo-kernel-bin", "gentoo-kernel", "manual", "custom"])
@pytest.mark.parametrize("on", [True, False])
def test_the_scheduler_rules_never_compete_with_gentoos_package(kernel, on):
    """The package's rule runs after ours at every boot: on an NVMe disk it wrote none over Kyber (the first real NVMe VM of RC4)."""
    m = manifest(system={"kernel": kernel}, hardware={"tuning": on})
    ours = tuning.IOSCHED_FILE in files_of(hwplan.plan(m, profile()))
    package = "sys-block/io-scheduler-udev-rules" in configurer(Path("/nonexistent")).kernel_extra_atoms(m)
    assert not (ours and package)
    assert ours == (on and kernel in tuning.DIST_KERNELS)
    assert package == (not on and kernel in tuning.DIST_KERNELS)


@pytest.mark.parametrize("kernel", ["manual", "custom"])
def test_a_kernel_of_your_own_keeps_its_schedulers_and_huge_pages(kernel):
    result = hwplan.plan(manifest(system={"kernel": kernel}), profile())
    assert tuning.SYSCTL_FILE in files_of(result) and not {tuning.IOSCHED_FILE, tuning.THP_FILE} & set(files_of(result))
    assert any("disk schedulers and huge pages" in note for note in result.notes)


def test_audio_power_saving_goes_through_tlp_when_tlp_manages_the_laptop():
    result = hwplan.plan(manifest(hardware={"power": "tlp"}), profile(form="laptop"))
    assert files_of(result)[tuning.AUDIO_TLP_FILE][0].splitlines()[-1] == "SOUND_POWER_SAVE_ON_AC=0"
    assert tuning.AUDIO_RULES_FILE not in files_of(result) and tuning.AUDIO_SCRIPT not in files_of(result)


@pytest.mark.parametrize("form, power", [("laptop", "ppd"), ("laptop", "none"), ("desktop", "auto")])
def test_otherwise_a_udev_rule_switches_it_by_the_charger(form, power):
    result = hwplan.plan(manifest(hardware={"power": power}), profile(form=form))
    files = files_of(result)
    assert result.scalars["power"] != "tlp" and tuning.AUDIO_TLP_FILE not in files
    assert files[tuning.AUDIO_SCRIPT][1] == 0o755 and tuning.AUDIO_SCRIPT in files[tuning.AUDIO_RULES_FILE][0]


def _run_audio_script(tmp_path: Path, supplies: dict[str, tuple[str, str]]) -> str:
    root = tmp_path / "root"
    param = root / "sys/module/snd_hda_intel/parameters/power_save"
    param.parent.mkdir(parents=True)
    param.write_text("1\n")
    for name, (kind, online) in supplies.items():
        supply = root / "sys/class/power_supply" / name
        supply.mkdir(parents=True)
        (supply / "type").write_text(kind + "\n")
        (supply / "online").write_text(online + "\n")
    script = tmp_path / "installer-audio-power"
    script.write_text(tuning.AUDIO_SCRIPT_TEXT.replace("/sys/", f"{root}/sys/"))
    subprocess.run(["sh", str(script)], check=True)
    return param.read_text().strip()


def test_the_audio_script_saves_power_only_on_battery(tmp_path):
    assert _run_audio_script(tmp_path / "battery", {"AC": ("Mains", "0"), "BAT0": ("Battery", "1")}) == "1"
    assert _run_audio_script(tmp_path / "charger", {"AC": ("Mains", "1"), "BAT0": ("Battery", "1")}) == "0"
    assert _run_audio_script(tmp_path / "desktop", {}) == "0"
    assert _run_audio_script(tmp_path / "two-chargers", {"AC0": ("Mains", "0"), "USB": ("Mains", "1")}) == "0"


@pytest.mark.parametrize("desktop, form, expected", [
    ("xfce", "laptop", tuning.TOUCHPAD_X11_FILE), ("mate", "laptop", tuning.TOUCHPAD_X11_FILE), ("i3", "laptop", tuning.TOUCHPAD_X11_FILE),
    ("sway", "laptop", tuning.TOUCHPAD_SWAY_FILE), ("kde", "laptop", None), ("gnome", "laptop", None), ("minimal", "laptop", None),
    ("xfce", "desktop", None),
])
def test_tap_to_click_is_set_where_the_desktop_does_not_set_it_itself(desktop, form, expected):
    result = hwplan.plan(manifest(system={"desktop": desktop}), profile(form=form))
    touchpad = set(files_of(result)) & {tuning.TOUCHPAD_X11_FILE, tuning.TOUCHPAD_SWAY_FILE}
    assert touchpad == ({expected} if expected else set())


def test_the_x11_touchpad_file_names_its_driver_so_the_order_of_the_config_folders_does_not_matter():
    # /etc/X11/xorg.conf.d and /usr/share/X11/xorg.conf.d (40-libinput.conf) are read as two folders: a MatchDriver could run first
    assert 'Driver "libinput"' in tuning.TOUCHPAD_X11 and "MatchDriver" not in tuning.TOUCHPAD_X11
    live = (Path(__file__).resolve().parents[2] / "iso/root_overlay/etc/X11/xorg.conf.d/50-gentoo-live-touchpad.conf").read_text()
    assert live == tuning.TOUCHPAD_X11  # the live disc taps the same way as the installed system


def test_the_wifi_country_follows_the_time_zone(berlin):
    result = hwplan.plan(manifest(system={"timezone": "Europe/Berlin"}), profile(wifi=True))
    assert files_of(result)[tuning.WIFI_FILE][0].splitlines()[-1] == "options cfg80211 ieee80211_regdom=DE"
    assert tuning.A_REGDB in result.optional_atoms and result.scalars["wifi_country"] == "DE"


def test_a_zone_without_one_country_still_gets_the_regulatory_database(berlin):
    result = hwplan.plan(manifest(system={"timezone": "UTC"}), profile(wifi=True))
    assert tuning.WIFI_FILE not in files_of(result) and tuning.A_REGDB in result.optional_atoms
    assert any("names no single country" in line for line in result.summary)


def test_no_wifi_card_no_wifi_settings(berlin):
    result = hwplan.plan(manifest(system={"timezone": "Europe/Berlin"}), profile())
    assert tuning.WIFI_FILE not in files_of(result) and tuning.A_REGDB not in result.optional_atoms


def test_zone_tab_is_read_like_tzdata_writes_it(tmp_path):
    table = tmp_path / "zone.tab"
    table.write_text("# comment\tline\tEurope/Nowhere\nDE\t+5230+01322\tEurope/Berlin\nid\t-0610+10648\tAsia/Jakarta\tJava, Sumatra\nbad line\n")
    tuning.zone_countries.cache_clear()
    try:
        assert tuning.zone_countries(str(table)) == {"Europe/Berlin": "DE", "Asia/Jakarta": "ID"}
        assert tuning.zone_countries(str(tmp_path / "missing")) == {}
        assert tuning.wifi_country("Etc/UTC", {"Europe/Berlin": "DE"}) == ""
    finally:
        tuning.zone_countries.cache_clear()


def test_a_virtual_machine_keeps_its_watchdog():
    result = hwplan.plan(manifest(), profile(hypervisor="kvm"))
    assert tuning.WATCHDOG_FILE not in files_of(result) and tuning.SYSCTL_FILE in files_of(result)


def test_the_files_are_well_formed_and_credit_cachyos(berlin):
    result = hwplan.plan(manifest(system={"desktop": "xfce", "timezone": "Europe/Berlin"}, hardware={"zram": "yes", "power": "ppd"}),
                         profile(form="laptop", wifi=True))
    files = {path: content for path, (content, _mode) in files_of(result).items() if path in ALL_FILES}
    assert len(files) == 8
    for path, content in files.items():
        assert "values from CachyOS-Settings" in content, path
    for line in files[tuning.SYSCTL_FILE].splitlines():
        assert line.startswith("#") or re.fullmatch(r"-?[a-z0-9_.-]+ = \S+", line), line
        assert "unprivileged_userns_clone" not in line and "sysrq" not in line  # not on Gentoo kernels; a security default
    for path in (tuning.IOSCHED_FILE, tuning.AUDIO_RULES_FILE):
        for line in files[path].splitlines():
            if line and not line.startswith("#"):
                assert all(UDEV_PAIR.fullmatch(pair.strip()) for pair in line.split(", ")), line
    subprocess.run(["sh", "-n", "-c", files[tuning.AUDIO_SCRIPT]], check=True)
