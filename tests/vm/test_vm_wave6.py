"""scripts/vm_wave6.py: the AI rescue scenarios against a scripted stick (tests/vm/stick.py). No VM is started.

Each check is shown twice: it passes on the correct stick, and it fails, with the right words, when the stick is wrong in exactly that way.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile

import pytest

import vm_wave6 as w6
from tests.vm.stick import FINGERPRINT, PASSWORD, PRINTED_HEX, SHARE_PASSWORD, Stick


def probes(stick: Stick, vt: int = 9):
    results = {}
    for name, command, parser, as_ai in w6.security_probes(vt):
        code, text = stick.run(command if not as_ai else w6.as_ai(command), name)
        results[name] = parser(code, text)
    return results


# --- running as rescue-ai ----------------------------------------------------------------------------------------------------------------------

def test_commands_for_rescue_ai_start_in_its_home_with_a_clean_environment_and_its_own_groups():
    command = w6.as_ai("echo hi")
    assert command.startswith("setpriv --reuid=rescue-ai --regid=rescue-ai --init-groups --reset-env env -i -C /home/rescue-ai ")
    assert "HOME=/home/rescue-ai" in command and "sudo" not in command
    assert w6.in_background("true").endswith("& echo started")


# --- the boot entry ------------------------------------------------------------------------------------------------------------------------------

def test_the_launcher_is_answered_with_enter_then_n_and_typing_stops_once_the_session_is_up():
    stick = Stick(session_after_keys=2)
    up, typed = w6.drive_launcher(stick.hands(), timeout=100, settle=1)
    assert up and typed == ["<Enter>", "n"] and stick.typed == ["", "n"]


def test_a_session_that_is_up_early_gets_no_more_keys():
    stick = Stick(session_after_keys=0)
    up, typed = w6.drive_launcher(stick.hands(), timeout=100, settle=1)
    assert up and typed == [] and stick.typed == [], "a keystroke after the session is up would land in the agent window"


def test_a_session_that_never_comes_is_reported_after_the_timeout_not_forever():
    stick = Stick(session_after_keys=99)
    up, typed = w6.drive_launcher(stick.hands(), timeout=30, settle=5)
    assert not up and typed == ["<Enter>", "n"]


def test_the_correct_session_passes():
    stick = Stick(session_after_keys=0)
    assert probes(stick)["ai-session"].success


@pytest.mark.parametrize("change, words", [
    (dict(window_names=("agent", "shell", "approvals")), "approval window"),
    (dict(window_names=("shell",)), "exactly"),
    (dict(pane_tty="/dev/tty3"), "not on a pty"),
    (dict(pane_path="/mnt/gentoo"), "starts in /mnt/gentoo"),
    (dict(client_tty="/dev/pts/9"), "no tmux client is attached on /dev/tty1"),
    (dict(tmux_owner="root"), "belong to root"),
])
def test_a_wrong_session_names_what_is_wrong(change, words):
    result = probes(Stick(**change))["ai-session"]
    assert not result.success and words in result.error


def test_a_missing_session_or_garbled_output_is_could_not_tell_never_a_pass():
    assert not w6.parse_session_probe(0, "").success
    assert "could not tell" in w6.parse_session_probe(0, "").error


# --- the security design --------------------------------------------------------------------------------------------------------------------------

def test_the_correct_stick_passes_every_security_probe():
    results = probes(Stick())
    assert all(result.success for result in results.values()), {k: v.error for k, v in results.items() if not v.success}


@pytest.mark.parametrize("probe, change, words", [
    ("yama", dict(yama=False), "Yama is not in the kernel's LSM list"),
    ("yama", dict(ptrace=0), "ptrace_scope is '0'"),
    ("ai-identity", dict(groups="rescue-ai tty video"), "group(s) tty, video"),
    ("ai-identity", dict(sudo_text="User rescue-ai may run the following commands on gentoo-vm:\n    (ALL) NOPASSWD: ALL\n"), "no sudo rule"),
    ("ai-consoles", dict(console_open=True), "can open /dev/tty1"),
    ("ai-consoles", dict(secrets_readable=True), "can read /run/gentoo-rescue/do-password"),
    ("approver", dict(socket_mode="root:root 666"), "expected 'root:rescue-ai 660'"),
    ("ai-cwd", dict(cwd_in_target=True), "inside the target disk"),
])
def test_each_security_rule_fails_when_the_stick_breaks_it(probe, change, words):
    result = probes(Stick(**change))[probe]
    assert not result.success and words in result.error


def test_a_console_that_does_not_exist_cannot_prove_it_is_closed():
    report = {"open": {"/dev/tty1": "refused: Permission denied", "/dev/tty9": "missing", "/dev/tty0": "missing", "/dev/console": "missing"},
              "chvt": "refused (exit 1)", "tiocsti": "", "secrets": {}, "sudo": "refused", "active": "tty1"}
    result = w6.parse_console_probe(0, json.dumps(report), 9)
    assert not result.success and "/dev/tty9 does not exist" in result.error


def test_the_approver_probe_does_not_count_its_own_shell_as_the_approver():
    assert "'[g]entoo-rescue-approver'" in w6.APPROVER_COMMAND
    only_the_shell = ("9999 bash -c pgrep -af '[g]entoo-rescue-approver'; echo ---; stat -c ... gentoo-rescue-approver\n---\n"
                      "root:rescue-ai 660 /run/gentoo-rescue/do.sock\nroot:root 600 /run/gentoo-rescue/do-password\n---\n")
    assert w6.approver_processes(only_the_shell) == []
    result = w6.parse_approver_probe(0, only_the_shell)
    assert not result.success and "not running" in result.error


def test_the_stick_without_an_approver_fails_the_probe():
    stick = Stick(approver_running=False)
    code, text = stick.run(w6.APPROVER_COMMAND, "approver")
    assert not w6.parse_approver_probe(code, text).success


def test_the_approver_vt_comes_from_its_command_line_then_the_file_then_the_default():
    assert w6.approver_vt("412 gentoo-rescue-approver --vt 8 --socket x\n---\nroot:root 600 x\n---\n7\n") == 8
    assert w6.approver_vt("---\n\n---\n7\n") == 7
    assert w6.approver_vt("") == 9


# --- gentoo-rescue-do ----------------------------------------------------------------------------------------------------------------------------

def run_do(**change):
    stick = Stick(**change)
    return stick, w6.do_flow(stick.hands(), stick.approver_vt)


def test_the_do_flow_passes_on_a_correct_approver_and_types_only_where_it_should():
    stick, report = run_do()
    assert report.success, report.problems
    assert stick.typed == [PASSWORD, ""], "the password for exactly one command, then bare Enter for the next"
    assert [event.get("answer") for event in stick.audit if event["event"] == "answer"] == ["approved", "wrong"]
    assert not [event for event in stick.audit if event["event"] == "refused"], "no request ran into the approver's quiet period"
    assert PASSWORD not in json.dumps(report.to_dict()), "the password never reaches the report"


def test_the_password_is_hidden_from_any_detail_a_failure_prints():
    stick = Stick(audit_has_password=True)
    hands = stick.hands()
    report = w6.do_flow(hands, 9)
    assert not report.success and "in the audit log" in " ".join(report.problems)
    assert PASSWORD not in hands.hide(f"leaked {PASSWORD}")


def test_a_password_the_harness_cannot_type_is_reported_before_anything_is_started():
    stick, report = run_do(password="pässwörd-1")
    assert not report.success and "cannot type" in report.problems[0] and "letters, digits and -" in report.problems[0]
    assert stick.typed == [] and stick.requests == {}


def test_an_approver_that_never_takes_over_the_console_fails():
    _stick, report = run_do(approver_switches=False)
    assert any("takes over the console" in problem for problem in report.problems)


def test_a_command_that_runs_without_asking_fails():
    _stick, report = run_do(runs_without_approval=True)
    assert any("takes over the console" in problem or "nothing ran before the approval" in problem for problem in report.problems)


def test_an_approval_that_carries_over_to_the_next_request_fails():
    _stick, report = run_do(carries_approval=True)
    assert not report.success


def test_a_password_in_the_audit_log_fails():
    _stick, report = run_do(audit_has_password=True)
    assert any("password is in the audit log" in problem for problem in report.problems)


def test_the_audit_checker_wants_the_two_requests_and_one_exit():
    events = [{"event": "request", "argv": ["/usr/bin/id", "-u"], "cwd": "/home/rescue-ai"}] * 2
    events += [{"event": "answer", "answer": "approved"}, {"event": "answer", "answer": "wrong"}, {"event": "exit", "status": 0}]
    assert w6.audit_problems(events, ["approved", "wrong"], "pw", "") == []
    assert w6.audit_problems(events[:-1], ["approved", "wrong"], "pw", "")


def test_the_stick_models_the_approvers_quiet_period_so_a_refusal_first_fails():
    """The old order (bare Enter, then the password at once) is turned away by the real approver before it asks: the stick says so too."""
    stick = Stick()
    hands = stick.hands()
    for number, answer in ((1, ""), (2, PASSWORD)):
        hands.run(w6.do_start_command(number, hands.home), f"do-start-{number}")
        if stick.active == "tty9":
            hands.type_console(answer)
    assert stick.requests[2]["rc"] == 77 and stick.active == "tty1"
    assert [e["event"] for e in stick.audit].count("refused") == 1
    assert [e["event"] for e in stick.audit].count("request") == 1
    # after the quiet period (a bit over BACKOFF_START) a new request is asked again
    stick.clock.sleep(6)
    hands.run(w6.do_start_command(3, hands.home), "do-start-3")
    assert stick.active == "tty9" and stick.requests[3]["state"] == "waiting"


def test_an_approval_ends_the_quiet_period():
    stick = Stick()
    hands = stick.hands()
    hands.run(w6.do_start_command(1, hands.home), "do-start-1")
    hands.type_console(PASSWORD)
    hands.run(w6.do_start_command(2, hands.home), "do-start-2")
    assert stick.active == "tty9"


# --- gentoo-rescue-mount -------------------------------------------------------------------------------------------------------------------------

def test_mount_passes_on_a_read_only_gentoo_at_mnt_gentoo_and_unmounts():
    stick = Stick()
    report = w6.mount_flow(stick.hands())
    assert report.success, report.problems
    assert not stick.mounted


@pytest.mark.parametrize("change, words", [
    (dict(mount_ro=False), "not read-only"),
    (dict(mount_rc=0), "a file could be created"),
    (dict(cwd_in_target=True), "inside the target disk"),
])
def test_mount_failures_are_named(change, words):
    report = w6.mount_flow(Stick(**change))
    assert not report.success and words in " ".join(report.problems)


def test_a_mount_that_says_read_write_or_forgets_the_hints_fails():
    proof = "/mnt/rescue/r1\n---\nro\n---\nGentoo\n---\nrc=1\n"
    text = "Your Gentoo system is open at /mnt/gentoo (read-write), session r1."
    problems = w6.mount_problems(0, text, proof)
    assert any("read-write" in problem for problem in problems) and any("gentoo-chroot" in problem for problem in problems)
    assert any("--unmount" in problem for problem in problems)
    assert any("could not tell" in problem for problem in w6.mount_problems(0, "ok", "garbage"))


# --- gentoo-rescue-share -------------------------------------------------------------------------------------------------------------------------

def run_share(**change):
    stick = Stick(**change)
    return stick, w6.share_flow(stick.hands(), stick.forward, stick.fetch)


def test_the_share_password_leaves_the_logs_of_the_command_that_printed_it():
    stick = Stick()
    logs: dict[str, str] = {}
    base = stick.hands()

    def run(command, label):
        code, text = stick.run(command, label)
        logs[label] = logs.get(label, "") + hands.hide(text)      # like the real run(): hides what is known by now; what the harness wrote to disk before it knew the password
        return code, text

    def relog(label):
        logs[label] = hands.hide(logs[label])

    hands = w6.Hands(run, base.type_console, base.password, base.sleep, base.now, relog=relog)
    report = w6.share_flow(hands, stick.forward, stick.fetch)
    assert report.success, report.problems
    assert {"share-start-writable", "share-start-read-only", "share-again"} <= set(logs)
    assert not [label for label, text in logs.items() if SHARE_PASSWORD in text], "the share password is in a log"
    assert "[hidden]" in logs["share-start-writable"]


def test_the_share_flow_passes_on_a_correct_share_in_both_modes():
    stick, report = run_share()
    assert report.success, report.problems
    names = [step["step"] for step in report.steps]
    assert any(name.startswith("writable: the right password opens the page") for name in names)
    assert any(name.startswith("read-only: stop ends ttyd") for name in names)
    assert SHARE_PASSWORD not in json.dumps(report.to_dict())


@pytest.mark.parametrize("change, words", [
    (dict(share_runs_as="root"), "ttyd runs as root"),
    (dict(share_stop_works=False), "ttyd still runs"),
    (dict(share_prints_password_in_status=True), "never the password"),
    (dict(share_writable_flag=False), "no -W"),
])
def test_share_failures_are_named(change, words):
    _stick, report = run_share(**change)
    assert not report.success and words in " ".join(report.problems)


def test_the_fingerprint_that_is_served_must_be_the_one_that_was_printed():
    stick = Stick()
    original = stick.fetch
    stick.fetch = lambda port, user, password, scheme: w6.Fetch(200, "00" * 32) if user and password == SHARE_PASSWORD and scheme == "https" else original(port, user, password, scheme)
    report = w6.share_flow(stick.hands(), stick.forward, stick.fetch)
    assert any("certificate that is served" in problem for problem in report.problems)


def test_a_share_that_answers_without_a_password_or_over_plain_http_fails():
    stick = Stick()
    original = stick.fetch
    stick.fetch = lambda port, user, password, scheme: w6.Fetch(200, PRINTED_HEX) if (user is None or scheme == "http") and stick.share_running else original(port, user, password, scheme)
    report = w6.share_flow(stick.hands(), stick.forward, stick.fetch)
    joined = " ".join(report.problems)
    assert "without the password the page is refused" in joined and "plain HTTP" in joined


def test_a_port_that_cannot_be_forwarded_is_a_finding_not_a_crash():
    stick = Stick()

    def broken(_port):
        raise RuntimeError("QEMU refused the port forward: no such netdev")

    report = w6.share_flow(stick.hands(), broken, stick.fetch)
    assert any("can be reached from the host" in problem for problem in report.problems)


def test_the_start_output_is_parsed_and_unreadable_output_gives_nothing():
    info = w6.parse_share_start(Stick().share_start("gentoo-rescue-share", None))
    assert (info.address, info.port, info.user, info.password) == ("10.0.2.15", 23456, "rescue", SHARE_PASSWORD) and info.fingerprint == PRINTED_HEX
    assert w6.parse_share_start("nothing useful") == w6.ShareInfo()


@pytest.mark.parametrize("text", [FINGERPRINT, "SHA256 Fingerprint=" + FINGERPRINT, "sha256:" + PRINTED_HEX.upper(), PRINTED_HEX])
def test_fingerprints_in_every_common_format_compare_equal(text):
    assert w6.normalize_fingerprint(text) == PRINTED_HEX


def test_an_openssh_style_fingerprint_is_converted_and_junk_is_empty():
    digest = hashlib.sha256(b"cert").digest()
    openssh = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
    assert w6.normalize_fingerprint(openssh) == digest.hex()
    assert w6.normalize_fingerprint("not a fingerprint") == "" and w6.normalize_fingerprint("") == ""


# --- gentoo-rescue-agents --------------------------------------------------------------------------------------------------------------------------

def test_the_agents_flow_refuses_bad_files_and_installs_good_ones():
    stick = Stick()
    report = w6.agents_flow(stick.hands(), lambda payload: stick.run("python3 -c 'import base64, json, os, pwd, sys'", "upload"))
    assert report.success, report.problems
    assert not stick.bin_files, "remove took them away again"


def test_an_installer_that_accepts_a_wrong_file_fails_the_flow():
    stick = Stick(refuses_bad=False)
    report = w6.agents_flow(stick.hands(), lambda payload: stick.run("python3 -c 'import base64, json, os, pwd, sys'", "upload"))
    assert not report.success and any("a wrong file is refused" in problem for problem in report.problems)


def test_the_fake_mirror_has_true_sizes_and_checksums_and_real_archives():
    files, pins = w6.fake_mirror()
    for name in w6.AGENTS:
        pin = pins[name]
        filename = pin["url"].rsplit("/", 1)[-1]
        assert pin["sha256"] == hashlib.sha256(files[filename]).hexdigest() and pin["size"] == len(files[filename])
        assert pin["url"].startswith("https://github.com/")
    with tarfile.open(fileobj=io.BytesIO(files["codex-fake-x86_64.tar.gz"])) as archive:
        assert archive.getnames() == ["codex"] and archive.extractfile("codex").read().startswith(b"#!/bin/sh")


def test_the_tampered_file_has_the_same_size_and_another_checksum():
    files, _pins = w6.fake_mirror()
    first = files["claude-fake-linux-x64"]
    assert len(w6.tampered(first)) == len(first) and w6.tampered(first) != first


def test_the_upload_payload_round_trips_and_carries_the_pins():
    files, pins = w6.fake_mirror()
    payload = json.loads(w6.upload_payload({w6.MIRROR_DIR: files}, pins))
    assert base64.b64decode(payload["dirs"][w6.MIRROR_DIR]["claude-fake-linux-x64"]) == files["claude-fake-linux-x64"]
    assert payload["pins_path"] == w6.FAKE_PINS and payload["pins"]["claude"]["binary"] == "claude"


def test_agent_install_problems_name_a_wrong_owner_mode_or_program():
    good = "".join(f"rescue-ai 755 /home/rescue-ai/.local/bin/{name}\n" for name in w6.AGENTS) + "---\n" + "".join(f"{name}-fake 0.0.1\n" for name in w6.AGENTS)
    assert w6.agent_install_problems(good) == []
    bad = good.replace("rescue-ai 755 /home/rescue-ai/.local/bin/codex", "root 755 /home/rescue-ai/.local/bin/codex").replace("opencode-fake 0.0.1\n", "")
    problems = w6.agent_install_problems(bad)
    assert any("codex is root 755" in problem for problem in problems) and any("opencode does not run" in problem for problem in problems)


# --- the host's HTTPS client against a real TLS server with basic authentication ------------------------------------------------------------------

@pytest.fixture
def tls_server(tmp_path):
    import base64 as b64
    import http.server
    import shutil
    import ssl
    import subprocess
    import threading

    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-subj", "/CN=test", "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    expected = "Basic " + b64.b64encode(b"rescue:bako-fidu-meza").decode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.headers.get("Authorization") == expected
            self.send_response(200 if ok else 401)
            self.send_header("WWW-Authenticate", 'Basic realm="ttyd"')
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fingerprint = subprocess.run(["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", str(cert)], check=True, capture_output=True, text=True).stdout
    yield server.server_address[1], w6.normalize_fingerprint(fingerprint.strip().split("=", 1)[1])
    server.shutdown()
    server.server_close()


def test_the_https_client_sees_the_status_and_the_certificate_the_server_showed(tls_server):
    port, printed = tls_server
    good = w6.fetch_https(port, "rescue", "bako-fidu-meza")
    assert good.status == 200 and good.fingerprint == printed, "openssl's fingerprint of the file equals the digest of the certificate on the wire"
    assert w6.fetch_https(port, None, None).status == 401
    assert w6.fetch_https(port, "rescue", "wrong").status == 401
    plain = w6.fetch_https(port, "rescue", "bako-fidu-meza", "http")
    assert plain.status != 200 and (plain.status is None or plain.status >= 400)


def test_a_closed_port_is_an_error_not_a_status():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    answer = w6.fetch_https(port, "rescue", "x")
    assert answer.status is None and answer.error
