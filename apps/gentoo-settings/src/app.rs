//! Application state and the window chrome: navigation, the pending-changes
//! bar, the review dialog, the progress dialog and toasts.

use crate::exec::{self, ExecMsg};
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use crate::views;
use egui::{Align, Color32, CornerRadius, Frame, Layout, Margin, RichText, Stroke};
use egui_phosphor::regular as icon;
use gentoo_settings::catalog::{self, Category};
use gentoo_settings::detect::{self, System};
use gentoo_settings::history::{self, Entry};
use gentoo_settings::plan::{self, Change, Plan};
use gentoo_settings::protocol::{Event, FileAction, Request, VERSION};
use gentoo_settings::status::{self, Badge, Scan, Status};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::mpsc::{Receiver, Sender, channel};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tab {
    Tweaks,
    System,
    Activity,
}

impl Tab {
    pub const ALL: [Tab; 3] = [Tab::Tweaks, Tab::System, Tab::Activity];

    pub fn label(&self) -> &'static str {
        match self {
            Tab::Tweaks => "Tweaks",
            Tab::System => "System",
            Tab::Activity => "Activity",
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

pub struct Toast {
    pub kind: ToastKind,
    pub text: String,
    pub created: Instant,
}

/// A staged change to one tweak.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pending {
    pub enable: bool,
    pub value: Option<String>,
}

pub struct Review {
    pub label: String,
    pub changes: Vec<Change>,
    pub plan: Result<Plan, String>,
    pub expanded: Option<usize>,
}

pub struct Run {
    pub label: String,
    pub events: Vec<Event>,
    /// `Some(ok)` once the helper has exited.
    pub done: Option<bool>,
    pub failure: Option<String>,
    pub started: Instant,
}

pub struct App {
    pub sys: System,
    pub scan: Scan,
    pub history: Vec<Entry>,
    pub tab: Tab,
    /// `None`: the overview page.
    pub category: Option<Category>,
    pub expanded: HashSet<&'static str>,
    pub pending: BTreeMap<&'static str, Pending>,
    /// Parameter values being edited for tweaks that are not applied.
    pub values: HashMap<&'static str, String>,
    pub query: String,
    pub show_unavailable: bool,
    pub review: Option<Review>,
    pub run: Option<Run>,
    pub toasts: Vec<Toast>,
    /// The init system is overridden for preview: applying is disabled.
    pub preview_init: bool,
    exec_tx: Sender<ExecMsg>,
    exec_rx: Receiver<ExecMsg>,
    pub ctx: egui::Context,
    script: Option<crate::shot::Script>,
}

pub fn load_system() -> (System, bool) {
    let mut sys = match exec::scratch_root() {
        Some(r) => System::detect_at(&r),
        None => System::detect(),
    };
    let mut preview = false;
    if let Some(i) = detect::init_override()
        && i != sys.init
    {
        sys.init = i;
        sys.init_reason = format!("GENTOO_SETTINGS_INIT={} (preview)", i.label());
        preview = exec::scratch_root().is_none();
    }
    (sys, preview)
}

impl App {
    pub fn new(cc: &eframe::CreationContext<'_>) -> App {
        let ctx = cc.egui_ctx.clone();
        theme::install_fonts(&ctx, None);
        theme::apply(&ctx);
        let (sys, preview_init) = load_system();
        let scan = status::scan(&sys);
        let history = history::load(&sys);
        let (exec_tx, exec_rx) = channel();
        App {
            sys,
            scan,
            history,
            tab: Tab::Tweaks,
            category: None,
            expanded: HashSet::new(),
            pending: BTreeMap::new(),
            values: HashMap::new(),
            query: String::new(),
            show_unavailable: true,
            review: None,
            run: None,
            toasts: Vec::new(),
            preview_init,
            exec_tx,
            exec_rx,
            ctx,
            script: crate::shot::Script::from_env(),
        }
    }

