//! /etc/portage editor: every package.* file as switchable rows, plus
//! make.conf, env files, profiles, news, config updates and stray files.

use super::common;
use crate::app::{Action, App, FileChange, JobKind};
use crate::data::Reload;
use crate::edits;
use crate::portage::atom::Op as AOp;
use crate::portage::config::{self, ConfKind, ConfSet, Entry, Line, StrayReason};
use crate::portage::makeconf::{self, COMMON_FEATURES};
use crate::portage::system::{self, PendingUpdate};
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Color32, Margin, RichText, Ui};
use egui_phosphor::regular as icon;
use portagehub::protocol::Op;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Page {
    #[default]
    Overview,
    Kind(ConfKind),
    MakeConf,
    EnvFiles,
    Profile,
    News,
    Updates,
    Strays,
    /// The helper's journal: multi-file changes that can be undone.
    Changes,
}

#[derive(Default)]
pub struct Wizard {
    pub query: String,
    pub key: Option<String>,
    pub op: Option<AOp>,
    pub version: String,
    pub slot: String,
    pub repo: String,
    pub values: String,
    pub file: String,
}

#[derive(Default)]
pub struct State {
    pub page: Page,
    pub file_filter: Option<PathBuf>,
    pub filter: String,
    pub editing: Option<(PathBuf, usize, String)>,
    pub raw: Option<(PathBuf, String)>,
    pub wizard: Wizard,
    pub show_wizard: bool,
    pub news_open: Option<String>,
    pub mk_raw: Option<String>,
    pub mk_edit: Option<(String, String)>,
    pub env_edit: Option<(PathBuf, String)>,
    pub env_new: String,
    pub profile_pick: Option<usize>,
    pub updates: Option<Arc<Mutex<Option<Vec<PendingUpdate>>>>>,
    pub stray_names: std::collections::HashMap<PathBuf, String>,
}

impl State {
    pub fn open_news(&mut self, name: &str) {
        self.page = Page::News;
        self.news_open = Some(name.to_string());
    }
}

pub fn show(app: &mut App, ui: &mut Ui) {
    let Some(cfg) = app.data.config.clone() else {
        w::loading(ui, "Reading /etc/portage…");
        return;
    };
    egui::Panel::left("cfg-side")
        .resizable(true)
        .default_size(270.0)
        .frame(egui::Frame::new().fill(SIDEBAR).inner_margin(Margin::symmetric(0, 8)))
        .show(ui, |ui| {
            egui::ScrollArea::vertical().id_salt("cfg-side-scroll").show(ui, |ui| {
                ui.spacing_mut().item_spacing.y = 0.0;
                if w::side_item(ui, icon::HOUSE, "Overview", app.config_view.page == Page::Overview, None).clicked() {
                    app.config_view.page = Page::Overview;
                }
                side_header(ui, "PACKAGE SETTINGS");
                for k in ConfKind::ALL {
                    if k.is_advanced() && !app.settings.show_advanced {
                        continue;
                    }
                    let count = cfg.set(k).map(|s| s.entry_count());
                    let sel = app.config_view.page == Page::Kind(k);
                    if w::side_item(ui, k.icon(), k.title(), sel, count).on_hover_text(k.path().to_string_lossy()).clicked() {
                        app.config_view.page = Page::Kind(k);
                        app.config_view.file_filter = None;
                        app.config_view.editing = None;
                        app.config_view.raw = None;
                    }
                }
                side_header(ui, "SYSTEM");
                for (p, ic, label) in [
                    (Page::MakeConf, icon::GEAR_SIX, "make.conf"),
                    (Page::EnvFiles, icon::FILE_CODE, "Environment files (env/)"),
                    (Page::Profile, icon::USER_CIRCLE_GEAR, "Profile"),
                ] {
                    let n = if p == Page::EnvFiles { Some(cfg.env_files.len()) } else { None };
                    if w::side_item(ui, ic, label, app.config_view.page == p, n).clicked() {
                        app.config_view.page = p;
                        if p == Page::Profile && app.data.profiles.is_none() {
                            app.push(Action::Reload(Reload::Profiles));
                        }
                    }
                }
                side_header(ui, "MAINTENANCE");
                let unread = app.data.news.as_ref().map(|n| n.iter().filter(|x| x.unread).count()).unwrap_or(0);
                if w::side_item(ui, icon::NEWSPAPER, "News", app.config_view.page == Page::News, Some(unread)).clicked() {
                    app.config_view.page = Page::News;
                }
                if w::side_item(ui, icon::FILE_ARROW_UP, "Config file updates", app.config_view.page == Page::Updates, None).clicked() {
                    app.config_view.page = Page::Updates;
                    app.config_view.updates = None;
                }
                if w::side_item(ui, icon::CLOCK_COUNTER_CLOCKWISE, "Recent changes (undo)", app.config_view.page == Page::Changes, None).clicked() {
                    app.config_view.page = Page::Changes;
                    if app.recent_changes.is_none() && app.exec.root_ready() {
                        app.list_changes();
                    }
                }
                let strays = cfg.strays.len();
                let label = if strays > 0 { format!("Stray files {}", icon::WARNING) } else { "Stray files".to_string() };
                if w::side_item(ui, icon::QUESTION, &label, app.config_view.page == Page::Strays, Some(strays)).clicked() {
                    app.config_view.page = Page::Strays;
                }
            });
        });
    undo_bar(app, ui);
    egui::CentralPanel::default().frame(egui::Frame::new().fill(BG)).show(ui, |ui| match app.config_view.page {
        Page::Overview => overview(app, ui),
        Page::Kind(k) => kind_page(app, ui, k),
        Page::MakeConf => make_conf(app, ui),
        Page::EnvFiles => env_files(app, ui),
        Page::Profile => profile(app, ui),
        Page::News => news(app, ui),
        Page::Updates => updates(app, ui),
        Page::Strays => strays(app, ui),
        Page::Changes => changes(app, ui),
    });
}

/// "Saved … Undo" strip after a change that the helper recorded as a transaction.
fn undo_bar(app: &mut App, ui: &mut Ui) {
    let Some(last) = app.last_change.clone() else { return };
    egui::Panel::top("cfg-undo").frame(egui::Frame::new().fill(PANEL).inner_margin(Margin::symmetric(14, 6))).show_separator_line(false).show(ui, |ui| {
        ui.horizontal(|ui| {
            ui.label(RichText::new(icon::CHECK_CIRCLE).color(GREEN));
            ui.label(RichText::new(format!("Saved: {}", last.title)).color(TEXT));
            if w::button(ui, icon::ARROW_COUNTER_CLOCKWISE, "Undo").on_hover_text("Restore the files as they were before this change (refused if one was edited since)").clicked() {
                app.undo_change(&last.tx, &last.title, false);
            }
            if w::icon_button(ui, icon::X, "Dismiss").clicked() {
                app.last_change = None;
            }
        });
    });
}

fn changes(app: &mut App, ui: &mut Ui) {
    let loading = app.jobs.iter().any(|j| j.running() && matches!(j.kind, JobKind::ChangeList));
    let ready = app.exec.root_ready();
    common::page(ui, "cfg-changes", |ui| {
        ui.horizontal(|ui| {
            w::title(ui, "Recent changes");
            if ready && !loading && w::icon_button(ui, icon::ARROWS_CLOCKWISE, "Reload").clicked() {
                app.list_changes();
            }
        });
        w::subtitle(ui, "Changes that touch several files at once are recorded by the admin helper, so they can be undone later. Undo restores the files exactly as they were, and refuses when one of them was edited after the change.");
        ui.add_space(6.0);
        if !ready {
            ui.label(w::dim("The journal is readable only by root. Start the admin session to see it."));
            if w::primary(ui, icon::LOCK_OPEN, "Start admin session").clicked() {
                app.push(Action::StartHelper);
            }
            return;
        }
        let Some(list) = app.recent_changes.clone() else {
            if loading {
                w::loading(ui, "Reading the journal…");
            } else if w::primary(ui, icon::ARROWS_CLOCKWISE, "Load recent changes").clicked() {
                app.list_changes();
            }
            return;
        };
        if list.is_empty() {
            w::empty_state(ui, icon::CLOCK_COUNTER_CLOCKWISE, "Nothing recorded yet", "Multi-file changes you make with PortageHub show up here.");
            return;
        }
        for tx in &list {
            w::card().show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    ui.label(RichText::new(icon::CLOCK_COUNTER_CLOCKWISE).size(18.0).color(ACCENT));
                    ui.vertical(|ui| {
                        ui.label(w::strong(&tx.label));
                        let when = tx.id.split('-').next().and_then(|t| t.parse::<i64>().ok()).unwrap_or(tx.created);
                        ui.label(w::dim(format!("{} · {} file(s)", crate::fmt::ago(when), tx.files.len())));
                    });
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| match tx.state.as_str() {
                        "committed" => {
                            if app.settings.show_advanced && w::danger(ui, icon::WARNING, "Undo anyway").on_hover_text("Also overwrite files edited since the change").clicked() {
                                app.undo_change(&tx.id, &tx.label, true);
                            }
                            if w::button(ui, icon::ARROW_COUNTER_CLOCKWISE, "Undo").clicked() {
                                app.undo_change(&tx.id, &tx.label, false);
                            }
                        }
                        "rolled_back" => {
                            w::badge(ui, "UNDONE", TEXT, Color32::from_rgb(0x3a, 0x46, 0x55));
                        }
                        _ => {
                            w::badge(ui, "UNFINISHED", Color32::from_rgb(0x2b, 0x1a, 0x00), ORANGE);
                        }
                    });
                });
                egui::CollapsingHeader::new("Files").id_salt(&tx.id).show(ui, |ui| {
                    for f in &tx.files {
                        ui.label(w::mono(f));
                    }
                });
            });
            ui.add_space(3.0);
        }
    });
}

