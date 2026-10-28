//! Patches: a store-like browser for the patch catalog and your own patches.

use super::theme::*;
use super::{open_path, App, Load, Page};
use eframe::egui::{self, Color32, Margin, RichText, ScrollArea, Sense, Ui, Vec2};
use kernelsmith_core::hw::SystemInfo;
use kernelsmith_core::patches::{self, PatchCategory, PlannedPatch};
use kernelsmith_core::paths;
use kernelsmith_core::util::human_bytes;
use std::sync::Arc;

const CATS: [PatchCategory; 5] = [
    PatchCategory::Scheduler,
    PatchCategory::Performance,
    PatchCategory::Hardware,
    PatchCategory::Filesystem,
    PatchCategory::Security,
];

impl App {
    pub(super) fn patches_page(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>) {
        let ctx = ui.ctx().clone();
        let sources = self.sources.ready().cloned().unwrap_or_default();
        let mut plan = self.wiz.plan.take();
        let src = plan.as_ref().and_then(|p| sources.iter().find(|s| s.path == p.source).cloned());
        let catalog = patches::catalog();

        egui::SidePanel::left("patch-cats")
            .exact_width(220.0)
            .resizable(false)
            .frame(egui::Frame::new().fill(Color32::from_rgba_unmultiplied(0x10, 0x17, 0x20, 0xd0)).inner_margin(Margin::symmetric(14, 20)))
            .show_inside(ui, |ui| {
                ui.label(RichText::new("BROWSE").font(bold(13.0)).color(TEXT_DIM));
                ui.add_space(8.0);
                let all_sel = self.patch_filter.is_none();
                if cat_item(ui, "All patches", catalog.len(), all_sel).clicked() {
                    self.patch_filter = None;
                }
                for c in CATS {
                    let n = catalog.iter().filter(|p| p.category == c).count();
                    if cat_item(ui, c.label(), n, self.patch_filter == Some(c)).clicked() {
                        self.patch_filter = Some(c);
                    }
                }
                let local_n = self.local_patches.len();
                if cat_item(ui, PatchCategory::Local.label(), local_n, self.patch_filter == Some(PatchCategory::Local)).clicked() {
                    self.patch_filter = Some(PatchCategory::Local);
                }
                ui.add_space(16.0);
                if let Some(p) = &plan {
                    ui.label(RichText::new("IN YOUR BUILD").font(bold(12.0)).color(TEXT_DIM));
                    if p.patches.is_empty() {
                        dim(ui, "No patches yet");
                    }
                    for pp in &p.patches {
                        ui.label(RichText::new(format!("• {}", pp.title)).color(TEXT_BRIGHT));
                    }
                    ui.add_space(8.0);
                    if ui.link("Continue in Build ›").clicked() {
                        self.page = Page::Build;
                        self.wiz.step = 5;
                    }
                }
            });

        egui::CentralPanel::default()
            .frame(egui::Frame::new().inner_margin(Margin { left: 28, right: 28, top: 22, bottom: 10 }))
            .show_inside(ui, |ui| {
                ui.horizontal(|ui| {
                    heading(ui, "Patches");
                    ui.add_space(20.0);
                    ui.add(egui::TextEdit::singleline(&mut self.patch_search).hint_text("🔍 Search patches").desired_width(280.0));
                });
                match &src {
                    Some(s) => subheading(ui, &format!("Matched to your build source: Linux {} ({})", s.display(), s.flavor.label())),
                    None => subheading(ui, "Pick a kernel source in Build to see which patches fit it."),
                }
                ui.add_space(8.0);
                ScrollArea::vertical().id_salt("patches").auto_shrink([false, false]).show(ui, |ui| {
                    ui.set_max_width(ui.available_width().min(1100.0));
                    let q = self.patch_search.to_lowercase();
                    if self.patch_filter != Some(PatchCategory::Local) {
                        let statuses = src.as_ref().and_then(|s| self.patch_statuses(&ctx, s));
                        if src.is_some() && statuses.is_none() {
                            ui.horizontal(|ui| {
                                ui.add(egui::Spinner::new().color(ACCENT));
                                dim(ui, "Checking patch availability…");
                            });
                        }
                        let filter = self.patch_filter;
                        for info in catalog.iter().filter(|p| filter.is_none_or(|c| c == p.category)) {
                            if !q.is_empty() && !format!("{} {} {}", info.title, info.summary, info.explain).to_lowercase().contains(&q) {
                                continue;
                            }
                            match (plan.as_mut(), src.as_ref()) {
                                (Some(p), Some(s)) => {
                                    let status = statuses.as_ref().and_then(|v| v.iter().find(|(id, _)| id == info.id).map(|(_, s)| s.clone()));
                                    self.patch_card(ui, &ctx, sys, p, s, info, status);
                                }
                                _ => {
                                    panel(ui, |ui| {
                                        ui.set_width(ui.available_width());
                                        ui.horizontal(|ui| {
                                            ui.label(RichText::new(info.icon).size(22.0));
                                            ui.vertical(|ui| {
                                                ui.label(RichText::new(info.title).font(bold(15.5)).color(TEXT_BRIGHT));
                                                dim(ui, info.summary);
                                            });
                                        });
                                    });
                                }
                            }
                            ui.add_space(6.0);
                        }
                    }
                    if self.patch_filter.is_none() || self.patch_filter == Some(PatchCategory::Local) {
                        self.local_section(ui, plan.as_mut(), &q);
                    }
                });
            });
        self.wiz.plan = plan;
    }

