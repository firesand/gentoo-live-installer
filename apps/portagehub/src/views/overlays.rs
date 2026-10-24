//! Overlays: configured repositories, syncing, adding and removing.

use super::common::{self, page, subnav};
use crate::app::{Action, App, FileChange, JobKind};
use crate::data::Reload;
use crate::portage::emerge::{self, EmergeOptions};
use crate::portage::repos::{self, RemoteState, Repo};
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Color32, RichText, Ui};
use egui_phosphor::regular as icon;
use std::path::PathBuf;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Page {
    #[default]
    Installed,
    Add,
    Custom,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum SyncType {
    #[default]
    Git,
    Rsync,
    Mercurial,
    Svn,
    Local,
}

impl SyncType {
    fn as_str(&self) -> &'static str {
        match self {
            SyncType::Git => "git",
            SyncType::Rsync => "rsync",
            SyncType::Mercurial => "mercurial",
            SyncType::Svn => "svn",
            SyncType::Local => "",
        }
    }
}

#[derive(Default)]
pub struct State {
    pub page: Page,
    pub search: String,
    pub only_enabled: bool,
    pub editing: Option<(String, Vec<(String, String)>)>,
    pub new_name: String,
    pub new_type: SyncType,
    pub new_uri: String,
    pub new_location: String,
    pub new_priority: String,
    pub new_masters: String,
    pub new_autosync: bool,
    pub new_depth: String,
    pub sync_after: bool,
    pub confirm_remove: Option<String>,
}

pub fn show(app: &mut App, ui: &mut Ui) {
    let mut p = app.overlays.page;
    subnav(ui, &mut p, &[(Page::Installed, "My repositories"), (Page::Add, "Add from official list"), (Page::Custom, "Add custom / local")], |ui| {
        if w::big_button(ui, &format!("{}  SYNC ALL", icon::ARROWS_CLOCKWISE), GRAD_BLUE, 140.0).clicked() {
            app.push(Action::Emerge(EmergeOptions { action: emerge::Action::Sync, ..Default::default() }, vec![]));
        }
    });
    if p == Page::Add && app.overlays.page != Page::Add && app.data.remote_repos.is_none() {
        app.push(Action::Reload(Reload::RemoteRepos));
    }
    app.overlays.page = p;
    match p {
        Page::Installed => installed(app, ui),
        Page::Add => add_official(app, ui),
        Page::Custom => add_custom(app, ui),
    }
    remove_dialog(app, ui.ctx());
}

fn installed(app: &mut App, ui: &mut Ui) {
    let Some(repos) = app.data.repos.clone() else {
        w::loading(ui, "Reading repos.conf…");
        return;
    };
    let counts = app.data.catalog.as_ref().map(|c| c.repo_counts.clone()).unwrap_or_default();
    page(ui, "repos", |ui| {
        w::title(ui, "Repositories");
        w::subtitle(ui, &format!("{} configured · main repository: {}", repos.repos.len(), repos.main_repo));
        ui.add_space(6.0);
        common::tip(
            app,
            ui,
            "Overlays are extra repositories of ebuilds on top of ::gentoo (for example GURU or steam-overlay). Higher priority wins when two repositories provide the same version. Sync downloads their latest state.",
        );
        for w_ in &repos.warnings {
            w::callout(ui, ORANGE, icon::WARNING, w_);
        }
        for r in &repos.repos {
            repo_card(app, ui, r, counts.get(&r.name).copied());
            ui.add_space(6.0);
        }
    });
}

