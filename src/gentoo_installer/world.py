"""PORT: whether to update the @world set during the installation (Handbook: Optional, Updating the @world set).

The Handbook says to update @world when the profile or the USE flags differ from the ones the stage3 was built with, so that
every program of the starting system matches the choices made here. Skipping it is fine: it can be done later.

* ``now``    update during the installation
* ``later``  skip it
* ``auto``   update only when something differs from the stage3 (the chosen profile, or USE flags set by the user)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .manifest import PROFILE_BASE, stage_family
from .stagechoice import effective_stage_variant

if TYPE_CHECKING:
    from .manifest import InstallManifest

WORLD_HELP = ("Brings every program of the starting system in line with your choices; skipping it is fine, you can update later.")
DEPCLEAN_HELP = "Removes programs that nothing needs any more after the update. Off by default; the Handbook suggests reading what it removes first."


def stage_profile(stage_variant: str) -> str:
    """The profile a stage3 variant ships with ("amd64-desktop-systemd" -> .../desktop/systemd)."""
    family = stage_family(stage_variant)
    path = PROFILE_BASE + {"standard": "", "desktop": "/desktop", "nomultilib": "/no-multilib", "hardened": "/hardened"}[family]
    return path + ("/systemd" if stage_variant.endswith("systemd") else "")


def world_update_reason(manifest: "InstallManifest") -> str:
    """Why @world needs an update here ("" when it does not): the profile differs from the stage3's, or USE was changed."""
    system, portage = manifest.system, manifest.portage
    variant = effective_stage_variant(system.stage_variant, system.desktop, system.init, system.multilib, system.stage_auto)
    chosen = manifest.effective_profile()  # the override, else the profile set by the user, else the derived one
    if chosen != stage_profile(variant):
        return "the chosen profile differs from the one the stage3 was built with"
    if portage.use_flags or any(flags for flags in portage.package_use.values()):
        return "USE flags were changed"
    return ""


def should_update_world(manifest: "InstallManifest") -> bool:
    """True when @world is updated during the installation."""
    choice = manifest.system.world_update
    if choice == "now":
        return True
    if choice == "auto":
        return bool(world_update_reason(manifest))
    return False


def should_depclean(manifest: "InstallManifest", world_updated: bool) -> bool:
    """depclean runs only when it was ticked and the @world update just finished without error."""
    return manifest.portage.depclean and world_updated
