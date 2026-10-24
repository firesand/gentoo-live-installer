"""The summary, the problems that block the start, and a settings file reviewed interactively."""

from __future__ import annotations

import json
from pathlib import Path

from gentoo_installer.manifest import InstallManifest
from gentoo_installer.textui.inventory import MachineView
from gentoo_installer.textui.summary import build_summary, keyboard_label, settings_rows

from tests.textui.support import (
    END, PASSWORD, FakeHost, FakeInstaller, Script, inspect_reply, run_main, to_disk_step, valid_manifest, walk_to_review,
)

VDA = {"/dev/vda": "VDA-SERIAL-0001"}
GIB = 1024**3


def serial_installer(installer, **kwargs) -> FakeInstaller:
    return installer(inspect_reply("qemu-q35-virtio", serials=VDA), **kwargs)


# --- what the summary says ---------------------------------------------------------------------------------------------------------------------


def test_the_summary_lists_the_settings_the_hardware_plan_and_what_happens_to_the_disk(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    script = Script([*walk_to_review(), ("Step 9 of 9", END)])
    run_main(["--socket", fake.path], script, host)
    text = script.flat
    for expected in (
        "Disk /dev/vda", "ALL DATA ON IT WILL BE ERASED", "Boot mode UEFI, GPT partition table", "Filesystem ext4, 2048 MiB swap", "Encryption Not enabled",
        "System OpenRC with Xfce; binary packages", "Account alice on a computer named mybox", "Region Europe/Berlin, de_DE.UTF-8",
        "Chosen for this computer:", "Virtual machine: qemu", "What will happen to the disks:", "ERASE /dev/vda", "CREATE EFI system partition", "Other disks will not be modified.",
    ):
        assert expected in text, expected
    assert "ALL DATA ON IT WILL" in script.text and PASSWORD not in script.text


def test_encryption_and_a_path_identity_show_up_in_the_summary(installer, host: FakeHost) -> None:
    fake = installer(inspect_reply("qemu-q35-virtio"))
    script = Script([*walk_to_review(approve="y", luks="disk-passphrase-1"), ("Step 9 of 9", END)])
    run_main(["--socket", fake.path], script, host)
    assert "recognised by its hardware path, size and model" in script.flat
    assert "Encryption LUKS2 on the root volume; swap is never stored unencrypted (/boot stays readable for the bootloader)" in script.flat
    assert "/boot (ext4, 1.0 GiB)" in script.flat and "not encrypted: the bootloader must read it" in script.flat
    assert "disk-passphrase-1" not in script.text


# --- problems stop the start --------------------------------------------------------------------------------------------------------------------------


def test_a_missing_program_blocks_the_start_until_the_choice_is_changed(installer, host: FakeHost) -> None:
    reply = inspect_reply("qemu-q35-virtio", serials=VDA, tools={"cryptsetup": False, "mkfs.ext4": True, "mkfs.vfat": True, "swapon": True, "swapoff": True})
    fake = installer(reply)
    steps = [
        *walk_to_review(luks="disk-passphrase-1"),
        ("Step 9 of 9", "/dev/vda"),  # typing the disk name does not help while there are problems
        ("The problems above must be fixed first", "c"), ("Which setting do you want to change", "5"),
        ("Step 5 of 9", ""), ("Encrypt", "n"), ("Step 6 of 9", ""), ("init system", ""),
        ("Step 7 of 9", ""), ("Your user name", ""), ("Keep the password", ""), ("How should", ""),
        ("Step 8 of 9", "5"), ("Choose your time zone", ""), ("Which language", ""),
        ("type the disk name exactly", "/dev/vda"), ("The installation finished", "4"),
    ]
    script = Script(steps)
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 0
    assert "These problems stop the installation until they are fixed:" in script.text
    assert "lacks programs that these settings need: cryptsetup" in script.flat
    assert fake.manifests[0]["storage"]["luks"] is False


def test_an_error_of_the_hardware_probe_blocks_until_it_is_gone_after_a_new_check(installer, host: FakeHost) -> None:
    clock = {"code": "hw.clock_skew", "severity": "error", "message": "The clock is three days behind.", "action": "sync_time"}
    fake = installer(inspect_reply("qemu-q35-virtio", serials=VDA, extra_findings=[clock]))
    script = Script([*walk_to_review(), ("Step 9 of 9", "r"), ("type the disk name exactly", "/dev/vda"), ("The installation finished", "4")])
    original = fake.respond

    def respond(request):
        if request.get("action") == "inspect" and any(r.get("action") == "inspect" for r in fake.requests[:-0 or None]):
            fake.reply = inspect_reply("qemu-q35-virtio", serials=VDA)  # the clock was fixed in between
        return original(request)

    fake.respond = respond  # type: ignore[method-assign]
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 0 and script.text.count("The clock is three days behind.") >= 2  # shown in the hardware step and in the first summary
    assert "These problems stop the installation" in script.text


def test_a_disk_that_is_too_small_for_the_layout_is_refused_with_advice(installer, host: FakeHost) -> None:
    reply = inspect_reply("qemu-q35-virtio", serials=VDA)
    for disk in reply["disks"]:
        if disk["path"] == "/dev/vda":
            disk["size"] = 20 * GIB
    fake = installer(reply)
    script = Script([*to_disk_step(), ("Step 4 of 9: Disk", "1"), ("Step 5 of 9", ""), ("Encrypt", ""), ("Step 6 of 9", "2"), ("too small", END)])
    run_main(["--socket", fake.path], script, host)
    assert "The disk is too small for this layout" in script.flat and "pick a lighter desktop" in script.flat


def test_going_back_from_the_review_returns_to_the_last_step(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    script = Script([*walk_to_review(), ("Step 9 of 9", "b"), ("Step 8 of 9: Time zone and language", END)])  # the step starts again; Enter keeps every answer
    assert run_main(["--socket", fake.path], script, host).code == 130
    assert script.text.count("Step 9 of 9: Review and start") == 1 and script.text.count("Step 8 of 9") == 2


def test_quitting_from_the_review_asks_first(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    script = Script([*walk_to_review(), ("Step 9 of 9", "q"), ("Quit the installer?", "n"), ("type the disk name", "q"), ("Quit the installer?", "y")])
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 130 and not [r for r in fake.requests if r["action"] == "start"]


# --- the summary as a function ---------------------------------------------------------------------------------------------------------------------------


def machine_view() -> MachineView:
    return MachineView(inspect_reply("qemu-q35-virtio", serials=VDA))


def test_build_summary_of_a_valid_manifest_has_no_problems() -> None:
    summary = build_summary(valid_manifest(), machine_view())
    assert summary.ok and summary.hardware and any(line.startswith("ERASE  /dev/vda") for line in summary.storage)
    assert dict(summary.settings)["Disk"].endswith("ALL DATA ON IT WILL BE ERASED.")
    data = summary.to_dict()
    assert set(data) == {"settings", "hardware_plan", "storage_plan", "notes", "advice", "problems"} and json.dumps(data)


def test_problems_from_the_manifest_the_plan_and_the_disk_are_all_collected() -> None:
    manifest = valid_manifest()
    manifest.system.timezone = "Mars/Olympus"
    manifest.storage.device = "/dev/nope"
    summary = build_summary(manifest, machine_view())
    assert not summary.ok
    assert any("Unknown timezone" in problem for problem in summary.problems)
    assert any("/dev/nope was not found on this PC" in problem for problem in summary.problems)  # hwplan: the target is not among the detected disks


def test_a_missing_identity_is_a_problem_only_when_it_is_required() -> None:
    manifest = valid_manifest()
    manifest.storage.device_id = ""
    assert build_summary(manifest, machine_view()).ok
    required = build_summary(manifest, machine_view(), require_identity=True)
    assert not required.ok and any("is not identified yet" in problem for problem in required.problems)


def test_a_manifest_with_warnings_shows_them_as_notes() -> None:
    class Warned(InstallManifest):
        def warnings(self) -> list[str]:  # the method the manifest owner adds (contract I4)
            return ["GNOME on a hardened stage3 is not verified."]

    manifest = Warned()
    manifest.storage.device, manifest.storage.device_id = "/dev/vda", "VDA-SERIAL-0001"
    manifest.accounts.password = manifest.accounts.root_password = PASSWORD
    assert "GNOME on a hardened stage3 is not verified." in build_summary(manifest, machine_view()).notes


def test_the_summary_survives_a_planner_that_breaks(monkeypatch) -> None:
    from gentoo_installerd import hwplan

    def broken(*args, **kwargs):
        raise RuntimeError("planner bug")

    monkeypatch.setattr(hwplan, "plan", broken)
    summary = build_summary(valid_manifest(), machine_view())
    assert any("could not be worked out here (RuntimeError: planner bug)" in note for note in summary.notes) and summary.ok


def test_keyboard_label_and_settings_rows() -> None:
    manifest = valid_manifest()
    assert keyboard_label(manifest) == "English (US)"
    manifest.system.keymap, manifest.system.keyboard_layout, manifest.system.keyboard_variant = "us", "ru", ""
    assert keyboard_label(manifest) == "Russian (console stays US)"
    manifest.system.keyboard_layout, manifest.system.keyboard_variant = "xx", "abc"
    assert keyboard_label(manifest) == "xx (abc)"
    rows = dict(settings_rows(manifest, MachineView()))
    assert rows["Disk"].startswith("/dev/vda") and rows["Network"] == "networkmanager"


# --- a settings file reviewed interactively -------------------------------------------------------------------------------------------------------------------


def write_redacted(tmp_path: Path, **changes) -> str:
    manifest = valid_manifest()
    manifest.storage.device_id = ""
    for key, value in changes.items():
        section, _, name = key.partition("__")
        setattr(getattr(manifest, section), name, value)
    path = tmp_path / "saved.json"
    path.write_text(manifest.to_json(redact=True))
    return str(path)


def test_a_saved_copy_is_reviewed_with_the_missing_passwords_asked(installer, host: FakeHost, tmp_path: Path) -> None:
    fake = installer(inspect_reply("qemu-q35-virtio"))
    script = Script([
        ("does not contain passwords", "x"), ("at least 8 characters", "typed-password-1"), ("Type it again", "typed-password-1"),
        ("no root password", "y"),
        ("neither a serial number nor a WWN", "y"),
        ("type the disk name exactly", "/dev/vda"), ("The installation finished", "4"),
    ])
    result = run_main(["--manifest", write_redacted(tmp_path), "--socket", fake.path], script, host)
    script.assert_finished()
    sent = fake.manifests[0]
    assert result.code == 0 and sent["accounts"]["password"] == "typed-password-1" and sent["accounts"]["root_password"] == "typed-password-1"
    assert sent["storage"]["device_id_kind"] == "path" and sent["storage"]["device_id"].startswith("pci-0000:00:03.0|")
    assert "typed-password-1" not in script.text
    assert host.calls == []  # a settings file never switches the console keyboard behind the user's back


def test_the_settings_file_is_kept_as_it_is_and_nothing_is_changed_from_the_hardware(installer, host: FakeHost, tmp_path: Path) -> None:
    fake = serial_installer(installer)
    path = write_redacted(tmp_path, storage__firmware="uefi", system__keymap="de-latin1", system__keyboard_layout="de", storage__swap_mib=1234)
    script = Script([
        ("does not contain passwords", "x"), ("at least", "typed-password-1"), ("Type it again", "typed-password-1"), ("no root password", "y"),
        ("type the disk name exactly", "/dev/vda"), ("The installation finished", "4"),
    ])
    run_main(["--manifest", path, "--socket", fake.path], script, host)
    sent = fake.manifests[0]
    assert sent["storage"]["swap_mib"] == 1234 and sent["system"]["keymap"] == "de-latin1"
    assert sent["storage"]["device_id"] == "VDA-SERIAL-0001"  # pinned from the inventory


def test_a_saved_copy_can_be_changed_before_it_is_installed(installer, host: FakeHost, tmp_path: Path) -> None:
    fake = serial_installer(installer)
    script = Script([
        ("does not contain passwords", "x"), ("at least", "typed-password-1"), ("Type it again", "typed-password-1"), ("no root password", "y"),
        ("type the disk name exactly", "c"), ("Which setting", "6"),
        ("Step 6 of 9", "4"), ("init system", "2"),
        ("Step 7 of 9", ""), ("Your user name", ""), ("Keep the password", ""), ("How should", ""),
        ("Step 8 of 9", "5"), ("Choose your time zone", "berlin"), ("Which language", ""),
        ("type the disk name exactly", "/dev/vda"), ("The installation finished", "4"),
    ])
    result = run_main(["--manifest", write_redacted(tmp_path), "--socket", fake.path], script, host)
    sent = fake.manifests[0]
    assert result.code == 0 and sent["system"]["desktop"] == "minimal" and sent["system"]["init"] == "systemd" and sent["system"]["stage_variant"] == "amd64-systemd"
    assert sent["accounts"]["password"] == "typed-password-1"