    pub fn toast(&mut self, kind: ToastKind, text: impl Into<String>) {
        self.toasts.push(Toast { kind, text: text.into(), created: Instant::now() });
        self.ctx.request_repaint();
    }

    pub fn refresh(&mut self) {
        let (sys, preview) = load_system();
        self.sys = sys;
        self.preview_init = preview;
        self.scan = status::scan(&self.sys);
        self.history = history::load(&self.sys);
        // Drop staged changes that are now the state of the system.
        let scan = &self.scan;
        self.pending.retain(|id, p| {
            scan.get(id).is_some_and(|s| p.enable != s.installed.is_some() || (p.enable && p.value.as_deref().is_some_and(|v| v != s.value())))
        });
    }

    pub fn status(&self, id: &str) -> Option<&Status> {
        self.scan.get(id)
    }

    /// Will the tweak be applied once the pending changes are?
    pub fn wanted_on(&self, s: &Status) -> bool {
        self.pending.get(s.tweak.id).map(|p| p.enable).unwrap_or(s.installed.is_some())
    }

    /// The value shown in the editor: pending, being edited, installed or default.
    pub fn shown_value(&self, s: &Status) -> String {
        if let Some(Pending { enable: true, value: Some(v) }) = self.pending.get(s.tweak.id) {
            return v.clone();
        }
        self.values.get(s.tweak.id).cloned().unwrap_or_else(|| s.value())
    }

    pub fn set_on(&mut self, id: &'static str, on: bool) {
        let Some(s) = self.status(id) else { return };
        let installed = s.installed.clone();
        let value = status::has_param(s.tweak).then(|| self.shown_value(s));
        match (&installed, on) {
            (Some(i), true) if value.as_deref().is_none_or(|v| v == i.value) && !i.modified && !i.partial => {
                self.pending.remove(id);
            }
            (None, false) => {
                self.pending.remove(id);
            }
            _ => {
                self.pending.insert(id, Pending { enable: on, value: if on { value } else { None } });
            }
        }
    }

    pub fn set_value(&mut self, id: &'static str, value: String) {
        self.values.insert(id, value);
        let on = self.status(id).map(|s| self.wanted_on(s)).unwrap_or(false);
        if on {
            self.set_on(id, true);
        }
    }

