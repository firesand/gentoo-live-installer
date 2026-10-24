//! Drives the real helper binary over its JSON-lines protocol, connected the
//! way the GUI connects it (a socketpair). Runs unprivileged: system tests
//! only use read-only commands, file operations run in a scratch root
//! (`PORTAGEHUB_ROOT`, honoured only when not root).

use portagehub::protocol::{Event, Op, Request};
use std::io::{BufRead, BufReader, Write};
use std::os::fd::OwnedFd;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

/// Tests that run the real emerge: skipped where Portage is not installed
/// (CI on other distributions) or when SKIP_EMERGE_TESTS=1 (ebuild sandbox).
fn have_emerge() -> bool {
    let ok = std::env::var_os("SKIP_EMERGE_TESTS").is_none_or(|v| v != "1") && portagehub::server::resolve_program("emerge").is_some();
    if !ok {
        eprintln!("emerge not available: skipping the parts that run it");
    }
    ok
}

struct Helper {
    child: std::process::Child,
    stdin: UnixStream,
    rx: mpsc::Receiver<Event>,
}

impl Helper {
    fn start() -> Helper {
        Helper::start_with(&[])
    }

    fn start_with(env: &[(&str, &Path)]) -> Helper {
        let (ours, theirs) = UnixStream::pair().unwrap();
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_portagehub-helper"));
        cmd.stdin(Stdio::from(OwnedFd::from(theirs.try_clone().unwrap()))).stdout(Stdio::from(OwnedFd::from(theirs)));
        for (k, v) in env {
            cmd.env(k, v);
        }
        let child = cmd.spawn().expect("spawn helper");
        drop(cmd);
        let reader = ours.try_clone().unwrap();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(reader).lines().map_while(Result::ok) {
                let ev: Event = serde_json::from_str(&line).expect("valid event json");
                if tx.send(ev).is_err() {
                    break;
                }
            }
        });
        Helper { child, stdin: ours, rx }
    }

    fn send(&mut self, id: u64, op: Op) {
        self.send_req(Request::new(id, op));
    }

    fn send_req(&mut self, req: Request) {
        let s = serde_json::to_string(&req).unwrap();
        writeln!(self.stdin, "{s}").unwrap();
        self.stdin.flush().unwrap();
    }

    /// Send and wait for the end (Ok/Err): returns the error, if any, and the events.
    fn call(&mut self, id: u64, tx: Option<&str>, op: Op) -> (Option<String>, Vec<Event>) {
        self.send_req(Request { id, tx: tx.map(String::from), op });
        let evs = self.until(|e| matches!(e, Event::Ok { id: i } | Event::Err { id: i, .. } if *i == id), 30);
        let err = evs.iter().find_map(|e| match e {
            Event::Err { id: i, msg } if *i == id => Some(msg.clone()),
            _ => None,
        });
        (err, evs)
    }

    fn close(self) {
        let _ = self.stdin.shutdown(std::net::Shutdown::Both);
        let mut child = self.child;
        let _ = child.wait();
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
    if have_emerge() {
        h.send(2, Op::Run { argv: vec!["emerge".into(), "--version".into()], env: vec![], cwd: None });
        let evs = h.until(|e| matches!(e, Event::Exit { id: 2, .. }), 60);
        assert!(evs.iter().any(|e| matches!(e, Event::Started { id: 2, .. })));
        assert!(evs.iter().any(|e| matches!(e, Event::Line { id: 2, text, .. } if text.contains("Portage"))), "{evs:?}");
        assert!(evs.iter().any(|e| matches!(e, Event::Exit { id: 2, code: Some(0), .. })));
    }

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

    // Closing the socket makes the helper exit.
    h.stdin.shutdown(std::net::Shutdown::Write).unwrap();
    let status = h.child.wait().unwrap();
    assert!(status.success());
}

