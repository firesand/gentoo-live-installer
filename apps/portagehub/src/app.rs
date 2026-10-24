//! Application state, background loading, job bookkeeping and the window shell.

use crate::batch;
use crate::data::{self, Data, DataMsg, PkgDetail, Reload};
use crate::exec::{ExecMsg, Executor, HelperState, Priv};
use crate::portage::emerge::EmergeOptions;
use crate::portage::pretend::{self, Plan};
use crate::portage::progress::{self, Tracker};
use crate::portage::{catalog, config, history, makeconf, repos, system, useflags, vdb};
use crate::settings::Settings;
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use crate::views;
use egui::{Align, Color32, CornerRadius, Frame, Layout, Margin, RichText, Stroke};
use egui_phosphor::regular as icon;
use portagehub::protocol::{Event, Expect, Op, TxInfo};
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
    Root { title: String, argv: Vec<String>, reload: Vec<Reload>, kind: JobKind },
    Capture { tag: String, argv: Vec<String>, privilege: Priv },
    Batch { title: String, ops: Vec<Op>, reload: Vec<Reload>, then: Option<Box<Action>> },
    Write { title: String, changes: Vec<FileChange>, reload: Vec<Reload>, then: Option<Box<Action>> },
    /// Ask before running `ops` as one batch. `changes` only make the dialog show what will happen
    /// (a diff per file); `unreadable` files cannot be previewed and `notes` explain what is left out.
    /// The confirmation is not optional, whatever the "review changes" setting says.
    ReviewOps { title: String, changes: Vec<FileChange>, unreadable: Vec<PathBuf>, notes: Vec<String>, ops: Vec<Op>, reload: Vec<Reload> },
    Reload(Reload),
    Toast(ToastKind, String),
    Cancel(u64),
    /// Stop with SIGINT now, SIGTERM after ten seconds and SIGKILL after ten more if it still runs.
    ForceStop(u64),
    StartHelper,
    LoadDetail(String),
    LoadEbuild(PathBuf),
    RePretend,
    CheckWorld,
    /// A privileged helper operation shown as a job.
    RootOp { title: String, op: Op, kind: JobKind, reload: Vec<Reload> },
    /// An unprivileged background command with a specific job kind.
    Job { title: String, argv: Vec<String>, kind: JobKind },
}

#[derive(Debug, Clone)]
pub enum JobKind {
    Generic,
    Pretend { opts: EmergeOptions },
    Emerge { opts: EmergeOptions, tracker: Box<Tracker> },
    Sync,
    Capture { tag: String },
    /// Background `emerge -uDN @world --pretend` for the Store's update list.
    WorldCheck,
    /// Root-only boot files (JSON from the helper).
    BootScan,
    /// Something that changed boot files: re-check afterwards.
    BootFix,
    /// `emerge --pretend --depclean`, eclean-* --pretend for the Cleanup page.
    CleanupScan { tag: String },
    /// mount -o remount of a boot partition.
    Remount,
    /// The helper's journal of recent changes (JSON from `Op::ListTx`).
    ChangeList,
    /// Undo of a recorded change.
    Undo,
}

#[derive(Debug, Clone)]
pub struct Job {
    pub id: u64,
    pub title: String,
    pub kind: JobKind,
    pub privilege: Priv,
    pub argv: Vec<String>,
    /// Output lines (is it stderr?, text). Shared with the views: appending copies only if one still holds it.
    pub lines: Arc<Vec<(bool, String)>>,
    /// How many of the oldest lines were dropped to keep the log bounded.
    pub dropped_lines: usize,
    pub partial: Option<String>,
    pub started: Instant,
    pub ended: Option<Instant>,
    pub exit: Option<Option<i32>>,
    pub error: Option<String>,
    pub reload: Vec<Reload>,
    /// Set once the user asked to stop it: the last signal sent and when.
    pub stop: Option<(crate::stop::Stage, Instant)>,
}

impl Job {
    pub fn running(&self) -> bool {
        self.ended.is_none()
    }

    pub fn success(&self) -> bool {
        self.exit == Some(Some(0)) && self.error.is_none()
    }

    pub fn output(&self) -> String {
        let mut s = String::new();
        for (_, l) in self.lines.iter() {
            s.push_str(l);
            s.push('\n');
        }
        s
    }

    pub fn status_text(&self) -> String {
        if self.running() {
            "Running".into()
        } else if let Some(e) = &self.error {
            format!("Error: {e}")
        } else {
            match self.exit {
                Some(Some(0)) => "Finished".into(),
                Some(Some(c)) => format!("Exited with code {c}"),
                Some(None) => "Killed".into(),
                None => "Finished".into(),
            }
        }
    }
}

pub struct Batch {
    pub title: String,
    pub run: batch::Run,
    pub current: Option<u64>,
    pub reload: Vec<Reload>,
    pub then: Option<Box<Action>>,
}

impl Batch {
    /// (operations finished, operations in total).
    pub fn progress(&self) -> (usize, usize) {
        self.run.progress()
    }
}

/// The newest change the helper recorded for undo.
#[derive(Debug, Clone)]
pub struct LastChange {
    pub tx: String,
    pub title: String,
}

#[derive(Default)]
pub struct Captured {
    pub output: String,
    pub code: Option<i32>,
    pub done: bool,
    pub job: u64,
}

pub struct ReviewDialog {
    pub title: String,
    pub changes: Vec<FileChange>,
    pub reload: Vec<Reload>,
    pub then: Option<Box<Action>>,
    pub expanded: usize,
    /// Run these operations instead of writing `changes` (which then only illustrate them).
    pub ops: Option<Vec<Op>>,
    /// Files that exist but cannot be shown as a diff (root-only).
    pub unreadable: Vec<PathBuf>,
    pub notes: Vec<String>,
    /// Lines per change that make Portage run commands as root (see `danger.rs`).
    pub warnings: Vec<Vec<crate::danger::Warning>>,
}

impl ReviewDialog {
    pub fn new(title: String, changes: Vec<FileChange>, reload: Vec<Reload>, then: Option<Box<Action>>) -> ReviewDialog {
        let warnings = changes.iter().map(crate::danger::flag_dangerous_lines).collect();
        ReviewDialog { title, changes, reload, then, expanded: 0, ops: None, unreadable: Vec::new(), notes: Vec::new(), warnings }
    }
}

pub struct App {
    pub data: Data,
    pub details: HashMap<String, Arc<PkgDetail>>,
    pub ebuilds: HashMap<PathBuf, Arc<String>>,
    pub loading: HashSet<Reload>,
    pending_reload: HashSet<Reload>,
    pub detail_loading: HashSet<String>,
    /// Kernels whose NVIDIA driver check is running.
    nvidia_probing: HashSet<String>,
    ebuild_loading: HashSet<PathBuf>,
    data_tx: Sender<DataMsg>,
    data_rx: Receiver<DataMsg>,
    exec_rx: Receiver<ExecMsg>,
    pub exec: Executor,
    pub jobs: Vec<Job>,
    pub batches: Vec<Batch>,
    /// The build directories of the running emerge are private to the portage user, so only emerge's
    /// own output feeds the live phases (Activity says so).
    pub build_logs_private: bool,
    /// The admin helper's audit log is not recording (shown in Settings).
    pub audit_note: Option<String>,
    /// The change that can be undone with one click (a multi-file batch that was committed).
    pub last_change: Option<LastChange>,
    /// The helper's journal, newest first (loaded on demand: the journal is root-only).
    pub recent_changes: Option<Vec<TxInfo>>,
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
    /// The settings as last written, and when: a change is saved within a second, not at exit only.
    saved_settings: Settings,
    settings_saved_at: Instant,
    /// The window has been fitted to its monitor (done once, on the first frame).
    size_checked: bool,
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

/// A detail page with nothing in it, so a page that cannot be built stops spinning.
fn empty_detail(key: &str) -> PkgDetail {
    PkgDetail {
        key: key.to_string(),
        cat: key.split('/').next().unwrap_or("").into(),
        pn: key.split('/').nth(1).unwrap_or("").into(),
        versions: Vec::new(),
        xml: Default::default(),
        pkg_dir: None,
        files: Vec::new(),
        installed_meta: Vec::new(),
    }
}

/// A load failed: it no longer counts as running, and nothing waits to redo it.
fn forget_load(loading: &mut HashSet<Reload>, pending: &mut HashSet<Reload>, r: Reload) {
    loading.remove(&r);
    pending.remove(&r);
}

/// What a refused write means for a person.
fn explain_write_error(msg: &str) -> String {
    match msg.split_once("changed on disk: ") {
        Some((_, path)) => format!(
            "{path} was changed outside PortageHub after it was read, so PortageHub left it alone. The settings are being reloaded: make the edit again on the current content."
        ),
        None => msg.to_string(),
    }
}

/// Settings to read again after a write was refused because a file had changed.
fn stale_file_reloads(msg: &str) -> Vec<Reload> {
    if msg.contains("changed on disk: ") {
        vec![Reload::Config, Reload::MakeConf, Reload::Repos]
    } else {
        Vec::new()
    }
}

fn world_cache_path() -> PathBuf {
    let base = std::env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".cache")))
        .unwrap_or_else(|| PathBuf::from("/tmp"));
    base.join("portagehub").join("world-check.txt")
}