fn side_header(ui: &mut Ui, t: &str) {
    ui.add_space(10.0);
    ui.horizontal(|ui| {
        ui.add_space(12.0);
        ui.label(RichText::new(t).size(11.0).family(theme::bold()).color(TEXT_FAINT));
    });
    ui.add_space(4.0);
}

fn overview(app: &mut App, ui: &mut Ui) {
    let Some(cfg) = app.data.config.clone() else { return };
    common::page(ui, "cfg-overview", |ui| {
        w::title(ui, "Configuration");
        w::subtitle(ui, "Everything below /etc/portage that shapes how Portage builds and selects packages.");
        ui.add_space(8.0);
        if !cfg.strays.is_empty() {
            w::callout(ui, ORANGE, icon::WARNING, &format!("{} file(s) in /etc/portage are ignored by Portage because of their name. Open “Stray files” to fix them.", cfg.strays.len()));
            ui.add_space(6.0);
        }
        let per_row = ((ui.available_width() + 10.0) / 330.0).floor().max(1.0) as usize;
        let kinds: Vec<ConfKind> = ConfKind::ALL.iter().copied().filter(|k| !k.is_advanced() || app.settings.show_advanced).collect();
        for row in kinds.chunks(per_row) {
            ui.horizontal_top(|ui| {
                for k in row {
                    let set = cfg.set(*k);
                    let r = w::card()
                        .show(ui, |ui| {
                            ui.set_width(296.0);
                            ui.set_height(108.0);
                            ui.horizontal(|ui| {
                                ui.label(RichText::new(k.icon()).size(26.0).color(ACCENT));
                                ui.vertical(|ui| {
                                    ui.label(RichText::new(k.title()).size(16.0).family(theme::bold()).color(WHITE));
                                    let info = match set {
                                        Some(s) if !s.exists => "not created yet".to_string(),
                                        Some(s) if s.is_dir => format!("{} entries in {} files", s.entry_count(), s.files.len()),
                                        Some(s) => format!("{} entries (single file)", s.entry_count()),
                                        None => String::new(),
                                    };
                                    ui.label(w::dim(info));
                                });
                            });
                            let ex = k.explain();
                            let short: String = ex.split(". ").next().unwrap_or(ex).chars().take(110).collect();
                            ui.add(egui::Label::new(RichText::new(short).size(12.0).color(TEXT_DIM)).wrap());
                        })
                        .response
                        .interact(egui::Sense::click())
                        .on_hover_cursor(egui::CursorIcon::PointingHand);
                    if r.clicked() {
                        app.config_view.page = Page::Kind(*k);
                    }
                }
            });
            ui.add_space(8.0);
        }
    });
}

fn kind_page(app: &mut App, ui: &mut Ui, kind: ConfKind) {
    let Some(cfg) = app.data.config.clone() else { return };
    let Some(set) = cfg.set(kind).cloned() else { return };
    common::page(ui, &format!("kind-{kind:?}"), |ui| {
        ui.horizontal(|ui| {
            ui.label(RichText::new(kind.icon()).size(30.0).color(ACCENT));
            ui.vertical(|ui| {
                w::title(ui, kind.title());
                ui.label(w::mono(set.base.to_string_lossy()));
            });
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                w::link(ui, &format!("{} Gentoo wiki", icon::BOOK_OPEN), kind.wiki());
            });
        });
        ui.add(egui::Label::new(RichText::new(kind.explain()).color(TEXT)).wrap());
        ui.add_space(6.0);
        // Layout info + conversion
        ui.horizontal(|ui| {
            if !set.exists {
                w::badge(ui, "NOT CREATED", TEXT, Color32::from_rgb(0x3a, 0x46, 0x55));
                ui.label(w::dim(format!("Adding an entry creates {}/{}", set.base.display(), app.settings.target_file)));
            } else if set.is_dir {
                w::badge(ui, "DIRECTORY", WHITE, Color32::from_rgb(0x2a, 0x6f, 0x97));
                ui.label(w::dim(format!("{} files — Portage reads them all in alphabetical order", set.files.len())));
            } else {
                w::badge(ui, "SINGLE FILE", Color32::from_rgb(0x2b, 0x1a, 0x00), ORANGE);
                if w::button(ui, icon::FOLDER_PLUS, "Convert to directory").on_hover_text(format!("Moves the file to {}/main so you can split settings into several files", set.base.display())).clicked() {
                    app.push(Action::Batch { title: format!("Convert {} to a directory", kind.file_name()), ops: edits::convert_to_dir_ops(&set.base), reload: vec![Reload::Config], then: None });
                }
            }
            if kind == ConfKind::Keywords && set.entry_count() > 0 {
                ui.label(RichText::new(format!("{} deprecated — consider moving these to package.accept_keywords", icon::WARNING)).color(ORANGE));
            }
        });
        if let Some(why) = cfg.root_symlink.then(|| config::symlink_message(std::path::Path::new(config::ETC_PORTAGE))).or_else(|| set.symlink.then(|| config::symlink_message(&set.base))) {
            ui.add_space(4.0);
            w::callout(ui, ORANGE, icon::LOCK, &why);
        }
        ui.add_space(6.0);

        // Add wizard
        let wiz_open = app.config_view.show_wizard;
        if w::primary(ui, if wiz_open { icon::CARET_UP } else { icon::PLUS }, if wiz_open { "Hide the add form" } else { "Add an entry" }).clicked() {
            app.config_view.show_wizard = !wiz_open;
        }
        if app.config_view.show_wizard {
            wizard(app, ui, kind, &set);
        }
        ui.add_space(8.0);

        // Files filter
        if set.is_dir && !set.files.is_empty() {
            ui.horizontal_wrapped(|ui| {
                if w::chip(ui, &format!("All files ({})", set.files.len()), app.config_view.file_filter.is_none()).clicked() {
                    app.config_view.file_filter = None;
                }
                for f in &set.files {
                    let n = f.entries().filter(|(_, e)| !e.disabled).count();
                    let label = if f.ignored { format!("{} {} (ignored)", icon::EYE_SLASH, f.rel) } else { format!("{} ({n})", f.rel) };
                    let r = w::chip(ui, &label, app.config_view.file_filter.as_ref() == Some(&f.path));
                    let r = if f.ignored { r.on_hover_text("Portage skips files starting with . or ending with ~") } else { r };
                    if r.clicked() {
                        app.config_view.file_filter = Some(f.path.clone());
                    }
                }
            });
        }
        ui.horizontal(|ui| {
            w::search_box(ui, &mut app.config_view.filter, "Filter entries", 300.0);
            if let Some(fp) = app.config_view.file_filter.clone().or_else(|| (!set.is_dir && set.exists).then(|| set.base.clone())) {
                if let Some(why) = cfg.edit_blocked_for(&fp) {
                    ui.label(RichText::new(format!("{} {why}", icon::LOCK)).color(ORANGE));
                } else if w::button(ui, icon::CODE, "Edit as text").clicked() {
                    let text = set.file(&fp).map(|f| f.raw.clone()).unwrap_or_default();
                    app.config_view.raw = Some((fp.clone(), text));
                }
                if set.is_dir && cfg.edit_blocked_for(&fp).is_none() && w::button(ui, icon::TRASH, "Delete this file").clicked() {
                    let old = set.file(&fp).map(|f| f.raw.clone()).unwrap_or_default();
                    app.push(Action::Write { title: format!("Delete {}", fp.display()), changes: vec![FileChange { path: fp.clone(), old, new: None }], reload: vec![Reload::Config], then: None });
                    app.config_view.file_filter = None;
                }
            }
        });
        // Raw editor
        if let Some((path, text)) = app.config_view.raw.as_mut() {
            let path = path.clone();
            w::card().show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.label(w::mono(path.to_string_lossy()));
                ui.add(egui::TextEdit::multiline(text).code_editor().desired_rows(14).desired_width(f32::INFINITY));
            });
            let mut close = false;
            ui.horizontal(|ui| {
                if w::primary(ui, icon::FLOPPY_DISK, "Save file").clicked() {
                    let old = set.file(&path).map(|f| f.raw.clone()).unwrap_or_default();
                    let mut new = app.config_view.raw.as_ref().unwrap().1.clone();
                    if !new.ends_with('\n') {
                        new.push('\n');
                    }
                    app.push(Action::Write { title: format!("Edit {}", path.display()), changes: vec![FileChange { path: path.clone(), old, new: Some(new) }], reload: vec![Reload::Config], then: None });
                    close = true;
                }
                if w::button(ui, "", "Close editor").clicked() {
                    close = true;
                }
            });
            if close {
                app.config_view.raw = None;
            }
            ui.add_space(8.0);
        }
        entries_table(app, ui, kind, &set);
    });
}

