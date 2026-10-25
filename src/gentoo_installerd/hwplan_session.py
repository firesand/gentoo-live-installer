"""Session extras of the hardware plan that follow the language and the user's needs: input methods, the screen reader, symbol fonts.

``SessionPlanMixin`` is mixed into ``hwplan._Planner``. The input method is chosen from the script of the locale (Chinese, Japanese and Korean need one
to type at all); every package is an optional atom, so a build failure degrades instead of stopping the installation. Nothing sets the global USE
flag ``accessibility``: it would rebuild a large part of the system and stop the binary packages from matching, and Orca works through AT-SPI without it.
"""

from __future__ import annotations

from typing import Any

from gentoo_installer import hardware_data as data
from gentoo_installer import keyboard_data
from gentoo_installer.manifest import InstallManifest

# language -> (fcitx5 engine, ibus engine)
ENGINES = {
    "ja": ("app-i18n/fcitx-anthy:5", "app-i18n/ibus-anthy"),
    "zh": ("app-i18n/fcitx-chinese-addons:5", "app-i18n/ibus-libpinyin"),
    "ko": ("app-i18n/fcitx-hangul:5", "app-i18n/ibus-hangul"),
}
ENV_FILE = "/etc/env.d/90installer-input-method"
GTK_DESKTOPS_WITHOUT_WAYLAND_INPUT = ("xfce", "i3", "lxqt", "mate", "cinnamon")  # X11 sessions: the GTK module is what lets GTK programs reach the input method
ACCESSIBILITY_ATOMS = ("app-accessibility/orca", "app-accessibility/speech-dispatcher", "app-accessibility/espeak-ng", "dev-libs/at-spi2-core")


def input_method_for(manifest: InstallManifest) -> str:
    """The input method this installation gets: ibus, fcitx5 or none (Automatic follows the script of the locale)."""
    choice = manifest.options.input_method
    traits = data.desktop_traits(manifest.system.desktop)
    if choice != "auto":
        return "" if choice == "none" or not traits.graphical else choice
    if not traits.graphical or keyboard_data.locale_script(manifest.system.locale) != "cjk":
        return ""
    return "ibus" if manifest.system.desktop in ("gnome", "gnome-light") else "fcitx5"  # GNOME has ibus built in


class SessionPlanMixin:
    m: InstallManifest
    p: Any
    desktop: str
    traits: data.DesktopTraits

    def session(self) -> None:
        self.input_method()
        self.accessibility()
        if self.desktop == "sway":
            self.p.optional_atoms.append("media-fonts/fontawesome")  # the icons of waybar's default bar

    def input_method(self) -> None:
        method = input_method_for(self.m)
        if not method:
            return
        self.p.scalars["input_method"] = method
        language = keyboard_data.locale_language(self.m.system.locale)
        engines = ENGINES.get(language)
        lines = ["# Written by the Gentoo installer: the input method of the session."]
        if method == "fcitx5":
            self.p.optional_atoms.extend(["app-i18n/fcitx:5", "app-i18n/fcitx-gtk:5", "app-i18n/fcitx-qt:5", "app-i18n/fcitx-configtool:5"])
            if engines:
                self.p.optional_atoms.append(engines[0])
            lines += ["XMODIFIERS=@im=fcitx", "QT_IM_MODULE=fcitx"]
            if self.desktop in GTK_DESKTOPS_WITHOUT_WAYLAND_INPUT:
                lines.append("GTK_IM_MODULE=fcitx")
            name = "Fcitx 5"
        else:
            self.p.optional_atoms.append("app-i18n/ibus")
            if engines:
                self.p.optional_atoms.append(engines[1])
            lines += ["XMODIFIERS=@im=ibus"]
            if self.desktop not in ("gnome", "gnome-light"):
                lines += ["GTK_IM_MODULE=ibus", "QT_IM_MODULE=ibus"]
            name = "IBus"
        self.file(ENV_FILE, "\n".join(lines) + "\n")  # type: ignore[attr-defined]
        self.note(f"{name} was added as the input method for {self.m.system.locale}: it is needed to type that language.")  # type: ignore[attr-defined]
        self.first_step(f"Typing {self.m.system.locale}: {name} starts with your session. Add your input source in its settings"  # type: ignore[attr-defined]
                        + (" (Keyboard, Input Sources)." if method == "ibus" and self.desktop in ("gnome", "gnome-light") else "."))

    def accessibility(self) -> None:
        if not self.m.options.accessibility or not self.traits.graphical:
            return
        self.p.optional_atoms.extend(ACCESSIBILITY_ATOMS)
        self.use("app-accessibility/speech-dispatcher", "pipewire" if self.m.options.pipewire else "alsa")  # type: ignore[attr-defined]
        self.first_step("Screen reader: press Alt+Super+S to start Orca, or turn on the screen reader in the desktop's accessibility settings.")  # type: ignore[attr-defined]
        self.note("Accessibility: the Orca screen reader and a speech voice (espeak-ng) were added.")  # type: ignore[attr-defined]
