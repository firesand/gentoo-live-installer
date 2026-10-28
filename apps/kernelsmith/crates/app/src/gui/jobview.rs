//! The Activity page: live progress of a running job, styled after Steam's
//! download manager (big progress bar, throughput graph, step list, log).

use super::theme::*;
use super::{open_path, App, Page};
use crate::runner::{is_compile_line, Event, Runner};
use eframe::egui::{self, Color32, CornerRadius, Margin, RichText, ScrollArea, Sense, Stroke, Ui, Vec2};
use kernelsmith_core::hw::SystemInfo;
use kernelsmith_core::job::PreparedJob;
use kernelsmith_core::script;
use std::collections::{HashMap, VecDeque};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

const LOG_LINES: usize = 4000;
/// Compiled files per enabled Kconfig option, measured on real builds.
const OBJECTS_PER_OPTION: f64 = 2.0;

#[derive(Clone)]
pub enum JobKind {
    Build { release: String, autofdo_ready: bool },
    Uninstall { release: String },
    Sources,
    Tuning,
    Portage,
    Profile { release: String },
}

pub struct JobView {
    pub job: PreparedJob,
    pub kind: JobKind,
    runner: Option<Runner>,
    started: Instant,
    finished: Option<(Instant, Option<i32>)>,
    stages: Vec<String>,
    current: Option<String>,
    stage_times: HashMap<String, (Instant, Option<Instant>)>,
    compiled: u64,
    est_total: u64,
    log: VecDeque<String>,
    messages: Vec<(u8, String)>,
    verify_ok: usize,
    verify_miss: Vec<(String, String, String, String)>,
    config_stats: Option<(u64, u64)>,
    done: Option<String>,
    modrebuild_ok: Option<bool>,
    pub profile_out: Option<PathBuf>,
    throughput: Vec<f32>,
    last_sample: (Instant, u64),
    show_log: bool,
    cancel_requested: bool,
}

impl JobView {
    pub fn new(job: PreparedJob, kind: JobKind, runner: Runner) -> Self {
        let now = Instant::now();
        JobView {
            stages: job.stages.clone(),
            est_total: job.est_objects.max(1),
            job,
            kind,
            runner: Some(runner),
            started: now,
            finished: None,
            current: None,
            stage_times: HashMap::new(),
            compiled: 0,
            log: VecDeque::new(),
            messages: vec![],
            verify_ok: 0,
            verify_miss: vec![],
            config_stats: None,
            done: None,
            modrebuild_ok: None,
            profile_out: None,
            throughput: vec![],
            last_sample: (now, 0),
            show_log: false,
            cancel_requested: false,
        }
    }

    pub fn cancel(&mut self) {
        if let Some(r) = self.runner.as_mut() {
            r.cancel();
        }
        self.cancel_requested = true;
    }

    pub fn running(&self) -> bool {
        self.finished.is_none()
    }

    pub fn succeeded(&self) -> bool {
        matches!(self.finished, Some((_, Some(0)))) && self.done.is_some()
    }

    fn push_log(&mut self, line: String) {
        if self.log.len() >= LOG_LINES {
            self.log.pop_front();
        }
        self.log.push_back(line);
    }

