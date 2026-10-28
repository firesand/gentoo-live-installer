//! The guided build wizard.

use super::jobview::JobKind;
use super::modals::Modal;
use super::theme::*;
use super::{open_path, App, Load};
use eframe::egui::{self, Color32, CornerRadius, Margin, RichText, ScrollArea, Sense, Ui, Vec2};
use kernelsmith_core::hw::{InitSystem, Initramfs, SystemInfo};
use kernelsmith_core::job;
use kernelsmith_core::kconfig::SymbolIndex;
use kernelsmith_core::patches::{self, PatchStatus, PlannedPatch};
use kernelsmith_core::paths;
use kernelsmith_core::plan::{self, BuildPlan, Level, Seed};
use kernelsmith_core::profiles::{self, ProfileId};
use kernelsmith_core::script::{self, ScriptInputs};
use kernelsmith_core::slim::{self, SlimMode};
use kernelsmith_core::sources::{Flavor, KernelSource};
use kernelsmith_core::tweaks::{self, Ctx, Risk};
use kernelsmith_core::util::{disk_free, human_bytes};
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub const STEPS: [(&str, &str); 8] = [
    ("Your PC", "What Kernelsmith found"),
    ("Kernel source", "Where to start from"),
    ("Your goal", "What this PC is for"),
    ("Hardware fit", "Drivers to keep"),
    ("Optimizations", "Speed, latency, size"),
    ("Patches", "Extra features"),
    ("Install", "Name, boot & drivers"),
    ("Review & build", "Check and go"),
];

#[derive(Default)]
pub struct Wizard {
    pub step: usize,
    pub plan: Option<BuildPlan>,
    pub customized: bool,
    pub user_set: HashSet<String>,
    pub notes: Vec<String>,
    pub show_advanced: bool,
    pub expanded: HashSet<String>,
    pub seed_path: String,
    pub cachy_config: Option<Load<Result<PathBuf, String>>>,
}

impl Wizard {
    /// Start over with a given plan (e.g. a preset or an AutoFDO rebuild).
    pub fn load_plan(&mut self, plan: BuildPlan, step: usize) {
        self.plan = Some(plan);
        self.step = step;
        self.customized = true;
        self.user_set.clear();
        self.notes.clear();
    }
}

fn default_source(list: &[KernelSource]) -> Option<&KernelSource> {
    list.iter()
        .filter(|s| s.buildable && s.flavor != Flavor::Kernelsmith)
        .min_by_key(|s| (std::cmp::Reverse(s.version_tuple()), s.recommended_rank()))
}

fn has_cachyos_overlay() -> bool {
    Path::new("/var/db/repos/CachyOS-kernels/sys-kernel/cachyos-sources").is_dir()
}

impl App {
    pub(super) fn wizard(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>) {
        let ctx = ui.ctx().clone();
        let sources = self.sources.ready().cloned().unwrap_or_default();

        if self.wiz.plan.is_none() {
            if let Some(src) = default_source(&sources) {
                self.wiz.plan = Some(BuildPlan::new(sys, profiles::recommend(sys), src));
            }
        }
        let mut plan = self.wiz.plan.take();
        let src = plan.as_ref().and_then(|p| sources.iter().find(|s| s.path == p.source).cloned());
        let symbols = src.as_ref().and_then(|s| self.symbols_for(&ctx, &s.path));

        if let Some(p) = plan.as_mut() {
            let provided = p.provided_symbols();
            let notes = tweaks::normalize(&mut p.tweaks, sys, symbols.as_deref(), &provided);
            for n in notes {
                if !self.wiz.notes.contains(&n) {
                    self.wiz.notes.push(n);
                }
            }
        }

        egui::SidePanel::left("wiz-steps")
            .exact_width(250.0)
            .resizable(false)
            .frame(egui::Frame::new().fill(Color32::from_rgba_unmultiplied(0x10, 0x17, 0x20, 0xd0)).inner_margin(Margin::symmetric(14, 18)))
            .show_inside(ui, |ui| self.step_list(ui, plan.as_ref(), src.as_ref()));

        egui::TopBottomPanel::bottom("wiz-actions")
            .exact_height(64.0)
            .frame(egui::Frame::new().fill(Color32::from_rgba_unmultiplied(0x10, 0x17, 0x20, 0xe0)).inner_margin(Margin::symmetric(24, 10)))
            .show_inside(ui, |ui| self.action_bar(ui, sys, plan.as_mut(), src.as_ref(), symbols.as_deref()));

        egui::CentralPanel::default()
            .frame(egui::Frame::new().inner_margin(Margin { left: 30, right: 30, top: 22, bottom: 10 }))
            .show_inside(ui, |ui| {
                let (title, sub) = STEPS[self.wiz.step];
                heading(ui, title);
                subheading(ui, step_blurb(self.wiz.step, sub));
                ui.add_space(10.0);
                ScrollArea::vertical().id_salt(("wiz", self.wiz.step)).auto_shrink([false, false]).show(ui, |ui| {
                    ui.set_max_width(ui.available_width().min(1100.0));
                    match (self.wiz.step, plan.as_mut()) {
                        (0, _) => self.step_pc(ui, sys, &sources),
                        (1, _) => self.step_source(ui, sys, &sources, plan.as_mut()),
                        (_, None) => {
                            callout(ui, WARN, "!", "Pick a kernel source first.");
                        }
                        (2, Some(p)) => self.step_goal(ui, sys, p),
                        (3, Some(p)) => self.step_hardware(ui, sys, p, src.as_ref()),
                        (4, Some(p)) => self.step_tweaks(ui, sys, p, symbols.as_deref()),
                        (5, Some(p)) => self.step_patches(ui, sys, p, src.as_ref()),
                        (6, Some(p)) => self.step_install(ui, sys, p, src.as_ref()),
                        (_, Some(p)) => self.step_review(ui, sys, p, src.as_ref(), symbols.as_deref()),
                    }
                    ui.add_space(30.0);
                });
            });
        self.wiz.plan = plan;
    }