/// First line: "<unix time> <exit code>", then the raw emerge output.
fn load_world_cache() -> Option<(Arc<Plan>, i64)> {
    let text = std::fs::read_to_string(world_cache_path()).ok()?;
    let (head, body) = text.split_once('\n')?;
    let mut it = head.split_whitespace();
    let ts: i64 = it.next()?.parse().ok()?;
    let code: Option<i32> = it.next().and_then(|c| c.parse().ok());
    Some((Arc::new(pretend::parse(body, code)), ts))
}

fn save_world_cache(ts: i64, code: Option<i32>, output: &str) {
    let p = world_cache_path();
    if let Some(d) = p.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let _ = std::fs::write(p, format!("{ts} {}\n{output}", code.map(|c| c.to_string()).unwrap_or_else(|| "-".into())));
}

fn cleanup_cache_path(tag: &str) -> PathBuf {
    world_cache_path().with_file_name(format!("cleanup-{tag}.txt"))
}

fn load_cleanup_cache() -> HashMap<String, (String, Option<i32>, i64)> {
    let mut m = HashMap::new();
    for tag in ["depclean", "distfiles", "binpkgs"] {
        if let Ok(text) = std::fs::read_to_string(cleanup_cache_path(tag))
            && let Some((head, body)) = text.split_once('\n')
        {
            let mut it = head.split_whitespace();
            let ts: i64 = it.next().and_then(|x| x.parse().ok()).unwrap_or(0);
            let code = it.next().and_then(|c| c.parse().ok());
            m.insert(tag.to_string(), (body.to_string(), code, ts));
        }
    }
    m
}

impl App {
    pub fn new(cc: &eframe::CreationContext<'_>) -> App {
        let (settings, settings_note) = Settings::load();
        let ctx = cc.egui_ctx.clone();
        theme::install_fonts(&ctx, settings.font_family.as_deref());
        theme::apply(&ctx);
        ctx.set_zoom_factor(settings.ui_scale);
        let (data_tx, data_rx) = channel();
        let (exec_tx, exec_rx) = channel();
        let exec = Executor::new(exec_tx, ctx.clone());
        let settings_on_disk = settings.clone();
        let mut app = App {
            data: Data::default(),
            details: HashMap::new(),
            ebuilds: HashMap::new(),
            loading: HashSet::new(),
            pending_reload: HashSet::new(),
            detail_loading: HashSet::new(),
            nvidia_probing: HashSet::new(),
            ebuild_loading: HashSet::new(),
            data_tx,
            data_rx,
            exec_rx,
            exec,
            jobs: Vec::new(),
            batches: Vec::new(),
            build_logs_private: false,
            audit_note: None,
            last_change: None,
            recent_changes: None,
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
            saved_settings: settings_on_disk,
            settings_saved_at: Instant::now(),
            size_checked: false,
            script: crate::shot::Script::from_env(),
            confirm_close: false,
            allow_close: false,
            world_plan: load_world_cache(),
            world_job: None,
            world_auto_done: false,
            boot_scan: None,
            kernel_attention: false,
            maintenance: Default::default(),
            cleanup_scans: load_cleanup_cache(),
            boot_mounts_cache: (Instant::now() - Duration::from_secs(60), Vec::new()),
            remount_restore: Vec::new(),
        };
        if let Some(note) = settings_note {
            app.toast(ToastKind::Warn, note);
        }
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
            history.merges.push(crate::portage::history::MergeRecord { cpv: c.to_string(), key: key.clone(), ver: String::new(), start: 0, end: *secs as i64, ok: true, binary: false });
            history.by_key.entry(key).or_default().push(i);
        }
        let tracker = Box::new(Tracker::new(&planned, &history));
        let id = self.exec.alloc_id();
        let opts = EmergeOptions::default().targets(&["@demo"]);
        self.jobs.push(Job {
            id,
            title: "emerge --update @demo (demo)".into(),
            kind: JobKind::Emerge { opts, tracker },
            privilege: Priv::User,
            argv: vec!["emerge".into(), "@demo".into()],
            lines: Arc::default(),
            dropped_lines: 0,
            partial: None,
            started: Instant::now(),
            ended: None,
            exit: None,
            error: None,
            reload: vec![],
            stop: None,
        });
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

    /// Send the next signal of the stop schedule to job `id` (see `stop.rs`).
    fn stop_job(&mut self, id: u64) {
        let Some(job) = self.jobs.iter_mut().find(|j| j.id == id && j.running()) else { return };
        let since = job.stop.map(|(_, at)| at.elapsed()).unwrap_or_default();
        let Some(stage) = crate::stop::next_stop(job.stop.map(|(s, _)| s), since) else { return };
        job.stop = Some((stage, Instant::now()));
        let privilege = job.privilege;
        self.exec.cancel(id, privilege, stage.signal());
        self.toast(ToastKind::Info, stage.describe());
    }

    /// Write the settings when they differ from what is on disk (at most once a second), so a
    /// crash or a SIGTERM no longer loses the toggles of the whole session.
    fn save_settings_if_changed(&mut self) {
        if self.settings == self.saved_settings {
            return;
        }
        if self.settings_saved_at.elapsed() < Duration::from_secs(1) {
            self.ctx.request_repaint_after(Duration::from_secs(1));
            return;
        }
        self.settings.save();
        self.saved_settings = self.settings.clone();
        self.settings_saved_at = Instant::now();
    }

    /// Jobs the user asked to stop that still run get the next, harder signal on time.
    fn escalate_stops(&mut self) {
        let due: Vec<u64> = self
            .jobs
            .iter()
            .filter(|j| j.running())
            .filter_map(|j| j.stop.map(|(stage, at)| (j.id, crate::stop::next_stop(Some(stage), at.elapsed()))))
            .filter_map(|(id, next)| next.map(|_| id))
            .collect();
        for id in due {
            self.stop_job(id);
        }
        if self.jobs.iter().any(|j| j.running() && j.stop.is_some_and(|(s, _)| s != crate::stop::Stage::Killed)) {
            self.ctx.request_repaint_after(Duration::from_secs(1));
        }
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
            .filter(|j| j.running() && !matches!(j.kind, JobKind::Capture { .. } | JobKind::WorldCheck | JobKind::BootScan | JobKind::CleanupScan { .. } | JobKind::ChangeList))
            .count()
            + self.batches.len()
    }

    // ------------------------------------------------------------------
    // Background loading

    pub fn spawn_load(&mut self, r: Reload) {
        if self.loading.contains(&r) {
            // A load started from older data is still running: redo it when it lands.
            self.pending_reload.insert(r);
            return;
        }
        let snapshot = self.data.clone();
        let tx = self.data_tx.clone();
        let ctx = self.ctx.clone();
        match r {
            Reload::Catalog | Reload::UseDesc | Reload::News | Reload::Masks if snapshot.repos.is_none() => {
                self.spawn_load(Reload::Repos);
                return;
            }
            Reload::Kernels if snapshot.vdb.is_none() => return,
            Reload::Leftovers if snapshot.vdb.is_none() || snapshot.catalog.is_none() || snapshot.config.is_none() || snapshot.env.is_none() => return,
            Reload::Updates
                if snapshot.catalog.is_none()
                    || snapshot.vdb.is_none()
                    || snapshot.env.is_none()
                    || snapshot.config.is_none()
                    || snapshot.masks.is_none() =>
            {
                return;
            }
            _ => {}
        }
        self.loading.insert(r);
        let keep_set = self.settings.kernel_keep_set.clone();
        let log_days = self.settings.log_keep_days;
        std::thread::spawn(move || {
            let msg = data::run_loader(r, move || match r {
                Reload::Repos => DataMsg::Repos(Arc::new(repos::load())),
                Reload::Catalog => DataMsg::Catalog(Arc::new(catalog::scan(snapshot.repos.as_ref().unwrap()))),
                Reload::Vdb => DataMsg::Vdb(Arc::new(vdb::load())),
                Reload::History => DataMsg::History(Arc::new(history::load())),
                Reload::Config => DataMsg::Config(Arc::new(config::load())),
                Reload::MakeConf => DataMsg::MakeConf(Arc::new(makeconf::load())),
                Reload::Env => DataMsg::Env(Arc::new(system::load_env())),
                Reload::UseDesc => {
                    let r = snapshot.repos.as_ref().unwrap();
                    let locs: Vec<&std::path::Path> = r.repos.iter().filter(|r| r.exists).map(|r| r.location.as_path()).collect();
                    DataMsg::UseDesc(Arc::new(useflags::load(&locs)))
                }
                Reload::RemoteRepos => {
                    let out = std::process::Command::new("eselect").args(["--colour=no", "repository", "list"]).output();
                    let text = out.map(|o| String::from_utf8_lossy(&o.stdout).to_string()).unwrap_or_default();
                    DataMsg::RemoteRepos(Arc::new(repos::parse_eselect_list(&text)))
                }
                Reload::Profiles => {
                    let out = std::process::Command::new("eselect").args(["--colour=no", "profile", "list"]).output();
                    let text = out.map(|o| String::from_utf8_lossy(&o.stdout).to_string()).unwrap_or_default();
                    DataMsg::Profiles(Arc::new(system::parse_profiles(&text)))
                }
                Reload::News => {
                    let r = snapshot.repos.as_ref().unwrap();
                    let mut all = Vec::new();
                    for repo in r.repos.iter().filter(|r| r.exists) {
                        all.extend(system::load_news(&repo.name, &repo.location));
                    }
                    all.sort_by(|a, b| b.posted.cmp(&a.posted));
                    DataMsg::News(Arc::new(all))
                }
                Reload::Updates => DataMsg::Updates(Arc::new(data::compute_updates(&snapshot))),
                Reload::Masks => DataMsg::Masks(Arc::new(crate::portage::profile::load(snapshot.repos.as_ref().unwrap()))),
                Reload::Kernels => DataMsg::Kernels(Arc::new(crate::portage::kernel::load(snapshot.vdb.as_ref().unwrap(), &keep_set))),
                Reload::Leftovers => {
                    let env = snapshot.env.as_ref().unwrap();
                    DataMsg::Leftovers(Arc::new(crate::portage::cleanup::scan(
                        snapshot.vdb.as_ref().unwrap(),
                        snapshot.catalog.as_ref().unwrap(),
                        snapshot.config.as_ref().unwrap(),
                        env.arch(),
                        env.get("PORT_LOGDIR"),
                        log_days,
                    )))
                }
            });
            let _ = tx.send(msg);
            ctx.request_repaint();
        });
    }