    /// Drain runner output. Returns true on the frame the job finishes.
    pub fn pump(&mut self, ctx: &egui::Context) -> bool {
        let Some(runner) = self.runner.as_mut() else { return false };
        let mut events = vec![];
        while let Ok(ev) = runner.rx.try_recv() {
            events.push(ev);
            if events.len() > 5000 {
                break;
            }
        }
        let exit = runner.poll_exit();
        let now = Instant::now();
        for ev in events {
            match ev {
                Event::Line(l) => {
                    if is_compile_line(&l) {
                        self.compiled += 1;
                    }
                    self.push_log(l);
                }
                Event::Plan(stages) => self.stages = stages,
                Event::Stage { id, label } => {
                    if let Some(cur) = &self.current {
                        if let Some(t) = self.stage_times.get_mut(cur) {
                            t.1 = Some(now);
                        }
                    }
                    self.stage_times.insert(id.clone(), (now, None));
                    self.push_log(format!("==> {label}"));
                    self.current = Some(id);
                }
                Event::Note(n) => {
                    self.push_log(format!("  • {n}"));
                    self.messages.push((3, n));
                }
                Event::Warn(w) => {
                    self.push_log(format!("  ! {w}"));
                    self.messages.push((1, w));
                }
                Event::Error(e) => {
                    self.push_log(format!("  ✖ {e}"));
                    self.messages.push((2, e));
                }
                Event::Verify { ok, sym, want, got, title } => {
                    if ok {
                        self.verify_ok += 1;
                    } else {
                        self.verify_miss.push((sym, want, got, title));
                    }
                }
                Event::ConfigStats { y, m } => {
                    self.config_stats = Some((y, m));
                    self.est_total = (((y + m) as f64) * OBJECTS_PER_OPTION) as u64;
                }
                Event::ModRebuild(ok) => self.modrebuild_ok = Some(ok),
                Event::Profile(p) => self.profile_out = Some(p),
                Event::Done(r) => self.done = Some(r),
            }
        }
        // One throughput sample per second for the graph.
        if now.duration_since(self.last_sample.0) >= Duration::from_secs(1) && self.finished.is_none() {
            let secs = now.duration_since(self.last_sample.0).as_secs_f32();
            self.throughput.push((self.compiled - self.last_sample.1) as f32 / secs);
            if self.throughput.len() > 600 {
                self.throughput.remove(0);
            }
            self.last_sample = (now, self.compiled);
        }
        if let Some(code) = exit {
            // Drain anything left in the pipe.
            let rest: Vec<Event> = self.runner.as_ref().map(|r| r.rx.try_iter().collect()).unwrap_or_default();
            for ev in rest {
                match ev {
                    Event::Line(l) => self.push_log(l),
                    Event::Done(d) => self.done = Some(d),
                    Event::Error(e) => self.messages.push((2, e)),
                    Event::Profile(p) => self.profile_out = Some(p),
                    _ => {}
                }
            }
            if let Some(cur) = &self.current {
                if let Some(t) = self.stage_times.get_mut(cur) {
                    t.1 = Some(now);
                }
            }
            self.finished = Some((now, code));
            self.runner = None;
            ctx.request_repaint();
            return true;
        }
        ctx.request_repaint_after(Duration::from_millis(250));
        false
    }

    fn stage_weight(id: &str, n: usize) -> f32 {
        script::STAGES.iter().find(|s| s.0 == id).map(|s| s.2).unwrap_or(1.0 / n.max(1) as f32)
    }

    pub fn progress(&self) -> f32 {
        if self.succeeded() {
            return 1.0;
        }
        let n = self.stages.len();
        let total: f32 = self.stages.iter().map(|s| Self::stage_weight(s, n)).sum::<f32>().max(0.0001);
        let mut acc = 0.0;
        for s in &self.stages {
            let w = Self::stage_weight(s, n);
            if Some(s) == self.current.as_ref() {
                let inner = if s == "build" { (self.compiled as f32 / self.est_total as f32).min(0.98) } else { 0.4 };
                acc += w * inner;
                break;
            }
            acc += w;
        }
        (acc / total).clamp(0.0, 1.0)
    }

    fn eta(&self) -> Option<Duration> {
        if self.current.as_deref() != Some("build") || self.compiled < 200 {
            return None;
        }
        let (start, _) = self.stage_times.get("build")?;
        let el = start.elapsed().as_secs_f64();
        let rate = self.compiled as f64 / el.max(1.0);
        let remaining = (self.est_total.saturating_sub(self.compiled)) as f64 / rate.max(0.1);
        Some(Duration::from_secs_f64(remaining + 45.0))
    }