    fn step_list(&mut self, ui: &mut Ui, plan: Option<&BuildPlan>, src: Option<&KernelSource>) {
        ui.label(RichText::new("BUILD A KERNEL").font(bold(13.0)).color(TEXT_DIM));
        ui.add_space(10.0);
        for (i, (title, sub)) in STEPS.iter().enumerate() {
            let current = i == self.wiz.step;
            let reachable = i <= 1 || plan.is_some();
            let (rect, resp) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 52.0), if reachable { Sense::click() } else { Sense::hover() });
            let p = ui.painter();
            if current {
                hgradient(p, rect, ACCENT_STRONG.linear_multiply(0.35), Color32::TRANSPARENT);
                p.rect_filled(egui::Rect::from_min_size(rect.min, Vec2::new(3.0, rect.height())), CornerRadius::ZERO, ACCENT);
            } else if resp.hovered() && reachable {
                p.rect_filled(rect, CornerRadius::same(3), Color32::from_white_alpha(8));
            }
            let c = egui::pos2(rect.left() + 22.0, rect.center().y);
            let done = i < self.wiz.step;
            let (circle, fg) = if current {
                (ACCENT_STRONG, Color32::WHITE)
            } else if done {
                (GREEN_B, Color32::WHITE)
            } else {
                (Color32::from_rgb(0x2a, 0x3a, 0x4f), TEXT_DIM)
            };
            p.circle_filled(c, 13.0, circle);
            let mark = if done { "✔".to_string() } else { (i + 1).to_string() };
            p.text(c, egui::Align2::CENTER_CENTER, mark, bold(13.0), fg);
            let tc = if current { TEXT_BRIGHT } else if reachable { TEXT } else { TEXT_DIM };
            p.text(egui::pos2(rect.left() + 46.0, rect.center().y - 9.0), egui::Align2::LEFT_CENTER, *title, bold(14.5), tc);
            p.text(egui::pos2(rect.left() + 46.0, rect.center().y + 10.0), egui::Align2::LEFT_CENTER, *sub, regular(12.0), TEXT_DIM);
            if resp.clicked() && reachable {
                self.wiz.step = i;
            }
        }
        ui.add_space(16.0);
        if let (Some(p), Some(s)) = (plan, src) {
            egui::Frame::new().fill(PANEL).corner_radius(CornerRadius::same(4)).inner_margin(Margin::same(12)).show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.label(RichText::new("YOUR KERNEL").font(bold(11.5)).color(TEXT_DIM));
                ui.label(RichText::new(p.release(s)).font(bold(14.0)).color(ACCENT));
                let pr = profiles::get(p.profile);
                ui.label(RichText::new(format!("{} {}", pr.icon, pr.title)).color(TEXT));
                ui.label(RichText::new(format!("{} · {}", s.flavor.label(), p.slim.label())).color(TEXT_DIM).size(12.5));
            });
        }
    }

    fn action_bar(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>, plan: Option<&mut BuildPlan>, src: Option<&KernelSource>, symbols: Option<&SymbolIndex>) {
        ui.horizontal_centered(|ui| {
            if self.wiz.step > 0 && button(ui, "◀  Back", Btn::Secondary).clicked() {
                self.wiz.step -= 1;
            }
            if let (Some(p), Some(_)) = (plan.as_ref(), src) {
                ui.add_space(14.0);
                let est = plan::estimate(p, sys);
                dim(ui, &format!("⏱ about {}–{} min   💾 ~{}", est.minutes_low, est.minutes_high, human_bytes(est.disk_bytes)));
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let last = STEPS.len() - 1;
                if self.wiz.step < last {
                    let can = self.wiz.step == 0 || plan.is_some();
                    if button_enabled(ui, "Next  ▶", Btn::Blue, can).clicked() {
                        self.wiz.step += 1;
                    }
                } else if let (Some(p), Some(s)) = (plan, src) {
                    let findings = plan::check(p, sys, Some(s));
                    let blocked = findings.iter().any(|f| f.level == Level::Error);
                    let label = if p.install.install { "BUILD & INSTALL" } else { "BUILD" };
                    let r = button_sized(ui, label, Btn::Primary, !blocked, 44.0);
                    if blocked {
                        r.on_hover_text("Fix the problems listed above first.");
                    } else if r.clicked() {
                        match job::prepare_build(p, sys, s, symbols) {
                            Ok(j) => {
                                let release = j.release.clone().unwrap_or_default();
                                let needs_root = p.needs_root();
                                let afdo = p.tweaks.get("autofdo").is_some_and(|v| v != "off");
                                self.start_job(j, JobKind::Build { release, autofdo_ready: afdo }, needs_root);
                            }
                            Err(e) => self.modal = Some(Modal::message("Couldn't prepare the build", format!("{e:#}"))),
                        }
                    }
                }
            });
        });
    }

    // ───────────────────────────── Step 0 ─────────────────────────────
    fn step_pc(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>, sources: &[KernelSource]) {
        ui.columns(2, |cols| {
            panel(&mut cols[0], |ui| {
                ui.set_width(ui.available_width());
                title(ui, "This computer");
                ui.add_space(4.0);
                egui::Grid::new("pc").num_columns(2).spacing([16.0, 6.0]).show(ui, |ui| {
                    kv(ui, "Processor", format!("{} ({} cores / {} threads)", sys.cpu.model_name, sys.cpu.cores, sys.cpu.threads));
                    kv(ui, "Graphics", sys.primary_gpu_name());
                    kv(ui, "Memory", format!("{:.1} GiB", sys.memory_gib()));
                    kv(ui, "Root disk", format!("{} ({})", sys.root_device, sys.root_fs));
                    kv(ui, "Init system", sys.init.label());
                    kv(ui, "Boot", format!("{} + {}", sys.boot.bootloader.label(), if sys.boot.initramfs == Initramfs::None { "no initramfs".into() } else { format!("{} initramfs", sys.boot.initramfs.label()) }));
                    kv(ui, "Running kernel", &sys.running_kernel);
                });
            });
            panel(&mut cols[1], |ui| {
                ui.set_width(ui.available_width());
                title(ui, "Ready to build?");
                ui.add_space(4.0);
                readiness(ui, sys, sources);
            });
        });
        ui.add_space(10.0);
        ui.horizontal(|ui| {
            if button(ui, "↻  Rescan hardware", Btn::Secondary).clicked() {
                let ctx = ui.ctx().clone();
                self.rescan_system(&ctx);
            }
            if button(ui, "See all hardware details", Btn::Secondary).clicked() {
                self.page = super::Page::Hardware;
            }
        });
        ui.add_space(12.0);
        callout(
            ui,
            ACCENT,
            "ℹ",
            "Nothing is changed on your system until you press Build on the last step. Your current kernel stays installed, \
             so you can always pick it from the boot menu if anything goes wrong.",
        );
    }

    // ───────────────────────────── Step 1 ─────────────────────────────
    fn step_source(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>, sources: &[KernelSource], plan: Option<&mut BuildPlan>) {
        let ctx = ui.ctx().clone();
        if self.sources.is_loading() {
            ui.add(egui::Spinner::new().color(ACCENT));
            return;
        }
        let buildable: Vec<&KernelSource> = sources.iter().filter(|s| s.buildable && s.flavor != Flavor::Kernelsmith).collect();
        if buildable.is_empty() {
            callout(
                ui,
                WARN,
                "!",
                "No full kernel source tree was found in /usr/src. Trees ending in “-dist” only contain headers that Portage \
                 installs for building driver modules — they can't build a kernel. Install a source package below; Kernelsmith \
                 never modifies it (it works on a copy).",
            );
            ui.add_space(8.0);
        }
        let selected = plan.as_ref().map(|p| p.source.clone());
        let best = default_source(sources).map(|s| s.path.clone());
        let mut choose: Option<KernelSource> = None;
        for s in &buildable {
            let sel = selected.as_ref() == Some(&s.path);
            let (resp, _) = card(ui, ("src", &s.path), sel, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    ui.label(RichText::new(s.display()).font(bold(22.0)).color(TEXT_BRIGHT));
                    ui.add_space(8.0);
                    badge(ui, s.flavor.label(), ACCENT);
                    if best.as_ref() == Some(&s.path) {
                        badge(ui, "Recommended", GREEN);
                    }
                    if s.is_current_symlink {
                        badge(ui, "/usr/src/linux", TEXT_DIM);
                    }
                    if let Some(idx) = self.symbols.get(&s.path).and_then(|l| l.ready()) {
                        if idx.has("SCHED_BORE") {
                            badge(ui, "BORE", PURPLE);
                        }
                        if idx.has("NTSYNC") {
                            badge(ui, "NTSYNC", PURPLE);
                        }
                    }
                });
                dim(ui, &format!("{}  —  {}", s.path.display(), s.flavor.blurb()));
            });
            if resp.clicked() && !sel {
                choose = Some((*s).clone());
            }
            ui.add_space(6.0);
        }
        if let Some(s) = choose {
            match plan {
                Some(p) => {
                    let old_mm = crate::gui::wizard::mm_of(&p.source, sources);
                    p.source = s.path.clone();
                    if old_mm != s.major_minor() && p.patches.iter().any(|x| !x.id.starts_with("local:")) {
                        p.patches.retain(|x| x.id.starts_with("local:"));
                        self.toast("Catalog patches were removed because they belong to another kernel version.", WARN);
                    }
                }
                None => self.wiz.plan = Some(BuildPlan::new(sys, profiles::recommend(sys), &s)),
            }
            self.symbols_for(&ctx, &s.path);
        }

        let unusable: Vec<&KernelSource> = sources.iter().filter(|s| !s.buildable).collect();
        if !unusable.is_empty() {
            ui.add_space(4.0);
            egui::CollapsingHeader::new(RichText::new(format!("{} headers-only tree(s) can't be used", unusable.len())).color(TEXT_DIM))
                .id_salt("unusable")
                .show(ui, |ui| {
                    for s in unusable {
                        dim(ui, &format!("• {} — {}", s.dir_name, s.flavor.blurb()));
                    }
                });
        }

        ui.add_space(10.0);
        section(ui, "Get kernel sources");
        let mut list: Vec<(&str, &str, &str)> = kernelsmith_core::sources::source_packages();
        if !has_cachyos_overlay() {
            list.retain(|(a, _, _)| *a != "sys-kernel/cachyos-sources");
        }
        let installed: Vec<String> = sys.source_packages.iter().map(|p| p.atom.clone()).collect();
        for (atom, desc, repo) in list {
            panel(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    ui.vertical(|ui| {
                        ui.label(RichText::new(atom).font(bold(15.0)).color(TEXT_BRIGHT));
                        dim(ui, &format!("{desc}  ({repo})"));
                    });
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if installed.iter().any(|a| a == atom) {
                            badge(ui, "Installed", GREEN);
                        } else if button(ui, "Install", Btn::Blue).clicked() {
                            match job::install_sources(atom) {
                                Ok(j) => self.start_job(j, JobKind::Sources, true),
                                Err(e) => self.modal = Some(Modal::message("Couldn't prepare", format!("{e:#}"))),
                            }
                        }
                        if ui.small_button("Copy command").clicked() {
                            ui.ctx().copy_text(format!("emerge --ask {atom}"));
                            self.toast("Command copied", ACCENT);
                        }
                    });
                });
            });
            ui.add_space(4.0);
        }
        if !has_cachyos_overlay() {
            dim(ui, "Tip: for CachyOS sources, add the CachyOS-kernels overlay first: eselect repository enable CachyOS-kernels && emaint sync -r CachyOS-kernels");
        }
    }

    // ───────────────────────────── Step 2 ─────────────────────────────
    fn step_goal(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>, plan: &mut BuildPlan) {
        let rec = profiles::recommend(sys);
        let w = ((ui.available_width() - 2.0 * 14.0) / 3.0).max(240.0);
        let mut pick: Option<ProfileId> = None;
        for row in profiles::ALL.chunks(3) {
            ui.horizontal_top(|ui| {
                ui.spacing_mut().item_spacing.x = 14.0;
                for id in row {
                    let pr = profiles::get(*id);
                    ui.allocate_ui(Vec2::new(w, 330.0), |ui| {
                        ui.set_width(w);
                        let (resp, _) = card(ui, ("profile", *id as u8), plan.profile == *id, |ui| {
                            ui.set_width(w - 30.0);
                            ui.set_min_height(290.0);
                            let (rect, _) = ui.allocate_exact_size(Vec2::new(w - 30.0, 96.0), Sense::hover());
                            let (a, b) = profile_colors(*id);
                            icon_capsule(ui, rect, pr.icon, a, b);
                            if *id == rec {
                                let r = egui::Rect::from_min_size(rect.left_top() + Vec2::new(8.0, 8.0), Vec2::new(118.0, 20.0));
                                ui.painter().rect_filled(r, CornerRadius::same(2), GREEN_B);
                                ui.painter().text(r.center(), egui::Align2::CENTER_CENTER, "RECOMMENDED", bold(11.0), Color32::WHITE);
                            }
                            ui.add_space(4.0);
                            ui.label(RichText::new(pr.title).font(bold(18.0)).color(TEXT_BRIGHT));
                            ui.label(RichText::new(pr.tagline).color(ACCENT));
                            ui.add(egui::Label::new(RichText::new(pr.description).color(TEXT_DIM).size(13.0)).wrap());
                            ui.horizontal_wrapped(|ui| {
                                for h in pr.highlights {
                                    tag(ui, h);
                                }
                            });
                            dim(ui, &format!("Similar to: {}", pr.similar_to));
                        });
                        if resp.clicked() && plan.profile != *id {
                            pick = Some(*id);
                        }
                    });
                }
            });
            ui.add_space(14.0);
        }
        if let Some(id) = pick {
            if self.wiz.customized {
                self.modal = Some(Modal::ConfirmProfile(id));
            } else {
                apply_profile(&mut self.wiz, plan, sys, id);
            }
        }
    }

    // ───────────────────────────── Step 3 ─────────────────────────────
    fn step_hardware(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>, plan: &mut BuildPlan, src: Option<&KernelSource>) {
        section(ui, "How much should be trimmed?");
        let modes = [SlimMode::Balanced, SlimMode::Aggressive, SlimMode::Off];
        let w = ((ui.available_width() - 28.0) / 3.0).max(220.0);
        ui.horizontal_top(|ui| {
            ui.spacing_mut().item_spacing.x = 14.0;
            for m in modes {
                ui.allocate_ui(Vec2::new(w, 170.0), |ui| {
                    ui.set_width(w);
                    let (resp, _) = card(ui, ("slim", m as u8), plan.slim == m, |ui| {
                        ui.set_width(w - 30.0);
                        ui.set_min_height(130.0);
                        ui.horizontal(|ui| {
                            ui.label(RichText::new(m.label()).font(bold(16.0)).color(TEXT_BRIGHT));
                            if m == SlimMode::Balanced {
                                badge(ui, "Best", GREEN);
                            }
                        });
                        ui.add(egui::Label::new(RichText::new(m.explain()).color(TEXT_DIM).size(13.0)).wrap());
                    });
                    if resp.clicked() && plan.slim != m {
                        plan.slim = m;
                        plan.keep = slim::default_keep(sys, plan.profile, m);
                        self.wiz.customized = true;
                    }
                });
            }
        });

        if plan.slim != SlimMode::Off {
            ui.add_space(8.0);
            callout(
                ui,
                ACCENT,
                "🔌",
                &format!(
                    "Plug in every device you use now — controllers, headsets, webcams, USB drives — then press Rescan. \
                     Kernelsmith keeps drivers for everything currently in use ({} modules loaded) plus the families you tick below.",
                    sys.loaded_modules.len()
                ),
            );
            ui.horizontal(|ui| {
                if button(ui, "↻  Rescan devices", Btn::Secondary).clicked() {
                    let ctx = ui.ctx().clone();
                    self.rescan_system(&ctx);
                    self.toast("Rescanning devices…", ACCENT);
                }
                if sys.modprobed_db.is_some() {
                    ui.add_space(10.0);
                    switch(ui, &mut plan.use_modprobed_db, true);
                    ui.label("Also keep everything modprobed-db has seen");
                } else {
                    dim(ui, "Tip: sys-kernel/modprobed-db remembers devices you used over weeks — even safer trimming.");
                }
            });
            ui.add_space(8.0);
            section(ui, "Always keep these device families");
            let cats = slim::categories();
            let colw = (ui.available_width() - 14.0) / 2.0;
            for pair in cats.chunks(2) {
                ui.horizontal_top(|ui| {
                    ui.spacing_mut().item_spacing.x = 14.0;
                    for c in pair {
                        ui.allocate_ui(Vec2::new(colw, 70.0), |ui| {
                            ui.set_width(colw);
                            panel(ui, |ui| {
                                ui.set_width(colw - 30.0);
                                ui.horizontal(|ui| {
                                    let mut on = plan.keep.iter().any(|k| k == c.id);
                                    if switch(ui, &mut on, true).changed() {
                                        if on {
                                            plan.keep.push(c.id.to_string());
                                        } else {
                                            plan.keep.retain(|k| k != c.id);
                                        }
                                        self.wiz.customized = true;
                                    }
                                    ui.label(RichText::new(c.icon).size(18.0));
                                    ui.vertical(|ui| {
                                        ui.label(RichText::new(c.title).font(bold(14.0)).color(TEXT_BRIGHT));
                                        ui.add(egui::Label::new(RichText::new(c.desc).color(TEXT_DIM).size(12.5)).wrap());
                                    });
                                });
                            });
                        });
                    }
                });
                ui.add_space(6.0);
            }
        }

        ui.add_space(8.0);
        section(ui, "Start from which configuration?");
        let mut seed = plan.seed.clone();
        let opts: Vec<(Seed, &str, String, bool)> = vec![
            (Seed::Running, "Your current kernel (recommended)", format!("The config of {} — already known to boot this PC.", sys.running_kernel), sys.proc_config),
            (Seed::TreeConfig, "The source tree's .config", "Use a .config you prepared yourself in the source tree.".into(), src.is_some_and(|s| s.has_config)),
            (Seed::Defconfig, "Upstream defaults", "Minimal upstream defconfig. Only for experts — may lack drivers your PC needs.".into(), true),
        ];
        for (s, label, desc, ok) in opts {
            ui.horizontal(|ui| {
                ui.add_enabled_ui(ok, |ui| {
                    if ui.radio(seed == s, RichText::new(label).color(TEXT_BRIGHT)).clicked() {
                        seed = s.clone();
                    }
                });
                dim(ui, &desc);
            });
        }
        ui.horizontal(|ui| {
            let is_file = matches!(seed, Seed::File(_));
            if ui.radio(is_file, RichText::new("A config file").color(TEXT_BRIGHT)).clicked() && !self.wiz.seed_path.is_empty() {
                seed = Seed::File(PathBuf::from(self.wiz.seed_path.trim()));
            }
            let r = ui.add(egui::TextEdit::singleline(&mut self.wiz.seed_path).hint_text("/path/to/config").desired_width(320.0));
            if r.changed() && !self.wiz.seed_path.trim().is_empty() {
                seed = Seed::File(PathBuf::from(self.wiz.seed_path.trim()));
            }
            let busy = self.wiz.cachy_config.as_ref().is_some_and(|l| l.is_loading());
            if button_enabled(ui, "Use CachyOS's config", Btn::Secondary, !busy && !self.settings.offline)
                .on_hover_text("Download the tuned config CachyOS ships with linux-cachyos")
                .clicked()
            {
                let ctx = ui.ctx().clone();
                self.wiz.cachy_config = Some(Load::spawn(&ctx, download_cachy_config));
            }
            if busy {
                ui.add(egui::Spinner::new().color(ACCENT));
            }
        });
        if let Some(l) = self.wiz.cachy_config.as_mut() {
            if l.poll() {
                match l.ready() {
                    Some(Ok(p)) => {
                        self.wiz.seed_path = p.display().to_string();
                        seed = Seed::File(p.clone());
                        self.toasts.push(super::Toast { text: "CachyOS config downloaded".into(), color: GREEN, until: std::time::Instant::now() + std::time::Duration::from_secs(4) });
                    }
                    Some(Err(e)) => self.modal = Some(Modal::message("Download failed", e.clone())),
                    None => {}
                }
            }
        }
        if seed != plan.seed {
            plan.seed = seed;
            self.wiz.customized = true;
        }

        ui.add_space(8.0);
        section(ui, "Init system support");
        ui.horizontal(|ui| {
            for (init, label) in [(InitSystem::OpenRc, "OpenRC"), (InitSystem::Systemd, "systemd")] {
                let mut on = plan.inits.contains(&init);
                let detected = sys.init == init;
                if ui.checkbox(&mut on, RichText::new(if detected { format!("{label} (detected)") } else { label.to_string() }).color(TEXT_BRIGHT)).changed() {
                    if on {
                        plan.inits.push(init);
                    } else if plan.inits.len() > 1 || !detected {
                        plan.inits.retain(|i| *i != init);
                    }
                }
                ui.add_space(16.0);
            }
        });
        dim(ui, "Supporting both costs almost nothing and lets you switch init systems later without rebuilding.");
    }

    // ───────────────────────────── Step 4 ─────────────────────────────
    fn step_tweaks(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>, plan: &mut BuildPlan, symbols: Option<&SymbolIndex>) {
        let defaults = profiles::defaults(plan.profile, sys);
        ui.horizontal(|ui| {
            if button(ui, "Reset to profile defaults", Btn::Secondary).clicked() {
                plan.tweaks = defaults.clone();
                self.wiz.user_set.clear();
                self.wiz.notes.clear();
            }
            ui.add_space(12.0);
            if switch(ui, &mut self.wiz.show_advanced, true).changed() {
                self.settings.show_advanced = self.wiz.show_advanced;
                self.settings.save();
            }
            ui.label("Show expert options");
            if symbols.is_none() {
                ui.add_space(12.0);
                ui.add(egui::Spinner::new().color(ACCENT));
                dim(ui, "Checking which options this source supports…");
            }
        });
        for n in &self.wiz.notes {
            callout(ui, ACCENT, "ℹ", n);
        }
        ui.add_space(4.0);
        let provided = plan.provided_symbols();
        let mut cats: Vec<tweaks::Category> = tweaks::catalog().iter().map(|t| t.category).collect();
        cats.dedup();
        for cat in cats {
            let items: Vec<&tweaks::Tweak> = tweaks::catalog()
                .iter()
                .filter(|t| t.category == cat && (self.wiz.show_advanced || !t.advanced))
                .collect();
            if items.is_empty() {
                continue;
            }
            section(ui, &format!("{}  {}", cat.icon(), cat.label()));
            for t in items {
                let snapshot = plan.tweaks.clone();
                let ctx = Ctx { sys, symbols, provided: &provided, selection: &snapshot };
                let mut sel = plan.tweaks.get(t.id).cloned().unwrap_or_default();
                let def = defaults.get(t.id).cloned().unwrap_or_default();
                let options: Vec<(String, String, Result<(), String>, bool)> = t
                    .choices
                    .iter()
                    .map(|c| (c.id.to_string(), c.label.to_string(), (c.avail)(&ctx), c.id == def))
                    .collect();
                let choice = t.choice(&sel);
                panel(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(t.title).font(bold(15.5)).color(TEXT_BRIGHT));
                        if let Some(c) = choice {
                            match c.risk {
                                Risk::Safe => {}
                                Risk::Moderate => {
                                    badge(ui, c.risk.label(), WARN);
                                }
                                Risk::Advanced => {
                                    badge(ui, c.risk.label(), DANGER);
                                }
                            }
                        }
                        if sel != def && !def.is_empty() {
                            badge(ui, "Changed", ACCENT);
                        }
                        if t.advanced {
                            badge(ui, "Expert", PURPLE);
                        }
                    });
                    dim(ui, t.summary);
                    ui.add_space(4.0);
                    if segmented(ui, &options, &mut sel) {
                        plan.tweaks.insert(t.id.to_string(), sel.clone());
                        self.wiz.user_set.insert(t.id.to_string());
                        self.wiz.customized = true;
                    }
                    if let Some(c) = t.choice(&sel) {
                        ui.label(RichText::new(c.desc).color(TEXT).size(13.0));
                    }
                    let open = self.wiz.expanded.contains(t.id);
                    if ui.add(egui::Label::new(RichText::new(if open { "- Less" } else { "+ What does this mean?" }).color(ACCENT).size(13.0)).sense(Sense::click())).clicked() {
                        if open {
                            self.wiz.expanded.remove(t.id);
                        } else {
                            self.wiz.expanded.insert(t.id.to_string());
                        }
                    }
                    if open {
                        ui.add(egui::Label::new(RichText::new(t.explain).color(TEXT).size(13.5)).wrap());
                        ui.add_space(2.0);
                        for c in &t.choices {
                            let avail = (c.avail)(&ctx);
                            let line = match &avail {
                                Ok(()) => format!("• {} — {}", c.label, c.desc),
                                Err(why) => format!("• {} — {} (unavailable: {why})", c.label, c.desc),
                            };
                            ui.label(RichText::new(line).color(if avail.is_ok() { TEXT_DIM } else { TEXT_DIM.gamma_multiply(0.7) }).size(12.5));
                        }
                    }
                });
                ui.add_space(6.0);
            }
        }
    }

    // ───────────────────────────── Step 5 ─────────────────────────────
    fn step_patches(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>, plan: &mut BuildPlan, src: Option<&KernelSource>) {
        let Some(src) = src else { return };
        let ctx = ui.ctx().clone();
        dim(ui, &format!("Patches add features that aren't in the {} source yet. Each one is test-applied before your build starts.", src.display()));
        ui.add_space(6.0);
        let statuses = self.patch_statuses(&ctx, src);
        if statuses.is_none() {
            ui.horizontal(|ui| {
                ui.add(egui::Spinner::new().color(ACCENT));
                dim(ui, "Looking up patches for this kernel version…");
            });
        }
        let statuses = statuses.unwrap_or_default();
        let mut catalog = patches::catalog();
        catalog.sort_by_key(|p| !(p.recommend)(sys, plan.profile));
        for info in catalog {
            let status = statuses.iter().find(|(id, _)| id == info.id).map(|(_, s)| s.clone());
            self.patch_card(ui, &ctx, sys, plan, src, &info, status);
            ui.add_space(6.0);
        }

        section(ui, "Your own patches");
        if self.local_patches.is_empty() {
            dim(ui, &format!("Drop .patch files into {} (or import one from the Patches page) and they'll appear here.", paths::user_patch_dir().display()));
        }
        for lp in self.local_patches.clone() {
            let id = format!("local:{}", lp.name);
            let mut on = plan.patches.iter().any(|p| p.id == id);
            ui.horizontal(|ui| {
                if switch(ui, &mut on, true).changed() {
                    if on {
                        plan.patches.push(PlannedPatch { id: id.clone(), title: lp.name.clone(), file: lp.path.clone(), url: None });
                    } else {
                        plan.patches.retain(|p| p.id != id);
                    }
                }
                ui.label(RichText::new(&lp.name).color(TEXT_BRIGHT));
                dim(ui, &human_bytes(lp.size));
                self.dry_run_button(ui, &ctx, &id, src, &lp.path);
            });
        }
        ui.horizontal(|ui| {
            if button(ui, "Open patches folder", Btn::Secondary).clicked() {
                let _ = std::fs::create_dir_all(paths::user_patch_dir());
                open_path(&paths::user_patch_dir());
            }
            if button(ui, "↻ Refresh", Btn::Secondary).clicked() {
                self.local_patches = patches::local_patches();
                self.patch_status.remove(&src.path);
            }
        });
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) fn patch_card(
        &mut self,
        ui: &mut Ui,
        ctx: &egui::Context,
        sys: &SystemInfo,
        plan: &mut BuildPlan,
        src: &KernelSource,
        info: &patches::PatchInfo,
        status: Option<PatchStatus>,
    ) {
        let added = plan.patches.iter().any(|p| p.id == info.id);
        let recommended = (info.recommend)(sys, plan.profile);
        let key = format!("{}@{}", info.id, src.path.display());
        if let Some(dl) = self.patch_downloads.get_mut(&key) {
            if dl.poll() {
                match dl.ready() {
                    Some(Ok(pp)) => {
                        if !plan.patches.iter().any(|p| p.id == pp.id) {
                            plan.patches.push(pp.clone());
                        }
                        reapply_defaults(&mut self.wiz, plan, sys);
                    }
                    Some(Err(e)) => self.modal = Some(Modal::message("Download failed", e.clone())),
                    None => {}
                }
            }
        }
        let downloading = self.patch_downloads.get(&key).is_some_and(|l| l.is_loading());
        panel(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                ui.label(RichText::new(info.icon).size(22.0));
                ui.vertical(|ui| {
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(info.title).font(bold(15.5)).color(TEXT_BRIGHT));
                        tag(ui, info.category.label());
                        if recommended {
                            badge(ui, "Recommended", GREEN);
                        }
                        match &status {
                            Some(PatchStatus::Included) => {
                                badge(ui, "Already included", GREEN);
                            }
                            Some(PatchStatus::Available { .. }) => {
                                badge(ui, "Available", ACCENT);
                            }
                            Some(PatchStatus::NotPublished(_)) => {
                                badge(ui, "Not for this version", TEXT_DIM);
                            }
                            Some(PatchStatus::Error(_)) => {
                                badge(ui, "Offline?", WARN);
                            }
                            None => {}
                        }
                        if added {
                            badge(ui, "In your build", ACCENT_STRONG);
                        }
                    });
                    dim(ui, info.summary);
                });
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if let Some(PatchStatus::Available { url, file_name }) = &status {
                        if added {
                            if button(ui, "Remove", Btn::Secondary).clicked() {
                                plan.patches.retain(|p| p.id != info.id);
                            }
                            if let Some(pp) = plan.patches.iter().find(|p| p.id == info.id).cloned() {
                                self.dry_run_button(ui, ctx, info.id, src, &pp.file);
                            }
                        } else if downloading {
                            ui.add(egui::Spinner::new().color(ACCENT));
                        } else if button(ui, "+ Add", Btn::Primary).clicked() {
                            let fetcher = self.fetcher();
                            let (url, file_name, mm) = (url.clone(), file_name.clone(), src.major_minor());
                            let (id, title) = (info.id.to_string(), info.title.to_string());
                            self.patch_downloads.insert(
                                key.clone(),
                                Load::spawn(ctx, move || {
                                    fetcher
                                        .download(&url, &mm, &file_name)
                                        .map(|file| PlannedPatch { id, title, file, url: Some(url) })
                                        .map_err(|e| format!("{e:#}"))
                                }),
                            );
                        }
                    }
                });
            });
            let open_id = format!("patch-{}", info.id);
            let open = self.wiz.expanded.contains(&open_id);
            if ui.add(egui::Label::new(RichText::new(if open { "- Less" } else { "+ Details" }).color(ACCENT).size(13.0)).sense(Sense::click())).clicked() {
                if open {
                    self.wiz.expanded.remove(&open_id);
                } else {
                    self.wiz.expanded.insert(open_id);
                }
            }
            if open {
                ui.add(egui::Label::new(RichText::new(info.explain).color(TEXT).size(13.5)).wrap());
                match &status {
                    Some(PatchStatus::NotPublished(why)) | Some(PatchStatus::Error(why)) => {
                        ui.add(egui::Label::new(RichText::new(why).color(WARN).size(13.0)).wrap());
                    }
                    Some(PatchStatus::Available { url, .. }) => {
                        dim(ui, &format!("Source: {url}"));
                    }
                    _ => {}
                }
                ui.hyperlink_to(RichText::new("Project homepage").size(13.0), info.homepage);
            }
        });
    }

    pub(super) fn dry_run_button(&mut self, ui: &mut Ui, ctx: &egui::Context, id: &str, src: &KernelSource, file: &Path) {
        let key = format!("{id}@{}", src.path.display());
        let entry = self.dry_runs.get_mut(&key);
        match entry {
            Some(l) => {
                l.poll();
                match l.ready() {
                    Some((true, _)) => {
                        badge(ui, "Applies cleanly", GREEN);
                    }
                    Some((false, out)) => {
                        badge(ui, "Doesn't apply", DANGER).on_hover_text(out.chars().take(1500).collect::<String>());
                    }
                    None => {
                        ui.add(egui::Spinner::new().color(ACCENT));
                    }
                }
            }
            None => {
                if ui.small_button("Test").on_hover_text("Check that the patch applies to this source (read-only)").clicked() {
                    let (tree, f) = (src.path.clone(), file.to_path_buf());
                    self.dry_runs.insert(key, Load::spawn(ctx, move || patches::dry_run(&tree, &f)));
                }
            }
        }
    }

    // ───────────────────────────── Step 6 ─────────────────────────────
    fn step_install(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>, plan: &mut BuildPlan, src: Option<&KernelSource>) {
        let Some(src) = src else { return };
        section(ui, "Name");
        ui.horizontal(|ui| {
            ui.label(RichText::new("Kernel name").color(TEXT_BRIGHT));
            let r = ui.add(egui::TextEdit::singleline(&mut plan.name).desired_width(220.0).hint_text("gaming"));
            if r.changed() {
                // Keep it valid for LOCALVERSION while still letting people type dashes.
                plan.name = plan.name.to_lowercase().chars().filter(|c| c.is_ascii_alphanumeric() || *c == '-').take(24).collect();
            }
            ui.add_space(10.0);
            dim(ui, "›");
            ui.label(RichText::new(plan.release(src)).font(bold(15.0)).color(ACCENT));
        });
        dim(ui, &format!("Shown by `uname -r` and in your boot menu. Build tree: {}", plan.dest_dir(src).display()));

        ui.add_space(8.0);
        section(ui, "Install");
        let boot_desc = format!(
            "Copies the kernel to /boot{} and updates {} using Gentoo's installkernel — the same path `make install` takes.",
            if sys.boot.initramfs != Initramfs::None { format!(", generates a {} initramfs", sys.boot.initramfs.label()) } else { String::new() },
            sys.boot.bootloader.label()
        );
        option_row(ui, &mut plan.install.install, true, "Install the kernel and update the boot menu", &boot_desc);
        let has_ext = !sys.external_modules.is_empty();
        let ext_names: Vec<String> = sys.external_modules.iter().map(|p| p.atom.rsplit('/').next().unwrap_or(&p.atom).to_string()).collect();
        option_row(
            ui,
            &mut plan.install.module_rebuild,
            plan.install.install && has_ext,
            "Rebuild driver modules for the new kernel",
            &if has_ext {
                format!("Runs `emerge @module-rebuild` for: {}. Without this, these drivers won't load on the new kernel.", ext_names.join(", "))
            } else {
                "No out-of-tree drivers (like NVIDIA) are installed, so nothing needs rebuilding.".into()
            },
        );
        option_row(
            ui,
            &mut plan.install.preserve_old_modules,
            plan.install.install && plan.install.module_rebuild,
            "Keep those drivers working on your other kernels",
            "Portage would otherwise remove e.g. the NVIDIA module from your current kernel when it rebuilds it for the new one. Kernelsmith saves and restores them so you can always boot back.",
        );
        option_row(
            ui,
            &mut plan.install.set_symlink,
            plan.install.install,
            "Make it the default source tree (/usr/src/linux)",
            "Future driver updates from Portage (e.g. a new NVIDIA version) will then build for this kernel. Undone automatically when you uninstall.",
        );
        option_row(ui, &mut plan.install.clean_after, true, "Free disk space after building", "Runs `make clean` (keeps what's needed to build driver modules later). Saves several GB.");
        ui.horizontal(|ui| {
            option_row_inline(ui, &mut plan.install.runtime_tuning, true, "Install the runtime tuning pack", "sysctl + udev settings: smarter I/O schedulers per disk, larger file caches, no NMI watchdog, higher map count for games. Works with OpenRC and systemd; removable from the Library.");
            if ui.small_button("Preview").clicked() {
                let text = format!(
                    "# {}\n{}\n# {}\n{}",
                    kernelsmith_core::tuning::SYSCTL_PATH,
                    kernelsmith_core::tuning::sysctl_conf(plan.profile == ProfileId::Gaming),
                    kernelsmith_core::tuning::UDEV_PATH,
                    kernelsmith_core::tuning::udev_rules()
                );
                self.modal = Some(Modal::Script { title: "Runtime tuning pack".into(), text });
            }
        });

        ui.add_space(8.0);
        section(ui, "Build speed");
        ui.horizontal(|ui| {
            ui.label("Parallel jobs");
            let max = (sys.cpu.threads * 2).max(2);
            ui.add(egui::Slider::new(&mut plan.jobs, 1..=max));
            dim(ui, &format!("Your CPU has {} threads. Lower this to keep the PC usable while it builds.", sys.cpu.threads));
        });
        if let Some(free) = disk_free(Path::new("/usr/src")) {
            dim(ui, &format!("Free space in /usr/src: {}", human_bytes(free)));
        }

        ui.add_space(10.0);
        section(ui, "Prefer zero-maintenance?");
        panel(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                ui.vertical(|ui| {
                    ui.label(RichText::new("Let Portage build and update it for you").font(bold(15.0)).color(TEXT_BRIGHT));
                    ui.add(
                        egui::Label::new(
                            RichText::new(
                                "Instead of building now, Kernelsmith can save your choices as a config fragment (/etc/kernel/config.d) and \
                                 patches (/etc/portage/patches) for a distribution kernel like gentoo-kernel or cachyos-kernel. Every \
                                 `emerge -u` then rebuilds it with your settings. Hardware trimming isn't available in this mode.",
                            )
                            .color(TEXT_DIM)
                            .size(13.0),
                        )
                        .wrap(),
                    );
                });
            });
            if button(ui, "Export to Portage…", Btn::Blue).clicked() {
                self.modal = Some(Modal::Portage { package: 0, rebuild: false });
            }
        });
    }

    // ───────────────────────────── Step 7 ─────────────────────────────
    fn step_review(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>, plan: &mut BuildPlan, src: Option<&KernelSource>, symbols: Option<&SymbolIndex>) {
        let Some(src) = src else { return };
        let findings = plan::check(plan, sys, Some(src));
        let resolved = plan::resolve(plan, sys, src, symbols);
        let est = plan::estimate(plan, sys);
        ui.columns(2, |cols| {
            panel(&mut cols[0], |ui| {
                ui.set_width(ui.available_width());
                title(ui, "Your kernel");
                ui.add_space(4.0);
                let sel = |k: &str| {
                    let t = tweaks::find(k);
                    let v = plan.tweaks.get(k).cloned().unwrap_or_default();
                    t.and_then(|t| t.choice(&v)).map(|c| c.label.to_string()).unwrap_or(v)
                };
                egui::Grid::new("review").num_columns(2).spacing([16.0, 5.0]).show(ui, |ui| {
                    kv(ui, "Name", RichText::new(&resolved.release).color(ACCENT).strong());
                    kv(ui, "Source", format!("{} ({})", src.display(), src.flavor.label()));
                    let pr = profiles::get(plan.profile);
                    kv(ui, "Profile", format!("{} {}", pr.icon, pr.title));
                    kv(ui, "Start from", plan.seed.label());
                    kv(ui, "Drivers", format!("{} — keeping {} famil{}", plan.slim.label(), plan.keep.len(), if plan.keep.len() == 1 { "y" } else { "ies" }));
                    kv(ui, "Compiler", format!("{} · LTO {} · {}", sel("compiler"), sel("lto"), sel("optimize")));
                    kv(ui, "CPU target", sel("march"));
                    kv(ui, "Scheduler", format!("{} · sched_ext {}", sel("scheduler"), sel("sched_ext")));
                    kv(ui, "Timer", format!("{} · {} preemption", sel("hz"), sel("preempt")));
                    kv(ui, "Gaming", format!("NTSYNC {} · controllers {}", sel("ntsync"), sel("gaming_input")));
                    kv(ui, "AutoFDO", if plan.profile_data.is_some() { "Rebuilding with your recorded profile".to_string() } else { sel("autofdo") });
                    let patch_names: Vec<&str> = plan.patches.iter().map(|p| p.title.as_str()).collect();
                    kv(ui, "Patches", if patch_names.is_empty() { "None".into() } else { patch_names.join(", ") });
                    kv(
                        ui,
                        "Install",
                        if plan.install.install {
                            format!("/boot via installkernel › {}{}", sys.boot.bootloader.label(), if plan.install.module_rebuild { " · rebuild drivers" } else { "" })
                        } else {
                            "Build only".into()
                        },
                    );
                    kv(ui, "Init systems", plan.inits.iter().map(|i| i.label()).collect::<Vec<_>>().join(" + "));
                });
            });
            let ui = &mut cols[1];
            panel(ui, |ui| {
                ui.set_width(ui.available_width());
                title(ui, "Estimate");
                ui.label(RichText::new(format!("{}–{} minutes", est.minutes_low, est.minutes_high)).font(bold(26.0)).color(TEXT_BRIGHT));
                dim(ui, &format!("≈ {} compiled files · up to {} of disk during the build", est.objects, human_bytes(est.disk_bytes)));
                dim(ui, &format!("{} kernel options will be set and verified.", resolved.expectations.len()));
            });
            ui.add_space(8.0);
            panel(ui, |ui| {
                ui.set_width(ui.available_width());
                title(ui, "Checks");
                if findings.is_empty() {
                    status_line(ui, 0, "Everything looks good.");
                }
                for f in &findings {
                    status_line(ui, match f.level { Level::Info => 3, Level::Warn => 1, Level::Error => 2 }, &f.text);
                }
                if !resolved.unsupported.is_empty() {
                    egui::CollapsingHeader::new(RichText::new(format!("{} option(s) not available in this source", resolved.unsupported.len())).color(WARN))
                        .id_salt("unsupported")
                        .show(ui, |ui| {
                            for u in &resolved.unsupported {
                                dim(ui, u);
                            }
                        });
                }
            });
        });
        ui.add_space(10.0);
        ui.horizontal(|ui| {
            if button(ui, "View build script", Btn::Secondary).clicked() {
                let body = script::generate(&ScriptInputs { plan, sys, src, resolved: &resolved, job_dir: &paths::jobs_dir().join("<job>") });
                self.modal = Some(Modal::Script { title: "Build script (runs as root)".into(), text: body });
            }
            if button(ui, "Save as preset…", Btn::Secondary).clicked() {
                self.modal = Some(Modal::SavePreset { name: plan.name.clone() });
            }
        });
        ui.add_space(8.0);
        callout(
            ui,
            ACCENT,
            "🔑",
            "When you press Build, your desktop will ask for your password once. Building, installing and updating the boot \
             menu need administrator rights; everything that runs is in the script above.",
        );
    }
}

