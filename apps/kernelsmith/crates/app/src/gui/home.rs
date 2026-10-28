//! Home: the "store front" — what Kernelsmith does and where to start.

use super::theme::*;
use super::wizard::{apply_profile, profile_gradient};
use super::{App, Page};
use eframe::egui::{self, Color32, CornerRadius, Margin, RichText, ScrollArea, Sense, Stroke, Ui, Vec2};
use kernelsmith_core::hw::SystemInfo;
use kernelsmith_core::plan::BuildPlan;
use kernelsmith_core::profiles::{self, ProfileId};
use kernelsmith_core::sources::Flavor;
use std::sync::Arc;

const FEATURES: [(&str, &str, &str); 8] = [
    ("⚡", "BORE scheduler", "Games and the desktop stay smooth while the CPU is busy."),
    ("🔗", "ThinLTO", "Clang optimizes the whole kernel as one program."),
    ("📈", "AutoFDO", "Profile-guided rebuilds from your own workload — the Clear Linux trick."),
    ("🎮", "NTSYNC", "Native Windows sync primitives for Wine & Proton."),
    ("🎯", "Native CPU", "Uses every instruction your processor supports."),
    ("✂", "Hardware fit", "Only the drivers you need: builds in minutes, not an hour."),
    ("🌐", "BBR", "Faster, lower-latency downloads on busy networks."),
    ("📦", "zswap + MGLRU", "Stays responsive when memory runs low."),
];

impl App {
    pub(super) fn home(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>) {
        ScrollArea::vertical().id_salt("home").auto_shrink([false, false]).show(ui, |ui| {
            egui::Frame::new().inner_margin(Margin { left: 34, right: 34, top: 24, bottom: 30 }).show(ui, |ui| {
                ui.set_max_width(ui.available_width().min(1240.0));
                self.hero(ui, sys);
                ui.add_space(18.0);

                let sources = self.sources.ready().cloned().unwrap_or_default();
                let buildable = sources.iter().any(|s| s.buildable && s.flavor != Flavor::Kernelsmith);
                if !buildable && !self.sources.is_loading() {
                    callout(
                        ui,
                        WARN,
                        "!",
                        "You don't have full kernel sources installed yet (the “-dist” trees in /usr/src only hold headers). \
                         The Build page installs gentoo-sources or cachyos-sources for you in one click.",
                    );
                    ui.add_space(6.0);
                    if button(ui, "Get kernel sources", Btn::Blue).clicked() {
                        self.page = Page::Build;
                        self.wiz.step = 1;
                    }
                    ui.add_space(12.0);
                }

                section(ui, "Recommended for your PC");
                let rec = profiles::recommend(sys);
                let mut order: Vec<ProfileId> = profiles::ALL.to_vec();
                order.sort_by_key(|p| *p != rec);
                let w = ((ui.available_width() - 2.0 * 14.0) / 3.0).max(250.0);
                let mut chosen = None;
                for row in order.chunks(3) {
                    ui.horizontal_top(|ui| {
                        ui.spacing_mut().item_spacing.x = 14.0;
                        for id in row {
                            if capsule(ui, *id, w, *id == rec).clicked() {
                                chosen = Some(*id);
                            }
                        }
                    });
                    ui.add_space(14.0);
                }
                if let Some(id) = chosen {
                    self.start_with_profile(sys, id);
                }

                section(ui, "What Kernelsmith tunes");
                let w4 = ((ui.available_width() - 3.0 * 12.0) / 4.0).max(200.0);
                for row in FEATURES.chunks(4) {
                    ui.horizontal_top(|ui| {
                        ui.spacing_mut().item_spacing.x = 12.0;
                        for (icon, name, desc) in row {
                            ui.allocate_ui(Vec2::new(w4, 96.0), |ui| {
                                ui.set_width(w4);
                                panel(ui, |ui| {
                                    ui.set_width(w4 - 30.0);
                                    ui.set_min_height(64.0);
                                    ui.horizontal(|ui| {
                                        ui.label(RichText::new(*icon).size(22.0).color(ACCENT));
                                        ui.label(RichText::new(*name).font(bold(15.0)).color(TEXT_BRIGHT));
                                    });
                                    ui.add(egui::Label::new(RichText::new(*desc).color(TEXT_DIM).size(13.0)).wrap());
                                });
                            });
                        }
                    });
                    ui.add_space(10.0);
                }

                section(ui, "How it works");
                let steps = [
                    ("1", "Scan", "Detects your CPU, GPU, disks, init system and bootloader."),
                    ("2", "Choose", "Pick a goal; every option is explained in plain words."),
                    ("3", "Build", "Compiles a copy of the source — your system isn't touched until install."),
                    ("4", "Boot", "Installed the Gentoo way via installkernel. Your old kernel stays as a fallback."),
                ];
                ui.horizontal_top(|ui| {
                    ui.spacing_mut().item_spacing.x = 12.0;
                    for (n, t, d) in steps {
                        ui.allocate_ui(Vec2::new(w4, 110.0), |ui| {
                            ui.set_width(w4);
                            ui.horizontal_top(|ui| {
                                let (r, _) = ui.allocate_exact_size(Vec2::splat(38.0), Sense::hover());
                                ui.painter().circle_filled(r.center(), 19.0, ACCENT_STRONG.linear_multiply(0.3));
                                ui.painter().circle_stroke(r.center(), 19.0, Stroke::new(1.5_f32, ACCENT));
                                ui.painter().text(r.center(), egui::Align2::CENTER_CENTER, n, bold(17.0), TEXT_BRIGHT);
                                ui.vertical(|ui| {
                                    ui.label(RichText::new(t).font(bold(15.0)).color(TEXT_BRIGHT));
                                    ui.add(egui::Label::new(RichText::new(d).color(TEXT_DIM).size(13.0)).wrap());
                                });
                            });
                        });
                    }
                });

                if let Some(kernels) = self.library.ready() {
                    ui.add_space(12.0);
                    section(ui, "Your kernels");
                    let ours = kernels.iter().filter(|k| k.manifest().is_some()).count();
                    ui.horizontal(|ui| {
                        dim(ui, &format!("{} installed · {} built with Kernelsmith · running {}", kernels.len(), ours, sys.running_kernel));
                        if ui.link("Open Library").clicked() {
                            self.page = Page::Library;
                        }
                    });
                }
            });
        });
    }

