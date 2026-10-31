import pytest

from gentoo_installer.manifest import InstallManifest

from tests.vm.helpers import PUBLIC_KEY, load_vm_module


vm = load_vm_module()


@pytest.mark.parametrize("name", vm.ALL_SCENARIOS)
def test_scenario_manifests_match_schema_and_fast_install_choices(name):
    scenario = vm.SCENARIO_MAP[name]
    manifest = vm.build_manifest(scenario, PUBLIC_KEY)

    assert manifest.validate() == []
    assert InstallManifest.from_json(manifest.to_json()).to_dict() == manifest.to_dict()
    assert manifest.storage.device == "/dev/vda"
    assert manifest.storage.firmware == scenario.firmware
    assert manifest.storage.table == ("dos" if name == "bios" else "gpt")
    assert manifest.storage.luks is (name == "uefi-luks")
    assert manifest.system.init == scenario.init
    assert manifest.system.stage_variant == f"amd64-{scenario.init}"
    assert manifest.system.profile == "default/linux/amd64/23.0" + ("/systemd" if scenario.init == "systemd" else "")
    assert manifest.system.kernel == "gentoo-kernel-bin"
    assert manifest.portage.binhost is True
    assert "getbinpkg" in manifest.portage.features
    assert manifest.accounts.hostname == scenario.hostname
    assert manifest.accounts.ssh_authorized_keys == [PUBLIC_KEY]
    assert manifest.accounts.disable_root_login is True
    assert manifest.options.ssh is True
    assert manifest.options.install_hwmonux is True
    assert manifest.options.install_portagehub is True
    assert manifest.network.dhcp is (name != "bios")
    if name == "bios":
        assert manifest.network.address == "10.0.2.15/24"
        assert manifest.network.gateway == "10.0.2.2"
        assert manifest.network.dns == ["10.0.2.3"]


def test_luks_passphrase_is_test_only_and_sufficiently_long():
    manifest = vm.build_manifest(vm.SCENARIO_MAP["uefi-luks"], PUBLIC_KEY)
    assert manifest.storage.luks_passphrase == vm.TEST_LUKS_PASSPHRASE
    assert len(manifest.storage.luks_passphrase) >= 8


def test_bounded_compiler_resources_survive_saved_manifest(tmp_path):
    from gentoo_installerd.configure import render_make_conf

    manifest = vm.build_manifest(vm.SCENARIO_MAP["openrc"], PUBLIC_KEY,
                                 build_jobs=1, swap_mib=4096)
    saved = tmp_path / "manifest.json"
    manifest.save(saved, redact=False)
    reloaded = InstallManifest.load(saved)
    assert reloaded.storage.swap_mib == 4096
    assert reloaded.portage.make_opts == "-j1"
    # ninja-utils.eclass derives Ninja's -j/-l from MAKEOPTS when NINJAOPTS is unset, so only MAKEOPTS is persisted.
    rendered = render_make_conf(reloaded)
    assert "MAKEOPTS=-j1\n" in rendered
    assert "NINJAOPTS" not in rendered
    assert vm.companion_selection(reloaded) == {"hwmonux": True, "portagehub": True, "gentoo-settings": True, "kernelsmith": True}


@pytest.mark.parametrize(("jobs", "swap"), [(0, 512), (257, 512), (1, -1), (1, 131073)])
def test_invalid_compiler_resources_are_rejected(jobs, swap):
    with pytest.raises(vm.HarnessError, match="Build jobs"):
        vm.build_manifest(vm.SCENARIO_MAP["openrc"], PUBLIC_KEY,
                          build_jobs=jobs, swap_mib=swap)


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ("all", {"hwmonux": True, "portagehub": True, "gentoo-settings": True, "kernelsmith": True}),
        ("rust", {"hwmonux": False, "portagehub": True, "gentoo-settings": True, "kernelsmith": True}),
        ("both", {"hwmonux": True, "portagehub": True, "gentoo-settings": False, "kernelsmith": False}),
        ("portagehub", {"hwmonux": False, "portagehub": True, "gentoo-settings": False, "kernelsmith": False}),
        ("none", {"hwmonux": False, "portagehub": False, "gentoo-settings": False, "kernelsmith": False}),
    ],
)
def test_companion_selection_is_written_to_manifest(selection, expected):
    manifest = vm.build_manifest(vm.SCENARIO_MAP["openrc"], PUBLIC_KEY, selection)
    assert vm.companion_selection(manifest) == expected
    assert manifest.validate() == []