#[test]
fn cancel_running_job() {
    if !have_emerge() {
        return;
    }
    let mut h = Helper::start();
    h.until(|e| matches!(e, Event::Ready { .. }), 10);
    // A slow, read-only dependency calculation.
    h.send(1, Op::Run { argv: vec!["emerge".into(), "--pretend".into(), "--emptytree".into(), "--ask=n".into(), "@world".into()], env: vec![], cwd: None });
    h.until(|e| matches!(e, Event::Started { id: 1, .. }), 30);
    std::thread::sleep(Duration::from_millis(800));
    h.send(2, Op::Signal { target: 1, signal: libc::SIGINT });
    let evs = h.until(|e| matches!(e, Event::Exit { id: 1, .. }), 30);
    // On a tiny system (CI container) emerge may finish before the signal.
    if evs.iter().any(|e| matches!(e, Event::Err { id: 2, msg } if msg.contains("no such running job"))) {
        eprintln!("emerge finished before it could be cancelled; skipping");
        h.close();
        return;
    }
    assert!(evs.iter().any(|e| matches!(e, Event::Ok { id: 2 })), "{evs:?}");
    let exit = evs.iter().find_map(|e| match e {
        Event::Exit { id: 1, code, signal } => Some((*code, *signal)),
        _ => None,
    });
    assert_ne!(exit, Some((Some(0), None)), "should not have finished normally");
    h.close();
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

    h.close();
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
    h.close();
}

// ----------------------------------------------------------------------
// Hermetic tests: a scratch root instead of the real system.

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("portagehub-it-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    for sub in ["etc/portage/package.use", "etc/ssh", "var/lib", "var/log", "boot", "lib/modules", "usr/src", "var/db/pkg", "var/tmp", "outside"] {
        std::fs::create_dir_all(d.join(sub)).unwrap();
    }
    d
}

fn in_root(root: &Path) -> Helper {
    let h = Helper::start_with(&[("PORTAGEHUB_ROOT", root)]);
    h.until(|e| matches!(e, Event::Ready { .. }), 10);
    h
}

fn write(path: &str, content: &str) -> Op {
    Op::WriteFile { path: path.into(), content: content.into(), backup: false }
}

fn audit(root: &Path) -> Vec<portagehub::audit::Record> {
    portagehub::audit::read(&portagehub::audit::path_for(root)).unwrap_or_default()
}

/// The transaction id of the newest commit record.
fn last_commit(root: &Path) -> String {
    audit(root).iter().rev().find(|r| r.event == "commit").and_then(|r| r.tx.clone()).expect("a commit record")
}

