//! PortageHub — a Steam-styled graphical front end for Gentoo's Portage.

mod app;
mod batch;
mod danger;
mod data;
mod edits;
mod exec;
mod fmt;
mod portage;
mod settings;
mod shot;
mod stop;
mod ui;
mod util;
mod views;
mod window;

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

/// Is a program available in the standard system directories?
pub fn exec_available(name: &str) -> bool {
    static CACHE: OnceLock<Mutex<HashMap<String, bool>>> = OnceLock::new();
    let cache = CACHE.get_or_init(Default::default);
    if let Some(v) = cache.lock().unwrap().get(name) {
        return *v;
    }
    let found = portagehub::protocol::PROGRAM_DIRS.iter().any(|d| std::path::Path::new(d).join(name).is_file());
    cache.lock().unwrap().insert(name.to_string(), found);
    found
}

fn self_test() {
    let t = std::time::Instant::now();
    let repos = portage::repos::load();
    println!("repos: {} ({:?})", repos.repos.len(), t.elapsed());
    for r in &repos.repos {
        println!("  {:<20} {:<40} sync={} prio={} exists={}", r.name, r.location.display(), r.sync_type(), r.priority(), r.exists);
    }
    let t = std::time::Instant::now();
    let cat = portage::catalog::scan(&repos);
    println!("catalog: {} packages, {} ebuilds ({:?})", cat.pkgs.len(), cat.ebuild_count, t.elapsed());
    let t = std::time::Instant::now();
    let vdb = portage::vdb::load();
    println!("vdb: {} installed, world {} ({:?})", vdb.pkgs.len(), vdb.world_raw.len(), t.elapsed());
    let t = std::time::Instant::now();
    let h = portage::history::load();
    match &h.problem {
        Some(p) => println!("history: unavailable — {} ({:?})", p.message().0, t.elapsed()),
        None => println!("history: {} merges, {} syncs, median {:?} ({:?})", h.merges.len(), h.syncs.len(), h.median_build, t.elapsed()),
    }
    let cfg = portage::config::load();
    for s in &cfg.sets {
        println!("  {:<26} exists={} dir={} files={} entries={}", s.kind.file_name(), s.exists, s.is_dir, s.files.len(), s.entry_count());
    }
    for s in &cfg.strays {
        println!("  stray: {} {:?}", s.path.display(), s.reason);
    }
    let mc = portage::makeconf::load();
    println!("make.conf: {} assignments, MAKEOPTS={}", mc.assignments.len(), mc.value("MAKEOPTS"));
    let env = portage::system::load_env();
    println!("env: ARCH={} portage={} testing={}", env.arch(), env.portage_version, env.accepts_testing());
    let masks = portage::profile::load(&repos);
    println!("masks: {} entries, profile chain:", masks.len());
    for p in &masks.chain {
        println!("  {}", p.display());
    }
    let data = data::Data {
        masks: Some(masks.into()),
        repos: Some(repos.into()),
        catalog: Some(cat.into()),
        vdb: Some(vdb.into()),
        history: Some(h.into()),
        config: Some(cfg.into()),
        env: Some(env.into()),
        ..Default::default()
    };
    let t = std::time::Instant::now();
    if let Some(d) = data::build_detail("app-editors/vim", &data) {
        println!("detail vim: {} versions, slots {:?}, flags {} ({:?})", d.versions.len(), d.slots(), d.xml.flags.len(), t.elapsed());
    }
    let t = std::time::Instant::now();
    let ks = portage::kernel::load(data.vdb.as_ref().unwrap(), "kernels-keep");
    println!("kernels ({:?}): running {} · last-good {:?} · previous-good {:?} · bootloader {:?} (detected {:?}, current {:?}) · initramfs {:?}", t.elapsed(), ks.running, ks.last_good, ks.previous_good, ks.boot.primary, ks.boot.detected, ks.boot.current, ks.boot.initramfs_tool);
    for k in &ks.kernels {
        println!(
            "  {:<22} pkg={:<38} image={} initramfs={} modules={} sources={} old={} nvidia={:?}",
            k.kver,
            k.pkg.clone().unwrap_or("-".into()),
            k.image.is_some(),
            k.initramfs.is_some(),
            k.modules,
            k.sources,
            k.old_files.len(),
            ks.nvidia_status(k)
        );
    }
    println!("  orphan kernel packages: {:?} · keep set {:?} in world: {} · nvidia {:?}", ks.orphan_packages, ks.keep_set_atoms, ks.keep_set_in_world, ks.nvidia);
    for f in portage::kernel::check(&ks, None, true, 2, true) {
        println!("  [{:?}] {} — {}", f.severity, f.title, f.detail);
    }
    let t = std::time::Instant::now();
    let u = data::compute_updates(&data);
    println!("updates: {} ({:?})", u.len(), t.elapsed());
    for c in u.iter() {
        let held: Vec<String> = c.held_by.iter().map(|(p, a)| format!("{p} needs {a}")).collect();
        println!("  {} {} -> {} ::{}{}", c.key, c.installed, c.available, c.repo, if held.is_empty() { String::new() } else { format!("   [held back: {}]", held.join("; ")) });
    }
}

