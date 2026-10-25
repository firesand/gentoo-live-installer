"""The gaming option: Portage files, tweaks, first-boot Flathub, the overlay, and the read-only readiness list."""

from __future__ import annotations

import re
from pathlib import Path

from gentoo_installerd import gaming_readiness as gr
from gentoo_installerd.configure import choice_problems
from gentoo_installerd.configure_gaming import FIRSTBOOT_LOCAL, FIRSTBOOT_SCRIPT, FIRSTBOOT_UNIT, KEYWORD_FILE, LICENSE_FILE, MODULES_FILE, SYSCTL_FILE, USE_FILE

from tests.backend.target_helpers import ScriptedRunner, configurer, read, target_manifest


def gaming(tmp_path: Path, init="openrc", bundle: Path | None = None, **options):
    m = target_manifest(system={"init": init, "stage_variant": f"amd64-{init}"}, options={"gaming_accept_external": True, **options})
    runner = ScriptedRunner()
    kwargs = {"companion_bundle": bundle} if bundle else {}
    c = configurer(tmp_path, runner, **kwargs)
    notes: list[str] = []
    c.notify = notes.append
    return m, c, runner, notes


def tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_gaming_off_does_nothing_at_all(tmp_path):
    m, c, runner, _ = gaming(tmp_path)
    assert m.options.gaming == "off"
    before = tree(tmp_path)
    assert c.install_gaming(m) == []
    assert runner.commands == [] and tree(tmp_path) == before
    assert c.gaming_atoms(m) == [] and c.gaming_portage_files(m) == [] and c.gaming_system_files(m) == []


def test_flatpak_mode_on_openrc(tmp_path):
    m, c, runner, _ = gaming(tmp_path, gaming="flatpak")
    assert c.install_gaming(m) == []
    assert read(tmp_path, KEYWORD_FILE) == "games-util/game-device-udev-rules ~amd64\n"
    assert not (tmp_path / LICENSE_FILE).exists() and not (tmp_path / USE_FILE).exists()
    assert read(tmp_path, SYSCTL_FILE).endswith("vm.max_map_count = 2147483642\n")
    assert read(tmp_path, MODULES_FILE).endswith("ntsync\n")
    script = read(tmp_path, FIRSTBOOT_SCRIPT)
    assert "flatpak remote-add --system --if-not-exists flathub" in script and "com.valvesoftware.Steam com.heroicgameslauncher.hgl" in script
    assert (tmp_path / FIRSTBOOT_SCRIPT).stat().st_mode & 0o111
    assert "exec /usr/local/sbin/gentoo-installer-gaming-firstboot" in read(tmp_path, FIRSTBOOT_LOCAL)
    assert not (tmp_path / FIRSTBOOT_UNIT).exists()
    emerge = next(line for line in runner.lines() if " emerge " in line)
    assert all(atom in emerge for atom in ("sys-apps/flatpak", "sys-apps/xdg-desktop-portal", "games-util/gamemode"))
    lines = runner.lines()
    assert f"chroot {tmp_path} rc-update add local default" in lines
    assert f"chroot {tmp_path} usermod --append --groups gamemode gentoo" in lines


def test_flatpak_mode_on_systemd_uses_a_unit(tmp_path):
    m, c, runner, _ = gaming(tmp_path, "systemd", gaming="flatpak")
    c.install_gaming(m)
    unit = read(tmp_path, FIRSTBOOT_UNIT)
    assert "After=network-online.target" in unit and "ConditionPathExists=!/var/lib/gentoo-installer/gaming-firstboot.done" in unit
    assert not (tmp_path / FIRSTBOOT_LOCAL).exists()
    assert f"chroot {tmp_path} systemctl enable gentoo-installer-gaming-firstboot.service" in runner.lines()


def test_native_mode_files(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "steam-overlay/profiles").mkdir(parents=True)
    (bundle / "steam-overlay/profiles/repo_name").write_text("steam-overlay\n")
    root = tmp_path / "root"
    root.mkdir()
    m, c, runner, _ = gaming(root, gaming="native", bundle=bundle)
    assert c.install_gaming(m) == []
    assert read(root, KEYWORD_FILE) == "games-util/steam-launcher ~amd64\ngames-util/esteam ~amd64\ngames-util/game-device-udev-rules ~amd64\n"
    assert read(root, LICENSE_FILE) == "games-util/steam-launcher ValveSteamLicense\n"
    use = read(root, USE_FILE)
    assert "media-libs/vulkan-loader abi_x86_32\n" in use and "media-libs/mesa abi_x86_32 vulkan\n" in use and "media-video/pipewire abi_x86_32\n" in use
    assert (root / "var/db/repos/steam-overlay/profiles/repo_name").is_file()
    assert "masters = gentoo" in read(root, "etc/portage/repos.conf/steam-overlay.conf")
    assert not (root / FIRSTBOOT_SCRIPT).exists()
    emerge = next(line for line in runner.lines() if " emerge " in line)
    assert "games-util/steam-launcher" in emerge and "games-util/esteam" in emerge and "gnome-extra/zenity" in emerge


