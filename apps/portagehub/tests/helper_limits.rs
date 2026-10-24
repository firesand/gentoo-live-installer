//! The helper's input handling: sizes, encodings, idle exit.

mod common;

use common::{Helper, Scratch};
use portagehub::protocol::{Event, Op};
use std::time::Duration;

fn error_of(h: &Helper, id: u64) -> String {
    h.finish(id).unwrap_err()
}

#[test]
fn an_oversized_request_is_refused_and_the_session_keeps_serving() {
    let mut h = Helper::start();
    h.until(|e| matches!(e, Event::Ready { .. }), 10);
    // 20 MiB on one line, id first like the GUI writes it.
    let mut line = br#"{"id":7,"op":"write_file","path":"/etc/portage/x","content":""#.to_vec();
    line.resize(20 << 20, b'a');
    line.extend(b"\"}\n");
    h.send_raw(&line);
    let e = error_of(&h, 7);
    assert!(e.contains("request too large"), "{e}");
    // The next, ordinary request is answered: the rest of the long line was discarded, not parsed.
    h.send(8, Op::Ping);
    h.finish(8).unwrap();
    assert!(h.quit().success());
}

#[test]
fn a_line_exactly_at_the_limit_is_still_read() {
    let mut h = Helper::start();
    h.until(|e| matches!(e, Event::Ready { .. }), 10);
    let head = br#"{"id":3,"op":"ping","pad":""#;
    let mut line = head.to_vec();
    line.resize(portagehub::protocol::MAX_REQUEST_LINE - 2, b' ');
    line.extend(b"\"}");
    assert_eq!(line.len(), portagehub::protocol::MAX_REQUEST_LINE);
    line.push(b'\n');
    h.send_raw(&line);
    h.finish(3).unwrap();
    assert!(h.quit().success());
}

#[test]
fn a_file_over_the_write_limit_is_refused() {
    let root = Scratch::new("bigwrite");
    let mut h = Helper::start_in(&root.0);
    let big = "x".repeat(5 << 20);
    h.send(1, Op::WriteFile { path: "/etc/portage/package.use/big".into(), content: big, backup: false, expect: None });
    let e = error_of(&h, 1);
    assert!(e.contains("more than the 4 MiB"), "{e}");
    assert!(!root.path("etc/portage/package.use/big").exists());
    // Just under the limit works.
    h.send(2, Op::WriteFile { path: "/etc/portage/package.use/ok".into(), content: "y".repeat(4 << 20), backup: false, expect: None });
    h.finish(2).unwrap();
    assert_eq!(std::fs::metadata(root.path("etc/portage/package.use/ok")).unwrap().len(), 4 << 20);
    assert!(h.quit().success());
}

#[test]
fn invalid_utf8_gets_an_answer_and_does_not_end_the_session() {
    let mut h = Helper::start();
    h.until(|e| matches!(e, Event::Ready { .. }), 10);
    h.send_raw(b"{\"id\":2,\"op\":\"ping\"}\xff\xfe\n");
    let e = error_of(&h, 2);
    assert_eq!(e, "request is not valid UTF-8");
    // Garbage with no recoverable id is answered under id 0.
    h.send_raw(b"\xff\xfe\xfd\n");
    let evs = h.until(|e| matches!(e, Event::Err { id: 0, .. }), 10);
    assert!(matches!(evs.last(), Some(Event::Err { msg, .. }) if msg == "request is not valid UTF-8"));
    // The session is still there.
    h.send(3, Op::Ping);
    h.finish(3).unwrap();
    assert!(h.quit().success());
}

