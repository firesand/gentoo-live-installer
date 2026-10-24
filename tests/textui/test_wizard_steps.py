"""One wizard step at a time, against a fake installer service and a fake live system."""

from __future__ import annotations

import copy

import pytest

from gentoo_installer.manifest import InstallManifest
from gentoo_installer.textui.console import Console, GoBack
from gentoo_installer.textui.daemon import Daemon
from gentoo_installer.textui.inventory import MachineView
from gentoo_installer.textui.wizard import Wizard

from tests.textui.support import FakeHost, FakeInstaller, Script, device_number, inspect_reply

GIB = 1024**3
PASSWORD = "correct-horse-1"


@pytest.fixture
def start(installer, host: FakeHost):
    """``start(steps, reply=None)`` -> (wizard, script, fake service); the wizard is built the way the command line builds it."""

    def make(steps, reply=None, **kwargs):
        fake: FakeInstaller = installer(reply if reply is not None else inspect_reply("qemu-q35-virtio", serials={"/dev/vda": "VDA-SERIAL-0001"}))
        script = Script(steps)
        console = Console(script.stdin, script.out, environ={"TERM": "dumb"})
        daemon = Daemon(fake.path)
        wizard = Wizard(console, host, daemon, view=MachineView(fake.reply, local_by_path=host.by_path_names()), state=daemon.probe(), **kwargs)
        return wizard, script, fake

    return make


def with_disks(reply: dict, *disks: dict) -> dict:
    """The reply with its usable disk replaced by ``disks`` (lsblk dictionaries)."""
    changed = copy.deepcopy(reply)
    changed["disks"] = [disk for disk in changed["disks"] if disk["path"] != "/dev/vda"] + list(disks)
    return changed


def disk_dict(name: str, size: int = 100 * GIB, **kwargs) -> dict:
    from gentoo_installer import blockdev

    return blockdev.BlockDevice(name=name, path="/dev/" + name, type="disk", size=size, maj_min=kwargs.pop("maj_min", device_number(name)), **kwargs).to_dict()


# --- keyboard --------------------------------------------------------------------------------------------------------------


def test_the_default_keyboard_needs_no_test_and_is_applied_to_the_console(start, host: FakeHost) -> None:
    wizard, script, _ = start([("English (US)", "")])
    wizard.step_keyboard()
    system = wizard.manifest.system
    assert (system.keymap, system.keyboard_layout, system.keyboard_variant) == ("us", "us", "")
    assert host.called("loadkeys") == [("loadkeys", "us")] and "Test the layout" not in script.flat


def test_keymap_layout_and_variant_are_set_together(start, host: FakeHost) -> None:
    wizard, script, _ = start([("", "5"), ("Test the layout", "yz"), ("match the keys", "")])
    wizard.step_keyboard()
    system = wizard.manifest.system
    assert (system.keymap, system.keyboard_layout, system.keyboard_variant) == ("de-latin1-nodeadkeys", "de", "nodeadkeys")
    assert host.called("loadkeys") == [("loadkeys", "de-latin1-nodeadkeys")]
    assert "This console now uses the keymap de-latin1-nodeadkeys (German (no dead keys))." in script.flat


def test_a_layout_with_another_alphabet_keeps_the_us_console_map(start, host: FakeHost) -> None:
    wizard, _, _ = start([("", "Russian")])
    wizard.step_keyboard()
    system = wizard.manifest.system
    assert (system.keymap, system.keyboard_layout) == ("us", "ru")


def test_a_wrong_keyboard_test_lets_the_user_choose_again(start, host: FakeHost) -> None:
    wizard, _, _ = start([("", "4"), ("Test the layout", "xx"), ("match the keys", "n"), ("", "8"), ("Test the layout", "xx"), ("match the keys", "")])
    wizard.step_keyboard()
    assert wizard.manifest.system.keymap == "fr"
    assert [call[1] for call in host.called("loadkeys")] == ["de-latin1", "fr"]


def test_a_console_that_cannot_switch_keymaps_is_not_an_error(start, host: FakeHost) -> None:
    host.keymap_result = (False, "Couldn't get a file descriptor referring to the console")
    wizard, script, _ = start([("", "4"), ("Test the layout", ""), ("match the keys", "")])
    wizard.step_keyboard()
    assert wizard.manifest.system.keymap == "de-latin1"
    assert "was not switched" in script.flat and "serial line or SSH" in script.flat


def test_another_keymap_can_be_typed_and_is_checked_against_the_installed_ones(start, host: FakeHost) -> None:
    wizard, script, _ = start([
        ("", "another"), ("Console keymap name", "de-latin"), ("Did you mean", "bad name!"), ("letters, digits", "ru"),
        ("Desktop keyboard layout", "Q"), ("Invalid keyboard layout", "ru"), ("Layout variant", ""), ("Test the layout", ""), ("match the keys", ""),
    ])
    wizard.step_keyboard()
    system = wizard.manifest.system
    assert (system.keymap, system.keyboard_layout, system.keyboard_variant) == ("ru", "ru", "")
    assert "This system has no keymap called de-latin" in script.flat and "Did you mean: de-latin1" in script.flat