fn entries_table(app: &mut App, ui: &mut Ui, kind: ConfKind, set: &ConfSet) {
    let Some(cfg) = app.data.config.clone() else { return };
    let filter = app.config_view.filter.to_lowercase();
    let mut any = false;
    for f in &set.files {
        if let Some(ff) = &app.config_view.file_filter
            && *ff != f.path
        {
            continue;
        }
        let entries: Vec<(usize, Entry)> = f
            .entries()
            .filter(|(_, e)| filter.is_empty() || e.render().to_lowercase().contains(&filter))
            .map(|(i, e)| (i, e.clone()))
            .collect();
        if let Some(why) = &f.unreadable {
            any = true;
            w::section(ui, &f.rel);
            w::callout(ui, ORANGE, icon::WARNING, why);
            continue;
        }
        if entries.is_empty() {
            continue;
        }
        any = true;
        w::section(ui, &format!("{}{}", f.rel, if f.ignored { "  (ignored by Portage)" } else { "" }));
        if f.symlink {
            w::callout(ui, ORANGE, icon::LOCK, &config::symlink_message(&f.path));
        }
        for (idx, e) in entries {
            let editing = app.config_view.editing.as_ref().is_some_and(|(p, i, _)| *p == f.path && *i == idx);
            let mut comment_above = None;
            if idx > 0
                && let Some(Line::Comment(c)) = f.lines.get(idx - 1)
            {
                comment_above = Some(c.trim_start_matches('#').trim().to_string());
            }
            egui::Frame::new()
                .fill(if e.disabled { Color32::from_rgb(0x14, 0x1b, 0x24) } else { CARD })
                .corner_radius(3)
                .inner_margin(Margin::symmetric(10, 6))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal(|ui| {
                        let mut on = !e.disabled;
                        if w::toggle(ui, &mut on).on_hover_text(if on { "Active — switch off to comment it out" } else { "Commented out — switch on to activate" }).changed() {
                            let mut ne = e.clone();
                            ne.disabled = !on;
                            let ch = edits::replace_entry(&cfg, &f.path, idx, Some(ne)).map(|c| vec![c]);
                            app.push_write(format!("{} {}", if on { "Enable" } else { "Disable" }, e.atom_text), ch, vec![Reload::Config], None);
                        }
                        if editing {
                            let text = &mut app.config_view.editing.as_mut().unwrap().2;
                            ui.add(egui::TextEdit::singleline(text).font(egui::TextStyle::Monospace).desired_width(ui.available_width() - 200.0));
                            if w::primary(ui, icon::CHECK, "Save").clicked() {
                                let t = app.config_view.editing.take().unwrap().2;
                                let mut toks = t.split_whitespace();
                                if let Some(a) = toks.next() {
                                    let mut ne = Entry::new(a, toks.map(String::from).collect());
                                    ne.comment = e.comment.clone();
                                    ne.disabled = e.disabled;
                                    let ch = edits::replace_entry(&cfg, &f.path, idx, Some(ne)).map(|c| vec![c]);
                                    app.push_write(format!("Edit {}", e.atom_text), ch, vec![Reload::Config], None);
                                }
                            }
                            if w::button(ui, "", "Cancel").clicked() {
                                app.config_view.editing = None;
                            }
                            return;
                        }
                        ui.vertical(|ui| {
                            ui.horizontal_wrapped(|ui| {
                                let valid = e.atom.is_some() || e.atom_text.starts_with('@');
                                let col = if e.disabled { TEXT_FAINT } else if valid { WHITE } else { RED };
                                let r = ui.add(egui::Label::new(RichText::new(&e.atom_text).monospace().color(col)).sense(egui::Sense::click()));
                                if !valid {
                                    r.clone().on_hover_text("Not a valid atom — Portage will warn about this line");
                                }
                                if let Some(a) = &e.atom
                                    && !a.is_wildcard()
                                    && r.on_hover_text("Open package").clicked()
                                {
                                    app.push(Action::Open(a.key()));
                                }
                                for v in &e.values {
                                    if kind == ConfKind::Use || kind == ConfKind::ProfileUseMask || kind == ConfKind::ProfileUseForce {
                                        let (n, onf) = match v.strip_prefix('-') {
                                            Some(x) => (x, false),
                                            None => (v.as_str(), true),
                                        };
                                        w::flag_chip(ui, n, onf, false);
                                    } else {
                                        w::badge(ui, v, TEXT, Color32::from_rgb(0x2a, 0x35, 0x43));
                                    }
                                }
                            });
                            if app.settings.beginner_tips {
                                ui.label(RichText::new(e.describe(kind)).size(12.0).color(TEXT_DIM));
                            }
                            if let Some(c) = comment_above.as_ref().filter(|c| !c.is_empty()) {
                                ui.label(RichText::new(format!("# {c}")).size(11.5).color(TEXT_FAINT));
                            }
                            if let Some(c) = &e.comment {
                                ui.label(RichText::new(format!("#{c}")).size(11.5).color(TEXT_FAINT));
                            }
                        });
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if w::icon_button(ui, icon::TRASH, "Delete line").clicked() {
                                let ch = edits::replace_entry(&cfg, &f.path, idx, None).map(|c| vec![c]);
                                app.push_write(format!("Delete {}", e.atom_text), ch, vec![Reload::Config], None);
                            }
                            if w::icon_button(ui, icon::PENCIL_SIMPLE, "Edit line").clicked() {
                                let mut t = e.atom_text.clone();
                                for v in &e.values {
                                    t.push(' ');
                                    t.push_str(v);
                                }
                                app.config_view.editing = Some((f.path.clone(), idx, t));
                            }
                        });
                    });
                });
            ui.add_space(2.0);
        }
    }
    if !any {
        w::empty_state(ui, kind.icon(), "No entries", if filter.is_empty() { "Use “Add an entry” to create the first one." } else { "Nothing matches the filter." });
    }
}

