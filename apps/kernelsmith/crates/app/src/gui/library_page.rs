//! Library: installed kernels and everything you can do with them afterwards.

use super::jobview::JobKind;
use super::modals::Modal;
use super::theme::*;
use super::{list_presets, App, Page};
use eframe::egui::{self, Color32, CornerRadius, Margin, RichText, ScrollArea, Sense, Stroke, Ui, Vec2};
use kernelsmith_core::hw::SystemInfo;
use kernelsmith_core::library::{self, InstalledKernel, Origin};
use kernelsmith_core::plan::BuildPlan;
use kernelsmith_core::profiles::ProfileId;
use kernelsmith_core::sources::{Flavor, KernelSource};
use kernelsmith_core::util::format_epoch;
use kernelsmith_core::{autofdo, job, portage, tuning};
use std::path::PathBuf;
use std::sync::Arc;

const TUNING_ID: &str = "::tuning";
const PRESETS_ID: &str = "::presets";

impl App {
    pub(super) fn library_page(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>) {
        let kernels = self.library.ready().cloned().unwrap_or_default();
        if self.lib_selected.is_none() {
            self.lib_selected = kernels
                .iter()
                .find(|k| k.manifest().is_some())
                .or_else(|| kernels.first())
                .map(|k| k.release.clone());
        }
        egui::SidePanel::left("lib-list")
            .exact_width(300.0)
            .resizable(false)
            .frame(egui::Frame::new().fill(Color32::from_rgba_unmultiplied(0x10, 0x17, 0x20, 0xd0)).inner_margin(Margin::symmetric(12, 18)))
            .show_inside(ui, |ui| {
                ui.horizontal(|ui| {
                    ui.label(RichText::new("INSTALLED KERNELS").font(bold(13.0)).color(TEXT_DIM));
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.small_button("↻").on_hover_text("Refresh").clicked() {
                            let ctx = ui.ctx().clone();
                            self.refresh_library(&ctx);
                        }
                    });
                });
                ui.add_space(6.0);
                if self.library.is_loading() {
                    ui.add(egui::Spinner::new().color(ACCENT));
                }
                for k in &kernels {
                    let sel = self.lib_selected.as_deref() == Some(k.release.as_str());
                    let sub = match &k.origin {
                        Origin::Kernelsmith(m) => format!("Kernelsmith · {}", m.profile),
                        Origin::Portage(_) => "Portage dist-kernel".into(),
                        Origin::Manual => "Manual".into(),
                    };
                    if list_item(ui, &k.release, &sub, sel, k.running, matches!(k.origin, Origin::Kernelsmith(_))).clicked() {
                        self.lib_selected = Some(k.release.clone());
                    }
                }
                ui.add_space(14.0);
                ui.label(RichText::new("TOOLS").font(bold(13.0)).color(TEXT_DIM));
                ui.add_space(6.0);
                if list_item(ui, "Saved presets", &format!("{} preset(s)", self.presets.len()), self.lib_selected.as_deref() == Some(PRESETS_ID), false, false).clicked() {
                    self.lib_selected = Some(PRESETS_ID.into());
                    self.presets = list_presets();
                }
                let tuned = tuning::is_installed();
                if list_item(ui, "Runtime tuning & Portage", if tuned { "Tuning pack installed" } else { "Not installed" }, self.lib_selected.as_deref() == Some(TUNING_ID), false, false).clicked() {
                    self.lib_selected = Some(TUNING_ID.into());
                }
            });

        egui::CentralPanel::default()
            .frame(egui::Frame::new().inner_margin(Margin { left: 28, right: 28, top: 22, bottom: 10 }))
            .show_inside(ui, |ui| {
                ScrollArea::vertical().id_salt("lib").auto_shrink([false, false]).show(ui, |ui| {
                    ui.set_max_width(ui.available_width().min(1000.0));
                    match self.lib_selected.clone().as_deref() {
                        Some(PRESETS_ID) => self.presets_panel(ui, sys),
                        Some(TUNING_ID) => self.tuning_panel(ui, sys),
                        Some(rel) => match kernels.iter().find(|k| k.release == rel) {
                            Some(k) => self.kernel_detail(ui, sys, k),
                            None => {
                                dim(ui, "Select a kernel.");
                            }
                        },
                        None => {
                            dim(ui, "No kernels found.");
                        }
                    }
                });
            });
    }

    fn kernel_detail(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>, k: &InstalledKernel) {
        let (rect, _) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 150.0), Sense::hover());
        let (a, b) = match &k.origin {
            Origin::Kernelsmith(m) => super::wizard::profile_gradient(profile_from_slug(&m.profile)),
            Origin::Portage(_) => (Color32::from_rgb(0x4a, 0x3f, 0x7a), Color32::from_rgb(0x1d, 0x1a, 0x33)),
            Origin::Manual => (Color32::from_rgb(0x3a, 0x48, 0x5a), Color32::from_rgb(0x1a, 0x22, 0x2d)),
        };
        hgradient(ui.painter(), rect, a, b);
        ui.painter().rect_stroke(rect, CornerRadius::same(4), Stroke::new(1.0_f32, BORDER), egui::StrokeKind::Inside);
        ui.painter().text(rect.left_bottom() + Vec2::new(22.0, -48.0), egui::Align2::LEFT_BOTTOM, &k.release, bold(30.0), Color32::WHITE);
        ui.painter().text(rect.left_bottom() + Vec2::new(22.0, -22.0), egui::Align2::LEFT_BOTTOM, k.origin_label(), regular(15.0), Color32::from_white_alpha(210));
        ui.add_space(10.0);
        ui.horizontal(|ui| {
            if k.running {
                badge(ui, "Running now", GREEN);
            }
            if k.boot_image.is_some() {
                badge(ui, "Bootable", ACCENT);
            }
            if let Some(m) = k.manifest() {
                if m.autofdo_ready {
                    badge(ui, "Profile-ready", PURPLE);
                }
            }
        });
        ui.add_space(8.0);

        // ── Actions ──
        if let Some(m) = k.manifest() {
            ui.horizontal(|ui| {
                if big_button(ui, "REBUILD WITH NEWEST SOURCES", Btn::Primary, true)
                    .on_hover_text("Same settings, newest kernel source — the easy way to keep up with updates")
                    .clicked()
                {
                    match BuildPlan::load(&m.plan_file()) {
                        Ok(mut plan) => {
                            if let Some(src) = newest_source(self.sources.ready().map(|v| v.as_slice()).unwrap_or(&[])) {
                                plan.source = src.path.clone();
                            }
                            plan.profile_data = None;
                            self.wiz.load_plan(plan, 7);
                            self.page = Page::Build;
                        }
                        Err(e) => self.modal = Some(Modal::message("Couldn't read the saved plan", format!("{e:#}"))),
                    }
                }
                if m.autofdo_ready && button_sized(ui, "Optimize with AutoFDO", Btn::Blue, true, 46.0).clicked() {
                    self.modal = Some(Modal::Profiling { release: k.release.clone(), minutes: 5, propeller: false });
                }
                let r = button_sized(ui, "Uninstall", Btn::Danger, !k.running, 46.0);
                if k.running {
                    r.on_hover_text("You can't remove the kernel you're running. Boot another one first.");
                } else if r.clicked() {
                    self.modal = Some(Modal::ConfirmUninstall(k.release.clone()));
                }
            });
            ui.add_space(10.0);
        }

        panel(ui, |ui| {
            ui.set_width(ui.available_width());
            title(ui, "Details");
            egui::Grid::new("kdetail").num_columns(2).spacing([18.0, 6.0]).show(ui, |ui| {
                kv(ui, "Release", &k.release);
                kv(ui, "Kernel image", k.boot_image.as_ref().map(|p| p.display().to_string()).unwrap_or_else(|| "—".into()));
                kv(ui, "Modules", k.modules_dir.display().to_string());
                if let Some(t) = &k.build_tree {
                    kv(ui, "Build tree", t.display().to_string());
                }
                if let Some(m) = k.manifest() {
                    kv(ui, "Built", format!("{} UTC", format_epoch(m.created)));
                    kv(ui, "Profile", m.profile.clone());
                    kv(ui, "From source", m.source.display().to_string());
                    kv(ui, "Installed files", m.files.len().to_string());
                }
            });
            ui.add_space(6.0);
            if let Some(cfg) = library::kernel_config_path(k) {
                if ui.button("Show kernel configuration").clicked() {
                    let text = std::fs::read_to_string(&cfg).unwrap_or_default();
                    self.modal = Some(Modal::Script { title: format!("Configuration of {}", k.release), text });
                }
            }
        });
        ui.add_space(8.0);
        match &k.origin {
            Origin::Kernelsmith(m) => {
                if m.autofdo_ready {
                    callout(
                        ui,
                        PURPLE,
                        "📈",
                        if k.running {
                            "This kernel is ready for profile-guided optimization. Press “Optimize with AutoFDO”, play or work for a few minutes, then rebuild."
                        } else {
                            "Boot into this kernel to record an AutoFDO profile for it."
                        },
                    );
                }
                let profiles = autofdo::profiles_for(&k.release);
                if !profiles.is_empty() {
                    section(ui, "Recorded profiles");
                    for p in profiles {
                        ui.horizontal(|ui| {
                            dim(ui, &p.display().to_string());
                            if ui.small_button("Rebuild with this profile").clicked() {
                                self.start_autofdo_rebuild(&k.release, p.clone());
                            }
                        });
                    }
                }
            }
            Origin::Portage(pkg) => callout(
                ui,
                ACCENT,
                "ℹ",
                &format!("Installed by Portage ({pkg}). Remove it with `emerge --depclean`, or customize it from the Install step's “Export to Portage” option."),
            ),
            Origin::Manual => callout(ui, ACCENT, "ℹ", "Installed outside Kernelsmith and Portage, so Kernelsmith won't remove it."),
        }
        let _ = sys;
    }

    fn presets_panel(&mut self, ui: &mut Ui, _sys: &Arc<SystemInfo>) {
        heading(ui, "Saved presets");
        subheading(ui, "A preset remembers every choice. When a new kernel version arrives, rebuild it in two clicks.");
        ui.add_space(8.0);
        if self.presets.is_empty() {
            dim(ui, "No presets yet — save one from the Review step of the Build wizard.");
        }
        let mut delete = None;
        for p in self.presets.clone() {
            panel(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    ui.label(RichText::new("📋").size(20.0));
                    let name = p.file_stem().unwrap_or_default().to_string_lossy().into_owned();
                    ui.vertical(|ui| {
                        ui.label(RichText::new(&name).font(bold(15.0)).color(TEXT_BRIGHT));
                        dim(ui, &p.display().to_string());
                    });
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.small_button("Delete").clicked() {
                            delete = Some(p.clone());
                        }
                        if button(ui, "Build with newest source", Btn::Primary).clicked() {
                            match BuildPlan::load(&p) {
                                Ok(mut plan) => {
                                    if let Some(src) = newest_source(self.sources.ready().map(|v| v.as_slice()).unwrap_or(&[])) {
                                        plan.source = src.path.clone();
                                    }
                                    self.wiz.load_plan(plan, 7);
                                    self.page = Page::Build;
                                }
                                Err(e) => self.modal = Some(Modal::message("Couldn't load preset", format!("{e:#}"))),
                            }
                        }
                        if button(ui, "Open in wizard", Btn::Secondary).clicked() {
                            match BuildPlan::load(&p) {
                                Ok(plan) => {
                                    self.wiz.load_plan(plan, 1);
                                    self.page = Page::Build;
                                }
                                Err(e) => self.modal = Some(Modal::message("Couldn't load preset", format!("{e:#}"))),
                            }
                        }
                    });
                });
            });
            ui.add_space(6.0);
        }
        if let Some(p) = delete {
            let _ = std::fs::remove_file(p);
            self.presets = list_presets();
        }
        ui.add_space(8.0);
        callout(
            ui,
            ACCENT,
            "⌨",
            "From a terminal (or a Portage hook) the same works without the GUI:  kernelsmith build --preset ~/.config/kernelsmith/presets/NAME.toml --latest",
        );
    }

    fn tuning_panel(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>) {
        heading(ui, "Runtime tuning & Portage");
        ui.add_space(6.0);
        panel(ui, |ui| {
            ui.set_width(ui.available_width());
            title(ui, "Runtime tuning pack");
            ui.add(
                egui::Label::new(
                    RichText::new(
                        "System-wide settings that complement your kernel: best I/O scheduler per disk, larger file caches, \
                         smoother writeback, no NMI watchdog, and (for gaming) a higher memory-map limit. Plain files in \
                         /etc/sysctl.d and /etc/udev/rules.d — works with OpenRC and systemd.",
                    )
                    .color(TEXT_DIM),
                )
                .wrap(),
            );
            ui.add_space(6.0);
            let installed = tuning::is_installed();
            ui.horizontal(|ui| {
                if installed {
                    badge(ui, "Installed", GREEN);
                    if button(ui, "Remove", Btn::Danger).clicked() {
                        match job::tuning(false, false) {
                            Ok(j) => self.start_job(j, JobKind::Tuning, true),
                            Err(e) => self.modal = Some(Modal::message("Couldn't prepare", format!("{e:#}"))),
                        }
                    }
                } else {
                    let gaming = self.wiz.plan.as_ref().is_none_or(|p| p.profile == ProfileId::Gaming);
                    if button(ui, "Install", Btn::Primary).clicked() {
                        match job::tuning(true, gaming) {
                            Ok(j) => self.start_job(j, JobKind::Tuning, true),
                            Err(e) => self.modal = Some(Modal::message("Couldn't prepare", format!("{e:#}"))),
                        }
                    }
                }
                if ui.button("Preview").clicked() {
                    let text = format!("# {}\n{}\n# {}\n{}", tuning::SYSCTL_PATH, tuning::sysctl_conf(true), tuning::UDEV_PATH, tuning::udev_rules());
                    self.modal = Some(Modal::Script { title: "Runtime tuning pack".into(), text });
                }
            });
        });
        ui.add_space(10.0);
        panel(ui, |ui| {
            ui.set_width(ui.available_width());
            title(ui, "Portage integrations");
            dim(ui, "Customizations you handed to a Portage distribution kernel (config fragment, USE flags, patches).");
            let list = portage::integrations();
            if list.is_empty() {
                dim(ui, "None yet. Use “Export to Portage” in the Build wizard's Install step.");
            }
            for (name, files) in list {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(&name).font(bold(14.5)).color(TEXT_BRIGHT));
                    dim(ui, &format!("{} file(s)", files.len()));
                    if ui.small_button("Remove").clicked() {
                        match portage::remove_job(&name) {
                            Ok(j) => self.start_job(j, JobKind::Portage, true),
                            Err(e) => self.modal = Some(Modal::message("Couldn't prepare", format!("{e:#}"))),
                        }
                    }
                });
                for f in files {
                    dim(ui, &format!("   {f}"));
                }
            }
        });
        let _ = sys;
    }

    pub(super) fn start_autofdo_rebuild(&mut self, release: &str, afdo: PathBuf) {
        let manifests = library::load_manifests();
        let Some(m) = manifests.get(release) else {
            self.modal = Some(Modal::message("Not found", format!("No Kernelsmith manifest for {release}.")));
            return;
        };
        match autofdo::rebuild_plan(m, afdo) {
            Ok(plan) => {
                self.wiz.load_plan(plan, 7);
                self.page = Page::Build;
                self.toast("Loaded your settings with the AutoFDO profile — review and build.", PURPLE);
            }
            Err(e) => self.modal = Some(Modal::message("Couldn't prepare the rebuild", format!("{e:#}"))),
        }
    }
}

