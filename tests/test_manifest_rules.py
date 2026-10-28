"""Manifest rules of Wave 2: explicit partition format, stage3/profile consistency, RAID, proxy secrets, advice."""

from __future__ import annotations

import hashlib
import json

import pytest

from gentoo_installer import manifest as manifest_module
from gentoo_installer.manifest import (
    FAMILY_DESKTOP_SUPPORT,
    PUBLISHED_PROFILES,
    STAGE3_VARIANTS,
    InstallManifest,
    ManifestError,
    Partition,
    desktop_problem,
    profile_problem,
    stage_family,
)


def valid() -> InstallManifest:
    m = InstallManifest()
    m.storage.device = "/dev/nvme0n1"
    m.accounts.password = "user-password"
    m.accounts.root_password = "root-password"
    return m


def with_changes(**changes) -> InstallManifest:
    m = valid()
    for name, value in changes.items():
        section, _, field = name.partition("__")
        setattr(getattr(m, section), field, value)
    return m


# ------------------------------------------------------------------------ manual rows state their format


class TestPartitionFormat:
    def manual(self, *rows: dict) -> dict:
        raw = valid().to_dict()
        raw["storage"]["mode"] = "manual"
        raw["storage"]["partitions"] = list(rows)
        return raw

    def test_a_row_without_format_is_refused_and_named(self):
        raw = self.manual({"device": "/dev/nvme0n1p1", "mountpoint": "/efi", "filesystem": "vfat", "format": False},
                          {"device": "/dev/nvme0n1p2", "mountpoint": "/", "filesystem": "ext4"})
        with pytest.raises(ManifestError, match=r"storage\.partitions\[1\]\.format must be stated explicitly: true formats the partition, false keeps its data"):
            InstallManifest.from_dict(raw)

    @pytest.mark.parametrize("value", [True, False])
    def test_either_answer_is_accepted(self, value):
        raw = self.manual({"device": "/dev/nvme0n1p1", "mountpoint": "/efi", "filesystem": "vfat", "format": value},
                          {"device": "/dev/nvme0n1p2", "mountpoint": "/", "filesystem": "ext4", "format": True})
        assert InstallManifest.from_dict(raw).storage.partitions[0].format is value

    def test_a_format_that_is_not_true_or_false_is_still_a_type_error(self):
        raw = self.manual({"device": "/dev/nvme0n1p2", "mountpoint": "/", "format": "yes"})
        with pytest.raises(ManifestError, match="format must be true or false"):
            InstallManifest.from_dict(raw)

    def test_everything_the_installer_writes_states_it(self):
        m = valid()
        m.storage.mode = "manual"
        m.storage.partitions = [Partition("/dev/nvme0n1p1", "/efi", "vfat"), Partition("/dev/nvme0n1p2", "/")]
        assert all("format" in row for row in m.to_dict()["storage"]["partitions"])
        assert InstallManifest.from_json(m.to_json()).storage.partitions == m.storage.partitions

    def test_other_lists_are_converted_as_before_and_errors_name_the_item(self):
        with pytest.raises(ManifestError, match=r"storage\.partitions\[0\]\.device must be a string"):
            InstallManifest.from_dict(self.manual({"device": 5, "format": True}))
        assert InstallManifest.from_dict({"options": {"extra_packages": ["app-editors/vim"]}}).options.extra_packages == ["app-editors/vim"]


# ------------------------------------------------------ stage3 family, desktop, profile and multilib


