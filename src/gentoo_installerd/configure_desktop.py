"""PORT: installing the desktop set (packages, USE flags, services and session files for KDE, GNOME and Xfce)."""

from __future__ import annotations

from dataclasses import dataclass, field

from gentoo_installer import desktops
from gentoo_installer.manifest import COMPANION_APPS, InstallManifest
from gentoo_installer.world import should_depclean, should_update_world

from .configure_base import _plan_list
from .configure_packages import PackageInstallError, PackagesMixin
from .configure_portage import COMPANION_ATOMS
from .configure_system import _keyboard_is_default
from .runner import RunnerError

FLATHUB_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"
I3_MARKER = "# --- Gentoo installer"
POLKIT_AGENT = "/usr/libexec/polkit-mate-authentication-agent-1"

# Installed before the desktop set so that the desktop's own choice of a power-profiles provider finds it.
PREFERRED_FIRST = "sys-power/tlp"


def render_sway_snippet(manifest: InstallManifest) -> str:
    """/etc/sway/config.d/50-gentoo-installer.conf: sway's default configuration includes config.d/*."""
    system, options = manifest.system, manifest.options
    lines = ["# Written by the Gentoo installer. Copy /etc/sway/config to ~/.config/sway/config to change anything.",
             "# This snippet starts the helpers a tiling session does not start by itself.",
             f"exec {POLKIT_AGENT}", "exec mako", "bar bar-0 {", "    swaybar_command waybar", "}"]
    if manifest.network.manager == "networkmanager":
        lines.append("exec nm-applet --indicator")
    if options.pipewire and system.init == "openrc":
        lines.append("exec gentoo-pipewire-launcher restart")
    lines.append("exec swayidle -w timeout 300 'swaylock -f' before-sleep 'swaylock -f'")
    if not _keyboard_is_default(system):
        lines += ["input type:keyboard {", f"    xkb_layout {system.keyboard_layout}"]
        if system.keyboard_variant:
            lines.append(f"    xkb_variant {system.keyboard_variant}")
        if system.keyboard_options:
            lines.append(f"    xkb_options {system.keyboard_options}")
        lines.append("}")
    return "\n".join(lines) + "\n"


