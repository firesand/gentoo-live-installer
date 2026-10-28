//! Privileged helper for Gentoo Settings.
//!
//! Started through `pkexec` for each transaction. It reads one JSON request
//! from stdin — tweak ids, enable/disable and values, nothing else — computes
//! the plan from its own built-in catalog, carries it out and streams JSON
//! events to stdout. It never takes paths, file contents or commands from the
//! caller, and only writes the files listed in `managed::FILES`.
//!
//! Without root it only works on a scratch tree given by
//! `GENTOO_SETTINGS_ROOT` and runs no external programs (for tests).

use gentoo_settings::apply::Runner;
use gentoo_settings::detect::{self, System};
use gentoo_settings::protocol::{Event, Request, VERSION};
use std::io::{Read, Write};
use std::path::PathBuf;

fn emit(ev: &Event) {
    let mut out = std::io::stdout().lock();
    if let Ok(s) = serde_json::to_string(ev) {
        let _ = writeln!(out, "{s}");
        let _ = out.flush();
    }
}

/// The user who asked for root (pkexec sets PKEXEC_UID).
fn requesting_uid() -> u32 {
    if unsafe { libc::geteuid() } != 0 {
        return unsafe { libc::getuid() };
    }
    ["PKEXEC_UID", "SUDO_UID"].iter().find_map(|k| std::env::var(k).ok()?.parse().ok()).unwrap_or(0)
}

fn main() {
    let is_root = unsafe { libc::geteuid() } == 0;
    let root = if is_root {
        PathBuf::from("/")
    } else {
        match std::env::var_os("GENTOO_SETTINGS_ROOT").map(PathBuf::from).filter(|p| p.is_absolute() && p.is_dir()) {
            Some(r) => r,
            None => {
                emit(&Event::Error { msg: "gentoo-settings-helper must run as root (through pkexec)".into() });
                std::process::exit(2);
            }
        }
    };
    let dry = root != std::path::Path::new("/");

    let mut input = String::new();
    if std::io::stdin().take(1 << 20).read_to_string(&mut input).is_err() {
        emit(&Event::Error { msg: "cannot read the request".into() });
        std::process::exit(2);
    }
    let req: Request = match serde_json::from_str(&input) {
        Ok(r) => r,
        Err(e) => {
            emit(&Event::Error { msg: format!("invalid request: {e}") });
            std::process::exit(2);
        }
    };

    let mut sys = System::detect_at(&root);
    // Previewing the other init system is for scratch trees only.
    if !is_root && let Some(i) = detect::init_override() {
        sys.init = i;
    }
    let uid = requesting_uid();
    emit(&Event::Ready { version: VERSION.into(), uid, init: sys.init, dry_run: dry });
    if req.version != VERSION {
        emit(&Event::Error { msg: format!("the installed helper is version {VERSION} but the GUI is {} — reinstall with dist/install.sh", req.version) });
        std::process::exit(2);
    }

    // Files we create are world-readable regardless of the caller's umask.
    unsafe { libc::umask(0o022) };
    let mut sink = |ev: Event| emit(&ev);
    let ok = Runner::new(&sys, dry, uid, &mut sink).run(&req);
    std::process::exit(if ok { 0 } else { 1 });
}
