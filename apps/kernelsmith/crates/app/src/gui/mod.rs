//! The graphical app: a Steam-style shell with Home, Build, Patches, Library
//! and Hardware pages plus an Activity view for running jobs.

mod hardware_page;
mod home;
mod jobview;
mod library_page;
mod modals;
mod patches_page;
pub mod theme;
mod wizard;

use crate::runner::{pick_elevation, Runner};
use eframe::egui::{self, Align, Color32, CornerRadius, Layout, Margin, RichText, Sense, Stroke, Vec2};
use jobview::{JobKind, JobView};
use kernelsmith_core::hw::{self, SystemInfo};
use kernelsmith_core::job::PreparedJob;
use kernelsmith_core::kconfig::{scan_symbols, SymbolIndex};
use kernelsmith_core::library::{self, InstalledKernel};
use kernelsmith_core::patches::{self, LocalPatch, PatchCategory, PatchStatus, PlannedPatch};
use kernelsmith_core::paths;
use kernelsmith_core::sources::{self, KernelSource};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{channel, Receiver, TryRecvError};
use std::sync::Arc;
use std::time::{Duration, Instant};
use theme::*;

pub fn run() -> anyhow::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("Kernelsmith")
            .with_app_id("kernelsmith")
            .with_inner_size([1320.0, 860.0])
            .with_min_inner_size([1000.0, 660.0])
            .with_icon(Arc::new(make_icon())),
        ..Default::default()
    };
    eframe::run_native("Kernelsmith", options, Box::new(|cc| Ok(Box::new(App::new(cc)))))
        .map_err(|e| anyhow::anyhow!("could not open a window: {e}"))
}

/// A value computed on a background thread.
pub enum Load<T> {
    Idle,
    Loading(Receiver<T>),
    Ready(T),
}

