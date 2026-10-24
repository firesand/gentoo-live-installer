//! Merge history from emerge.log: timeline, failures and build-time stats.

use super::common;
use crate::app::{Action, App};
use crate::data::Reload;
use crate::portage::history::fmt_duration;
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Color32, Margin, RichText, Ui};
use egui_phosphor::regular as icon;
use std::time::Duration;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Show {
    #[default]
    All,
    Failed,
    Unmerges,
    Sessions,
    /// What PortageHub changed as root (audit log), with undo.
    Changes,
}

const PAGES: &[(Show, &str)] =
    &[(Show::All, "Merges"), (Show::Failed, "Failures"), (Show::Unmerges, "Removals"), (Show::Sessions, "Commands"), (Show::Changes, "PortageHub changes")];

#[derive(Default)]
pub struct State {
    pub query: String,
    pub show: Show,
    /// Undo waiting for confirmation: (transaction, label, files).
    pub confirm_undo: Option<(String, String, Vec<String>)>,
    /// Undo refused because files changed since: (transaction, title, message).
    pub undo_conflict: Option<(String, String, String)>,
}

pub fn show(app: &mut App, ui: &mut Ui) {
    if app.history_view.show == Show::Changes {
        changes(app, ui);
        return;
    }
    let Some(h) = app.data.history.clone() else {
        w::loading(ui, "Reading /var/log/emerge.log…");
        return;
    };
    if !h.readable {
        w::empty_state(ui, icon::LOCK, "emerge.log is not readable", "Add your user to the portage group to see the history.");
        return;
    }
    egui::Frame::new().inner_margin(Margin::symmetric(24, 12)).show(ui, |ui| {
        ui.horizontal(|ui| {
            w::title(ui, "History");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                w::search_box(ui, &mut app.history_view.query, "Filter by package", 260.0);
            });
        });
        let ok: Vec<_> = h.merges.iter().filter(|m| m.ok).collect();
        let failed = h.merges.iter().filter(|m| !m.ok).count();
        let total: u64 = ok.iter().filter(|m| !m.binary).map(|m| m.duration().as_secs()).sum();
        let month_ago = crate::fmt::now_ts() - 30 * 86_400;
        let last30: u64 = ok.iter().filter(|m| m.end > month_ago && !m.binary).map(|m| m.duration().as_secs()).sum();
        ui.horizontal_top(|ui| {
            for (l, v, c) in [
                ("Merges", ok.len().to_string(), ACCENT),
                ("Failed builds", failed.to_string(), RED),
                ("Total compile time", fmt_duration(Duration::from_secs(total)), GREEN),
                ("Last 30 days", fmt_duration(Duration::from_secs(last30)), PURPLE),
                ("Syncs", h.syncs.len().to_string(), ORANGE),
                ("Median build", fmt_duration(h.median_build), TEXT),
            ] {
                w::card().show(ui, |ui| {
                    ui.set_min_width(140.0);
                    ui.label(RichText::new(l.to_uppercase()).size(11.0).family(theme::bold()).color(TEXT_DIM));
                    ui.label(RichText::new(v).size(22.0).family(theme::bold()).color(c));
                });
            }
        });
        ui.add_space(4.0);
        // Activity chart: compile hours per day for the last 60 days.
        chart(ui, &h);
        ui.add_space(4.0);
        w::segmented(ui, &mut app.history_view.show, PAGES);
    });
    let q = app.history_view.query.to_lowercase();
    egui::Frame::new().inner_margin(Margin::symmetric(24, 0)).show(ui, |ui| match app.history_view.show {
        Show::All | Show::Failed => {
            let rows: Vec<_> =
                h.merges.iter().rev().filter(|m| app.history_view.show == Show::All || !m.ok).filter(|m| q.is_empty() || m.cpv.contains(&q)).collect();
            egui::ScrollArea::vertical().id_salt("hist").auto_shrink([false, false]).show_rows(ui, 26.0, rows.len(), |ui, range| {
                for m in &rows[range] {
                    ui.horizontal(|ui| {
                        ui.set_height(24.0);
                        ui.add_sized([130.0, 20.0], egui::Label::new(w::dim(crate::fmt::date(m.end))));
                        if m.ok {
                            w::status_badge(ui, if m.binary { "BINARY" } else { "DONE" });
                        } else {
                            w::status_badge(ui, "FAILED");
                        }
                        let r = ui.add(egui::Label::new(RichText::new(&m.key).color(WHITE)).sense(egui::Sense::click()));
                        if r.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                            app.push(Action::Open(m.key.clone()));
                        }
                        ui.label(RichText::new(&m.ver).color(TEXT));
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            ui.label(RichText::new(fmt_duration(m.duration())).color(if m.ok { TEXT } else { RED }));
                        });
                    });
                }
            });
        }
        Show::Unmerges => {
            let rows: Vec<_> = h.unmerges.iter().rev().filter(|u| q.is_empty() || u.1.contains(&q)).collect();
            egui::ScrollArea::vertical().id_salt("unm").auto_shrink([false, false]).show_rows(ui, 24.0, rows.len(), |ui, range| {
                for (ts, cpv) in &rows[range] {
                    ui.horizontal(|ui| {
                        ui.add_sized([130.0, 20.0], egui::Label::new(w::dim(crate::fmt::date(*ts))));
                        w::status_badge(ui, "REMOVE");
                        ui.label(RichText::new(cpv).color(TEXT));
                    });
                }
            });
        }
        Show::Changes => {}
        Show::Sessions => {
            let rows: Vec<_> = h.sessions.iter().rev().filter(|s| q.is_empty() || s.args.contains(&q)).collect();
            egui::ScrollArea::vertical().id_salt("sess").auto_shrink([false, false]).show_rows(ui, 24.0, rows.len(), |ui, range| {
                for s in &rows[range] {
                    ui.horizontal(|ui| {
                        ui.add_sized([130.0, 20.0], egui::Label::new(w::dim(crate::fmt::date(s.start))));
                        ui.label(w::mono(format!("emerge {}", s.args)));
                        if ui.small_button(icon::COPY).on_hover_text("Copy command").clicked() {
                            ui.ctx().copy_text(format!("emerge {}", s.args));
                        }
                    });
                }
            });
        }
    });
    let _ = common::category_color;
}

