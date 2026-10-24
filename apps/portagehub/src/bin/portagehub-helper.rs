//! Privileged helper for PortageHub.
//!
//! Started once per session through `pkexec`, it reads JSON requests from
//! stdin and answers with JSON events on stdout. While a session is open
//! PortageHub can run package-manager commands and rewrite `/etc/portage` as
//! root, which is as good as full administrator access: the polkit prompt is
//! the real boundary, and what the helper checks (programs and their
//! arguments, writable paths, an audit log) guards against mistakes and
//! against other programs of the same user, not against the person at the
//! keyboard.
//!
//! As root, it only talks to the process that started it: stdin and stdout
//! must be one end of a socketpair (a socket cannot be reopened through
//! `/proc/<pid>/fd`, unlike a pipe), and the process at the other end must be
//! the user polkit authenticated and one of our ancestors (see `peer`).
//!
//! The session ends when the GUI closes its end, or after 15 minutes without
//! a request or a running job (`PORTAGEHUB_IDLE_SECS` shortens that for tests,
//! only when not root).

use portagehub::peer::{self, PeerCred, StreamId};
use portagehub::protocol::{self, Event, IDLE_EXIT_MESSAGE, IDLE_EXIT_SECS, MAX_QUEUED_EVENTS, MAX_REQUEST_LINE};
use portagehub::queue::EventQueue;
use portagehub::server::{Server, Sink};
use std::io::{BufRead, BufReader, Read, Write};
use std::sync::{Arc, mpsc};
use std::time::{Duration, Instant};

fn fstat(fd: i32) -> Option<libc::stat> {
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    (unsafe { libc::fstat(fd, &mut st) } == 0).then_some(st)
}

// st_dev/st_ino are u64 on x86_64 but narrower on some 32-bit targets: keep the casts portable.
#[allow(clippy::unnecessary_cast)]
fn stream_id(fd: i32) -> Option<StreamId> {
    let st = fstat(fd)?;
    Some(StreamId { is_socket: st.st_mode & libc::S_IFMT == libc::S_IFSOCK, dev: st.st_dev as u64, ino: st.st_ino as u64 })
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
    let (Some(i), Some(o)) = (stream_id(0), stream_id(1)) else {
        return Err("cannot inspect stdin/stdout".into());
    };
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe { libc::getsockopt(0, libc::SOL_SOCKET, libc::SO_PEERCRED, &mut cred as *mut _ as *mut libc::c_void, &mut len) } != 0 {
        return Err(format!("cannot read peer credentials: {}", std::io::Error::last_os_error()));
    }
    // pkexec execs us in place (parent = GUI); sudo/doas may add one or two intermediate processes.
    let mut ancestors = Vec::new();
    let mut p = unsafe { libc::getppid() };
    for _ in 0..peer::MAX_ANCESTORS {
        ancestors.push(p);
        match ppid_of(p) {
            Some(pp) if pp > 0 => p = pp,
            _ => break,
        }
    }
    peer::verify_peer(i, o, PeerCred { pid: cred.pid, uid: cred.uid }, requesting_uid().unwrap_or(0), &ancestors)
}

/// How long without a request or a job ends the session.
fn idle_limit() -> Duration {
    if unsafe { libc::geteuid() } != 0
        && let Some(secs) = std::env::var("PORTAGEHUB_IDLE_SECS").ok().and_then(|v| v.parse::<u64>().ok())
    {
        return Duration::from_secs(secs);
    }
    Duration::from_secs(IDLE_EXIT_SECS)
}

/// What the reader thread found on stdin.
enum Input {
    Line(Vec<u8>),
    /// A line over the size limit: its start (enough to find the request id) is kept.
    TooLong(Vec<u8>),
    Eof,
    Failed(std::io::Error),
}