impl<T: Send + 'static> Load<T> {
    pub fn spawn(ctx: &egui::Context, f: impl FnOnce() -> T + Send + 'static) -> Self {
        let (tx, rx) = channel();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let _ = tx.send(f());
            ctx.request_repaint();
        });
        Load::Loading(rx)
    }

    /// Returns true on the frame the value arrives.
    pub fn poll(&mut self) -> bool {
        if let Load::Loading(rx) = self {
            match rx.try_recv() {
                Ok(v) => {
                    *self = Load::Ready(v);
                    return true;
                }
                Err(TryRecvError::Disconnected) => *self = Load::Idle,
                Err(TryRecvError::Empty) => {}
            }
        }
        false
    }

    pub fn ready(&self) -> Option<&T> {
        match self {
            Load::Ready(v) => Some(v),
            _ => None,
        }
    }

    pub fn is_loading(&self) -> bool {
        matches!(self, Load::Loading(_))
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Page {
    Home,
    Build,
    Patches,
    Library,
    Hardware,
    Activity,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Settings {
    pub show_advanced: bool,
    pub offline: bool,
}

impl Settings {
    fn load() -> Self {
        std::fs::read_to_string(paths::settings_file()).ok().and_then(|t| toml::from_str(&t).ok()).unwrap_or_default()
    }

    fn save(&self) {
        let _ = std::fs::create_dir_all(paths::config_dir());
        if let Ok(t) = toml::to_string_pretty(self) {
            let _ = std::fs::write(paths::settings_file(), t);
        }
    }
}

pub struct Toast {
    text: String,
    color: Color32,
    until: Instant,
}

pub struct App {
    page: Page,
    sys: Load<Arc<SystemInfo>>,
    sources: Load<Vec<KernelSource>>,
    symbols: HashMap<PathBuf, Load<Arc<SymbolIndex>>>,
    patch_status: HashMap<PathBuf, Load<Vec<(String, PatchStatus)>>>,
    patch_downloads: HashMap<String, Load<Result<PlannedPatch, String>>>,
    dry_runs: HashMap<String, Load<(bool, String)>>,
    library: Load<Vec<InstalledKernel>>,
    local_patches: Vec<LocalPatch>,
    wiz: wizard::Wizard,
    job: Option<JobView>,
    modal: Option<modals::Modal>,
    toasts: Vec<Toast>,
    settings: Settings,
    import_url: String,
    import: Load<Result<PathBuf, String>>,
    patch_search: String,
    patch_filter: Option<PatchCategory>,
    lib_selected: Option<String>,
    presets: Vec<PathBuf>,
    allow_close: bool,
}

impl App {
    fn new(cc: &eframe::CreationContext) -> Self {
        install_fonts(&cc.egui_ctx);
        install_style(&cc.egui_ctx);
        let ctx = &cc.egui_ctx;
        let mut app = App {
            page: Page::Home,
            sys: Load::spawn(ctx, || Arc::new(hw::detect())),
            sources: Load::spawn(ctx, sources::discover),
            symbols: HashMap::new(),
            patch_status: HashMap::new(),
            patch_downloads: HashMap::new(),
            dry_runs: HashMap::new(),
            library: Load::Idle,
            local_patches: patches::local_patches(),
            wiz: wizard::Wizard::default(),
            job: None,
            modal: None,
            toasts: vec![],
            settings: Settings::load(),
            import_url: String::new(),
            import: Load::Idle,
            patch_search: String::new(),
            patch_filter: None,
            lib_selected: None,
            presets: list_presets(),
            allow_close: false,
        };
        let _ = std::fs::create_dir_all(paths::user_patch_dir());
        app.wiz.show_advanced = app.settings.show_advanced;
        // Deep links, e.g. KERNELSMITH_PAGE=build KERNELSMITH_STEP=4 (handy for docs and testing).
        app.page = match std::env::var("KERNELSMITH_PAGE").as_deref() {
            Ok("build") => Page::Build,
            Ok("patches") => Page::Patches,
            Ok("library") => Page::Library,
            Ok("hardware") => Page::Hardware,
            _ => Page::Home,
        };
        if let Some(step) = std::env::var("KERNELSMITH_STEP").ok().and_then(|s| s.parse::<usize>().ok()) {
            app.wiz.step = step.min(wizard::STEPS.len() - 1);
        }
        // Watch a job prepared with `kernelsmith plan` in the Activity view.
        if let Some(script) = std::env::var_os("KERNELSMITH_RUN_JOB").map(PathBuf::from) {
            let dir = script.parent().map(Path::to_path_buf).unwrap_or_default();
            let plan = kernelsmith_core::plan::BuildPlan::load(&dir.join("plan.toml")).ok();
            let job = PreparedJob {
                title: match &plan {
                    Some(p) => format!("Building “{}”", p.name),
                    None => format!("Running {}", dir.file_name().unwrap_or_default().to_string_lossy()),
                },
                log: dir.join("output.log"),
                stages: plan.as_ref().map(|p| kernelsmith_core::script::stages_for(p).into_iter().map(String::from).collect()).unwrap_or_default(),
                release: None,
                est_objects: 7000,
                dir,
                script,
            };
            let needs_root = plan.as_ref().is_none_or(|p| p.needs_root());
            let release = plan.map(|p| p.name).unwrap_or_default();
            app.start_job(job, JobKind::Build { release, autofdo_ready: false }, needs_root);
        }
        app
    }

    fn toast(&mut self, text: impl Into<String>, color: Color32) {
        self.toasts.push(Toast { text: text.into(), color, until: Instant::now() + Duration::from_secs(5) });
    }

    fn fetcher(&self) -> patches::Fetcher {
        patches::Fetcher { offline: self.settings.offline, ..Default::default() }
    }

    /// Symbol index for a source tree, scanning it in the background on first use.
    fn symbols_for(&mut self, ctx: &egui::Context, path: &Path) -> Option<Arc<SymbolIndex>> {
        let entry = self.symbols.entry(path.to_path_buf()).or_insert_with(|| {
            let p = path.to_path_buf();
            Load::spawn(ctx, move || Arc::new(scan_symbols(&p)))
        });
        entry.poll();
        entry.ready().cloned()
    }

    /// Catalog status for every patch against a source tree (network, cached).
    fn patch_statuses(&mut self, ctx: &egui::Context, src: &KernelSource) -> Option<Vec<(String, PatchStatus)>> {
        let symbols = self.symbols_for(ctx, &src.path)?;
        let fetcher = self.fetcher();
        let entry = self.patch_status.entry(src.path.clone()).or_insert_with(|| {
            let src = src.clone();
            Load::spawn(ctx, move || {
                patches::catalog()
                    .iter()
                    .map(|p| (p.id.to_string(), patches::resolve(p, &src, Some(&symbols), &fetcher)))
                    .collect()
            })
        });
        entry.poll();
        entry.ready().cloned()
    }

    fn refresh_library(&mut self, ctx: &egui::Context) {
        if let Some(sys) = self.sys.ready().cloned() {
            self.library = Load::spawn(ctx, move || library::discover(&sys));
        }
    }

    fn refresh_sources(&mut self, ctx: &egui::Context) {
        self.sources = Load::spawn(ctx, sources::discover);
        self.patch_status.clear();
    }

    fn rescan_system(&mut self, ctx: &egui::Context) {
        self.sys = Load::spawn(ctx, || Arc::new(hw::detect()));
    }

    /// Run a prepared root job and switch to the Activity page.
    fn start_job(&mut self, job: PreparedJob, kind: JobKind, needs_root: bool) {
        if self.job.as_ref().is_some_and(|j| j.running()) {
            self.toast("Another job is still running.", WARN);
            return;
        }
        let started = pick_elevation(needs_root).and_then(|el| Runner::start(&job, el));
        match started {
            Ok(runner) => {
                self.job = Some(JobView::new(job, kind, runner));
                self.page = Page::Activity;
            }
            Err(e) => self.modal = Some(modals::Modal::message("Couldn't start", format!("{e:#}"))),
        }
    }

    fn on_job_finished(&mut self, ctx: &egui::Context) {
        self.refresh_library(ctx);
        self.refresh_sources(ctx);
        self.local_patches = patches::local_patches();
    }

    fn top_bar(&mut self, ctx: &egui::Context, sys: Option<&SystemInfo>) {
        egui::TopBottomPanel::top("top")
            .exact_height(60.0)
            .frame(egui::Frame::new().fill(BG_TOP).inner_margin(Margin::symmetric(22, 0)))
            .show(ctx, |ui| {
                ui.horizontal_centered(|ui| {
                    logo(ui);
                    ui.add_space(26.0);
                    let mut tabs = vec![
                        (Page::Home, "HOME"),
                        (Page::Build, "BUILD"),
                        (Page::Patches, "PATCHES"),
                        (Page::Library, "LIBRARY"),
                        (Page::Hardware, "HARDWARE"),
                    ];
                    if self.job.is_some() {
                        tabs.push((Page::Activity, "ACTIVITY"));
                    }
                    for (page, label) in tabs {
                        if nav_item(ui, label, self.page == page).clicked() {
                            self.page = page;
                        }
                        ui.add_space(14.0);
                    }
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        let gear = ui.add(egui::Button::new(RichText::new("⚙").size(18.0)).frame(false)).on_hover_text("Settings");
                        if gear.clicked() {
                            self.modal = Some(modals::Modal::Settings);
                        }
                        if let Some(sys) = sys {
                            ui.add_space(8.0);
                            chip(ui, &format!("▶ {}", sys.running_kernel));
                            chip(ui, sys.boot.bootloader.label());
                            chip(ui, sys.init.label());
                        }
                    });
                });
            });
    }

    /// Steam's bottom "Downloads" strip, shown when a job exists and you're elsewhere.
    fn bottom_bar(&mut self, ctx: &egui::Context) {
        let Some(job) = &self.job else { return };
        if self.page == Page::Activity {
            return;
        }
        let (frac, label, color) = job.summary();
        let mut open = false;
        egui::TopBottomPanel::bottom("bottom")
            .exact_height(44.0)
            .frame(egui::Frame::new().fill(BG_TOP).inner_margin(Margin::symmetric(22, 8)))
            .show(ctx, |ui| {
                let resp = ui.interact(ui.max_rect(), ui.id().with("bb"), Sense::click());
                ui.horizontal_centered(|ui| {
                    ui.label(RichText::new("ACTIVITY").font(bold(13.0)).color(TEXT_DIM));
                    ui.add_space(10.0);
                    ui.label(RichText::new(&label).color(color));
                    ui.add_space(12.0);
                    ui.allocate_ui(Vec2::new(ui.available_width() - 120.0, 10.0), |ui| {
                        progress(ui, frac, 8.0, BLUE_A, ACCENT_STRONG);
                    });
                    ui.label(RichText::new(format!("{:.0}%", frac * 100.0)).color(TEXT_BRIGHT));
                });
                if resp.clicked() {
                    open = true;
                }
            });
        if open {
            self.page = Page::Activity;
        }
    }

    fn toasts_ui(&mut self, ctx: &egui::Context) {
        let now = Instant::now();
        self.toasts.retain(|t| t.until > now);
        if self.toasts.is_empty() {
            return;
        }
        egui::Area::new(egui::Id::new("toasts"))
            .anchor(egui::Align2::RIGHT_BOTTOM, Vec2::new(-20.0, -60.0))
            .order(egui::Order::Foreground)
            .show(ctx, |ui| {
                for t in &self.toasts {
                    egui::Frame::new()
                        .fill(PANEL_SEL)
                        .stroke(Stroke::new(1.0_f32, t.color))
                        .corner_radius(CornerRadius::same(4))
                        .inner_margin(Margin::symmetric(14, 10))
                        .show(ui, |ui| {
                            ui.label(RichText::new(&t.text).color(TEXT_BRIGHT));
                        });
                    ui.add_space(6.0);
                }
            });
        ctx.request_repaint_after(Duration::from_millis(500));
    }
}

impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if self.sys.poll() {
            self.refresh_library(ctx);
        }
        self.sources.poll();
        self.library.poll();
        if let Some(job) = &mut self.job {
            if job.pump(ctx) {
                self.on_job_finished(ctx);
            }
        }

        if ctx.input(|i| i.viewport().close_requested()) && !self.allow_close && self.job.as_ref().is_some_and(|j| j.running()) {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            self.modal = Some(modals::Modal::ConfirmQuit);
        }

        let sys = self.sys.ready().cloned();
        self.top_bar(ctx, sys.as_deref());
        self.bottom_bar(ctx);

        egui::CentralPanel::default().frame(egui::Frame::new().fill(BG)).show(ctx, |ui| {
            let r = ui.max_rect();
            vgradient(
                ui.painter(),
                egui::Rect::from_min_size(r.min, Vec2::new(r.width(), 260.0)),
                Color32::from_rgb(0x22, 0x3a, 0x52),
                BG,
            );
            let Some(sys) = sys else {
                ui.centered_and_justified(|ui| {
                    ui.vertical_centered(|ui| {
                        ui.add_space(ui.available_height() / 2.0 - 40.0);
                        ui.add(egui::Spinner::new().size(36.0).color(ACCENT));
                        ui.label(RichText::new("Scanning your hardware…").color(TEXT_DIM));
                    });
                });
                return;
            };
            match self.page {
                Page::Home => self.home(ui, &sys),
                Page::Build => self.wizard(ui, &sys),
                Page::Patches => self.patches_page(ui, &sys),
                Page::Library => self.library_page(ui, &sys),
                Page::Hardware => self.hardware_page(ui, &sys),
                Page::Activity => self.activity(ui, &sys),
            }
        });

        if let Some(sys) = self.sys.ready().cloned() {
            self.modal_ui(ctx, &sys);
        }
        self.toasts_ui(ctx);
    }
}

