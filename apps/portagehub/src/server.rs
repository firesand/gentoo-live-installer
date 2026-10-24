//! Request handling shared by the root helper and by the GUI when it already
//! runs as root (then no helper process is needed).
//!
//! File changes are confined with [`Beneath`] (no symlink or `..` escapes),
//! checked against [`policy`], recorded in a [`Journal`] transaction so they
//! can be rolled back or undone, and every request is written to the
//! [`Audit`] log.

use crate::audit::{Audit, Record};
use crate::journal::Journal;
use crate::policy;
use crate::protocol::{self, Event, Expect, KERNEL_STATE_DIRS, Op, Request, StreamKind, TxInfo};
use crate::rootops::{self, at};
use crate::safefs::Beneath;
use crate::stream::spawn_streaming;
use std::collections::HashMap;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

pub type Sink = Arc<dyn Fn(Event) + Send + Sync>;

#[derive(Clone)]
pub struct Server {
    pgids: Arc<Mutex<HashMap<u64, u32>>>,
    running: Arc<Mutex<usize>>,
    root: Arc<PathBuf>,
    journal: Arc<Journal>,
    audit: Arc<Audit>,
    /// The audit log stopped recording and the user has been told.
    audit_notified: Arc<AtomicBool>,
}

impl Default for Server {
    fn default() -> Server {
        Server::new(system_root())
    }
}

/// The filesystem root to work on: `/`, or `PORTAGEHUB_ROOT` (a scratch tree
/// for tests), which is only honoured when not running as root.
pub fn system_root() -> PathBuf {
    if unsafe { libc::geteuid() } != 0
        && let Some(r) = std::env::var_os("PORTAGEHUB_ROOT").map(PathBuf::from).filter(|r| r.is_absolute())
    {
        return r;
    }
    PathBuf::from("/")
}

fn es(e: io::Error) -> String {
    e.to_string()
}

/// One line for the audit log (never file contents).
pub fn describe(op: &Op) -> String {
    let mut s = match op {
        Op::Ping => "ping".into(),
        Op::Run { argv, env, .. } => {
            let env: String = env.iter().map(|(k, v)| format!("{k}={v} ")).collect();
            format!("run {env}{}", argv.join(" "))
        }
        Op::WriteFile { path, content, backup, .. } => format!("write {path} ({} bytes{})", content.len(), if *backup { ", backup" } else { "" }),
        Op::DeleteFile { path, .. } => format!("delete {path}"),
        Op::Mkdir { path } => format!("mkdir {path}"),
        Op::Rename { from, to, .. } => format!("rename {from} → {to}"),
        Op::Signal { target, signal } => format!("signal {signal} → request {target}"),
        Op::ConfigUpdate { path, accept, .. } => format!("{} config update {path}", if *accept { "accept" } else { "discard" }),
        Op::BootScan => "boot scan".into(),
        Op::BootAction { action, kver } => format!("{}{}", action.label(), kver.as_deref().map(|k| format!(" for {k}")).unwrap_or_default()),
        Op::MarkGood { kver, state_dir, backup } => format!("mark {kver} known-good in {state_dir}{}", if *backup { " (with /boot backup)" } else { "" }),
        Op::RemoveKernelLeftovers { kver, old_only } => format!("remove {}files of kernel {kver}", if *old_only { ".old " } else { "" }),
        Op::CleanBuildDirs => "clean leftover build directories".into(),
        Op::Remount { target, rw } => format!("remount {target} {}", if *rw { "rw" } else { "ro" }),
        Op::Begin { label } => format!("begin: {label}"),
        Op::Commit { tx } => format!("commit {tx}"),
        Op::Rollback { tx, force } => format!("undo {tx}{}", if *force { " (forced)" } else { "" }),
        Op::ListTx => "list recent changes".into(),
    };
    if s.len() > 600 {
        let mut cut = 600;
        while !s.is_char_boundary(cut) {
            cut -= 1;
        }
        s.truncate(cut);
        s.push('…');
    }
    s
}

