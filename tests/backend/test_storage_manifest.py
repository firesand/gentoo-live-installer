"""Storage section of the manifest: swap rows, labels, identities, approvals, encryption wording."""

from __future__ import annotations

import json

import pytest

from gentoo_installer.manifest import InstallManifest, ManifestError, Partition, Storage

from tests.backend.helpers import manifest


def manual(*rows: Partition, firmware: str = "bios") -> InstallManifest:
    m = manifest()
    m.storage.mode = "manual"
    m.storage.firmware = firmware
    m.storage.swap_mib = 0
    m.storage.partitions = list(rows)
    return m


def storage_errors(m: InstallManifest) -> list[str]:
    return m.validate_sections()["storage"]


class TestManualSwapRows:
    def test_swap_row_without_a_mountpoint_is_valid(self):
        m = manual(Partition("/dev/sda1", "/", "ext4"), Partition("/dev/sda2", "", "swap"))
        assert storage_errors(m) == []

    def test_swap_row_with_a_mountpoint_is_rejected(self):
        m = manual(Partition("/dev/sda1", "/", "ext4"), Partition("/dev/sda2", "/swap", "swap"))
        assert any("swap partition has no mountpoint" in e for e in storage_errors(m))

    def test_other_rows_still_need_an_absolute_mountpoint(self):
        m = manual(Partition("/dev/sda1", "/", "ext4"), Partition("/dev/sda3", "", "ext4"))
        assert any("Invalid mountpoint" in e for e in storage_errors(m))
        assert any("Invalid mountpoint" in e for e in storage_errors(manual(Partition("/dev/sda1", "/data dir", "ext4"), Partition("/dev/sda2", "/", "ext4"))))

    def test_devices_with_whitespace_are_rejected(self):
        m = manual(Partition("/dev/sda 1", "/", "ext4"))
        assert any("Invalid partition device" in e for e in storage_errors(m))


class TestRowFieldsThatNothingApplies:
    def test_size_and_flags_are_rejected_instead_of_silently_ignored(self):
        m = manual(Partition("/dev/sda1", "/", "ext4", size_mib=1024, flags=["boot"]))
        errors = storage_errors(m)
        assert any("sizes are not supported" in e for e in errors)
        assert any("flags are not supported" in e for e in errors)

    @pytest.mark.parametrize(
        ("filesystem", "label", "fragment"),
        [
            ("ext4", "x" * 17, "too long"),
            ("xfs", "x" * 13, "too long"),
            ("vfat", "x" * 12, "too long"),
            ("swap", "x" * 16, "too long"),
            ("ext4", "my disk", "spaces"),
            ("ext4", "-L", "dash"),
            ("vfat", "bad.label", "FAT"),
            ("none", "x", "has none"),
        ],
    )
    def test_bad_labels(self, filesystem, label, fragment):
        m = manual(Partition("/dev/sda1", "/", "ext4"), Partition("/dev/sda2", "/data", filesystem, label=label))
        assert any(fragment in e for e in storage_errors(m)), storage_errors(m)

    @pytest.mark.parametrize(("filesystem", "label"), [("ext4", "x" * 16), ("xfs", "x" * 12), ("vfat", "EFI-1"), ("swap", "x" * 15), ("btrfs", "data_1")])
    def test_good_labels(self, filesystem, label):
        m = manual(Partition("/dev/sda1", "/", "ext4"), Partition("/dev/sda2", "/data", filesystem, label=label))
        assert [e for e in storage_errors(m) if "label" in e.lower()] == []

    def test_a_label_on_a_preserved_partition_would_be_ignored(self):
        m = manual(Partition("/dev/sda1", "/", "ext4"), Partition("/dev/sda2", "/data", "ext4", format=False, label="data"))
        assert any("only written when the partition is formatted" in e for e in storage_errors(m))

    def test_class_level_limits_are_not_serialised(self):
        row = Partition("/dev/sda1", "/", "ext4", label="root")
        assert "LABEL_LIMITS" not in InstallManifest.to_dict(manual(row))["storage"]["partitions"][0]