fn repo_card(app: &mut App, ui: &mut Ui, r: &Repo, ebuilds: Option<usize>) {
    let tint = if !r.exists { RED } else if r.is_main { GREEN } else { ACCENT_DIM };
    w::card_tinted(tint).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            let (rect, _) = ui.allocate_exact_size(egui::vec2(46.0, 46.0), egui::Sense::hover());
            let base = common::category_color(&r.name);
            w::paint_vgradient(ui.painter(), rect, w::lerp_color(base, WHITE, 0.15), w::lerp_color(base, Color32::BLACK, 0.3));
            ui.painter().text(rect.center(), egui::Align2::CENTER_CENTER, if r.is_main { icon::CUBE } else { icon::GIT_BRANCH }, egui::FontId::proportional(24.0), WHITE);
            ui.vertical(|ui| {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(&r.name).size(18.0).family(theme::bold()).color(WHITE));
                    if r.is_main {
                        w::badge(ui, "MAIN", GREEN_TEXT, GREEN_DARK);
                    }
                    if !r.exists {
                        w::badge(ui, "MISSING", WHITE, Color32::from_rgb(0xa3, 0x2e, 0x2e)).on_hover_text("The location does not exist — sync it or fix the path");
                    }
                    if r.sync_type() == "none" {
                        w::badge(ui, "LOCAL", TEXT, Color32::from_rgb(0x3a, 0x46, 0x55));
                    } else if !r.auto_sync() {
                        w::badge(ui, "MANUAL SYNC", Color32::from_rgb(0x2b, 0x1a, 0x00), ORANGE).on_hover_text("auto-sync = no: emerge --sync skips it");
                    }
                });
                ui.label(w::mono(r.location.to_string_lossy()));
            });
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if r.sync_type() != "none" && w::primary(ui, icon::ARROWS_CLOCKWISE, "Sync").clicked() {
                    app.push(Action::Root {
                        title: format!("Sync {}", r.name),
                        argv: vec!["emaint".into(), "sync".into(), "-r".into(), r.name.clone()],
                        reload: vec![Reload::Repos],
                        kind: JobKind::Sync,
                    });
                    app.emerge.page = super::emerge::Page::Activity;
                }
                ui.menu_button(RichText::new(icon::DOTS_THREE_VERTICAL).size(18.0), |ui| {
                    if ui.button(format!("{}  Browse its packages", icon::STOREFRONT)).clicked() {
                        app.store.repo_filter = Some(r.name.clone());
                        app.push(Action::Search(String::new()));
                        ui.close();
                    }
                    if r.user_file().is_some() && ui.button(format!("{}  Edit settings", icon::PENCIL_SIMPLE)).clicked() {
                        app.overlays.editing = Some((r.name.clone(), r.keys.clone()));
                        ui.close();
                    }
                    if ui.button(format!("{}  Regenerate metadata cache (egencache)", icon::DATABASE)).on_hover_text("Speeds up overlays without a cache").clicked() {
                        app.push(Action::Root {
                            title: format!("egencache {}", r.name),
                            argv: vec!["egencache".into(), "--update".into(), format!("--repo={}", r.name), "--jobs=8".into()],
                            reload: vec![Reload::Catalog],
                            kind: JobKind::Generic,
                        });
                        ui.close();
                    }
                    if ui.button(format!("{}  Open folder", icon::FOLDER_OPEN)).clicked() {
                        crate::portage::system::open_url(&r.location.to_string_lossy());
                        ui.close();
                    }
                    if !r.is_main {
                        ui.separator();
                        if ui.button(RichText::new(format!("{}  Remove repository…", icon::TRASH)).color(RED)).clicked() {
                            app.overlays.confirm_remove = Some(r.name.clone());
                            ui.close();
                        }
                    }
                });
            });
        });
        ui.add_space(4.0);
        ui.horizontal_wrapped(|ui| {
            let sync = if r.sync_type() == "none" { "no sync".to_string() } else { r.sync_type().to_string() };
            w::stat(ui, "Sync", &sync);
            ui.add_space(16.0);
            w::stat(ui, "Priority", &r.priority().to_string());
            ui.add_space(16.0);
            w::stat(ui, "Ebuilds", &ebuilds.map(|n| n.to_string()).unwrap_or_else(|| "…".into()));
            ui.add_space(16.0);
            w::stat(ui, "Last sync", &r.last_sync.map(crate::fmt::ago_system).unwrap_or_else(|| "unknown".into()));
            ui.add_space(16.0);
            let m = r.masters();
            if !m.is_empty() {
                w::stat(ui, "Masters", &m);
                ui.add_space(16.0);
            }
            if let Some(f) = &r.file {
                w::stat(ui, "Defined in", &f.to_string_lossy());
            }
        });
        if !r.sync_uri().is_empty() {
            ui.horizontal(|ui| {
                ui.label(w::dim("URI"));
                w::link(ui, r.sync_uri(), r.sync_uri());
            });
        }
        // Inline editor
        let editing = app.overlays.editing.as_ref().is_some_and(|(n, _)| *n == r.name);
        if editing {
            ui.separator();
            let mut save = false;
            let mut cancel = false;
            if let Some((_, keys)) = app.overlays.editing.as_mut() {
                egui::Grid::new(("edit-repo", &r.name)).num_columns(3).spacing([10.0, 6.0]).show(ui, |ui| {
                    let mut remove = None;
                    for (i, (k, v)) in keys.iter_mut().enumerate() {
                        ui.add(egui::TextEdit::singleline(k).desired_width(180.0).font(egui::TextStyle::Monospace));
                        ui.add(egui::TextEdit::singleline(v).desired_width(420.0).font(egui::TextStyle::Monospace));
                        if w::icon_button(ui, icon::TRASH, "Remove key").clicked() {
                            remove = Some(i);
                        }
                        ui.end_row();
                    }
                    if let Some(i) = remove {
                        keys.remove(i);
                    }
                });
                ui.horizontal(|ui| {
                    for k in ["priority", "auto-sync", "sync-depth", "masters", "sync-git-verify-commit-signature", "clone-depth"] {
                        if !keys.iter().any(|(x, _)| x == k) && ui.small_button(format!("+ {k}")).clicked() {
                            keys.push((k.to_string(), String::new()));
                        }
                    }
                });
                ui.horizontal(|ui| {
                    if w::primary(ui, icon::FLOPPY_DISK, "Save").clicked() {
                        save = true;
                    }
                    if w::button(ui, "", "Cancel").clicked() {
                        cancel = true;
                    }
                });
            }
            if save && let Some((name, keys)) = app.overlays.editing.take() {
                if let Some(file) = r.user_file() {
                    let old = std::fs::read_to_string(file).unwrap_or_default();
                    let new = repos::replace_section(&old, &name, Some(&repos::render_section(&name, &keys)));
                    app.push(Action::Write {
                        title: format!("Update repository {name}"),
                        changes: vec![FileChange { path: file.to_path_buf(), old, new: Some(new) }],
                        reload: vec![Reload::Repos],
                        then: None,
                    });
                }
            }
            if cancel {
                app.overlays.editing = None;
            }
        }
    });
}

