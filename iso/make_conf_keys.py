"""The Portage settings that iso/ keeps across Catalyst's make.conf rewrites.

Catalyst writes /etc/portage/make.conf twice: before emerge (setup=True) and again when
it cleans the chroot for the image (setup=False). Both rewrites drop everything that is
not generated from the spec. iso/render-config.py takes these keys from
iso/portage/make.conf into make.conf.extra, and iso/catalyst-no-loop.py appends them after
each rewrite. Keeping the lists here makes it impossible for the two sides to drift.
"""
from __future__ import annotations

# Everything the build itself needs, in the order it is written.
BUILD_KEYS: tuple[str, ...] = (
    "MAKEOPTS",
    "NINJAOPTS",
    "EMERGE_DEFAULT_OPTS",
    "ACCEPT_LICENSE",
    "GRUB_PLATFORMS",
    "INPUT_DEVICES",
    "VIDEO_CARDS",
    "FEATURES",
    "PYTHON_TARGETS",
    "PYTHON_SINGLE_TARGET",
)

# What the live image keeps (the setup=False rewrite). The builder's job counts, its
# binary-package options and licence wildcard would only be wrong on somebody else's PC:
# a 2-core laptop must not inherit --jobs=4, and livecd-tools' autoconfig adds
# EMERGE_DEFAULT_OPTS from the CPU count of the machine it boots on, but only when
# make.conf does not already set --jobs.
LIVE_KEYS: tuple[str, ...] = (
    "GRUB_PLATFORMS",
    "INPUT_DEVICES",
    "VIDEO_CARDS",
    "FEATURES",
    "PYTHON_TARGETS",
    "PYTHON_SINGLE_TARGET",
)

# FEATURES words that make no sense on the live system (a binary package of every
# emerge would fill the RAM disk).
LIVE_DROPPED_FEATURES: tuple[str, ...] = ("buildpkg",)

# Lines that Catalyst itself writes from catalyst.conf and that point at the builder's
# directories; they are removed from the live make.conf.
BUILDER_ONLY_VARIABLES: tuple[str, ...] = ("DISTDIR", "PKGDIR")

assert set(LIVE_KEYS) <= set(BUILD_KEYS)