    pub fn request_detail(&mut self, key: &str) {
        if self.details.contains_key(key) || self.detail_loading.contains(key) || self.data.catalog.is_none() {
            return;
        }
        self.detail_loading.insert(key.to_string());
        let snapshot = self.data.clone();
        let tx = self.data_tx.clone();
        let ctx = self.ctx.clone();
        let key = key.to_string();
        std::thread::spawn(move || {
            let msg = data::run_detail(&key.clone(), || match data::build_detail(&key, &snapshot) {
                Some(d) => DataMsg::Detail(key.clone(), Arc::new(d)),
                None => DataMsg::Detail(key.clone(), Arc::new(empty_detail(&key))),
            });
            let _ = tx.send(msg);
            ctx.request_repaint();
        });
    }

    fn invalidate_details(&mut self) {
        self.details.clear();
        self.detail_loading.clear();
    }

    fn handle_data(&mut self, msg: DataMsg) {
        let done = |s: &mut Self, r: Reload| {
            s.loading.remove(&r);
        };
        match msg {
            DataMsg::Repos(r) => {
                done(self, Reload::Repos);
                for w in &r.warnings {
                    self.toast(ToastKind::Warn, w.clone());
                }
                self.data.repos = Some(r);
                self.spawn_load(Reload::Catalog);
                self.spawn_load(Reload::UseDesc);
                self.spawn_load(Reload::News);
                self.spawn_load(Reload::Masks);
            }
            DataMsg::Catalog(c) => {
                done(self, Reload::Catalog);
                self.data.catalog = Some(c);
                self.invalidate_details();
                self.spawn_load(Reload::Updates);
                self.spawn_load(Reload::Leftovers);
            }
            DataMsg::Vdb(v) => {
                done(self, Reload::Vdb);
                self.data.vdb = Some(v);
                self.invalidate_details();
                self.spawn_load(Reload::Updates);
                self.spawn_load(Reload::Kernels);
                self.spawn_load(Reload::Leftovers);
            }
            DataMsg::History(h) => {
                done(self, Reload::History);
                self.data.history = Some(h);
            }
            DataMsg::Config(c) => {
                done(self, Reload::Config);
                self.data.config = Some(c);
                self.invalidate_details();
                self.spawn_load(Reload::Updates);
                self.spawn_load(Reload::Leftovers);
            }
            DataMsg::MakeConf(m) => {
                done(self, Reload::MakeConf);
                self.data.makeconf = Some(m);
            }
            DataMsg::Env(e) => {
                done(self, Reload::Env);
                self.data.env = Some(e);
                self.invalidate_details();
                self.spawn_load(Reload::Updates);
                self.spawn_load(Reload::Leftovers);
            }
            DataMsg::UseDesc(u) => {
                done(self, Reload::UseDesc);
                self.data.usedesc = Some(u);
            }
            DataMsg::RemoteRepos(r) => {
                done(self, Reload::RemoteRepos);
                self.data.remote_repos = Some(r);
            }
            DataMsg::Profiles(p) => {
                done(self, Reload::Profiles);
                self.data.profiles = Some(p);
            }
            DataMsg::News(n) => {
                done(self, Reload::News);
                self.data.news = Some(n);
            }
            DataMsg::Kernels(k) => {
                done(self, Reload::Kernels);
                self.data.kernels = Some(k);
                self.maintenance.show_nvidia_help = false;
                if self.kernel_attention && !self.exec.root_ready() {
                    self.report_boot_check();
                }
            }
            DataMsg::Leftovers(l) => {
                done(self, Reload::Leftovers);
                self.data.leftovers = Some(l);
            }
            DataMsg::Masks(m) => {
                done(self, Reload::Masks);
                self.data.masks = Some(m);
                self.invalidate_details();
                self.spawn_load(Reload::Updates);
            }
            DataMsg::Updates(u) => {
                done(self, Reload::Updates);
                self.data.updates = Some(u);
            }
            DataMsg::Detail(k, d) => {
                self.detail_loading.remove(&k);
                self.details.insert(k, d);
            }
            DataMsg::Ebuild(p, t) => {
                self.ebuild_loading.remove(&p);
                self.ebuilds.insert(p, t);
            }
            DataMsg::Failed(r, why) => {
                forget_load(&mut self.loading, &mut self.pending_reload, r);
                self.toast(ToastKind::Error, format!("Loading {} failed: {why}. Press F5 to try again.", r.name()));
            }
            DataMsg::DetailFailed(key, why) => {
                self.detail_loading.remove(&key);
                self.details.insert(key.clone(), Arc::new(empty_detail(&key)));
                self.toast(ToastKind::Error, format!("The page of {key} could not be built: {why}"));
            }
            DataMsg::NvidiaProbe { kver, ok, detail } => {
                self.nvidia_probing.remove(&kver);
                if ok {
                    self.mark_kernel_good_after_probe(kver);
                } else {
                    self.toast(ToastKind::Error, format!("nvidia-smi failed ({detail}): the NVIDIA driver does not work on this kernel, so it is not marked known-good. Rebuild nvidia-drivers first."));
                }
            }
        }
        let ready: Vec<Reload> = self.pending_reload.iter().filter(|r| !self.loading.contains(r)).copied().collect();
        for r in ready {
            self.pending_reload.remove(&r);
            self.spawn_load(r);
        }
    }

    // ------------------------------------------------------------------
    // Jobs

    fn start_job(&mut self, title: String, argv: Vec<String>, env: Vec<(String, String)>, privilege: Priv, kind: JobKind, reload: Vec<Reload>) -> u64 {
        let id = self.exec.run(argv.clone(), env, privilege);
        self.jobs.push(Job {
            id,
            title,
            kind,
            privilege,
            argv,
            lines: Arc::default(),
            dropped_lines: 0,
            partial: None,
            started: Instant::now(),
            ended: None,
            exit: None,
            error: None,
            reload,
            stop: None,
        });
        // Keep the job list bounded.
        if self.jobs.len() > 60 {
            let drop_n = self.jobs.len() - 60;
            let mut removed = 0;
            self.jobs.retain(|j| {
                if removed < drop_n && !j.running() {
                    removed += 1;
                    false
                } else {
                    true
                }
            });
        }
        id
    }

    fn handle_exec(&mut self, msg: ExecMsg) {
        match msg {
            ExecMsg::Notice(msg) => {
                if msg.starts_with("Audit log:") {
                    self.audit_note = Some(msg.clone());
                }
                self.toast(ToastKind::Warn, msg);
            }
            ExecMsg::HelperReady(version, warnings) => {
                self.exec.on_helper_ready(version.as_deref());
                self.audit_note = warnings.iter().find(|w| w.starts_with("Audit log:")).cloned();
                for w in warnings {
                    self.toast(ToastKind::Warn, w);
                }
                let mine = env!("CARGO_PKG_VERSION");
                if self.exec.helper_outdated {
                    self.toast(
                        ToastKind::Warn,
                        format!(
                            "The installed admin helper ({}) is older than PortageHub {mine}. Newer actions need the matching helper: run sudo dist/install.sh.",
                            version.unwrap_or_else(|| "0.1".into())
                        ),
                    );
                } else {
                    self.toast(ToastKind::Success, "Administrator session started — privileged actions won't ask again until it ends (End admin session, quitting PortageHub, or 15 minutes without activity).");
                }
            }
            ExecMsg::HelperGone(exit) => {
                let was = self.exec.helper_state.clone();
                self.exec.on_helper_gone(exit.clone());
                if was != HelperState::NotStarted {
                    match exit.problem() {
                        Some(reason) => self.toast(ToastKind::Warn, format!("Administrator session ended: {reason}")),
                        None => self.toast(ToastKind::Info, "Admin session closed after 15 minutes idle — you will be asked again when needed."),
                    }
                }
            }
            ExecMsg::Event(ev, at) => self.handle_event(ev, at),
        }
    }