fn wizard(app: &mut App, ui: &mut Ui, kind: ConfKind, set: &ConfSet) {
    let Some(cfg) = app.data.config.clone() else { return };
    let catalog = app.data.catalog.clone();
    w::card_tinted(ACCENT).show(ui, |ui| {
        ui.set_width(ui.available_width());
        let wz = &mut app.config_view.wizard;
        egui::Grid::new("wizard").num_columns(2).spacing([14.0, 10.0]).show(ui, |ui| {
            ui.label(w::strong("1. Package"));
            ui.vertical(|ui| {
                ui.horizontal(|ui| {
                    w::search_box(ui, &mut wz.query, "Search a package, or type */* / category/*", 340.0);
                    if let Some(k) = &wz.key {
                        w::badge(ui, k, WHITE, Color32::from_rgb(0x2a, 0x6f, 0x97));
                    }
                });
                let q = wz.query.trim().to_lowercase();
                if !q.is_empty() && wz.key.as_deref() != Some(q.as_str()) {
                    ui.horizontal_wrapped(|ui| {
                        if q.contains('*') || q.contains('/') {
                            if ui.small_button(format!("use “{q}”")).clicked() {
                                wz.key = Some(q.clone());
                            }
                        }
                        if let Some(c) = &catalog {
                            let mut n = 0;
                            for p in &c.pkgs {
                                if p.pn.to_lowercase().starts_with(&q) || p.key().starts_with(&q) {
                                    if ui.small_button(p.key()).clicked() {
                                        wz.key = Some(p.key());
                                        wz.query = p.key();
                                    }
                                    n += 1;
                                    if n > 14 {
                                        break;
                                    }
                                }
                            }
                        }
                    });
                }
            });
            ui.end_row();

            ui.label(w::strong("2. Versions"));
            ui.vertical(|ui| {
                let mut op = wz.op.unwrap_or(AOp::Any);
                w::segmented(
                    ui,
                    &mut op,
                    &[(AOp::Any, "Any"), (AOp::Eq, "="), (AOp::Ge, ">="), (AOp::Gt, ">"), (AOp::Le, "<="), (AOp::Lt, "<"), (AOp::Tilde, "~"), (AOp::EqGlob, "=…*")],
                );
                wz.op = Some(op);
                ui.label(w::dim(op.label()));
                if op != AOp::Any {
                    ui.horizontal(|ui| {
                        let versions: Vec<String> = wz
                            .key
                            .as_ref()
                            .and_then(|k| catalog.as_ref().and_then(|c| c.get(k)))
                            .map(|p| p.ebuilds.iter().rev().map(|e| e.ver.raw.clone()).collect())
                            .unwrap_or_default();
                        egui::ComboBox::from_id_salt("wz-ver").selected_text(if wz.version.is_empty() { "pick version".to_string() } else { wz.version.clone() }).show_ui(ui, |ui| {
                            for v in versions {
                                ui.selectable_value(&mut wz.version, v.clone(), v);
                            }
                        });
                        ui.add(egui::TextEdit::singleline(&mut wz.version).hint_text("or type").desired_width(120.0));
                    });
                }
                ui.horizontal(|ui| {
                    ui.label(w::dim("Slot"));
                    ui.add(egui::TextEdit::singleline(&mut wz.slot).hint_text("any").desired_width(70.0));
                    ui.label(w::dim("Repository"));
                    ui.add(egui::TextEdit::singleline(&mut wz.repo).hint_text("any").desired_width(110.0));
                });
            });
            ui.end_row();

            if kind.has_values() {
                ui.label(w::strong(format!("3. {}", kind.values_label())));
                ui.vertical(|ui| {
                    ui.add(egui::TextEdit::singleline(&mut wz.values).font(egui::TextStyle::Monospace).desired_width(420.0).hint_text(match kind {
                        ConfKind::Use => "flag -otherflag",
                        ConfKind::AcceptKeywords => "empty = ~arch",
                        ConfKind::Env => "env file names",
                        _ => "",
                    }));
                    ui.horizontal_wrapped(|ui| {
                        let mut sugg: Vec<String> = kind.suggestions().iter().map(|s| s.to_string()).collect();
                        if kind == ConfKind::Env {
                            sugg = cfg.env_files.iter().map(|e| e.name.clone()).collect();
                        }
                        if matches!(kind, ConfKind::Use | ConfKind::ProfileUseForce | ConfKind::ProfileUseMask)
                            && let Some(k) = &wz.key
                        {
                            if let Some(d) = app.details.get(k)
                                && let Some(v) = d.versions.first()
                            {
                                sugg = v.meta.iuse().into_iter().map(|(f, _)| f).collect();
                            } else {
                                app.actions.push(Action::LoadDetail(k.clone()));
                            }
                        }
                        if kind == ConfKind::License
                            && let Some(k) = &wz.key
                            && let Some(d) = app.details.get(k)
                            && let Some(v) = d.versions.first()
                        {
                            let mut l = Vec::new();
                            crate::portage::dep::collect_leaves(&crate::portage::dep::parse(v.meta.get("LICENSE")), None, &mut l);
                            sugg.extend(l.into_iter().filter(|x| x != "||"));
                        }
                        for s in sugg {
                            let toks: Vec<String> = wz.values.split_whitespace().map(String::from).collect();
                            let on = toks.contains(&s);
                            let off = toks.contains(&format!("-{s}"));
                            let label = if on { s.clone() } else if off { format!("-{s}") } else { s.clone() };
                            let r = w::chip(ui, &label, on || off);
                            if r.clicked() {
                                let mut t: Vec<String> = toks.into_iter().filter(|x| x.trim_start_matches('-') != s).collect();
                                let use_like = matches!(kind, ConfKind::Use | ConfKind::ProfileUseForce | ConfKind::ProfileUseMask);
                                if !on && !off {
                                    t.push(s.clone());
                                } else if on && use_like {
                                    t.push(format!("-{s}"));
                                }
                                wz.values = t.join(" ");
                            }
                        }
                    });
                    if matches!(kind, ConfKind::Use) {
                        ui.label(w::dim("Click a flag once to enable, again to disable, a third time to remove it."));
                    }
                });
                ui.end_row();
            }

            ui.label(w::strong("File"));
            ui.horizontal(|ui| {
                if set.is_dir || !set.exists {
                    if wz.file.is_empty() {
                        wz.file = app.settings.target_file.clone();
                    }
                    egui::ComboBox::from_id_salt("wz-file").selected_text(wz.file.clone()).show_ui(ui, |ui| {
                        ui.selectable_value(&mut wz.file, app.settings.target_file.clone(), format!("{} (PortageHub)", app.settings.target_file));
                        for f in &set.files {
                            ui.selectable_value(&mut wz.file, f.rel.clone(), f.rel.clone());
                        }
                    });
                    ui.add(egui::TextEdit::singleline(&mut wz.file).desired_width(160.0).hint_text("new file name"));
                } else {
                    ui.label(w::mono(set.base.to_string_lossy()));
                }
            });
            ui.end_row();
        });

        // Build the line
        let wz = &app.config_view.wizard;
        let Some(key) = wz.key.clone() else {
            ui.label(w::dim("Pick a package first."));
            return;
        };
        let op = wz.op.unwrap_or(AOp::Any);
        let mut atom = if op == AOp::Any || wz.version.trim().is_empty() {
            key.clone()
        } else {
            format!("{}{key}-{}{}", op.as_str(), wz.version.trim(), if op == AOp::EqGlob { "*" } else { "" })
        };
        if !wz.slot.trim().is_empty() {
            atom.push_str(&format!(":{}", wz.slot.trim()));
        }
        if !wz.repo.trim().is_empty() {
            atom.push_str(&format!("::{}", wz.repo.trim()));
        }
        let entry = Entry::new(&atom, wz.values.split_whitespace().map(String::from).collect());
        ui.separator();
        ui.horizontal(|ui| {
            ui.label(w::mono(entry.render()));
            match &entry.atom {
                Some(_) => ui.label(w::dim(format!("→ {}", entry.describe(kind)))),
                None => ui.label(RichText::new(format!("{} invalid atom", icon::WARNING)).color(RED)),
            };
        });
        let file_name = if wz.file.trim().is_empty() { app.settings.target_file.clone() } else { wz.file.trim().to_string() };
        if ui.add_enabled(entry.atom.is_some(), egui::Button::new(RichText::new(format!("{}  Add entry", icon::PLUS)).color(WHITE)).fill(Color32::from_rgb(0x1f, 0x6f, 0xb2))).clicked() {
            let ch = edits::add_lines(&cfg, &[(kind, entry.render())], &file_name, None);
            app.push_write(format!("{}: {}", kind.file_name(), entry.render()), ch, vec![Reload::Config], None);
            app.config_view.wizard.values.clear();
        }
    });
}

