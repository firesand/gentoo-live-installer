//! Carrying out a plan as root (the helper's side): back up the files about to
//! change, stop what needs stopping, write the files atomically, make the
//! changes take effect, and record the transaction.
//!
//! With a scratch root (tests, `GENTOO_SETTINGS_ROOT`) files and the saved
//! /proc and /sys values are handled below that root, and external programs
//! are not run — they are reported as skipped.

use crate::detect::System;
use crate::history::{self, Entry};
use crate::managed;
use crate::plan::{self, Cmd, Plan, Prog};
use crate::protocol::{Event, FileAction, Request};
use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const STATE: &str = "/var/lib/gentoo-settings/state.json";
const BACKUPS: &str = "/var/lib/gentoo-settings/backups";

#[derive(Debug, Default, serde::Serialize, serde::Deserialize)]
pub struct State {
    /// Live values remembered before we changed them, by /proc or /sys path.
    #[serde(default)]
    pub saved: BTreeMap<String, String>,
    /// Blocks (tweak id, value) in each managed file after our last write.
    /// Lets the GUI know what is installed in files it cannot read
    /// (/etc/security/limits.d is often root-only).
    #[serde(default)]
    pub files: BTreeMap<String, Vec<(String, Option<String>)>>,
}

impl State {
    pub fn load(sys: &System) -> State {
        sys.read_raw(STATE).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default()
    }
}

pub fn now_ts() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

pub struct Runner<'a> {
    pub sys: &'a System,
    /// Skip external programs (scratch root).
    pub dry: bool,
    pub uid: u32,
    emit: &'a mut dyn FnMut(Event),
    warnings: Vec<String>,
}