    fn handle_event(&mut self, ev: Event, at: Instant) {
        let Some(id) = ev.id() else { return };
        // Batches first.
        if let Some(bi) = self.batches.iter().position(|b| b.current == Some(id)) {
            if let Event::Data { text, .. } = &ev {
                // The id of a transaction the batch just began.
                self.batches[bi].run.note_data(text.clone());
            }
            let outcome = match &ev {
                Event::Ok { .. } => Some(Ok(())),
                Event::Exit { code: Some(0), .. } => Some(Ok(())),
                Event::Exit { code, .. } => Some(Err(format!("command failed (exit {:?})", code))),
                Event::Err { msg, .. } => Some(Err(msg.clone())),
                _ => None,
            };
            if let Some(o) = outcome {
                self.advance_batch(bi, o);
            }
            return;
        }
        let Some(ji) = self.jobs.iter().position(|j| j.id == id) else { return };
        let history = self.data.history.clone().unwrap_or_default();
        let job = &mut self.jobs[ji];
        match ev {
            Event::Started { .. } | Event::Ready { .. } | Event::Notice { .. } => {}
            Event::Data { text, .. } => {
                Arc::make_mut(&mut job.lines).push((false, text));
            }
            Event::Ok { .. } => {
                // Helper operations (not programs) finish with Ok.
                job.exit = Some(Some(0));
                job.ended = Some(at);
                self.finish_job(ji);
            }
            Event::Line { stream, text, .. } => {
                if let JobKind::Emerge { tracker, .. } = &mut job.kind {
                    tracker.feed(&text, &history, at);
                }
                job.partial = None;
                let lines = Arc::make_mut(&mut job.lines);
                lines.push((stream == portagehub::protocol::StreamKind::Stderr, text));
                if lines.len() > 60_000 {
                    lines.drain(..10_000);
                    job.dropped_lines += 10_000;
                }
            }
            Event::Partial { text, .. } => {
                if let JobKind::Emerge { tracker, .. } = &mut job.kind {
                    tracker.feed(&text, &history, at);
                }
                job.partial = Some(text);
            }
            Event::Err { msg, .. } => {
                job.error = Some(msg);
                job.ended = Some(at);
                self.finish_job(ji);
            }
            Event::Exit { code, .. } => {
                job.exit = Some(code);
                job.ended = Some(at);
                self.finish_job(ji);
            }
        }
    }

    fn finish_job(&mut self, ji: usize) {
        let job = self.jobs[ji].clone();
        let ok = job.success();
        for r in &job.reload {
            self.spawn_load(*r);
        }
        match &job.kind {
            JobKind::Pretend { opts } => {
                let mut plan: Plan = pretend::parse(&job.output(), job.exit.flatten());
                if let Some(e) = &job.error {
                    plan.messages.push(e.clone());
                }
                if self.emerge.pretend_job == Some(job.id) {
                    self.emerge.plan = Some(plan);
                    self.emerge.plan_opts = Some(opts.clone());
                }
            }
            JobKind::Emerge { .. } => {
                if let JobKind::Emerge { tracker, .. } = &mut self.jobs[ji].kind {
                    tracker.finish(ok);
                }
                if ok {
                    self.toast(ToastKind::Success, format!("{} finished successfully", job.title));
                } else {
                    self.toast(ToastKind::Error, format!("{} failed — see the log", job.title));
                }
                for r in [Reload::Vdb, Reload::History, Reload::Config] {
                    self.spawn_load(r);
                }
                if self.settings.auto_world_check {
                    self.actions.push(Action::CheckWorld);
                }
                self.spawn_load(Reload::Leftovers);
                let touched_kernel = match &job.kind {
                    JobKind::Emerge { tracker, .. } => tracker.items.iter().any(|i| {
                        i.stage == crate::portage::progress::Stage::Done
                            && (i.key.starts_with("sys-kernel/") || i.key.starts_with("sys-boot/") || i.key == "virtual/dist-kernel" || i.key == "x11-drivers/nvidia-drivers")
                    }),
                    _ => false,
                };
                if !self.remount_restore.is_empty() && self.settings.boot_restore_ro {
                    let targets = std::mem::take(&mut self.remount_restore);
                    for t in targets {
                        self.actions.push(Action::RootOp {
                            title: format!("Make {t} read-only again"),
                            op: Op::Remount { target: t.clone(), rw: false },
                            kind: JobKind::Remount,
                            reload: vec![],
                        });
                    }
                }
                if touched_kernel && self.settings.boot_check_after_kernel {
                    self.kernel_attention = true;
                    self.toast(ToastKind::Info, "A kernel, bootloader or NVIDIA driver changed — checking the boot setup…");
                    self.spawn_load(Reload::Kernels);
                    if self.exec.root_ready() {
                        self.actions.push(Action::RootOp { title: "Read boot configuration".into(), op: Op::BootScan, kind: JobKind::BootScan, reload: vec![] });
                    }
                }
                let out = job.output();
                if out.contains("needs updating") {
                    self.toast(ToastKind::Warn, "Some configuration files need updating (Configuration → Config updates).");
                }
                if out.contains("@preserved-rebuild") {
                    self.toast(ToastKind::Warn, "Preserved libraries detected: run “Rebuild preserved libraries”.");
                }
            }
            JobKind::Sync => {
                if ok {
                    self.toast(ToastKind::Success, format!("{} completed", job.title));
                } else {
                    self.toast(ToastKind::Error, format!("{} failed", job.title));
                }
                for r in [Reload::Repos, Reload::History] {
                    self.spawn_load(r);
                }
                if ok && self.settings.auto_world_check {
                    self.actions.push(Action::CheckWorld);
                }
            }
            JobKind::BootScan => {
                if let Some(text) = job.lines.iter().map(|l| l.1.as_str()).find(|l| l.starts_with('{'))
                    && let Ok(scan) = serde_json::from_str::<portagehub::protocol::BootScan>(text)
                {
                    self.boot_scan = Some((Arc::new(scan), crate::fmt::now_ts()));
                } else if let Some(e) = &job.error {
                    self.toast(ToastKind::Error, format!("Could not read the boot configuration: {e}"));
                }
                if self.kernel_attention {
                    self.report_boot_check();
                }
            }
            JobKind::BootFix => {
                if ok {
                    self.toast(ToastKind::Success, format!("{}: done", job.title));
                } else {
                    self.toast(ToastKind::Error, format!("{} failed: {}", job.title, job.error.clone().unwrap_or_else(|| "see the log".into())));
                }
                self.spawn_load(Reload::Kernels);
                self.spawn_load(Reload::Leftovers);
                if self.exec.root_ready() {
                    self.actions.push(Action::RootOp { title: "Read boot configuration".into(), op: Op::BootScan, kind: JobKind::BootScan, reload: vec![] });
                }
            }
            JobKind::Remount => {
                self.invalidate_mounts();
                if ok {
                    self.toast(ToastKind::Success, format!("{}: done", job.title));
                    // What /boot holds (kernels, boot menu) may only be visible now.
                    self.spawn_load(Reload::Kernels);
                    if self.exec.root_ready() {
                        self.actions.push(Action::RootOp { title: "Read boot configuration".into(), op: Op::BootScan, kind: JobKind::BootScan, reload: vec![] });
                    }
                } else {
                    self.toast(ToastKind::Error, format!("{} failed: {}", job.title, job.error.clone().unwrap_or_else(|| job.output().lines().last().unwrap_or("").to_string())));
                }
            }
            JobKind::ChangeList => {
                if let Some(text) = job.lines.iter().map(|l| l.1.as_str()).find(|l| l.starts_with('['))
                    && let Ok(list) = serde_json::from_str::<Vec<TxInfo>>(text)
                {
                    self.recent_changes = Some(list);
                } else if let Some(e) = &job.error {
                    self.toast(ToastKind::Error, format!("Could not read the recent changes: {e}"));
                }
            }
            JobKind::Undo => {
                if ok {
                    self.toast(ToastKind::Success, format!("{}: done", job.title));
                    self.last_change = None;
                } else {
                    let why = job.error.clone().unwrap_or_else(|| "see the log".into());
                    self.toast(ToastKind::Error, format!("{} failed: {}", job.title, explain_write_error(&why)));
                }
                self.recent_changes = None;
                if self.exec.root_ready() {
                    self.list_changes();
                }
            }
            JobKind::CleanupScan { tag } => {
                if job.error.is_none() {
                    let out = job.output();
                    let code = job.exit.flatten();
                    let ts = crate::fmt::now_ts();
                    let p = cleanup_cache_path(tag);
                    if let Some(d) = p.parent() {
                        let _ = std::fs::create_dir_all(d);
                    }
                    let _ = std::fs::write(&p, format!("{ts} {}\n{out}", code.map(|c| c.to_string()).unwrap_or_else(|| "-".into())));
                    self.cleanup_scans.insert(tag.clone(), (out, code, ts));
                } else {
                    self.toast(ToastKind::Error, format!("{} failed: {}", job.title, job.error.clone().unwrap_or_default()));
                }
            }
            JobKind::WorldCheck => {
                if self.world_job == Some(job.id) {
                    self.world_job = None;
                }
                if job.error.is_none() && job.exit.flatten().is_some() {
                    let out = job.output();
                    let code = job.exit.flatten();
                    let ts = crate::fmt::now_ts();
                    self.world_plan = Some((Arc::new(pretend::parse(&out, code)), ts));
                    save_world_cache(ts, code, &out);
                }
            }
            JobKind::Capture { tag } => {
                let c = self.captures.entry(tag.clone()).or_default();
                c.output = job.output();
                c.code = job.exit.flatten();
                c.done = true;
            }
            JobKind::Generic => {
                if ok {
                    self.toast(ToastKind::Success, format!("{}: done", job.title));
                } else {
                    let tail = job.lines.iter().rev().take(3).map(|l| l.1.clone()).collect::<Vec<_>>();
                    let reason = job.error.clone().unwrap_or_else(|| tail.into_iter().rev().collect::<Vec<_>>().join(" "));
                    self.toast(ToastKind::Error, format!("{} failed: {}", job.title, reason));
                }
            }
        }
    }