class TestStageFamilies:
    @pytest.mark.parametrize(("variant", "family"), [("amd64-openrc", "standard"), ("amd64-systemd", "standard"), ("amd64-desktop-openrc", "desktop"),
                                                    ("amd64-nomultilib-systemd", "nomultilib"), ("amd64-hardened-openrc", "hardened")])
    def test_the_family_comes_from_the_variant_name(self, variant, family):
        assert stage_family(variant) == family

    @pytest.mark.parametrize("variant", ["amd64-nomultilib-openrc", "amd64-hardened-openrc", "amd64-hardened-systemd", "amd64-nomultilib-systemd"])
    def test_kde_cannot_be_installed_on_a_stage3_without_the_desktop_flags(self, variant):
        init = "systemd" if variant.endswith("systemd") else "openrc"
        m = with_changes(system__stage_variant=variant, system__init=init, system__desktop="kde", system__multilib="nomultilib" not in variant)
        errors = m.validate_sections()["system"]
        assert any("stage3" in error and "KDE Plasma needs a Standard or Desktop stage3" in error for error in errors), errors
        assert desktop_problem(variant, "kde") in errors
        with pytest.raises(ManifestError, match="stage3"):
            m.require_valid()

    @pytest.mark.parametrize("desktop", ["minimal", "gnome", "xfce"])
    @pytest.mark.parametrize("variant", ["amd64-nomultilib-openrc", "amd64-hardened-openrc"])
    def test_the_other_choices_are_allowed_but_gnome_and_xfce_come_with_a_warning(self, desktop, variant):
        m = with_changes(system__stage_variant=variant, system__desktop=desktop, system__multilib="nomultilib" not in variant,
                         options__install_hwmonux=False, options__install_portagehub=False,
                         options__install_gentoo_settings=False, options__install_kernelsmith=False)
        assert m.validate() == []
        unverified = [text for text in m.warnings() if text.startswith("Not verified")]
        assert bool(unverified) == (desktop != "minimal")

    @pytest.mark.parametrize("variant", ["amd64-openrc", "amd64-desktop-openrc"])
    @pytest.mark.parametrize("desktop", ["kde", "gnome", "xfce", "minimal"])
    def test_the_standard_and_desktop_stage3_take_every_desktop(self, variant, desktop):
        m = with_changes(system__stage_variant=variant, system__desktop=desktop)
        assert m.validate() == [] and not any(text.startswith("Not verified") for text in m.warnings())

    def test_the_tables_agree_with_each_other(self):
        assert set(FAMILY_DESKTOP_SUPPORT) == set(manifest_module.FAMILY_LABELS) == {"standard", "desktop", "nomultilib", "hardened"}
        assert manifest_module.UNVERIFIED_FAMILIES == {"nomultilib", "hardened"}


class TestProfiles:
    def test_every_published_profile_but_two_has_a_stage3_it_fits(self):
        assert len(PUBLISHED_PROFILES) == 14
        for profile in sorted(PUBLISHED_PROFILES):
            init = "systemd" if profile.endswith("/systemd") else "openrc"
            fitting = [variant for variant in sorted(STAGE3_VARIANTS) if variant.endswith(init) and profile_problem(profile, init, variant) == ""]
            # Gentoo publishes a hardened no-multilib profile, but the installer offers no stage3 for it.
            assert bool(fitting) is ("no-multilib/hardened" not in profile), (profile, fitting)

    def test_an_empty_profile_means_derive_it(self):
        assert profile_problem("", "openrc", "amd64-openrc") == "" and valid().validate() == []

    @pytest.mark.parametrize("profile", ["default/linux/amd64/17.1/desktop", "default/linux/amd64/23.0/desktop/plasma/nomultilib", "default/linux/amd64/23.0/desktop/kde"])
    def test_a_profile_nobody_publishes_is_refused_by_name(self, profile):
        errors = with_changes(system__profile=profile).validate_sections()["system"]
        assert any("profile" in error.lower() and "published" in error.lower() for error in errors), errors

    def test_the_profile_must_match_the_init_system(self):
        assert any("init system" in e for e in with_changes(system__profile="default/linux/amd64/23.0/systemd").validate_sections()["system"])
        assert any("init system" in e for e in with_changes(system__init="systemd", system__stage_variant="amd64-systemd",
                                                            system__profile="default/linux/amd64/23.0/desktop").validate_sections()["system"])
        assert with_changes(system__init="systemd", system__stage_variant="amd64-systemd", system__profile="default/linux/amd64/23.0/desktop/systemd").validate() == []

    def test_the_profile_must_match_the_stage3(self):
        multilib = with_changes(system__profile="default/linux/amd64/23.0/no-multilib").validate_sections()["system"]
        assert any("multilib versus no-multilib" in e for e in multilib)
        hardened = with_changes(system__profile="default/linux/amd64/23.0/hardened").validate_sections()["system"]
        assert any("hardened versus standard" in e for e in hardened)
        ok = with_changes(system__stage_variant="amd64-hardened-openrc", system__profile="default/linux/amd64/23.0/hardened", system__desktop="minimal")
        assert not any("profile" in e.lower() for e in ok.validate_sections()["system"])

    def test_a_derived_profile_is_always_consistent(self):
        for init in ("openrc", "systemd"):
            for variant in ("amd64", "amd64-desktop", "amd64-nomultilib", "amd64-hardened"):
                for desktop in ("minimal", "gnome", "xfce"):
                    m = with_changes(system__init=init, system__stage_variant=f"{variant}-{init}", system__desktop=desktop, system__multilib="nomultilib" not in variant)
                    assert profile_problem(m.effective_profile(), init, m.system.stage_variant) == "", (init, variant, desktop, m.effective_profile())


