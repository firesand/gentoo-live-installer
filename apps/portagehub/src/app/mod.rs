//! Application state and the window. The work is split into:
//! - [`jobs`]: jobs and batches (transactions), as plain state machines;
//! - [`events`]: starting jobs and reacting to helper events;
//! - [`actions`]: what the views ask for;
//! - [`loader`]: background loading of Portage data;
//! - [`shell`]: navigation, status bar, toasts and global dialogs.

mod actions;
mod events;
pub mod jobs;
mod loader;
mod shell;

pub use jobs::{Job, JobKind};

use crate::data::{Data, DataMsg, PkgDetail, Reload};
use crate::exec::{ExecMsg, Executor, Priv};
use crate::portage::emerge::EmergeOptions;
use crate::portage::pretend::Plan;
use crate::portage::progress::Tracker;
use crate::settings::Settings;
use crate::ui::theme::{self, *};
use crate::views;
use egui::{Frame, Margin};
use jobs::Batch;
use portagehub::protocol::{Event, Op};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::mpsc::{Receiver, Sender, channel};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tab {
    Store,
    Library,
    Emerge,
    Overlays,
    Config,
    Maintenance,
    History,
    Settings,
}

impl Tab {
    pub const MAIN: [Tab; 7] = [Tab::Store, Tab::Library, Tab::Emerge, Tab::Overlays, Tab::Config, Tab::Maintenance, Tab::History];

