"""Log access: a fixed catalogue, compressed and paged reads, hostile symlinks, the journal behind a confirmation, and where the saved report goes."""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import shutil
import time
from pathlib import Path

import pytest

from gentoo_installerd.rescue import logs
from gentoo_installerd.rescue.errors import RescueError

from . import fakeroot as fr
from .flow import mount_through_the_manager, plan_and_run, uefi_gentoo_lab, unmount

DEVICE = "/dev/nvme0n1p5"


def numbered(count: int, prefix: str = "line") -> str:
    return "".join(f"{prefix} {number}\n" for number in range(1, count + 1))


def logs_lab(lab_factory, **options):
    lab, tree = uefi_gentoo_lab(lab_factory, **options)
    fr.put(tree, "var/log/messages", numbered(500))
    (tree / "var/log/messages-20260101.gz").write_bytes(gzip.compress(numbered(30, "old").encode()))
    (tree / "var/log/messages.2.xz").write_bytes(lzma.compress(numbered(20, "older").encode()))
    (tree / "var/log/messages.3.bz2").write_bytes(bz2.compress(numbered(10, "oldest").encode()))
    fr.put(tree, "var/log/emerge.log", numbered(40, "emerge"))
    fr.put(tree, "var/log/portage/elog/summary.log", numbered(5, "elog"))
    fr.put(tree, "var/log/apt/history.log", numbered(5, "apt"))  # the log of another distribution's package manager is not in the catalogue
    fr.put(tree, "var/log/secret.txt", "not a catalogued log\n")
    fr.put(tree, "var/log/btmp", "failed logins\n")
    fr.put(tree, "var/log/journal/0123456789abcdef0123456789abcdef/system.journal", "binary")
    return lab, tree


def tail(manager, sid: str, path: str, **params) -> dict:
    return manager.query({"session_id": sid, "query": "logs.tail", "params": {"path": path, **params}})["data"]


def test_sources_list_the_catalogue_with_rotated_copies_and_the_journal(lab_factory) -> None:
    lab, _ = logs_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    sources = manager.query({"session_id": sid, "query": "logs.sources"})["data"]
    by_path = {item["path"]: item for item in sources["files"]}
    assert {"/var/log/messages", "/var/log/messages-20260101.gz", "/var/log/messages.2.xz", "/var/log/messages.3.bz2", "/var/log/emerge.log", "/var/log/portage/elog/summary.log"} <= set(by_path)
    assert by_path["/var/log/messages-20260101.gz"]["compressed"] is True and by_path["/var/log/messages"]["compressed"] is False
    assert "/var/log/secret.txt" not in by_path and "/var/log/btmp" not in by_path and "/var/log/apt/history.log" not in by_path  # only catalogued logs are offered
    assert sources["journal"] == [{"path": "/var/log/journal/0123456789abcdef0123456789abcdef", "files": 1}] and sources["persistent_journal"] is True


def test_tail_reads_plain_and_compressed_logs(lab_factory) -> None:
    lab, _ = logs_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    assert tail(manager, sid, "/var/log/messages", lines=3)["lines"] == ["line 498", "line 499", "line 500"]
    assert tail(manager, sid, "/var/log/messages-20260101.gz", lines=2)["lines"] == ["old 29", "old 30"]
    assert tail(manager, sid, "/var/log/messages.2.xz", lines=2)["lines"] == ["older 19", "older 20"]
    assert tail(manager, sid, "/var/log/messages.3.bz2", lines=2)["lines"] == ["oldest 9", "oldest 10"]
    assert tail(manager, sid, "/var/log/portage/elog/summary.log", lines=50)["lines"] == [f"elog {n}" for n in range(1, 6)]
    with pytest.raises(RescueError) as err:
        tail(manager, sid, "/var/log/apt/history.log")
    assert err.value.short == "safety_violation"


def test_tail_pages_backwards_and_bounds_its_answer(lab_factory) -> None:
    lab, _ = logs_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    first = tail(manager, sid, "/var/log/messages", lines=200)
    assert first["lines"][0] == "line 301" and first["lines"][-1] == "line 500" and first["more"] is True and first["next_offset"] == 200
    second = tail(manager, sid, "/var/log/messages", lines=200, offset=first["next_offset"])
    assert second["lines"][0] == "line 101" and second["lines"][-1] == "line 300" and second["more"] is True
    last = tail(manager, sid, "/var/log/messages", lines=200, offset=second["next_offset"])
    assert last["lines"][0] == "line 1" and last["lines"][-1] == "line 100" and last["more"] is False
    assert len(tail(manager, sid, "/var/log/messages", lines=999999)["lines"]) <= 2000  # the request cannot ask for more than the cap