class TestIdentityAndApprovals:
    def test_path_identity_needs_three_parts(self):
        m = manifest()
        m.storage.device_id_kind = "path"
        m.storage.device_id = "pci-0000:00:04.0|85899345920|QEMU HARDDISK"
        assert storage_errors(m) == []
        for bad in ("pci-0000:00:04.0", "pci/x|10|m", "|10|m", "pci|ten|m", "pci|10|m|x", "pci|10|m\n"):
            m.storage.device_id = bad
            assert any("by-path|size|model" in e for e in storage_errors(m)), bad

    def test_serial_and_wwn_kinds_need_a_value(self):
        for kind in ("serial", "wwn"):
            m = manifest()
            m.storage.device_id_kind = kind
            m.storage.device_id = ""
            assert any(f"identity kind is {kind}" in e for e in storage_errors(m))
            m.storage.device_id = "ABC123"
            assert storage_errors(m) == []

    def test_unknown_kind_is_rejected(self):
        m = manifest()
        m.storage.device_id_kind = "kernel-name"
        assert any("Unknown disk identity kind" in e for e in storage_errors(m))

    def test_additional_approvals_are_serials_wwns_or_path_identities(self):
        m = manifest()
        m.storage.additional_device_ids = ["SER-B", "0x5000c500aaaa", "pci-0000:00:05.0|1000204886016|ST1000DM010"]
        assert storage_errors(m) == []
        for bad in ("", "a|b", "pci|x|m", "evil\nserial"):
            m.storage.additional_device_ids = ["SER-B", bad]
            assert any("Additional disk approvals" in e for e in storage_errors(m)), repr(bad)

    def test_new_fields_default_off_and_old_manifests_still_load(self):
        raw = InstallManifest().to_dict()
        for name in ("additional_device_ids", "release_holders", "overwrite_existing_system"):
            del raw["storage"][name]
        loaded = InstallManifest.from_dict(raw)
        assert loaded.storage.additional_device_ids == []
        assert loaded.storage.release_holders is False and loaded.storage.overwrite_existing_system is False

    def test_new_fields_round_trip(self):
        m = manifest()
        m.storage.additional_device_ids = ["SER-B"]
        m.storage.release_holders = True
        m.storage.overwrite_existing_system = True
        again = InstallManifest.from_json(m.to_json())
        assert again.storage.additional_device_ids == ["SER-B"]
        assert again.storage.release_holders and again.storage.overwrite_existing_system
        with pytest.raises(ManifestError, match="must be a list"):
            InstallManifest.from_dict({"storage": {"additional_device_ids": "SER-B"}})


class TestGuidedRules:
    def test_encrypted_root_cannot_have_a_plaintext_home_partition(self):
        m = manifest()
        m.storage.luks = True
        m.storage.luks_passphrase = "correct horse battery"
        m.storage.separate_home = True
        m.storage.home_mib = 20000
        assert any("/home" in e and "unencrypted" in e for e in storage_errors(m))
        m.storage.separate_home = False
        assert storage_errors(m) == []

    def test_plain_installs_may_have_a_home_partition(self):
        m = manifest()
        m.storage.separate_home = True
        m.storage.home_mib = 20000
        assert storage_errors(m) == []

    def test_home_partition_needs_a_size(self):
        m = manifest()
        m.storage.separate_home = True
        assert any("home partition needs a positive size" in e for e in storage_errors(m))


class TestRaidAndEncryptionWording:
    def test_raid_needs_an_assembled_array_in_manual_mode(self):
        s = Storage(raid_level="1", device="/dev/sda")
        assert "not created by the installer" in s.raid_problem()
        s.mode = "manual"
        assert "/dev/md" in s.raid_problem()
        s.device = "/dev/md0"
        assert s.raid_problem() == ""
        assert Storage().raid_problem() == ""

    def test_no_luks_says_not_enabled(self):
        assert Storage().encryption_summary() == "Not enabled"

    def test_guided_luks_says_swap_is_never_stored_unencrypted(self):
        text = Storage(luks=True, swap_mib=4096).encryption_summary()
        assert text == "LUKS2 on the root volume; swap is never stored unencrypted (/boot stays readable for the bootloader)"
        assert "NOT" not in text
        assert "swap" not in Storage(luks=True, swap_mib=0).encryption_summary()

    def test_manual_luks_lists_what_is_not_encrypted(self):
        s = Storage(luks=True, mode="manual", partitions=[
            Partition("/dev/sda1", "/efi", "vfat"), Partition("/dev/sda2", "/boot", "ext4"), Partition("/dev/sda3", "/", "ext4"),
            Partition("/dev/sda4", "/home", "ext4"), Partition("/dev/sda5", "", "swap"), Partition("/dev/mapper/data", "/srv", "ext4"),
        ])
        text = s.encryption_summary()
        assert "root volume only" in text and "NOT encrypted: /home, swap" in text and "/srv" not in text
        assert json.dumps(text)