def test_native_mode_turns_32_bit_on_for_everything_and_covers_nvidia(tmp_path):
    m, c, _, _ = gaming(tmp_path, gaming="native")
    use = dict(c.gaming_portage_files(m))[USE_FILE]
    assert use.splitlines()[0] == "*/* abi_x86_32"
    for atom in ("x11-drivers/nvidia-drivers", "gui-libs/egl-gbm", "gui-libs/egl-wayland", "gui-libs/egl-x11", "media-libs/vulkan-layers"):
        assert f"{atom} abi_x86_32\n" in use
    m, c, _, _ = gaming(tmp_path, gaming="flatpak")
    assert USE_FILE not in dict(c.gaming_portage_files(m))


def test_native_mode_without_pipewire_has_no_pipewire_flag(tmp_path):
    m, c, _, _ = gaming(tmp_path, gaming="native", pipewire=False)
    assert "pipewire" not in dict(c.gaming_portage_files(m))[USE_FILE]


def test_tweaks_off_means_no_sysctl_or_modules_file_and_no_gamemode(tmp_path):
    m, c, runner, _ = gaming(tmp_path, gaming="flatpak", gaming_tweaks=False)
    c.install_gaming(m)
    assert not (tmp_path / SYSCTL_FILE).exists() and not (tmp_path / MODULES_FILE).exists()
    assert "games-util/gamemode" not in c.gaming_atoms(m) and not any("gamemode" in line for line in runner.lines())


def test_a_manual_kernel_gets_no_ntsync_line(tmp_path):
    m, c, _, _ = gaming(tmp_path, gaming="flatpak")
    m.system.kernel = "manual"
    c.install_gaming(m)
    assert (tmp_path / SYSCTL_FILE).exists() and not (tmp_path / MODULES_FILE).exists()


def test_windows_tools_are_native_packages_only_in_native_mode(tmp_path):
    m, c, _, _ = gaming(tmp_path, gaming="native", gaming_windows_tools=True)
    atoms = c.gaming_atoms(m)
    assert {"games-util/heroic-bin", "app-emulation/wine-vanilla", "app-emulation/dxvk", "app-emulation/vkd3d-proton", "app-emulation/protontricks"} <= set(atoms)
    assert "games-util/heroic-bin ~amd64\n" in dict(c.gaming_portage_files(m))[KEYWORD_FILE]
    m, c, _, _ = gaming(tmp_path, gaming="flatpak", gaming_windows_tools=True)
    assert "games-util/heroic-bin" not in c.gaming_atoms(m)


def test_native_without_the_overlay_is_a_clear_warning_not_a_crash(tmp_path):
    m, c, runner, _ = gaming(tmp_path, gaming="native", bundle=tmp_path / "missing")
    runner.script("*eselect repository", ("fail", "no network"))
    warnings = c.install_gaming(m)
    assert len(warnings) == 1 and "steam-overlay" in warnings[0] and "Flatpak" in warnings[0]
    assert not (tmp_path / USE_FILE).exists()


def test_failed_packages_are_warnings(tmp_path):
    m, c, runner, _ = gaming(tmp_path, gaming="flatpak")
    runner.script("*emerge", ("fail", "!!! build failed"), ("fail", "!!! build failed"))
    warnings = c.install_gaming(m)
    assert warnings and "could not be installed" in warnings[0]


def test_a_dry_run_writes_nothing(tmp_path):
    m, _, _, _ = gaming(tmp_path, gaming="flatpak")
    c = configurer(tmp_path, ScriptedRunner(), dry_run=True)
    c.notify = lambda message: None
    before = tree(tmp_path)
    c.install_gaming(m)
    assert tree(tmp_path) == before