/// Read request lines without ever holding more than [`MAX_REQUEST_LINE`] of one in memory.
fn read_requests(out: mpsc::Sender<Input>) {
    let stdin = std::io::stdin();
    let mut rdr = BufReader::with_capacity(64 << 10, stdin.lock());
    loop {
        let mut buf = Vec::new();
        let n = match (&mut rdr).take(MAX_REQUEST_LINE as u64 + 1).read_until(b'\n', &mut buf) {
            Ok(n) => n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => {
                let _ = out.send(Input::Failed(e));
                return;
            }
        };
        if n == 0 {
            let _ = out.send(Input::Eof);
            return;
        }
        if buf.last() != Some(&b'\n') && buf.len() > MAX_REQUEST_LINE {
            // Too long: throw the rest of the line away and keep serving.
            buf.truncate(256);
            if let Err(e) = rdr.skip_until(b'\n') {
                let _ = out.send(Input::Failed(e));
                return;
            }
            if out.send(Input::TooLong(buf)).is_err() {
                return;
            }
            continue;
        }
        while matches!(buf.last(), Some(b'\n' | b'\r')) {
            buf.pop();
        }
        if out.send(Input::Line(buf)).is_err() {
            return;
        }
    }
}

fn answer_error(queue: &EventQueue, id: u64, msg: impl Into<String>) {
    queue.push(Event::Err { id, msg: msg.into() });
}

fn main() {
    if let Err(e) = check_peer() {
        eprintln!("portagehub-helper: {e}");
        std::process::exit(2);
    }

    // Single writer thread so events never interleave mid-line. The queue between it and the
    // request handlers is bounded: a stalled GUI cannot make the helper grow without limit.
    let queue = Arc::new(EventQueue::new(MAX_QUEUED_EVENTS));
    let writer = {
        let queue = queue.clone();
        std::thread::spawn(move || {
            let stdout = std::io::stdout();
            while let Some(ev) = queue.pop() {
                let mut lock = stdout.lock();
                if let Ok(s) = serde_json::to_string(&ev) {
                    // The GUI may be gone; keep draining so children never block.
                    let _ = writeln!(lock, "{s}");
                    let _ = lock.flush();
                }
            }
        })
    };

    let server = Server::default();
    let warnings: Vec<String> = server.report_audit_problem().map(|p| format!("Audit log: not recording ({p})")).into_iter().collect();
    queue.push(Event::Ready { uid: unsafe { libc::getuid() }, version: Some(env!("CARGO_PKG_VERSION").to_string()), warnings });

    let sink: Sink = {
        let queue = queue.clone();
        Arc::new(move |ev| queue.push(ev))
    };

    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || read_requests(tx));

    let idle = idle_limit();
    let mut last_activity = Instant::now();
    loop {
        match rx.recv_timeout(Duration::from_millis(500)) {
            Ok(Input::Line(bytes)) => {
                last_activity = Instant::now();
                if bytes.iter().all(u8::is_ascii_whitespace) {
                    continue;
                }
                match std::str::from_utf8(&bytes) {
                    // A lossy conversion would corrupt a file the request writes: refuse instead.
                    Err(_) => answer_error(&queue, protocol::id_from_prefix(&bytes), "request is not valid UTF-8"),
                    Ok(line) => match protocol::parse_request(line) {
                        Ok(req) => server.handle(req, sink.clone()),
                        // Answer with the request's id so the GUI can fail that request.
                        Err(bad) => answer_error(&queue, bad.id, bad.msg),
                    },
                }
            }
            Ok(Input::TooLong(prefix)) => {
                last_activity = Instant::now();
                answer_error(&queue, protocol::id_from_prefix(&prefix), format!("request too large (the limit is {} MiB)", MAX_REQUEST_LINE >> 20));
            }
            Ok(Input::Eof) => break,
            Ok(Input::Failed(e)) => {
                eprintln!("portagehub-helper: reading requests failed: {e}");
                break;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if server.running() > 0 {
                    last_activity = Instant::now();
                } else if last_activity.elapsed() >= idle {
                    // Nobody is using the admin session: end it rather than leave root waiting.
                    eprintln!("{IDLE_EXIT_MESSAGE}");
                    break;
                }
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }

    // Input is over (the GUI exited, or the session went idle). Let running jobs finish instead
    // of killing a half-done emerge, then leave.
    while server.running() > 0 {
        std::thread::sleep(Duration::from_millis(500));
    }
    drop(sink);
    queue.close();
    let _ = writer.join();
}