    pub fn pending_changes(&self) -> Vec<Change> {
        let mut v: Vec<(&&'static str, &Pending)> = self.pending.iter().collect();
        v.sort_by_key(|(id, _)| catalog::index_of(id));
        v.into_iter().map(|(id, p)| Change { id: id.to_string(), enable: p.enable, value: p.value.clone() }).collect()
    }

    /// Stage CachyOS's defaults: every recommended tweak that applies to this
    /// machine and isn't in effect yet.
    pub fn stage_defaults(&mut self, only: Option<Category>) -> usize {
        let ids: Vec<&'static str> = self
            .scan
            .statuses
            .iter()
            .filter(|s| s.tweak.recommended && s.available.is_ok() && only.is_none_or(|c| s.tweak.category == c))
            .filter(|s| matches!(s.badge, Badge::Off | Badge::Modified))
            .map(|s| s.tweak.id)
            .collect();
        for id in &ids {
            self.set_on(id, true);
        }
        ids.len()
    }

    pub fn stage_revert_all(&mut self) -> usize {
        let ids: Vec<&'static str> = self.scan.statuses.iter().filter(|s| s.installed.is_some()).map(|s| s.tweak.id).collect();
        for id in &ids {
            self.set_on(id, false);
        }
        ids.len()
    }

    pub fn open_review(&mut self, label: String, changes: Vec<Change>) {
        if changes.is_empty() {
            self.toast(ToastKind::Info, "Nothing to change.");
            return;
        }
        let plan = plan::plan(&self.sys, &changes);
        self.review = Some(Review { label, changes, plan, expanded: None });
    }

    pub fn review_pending(&mut self) {
        let n = self.pending.len();
        self.open_review(format!("{n} tweak{}", if n == 1 { "" } else { "s" }), self.pending_changes());
    }

    /// Press APPLY in the review dialog.
    pub fn apply_review(&mut self) {
        if let Some(r) = self.review.take() {
            self.start_run(r.label, r.changes);
        }
    }

    fn start_run(&mut self, label: String, changes: Vec<Change>) {
        let req = Request { version: VERSION.into(), label: label.clone(), changes };
        match exec::start(&req, self.exec_tx.clone(), self.ctx.clone()) {
            Ok(()) => self.run = Some(Run { label, events: Vec::new(), done: None, failure: None, started: Instant::now() }),
            Err(e) => self.toast(ToastKind::Error, e),
        }
    }

    fn handle_exec(&mut self) {
        while let Ok(m) = self.exec_rx.try_recv() {
            let Some(run) = &mut self.run else { continue };
            match m {
                ExecMsg::Event(ev) => {
                    if let Event::Done { ok, .. } = &ev {
                        run.done = Some(*ok);
                    }
                    run.events.push(ev);
                }
                ExecMsg::Exit(code, err) => {
                    let ok = code == Some(0) && run.done == Some(true);
                    if !ok && run.done.is_none() {
                        let reported = run.events.iter().any(|e| matches!(e, Event::Error { .. }));
                        if !reported {
                            run.failure = Some(exec::explain_exit(code, &err));
                        }
                    }
                    run.done = Some(ok);
                    let label = run.label.clone();
                    let warned = run.events.iter().any(|e| matches!(e, Event::Done { warnings, .. } if !warnings.is_empty()));
                    self.refresh();
                    if ok && warned {
                        self.toast(
                            ToastKind::Warn,
                            format!("Applied with warnings: {label}. The files are in place; see the log for what did not take effect yet."),
                        );
                    } else if ok {
                        self.toast(ToastKind::Success, format!("Applied: {label}"));
                    }
                }
            }
        }
    }

    // ───────────────────────── chrome ─────────────────────────

    fn nav_bar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.add_space(6.0);
            let (rect, _) = ui.allocate_exact_size(egui::vec2(30.0, 30.0), egui::Sense::hover());
            ui.painter().circle_filled(rect.center(), 14.0, Color32::from_rgb(0x2a, 0x47, 0x5e));
            ui.painter().text(rect.center(), egui::Align2::CENTER_CENTER, icon::SLIDERS_HORIZONTAL, egui::FontId::proportional(17.0), ACCENT);
            ui.label(RichText::new("GENTOO").size(17.0).family(theme::bold()).color(WHITE));
            ui.label(RichText::new("SETTINGS").size(17.0).family(theme::light()).color(ACCENT));
            ui.add_space(24.0);
            for t in Tab::ALL {
                let mut label = t.label().to_string();
                if t == Tab::Activity && self.run.as_ref().is_some_and(|r| r.done.is_none()) {
                    label = format!("{label} {}", icon::CIRCLE_NOTCH);
                }
                if w::nav_tab(ui, &label, self.tab == t).clicked() {
                    if self.tab == t && t == Tab::Tweaks {
                        self.category = None;
                    }
                    self.tab = t;
                }
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.add_space(8.0);
                if w::icon_button(ui, icon::ARROWS_CLOCKWISE, "Re-read the system (F5)").clicked() {
                    self.refresh();
                    self.toast(ToastKind::Info, "System re-read.");
                }
                ui.add_space(4.0);
                let (col, tip) = if self.preview_init {
                    (YELLOW, format!("Previewing the {} implementation — applying is disabled.", self.sys.init.label()))
                } else {
                    (GREEN, format!("Init system: {} — detected because {}.", self.sys.init.label(), self.sys.init_reason))
                };
                ui.add(
                    egui::Button::new(RichText::new(format!("{} {}", icon::TREE_STRUCTURE, self.sys.init.label())).color(col).size(13.0))
                        .fill(Color32::from_rgb(0x22, 0x28, 0x33))
                        .corner_radius(CornerRadius::same(12)),
                )
                .on_hover_text(tip);
                ui.add_space(8.0);
                ui.label(RichText::new(format!("Linux {}", self.sys.kernel)).size(12.0).color(TEXT_FAINT));
                if exec::scratch_root().is_some() {
                    ui.add_space(8.0);
                    w::badge(ui, "SCRATCH ROOT", Color32::from_rgb(0x2b, 0x1a, 0x00), ORANGE);
                }
            });
        });
    }

    fn status_bar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.set_min_height(28.0);
            let active = self.scan.statuses.iter().filter(|s| s.badge.applied()).count();
            let avail = self.scan.statuses.iter().filter(|s| s.available.is_ok()).count();
            ui.label(w::dim(format!(
                "{} {} tweaks · {} available on this machine · {} applied by Gentoo Settings",
                icon::DATABASE,
                catalog::CATALOG.len(),
                avail,
                active
            )));
            if let Some(e) = self.history.first() {
                ui.label(w::dim(format!("· last change {}", crate::fmt::ago(e.ts))));
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.add_space(6.0);
                if !exec::helper_installed() && exec::scratch_root().is_none() {
                    ui.label(RichText::new(format!("{} helper not installed system-wide — run sudo dist/install.sh", icon::WARNING)).color(ORANGE).size(12.5))
                        .on_hover_text(
                            "Without /usr/libexec/gentoo-settings-helper and its polkit policy, pkexec shows a generic prompt for the development build.",
                        );
                }
            });
        });
    }

    /// Steam's download bar: what is staged, and the button to review it.
    fn pending_bar(&mut self, ui: &mut egui::Ui) {
        let n = self.pending.len();
        ui.horizontal(|ui| {
            ui.set_min_height(46.0);
            ui.label(RichText::new(icon::STACK).size(22.0).color(ACCENT));
            ui.vertical(|ui| {
                ui.spacing_mut().item_spacing.y = 1.0;
                ui.label(RichText::new(format!("{n} CHANGE{} PENDING", if n == 1 { "" } else { "S" })).size(12.0).family(theme::bold()).color(ACCENT));
                let on = self.pending.values().filter(|p| p.enable).count();
                let mut ids: Vec<&&str> = self.pending.keys().collect();
                ids.sort_by_key(|id| catalog::index_of(id));
                let titles: Vec<&str> = ids.iter().filter_map(|id| catalog::get(id).map(|t| t.title)).take(3).collect();
                let more = if n > 3 { format!(" and {} more", n - 3) } else { String::new() };
                ui.set_max_width((ui.available_width() - 330.0).max(200.0));
                ui.add(
                    egui::Label::new(RichText::new(format!("{on} to apply, {} to remove — {}{more}", n - on, titles.join(", "))).color(TEXT).size(13.0))
                        .truncate(),
                );
            });
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                let can = !self.preview_init;
                if ui
                    .add_enabled_ui(can, |ui| w::big_button(ui, &format!("{}  REVIEW & APPLY", icon::CHECK), GRAD_GREEN, 190.0))
                    .inner
                    .on_disabled_hover_text("Previewing another init system")
                    .clicked()
                {
                    self.review_pending();
                }
                ui.add_space(6.0);
                if w::button(ui, icon::X, "Discard").clicked() {
                    self.pending.clear();
                    self.values.clear();
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
        let lift = if self.pending.is_empty() { 60.0 } else { 120.0 };
        egui::Area::new(egui::Id::new("toasts"))
            .order(egui::Order::Foreground)
            .fixed_pos(egui::pos2(screen.right() - 400.0, screen.bottom() - lift - 70.0 * self.toasts.len().min(5) as f32))
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

    fn review_ui(&mut self, ctx: &egui::Context) {
        let Some(review) = &mut self.review else { return };
        let mut apply = false;
        let mut cancel = false;
        let names = |c: &Change| catalog::get(&c.id).map(|t| t.title).unwrap_or(&c.id).to_string();
        egui::Modal::new(egui::Id::new("review-dialog"))
            .frame(Frame::new().fill(PANEL).corner_radius(CornerRadius::same(6)).inner_margin(Margin::same(18)))
            .show(ctx, |ui| {
                ui.set_width(820.0f32.min(ctx.content_rect().width() - 80.0));
                ui.horizontal(|ui| {
                    ui.label(RichText::new(icon::FLOPPY_DISK).size(22.0).color(ACCENT));
                    w::title(ui, "Review changes");
                });
                w::subtitle(ui, &review.label);
                ui.add_space(6.0);
                egui::ScrollArea::vertical().max_height(ctx.content_rect().height() * 0.62).show(ui, |ui| {
                    w::section(ui, "Tweaks");
                    for c in &review.changes {
                        ui.horizontal(|ui| {
                            if c.enable {
                                w::status_badge(ui, "WILL ENABLE");
                            } else {
                                w::status_badge(ui, "WILL DISABLE");
                            }
                            ui.label(w::strong(names(c)));
                            if let Some(v) = &c.value {
                                ui.label(w::mono(format!("= {v}")));
                            }
                            if let Some(t) = catalog::get(&c.id)
                                && c.enable
                                && t.after != catalog::After::Now
                            {
                                ui.label(w::dim(format!("· full effect {}", t.after.label())));
                            }
                        });
                    }
                    match &review.plan {
                        Err(e) => {
                            ui.add_space(8.0);
                            w::callout(ui, RED, icon::X_CIRCLE, e);
                        }
                        Ok(plan) => {
                            w::section(ui, &format!("Files ({})", plan.files.len()));
                            if plan.files.is_empty() {
                                ui.label(w::dim("No file changes."));
                            }
                            for (i, f) in plan.files.iter().enumerate() {
                                let (ic, verb) = match (&f.old, &f.new) {
                                    (_, None) => (icon::TRASH, "delete"),
                                    (None, Some(_)) => (icon::FILE_PLUS, "create"),
                                    _ => (icon::PENCIL_SIMPLE, "modify"),
                                };
                                let open = review.expanded == Some(i);
                                let header = format!("{} {}  {verb} {}", if open { icon::CARET_DOWN } else { icon::CARET_RIGHT }, ic, f.path);
                                if ui.selectable_label(open, RichText::new(header).color(WHITE)).clicked() {
                                    review.expanded = if open { None } else { Some(i) };
                                }
                                if open {
                                    if f.unreadable {
                                        ui.label(w::dim(
                                            "This file is only readable by root; the old content shown is reconstructed from Gentoo Settings' records.",
                                        ));
                                    }
                                    Frame::new().fill(BG_DEEP).inner_margin(Margin::same(8)).corner_radius(CornerRadius::same(3)).show(ui, |ui| {
                                        ui.set_width(ui.available_width());
                                        let old = f.old.clone().unwrap_or_default();
                                        let new = f.new.clone().unwrap_or_default();
                                        for (k, line) in crate::fmt::line_diff(&old, &new) {
                                            let (prefix, col) = match k {
                                                '+' => ("+ ", GREEN),
                                                '-' => ("- ", RED),
                                                _ => ("  ", TEXT_DIM),
                                            };
                                            ui.label(RichText::new(format!("{prefix}{line}")).monospace().color(col));
                                        }
                                    });
                                }
                            }
                            let cmds: Vec<String> =
                                plan.pre.iter().chain(&plan.post).filter(|c| !matches!(c, plan::Cmd::Save { .. })).map(|c| c.describe()).collect();
                            if !cmds.is_empty() {
                                w::section(ui, &format!("Then, on the running system ({})", cmds.len()));
                                for c in cmds {
                                    ui.horizontal(|ui| {
                                        ui.label(RichText::new(icon::TERMINAL).color(GREEN));
                                        ui.label(RichText::new(c).monospace().color(Color32::from_rgb(0xd7, 0xe7, 0xb4)));
                                    });
                                }
                            }
                            let notes: Vec<&str> = review
                                .changes
                                .iter()
                                .filter(|c| c.enable)
                                .filter_map(|c| catalog::get(&c.id))
                                .map(|t| t.notes)
                                .filter(|n| !n.is_empty())
                                .collect();
                            if !notes.is_empty() {
                                w::section(ui, "Notes");
                                for n in notes {
                                    ui.label(RichText::new(format!("{}  {n}", icon::INFO)).color(TEXT_DIM));
                                }
                            }
                        }
                    }
                });
                ui.add_space(10.0);
                ui.horizontal(|ui| {
                    let ok = review.plan.is_ok();
                    if ui.add_enabled_ui(ok, |ui| w::big_button(ui, &format!("{}  APPLY", icon::CHECK), GRAD_GREEN, 140.0)).inner.clicked() {
                        apply = true;
                    }
                    if w::button(ui, "", "Cancel").clicked() {
                        cancel = true;
                    }
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        ui.label(w::dim(if exec::scratch_root().is_some() {
                            "Scratch root: files change below it, no programs run"
                        } else if unsafe { libc::geteuid() } == 0 {
                            "Runs as root"
                        } else {
                            "You will be asked for an administrator password"
                        }));
                    });
                });
            });
        if apply {
            self.apply_review();
        } else if cancel {
            self.review = None;
        }
    }

    fn run_ui(&mut self, ctx: &egui::Context) {
        let Some(run) = &self.run else { return };
        let mut close = false;
        egui::Modal::new(egui::Id::new("run-dialog")).frame(Frame::new().fill(PANEL).corner_radius(CornerRadius::same(6)).inner_margin(Margin::same(18))).show(
            ctx,
            |ui| {
                ui.set_width(720.0f32.min(ctx.content_rect().width() - 80.0));
                ui.horizontal(|ui| match run.done {
                    None => {
                        ui.add(egui::Spinner::new().size(20.0).color(ACCENT));
                        w::title(ui, "Applying…");
                    }
                    Some(true) => {
                        ui.label(RichText::new(icon::CHECK_CIRCLE).size(22.0).color(GREEN));
                        w::title(ui, "Done");
                    }
                    Some(false) => {
                        ui.label(RichText::new(icon::X_CIRCLE).size(22.0).color(RED));
                        w::title(ui, "Not applied");
                    }
                });
                w::subtitle(ui, &run.label);
                ui.add_space(6.0);
                if run.done.is_none() && run.events.is_empty() && run.started.elapsed() > Duration::from_millis(400) {
                    w::callout(ui, ACCENT, icon::LOCK, "Waiting for the administrator password (polkit)…");
                }
                if let Some(f) = &run.failure {
                    w::callout(ui, RED, icon::X_CIRCLE, f);
                }
                egui::ScrollArea::vertical().max_height(ctx.content_rect().height() * 0.55).stick_to_bottom(true).show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    for ev in &run.events {
                        match ev {
                            Event::Ready { dry_run, init, .. } => {
                                ui.label(w::dim(format!(
                                    "{} Helper started ({}{})",
                                    icon::LOCK_OPEN,
                                    init.label(),
                                    if *dry_run { ", scratch root" } else { "" }
                                )));
                            }
                            Event::Step { text } => {
                                ui.label(RichText::new(format!("{}  {text}", icon::ARROW_RIGHT)).color(ACCENT));
                            }
                            Event::File { path, action } => {
                                let (ic, verb, col) = match action {
                                    FileAction::Create => (icon::FILE_PLUS, "created", GREEN),
                                    FileAction::Modify => (icon::PENCIL_SIMPLE, "updated", ACCENT),
                                    FileAction::Delete => (icon::TRASH, "removed", ORANGE),
                                };
                                ui.horizontal(|ui| {
                                    ui.label(RichText::new(ic).color(col));
                                    ui.label(w::mono(path));
                                    ui.label(w::dim(verb));
                                });
                            }
                            Event::Command { text, ok, output, skipped } => {
                                ui.horizontal_wrapped(|ui| {
                                    let (ic, col) = if *skipped {
                                        (icon::MINUS, TEXT_DIM)
                                    } else if *ok {
                                        (icon::CHECK, GREEN)
                                    } else {
                                        (icon::WARNING, ORANGE)
                                    };
                                    ui.label(RichText::new(ic).color(col));
                                    let r = ui.label(RichText::new(text).monospace().color(Color32::from_rgb(0xd7, 0xe7, 0xb4)));
                                    if !output.is_empty() {
                                        r.on_hover_text(output);
                                        if !ok {
                                            ui.label(RichText::new(output).color(ORANGE).size(12.0));
                                        }
                                    }
                                });
                            }
                            Event::Done { warnings, backup, .. } => {
                                if let Some(b) = backup {
                                    ui.label(w::dim(format!("{} Previous files saved in {b}", icon::FLOPPY_DISK)));
                                }
                                if !warnings.is_empty() {
                                    ui.label(
                                        RichText::new(format!(
                                            "{} {} warning(s): the files are in place, but not everything took effect now.",
                                            icon::WARNING,
                                            warnings.len()
                                        ))
                                        .color(ORANGE),
                                    );
                                }
                            }
                            Event::Error { msg } => {
                                ui.label(RichText::new(format!("{}  {msg}", icon::X_CIRCLE)).color(RED));
                            }
                        }
                    }
                });
                ui.add_space(10.0);
                ui.horizontal(|ui| {
                    if ui.add_enabled_ui(run.done.is_some(), |ui| w::primary(ui, icon::CHECK, "Close")).inner.clicked() {
                        close = true;
                    }
                    if run.done == Some(true) {
                        let reboot = self.scan.statuses.iter().any(|s| s.badge == Badge::Pending(catalog::After::Reboot));
                        if reboot {
                            ui.label(RichText::new(format!("{} Some changes need a reboot to take full effect.", icon::POWER)).color(ORANGE));
                        }
                    }
                });
            },
        );
        if close {
            if self.run.as_ref().is_some_and(|r| r.done == Some(true)) {
                self.pending.clear();
                self.values.clear();
            }
            self.run = None;
        }
    }
}