    fn hero(&mut self, ui: &mut Ui, sys: &SystemInfo) {
        let h = 300.0;
        let (rect, _) = ui.allocate_exact_size(Vec2::new(ui.available_width(), h), Sense::hover());
        let p = ui.painter();
        hgradient(p, rect, Color32::from_rgb(0x1e, 0x4a, 0x72), Color32::from_rgb(0x12, 0x1d, 0x2b));
        // Decorative diagonal light streaks.
        for i in 0..6 {
            let x = rect.left() + rect.width() * (0.45 + i as f32 * 0.09);
            let pts = vec![
                egui::pos2(x, rect.top()),
                egui::pos2(x + 60.0, rect.top()),
                egui::pos2(x - 40.0, rect.bottom()),
                egui::pos2(x - 100.0, rect.bottom()),
            ];
            p.add(egui::Shape::convex_polygon(pts, Color32::from_white_alpha(4 + i as u8 * 2), Stroke::NONE));
        }
        p.rect_stroke(rect, CornerRadius::same(4), Stroke::new(1.0_f32, BORDER), egui::StrokeKind::Inside);

        let inner = rect.shrink2(Vec2::new(34.0, 30.0));
        let left = egui::Rect::from_min_size(inner.min, Vec2::new(inner.width() * 0.58, inner.height()));
        let right = egui::Rect::from_min_max(egui::pos2(inner.left() + inner.width() * 0.63, inner.top()), inner.max);
        ui.scope_builder(egui::UiBuilder::new().max_rect(left), |ui| {
            ui.label(RichText::new("FOR GENTOO LINUX · OPENRC & SYSTEMD").font(bold(12.5)).color(ACCENT));
            ui.label(RichText::new("Build a kernel made for your PC").font(bold(34.0)).color(TEXT_BRIGHT));
            ui.add(
                egui::Label::new(
                    RichText::new(
                        "Kernelsmith reads your hardware, applies proven optimizations from CachyOS, Clear Linux and upstream, \
                         and builds a slim kernel tuned for gaming, creating or work — explained step by step, installed the Gentoo \
                         way, removable in one click.",
                    )
                    .color(TEXT)
                    .size(15.0),
                )
                .wrap(),
            );
            ui.add_space(12.0);
            ui.horizontal(|ui| {
                if big_button(ui, "START BUILDING", Btn::Primary, true).clicked() {
                    self.page = Page::Build;
                }
                ui.add_space(8.0);
                if !self.presets.is_empty() && button_sized(ui, "Rebuild from a preset", Btn::Secondary, true, 46.0).clicked() {
                    self.page = Page::Library;
                }
            });
        });
        ui.scope_builder(egui::UiBuilder::new().max_rect(right), |ui| {
            egui::Frame::new()
                .fill(Color32::from_rgba_unmultiplied(0x0b, 0x12, 0x1b, 0xc0))
                .corner_radius(CornerRadius::same(4))
                .inner_margin(Margin::same(14))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.label(RichText::new("YOUR SYSTEM").font(bold(12.0)).color(TEXT_DIM));
                    let rows = [
                        ("⚡", sys.cpu.model_name.clone()),
                        ("🖥", sys.primary_gpu_name()),
                        ("🗄", format!("{:.0} GiB RAM · {} disk(s)", sys.memory_gib(), sys.storage.len())),
                        ("🔧", format!("{} · {} · {}", sys.init.label(), sys.boot.bootloader.label(), sys.boot.initramfs.label())),
                        ("▶", format!("Running {}", sys.running_kernel)),
                    ];
                    for (icon, text) in rows {
                        ui.horizontal(|ui| {
                            ui.label(RichText::new(icon).color(ACCENT));
                            ui.add(egui::Label::new(RichText::new(text).color(TEXT_BRIGHT)).truncate());
                        });
                    }
                    if ui.link("Hardware details ›").clicked() {
                        self.page = Page::Hardware;
                    }
                });
        });
    }

    /// Profile capsule click on Home: start the wizard with that goal.
    fn start_with_profile(&mut self, sys: &SystemInfo, id: ProfileId) {
        let sources = self.sources.ready().cloned().unwrap_or_default();
        if self.wiz.plan.is_none() {
            if let Some(src) = sources
                .iter()
                .filter(|s| s.buildable && s.flavor != Flavor::Kernelsmith)
                .min_by_key(|s| (std::cmp::Reverse(s.version_tuple()), s.recommended_rank()))
            {
                self.wiz.plan = Some(BuildPlan::new(sys, id, src));
            }
        }
        match self.wiz.plan.take() {
            Some(mut plan) => {
                apply_profile(&mut self.wiz, &mut plan, sys, id);
                self.wiz.plan = Some(plan);
                self.wiz.step = 3;
            }
            None => self.wiz.step = 1,
        }
        self.page = Page::Build;
    }
}

