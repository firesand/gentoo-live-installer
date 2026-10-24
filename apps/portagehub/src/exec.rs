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
    HelperReady(Option<String>),
    HelperGone(String),
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

pub fn find_helper() -> Option<PathBuf> {
    if let Some(p) = std::env::var_os("PORTAGEHUB_HELPER").map(PathBuf::from).filter(|p| p.is_absolute() && p.is_file()) {
        return Some(p);
    }
    let mut cands = Vec::new();
    if let Ok(exe) = std::env::current_exe()
        && let Some(dir) = exe.parent()
    {
        cands.push(dir.join("portagehub-helper"));
    }
    cands.push(PathBuf::from("/usr/libexec/portagehub-helper"));
    cands.push(PathBuf::from("/usr/bin/portagehub-helper"));
    cands.push(PathBuf::from("/usr/local/bin/portagehub-helper"));
    cands.into_iter().find(|p| p.is_file())
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
                    Ok(Event::Ready { version, .. }) => {
                        let _ = tx.send(ExecMsg::HelperReady(version));
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
            let reason = err_text.lock().unwrap().trim().to_string();
            let _ = tx.send(ExecMsg::HelperGone(if reason.is_empty() { "authentication cancelled or helper exited".into() } else { reason }));
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

    pub fn on_helper_gone(&mut self, reason: String) {
        if self.helper.is_none() && self.helper_state == HelperState::NotStarted {
            // We closed it ourselves.
            return;
        }
        if let Some(mut h) = self.helper.take() {
            let _ = h.child.try_wait();
        }
        self.fail_queued(reason);
    }

    pub fn stop_helper(&mut self) {
        // Closing stdin makes the helper exit once its jobs are done.
        self.helper = None;
        if !self.is_root {
            self.helper_state = HelperState::NotStarted;
        }
    }
}
