"""A ready-made laboratory: fake host, fake system, policy, runner and audit log in one temporary directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from gentoo_installerd.rescue.audit import AuditLog
from gentoo_installerd.rescue.hostenv import Host
from gentoo_installerd.rescue.runner import RescueRunner
from gentoo_installerd.rescue.safety import PolicyContext

from . import fakeroot
from .fakes import FakeSystem, make_host


class Lab:
    def __init__(self, tmp: Path, lsblk: str | Callable[[FakeSystem], str], *, uefi: bool = True, probing: bool = True, session_id: str = "", name: str = "", **host_options: Any) -> None:
        tmp = tmp / name if name else tmp
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.host_root = make_host(tmp / "host", uefi=uefi, **host_options)
        self.host = Host(self.host_root, which=lambda name: f"/usr/bin/{name}")
        self.system = FakeSystem(tmp / "fake", lsblk)
        (tmp / "fake").mkdir(parents=True, exist_ok=True)
        self.probe_root = tmp / "probe"
        self.state_dir = tmp / "state"
        self.base = tmp / "mnt"
        self.base.mkdir(exist_ok=True)
        self.ctx = PolicyContext(probe_root=str(self.probe_root), state_dir=str(self.state_dir), session_base=str(self.base), probing=probing, session_id=session_id)
        self.audit = AuditLog(tmp / "audit.jsonl", "lab")
        self.runner = RescueRunner(self.system, self.ctx, self.audit)

    def add_tree(self, device: str, family: str, **options: Any) -> Path:
        tree = fakeroot.make_root(self.tmp / "trees" / device.strip("/").replace("/", "_"), family, **options)
        self.system.attach(device, tree)
        return tree

    def add_plain_tree(self, device: str, files: dict[str, str]) -> Path:
        tree = self.tmp / "trees" / device.strip("/").replace("/", "_")
        tree.mkdir(parents=True, exist_ok=True)
        for path, text in files.items():
            fakeroot.put(tree, path, text)
        self.system.attach(device, tree)
        return tree

    def scanner(self, **kwargs: Any):
        from gentoo_installerd.rescue.scan import Scanner

        return Scanner(self.runner, self.ctx, self.host, self.probe_root, sleep=lambda _s: None, **kwargs)

    def graph(self):
        from gentoo_installerd.rescue.devices import build_graph, rescue_lsblk_argv
        from gentoo_installerd.rescue.scan import live_sources

        text = self.runner.run(rescue_lsblk_argv()).stdout
        graph = build_graph(text, live_sources=live_sources(self.runner), cmdline=self.host.cmdline())
        for node in graph:
            if node.live_medium:
                self.ctx.devices.mark_live(node.path)
        return graph

    def stack(self, ledger=None):
        from gentoo_installerd.rescue.ledger import Ledger
        from gentoo_installerd.rescue.stack import StackManager

        self.ledger = ledger or Ledger()
        return StackManager(self.runner, self.ctx, self.ledger, self.graph, self.host)
