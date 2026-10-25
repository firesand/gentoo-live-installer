"""ACCT: the optional gaming setup (options.gaming off | flatpak | native).

Off is the default and writes nothing: a default installation is byte-identical to one without this module.
The 32-bit USE flags of the graphics drivers belong to the hardware plan; the files here cover the other libraries.
"""

from __future__ import annotations

import shutil

from gentoo_installer.manifest import InstallManifest

from .configure_base import ConfigurationError
from .configure_packages import PackagesMixin
from .configure_portage import render_package_use
from .runner import RunnerError

OVERLAY_NAME = "steam-overlay"
OVERLAY_SNAPSHOT = "steam-overlay"  # a directory of the live medium's bundle (companion_bundle/steam-overlay), pinned by the image build
KEYWORD_FILE = "etc/portage/package.accept_keywords/installer-gaming"
LICENSE_FILE = "etc/portage/package.license/installer-gaming"
USE_FILE = "etc/portage/package.use/installer-gaming"
SYSCTL_FILE = "etc/sysctl.d/99-gaming.conf"
MODULES_FILE = "etc/modules-load.d/ntsync.conf"
FIRSTBOOT_SCRIPT = "usr/local/sbin/gentoo-installer-gaming-firstboot"
FIRSTBOOT_UNIT = "etc/systemd/system/gentoo-installer-gaming-firstboot.service"
FIRSTBOOT_LOCAL = "etc/local.d/50-gentoo-installer-gaming.start"
FIRSTBOOT_DONE = "/var/lib/gentoo-installer/gaming-firstboot.done"
FLATPAK_APPS = ("com.valvesoftware.Steam", "com.heroicgameslauncher.hgl")

COMMON_ATOMS = ("games-util/game-device-udev-rules", "sys-apps/xdg-desktop-portal")
FLATPAK_ATOMS = ("sys-apps/flatpak", *COMMON_ATOMS)
NATIVE_ATOMS = ("games-util/steam-launcher", "games-util/esteam", "gnome-extra/zenity", *COMMON_ATOMS)
TWEAK_ATOMS = ("games-util/gamemode",)
WINDOWS_ATOMS = ("games-util/heroic-bin", "app-emulation/wine-vanilla", "app-emulation/dxvk", "app-emulation/vkd3d-proton", "app-emulation/protontricks")
# Only these are testing (~amd64) in ::gentoo or the overlay; wine, dxvk, vkd3d-proton, gamemode and protontricks have stable versions.
TESTING_ATOMS = ("games-util/steam-launcher", "games-util/esteam", "games-util/game-device-udev-rules", "games-util/heroic-bin")
# The non-GPU libraries Steam needs in 32-bit form; mesa and the NVIDIA stack are added by the hardware plan (a repeat is harmless).
NATIVE_USE = {
    "media-libs/mesa": ["abi_x86_32", "vulkan"],
    "media-libs/vulkan-loader": ["abi_x86_32"],
    "media-libs/libglvnd": ["abi_x86_32"],
    "media-libs/libpulse": ["abi_x86_32"],
    # The 32-bit NVIDIA userland (harmless lines when the GPU is not NVIDIA: package.use for an absent package is ignored).
    "x11-drivers/nvidia-drivers": ["abi_x86_32"],
    "gui-libs/egl-gbm": ["abi_x86_32"],
    "gui-libs/egl-wayland": ["abi_x86_32"],
    "gui-libs/egl-x11": ["abi_x86_32"],
    "media-libs/vulkan-layers": ["abi_x86_32"],
}
# Steam's RDEPEND and mesa[abi_x86_32] pull a long closure (libdrm, fontconfig, expat, zlib, llvm ...) in 32-bit form, and Portage
# never turns abi_x86_32 on by itself, so native mode enables it for every package (the Gentoo wiki's multilib approach).
GLOBAL_ABI_LINE = "*/* abi_x86_32\n"

SYSCTL_TEXT = "# Written by the Gentoo LiveCD installer: many games need a large number of memory maps.\nvm.max_map_count = 2147483642\n"
MODULES_TEXT = "# Written by the Gentoo LiveCD installer: NTSYNC makes Windows games faster under Wine and Proton.\nntsync\n"


def firstboot_script(apps: tuple[str, ...] = FLATPAK_APPS) -> str:
    """Add Flathub and install the apps; the marker file stops it from running again once it worked."""
    return (
        "#!/bin/sh\n"
        "# Written by the Gentoo LiveCD installer. Runs at start-up until it succeeds once (it needs the network).\n"
        f'[ -e {FIRSTBOOT_DONE} ] && exit 0\n'
        "flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo || exit 1\n"
        f"flatpak install --system --noninteractive -y flathub {' '.join(apps)} || exit 1\n"
        f"mkdir -p {FIRSTBOOT_DONE.rsplit('/', 1)[0]} && touch {FIRSTBOOT_DONE}\n"
        "rm -f /etc/local.d/50-gentoo-installer-gaming.start\n"
    )


FIRSTBOOT_UNIT_TEXT = (
    "[Unit]\nDescription=Add Flathub and install the gaming apps (once)\n"
    f"ConditionPathExists=!{FIRSTBOOT_DONE}\nWants=network-online.target\nAfter=network-online.target\n\n"
    f"[Service]\nType=oneshot\nExecStart=/{FIRSTBOOT_SCRIPT}\nRestart=on-failure\nRestartSec=60\n\n[Install]\nWantedBy=multi-user.target\n"
)
FIRSTBOOT_LOCAL_TEXT = "#!/bin/sh\n# Written by the Gentoo LiveCD installer.\nexec /" + FIRSTBOOT_SCRIPT + "\n"


