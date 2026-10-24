"""A fake server registry and channel for the API tests (no socket, no server.py needed)."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable

from gentoo_installerd.events import Event


class ChannelClosed(Exception):
    pass


@dataclass
class Peer:
    pid: int = 4242
    uid: int = 1000
    gid: int = 1000


class FakeChannel:
    def __init__(self, *, die_after: int | None = None, error: type[BaseException] = ChannelClosed) -> None:
        self.events: list[dict[str, Any]] = []
        self.fds: list[list[int]] = []
        self.peer = Peer()
        self.closed = False
        self.die_after = die_after
        self.error = error

    def send(self, event: Event) -> None:
        if self.die_after is not None and len(self.events) >= self.die_after:
            self.closed = True
            raise self.error("peer left")
        self.events.append(event.to_dict())

    def send_with_fds(self, event: Event, fds: Sequence[int]) -> None:
        self.events.append(event.to_dict())
        self.fds.append([os.dup(fd) for fd in fds])

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["type"] == kind]

    @property
    def result(self) -> dict[str, Any]:
        return self.of_type("result")[-1]["data"]

    @property
    def completed(self) -> dict[str, Any]:
        done = self.of_type("completed")
        assert len(done) == 1, done
        return done[0]

    @property
    def error_event(self) -> dict[str, Any]:
        return self.of_type("error")[-1]


class FakeRegistry:
    def __init__(self) -> None:
        self.actions: dict[str, Callable[[dict[str, Any], Any], None]] = {}
        self.features: dict[str, str | None] = {}
        self.hooks: list[Callable[[], None]] = []

    def register_action(self, name: str, handler: Callable[[dict[str, Any], Any], None], *, feature: str | None = None) -> None:
        self.actions[name] = handler
        self.features[name] = feature

    def add_shutdown_hook(self, hook: Callable[[], None]) -> None:
        self.hooks.append(hook)

    def call(self, name: str, request: dict[str, Any] | None = None, channel: FakeChannel | None = None) -> FakeChannel:
        channel = channel or FakeChannel()
        self.actions[name]({"version": 1, "action": name, **(request or {})}, channel)
        return channel
