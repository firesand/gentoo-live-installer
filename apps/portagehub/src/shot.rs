//! Scripted UI automation for screenshots and smoke tests.
//!
//! `PORTAGEHUB_SCRIPT="tab=library;open=app-editors/vim;wait=3;shot=/tmp/a.png;exit"`

use crate::app::{Action, App, Tab};
use std::io::Write;
use std::path::PathBuf;
use std::time::Instant;

#[derive(Debug, Clone)]
pub enum Step {
    Tab(Tab),
    Open(String),
    DetailTab(String),
    EmergePage(String),
    ConfigPage(String),
    Search(String),
    StorePage(String),
    Category(String),
    Pretend(String),
    Root(String),
    WriteTest,
    Apply,
    CheckWorld,
    MPage(String),
    BootScan,
    CleanScan(String),
    Wait(f32),
    Shot(PathBuf),
    Exit,
}

#[derive(Default)]
pub struct Script {
    steps: std::collections::VecDeque<Step>,
    wait_until: Option<Instant>,
    waiting_shot: Option<PathBuf>,
}

impl Script {
    pub fn from_env() -> Option<Script> {
        let s = std::env::var("PORTAGEHUB_SCRIPT").ok()?;
        let mut steps = std::collections::VecDeque::new();
        for part in s.split(';').map(str::trim).filter(|p| !p.is_empty()) {
            let (k, v) = part.split_once('=').unwrap_or((part, ""));
            let step = match k {
                "tab" => Step::Tab(match v {
                    "library" => Tab::Library,
                    "emerge" => Tab::Emerge,
                    "overlays" => Tab::Overlays,
                    "config" => Tab::Config,
                    "history" => Tab::History,
                    "maintenance" => Tab::Maintenance,
                    "settings" => Tab::Settings,
                    _ => Tab::Store,
                }),
                "open" => Step::Open(v.to_string()),
                "dtab" => Step::DetailTab(v.to_string()),
                "epage" => Step::EmergePage(v.to_string()),
                "cpage" => Step::ConfigPage(v.to_string()),
                "search" => Step::Search(v.to_string()),
                "spage" => Step::StorePage(v.to_string()),
                "category" => Step::Category(v.to_string()),
                "pretend" => Step::Pretend(v.to_string()),
                "root" => Step::Root(v.to_string()),
                "writetest" => Step::WriteTest,
                "apply" => Step::Apply,
                "checkworld" => Step::CheckWorld,
                "mpage" => Step::MPage(v.to_string()),
                "bootscan" => Step::BootScan,
                "cleanscan" => Step::CleanScan(v.to_string()),
                "wait" => Step::Wait(v.parse().unwrap_or(1.0)),
                "shot" => Step::Shot(PathBuf::from(v)),
                "exit" => Step::Exit,
                _ => continue,
            };
            steps.push_back(step);
        }
        Some(Script { steps, ..Default::default() })
    }
}

