"""PORT: installing the desktop set (packages, USE flags, services and session files for KDE, GNOME and Xfce)."""

from __future__ import annotations

from dataclasses import dataclass, field

from gentoo_installer.manifest import InstallManifest

from .configure_base import _plan_list
from .configure_packages import PackageInstallError, PackagesMixin
from .configure_portage import COMPANION_ATOMS
from .configure_system import _keyboard_is_default
from .runner import RunnerError

# Installed before the desktop set so that the desktop's own choice of a power-profiles provider finds it.
PREFERRED_FIRST = "sys-power/tlp"


# --- package sets ---------------------------------------------------------------------------------


@dataclass
class DesktopAtoms:
    required: list[str] = field(default_factory=list)  # desktop environment, display manager, X server
    optional: list[str] = field(default_factory=list)  # extras: audio, Bluetooth, printing, companions, ...
    extras: list[str] = field(default_factory=list)  # packages typed in by the user, installed one by one
    groups: list[tuple[str, list[str]]] = field(default_factory=list)  # hardware plan groups

    def everything(self) -> list[str]:
        merged = [*self.required, *self.optional, *(atom for _, atoms in self.groups for atom in atoms), *self.extras]
        return list(dict.fromkeys(merged))


@dataclass
class DesktopResult:
    degraded: list[str] = field(default_factory=list)  # messages for packages that could not be installed
    failed: list[str] = field(default_factory=list)


class DesktopMixin(PackagesMixin):
    def desktop_atoms(self, manifest: InstallManifest, plan: object | None = None) -> DesktopAtoms:
        system, options = manifest.system, manifest.options
        plan = self._plan(plan)
        graphical = system.desktop != "minimal"
        result = DesktopAtoms()
        if system.desktop == "kde":
            result.required.extend((
                "kde-plasma/plasma-meta", "kde-apps/kde-apps-meta" if options.kde_apps == "full" else "kde-apps/kdecore-meta",
                "gui-apps/sddm", "x11-base/xorg-server"))
        elif system.desktop == "gnome":
            result.required.extend(("gnome-base/gnome", "gnome-base/gdm", "x11-base/xorg-server"))
        elif system.desktop == "xfce":
            # The panel's polkit agent is not pulled in on every profile.
            result.required.extend(("xfce-base/xfce4-meta", "x11-base/xorg-server", "x11-misc/lightdm",
                                    "x11-misc/lightdm-gtk-greeter", "gnome-extra/polkit-gnome"))
        if graphical and system.init == "openrc":
            result.required.append("gui-libs/display-manager-init")
        result.required.extend(str(atom) for atom in _plan_list(plan, "required_atoms"))
        if options.pipewire:
            result.optional.append("media-video/pipewire")
        if options.bluetooth:
            result.optional.append("net-wireless/bluez")
            if graphical:
                result.optional.append("net-wireless/blueman")  # the GUI needs a toolkit and X
        if options.printing:
            result.optional.append("net-print/cups")
        if options.flatpak:
            result.optional.append("sys-apps/flatpak")
        if graphical:
            result.optional.extend(("sys-fs/exfatprogs", "sys-fs/ntfs3g"))
        if options.install_hwmonux:
            result.optional.append("app-misc/hwmonux")
        if options.install_portagehub:
            result.optional.append("app-portage/portagehub")
        for group in _plan_list(plan, "optional_atoms"):
            if isinstance(group, str):
                result.groups.append((group, [group]))
            else:
                label, atoms = group
                result.groups.append((str(label), [str(atom) for atom in atoms]))
        result.extras.extend(options.extra_packages)
        result.required[:] = list(dict.fromkeys(result.required))
        result.optional[:] = [atom for atom in dict.fromkeys(result.optional) if atom not in result.required]
        return result

    def install_desktop(self, manifest: InstallManifest, plan: object | None = None) -> DesktopResult:
        """Desktop environment first (must succeed), then every optional package on its own terms."""
        plan = self._plan(plan)
        atoms = self.desktop_atoms(manifest, plan)
        result = DesktopResult()
        problem = self.prepare_companions(manifest)
        if problem:
            result.degraded.append("The companion applications were skipped: " + problem)
            atoms.optional = [atom for atom in atoms.optional if atom not in COMPANION_ATOMS]
        if manifest.system.world_update == "now":
            try:
                self.emerge(manifest, ("--update", "--deep", "--newuse", "--keep-going", "@world"))
            except RunnerError as exc:
                result.degraded.append("The @world update did not finish: " + self.explain(exc).summary[:200])
        # TLP goes first: KDE and GNOME need a power-profiles provider, and when TLP is not there yet they
        # pull in power-profiles-daemon, which TLP then blocks. With TLP installed first the desktop is satisfied by it.
        first = [(label, group) for label, group in atoms.groups if PREFERRED_FIRST in group]
        for label, group in first:
            outcome = self.emerge_keep_going(manifest, group, label=label, attempts=2)
            for atom in outcome.missing:
                result.failed.append(atom)
                result.degraded.append(f"{atom} ({label}) could not be installed: {outcome.reason}"[:300])
        atoms.groups = [item for item in atoms.groups if item not in first]
        if atoms.required:
            outcome = self.emerge_keep_going(manifest, atoms.required, label="Desktop")
            if outcome.missing:
                raise PackageInstallError("The desktop environment could not be installed completely (" + ", ".join(outcome.missing) + "): " + outcome.reason,
                                          failed=outcome.missing, resolution=outcome.kind == "resolution")
        if atoms.optional:
            outcome = self.emerge_keep_going(manifest, list(atoms.optional), label="Optional packages", attempts=2)
            # An unresolvable group installed nothing: try its packages one by one so a bad one cannot take
            # the others down. After build or download failures the missing ones are the broken ones.
            for atom in outcome.missing:
                single = self.emerge_keep_going(manifest, [atom], label=atom, attempts=1) if outcome.kind == "resolution" else outcome
                if single.missing:
                    result.failed.append(atom)
                    result.degraded.append(f"{atom} could not be installed: {single.reason or outcome.reason}"[:300])
        for label, group in atoms.groups:
            outcome = self.emerge_keep_going(manifest, group, label=label, attempts=2)
            for atom in outcome.missing:
                result.failed.append(atom)
                result.degraded.append(f"{atom} ({label}) could not be installed: {outcome.reason}"[:300])
        for atom in atoms.extras:
            outcome = self.emerge_keep_going(manifest, [atom], label=atom, attempts=1)
            if outcome.missing:
                result.failed.append(atom)
                result.degraded.append(f"{atom} could not be installed: {outcome.reason}"[:300])
        result.degraded.extend(problem[:300] for problem in self.restore_cycle_breaks(manifest))
        return result

    def configure_desktop_session(self, manifest: InstallManifest) -> None:
        """Files and steps that need the desktop packages: polkit agent autostart, dconf database."""
        system = manifest.system
        if system.desktop == "xfce":
            # polkit-gnome's own entry only starts under GNOME; PortageHub and settings tools need an agent.
            self.write("etc/xdg/autostart/gentoo-installer-polkit-agent.desktop",
                       "[Desktop Entry]\nType=Application\nName=PolicyKit Authentication Agent\n"
                       "Exec=/usr/libexec/polkit-gnome-authentication-agent-1\nOnlyShowIn=XFCE;\nNoDisplay=true\n")
        if system.desktop == "gnome" and not _keyboard_is_default(system):
            try:
                self.chroot(("dconf", "update"))
            except RunnerError:
                self.notify("warning: dconf update failed; the keyboard layout may need to be chosen again in GNOME settings")