fn remove_dialog(app: &mut App, ctx: &egui::Context) {
    let Some(name) = app.overlays.confirm_remove.clone() else { return };
    let repo = app.data.repos.as_ref().and_then(|r| r.get(&name).cloned());
    let mut close = false;
    egui::Modal::new(egui::Id::new("rm-repo")).show(ctx, |ui| {
        ui.set_width(520.0);
        w::title(ui, &format!("Remove {name}?"));
        ui.label(w::dim("Packages you installed from it stay installed, but you won't get updates for them."));
        ui.add_space(8.0);
        let managed = repo.as_ref().and_then(|r| r.file.as_ref()).is_some_and(|f| f.ends_with("eselect-repo.conf"));
        if managed {
            if w::danger(ui, icon::TRASH, "Remove with eselect (also deletes the files)").clicked() {
                app.push(Action::Root {
                    title: format!("Remove repository {name}"),
                    argv: vec!["eselect".into(), "repository".into(), "remove".into(), "-f".into(), name.clone()],
                    reload: vec![Reload::Repos, Reload::RemoteRepos],
                    kind: JobKind::Generic,
                });
                close = true;
            }
            if w::button(ui, icon::EYE_SLASH, "Disable only (keep files)").clicked() {
                app.push(Action::Root {
                    title: format!("Disable repository {name}"),
                    argv: vec!["eselect".into(), "repository".into(), "disable".into(), name.clone()],
                    reload: vec![Reload::Repos, Reload::RemoteRepos],
                    kind: JobKind::Generic,
                });
                close = true;
            }
        } else if let Some(file) = repo.as_ref().and_then(|r| r.user_file().map(|f| f.to_path_buf())) {
            ui.label(format!("Its section will be removed from {}. The repository files on disk are left alone.", file.display()));
            if w::danger(ui, icon::TRASH, "Remove section").clicked() {
                let old = std::fs::read_to_string(&file).unwrap_or_default();
                let new = repos::replace_section(&old, &name, None);
                let change = if new.trim().is_empty() { FileChange { path: file.clone(), old, new: None } } else { FileChange { path: file.clone(), old, new: Some(new) } };
                app.push(Action::Write { title: format!("Remove repository {name}"), changes: vec![change], reload: vec![Reload::Repos], then: None });
                close = true;
            }
        } else {
            ui.label(w::dim("This repository is defined outside /etc/portage and cannot be removed here."));
        }
        ui.add_space(6.0);
        if w::button(ui, "", "Cancel").clicked() {
            close = true;
        }
    });
    if close {
        app.overlays.confirm_remove = None;
    }
}