    pub fn summary(&self) -> (f32, String, Color32) {
        let stage = self.current.as_deref().map(script::stage_label).unwrap_or("Starting");
        match self.finished {
            None => (self.progress(), format!("{} — {}", self.job.title, stage), TEXT_BRIGHT),
            Some(_) if self.succeeded() => (1.0, format!("{} — finished", self.job.title), GREEN),
            Some(_) => (self.progress(), format!("{} — failed", self.job.title), DANGER),
        }
    }

    fn elapsed(&self) -> Duration {
        self.finished.map(|(t, _)| t).unwrap_or_else(Instant::now).duration_since(self.started)
    }

    fn install_reached(&self) -> bool {
        self.stage_times.contains_key("modules") || self.stage_times.contains_key("kernel")
    }
}

fn fmt_dur(d: Duration) -> String {
    let s = d.as_secs();
    if s >= 3600 {
        format!("{}h {:02}m", s / 3600, (s % 3600) / 60)
    } else {
        format!("{}:{:02}", s / 60, s % 60)
    }
}

impl App {
    pub(super) fn activity(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>) {
        let Some(job) = self.job.as_mut() else {
            ui.vertical_centered(|ui| {
                ui.add_space(80.0);
                dim(ui, "Nothing is running.");
            });
            return;
        };
        let mut go_library = false;
        let mut dismiss = false;
        let mut save_preset = false;
        let mut rebuild_profile: Option<(String, PathBuf)> = None;

        egui::Frame::new().inner_margin(Margin { left: 30, right: 30, top: 22, bottom: 16 }).show(ui, |ui| {
            ScrollArea::vertical().id_salt("activity").auto_shrink([false, false]).show(ui, |ui| {
                ui.set_max_width(ui.available_width().min(1200.0));
                // ── Header ──
                let (frac, _, _) = job.summary();
                ui.horizontal(|ui| {
                    let (icon, color) = match job.finished {
                        None => ("⚙", ACCENT),
                        Some(_) if job.succeeded() => ("✔", GREEN),
                        Some(_) => ("✖", DANGER),
                    };
                    let (rect, _) = ui.allocate_exact_size(Vec2::splat(64.0), Sense::hover());
                    vgradient(ui.painter(), rect, color.linear_multiply(0.5), color.linear_multiply(0.15));
                    ui.painter().text(rect.center(), egui::Align2::CENTER_CENTER, icon, regular(30.0), Color32::WHITE);
                    ui.vertical(|ui| {
                        ui.label(RichText::new(&job.job.title).font(bold(24.0)).color(TEXT_BRIGHT));
                        let status = match job.finished {
                            None if job.cancel_requested => "Cancelling…".to_string(),
                            None => job.current.as_deref().map(script::stage_label).unwrap_or("Waiting for authorization…").to_string(),
                            Some(_) if job.succeeded() => "Finished successfully".into(),
                            Some((_, Some(130))) => "Cancelled".into(),
                            Some(_) => "Failed".into(),
                        };
                        ui.label(RichText::new(status).color(ACCENT).size(16.0));
                    });
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if job.running() {
                            if button_enabled(ui, "Cancel", Btn::Danger, !job.cancel_requested).clicked() {
                                job.cancel();
                            }
                        } else if button(ui, "Dismiss", Btn::Secondary).clicked() {
                            dismiss = true;
                        }
                        ui.add_space(16.0);
                        ui.with_layout(egui::Layout::top_down(egui::Align::Max), |ui| {
                            ui.label(RichText::new(format!("Elapsed {}", fmt_dur(job.elapsed()))).color(TEXT_BRIGHT));
                            if let Some(eta) = job.eta() {
                                dim(ui, &format!("About {} left", fmt_dur(eta)));
                            }
                        });
                    });
                });
                ui.add_space(12.0);
                ui.horizontal(|ui| {
                    ui.label(RichText::new(format!("{:.0}%", frac * 100.0)).font(bold(20.0)).color(TEXT_BRIGHT));
                    ui.add_space(8.0);
                    let (a, b) = if job.finished.is_some() && !job.succeeded() { (DANGER, DANGER_B) } else { (BLUE_A, ACCENT_STRONG) };
                    progress(ui, frac, 14.0, a, b);
                });
                if job.current.as_deref() == Some("build") || job.compiled > 0 {
                    ui.add_space(8.0);
                    throughput_graph(ui, &job.throughput, job.compiled, job.est_total);
                }

                // ── Result panel ──
                if let Some((_, code)) = job.finished {
                    ui.add_space(12.0);
                    if job.succeeded() {
                        success_panel(ui, job, sys, &mut go_library, &mut save_preset, &mut rebuild_profile);
                    } else {
                        let errs: Vec<&String> = job.messages.iter().filter(|m| m.0 == 2).map(|m| &m.1).collect();
                        let mut text = if code == Some(130) { "The job was cancelled.".to_string() } else { "The job stopped with an error.".to_string() };
                        if let Some(e) = errs.last() {
                            text.push_str(&format!("\n\n{e}"));
                        }
                        if matches!(job.kind, JobKind::Build { .. }) {
                            text.push_str(if job.install_reached() {
                                "\n\nIt failed during installation; your previous kernels are untouched. Check the log below."
                            } else {
                                "\n\nNothing was installed and the unfinished build tree was removed. The log below shows what went wrong."
                            });
                        }
                        callout(ui, DANGER, "✖", &text);
                        job.show_log = true;
                    }
                }

                // ── Steps + messages ──
                ui.add_space(12.0);
                ui.columns(2, |cols| {
                    panel(&mut cols[0], |ui| {
                        ui.set_width(ui.available_width());
                        title(ui, "Steps");
                        for s in job.stages.clone() {
                            let t = job.stage_times.get(&s).copied();
                            let is_cur = job.current.as_deref() == Some(s.as_str()) && job.running();
                            ui.horizontal(|ui| {
                                match (t, is_cur) {
                                    (_, true) => {
                                        ui.add(egui::Spinner::new().size(14.0).color(ACCENT));
                                    }
                                    (Some((_, Some(_))), _) => {
                                        ui.label(RichText::new("✔").color(GREEN));
                                    }
                                    (Some((_, None)), false) if job.finished.is_some() && !job.succeeded() => {
                                        ui.label(RichText::new("✖").color(DANGER));
                                    }
                                    _ => {
                                        ui.label(RichText::new("○").color(TEXT_DIM));
                                    }
                                }
                                let label = script::stage_label(&s);
                                ui.label(RichText::new(label).color(if t.is_some() { TEXT_BRIGHT } else { TEXT_DIM }));
                                if let Some((a, b)) = t {
                                    let d = b.unwrap_or_else(Instant::now).duration_since(a);
                                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                        dim(ui, &fmt_dur(d));
                                    });
                                }
                            });
                        }
                        if let Some((y, m)) = job.config_stats {
                            ui.add_space(6.0);
                            dim(ui, &format!("Configuration: {y} built-in options, {m} modules"));
                        }
                        if job.verify_ok + job.verify_miss.len() > 0 {
                            let total = job.verify_ok + job.verify_miss.len();
                            if job.verify_miss.is_empty() {
                                status_line(ui, 0, &format!("All {total} requested options verified."));
                            } else {
                                status_line(ui, 1, &format!("{} of {total} options couldn't be applied (their dependencies aren't met):", job.verify_miss.len()));
                                for (sym, want, got, title_) in job.verify_miss.iter().take(30) {
                                    dim(ui, &format!("   CONFIG_{sym}: wanted {want}, got {got} — {title_}"));
                                }
                            }
                        }
                    });
                    panel(&mut cols[1], |ui| {
                        ui.set_width(ui.available_width());
                        title(ui, "Messages");
                        if job.messages.is_empty() {
                            dim(ui, "Nothing yet.");
                        }
                        let n = job.messages.len();
                        for (lvl, m) in job.messages.iter().skip(n.saturating_sub(14)) {
                            status_line(ui, *lvl, m);
                        }
                    });
                });

                // ── Log ──
                ui.add_space(10.0);
                ui.horizontal(|ui| {
                    if ui.add(egui::Label::new(RichText::new(if job.show_log { "- Hide log" } else { "+ Show full log" }).color(ACCENT)).sense(Sense::click())).clicked() {
                        job.show_log = !job.show_log;
                    }
                    ui.add_space(12.0);
                    if ui.small_button("Copy log").clicked() {
                        ui.ctx().copy_text(job.log.iter().cloned().collect::<Vec<_>>().join("\n"));
                    }
                    if ui.small_button("Open job folder").clicked() {
                        open_path(&job.job.dir);
                    }
                    dim(ui, &format!("Full log: {}", job.job.log.display()));
                });
                if job.show_log {
                    egui::Frame::new()
                        .fill(Color32::from_rgb(0x0b, 0x10, 0x16))
                        .stroke(Stroke::new(1.0_f32, BORDER))
                        .corner_radius(CornerRadius::same(4))
                        .inner_margin(Margin::same(10))
                        .show(ui, |ui| {
                            ScrollArea::vertical().id_salt("log").max_height(360.0).stick_to_bottom(true).auto_shrink([false, true]).show(ui, |ui| {
                                ui.set_width(ui.available_width());
                                for l in &job.log {
                                    let color = if l.starts_with("==>") {
                                        ACCENT
                                    } else if l.starts_with("  ✖") || l.contains("error:") {
                                        DANGER
                                    } else if l.starts_with("  !") || l.contains("warning:") {
                                        WARN
                                    } else {
                                        TEXT_DIM
                                    };
                                    ui.label(RichText::new(l).font(mono(12.5)).color(color));
                                }
                            });
                        });
                }
            });
        });

        if dismiss {
            self.job = None;
            self.page = Page::Library;
        }
        if go_library {
            self.page = Page::Library;
        }
        if save_preset {
            if let Some(plan) = self.wiz.plan.as_ref() {
                self.modal = Some(super::modals::Modal::SavePreset { name: plan.name.clone() });
            }
        }
        if let Some((release, afdo)) = rebuild_profile {
            self.start_autofdo_rebuild(&release, afdo);
        }
    }
}

