//! The "Library": installed packages, Steam-library style (list left, page right).

use super::common;
use crate::app::{Action, App};
use crate::portage::emerge::{self, EmergeOptions, Value};
use crate::portage::history::fmt_duration;
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Color32, Margin, RichText, Ui};
use egui_phosphor::regular as icon;
use std::collections::HashSet;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Filter {
    #[default]
    All,
    World,
    Deps,
    Updates,
    Orphans,
}

#[derive(Default)]
pub struct State {
    pub selected: Option<String>,
    pub filter: Filter,
    pub query: String,
    pub collapsed: HashSet<String>,
    pub collapse_all: bool,
}

enum Row {
    Header(String, usize),
    Item(String, String),
}

pub fn show(app: &mut App, ui: &mut Ui) {
    let Some(vdb) = app.data.vdb.clone() else {
        w::loading(ui, "Reading installed packages…");
        return;
    };
    let catalog = app.data.catalog.clone();
    let updates: HashSet<String> = app.data.updates.as_ref().map(|u| u.iter().map(|c| c.key.clone()).collect()).unwrap_or_default();

    egui::Panel::left("lib-side")
        .resizable(true)
        .default_size(300.0)
        .size_range(220.0..=520.0)
        .frame(egui::Frame::new().fill(SIDEBAR).inner_margin(Margin::symmetric(0, 8)))
        .show(ui, |ui| {
            egui::Frame::new().inner_margin(Margin::symmetric(10, 0)).show(ui, |ui| {
                ui.horizontal(|ui| {
                    if w::side_item(ui, icon::HOUSE, "Library home", app.library.selected.is_none(), None).clicked() {
                        app.library.selected = None;
                    }
                });
                ui.add_space(4.0);
                w::search_box(ui, &mut app.library.query, "Filter installed…", ui.available_width());
                ui.add_space(4.0);
                let up = format!("Updates {}", if updates.is_empty() { String::new() } else { updates.len().to_string() });
                w::segmented(
                    ui,
                    &mut app.library.filter,
                    &[(Filter::All, "All"), (Filter::World, "World"), (Filter::Deps, "Deps"), (Filter::Updates, up.as_str()), (Filter::Orphans, "Orphans")],
                );
                ui.add_space(4.0);
            });
            // Build rows
            let q = app.library.query.to_lowercase();
            let mut rows: Vec<Row> = Vec::new();
            let mut current_cat = String::new();
            let mut cat_count = 0usize;
            let mut header_pos = 0usize;
            let mut seen: HashSet<String> = HashSet::new();
            for p in &vdb.pkgs {
                let key = p.key();
                if !seen.insert(key.clone()) {
                    continue;
                }
                if !q.is_empty() && !key.contains(&q) {
                    continue;
                }
                let keep = match app.library.filter {
                    Filter::All => true,
                    Filter::World => vdb.in_world(&key),
                    Filter::Deps => !vdb.in_world(&key),
                    Filter::Updates => updates.contains(&key),
                    Filter::Orphans => catalog.as_ref().is_some_and(|c| c.get(&key).is_none()),
                };
                if !keep {
                    continue;
                }
                if p.cat != current_cat {
                    if !current_cat.is_empty()
                        && let Row::Header(_, n) = &mut rows[header_pos]
                    {
                        *n = cat_count;
                    }
                    current_cat = p.cat.clone();
                    header_pos = rows.len();
                    rows.push(Row::Header(p.cat.clone(), 0));
                    cat_count = 0;
                }
                cat_count += 1;
                if !app.library.collapsed.contains(&p.cat) {
                    rows.push(Row::Item(key, p.pn.clone()));
                }
            }
            if let Some(Row::Header(_, n)) = rows.get_mut(header_pos) {
                *n = cat_count;
            }
            ui.horizontal(|ui| {
                ui.add_space(10.0);
                ui.label(w::dim(format!("{} packages", seen.len())));
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.add_space(8.0);
                    if w::icon_button(ui, icon::ARROWS_IN_LINE_VERTICAL, "Collapse all categories").clicked() {
                        app.library.collapsed = vdb.pkgs.iter().map(|p| p.cat.clone()).collect();
                    }
                    if w::icon_button(ui, icon::ARROWS_OUT_LINE_VERTICAL, "Expand all").clicked() {
                        app.library.collapsed.clear();
                    }
                });
            });
            egui::ScrollArea::vertical().id_salt("lib-list").auto_shrink([false, false]).show_rows(ui, 30.0, rows.len(), |ui, range| {
                ui.spacing_mut().item_spacing.y = 0.0;
                for r in &rows[range] {
                    match r {
                        Row::Header(cat, n) => {
                            let collapsed = app.library.collapsed.contains(cat);
                            let ic = if collapsed { icon::CARET_RIGHT } else { icon::CARET_DOWN };
                            let resp = ui.add(
                                egui::Button::new(RichText::new(format!("{ic}  {}  ({n})", cat.to_uppercase())).size(11.5).family(theme::bold()).color(TEXT_DIM))
                                    .frame(false)
                                    .min_size(egui::vec2(ui.available_width(), 30.0))
                                    .right_text(""),
                            );
                            if resp.clicked() {
                                if collapsed {
                                    app.library.collapsed.remove(cat);
                                } else {
                                    app.library.collapsed.insert(cat.clone());
                                }
                            }
                        }
                        Row::Item(key, pn) => {
                            let sel = app.library.selected.as_deref() == Some(key.as_str());
                            let ic = if updates.contains(key) {
                                icon::ARROW_CIRCLE_UP
                            } else if vdb.in_world(key) {
                                icon::STAR
                            } else {
                                icon::PACKAGE
                            };
                            let resp = w::side_item(ui, ic, pn, sel, None);
                            if resp.clicked() {
                                app.library.selected = Some(key.clone());
                                app.detail.open(key);
                                app.push(Action::LoadDetail(key.clone()));
                            }
                            resp.context_menu(|ui| common::package_menu(app, ui, key, true));
                        }
                    }
                }
            });
        });

    egui::CentralPanel::default().frame(egui::Frame::new().fill(BG)).show(ui, |ui| {
        if app.library.selected.is_some() {
            super::details::show(app, ui, false);
        } else {
            library_home(app, ui);
        }
    });
}