fn add_official(app: &mut App, ui: &mut Ui) {
    let Some(list) = app.data.remote_repos.clone() else {
        if !app.is_loading(Reload::RemoteRepos) {
            app.push(Action::Reload(Reload::RemoteRepos));
        }
        w::loading(ui, "Fetching the official overlay list (eselect repository list)…");
        return;
    };
    page(ui, "add-official", |ui| {
        ui.horizontal(|ui| {
            w::title(ui, "Official overlay list");
            ui.label(w::dim(format!("{} repositories from repositories.xml", list.len())));
        });
        if list.is_empty() {
            w::callout(ui, ORANGE, icon::WARNING, "No list available. Install app-eselect/eselect-repository (emerge app-eselect/eselect-repository).");
            return;
        }
        ui.horizontal(|ui| {
            w::search_box(ui, &mut app.overlays.search, "Search overlays", 320.0);
            ui.label(w::dim("Enabled only"));
            w::toggle(ui, &mut app.overlays.only_enabled);
            ui.label(w::dim("Sync right after enabling"));
            w::toggle(ui, &mut app.overlays.sync_after);
            if w::icon_button(ui, icon::ARROWS_CLOCKWISE, "Reload list").clicked() {
                app.push(Action::Reload(Reload::RemoteRepos));
            }
        });
        ui.add_space(6.0);
        let q = app.overlays.search.to_lowercase();
        let shown: Vec<_> = list
            .iter()
            .filter(|r| q.is_empty() || r.name.to_lowercase().contains(&q) || r.homepage.to_lowercase().contains(&q))
            .filter(|r| !app.overlays.only_enabled || r.state != RemoteState::Available)
            .collect();
        egui::Grid::new("remote-grid").num_columns(3).striped(true).spacing([16.0, 8.0]).show(ui, |ui| {
            for r in shown.iter().take(600) {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(icon::GIT_BRANCH).color(ACCENT_DIM));
                    ui.label(RichText::new(&r.name).color(WHITE).family(theme::medium()));
                });
                if r.homepage.is_empty() {
                    ui.label("");
                } else {
                    w::link(ui, &r.homepage, &r.homepage);
                }
                match r.state {
                    RemoteState::Available => {
                        if w::primary(ui, icon::PLUS, "Enable").clicked() {
                            let name = r.name.clone();
                            app.push(Action::Root {
                                title: format!("Enable overlay {name}"),
                                argv: vec!["eselect".into(), "repository".into(), "enable".into(), name.clone()],
                                reload: vec![Reload::Repos, Reload::RemoteRepos],
                                kind: JobKind::Generic,
                            });
                            if app.overlays.sync_after {
                                app.push(Action::Root {
                                    title: format!("Sync {name}"),
                                    argv: vec!["emaint".into(), "sync".into(), "-r".into(), name],
                                    reload: vec![Reload::Repos],
                                    kind: JobKind::Sync,
                                });
                            }
                        }
                    }
                    RemoteState::Enabled => {
                        ui.horizontal(|ui| {
                            w::status_badge(ui, "INSTALLED");
                            if ui.small_button("Disable").clicked() {
                                app.overlays.confirm_remove = Some(r.name.clone());
                            }
                        });
                    }
                    RemoteState::Custom => {
                        w::badge(ui, "ENABLED (CUSTOM)", WHITE, Color32::from_rgb(0x2a, 0x6f, 0x97)).on_hover_text("Configured manually with different settings");
                    }
                    RemoteState::Local => {
                        w::badge(ui, "LOCAL", TEXT, Color32::from_rgb(0x3a, 0x46, 0x55));
                    }
                }
                ui.end_row();
            }
        });
        if shown.len() > 600 {
            ui.label(w::dim(format!("{} more — refine the search", shown.len() - 600)));
        }
    });
}

