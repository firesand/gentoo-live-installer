//! Running prepared jobs and turning their output into structured events.

use anyhow::{Context, Result};
use kernelsmith_core::job::PreparedJob;
use kernelsmith_core::util::{is_root, which};
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{channel, Receiver};

#[derive(Debug, Clone)]
pub enum Event {
    /// Ordinary output line (compiler output, emerge, …).
    Line(String),
    Plan(Vec<String>),
    Stage { id: String, label: String },
    Note(String),
    Warn(String),
    Error(String),
    Verify { ok: bool, sym: String, want: String, got: String, title: String },
    ConfigStats { y: u64, m: u64 },
    ModRebuild(bool),
    Profile(PathBuf),
    Done(String),
}

pub fn parse_line(line: &str) -> Event {
    let Some(rest) = line.strip_prefix("::") else { return Event::Line(line.to_string()) };
    let mut parts = rest.splitn(2, "::");
    let kind = parts.next().unwrap_or("");
    let body = parts.next().unwrap_or("").to_string();
    match kind {
        "plan" => Event::Plan(body.split(',').filter(|s| !s.is_empty()).map(String::from).collect()),
        "stage" => {
            let (id, label) = body.split_once("::").unwrap_or((&body, ""));
            Event::Stage { id: id.into(), label: label.into() }
        }
        "note" => Event::Note(body),
        "warn" => Event::Warn(body),
        "error" => Event::Error(body),
        "verify" => {
            let f: Vec<&str> = body.splitn(5, "::").collect();
            if f.len() == 5 {
                Event::Verify { ok: f[0] == "ok", sym: f[1].into(), want: f[2].into(), got: f[3].into(), title: f[4].into() }
            } else {
                Event::Line(line.into())
            }
        }
        "config-stats" => {
            let (y, m) = body.split_once("::").unwrap_or(("0", "0"));
            Event::ConfigStats { y: y.parse().unwrap_or(0), m: m.parse().unwrap_or(0) }
        }
        "modrebuild" => Event::ModRebuild(body == "1"),
        "profile" => Event::Profile(PathBuf::from(body)),
        "done" => Event::Done(body),
        _ => Event::Line(line.to_string()),
    }
}

/// Is this a kbuild "compiled one object" line?
pub fn is_compile_line(line: &str) -> bool {
    line.starts_with("  CC ") || line.starts_with("  CC [M]")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Elevation {
    /// Ask for the password with polkit (KDE/GNOME show a dialog).
    Pkexec,
    /// Already root, or the job doesn't need root.
    Direct,
}

pub fn pick_elevation(needs_root: bool) -> Result<Elevation> {
    if !needs_root || is_root() {
        return Ok(Elevation::Direct);
    }
    if which("pkexec").is_some() {
        Ok(Elevation::Pkexec)
    } else {
        anyhow::bail!("pkexec (sys-auth/polkit) is not installed. Run the job from a terminal instead: sudo kernelsmith run-job <script>")
    }
}

pub struct Runner {
    child: Child,
    stdin: Option<ChildStdin>,
    pub rx: Receiver<Event>,
}

impl Runner {
    pub fn start(job: &PreparedJob, elevation: Elevation) -> Result<Runner> {
        let exe = std::env::current_exe().context("locating the kernelsmith binary")?;
        let mut cmd = match elevation {
            Elevation::Pkexec => {
                let mut c = Command::new("pkexec");
                c.arg(&exe);
                c
            }
            Elevation::Direct => Command::new(&exe),
        };
        cmd.arg("run-job")
            .arg(&job.script)
            .arg("--log")
            .arg(&job.log)
            .arg("--cancel-on-eof")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = cmd.spawn().context("starting the job")?;
        let stdin = child.stdin.take();
        let (tx, rx) = channel();
        if let Some(out) = child.stdout.take() {
            let tx = tx.clone();
            std::thread::spawn(move || {
                for line in BufReader::new(out).lines().map_while(Result::ok) {
                    if tx.send(parse_line(&line)).is_err() {
                        break;
                    }
                }
            });
        }
        if let Some(err) = child.stderr.take() {
            let tx = tx.clone();
            std::thread::spawn(move || {
                for line in BufReader::new(err).lines().map_while(Result::ok) {
                    // pkexec reports a dismissed password dialog on stderr.
                    let ev = if line.contains("Not authorized") || line.contains("Request dismissed") {
                        Event::Error("Authentication was cancelled.".into())
                    } else {
                        Event::Line(line)
                    };
                    if tx.send(ev).is_err() {
                        break;
                    }
                }
            });
        }
        Ok(Runner { child, stdin, rx })
    }

    /// Ask the helper to stop the job (it signals the whole process group).
    pub fn cancel(&mut self) {
        if let Some(mut s) = self.stdin.take() {
            let _ = s.write_all(b"cancel\n");
            let _ = s.flush();
        }
    }

    /// `Some(code)` once the process has exited.
    pub fn poll_exit(&mut self) -> Option<Option<i32>> {
        match self.child.try_wait() {
            Ok(Some(status)) => Some(status.code()),
            Ok(None) => None,
            Err(_) => Some(None),
        }
    }
}

impl Drop for Runner {
    fn drop(&mut self) {
        self.cancel();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn markers() {
        match parse_line("::stage::build::Compiling the kernel") {
            Event::Stage { id, label } => {
                assert_eq!(id, "build");
                assert_eq!(label, "Compiling the kernel");
            }
            e => panic!("{e:?}"),
        }
        match parse_line("::verify::miss::NTSYNC::y::n::NTSYNC: On") {
            Event::Verify { ok, sym, want, got, title } => {
                assert!(!ok);
                assert_eq!((sym.as_str(), want.as_str(), got.as_str(), title.as_str()), ("NTSYNC", "y", "n", "NTSYNC: On"));
            }
            e => panic!("{e:?}"),
        }
        assert!(matches!(parse_line("  CC      kernel/fork.o"), Event::Line(_)));
        assert!(is_compile_line("  CC [M]  fs/btrfs/super.o"));
        assert!(!is_compile_line("  LD      vmlinux"));
        assert!(matches!(parse_line("::plan::a,b"), Event::Plan(v) if v == vec!["a", "b"]));
    }
}
