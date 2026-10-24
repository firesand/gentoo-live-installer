"""rescue.api.register(registry): the daemon protocol against a fake registry and channel."""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

from gentoo_installerd.rescue import FEATURE, api as rescue_api
from gentoo_installerd.rescue.hostenv import Host

from . import fakeroot as fr
from .apifake import ChannelClosed, FakeChannel, FakeRegistry
from .flow import uefi_gentoo_lab

PASSPHRASE = "very secret passphrase"


def registered(lab, **kwargs):
    registry = FakeRegistry()
    api = rescue_api.register(registry, manager=lab.manager(), enabled=True, **kwargs)
    return registry, api


def mounted(lab_factory, **options):
    lab, tree = uefi_gentoo_lab(lab_factory, **options)
    registry, api = registered(lab)
    scan = registry.call("rescue.scan", {"request_id": "q1"})
    candidate = [c for c in scan.result["candidates"] if c["device"] == "/dev/nvme0n1p5"][0]
    request = {"candidate": candidate["id"], "scan_id": scan.result["scan_id"]}
    dry = registry.call("rescue.mount", {**request, "dry_run": True})
    done = registry.call("rescue.mount", {**request, "plan_digest": dry.result["mount_plan"]["digest"]})
    return lab, tree, registry, api, done.result["session_id"]