fn newest_source(list: &[KernelSource]) -> Option<&KernelSource> {
    list.iter()
        .filter(|s| s.buildable && s.flavor != Flavor::Kernelsmith)
        .min_by_key(|s| (std::cmp::Reverse(s.version_tuple()), s.recommended_rank()))
}

fn profile_from_slug(s: &str) -> ProfileId {
    kernelsmith_core::profiles::ALL
        .into_iter()
        .find(|p| kernelsmith_core::script::profile_slug(*p) == s)
        .unwrap_or(ProfileId::Desktop)
}

fn list_item(ui: &mut Ui, title_text: &str, sub: &str, selected: bool, running: bool, ours: bool) -> egui::Response {
    let (rect, resp) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 50.0), Sense::click());
    let p = ui.painter();
    if selected {
        p.rect_filled(rect, CornerRadius::same(3), PANEL_SEL);
    } else if resp.hovered() {
        p.rect_filled(rect, CornerRadius::same(3), Color32::from_white_alpha(8));
    }
    let dot = egui::pos2(rect.left() + 14.0, rect.center().y);
    let dot_color = if running {
        GREEN
    } else if ours {
        ACCENT
    } else {
        Color32::from_rgb(0x4a, 0x58, 0x6a)
    };
    p.circle_filled(dot, 5.0, dot_color);
    p.text(egui::pos2(rect.left() + 28.0, rect.center().y - 9.0), egui::Align2::LEFT_CENTER, title_text, bold(14.0), if selected { TEXT_BRIGHT } else { TEXT });
    p.text(egui::pos2(rect.left() + 28.0, rect.center().y + 10.0), egui::Align2::LEFT_CENTER, sub, regular(12.0), TEXT_DIM);
    resp.on_hover_cursor(egui::CursorIcon::PointingHand)
}
