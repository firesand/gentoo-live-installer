"""The Gentoo adapter: the one place that knows how this tool reinstalls a boot loader, rebuilds an initramfs and reinstalls a kernel.

The tool repairs Gentoo only. ``detect_adapter`` therefore always answers with the Gentoo adapter, whose ``write_refusal`` turns every repair of a
system that is not Gentoo down (the engine does not open such a system at all; this is the second lock).
"""

from __future__ import annotations

from ..view import SessionView
from .base import Adapter
from .gentoo import GentooAdapter

_GENTOO = GentooAdapter()


def detect_adapter(view: SessionView) -> Adapter:
    return _GENTOO


__all__ = ["Adapter", "GentooAdapter", "detect_adapter"]