    pub fn label(&self) -> &'static str {
        match self {
            Tab::Store => "Store",
            Tab::Library => "Library",
            Tab::Emerge => "Emerge",
            Tab::Overlays => "Overlays",
            Tab::Config => "Configuration",
            Tab::Maintenance => "Maintenance",
            Tab::History => "History",
            Tab::Settings => "Settings",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToastKind {
    Info,
    Success,
    Warn,
    Error,
}

#[derive(Debug, Clone)]
pub struct Toast {
    pub kind: ToastKind,
    pub text: String,
    pub created: Instant,
}

/// A file modification to be confirmed and applied as root.
#[derive(Debug, Clone)]
pub struct FileChange {
    pub path: PathBuf,
    pub old: String,
    /// `None` deletes the file.
    pub new: Option<String>,
}

pub enum Action {
    Open(String),
    Tab(Tab),
    Search(String),
    Pretend(EmergeOptions),
    Emerge(EmergeOptions, Vec<(String, String, bool)>),
    Root {
        title: String,
        argv: Vec<String>,
        reload: Vec<Reload>,
        kind: JobKind,
    },
    Capture {
        tag: String,
        argv: Vec<String>,
        privilege: Priv,
    },
    Batch {
        title: String,
        ops: Vec<Op>,
        reload: Vec<Reload>,
        then: Option<Box<Action>>,
    },
    Write {
        title: String,
        changes: Vec<FileChange>,
        reload: Vec<Reload>,
        then: Option<Box<Action>>,
    },
    Reload(Reload),
    Cancel(u64),
    StartHelper,
    LoadDetail(String),
    LoadEbuild(PathBuf),
    RePretend,
    CheckWorld,
    /// A privileged helper operation shown as a job.
    RootOp {
        title: String,
        op: Op,
        kind: JobKind,
        reload: Vec<Reload>,
    },
    /// An unprivileged background command with a specific job kind.
    Job {
        title: String,
        argv: Vec<String>,
        kind: JobKind,
    },
}

#[derive(Default)]
pub struct Captured {
    pub output: String,
    pub code: Option<i32>,
    pub done: bool,
}

pub struct ReviewDialog {
    pub title: String,
    pub changes: Vec<FileChange>,
    pub reload: Vec<Reload>,
    pub then: Option<Box<Action>>,
    pub expanded: usize,
}

pub struct App {
    pub data: Data,
    pub details: HashMap<String, Arc<PkgDetail>>,
    pub ebuilds: HashMap<PathBuf, Arc<String>>,
    pub loading: HashSet<Reload>,
    pending_reload: HashSet<Reload>,
    pub detail_loading: HashSet<String>,
    ebuild_loading: HashSet<PathBuf>,
    data_tx: Sender<DataMsg>,
    data_rx: Receiver<DataMsg>,
    exec_rx: Receiver<ExecMsg>,
    pub exec: Executor,
    pub jobs: Vec<Job>,
    pub batches: Vec<Batch>,
    pub captures: HashMap<String, Captured>,
    pub actions: Vec<Action>,
    pub toasts: Vec<Toast>,
    pub tab: Tab,
    pub settings: Settings,
    pub ctx: egui::Context,
    pub review: Option<ReviewDialog>,
    pub store: views::store::State,
    pub library: views::library::State,
    pub detail: views::details::State,
    pub emerge: views::emerge::State,
    pub overlays: views::overlays::State,
    pub config_view: views::config::State,
    pub history_view: views::history::State,
    pub settings_view: views::settings::State,
    last_log_poll: Instant,
    fonts_applied: Option<String>,
    script: Option<crate::shot::Script>,
    confirm_close: bool,
    allow_close: bool,
    /// Last `emerge -uDN @world --pretend` result and when it ran.
    pub world_plan: Option<(Arc<Plan>, i64)>,
    pub world_job: Option<u64>,
    world_auto_done: bool,
    pub boot_scan: Option<(Arc<portagehub::protocol::BootScan>, i64)>,
    /// A kernel/bootloader/NVIDIA change happened: report the boot check when it lands.
    kernel_attention: bool,
    pub maintenance: views::maintenance::State,
    /// Output of cleanup scans by tag ("depclean", "distfiles", "binpkgs") with time.
    pub cleanup_scans: HashMap<String, (String, Option<i32>, i64)>,
    boot_mounts_cache: (Instant, Vec<crate::portage::mounts::BootMount>),
    /// Boot partitions PortageHub made writable that go back to read-only after the emerge.
    pub remount_restore: Vec<String>,
}

impl App {
    pub fn new(cc: &eframe::CreationContext<'_>) -> App {
        let settings = Settings::load();
        let ctx = cc.egui_ctx.clone();
        theme::install_fonts(&ctx, settings.font_family.as_deref());
        theme::apply(&ctx);
        ctx.set_zoom_factor(settings.ui_scale);
        let (data_tx, data_rx) = channel();
        let (exec_tx, exec_rx) = channel();
        let exec = Executor::new(exec_tx, ctx.clone());
        let mut app = App {
            data: Data::default(),
            details: HashMap::new(),
            ebuilds: HashMap::new(),
            loading: HashSet::new(),
            pending_reload: HashSet::new(),
            detail_loading: HashSet::new(),
            ebuild_loading: HashSet::new(),
            data_tx,
            data_rx,
            exec_rx,
            exec,
            jobs: Vec::new(),
            batches: Vec::new(),
            captures: HashMap::new(),
            actions: Vec::new(),
            toasts: Vec::new(),
            tab: Tab::Store,
            fonts_applied: settings.font_family.clone(),
            settings,
            ctx,
            review: None,
            store: Default::default(),
            library: Default::default(),
            detail: Default::default(),
            emerge: Default::default(),
            overlays: Default::default(),
            config_view: Default::default(),
            history_view: Default::default(),
            settings_view: Default::default(),
            last_log_poll: Instant::now(),
            script: crate::shot::Script::from_env(),
            confirm_close: false,
            allow_close: false,
            world_plan: loader::load_world_cache(),
            world_job: None,
            world_auto_done: false,
            boot_scan: None,
            kernel_attention: false,
            maintenance: Default::default(),
            cleanup_scans: loader::load_cleanup_cache(),
            boot_mounts_cache: (Instant::now() - Duration::from_secs(60), Vec::new()),
            remount_restore: Vec::new(),
        };
        for r in [Reload::Repos, Reload::Vdb, Reload::History, Reload::Config, Reload::MakeConf, Reload::Env] {
            app.spawn_load(r);
        }
        if std::env::var_os("PORTAGEHUB_DEMO").is_some() {
            app.start_demo();
        }
        app
    }

    /// Replay a synthetic parallel emerge so the progress UI can be tried
    /// without building anything (PORTAGEHUB_DEMO=1).
    fn start_demo(&mut self) {
        let pkgs = [
            ("dev-libs/libfoo-1.2.3", "gentoo", 6u64),
            ("media-libs/libbar-0.9", "gentoo", 9),
            ("app-misc/demo-app-2.0", "guru", 14),
            ("x11-misc/demo-tool-1.0_rc1", "gentoo", 5),
        ];
        let planned: Vec<(String, String, bool)> = pkgs.iter().map(|(c, r, _)| (c.to_string(), r.to_string(), false)).collect();
        let mut history = crate::portage::history::History::default();
        for (i, (c, _, secs)) in pkgs.iter().enumerate() {
            let key = crate::portage::atom::split_cpv(c).map(|(a, b, _)| format!("{a}/{b}")).unwrap();
            history.merges.push(crate::portage::history::MergeRecord {
                cpv: c.to_string(),
                key: key.clone(),
                ver: String::new(),
                start: 0,
                end: *secs as i64,
                ok: true,
                binary: false,
            });
            history.by_key.entry(key).or_default().push(i);
        }
        let tracker = Box::new(Tracker::new(&planned, &history));
        let id = self.exec.alloc_id();
        self.jobs.push(Job::new(
            id,
            "emerge --update @demo (demo)".into(),
            JobKind::Emerge { tracker },
            Priv::User,
            vec!["emerge".into(), "@demo".into()],
            vec![],
        ));
        self.emerge.active_job = Some(id);
        let sink = self.exec.sink();
        std::thread::spawn(move || {
            use portagehub::protocol::StreamKind;
            let line = |t: &str| sink(Event::Line { id, stream: StreamKind::Stdout, text: t.to_string() });
            let wait = |s: f32| std::thread::sleep(Duration::from_secs_f32(s));
            line(">>> Verifying ebuild manifests");
            line(">>> Starting parallel fetch");
            let n = pkgs.len();
            line(&format!(">>> Emerging (1 of {n}) {}::{}", pkgs[0].0, pkgs[0].1));
            line(&format!(">>> Emerging (2 of {n}) {}::{}", pkgs[1].0, pkgs[1].1));
            for k in 0..8 {
                line(&format!("make[{k}]: compiling src/file{k}.c"));
                wait(0.5);
            }
            line(&format!(">>> Installing (1 of {n}) {}::{}", pkgs[0].0, pkgs[0].1));
            wait(1.0);
            line(&format!(">>> Completed (1 of {n}) {}::{}", pkgs[0].0, pkgs[0].1));
            line(&format!(">>> Jobs: 1 of {n} complete, 1 running                Load avg: 7.92, 6.10, 4.33"));
            line(&format!(">>> Emerging (3 of {n}) {}::{}", pkgs[2].0, pkgs[2].1));
            wait(3.0);
            line(" * WARNING: demo warning from the build system");
            line(&format!(">>> Installing (2 of {n}) {}::{}", pkgs[1].0, pkgs[1].1));
            wait(1.0);
            line(&format!(">>> Completed (2 of {n}) {}::{}", pkgs[1].0, pkgs[1].1));
            line(&format!(">>> Jobs: 2 of {n} complete, 1 running                Load avg: 5.20, 5.80, 4.40"));
            wait(600.0);
        });
    }

    pub fn push(&mut self, a: Action) {
        self.actions.push(a);
    }

    pub fn toast(&mut self, kind: ToastKind, text: impl Into<String>) {
        self.toasts.push(Toast { kind, text: text.into(), created: Instant::now() });
        self.ctx.request_repaint();
    }

    pub fn is_loading(&self, r: Reload) -> bool {
        self.loading.contains(&r)
    }

    pub fn running_emerge(&self) -> Option<&Job> {
        self.jobs.iter().rev().find(|j| j.running() && matches!(j.kind, JobKind::Emerge { .. }))
    }

    pub fn job(&self, id: u64) -> Option<&Job> {
        self.jobs.iter().find(|j| j.id == id)
    }

    pub fn running_count(&self) -> usize {
        self.jobs
            .iter()
            .filter(|j| j.running() && !matches!(j.kind, JobKind::Capture { .. } | JobKind::WorldCheck | JobKind::BootScan | JobKind::CleanupScan { .. }))
            .count()
            + self.batches.len()
    }
}

impl eframe::App for App {
    fn logic(&mut self, _ctx: &egui::Context, _frame: &mut eframe::Frame) {
        while let Ok(m) = self.data_rx.try_recv() {
            self.handle_data(m);
        }
        while let Ok(m) = self.exec_rx.try_recv() {
            self.handle_exec(m);
        }
        self.poll_build_logs();
        if !self.world_auto_done && self.data.catalog.is_some() && self.data.vdb.is_some() && self.data.repos.is_some() {
            self.world_auto_done = true;
            let last_sync = self.data.last_sync().unwrap_or(0);
            let stale = self.world_plan.as_ref().is_none_or(|(_, ts)| *ts < last_sync || crate::fmt::now_ts() - ts > 6 * 3600);
            if self.settings.auto_world_check && stale && std::env::var_os("PORTAGEHUB_SCRIPT").is_none() {
                self.actions.push(Action::CheckWorld);
            }
        }
        self.process_actions();
        if self.running_emerge().is_some() {
            self.ctx.request_repaint_after(Duration::from_millis(500));
        }
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        if self.fonts_applied != self.settings.font_family {
            theme::install_fonts(&ctx, self.settings.font_family.as_deref());
            self.fonts_applied = self.settings.font_family.clone();
        }
        // F5 reloads everything that's cheap to reload.
        if ctx.input(|i| i.key_pressed(egui::Key::F5)) {
            for r in [Reload::Repos, Reload::Vdb, Reload::Config, Reload::MakeConf, Reload::History] {
                self.spawn_load(r);
            }
        }
        egui::Panel::top("nav").frame(Frame::new().fill(NAV).inner_margin(Margin::symmetric(8, 6))).show_separator_line(false).show(ui, |ui| self.nav_bar(ui));
        egui::Panel::bottom("status")
            .frame(Frame::new().fill(NAV).inner_margin(Margin::symmetric(10, 3)))
            .show_separator_line(false)
            .show(ui, |ui| self.status_bar(ui));
        egui::CentralPanel::default().frame(Frame::new().fill(BG)).show(ui, |ui| match self.tab {
            Tab::Store => views::store::show(self, ui),
            Tab::Library => views::library::show(self, ui),
            Tab::Emerge => views::emerge::show(self, ui),
            Tab::Overlays => views::overlays::show(self, ui),
            Tab::Config => views::config::show(self, ui),
            Tab::Maintenance => views::maintenance::show(self, ui),
            Tab::History => views::history::show(self, ui),
            Tab::Settings => views::settings::show(self, ui),
        });
        self.review_ui(&ctx);
        self.close_guard(&ctx);
        self.toasts_ui(&ctx);
        self.process_actions();
        if let Some(mut s) = self.script.take() {
            crate::shot::run(self, &ctx, &mut s);
            self.script = Some(s);
        }
    }

    fn save(&mut self, _storage: &mut dyn eframe::Storage) {
        self.settings.save();
    }

    fn on_exit(&mut self) {
        self.settings.save();
        self.exec.stop_helper();
    }
}