pub fn mm_of(path: &Path, sources: &[KernelSource]) -> String {
    sources.iter().find(|s| s.path == path).map(|s| s.major_minor()).unwrap_or_default()
}

pub fn apply_profile(wiz: &mut Wizard, plan: &mut BuildPlan, sys: &SystemInfo, id: ProfileId) {
    plan.apply_profile(sys, id);
    wiz.customized = false;
    wiz.user_set.clear();
    wiz.notes.clear();
}

/// After a patch adds symbols, restore profile defaults that were downgraded
/// earlier for lack of them — unless the user chose something explicitly.
fn reapply_defaults(wiz: &mut Wizard, plan: &mut BuildPlan, sys: &SystemInfo) {
    let defaults = profiles::defaults(plan.profile, sys);
    for (k, v) in defaults {
        if !wiz.user_set.contains(&k) {
            plan.tweaks.insert(k, v);
        }
    }
    wiz.notes.clear();
}

fn readiness(ui: &mut Ui, sys: &SystemInfo, sources: &[KernelSource]) {
    let tc = &sys.toolchain;
    let missing = tc.missing_essentials();
    if missing.is_empty() {
        status_line(ui, 0, "All required build tools are installed.");
    } else {
        let pkgs: Vec<&str> = missing.iter().map(|m| m.1).collect();
        status_line(ui, 2, &format!("Missing build tools — run: emerge --ask {}", pkgs.join(" ")));
    }
    if tc.clang_ready() {
        status_line(ui, 0, &format!("Clang/LLVM {} — ThinLTO and AutoFDO available.", tc.clang_major.map(|m| m.to_string()).unwrap_or_default()));
    } else {
        status_line(ui, 1, "Clang/LLVM not found — GCC will be used (no LTO). emerge llvm-core/clang llvm-core/lld to unlock it.");
    }
    if !tc.pahole {
        status_line(ui, 1, "pahole missing — needed for sched_ext/BTF. emerge dev-util/pahole");
    }
    let n = sources.iter().filter(|s| s.buildable && s.flavor != Flavor::Kernelsmith).count();
    if n > 0 {
        status_line(ui, 0, &format!("{n} kernel source tree(s) ready."));
    } else {
        status_line(ui, 2, "No full kernel sources yet — the next step helps you install them.");
    }
    match &sys.boot.installkernel {
        Some((v, flags)) => {
            let relevant: Vec<&str> = flags.iter().map(String::as_str).filter(|f| ["dracut", "ugrd", "grub", "systemd-boot", "uki", "efistub", "refind", "limine"].contains(f)).collect();
            status_line(ui, 0, &format!("installkernel {v} [{}] will set up booting.", relevant.join(" ")));
        }
        None => status_line(ui, 1, "installkernel is not installed — the boot menu won't update automatically. emerge sys-kernel/installkernel"),
    }
    match sys.boot.secure_boot {
        Some(true) => status_line(ui, 1, "Secure Boot is on — self-built kernels must be signed or Secure Boot disabled."),
        _ => status_line(ui, 0, "Secure Boot is off — custom kernels will boot."),
    }
    if sys.proc_config {
        status_line(ui, 0, "Your current kernel's configuration can be used as a proven starting point.");
    }
    if !sys.external_modules.is_empty() {
        let names: Vec<&str> = sys.external_modules.iter().map(|p| p.atom.rsplit('/').next().unwrap_or(&p.atom)).collect();
        status_line(ui, 0, &format!("Out-of-tree drivers will be rebuilt automatically: {}.", names.join(", ")));
    }
}

