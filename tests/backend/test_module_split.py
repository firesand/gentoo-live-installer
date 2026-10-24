"""The configure.py / install.py split: every name the old modules exported is still importable from the facade and from its new module."""

from __future__ import annotations

import importlib

import pytest

# name -> module (below gentoo_installerd) that defines it now; configure.py is a facade that re-exports all of them.
CONFIGURE_NAMES = {
    "ConfigurationError": "configure_base",
    "COMPANION_ATOMS": "configure_portage",
    "PREFERRED_FIRST": "configure_desktop",
    "PackageInstallError": "configure_packages",
    "PACKAGE_USE_ATOM": "configure_portage",
    "DESKTOP_USE": "configure_portage",
    "NVRAM_FAILURE": "configure_boot",
    "RECEIPT_PATH": "configure_base",
    "MEMORY_ENV_NAME": "configure_portage",
    "MEMORY_ENV_FILE": "configure_portage",
    "PACKAGE_ENV_FILE": "configure_portage",
    "NOT_ENOUGH_MEMORY": "configure_packages",
    "CERTIFICATE_ROOTS": "configure_network",
    "shell_value": "configure_base",
    "ninja_opts_from_make_opts": "configure_portage",
    "Resources": "configure_base",
    "machine_resources": "configure_base",
    "auto_make_opts": "configure_portage",
    "auto_emerge_jobs": "configure_portage",
    "_require_use": "configure_portage",
    "_default_use": "configure_portage",
    "_forbid_use": "configure_portage",
    "_INCREMENTAL_KEYS": "configure_portage",
    "_PLAN_FORBIDDEN_KEYS": "configure_portage",
    "render_make_conf": "configure_portage",
    "render_package_use": "configure_portage",
    "render_fstab": "configure_fstab",
    "locale_gen_line": "configure_system",
    "render_hosts": "configure_system",
    "render_systemd_network": "configure_network",
    "render_netifrc": "configure_network",
    "render_dhcpcd_static": "configure_network",
    "_keyfile_escape": "configure_network",
    "render_nm_ip_sections": "configure_network",
    "render_nm_wifi": "configure_network",
    "render_nm_wired": "configure_network",
    "_IMPORTABLE_NM_TYPES": "configure_network",
    "_NM_CERTIFICATE_KEYS": "configure_network",
    "ImportedProfile": "configure_network",
    "rewrite_keyfile": "configure_network",
    "_keyboard_is_default": "configure_system",
    "render_xorg_keyboard": "configure_system",
    "render_kxkbrc": "configure_system",
    "render_dconf_keyboard": "configure_system",
    "ESP_SYNC_HOOKS": "configure_boot",
    "_ESP_SYNC_TEMPLATE": "configure_boot",
    "render_esp_sync_hook": "configure_boot",
    "check_secure_boot_material": "configure_boot",
    "live_input_problems": "configure_system",
    "root_stack_problems": "configure_fstab",
    "BUILD_LOG_TAIL_BYTES": "configure_packages",
    "_LOG_LINES": "configure_packages",
    "_LOG_LINE_CHARS": "configure_packages",
    "_ANSI": "configure_packages",
    "FailedBuild": "configure_packages",
    "EmergeDiagnosis": "configure_packages",
    "_RESOLUTION_MARKERS": "configure_packages",
    "_TRANSIENT_PATTERN": "configure_packages",
    "_MEMORY_PATTERN": "configure_packages",
    "_LOG_MEMORY_PATTERN": "configure_packages",
    "_LOG_ERROR_PATTERNS": "configure_packages",
    "_FAILED_ENTRY": "configure_packages",
    "_FAILED_PHASE": "configure_packages",
    "_FAILED_TO_EMERGE": "configure_packages",
    "_DROPPED": "configure_packages",
    "_BUILD_LOG": "configure_packages",
    "_SHORT_OF_RAM": "configure_packages",
    "_SHORT_OF_SOMETHING": "configure_packages",
    "_NUMBERED_ENTRY": "configure_packages",
    "_PHASES": "configure_packages",
    "_build_log_findings": "configure_packages",
    "_failure_sentence": "configure_packages",
    "explain_emerge_failure": "configure_packages",
    "_CYCLE_BREAK_RE": "configure_packages",
    "CYCLE_BREAK_USE": "configure_packages",
    "CYCLE_BREAK_RECORD": "configure_packages",
    "MAX_CYCLE_BREAKS": "configure_packages",
    "package_of": "configure_packages",
    "cycle_break_suggestion": "configure_packages",
    "DesktopAtoms": "configure_desktop",
    "KeepGoingResult": "configure_packages",
    "DesktopResult": "configure_desktop",
    "_plan_list": "configure_base",
    "_plan_scalar": "configure_base",
    "GRUB_EFI_FILES": "configure_boot",
    "grub_efi_target": "configure_boot",
    "efi_arch": "configure_boot",
    "ExistingStack": "configure_fstab",
    "UNSUPPORTED_ROOT_TYPES": "configure_fstab",
    "LVM_NAME_RE": "configure_fstab",
    "MD_UUID_RE": "configure_fstab",
    "MDADM_ARRAY_RE": "configure_fstab",
    "_is_md": "configure_fstab",
    "detect_existing_stack": "configure_fstab",
    "_lvm_volume": "configure_fstab",
    "_md_uuid": "configure_fstab",
    "_md_scan": "configure_fstab",
}
# Names that moved out of install.py into stages/ (install.py imports them again).
INSTALL_MOVED = {
    "InstallError": "stages.context",
    "_timestamp": "stages.context",
    "_chown_root": "stages.context",
    "_write_private": "stages.context",
}
INSTALL_STAYED = ['CLOCK_TOLERANCE_SECONDS', 'CheckpointStore', 'EMERGE_PROGRESS', 'InstallBusyError', 'Installer', 'LOG', 'LOG_DIRECTORY_IN_TARGET', 'STAGES', 'STAGE_WEIGHTS', '_LogFile', 'describe_clock_offset', 'error_code', 'manifest_secrets', 'stage_weights', 'tools_for_selection']


@pytest.mark.parametrize("name", sorted(CONFIGURE_NAMES))
def test_configure_names_come_from_the_facade_and_the_new_module(name):
    facade = importlib.import_module("gentoo_installerd.configure")
    module = importlib.import_module("gentoo_installerd." + CONFIGURE_NAMES[name])
    assert getattr(facade, name) is getattr(module, name)


def test_configurer_has_the_methods_of_every_domain():
    from gentoo_installerd.configure import Configurer

    for method in ("write", "configure_portage", "configure_network", "configure_bootloader", "configure_identity",
                   "configure_accounts", "install_desktop", "detect_stack", "write_plan_files", "build_receipt"):
        assert callable(getattr(Configurer, method)), method


@pytest.mark.parametrize("name", sorted(INSTALL_MOVED))
def test_install_names_that_moved_are_still_importable_from_install(name):
    install = importlib.import_module("gentoo_installerd.install")
    module = importlib.import_module("gentoo_installerd." + INSTALL_MOVED[name])
    assert getattr(install, name) is getattr(module, name)


@pytest.mark.parametrize("name", INSTALL_STAYED)
def test_install_names_that_stayed(name):
    install = importlib.import_module("gentoo_installerd.install")
    assert hasattr(install, name)


def test_every_stage_has_its_method_on_the_installer():
    from gentoo_installerd.install import STAGES, Installer

    for stage in STAGES:
        if stage in ("cleanup", "preflight"):
            continue
        method = {"validation": "_validate"}.get(stage, "_" + stage)
        assert callable(getattr(Installer, method)), stage