class TestMultilib:
    def test_a_no_multilib_stage3_needs_multilib_turned_off(self):
        m = with_changes(system__stage_variant="amd64-nomultilib-openrc", system__desktop="minimal")
        assert any("no-multilib stage3 cannot run 32-bit applications" in e for e in m.validate_sections()["system"])
        m.system.multilib = False
        assert m.validate() == []

    def test_the_other_direction_was_already_checked(self):
        m = with_changes(system__multilib=False)
        assert any("Pure 64-bit installation requires a no-multilib stage3" in e for e in m.validate_sections()["system"])


# ------------------------------------------------------------------------------- proxy and secrets


class TestProxy:
    @pytest.mark.parametrize("proxy", ["http://proxy.lan:3128", "https://proxy.lan", "http://user:pass@proxy.lan:3128", "http://[2001:db8::1]:3128/"])
    def test_well_formed_proxies_are_accepted(self, proxy):
        assert with_changes(network__proxy=proxy).validate() == []

    @pytest.mark.parametrize("proxy", ["proxy.lan:3128", "socks5://proxy.lan:1080", "http://", "http://proxy.lan:0", "http://proxy.lan:3128/path", "http://proxy.lan:3128?x=1",
                                       "http://proxy lan:3128", "http://proxy.lan:99999", "ftp://proxy.lan"])
    def test_malformed_proxies_are_refused_in_the_network_section(self, proxy):
        errors = with_changes(network__proxy=proxy).validate_sections()["network"]
        assert any("proxy" in error.lower() for error in errors), (proxy, errors)

    def test_a_redacted_proxy_password_must_be_typed_again(self):
        errors = with_changes(network__proxy="http://alice:<redacted>@proxy.lan:3128").validate_sections()["network"]
        assert errors == ["Replace the redacted proxy password before installing"]


class TestRedaction:
    def test_only_the_proxy_password_is_masked(self):
        m = with_changes(network__proxy="http://alice:S3cret@proxy.lan:3128")
        redacted = m.to_dict(redact=True)
        assert redacted["network"]["proxy"] == "http://alice:<redacted>@proxy.lan:3128"
        assert redacted["accounts"]["password"] == "<redacted>" and redacted["storage"]["luks_passphrase"] == ""  # empty secrets stay empty
        assert "S3cret" not in m.to_json(redact=True)
        assert m.to_dict()["network"]["proxy"] == "http://alice:S3cret@proxy.lan:3128"  # the real thing is untouched

    @pytest.mark.parametrize(("proxy", "expected"), [
        ("http://alice:p@ss@proxy.lan:3128", "http://alice:<redacted>@proxy.lan:3128"),
        ("http://alice:a/b@proxy.lan:3128", "http://alice:<redacted>@proxy.lan:3128"),
        ("https://bob:pw@h", "https://bob:<redacted>@h"),
        ("http://proxy.lan:3128", "http://proxy.lan:3128"),
        ("http://alice@proxy.lan:3128", "http://alice@proxy.lan:3128"),
        ("http://alice:@proxy.lan:3128", "http://alice:@proxy.lan:3128"),
        ("http://alice:<redacted>@proxy.lan:3128", "http://alice:<redacted>@proxy.lan:3128"),
        ("not a url", "not a url"),
        ("", ""),
    ])
    def test_the_password_of_a_proxy_address_is_hidden_completely(self, proxy, expected):
        assert manifest_module._redact_url_password(proxy) == expected

    def test_the_fingerprint_does_not_change_with_the_proxy_password_but_does_with_the_proxy(self):
        def fingerprint(m: InstallManifest) -> str:
            return hashlib.sha256(m.to_json(redact=True).encode()).hexdigest()

        base = fingerprint(with_changes(network__proxy="http://alice:one@proxy.lan:3128"))
        assert fingerprint(with_changes(network__proxy="http://alice:two@proxy.lan:3128")) == base
        assert fingerprint(with_changes(network__proxy="http://alice:one@other.lan:3128")) != base

    def test_the_dead_secret_name_is_gone(self):
        assert manifest_module.SECRET_KEYS == {"password", "root_password", "luks_passphrase", "wifi_password"}
        assert all(hasattr(section, key) for section, key in (
            (valid().accounts, "password"), (valid().accounts, "root_password"), (valid().storage, "luks_passphrase"), (valid().network, "wifi_password")))