fn nav_item(ui: &mut egui::Ui, text: &str, selected: bool) -> egui::Response {
    let font = bold(15.0);
    let galley = ui.painter().layout_no_wrap(text.to_string(), font, TEXT);
    let (rect, resp) = ui.allocate_exact_size(Vec2::new(galley.size().x, 60.0), Sense::click());
    let color = if selected {
        ACCENT
    } else if resp.hovered() {
        TEXT_BRIGHT
    } else {
        TEXT
    };
    ui.painter().galley(rect.center() - galley.size() / 2.0, galley, color);
    if selected {
        let bar = egui::Rect::from_min_size(egui::pos2(rect.left(), rect.bottom() - 3.0), Vec2::new(rect.width(), 3.0));
        ui.painter().rect_filled(bar, CornerRadius::ZERO, ACCENT_STRONG);
    }
    resp.on_hover_cursor(egui::CursorIcon::PointingHand)
}

fn chip(ui: &mut egui::Ui, text: &str) {
    let galley = ui.painter().layout_no_wrap(text.to_string(), regular(12.5), TEXT);
    let size = galley.size() + Vec2::new(16.0, 8.0);
    let (rect, _) = ui.allocate_exact_size(size, Sense::hover());
    ui.painter().rect_filled(rect, CornerRadius::same(3), Color32::from_rgb(0x23, 0x2c, 0x3a));
    ui.painter().galley(rect.center() - galley.size() / 2.0, galley, TEXT);
}