def test_a_huge_log_is_read_from_its_end_only(lab_factory) -> None:
    lab, tree = logs_lab(lab_factory)
    big = tree / "var/log/emerge.log"
    with open(big, "wb") as handle:
        handle.truncate(300 * 1024 * 1024)  # a sparse 300 MiB file: reading it whole would be noticed
        handle.seek(0, os.SEEK_END)
        handle.write(b"\nthe last line\n")
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    started = time.monotonic()
    answer = tail(manager, sid, "/var/log/emerge.log", lines=1)
    assert answer["lines"] == ["the last line"] and answer["size"] > 300 * 1024 * 1024 and time.monotonic() - started < 5
    assert len(answer["text"]) <= logs.MAX_TAIL_BYTES


def test_a_decompression_bomb_is_cut_off(lab_factory) -> None:
    lab, tree = logs_lab(lab_factory)
    (tree / "var/log/messages-20260102.gz").write_bytes(gzip.compress(b"A" * 64 * 1024 * 1024 + b"\nend\n"))
    (tree / "var/log/messages.9.xz").write_bytes(lzma.compress(b"B" * 64 * 1024 * 1024))
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    for name in ("/var/log/messages-20260102.gz", "/var/log/messages.9.xz"):
        answer = tail(manager, sid, name, lines=5)
        assert len(answer["text"]) <= logs.MAX_TAIL_BYTES and len(answer["lines"]) <= 5


@pytest.mark.parametrize("path", ["/etc/shadow", "/etc/passwd", "/var/log/btmp", "/var/log/secret.txt", "/var/log/../../etc/shadow", "/var/log/journal/0123456789abcdef0123456789abcdef/../../../../etc/shadow",
                                  "/boot/grub/grub.cfg", "/", "", "/var/log", "/proc/self/environ"])
def test_only_catalogued_logs_are_served(lab_factory, path: str) -> None:
    lab, _ = logs_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    with pytest.raises(RescueError) as err:
        tail(manager, sid, path)
    assert err.value.short in {"safety_violation", "not_found"}


def test_symlinks_in_the_log_directory_cannot_leave_the_system(lab_factory, tmp_path: Path) -> None:
    lab, tree = logs_lab(lab_factory)
    outside = tmp_path / "live-host-secret"
    outside.write_text("the live system's secret\n")
    (tree / "etc/shadow").write_text("root:$6$hash:19000:::\n")
    for name, target in {"messages": "/etc/shadow", "kern.log": "../../etc/shadow", "syslog": "//etc/shadow", "daemon.log": "../../../../../../../etc/shadow",
                         "user.log": str(outside), "errors.log": "/var/log/../../etc/shadow", "boot.log": "/proc/self/environ"}.items():
        link = tree / "var/log" / name
        link.unlink(missing_ok=True)
        link.symlink_to(target)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    for name in ("messages", "kern.log", "syslog", "daemon.log", "user.log", "errors.log", "boot.log"):
        with pytest.raises(RescueError) as err:
            tail(manager, sid, f"/var/log/{name}")
        assert err.value.short in {"safety_violation", "not_found"}, name
        text = str(err.value.message) + str(err.value.to_data())
        assert "hash" not in text and "live system's secret" not in text


def test_a_log_symlinked_to_another_catalogued_log_is_followed_inside_the_system(lab_factory) -> None:
    lab, tree = logs_lab(lab_factory)
    link = tree / "var/log/syslog"
    link.unlink(missing_ok=True)
    link.symlink_to("/var/log/messages")  # absolute text means "inside this system", not the live system
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    answer = tail(manager, sid, "/var/log/syslog", lines=1)
    assert answer["lines"] == ["line 500"] and answer["path"] == "/var/log/messages"


def journal_lab(lab_factory):
    lab, tree = logs_lab(lab_factory)
    fr.put(tree, "usr/bin/journalctl", "#!/bin/sh\n", mode=0o755)
    lab.system.chroot_effects["journalctl"] = lambda system, root, command, extra: (0, "Oct 24 08:00:00 host kernel: first\nOct 24 08:00:01 host kernel: second\n")
    return lab, tree