fn option_row(ui: &mut Ui, value: &mut bool, enabled: bool, title_text: &str, desc: &str) {
    panel(ui, |ui| {
        ui.set_width(ui.available_width());
        option_row_inline(ui, value, enabled, title_text, desc);
    });
    ui.add_space(4.0);
}

fn option_row_inline(ui: &mut Ui, value: &mut bool, enabled: bool, title_text: &str, desc: &str) {
    ui.horizontal(|ui| {
        let mut v = *value && enabled;
        if switch(ui, &mut v, enabled).changed() {
            *value = v;
        }
        ui.vertical(|ui| {
            ui.label(RichText::new(title_text).font(bold(14.5)).color(if enabled { TEXT_BRIGHT } else { TEXT_DIM }));
            ui.add(egui::Label::new(RichText::new(desc).color(TEXT_DIM).size(13.0)).wrap());
        });
    });
}

fn step_blurb(step: usize, fallback: &'static str) -> &'static str {
    match step {
        0 => "Kernelsmith scanned your PC. Here's what it found and whether everything needed is installed.",
        1 => "Pick the kernel source to build from. Kernelsmith copies it, so the original stays untouched.",
        2 => "Choose what you mostly do on this PC. This sets sensible defaults you can fine-tune later.",
        3 => "A kernel tailored to your hardware builds several times faster and is much smaller.",
        4 => "Every option is explained. Recommended choices for your profile are marked with ★.",
        5 => "Optional extras from CachyOS and other projects, matched to your kernel version.",
        6 => "How the finished kernel is installed. The defaults are right for most people.",
        7 => "Last look before building. Nothing has been changed on your system yet.",
        _ => fallback,
    }
}

fn profile_colors(id: ProfileId) -> (Color32, Color32) {
    match id {
        ProfileId::Gaming => (Color32::from_rgb(0x2d, 0x7d, 0x46), Color32::from_rgb(0x12, 0x3a, 0x2a)),
        ProfileId::Desktop => (Color32::from_rgb(0x2f, 0x6f, 0xb5), Color32::from_rgb(0x15, 0x2e, 0x52)),
        ProfileId::Creator => (Color32::from_rgb(0x8a, 0x4f, 0xc7), Color32::from_rgb(0x3a, 0x1f, 0x5c)),
        ProfileId::Workstation => (Color32::from_rgb(0xb5, 0x7a, 0x2f), Color32::from_rgb(0x52, 0x33, 0x12)),
        ProfileId::Laptop => (Color32::from_rgb(0x2f, 0xa5, 0xa0), Color32::from_rgb(0x12, 0x45, 0x48)),
        ProfileId::Server => (Color32::from_rgb(0x5a, 0x6a, 0x7d), Color32::from_rgb(0x23, 0x2c, 0x38)),
    }
}

pub fn profile_gradient(id: ProfileId) -> (Color32, Color32) {
    profile_colors(id)
}

fn download_cachy_config() -> Result<PathBuf, String> {
    let url = "https://raw.githubusercontent.com/CachyOS/linux-cachyos/master/linux-cachyos/config";
    let dest = paths::cache_dir().join("cachyos.config");
    let _ = std::fs::create_dir_all(paths::cache_dir());
    let out = std::process::Command::new("curl")
        .args(["-fsSL", "--max-time", "60", "-o"])
        .arg(&dest)
        .arg(url)
        .output()
        .map_err(|e| format!("curl: {e}"))?;
    if !out.status.success() {
        return Err(String::from_utf8_lossy(&out.stderr).trim().to_string());
    }
    let text = std::fs::read_to_string(&dest).map_err(|e| e.to_string())?;
    if !text.contains("CONFIG_") {
        return Err("That doesn't look like a kernel config.".into());
    }
    Ok(dest)
}