    fn local_section(&mut self, ui: &mut Ui, mut plan: Option<&mut kernelsmith_core::plan::BuildPlan>, q: &str) {
        section(ui, "My patches");
        dim(ui, &format!("Patches in {} are offered for every build.", paths::user_patch_dir().display()));
        ui.add_space(4.0);
        let mut delete = None;
        for lp in self.local_patches.clone() {
            if !q.is_empty() && !lp.name.to_lowercase().contains(q) {
                continue;
            }
            panel(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    ui.label(RichText::new("📄").size(18.0));
                    ui.label(RichText::new(&lp.name).font(bold(14.5)).color(TEXT_BRIGHT));
                    dim(ui, &human_bytes(lp.size));
                    if lp.system {
                        badge(ui, "System", TEXT_DIM);
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if !lp.system && ui.small_button("Delete").clicked() {
                            delete = Some(lp.path.clone());
                        }
                        if let Some(p) = plan.as_deref_mut() {
                            let id = format!("local:{}", lp.name);
                            let added = p.patches.iter().any(|x| x.id == id);
                            if added {
                                if button(ui, "Remove", Btn::Secondary).clicked() {
                                    p.patches.retain(|x| x.id != id);
                                }
                            } else if button(ui, "+ Add", Btn::Primary).clicked() {
                                p.patches.push(PlannedPatch { id, title: lp.name.clone(), file: lp.path.clone(), url: None });
                            }
                        }
                    });
                });
            });
            ui.add_space(4.0);
        }
        if let Some(path) = delete {
            let _ = std::fs::remove_file(&path);
            self.local_patches = patches::local_patches();
        }
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            ui.add(egui::TextEdit::singleline(&mut self.import_url).hint_text("https://…/something.patch").desired_width(420.0));
            let busy = self.import.is_loading();
            if button_enabled(ui, "Import from URL", Btn::Blue, !busy && self.import_url.starts_with("http")).clicked() {
                let fetcher = self.fetcher();
                let url = self.import_url.trim().to_string();
                let ctx = ui.ctx().clone();
                self.import = Load::spawn(&ctx, move || patches::import_from_url(&fetcher, &url).map_err(|e| format!("{e:#}")));
            }
            if busy {
                ui.add(egui::Spinner::new().color(ACCENT));
            }
            if button(ui, "Open folder", Btn::Secondary).clicked() {
                let _ = std::fs::create_dir_all(paths::user_patch_dir());
                open_path(&paths::user_patch_dir());
            }
            if button(ui, "↻", Btn::Secondary).on_hover_text("Rescan the folder").clicked() {
                self.local_patches = patches::local_patches();
            }
        });
        if self.import.poll() {
            match self.import.ready() {
                Some(Ok(p)) => {
                    let msg = format!("Imported {}", p.file_name().unwrap_or_default().to_string_lossy());
                    self.toast(msg, GREEN);
                    self.import_url.clear();
                    self.local_patches = patches::local_patches();
                }
                Some(Err(e)) => {
                    let e = e.clone();
                    self.modal = Some(super::modals::Modal::message("Import failed", e));
                }
                None => {}
            }
        }
    }
}

fn cat_item(ui: &mut Ui, label: &str, count: usize, selected: bool) -> egui::Response {
    let (rect, resp) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 32.0), Sense::click());
    let p = ui.painter();
    if selected {
        hgradient(p, rect, ACCENT_STRONG.linear_multiply(0.35), Color32::TRANSPARENT);
    } else if resp.hovered() {
        p.rect_filled(rect, egui::CornerRadius::same(3), Color32::from_white_alpha(8));
    }
    p.text(rect.left_center() + Vec2::new(10.0, 0.0), egui::Align2::LEFT_CENTER, label, regular(14.5), if selected { TEXT_BRIGHT } else { TEXT });
    p.text(rect.right_center() - Vec2::new(10.0, 0.0), egui::Align2::RIGHT_CENTER, count.to_string(), regular(12.5), TEXT_DIM);
    resp.on_hover_cursor(egui::CursorIcon::PointingHand)
}