    fn advance_batch(&mut self, bi: usize, outcome: Result<(), String>) {
        let step = self.batches[bi].run.advance(outcome);
        self.run_step(bi, step);
    }

    fn run_step(&mut self, bi: usize, step: batch::Step) {
        match step {
            batch::Step::Send { op, tx } => {
                let id = self.exec.root_op_tx(op, tx);
                self.batches[bi].current = Some(id);
            }
            batch::Step::Finished(finish) => self.finish_batch(bi, finish),
        }
    }

    fn finish_batch(&mut self, bi: usize, finish: batch::Finish) {
        use batch::Finish;
        let b = self.batches.remove(bi);
        let mut reload = b.reload;
        let mut ok = false;
        match finish {
            Finish::Saved { tx, warning } => {
                ok = true;
                self.toast(ToastKind::Success, format!("{} — saved", b.title));
                if let Some(w) = warning {
                    self.toast(ToastKind::Warn, w);
                }
                if let Some(tx) = tx {
                    self.last_change = Some(LastChange { tx, title: b.title.clone() });
                    self.recent_changes = None;
                }
            }
            Finish::RolledBack { reason } => {
                self.toast(ToastKind::Error, format!("{} failed: {}. Nothing was changed: the steps that had run were rolled back.", b.title, explain_write_error(&reason)));
                reload.extend(stale_file_reloads(&reason));
            }
            Finish::Failed { reason, rollback_error, tx } => {
                self.toast(ToastKind::Error, format!("{} failed: {}", b.title, explain_write_error(&reason)));
                if let Some(e) = rollback_error {
                    self.toast(ToastKind::Error, format!("Rolling the earlier steps back failed too: {e}. Check /etc/portage before changing anything else."));
                }
                if let Some(tx) = tx {
                    self.last_change = Some(LastChange { tx, title: b.title.clone() });
                    self.recent_changes = None;
                }
                reload.extend(stale_file_reloads(&reason));
            }
        }
        self.config_view.updates = None; // the pending ._cfg list may have changed: scan again when it is shown
        for r in reload {
            self.spawn_load(r);
        }
        if ok && let Some(then) = b.then {
            self.actions.push(*then);
        }
    }

    // ------------------------------------------------------------------
    // Actions

    fn process_actions(&mut self) {
        let actions = std::mem::take(&mut self.actions);
        for a in actions {
            self.apply(a);
        }
    }

    fn apply(&mut self, a: Action) {
        match a {
            Action::Open(key) => {
                self.request_detail(&key);
                self.detail.open(&key);
                if self.tab != Tab::Library {
                    self.tab = Tab::Store;
                    self.store.show_detail = true;
                } else {
                    self.library.selected = Some(key);
                }
            }
            Action::Tab(t) => self.tab = t,
            Action::Search(q) => {
                self.tab = Tab::Store;
                self.store.query = q;
                self.store.show_detail = false;
                self.store.page = views::store::Page::Search;
            }
            Action::Pretend(opts) => {
                if let Some(problem) = opts.policy_error() {
                    self.toast(ToastKind::Error, problem);
                    return;
                }
                let mut env = opts.env.clone();
                env.retain(|(_, v)| !v.is_empty());
                let mut argv = vec!["emerge".to_string()];
                argv.extend(opts.pretend_args());
                let privilege = if self.settings.pretend_as_root { Priv::Root } else { Priv::User };
                let title = format!("Preview: {}", opts.command_line());
                let id = self.start_job(title, argv, env, privilege, JobKind::Pretend { opts: opts.clone() }, vec![]);
                self.emerge.pretend_job = Some(id);
                self.emerge.boot_override = false;
                self.emerge.plan = None;
                self.emerge.plan_opts = None;
                self.emerge.options = opts;
                self.emerge.page = views::emerge::Page::Review;
                self.tab = Tab::Emerge;
            }
            Action::RePretend => {
                if let Some(o) = self.emerge.plan_opts.clone().or_else(|| Some(self.emerge.options.clone())) {
                    self.apply(Action::Pretend(o));
                }
            }
            Action::Emerge(opts, planned) => {
                if let Some(problem) = opts.policy_error() {
                    self.toast(ToastKind::Error, problem);
                    return;
                }
                if self.running_emerge().is_some() {
                    self.toast(ToastKind::Warn, "Another emerge is already running. Wait for it to finish.");
                    return;
                }
                let touches_boot = planned.iter().any(|(cpv, _, _)| {
                    crate::portage::atom::split_cpv(cpv).is_some_and(|(c, p, _)| crate::portage::mounts::touches_boot(&format!("{c}/{p}")))
                });
                if touches_boot && self.settings.boot_auto_remount {
                    self.invalidate_mounts();
                    let blocked: Vec<crate::portage::mounts::BootMount> = self.boot_mounts().into_iter().filter(|m| m.blocks_writes()).collect();
                    if !blocked.is_empty() {
                        for m in &blocked {
                            if m.fstab_ro && !self.remount_restore.contains(&m.target) {
                                self.remount_restore.push(m.target.clone());
                            }
                        }
                        let ops = blocked.iter().map(|m| Op::Remount { target: m.target.clone(), rw: true }).collect();
                        self.toast(ToastKind::Info, format!("Making {} writable for the kernel/bootloader update…", blocked.iter().map(|m| m.target.as_str()).collect::<Vec<_>>().join(", ")));
                        self.apply(Action::Batch {
                            title: "Prepare boot partitions".into(),
                            ops,
                            reload: vec![],
                            then: Some(Box::new(Action::Emerge(opts, planned))),
                        });
                        self.invalidate_mounts();
                        return;
                    }
                }
                let history = self.data.history.clone().unwrap_or_default();
                let tracker = Box::new(Tracker::new(&planned, &history));
                let mut argv = vec!["emerge".to_string()];
                argv.extend(opts.run_args());
                let mut env = opts.env.clone();
                env.retain(|(_, v)| !v.is_empty());
                let title = format!("emerge {}", opts.args().join(" "));
                let kind = if opts.action == crate::portage::emerge::Action::Sync { JobKind::Sync } else { JobKind::Emerge { opts: opts.clone(), tracker } };
                let id = self.start_job(title, argv, env, Priv::Root, kind, vec![]);
                self.emerge.active_job = Some(id);
                self.emerge.page = views::emerge::Page::Activity;
                self.tab = Tab::Emerge;
            }
            Action::Root { title, argv, reload, kind } => {
                let id = self.start_job(title, argv, vec![], Priv::Root, kind, reload);
                self.emerge.selected_job = Some(id);
            }
            Action::Capture { tag, argv, privilege } => {
                let id = self.start_job(format!("{}", argv.join(" ")), argv, vec![], privilege, JobKind::Capture { tag: tag.clone() }, vec![]);
                self.captures.insert(tag, Captured { job: id, ..Default::default() });
            }
            Action::Batch { title, ops, reload, then } => {
                if ops.is_empty() {
                    return;
                }
                // Changes that must land together run in a helper transaction (see batch.rs);
                // a helper of another version may not know them.
                let mut run = batch::Run::new(&title, ops, !self.exec.helper_outdated);
                let first = run.first();
                self.batches.push(Batch { title, run, current: None, reload, then });
                let bi = self.batches.len() - 1;
                self.run_step(bi, first);
            }
            Action::Write { title, changes, reload, then } => {
                if changes.is_empty() {
                    self.toast(ToastKind::Info, "Nothing to change.");
                    return;
                }
                if self.settings.review_writes {
                    self.review = Some(ReviewDialog::new(title, changes, reload, then));
                } else {
                    self.apply_changes(title, changes, reload, then);
                }
            }
            Action::ReviewOps { title, changes, unreadable, notes, ops, reload } => {
                let mut dialog = ReviewDialog::new(title, changes, reload, None);
                dialog.ops = Some(ops);
                dialog.unreadable = unreadable;
                dialog.notes = notes;
                self.review = Some(dialog);
            }
            Action::Reload(r) => self.spawn_load(r),
            Action::Toast(k, t) => self.toast(k, t),
            Action::Cancel(id) => {
                if let Some(j) = self.jobs.iter().find(|j| j.id == id) {
                    let p = j.privilege;
                    self.exec.cancel(id, p, libc::SIGINT);
                    self.toast(ToastKind::Info, crate::stop::Stage::Interrupted.describe());
                }
            }
            Action::ForceStop(id) => self.stop_job(id),
            Action::StartHelper => {
                if !self.exec.root_ready() && self.exec.helper_state != HelperState::Starting {
                    self.exec.start_helper();
                }
            }
            Action::Job { title, argv, kind } => {
                self.start_job(title, argv, vec![], Priv::User, kind, vec![]);
            }
            Action::RootOp { title, op, kind, reload } => {
                let needs_new_helper = op.min_helper() > self.exec.helper_version;
                let id = self.exec.alloc_id();
                let label = format!("{op:?}").split([' ', '{']).next().unwrap_or("").to_string();
                self.jobs.push(Job {
                    id,
                    title,
                    kind,
                    privilege: Priv::Root,
                    argv: vec![format!("helper: {label}")],
                    lines: Arc::default(),
            dropped_lines: 0,
                    partial: None,
                    started: Instant::now(),
                    ended: None,
                    exit: None,
                    error: None,
                    reload,
                    stop: None,
                });
                if needs_new_helper && !self.exec.is_root && self.exec.helper_state == HelperState::Ready {
                    let ji = self.jobs.len() - 1;
                    self.jobs[ji].error = Some("the installed admin helper is too old for this action — run sudo dist/install.sh, then restart PortageHub".into());
                    self.jobs[ji].ended = Some(Instant::now());
                    self.finish_job(ji);
                } else {
                    self.exec.root_op_with_id(id, op);
                }
            }
            Action::CheckWorld => {
                if self.world_job.is_some_and(|id| self.job(id).is_some_and(|j| j.running())) {
                    return;
                }
                let opts = (crate::portage::emerge::recipes()[0].build)();
                // Low priority: it only reads, and can take a while on big systems.
                let mut argv = vec!["nice".to_string(), "-n".into(), "19".into(), "emerge".into()];
                argv.extend(opts.pretend_args());
                let id = self.start_job("Checking @world updates (emerge --pretend)".into(), argv, vec![], Priv::User, JobKind::WorldCheck, vec![]);
                self.world_job = Some(id);
            }
            Action::LoadDetail(k) => self.request_detail(&k),
            Action::LoadEbuild(p) => {
                if !self.ebuilds.contains_key(&p) && self.ebuild_loading.insert(p.clone()) {
                    let tx = self.data_tx.clone();
                    let ctx = self.ctx.clone();
                    std::thread::spawn(move || {
                        let t = std::fs::read_to_string(&p).unwrap_or_else(|e| format!("# cannot read {}: {e}", p.display()));
                        let _ = tx.send(DataMsg::Ebuild(p, Arc::new(t)));
                        ctx.request_repaint();
                    });
                }
            }
        }
    }