fn add_custom(app: &mut App, ui: &mut Ui) {
    page(ui, "add-custom", |ui| {
        w::title(ui, "Add a repository");
        w::subtitle(ui, "Point Portage at any git/rsync overlay, or create an empty local overlay for your own ebuilds.");
        ui.add_space(8.0);
        let s = &mut app.overlays;
        w::card().show(ui, |ui| {
            ui.set_width(ui.available_width());
            egui::Grid::new("custom-repo").num_columns(2).spacing([14.0, 10.0]).show(ui, |ui| {
                ui.label("Name");
                ui.add(egui::TextEdit::singleline(&mut s.new_name).hint_text("my-overlay").desired_width(300.0));
                ui.end_row();
                ui.label("Type");
                w::segmented(
                    ui,
                    &mut s.new_type,
                    &[(SyncType::Git, "git"), (SyncType::Rsync, "rsync"), (SyncType::Mercurial, "mercurial"), (SyncType::Svn, "svn"), (SyncType::Local, "Local (no sync)")],
                );
                ui.end_row();
                if s.new_type != SyncType::Local {
                    ui.label("Sync URI");
                    ui.add(egui::TextEdit::singleline(&mut s.new_uri).hint_text("https://github.com/user/overlay.git").desired_width(460.0));
                    ui.end_row();
                    ui.label("Auto sync");
                    w::toggle(ui, &mut s.new_autosync);
                    ui.end_row();
                    if s.new_type == SyncType::Git {
                        ui.label("Clone depth");
                        ui.add(egui::TextEdit::singleline(&mut s.new_depth).hint_text("1 (shallow, recommended)").desired_width(120.0));
                        ui.end_row();
                    }
                }
                ui.label("Location");
                let default_loc = format!("/var/db/repos/{}", if s.new_name.is_empty() { "<name>" } else { &s.new_name });
                ui.add(egui::TextEdit::singleline(&mut s.new_location).hint_text(default_loc).desired_width(460.0));
                ui.end_row();
                ui.label("Priority");
                ui.horizontal(|ui| {
                    ui.add(egui::TextEdit::singleline(&mut s.new_priority).hint_text("0").desired_width(80.0));
                    w::help(ui, "Higher numbers win when several repositories provide the same package version. ::gentoo is usually -1000.");
                });
                ui.end_row();
                ui.label("Masters");
                ui.horizontal(|ui| {
                    ui.add(egui::TextEdit::singleline(&mut s.new_masters).hint_text("gentoo").desired_width(200.0));
                    w::help(ui, "Repositories whose eclasses and profiles this overlay uses. Normally just gentoo.");
                });
                ui.end_row();
            });
        });
        let name = s.new_name.trim().to_string();
        let valid = !name.is_empty() && name.chars().all(|c| c.is_ascii_alphanumeric() || "-_".contains(c));
        let loc = if s.new_location.trim().is_empty() { format!("/var/db/repos/{name}") } else { s.new_location.trim().to_string() };
        let mut keys: Vec<(String, String)> = vec![("location".into(), loc.clone())];
        if s.new_type != SyncType::Local {
            keys.push(("sync-type".into(), s.new_type.as_str().into()));
            keys.push(("sync-uri".into(), s.new_uri.trim().into()));
            keys.push(("auto-sync".into(), if s.new_autosync { "yes".into() } else { "no".into() }));
            if s.new_type == SyncType::Git && !s.new_depth.trim().is_empty() {
                keys.push(("sync-depth".into(), s.new_depth.trim().into()));
            }
        } else {
            keys.push(("auto-sync".into(), "no".into()));
        }
        if !s.new_priority.trim().is_empty() {
            keys.push(("priority".into(), s.new_priority.trim().into()));
        }
        if !s.new_masters.trim().is_empty() {
            keys.push(("masters".into(), s.new_masters.trim().into()));
        }
        let section = repos::render_section(&name, &keys);
        ui.add_space(6.0);
        w::section(ui, "Will be written to /etc/portage/repos.conf");
        egui::Frame::new().fill(BG_DEEP).inner_margin(10).corner_radius(3).show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.label(w::mono(&section));
        });
        ui.add_space(6.0);
        let exists = app.data.repos.as_ref().is_some_and(|r| r.get(&name).is_some());
        if exists {
            ui.label(RichText::new(format!("{} a repository named {name} already exists", icon::WARNING)).color(ORANGE));
        }
        let is_local = s.new_type == SyncType::Local;
        let needs_uri = !is_local && s.new_uri.trim().is_empty();
        ui.horizontal(|ui| {
            let ok = valid && !exists && !needs_uri;
            if ui.add_enabled_ui(ok, |ui| w::big_button(ui, &format!("{}  ADD REPOSITORY", icon::PLUS), GRAD_GREEN, 200.0)).inner.clicked() {
                if is_local {
                    // eselect creates the skeleton (layout.conf, repo_name) and the repos.conf entry.
                    app.push(Action::Root {
                        title: format!("Create local overlay {name}"),
                        argv: vec!["eselect".into(), "repository".into(), "create".into(), name.clone(), loc.clone()],
                        reload: vec![Reload::Repos],
                        kind: JobKind::Generic,
                    });
                } else {
                    let conf = std::path::Path::new(repos::USER_REPOS_CONF);
                    let (path, old, new) = if conf.is_file() {
                        // Single-file repos.conf: append the section.
                        let old = std::fs::read_to_string(conf).unwrap_or_default();
                        let new = repos::replace_section(&old, &name, Some(&section));
                        (conf.to_path_buf(), old, new)
                    } else {
                        (PathBuf::from(format!("{}/{name}.conf", repos::USER_REPOS_CONF)), String::new(), section.clone())
                    };
                    let then = Some(Box::new(Action::Root {
                        title: format!("Sync {name}"),
                        argv: vec!["emaint".into(), "sync".into(), "-r".into(), name.clone()],
                        reload: vec![Reload::Repos],
                        kind: JobKind::Sync,
                    }));
                    app.push(Action::Write {
                        title: format!("Add repository {name}"),
                        changes: vec![FileChange { path, old, new: Some(new) }],
                        reload: vec![Reload::Repos],
                        then,
                    });
                }
            }
            if !valid && !name.is_empty() {
                ui.label(RichText::new("Names may only use letters, digits, - and _").color(ORANGE));
            }
            if needs_uri {
                ui.label(w::dim("Enter a sync URI"));
            }
        });
    });
}