fn make_conf(app: &mut App, ui: &mut Ui) {
    let Some(mc) = app.data.makeconf.clone() else {
        w::loading(ui, "Reading make.conf…");
        return;
    };
    let env = app.data.env.clone();
    let file = mc.main_file();
    let text = mc.text_of(&file).to_string();
    let threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4) as i64;
    common::page(ui, "makeconf", |ui| {
        w::title(ui, "make.conf");
        ui.label(w::mono(file.to_string_lossy()));
        common::tip(app, ui, "The global build settings. The cards below edit the most common variables; every other variable is listed further down. Effective values include what your profile adds.");
        let blocked = mc.edit_blocked(&file);
        if let Some(why) = &blocked {
            w::callout(ui, ORANGE, icon::LOCK, why);
        }
        for (f, problem) in &mc.problems {
            w::callout(ui, ORANGE, icon::WARNING, &format!("{}: {problem}. Portage cannot read the file as it is: fix it in the text editor at the bottom of this page first.", f.display()));
        }
        if !mc.sourced.is_empty() {
            let names = mc.sourced.iter().map(|p| p.display().to_string()).collect::<Vec<_>>().join(", ");
            w::callout(ui, ACCENT, icon::INFO, &format!("make.conf also reads {names} (source): variables may come from there too and override what is shown here."));
        }
        let mut set = |app: &mut App, name: &str, value: String| {
            match makeconf::set_var(&text, name, &value) {
                Ok(new) => app.push(Action::Write { title: format!("Set {name} in make.conf"), changes: vec![FileChange { path: file.clone(), old: text.clone(), new: Some(new) }], reload: vec![Reload::MakeConf, Reload::Env], then: None }),
                Err(why) => app.toast(crate::app::ToastKind::Error, why),
            }
        };
        let editable = |name: &str| blocked.is_none() && mc.last(name).is_none_or(|a| !a.self_ref && a.file == file);

        // MAKEOPTS
        w::section(ui, "Parallel building (MAKEOPTS)");
        w::card().show(ui, |ui| {
            ui.set_width(ui.available_width());
            let cur = mc.value("MAKEOPTS");
            let parse = |flag: char| -> Option<i64> {
                let toks: Vec<&str> = cur.split_whitespace().collect();
                for (i, t) in toks.iter().enumerate() {
                    if let Some(r) = t.strip_prefix(&format!("-{flag}")) {
                        if r.is_empty() {
                            return toks.get(i + 1).and_then(|n| n.parse().ok());
                        }
                        return r.trim_start_matches('=').parse().ok();
                    }
                    if let Some(r) = t.strip_prefix(if flag == 'j' { "--jobs=" } else { "--load-average=" }) {
                        return r.parse().ok();
                    }
                }
                None
            };
            let id = ui.id().with("mk-jobs");
            let (mut j, mut l) = ui.data_mut(|d| *d.get_temp_mut_or_insert_with(id, || (parse('j').unwrap_or(threads), parse('l').unwrap_or(0))));
            ui.horizontal(|ui| {
                ui.label(w::mono(format!("MAKEOPTS=\"{cur}\"")));
                w::help(ui, makeconf::describe("MAKEOPTS"));
            });
            ui.horizontal(|ui| {
                ui.label("Compile jobs (-j)");
                ui.add(egui::Slider::new(&mut j, 1..=threads * 2));
                ui.add_space(20.0);
                ui.label("Load limit (-l, 0 = none)");
                ui.add(egui::Slider::new(&mut l, 0..=threads * 2));
            });
            ui.data_mut(|d| d.insert_temp(id, (j, l)));
            ui.label(w::dim(format!("This machine has {threads} threads. Each compile job can use ~2 GB RAM for big C++ projects.")));
            let new = if l > 0 { format!("-j{j} -l{l}") } else { format!("-j{j}") };
            if new != cur && ui.add_enabled(editable("MAKEOPTS"), egui::Button::new(format!("{} Save MAKEOPTS=\"{new}\"", icon::FLOPPY_DISK))).clicked() {
                set(app, "MAKEOPTS", new);
            }
        });

        // ACCEPT_KEYWORDS
        w::section(ui, "Branch (ACCEPT_KEYWORDS)");
        w::card().show(ui, |ui| {
            ui.set_width(ui.available_width());
            let arch = env.as_ref().map(|e| e.arch().to_string()).unwrap_or_else(|| "amd64".into());
            let cur = mc.value("ACCEPT_KEYWORDS");
            let testing = cur.split_whitespace().any(|k| k == format!("~{arch}"));
            let mut sel = testing;
            ui.horizontal(|ui| {
                w::segmented(ui, &mut sel, &[(false, "Stable"), (true, "Testing (~)")]);
                ui.label(w::dim(if cur.is_empty() { format!("not set — profile default ({arch})") } else { format!("ACCEPT_KEYWORDS=\"{cur}\"") }));
                w::help(ui, makeconf::describe("ACCEPT_KEYWORDS"));
            });
            if sel != testing {
                w::callout(ui, ORANGE, icon::WARNING, "Switching the whole system to testing upgrades many packages and is hard to undo. Most users accept testing keywords per package instead.");
                if ui.button(format!("Confirm: set ACCEPT_KEYWORDS=\"{}\"", if sel { format!("~{arch}") } else { arch.clone() })).clicked() {
                    set(app, "ACCEPT_KEYWORDS", if sel { format!("~{arch}") } else { arch.clone() });
                }
            }
        });

        // ACCEPT_LICENSE
        w::section(ui, "Licenses (ACCEPT_LICENSE)");
        w::card().show(ui, |ui| {
            ui.set_width(ui.available_width());
            let cur = mc.value("ACCEPT_LICENSE");
            ui.horizontal_wrapped(|ui| {
                for (v, label) in [("-* @FREE", "Free software only"), ("-* @FREE @BINARY-REDISTRIBUTABLE", "Free + redistributable binaries"), ("*", "Accept everything")] {
                    if w::chip(ui, label, cur == v).on_hover_text(v).clicked() && cur != v && editable("ACCEPT_LICENSE") {
                        set(app, "ACCEPT_LICENSE", v.to_string());
                    }
                }
                ui.label(w::dim(if cur.is_empty() { "profile default (-* @FREE)".to_string() } else { format!("current: {cur}") }));
            });
        });

        // FEATURES
        w::section(ui, "Portage features (FEATURES)");
        w::card().show(ui, |ui| {
            ui.set_width(ui.available_width());
            let cur = mc.value("FEATURES");
            let toks: Vec<String> = cur.split_whitespace().map(String::from).collect();
            let effective = env.as_ref().map(|e| e.get("FEATURES").to_string()).unwrap_or_default();
            ui.horizontal_wrapped(|ui| {
                for (f, help) in COMMON_FEATURES {
                    let in_mc = toks.iter().any(|t| t == f);
                    let neg = toks.iter().any(|t| *t == format!("-{f}"));
                    let eff = effective.split_whitespace().any(|t| t == *f);
                    let label = if neg { format!("-{f}") } else { f.to_string() };
                    let r = w::chip(ui, &label, in_mc || (eff && !neg)).on_hover_text(format!("{help}\n{}", if in_mc { "set in make.conf" } else if eff { "enabled by default/profile" } else { "off" }));
                    if r.clicked() && editable("FEATURES") {
                        let mut t: Vec<String> = toks.iter().filter(|x| x.trim_start_matches('-') != *f).cloned().collect();
                        if !(in_mc || eff && !neg) {
                            t.push(f.to_string());
                        } else if eff && !in_mc {
                            t.push(format!("-{f}"));
                        }
                        set(app, "FEATURES", t.join(" "));
                    }
                }
            });
            if !editable("FEATURES") {
                ui.label(w::dim("FEATURES references itself or is set in another file — edit it as text below."));
            }
        });

        // USE
        w::section(ui, "Global USE flags");
        w::card().show(ui, |ui| {
            ui.set_width(ui.available_width());
            let cur = mc.value("USE");
            let toks: Vec<String> = cur.split_whitespace().map(String::from).collect();
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing = egui::vec2(4.0, 4.0);
                for t in &toks {
                    let (n, on) = match t.strip_prefix('-') {
                        Some(x) => (x, false),
                        None => (t.as_str(), true),
                    };
                    let r = w::flag_chip(ui, n, on, false);
                    let desc = app.data.usedesc.as_ref().map(|u| u.describe("", n, None).0).unwrap_or_default();
                    r.on_hover_text(if desc.is_empty() { n.to_string() } else { desc });
                }
            });
            let id = ui.id().with("use-edit");
            let mut buf = ui.data_mut(|d| d.get_temp::<String>(id)).unwrap_or_else(|| cur.clone());
            ui.add(egui::TextEdit::multiline(&mut buf).font(egui::TextStyle::Monospace).desired_rows(3).desired_width(f32::INFINITY));
            let changed = buf.split_whitespace().collect::<Vec<_>>() != toks.iter().map(|s| s.as_str()).collect::<Vec<_>>();
            ui.data_mut(|d| d.insert_temp(id, buf.clone()));
            if changed && ui.add_enabled(editable("USE"), egui::Button::new(format!("{} Save USE", icon::FLOPPY_DISK))).clicked() {
                set(app, "USE", buf.split_whitespace().collect::<Vec<_>>().join(" "));
                ui.data_mut(|d| d.remove::<String>(id));
            }
            if !editable("USE") {
                ui.label(w::dim("USE references itself (USE=\"${USE} …\") — edit it as text below."));
            }
        });

        // VIDEO_CARDS
        w::section(ui, "Hardware");
        w::card().show(ui, |ui| {
            ui.set_width(ui.available_width());
            for (var, opts) in [
                ("VIDEO_CARDS", &["nvidia", "amdgpu", "radeonsi", "radeon", "intel", "nouveau", "virgl", "vmware", "qxl", "fbdev", "vesa", "lavapipe", "d3d12", "freedreno"][..]),
                ("INPUT_DEVICES", &["libinput", "evdev", "synaptics", "wacom", "joystick"][..]),
            ] {
                let cur = mc.value(var);
                let toks: Vec<String> = cur.split_whitespace().map(String::from).collect();
                ui.horizontal_wrapped(|ui| {
                    ui.label(RichText::new(var).monospace().color(WHITE));
                    w::help(ui, makeconf::describe(var));
                    for o in opts {
                        let on = toks.iter().any(|t| t == o);
                        if w::chip(ui, o, on).clicked() && editable(var) {
                            let mut t = toks.clone();
                            if on {
                                t.retain(|x| x != o);
                            } else {
                                t.push(o.to_string());
                            }
                            set(app, var, t.join(" "));
                        }
                    }
                });
            }
        });

        // All variables
        w::section(ui, "All variables");
        egui::Grid::new("mk-all").num_columns(3).striped(true).spacing([14.0, 6.0]).show(ui, |ui| {
            ui.label(w::dim("Variable"));
            ui.label(w::dim("Value in make.conf (expanded)"));
            ui.label(w::dim("Effective"));
            ui.end_row();
            for name in mc.names() {
                let a = mc.last(&name).unwrap();
                ui.horizontal(|ui| {
                    ui.label(RichText::new(&name).monospace().color(ACCENT));
                    let d = makeconf::describe(&name);
                    if !d.is_empty() {
                        w::help(ui, d);
                    }
                    if mc.count(&name) > 1 {
                        ui.label(RichText::new(format!("×{}", mc.count(&name))).size(11.0).color(ORANGE)).on_hover_text("Assigned more than once; the last one wins");
                    }
                });
                let editing = app.config_view.mk_edit.as_ref().is_some_and(|(n, _)| *n == name);
                if editing {
                    let mut save = None;
                    ui.horizontal(|ui| {
                        let buf = &mut app.config_view.mk_edit.as_mut().unwrap().1;
                        ui.add(egui::TextEdit::singleline(buf).font(egui::TextStyle::Monospace).desired_width(420.0));
                        if ui.button(icon::CHECK).clicked() {
                            save = Some(buf.clone());
                        }
                        if ui.button(icon::X).clicked() {
                            save = Some(String::from("\u{0}"));
                        }
                    });
                    if let Some(v) = save {
                        app.config_view.mk_edit = None;
                        if v != "\u{0}" {
                            set(app, &name, v);
                        }
                    }
                } else {
                    ui.horizontal(|ui| {
                        ui.add(egui::Label::new(RichText::new(&a.expanded).monospace().size(12.0)).truncate()).on_hover_text(format!("as written: {}", a.value));
                        if !a.self_ref && a.file == file && w::icon_button(ui, icon::PENCIL_SIMPLE, "Edit").clicked() {
                            app.config_view.mk_edit = Some((name.clone(), a.value.clone()));
                        }
                    });
                }
                let eff = env.as_ref().and_then(|e| e.vars.get(&name)).cloned().unwrap_or_default();
                ui.add(egui::Label::new(RichText::new(eff).monospace().size(12.0).color(TEXT_DIM)).truncate());
                ui.end_row();
            }
        });
        ui.add_space(8.0);
        ui.horizontal(|ui| {
            if app.config_view.mk_raw.is_none() && blocked.is_none() && w::button(ui, icon::CODE, "Edit make.conf as text").clicked() {
                app.config_view.mk_raw = Some(text.clone());
            }
            if w::button(ui, icon::TERMINAL, "Show emerge --info").clicked() {
                app.push(Action::Capture { tag: "emerge-info".into(), argv: vec!["emerge".into(), "--info".into()], privilege: crate::exec::Priv::User });
                app.emerge.page = super::emerge::Page::Activity;
            }
        });
        if let Some(buf) = app.config_view.mk_raw.as_mut() {
            ui.add(egui::TextEdit::multiline(buf).code_editor().desired_rows(24).desired_width(f32::INFINITY));
            let mut close = false;
            ui.horizontal(|ui| {
                if w::primary(ui, icon::FLOPPY_DISK, "Save make.conf").clicked() {
                    let new = app.config_view.mk_raw.clone().unwrap();
                    app.push(Action::Write { title: "Edit make.conf".into(), changes: vec![FileChange { path: file.clone(), old: text.clone(), new: Some(new) }], reload: vec![Reload::MakeConf, Reload::Env], then: None });
                    close = true;
                }
                if w::button(ui, "", "Close").clicked() {
                    close = true;
                }
            });
            if close {
                app.config_view.mk_raw = None;
            }
        }
    });
}