fn capsule(ui: &mut Ui, id: ProfileId, w: f32, recommended: bool) -> egui::Response {
    let pr = profiles::get(id);
    let (a, b) = profile_gradient(id);
    ui.allocate_ui(Vec2::new(w, 250.0), |ui| {
        ui.set_width(w);
        let (resp, _) = card(ui, ("home-cap", id as u8), false, |ui| {
            ui.set_width(w - 30.0);
            ui.set_min_height(210.0);
            let (rect, _) = ui.allocate_exact_size(Vec2::new(w - 30.0, 92.0), Sense::hover());
            icon_capsule(ui, rect, pr.icon, a, b);
            if recommended {
                let r = egui::Rect::from_min_size(rect.left_top() + Vec2::new(8.0, 8.0), Vec2::new(118.0, 20.0));
                ui.painter().rect_filled(r, CornerRadius::same(2), GREEN_B);
                ui.painter().text(r.center(), egui::Align2::CENTER_CENTER, "RECOMMENDED", bold(11.0), Color32::WHITE);
            }
            ui.label(RichText::new(pr.title).font(bold(17.0)).color(TEXT_BRIGHT));
            ui.label(RichText::new(pr.tagline).color(ACCENT).size(13.5));
            ui.horizontal_wrapped(|ui| {
                for h in pr.highlights.iter().take(4) {
                    tag(ui, h);
                }
            });
        });
        resp
    })
    .inner
}