# --- network ------------------------------------------------------------------------------------------------------------------


def test_with_a_connection_the_step_just_continues(start) -> None:
    wizard, script, _ = start([("Internet: connected", "")])
    wizard.step_network()
    assert "ens3: network cable (cable plugged in)" in script.flat


def test_without_internet_the_hint_names_nmtui_and_the_default_is_to_check_again(start, host: FakeHost) -> None:
    reply = inspect_reply("qemu-q35-virtio", network=False)
    wizard, script, fake = start([("Internet: NOT connected", ""), ("", "2")], reply)
    wizard.step_network()
    assert "nmtui" in script.flat and "needs the internet" in script.flat
    assert [request["action"] for request in fake.requests].count("inspect") == 1  # Enter checked again; 2 continued anyway


def test_nmtui_runs_on_the_users_terminal_and_the_network_is_checked_again(start, host: FakeHost) -> None:
    reply = inspect_reply("qemu-q35-virtio", network=False)
    wizard, script, fake = start([("NOT connected", "n"), ("", "")], reply)
    fake.reply = inspect_reply("qemu-q35-virtio", network=True)  # the Wi-Fi came up
    wizard.step_network()
    assert host.called("run") == [("run", ("nmtui",))]
    assert "Internet: connected" in script.flat


def test_a_missing_nmtui_points_to_nmcli(start, host: FakeHost) -> None:
    host.interactive_status = 127
    wizard, script, _ = start([("NOT connected", "n"), ("", "2")], inspect_reply("qemu-q35-virtio", network=False))
    wizard.step_network()
    assert "nmcli device wifi connect" in script.flat


def test_a_download_proxy_is_checked_stored_and_used_for_the_next_check(start) -> None:
    wizard, script, fake = start([("", "p"), ("Download proxy", "ftp://nope"), ("must look like http", "http://proxy.example:3128"), ("", "")])
    wizard.step_network()
    assert wizard.manifest.network.proxy == "http://proxy.example:3128"
    inspects = [request for request in fake.requests if request["action"] == "inspect"]
    assert inspects[-1]["manifest"]["network"]["proxy"] == "http://proxy.example:3128"
    assert "http://proxy.example:3128" in script.flat


def test_the_proxy_can_be_removed(start) -> None:
    wizard, _, _ = start([("", "p"), ("Download proxy", "none"), ("", "")])
    wizard.manifest.network.proxy = "http://old.example:3128"
    wizard.step_network()
    assert wizard.manifest.network.proxy == ""


def test_the_adapters_are_listed_and_saved_connections_can_be_copied(start) -> None:
    reply = inspect_reply("qemu-q35-virtio")
    reply["hardware"]["nics"].append({"iface": "wlan0", "kind": "wifi", "carrier": True})
    reply["hardware"]["network_profiles"] = ["Home Wi-Fi", "Office"]
    wizard, script, _ = start([("wlan0: Wi-Fi", ""), ("saved network connections", "y")], reply)
    wizard.step_network()
    assert wizard.manifest.network.import_live_profiles is True and "Home Wi-Fi, Office" in script.flat


def test_without_a_network_adapter_the_step_explains(start) -> None:
    reply = inspect_reply("qemu-q35-virtio", network=False)
    reply["hardware"]["nics"] = []
    wizard, script, _ = start([("none were found", "2")], reply)
    wizard.step_network()
    assert "Plug in a network cable or a USB network adapter" in script.flat


# --- the hardware report ------------------------------------------------------------------------------------------------------------


def test_the_hardware_report_shows_the_machine_and_what_was_noticed(start) -> None:
    wizard, script, _ = start([("Boot mode", "")], inspect_reply("thinkpad-intel-nvidia-hybrid"))
    wizard.step_hardware()
    assert "Boot mode" in script.flat and "What the installer noticed:" in script.flat
    assert "UEFI" in script.flat


def test_errors_of_the_hardware_probe_are_shown_in_full(start) -> None:
    wizard, script, _ = start([("Error:", "")], inspect_reply("intel-rst-hidden-nvme"))
    wizard.step_hardware()
    assert "Error: No disk that can receive an installation was found." in script.flat
    assert "change SATA Operation to AHCI" in script.flat
    assert script.text.index("Error:") < script.text.index("Note:")  # errors come first