impl<'a> Runner<'a> {
    pub fn new(sys: &'a System, dry: bool, uid: u32, emit: &'a mut dyn FnMut(Event)) -> Runner<'a> {
        Runner { sys, dry, uid, emit, warnings: Vec::new() }
    }

    fn step(&mut self, text: impl Into<String>) {
        (self.emit)(Event::Step { text: text.into() });
    }

    fn warn(&mut self, text: String) {
        self.warnings.push(text);
    }

    /// Run a request end to end. Returns whether every step succeeded.
    pub fn run(&mut self, req: &Request) -> bool {
        let plan = match plan::plan(self.sys, &req.changes) {
            Ok(p) => p,
            Err(e) => {
                (self.emit)(Event::Error { msg: e });
                return false;
            }
        };
        // Root reads everything; this only trips on a scratch root.
        if let Some(f) = plan.files.iter().find(|f| f.unreadable) {
            (self.emit)(Event::Error { msg: format!("cannot read {}", f.path) });
            return false;
        }
        let ts = now_ts();
        let backup = match self.backup(&plan, ts) {
            Ok(b) => b,
            Err(e) => {
                (self.emit)(Event::Error { msg: format!("backup failed, nothing was changed: {e}") });
                return false;
            }
        };
        let mut state = self.load_state();

        if !plan.pre.is_empty() {
            self.step("Stopping what the disabled tweaks started");
        }
        for c in &plan.pre {
            self.exec(c, &mut state);
        }

        let mut written = Vec::new();
        let mut files_ok = true;
        for fc in &plan.files {
            let action = match (&fc.old, &fc.new) {
                (_, None) => FileAction::Delete,
                (None, Some(_)) => FileAction::Create,
                (Some(_), Some(_)) => FileAction::Modify,
            };
            match self.put_file(&fc.path, fc.new.as_deref(), fc.mode) {
                Ok(()) => {
                    let blocks = fc.new.as_deref().map(managed::parse).map(|p| p.blocks.into_iter().map(|b| (b.id, b.value)).collect()).unwrap_or_default();
                    state.files.insert(fc.path.clone(), blocks);
                    written.push(fc.path.clone());
                    (self.emit)(Event::File { path: fc.path.clone(), action });
                }
                Err(e) => {
                    files_ok = false;
                    (self.emit)(Event::Error { msg: format!("{}: {e}", fc.path) });
                    break;
                }
            }
        }

        if files_ok {
            if !plan.post.is_empty() {
                self.step("Applying the changes to the running system");
            }
            for c in &plan.post {
                self.exec(c, &mut state);
            }
        }
        if let Err(e) = self.save_state(&state) {
            self.warn(format!("could not save {STATE}: {e}"));
        }
        let entry = Entry {
            ts,
            uid: self.uid,
            init: self.sys.init.label().to_string(),
            label: req.label.clone(),
            changes: req.changes.clone(),
            undo: plan.undo.clone(),
            files: written,
            backup: backup.clone(),
            ok: files_ok,
            warnings: self.warnings.clone(),
        };
        if let Err(e) = history::append(self.sys, &entry) {
            self.warn(format!("could not write the history: {e}"));
        }
        (self.emit)(Event::Done { ok: files_ok, backup, warnings: self.warnings.clone() });
        files_ok
    }

    /// Copy every file about to change into a fresh backup directory.
    fn backup(&mut self, plan: &Plan, ts: i64) -> std::io::Result<Option<String>> {
        let olds: Vec<_> = plan.files.iter().filter_map(|f| f.old.as_ref().map(|o| (f.path.as_str(), o.as_str()))).collect();
        if olds.is_empty() {
            return Ok(None);
        }
        let rel = format!("{BACKUPS}/{ts}-{}", std::process::id());
        let dir = self.sys.path(&rel);
        for (path, text) in olds {
            let dest = dir.join(path.trim_start_matches('/'));
            mkdirs(dest.parent().unwrap_or(&dir), 0o700)?;
            std::fs::write(&dest, text)?;
        }
        self.step(format!("Backed up the old files to {rel}"));
        Ok(Some(rel))
    }

    fn load_state(&self) -> State {
        State::load(self.sys)
    }

    fn save_state(&self, st: &State) -> std::io::Result<()> {
        let text = serde_json::to_string_pretty(st).map_err(std::io::Error::other)?;
        atomic_write(&self.sys.path(STATE), &text, 0o644)
    }

    fn put_file(&mut self, path: &str, content: Option<&str>, mode: u32) -> std::io::Result<()> {
        // Only our own files, whatever the plan says.
        if managed::spec(path).is_none() {
            return Err(std::io::Error::other("not a file managed by gentoo-settings"));
        }
        let p = self.sys.path(path);
        match content {
            Some(c) => atomic_write(&p, c, mode),
            None => match std::fs::remove_file(&p) {
                Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
                _ => Ok(()),
            },
        }
    }

    fn exec(&mut self, c: &Cmd, state: &mut State) {
        let text = c.describe();
        let result: Result<(String, bool), String> = match c {
            Cmd::Save { path } => self.live_path(path).map(|p| {
                if !state.saved.contains_key(path)
                    && let Ok(v) = std::fs::read_to_string(&p)
                {
                    state.saved.insert(path.clone(), crate::detect::selected(&v));
                }
                (String::new(), false)
            }),
            Cmd::Write { path, value } => self.live_path(path).and_then(|p| {
                if !state.saved.contains_key(path)
                    && let Ok(v) = std::fs::read_to_string(&p)
                {
                    state.saved.insert(path.clone(), crate::detect::selected(&v));
                }
                write_live(&p, value).map(|_| (String::new(), false))
            }),
            Cmd::Restore { path } => self.live_path(path).and_then(|p| match state.saved.remove(path) {
                Some(v) => write_live(&p, &v).map(|_| (format!("put back {v}"), false)),
                None => Ok(("nothing was remembered".into(), false)),
            }),
            Cmd::Exec { prog, args } => self.run_prog(prog, args),
        };
        match result {
            // Remembering a value is bookkeeping, not worth a log line.
            Ok(_) if matches!(c, Cmd::Save { .. }) => {}
            Ok((output, skipped)) => (self.emit)(Event::Command { text, ok: true, output, skipped }),
            Err(e) => {
                self.warn(format!("{text}: {e}"));
                (self.emit)(Event::Command { text, ok: false, output: e, skipped: false });
            }
        }
    }

    fn live_path(&self, path: &str) -> Result<PathBuf, String> {
        if !(path.starts_with("/proc/sys/") || path.starts_with("/sys/")) || path.contains("..") {
            return Err(format!("{path} is not a kernel setting"));
        }
        Ok(self.sys.path(path))
    }

    fn run_prog(&mut self, prog: &Prog, args: &[String]) -> Result<(String, bool), String> {
        let exe = match prog {
            Prog::Script(p) => {
                if managed::spec(p).is_none_or(|s| s.mode & 0o111 == 0) {
                    return Err(format!("{p} is not one of our scripts"));
                }
                p.clone()
            }
            other => {
                let name = other.name();
                match crate::PROGRAM_DIRS.iter().map(|d| format!("{d}/{name}")).find(|p| Path::new(p).is_file()) {
                    Some(p) => p,
                    None if self.dry => name.to_string(),
                    None => return Err(format!("{name} is not installed")),
                }
            }
        };
        if self.dry {
            return Ok(("not run (scratch root)".into(), true));
        }
        run_with_timeout(&exe, args, Duration::from_secs(120)).map(|o| (o, false))
    }
}