fn logo(ui: &mut egui::Ui) {
    let (rect, _) = ui.allocate_exact_size(Vec2::new(30.0, 30.0), Sense::hover());
    let p = ui.painter();
    vgradient(p, rect, BLUE_A, BLUE_B);
    let c = rect.center();
    // A stylised chip: core square with pins.
    p.rect_stroke(egui::Rect::from_center_size(c, Vec2::splat(14.0)), CornerRadius::same(2), Stroke::new(2.0_f32, Color32::WHITE), egui::StrokeKind::Middle);
    for i in -1..=1 {
        let o = i as f32 * 4.5;
        for (a, b) in [
            (egui::pos2(c.x + o, rect.top() + 3.0), egui::pos2(c.x + o, c.y - 7.0)),
            (egui::pos2(c.x + o, c.y + 7.0), egui::pos2(c.x + o, rect.bottom() - 3.0)),
            (egui::pos2(rect.left() + 3.0, c.y + o), egui::pos2(c.x - 7.0, c.y + o)),
            (egui::pos2(c.x + 7.0, c.y + o), egui::pos2(rect.right() - 3.0, c.y + o)),
        ] {
            p.line_segment([a, b], Stroke::new(1.5_f32, Color32::from_white_alpha(200)));
        }
    }
    p.circle_filled(c, 3.0, Color32::WHITE);
    ui.add_space(8.0);
    ui.label(RichText::new("KERNELSMITH").font(bold(20.0)).color(TEXT_BRIGHT));
}

/// Window icon drawn in code so the binary is self-contained.
fn make_icon() -> egui::IconData {
    let n = 64usize;
    let mut rgba = vec![0u8; n * n * 4];
    for y in 0..n {
        for x in 0..n {
            let (fx, fy) = (x as f32, y as f32);
            let r = 12.0;
            let inside_round = {
                let cx = fx.clamp(r, n as f32 - 1.0 - r);
                let cy = fy.clamp(r, n as f32 - 1.0 - r);
                (fx - cx).powi(2) + (fy - cy).powi(2) <= r * r
            };
            if !inside_round {
                continue;
            }
            let t = fy / n as f32;
            let mut c = [
                (0x47 as f32 * (1.0 - t) + 0x1a as f32 * t) as u8,
                (0xbf as f32 * (1.0 - t) + 0x44 as f32 * t) as u8,
                (0xff as f32 * (1.0 - t) + 0xc2 as f32 * t) as u8,
            ];
            let (dx, dy) = ((fx - 31.5).abs(), (fy - 31.5).abs());
            let ring = (dx.max(dy) - 12.0).abs() < 2.0;
            let pin = dx.max(dy) > 14.0 && dx.max(dy) < 26.0 && [0.0f32, 9.0].iter().any(|o| (dx.min(dy) - o).abs() < 1.5);
            let core = dx * dx + dy * dy < 25.0;
            if ring || pin || core {
                c = [255, 255, 255];
            }
            let i = (y * n + x) * 4;
            rgba[i..i + 4].copy_from_slice(&[c[0], c[1], c[2], 255]);
        }
    }
    egui::IconData { rgba, width: n as u32, height: n as u32 }
}

pub fn list_presets() -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = std::fs::read_dir(paths::presets_dir())
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|e| e == "toml"))
        .collect();
    v.sort();
    v
}

/// Open a folder or file with the desktop's default handler.
pub fn open_path(p: &Path) {
    let _ = std::process::Command::new("xdg-open")
        .arg(p)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn();
}