const ENV_TEMPLATES: &[(&str, &str, &str)] = &[
    ("notmpfs", "Build on disk instead of tmpfs (for huge packages)", "PORTAGE_TMPDIR=\"/var/tmp/notmpfs\"\n"),
    ("nolto", "Disable link-time optimisation", "CFLAGS=\"${CFLAGS} -fno-lto\"\nCXXFLAGS=\"${CXXFLAGS} -fno-lto\"\nLDFLAGS=\"${LDFLAGS} -fno-lto\"\n"),
    ("clang", "Build with clang/LLVM", "CC=\"clang\"\nCXX=\"clang++\"\nAR=\"llvm-ar\"\nNM=\"llvm-nm\"\nRANLIB=\"llvm-ranlib\"\n"),
    ("ccache", "Use ccache for this package", "FEATURES=\"ccache\"\n"),
    ("debug", "Build with debug symbols", "CFLAGS=\"${CFLAGS} -ggdb3\"\nCXXFLAGS=\"${CXXFLAGS} -ggdb3\"\nFEATURES=\"splitdebug compressdebug -nostrip\"\n"),
    ("lowjobs", "Fewer parallel jobs (RAM-hungry builds)", "MAKEOPTS=\"-j4\"\n"),
    ("notest", "Never run tests", "FEATURES=\"-test\"\n"),
];

