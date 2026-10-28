//! Gentoo Settings — CachyOS-Settings style tuning for Gentoo, with a
//! Steam-styled GUI (the PortageHub look). Works with OpenRC and systemd.

mod app;
mod exec;
mod fmt;
mod shot;
mod ui;
mod views;

use gentoo_settings::catalog::CATALOG;
use gentoo_settings::plan::{self, Change};
use gentoo_settings::status;

pub fn open_url(url: &str) {
    let _ = std::process::Command::new("xdg-open").arg(url).stdin(std::process::Stdio::null()).spawn();
}

/// `--status`: every tweak and where it stands on this machine.
fn status_cli() {
    let (sys, _) = app::load_system();
    println!("init: {} ({})", sys.init.label(), sys.init_reason);
    println!("kernel: {}", sys.kernel);
    let scan = status::scan(&sys);
    for s in &scan.statuses {
        let why = match &s.available {
            Err(na) if s.installed.is_none() => format!("  [{}]", na.reason),
            _ => String::new(),
        };
        let live = if s.live.now.is_empty() { String::new() } else { format!("  now: {}", s.live.now) };
        println!("{:<34} {:<16}{live}{why}", s.tweak.id, s.badge.label());
        for c in &s.conflicts {
            println!("    ! {c}");
        }
    }
    if !scan.unreadable.is_empty() {
        println!("root-only (not inspected): {}", scan.unreadable.join(", "));
    }
}

/// `--plan id[=value] -id …`: show what applying would change. Writes nothing.
fn plan_cli(args: &[String]) {
    let (sys, _) = app::load_system();
    let changes: Vec<Change> = args
        .iter()
        .map(|a| match a.strip_prefix('-') {
            Some(id) => Change { id: id.into(), enable: false, value: None },
            None => {
                let (id, v) = a.split_once('=').map(|(i, v)| (i, Some(v.to_string()))).unwrap_or((a, None));
                Change { id: id.into(), enable: true, value: v }
            }
        })
        .collect();
    match plan::plan(&sys, &changes) {
        Err(e) => eprintln!("error: {e}"),
        Ok(p) => {
            for f in &p.files {
                println!(
                    "── {} ({})",
                    f.path,
                    if f.new.is_none() {
                        "delete"
                    } else if f.old.is_none() {
                        "create"
                    } else {
                        "modify"
                    }
                );
                for (k, l) in fmt::line_diff(f.old.as_deref().unwrap_or(""), f.new.as_deref().unwrap_or("")) {
                    println!("{k} {l}");
                }
            }
            for c in p.pre.iter().chain(&p.post) {
                println!("$ {}", c.describe());
            }
        }
    }
}

fn main() -> eframe::Result {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().is_some_and(|a| a.starts_with("--")) {
        // `gentoo-settings --status | head` should just stop, not panic.
        unsafe { libc::signal(libc::SIGPIPE, libc::SIG_DFL) };
    }
    match args.first().map(String::as_str) {
        Some("--version") => {
            println!("gentoo-settings {}", gentoo_settings::protocol::VERSION);
            return Ok(());
        }
        Some("--status") => {
            status_cli();
            return Ok(());
        }
        Some("--list") => {
            for t in CATALOG {
                println!("{:<34} {:<18} {}", t.id, t.category.label(), t.title);
            }
            return Ok(());
        }
        Some("--plan") => {
            plan_cli(&args[1..]);
            return Ok(());
        }
        Some("--help" | "-h") => {
            println!(
                "gentoo-settings [--status | --list | --plan ID[=VALUE] -ID … | --version]\n\n\
                 Without options, opens the GUI. Environment:\n  \
                 GENTOO_SETTINGS_ROOT=/path   work on a scratch tree (no programs run)\n  \
                 GENTOO_SETTINGS_INIT=systemd preview the other init system's implementation"
            );
            return Ok(());
        }
        _ => {}
    }
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("Gentoo Settings")
            .with_app_id("gentoo-settings")
            .with_inner_size([1320.0, 860.0])
            .with_min_inner_size([960.0, 600.0]),
        ..Default::default()
    };
    eframe::run_native("Gentoo Settings", options, Box::new(|cc| Ok(Box::new(app::App::new(cc)))))
}