fn library_home(app: &mut App, ui: &mut Ui) {
    let Some(vdb) = app.data.vdb.clone() else { return };
    let history = app.data.history.clone();
    common::page(ui, "lib-home", |ui| {
        w::title(ui, "Library");
        w::subtitle(ui, "Everything installed on this system. Pick a package on the left for details.");
        ui.add_space(10.0);
        ui.horizontal_top(|ui| {
            let orphans = app.data.catalog.as_ref().map(|c| vdb.by_key.keys().filter(|k| c.get(k).is_none()).count()).unwrap_or(0);
            for (label, value, col) in [
                ("Installed", vdb.by_key.len().to_string(), ACCENT),
                ("In @world", vdb.world_raw.len().to_string(), PURPLE),
                ("Disk usage", crate::fmt::size(vdb.total_size), GREEN),
                ("Possible updates", app.data.updates.as_ref().map(|u| u.len().to_string()).unwrap_or("…".into()), ORANGE),
                ("Not in any repo", orphans.to_string(), RED),
            ] {
                w::card().show(ui, |ui| {
                    ui.set_min_width(150.0);
                    ui.label(RichText::new(label.to_uppercase()).size(11.0).family(theme::bold()).color(TEXT_DIM));
                    ui.label(RichText::new(value).size(26.0).family(theme::bold()).color(col));
                });
            }
        });
        ui.add_space(8.0);
        ui.horizontal(|ui| {
            if w::big_button(ui, &format!("{}  UPDATE @WORLD", icon::ROCKET_LAUNCH), GRAD_GREEN, 200.0).clicked() {
                app.push(Action::Pretend((emerge::recipes()[0].build)()));
            }
            if w::big_button(ui, &format!("{}  CLEAN UNNEEDED", icon::BROOM), GRAD_GREY, 200.0)
                .on_hover_text("Preview emerge --depclean: remove dependencies nothing needs anymore")
                .clicked()
            {
                app.push(Action::Pretend(EmergeOptions { action: emerge::Action::Depclean, ..Default::default() }));
            }
            if w::big_button(ui, &format!("{}  PRESERVED-REBUILD", icon::WRENCH), GRAD_GREY, 200.0).clicked() {
                app.push(Action::Pretend(EmergeOptions::default().targets(&["@preserved-rebuild"])));
            }
        });
        ui.add_space(10.0);

        if let Some(u) = app.data.updates.clone()
            && !u.is_empty()
        {
            w::section(ui, &format!("Possible updates ({})", u.len()));
            let plan = app.world_plan.clone();
            egui::Grid::new("upd-grid").num_columns(6).striped(true).spacing([18.0, 6.0]).show(ui, |ui| {
                ui.label(w::dim("Package"));
                ui.label(w::dim("Installed"));
                ui.label(w::dim("Available"));
                ui.label(w::dim("Slot"));
                ui.label(w::dim("emerge -uDN @world"));
                ui.label(w::dim(""));
                ui.end_row();
                for c in u.iter().take(200) {
                    let st = crate::data::classify(c, plan.as_ref().map(|(p, _)| p.as_ref()));
                    if ui.link(RichText::new(&c.key).color(WHITE)).clicked() {
                        app.library.selected = Some(c.key.clone());
                        app.detail.open(&c.key);
                        app.push(Action::LoadDetail(c.key.clone()));
                    }
                    ui.label(&c.installed);
                    ui.label(RichText::new(&c.available).color(GREEN_TEXT));
                    ui.label(w::dim(&c.slot));
                    if st.label().is_empty() {
                        ui.label(w::dim("not checked"));
                    } else {
                        w::status_badge(ui, st.label()).on_hover_text(st.explain());
                    }
                    if ui.small_button(format!("{} Update", icon::ARROW_CIRCLE_UP)).clicked() {
                        app.push(Action::Pretend(EmergeOptions::default().with("--update", Value::On).targets(&[c.key.as_str()])));
                    }
                    ui.end_row();
                }
            });
        }

        if let Some(h) = history {
            w::section(ui, "Longest builds on this machine");
            let mut longest: Vec<(String, std::time::Duration)> = h.by_key.keys().filter_map(|k| h.estimate(k).map(|d| (k.clone(), d))).collect();
            longest.sort_by(|a, b| b.1.cmp(&a.1));
            let max = longest.first().map(|x| x.1.as_secs_f32()).unwrap_or(1.0).max(1.0);
            for (k, d) in longest.iter().take(12) {
                ui.horizontal(|ui| {
                    let r = ui.add_sized([240.0, 20.0], egui::Label::new(RichText::new(k).color(TEXT)).truncate().sense(egui::Sense::click()));
                    if r.clicked() {
                        app.library.selected = Some(k.clone());
                        app.detail.open(k);
                        app.push(Action::LoadDetail(k.clone()));
                    }
                    let wbar = (ui.available_width() - 90.0).max(50.0);
                    let (rect, _) = ui.allocate_exact_size(egui::vec2(wbar, 14.0), egui::Sense::hover());
                    ui.painter().rect_filled(rect, 2.0, Color32::from_rgb(0x0e, 0x14, 0x1b));
                    let fill = egui::Rect::from_min_size(rect.min, egui::vec2(rect.width() * d.as_secs_f32() / max, rect.height()));
                    w::paint_hgradient(ui.painter(), fill, Color32::from_rgb(0x1a, 0x9f, 0xff), Color32::from_rgb(0x47, 0xbf, 0xff));
                    ui.label(w::dim(fmt_duration(*d)));
                });
            }
        }
    });
}
