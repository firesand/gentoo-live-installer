"""gentoo-rescue-share: the ttyd command line and every protection the spec lists, with fakes (the VM harness checks the real TLS answer)."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from gentoo_rescue_ai import share

IP_OUT = """2: enp5s0    inet 192.168.1.23/24 brd 192.168.1.255 scope global dynamic enp5s0\\       valid_lft 86000sec
3: wlan0    inet 10.0.0.7/24 brd 10.0.0.255 scope global dynamic wlan0\\       valid_lft 3000sec
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever
4: usb0    inet 169.254.3.4/16 brd 169.254.255.255 scope global usb0\\       valid_lft forever
"""
ROUTE_OUT = "default via 10.0.0.1 dev wlan0 proto dhcp src 10.0.0.7 metric 600\n"


def test_the_password_is_three_pronounceable_words_and_not_repeated() -> None:
    seen = {share.make_password() for _ in range(200)}
    assert len(seen) == 200
    for password in seen:
        assert re.fullmatch(r"([bdfgklmnprstvz][aeiou]){2}(-([bdfgklmnprstvz][aeiou]){2}){2}", password), password
    assert 36 < 3 * math.log2((len(share.CONSONANTS) * len(share.VOWELS)) ** 2) < 38  # about 37 bits


def test_only_real_lan_addresses_count_and_the_default_route_wins() -> None:
    addresses = share.lan_addresses(IP_OUT)
    assert addresses == [("enp5s0", "192.168.1.23"), ("wlan0", "10.0.0.7")]  # not lo, not link-local
    assert share.default_route_interface(ROUTE_OUT) == "wlan0"
    assert share.choose_interface(addresses, None, "wlan0") == ("wlan0", "10.0.0.7")
    assert share.choose_interface(addresses, None, None) == ("enp5s0", "192.168.1.23")
    assert share.choose_interface(addresses, "enp5s0", "wlan0") == ("enp5s0", "192.168.1.23")
    with pytest.raises(share.ShareError, match="has no LAN address"):
        share.choose_interface(addresses, "usb0", None)
    with pytest.raises(share.ShareError, match="no LAN address"):
        share.choose_interface([], None, None)


def test_ttyd_gets_tls_a_password_one_client_the_origin_check_and_the_lan_interface() -> None:
    argv = share.ttyd_argv(ttyd="/usr/bin/ttyd", port=23456, interface="wlan0", cert="/c.pem", key="/k.pem", password="bako-fidu-meza", read_only=False)
    assert argv[0] == "/usr/bin/ttyd"
    pairs = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1) if argv[i] in {"-p", "-i", "-C", "-K", "-c", "-m"}}
    assert pairs == {"-p": "23456", "-i": "wlan0", "-C": "/c.pem", "-K": "/k.pem", "-c": "rescue:bako-fidu-meza", "-m": "1"}
    assert "-S" in argv and "-O" in argv and "-W" in argv
    assert argv[-4:] == ["tmux", "attach", "-t", "rescue"] and "-r" not in argv


def test_read_only_drops_the_writable_flag_and_attaches_read_only() -> None:
    argv = share.ttyd_argv(ttyd="ttyd", port=1, interface="eth0", cert="c", key="k", password="p-p-p", read_only=True)
    assert "-W" not in argv and argv[-5:] == ["tmux", "attach", "-r", "-t", "rescue"]


def test_the_certificate_is_a_fresh_ec_key_for_the_address_and_only_a_day() -> None:
    argv = share.openssl_argv("openssl", "/run/c.pem", "/run/k.pem", "10.0.0.7")
    assert argv[:3] == ["openssl", "req", "-x509"] and "subjectAltName=IP:10.0.0.7" in argv and argv[argv.index("-days") + 1] == "1" and "-nodes" in argv


@pytest.mark.skipif(not __import__("shutil").which("openssl"), reason="openssl is not installed")
def test_openssl_really_makes_the_certificate_and_a_fingerprint(tmp_path) -> None:
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    done = subprocess.run(share.openssl_argv("openssl", str(cert), str(key), "10.0.0.7"), capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert re.fullmatch(r"([0-9A-F]{2}:){31}[0-9A-F]{2}", share.fingerprint("openssl", str(cert)))


def test_idle_time_ignores_the_console_and_counts_only_phone_clients() -> None:
    clients = [("/dev/tty1", 1000.0), ("/dev/pts/7", 400.0)]
    assert share.idle_seconds(clients, now=1000.0, started=0.0) == 600.0  # the console client is busy, the phone is not
    assert share.idle_seconds([("/dev/tty1", 999.0)], now=1000.0, started=100.0) == 900.0  # no phone yet: idle since the start
    assert share.idle_seconds([("/dev/pts/7", 990.0)], now=1000.0, started=0.0) == 10.0


def state(minutes: int = 15, started: float = 0.0) -> share.State:
    return share.State(supervisor=1, ttyd=2, port=3, address="10.0.0.7", interface="wlan0", read_only=False, minutes=minutes, started=started, folder="/x", fingerprint="AA")


def test_the_share_stops_after_the_idle_time_when_ttyd_ends_or_the_session_ends() -> None:
    clock = {"t": 0.0}
    stops: list[str] = []
    reason = share.supervise(state(minutes=2), ttyd_alive=lambda: True, clients=lambda: [("/dev/pts/1", 30.0)], stop=stops.append, now=lambda: clock["t"],
                             sleep=lambda s: clock.update(t=clock["t"] + 60), poll=5)
    assert reason == "nothing happened for 2 minutes" and stops == [reason] and clock["t"] == 180
    assert share.supervise(state(), ttyd_alive=lambda: False, clients=lambda: [], stop=stops.append, now=lambda: 0.0, sleep=lambda s: None) == "ttyd ended"
    assert share.supervise(state(), ttyd_alive=lambda: True, clients=lambda: [], stop=stops.append, now=lambda: 0.0, sleep=lambda s: None, tmux_up=lambda: False) == "the rescue session ended"


def test_activity_keeps_the_share_alive() -> None:
    clock = {"t": 0.0, "n": 0}

    def sleep(_: float) -> None:
        clock["t"] += 60
        clock["n"] += 1

    stops: list[str] = []
    share.supervise(state(minutes=5), ttyd_alive=lambda: clock["n"] < 20, clients=lambda: [("/dev/pts/1", clock["t"])], stop=stops.append, now=lambda: clock["t"], sleep=sleep)
    assert stops == ["ttyd ended"]  # 20 minutes of constant activity never hit the 5 minute idle limit


def test_stop_only_kills_processes_that_are_really_ttyd_and_the_share_and_removes_the_key(tmp_path) -> None:
    (tmp_path / "cert.pem").write_text("c")
    (tmp_path / "key.pem").write_text("k")
    mine = share.State(supervisor=os.getpid() + 100000, ttyd=os.getpid(), port=1, address="a", interface="i", read_only=False, minutes=1, started=0, folder=str(tmp_path), fingerprint="")
    (tmp_path / "state.json").write_text(json.dumps(mine.__dict__))
    killed: list[tuple[int, int]] = []
    lines: list[str] = []
    # ttyd is not in this test process's command line, and the supervisor pid does not exist: nothing may be killed
    assert share.stop_share(tmp_path, say=lines.append, kill=lambda pid, sig: killed.append((pid, sig))) is False
    assert killed == [] and lines == ["No share was running."]
    assert not (tmp_path / "key.pem").exists() and not (tmp_path / "cert.pem").exists() and not (tmp_path / "state.json").exists()


def test_stop_terminates_a_matching_ttyd_process(tmp_path) -> None:
    process = subprocess.Popen(["bash", "-c", "exec -a ttyd sleep 30"])
    try:
        (tmp_path / "state.json").write_text(json.dumps(share.State(supervisor=1, ttyd=process.pid, port=1, address="a", interface="i", read_only=False, minutes=1, started=0, folder="", fingerprint="").__dict__))
        for _ in range(50):
            if share.alive(process.pid, "ttyd"):
                break
            __import__("time").sleep(0.05)
        lines: list[str] = []
        assert share.stop_share(tmp_path, say=lines.append) is True and lines == ["The share was stopped."]
        process.wait(5)
    finally:
        if process.poll() is None:
            process.kill()


def test_the_state_folder_is_private(tmp_path) -> None:
    folder = share.state_dir({}, home=str(tmp_path))
    assert folder == tmp_path / ".cache" / "gentoo-rescue-share" or folder == Path("/run/gentoo-rescue-share")
    if folder != Path("/run/gentoo-rescue-share"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


class FakeRun:
    """Stands in for subprocess.run / Popen so start() runs without ttyd, tmux or a network."""

    def __init__(self, addresses: str = IP_OUT, session: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.addresses, self.session = addresses, session

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        out = ""
        code = 0
        if argv[0] == "ip" and "addr" in argv:
            out = self.addresses
        elif argv[0] == "ip" and "route" in argv:
            out = ROUTE_OUT
        elif argv[1:2] == ["has-session"]:
            code = 0 if self.session else 1
        elif argv[0].endswith("openssl") and argv[1] == "req":
            Path(argv[argv.index("-out") + 1]).write_text("CERT")
            Path(argv[argv.index("-keyout") + 1]).write_text("KEY")
        elif argv[0].endswith("openssl") and argv[1] == "x509":
            out = "sha256 Fingerprint=AA:BB:CC\n"
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    def popen(self, argv, **kwargs):
        self.calls.append(list(argv))

        class Process:
            pid = 4242

            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                pass

        return Process()


@pytest.fixture
def fake_share(monkeypatch, tmp_path):
    fake = FakeRun()
    monkeypatch.setattr(share.subprocess, "run", fake.run)
    monkeypatch.setattr(share.subprocess, "Popen", fake.popen)
    monkeypatch.setattr(share.time, "sleep", lambda s: None)
    monkeypatch.setattr(share, "state_dir", lambda *a, **k: tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(share, "qr_text", lambda url: "<QR " + url + ">")
    monkeypatch.setattr(share, "supervise", lambda *a, **k: "stub")
    monkeypatch.setattr(share, "free_port", lambda *a, **k: 23456)
    return fake


def args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**{"read_only": False, "interface": None, "minutes": 15, **kw})


def started(fake, tmp_path, **kw):
    lines: list[str] = []
    code = share.start(args(**kw), say=lines.append, which=lambda n: f"/usr/bin/{n}", foreground=True)
    return code, "\n".join(lines)


def test_start_shows_qr_password_fingerprint_and_the_phone_can_never_approve(fake_share, tmp_path) -> None:
    code, text = started(fake_share, tmp_path)
    assert code == 0
    ttyd = next(c for c in fake_share.calls if c[0] == "/usr/bin/ttyd")
    password = ttyd[ttyd.index("-c") + 1].split(":", 1)[1]
    assert f"<QR https://rescue:{password}@10.0.0.7:{ttyd[ttyd.index('-p') + 1]}/>" in text  # the default route's interface, wlan0
    assert ttyd[ttyd.index("-i") + 1] == "wlan0" and "-W" in ttyd and "CAN TYPE" in text
    assert "AA:BB:CC" in text and password in text and "NEVER approve a root command" in text and "gentoo-rescue-share stop" in text and "self-signed" in text
    assert ["/usr/bin/tmux", "set-option", "-t", "rescue", "window-size", "latest"] in fake_share.calls


def test_start_read_only_says_view_only(fake_share, tmp_path) -> None:
    _, text = started(fake_share, tmp_path, read_only=True, interface="enp5s0")
    ttyd = next(c for c in fake_share.calls if c[0] == "/usr/bin/ttyd")
    assert "-W" not in ttyd and "-r" in ttyd and "VIEW ONLY" in text and ttyd[ttyd.index("-i") + 1] == "enp5s0"


def test_the_key_is_private_and_removed_when_the_share_ends(fake_share, tmp_path) -> None:
    started(fake_share, tmp_path)
    assert not (tmp_path / "key.pem").exists() and not (tmp_path / "state.json").exists()  # foreground mode ends at once because supervise is stubbed


def test_start_refuses_without_a_lan_address_without_a_session_and_as_root(fake_share, tmp_path, monkeypatch) -> None:
    fake_share.addresses = "1: lo    inet 127.0.0.1/8 scope host lo\n"
    with pytest.raises(share.ShareError, match="no LAN address"):
        started(fake_share, tmp_path)
    fake_share.addresses, fake_share.session = IP_OUT, False
    with pytest.raises(share.ShareError, match="no rescue session"):
        started(fake_share, tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(share.ShareError, match="never runs as root"):
        started(fake_share, tmp_path)
    assert not any(c[0].endswith("ttyd") for c in fake_share.calls)


def test_a_missing_ttyd_is_said_plainly(fake_share, tmp_path) -> None:
    with pytest.raises(share.ShareError, match="ttyd is not installed"):
        share.start(args(), say=lambda s: None, which=lambda n: None if n == "ttyd" else f"/usr/bin/{n}", foreground=True)


def test_the_minutes_are_bounded_and_the_command_line_parses() -> None:
    assert share.parse(["--minutes", "30", "--read-only"]).read_only is True
    assert share.parse(["stop"]).action == "stop"
    assert share.main(["--minutes", "0"]) == 2


def test_the_share_listens_on_a_local_network_only() -> None:
    """A public address would put the agents' shell on the internet, behind nothing but the password."""
    assert all(share.is_local(a) for a in ("10.1.2.3", "172.16.0.9", "172.31.255.1", "192.168.1.23", "100.64.0.5", "100.127.255.1"))
    assert not any(share.is_local(a) for a in ("8.8.8.8", "172.32.0.1", "100.128.0.1", "203.0.113.7", "not-an-address"))
    listed = [("eth0", "203.0.113.7"), ("wlan0", "192.168.1.23")]
    assert share.pick_address(listed, None, "eth0") == ("wlan0", "192.168.1.23"), "the default route is public: the local one is used"
    with pytest.raises(share.ShareError, match="on the internet, not on a local network"):
        share.pick_address([("eth0", "203.0.113.7")], None, "eth0")
    with pytest.raises(share.ShareError, match="203.0.113.7 .eth0. is on the internet"):
        share.pick_address(listed, "eth0", None)
    with pytest.raises(share.ShareError, match="no LAN address"):
        share.pick_address([], None, None)
    assert share.lan_addresses("2: eth0    inet 203.0.113.7/24 brd 203.0.113.255 scope global eth0\n") == []


def test_the_background_supervisor_watches_ttyd_by_its_pid(tmp_path, monkeypatch) -> None:
    """The supervisor is forked after ttyd started, so it is not ttyd's parent and Popen.poll() would say "ended" at once (the first
    VM run left ttyd running with no state and no way to stop it). It must use the pid and the command line instead."""
    import inspect
    import signal as signals

    source = inspect.getsource(share.main) if hasattr(share, "main") else ""
    body = "".join(inspect.getsource(f) for f in (getattr(share, n) for n in dir(share)) if inspect.isfunction(f) and "os.fork()" in inspect.getsource(f))
    after_fork = body[body.index("os.fork()"):]
    assert "process.poll()" not in after_fork and 'alive(process.pid, "ttyd")' in after_fork and "terminate_ttyd(process.pid)" in after_fork, source[:0]
    killed = []
    monkeypatch.setattr(share, "alive", lambda pid, needle: pid == 4242 and needle == "ttyd")
    share.terminate_ttyd(4242, kill=lambda pid, sig: killed.append((pid, sig)))
    share.terminate_ttyd(5151, kill=lambda pid, sig: killed.append((pid, sig)))  # not ttyd (any more): left alone
    assert killed == [(4242, signals.SIGTERM)]
