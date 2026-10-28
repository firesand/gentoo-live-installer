//! Running the privileged helper for one transaction: through `pkexec` (one
//! polkit prompt; `auth_admin_keep` spares a second one for a few minutes),
//! directly when the GUI already runs as root, or on a scratch tree when
//! `GENTOO_SETTINGS_ROOT` is set.

use gentoo_settings::protocol::{Event, Request};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc::Sender;

#[derive(Debug)]
pub enum ExecMsg {
    Event(Event),
    /// The helper (or pkexec) exited; stderr text when it said something.
    Exit(Option<i32>, String),
}

pub fn find_helper() -> Option<PathBuf> {
    if let Some(p) = std::env::var_os("GENTOO_SETTINGS_HELPER").map(PathBuf::from).filter(|p| p.is_absolute() && p.is_file()) {
        return Some(p);
    }
    let mut cands = Vec::new();
    if let Ok(exe) = std::env::current_exe()
        && let Some(dir) = exe.parent()
    {
        cands.push(dir.join("gentoo-settings-helper"));
    }
    cands.push(PathBuf::from("/usr/libexec/gentoo-settings-helper"));
    cands.push(PathBuf::from("/usr/local/libexec/gentoo-settings-helper"));
    cands.into_iter().find(|p| p.is_file())
}

/// Installed system-wide (where the polkit policy points)?
pub fn helper_installed() -> bool {
    PathBuf::from("/usr/libexec/gentoo-settings-helper").is_file()
}

pub fn scratch_root() -> Option<PathBuf> {
    std::env::var_os("GENTOO_SETTINGS_ROOT").map(PathBuf::from).filter(|p| p.is_absolute() && p.is_dir())
}

pub fn start(req: &Request, tx: Sender<ExecMsg>, ctx: egui::Context) -> Result<(), String> {
    let helper = find_helper().ok_or("gentoo-settings-helper was not found — install with dist/install.sh")?;
    let is_root = unsafe { libc::geteuid() } == 0;
    let mut cmd = if is_root || scratch_root().is_some() {
        Command::new(&helper)
    } else {
        let mut c = Command::new("pkexec");
        c.arg(&helper);
        c
    };
    let mut child = cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(|e| format!("cannot start the helper: {e}"))?;
    let body = serde_json::to_string(req).map_err(|e| e.to_string())?;
    let mut stdin = child.stdin.take().ok_or("no stdin")?;
    std::thread::spawn(move || {
        let _ = stdin.write_all(body.as_bytes());
        // Dropping stdin closes it: the helper reads to end of input.
    });
    let stdout = child.stdout.take().ok_or("no stdout")?;
    let mut stderr = child.stderr.take().ok_or("no stderr")?;
    let err_reader = std::thread::spawn(move || {
        let mut s = String::new();
        let _ = stderr.read_to_string(&mut s);
        s
    });
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            match serde_json::from_str::<Event>(&line) {
                Ok(ev) => {
                    let _ = tx.send(ExecMsg::Event(ev));
                }
                Err(_) if !line.trim().is_empty() => {
                    let _ = tx.send(ExecMsg::Event(Event::Step { text: line }));
                }
                Err(_) => {}
            }
            ctx.request_repaint();
        }
        let code = child.wait().ok().and_then(|s| s.code());
        let err = err_reader.join().unwrap_or_default();
        let _ = tx.send(ExecMsg::Exit(code, err.trim().to_string()));
        ctx.request_repaint();
    });
    Ok(())
}

/// What a pkexec exit code means.
pub fn explain_exit(code: Option<i32>, stderr: &str) -> String {
    match code {
        Some(126) => "Authorization was cancelled or denied.".into(),
        Some(127) if stderr.contains("Not authorized") => "Not authorized.".into(),
        Some(127) => format!("pkexec could not run the helper: {stderr}"),
        Some(c) if !stderr.is_empty() => format!("The helper exited with {c}: {stderr}"),
        Some(c) => format!("The helper exited with {c}."),
        None => "The helper was killed.".into(),
    }
}