def render_i3_snippet(manifest: InstallManifest) -> str:
    system, options = manifest.system, manifest.options
    lines = [f"{I3_MARKER} (added after the packages were installed) ---", f"exec --no-startup-id {POLKIT_AGENT}"]
    if manifest.network.manager == "networkmanager":
        lines.append("exec --no-startup-id nm-applet")
    if options.pipewire and system.init == "openrc":
        lines.append("exec --no-startup-id gentoo-pipewire-launcher restart")
    lines += ["exec --no-startup-id picom -b", "exec --no-startup-id dunst",
              "exec --no-startup-id xss-lock --transfer-sleep-lock -- i3lock --nofork"]
    return "\n".join(lines) + "\n"


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
        if graphical:
            result.required.extend(desktops.required_atoms(system.desktop, system.init, options.kde_apps, manifest.network.manager))
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
        if graphical and options.browser == "firefox":
            result.optional.append(desktops.FIREFOX)  # the installer falls back to the binary build when this one fails
        if desktops.flatpak_wanted(options):
            result.optional.append(desktops.FLATPAK)
            if graphical:
                result.optional.extend(desktops.portal_atoms(system.desktop))
        if graphical:
            result.optional.extend(("sys-fs/exfatprogs", "sys-fs/ntfs3g"))
        result.optional.extend(atom for field_name, _, atom in COMPANION_APPS if getattr(options, field_name))
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

    def choose_browser(self, manifest: InstallManifest, atoms: DesktopAtoms) -> None:
        """Firefox from the binary package host when it has one; otherwise Mozilla's firefox-bin (no hours of compiling).

        The source build of Firefox takes hours and a lot of memory. Without a binary package for the exact build
        (checked with emerge --pretend --usepkgonly) the binary build from Mozilla is installed instead.
        """
        if desktops.FIREFOX not in atoms.optional:
            return
        reason = ""
        if not manifest.portage.binhost:
            reason = "this installation builds packages from source"
        else:
            try:
                self.emerge(manifest, ("--pretend", "--getbinpkg", "--usepkgonly", desktops.FIREFOX), capture=True)
            except RunnerError:
                reason = "the binary package host has no matching Firefox package"
        if reason:
            self.notify(f"Firefox: {reason}, so Mozilla's binary build ({desktops.FIREFOX_BIN}) is installed; compiling Firefox would take hours.")
            atoms.optional[:] = [desktops.FIREFOX_BIN if atom == desktops.FIREFOX else atom for atom in atoms.optional]

    def install_desktop(self, manifest: InstallManifest, plan: object | None = None) -> DesktopResult:
        """Desktop environment first (must succeed), then every optional package on its own terms."""
        plan = self._plan(plan)
        atoms = self.desktop_atoms(manifest, plan)
        result = DesktopResult()
        result.degraded.extend(self.prepare_plan_repos(manifest, plan))  # the hardware plan's extra software sources (overlays), before their packages
        problem = self.prepare_companions(manifest)
        if problem:
            result.degraded.append("The companion applications were skipped: " + problem)
            atoms.optional = [atom for atom in atoms.optional if atom not in COMPANION_ATOMS]
        world_updated = False
        if should_update_world(manifest):
            try:
                self.emerge(manifest, ("--update", "--deep", "--newuse", "--keep-going", "@world"))
                world_updated = True
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
        self.choose_browser(manifest, atoms)
        if atoms.optional:
            outcome = self.emerge_keep_going(manifest, list(atoms.optional), label="Optional packages", attempts=2)
            # An unresolvable group installed nothing: try its packages one by one so a bad one cannot take
            # the others down. After build or download failures the missing ones are the broken ones.
            for atom in outcome.missing:
                single = self.emerge_keep_going(manifest, [atom], label=atom, attempts=1) if outcome.kind == "resolution" else outcome
                if single.missing and atom == desktops.FIREFOX:
                    # Mozilla's own binary build needs no compiler; better a working browser than none.
                    self.notify("warning: Firefox could not be installed; trying firefox-bin (Mozilla's binary build) instead")
                    single = self.emerge_keep_going(manifest, [desktops.FIREFOX_BIN], label=desktops.FIREFOX_BIN, attempts=1)
                    atom = desktops.FIREFOX_BIN if single.missing else ""
                if atom and single.missing:
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
        if manifest.portage.depclean and not world_updated:
            result.degraded.append("Removing programs that are no longer needed was skipped: it runs only after the whole system was updated during the installation.")
        if should_depclean(manifest, world_updated):
            # Last, when everything the installer wants is in place, so that nothing it still needs is removed.
            try:
                self.emerge(manifest, ("--depclean",))
            except RunnerError as exc:
                result.degraded.append("Removing programs that are no longer needed did not finish: " + self.explain(exc).summary[:200])
        return result

    def configure_desktop_session(self, manifest: InstallManifest) -> None:
        """Files and steps that need the desktop packages: polkit agent autostart, dconf database."""
        system = manifest.system
        if system.desktop == "xfce":
            # polkit-gnome's own entry only starts under GNOME; PortageHub and settings tools need an agent.
            self.write("etc/xdg/autostart/gentoo-installer-polkit-agent.desktop",
                       "[Desktop Entry]\nType=Application\nName=PolicyKit Authentication Agent\n"
                       "Exec=/usr/libexec/polkit-gnome-authentication-agent-1\nOnlyShowIn=XFCE;\nNoDisplay=true\n")
        if system.desktop == "sway":
            self.write("etc/sway/config.d/50-gentoo-installer.conf", render_sway_snippet(manifest))
        elif system.desktop == "i3":
            self.append_i3_snippet(manifest)
        if manifest.options.flathub:
            self.add_flathub()
        entry = desktops.get(system.desktop)
        if entry is not None and entry.keyboard == "dconf" and not _keyboard_is_default(system):
            try:
                self.chroot(("dconf", "update"))
            except RunnerError:
                self.notify("warning: dconf update failed; the keyboard layout may need to be chosen again in GNOME settings")

    def add_flathub(self) -> None:
        """The Flathub remote, system-wide, so that the first Flatpak install after the first boot just works."""
        try:
            self.chroot(("flatpak", "remote-add", "--system", "--if-not-exists", "flathub", FLATHUB_REPO))
        except RunnerError:
            self.notify("warning: the Flathub remote could not be added (no network?). After the first boot run: "
                        f"flatpak remote-add --system --if-not-exists flathub {FLATHUB_REPO}")

    def append_i3_snippet(self, manifest: InstallManifest) -> None:
        """Start the network icon, the polkit agent and (OpenRC) the audio session from i3's own configuration.

        The accounts exist before the desktop packages, so /etc/skel is too late; i3 reads /etc/i3/config for every user that has
        no configuration of their own, so the lines go there, once.
        """
        path = "etc/i3/config"
        current = self.read_text(path)
        if self.dry_run:
            return
        if not current:
            self.notify("warning: /etc/i3/config was not found, so the network icon and the polkit agent are not started with i3")
            return
        if I3_MARKER in current:
            return
        self.write(path, current.rstrip("\n") + "\n\n" + render_i3_snippet(manifest))