def test_register_adds_every_action_with_the_feature(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    registry, api = registered(lab)
    assert FEATURE == "rescue.v1" and set(registry.actions) == set(rescue_api.ACTIONS) and set(registry.features.values()) == {"rescue.v1"}
    assert len(registry.hooks) == 1
    caps = api.capabilities()
    assert caps["api"] == 1 and caps["actions"] == list(rescue_api.ACTIONS) and "gentoo" in caps["repairs"] and "arch" not in caps["repairs"] and "arch" in caps["families"]


def test_the_api_is_off_outside_the_live_medium_by_default(lab_factory, tmp_path) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    off = FakeRegistry()
    assert rescue_api.register(off, manager=lab.manager(), host=Host(tmp_path / "plain-host")) is None and off.actions == {}
    live_host = lab.host_root
    on = FakeRegistry()
    assert rescue_api.register(on, manager=lab.manager(), host=Host(live_host)) is not None and on.actions  # the fixture command line contains cdroot
    forced = FakeRegistry()
    assert rescue_api.register(forced, manager=lab.manager(), enabled=lambda: True, host=Host(tmp_path / "plain-host")) is not None
    refused = FakeRegistry()
    assert rescue_api.register(refused, manager=lab.manager(), enabled=False, host=Host(live_host)) is None


def test_scan_streams_stage_events_then_one_result_and_one_completed(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    registry, _api = registered(lab)
    channel = registry.call("rescue.scan", {"request_id": "abc"})
    kinds = [event["type"] for event in channel.events]
    assert kinds[-2:] == ["result", "completed"] and kinds.count("completed") == 1 and "stage" in kinds
    assert channel.completed["data"]["success"] is True
    seqs = [event["seq"] for event in channel.events]
    assert seqs == sorted(seqs) and seqs[0] == 1 and len(set(seqs)) == len(seqs)
    assert {event["job_id"] for event in channel.events} == {channel.events[0]["job_id"]}
    assert all(event["data"].get("request_id") == "abc" for event in channel.events)
    assert channel.result["candidates"] and channel.result["environment"]["uefi"] is True
    assert all(event["version"] == 1 for event in channel.events)


def test_errors_are_one_error_event_and_a_failed_completed(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    registry, _api = registered(lab)
    channel = registry.call("rescue.mount", {"candidate": "cdeadbeef", "scan_id": "none", "request_id": "r9"})
    assert channel.error_event["error"] == "rescue.not_found" and channel.error_event["data"]["code"] == "rescue.not_found"
    assert channel.completed["data"] == {"success": False, "request_id": "r9"}
    assert [e["type"] for e in channel.events].count("error") == 1
    quick = registry.call("rescue.session", {"session_id": "r000000"})
    assert quick.error_event["error"] == "rescue.not_found" and quick.completed["data"]["success"] is False


def test_unexpected_exceptions_become_proto_internal(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    registry, api = registered(lab)
    api.manager.session_summary = lambda sid="": (_ for _ in ()).throw(KeyError("boom"))  # type: ignore[method-assign]
    channel = registry.call("rescue.session")
    assert channel.error_event["error"] == "proto.internal" and "boom" not in json.dumps(channel.events)
    assert channel.completed["data"]["success"] is False


def test_the_full_repair_through_the_protocol(lab_factory) -> None:
    lab, tree, registry, api, sid = mounted(lab_factory)
    plan = registry.call("rescue.plan", {"session_id": sid, "action_id": "boot.config"}).result
    assert plan["action"] == "boot.config" and plan["confirm"]["review"] is True
    ran = registry.call("rescue.run", {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True, "request_id": "run1"})
    steps = [event for event in ran.of_type("stage") if event["stage"] == "step"]
    assert steps and {s["data"]["status"] for s in steps} >= {"running", "completed"} and ran.result["outcome"] == "success"
    assert any(event["type"] == "log" for event in ran.events) and ran.completed["data"]["success"] is True
    assert all(event["data"].get("session_id") == sid for event in ran.events)
    info = registry.call("rescue.session", {"session_id": sid}).result
    assert info["mode"] == "rw" and info["state"] == "mounted_rw"
    report = registry.call("rescue.audit", {"session_id": sid}).result
    assert report["name"].endswith(f"{sid}.jsonl") and '"kind":"plan"' in report["text"] and '"kind":"exec"' in report["text"]
    unmounted = registry.call("rescue.unmount", {"session_id": sid})
    assert unmounted.result["leftovers"] == [] and unmounted.result["report"].startswith("/var/log/gentoo-rescue/")
    assert registry.call("rescue.session").result["sessions"] == []


def test_queries_work_in_a_read_only_session(lab_factory) -> None:
    lab, tree, registry, api, sid = mounted(lab_factory)
    for query in ("diag.summary", "boot.inventory", "fstab.show", "acct.users", "logs.sources", "efi.list", "logs.pstore", "logs.live_dmesg"):
        channel = registry.call("rescue.query", {"session_id": sid, "query": query})
        assert channel.result["query"] == query and channel.completed["data"]["success"] is True, query
    assert registry.call("rescue.query", {"session_id": sid, "query": "logs.tail", "params": {"path": "/var/log/messages", "lines": 5}}).result["data"]["lines"] == ["Oct 24 08:00:00 localhost kernel: boot"]
    bad = registry.call("rescue.query", {"session_id": sid, "query": "no.such"})
    assert bad.error_event["error"] == "rescue.bad_request"
    assert lab.system.chroot_calls == [] and registry.call("rescue.session", {"session_id": sid}).result["mode"] == "ro"


def test_a_vanished_client_does_not_stop_the_operation_and_can_reattach(lab_factory) -> None:
    lab, tree, registry, api, sid = mounted(lab_factory)
    plan = registry.call("rescue.plan", {"session_id": sid, "action_id": "boot.initramfs"}).result
    original = lab.system.run
    release = threading.Event()
    started = threading.Event()

    def slow(argv, **kwargs):
        if "dracut" in argv:
            started.set()
            release.wait(10)
        return original(argv, **kwargs)

    lab.system.run = slow  # type: ignore[method-assign]
    dying = FakeChannel(die_after=3)
    thread = threading.Thread(target=lambda: registry.call("rescue.run", {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True}, dying))
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive() and dying.closed  # the connection thread returned
    assert started.wait(10)
    job = api.manager.ops.latest(sid)
    assert job is not None and job.success is None  # still running without a client
    release.set()
    assert job.wait(10)
    seen = max(event["seq"] for event in dying.events)
    again = registry.call("rescue.events", {"job_id": job.id, "after_seq": seen})
    assert again.events[0]["seq"] == seen + 1 and again.completed["data"]["success"] is True
    assert again.of_type("result")[0]["data"]["outcome"] == "success"
    latest = registry.call("rescue.events", {"session_id": sid, "after_seq": 0})
    assert latest.events[0]["seq"] == 1 and latest.events[-1]["type"] == "completed"
    assert registry.call("rescue.events", {"job_id": "o0000000000"}).error_event["error"] == "rescue.not_found"


@pytest.mark.parametrize("failure", [OSError, TimeoutError, ConnectionResetError, BrokenPipeError, ChannelClosed])
def test_every_kind_of_send_failure_is_survived(lab_factory, failure) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    registry, api = registered(lab)
    channel = FakeChannel(die_after=1, error=failure)
    registry.call("rescue.scan", {}, channel)
    assert channel.closed
    job = api.manager.ops.latest()
    assert job is not None and job.wait(10) and job.success is True


def test_cancel_through_the_protocol(lab_factory) -> None:
    lab, tree, registry, api, sid = mounted(lab_factory)
    reply = registry.call("rescue.cancel", {"session_id": sid})
    assert reply.error_event["error"] == "rescue.not_found"


def test_install_and_rescue_exclude_each_other(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    state = {"reason": "gentoo installation is at the packages stage"}
    registry, api = registered(lab, install_busy=lambda: state["reason"])
    for name in ("rescue.scan", "rescue.unlock", "rescue.activate", "rescue.mount"):
        channel = registry.call(name, {"device": "/dev/nvme0n1p3", "candidate": "x"})
        assert channel.error_event["error"] == "rescue.busy_install" and "packages stage" in channel.error_event["message"], name
        assert channel.completed["data"]["success"] is False
    assert api.install_blocker() is None
    state["reason"] = ""
    lab2, tree, registry2, api2, sid = mounted(lab_factory)
    blocker = api2.install_blocker()
    assert blocker and "/dev/nvme0n1p5" in blocker and "unmount" in blocker
    registry2.call("rescue.unmount", {"session_id": sid})
    assert api2.install_blocker() is None


def test_the_shell_reply_carries_the_pty_master(lab_factory) -> None:
    import subprocess

    lab, tree, registry, api, sid = mounted(lab_factory)
    api.manager.shell_spawn = lambda argv, **kwargs: subprocess.Popen(["/bin/sh", "-c", "echo hello-from-terminal"], **kwargs)
    channel = registry.call("rescue.shell", {"session_id": sid, "confirm": True, "typed": "nvme0n1p5", "rows": 30, "cols": 100, "request_id": "t1"})
    assert len(channel.events) == 1 and channel.events[0]["type"] == "result"
    info = channel.events[0]["data"]
    assert info["mode"] == "chroot-ro" and info["pid"] > 0 and info["transcript"].endswith(".log") and info["request_id"] == "t1"
    (fd,) = channel.fds[0]
    deadline = time.time() + 5
    seen = b""
    import select

    while b"hello-from-terminal" not in seen and time.time() < deadline:
        if select.select([fd], [], [], 0.2)[0]:
            try:
                seen += os.read(fd, 100)
            except OSError:
                break
    os.close(fd)
    assert b"hello-from-terminal" in seen
    missing = registry.call("rescue.shell", {"session_id": sid})
    assert missing.error_event["error"] == "rescue.needs_confirmation"


def test_secrets_never_appear_in_events_or_logs(lab_factory) -> None:
    lab, tree, registry, api, sid = mounted(lab_factory)
    plan = registry.call("rescue.plan", {"session_id": sid, "action_id": "acct.password", "params": {"username": "root"}}).result
    ran = registry.call("rescue.run", {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True, "typed": plan["confirm"]["typed"], "secrets": {"password": PASSPHRASE}})
    assert ran.completed["data"]["success"] is True
    blob = json.dumps(ran.events) + registry.call("rescue.audit", {"session_id": sid}).result["text"] + json.dumps(registry.call("rescue.session", {"session_id": sid}).events)
    assert PASSPHRASE not in blob
    unlock = registry.call("rescue.unlock", {"device": "/dev/nvme0n1p3", "passphrase": PASSPHRASE})
    assert PASSPHRASE not in json.dumps(unlock.events) and unlock.error_event["error"] in {"rescue.not_luks", "rescue.live_medium"}


def test_recover_reports_leftovers_of_a_previous_daemon(lab_factory) -> None:
    lab, tree, registry, api, sid = mounted(lab_factory)
    registry2 = FakeRegistry()
    api2 = rescue_api.register(registry2, manager=lab.manager(), enabled=True)
    assert api2 is not None and [item["sid"] for item in api2.startup_orphans] == [sid] and api2.startup_orphans[0]["state"] == "alive"
    assert api2.status()["orphans"] == 1
    listing = registry2.call("rescue.recover").result["orphans"]
    assert listing[0]["sid"] == sid and listing[0]["mounts"]
    cleaned = registry2.call("rescue.recover", {"do": "cleanup", "sid": sid})
    assert cleaned.result["leftovers"] == [] and lab.system.mounted_targets() == [] and not any(lab.system.ro.values())
    assert registry2.call("rescue.recover").result["orphans"] == []
    assert registry2.call("rescue.recover", {"do": "adopt", "sid": "r000000"}).error_event["error"] == "rescue.not_found"
    assert registry2.call("rescue.recover", {"do": "explode"}).error_event["error"] == "rescue.bad_request"


def test_the_shutdown_hook_releases_sessions_without_force(lab_factory) -> None:
    lab, tree, registry, api, sid = mounted(lab_factory)
    registry.hooks[0]()
    assert lab.system.mounted_targets() == [] and api.manager.sessions == {} and not any(lab.system.ro.values())


def test_a_busy_session_refuses_a_second_operation(lab_factory) -> None:
    lab, tree, registry, api, sid = mounted(lab_factory)
    session = api.manager.session(sid)
    assert session.op_lock.acquire(blocking=False)
    try:
        plan = registry.call("rescue.plan", {"session_id": sid, "action_id": "boot.config"}).result
        channel = registry.call("rescue.run", {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True})
        assert channel.error_event["error"] == "rescue.busy"
        unmount = registry.call("rescue.unmount", {"session_id": sid})
        assert unmount.error_event["error"] == "rescue.busy"
    finally:
        session.op_lock.release()