class TestLoadRedacted:
    def saved(self, tmp_path, m: InstallManifest):
        path = tmp_path / "settings.json"
        m.save(path)  # redacted by default
        return path

    def test_secrets_come_back_empty_and_everything_else_is_kept(self, tmp_path):
        m = with_changes(system__desktop="gnome", storage__luks=True, storage__luks_passphrase="long enough passphrase", network__wifi_ssid="home",
                         network__wifi_password="wifi-secret-1", network__manager="networkmanager", network__proxy="http://alice:S3cret@proxy.lan:3128")
        loaded = InstallManifest.load_redacted(self.saved(tmp_path, m))
        assert loaded.system.desktop == "gnome" and loaded.network.wifi_ssid == "home" and loaded.storage.luks is True
        assert (loaded.accounts.password, loaded.accounts.root_password, loaded.storage.luks_passphrase, loaded.network.wifi_password) == ("", "", "", "")

    def test_the_proxy_keeps_its_placeholder_so_validation_asks_for_the_password(self, tmp_path):
        loaded = InstallManifest.load_redacted(self.saved(tmp_path, with_changes(network__proxy="http://alice:S3cret@proxy.lan:3128")))
        assert loaded.network.proxy == "http://alice:<redacted>@proxy.lan:3128"
        assert "Replace the redacted proxy password before installing" in loaded.validate()

    def test_the_manifest_is_not_validated_while_it_is_read(self, tmp_path):
        loaded = InstallManifest.load_redacted(self.saved(tmp_path, valid()))
        assert loaded.validate() != []  # the passwords are missing, which is the point

    def test_a_file_that_cannot_be_read_is_a_manifest_error(self, tmp_path):
        broken = tmp_path / "broken.json"
        broken.write_text("{not json")
        with pytest.raises(ManifestError, match="Cannot read the settings file"):
            InstallManifest.load_redacted(broken)
        with pytest.raises(ManifestError, match="Cannot read the settings file"):
            InstallManifest.load_redacted(tmp_path / "missing.json")

    def test_unknown_fields_are_still_refused(self, tmp_path):
        path = tmp_path / "odd.json"
        path.write_text(json.dumps({"surprise": 1}))
        with pytest.raises(ManifestError, match="Unknown manifest fields"):
            InstallManifest.load_redacted(path)

    def test_the_normal_loader_still_refuses_a_redacted_file(self, tmp_path):
        with pytest.raises(ManifestError, match="redacted secret"):
            InstallManifest.load(self.saved(tmp_path, valid()))


# ----------------------------------------------------------------------------------------- RAID


