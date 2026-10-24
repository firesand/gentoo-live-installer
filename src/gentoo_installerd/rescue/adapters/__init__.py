"""Distribution adapters; Arch, openSUSE and the generic adapter detect and diagnose only (writes are refused with a reason)."""

from __future__ import annotations

from ..view import SessionView
from .base import Adapter
from .debian import DebianAdapter
from .fedora import FedoraAdapter
from .gentoo import GentooAdapter


class ReadOnlyAdapter(Adapter):
    """Detection and read-only diagnosis only: the distributions without a tested repair path."""

    def __init__(self, family: str = "generic") -> None:
        self.family = family  # type: ignore[misc]


_ADAPTERS: dict[str, Adapter] = {"gentoo": GentooAdapter(), "debian": DebianAdapter(), "fedora": FedoraAdapter()}


def detect_adapter(view: SessionView) -> Adapter:
    return _ADAPTERS.get(view.family) or ReadOnlyAdapter(view.family)


__all__ = ["Adapter", "DebianAdapter", "FedoraAdapter", "GentooAdapter", "ReadOnlyAdapter", "detect_adapter"]
