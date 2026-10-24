//! Privileged helper for PortageHub.
//!
//! Started once per session through `pkexec`, it reads JSON requests from
//! stdin and answers with JSON events on stdout. It only runs a fixed list of
//! Portage tools and only writes below `/etc/portage/` (plus the few fixed
//! operations in `rootops`).
//!
//! As root, it only talks to the process that started it: stdin and stdout
//! must be one end of a socketpair (a socket cannot be reopened through
//! `/proc/<pid>/fd`, unlike a pipe), and the process at the other end must be
//! the user polkit authenticated and one of our ancestors.

use portagehub::protocol::Event;
use portagehub::server::{Server, Sink};
use std::io::{BufRead, Write};
use std::sync::Arc;
use std::sync::mpsc;

fn fstat(fd: i32) -> Option<libc::stat> {
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    (unsafe { libc::fstat(fd, &mut st) } == 0).then_some(st)
}

/// Parent pid of `pid` (from /proc/<pid>/stat).
fn ppid_of(pid: i32) -> Option<i32> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // "pid (comm) state ppid ...": comm may contain spaces, so split after the last ')'.
    stat.rsplit_once(')')?.1.split_whitespace().nth(1)?.parse().ok()
}

/// The uid of the user who asked for root (set by pkexec, sudo or doas).
fn requesting_uid() -> Option<u32> {
    for k in ["PKEXEC_UID", "SUDO_UID"] {
        if let Some(u) = std::env::var(k).ok().and_then(|v| v.parse().ok()) {
            return Some(u);
        }
    }
    let user = std::ffi::CString::new(std::env::var("DOAS_USER").ok()?).ok()?;
    let pw = unsafe { libc::getpwnam(user.as_ptr()) };
    (!pw.is_null()).then(|| unsafe { (*pw).pw_uid })
}

fn check_peer() -> Result<(), String> {
    if unsafe { libc::geteuid() } != 0 {
        return Ok(());
    }
    let (Some(i), Some(o)) = (fstat(0), fstat(1)) else {
        return Err("cannot inspect stdin/stdout".into());
    };
    if i.st_mode & libc::S_IFMT != libc::S_IFSOCK || (i.st_dev, i.st_ino) != (o.st_dev, o.st_ino) {
        return Err("stdin/stdout are not the socket PortageHub provides — the helper must be started by PortageHub".into());
    }
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe { libc::getsockopt(0, libc::SOL_SOCKET, libc::SO_PEERCRED, &mut cred as *mut _ as *mut libc::c_void, &mut len) } != 0 {
        return Err(format!("cannot read peer credentials: {}", std::io::Error::last_os_error()));
    }
    let expected = requesting_uid().unwrap_or(0);
    if cred.uid != expected {
        return Err(format!("peer uid {} is not the authenticated user {expected}", cred.uid));
    }
    // pkexec execs us in place (parent = GUI); sudo/doas may add one or two
    // intermediate processes.
    let mut p = unsafe { libc::getppid() };
    for _ in 0..4 {
        if p == cred.pid {
            return Ok(());
        }
        match ppid_of(p) {
            Some(pp) if pp > 0 => p = pp,
            _ => break,
        }
    }
    Err(format!("peer process {} is not the one that started the helper", cred.pid))
}

fn main() {
    if let Err(e) = check_peer() {
        eprintln!("portagehub-helper: {e}");
        std::process::exit(2);
    }

    // Single writer thread so events never interleave mid-line.
    let (tx, rx) = mpsc::channel::<Event>();
    let writer = std::thread::spawn(move || {
        let stdout = std::io::stdout();
        for ev in rx {
            let mut lock = stdout.lock();
            if let Ok(s) = serde_json::to_string(&ev) {
                // The GUI may be gone; keep draining so children never block.
                let _ = writeln!(lock, "{s}");
                let _ = lock.flush();
            }
        }
    });

    let uid = unsafe { libc::getuid() };
    let _ = tx.send(Event::Ready { uid, version: Some(env!("CARGO_PKG_VERSION").to_string()) });

    let server = Server::default();
    let tx_sink = std::sync::Mutex::new(tx.clone());
    let sink: Sink = Arc::new(move |ev| {
        let _ = tx_sink.lock().unwrap().send(ev);
    });

    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        match portagehub::protocol::parse_request(&line) {
            Ok(req) => server.handle(req, sink.clone()),
            Err(bad) => {
                // Answer with the request's id so the GUI can fail that request.
                let _ = tx.send(Event::Err { id: bad.id, msg: bad.msg });
            }
        }
    }

    // stdin closed: the GUI exited. Let running jobs finish instead of killing
    // a half-done emerge, then leave.
    while server.running() > 0 {
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    drop(sink);
    drop(tx);
    let _ = writer.join();
}