class TestRaid:
    def test_raid_is_only_for_an_assembled_array_in_manual_mode(self):
        guided = with_changes(storage__raid_level="1").validate_sections()["storage"]
        assert any("not created by the installer" in e for e in guided)
        wrong_disk = with_changes(storage__raid_level="1", storage__mode="manual", storage__partitions=[Partition("/dev/sda1", "/", "ext4")],
                                  storage__firmware="bios").validate_sections()["storage"]
        assert any("assembled /dev/md device" in e for e in wrong_disk)
        array = with_changes(storage__raid_level="1", storage__mode="manual", storage__device="/dev/md0",
                             storage__partitions=[Partition("/dev/md0", "/", "ext4")], storage__firmware="bios")
        assert array.validate() == []

    def test_an_unknown_level_is_reported_once(self):
        errors = with_changes(storage__raid_level="7").validate_sections()["storage"]
        assert errors.count("Unsupported RAID level") == 1 and not any("not created" in e for e in errors)


# --------------------------------------------------------------------------------- advice and summary


class TestWarnings:
    def test_the_defaults_come_with_no_advice(self):
        assert valid().warnings() == []

    def test_companions_on_a_minimal_system_are_only_build_time(self):
        assert manifest_module.COMPANIONS_ON_MINIMAL in with_changes(system__desktop="minimal").warnings()
        assert manifest_module.COMPANIONS_ON_MINIMAL not in with_changes(system__desktop="minimal", options__install_hwmonux=False, options__install_portagehub=False,
                         options__install_gentoo_settings=False, options__install_kernelsmith=False).warnings()

    def test_the_known_limits_are_stated(self):
        text = " ".join(with_changes(system__kernel="manual", system__initramfs="none", system__secure_boot=True, system__dual_boot=True,
                                    system__bootloader="systemd-boot", network__import_live_profiles=True).warnings())
        for fragment in ("manual kernel", "Kernel updates are signed automatically", "Dual boot detection works with GRUB only", "Wi-Fi passwords"):
            assert fragment in text

    def test_advice_never_blocks(self):
        m = with_changes(system__stage_variant="amd64-hardened-openrc", system__desktop="gnome", system__multilib=True, options__install_hwmonux=False)
        assert m.warnings() and m.validate() == []


class TestSummary:
    def row(self, m: InstallManifest, name: str) -> str:
        return dict(m.summary())[name]

    def test_encryption_says_what_it_covers(self):
        assert self.row(valid(), "Encryption") == "Not enabled"
        guided = with_changes(storage__luks=True, storage__luks_passphrase="long enough passphrase")
        assert self.row(guided, "Encryption") == "LUKS2 on the root volume; swap is never stored unencrypted (/boot stays readable for the bootloader)"
        assert "LUKS enabled" not in guided.summary()

    def test_a_manual_layout_lists_what_stays_in_the_clear(self):
        m = with_changes(storage__mode="manual", storage__luks=True, storage__luks_passphrase="long enough passphrase", storage__partitions=[
            Partition("/dev/sda1", "/efi", "vfat"), Partition("/dev/sda2", "/boot", "ext4"), Partition("/dev/sda3", "/", "ext4"), Partition("/dev/sda4", "/home", "ext4")])
        assert "NOT encrypted: /home" in self.row(m, "Encryption")


class TestKernelSeries:
    def test_the_newest_series_is_the_default_and_both_series_are_valid(self):
        assert InstallManifest().system.kernel_series == "newest"
        assert with_changes(system__kernel_series="longterm").validate() == []

    def test_an_unknown_series_is_refused(self):
        assert "Unknown kernel series: choose newest or longterm" in with_changes(system__kernel_series="lts").validate()

    def test_a_saved_file_keeps_the_series(self, tmp_path):
        saved = tmp_path / "manifest.json"
        with_changes(system__kernel_series="longterm").save(saved, redact=False)
        assert InstallManifest.load(saved).system.kernel_series == "longterm"


class TestCompanions:
    def test_all_four_are_installed_by_default_in_their_order(self):
        assert InstallManifest().options.companions() == ["HWMonux", "PortageHub", "Gentoo Settings", "KernelSmith"]
        assert [atom for _, _, atom in manifest_module.COMPANION_APPS] == [
            "app-misc/hwmonux", "app-portage/portagehub", "app-admin/gentoo-settings", "sys-kernel/kernelsmith"]

    def test_the_summary_names_them(self):
        rows = dict(with_changes(options__install_hwmonux=False).summary())
        assert rows["Companion apps"] == "PortageHub, Gentoo Settings, KernelSmith"