fn env_files(app: &mut App, ui: &mut Ui) {
    let Some(cfg) = app.data.config.clone() else { return };
    let users: std::collections::HashMap<String, Vec<String>> = {
        let mut m: std::collections::HashMap<String, Vec<String>> = Default::default();
        if let Some(s) = cfg.set(ConfKind::Env) {
            for f in &s.files {
                for (_, e) in f.entries() {
                    for v in &e.values {
                        m.entry(v.clone()).or_default().push(e.atom_text.clone());
                    }
                }
            }
        }
        m
    };
    common::page(ui, "envfiles", |ui| {
        w::title(ui, "Environment files");
        w::subtitle(ui, "/etc/portage/env/* — named sets of variables that package.env applies to selected packages.");
        ui.add_space(6.0);
        for e in &cfg.env_files {
            w::card().show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    ui.label(RichText::new(icon::FILE_CODE).size(20.0).color(ACCENT));
                    ui.label(RichText::new(&e.name).size(16.0).family(theme::bold()).color(WHITE));
                    let used = users.get(&e.name).map(|v| v.len()).unwrap_or(0);
                    ui.label(w::dim(format!("used by {used} package.env entr{}", if used == 1 { "y" } else { "ies" }))).on_hover_text(users.get(&e.name).map(|v| v.join("\n")).unwrap_or_default());
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if cfg.edit_blocked_for(&e.path).is_some() {
                            return;
                        }
                        if w::icon_button(ui, icon::TRASH, "Delete").clicked() {
                            app.push(Action::Write { title: format!("Delete env/{}", e.name), changes: vec![FileChange { path: e.path.clone(), old: e.content.clone(), new: None }], reload: vec![Reload::Config], then: None });
                        }
                        if w::icon_button(ui, icon::PENCIL_SIMPLE, "Edit").clicked() {
                            app.config_view.env_edit = Some((e.path.clone(), e.content.clone()));
                        }
                    });
                });
                if let Some(why) = cfg.edit_blocked_for(&e.path) {
                    w::callout(ui, ORANGE, icon::LOCK, &why);
                }
                let editing = app.config_view.env_edit.as_ref().is_some_and(|(p, _)| *p == e.path);
                if editing {
                    let buf = &mut app.config_view.env_edit.as_mut().unwrap().1;
                    ui.add(egui::TextEdit::multiline(buf).code_editor().desired_rows(6).desired_width(f32::INFINITY));
                    ui.horizontal(|ui| {
                        if w::primary(ui, icon::FLOPPY_DISK, "Save").clicked() {
                            let (p, t) = app.config_view.env_edit.take().unwrap();
                            app.push(Action::Write { title: format!("Edit env/{}", e.name), changes: vec![FileChange { path: p, old: e.content.clone(), new: Some(t) }], reload: vec![Reload::Config], then: None });
                        }
                        if w::button(ui, "", "Cancel").clicked() {
                            app.config_view.env_edit = None;
                        }
                    });
                } else {
                    ui.add(egui::Label::new(RichText::new(e.content.trim()).monospace().size(12.0).color(TEXT_DIM)));
                }
            });
            ui.add_space(4.0);
        }
        w::section(ui, "New environment file");
        ui.horizontal(|ui| {
            ui.add(egui::TextEdit::singleline(&mut app.config_view.env_new).hint_text("name").desired_width(180.0));
            let name = app.config_view.env_new.trim().to_string();
            if ui.add_enabled(!name.is_empty() && !name.contains('/'), egui::Button::new(format!("{} Create empty", icon::PLUS))).clicked() {
                app.push(Action::Write { title: format!("Create env/{name}"), changes: vec![FileChange { path: PathBuf::from(format!("/etc/portage/env/{name}")), old: String::new(), new: Some("# variables for package.env\n".into()) }], reload: vec![Reload::Config], then: None });
            }
        });
        ui.label(w::dim("Or start from a template:"));
        ui.horizontal_wrapped(|ui| {
            for (name, desc, content) in ENV_TEMPLATES {
                let exists = cfg.env_files.iter().any(|e| e.name == *name);
                if ui.add_enabled(!exists, egui::Button::new(format!("{name}"))).on_hover_text(format!("{desc}\n\n{content}")).clicked() {
                    app.push(Action::Write { title: format!("Create env/{name}"), changes: vec![FileChange { path: PathBuf::from(format!("/etc/portage/env/{name}")), old: String::new(), new: Some(format!("# {desc}\n{content}")) }], reload: vec![Reload::Config], then: None });
                }
            }
        });
        ui.label(w::dim("Then assign it in Package environment (package.env), e.g. “www-client/chromium notmpfs”."));
    });
}

fn profile(app: &mut App, ui: &mut Ui) {
    common::page(ui, "profile", |ui| {
        w::title(ui, "System profile");
        w::subtitle(ui, "The profile sets default USE flags, masks and the base system. Change it only when the Gentoo news or handbook tells you to.");
        ui.add_space(6.0);
        let Some(profiles) = app.data.profiles.clone() else {
            w::loading(ui, "eselect profile list…");
            if !app.is_loading(Reload::Profiles) {
                app.push(Action::Reload(Reload::Profiles));
            }
            return;
        };
        if let Ok(link) = std::fs::read_link("/etc/portage/make.profile") {
            ui.label(w::mono(format!("make.profile → {}", link.display())));
        }
        let current = profiles.iter().find(|p| p.current).map(|p| p.num);
        let mut pick = app.config_view.profile_pick.or(current);
        let show_all = app.settings.show_advanced;
        egui::Grid::new("profiles").num_columns(3).striped(true).spacing([14.0, 5.0]).show(ui, |ui| {
            for p in profiles.iter() {
                if !show_all && p.status != "stable" && !p.current {
                    continue;
                }
                ui.radio_value(&mut pick, Some(p.num), RichText::new(&p.path).monospace().color(if p.current { GREEN_TEXT } else { TEXT }));
                ui.label(w::dim(&p.status));
                if p.current {
                    w::status_badge(ui, "INSTALLED");
                } else {
                    ui.label("");
                }
                ui.end_row();
            }
        });
        if !show_all {
            ui.label(w::dim("Only stable profiles are shown (enable advanced options in Settings to see dev/exp ones)."));
        }
        app.config_view.profile_pick = pick;
        if pick != current
            && let Some(n) = pick
        {
            w::callout(ui, ORANGE, icon::WARNING, "After switching profiles you normally need a full update: emerge -uDN @world. Read the news first.");
            if w::danger(ui, icon::USER_CIRCLE_GEAR, &format!("Switch to profile [{n}]")).clicked() {
                app.push(Action::Root {
                    title: format!("eselect profile set {n}"),
                    argv: vec!["eselect".into(), "profile".into(), "set".into(), n.to_string()],
                    reload: vec![Reload::Profiles, Reload::Env],
                    kind: JobKind::Generic,
                });
            }
        }
    });
}

fn news(app: &mut App, ui: &mut Ui) {
    let Some(news) = app.data.news.clone() else {
        w::loading(ui, "Reading news…");
        return;
    };
    egui::Panel::left("news-list").resizable(true).default_size(360.0).frame(egui::Frame::new().fill(Color32::from_rgb(0x19, 0x22, 0x2e)).inner_margin(Margin::same(8))).show(ui, |ui| {
        ui.horizontal(|ui| {
            ui.label(RichText::new("NEWS").size(12.0).family(theme::bold()).color(TEXT_DIM));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.small_button("Mark all read").on_hover_text("eselect news read --quiet all").clicked() {
                    app.push(Action::Root {
                        title: "Mark news as read".into(),
                        argv: vec!["eselect".into(), "news".into(), "read".into(), "--quiet".into(), "all".into()],
                        reload: vec![Reload::News],
                        kind: JobKind::Generic,
                    });
                }
            });
        });
        egui::ScrollArea::vertical().id_salt("news-scroll").show(ui, |ui| {
            for n in news.iter() {
                let sel = app.config_view.news_open.as_deref() == Some(&n.name);
                let r = ui.add_sized(
                    [ui.available_width(), 46.0],
                    egui::Button::new(
                        RichText::new(format!("{} {}\n{}", if n.unread { icon::CIRCLE } else { "" }, n.title, n.posted)).color(if n.unread { WHITE } else { TEXT_DIM }),
                    )
                    .selected(sel)
                    .wrap(),
                );
                if r.clicked() {
                    app.config_view.news_open = Some(n.name.clone());
                }
            }
        });
    });
    egui::CentralPanel::default().frame(egui::Frame::new().fill(BG)).show(ui, |ui| {
        let Some(item) = app.config_view.news_open.as_ref().and_then(|n| news.iter().find(|x| x.name == *n)).or_else(|| news.first()) else {
            w::empty_state(ui, icon::NEWSPAPER, "No news", "");
            return;
        };
        common::page(ui, "news-body", |ui| {
            w::title(ui, &item.title);
            ui.label(w::dim(format!("{} · {}", item.posted, item.name)));
            ui.add_space(8.0);
            let text = std::fs::read_to_string(&item.path).unwrap_or_default();
            let body = text.split("\n\n").skip(1).collect::<Vec<_>>().join("\n\n");
            ui.add(egui::Label::new(RichText::new(body).size(14.5).color(TEXT)).wrap());
            ui.add_space(10.0);
            if item.unread && w::primary(ui, icon::CHECK, "Mark as read").clicked() {
                app.push(Action::Root {
                    title: format!("Read news {}", item.name),
                    argv: vec!["eselect".into(), "news".into(), "read".into(), "--quiet".into(), item.name.clone()],
                    reload: vec![Reload::News],
                    kind: JobKind::Generic,
                });
            }
        });
    });
}

/// Accept a pending update; the helper checks that the file it replaces is still the one that was shown.
fn accept_op(u: &PendingUpdate) -> Op {
    let expect = match &u.old {
        Some(old) => Some(portagehub::protocol::Expect::of(old)),
        None if !u.target.exists() => Some(portagehub::protocol::Expect::Absent),
        None => None, // root-only or too large to have been read
    };
    Op::ConfigUpdate { path: u.update.to_string_lossy().to_string(), accept: true, expect }
}