impl eframe::App for App {
    fn logic(&mut self, _ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.handle_exec();
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        if ctx.input(|i| i.key_pressed(egui::Key::F5)) {
            self.refresh();
        }
        egui::Panel::top("nav").frame(Frame::new().fill(NAV).inner_margin(Margin::symmetric(8, 6))).show_separator_line(false).show(ui, |ui| self.nav_bar(ui));
        egui::Panel::bottom("status")
            .frame(Frame::new().fill(NAV).inner_margin(Margin::symmetric(10, 3)))
            .show_separator_line(false)
            .show(ui, |ui| self.status_bar(ui));
        if !self.pending.is_empty() {
            egui::Panel::bottom("pending")
                .frame(Frame::new().fill(Color32::from_rgb(0x1a, 0x2f, 0x45)).inner_margin(Margin::symmetric(16, 6)).stroke(Stroke::new(1.0, ACCENT_DIM)))
                .show_separator_line(false)
                .show(ui, |ui| self.pending_bar(ui));
        }
        egui::CentralPanel::default().frame(Frame::new().fill(BG)).show(ui, |ui| match self.tab {
            Tab::Tweaks => views::tweaks::show(self, ui),
            Tab::System => views::system::show(self, ui),
            Tab::Activity => views::activity::show(self, ui),
        });
        self.review_ui(&ctx);
        self.run_ui(&ctx);
        self.toasts_ui(&ctx);
        if let Some(mut s) = self.script.take() {
            crate::shot::run(self, &ctx, &mut s);
            self.script = Some(s);
        }
    }
}