    pub fn apply_changes(&mut self, title: String, changes: Vec<FileChange>, reload: Vec<Reload>, then: Option<Box<Action>>) {
        let backup = self.settings.backup_on_write;
        let ops = changes
            .into_iter()
            .map(|c| {
                // The helper refuses the write when the file is no longer what the GUI read, so a
                // change made outside PortageHub since then is never silently overwritten.
                let expect = Some(Expect::of(&c.old));
                match c.new {
                    Some(content) => Op::WriteFile { path: c.path.to_string_lossy().to_string(), content, backup, expect },
                    None => Op::DeleteFile { path: c.path.to_string_lossy().to_string(), expect },
                }
            })
            .collect();
        self.apply(Action::Batch { title, ops, reload, then });
    }

    /// Queue the review and write of the changes an edit produced, or tell the user why it
    /// produced none (a file PortageHub will not edit, for example).
    pub fn push_write(&mut self, title: impl Into<String>, changes: Result<Vec<FileChange>, String>, reload: Vec<Reload>, then: Option<Box<Action>>) {
        match changes {
            Ok(changes) => self.push(Action::Write { title: title.into(), changes, reload, then }),
            Err(why) => self.toast(ToastKind::Error, why),
        }
    }

    /// Ask the helper for its journal (needs the admin session: the journal is root-only).
    pub fn list_changes(&mut self) {
        self.push(Action::RootOp { title: "Read recent changes".into(), op: Op::ListTx, kind: JobKind::ChangeList, reload: vec![] });
    }

    /// Undo a recorded change. Without `force` the helper refuses when a file was changed since.
    pub fn undo_change(&mut self, tx: &str, title: &str, force: bool) {
        self.push(Action::RootOp {
            title: format!("Undo: {title}"),
            op: Op::Rollback { tx: tx.to_string(), force },
            kind: JobKind::Undo,
            reload: vec![Reload::Config, Reload::MakeConf, Reload::Repos, Reload::Kernels],
        });
    }

    /// Boot partitions as currently mounted (refreshed at most every 2 s).
    pub fn boot_mounts(&mut self) -> Vec<crate::portage::mounts::BootMount> {
        if self.boot_mounts_cache.0.elapsed() > Duration::from_secs(2) {
            self.boot_mounts_cache = (Instant::now(), crate::portage::mounts::load());
        }
        self.boot_mounts_cache.1.clone()
    }

    pub fn invalidate_mounts(&mut self) {
        self.boot_mounts_cache.0 = Instant::now() - Duration::from_secs(60);
    }

    /// Remount a boot partition through the helper; `restore` marks it to go
    /// back to read-only after the next emerge.
    pub fn remount_boot(&mut self, target: &str, rw: bool, restore: bool) {
        if rw && restore && !self.remount_restore.iter().any(|t| t == target) {
            self.remount_restore.push(target.to_string());
        }
        if !rw {
            self.remount_restore.retain(|t| t != target);
        }
        self.apply(Action::RootOp {
            title: format!("Make {target} {}", if rw { "writable" } else { "read-only" }),
            op: Op::Remount { target: target.to_string(), rw },
            kind: JobKind::Remount,
            reload: vec![],
        });
    }

    /// Toast the result of the boot check after a kernel-related change.
    fn report_boot_check(&mut self) {
        self.kernel_attention = false;
        let Some(ks) = self.data.kernels.clone() else { return };
        let scan = self.boot_scan.as_ref().map(|(s, _)| s.clone());
        let findings = crate::portage::kernel::check(&ks, scan.as_deref(), self.settings.kernel_protect, 2, self.settings.nvidia_alerts);
        use crate::portage::kernel::Severity;
        let bad: Vec<_> = findings.iter().filter(|f| f.severity >= Severity::Warn).collect();
        if bad.is_empty() {
            self.toast(ToastKind::Success, "Boot check after the kernel/driver update: everything is in place.");
        } else {
            for f in bad.iter().take(3) {
                self.toast(if f.severity == Severity::Error { ToastKind::Error } else { ToastKind::Warn }, format!("{} — open Maintenance → Kernels & Boot", f.title));
            }
        }
    }

    /// Record the running kernel as known-good and protect it (and the previous one) from depclean.
    pub fn mark_kernel_good(&mut self, kver: String) {
        let Some(ks) = self.data.kernels.clone() else { return };
        if kver != ks.running {
            self.toast(ToastKind::Warn, format!("Only the kernel you are running can be marked known-good — boot {kver} first."));
            return;
        }
        if self.settings.kernel_mark_requires_nvidia && ks.nvidia.gpu && ks.nvidia.drivers.is_some() {
            // A wedged NVIDIA driver can make nvidia-smi hang: ask on a thread, with a limit, and go on when it answers.
            if self.nvidia_probing.insert(kver.clone()) {
                self.toast(ToastKind::Info, "Checking the NVIDIA driver…");
                let (tx, ctx) = (self.data_tx.clone(), self.ctx.clone());
                std::thread::spawn(move || {
                    let mut cmd = std::process::Command::new("nvidia-smi");
                    cmd.arg("-L");
                    let (ok, detail) = match crate::util::run_with_timeout(cmd, Duration::from_secs(10)) {
                        Ok(out) if out.status.success() => (true, String::new()),
                        Ok(out) => (false, String::from_utf8_lossy(&out.stderr).lines().next().unwrap_or("it reported an error").to_string()),
                        Err(e) => (false, format!("nvidia-smi {e}")),
                    };
                    let _ = tx.send(DataMsg::NvidiaProbe { kver, ok, detail });
                    ctx.request_repaint();
                });
            }
            return;
        }
        self.mark_kernel_good_after_probe(kver);
    }

    fn mark_kernel_good_after_probe(&mut self, kver: String) {
        let Some(ks) = self.data.kernels.clone() else { return };
        if (Op::MarkGood { kver: String::new(), state_dir: String::new(), backup: false }).min_helper() > self.exec.helper_version && !self.exec.is_root {
            self.toast(ToastKind::Error, "The installed admin helper is too old for this — run sudo dist/install.sh, then restart PortageHub.");
            return;
        }
        let new_prev = match &ks.last_good {
            Some(l) if *l != kver => Some(l.clone()),
            _ => ks.previous_good.clone(),
        };
        let mut ops = vec![Op::MarkGood {
            kver: kver.clone(),
            state_dir: ks.state_dir.to_string_lossy().to_string(),
            backup: self.settings.kernel_backup_on_mark,
        }];
        if self.settings.kernel_protect {
            let atoms = crate::portage::kernel::wanted_keep_atoms(&ks, Some(&kver), new_prev.as_deref());
            let mut content = String::from("# Proven-good kernels protected from --depclean. Managed by PortageHub (same format as kernel-mark-good).\n");
            for a in &atoms {
                content.push_str(a);
                content.push('\n');
            }
            ops.push(Op::WriteFile { path: format!("/etc/portage/sets/{}", ks.keep_set), content, backup: false, expect: None });
            if !ks.keep_set_in_world {
                ops.push(Op::Run { argv: vec!["emerge".into(), "--noreplace".into(), "--ask=n".into(), format!("@{}", ks.keep_set)], env: vec![], cwd: None });
            }
        }
        self.apply(Action::Batch {
            title: format!("Mark {kver} as known-good"),
            ops,
            reload: vec![Reload::Kernels, Reload::Config, Reload::Vdb],
            then: None,
        });
    }