fn updates(app: &mut App, ui: &mut Ui) {
    let protect = app.data.env.as_ref().map(|e| e.get("CONFIG_PROTECT").to_string()).unwrap_or_default();
    if app.config_view.updates.is_none() {
        let slot: Arc<Mutex<Option<Vec<PendingUpdate>>>> = Arc::default();
        let s2 = slot.clone();
        let ctx = ui.ctx().clone();
        std::thread::spawn(move || {
            let r = system::find_config_updates(&protect);
            *s2.lock().unwrap() = Some(r);
            ctx.request_repaint();
        });
        app.config_view.updates = Some(slot);
    }
    let list = app.config_view.updates.as_ref().unwrap().lock().unwrap().clone();
    let applying = !app.batches.is_empty();
    common::page(ui, "cfg-updates", |ui| {
        ui.horizontal(|ui| {
            w::title(ui, "Configuration file updates");
            if w::icon_button(ui, icon::ARROWS_CLOCKWISE, "Rescan").clicked() {
                app.config_view.updates = None;
            }
        });
        w::subtitle(ui, "When a package ships a new version of a config file you edited, Portage saves it as ._cfgXXXX_name instead of overwriting yours (CONFIG_PROTECT).");
        ui.add_space(6.0);
        if applying {
            w::loading(ui, "Applying…");
            return;
        }
        let Some(list) = list else {
            w::loading(ui, "Scanning CONFIG_PROTECT directories…");
            return;
        };
        if list.is_empty() {
            w::empty_state(ui, icon::CHECK_CIRCLE, "All configuration files are up to date", "Nothing is waiting to be merged.");
            return;
        }
        let (fine, risky) = system::split_risky(&list);
        ui.horizontal(|ui| {
            if !fine.is_empty() && w::primary(ui, icon::CHECKS, &format!("Review and accept {} new version{}…", fine.len(), if fine.len() == 1 { "" } else { "s" })).clicked() {
                // Replacing many files at once is never one blind click: show every diff first.
                let (mut changes, mut unreadable) = (Vec::new(), Vec::new());
                for u in &fine {
                    match (&u.old, &u.new) {
                        (Some(old), Some(new)) => changes.push(FileChange { path: u.target.clone(), old: old.clone(), new: Some(new.clone()) }),
                        (None, Some(new)) if !u.target.exists() => changes.push(FileChange { path: u.target.clone(), old: String::new(), new: Some(new.clone()) }),
                        _ => unreadable.push(u.target.clone()),
                    }
                }
                let mut notes = vec!["Each file below is replaced by its new version. Everything is one recorded change: you can undo it afterwards.".to_string()];
                if !risky.is_empty() {
                    notes.push(format!(
                        "Not included, because they can lock you out or break the boot: {}. Review those one by one.",
                        risky.iter().map(|u| u.target.display().to_string()).collect::<Vec<_>>().join(", ")
                    ));
                }
                let ops = fine.iter().map(|u| accept_op(u)).collect();
                app.push(Action::ReviewOps { title: format!("Accept {} new configuration file versions", fine.len()), changes, unreadable, notes, ops, reload: vec![] });
            }
            if w::button(ui, icon::X, "Keep all current files").on_hover_text("Delete every pending new version (recorded, so it can be undone)").clicked() {
                let ops = list.iter().map(|u| Op::ConfigUpdate { path: u.update.to_string_lossy().to_string(), accept: false, expect: None }).collect();
                app.push(Action::Batch { title: "Discard all config updates".into(), ops, reload: vec![], then: None });
            }
        });
        if !risky.is_empty() {
            ui.add_space(4.0);
            w::callout(
                ui,
                ORANGE,
                icon::SHIELD_WARNING,
                &format!("{} file(s) here (SSH, PAM, sudo, fstab, GRUB defaults, polkit, accounts, /etc/portage) can lock you out or break the boot. They are left out of the bulk action: read the differences and decide for each.", risky.len()),
            );
        }
        for u in &list {
            let (upd, target) = (&u.update, &u.target);
            w::card().show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    let risky = system::is_risky(target);
                    ui.label(RichText::new(if risky { icon::SHIELD_WARNING } else { icon::FILE_ARROW_UP }).size(18.0).color(ORANGE));
                    ui.label(w::strong(target.to_string_lossy()));
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if w::button(ui, icon::X, "Keep mine").on_hover_text("Delete the new version").clicked() {
                            app.push(Action::Batch { title: format!("Keep {}", target.display()), ops: vec![Op::ConfigUpdate { path: upd.to_string_lossy().to_string(), accept: false, expect: None }], reload: vec![], then: None });
                        }
                        if w::primary(ui, icon::CHECK, "Use new version").clicked() {
                            app.push(Action::Batch { title: format!("Update {}", target.display()), ops: vec![accept_op(u)], reload: vec![], then: None });
                        }
                    });
                });
                match (&u.old, &u.new) {
                    (Some(o), Some(n)) => {
                        egui::CollapsingHeader::new("Show differences").id_salt(upd).default_open(system::is_risky(target)).show(ui, |ui| {
                            for (c, l) in crate::fmt::line_diff(o, n) {
                                let col = match c {
                                    '+' => GREEN,
                                    '-' => RED,
                                    _ => TEXT_DIM,
                                };
                                ui.label(RichText::new(format!("{c} {l}")).monospace().size(12.0).color(col));
                            }
                        });
                    }
                    _ => {
                        ui.label(w::dim("Cannot show the differences: one of the files is root-only, missing or too large."));
                    }
                }
            });
            ui.add_space(3.0);
        }
    });
}

fn strays(app: &mut App, ui: &mut Ui) {
    let Some(cfg) = app.data.config.clone() else { return };
    common::page(ui, "strays", |ui| {
        w::title(ui, "Stray files in /etc/portage");
        w::subtitle(ui, "Portage only reads files with exact names like package.use. Anything else is silently ignored — often a typo or a forgotten backup.");
        ui.add_space(8.0);
        if cfg.strays.is_empty() {
            w::empty_state(ui, icon::CHECK_CIRCLE, "Nothing stray", "Every package.* file has a name Portage understands.");
            return;
        }
        for s in &cfg.strays {
            w::card_tinted(ORANGE).show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    ui.label(RichText::new(icon::WARNING).size(20.0).color(ORANGE));
                    ui.label(w::strong(s.path.to_string_lossy()));
                });
                let (content, unreadable) = match config::read_utf8(&s.path) {
                    Ok(t) => (t, None),
                    Err(why) => (String::new(), Some(why)),
                };
                match &s.reason {
                    StrayReason::Misnamed { kind, suggested_name } => {
                        ui.label(format!("Looks like it was meant to be inside {}/ — the separator is missing, so Portage ignores these entries.", kind.file_name()));
                        let name = app.config_view.stray_names.entry(s.path.clone()).or_insert_with(|| suggested_name.clone());
                        ui.horizontal(|ui| {
                            ui.label(w::dim(format!("Move to {}/", kind.path().display())));
                            ui.add(egui::TextEdit::singleline(name).desired_width(200.0));
                        });
                        let name = name.clone();
                        match edits::plan_stray_fix(&cfg, &s.path, *kind, &name) {
                            Ok(plan) => {
                                ui.label(RichText::new(format!("{} {}", icon::INFO, plan.summary)).color(ACCENT));
                                if w::primary(ui, icon::ARROW_BEND_DOWN_RIGHT, "Fix it").clicked() {
                                    app.push(Action::Batch { title: format!("Fix {}", s.path.display()), ops: plan.ops, reload: vec![Reload::Config], then: None });
                                }
                            }
                            Err(why) => {
                                ui.label(RichText::new(format!("{} {why}", icon::WARNING)).color(ORANGE));
                            }
                        }
                    }
                    StrayReason::Backup => {
                        ui.label("This looks like a backup copy. Portage does not read it.");
                    }
                    StrayReason::Unknown => {
                        ui.label("Unknown package.* name; Portage does not read it.");
                    }
                }
                if !content.trim().is_empty() {
                    egui::CollapsingHeader::new("Contents").id_salt(&s.path).show(ui, |ui| {
                        ui.label(w::mono(content.trim()));
                    });
                }
                if let Some(why) = &unreadable {
                    ui.label(RichText::new(format!("{} {why}", icon::WARNING)).color(ORANGE));
                } else if w::danger(ui, icon::TRASH, "Delete file").clicked() {
                    // DeleteFile through a reviewed write.
                    app.push(Action::Write { title: format!("Delete {}", s.path.display()), changes: vec![FileChange { path: s.path.clone(), old: content.clone(), new: None }], reload: vec![Reload::Config], then: None });
                }
            });
            ui.add_space(4.0);
        }
    });
}
