"""CORE: the desktop registry, the single source of desktop facts.

Every consumer reads this table: the manifest (validation, the Portage profile), the System page and the text installer
(the cards), the daemon (packages, USE flags, display manager, services, session files), the hardware planner (power
profiles, Wayland, memory advice) and the disk-space checks. Adding a desktop is one entry here, plus the cells of the
resolve gate and the plan lint for it (docs/protocol.md). Nothing in this module imports another installer module, so
the manifest can import it.

Sizes are the measured closure of the default package set where the project measured it (KDE Plasma, GNOME, Xfce) and
estimates from the Portage simulations for the others. Package atoms must be stable ~amd64-free in the pinned tree
(tests/test_desktops.py checks that when GENTOO_PINNED_TREE points at the tree).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# The package that supplies the X server, and the polkit agent that Xfce, i3 and Sway sessions start themselves.
XORG = "x11-base/xorg-server"
SDDM, GDM, LIGHTDM = "gui-apps/sddm", "gnome-base/gdm", "x11-misc/lightdm"
LIGHTDM_GREETER = "x11-misc/lightdm-gtk-greeter"
NM_APPLET = "gnome-extra/nm-applet"
DISPLAY_MANAGER_INIT = "gui-libs/display-manager-init"
FIREFOX, FIREFOX_BIN = "www-client/firefox", "www-client/firefox-bin"
FLATPAK = "sys-apps/flatpak"
KDE_APPS_CORE, KDE_APPS_FULL = "kde-apps/kdecore-meta", "kde-apps/kde-apps-meta"

# Display manager key -> (atoms installed with it, OpenRC DISPLAYMANAGER name, systemd unit).
DISPLAY_MANAGERS: Mapping[str, tuple[tuple[str, ...], str, str]] = {
    "sddm": ((SDDM,), "sddm", "sddm"),
    "gdm": ((GDM,), "gdm", "gdm"),
    "lightdm": ((LIGHTDM, LIGHTDM_GREETER), "lightdm", "lightdm"),
}

FAMILIES = ("standard", "desktop", "nomultilib", "hardened")
UNVERIFIED_FAMILIES = frozenset({"nomultilib", "hardened"})
FAMILY_LABELS = {"standard": "Standard", "desktop": "Desktop", "nomultilib": "No multilib", "hardened": "Hardened"}
FAMILY_PREFIX = {"standard": "amd64", "desktop": "amd64-desktop", "nomultilib": "amd64-nomultilib", "hardened": "amd64-hardened"}

KDE_NEEDS_DESKTOP_STAGE = (
    "KDE Plasma needs a Standard or Desktop stage3: the Hardened and No-multilib profiles do not enable the desktop USE flags Plasma requires, "
    "so the package step would fail after your disk was erased. Choose a Standard stage3 or the Minimal system."
)
CINNAMON_NEEDS_GNOME_PROFILE = (
    "Cinnamon needs the desktop/gnome Portage profile, which the Hardened and No-multilib stage3 families do not have "
    "(without it the web engine would be compiled for hours). Choose a Standard or Desktop stage3, or another desktop."
)


@dataclass(frozen=True)
class Desktop:
    key: str
    label: str
    tier: str  # "guided" (a card in Guided mode), "more" (under "More desktops"), "none" (no desktop)
    blurb: str
    profile_suffix: str = ""  # appended to the 23.0 profile: /desktop/plasma, /desktop/gnome, /desktop
    dm: str = ""  # key of DISPLAY_MANAGERS
    required: tuple[str, ...] = ()  # the desktop, the display manager's X server and the desktop's own companions, in install order
    init_atoms: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # extra atoms for one init system
    init_services: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)  # init -> ((service, runlevel), ...), optional services
    package_use: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # atom -> USE flags the desktop's own set needs
    flatpak_use: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # the same, only when Flatpak is installed
    portals: tuple[str, ...] = ()  # xdg-desktop-portal backends the desktop does not already bring (installed with Flatpak)
    session: str = "x11"  # x11 | wayland | both
    ppd_ui: bool = False  # the desktop has a power-profile switcher that wants power-profiles-daemon
    wayland_use: bool = False  # NVIDIA: build the driver with its Wayland support
    brings_noto: bool = False  # the package set already brings media-fonts/noto
    heavy: bool = False  # slow below 4 GiB of memory
    root_gib: tuple[int, int] = (8, 15)  # (minimum, recommended) root filesystem with the binary package host
    build_extra_gib: int = 10  # extra room when packages are compiled
    families: frozenset[str] = frozenset(FAMILIES)  # stage3 families the desktop can be built on
    experienced: bool = False  # for people who know what a tiling window manager is
    note: str = ""  # one more line on the card
    measured: bool = True  # False: the sizes are estimates from simulations

    @property
    def graphical(self) -> bool:
        return self.key != "minimal"

    def card_text(self) -> str:
        return f"{self.blurb} {self.note}".strip()


def _tuple_of(*atoms: str) -> tuple[str, ...]:
    return tuple(atoms)


_WITH_DESKTOP_FLAGS = frozenset({"standard", "desktop"})

DESKTOP_LIST: tuple[Desktop, ...] = (
    Desktop(
        "kde", "KDE Plasma", "guided",
        "A polished, flexible desktop with SDDM and a familiar application suite. Modern and customisable; good with NVIDIA.",
        profile_suffix="/desktop/plasma", dm="sddm",
        required=_tuple_of("kde-plasma/plasma-meta", KDE_APPS_CORE, SDDM, XORG),
        package_use={"dev-qt/qtpositioning": ("geoclue",)},  # kde-apps-meta -> kdeutils-meta -> kweather
        flatpak_use={"kde-plasma/plasma-meta": ("discover", "flatpak")},
        session="both", ppd_ui=True, wayland_use=True, brings_noto=True, heavy=True,
        root_gib=(24, 40), build_extra_gib=16, families=_WITH_DESKTOP_FLAGS,
    ),
    Desktop(
        "gnome", "GNOME", "guided",
        "A focused, modern desktop with GDM and GNOME applications.",
        profile_suffix="/desktop/gnome", dm="gdm",
        required=_tuple_of("gnome-base/gnome", GDM, XORG),
        package_use={"net-libs/ngtcp2": ("gnutls",)},  # samba -> libngtcp2 needs the GnuTLS backend
        session="both", ppd_ui=True, wayland_use=True, heavy=True,
        root_gib=(18, 28), build_extra_gib=14,
    ),
    Desktop(
        "xfce", "Xfce", "guided",
        "A lightweight, traditional desktop. Works well on older or resource-limited hardware.",
        profile_suffix="/desktop", dm="lightdm",
        # The panel's polkit agent is not pulled in on every profile.
        required=_tuple_of("xfce-base/xfce4-meta", XORG, LIGHTDM, LIGHTDM_GREETER, "gnome-extra/polkit-gnome"),
        package_use={"dev-libs/libdbusmenu": ("gtk3",)},  # xfce4-panel needs libdbusmenu[gtk3]
        root_gib=(14, 22), build_extra_gib=12,
    ),
    Desktop(
        "minimal", "Minimal system", "none",
        "No graphical desktop. Install only the base system and selected tools.",
        root_gib=(8, 15), build_extra_gib=10,
    ),
    Desktop(
        "cinnamon", "Cinnamon", "more",
        "A traditional, Windows-like desktop (the Linux Mint style) on X11 with LightDM.",
        profile_suffix="/desktop/gnome", dm="lightdm",
        required=_tuple_of("gnome-extra/cinnamon", XORG, LIGHTDM, LIGHTDM_GREETER, NM_APPLET, "x11-terms/gnome-terminal", "app-editors/gedit",
                           "media-gfx/eog", "app-arch/file-roller", "gnome-extra/nemo-fileroller", "media-gfx/maim"),
        init_atoms={"openrc": ("app-admin/openrc-settingsd",)},
        init_services={"openrc": (("openrc-settingsd", "default"),)},
        package_use={"net-libs/ngtcp2": ("gnutls",)},
        portals=_tuple_of("sys-apps/xdg-desktop-portal-gtk", "sys-apps/xdg-desktop-portal-xapp"),
        root_gib=(16, 24), build_extra_gib=12, families=_WITH_DESKTOP_FLAGS, measured=False,
        note="It needs the desktop/gnome Portage profile (the installer selects it): without it the web engine would be compiled for hours.",
    ),
    Desktop(
        "mate", "MATE", "more",
        "A classic, lightweight desktop in the style of GNOME 2, with LightDM.",
        profile_suffix="/desktop", dm="lightdm",
        required=_tuple_of("mate-base/mate", XORG, LIGHTDM, LIGHTDM_GREETER, NM_APPLET),
        init_atoms={"openrc": ("app-admin/openrc-settingsd",)},
        init_services={"openrc": (("openrc-settingsd", "default"),)},
        package_use={"mate-base/mate": ("extras", "themes", "notification")},
        portals=_tuple_of("sys-apps/xdg-desktop-portal-gtk", "sys-apps/xdg-desktop-portal-xapp"),
        root_gib=(14, 22), build_extra_gib=12, measured=False,
    ),
    Desktop(
        "lxqt", "LXQt", "more",
        "A very light Qt desktop with SDDM; a good choice for old computers.",
        profile_suffix="/desktop", dm="sddm",
        required=_tuple_of("lxqt-base/lxqt-meta", SDDM, XORG, NM_APPLET, "app-editors/featherpad"),
        package_use={"lxqt-base/lxqt-meta": ("powermanagement",)},
        root_gib=(14, 22), build_extra_gib=12, measured=False,
    ),
    Desktop(
        "gnome-light", "GNOME Light", "more",
        "GNOME with fewer applications installed: the same desktop, less to download and compile.",
        profile_suffix="/desktop/gnome", dm="gdm",
        required=_tuple_of("gnome-base/gnome-light", GDM, XORG),
        package_use={"net-libs/ngtcp2": ("gnutls",)},
        session="both", ppd_ui=True, wayland_use=True, heavy=True,
        root_gib=(16, 24), build_extra_gib=12, measured=False,
    ),
    Desktop(
        "sway", "Sway", "more",
        "A tiling Wayland window manager driven by the keyboard, with a bar, launcher, terminal and network applet. For experienced users.",
        profile_suffix="/desktop", dm="sddm",
        required=_tuple_of("gui-wm/sway", SDDM, "gui-apps/foot", "gui-apps/waybar", "gui-apps/mako", "gui-apps/wofi", "gui-apps/swaylock",
                           "gui-apps/swayidle", "gui-apps/swaybg", "gui-apps/grim", "gui-apps/slurp", "gui-apps/wl-clipboard", "media-sound/pavucontrol",
                           "gui-libs/xdg-desktop-portal-wlr", "sys-apps/xdg-desktop-portal-gtk", NM_APPLET, "mate-extra/mate-polkit"),
        session="wayland", wayland_use=True, experienced=True,
        root_gib=(12, 20), build_extra_gib=10, measured=False,
        note="NVIDIA graphics are not supported by upstream Sway. There is no settings program: you edit a text file.",
    ),
    Desktop(
        "i3", "i3", "more",
        "A tiling window manager on X11 driven by the keyboard, with a status bar, launcher and network applet. For experienced users.",
        profile_suffix="/desktop", dm="lightdm",
        required=_tuple_of("x11-wm/i3", XORG, LIGHTDM, LIGHTDM_GREETER, "x11-misc/i3status", "x11-misc/i3lock", "x11-misc/dmenu", "x11-terms/xfce4-terminal",
                           "media-gfx/feh", "x11-misc/picom", "x11-misc/dunst", "media-gfx/maim", "x11-misc/xss-lock", "media-sound/pavucontrol",
                           NM_APPLET, "mate-extra/mate-polkit"),
        portals=_tuple_of("sys-apps/xdg-desktop-portal-gtk"),
        experienced=True, root_gib=(12, 18), build_extra_gib=10, measured=False,
        note="There is no settings program: you edit a text file.",
    ),
)

REGISTRY: Mapping[str, Desktop] = {desktop.key: desktop for desktop in DESKTOP_LIST}
DESKTOPS: tuple[str, ...] = tuple(REGISTRY)
GUIDED_DESKTOPS = tuple(d.key for d in DESKTOP_LIST if d.tier in {"guided", "none"})
MORE_DESKTOPS = tuple(d.key for d in DESKTOP_LIST if d.tier == "more")


def get(key: str) -> Desktop | None:
    return REGISTRY.get(key)


def is_graphical(key: str) -> bool:
    return key in REGISTRY and key != "minimal"


def family_supports(desktop: str, family: str) -> bool:
    """True when the desktop may be installed on this stage3 family (on Hardened and No-multilib possibly unverified)."""
    entry = REGISTRY.get(desktop)
    return entry is not None and entry.graphical and family in entry.families


def family_problem(desktop: str, family: str) -> str:
    """Why this desktop cannot be installed on this stage3 family ('' when it can, possibly with a warning)."""
    entry = REGISTRY.get(desktop)
    if entry is None or not entry.graphical or family_supports(desktop, family):
        return ""
    if desktop == "kde":
        return KDE_NEEDS_DESKTOP_STAGE
    if desktop == "cinnamon":
        return CINNAMON_NEEDS_GNOME_PROFILE
    return f"{entry.label} is not available on this stage3: choose a Standard or Desktop stage3 or the Minimal system."


def family_desktop_support() -> dict[str, set[str]]:
    return {family: {key for key in DESKTOPS if family_supports(key, family)} for family in FAMILIES}


def unverified(desktop: str, family: str) -> bool:
    """True when the desktop is offered on this family but was not verified there (the Review page warns)."""
    return family in UNVERIFIED_FAMILIES and family_supports(desktop, family)


def profile_suffix(desktop: str) -> str:
    entry = REGISTRY.get(desktop)
    return entry.profile_suffix if entry else ""


def required_atoms(desktop: str, init: str, kde_apps: str = "core") -> list[str]:
    """The desktop environment, its display manager and X server, in install order, for one init system."""
    entry = REGISTRY[desktop]
    atoms = [KDE_APPS_FULL if (atom == KDE_APPS_CORE and kde_apps == "full") else atom for atom in entry.required]
    atoms.extend(entry.init_atoms.get(init, ()))
    return list(dict.fromkeys(atoms))


def package_use(desktop: str, flatpak: bool = False) -> dict[str, tuple[str, ...]]:
    entry = REGISTRY.get(desktop)
    if entry is None:
        return {}
    use = dict(entry.package_use)
    if flatpak:
        use.update(entry.flatpak_use)
    return use


def display_manager(desktop: str) -> tuple[str, str]:
    """(OpenRC DISPLAYMANAGER name, systemd unit) of the desktop's display manager; ('', '') without one."""
    entry = REGISTRY.get(desktop)
    if entry is None or not entry.dm:
        return "", ""
    _, openrc_name, unit = DISPLAY_MANAGERS[entry.dm]
    return openrc_name, unit


def services(desktop: str, init: str) -> tuple[tuple[str, str], ...]:
    entry = REGISTRY.get(desktop)
    return tuple(entry.init_services.get(init, ())) if entry else ()


def root_gib(desktop: str) -> tuple[int, int]:
    """(minimum, recommended) root filesystem in GiB with the binary package host; unknown desktops use KDE Plasma's figures."""
    return (REGISTRY.get(desktop) or REGISTRY["kde"]).root_gib


def build_extra_gib(desktop: str) -> int:
    return (REGISTRY.get(desktop) or REGISTRY["kde"]).build_extra_gib


def card_choices(keys: tuple[str, ...], recommended: str = "kde") -> list[tuple[str, str, str, bool]]:
    """(key, title, description, recommended) tuples for the GUI cards of these desktops."""
    out = []
    for key in keys:
        entry = REGISTRY[key]
        text = entry.card_text()
        if entry.graphical:
            text += f" About {entry.root_gib[0]} GiB of disk space{'' if entry.measured else ' (estimated)'}."
        if entry.session == "x11":
            text += " X11 session."
        if entry.experienced:
            text += " For experienced users."
        out.append((key, entry.label, text, key == recommended))
    return out