def test_a_wrong_clock_can_be_set_from_the_network(start) -> None:
    clock = {"code": "hw.clock_skew", "severity": "error", "message": "The clock is 3 days behind.", "action": "sync_time"}
    wizard, script, fake = start([("The clock is 3 days behind", "t"), ("", "")], inspect_reply("qemu-q35-virtio", extra_findings=[clock]))
    wizard.step_hardware()
    assert [request["action"] for request in fake.requests].count("sync_time") == 1
    assert "The clock was set from the network." in script.flat


def test_when_the_network_clock_fails_the_time_can_be_typed(start) -> None:
    clock = {"code": "hw.clock_skew", "severity": "error", "message": "The clock is wrong.", "action": "set_time"}
    wizard, script, fake = start([("", "t"), ("UTC date and time", "tomorrow"), ("Use the form", "2026-10-24 14:30:00"), ("", "")], inspect_reply("qemu-q35-virtio", extra_findings=[clock]))
    fake.action_replies["sync_time"] = {"ok": False, "message": "chronyd could not reach a time server."}
    wizard.step_hardware()
    asked = [request for request in fake.requests if request["action"] == "set_time"]
    assert asked and asked[0]["utc"] == "2026-10-24 14:30:00"
    assert "chronyd could not reach a time server." in script.flat


def test_the_clock_command_is_only_offered_when_the_service_can_do_it(start) -> None:
    clock = {"code": "hw.clock_skew", "severity": "error", "message": "The clock is wrong.", "action": "sync_time"}
    reply = inspect_reply("qemu-q35-virtio", extra_findings=[clock])
    wizard, script, fake = start([("", "")], reply)
    fake.actions = [name for name in fake.actions if name not in {"sync_time", "set_time"}]
    fake.features = ["install.v1"]
    wizard.state = Daemon(fake.path).probe()
    wizard.step_hardware()
    assert "t = fix the clock" not in script.flat


def test_check_again_asks_the_service_again(start) -> None:
    wizard, _, fake = start([("", "r"), ("", "")])
    wizard.step_hardware()
    assert [request["action"] for request in fake.requests].count("inspect") == 1


def test_without_hardware_details_the_step_says_so(start) -> None:
    wizard, script, _ = start([("No hardware details", "")], {"architecture": "x86_64", "uefi": True, "disks": [], "network": True})
    wizard.step_hardware()
    assert "No hardware details are available" in script.flat


# --- the disk ---------------------------------------------------------------------------------------------------------------------------


def test_the_disk_menu_shows_what_each_disk_is_and_what_is_on_it(start) -> None:
    reply = inspect_reply("old-bios-sata-radeon")
    wizard, script, _ = start([("Which disk", "1")], reply)
    wizard.step_disk()
    assert "/dev/sda - WDC WD5000AAKX-0" in script.flat and "serial ...XXXXXX" in script.flat
    assert "SATA" in script.flat and "ntfs" in script.flat and "Looks like Windows is installed here." in script.flat
    assert "/dev/sdb (USB DISK 3.0) is not offered: it holds the live installer medium" in script.flat
    storage = wizard.manifest.storage
    assert (storage.device, storage.device_id, storage.device_id_kind) == ("/dev/sda", "0x50014ee000000000", "")


def test_nothing_is_offered_when_no_disk_can_be_used_and_a_rescan_finds_new_ones(start) -> None:
    reply = inspect_reply("intel-rst-hidden-nvme")
    wizard, script, fake = start([("No disk can be installed to", ""), ("Which disk", "1")], reply)
    fake.reply = with_disks(inspect_reply("intel-rst-hidden-nvme"), disk_dict("nvme0n1", serial="NVME-1", model="Fast SSD"))
    wizard.step_disk()
    assert "RAID/Intel RST/VMD" in script.flat and wizard.manifest.storage.device == "/dev/nvme0n1"


def test_a_mounted_disk_is_refused_with_advice(start) -> None:
    reply = inspect_reply("qemu-q35-virtio", serials={"/dev/vda": "VDA-1"}, mounted=["/dev/vda"])
    reply["disks"].append(disk_dict("vdb", serial="VDB-1"))
    wizard, script, _ = start([("Which disk", "1"), ("is in use", "2")], reply)
    wizard.step_disk()
    assert "/dev/vda is in use (/mnt/data)" in script.flat and "umount" in script.flat
    assert wizard.manifest.storage.device == "/dev/vdb"


def test_a_scan_picks_up_a_disk_that_was_plugged_in(start) -> None:
    wizard, script, fake = start([("Which disk", "r"), ("Which disk", "1")])
    fake.reply = with_disks(fake.reply, disk_dict("sdc", serial="NEW-1"))  # the disk appears after the first scan
    wizard.step_disk()
    assert wizard.manifest.storage.device == "/dev/sdc" and "/dev/sdc - " in script.text