fn mkdirs(dir: &Path, mode: u32) -> std::io::Result<()> {
    std::fs::DirBuilder::new().recursive(true).mode(mode).create(dir)
}

/// Write via a temporary file in the same directory and rename over the target.
pub fn atomic_write(path: &Path, content: &str, mode: u32) -> std::io::Result<()> {
    let dir = path.parent().ok_or_else(|| std::io::Error::other("no parent directory"))?;
    mkdirs(dir, 0o755)?;
    let name = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    let tmp = dir.join(format!(".{name}.gentoo-settings-{}", std::process::id()));
    let _ = std::fs::remove_file(&tmp);
    let res = (|| {
        let mut f = std::fs::OpenOptions::new().write(true).create_new(true).mode(mode).custom_flags(libc::O_NOFOLLOW).open(&tmp)?;
        f.write_all(content.as_bytes())?;
        f.set_permissions(std::fs::Permissions::from_mode(mode))?;
        f.sync_all()?;
        std::fs::rename(&tmp, path)
    })();
    if res.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    res
}

fn write_live(p: &Path, value: &str) -> Result<(), String> {
    let mut f = std::fs::OpenOptions::new().write(true).truncate(true).open(p).map_err(|e| format!("{}: {e}", p.display()))?;
    f.write_all(value.as_bytes()).map_err(|e| format!("{}: {e}", p.display()))
}

fn run_with_timeout(exe: &str, args: &[String], limit: Duration) -> Result<String, String> {
    let mut child = Command::new(exe)
        .args(args)
        .env_clear()
        .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        .env("LC_ALL", "C")
        .current_dir("/")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("cannot start {exe}: {e}"))?;
    // One reader per pipe so neither can fill up and block the child.
    let drain = |p: Option<Box<dyn Read + Send>>| {
        std::thread::spawn(move || {
            let mut s = String::new();
            if let Some(mut p) = p {
                let _ = p.read_to_string(&mut s);
            }
            s
        })
    };
    let out = drain(child.stdout.take().map(|o| Box::new(o) as Box<dyn Read + Send>));
    let err = drain(child.stderr.take().map(|e| Box::new(e) as Box<dyn Read + Send>));
    let start = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break st,
            Ok(None) if start.elapsed() > limit => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("timed out after {}s", limit.as_secs()));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(50)),
            Err(e) => return Err(e.to_string()),
        }
    };
    let output = format!("{}\n{}", out.join().unwrap_or_default().trim(), err.join().unwrap_or_default().trim()).trim().to_string();
    if status.success() { Ok(output) } else { Err(if output.is_empty() { format!("exited with {status}") } else { output }) }
}
