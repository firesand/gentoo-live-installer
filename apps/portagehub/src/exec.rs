//! Running commands: unprivileged ones directly, privileged ones through the
//! root helper (one `pkexec` prompt per session), or in-process when the GUI
//! itself runs as root.
//!
//! The helper's stdin/stdout are one end of a socketpair: unlike a pipe, a
//! socket cannot be reopened by another process of the same user through
//! `/proc/<pid>/fd`, so nobody else can inject requests into the root session.

use portagehub::protocol::{Event, Op, Request};
use portagehub::server::{Server, Sink};
use portagehub::stream::spawn_streaming;
use std::collections::{HashMap, HashSet, VecDeque};
use std::io::{BufRead, BufReader, Write};
use std::net::Shutdown;
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Priv {
    User,
    Root,
}

#[derive(Debug, Clone, PartialEq)]
pub enum HelperState {
    NotStarted,
    Starting,
    Ready,
    Failed(String),
}

/// Messages from executor threads to the app.
#[derive(Debug)]
pub enum ExecMsg {
    /// An event and when it arrived (output is timestamped on arrival, so
    /// progress stays accurate even when the window is hidden and not repainting).
    Event(Event, std::time::Instant),
    /// The helper's version and the problems its session starts with.
    HelperReady(Option<String>, Vec<String>),
    HelperGone(HelperExit),
    /// Something the helper wants the user to know that belongs to no request.
    Notice(String),
}

struct HelperConn {
    sock: UnixStream,
    child: Child,
}

impl Drop for HelperConn {
    fn drop(&mut self) {
        // The reader thread holds a clone of the socket: shut down our
        // direction so the helper sees end-of-input and exits after its jobs.
        let _ = self.sock.shutdown(Shutdown::Write);
    }
}

pub struct Executor {
    next_id: u64,
    tx: Sender<ExecMsg>,
    ctx: egui::Context,
    pub is_root: bool,
    pub helper_state: HelperState,
    helper: Option<HelperConn>,
    /// In-process request handler when the GUI itself runs as root.
    local_root: Option<Server>,
    /// Requests sent to the helper that have not finished yet.
    pending_root: Arc<Mutex<HashSet<u64>>>,
    /// Requests waiting for the helper to start.
    queued: VecDeque<Request>,
    user_pids: Arc<Mutex<HashMap<u64, u32>>>,
    pub helper_path: Option<PathBuf>,
    /// The running helper reported a different version than the GUI.
    pub helper_outdated: bool,
    /// (major, minor) of the running helper.
    pub helper_version: (u32, u32),
}

fn ev_msg(e: Event) -> ExecMsg {
    ExecMsg::Event(e, std::time::Instant::now())
}

/// Where the helper may be installed: next to the GUI, then the system locations (a `PREFIX=/usr/local`
/// install puts it in /usr/local/libexec).
pub fn helper_candidates(exe_dir: Option<&std::path::Path>) -> Vec<PathBuf> {
    let mut cands = Vec::new();
    if let Some(dir) = exe_dir {
        cands.push(dir.join("portagehub-helper"));
    }
    for dir in ["/usr/libexec", "/usr/bin", "/usr/local/libexec", "/usr/local/bin", "/usr/local/sbin"] {
        cands.push(PathBuf::from(dir).join("portagehub-helper"));
    }
    cands
}

pub fn find_helper() -> Option<PathBuf> {
    if let Some(p) = std::env::var_os("PORTAGEHUB_HELPER").map(PathBuf::from).filter(|p| p.is_absolute() && p.is_file()) {
        return Some(p);
    }
    let exe = std::env::current_exe().ok();
    helper_candidates(exe.as_deref().and_then(|e| e.parent())).into_iter().find(|p| p.is_file())
}

/// Why the helper process ended, from what it wrote on stderr.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HelperExit {
    /// The session was idle for 15 minutes and closed itself: not an error; the next admin action asks again.
    Idle,
    /// pkexec found no polkit agent to ask for the password.
    NoAgent,
    /// Anything else, as the helper or pkexec said it (empty: it was cancelled or exited silently).
    Other(String),
}