def test_a_disk_without_a_serial_number_needs_explicit_approval_and_gets_a_path_identity(start) -> None:
    wizard, script, _ = start([("Which disk", "1"), ("neither a serial number nor a WWN", "y")], inspect_reply("qemu-q35-virtio"))
    wizard.step_disk()
    storage = wizard.manifest.storage
    assert "pci-0000:00:03.0" in script.flat and "recognised by its hardware path" in script.flat
    assert (storage.device, storage.device_id_kind) == ("/dev/vda", "path")
    assert storage.device_id == f"pci-0000:00:03.0|{40 * GIB}|"


def test_refusing_the_approval_returns_to_the_disk_menu(start) -> None:
    reply = inspect_reply("qemu-q35-virtio")
    reply["disks"].append(disk_dict("vdb", serial="VDB-1"))
    wizard, script, _ = start([("Which disk", "1"), ("neither a serial number", "n"), ("Which disk", "2")], reply)
    wizard.step_disk()
    assert wizard.manifest.storage.device == "/dev/vdb" and wizard.manifest.storage.device_id_kind == ""


def test_a_disk_without_any_stable_identity_cannot_be_used(start) -> None:
    reply = inspect_reply("qemu-q35-virtio")
    for item in reply["hardware"]["disks"]:
        item["by_path"] = ""
    reply["disks"].append(disk_dict("vdb", serial="VDB-1"))
    wizard, script, _ = start([("Which disk", "1"), ("no stable hardware path", "2")], reply)
    wizard.step_disk()
    assert "QEMU: -device virtio-blk-pci,drive=d0,serial=MYDISK" in script.flat and wizard.manifest.storage.device == "/dev/vdb"


def test_a_removable_disk_can_be_made_portable(start) -> None:
    reply = inspect_reply("qemu-q35-virtio")
    reply["disks"].append(disk_dict("sdc", serial="USB-1", removable=True, hotplug=True, transport="usb"))
    wizard, script, _ = start([("Which disk", "2"), ("start this system on other computers", "y")], reply)
    wizard.step_disk()
    assert wizard.manifest.hardware.target == "portable" and "removable" in script.flat


def test_a_fixed_disk_does_not_ask_about_other_computers(start) -> None:
    wizard, script, _ = start([("Which disk", "1")])
    wizard.step_disk()
    assert "other computers" not in script.flat and wizard.manifest.hardware.target == "this"


def test_going_back_from_the_disk_menu_leaves_the_step(start) -> None:
    wizard, _, _ = start([("Which disk", "b")])
    with pytest.raises(GoBack):
        wizard.step_disk()


# --- filesystem and encryption --------------------------------------------------------------------------------------------------------------


def test_only_guided_erase_is_offered_and_the_text_says_where_manual_mode_is(start) -> None:
    wizard, script, _ = start([("Which filesystem", "3"), ("Encrypt the system disk", "n")])
    wizard.step_storage()
    storage = wizard.manifest.storage
    assert (storage.mode, storage.filesystem, storage.luks, storage.luks_passphrase) == ("erase", "btrfs", False, "")
    assert "graphical installer" in script.flat and "--manifest FILE" in script.flat and "XFS" in script.flat


def test_the_passphrase_is_asked_twice_and_a_mismatch_asks_again(start) -> None:
    wizard, script, _ = start([
        ("Which filesystem", ""), ("Encrypt the system disk", "y"), ("Passphrase", "first-passphrase-1"), ("Type it again", "other-passphrase-2"),
        ("not the same", "right-passphrase-3"), ("Type it again", "right-passphrase-3"),
    ])
    wizard.step_storage()
    storage = wizard.manifest.storage
    assert storage.luks and storage.luks_passphrase == "right-passphrase-3"
    assert "The two passphrases are not the same." in script.flat
    assert "first-passphrase-1" not in script.flat and "right-passphrase-3" not in script.flat


def test_a_short_passphrase_is_refused_with_the_manifests_own_message(start) -> None:
    wizard, script, _ = start([("", ""), ("Encrypt", "y"), ("Passphrase", "short"), ("at least 8 characters", "long-enough-1"), ("again", "long-enough-1")])
    wizard.step_storage()
    assert "The LUKS passphrase must contain at least 8 characters" in script.flat


def test_the_passphrase_warning_names_the_keyboard_used_at_boot(start) -> None:
    wizard, script, _ = start([("", ""), ("Encrypt", "y"), ("Passphrase", "long-enough-1"), ("again", "long-enough-1")])
    wizard.manifest.system.keymap, wizard.manifest.system.keyboard_layout = "de-latin1", "de"
    wizard.step_storage()
    assert "keyboard layout is the one you chose in step 1: German" in script.flat and "console keymap de-latin1" in script.flat