def test_native_needs_the_accept_box():
    m = target_manifest(options={"gaming": "native"})
    assert any("accepts this" in p for p in choice_problems(m))
    m.options.gaming_accept_external = True
    assert choice_problems(m) == []
    assert choice_problems(target_manifest(options={"gaming": "flatpak"})) == []


def test_manifest_rules_for_gaming_still_hold():
    assert any("needs a desktop" in p for p in target_manifest(system={"desktop": "minimal"}, options={"gaming": "flatpak"}).validate())
    assert any("32-bit" in p for p in target_manifest(system={"multilib": False}, options={"gaming": "native"}).validate())


# --- the readiness list ----------------------------------------------------------------------------


def fake_pc(root: Path, *, uefi=True, secure_boot=True, tpm="2", iommu=True, lockdown="none [integrity] confidentiality", lsm="lockdown,yama,bpf", kexec="1"):
    def put(path: str, data):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode())

    if uefi:
        put(gr.EFI_SECURE_BOOT, bytes([6, 0, 0, 0, 1 if secure_boot else 0]))
    if tpm:
        put("sys/class/tpm/tpm0/tpm_version_major", tpm + "\n")
    if iommu:
        put("sys/class/iommu/dmar0/x", "")
    if lockdown:
        put("sys/kernel/security/lockdown", lockdown + "\n")
    if lsm:
        put("sys/kernel/security/lsm", lsm + "\n")
    if kexec:
        put("proc/sys/kernel/kexec_load_disabled", kexec + "\n")


def states(rows):
    return {row.name: row.state for row in rows}


def test_a_pc_that_meets_everything_checkable(tmp_path):
    fake_pc(tmp_path)
    got = states(gr.check_this_pc(tmp_path, live=False))
    assert got == {"Secure Boot": "yes", "TPM 2.0": "yes", "IOMMU": "yes", "Measured boot": "unknown", "Kernel lockdown": "yes",
                   "BPF security module": "yes", "kexec": "yes", "Signed kernel and modules": "unknown"}


def test_a_bare_pc_has_no_exceptions_and_says_what_is_missing(tmp_path):
    rows = gr.check_this_pc(tmp_path, live=False)  # an empty tree: BIOS boot, no TPM, no security files
    got = states(rows)
    assert got["Secure Boot"] == "no" and "BIOS mode" in next(r for r in rows if r.name == "Secure Boot").detail
    assert got["TPM 2.0"] == "no" and got["IOMMU"] == "no" and got["Kernel lockdown"] == "unknown" and got["kexec"] == "unknown"


def test_individual_failures(tmp_path):
    fake_pc(tmp_path, secure_boot=False, tpm="1", lockdown="[none] integrity confidentiality", lsm="lockdown,yama", kexec="0")
    got = states(gr.check_this_pc(tmp_path, live=False))
    assert got["Secure Boot"] == "no" and got["TPM 2.0"] == "no" and got["Kernel lockdown"] == "no" and got["BPF security module"] == "no" and got["kexec"] == "no"


def test_live_session_kernel_rows_are_decided_after_the_installation(tmp_path):
    fake_pc(tmp_path)
    got = states(gr.check_this_pc(tmp_path, live=True))
    assert got["Kernel lockdown"] == got["BPF security module"] == got["kexec"] == "later"
    assert got["Secure Boot"] == "yes" and got["TPM 2.0"] == "yes"


def test_the_check_writes_nothing(tmp_path):
    fake_pc(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    gr.check_this_pc(tmp_path)
    assert before == {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}


def texts():
    return [value for name, value in vars(gr).items() if isinstance(value, str) and name.isupper()] + list(gr.FORTNITE_REQUIREMENTS)


def test_the_status_is_dated_honest_and_complete():
    status = gr.FORTNITE_STATUS
    assert "24 October 2026" in status and "not playable on Linux" in status
    assert "Secondary sources report" in status and "inference" in status and gr.EPIC_URL in status
    assert len(gr.FORTNITE_REQUIREMENTS) == 8
    for word in ("signed kernel", "Secure Boot", "TPM 2.0", "measured boot", "IOMMU", "lockdown", "kexec", "BPF"):
        assert word.lower() in status.lower(), word
    assert "does not make Fortnite work" in status


def test_no_text_promises_that_fortnite_is_ready_or_supported():
    for text in texts() + [row.line() for row in gr.check_this_pc("/nonexistent")]:
        assert not re.search(r"\bready\b|\bsupported\b|\bcompatible\b", text, re.I), text
        assert "not a ban" not in text and "error, not" not in text