pub fn explain_helper_exit(stderr: &str) -> HelperExit {
    let text = stderr.trim();
    if text.contains(portagehub::protocol::IDLE_EXIT_MESSAGE) {
        HelperExit::Idle
    } else if text.contains("No authentication agent found") || text.contains("Error creating textual authentication agent") {
        HelperExit::NoAgent
    } else {
        HelperExit::Other(text.to_string())
    }
}

impl HelperExit {
    /// The message for the user (`None` for an idle close, which gets its own, milder one).
    pub fn problem(&self) -> Option<String> {
        match self {
            HelperExit::Idle => None,
            HelperExit::NoAgent => Some("No polkit authentication agent is running in this session, so there is nobody to ask for your password. Start one (for example polkit-kde-agent, lxqt-policykit or polkit-gnome-authentication-agent-1 from your desktop's autostart) and try again.".into()),
            HelperExit::Other(t) if t.is_empty() => Some("authentication cancelled or helper exited".into()),
            HelperExit::Other(t) => Some(t.clone()),
        }
    }
}

impl Executor {
    pub fn new(tx: Sender<ExecMsg>, ctx: egui::Context) -> Executor {
        let is_root = unsafe { libc::geteuid() } == 0;
        Executor {
            next_id: 1,
            tx,
            ctx,
            is_root,
            helper_state: if is_root { HelperState::Ready } else { HelperState::NotStarted },
            helper: None,
            local_root: is_root.then(Server::default),
            pending_root: Arc::default(),
            queued: VecDeque::new(),
            user_pids: Arc::default(),
            helper_path: find_helper(),
            helper_outdated: false,
            helper_version: portagehub::protocol::parse_version(Some(env!("CARGO_PKG_VERSION"))),
        }
    }

    pub fn sink(&self) -> Sink {
        let tx = Mutex::new(self.tx.clone());
        let ctx = self.ctx.clone();
        Arc::new(move |ev| {
            let _ = tx.lock().unwrap().send(ev_msg(ev));
            ctx.request_repaint();
        })
    }

    pub fn alloc_id(&mut self) -> u64 {
        let id = self.next_id;
        self.next_id += 1;
        id
    }

    pub fn root_ready(&self) -> bool {
        self.is_root || self.helper_state == HelperState::Ready
    }

    /// Run a program; output arrives as `ExecMsg::Event`s tagged with the returned id.
    pub fn run(&mut self, argv: Vec<String>, env: Vec<(String, String)>, privilege: Priv) -> u64 {
        let id = self.alloc_id();
        match privilege {
            Priv::User => {
                let sink = self.sink();
                let pids = self.user_pids.clone();
                let wrapped: Sink = Arc::new(move |ev: Event| {
                    if let Event::Exit { id, .. } = &ev {
                        pids.lock().unwrap().remove(id);
                    }
                    sink(ev)
                });
                match spawn_streaming(id, &argv[0], &argv[1..], &env, None, wrapped) {
                    Ok(pid) => {
                        self.user_pids.lock().unwrap().insert(id, pid);
                    }
                    Err(e) => {
                        let _ = self.tx.send(ev_msg(Event::Err { id, msg: format!("{}: {e}", argv[0]) }));
                    }
                }
            }
            Priv::Root => self.submit_root(Request::new(id, Op::Run { argv, env, cwd: None })),
        }
        id
    }

    pub fn root_op(&mut self, op: Op) -> u64 {
        self.root_op_tx(op, None)
    }

    /// A root operation that belongs to transaction `tx` (from `Op::Begin`).
    pub fn root_op_tx(&mut self, op: Op, tx: Option<String>) -> u64 {
        let id = self.alloc_id();
        self.submit_root(Request { id, tx, op });
        id
    }

    pub fn root_op_with_id(&mut self, id: u64, op: Op) {
        self.submit_root(Request::new(id, op));
    }

    /// Does the running helper understand `op`?
    pub fn supports(&self, op: &Op) -> bool {
        self.is_root || op.min_helper() <= self.helper_version
    }

    pub fn cancel(&mut self, id: u64, privilege: Priv, signal: i32) {
        match privilege {
            Priv::User => {
                if let Some(pid) = self.user_pids.lock().unwrap().get(&id) {
                    unsafe {
                        libc::kill(-(*pid as i32), signal);
                    }
                }
            }
            Priv::Root => {
                let sid = self.alloc_id();
                self.submit_root(Request::new(sid, Op::Signal { target: id, signal }));
            }
        }
    }