def test_a_layout_with_another_alphabet_is_warned_about_for_the_passphrase(start) -> None:
    wizard, script, _ = start([("", ""), ("Encrypt", "y"), ("Passphrase", "long-enough-1"), ("again", "long-enough-1")])
    wizard.manifest.system.keymap, wizard.manifest.system.keyboard_layout = "us", "ru"
    wizard.step_storage()
    assert "works like a US keyboard" in script.flat


def test_going_back_skips_the_parts_of_a_step_that_asked_nothing(start) -> None:
    wizard, _, _ = start([])
    log: list[str] = []
    attempts = {"c": 0}

    def a() -> bool:
        log.append("a")
        return True

    def b() -> bool:
        log.append("b")
        return False  # nothing was asked here, for example the passphrase of an unencrypted install

    def c() -> bool:
        log.append("c")
        attempts["c"] += 1
        if attempts["c"] == 1:
            raise GoBack
        return True

    wizard.substeps(a, b, c)
    assert log == ["a", "b", "c", "a", "b", "c"]
    with pytest.raises(GoBack):  # nothing asked before it: the step itself is left
        wizard.substeps(lambda: (_ for _ in ()).throw(GoBack()))


def test_turning_encryption_off_forgets_the_passphrase(start) -> None:
    wizard, _, _ = start([("", ""), ("Encrypt", "n")])
    wizard.manifest.storage.luks, wizard.manifest.storage.luks_passphrase = True, "old-passphrase-1"
    wizard.step_storage()
    assert not wizard.manifest.storage.luks and wizard.manifest.storage.luks_passphrase == ""


def test_a_passphrase_from_before_can_be_kept(start) -> None:
    wizard, _, _ = start([("", ""), ("Encrypt", "y"), ("Keep the passphrase", "")])
    wizard.manifest.storage.luks, wizard.manifest.storage.luks_passphrase = True, "old-passphrase-1"
    wizard.step_storage()
    assert wizard.manifest.storage.luks_passphrase == "old-passphrase-1"


# --- desktop and init --------------------------------------------------------------------------------------------------------------------------


def test_a_computer_with_little_memory_is_offered_xfce(start) -> None:
    wizard, script, _ = start([("Which desktop", ""), ("Which init system", "")])  # the 4 GiB virtual machine
    wizard.step_system()
    assert wizard.manifest.system.desktop == "xfce" and "little memory, so Xfce is suggested" in script.flat


def test_a_desktop_that_does_not_fit_the_disk_is_refused_with_the_reason(start) -> None:
    reply = inspect_reply("qemu-q35-virtio")
    reply["disks"].append(disk_dict("sdc", size=20 * GIB, serial="SMALL-1"))
    wizard, script, _ = start([("Which disk", "2"), ("Which desktop", "1"), ("too small", "2"), ("too small", "3"), ("Which init system", "")], reply)
    wizard.step_disk()
    wizard.step_system()
    # 20 GiB minus the boot partition and swap leaves 17 GiB: KDE needs 24 and GNOME 18, Xfce needs 14.
    assert wizard.manifest.system.desktop == "xfce" and "Not possible on the disk you chose" in script.flat


def test_the_init_system_decides_the_stage3_variant(start) -> None:
    wizard, _, _ = start([("Which desktop", "4"), ("Which init system", "2")])
    wizard.step_system()
    system = wizard.manifest.system
    assert (system.init, system.stage_variant, system.profile, system.multilib) == ("systemd", "amd64-systemd", "", True)
    assert wizard.manifest.validate_sections()["system"] == []
    wizard2, _, _ = start([("Which desktop", "1"), ("Which init system", "1")])
    wizard2.step_system()
    assert wizard2.manifest.system.stage_variant == "amd64-openrc"


def test_choosing_minimal_drops_the_graphical_companions_and_a_desktop_brings_them_back(start) -> None:
    wizard, _, _ = start([("Which desktop", "4"), ("init", ""), ("Which desktop", "3"), ("init", "")])
    wizard.step_system()
    options = wizard.manifest.options
    assert not options.install_hwmonux and not options.install_portagehub
    wizard.step_system()
    assert options.install_hwmonux and options.install_portagehub


# --- accounts ---------------------------------------------------------------------------------------------------------------------------------------


def test_the_hostname_and_user_name_follow_the_manifests_rules(start) -> None:
    wizard, script, _ = start([
        ("Computer name", "my_box"), ("Hostname must contain only", "mybox"), ("Your user name", "Alice"), ("lowercase Linux username", "root"),
        ("name the system uses itself", "alice"), ("Password for alice", PASSWORD), ("Type it again", PASSWORD), ("How should", ""),
    ])
    wizard.step_accounts()
    accounts = wizard.manifest.accounts
    assert (accounts.hostname, accounts.username) == ("mybox", "alice")