pub fn run(app: &mut App, ctx: &egui::Context, script: &mut Script) {
    // Collect screenshot results.
    if let Some(path) = script.waiting_shot.clone() {
        let img = ctx.input(|i| {
            i.raw.events.iter().find_map(|e| match e {
                egui::Event::Screenshot { image, .. } => Some(image.clone()),
                _ => None,
            })
        });
        if let Some(img) = img {
            if let Err(e) = write_png(&path, &img) {
                eprintln!("screenshot {}: {e}", path.display());
            } else {
                eprintln!("screenshot saved: {}", path.display());
            }
            script.waiting_shot = None;
        } else {
            ctx.request_repaint();
            return;
        }
    }
    if let Some(t) = script.wait_until {
        if Instant::now() < t {
            ctx.request_repaint_after(std::time::Duration::from_millis(100));
            return;
        }
        script.wait_until = None;
    }
    while let Some(step) = script.steps.pop_front() {
        match step {
            Step::Tab(t) => app.tab = t,
            Step::Open(k) => app.push(Action::Open(k)),
            Step::DetailTab(t) => {
                use crate::views::details::DTab;
                app.detail.tab = match t.as_str() {
                    "versions" => DTab::Versions,
                    "use" => DTab::Use,
                    "deps" => DTab::Deps,
                    "ebuild" => DTab::Ebuild,
                    "files" => DTab::Files,
                    "history" => DTab::History,
                    "config" => DTab::Config,
                    _ => DTab::Overview,
                };
            }
            Step::EmergePage(p) => {
                use crate::views::emerge::Page;
                app.emerge.page = match p.as_str() {
                    "custom" => Page::Custom,
                    "review" => Page::Review,
                    "activity" => Page::Activity,
                    _ => Page::Recipes,
                };
            }
            Step::ConfigPage(p) => {
                use crate::portage::config::ConfKind;
                use crate::views::config::Page;
                app.config_view.page = match p.as_str() {
                    "makeconf" => Page::MakeConf,
                    "env" => Page::EnvFiles,
                    "profile" => Page::Profile,
                    "news" => Page::News,
                    "updates" => Page::Updates,
                    "strays" => Page::Strays,
                    "overview" => Page::Overview,
                    other => ConfKind::ALL.iter().find(|k| k.file_name() == other).map(|k| Page::Kind(*k)).unwrap_or(Page::Overview),
                };
            }
            Step::Search(q) => app.push(Action::Search(q)),
            Step::StorePage(p) => {
                use crate::views::store::Page;
                app.tab = Tab::Store;
                app.store.show_detail = false;
                app.store.page = match p.as_str() {
                    "categories" => Page::Categories,
                    "search" => Page::Search,
                    _ => Page::Home,
                };
            }
            Step::Category(c) => {
                app.tab = Tab::Store;
                app.store.show_detail = false;
                app.store.page = crate::views::store::Page::Categories;
                app.store.category = if c.is_empty() { None } else { Some(c) };
            }
            Step::Pretend(t) => {
                let opts = match t.parse::<usize>() {
                    Ok(i) => (crate::portage::emerge::recipes()[i.min(7)].build)(),
                    Err(_) => crate::portage::emerge::EmergeOptions::default().targets(&[t.as_str()]),
                };
                app.push(Action::Pretend(opts));
            }
            Step::Root(cmd) => {
                app.push(Action::Root {
                    title: cmd.clone(),
                    argv: cmd.split_whitespace().map(String::from).collect(),
                    reload: vec![],
                    kind: crate::app::JobKind::Generic,
                });
                app.emerge.page = crate::views::emerge::Page::Activity;
            }
            Step::WriteTest => {
                let path = PathBuf::from(format!("/etc/portage/package.use/{}", app.settings.target_file));
                let old = std::fs::read_to_string(&path).unwrap_or_default();
                let new = crate::portage::config::append_line(&old, "# portagehub write test");
                app.push(Action::Write {
                    title: "Write test".into(),
                    changes: vec![crate::app::FileChange { path, old, new: Some(new) }],
                    reload: vec![crate::data::Reload::Config],
                    then: None,
                });
            }
            Step::CheckWorld => app.push(Action::CheckWorld),
            Step::MPage(p) => {
                app.tab = Tab::Maintenance;
                app.maintenance.page = if p == "cleanup" { crate::views::maintenance::Page::Cleanup } else { crate::views::maintenance::Page::Kernels };
            }
            Step::CleanScan(tag) => {
                let argv: Vec<String> = match tag.as_str() {
                    "distfiles" => vec!["eclean-dist", "--deep", "--pretend"],
                    "binpkgs" => vec!["eclean-pkg", "--deep", "--pretend"],
                    _ => vec!["emerge", "--pretend", "--depclean", "--color=n", "--nospinner", "--ask=n"],
                }
                .into_iter()
                .map(String::from)
                .collect();
                app.push(Action::Job { title: format!("scan {tag}"), argv, kind: crate::app::JobKind::CleanupScan { tag } });
            }
            Step::BootScan => app.push(Action::RootOp {
                title: "Read boot configuration".into(),
                op: portagehub::protocol::Op::BootScan,
                kind: crate::app::JobKind::BootScan,
                reload: vec![],
            }),
            Step::Apply => {
                if let Some(r) = app.review.take() {
                    app.apply_changes(r.title, r.changes, r.reload, r.then);
                }
            }
            Step::Wait(s) => {
                script.wait_until = Some(Instant::now() + std::time::Duration::from_secs_f32(s));
                ctx.request_repaint();
                return;
            }
            Step::Shot(p) => {
                ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(egui::UserData::default()));
                script.waiting_shot = Some(p);
                ctx.request_repaint();
                return;
            }
            Step::Exit => {
                ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                return;
            }
        }
    }
}

fn crc32(data: &[u8], mut crc: u32) -> u32 {
    crc = !crc;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0xEDB8_8320 } else { crc >> 1 };
        }
    }
    !crc
}

fn adler32(data: &[u8]) -> u32 {
    let (mut a, mut b) = (1u32, 0u32);
    for &x in data {
        a = (a + x as u32) % 65521;
        b = (b + a) % 65521;
    }
    (b << 16) | a
}

/// Minimal PNG writer (stored deflate blocks, no compression).
pub fn write_png(path: &std::path::Path, img: &egui::ColorImage) -> std::io::Result<()> {
    let [w, h] = img.size;
    let mut raw = Vec::with_capacity((w * 3 + 1) * h);
    for y in 0..h {
        raw.push(0u8);
        for x in 0..w {
            let c = img.pixels[y * w + x];
            raw.extend_from_slice(&[c.r(), c.g(), c.b()]);
        }
    }
    let mut z = vec![0x78, 0x01];
    for (i, chunk) in raw.chunks(65535).enumerate() {
        let last = (i + 1) * 65535 >= raw.len();
        z.push(if last { 1 } else { 0 });
        let len = chunk.len() as u16;
        z.extend_from_slice(&len.to_le_bytes());
        z.extend_from_slice(&(!len).to_le_bytes());
        z.extend_from_slice(chunk);
    }
    z.extend_from_slice(&adler32(&raw).to_be_bytes());
    let mut out = Vec::new();
    out.extend_from_slice(b"\x89PNG\r\n\x1a\n");
    let mut chunk = |ty: &[u8], data: &[u8]| {
        out.extend_from_slice(&(data.len() as u32).to_be_bytes());
        out.extend_from_slice(ty);
        out.extend_from_slice(data);
        let crc = crc32(data, crc32(ty, 0));
        out.extend_from_slice(&crc.to_be_bytes());
    };
    let mut ihdr = Vec::new();
    ihdr.extend_from_slice(&(w as u32).to_be_bytes());
    ihdr.extend_from_slice(&(h as u32).to_be_bytes());
    ihdr.extend_from_slice(&[8, 2, 0, 0, 0]);
    chunk(b"IHDR", &ihdr);
    chunk(b"IDAT", &z);
    chunk(b"IEND", &[]);
    let mut f = std::fs::File::create(path)?;
    f.write_all(&out)
}
