//! The window chrome: navigation bar, status bar, toasts and the global dialogs.

use super::jobs::JobKind;
use super::{Action, App, Tab, ToastKind};
use crate::data::Reload;
use crate::exec::HelperState;
use crate::portage::history;
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use crate::views;
use egui::{Align, Color32, CornerRadius, Frame, Layout, Margin, RichText, Stroke};
use egui_phosphor::regular as icon;
use std::time::Duration;

impl App {
    pub(super) fn nav_bar(&mut self, ui: &mut egui::Ui) {
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
                HelperState::Ready => {
                    (format!("{} Admin session", icon::LOCK_OPEN), GREEN, "Privileged actions run through the helper without asking again.".into())
                }
                HelperState::Starting => (format!("{} Authenticating…", icon::HOURGLASS), YELLOW, "Waiting for the polkit password dialog.".into()),
                HelperState::Failed(e) => (format!("{} Not authorized", icon::LOCK), RED, format!("Last attempt failed: {e}\nClick to retry.")),
                HelperState::NotStarted => (
                    format!("{} User mode", icon::LOCK),
                    TEXT_DIM,
                    "Browsing works without root. Click to unlock privileged actions (one password prompt per session).".into(),
                ),
            }
        };
        let r = ui
            .add(egui::Button::new(RichText::new(txt).color(col).size(13.0)).fill(Color32::from_rgb(0x22, 0x28, 0x33)).corner_radius(CornerRadius::same(12)))
            .on_hover_text(tip);
        if r.clicked() {
            self.push(Action::StartHelper);
        }
    }

    pub(super) fn status_bar(&mut self, ui: &mut egui::Ui) {
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
                ui.label(w::dim(format!("{} {} packages · {} ebuilds in {} repos", icon::DATABASE, c.pkgs.len(), c.ebuild_count, c.repo_locations.len())));
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
                    JobKind::Emerge { tracker, .. } => {
                        Some((tracker.fraction(), tracker.counts(), tracker.total, tracker.remaining(), tracker.elapsed(), tracker.last_status.clone()))
                    }
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

    pub(super) fn toasts_ui(&mut self, ctx: &egui::Context) {
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
    pub(super) fn close_guard(&mut self, ctx: &egui::Context) {
        if ctx.input(|i| i.viewport().close_requested()) && !self.allow_close && self.running_emerge().is_some() {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            self.confirm_close = true;
        }
        if !self.confirm_close {
            return;
        }
        let in_process = self.exec.is_root;
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
                    if w::danger(ui, icon::SIGN_OUT, "Quit anyway").clicked() {
                        self.allow_close = true;
                        self.confirm_close = false;
                        ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                });
            });
    }

    pub(super) fn review_ui(&mut self, ctx: &egui::Context) {
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
                egui::ScrollArea::vertical().max_height(ctx.content_rect().height() * 0.6).show(ui, |ui| {
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
                        ui.label(w::dim(if self.exec.root_ready() {
                            "Runs as root through the admin session"
                        } else {
                            "You will be asked for your password once"
                        }));
                    });
                });
            });
        if apply {
            let r = self.review.take().unwrap();
            self.apply_changes(r.title, r.changes, r.reload, r.then);
        } else if cancel {
            self.review = None;
        }
    }
}