    fn submit_root(&mut self, req: Request) {
        if let Some(server) = &self.local_root {
            server.handle(req, self.sink());
            return;
        }
        match &self.helper_state {
            HelperState::Ready => self.send_to_helper(req),
            HelperState::Starting => self.queued.push_back(req),
            HelperState::NotStarted | HelperState::Failed(_) => {
                self.queued.push_back(req);
                self.start_helper();
            }
        }
    }

    fn send_to_helper(&mut self, req: Request) {
        let id = req.id;
        if req.op.min_helper() > self.helper_version {
            let _ = self.tx.send(ev_msg(Event::Err {
                id,
                msg: "the installed admin helper is too old for this action — run sudo dist/install.sh, then restart PortageHub".into(),
            }));
            return;
        }
        let line = serde_json::to_string(&req).unwrap_or_default();
        let ok = match self.helper.as_mut() {
            Some(h) => writeln!(h.sock, "{line}").and_then(|_| h.sock.flush()).is_ok(),
            None => false,
        };
        if ok {
            self.pending_root.lock().unwrap().insert(id);
        } else {
            let _ = self.tx.send(ev_msg(Event::Err { id, msg: "root helper is not running".into() }));
            self.helper_state = HelperState::Failed("helper pipe closed".into());
        }
    }

