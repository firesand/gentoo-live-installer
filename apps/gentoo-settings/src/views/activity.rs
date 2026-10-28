//! The "Activity" tab: every transaction the helper recorded, with an undo.

use super::common::page;
use crate::app::App;
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Color32, RichText, Ui};
use egui_phosphor::regular as icon;
use gentoo_settings::catalog;
use gentoo_settings::history::HISTORY;
use gentoo_settings::plan::Change;

fn change_text(c: &Change) -> (String, bool) {
    let title = catalog::get(&c.id).map(|t| t.title).unwrap_or(&c.id);
    match (&c.value, c.enable) {
        (Some(v), true) => (format!("{title} = {v}"), true),
        (None, true) => (title.to_string(), true),
        (_, false) => (title.to_string(), false),
    }
}

pub fn show(app: &mut App, ui: &mut Ui) {
    let entries = app.history.clone();
    let mut undo: Option<(String, Vec<Change>)> = None;
    page(ui, "activity", |ui| {
        w::title(ui, "Activity");
        w::subtitle(ui, &format!("Every change Gentoo Settings made, newest first (from {HISTORY})."));
        ui.add_space(8.0);
        if entries.is_empty() {
            w::empty_state(
                ui,
                icon::CLOCK_COUNTER_CLOCKWISE,
                "Nothing applied yet",
                "Changes you apply show up here, with their files, backups and an undo button.",
            );
            return;
        }
        for (i, e) in entries.iter().enumerate() {
            w::card().show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    ui.label(RichText::new(if e.ok { icon::CHECK_CIRCLE } else { icon::X_CIRCLE }).size(22.0).color(if e.ok { GREEN } else { RED }));
                    ui.vertical(|ui| {
                        ui.horizontal(|ui| {
                            ui.label(RichText::new(if e.label.is_empty() { "Change" } else { &e.label }).size(15.0).family(theme::bold()).color(WHITE));
                            if !e.warnings.is_empty() {
                                w::status_badge(ui, "WARNING").on_hover_text(e.warnings.join("\n"));
                            }
                            if i == 0 {
                                w::status_badge(ui, "LATEST");
                            }
                        });
                        ui.label(w::dim(format!(
                            "{} ({}) · {} · uid {} · {} file{}",
                            crate::fmt::date(e.ts),
                            crate::fmt::ago(e.ts),
                            e.init,
                            e.uid,
                            e.files.len(),
                            if e.files.len() == 1 { "" } else { "s" }
                        )));
                    });
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        let r = ui
                            .add_enabled_ui(e.ok && !app.preview_init, |ui| w::button(ui, icon::ARROW_U_UP_LEFT, "Undo"))
                            .inner
                            .on_hover_text("Review a change that puts these tweaks back the way they were before");
                        if r.clicked() {
                            undo = Some((format!("Undo: {}", if e.label.is_empty() { "change" } else { &e.label }), e.undo.clone()));
                        }
                    });
                });
                ui.add_space(4.0);
                ui.horizontal_wrapped(|ui| {
                    for c in &e.changes {
                        let (text, on) = change_text(c);
                        let (fg, bg) = if on {
                            (GREEN_TEXT, Color32::from_rgb(0x2d, 0x40, 0x16))
                        } else {
                            (Color32::from_rgb(0xe8, 0x9a, 0x9a), Color32::from_rgb(0x40, 0x22, 0x22))
                        };
                        w::badge(ui, &format!("{} {text}", if on { "+" } else { "−" }), fg, bg);
                    }
                });
                if let Some(b) = &e.backup {
                    ui.label(w::dim(format!("{} previous files backed up in {b}", icon::FLOPPY_DISK)));
                }
                if !e.files.is_empty() {
                    egui::CollapsingHeader::new(RichText::new("Files").color(TEXT_DIM).size(12.5)).id_salt(("files", i)).show(ui, |ui| {
                        for f in &e.files {
                            ui.label(w::mono(f));
                        }
                    });
                }
            });
            ui.add_space(6.0);
        }
    });
    if let Some((label, changes)) = undo {
        // Skip entries that already describe the current state.
        let changes: Vec<Change> = changes
            .into_iter()
            .filter(|c| match app.status(&c.id) {
                Some(s) => c.enable != s.installed.is_some() || (c.enable && c.value.as_ref().is_some_and(|v| *v != s.value())),
                None => false,
            })
            .collect();
        app.open_review(label, changes);
    }
}