    /// PORTAGE_TMPDIR for a package, honouring package.env overrides (e.g. a "notmpfs" env file).
    pub fn tmpdir_for(&self, cpv: &str) -> PathBuf {
        use crate::portage::config::ConfKind;
        let mut dir = self.data.env.as_ref().map(|e| e.get("PORTAGE_TMPDIR").to_string()).filter(|s| !s.is_empty()).unwrap_or_else(|| "/var/tmp".into());
        if let (Some(cfg), Some((c, p, v))) = (self.data.config.as_ref(), crate::portage::atom::split_cpv(cpv))
            && let Some(set) = cfg.set(ConfKind::Env)
        {
            for f in set.files.iter().filter(|f| !f.ignored) {
                for (_, e) in f.entries() {
                    if e.disabled || !e.atom.as_ref().is_some_and(|a| a.matches(&c, &p, &v, None, None)) {
                        continue;
                    }
                    for name in &e.values {
                        if let Some(envf) = cfg.env_files.iter().find(|x| x.name == *name) {
                            for (k, val, _) in makeconf::scan(&envf.content) {
                                if k == "PORTAGE_TMPDIR" && !val.is_empty() {
                                    dir = val;
                                }
                            }
                        }
                    }
                }
            }
        }
        PathBuf::from(dir)
    }