class GamingMixin(PackagesMixin):
    def gaming_atoms(self, manifest: InstallManifest) -> list[str]:
        """The packages of the chosen gaming mode ([] when off)."""
        options = manifest.options
        if options.gaming == "off":
            return []
        atoms = list(FLATPAK_ATOMS if options.gaming == "flatpak" else NATIVE_ATOMS)
        if options.gaming_tweaks:
            atoms.extend(TWEAK_ATOMS)
        if options.gaming_windows_tools and options.gaming == "native":
            atoms.extend(WINDOWS_ATOMS)  # in Flatpak mode Heroic comes as a Flatpak instead
        return list(dict.fromkeys(atoms))

    def gaming_portage_files(self, manifest: InstallManifest) -> list[tuple[str, str]]:
        """(path, content) of the Portage files for this mode; nothing when gaming is off."""
        options = manifest.options
        if options.gaming == "off":
            return []
        atoms = self.gaming_atoms(manifest)
        keywords = [f"{atom} ~amd64\n" for atom in TESTING_ATOMS if atom in atoms]
        files = [(KEYWORD_FILE, "".join(keywords))] if keywords else []
        if options.gaming == "native":
            files.append((LICENSE_FILE, "games-util/steam-launcher ValveSteamLicense\n"))
            use = dict(NATIVE_USE)
            if options.pipewire:
                use["media-video/pipewire"] = ["abi_x86_32"]
            files.append((USE_FILE, GLOBAL_ABI_LINE + render_package_use(use)))
        return files

    def gaming_system_files(self, manifest: InstallManifest) -> list[tuple[str, str, int]]:
        """The tweaks and the first-boot Flathub step, as (path, content, mode)."""
        system, options = manifest.system, manifest.options
        if options.gaming == "off":
            return []
        files: list[tuple[str, str, int]] = []
        if options.gaming_tweaks:
            files.append((SYSCTL_FILE, SYSCTL_TEXT, 0o644))
            if system.kernel in {"gentoo-kernel", "gentoo-kernel-bin"}:  # a manual or custom kernel may not have NTSYNC
                files.append((MODULES_FILE, MODULES_TEXT, 0o644))
        if options.gaming == "flatpak":
            files.append((FIRSTBOOT_SCRIPT, firstboot_script(), 0o755))
            if system.init == "systemd":
                files.append((FIRSTBOOT_UNIT, FIRSTBOOT_UNIT_TEXT, 0o644))
            else:
                files.append((FIRSTBOOT_LOCAL, FIRSTBOOT_LOCAL_TEXT, 0o755))
        return files

    def prepare_steam_overlay(self, manifest: InstallManifest) -> None:
        """Native mode: the pinned steam-overlay snapshot from the live medium, else the network (eselect repository)."""
        if self.dry_run:
            return
        snapshot = self.companion_bundle / OVERLAY_SNAPSHOT
        if (snapshot / "profiles/repo_name").is_file():
            shutil.copytree(snapshot, self.path(f"var/db/repos/{OVERLAY_NAME}"), dirs_exist_ok=True, symlinks=False)
            self.write(f"etc/portage/repos.conf/{OVERLAY_NAME}.conf",
                       f"[{OVERLAY_NAME}]\nlocation = /var/db/repos/{OVERLAY_NAME}\nmasters = gentoo\nauto-sync = no\n")
            return
        self.notify("The steam-overlay snapshot is not on this medium; trying to fetch the repository from the network")
        try:
            self.emerge(manifest, ("--noreplace", "app-eselect/eselect-repository", "dev-vcs/git"))
            self.chroot(("eselect", "repository", "enable", OVERLAY_NAME))
            self.chroot(("emerge", "--sync", OVERLAY_NAME))
        except RunnerError as exc:
            raise ConfigurationError("Native gaming needs the steam-overlay repository: it is not on the live medium and could not be fetched "
                                     f"from the network ({str(exc)[:120]}). Choose Flatpak gaming, or connect to the network and try again.") from exc

    def install_gaming(self, manifest: InstallManifest, plan: object | None = None) -> list[str]:
        """Install the gaming setup after the desktop. Returns warnings; a failure here never stops a bootable system."""
        options = manifest.options
        if options.gaming == "off":
            return []
        warnings: list[str] = []
        try:
            if options.gaming == "native":
                self.prepare_steam_overlay(manifest)
        except ConfigurationError as exc:
            return [str(exc)]
        for path, content in self.gaming_portage_files(manifest):
            self.write(path, content)
        atoms = self.gaming_atoms(manifest)
        outcome = self.emerge_keep_going(manifest, atoms, label="Gaming", attempts=2)
        for atom in outcome.missing:
            warnings.append(f"{atom} (gaming) could not be installed: {outcome.reason}"[:300])
        for path, content, mode in self.gaming_system_files(manifest):
            self.write(path, content, mode)
        if options.gaming == "flatpak" and manifest.system.init == "systemd":
            self.chroot_step(("systemctl", "enable", "gentoo-installer-gaming-firstboot.service"), warnings)
        elif options.gaming == "flatpak":
            self.chroot_step(("rc-update", "add", "local", "default"), warnings)
        if options.gaming_tweaks and "games-util/gamemode" not in outcome.missing:
            self.chroot_step(("usermod", "--append", "--groups", "gamemode", manifest.accounts.username), warnings)
        return warnings

    def chroot_step(self, argv: tuple[str, ...], warnings: list[str]) -> None:
        try:
            self.chroot(argv)
        except RunnerError as exc:
            warnings.append(f"{' '.join(argv)} failed ({str(exc)[:120]})")