def test_companion_probe_plan_uses_saved_manifest_flags(tmp_path):
    manifest = vm.build_manifest(vm.SCENARIO_MAP["systemd"], PUBLIC_KEY, "portagehub")
    saved = tmp_path / "manifest.json"
    manifest.save(saved, redact=False)
    reloaded = InstallManifest.load(saved)

    plan = vm.companion_probe_plan(reloaded)
    assert [name for name, _, _ in plan] == ["disabled-hwmonux-absent", "portagehub", "disabled-gentoo-settings-absent", "disabled-kernelsmith-absent"]
    assert "hwmonux --dump" not in plan[0][1]
    assert "portagehub --self-test" == plan[1][1]
    assert "/var/db/pkg/app-admin" in plan[2][1] and "gentoo-settings-*" in plan[2][1]
    assert "/var/db/pkg/sys-kernel" in plan[3][1] and "kernelsmith-*" in plan[3][1]


def test_the_rust_companions_are_probed_without_a_display_on_each_init():
    for scenario, label in (("openrc", "OpenRC"), ("systemd", "systemd")):
        manifest = vm.build_manifest(vm.SCENARIO_MAP[scenario], PUBLIC_KEY, "rust")
        plan = {name: (command, parse) for name, command, parse in vm.companion_probe_plan(manifest)}
        assert plan["gentoo-settings"][0] == "gentoo-settings --status" and plan["kernelsmith"][0] == "kernelsmith detect"
        assert plan["gentoo-settings"][1](0, f"init: {label} (detected)\nkernel: 7.2.13\n").success
        assert not plan["gentoo-settings"][1](0, "init: unknown init (no clue)\n").success
        assert plan["kernelsmith"][1](0, f"System      vm (Gentoo)\nInit        {label}\n").success
        assert not plan["kernelsmith"][1](0, f"System      vm (not Gentoo)\nInit        {label}\n").success
        assert "disabled-hwmonux-absent" in plan


@pytest.mark.parametrize("key", ["", "not-a-key", "ssh-ed25519 bad;payload", "ssh-rsa\nsecond-line"])
def test_invalid_public_keys_are_rejected(key):
    with pytest.raises(vm.HarnessError, match="public key"):
        vm.build_manifest(vm.SCENARIO_MAP["openrc"], key)


def test_defaults_scenario_keeps_every_gui_default_except_disk_and_access():
    manifest = vm.build_manifest(vm.SCENARIO_MAP["defaults"], PUBLIC_KEY)
    expected = InstallManifest()
    expected.storage.device = vm.TARGET_DEVICE
    expected.storage.device_id = vm.TARGET_SERIAL
    expected.accounts.hostname = "gentoo-vm-defaults"
    expected.accounts.username = vm.TEST_USERNAME
    expected.accounts.password = vm.TEST_PASSWORD
    expected.accounts.root_mode = "locked"  # the wizard's first choice, as a fresh Personalize page shows it
    expected.accounts.ssh_authorized_keys = [PUBLIC_KEY]
    expected.options.ssh = True

    assert manifest.validate() == []
    assert manifest.to_dict() == expected.to_dict()
    assert manifest.system.desktop == "kde"


def test_defaults_scenario_applies_only_explicit_resource_overrides():
    manifest = vm.build_manifest(vm.SCENARIO_MAP["defaults"], PUBLIC_KEY, build_jobs=1, swap_mib=8192)
    assert manifest.portage.make_opts == "-j1"
    assert manifest.storage.swap_mib == 8192
    assert vm.build_manifest(vm.SCENARIO_MAP["openrc"], PUBLIC_KEY).storage.swap_mib == 512


def test_all_excludes_the_long_defaults_scenario():
    assert "defaults" in vm.SCENARIOS
    assert "defaults" not in vm.ALL_SCENARIOS


