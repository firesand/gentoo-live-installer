//! Scripted UI steps for screenshots and smoke tests:
//! `GENTOO_SETTINGS_SCRIPT="tab=tweaks;cat=memory;expand=zram;wait=1;shot=/tmp/a.png;exit"`

use crate::app::{App, Tab};
use gentoo_settings::catalog::Category;
use std::io::Write;
use std::path::PathBuf;
use std::time::Instant;

#[derive(Debug, Clone)]
pub enum Step {
    Tab(Tab),
    Category(Option<Category>),
    Expand(String),
    Toggle(String),
    Defaults,
    Review,
    ReviewFile(usize),
    Apply,
    Search(String),
    Scroll(f32),
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
        let s = std::env::var("GENTOO_SETTINGS_SCRIPT").ok()?;
        let mut steps = std::collections::VecDeque::new();
        for part in s.split(';').map(str::trim).filter(|p| !p.is_empty()) {
            let (k, v) = part.split_once('=').unwrap_or((part, ""));
            steps.push_back(match k {
                "tab" => Step::Tab(match v {
                    "system" => Tab::System,
                    "activity" => Tab::Activity,
                    _ => Tab::Tweaks,
                }),
                "cat" => Step::Category(Category::ALL.into_iter().find(|c| format!("{c:?}").eq_ignore_ascii_case(v))),
                "expand" => Step::Expand(v.into()),
                "toggle" => Step::Toggle(v.into()),
                "defaults" => Step::Defaults,
                "review" => Step::Review,
                "apply" => Step::Apply,
                "reviewfile" => Step::ReviewFile(v.parse().unwrap_or(0)),
                "search" => Step::Search(v.into()),
                "scroll" => Step::Scroll(v.parse().unwrap_or(0.0)),
                "wait" => Step::Wait(v.parse().unwrap_or(1.0)),
                "shot" => Step::Shot(PathBuf::from(v)),
                "exit" => Step::Exit,
                _ => continue,
            });
        }
        // Let fonts and the first layout settle.
        steps.push_front(Step::Wait(0.6));
        Some(Script { steps, ..Default::default() })
    }
}

pub fn run(app: &mut App, ctx: &egui::Context, script: &mut Script) {
    if let Some(path) = script.waiting_shot.clone() {
        let img = ctx.input(|i| {
            i.raw.events.iter().find_map(|e| match e {
                egui::Event::Screenshot { image, .. } => Some(image.clone()),
                _ => None,
            })
        });
        if let Some(img) = img {
            match write_png(&path, &img) {
                Ok(()) => eprintln!("screenshot saved: {}", path.display()),
                Err(e) => eprintln!("screenshot {}: {e}", path.display()),
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
            Step::Category(c) => {
                app.tab = Tab::Tweaks;
                app.category = c;
            }
            Step::Expand(id) => {
                if let Some(t) = gentoo_settings::catalog::get(&id) {
                    app.expanded.insert(t.id);
                }
            }
            Step::Toggle(id) => {
                if let Some(s) = app.status(&id).cloned() {
                    let on = !app.wanted_on(&s);
                    app.set_on(s.tweak.id, on);
                }
            }
            Step::Defaults => {
                app.stage_defaults(None);
            }
            Step::Review => app.review_pending(),
            Step::Apply => app.apply_review(),
            Step::ReviewFile(i) => {
                if let Some(r) = &mut app.review {
                    r.expanded = Some(i);
                }
            }
            Step::Search(q) => app.query = q,
            Step::Scroll(y) => {
                ctx.data_mut(|d| d.insert_temp(egui::Id::new("script-scroll"), y));
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
        ctx.request_repaint();
    }
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