    pub fn start_helper(&mut self) {
        let Some(path) = self.helper_path.clone() else {
            self.fail_queued("portagehub-helper binary not found next to the GUI or in /usr/libexec".into());
            return;
        };
        self.helper_state = HelperState::Starting;
        // PORTAGEHUB_ELEVATE: "pkexec" (default), "none" (run unprivileged — testing only),
        // or any launcher command such as "doas" or "sudo".
        let launcher = std::env::var("PORTAGEHUB_ELEVATE").unwrap_or_else(|_| "pkexec".into());
        let mut cmd = if launcher == "none" {
            Command::new(&path)
        } else {
            let mut parts = launcher.split_whitespace();
            let mut c = Command::new(parts.next().unwrap_or("pkexec"));
            c.args(parts).arg(&path);
            c
        };
        let (sock, child_end) = match UnixStream::pair() {
            Ok(p) => p,
            Err(e) => {
                self.fail_queued(format!("could not create the helper socket: {e}"));
                return;
            }
        };
        let child_in = match child_end.try_clone() {
            Ok(c) => c,
            Err(e) => {
                self.fail_queued(format!("could not create the helper socket: {e}"));
                return;
            }
        };
        cmd.stdin(Stdio::from(OwnedFd::from(child_in))).stdout(Stdio::from(OwnedFd::from(child_end))).stderr(Stdio::piped());
        let spawn = cmd.spawn();
        // Close our copies of the child's end, so the reader sees EOF when the helper exits.
        drop(cmd);
        let mut child = match spawn {
            Ok(c) => c,
            Err(e) => {
                self.fail_queued(format!("could not start pkexec: {e}"));
                return;
            }
        };
        let stdout = match sock.try_clone() {
            Ok(s) => s,
            Err(e) => {
                let _ = child.kill();
                self.fail_queued(format!("could not create the helper socket: {e}"));
                return;
            }
        };
        let stderr = child.stderr.take().unwrap();
        let tx = self.tx.clone();
        let ctx = self.ctx.clone();
        let pending = self.pending_root.clone();
        let err_text: Arc<Mutex<String>> = Arc::default();
        let err2 = err_text.clone();
        std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                let mut e = err2.lock().unwrap();
                if e.len() < 2000 {
                    e.push_str(&line);
                    e.push('\n');
                }
            }
        });
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                match serde_json::from_str::<Event>(&line) {
                    Ok(Event::Ready { version, warnings, .. }) => {
                        let _ = tx.send(ExecMsg::HelperReady(version, warnings));
                    }
                    Ok(Event::Notice { msg }) => {
                        let _ = tx.send(ExecMsg::Notice(msg));
                    }
                    Ok(ev) => {
                        if let Some(id) = ev.id()
                            && matches!(ev, Event::Exit { .. } | Event::Ok { .. } | Event::Err { .. })
                        {
                            pending.lock().unwrap().remove(&id);
                        }
                        let _ = tx.send(ev_msg(ev));
                    }
                    Err(_) => {}
                }
                ctx.request_repaint();
            }
            // Helper ended: fail whatever was still pending.
            std::thread::sleep(std::time::Duration::from_millis(100));
            let ids: Vec<u64> = pending.lock().unwrap().drain().collect();
            for id in ids {
                let _ = tx.send(ev_msg(Event::Err { id, msg: "root helper exited".into() }));
            }
            let exit = explain_helper_exit(&err_text.lock().unwrap());
            let _ = tx.send(ExecMsg::HelperGone(exit));
            ctx.request_repaint();
        });
        self.helper = Some(HelperConn { sock, child });
    }

    fn fail_queued(&mut self, msg: String) {
        self.helper_state = HelperState::Failed(msg.clone());
        while let Some(r) = self.queued.pop_front() {
            let _ = self.tx.send(ev_msg(Event::Err { id: r.id, msg: msg.clone() }));
        }
        self.ctx.request_repaint();
    }

    /// Called by the app when the helper reported Ready.
    pub fn on_helper_ready(&mut self, version: Option<&str>) {
        self.helper_state = HelperState::Ready;
        self.helper_outdated = version != Some(env!("CARGO_PKG_VERSION"));
        self.helper_version = portagehub::protocol::parse_version(version);
        while let Some(r) = self.queued.pop_front() {
            self.send_to_helper(r);
        }
    }

    pub fn on_helper_gone(&mut self, exit: HelperExit) {
        if self.helper.is_none() && self.helper_state == HelperState::NotStarted {
            // We closed it ourselves.
            return;
        }
        if let Some(mut h) = self.helper.take() {
            let _ = h.child.try_wait();
        }
        match exit.problem() {
            Some(reason) => self.fail_queued(reason),
            None => {
                // An idle session closed itself: the next admin action starts a new one (and asks
                // for the password again), as if none had been started yet.
                self.helper_state = HelperState::NotStarted;
                self.ctx.request_repaint();
            }
        }
    }

    pub fn stop_helper(&mut self) {
        // Closing stdin makes the helper exit once its jobs are done.
        self.helper = None;
        if !self.is_root {
            self.helper_state = HelperState::NotStarted;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_helper_says_why_it_ended() {
        assert_eq!(explain_helper_exit("portagehub-helper: idle for 15 minutes, exiting\n"), HelperExit::Idle);
        assert_eq!(explain_helper_exit("Error getting authority: ...\nNo authentication agent found.\n"), HelperExit::NoAgent);
        assert_eq!(explain_helper_exit("Error creating textual authentication agent: Error opening current controlling terminal for the process (`/dev/tty'): No such device or address\n"), HelperExit::NoAgent);
        assert_eq!(explain_helper_exit("  \n"), HelperExit::Other(String::new()));
        assert_eq!(explain_helper_exit("portagehub-helper: peer uid 1001 is not the authenticated user 1000\n"), HelperExit::Other("portagehub-helper: peer uid 1001 is not the authenticated user 1000".into()));
    }

    #[test]
    fn messages_for_the_user() {
        assert_eq!(HelperExit::Idle.problem(), None);
        let agent = HelperExit::NoAgent.problem().unwrap();
        assert!(agent.starts_with("No polkit authentication agent is running in this session") && agent.contains("polkit-kde-agent"), "{agent}");
        assert_eq!(HelperExit::Other(String::new()).problem().as_deref(), Some("authentication cancelled or helper exited"));
        assert_eq!(HelperExit::Other("boom".into()).problem().as_deref(), Some("boom"));
    }

    #[test]
    fn the_helper_is_found_where_a_prefix_install_puts_it() {
        let c = helper_candidates(Some(std::path::Path::new("/opt/ph")));
        assert_eq!(c[0], PathBuf::from("/opt/ph/portagehub-helper"));
        for want in ["/usr/libexec", "/usr/bin", "/usr/local/libexec", "/usr/local/bin", "/usr/local/sbin"] {
            assert!(c.contains(&PathBuf::from(want).join("portagehub-helper")), "{want}");
        }
        assert_eq!(helper_candidates(None).len(), 5);
    }
}