def test_passwords_are_checked_and_never_shown(start) -> None:
    wizard, script, _ = start([
        ("Computer name", ""), ("Your user name", "alice"), ("Password for alice", "short"), ("at least 8 characters", "first-password-1"),
        ("Type it again", "second-password-2"), ("not the same", "right-password-3"), ("Type it again", "right-password-3"), ("How should", ""),
    ])
    wizard.step_accounts()
    assert wizard.manifest.accounts.password == "right-password-3" and wizard.manifest.accounts.root_password == "right-password-3"
    assert "The user password must contain at least 8 characters" in script.flat
    assert not any(secret in script.flat for secret in ("short\n", "first-password-1", "second-password-2", "right-password-3"))


def test_root_can_have_a_separate_password(start) -> None:
    wizard, _, _ = start([
        ("Computer name", ""), ("Your user name", "alice"), ("Password for alice", PASSWORD), ("Type it again", PASSWORD),
        ("How should", "2"), ("Password for root", "root-password-9"), ("Type it again", "root-password-9"),
    ])
    wizard.step_accounts()
    accounts = wizard.manifest.accounts
    assert accounts.root_password == "root-password-9" and accounts.password == PASSWORD and not accounts.disable_root_login
    assert wizard.manifest.validate_sections()["accounts"] == []


def test_root_login_can_be_turned_off(start) -> None:
    wizard, _, _ = start([
        ("Computer name", ""), ("Your user name", "alice"), ("Password for alice", PASSWORD), ("Type it again", PASSWORD), ("How should", "3"),
    ])
    wizard.step_accounts()
    accounts = wizard.manifest.accounts
    assert accounts.disable_root_login and accounts.root_password == ""
    assert wizard.manifest.validate_sections()["accounts"] == []


# --- time zone and language ---------------------------------------------------------------------------------------------------------------------


def test_the_time_zone_is_chosen_by_region_then_by_city(start) -> None:
    wizard, _, _ = start([("Where are you", "4"), ("Choose your time zone", "jakarta"), ("Which language", "")])
    wizard.step_region()
    assert wizard.manifest.system.timezone == "Asia/Jakarta"


def test_utc_needs_no_city_and_all_zones_can_be_searched(start) -> None:
    wizard, _, _ = start([("Where are you", "1"), ("Which language", "")])
    wizard.step_region()
    assert wizard.manifest.system.timezone == "UTC"
    wizard2, _, _ = start([("Where are you", "6"), ("Choose your time zone", "new_york"), ("Which language", "")])
    wizard2.step_region()
    assert wizard2.manifest.system.timezone == "America/New_York"


def test_a_missing_zone_database_falls_back_to_typing_the_name(start, host: FakeHost) -> None:
    host.zones = []
    wizard, script, _ = start([("Time zone", "Mars/Olympus"), ("Unknown timezone", "Europe/Paris"), ("Which language", "")])
    wizard.step_region()
    assert wizard.manifest.system.timezone == "Europe/Paris"


def test_the_language_follows_the_keyboard_unless_the_user_chose_otherwise(start) -> None:
    wizard, script, _ = start([("Where are you", ""), ("Which language", "")])
    wizard.manifest.system.keyboard_layout = "de"
    wizard.step_region()
    assert wizard.manifest.system.locale == "de_DE.UTF-8" and "Enter = de_DE.UTF-8" in script.flat
    wizard2, _, _ = start([("Where are you", ""), ("Which language", "")])
    wizard2.manifest.system.keyboard_layout, wizard2.manifest.system.locale = "de", "en_GB.UTF-8"
    wizard2.step_region()
    assert wizard2.manifest.system.locale == "en_GB.UTF-8"


def test_the_defaults_come_from_the_hardware(start) -> None:
    wizard, _, _ = start([], inspect_reply("old-bios-sata-radeon"))
    assert wizard.manifest.storage.firmware == "bios"
    assert wizard.manifest.storage.swap_mib == 4096  # 3.8 GiB of memory: the Handbook table (up to 4 GiB of RAM: 4 GiB)
    machine, _, _ = start([])
    assert machine.manifest.storage.firmware == "uefi" and machine.manifest.options.microcode is False  # in a virtual machine


def test_the_boot_mode_follows_the_machine_even_without_a_hardware_report(start) -> None:
    wizard, _, _ = start([], {"architecture": "x86_64", "uefi": False, "disks": []})
    assert wizard.manifest.storage.firmware == "bios"
    from_file, _, _ = start([], inspect_reply("old-bios-sata-radeon"), from_file=True)
    assert from_file.manifest.storage.firmware == "uefi"  # a settings file is never changed behind the user's back