    /// Tail build logs of running packages to show their phase.
    fn poll_build_logs(&mut self) {
        if self.last_log_poll.elapsed() < Duration::from_millis(1500) {
            return;
        }
        self.last_log_poll = Instant::now();
        // Resolve each active package's build dir first (PORTAGE_TMPDIR can differ per package).
        let mut dirs: HashMap<String, PathBuf> = HashMap::new();
        for j in self.jobs.iter().filter(|j| j.running()) {
            if let JobKind::Emerge { tracker, .. } = &j.kind {
                for i in tracker.items.iter().filter(|i| i.stage.active()) {
                    dirs.insert(i.cpv.clone(), self.tmpdir_for(&i.cpv));
                }
            }
        }
        // Portage makes <PORTAGE_TMPDIR>/portage 0700 portage:portage: other users cannot even look in.
        self.build_logs_private = dirs.values().any(|d| matches!(std::fs::read_dir(d.join("portage")), Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied));
        for j in self.jobs.iter_mut().filter(|j| j.running()) {
            if let JobKind::Emerge { tracker, .. } = &mut j.kind {
                let active: Vec<(usize, PathBuf)> = tracker
                    .items
                    .iter()
                    .enumerate()
                    .filter(|(_, i)| i.stage.active())
                    .filter_map(|(n, i)| dirs.get(&i.cpv).and_then(|d| i.build_dir_log(d)).map(|p| (n, p)))
                    .collect();
                for (n, p) in active {
                    if let Some(t) = progress::tail(&p, 16 * 1024) {
                        tracker.set_phase_from_log(n, &t);
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // Shell

    fn nav_bar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.add_space(6.0);
            let (rect, _) = ui.allocate_exact_size(egui::vec2(30.0, 30.0), egui::Sense::hover());
            ui.painter().circle_filled(rect.center(), 14.0, Color32::from_rgb(0x2a, 0x47, 0x5e));
            ui.painter().text(rect.center(), egui::Align2::CENTER_CENTER, icon::CUBE, egui::FontId::proportional(18.0), ACCENT);
            ui.label(RichText::new("PORTAGE").size(17.0).family(theme::bold()).color(WHITE));
            ui.label(RichText::new("HUB").size(17.0).family(theme::light()).color(ACCENT));
            ui.add_space(24.0);
            for t in Tab::MAIN {
                let mut label = t.label().to_string();
                if t == Tab::Emerge && self.running_emerge().is_some() {
                    label = format!("{} {}", t.label(), icon::CIRCLE_NOTCH);
                }
                if w::nav_tab(ui, &label, self.tab == t).clicked() {
                    if self.tab == t && t == Tab::Store {
                        self.store.show_detail = false;
                    }
                    self.tab = t;
                }
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.add_space(8.0);
                if w::icon_button(ui, icon::GEAR_SIX, "Settings").clicked() {
                    self.tab = Tab::Settings;
                }
                ui.add_space(4.0);
                self.root_pill(ui);
                ui.add_space(8.0);
                if let Some(env) = &self.data.env
                    && !env.portage_version.is_empty()
                {
                    ui.label(RichText::new(format!("Portage {}", env.portage_version)).size(12.0).color(TEXT_FAINT));
                }
            });
        });
    }

    fn root_pill(&mut self, ui: &mut egui::Ui) {
        let (txt, col, tip) = if self.exec.is_root {
            (format!("{} root", icon::SHIELD_CHECK), GREEN, "PortageHub itself runs as root.".to_string())
        } else {
            match &self.exec.helper_state {
                HelperState::Ready => (format!("{} Admin session", icon::LOCK_OPEN), GREEN, "Privileged actions run through the helper without asking again.".into()),
                HelperState::Starting => (format!("{} Authenticating…", icon::HOURGLASS), YELLOW, "Waiting for the polkit password dialog.".into()),
                HelperState::Failed(e) => (format!("{} Not authorized", icon::LOCK), RED, format!("Last attempt failed: {e}\nClick to retry.")),
                HelperState::NotStarted => (format!("{} User mode", icon::LOCK), TEXT_DIM, "Browsing works without root. Click to unlock privileged actions (one password prompt per session).".into()),
            }
        };
        let r = ui.add(egui::Button::new(RichText::new(txt).color(col).size(13.0)).fill(Color32::from_rgb(0x22, 0x28, 0x33)).corner_radius(CornerRadius::same(12))).on_hover_text(tip);
        if r.clicked() {
            self.push(Action::StartHelper);
        }
    }

    fn status_bar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.set_min_height(30.0);
            let busy: Vec<&str> = [
                (Reload::Repos, "repositories"),
                (Reload::Catalog, "package tree"),
                (Reload::Vdb, "installed packages"),
                (Reload::History, "history"),
                (Reload::Updates, "update check"),
            ]
            .iter()
            .filter(|(r, _)| self.loading.contains(r))
            .map(|(_, n)| *n)
            .collect();
            if !busy.is_empty() {
                ui.add(egui::Spinner::new().size(14.0).color(ACCENT));
                ui.label(w::dim(format!("Loading {}…", busy.join(", "))));
            } else if let Some(c) = &self.data.catalog {
                ui.label(w::dim(format!(
                    "{} {} packages · {} ebuilds in {} repos",
                    icon::DATABASE,
                    c.pkgs.len(),
                    c.ebuild_count,
                    c.repo_locations.len()
                )));
                if let Some(v) = &self.data.vdb {
                    ui.label(w::dim(format!("· {} installed", v.pkgs.len())));
                }
                if let Some(ts) = self.data.last_sync() {
                    ui.label(w::dim(format!("· synced {}", crate::fmt::ago(ts))));
                }
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.add_space(6.0);
                let running = self.running_count();
                if running > 0 {
                    ui.label(RichText::new(format!("{} {} active", icon::CIRCLE_NOTCH, running)).color(ACCENT));
                }
                let emerge = self.running_emerge().map(|j| match &j.kind {
                    JobKind::Emerge { tracker, .. } => Some((tracker.fraction(), tracker.counts(), tracker.total, tracker.remaining(), tracker.elapsed(), tracker.last_status.clone())),
                    _ => None,
                });
                if let Some(Some((frac, (done, _failed, _active), total, rem, el, status))) = emerge {
                    let resp = ui
                        .allocate_ui(egui::vec2(380.0, 22.0), |ui| {
                            let text = format!("{done}/{total} · {} elapsed · ~{} left", history::fmt_duration(el), history::fmt_duration(rem));
                            w::progress(ui, frac, 18.0, &text, true)
                        })
                        .inner;
                    if resp.interact(egui::Sense::click()).on_hover_text(status).clicked() {
                        self.tab = Tab::Emerge;
                        self.emerge.page = views::emerge::Page::Activity;
                    }
                    ui.label(RichText::new("EMERGING").size(11.0).family(theme::bold()).color(ACCENT));
                }
            });
        });
    }

    fn toasts_ui(&mut self, ctx: &egui::Context) {
        self.toasts.retain(|t| t.created.elapsed() < Duration::from_secs(if t.kind == ToastKind::Error { 12 } else { 6 }));
        if self.toasts.is_empty() {
            return;
        }
        ctx.request_repaint_after(Duration::from_millis(250));
        let screen = ctx.content_rect();
        egui::Area::new(egui::Id::new("toasts"))
            .order(egui::Order::Foreground)
            .fixed_pos(egui::pos2(screen.right() - 400.0, screen.bottom() - 60.0 - 70.0 * self.toasts.len().min(5) as f32))
            .show(ctx, |ui| {
                ui.set_width(380.0);
                let mut close = None;
                for (i, t) in self.toasts.iter().enumerate().rev().take(5) {
                    let (col, ic) = match t.kind {
                        ToastKind::Info => (ACCENT, icon::INFO),
                        ToastKind::Success => (GREEN, icon::CHECK_CIRCLE),
                        ToastKind::Warn => (ORANGE, icon::WARNING),
                        ToastKind::Error => (RED, icon::X_CIRCLE),
                    };
                    Frame::new()
                        .fill(Color32::from_rgb(0x23, 0x31, 0x44))
                        .stroke(Stroke::new(1.0, col.gamma_multiply(0.6)))
                        .corner_radius(CornerRadius::same(4))
                        .inner_margin(Margin::same(10))
                        .shadow(egui::epaint::Shadow { offset: [0, 4], blur: 12, spread: 0, color: Color32::from_black_alpha(120) })
                        .show(ui, |ui| {
                            ui.set_width(360.0);
                            ui.horizontal_top(|ui| {
                                ui.label(RichText::new(ic).size(18.0).color(col));
                                ui.allocate_ui(egui::vec2(300.0, 0.0), |ui| {
                                    ui.add(egui::Label::new(RichText::new(&t.text).color(TEXT)).wrap());
                                });
                                if ui.add(egui::Button::new(RichText::new(icon::X).color(TEXT_DIM)).frame(false)).clicked() {
                                    close = Some(i);
                                }
                            });
                        });
                    ui.add_space(6.0);
                }
                if let Some(i) = close {
                    self.toasts.remove(i);
                }
            });
    }

    /// Ask before quitting while an emerge is running.
    fn close_guard(&mut self, ctx: &egui::Context) {
        if ctx.input(|i| i.viewport().close_requested()) && !self.allow_close && self.running_emerge().is_some() {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            self.confirm_close = true;
        }
        if !self.confirm_close {
            return;
        }
        let in_process = self.exec.is_root;
        let emerge = self.running_emerge().map(|j| (j.id, j.privilege));
        egui::Modal::new(egui::Id::new("close-guard"))
            .frame(Frame::new().fill(PANEL).corner_radius(CornerRadius::same(6)).inner_margin(Margin::same(18)))
            .show(ctx, |ui| {
                ui.set_width(480.0);
                ui.horizontal(|ui| {
                    ui.label(RichText::new(icon::WARNING).size(22.0).color(ORANGE));
                    w::title(ui, "An emerge is still running");
                });
                ui.add(egui::Label::new(RichText::new(if in_process {
                    "PortageHub runs as root, so quitting now stops the emerge. You can continue it later with “Resume interrupted emerge”."
                } else {
                    "If you quit, the emerge keeps running in the background (the admin helper stays alive until it finishes), but you won't see its progress here. emerge.log and History will still record it."
                }).color(TEXT)).wrap());
                ui.add_space(10.0);
                ui.horizontal(|ui| {
                    if w::primary(ui, icon::ARROW_LEFT, "Keep PortageHub open").clicked() {
                        self.confirm_close = false;
                    }
                    if !in_process && let Some((id, privilege)) = emerge && w::button(ui, icon::STOP, "Stop emerge and quit").on_hover_text("Sends SIGINT (like Ctrl+C) and quits. Continue later with “Resume interrupted emerge”.").clicked() {
                        // Otherwise the emerge would go on without a window to watch it, held by a helper nobody can address any more.
                        self.exec.cancel(id, privilege, libc::SIGINT);
                        self.allow_close = true;
                        self.confirm_close = false;
                        ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                    if w::danger(ui, icon::SIGN_OUT, "Quit anyway").clicked() {
                        self.allow_close = true;
                        self.confirm_close = false;
                        ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                });
            });
    }

    fn review_ui(&mut self, ctx: &egui::Context) {
        let Some(review) = &mut self.review else { return };
        let mut apply = false;
        let mut cancel = false;
        egui::Modal::new(egui::Id::new("review-dialog"))
            .frame(Frame::new().fill(PANEL).corner_radius(CornerRadius::same(6)).inner_margin(Margin::same(18)))
            .show(ctx, |ui| {
                ui.set_width(760.0f32.min(ctx.content_rect().width() - 80.0));
                ui.horizontal(|ui| {
                    ui.label(RichText::new(icon::FLOPPY_DISK).size(22.0).color(ACCENT));
                    w::title(ui, "Review changes");
                });
                w::subtitle(ui, &review.title);
                ui.add_space(6.0);
                for note in &review.notes {
                    w::callout(ui, ORANGE, icon::INFO, note);
                    ui.add_space(4.0);
                }
                egui::ScrollArea::vertical().max_height(ctx.content_rect().height() * 0.6).show(ui, |ui| {
                    for path in &review.unreadable {
                        ui.label(RichText::new(format!("{} {} — cannot be previewed (root-only file)", icon::LOCK, path.display())).color(TEXT_DIM));
                    }
                    for (i, c) in review.changes.iter().enumerate() {
                        let header = match &c.new {
                            None => format!("{} delete {}", icon::TRASH, c.path.display()),
                            Some(_) if c.old.is_empty() => format!("{} create {}", icon::FILE_PLUS, c.path.display()),
                            Some(_) => format!("{} modify {}", icon::PENCIL_SIMPLE, c.path.display()),
                        };
                        let open = review.expanded == i;
                        if ui.selectable_label(open, RichText::new(header).color(WHITE)).clicked() {
                            review.expanded = i;
                        }
                        for warning in review.warnings.get(i).into_iter().flatten() {
                            w::callout(ui, RED, icon::WARNING, &format!("Runs commands as root — {}: {}", warning.why, warning.line));
                        }
                        if open {
                            Frame::new().fill(BG_DEEP).inner_margin(Margin::same(8)).corner_radius(CornerRadius::same(3)).show(ui, |ui| {
                                ui.set_width(ui.available_width());
                                let new = c.new.clone().unwrap_or_default();
                                for d in crate::fmt::line_diff(&c.old, &new) {
                                    let (prefix, col) = match d.0 {
                                        '+' => ("+ ", GREEN),
                                        '-' => ("- ", RED),
                                        _ => ("  ", TEXT_DIM),
                                    };
                                    ui.label(RichText::new(format!("{prefix}{}", d.1)).monospace().color(col));
                                }
                            });
                        }
                    }
                });
                ui.add_space(10.0);
                ui.horizontal(|ui| {
                    if w::big_button(ui, &format!("{}  APPLY", icon::CHECK), GRAD_GREEN, 140.0).clicked() {
                        apply = true;
                    }
                    if w::button(ui, "", "Cancel").clicked() {
                        cancel = true;
                    }
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        ui.label(w::dim(if self.exec.root_ready() { "Runs as root through the admin session" } else { "You will be asked for your password once" }));
                    });
                });
            });
        if apply {
            let r = self.review.take().unwrap();
            match r.ops {
                Some(ops) => self.apply(Action::Batch { title: r.title, ops, reload: r.reload, then: r.then }),
                None => self.apply_changes(r.title, r.changes, r.reload, r.then),
            }
        } else if cancel {
            self.review = None;
        }
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
        self.escalate_stops();
        self.save_settings_if_changed();
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
        if !self.size_checked
            && let Some(monitor) = ctx.input(|i| i.viewport().monitor_size)
        {
            // A 1366x768 laptop panel is smaller than the 1380x860 the window asks for.
            self.size_checked = true;
            let (w, h) = crate::window::fit(crate::window::WANTED, (monitor.x, monitor.y));
            if (w, h) != crate::window::WANTED {
                ctx.send_viewport_cmd(egui::ViewportCommand::InnerSize(egui::vec2(w, h)));
            }
        }
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
        egui::Panel::top("nav")
            .frame(Frame::new().fill(NAV).inner_margin(Margin::symmetric(8, 6)))
            .show_separator_line(false)
            .show(ui, |ui| self.nav_bar(ui));
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_failed_load_stops_waiting_and_is_not_redone_by_itself() {
        let mut loading: HashSet<Reload> = [Reload::Config, Reload::Vdb].into();
        let mut pending: HashSet<Reload> = [Reload::Config].into();
        forget_load(&mut loading, &mut pending, Reload::Config);
        assert_eq!(loading, [Reload::Vdb].into());
        assert!(pending.is_empty(), "a queued reload of a loader that just panicked would panic again at once");
        // Unrelated loads are untouched.
        forget_load(&mut loading, &mut pending, Reload::Kernels);
        assert_eq!(loading, [Reload::Vdb].into());
    }

    #[test]
    fn a_page_that_cannot_be_built_is_empty_not_endless() {
        let d = empty_detail("app-misc/foo");
        assert_eq!((d.cat.as_str(), d.pn.as_str(), d.key.as_str()), ("app-misc", "foo", "app-misc/foo"));
        assert!(d.versions.is_empty());
    }

    #[test]
    fn write_errors_are_explained() {
        let e = explain_write_error("changed on disk: /etc/portage/package.use/zz");
        assert!(e.contains("/etc/portage/package.use/zz was changed outside PortageHub"), "{e}");
        assert_eq!(explain_write_error("disk full"), "disk full");
        assert_eq!(stale_file_reloads("changed on disk: /x"), vec![Reload::Config, Reload::MakeConf, Reload::Repos]);
        assert!(stale_file_reloads("disk full").is_empty());
    }
}