/// Admin changes from the helper's audit log, each transaction with Undo.
fn changes(app: &mut App, ui: &mut Ui) {
    use crate::changes::ChangeState;
    if app.data.audit.is_none() && !app.is_loading(Reload::Audit) {
        app.push(Action::Reload(Reload::Audit));
    }
    egui::Frame::new().inner_margin(Margin::symmetric(24, 12)).show(ui, |ui| {
        ui.horizontal(|ui| {
            w::title(ui, "History");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if w::button(ui, icon::ARROW_CLOCKWISE, "Refresh").clicked() {
                    app.push(Action::Reload(Reload::Audit));
                }
                w::search_box(ui, &mut app.history_view.query, "Filter by file or text", 260.0);
            });
        });
        w::segmented(ui, &mut app.history_view.show, PAGES);
        ui.add(egui::Label::new(w::dim("Every change PortageHub made as root, from /var/log/portagehub/audit.log. Changes to files can be undone: the previous contents are kept for the last 50 changes.")).wrap());
    });
    let Some(log) = app.data.audit.clone() else {
        w::loading(ui, "Reading the audit log…");
        return;
    };
    if !log.readable || (log.changes.is_empty() && log.commands.is_empty()) {
        w::empty_state(ui, icon::CLOCK_COUNTER_CLOCKWISE, "No changes recorded yet", "Changes made through PortageHub 0.4 or newer appear here.");
        return;
    }
    let q = app.history_view.query.to_lowercase();
    let can_undo = app.exec.supports(&portagehub::protocol::Op::Rollback { transaction: String::new(), force: false });
    egui::Frame::new().inner_margin(Margin::symmetric(24, 0)).show(ui, |ui| {
        egui::ScrollArea::vertical().id_salt("changes").auto_shrink([false, false]).show(ui, |ui| {
            w::section(ui, "Changes to files");
            let shown: Vec<_> = log
                .changes
                .iter()
                .filter(|c| q.is_empty() || c.label.to_lowercase().contains(&q) || c.files.iter().any(|f| f.to_lowercase().contains(&q)))
                .take(200)
                .collect();
            if shown.is_empty() {
                ui.label(w::dim("Nothing matches."));
            }
            for c in shown {
                w::card().show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal(|ui| {
                        ui.add_sized([130.0, 20.0], egui::Label::new(w::dim(crate::fmt::date(c.ts))));
                        let (label, fg) = match c.state {
                            ChangeState::Saved => ("SAVED", GREEN),
                            ChangeState::Undone => ("UNDONE", TEXT_DIM),
                            ChangeState::RolledBack => ("ROLLED BACK", ORANGE),
                            ChangeState::Incomplete => ("INCOMPLETE", YELLOW),
                        };
                        w::badge(ui, label, fg, fg.gamma_multiply(0.18));
                        ui.label(RichText::new(&c.label).color(WHITE)).on_hover_text(format!("Requested by uid {} · transaction {}", c.uid, c.tx));
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if c.can_undo() {
                                let r = ui.add_enabled_ui(can_undo, |ui| w::button(ui, icon::ARROW_COUNTER_CLOCKWISE, "Undo")).inner;
                                if r.on_disabled_hover_text("The installed admin helper is too old for undo — run sudo dist/install.sh.").clicked() {
                                    app.history_view.confirm_undo = Some((c.tx.clone(), c.label.clone(), c.files.clone()));
                                }
                            } else if let Some(t) = c.undone_ts {
                                ui.label(w::dim(format!("{} {}", if c.state == ChangeState::Undone { "undone" } else { "rolled back" }, crate::fmt::ago(t))));
                            }
                        });
                    });
                    let lines: Vec<&String> = if c.files.is_empty() { c.steps.iter().collect() } else { c.files.iter().collect() };
                    for f in lines.iter().take(8) {
                        ui.label(w::mono(f.as_str()));
                    }
                    if lines.len() > 8 {
                        ui.label(w::dim(format!("… and {} more", lines.len() - 8)));
                    }
                });
                ui.add_space(4.0);
            }
            ui.add_space(8.0);
            w::section(ui, "Commands and other operations");
            for c in log.commands.iter().filter(|c| q.is_empty() || c.what.to_lowercase().contains(&q)).take(300) {
                ui.horizontal(|ui| {
                    ui.add_sized([130.0, 20.0], egui::Label::new(w::dim(crate::fmt::date(c.ts))));
                    match c.ok {
                        Some(true) => w::status_badge(ui, "DONE"),
                        Some(false) => w::status_badge(ui, "FAILED"),
                        None => w::status_badge(ui, "NO RESULT"),
                    };
                    ui.label(w::mono(&c.what)).on_hover_text(format!(
                        "Requested by uid {}{}",
                        c.uid,
                        if c.detail.is_empty() { String::new() } else { format!(" · {}", c.detail) }
                    ));
                });
            }
        });
    });
    undo_dialogs(app, ui.ctx());
}

