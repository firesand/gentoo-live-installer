//! Drives the real helper binary over its JSON-lines protocol.
//! Runs unprivileged and only uses read-only commands.

use portagehub::protocol::{Event, Op, Request};
use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

struct Helper {
    child: std::process::Child,
    stdin: std::process::ChildStdin,
    rx: mpsc::Receiver<Event>,
}

impl Helper {
    fn start() -> Helper {
        let mut child = Command::new(env!("CARGO_BIN_EXE_portagehub-helper"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .expect("spawn helper");
        let stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                let ev: Event = serde_json::from_str(&line).expect("valid event json");
                if tx.send(ev).is_err() {
                    break;
                }
            }
        });
        Helper { child, stdin, rx }
    }

    fn send(&mut self, id: u64, op: Op) {
        let s = serde_json::to_string(&Request::new(id, op)).unwrap();
        writeln!(self.stdin, "{s}").unwrap();
        self.stdin.flush().unwrap();
    }

    fn until(&self, mut pred: impl FnMut(&Event) -> bool, secs: u64) -> Vec<Event> {
        let mut seen = Vec::new();
        let deadline = std::time::Instant::now() + Duration::from_secs(secs);
        loop {
            let left = deadline.saturating_duration_since(std::time::Instant::now());
            let ev = self.rx.recv_timeout(left).unwrap_or_else(|_| panic!("timeout; saw {seen:?}"));
            let done = pred(&ev);
            seen.push(ev);
            if done {
                return seen;
            }
        }
    }
}

#[test]
fn protocol_roundtrip() {
    let mut h = Helper::start();
    h.until(|e| matches!(e, Event::Ready { .. }), 10);

    h.send(1, Op::Ping);
    h.until(|e| matches!(e, Event::Ok { id: 1 }), 10);

    // Allowed program, streamed output, exit code.
    h.send(2, Op::Run { argv: vec!["emerge".into(), "--version".into()], env: vec![], cwd: None });
    let evs = h.until(|e| matches!(e, Event::Exit { id: 2, .. }), 60);
    assert!(evs.iter().any(|e| matches!(e, Event::Started { id: 2, .. })));
    assert!(evs.iter().any(|e| matches!(e, Event::Line { id: 2, text, .. } if text.contains("Portage"))), "{evs:?}");
    assert!(evs.iter().any(|e| matches!(e, Event::Exit { id: 2, code: Some(0), .. })));

    // Disallowed program.
    h.send(3, Op::Run { argv: vec!["rm".into(), "-rf".into(), "/tmp/nothing".into()], env: vec![], cwd: None });
    let evs = h.until(|e| matches!(e, Event::Err { id: 3, .. }), 10);
    assert!(matches!(evs.last(), Some(Event::Err { msg, .. }) if msg.contains("not allowed")));

    // Path outside /etc/portage.
    h.send(4, Op::WriteFile { path: "/etc/passwd".into(), content: "x".into(), backup: false });
    let evs = h.until(|e| matches!(e, Event::Err { id: 4, .. }), 10);
    assert!(matches!(evs.last(), Some(Event::Err { msg, .. }) if msg.contains("not allowed")));
    h.send(5, Op::DeleteFile { path: "/etc/portage/../shadow".into() });
    h.until(|e| matches!(e, Event::Err { id: 5, .. }), 10);

    // Bad config-update path.
    h.send(6, Op::ConfigUpdate { path: "/etc/passwd".into(), accept: true });
    h.until(|e| matches!(e, Event::Err { id: 6, .. }), 10);

    // Unknown signal target.
    h.send(7, Op::Signal { target: 999, signal: 2 });
    h.until(|e| matches!(e, Event::Err { id: 7, .. }), 10);

    // Closing stdin makes the helper exit.
    drop(h.stdin);
    let status = h.child.wait().unwrap();
    assert!(status.success());
}

#[test]
fn kernel_ops_refuse_unsafe_requests() {
    use portagehub::protocol::BootAction;
    let mut h = Helper::start();
    h.until(|e| matches!(e, Event::Ready { version: Some(_), .. }), 10);
    let err = |evs: &[Event], id: u64| -> String {
        evs.iter()
            .find_map(|e| match e {
                Event::Err { id: i, msg } if *i == id => Some(msg.clone()),
                _ => None,
            })
            .unwrap_or_default()
    };

    let running = std::fs::read_to_string("/proc/sys/kernel/osrelease").unwrap().trim().to_string();
    h.send(1, Op::RemoveKernelLeftovers { kver: running, old_only: false });
    let evs = h.until(|e| matches!(e, Event::Err { id: 1, .. } | Event::Ok { id: 1 }), 30);
    assert!(err(&evs, 1).contains("running kernel"), "{evs:?}");

    h.send(2, Op::RemoveKernelLeftovers { kver: "../../etc".into(), old_only: false });
    let evs = h.until(|e| matches!(e, Event::Err { id: 2, .. } | Event::Ok { id: 2 }), 10);
    assert!(err(&evs, 2).contains("invalid"), "{evs:?}");

    h.send(3, Op::BootAction { action: BootAction::DracutRegen, kver: Some("6.1/../../x".into()) });
    let evs = h.until(|e| matches!(e, Event::Err { id: 3, .. } | Event::Started { id: 3, .. }), 10);
    assert!(err(&evs, 3).contains("unknown kernel"), "{evs:?}");

    h.send(4, Op::MarkGood { kver: "1.0".into(), state_dir: "/tmp/evil".into(), backup: false });
    let evs = h.until(|e| matches!(e, Event::Err { id: 4, .. } | Event::Ok { id: 4 }), 10);
    assert!(err(&evs, 4).contains("not allowed"), "{evs:?}");

    // Read-only scan works unprivileged and says so.
    h.send(5, Op::BootScan);
    let evs = h.until(|e| matches!(e, Event::Ok { id: 5 }), 30);
    let data = evs
        .iter()
        .find_map(|e| match e {
            Event::Data { id: 5, text } => Some(text.clone()),
            _ => None,
        })
        .expect("boot scan data");
    let scan: portagehub::protocol::BootScan = serde_json::from_str(&data).unwrap();
    assert_eq!(scan.root, unsafe { libc::geteuid() } == 0);

    // An unknown operation is answered with the request id (old/new mismatch).
    writeln!(h.stdin, r#"{{"id":6,"op":"does_not_exist"}}"#).unwrap();
    h.stdin.flush().unwrap();
    let evs = h.until(|e| matches!(e, Event::Err { id: 6, .. }), 10);
    assert!(err(&evs, 6).contains("unsupported"), "{evs:?}");

    drop(h.stdin);
    let _ = h.child.wait();
}

#[test]
fn remount_only_boot_partitions() {
    let mut h = Helper::start();
    h.until(|e| matches!(e, Event::Ready { .. }), 10);
    for (id, target) in [(1u64, "/"), (2, "/etc"), (3, "/boot/../etc"), (4, "/mnt/steam-library")] {
        h.send(id, Op::Remount { target: target.into(), rw: true });
        let evs = h.until(|e| matches!(e, Event::Err { id: i, .. } | Event::Started { id: i, .. } if *i == id), 10);
        assert!(matches!(evs.last(), Some(Event::Err { msg, .. }) if msg.contains("not a boot mount point")), "{target}: {evs:?}");
    }
    drop(h.stdin);
    let _ = h.child.wait();
}
