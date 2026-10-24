//! Merge history from emerge.log: timeline, failures and build-time stats.

use super::common;
use crate::app::{Action, App};
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
}

#[derive(Default)]
pub struct State {
    pub query: String,
    pub show: Show,
}

pub fn show(app: &mut App, ui: &mut Ui) {
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
        w::segmented(ui, &mut app.history_view.show, &[(Show::All, "Merges"), (Show::Failed, "Failures"), (Show::Unmerges, "Removals"), (Show::Sessions, "Commands")]);
    });
    let q = app.history_view.query.to_lowercase();
    egui::Frame::new().inner_margin(Margin::symmetric(24, 0)).show(ui, |ui| match app.history_view.show {
        Show::All | Show::Failed => {
            let rows: Vec<_> = h
                .merges
                .iter()
                .rev()
                .filter(|m| app.history_view.show == Show::All || !m.ok)
                .filter(|m| q.is_empty() || m.cpv.contains(&q))
                .collect();
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
        let r = egui::Rect::from_min_max(egui::pos2(rect.left() + i as f32 * bw + 1.0, rect.bottom() - 4.0 - hgt), egui::pos2(rect.left() + (i + 1) as f32 * bw - 1.0, rect.bottom() - 4.0));
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
