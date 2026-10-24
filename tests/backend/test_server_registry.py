"""docs/protocol.md: the action registry, Channel, capabilities, error codes and the exception boundary."""
from __future__ import annotations

import array
import importlib.machinery
import json
import logging
import os
import socket
import sys
import threading
import types as pytypes
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd import server as server_module
from gentoo_installerd.events import Event
from gentoo_installerd.server import (
    ActionError,
    ActionRegistry,
    Channel,
    ChannelClosed,
    InstallManager,
    PeerCredentials,
    load_plugins,
)

from tests.backend.test_server_support import DaemonCase, types, wait_for


class RegistryTests(unittest.TestCase):
    def test_builtin_actions_and_features(self) -> None:
        registry = ActionRegistry()
        self.assertEqual(registry.actions(), ["attach", "cancel", "capabilities", "inspect", "log", "start", "status"])
        self.assertEqual(registry.features(), ["install.v1", "attach.v1"])
        self.assertEqual(registry.capabilities()["protocol"], 1)

    def test_register_action_adds_the_action_and_its_feature(self) -> None:
        registry = ActionRegistry()
        registry.register_action("rescue.scan", lambda request, channel: None, feature="rescue.v1")
        registry.register_action("rescue.mount", lambda request, channel: None, feature="rescue.v1")
        self.assertIn("rescue.scan", registry.actions())
        self.assertEqual(registry.features(), ["install.v1", "attach.v1", "rescue.v1"])
        self.assertIsNotNone(registry.lookup("rescue.scan"))
        self.assertIsNone(registry.lookup("rescue.nothing"))

    def test_an_action_cannot_be_hijacked_but_the_same_handler_may_register_twice(self) -> None:
        registry = ActionRegistry()

        def first(request, channel): ...

        def second(request, channel): ...

        registry.register_action("x.one", first)
        registry.register_action("x.one", first)
        with self.assertRaises(ValueError):
            registry.register_action("x.one", second)
        with self.assertRaises(ValueError):
            registry.register_action("status", second)
        registry.register_action("x.one", second, replace=True)
        self.assertIs(registry.lookup("x.one"), second)

    def test_a_bound_method_may_be_registered_again(self) -> None:
        # rescue.api.register(registry) can run twice in one process (tests, a restarted daemon object)
        class Api:
            def scan(self, request, channel): ...

        api = Api()
        registry = ActionRegistry()
        registry.register_action("rescue.scan", api.scan)
        registry.register_action("rescue.scan", api.scan)  # a new bound-method object, equal to the first
        with self.assertRaises(ValueError):
            registry.register_action("rescue.scan", Api().scan)

    def test_bad_names_and_handlers_are_rejected(self) -> None:
        registry = ActionRegistry()
        for name in ("", "has space", "9lead", "semi;colon", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                registry.register_action(name, lambda request, channel: None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            registry.register_action("x.y", "not callable")  # type: ignore[arg-type]

    def test_module_level_functions_use_the_process_registry(self) -> None:
        name = "test_server_registry.module_level"
        try:
            server_module.register_action(name, lambda request, channel: None, feature="test.v1")
            self.assertIn(name, server_module.REGISTRY.actions())
            self.assertIn("test.v1", server_module.REGISTRY.features())
        finally:
            server_module.REGISTRY.unregister_action(name)
            server_module.REGISTRY._features.pop("test.v1", None)

    def test_shutdown_hooks_run_last_registered_first_and_a_failure_does_not_stop_the_rest(self) -> None:
        registry = ActionRegistry()
        order: list[str] = []
        registry.add_shutdown_hook(lambda: order.append("first"))
        registry.add_shutdown_hook(lambda: (_ for _ in ()).throw(RuntimeError("hook bug")))
        registry.add_shutdown_hook(lambda: order.append("third"))
        with self.assertLogs("gentoo_installerd.server", level="ERROR") as logs:
            registry.run_shutdown_hooks()
        self.assertEqual(order, ["third", "first"])
        self.assertTrue(any("hook bug" in line for line in logs.output))

    def test_a_hanging_shutdown_hook_is_abandoned_after_its_timeout(self) -> None:
        registry = ActionRegistry()
        release = threading.Event()
        order: list[str] = []
        registry.add_shutdown_hook(lambda: order.append("after"))
        registry.add_shutdown_hook(lambda: release.wait(30))
        try:
            with self.assertLogs("gentoo_installerd.server", level="ERROR"):
                registry.run_shutdown_hooks(timeout=0.2)
        finally:
            release.set()
        self.assertEqual(order, ["after"])

    def test_status_providers_extend_the_status_reply_and_failures_are_contained(self) -> None:
        registry = ActionRegistry()
        registry.add_status_provider("rescue", lambda: {"sessions": 0})
        registry.add_status_provider("broken", lambda: 1 / 0)
        registry.add_status_provider("absent", lambda: None)
        with self.assertLogs("gentoo_installerd.server", level="ERROR"):
            self.assertEqual(registry.status_extensions(), {"rescue": {"sessions": 0}})


# Import of either name raises ModuleNotFoundError, also when another test already imported the real module.
ABSENT_RESCUE = {"gentoo_installerd.rescue": None, "gentoo_installerd.rescue.api": None}


class PluginTests(unittest.TestCase):
    def install_module(self, name: str, **attributes) -> None:
        module = pytypes.ModuleType(name)
        module.__spec__ = importlib.machinery.ModuleSpec(name, None)
        for key, value in attributes.items():
            setattr(module, key, value)
        patcher = patch.dict(sys.modules, {name: module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_present_plugins_register_and_absent_ones_are_skipped(self) -> None:
        registry = ActionRegistry()
        calls: list[object] = []

        def register(reg):
            calls.append(reg)
            reg.register_action("hw.sync_time", lambda request, channel: None, feature="hardware.v1")

        self.install_module("gentoo_installerd.hwactions", register=register)
        with patch.dict(sys.modules, ABSENT_RESCUE):
            loaded = load_plugins(registry)
        self.assertEqual(loaded, ["gentoo_installerd.hwactions"])
        self.assertEqual(calls, [registry])
        self.assertIn("hardware.v1", registry.features())

    def test_rescue_is_not_loaded_with_no_rescue(self) -> None:
        registry = ActionRegistry()
        self.install_module("gentoo_installerd.rescue.api", register=lambda reg: reg.register_action("rescue.scan", lambda request, channel: None, feature="rescue.v1"))
        self.install_module("gentoo_installerd.rescue")
        self.assertEqual(load_plugins(registry, rescue=False), [])
        self.assertNotIn("rescue.v1", registry.features())
        self.assertEqual(load_plugins(registry, rescue=True), ["gentoo_installerd.rescue.api"])
        self.assertIn("rescue.v1", registry.features())

    def test_a_broken_plugin_is_logged_and_does_not_stop_the_daemon(self) -> None:
        registry = ActionRegistry()
        self.install_module("gentoo_installerd.hwactions", register=lambda reg: 1 / 0)
        with patch.dict(sys.modules, ABSENT_RESCUE), self.assertLogs("gentoo_installerd.server", level="ERROR"):
            self.assertEqual(load_plugins(registry), [])

    def test_a_plugin_with_a_missing_dependency_is_an_error_not_silence(self) -> None:
        registry = ActionRegistry()
        with patch("importlib.import_module", side_effect=ModuleNotFoundError("No module named 'needed_lib'", name="needed_lib")), self.assertLogs("gentoo_installerd.server", level="ERROR") as logs:
            self.assertEqual(load_plugins(registry), [])
        self.assertTrue(any("needed_lib" in line for line in logs.output))


class ChannelTests(unittest.TestCase):
    def pair(self) -> tuple[Channel, socket.socket]:
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.settimeout(5)
        return Channel(a), b

    def test_send_writes_one_json_line_and_tracks_completion(self) -> None:
        channel, peer = self.pair()
        channel.send(Event("status", data={"x": 1}))
        self.assertFalse(channel.completed_sent)
        channel.send(Event("completed", data={"success": True}))
        self.assertTrue(channel.completed_sent)
        lines = peer.recv(65536).decode().splitlines()
        self.assertEqual([json.loads(line)["type"] for line in lines], ["status", "completed"])

    def test_request_id_is_echoed_in_every_event(self) -> None:
        channel, peer = self.pair()
        channel.request_id = "req-7"
        channel.send(Event("status", data={"x": 1}))
        channel.send(Event("completed", data={"success": True}))
        events = [json.loads(line) for line in peer.recv(65536).decode().splitlines()]
        self.assertEqual([event["data"]["request_id"] for event in events], ["req-7", "req-7"])
        self.assertEqual(events[0]["data"]["x"], 1)

    def test_send_after_the_peer_left_raises_channel_closed(self) -> None:
        channel, peer = self.pair()
        peer.close()
        with self.assertRaises(ChannelClosed):
            for _ in range(200):  # the first send after a close may still be buffered
                channel.send(Event("log", message="x" * 1000))
        self.assertTrue(channel.closed)
        with self.assertRaises(ChannelClosed):
            channel.send(Event("log", message="again"))

    def test_a_send_timeout_closes_the_channel_instead_of_raising_a_bare_timeout(self) -> None:
        channel, peer = self.pair()
        channel._sock.settimeout(0.2)
        with self.assertRaises(ChannelClosed):
            for _ in range(100000):  # nobody reads: the socket buffer fills and the send times out
                channel.send(Event("log", message="x" * 1000))
        self.assertTrue(channel.closed)

    def test_closed_tells_a_vanished_peer_from_one_that_only_stopped_writing(self) -> None:
        channel, peer = self.pair()
        self.assertFalse(channel.closed)
        peer.shutdown(socket.SHUT_WR)  # the client sent its request and waits for the answer
        self.assertFalse(channel.closed)
        channel.send(Event("status"))  # and it is still reachable
        self.assertEqual(json.loads(peer.recv(4096))["type"], "status")
        peer.close()
        self.assertTrue(channel.closed)

    def test_peer_credentials_come_from_the_kernel(self) -> None:
        channel, _peer = self.pair()
        self.assertEqual(channel.peer, PeerCredentials(os.getpid(), os.getuid(), os.getgid()))

    def test_send_with_fds_passes_descriptors(self) -> None:
        channel, peer = self.pair()
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        try:
            channel.send_with_fds(Event("result", data={"fd": 1}), [write_end])
        finally:
            os.close(write_end)
        message, fds, _flags, _address = socket.recv_fds(peer, 65536, 4)
        self.assertEqual(json.loads(message)["type"], "result")
        self.assertEqual(len(fds), 1)
        os.write(fds[0], b"hello")
        os.close(fds[0])
        self.assertEqual(os.read(read_end, 10), b"hello")

    def test_channel_closed_is_an_oserror(self) -> None:
        self.assertTrue(issubclass(ChannelClosed, OSError))


class ProtocolBoundaryTests(DaemonCase):
    def setUp(self) -> None:
        self.start_daemon()

    def test_capabilities_lists_features_actions_and_the_stage_table(self) -> None:
        self.registry.register_action("rescue.scan", lambda request, channel: channel.send(Event("result", data={"ok": True})), feature="rescue.v1")
        events = self.call({"action": "capabilities", "request_id": "c1"})
        self.assertEqual(types(events), ["result"])
        data = events[0]["data"]
        self.assertEqual(data["protocol"], 1)
        self.assertEqual(data["features"][:3], ["install.v1", "attach.v1", "rescue.v1"])
        self.assertIn("rescue.scan", data["actions"])
        self.assertEqual([stage["name"] for stage in data["stages"]][:3], ["preflight", "storage", "stage3"])
        self.assertEqual(sum(stage["weight"] for stage in data["stages"]), 100)
        self.assertEqual(data["request_id"], "c1")

    def test_hardware_feature_appears_when_the_module_exists(self) -> None:
        module = pytypes.ModuleType("gentoo_installer.hardware")
        module.__spec__ = importlib.machinery.ModuleSpec("gentoo_installer.hardware", None)
        with patch.dict(sys.modules, {"gentoo_installer.hardware": module}):
            features = self.call({"action": "capabilities"})[0]["data"]["features"]
        self.assertIn("hardware.v1", features)

    def test_a_registered_handler_gets_the_request_and_a_channel(self) -> None:
        seen = {}

        def handler(request, channel):
            seen["request"] = request
            seen["peer"] = channel.peer
            channel.send(Event("result", data={"echo": request.get("value")}))

        self.registry.register_action("test.echo", handler)
        events = self.call({"action": "test.echo", "value": 42, "request_id": 9})
        self.assertEqual(types(events), ["result"])
        self.assertEqual(events[0]["data"], {"echo": 42, "request_id": 9})
        self.assertEqual(seen["peer"].uid, os.getuid())
        self.assertEqual(seen["request"]["value"], 42)

    def test_unsupported_action_has_a_namespaced_code_and_ends_with_completed(self) -> None:
        events = self.call({"action": "rescue.scan"})
        self.assertEqual(types(events), ["error", "completed"])
        self.assertEqual(events[0]["data"]["code"], "proto.unsupported_action")
        self.assertIn("status", events[0]["error"])
        self.assertIs(events[1]["data"]["success"], False)

    def test_malformed_requests_get_bad_request(self) -> None:
        cases = {
            "not json": b"this is not json\n",
            "array": b"[1, 2]\n",
            "wrong version": b'{"version": 2, "action": "status"}\n',
            "no action": b'{"version": 1}\n',
            "bad request id": b'{"version": 1, "action": "status", "request_id": {"a": 1}}\n',
        }
        for name, raw in cases.items():
            with self.subTest(case=name):
                events = self.call({}, raw=raw)
                self.assertEqual(types(events), ["error", "completed"], events)
                self.assertIn(events[0]["data"]["code"], {"proto.bad_request", "proto.unsupported_action"})

    def test_an_oversized_request_is_refused(self) -> None:
        with patch.object(server_module, "MAX_REQUEST_BYTES", 100):
            events = self.call({}, raw=b'{"version":1,"action":"status","pad":"' + b"x" * 500 + b'"}\n')
        self.assertEqual(events[0]["data"]["code"], "proto.bad_request")
        self.assertIn("too large", events[0]["error"])

    def test_a_client_that_sends_nothing_gets_proto_timeout(self) -> None:
        with patch.object(server_module, "REQUEST_TIMEOUT", 0.3), self.connect() as client:
            events = []
            for line in client.makefile("rb"):
                events.append(json.loads(line))
        self.assertEqual(types(events), ["error", "completed"])
        self.assertEqual(events[0]["data"]["code"], "proto.timeout")

    def test_a_client_that_connects_and_leaves_is_harmless(self) -> None:
        with self.connect():
            pass
        self.assertEqual(self.call({"action": "status"})[0]["data"]["status"], "idle")

    def test_an_uncaught_handler_exception_becomes_proto_internal_and_completed(self) -> None:
        self.registry.register_action("test.bug", lambda request, channel: 1 / 0)
        with self.assertLogs("gentoo_installerd.server", level="ERROR") as logs:
            events = self.call({"action": "test.bug"})
        self.assertEqual(types(events), ["error", "completed"])
        self.assertEqual(events[0]["data"]["code"], "proto.internal")
        self.assertIn("Installer backend error", events[0]["error"])
        self.assertIs(events[1]["data"]["success"], False)
        self.assertTrue(any("ZeroDivisionError" in line for line in logs.output))

    def test_an_oserror_in_a_handler_is_reported_not_swallowed(self) -> None:
        def handler(request, channel):
            raise OSError(28, "No space left on device")

        self.registry.register_action("test.disk", handler)
        with self.assertLogs("gentoo_installerd.server", level="ERROR"):
            events = self.call({"action": "test.disk"})
        self.assertEqual(events[0]["data"]["code"], "proto.internal")
        self.assertIn("No space left on device", events[0]["error"])

    def test_a_handler_that_raised_after_completing_does_not_send_a_second_completed(self) -> None:
        def handler(request, channel):
            channel.send(Event("completed", data={"success": True}))
            raise RuntimeError("late failure")

        self.registry.register_action("test.late", handler)
        with self.assertLogs("gentoo_installerd.server", level="ERROR"):
            events = self.call({"action": "test.late"})
        self.assertEqual(types(events), ["completed", "error"])

    def test_action_errors_carry_their_code_and_data(self) -> None:
        def handler(request, channel):
            raise ActionError("The device is busy", "rescue.busy_install", device="/dev/sda")

        self.registry.register_action("test.busy", handler)
        events = self.call({"action": "test.busy"})
        self.assertEqual(types(events), ["error", "completed"])
        self.assertEqual(events[0]["data"], {"code": "rescue.busy_install", "device": "/dev/sda"})

    def test_value_errors_are_bad_requests(self) -> None:
        def handler(request, channel):
            raise ValueError("device must be an absolute path")

        self.registry.register_action("test.value", handler)
        events = self.call({"action": "test.value"})
        self.assertEqual(events[0]["data"]["code"], "proto.bad_request")

    def test_a_client_that_leaves_mid_handler_does_not_log_a_traceback(self) -> None:
        started = threading.Event()
        finish = threading.Event()
        outcome: list[str] = []

        def handler(request, channel):
            started.set()
            finish.wait(5)
            try:
                for _ in range(500):
                    channel.send(Event("log", message="x" * 2000))
            except ChannelClosed:
                outcome.append("closed")

        self.registry.register_action("test.slow", handler)
        records: list[logging.LogRecord] = []
        handler_log = logging.Handler()
        handler_log.emit = records.append  # type: ignore[method-assign]
        logging.getLogger("gentoo_installerd").addHandler(handler_log)
        self.addCleanup(logging.getLogger("gentoo_installerd").removeHandler, handler_log)
        with self.connect() as client:
            client.sendall(b'{"version":1,"action":"test.slow"}\n')
            self.assertTrue(started.wait(5))
        finish.set()
        self.assertTrue(wait_for(lambda: outcome == ["closed"]))
        self.assertFalse([record for record in records if record.exc_info])

    def test_status_includes_the_registered_extensions(self) -> None:
        self.registry.add_status_provider("rescue", lambda: {"sessions": 2})
        data = self.call({"action": "status"})[0]["data"]
        self.assertEqual(data["rescue"], {"sessions": 2})
        self.assertEqual(data["status"], "idle")

    def test_the_manager_is_reachable_for_plugins(self) -> None:
        seen = []
        self.registry.register_action("test.manager", lambda request, channel: seen.append((channel.install_manager, channel.server.registry)))
        self.call({"action": "test.manager"})
        self.assertEqual(seen, [(self.manager, self.registry)])
        self.assertIs(self.registry.install_manager, self.manager)


if __name__ == "__main__":
    unittest.main()