#[test]
fn writes_stay_in_etc_portage_and_never_follow_symlinks() {
    let root = scratch("escape");
    let etc = root.join("etc/portage");
    symlink(root.join("outside"), etc.join("make.profile")).unwrap();
    symlink("../../outside", etc.join("package.use/up")).unwrap();
    std::fs::write(root.join("outside/target"), "keep").unwrap();
    symlink(root.join("outside/target"), etc.join("package.use/link")).unwrap();
    let mut h = in_root(&root);

    let (err, _) = h.call(1, None, write("/etc/portage/package.use/zz-portagehub", "app-misc/foo bar\n"));
    assert_eq!(err, None);
    assert_eq!(std::fs::read_to_string(etc.join("package.use/zz-portagehub")).unwrap(), "app-misc/foo bar\n");

    for (id, path) in [(2, "/etc/portage/make.profile/package.use"), (3, "/etc/portage/package.use/up/x"), (4, "/etc/portage/../ssh/sshd_config")] {
        let (err, _) = h.call(id, None, write(path, "evil"));
        assert!(err.is_some(), "{path} must be refused");
    }
    // A symlink as the target itself is replaced, not followed.
    assert_eq!(h.call(5, None, write("/etc/portage/package.use/link", "new")).0, None);
    assert_eq!(std::fs::read_to_string(root.join("outside/target")).unwrap(), "keep");
    assert!(std::fs::read_dir(root.join("outside")).unwrap().count() == 1, "nothing new outside");
    // Hooks and ._cfg names are left to Portage.
    for (id, path) in [(6, "/etc/portage/bashrc"), (7, "/etc/portage/postsync.d/99-x"), (8, "/etc/portage/._cfg0000_bashrc")] {
        let (err, _) = h.call(id, None, write(path, "evil"));
        assert!(err.unwrap_or_default().contains("not allowed"), "{path}");
    }
    // Renames and deletes are confined too.
    assert!(h.call(9, None, Op::Rename { from: "/etc/portage/package.use/zz-portagehub".into(), to: "/etc/portage/make.profile/x".into() }).0.is_some());
    assert!(h.call(10, None, Op::DeleteFile { path: "/etc/portage/make.profile/parent".into() }).0.is_some());
    assert!(etc.join("package.use/zz-portagehub").exists());

    // Every request is in the audit log, with its outcome.
    let log = audit(&root);
    assert!(log.iter().any(|r| r.event == "request" && r.op == "write_file" && r.what.contains("zz-portagehub")));
    assert!(log.iter().any(|r| r.event == "done" && r.id == 2 && r.ok == Some(false)));
    assert!(log.iter().any(|r| r.event == "commit" && r.files == vec!["/etc/portage/package.use/zz-portagehub".to_string()]));
    let mode = std::fs::metadata(root.join("var/lib/portagehub/journal")).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode, 0o700, "journal holds copies of root-only files");
    std::fs::create_dir(root.join("probe")).unwrap();
    let default_mode = std::fs::metadata(root.join("probe")).unwrap().permissions().mode() & 0o777;
    let parent = std::fs::metadata(root.join("var/lib/portagehub")).unwrap().permissions().mode() & 0o777;
    assert_eq!(parent, default_mode, "only the journal is private; the GUI reads known-good state next to it");
    h.close();
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn failed_transaction_rolls_back_byte_for_byte() {
    let root = scratch("rollback");
    let etc = root.join("etc/portage");
    let original = b"# hand-written\n>=dev-lang/rust-1.80 clippy\n\xff binary tail".to_vec();
    std::fs::write(etc.join("package.use/main"), &original).unwrap();
    std::fs::set_permissions(etc.join("package.use/main"), std::fs::Permissions::from_mode(0o640)).unwrap();
    let mut h = in_root(&root);

    let (err, evs) = h.call(1, None, Op::Begin { label: "Apply USE flags".into() });
    assert_eq!(err, None);
    let tx = evs.iter().find_map(|e| match e {
        Event::Data { id: 1, text } => Some(text.clone()),
        _ => None,
    });
    let tx = tx.expect("transaction id");
    assert_eq!(h.call(2, Some(&tx), Op::WriteFile { path: "/etc/portage/package.use/main".into(), content: "replaced\n".into(), backup: true }).0, None);
    assert_eq!(h.call(3, Some(&tx), write("/etc/portage/package.accept_keywords/new", "x/y ~amd64\n")).0, None);
    assert_eq!(h.call(4, Some(&tx), Op::Mkdir { path: "/etc/portage/env".into() }).0, None);
    assert!(etc.join("package.use/main~").exists() && etc.join("env").is_dir());
    // The next step fails; the GUI then rolls the transaction back.
    assert!(h.call(5, Some(&tx), write("/etc/portage/bashrc", "x")).0.is_some());
    let (err, evs) = h.call(6, None, Op::Rollback { transaction: tx.clone(), force: false });
    assert_eq!(err, None, "{evs:?}");
    assert_eq!(std::fs::read(etc.join("package.use/main")).unwrap(), original);
    assert_eq!(std::fs::metadata(etc.join("package.use/main")).unwrap().permissions().mode() & 0o777, 0o640);
    for gone in ["package.use/main~", "package.accept_keywords/new", "package.accept_keywords", "env", "bashrc"] {
        assert!(!etc.join(gone).exists(), "{gone} should be gone after the rollback");
    }
    assert!(h.call(7, None, Op::Rollback { transaction: tx.clone(), force: false }).0.is_some(), "only once");
    assert!(h.call(8, Some(&tx), write("/etc/portage/x", "x")).0.is_some(), "a rolled-back transaction is closed");
    assert!(h.call(9, Some("../../etc"), write("/etc/portage/x", "x")).0.is_some(), "bogus transaction ids are refused");
    assert!(!etc.join("x").exists());
    h.close();
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn undo_of_saved_changes_and_config_updates() {
    let root = scratch("undo");
    std::fs::write(root.join("etc/ssh/sshd_config"), "Port 22\n").unwrap();
    std::fs::write(root.join("etc/ssh/._cfg0000_sshd_config"), "Port 2222\n").unwrap();
    let mut h = in_root(&root);

    // Accepting a config update is recorded and can be undone.
    assert_eq!(h.call(1, None, Op::ConfigUpdate { path: "/etc/ssh/._cfg0000_sshd_config".into(), accept: true }).0, None);
    assert_eq!(std::fs::read_to_string(root.join("etc/ssh/sshd_config")).unwrap(), "Port 2222\n");
    let tx = last_commit(&root);
    assert_eq!(h.call(2, None, Op::Rollback { transaction: tx, force: false }).0, None);
    assert_eq!(std::fs::read_to_string(root.join("etc/ssh/sshd_config")).unwrap(), "Port 22\n");
    assert_eq!(std::fs::read_to_string(root.join("etc/ssh/._cfg0000_sshd_config")).unwrap(), "Port 2222\n");

    // Undo refuses to overwrite later edits unless forced.
    assert_eq!(h.call(3, None, write("/etc/portage/make.conf", "MAKEOPTS=\"-j8\"\n")).0, None);
    let tx = last_commit(&root);
    std::fs::write(root.join("etc/portage/make.conf"), "MAKEOPTS=\"-j32\"\n").unwrap();
    let (err, _) = h.call(4, None, Op::Rollback { transaction: tx.clone(), force: false });
    assert!(err.unwrap_or_default().contains("changed since"));
    assert!(root.join("etc/portage/make.conf").exists());
    assert_eq!(h.call(5, None, Op::Rollback { transaction: tx, force: true }).0, None);
    assert!(!root.join("etc/portage/make.conf").exists(), "it did not exist before");
    let log = audit(&root);
    assert_eq!(log.iter().filter(|r| r.event == "rollback").count(), 2);
    h.close();
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn kernel_and_cleanup_ops_stay_in_their_trees() {
    let root = scratch("kernel");
    std::fs::write(root.join("boot/vmlinuz-1.2.3-test"), "kernel image").unwrap();
    std::fs::write(root.join("outside/keep"), "k").unwrap();
    let mut h = in_root(&root);

    let mark = |backup| Op::MarkGood { kver: "1.2.3-test".into(), state_dir: "/var/lib/portagehub/kernels".into(), backup };
    let (err, evs) = h.call(1, None, mark(true));
    assert_eq!(err, None, "{evs:?}");
    assert_eq!(std::fs::read_to_string(root.join("var/lib/portagehub/kernels/last-good")).unwrap(), "1.2.3-test\n");
    assert_eq!(std::fs::read_to_string(root.join("boot/kernel-backup/1.2.3-test/vmlinuz-1.2.3-test")).unwrap(), "kernel image");
    // Marking is undoable like any other file change.
    let tx = last_commit(&root);
    assert_eq!(h.call(2, None, Op::Rollback { transaction: tx, force: false }).0, None);
    assert!(!root.join("var/lib/portagehub/kernels/last-good").exists());
    // A backup directory that is a symlink is refused.
    std::fs::remove_dir_all(root.join("boot/kernel-backup")).unwrap();
    symlink(root.join("outside"), root.join("boot/kernel-backup")).unwrap();
    assert!(h.call(3, None, mark(true)).0.is_some());
    assert_eq!(std::fs::read_dir(root.join("outside")).unwrap().count(), 1);

    // Leftover removal deletes the module tree without following links out of it.
    let m = root.join("lib/modules/1.0.0-old");
    std::fs::create_dir_all(m.join("kernel/drivers")).unwrap();
    std::fs::write(m.join("kernel/drivers/x.ko"), "ko").unwrap();
    symlink(root.join("outside"), m.join("build")).unwrap();
    std::fs::write(root.join("boot/vmlinuz-1.0.0-old"), "old").unwrap();
    let (err, evs) = h.call(4, None, Op::RemoveKernelLeftovers { kver: "1.0.0-old".into(), old_only: false });
    assert_eq!(err, None, "{evs:?}");
    assert!(!m.exists() && !root.join("boot/vmlinuz-1.0.0-old").exists());
    assert!(root.join("outside/keep").exists());

    // Build-dir cleanup, with a symlinked category that must be skipped.
    let bd = root.join("var/tmp/portage/app-misc/foo-1.0/work");
    std::fs::create_dir_all(&bd).unwrap();
    std::fs::create_dir_all(root.join("var/tmp/portage/app-misc/foo-1.0/temp")).unwrap();
    // Same <cat>/<pf> shape but no temp/: not a build dir (e.g. an old /usr/portage tree).
    std::fs::create_dir_all(root.join("var/tmp/portage/dev-lang/tree-pkg/files")).unwrap();
    symlink(root.join("outside"), bd.join("link")).unwrap();
    symlink(root.join("outside"), root.join("var/tmp/portage/sys-evil")).unwrap();
    let (err, _) = h.call(5, None, Op::CleanBuildDirs);
    match err {
        Some(e) if e.contains("emerge is running") => eprintln!("skipping build-dir cleanup check: {e}"),
        e => {
            assert_eq!(e, None);
            assert!(!root.join("var/tmp/portage/app-misc").exists());
            assert!(root.join("var/tmp/portage/dev-lang/tree-pkg/files").exists());
            assert!(root.join("outside/keep").exists());
        }
    }
    h.close();
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn peer_must_be_the_socket_the_gui_provides() {
    // Same check the helper does as root, switched on for an unprivileged run.
    let strict: &[(&str, &Path)] = &[("PORTAGEHUB_STRICT_PEER", Path::new("1"))];
    let mut h = Helper::start_with(strict);
    h.until(|e| matches!(e, Event::Ready { .. }), 10);
    h.send(1, Op::Ping);
    h.until(|e| matches!(e, Event::Ok { id: 1 }), 10);

    // Pipes (which another process of the user could reopen) are refused.
    let out = Command::new(env!("CARGO_BIN_EXE_portagehub-helper"))
        .env("PORTAGEHUB_STRICT_PEER", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2), "{}", String::from_utf8_lossy(&out.stderr));
    assert!(String::from_utf8_lossy(&out.stderr).contains("not the socket"));

    // Another process cannot open the helper's stdin through /proc.
    let pid = h.child.id();
    let e = std::fs::OpenOptions::new().write(true).open(format!("/proc/{pid}/fd/0")).unwrap_err();
    assert_eq!(e.raw_os_error(), Some(libc::ENXIO), "{e}");
    h.close();
}

#[test]
fn etc_portage_may_itself_be_a_symlink() {
    // Some keep /etc/portage in a git checkout and link it; the base is
    // resolved, while links below it are still refused.
    let root = scratch("baselink");
    std::fs::rename(root.join("etc/portage"), root.join("portage-git")).unwrap();
    symlink("../portage-git", root.join("etc/portage")).unwrap();
    let mut h = in_root(&root);
    assert_eq!(h.call(1, None, write("/etc/portage/package.use/zz", "a/b x\n")).0, None);
    assert_eq!(std::fs::read_to_string(root.join("portage-git/package.use/zz")).unwrap(), "a/b x\n");
    symlink(root.join("outside"), root.join("portage-git/evil")).unwrap();
    assert!(h.call(2, None, write("/etc/portage/evil/x", "x")).0.is_some());
    let tx = last_commit(&root);
    assert_eq!(h.call(3, None, Op::Rollback { transaction: tx, force: false }).0, None);
    assert!(!root.join("portage-git/package.use/zz").exists());
    h.close();
    let _ = std::fs::remove_dir_all(&root);
}