/// `portagehub --boot-check <scan.json>`: run the boot check against a saved
/// boot scan (the JSON the helper returns) and print the findings.
fn boot_check_cli(path: &str) {
    let text = std::fs::read_to_string(path).unwrap_or_default();
    // Accept either the raw scan or the helper's event stream.
    let json = text
        .lines()
        .find_map(|l| {
            let v: serde_json::Value = serde_json::from_str(l).ok()?;
            if v.get("ev").and_then(|e| e.as_str()) == Some("data") { v.get("text").and_then(|t| t.as_str()).map(String::from) } else if v.get("files").is_some() { Some(l.to_string()) } else { None }
        })
        .unwrap_or(text);
    let scan: portagehub::protocol::BootScan = match serde_json::from_str(&json) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("cannot parse {path}: {e}");
            return;
        }
    };
    let vdb = portage::vdb::load();
    let (settings, _) = settings::Settings::load();
    let ks = portage::kernel::load(&vdb, &settings.kernel_keep_set);
    for f in portage::kernel::check(&ks, Some(&scan), settings.kernel_protect, 2, settings.nvidia_alerts) {
        println!("[{:?}] {}\n    {}{}", f.severity, f.title, f.detail, if f.fixes.is_empty() { String::new() } else { format!("\n    fixes: {:?}", f.fixes) });
    }
}

fn main() -> eframe::Result {
    if std::env::args().any(|a| a == "--self-test") {
        self_test();
        return Ok(());
    }
    let args: Vec<String> = std::env::args().collect();
    if let Some(i) = args.iter().position(|a| a == "--boot-check") {
        boot_check_cli(args.get(i + 1).map(|s| s.as_str()).unwrap_or(""));
        return Ok(());
    }
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("PortageHub")
            .with_app_id("portagehub")
            .with_inner_size([window::WANTED.0, window::WANTED.1])
            .with_min_inner_size([window::MIN.0, window::MIN.1]),
        ..Default::default()
    };
    if let Err(e) = eframe::run_native("PortageHub", options, Box::new(|cc| Ok(Box::new(app::App::new(cc))))) {
        let has_display = std::env::var_os("DISPLAY").is_some() || std::env::var_os("WAYLAND_DISPLAY").is_some();
        eprintln!("{}", window_error_text(&e.to_string(), has_display));
        std::process::exit(1);
    }
    Ok(())
}

/// What to print when no window can be opened: the reason, and what usually helps.
fn window_error_text(reason: &str, has_display: bool) -> String {
    let mut text = format!("PortageHub could not open a window: {reason}");
    if !has_display {
        text.push_str("\nNo display was found (DISPLAY and WAYLAND_DISPLAY are both unset): start it from a desktop session.");
    }
    text.push_str("\nOn machines without a working Vulkan driver (older hardware, virtual machines) try: WGPU_BACKEND=gl portagehub");
    text
}

#[cfg(test)]
mod tests {
    use super::window_error_text;

    #[test]
    fn a_failed_window_is_explained() {
        let t = window_error_text("FailedToCreateSurfaceForAnyBackend", true);
        assert!(t.starts_with("PortageHub could not open a window: FailedToCreateSurfaceForAnyBackend"));
        assert!(t.contains("WGPU_BACKEND=gl portagehub") && !t.contains("No display"));
        let t = window_error_text("x", false);
        assert!(t.contains("No display was found") && t.contains("WGPU_BACKEND=gl"));
    }
}