fn throughput_graph(ui: &mut Ui, samples: &[f32], compiled: u64, est: u64) {
    let h = 90.0;
    let (rect, _) = ui.allocate_exact_size(Vec2::new(ui.available_width(), h), Sense::hover());
    let p = ui.painter();
    p.rect_filled(rect, CornerRadius::same(3), Color32::from_rgb(0x0f, 0x16, 0x1f));
    for i in 1..4 {
        let y = rect.top() + rect.height() * i as f32 / 4.0;
        p.line_segment([egui::pos2(rect.left(), y), egui::pos2(rect.right(), y)], Stroke::new(1.0_f32, Color32::from_white_alpha(10)));
    }
    let max = samples.iter().cloned().fold(1.0f32, f32::max);
    if samples.len() > 1 {
        let n = samples.len();
        let dx = rect.width() / (n - 1).max(1) as f32;
        let pts: Vec<egui::Pos2> = samples
            .iter()
            .enumerate()
            .map(|(i, v)| egui::pos2(rect.left() + i as f32 * dx, rect.bottom() - (v / max) * (rect.height() - 8.0)))
            .collect();
        // Filled area under the curve, Steam download-graph style.
        let mut mesh = egui::Mesh::default();
        for (i, pt) in pts.iter().enumerate() {
            mesh.colored_vertex(*pt, ACCENT_STRONG.linear_multiply(0.45));
            mesh.colored_vertex(egui::pos2(pt.x, rect.bottom()), ACCENT_STRONG.linear_multiply(0.05));
            if i > 0 {
                let k = (i * 2) as u32;
                mesh.add_triangle(k - 2, k - 1, k);
                mesh.add_triangle(k - 1, k, k + 1);
            }
        }
        p.add(egui::Shape::mesh(mesh));
        p.add(egui::Shape::line(pts, Stroke::new(2.0_f32, ACCENT)));
    }
    let cur = samples.last().copied().unwrap_or(0.0);
    p.text(
        rect.left_top() + Vec2::new(10.0, 8.0),
        egui::Align2::LEFT_TOP,
        format!("COMPILING  {cur:.0} files/s   ·   {compiled} of ≈{est} files"),
        bold(12.0),
        TEXT,
    );
}