@pytest.mark.parametrize(("init", "desktop", "fragment"), [
    ("openrc", "kde", "grep -qxF 'DISPLAYMANAGER=\"sddm\"' /etc/conf.d/display-manager"),
    ("systemd", "gnome", "systemctl is-enabled gdm.service"),
    ("openrc", "xfce", "/var/db/pkg/xfce-base/xfce4-meta-[0-9]*"),
])
def test_desktop_probe_checks_package_and_display_manager(init, desktop, fragment):
    manifest = InstallManifest()
    manifest.system.init = init
    manifest.system.desktop = desktop
    [(name, command, parser)] = vm.desktop_probe_plan(manifest)
    assert name == "desktop"
    assert fragment in command
    assert parser(0, "ok").success
    assert not parser(1, "").success


def test_minimal_desktop_has_no_desktop_probe():
    manifest = InstallManifest()
    manifest.system.desktop = "minimal"
    assert vm.desktop_probe_plan(manifest) == []


def _flatten(value, prefix=""):
    if isinstance(value, dict) and value:
        return {path: item for key, child in value.items() for path, item in _flatten(child, f"{prefix}.{key}" if prefix else key).items()}
    return {prefix: value}


ACCESS_ONLY = {"accounts.root_mode", "accounts.disable_root_login", "storage.device", "storage.device_id", "accounts.hostname", "accounts.username", "accounts.password",
               "accounts.root_password", "accounts.ssh_authorized_keys", "options.ssh"}


@pytest.mark.parametrize(("kwargs", "extra"), [
    ({}, set()),
    ({"build_jobs": 1}, {"portage.make_opts"}),
    ({"build_jobs": 1, "swap_mib": 8192}, {"portage.make_opts", "storage.swap_mib"}),
])
def test_defaults_scenario_differs_from_the_wizard_defaults_only_in_disk_credentials_and_resources(kwargs, extra):
    """T14: the untouched install must stay untouched; a new field's default can never drift in unnoticed."""
    expected = _flatten(InstallManifest().to_dict(redact=False))
    actual = _flatten(vm.build_manifest(vm.SCENARIO_MAP["defaults"], PUBLIC_KEY, **kwargs).to_dict(redact=False))
    changed = {path for path in set(expected) | set(actual) if expected.get(path) != actual.get(path)}
    assert changed <= ACCESS_ONLY | extra, sorted(changed - ACCESS_ONLY - extra)
    # and the defaults the user cares most about are the real ones
    for path in ("system.desktop", "system.profile", "options.microcode", "options.firmware", "options.pipewire",
                 "options.bluetooth", "options.printing", "options.timesync", "options.cron", "hardware.guest_tools"):
        assert actual[path] == expected[path], path


def test_the_defaults_scenario_probes_the_default_desktop_and_every_companion():
    manifest = vm.build_manifest(vm.SCENARIO_MAP["defaults"], PUBLIC_KEY)
    assert vm.companion_selection(manifest) == {"hwmonux": True, "portagehub": True, "gentoo-settings": True, "kernelsmith": True}
    assert [name for name, _, _ in vm.desktop_probe_plan(manifest)] == ["desktop"]
    probes = {probe[0] for probe in vm.installed_system_probes(manifest, vm.SCENARIO_MAP["defaults"])}
    assert {"firmware", "cmdline", "root-source", "swap"} <= probes


def test_luks_with_lvm_inside_is_a_valid_guided_install_that_can_be_unlocked_on_the_serial_console():
    scenario = vm.SCENARIO_MAP["uefi-luks-lvm"]
    manifest = vm.build_manifest(scenario, PUBLIC_KEY)
    assert manifest.validate() == [] and (manifest.storage.luks, manifest.storage.lvm, manifest.storage.mode) == (True, True, "erase")
    assert (scenario.firmware, scenario.init, scenario.in_all) == ("uefi", "openrc", False)
    assert vm.serial_console_configured(manifest.system.kernel_cmdline_extra) and manifest.storage.luks_passphrase == vm.TEST_LUKS_PASSPHRASE
    assert "lvm" in vm.list_scenarios().split("uefi-luks-lvm", 1)[1].splitlines()[0]