fn undo_dialogs(app: &mut App, ctx: &egui::Context) {
    use egui::{CornerRadius, Frame, Modal};
    let frame = Frame::new().fill(PANEL).corner_radius(CornerRadius::same(6)).inner_margin(Margin::same(18));
    if let Some((tx, label, files)) = app.history_view.confirm_undo.clone() {
        let mut close = false;
        Modal::new(egui::Id::new("confirm-undo")).frame(frame).show(ctx, |ui| {
            ui.set_width(520.0);
            w::title(ui, "Undo this change?");
            w::subtitle(ui, &label);
            ui.label(
                RichText::new(if files.is_empty() {
                    "Every file this change touched gets its previous contents back.".to_string()
                } else {
                    "These files get their previous contents back (files the change created are removed):".to_string()
                })
                .color(TEXT),
            );
            for f in files.iter().take(12) {
                ui.label(w::mono(f.as_str()));
            }
            ui.add_space(8.0);
            ui.horizontal(|ui| {
                if w::primary(ui, icon::ARROW_COUNTER_CLOCKWISE, "Undo").clicked() {
                    app.push(undo_action(tx.clone(), &label, false));
                    close = true;
                }
                if w::button(ui, "", "Cancel").clicked() {
                    close = true;
                }
            });
        });
        if close {
            app.history_view.confirm_undo = None;
        }
    }
    if let Some((tx, title, msg)) = app.history_view.undo_conflict.clone() {
        let mut close = false;
        Modal::new(egui::Id::new("undo-conflict")).frame(frame).show(ctx, |ui| {
            ui.set_width(520.0);
            ui.horizontal(|ui| {
                ui.label(RichText::new(icon::WARNING).size(22.0).color(ORANGE));
                w::title(ui, "Files changed since then");
            });
            ui.add(
                egui::Label::new(
                    RichText::new(format!(
                        "{msg}.\nUndoing anyway replaces those later edits with the contents from before “{}”.",
                        title.trim_start_matches("Undo: ")
                    ))
                    .color(TEXT),
                )
                .wrap(),
            );
            ui.add_space(8.0);
            ui.horizontal(|ui| {
                if w::danger(ui, icon::ARROW_COUNTER_CLOCKWISE, "Undo anyway").clicked() {
                    app.push(undo_action(tx.clone(), title.trim_start_matches("Undo: "), true));
                    close = true;
                }
                if w::button(ui, "", "Keep the files").clicked() {
                    close = true;
                }
            });
        });
        if close {
            app.history_view.undo_conflict = None;
        }
    }
}