def test_a_proxy_with_a_password_is_shown_without_it_and_kept_on_enter(start) -> None:
    wizard, script, fake = start([("", "p"), ("Download proxy", "http://me:topsecret-1@proxy.example:3128"), ("Download proxy: http://me:<redacted>@proxy.example:3128", "p"), ("keep", ""), ("", "")])
    wizard.step_network()
    assert wizard.manifest.network.proxy == "http://me:topsecret-1@proxy.example:3128"  # Enter kept the real value
    assert script.text.count("topsecret-1") == 1  # only the echo of what the user typed; the program itself never prints it
    sent = [request for request in fake.requests if request["action"] == "inspect"]
    assert sent[-1]["manifest"]["network"]["proxy"] == "http://me:topsecret-1@proxy.example:3128"


# --- settings files with their own partitions or another stage3 family ------------------------------------------------------------------------------------


def test_the_stage3_family_of_a_settings_file_is_kept_when_the_init_system_changes(start) -> None:
    wizard, _, _ = start([("Which init system", "2")], from_file=True)
    wizard.manifest.system.stage_variant = "amd64-hardened-openrc"
    wizard._ask_init()
    assert (wizard.manifest.system.stage_variant, wizard.manifest.system.multilib) == ("amd64-hardened-systemd", True)
    wizard2, _, _ = start([("Which init system", "2")], from_file=True)
    # KDE needs a Standard or Desktop stage3 (manifest rule), so this settings file uses Xfce.
    wizard2.manifest.system.stage_variant, wizard2.manifest.system.multilib = "amd64-nomultilib-openrc", False
    wizard2.manifest.system.desktop = "xfce"
    wizard2._ask_init()
    assert (wizard2.manifest.system.stage_variant, wizard2.manifest.system.multilib) == ("amd64-nomultilib-systemd", False)
    assert wizard2.manifest.validate_sections()["system"] == []


def test_kde_is_refused_on_a_hardened_or_no_multilib_stage3(start) -> None:
    wizard, script, _ = start([("Which desktop", "1"), ("Hardened and No-multilib", "2"), ("Which init system", "")], from_file=True)
    wizard.manifest.system.stage_variant = "amd64-hardened-openrc"
    wizard.step_system()
    assert wizard.manifest.system.desktop == "gnome" and "KDE Plasma needs a Standard or Desktop stage3" in script.flat


def manual_manifest(**extra) -> InstallManifest:
    from gentoo_installer.manifest import Partition

    manifest = InstallManifest()
    manifest.storage.mode, manifest.storage.device, manifest.storage.device_id = "manual", "/dev/vda", "VDA-SERIAL-0001"
    manifest.storage.additional_device_ids = ["VDB-SERIAL-0002"]
    manifest.storage.partitions = [
        Partition("/dev/vda1", "/efi", "vfat", format=False), Partition("/dev/vda2", "/", "ext4", format=True), Partition("/dev/vdb1", "/home", "ext4", format=False),
    ]
    manifest.accounts.password = manifest.accounts.root_password = "correct-horse-1"
    return manifest


def two_disk_reply() -> dict:
    from gentoo_installer import blockdev

    def disk(name: str, serial: str, parts: list[tuple[str, str, int]]) -> dict:
        children = tuple(blockdev.BlockDevice(name=n, path="/dev/" + n, type="part", size=size, fstype=fs, maj_min=device_number(n)) for n, fs, size in parts)
        return blockdev.BlockDevice(name=name, path="/dev/" + name, type="disk", size=100 * GIB, serial=serial, maj_min=device_number(name), children=children).to_dict()

    reply = inspect_reply("qemu-q35-virtio", serials={"/dev/vda": "VDA-SERIAL-0001"})
    reply["disks"] = [disk("vda", "VDA-SERIAL-0001", [("vda1", "vfat", GIB), ("vda2", "ext4", 60 * GIB)]), disk("vdb", "VDB-SERIAL-0002", [("vdb1", "ext4", 90 * GIB)]),
                      *[d for d in reply["disks"] if d["path"] == "/dev/sdb"]]
    return reply


def test_the_disk_and_filesystem_steps_leave_your_own_partitions_alone(start) -> None:
    wizard, script, _ = start([], two_disk_reply(), manifest=manual_manifest(), from_file=True)
    before = wizard.manifest.to_dict()
    wizard.step_disk()
    wizard.step_storage()
    assert wizard.manifest.to_dict() == before and wizard.manifest.storage.mode == "manual"
    assert script.flat.count("use your own partitions (manual mode)") == 2


def test_every_disk_that_a_manual_layout_changes_must_be_typed(start) -> None:
    wizard, script, _ = start([], two_disk_reply(), manifest=manual_manifest(), from_file=True)
    assert wizard.required_disks() == ["/dev/vda", "/dev/vdb"]
    s = Script([("type the name of the main disk", "/dev/vda"), ("also changes /dev/vdb", "/dev/vdc"), ("That is not /dev/vdb", "/dev/vdb")])
    wizard.console = Console(s.stdin, s.out, environ={"TERM": "dumb"})
    plan = wizard.review()
    assert plan.resume is False and "ERASE" not in s.text and "USE    /dev/vda" in s.text and "USE    /dev/vdb" in s.text
    assert "FORMAT /dev/vda2 -> / (ext4" in s.flat and "KEEP /dev/vdb1 -> /home" in s.flat
    assert "Partitions 3 chosen by you, listed below" in s.flat and "install Gentoo on the partitions listed above" in s.flat and "erase /dev/vda" not in s.flat