#[test]
fn blank_lines_and_bad_json_do_not_kill_the_session() {
    let mut h = Helper::start();
    h.until(|e| matches!(e, Event::Ready { .. }), 10);
    h.send_raw(b"\n   \n\r\n");
    h.send_raw(b"{\"id\":5,\"op\":\n");
    let evs = h.until(|e| matches!(e, Event::Err { .. }), 10);
    assert!(matches!(evs.last(), Some(Event::Err { msg, .. }) if msg.contains("not valid JSON")), "{evs:?}");
    h.send(6, Op::Ping);
    h.finish(6).unwrap();
    // A request without the trailing newline at the end of input is still served.
    h.send_raw(br#"{"id":9,"op":"ping"}"#);
    h.close_stdin();
    let evs = h.until(|e| matches!(e, Event::Ok { id: 9 }), 10);
    assert!(!evs.is_empty());
    assert!(h.child.wait().unwrap().success());
}

#[test]
fn an_idle_session_ends_by_itself() {
    let mut h = Helper::spawn(None, &[("PORTAGEHUB_IDLE_SECS", "1")]);
    h.until(|e| matches!(e, Event::Ready { .. }), 10);
    let started = std::time::Instant::now();
    let status = h.wait_exit(8).expect("the idle helper exits on its own");
    assert!(status.success(), "{status:?}");
    assert!(started.elapsed() < Duration::from_secs(6));
    assert!(h.stderr_text().contains("idle for 15 minutes, exiting"), "{}", h.stderr_text());
}

#[test]
fn requests_keep_an_idle_session_alive() {
    let mut h = Helper::spawn(None, &[("PORTAGEHUB_IDLE_SECS", "2")]);
    h.until(|e| matches!(e, Event::Ready { .. }), 10);
    // Pinging every second for four seconds: more than the idle limit, but never idle for 2 s.
    for i in 0..4u64 {
        std::thread::sleep(Duration::from_millis(1000));
        h.send(10 + i, Op::Ping);
        h.finish(10 + i).unwrap();
    }
    assert!(h.child.try_wait().unwrap().is_none(), "still running");
    let status = h.wait_exit(10).expect("and then it goes idle");
    assert!(status.success());
}

// --- the audit log never fails silently ---------------------------------------------------

#[test]
fn an_unwritable_audit_log_is_reported_but_does_not_stop_the_work() {
    let root = Scratch::new("auditfail");
    // var/log is a plain file, so /var/log/portagehub cannot be created.
    std::fs::create_dir_all(root.path("var")).unwrap();
    std::fs::write(root.path("var/log"), b"in the way").unwrap();
    let mut h = Helper::start_in(&root.0);
    let evs = h.until(|e| matches!(e, Event::Ready { .. }), 10);
    match evs.last() {
        Some(Event::Ready { warnings, .. }) => {
            assert_eq!(warnings.len(), 1, "{warnings:?}");
            assert!(warnings[0].starts_with("Audit log: not recording") && warnings[0].contains("portagehub"), "{}", warnings[0]);
        }
        other => panic!("{other:?}"),
    }
    // Policy is fail-open: the change itself goes through, and the user is not told twice.
    h.send(1, Op::WriteFile { path: "/etc/portage/package.use/a".into(), content: "x\n".into(), backup: false, expect: None });
    let evs = h.until(|e| matches!(e, Event::Ok { id: 1 }), 10);
    assert!(!evs.iter().any(|e| matches!(e, Event::Notice { .. })), "{evs:?}");
    assert_eq!(std::fs::read_to_string(root.path("etc/portage/package.use/a")).unwrap(), "x\n");
    assert!(h.quit().success());
}

#[test]
fn a_working_audit_log_has_no_warnings_and_records_the_request_id_on_commit() {
    let root = Scratch::new("auditok");
    let mut h = Helper::start_in(&root.0);
    let evs = h.until(|e| matches!(e, Event::Ready { .. }), 10);
    assert!(matches!(evs.last(), Some(Event::Ready { warnings, .. }) if warnings.is_empty()));
    h.send(41, Op::WriteFile { path: "/etc/portage/package.use/a".into(), content: "x\n".into(), backup: false, expect: None });
    h.finish(41).unwrap();
    assert!(h.quit().success());
    let records = portagehub::audit::read(&portagehub::audit::path_for(&root.0));
    let commit = records.iter().find(|r| r.event == "commit").expect("the auto-transaction's commit is logged");
    assert_eq!(commit.id, 41, "the commit record belongs to its request: {records:?}");
    assert!(records.iter().any(|r| r.event == "request" && r.id == 41));
    assert!(records.iter().any(|r| r.event == "done" && r.id == 41 && r.ok == Some(true)));
}

#[test]
fn only_a_few_signals_may_be_sent_to_jobs() {
    let mut h = Helper::start();
    h.until(|e| matches!(e, Event::Ready { .. }), 10);
    for (id, signal) in [(1u64, 19), (2, 0), (3, 9999), (4, 11), (5, -9)] {
        h.send(id, Op::Signal { target: 99, signal });
        let e = error_of(&h, id);
        assert!(e.contains("signal not allowed"), "{signal}: {e}");
    }
    // An allowed signal reaches the "no such job" check instead.
    for (id, signal) in [(6u64, 1), (7, 2), (8, 9), (9, 15), (10, 18)] {
        h.send(id, Op::Signal { target: 99, signal });
        assert_eq!(error_of(&h, id), "no such running job");
    }
    assert!(h.quit().success());
}