fn undo_action(tx: String, label: &str, force: bool) -> Action {
    Action::RootOp {
        title: format!("Undo: {label}"),
        op: portagehub::protocol::Op::Rollback { transaction: tx.clone(), force },
        kind: crate::app::JobKind::Undo { tx },
        reload: vec![],
    }
}

fn chart(ui: &mut Ui, h: &crate::portage::history::History) {
    let days = 60usize;
    let now = crate::fmt::now_ts();
    let mut buckets = vec![0u64; days];
    for m in h.merges.iter().filter(|m| m.ok) {
        let age = (now - m.end) / 86_400;
        if age >= 0 && (age as usize) < days {
            buckets[days - 1 - age as usize] += m.duration().as_secs();
        }
    }
    let max = buckets.iter().copied().max().unwrap_or(0).max(60) as f32;
    let width = ui.available_width();
    let (rect, resp) = ui.allocate_exact_size(egui::vec2(width, 70.0), egui::Sense::hover());
    let p = ui.painter();
    p.rect_filled(rect, 3.0, Color32::from_rgb(0x14, 0x1d, 0x28));
    let bw = rect.width() / days as f32;
    let mut hovered = None;
    for (i, b) in buckets.iter().enumerate() {
        let hgt = (*b as f32 / max) * (rect.height() - 14.0);
        let r = egui::Rect::from_min_max(
            egui::pos2(rect.left() + i as f32 * bw + 1.0, rect.bottom() - 4.0 - hgt),
            egui::pos2(rect.left() + (i + 1) as f32 * bw - 1.0, rect.bottom() - 4.0),
        );
        let hover = resp.hover_pos().is_some_and(|pos| pos.x >= r.left() - 1.0 && pos.x <= r.right() + 1.0);
        if hover {
            hovered = Some((i, *b));
        }
        p.rect_filled(r, 1.0, if hover { ACCENT } else { ACCENT_DIM });
    }
    p.text(rect.left_top() + egui::vec2(8.0, 4.0), egui::Align2::LEFT_TOP, "Compile time per day — last 60 days", egui::FontId::proportional(11.5), TEXT_DIM);
    if let Some((i, b)) = hovered {
        resp.on_hover_text(format!("{} days ago: {}", days - 1 - i, fmt_duration(Duration::from_secs(b))));
    }
}