impl Server {
    pub fn new(root: PathBuf) -> Server {
        Server {
            pgids: Arc::default(),
            running: Arc::default(),
            journal: Arc::new(Journal::new(&root)),
            audit: Arc::new(Audit::open(&root)),
            audit_notified: Arc::new(AtomicBool::new(false)),
            root: Arc::new(root),
        }
    }

    /// Why the audit log may be missing records (it could not be opened or written), if it may.
    pub fn audit_problem(&self) -> Option<String> {
        self.audit.problem()
    }

    /// [`Server::audit_problem`], for the caller that tells the user now: no separate `Notice` follows
    /// for a problem that was already reported.
    pub fn report_audit_problem(&self) -> Option<String> {
        let problem = self.audit.problem()?;
        self.audit_notified.store(true, Ordering::SeqCst);
        Some(problem)
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn running(&self) -> usize {
        *self.running.lock().unwrap()
    }

    pub fn handle(&self, req: Request, sink: Sink) {
        let Request { id, tx, op } = req;
        if let Some(why) = self.audit.problem()
            && !self.audit_notified.swap(true, Ordering::SeqCst)
        {
            // Fail-open on purpose (an unwritable log must not stop an upgrade), but never silent.
            sink(Event::Notice { msg: format!("Audit log: not recording ({why})") });
        }
        let sink = if matches!(op, Op::Ping) {
            sink
        } else {
            self.audit.log(Record { id, event: "request".into(), tx: tx.clone(), what: describe(&op), ..Default::default() });
            self.audited(id, tx.clone(), sink)
        };
        let reply = |r: Result<(), String>| {
            sink(match r {
                Ok(()) => Event::Ok { id },
                Err(msg) => Event::Err { id, msg },
            })
        };
        let tx = tx.as_deref();
        match op {
            Op::Ping => reply(Ok(())),
            Op::Run { argv, env, cwd: _ } => {
                let Some(first) = argv.first() else {
                    return reply(Err("empty command".into()));
                };
                let Some(program) = resolve_program(first) else {
                    return reply(Err(format!("program not allowed: {first}")));
                };
                let base = Path::new(&program).file_name().map(|b| b.to_string_lossy().to_string()).unwrap_or_default();
                if let Err(e) = policy::check_args(&base, &argv[1..]) {
                    return reply(Err(e));
                }
                if let Some((k, _)) = env.iter().find(|(k, v)| !policy::env_allowed(k) || v.contains('\0')) {
                    return reply(Err(format!("environment variable not allowed: {k}")));
                }
                self.spawn_tracked(id, &program, &argv[1..], &env, sink);
            }
            Op::BootScan => {
                let scan = rootops::boot_scan(&self.root);
                sink(Event::Data { id, text: serde_json::to_string(&scan).unwrap_or_default() });
                reply(Ok(()));
            }
            Op::BootAction { action, kver } => match rootops::boot_action_argv(&self.root, action, kver.as_deref()) {
                Ok((program, args)) => self.spawn_tracked(id, &program, &args, &[], sink),
                Err(e) => reply(Err(e)),
            },
            Op::MarkGood { kver, state_dir, backup } => {
                let me = self.clone();
                let tx = tx.map(String::from);
                self.blocking(id, sink, move |log| me.mark_good(id, tx.as_deref(), &kver, &state_dir, backup, log));
            }
            Op::RemoveKernelLeftovers { kver, old_only } => {
                let root = self.root.clone();
                self.blocking(id, sink, move |log| rootops::remove_kernel_leftovers(&root, &kver, old_only, log));
            }
            Op::Remount { target, rw } => match rootops::remount_argv(&self.root, &target, rw) {
                Ok((program, args)) => self.spawn_tracked(id, &program, &args, &[], sink),
                Err(e) => reply(Err(e)),
            },
            Op::CleanBuildDirs => {
                let root = self.root.clone();
                self.blocking(id, sink, move |log| rootops::clean_build_dirs(&root, log));
            }
            Op::WriteFile { path, content, backup, expect } => reply(self.write_file_for(id, tx, &path, content.as_bytes(), backup, expect)),
            Op::DeleteFile { path, expect } => reply(self.delete_file(id, tx, &path, expect)),
            Op::Mkdir { path } => reply(self.mkdir(id, tx, &path)),
            Op::Rename { from, to, expect } => reply(self.rename(id, tx, &from, &to, expect)),
            Op::ConfigUpdate { path, accept, expect } => reply(self.config_update(id, tx, &path, accept, expect)),
            Op::Signal { signal, .. } if !protocol::ALLOWED_SIGNALS.contains(&signal) => reply(Err(format!("signal not allowed: {signal}"))),
            Op::Signal { target, signal } => {
                let pid = self.pgids.lock().unwrap().get(&target).copied();
                match pid {
                    Some(pid) => {
                        let rc = unsafe { libc::kill(-(pid as i32), signal) };
                        reply(if rc == 0 { Ok(()) } else { Err(io::Error::last_os_error().to_string()) })
                    }
                    None => reply(Err("no such running job".into())),
                }
            }
            Op::Begin { label } => match self.journal.begin(&label) {
                Ok(t) => {
                    self.audit.log(Record { id, event: "begin".into(), tx: Some(t.clone()), what: label, ..Default::default() });
                    sink(Event::Data { id, text: t });
                    reply(Ok(()));
                }
                Err(e) => reply(Err(format!("cannot start a transaction: {e}"))),
            },
            Op::ListTx => {
                sink(Event::Data { id, text: serde_json::to_string(&self.list_tx()).unwrap_or_else(|_| "[]".into()) });
                reply(Ok(()));
            }
            Op::Commit { tx: t } => reply(self.commit(id, &t)),
            Op::Rollback { tx: t, force } => match self.journal.rollback(&t, force, &|p| self.confine(p)) {
                Ok((m, done)) => {
                    for text in &done {
                        sink(Event::Line { id, stream: StreamKind::Stdout, text: text.clone() });
                    }
                    self.audit.log(Record { id, event: "rollback".into(), tx: Some(t), what: m.label.clone(), files: m.files(), ..Default::default() });
                    reply(Ok(()));
                }
                Err(e) => reply(Err(e.to_string())),
            },
        }
    }

    /// Log how a request ended.
    fn audited(&self, id: u64, tx: Option<String>, sink: Sink) -> Sink {
        let audit = self.audit.clone();
        Arc::new(move |ev: Event| {
            let done = match &ev {
                Event::Ok { .. } => Some((true, String::new())),
                Event::Err { msg, .. } => Some((false, msg.clone())),
                Event::Exit { code, signal, .. } => Some((
                    *code == Some(0),
                    match (code, signal) {
                        (Some(c), _) => format!("exit {c}"),
                        (None, Some(s)) => format!("signal {s}"),
                        _ => String::new(),
                    },
                )),
                _ => None,
            };
            if let Some((ok, detail)) = done {
                audit.log(Record { id, event: "done".into(), tx: tx.clone(), ok: Some(ok), detail, ..Default::default() });
            }
            sink(ev)
        })
    }

    // ------------------------------------------------------------------
    // Transactions

    /// The directory confining a logical path, for rollback.
    fn confine(&self, logical: &str) -> io::Result<(Beneath, PathBuf)> {
        let p = Path::new(logical);
        for base in ["/etc/portage"].iter().chain(KERNEL_STATE_DIRS).chain(["/etc"].iter()) {
            if let Ok(rel) = p.strip_prefix(base)
                && !rel.as_os_str().is_empty()
            {
                return Ok((Beneath::open(&at(&self.root, base))?, rel.to_path_buf()));
            }
        }
        Err(io::Error::new(io::ErrorKind::PermissionDenied, format!("path not allowed: {logical}")))
    }

    fn commit(&self, id: u64, tx: &str) -> Result<(), String> {
        let m = self.journal.commit(tx, &|p| self.confine(p)).map_err(es)?;
        self.audit.log(Record { id, event: "commit".into(), tx: Some(tx.into()), what: m.label.clone(), files: m.files(), ..Default::default() });
        Ok(())
    }

    /// Run `f` in transaction `tx`, or in a transaction of its own that is
    /// committed on success and rolled back on failure.
    fn in_tx(&self, id: u64, tx: Option<&str>, label: &str, f: impl FnOnce(&str) -> Result<(), String>) -> Result<(), String> {
        if let Some(t) = tx {
            return f(t);
        }
        let t = self.journal.begin(label).map_err(|e| format!("cannot record the change for undo: {e}"))?;
        match f(&t) {
            Ok(()) => self.commit(id, &t),
            Err(e) => {
                let _ = self.journal.rollback(&t, true, &|p| self.confine(p));
                Err(e)
            }
        }
    }

    /// Record the directories that creating `rel` (and, with `this`, `rel` itself) will add.
    fn record_new_dirs(&self, tx: &str, base: &Beneath, logical_base: &str, rel: &Path, this: bool) -> Result<(), String> {
        let mut cur = PathBuf::new();
        let comps: Vec<_> = rel.components().collect();
        let n = if this { comps.len() } else { comps.len().saturating_sub(1) };
        for c in &comps[..n] {
            cur.push(c);
            if !base.exists(&cur).map_err(es)? {
                self.journal.record_dir_created(tx, &format!("{logical_base}/{}", cur.display())).map_err(es)?;
            }
        }
        Ok(())
    }

    fn record(&self, tx: &str, logical_base: &str, base: &Beneath, rel: &Path) -> Result<(), String> {
        self.journal.record(tx, &format!("{logical_base}/{}", rel.display()), base, rel).map_err(|e| format!("cannot save {logical_base}/{} for undo: {e}", rel.display()))
    }

    // ------------------------------------------------------------------
    // File operations

    /// A writable path below /etc/portage: its confining directory and relative path.
    fn portage(&self, path: &str) -> Result<(Beneath, PathBuf), String> {
        let rel = protocol::portage_rel(path).ok_or_else(|| format!("path not allowed: {path}"))?;
        if !policy::portage_write_allowed(&rel.to_string_lossy()) {
            return Err(format!("path not allowed: {path} (shell hooks, env/<category>/<package>, patches/ and ._cfg files are left to Portage)"));
        }
        let base = Beneath::open(&at(&self.root, "/etc/portage")).map_err(|e| format!("/etc/portage: {e}"))?;
        Ok((base, rel))
    }

    /// Recent transactions, newest first (the journal itself is root-only).
    pub fn list_tx(&self) -> Vec<TxInfo> {
        self.journal
            .list()
            .into_iter()
            .map(|m| TxInfo {
                files: m.files(),
                id: m.id,
                label: m.label,
                created: m.created,
                state: match m.state {
                    crate::journal::TxState::Open => "open",
                    crate::journal::TxState::Committed => "committed",
                    crate::journal::TxState::RolledBack => "rolled_back",
                }
                .into(),
            })
            .collect()
    }

    /// Refuse when the file is no longer what the caller last saw, so a change made outside
    /// PortageHub in the meantime is never silently overwritten (or deleted, or moved).
    fn check_expect(b: &Beneath, rel: &Path, path: &str, expect: Option<Expect>) -> Result<(), String> {
        let Some(expect) = expect else { return Ok(()) };
        let current = b.read(rel).map_err(es)?;
        if expect.holds(current.as_deref()) { Ok(()) } else { Err(format!("changed on disk: {path}")) }
    }

    /// [`Server::write_file_for`] outside of any request (tests, tools).
    pub fn write_file(&self, tx: Option<&str>, path: &str, content: &[u8], backup: bool, expect: Option<Expect>) -> Result<(), String> {
        self.write_file_for(0, tx, path, content, backup, expect)
    }

    /// Replace a file below /etc/portage for request `id` (which the audit log's commit record carries).
    pub fn write_file_for(&self, id: u64, tx: Option<&str>, path: &str, content: &[u8], backup: bool, expect: Option<Expect>) -> Result<(), String> {
        const P: &str = "/etc/portage";
        if content.len() > protocol::MAX_WRITE {
            return Err(format!("{path}: {} bytes is more than the {} MiB a configuration file may have", content.len(), protocol::MAX_WRITE >> 20));
        }
        let (b, rel) = self.portage(path)?;
        if b.is_dir(&rel).map_err(es)? {
            return Err(format!("{path} is a directory"));
        }
        Server::check_expect(&b, &rel, path, expect)?;
        self.in_tx(id, tx, &format!("write {path}"), |t| {
            self.record_new_dirs(t, &b, P, &rel, false)?;
            if backup && let Some(old) = b.read(&rel).map_err(es)? {
                // `~` suffix: Portage ignores these files.
                let mut bk = rel.clone().into_os_string();
                bk.push("~");
                let bk = PathBuf::from(bk);
                self.record(t, P, &b, &bk)?;
                b.write_atomic(&bk, &old).map_err(es)?;
            }
            self.record(t, P, &b, &rel)?;
            b.write_atomic(&rel, content).map_err(es)
        })
    }

    fn delete_file(&self, id: u64, tx: Option<&str>, path: &str, expect: Option<Expect>) -> Result<(), String> {
        let (b, rel) = self.portage(path)?;
        if b.is_dir(&rel).map_err(es)? {
            return Err(format!("{path} is a directory"));
        }
        if !b.exists(&rel).map_err(es)? {
            return Err(format!("{path} does not exist"));
        }
        Server::check_expect(&b, &rel, path, expect)?;
        self.in_tx(id, tx, &format!("delete {path}"), |t| {
            self.record(t, "/etc/portage", &b, &rel)?;
            b.unlink(&rel).map_err(es)
        })
    }

    fn mkdir(&self, id: u64, tx: Option<&str>, path: &str) -> Result<(), String> {
        let (b, rel) = self.portage(path)?;
        if b.is_dir(&rel).map_err(es)? {
            return Ok(());
        }
        self.in_tx(id, tx, &format!("mkdir {path}"), |t| {
            self.record_new_dirs(t, &b, "/etc/portage", &rel, true)?;
            b.mkdirs(&rel).map_err(es)
        })
    }

    fn rename(&self, id: u64, tx: Option<&str>, from: &str, to: &str, expect: Option<Expect>) -> Result<(), String> {
        const P: &str = "/etc/portage";
        let (b, fr) = self.portage(from)?;
        let (_, tr) = self.portage(to)?;
        if b.exists(&tr).map_err(es)? {
            return Err(format!("{to} already exists"));
        }
        if !b.exists(&fr).map_err(es)? {
            return Err(format!("{from} does not exist"));
        }
        if b.is_dir(&fr).map_err(es)? {
            return Err(format!("{from} is a directory; only files can be moved"));
        }
        Server::check_expect(&b, &fr, from, expect)?;
        self.in_tx(id, tx, &format!("rename {from} → {to}"), |t| {
            self.record_new_dirs(t, &b, P, &tr, false)?;
            self.record(t, P, &b, &fr)?;
            self.record(t, P, &b, &tr)?;
            b.rename_noreplace(&fr, &tr).map_err(es)
        })
    }

    fn config_update(&self, id: u64, tx: Option<&str>, path: &str, accept: bool, expect: Option<Expect>) -> Result<(), String> {
        const E: &str = "/etc";
        let (upd, target) = protocol::config_update_paths(path).ok_or_else(|| format!("not a config update file: {path}"))?;
        let b = Beneath::open(&at(&self.root, E)).map_err(|e| format!("/etc: {e}"))?;
        if !b.exists(&upd).map_err(es)? || b.is_dir(&upd).map_err(es)? {
            return Err(format!("{path} does not exist"));
        }
        if accept {
            // The diff the user approved was against the file as it was then.
            Server::check_expect(&b, &target, &format!("/etc/{}", target.display()), expect)?;
        }
        let label = format!("{} config update {path}", if accept { "accept" } else { "discard" });
        self.in_tx(id, tx, &label, |t| {
            if accept {
                self.record(t, E, &b, &target)?;
                self.record(t, E, &b, &upd)?;
                b.rename_replace(&upd, &target).map_err(es)
            } else {
                self.record(t, E, &b, &upd)?;
                b.unlink(&upd).map_err(es)
            }
        })
    }

    fn mark_good(&self, id: u64, tx: Option<&str>, kver: &str, state_dir: &str, backup: bool, log: &dyn Fn(String)) -> Result<(), String> {
        if !protocol::valid_kver(kver) {
            return Err(format!("invalid kernel version {kver}"));
        }
        let dir = rootops::state_dir_handle(&self.root, state_dir)?;
        self.in_tx(id, tx, &format!("mark kernel {kver} known-good"), |t| {
            for f in ["last-good", "previous-good"] {
                self.record(t, state_dir, &dir, Path::new(f))?;
            }
            rootops::mark_good(&self.root, kver, state_dir, backup, log)
        })
    }

    // ------------------------------------------------------------------
    // Processes

    fn spawn_tracked(&self, id: u64, program: &str, args: &[String], env: &[(String, String)], sink: Sink) {
        let pg = self.pgids.clone();
        let run = self.running.clone();
        *self.running.lock().unwrap() += 1;
        let inner = sink.clone();
        let wrapped: Sink = Arc::new(move |ev: Event| {
            if let Event::Exit { id, .. } = &ev {
                pg.lock().unwrap().remove(id);
                let mut r = run.lock().unwrap();
                *r = r.saturating_sub(1);
            }
            inner(ev);
        });
        match spawn_streaming(id, program, args, env, Some("/"), wrapped) {
            Ok(pid) => {
                self.pgids.lock().unwrap().insert(id, pid);
            }
            Err(e) => {
                let mut r = self.running.lock().unwrap();
                *r = r.saturating_sub(1);
                drop(r);
                sink(Event::Err { id, msg: format!("failed to start {program}: {e}") });
            }
        }
    }

    /// Run a slow operation on its own thread, streaming log lines, ending in Ok/Err.
    fn blocking(&self, id: u64, sink: Sink, f: impl FnOnce(&dyn Fn(String)) -> Result<(), String> + Send + 'static) {
        let run = self.running.clone();
        *run.lock().unwrap() += 1;
        std::thread::spawn(move || {
            let s2 = sink.clone();
            let log = move |text: String| s2(Event::Line { id, stream: StreamKind::Stdout, text });
            let r = f(&log);
            sink(match r {
                Ok(()) => Event::Ok { id },
                Err(msg) => Event::Err { id, msg },
            });
            let mut r = run.lock().unwrap();
            *r = r.saturating_sub(1);
        });
    }
}

pub fn resolve_program(name: &str) -> Option<String> {
    let base = Path::new(name).file_name()?.to_str()?;
    if !protocol::ALLOWED_PROGRAMS.contains(&base) {
        return None;
    }
    protocol::PROGRAM_DIRS
        .iter()
        .map(|d| PathBuf::from(d).join(base))
        .find(|p| p.is_file())
        .map(|p| p.to_string_lossy().into_owned())
}

#[cfg(test)]
mod cancellation_tests {
    use super::*;
    use std::sync::mpsc;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    struct GroupCleanup(Option<u32>);