fn success_panel(
    ui: &mut Ui,
    job: &JobView,
    sys: &SystemInfo,
    go_library: &mut bool,
    save_preset: &mut bool,
    rebuild_profile: &mut Option<(String, PathBuf)>,
) {
    match &job.kind {
        JobKind::Build { release, autofdo_ready } => {
            egui::Frame::new()
                .fill(GREEN_B.linear_multiply(0.25))
                .stroke(Stroke::new(1.0_f32, GREEN.linear_multiply(0.6)))
                .corner_radius(CornerRadius::same(4))
                .inner_margin(Margin::same(16))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    let installed = job.install_reached();
                    ui.label(RichText::new(if installed { format!("🎉  {release} is installed") } else { format!("🎉  {release} is built") }).font(bold(20.0)).color(TEXT_BRIGHT));
                    ui.add_space(6.0);
                    if installed {
                        status_line(ui, 0, &format!("Reboot and pick “{release}” in the {} menu. GRUB usually makes the newest kernel the default.", sys.boot.bootloader.label()));
                        status_line(ui, 0, &format!("Your current kernel ({}) stays installed — choose it in the boot menu if anything misbehaves.", sys.running_kernel));
                        match job.modrebuild_ok {
                            Some(true) => status_line(ui, 0, "Driver modules (NVIDIA, …) were rebuilt for the new kernel."),
                            Some(false) => status_line(ui, 2, "Some driver modules failed to rebuild — don't boot the new kernel until that's fixed (see Messages)."),
                            None => {}
                        }
                        if *autofdo_ready {
                            status_line(ui, 0, "This kernel is profile-ready: after rebooting into it, open Library › “Optimize with AutoFDO”.");
                        }
                    } else {
                        status_line(ui, 0, "Build-only mode: nothing was installed.");
                    }
                    ui.add_space(8.0);
                    ui.horizontal(|ui| {
                        if button(ui, "Go to Library", Btn::Primary).clicked() {
                            *go_library = true;
                        }
                        if button(ui, "Save settings as preset", Btn::Secondary).clicked() {
                            *save_preset = true;
                        }
                    });
                });
        }
        JobKind::Profile { release } => {
            callout(ui, GREEN, "✔", "Profile recorded. Rebuild the kernel with it to apply the optimizations.");
            if let Some(p) = &job.profile_out {
                ui.add_space(6.0);
                if button(ui, "Rebuild with this profile", Btn::Primary).clicked() {
                    *rebuild_profile = Some((release.clone(), p.clone()));
                }
            }
        }
        JobKind::Uninstall { release } => callout(ui, GREEN, "✔", &format!("{release} was removed and the boot menu updated.")),
        JobKind::Sources => callout(ui, GREEN, "✔", "Kernel sources installed. Head back to Build to use them."),
        JobKind::Tuning => callout(ui, GREEN, "✔", "Runtime tuning updated."),
        JobKind::Portage => callout(ui, GREEN, "✔", "Portage integration saved. Your kernel package will now always be built with these settings."),
    }
}