def test_the_journal_needs_a_confirmation_that_names_what_runs(lab_factory) -> None:
    lab, _ = journal_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    with pytest.raises(RescueError) as err:
        manager.query({"session_id": sid, "query": "logs.journal", "params": {}})
    assert err.value.short == "needs_confirmation" and "own journalctl program as root" in err.value.message and not lab.system.chroot_calls
    answer = manager.query({"session_id": sid, "query": "logs.journal", "params": {"confirm_exec": True, "lines": 50, "priority": "err", "kernel": True}})["data"]
    assert answer["lines"][-1].endswith("kernel: second") and answer["rc"] == 0
    root, env, command = lab.system.chroot_calls[-1]
    assert command[0] == "journalctl" and "--directory=/var/log/journal" in command and "-p" in command and command[command.index("-p") + 1] == "err" and "-k" in command
    assert env["PATH"].startswith("/usr/sbin:/usr/bin:/sbin:/bin") and "PYTHONPATH" not in env


def test_the_journal_refuses_bad_input_and_inspect_only_sessions(lab_factory) -> None:
    lab, _ = journal_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    for bad in ({"priority": "err; rm -rf /"}, {"priority": "--help"}):
        with pytest.raises(RescueError) as err:
            manager.query({"session_id": sid, "query": "logs.journal", "params": {"confirm_exec": True, **bad}})
        assert err.value.short == "bad_request"
    assert not lab.system.chroot_calls
    unmount(manager, sid)
    manager2, sid2, _ = mount_through_the_manager(lab, candidate_device=DEVICE, allow_write=False)
    with pytest.raises(RescueError) as err:
        manager2.query({"session_id": sid2, "query": "logs.journal", "params": {"confirm_exec": True}})
    assert err.value.short == "needs_write" and not lab.system.chroot_calls  # no program of an inspect-only system ever runs (review R9)


def test_pstore_and_the_live_kernel_log_do_not_touch_the_system(lab_factory) -> None:
    lab, tree = logs_lab(lab_factory)
    pstore = lab.host_root / "sys/fs/pstore"
    pstore.mkdir(parents=True, exist_ok=True)
    (pstore / "dmesg-ramoops-0").write_text("Kernel panic - not syncing\n")
    lab.system.script(("dmesg",), "[    1.0] some warning\n")
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    records = manager.query({"session_id": sid, "query": "logs.pstore"})["data"]["entries"]
    assert records == [{"name": "dmesg-ramoops-0", "size": 27, "text": "Kernel panic - not syncing\n"}]
    live = manager.query({"session_id": sid, "query": "logs.live_dmesg"})["data"]
    assert live["lines"] == ["[    1.0] some warning"] and "this live session" in live["note"]
    assert not lab.system.chroot_calls


def repaired_session(lab_factory, *, var_log_link: str | None = None):
    lab, tree = logs_lab(lab_factory)
    if var_log_link:
        shutil.rmtree(tree / "var/log")
        (tree / "var/log").symlink_to(var_log_link)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    plan_and_run(manager, sid, "session.enter_write")
    return lab, tree, manager, sid


def test_the_report_is_saved_in_the_repaired_system_with_private_permissions(lab_factory) -> None:
    lab, tree, manager, sid = repaired_session(lab_factory)
    result = unmount(manager, sid)
    assert result["leftovers"] == [] and result["report"].startswith("/var/log/gentoo-rescue/") and result["report"].endswith(f"-{sid}.jsonl")
    saved = tree / result["report"].lstrip("/")
    assert saved.is_file() and oct(saved.stat().st_mode & 0o777) == "0o600" and '"kind":"plan"' in saved.read_text()


def test_no_report_is_written_where_the_next_reboot_would_lose_it(lab_factory) -> None:
    lab, tree, manager, sid = repaired_session(lab_factory, var_log_link="/run/log")  # /var/log -> /run/log is memory in the repaired system and in this session
    result = unmount(manager, sid)
    assert result["leftovers"] == [] and "report" not in result
    assert not list(tree.rglob("*.jsonl")) and (tree / "var/log").is_symlink()


def test_a_read_only_session_saves_no_report(lab_factory) -> None:
    lab, tree = logs_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device=DEVICE)
    result = unmount(manager, sid)
    assert "report" not in result and not (tree / "var/log/gentoo-rescue").exists()