    impl Drop for GroupCleanup {
        fn drop(&mut self) {
            let Some(pid) = self.0.take() else { return };
            unsafe { libc::kill(-(pid as i32), libc::SIGKILL) };
            let deadline = Instant::now() + Duration::from_secs(5);
            while Instant::now() < deadline {
                let alive = unsafe { libc::kill(pid as i32, 0) } == 0;
                if !alive {
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        }
    }

    #[test]
    fn signal_cancels_ready_process_group_fixture() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root =
            std::env::temp_dir().join(format!("portagehub-cancel-{}-{nonce}", std::process::id()));
        std::fs::create_dir_all(&root).expect("create isolated audit root");
        struct DropRoot(std::path::PathBuf);
        impl Drop for DropRoot {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
        let _root_cleanup = DropRoot(root.clone());

        let server = Server::new(root);
        let (tx, rx) = mpsc::channel();
        let sink: Sink = Arc::new(move |event| {
            let _ = tx.send(event);
        });
        // A POSIX shell is all this needs. A shell runs a trap between commands, never inside one,
        // and READY is seen before the first `sleep` is even forked: a signal sent at once can
        // reach that child while it is still a copy of the shell (it then runs the shell's
        // handler and goes on to sleep the full time). The loop makes the trap run within a
        // second whichever way the race goes; with a single long `sleep` this test was flaky.
        let fixture = "trap 'exit 42' INT; echo READY; while :; do sleep 1; done; echo not reached";
        server.spawn_tracked(
            1,
            "/bin/sh",
            &["-c".into(), fixture.into()],
            &[],
            sink.clone(),
        );

        let deadline = Instant::now() + Duration::from_secs(5);
        let mut cleanup = GroupCleanup(None);
        let mut ready = false;
        while Instant::now() < deadline {
            match rx.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
                Ok(Event::Started { id: 1, pid }) => cleanup.0 = Some(pid),
                Ok(Event::Line { id: 1, text, .. }) if text == "READY" => {
                    ready = true;
                    break;
                }
                Ok(Event::Exit {
                    id: 1,
                    code,
                    signal,
                }) => panic!("fixture exited before cancellation: {code:?}/{signal:?}"),
                Ok(_) => {}
                Err(e) => panic!("fixture readiness timeout: {e}"),
            }
        }
        assert!(
            ready,
            "fixture did not establish readiness within 5 seconds"
        );

        // spawn_tracked has returned and emitted READY, so its process-group
        // id is installed. Send exactly one signal and accept either ordering
        // between the synchronous acknowledgement and asynchronous Exit event.
        server.handle(
            Request::new(
                2,
                Op::Signal {
                    target: 1,
                    signal: libc::SIGINT,
                },
            ),
            sink.clone(),
        );

        let exit_deadline = Instant::now() + Duration::from_secs(5);
        let mut signal_ok = false;
        let mut exit = None;
        while Instant::now() < exit_deadline && (!signal_ok || exit.is_none()) {
            match rx.recv_timeout(exit_deadline.saturating_duration_since(Instant::now())) {
                Ok(Event::Ok { id: 2 }) => signal_ok = true,
                Ok(Event::Err { id: 2, msg }) => panic!("signal request failed: {msg}"),
                Ok(Event::Exit {
                    id: 1,
                    code,
                    signal,
                }) => {
                    exit = Some((code, signal));
                    cleanup.0 = None;
                }
                Ok(_) => {}
                Err(e) => panic!("signal acknowledgement/fixture exit timeout: {e}"),
            }
        }
        assert!(signal_ok, "server did not acknowledge SIGINT within 5 seconds");
        assert_eq!(
            exit,
            Some((Some(42), None)),
            "SIGINT handler should produce its explicit cancellation status"
        );
        assert_eq!(
            server.running(),
            0,
            "completed fixture should be removed from running count"
        );
    }
}