def test_a_layout_on_several_disks_is_refused_when_the_second_name_is_wrong_three_times(start) -> None:
    wizard, _, _ = start([], two_disk_reply(), manifest=manual_manifest(), from_file=True)
    script = Script([("type the name of the main disk", "/dev/vda"), ("also changes /dev/vdb", "x"), ("That is not", "y"), ("That is not", "z")])
    wizard.console = Console(script.stdin, script.out, environ={"TERM": "dumb"})
    result = wizard.review()
    assert type(result).__name__ == "Refused" and "another disk" in result.reason


def test_a_disk_whose_serial_number_is_shared_needs_the_hardware_path(start) -> None:
    reply = inspect_reply("qemu-q35-virtio")
    reply["disks"] = [d for d in reply["disks"] if d["path"] != "/dev/vda"] + [disk_dict("sdc", serial="0000000000"), disk_dict("sdd", serial="0000000000")]
    for item in ("sdc", "sdd"):
        reply["hardware"]["disks"].append({"path": "/dev/" + item, "by_path": f"pci-0000:00:14.0-usb-0:{item}", "serial": "0000000000"})
    wizard, script, _ = start([("Which disk", "1"), ("another disk reports too", "y")], reply)
    wizard.step_disk()
    storage = wizard.manifest.storage
    assert storage.device_id_kind == "path" and storage.device_id.startswith("pci-0000:00:14.0-usb-0:sdc|")
    assert "cannot tell them apart" in script.flat


def windows_next_to_the_target() -> dict:
    """A virtual machine whose second disk holds a Windows partition and whose firmware lists Windows Boot Manager."""
    from gentoo_installer import blockdev

    reply = inspect_reply("qemu-q35-virtio", serials={"/dev/vda": "VDA-SERIAL-0001"})
    reply["hardware"]["firmware"]["windows_boot_entry"] = True
    windows = blockdev.BlockDevice(name="sdc", path="/dev/sdc", type="disk", size=100 * GIB, serial="WIN-1", maj_min=device_number("sdc"),
                                   children=(blockdev.BlockDevice(name="sdc1", path="/dev/sdc1", type="part", size=90 * GIB, fstype="ntfs", maj_min=device_number("sdc1")),))
    reply["disks"].append(windows.to_dict())
    return reply


def test_a_computer_that_keeps_windows_is_asked_about_the_hardware_clock(start) -> None:
    wizard, script, _ = start([("Where are you", ""), ("Which language", ""), ("Keep the hardware clock in local time?", "y")], windows_next_to_the_target())
    wizard.manifest.storage.device = "/dev/vda"
    wizard.step_region()
    assert wizard.manifest.system.rtc_local is True and "Windows seems to be installed" in script.flat
    plain, script2, _ = start([("Where are you", ""), ("Which language", "")])
    plain.step_region()
    assert plain.manifest.system.rtc_local is False and "hardware clock" not in script2.flat


def test_erasing_the_windows_disk_is_not_a_dual_boot(start) -> None:
    wizard, script, _ = start([("Where are you", ""), ("Which language", "")], windows_next_to_the_target())
    wizard.manifest.storage.device = "/dev/sdc"  # the disk with Windows is the one being erased
    wizard.step_region()
    assert wizard.manifest.system.rtc_local is False and "hardware clock" not in script.flat


def test_a_clock_that_is_already_in_local_time_is_a_reason_to_ask(start) -> None:
    reply = inspect_reply("qemu-q35-virtio", serials={"/dev/vda": "VDA-SERIAL-0001"})
    reply["hardware"]["clock"]["rtc_mode"] = "local"
    wizard, script, _ = start([("Where are you", ""), ("Which language", ""), ("hardware clock in local time", "n")], reply)
    wizard.manifest.storage.device = "/dev/vda"
    wizard.step_region()
    assert wizard.manifest.system.rtc_local is False


def test_advice_about_choices_the_text_installer_does_not_offer_says_so(installer, host: FakeHost) -> None:
    from gentoo_installer.textui.summary import ONLY_ELSEWHERE, build_summary
    from tests.textui.support import valid_manifest

    reply = inspect_reply("asus-desktop-rtx4090")
    view = MachineView(reply)
    manifest = valid_manifest("/dev/nvme0n1")
    advice = build_summary(manifest, view).advice
    assert any("NVIDIA proprietary driver" in item and item.endswith(ONLY_ELSEWHERE) for item in advice)
