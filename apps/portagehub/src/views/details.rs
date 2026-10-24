//! Package page: hero banner with the main action, then tabs for versions,
//! USE flags, dependencies, the ebuild, installed files, history and config.

use super::common::{self, category_color, category_icon};
use crate::app::{Action, App, JobKind};
use crate::data::{KwState, PkgDetail, Reload, VersionInfo};
use crate::edits;
use crate::exec::Priv;
use crate::portage::config::{ConfKind, Entry};
use crate::portage::dep::{self, DepNode};
use crate::portage::emerge::{self, EmergeOptions, Value};
use crate::portage::history::fmt_duration;
use crate::portage::metadata;
use crate::portage::useflags::FlagScope;
use crate::portage::vdb::{self, ContentEntry};
use crate::ui::theme::{self, *};
use crate::ui::widgets::{self as w, Tri};
use egui::{Color32, FontId, Margin, RichText, Sense, Ui};
use egui_phosphor::regular as icon;
use std::collections::{BTreeMap, HashSet};
use std::sync::{Arc, Mutex};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum DTab {
    #[default]
    Overview,
    Versions,
    Use,
    Deps,
    Ebuild,
    Files,
    History,
    Config,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum UseScope {
    #[default]
    Package,
    Slot,
    Version,
}

type Shared<T> = Arc<Mutex<Option<T>>>;

/// Reverse dependencies being computed in the background: (key, depender).
type Rdeps = Shared<Vec<(String, String)>>;

#[derive(Default)]
pub struct State {
    pub key: Option<String>,
    pub tab: DTab,
    pub ver: usize,
    pub ver_chosen: bool,
    pub use_edits: BTreeMap<String, Tri>,
    pub use_scope: UseScope,
    pub use_filter: String,
    pub rebuild_after: bool,
    pub deps_active: bool,
    pub files_filter: String,
    files: Option<(String, Arc<Vec<ContentEntry>>)>,
    rdeps: Option<(String, Rdeps)>,
    pub quick_kind: Option<ConfKind>,
    pub quick_value: String,
    pub quick_atom: String,
}

impl State {
    pub fn open(&mut self, key: &str) {
        if self.key.as_deref() != Some(key) {
            *self = State { tab: self.tab, rebuild_after: true, deps_active: self.deps_active, ..Default::default() };
            self.key = Some(key.to_string());
            if matches!(self.tab, DTab::Files | DTab::History | DTab::Config) {
                self.tab = DTab::Overview;
            }
        }
    }
}

pub fn show(app: &mut App, ui: &mut Ui, back: bool) {
    let Some(key) = app.detail.key.clone().or_else(|| app.library.selected.clone()) else {
        w::empty_state(ui, icon::PACKAGE, "No package selected", "");
        return;
    };
    if app.detail.key.is_none() {
        app.detail.open(&key);
    }
    app.request_detail(&key);
    let detail = app.details.get(&key).cloned();
    let vdb = app.data.vdb.clone();
    let installed = vdb.as_ref().map(|v| v.installed(&key).into_iter().cloned().collect::<Vec<_>>()).unwrap_or_default();

    hero(app, ui, &key, detail.as_deref(), &installed, back);

    let Some(detail) = detail else {
        w::loading(ui, "Reading ebuild metadata…");
        return;
    };
    if detail.versions.is_empty() && installed.is_empty() {
        w::empty_state(ui, icon::WARNING, "Package not found", "It is not in any configured repository.");
        return;
    }

    // Tabs
    let has_files = !installed.is_empty();
    let mut tabs = vec![
        (DTab::Overview, format!("{} Overview", icon::INFO)),
        (DTab::Versions, format!("{} Versions ({})", icon::STACK, detail.versions.len())),
        (DTab::Use, format!("{} USE flags", icon::TOGGLE_RIGHT)),
        (DTab::Deps, format!("{} Dependencies", icon::TREE_STRUCTURE)),
        (DTab::Ebuild, format!("{} Ebuild", icon::FILE_CODE)),
    ];
    if has_files {
        tabs.push((DTab::Files, format!("{} Files", icon::FOLDER_OPEN)));
    }
    tabs.push((DTab::History, format!("{} History", icon::CLOCK_COUNTER_CLOCKWISE)));
    let cfg_count = app.data.config.as_ref().map(|c| c.entries_for(&detail.cat, &detail.pn).len()).unwrap_or(0);
    tabs.push((DTab::Config, format!("{} My settings ({cfg_count})", icon::SLIDERS)));
    egui::Frame::new().fill(Color32::from_rgb(0x16, 0x20, 0x2d)).inner_margin(Margin::symmetric(20, 0)).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            for (t, label) in &tabs {
                let sel = app.detail.tab == *t;
                let r = ui.add(
                    egui::Button::new(RichText::new(label).size(13.5).color(if sel { WHITE } else { TEXT_DIM })).frame(false).min_size(egui::vec2(0.0, 36.0)),
                );
                if sel {
                    ui.painter().hline(r.rect.x_range(), r.rect.bottom() - 1.0, egui::Stroke::new(2.0, ACCENT));
                }
                if r.clicked() {
                    app.detail.tab = *t;
                }
            }
        });
    });
    if !app.detail.ver_chosen || app.detail.ver >= detail.versions.len().max(1) {
        // Start on the installed version, else the best installable one.
        let accepts_testing = app.data.env.as_ref().is_some_and(|e| e.accepts_testing());
        app.detail.ver = detail
            .versions
            .iter()
            .position(|v| v.installed)
            .or_else(|| detail.versions.iter().position(|v| v.visible(accepts_testing) && !v.ver.is_live()))
            .or_else(|| detail.versions.iter().position(|v| !v.ver.is_live()))
            .unwrap_or(0);
        app.detail.ver_chosen = true;
    }
    let tab = app.detail.tab;
    common::page(ui, &format!("detail-{key}-{tab:?}"), |ui| match tab {
        DTab::Overview => overview(app, ui, &detail, &installed),
        DTab::Versions => versions(app, ui, &detail),
        DTab::Use => use_flags(app, ui, &detail, &installed),
        DTab::Deps => deps(app, ui, &detail, &installed),
        DTab::Ebuild => ebuild(app, ui, &detail),
        DTab::Files => files(app, ui, &installed),
        DTab::History => history(app, ui, &key),
        DTab::Config => my_settings(app, ui, &detail),
    });
}

fn hero(app: &mut App, ui: &mut Ui, key: &str, detail: Option<&PkgDetail>, installed: &[vdb::Installed], back: bool) {
    let (cat, pn) = key.split_once('/').unwrap_or(("", key));
    let width = ui.available_width();
    let (rect, _) = ui.allocate_exact_size(egui::vec2(width, 200.0), Sense::hover());
    let base = category_color(cat);
    let p = ui.painter();
    w::paint_hgradient(p, rect, w::lerp_color(base, Color32::BLACK, 0.15), w::lerp_color(base, BG, 0.85));
    w::paint_vgradient(p, rect, Color32::from_black_alpha(0), Color32::from_black_alpha(120));
    p.text(
        rect.right_center() + egui::vec2(-120.0, -10.0),
        egui::Align2::CENTER_CENTER,
        category_icon(cat),
        FontId::proportional(160.0),
        Color32::from_white_alpha(22),
    );

    let accepts_testing = app.data.env.as_ref().is_some_and(|e| e.accepts_testing());
    let best = detail.and_then(|d| d.best_visible(accepts_testing));
    let desc = detail
        .and_then(|d| d.versions.first().map(|v| v.meta.get("DESCRIPTION").to_string()))
        .filter(|s| !s.is_empty())
        .or_else(|| installed.last().map(|i| i.desc.clone()))
        .unwrap_or_default();

    let inner = rect.shrink2(egui::vec2(26.0, 16.0));
    let mut child = ui.new_child(egui::UiBuilder::new().max_rect(inner).layout(egui::Layout::top_down(egui::Align::Min)));
    let ui = &mut child;
    if back {
        if ui.add(egui::Button::new(RichText::new(format!("{}  Back", icon::ARROW_LEFT)).color(TEXT)).frame(false)).clicked() {
            app.store.show_detail = false;
        }
    } else {
        ui.add_space(18.0);
    }
    ui.label(RichText::new(cat.to_uppercase()).size(12.0).family(theme::bold()).color(Color32::from_white_alpha(170)));
    ui.label(RichText::new(pn).size(34.0).family(theme::bold()).color(WHITE));
    ui.label(RichText::new(desc).size(14.5).color(TEXT));
    ui.add_space(10.0);
    let update = app.data.updates.as_ref().and_then(|u| u.iter().find(|c| c.key == key).cloned());
    ui.horizontal(|ui| {
        let is_inst = !installed.is_empty();
        let (label, grad, opts) = if !is_inst {
            (format!("{}  INSTALL", icon::DOWNLOAD_SIMPLE), GRAD_GREEN, EmergeOptions::default().targets(&[key]))
        } else if update.is_some() {
            (format!("{}  UPDATE", icon::ARROW_CIRCLE_UP), GRAD_BLUE, EmergeOptions::default().with("--update", Value::On).targets(&[key]))
        } else {
            (format!("{}  REBUILD", icon::ARROWS_CLOCKWISE), GRAD_GREY, EmergeOptions::default().with("--oneshot", Value::On).targets(&[key]))
        };
        let can = detail.is_some_and(|d| !d.versions.is_empty());
        if ui
            .add_enabled_ui(can, |ui| w::big_button(ui, &label, grad, 170.0))
            .inner
            .on_hover_text("Shows a preview first — nothing is changed until you confirm")
            .clicked()
        {
            app.push(Action::Pretend(opts));
        }
        // Options dropdown next to the big button.
        let drop = ui.add(
            egui::Button::new(RichText::new(icon::CARET_DOWN).size(18.0).color(WHITE))
                .fill(Color32::from_rgb(0x2a, 0x3d, 0x52))
                .min_size(egui::vec2(36.0, 40.0)),
        );
        egui::Popup::menu(&drop).show(|ui| {
            ui.set_min_width(280.0);
            action_menu(app, ui, key, detail, is_inst);
        });
        ui.add_space(18.0);
        let inst_text =
            if installed.is_empty() { "Not installed".to_string() } else { installed.iter().map(|i| i.ver.raw.clone()).collect::<Vec<_>>().join(", ") };
        w::stat(ui, "Installed", &inst_text);
        ui.add_space(14.0);
        w::stat(ui, "Best version", &best.map(|b| b.ver.raw.clone()).unwrap_or_else(|| "—".into()));
        ui.add_space(14.0);
        if let Some(i) = installed.last() {
            w::stat(ui, "Last built", &crate::fmt::ago(i.build_time));
            ui.add_space(14.0);
            w::stat(ui, "Size", &crate::fmt::size(i.size));
            ui.add_space(14.0);
        }
        if let Some(h) = &app.data.history
            && let Some(d) = h.estimate(key)
        {
            w::stat(ui, "Build time", &format!("~{}", fmt_duration(d)));
            ui.add_space(14.0);
        }
        if let Some(v) = &app.data.vdb
            && v.in_world(key)
        {
            w::status_badge(ui, "WORLD").on_hover_text("You installed this explicitly; it is part of @world.");
        }
        if let Some(u) = &update {
            w::status_badge(ui, "UPDATE").on_hover_text(format!("{} → {}", u.installed, u.available));
        }
    });
}

fn action_menu(app: &mut App, ui: &mut Ui, key: &str, detail: Option<&PkgDetail>, installed: bool) {
    ui.label(w::dim("Install"));
    if ui.button(format!("{}  Install and add to @world", icon::DOWNLOAD_SIMPLE)).clicked() {
        app.push(Action::Pretend(EmergeOptions::default().targets(&[key])));
        ui.close();
    }
    if ui.button(format!("{}  Install as dependency only (--oneshot)", icon::DOWNLOAD_SIMPLE)).clicked() {
        app.push(Action::Pretend(EmergeOptions::default().with("--oneshot", Value::On).targets(&[key])));
        ui.close();
    }
    if let Some(d) = detail {
        ui.menu_button(format!("{}  Install a specific version", icon::STACK), |ui| {
            for v in d.versions.iter().take(40) {
                let label = format!("{}  ::{}  [{}]", v.ver, v.repo, v.kw.label(&app.data.arch()));
                if ui.button(label).clicked() {
                    let atom = format!("={}", v.cpv(key));
                    app.push(Action::Pretend(EmergeOptions::default().targets(&[atom.as_str()])));
                    ui.close();
                }
            }
        });
        let slots = d.slots();
        if slots.len() > 1 {
            ui.menu_button(format!("{}  Install into slot", icon::SQUARES_FOUR), |ui| {
                for s in slots {
                    if ui.button(format!("{key}:{s}")).clicked() {
                        let atom = format!("{key}:{s}");
                        app.push(Action::Pretend(EmergeOptions::default().targets(&[atom.as_str()])));
                        ui.close();
                    }
                }
            });
        }
    }
    if ui.button(format!("{}  Download sources only", icon::CLOUD_ARROW_DOWN)).clicked() {
        app.push(Action::Pretend(EmergeOptions::default().with("--fetchonly", Value::On).targets(&[key])));
        ui.close();
    }
    if ui.button(format!("{}  Install with binary packages if available", icon::PACKAGE)).clicked() {
        app.push(Action::Pretend(EmergeOptions::default().with("--getbinpkg", Value::On).with("--usepkg", Value::On).targets(&[key])));
        ui.close();
    }
    if ui.button(format!("{}  Customise all emerge options…", icon::SLIDERS)).clicked() {
        app.emerge.options = EmergeOptions::default().targets(&[key]);
        app.emerge.page = super::emerge::Page::Custom;
        app.push(Action::Tab(crate::app::Tab::Emerge));
        ui.close();
    }
    if installed {
        ui.separator();
        ui.label(w::dim("Installed package"));
        if ui.button(format!("{}  Update (-u)", icon::ARROW_CIRCLE_UP)).clicked() {
            app.push(Action::Pretend(EmergeOptions::default().with("--update", Value::On).targets(&[key])));
            ui.close();
        }
        if ui.button(format!("{}  Rebuild (--oneshot)", icon::ARROWS_CLOCKWISE)).clicked() {
            app.push(Action::Pretend(EmergeOptions::default().with("--oneshot", Value::On).targets(&[key])));
            ui.close();
        }
        let in_world = app.data.vdb.as_ref().is_some_and(|v| v.in_world(key));
        if in_world {
            if ui.button(format!("{}  Remove from @world (keep installed)", icon::STAR_HALF)).clicked() {
                app.push(Action::Root {
                    title: format!("Deselect {key}"),
                    argv: vec!["emerge".into(), "--deselect".into(), key.into()],
                    reload: vec![Reload::Vdb],
                    kind: JobKind::Generic,
                });
                ui.close();
            }
        } else if ui.button(format!("{}  Add to @world", icon::STAR)).clicked() {
            app.push(Action::Root {
                title: format!("Select {key}"),
                argv: vec!["emerge".into(), "--noreplace".into(), key.into()],
                reload: vec![Reload::Vdb],
                kind: JobKind::Generic,
            });
            ui.close();
        }
        if ui.button(format!("{}  Remove safely (--depclean)", icon::TRASH)).clicked() {
            app.push(Action::Pretend(EmergeOptions { action: emerge::Action::Depclean, ..Default::default() }.targets(&[key])));
            ui.close();
        }
        if ui
            .button(RichText::new(format!("{}  Force remove (--unmerge)", icon::WARNING)).color(RED))
            .on_hover_text("Removes it even if other packages need it")
            .clicked()
        {
            app.push(Action::Pretend(EmergeOptions { action: emerge::Action::Unmerge, ..Default::default() }.targets(&[key])));
            ui.close();
        }
        if ui.button(format!("{}  Run pkg_config", icon::GEAR)).clicked() {
            app.push(Action::Root {
                title: format!("emerge --config {key}"),
                argv: vec!["emerge".into(), "--config".into(), key.into()],
                reload: vec![],
                kind: JobKind::Generic,
            });
            ui.close();
        }
    }
    ui.separator();
    ui.label(w::dim("Configure"));
    if ui.button(format!("{}  Edit USE flags", icon::TOGGLE_RIGHT)).clicked() {
        app.detail.tab = DTab::Use;
        ui.close();
    }
    if ui.button(format!("{}  Accept testing keywords…", icon::FLASK)).clicked() {
        app.detail.tab = DTab::Config;
        app.detail.quick_kind = Some(ConfKind::AcceptKeywords);
        app.detail.quick_value = format!("~{}", app.data.arch());
        app.detail.quick_atom = key.to_string();
        ui.close();
    }
    if ui.button(format!("{}  Mask…", icon::PROHIBIT)).clicked() {
        app.detail.tab = DTab::Config;
        app.detail.quick_kind = Some(ConfKind::Mask);
        app.detail.quick_atom = key.to_string();
        ui.close();
    }
    ui.separator();
    if ui.button(format!("{}  Copy atom", icon::COPY)).clicked() {
        ui.ctx().copy_text(key.to_string());
        ui.close();
    }
}

fn kv(ui: &mut Ui, k: &str, v: impl Into<egui::WidgetText>) {
    ui.label(RichText::new(k).color(TEXT_DIM));
    ui.add(egui::Label::new(v).wrap());
    ui.end_row();
}

fn overview(app: &mut App, ui: &mut Ui, d: &PkgDetail, installed: &[vdb::Installed]) {
    let arch = app.data.arch();
    let Some(v) = d.versions.get(app.detail.ver).or(d.versions.first()) else {
        ui.label(w::dim("This package is installed but no repository provides it anymore."));
        if let Some(i) = installed.last() {
            ui.label(format!("Installed from ::{} — consider removing it or adding the repository back.", i.repo));
        }
        return;
    };
    ui.columns(2, |cols| {
        let ui = &mut cols[0];
        if !d.xml.longdescription.is_empty() {
            w::section(ui, "About");
            ui.label(&d.xml.longdescription);
        }
        w::section(ui, "Details");
        egui::Grid::new("ov-grid").num_columns(2).spacing([16.0, 6.0]).show(ui, |ui| {
            ui.label(RichText::new("Homepage").color(TEXT_DIM));
            ui.vertical(|ui| {
                for h in v.meta.get("HOMEPAGE").split_whitespace() {
                    w::link(ui, h, h);
                }
            });
            ui.end_row();
            kv(ui, "License", v.meta.get("LICENSE"));
            kv(ui, "Slots", d.slots().join(", "));
            kv(ui, "EAPI", v.meta.get("EAPI"));
            if !v.meta.get("RESTRICT").is_empty() {
                kv(ui, "Restrict", v.meta.get("RESTRICT"));
            }
            if !v.meta.get("PROPERTIES").is_empty() {
                kv(ui, "Properties", v.meta.get("PROPERTIES"));
            }
            if !v.meta.get("INHERIT").is_empty() {
                kv(ui, "Eclasses", v.meta.get("INHERIT"));
            }
            let repos: Vec<String> = {
                let mut r: Vec<String> = d.versions.iter().map(|v| v.repo.clone()).collect();
                r.dedup();
                r.into_iter().collect::<std::collections::BTreeSet<_>>().into_iter().map(|r| format!("::{r}")).collect()
            };
            kv(ui, "Repositories", repos.join("  "));
            kv(ui, "Metadata from", RichText::new(v.meta.source.label()).color(TEXT_DIM));
        });
        if let Some(i) = installed.last() {
            w::section(ui, "Installed");
            egui::Grid::new("inst-grid").num_columns(2).spacing([16.0, 6.0]).show(ui, |ui| {
                for i in installed {
                    kv(ui, "Version", RichText::new(format!("{}  (slot {}, from ::{})", i.ver, i.slot, i.repo)).color(WHITE));
                }
                kv(ui, "Built", format!("{} ({})", crate::fmt::date(i.build_time), crate::fmt::ago(i.build_time)));
                let cflags = i.read("CFLAGS");
                if !cflags.is_empty() {
                    kv(ui, "CFLAGS", RichText::new(cflags).monospace().size(12.0));
                }
                let use_ = i.read("USE");
                let iuse: HashSet<String> = i.read("IUSE").split_whitespace().map(|f| f.trim_start_matches(['+', '-']).to_string()).collect();
                let on: Vec<&str> = use_.split_whitespace().filter(|f| iuse.contains(*f)).collect();
                kv(ui, "Built with", RichText::new(on.join(" ")).monospace().size(12.0).color(GREEN_TEXT));
            });
            let mut world = app.data.vdb.as_ref().is_some_and(|v| v.in_world(&d.key));
            ui.add_space(6.0);
            if w::switch_row(
                ui,
                &mut world,
                "Selected (in @world)",
                "Packages in @world are kept and updated. Packages not in @world are dependencies and are removed by depclean once nothing needs them.",
            )
            .changed()
            {
                let (flag, title) = if world { ("--noreplace", "Add to @world") } else { ("--deselect", "Remove from @world") };
                app.push(Action::Root {
                    title: format!("{title}: {}", d.key),
                    argv: vec!["emerge".into(), flag.into(), d.key.clone()],
                    reload: vec![Reload::Vdb],
                    kind: JobKind::Generic,
                });
            }
        }
    });
    ui.add_space(8.0);
    ui.columns(2, |cols| {
        let ui = &mut cols[0];
        w::section(ui, &format!("Keywords of {}", v.ver));
        ui.horizontal_wrapped(|ui| {
            for k in v.meta.keywords() {
                let mine = k.trim_start_matches(['~', '-']) == arch;
                let (fg, bg) = if k.starts_with('~') {
                    (Color32::from_rgb(0x2b, 0x1a, 0x00), ORANGE)
                } else if k.starts_with('-') {
                    (WHITE, Color32::from_rgb(0xa3, 0x2e, 0x2e))
                } else {
                    (GREEN_TEXT, GREEN_DARK)
                };
                let r = w::badge(ui, &k, fg, if mine { bg } else { bg.gamma_multiply(0.45) });
                if mine {
                    r.on_hover_text("Your architecture");
                }
            }
            if v.meta.keywords().is_empty() {
                ui.label(w::dim("No keywords (live or experimental ebuild)."));
            }
        });
        let ui = &mut cols[1];
        if !d.xml.maintainers.is_empty() || !d.xml.upstream.is_empty() {
            w::section(ui, "People & upstream");
            for m in &d.xml.maintainers {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(if m.kind == "project" { icon::USERS_THREE } else { icon::USER }).color(ACCENT_DIM));
                    let name = if m.name.is_empty() { m.email.clone() } else { format!("{} <{}>", m.name, m.email) };
                    ui.label(name);
                });
            }
            for (k, val) in &d.xml.upstream {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(icon::LINK).color(ACCENT_DIM));
                    match metadata::remote_url(k, val) {
                        Some(url) => {
                            w::link(ui, &format!("{k}: {val}"), &url);
                        }
                        None => {
                            ui.label(format!("{k}: {val}"));
                        }
                    }
                });
            }
        }
    });
}

fn kw_badge(ui: &mut Ui, v: &VersionInfo, arch: &str) {
    if v.ver.is_live() {
        w::status_badge(ui, "LIVE").on_hover_text("Builds the latest upstream source (9999). Needs ** keywords.");
        return;
    }
    match v.kw {
        KwState::Stable => w::status_badge(ui, "STABLE"),
        KwState::Testing => w::status_badge(ui, "TESTING").on_hover_text(format!("Keyword ~{arch}: needs Accept keywords unless you run the testing branch")),
        KwState::Missing => w::status_badge(ui, "NO KEYWORD").on_hover_text("Not keyworded for your arch: needs ** in package.accept_keywords"),
        KwState::Broken => w::status_badge(ui, "BROKEN").on_hover_text(format!("-{arch}: known not to work on your arch")),
    };
}

fn versions(app: &mut App, ui: &mut Ui, d: &PkgDetail) {
    let arch = app.data.arch();
    let accepts_testing = app.data.env.as_ref().is_some_and(|e| e.accepts_testing());
    common::tip(
        app,
        ui,
        "Click a version for actions. STABLE versions install by default; TESTING (~arch) ones need an Accept-keywords entry; versions you masked stay hidden. Slots let several versions be installed side by side.",
    );
    let mut by_slot: BTreeMap<String, Vec<&VersionInfo>> = BTreeMap::new();
    for v in &d.versions {
        by_slot.entry(v.meta.slot().split('/').next().unwrap_or("0").to_string()).or_default().push(v);
    }
    let slot_desc: BTreeMap<String, String> = d.xml.slots.iter().cloned().collect();
    let mut slots: Vec<(&String, &Vec<&VersionInfo>)> = by_slot.iter().collect();
    // Newest slot first, comparing slots like versions ("17" > "9.5.0").
    slots.sort_by(|a, b| {
        let va = crate::portage::version::Version::parse(a.0);
        let vb = crate::portage::version::Version::parse(b.0);
        match (va, vb) {
            (Some(x), Some(y)) => y.cmp(&x),
            _ => b.0.cmp(a.0),
        }
    });
    for (slot, list) in slots {
        w::section(ui, &format!("Slot {slot}{}", slot_desc.get(slot).map(|s| format!(" — {s}")).unwrap_or_default()));
        let mut shown_reason: Option<Vec<String>> = None;
        for v in list {
            let row = w::card().inner_margin(Margin::symmetric(12, 8)).show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    ui.label(RichText::new(v.ver.raw.clone()).size(16.0).family(theme::medium()).color(WHITE));
                    ui.label(w::dim(format!("::{}", v.repo)));
                    ui.label(w::dim(format!("slot {}", v.meta.slot())));
                    kw_badge(ui, v, &arch);
                    if v.installed {
                        w::status_badge(ui, "INSTALLED");
                    }
                    if v.user_masked && !v.user_unmasked {
                        w::status_badge(ui, "MASKED").on_hover_text("Masked by your package.mask");
                    } else if let Some((src, reason)) = &v.profile_mask
                        && !v.user_unmasked
                    {
                        let why = if reason.is_empty() { String::new() } else { format!("\n\n{}", reason.join("\n")) };
                        w::status_badge(ui, "MASKED").on_hover_text(format!("Masked by {}{why}", src.display()));
                    }
                    if v.user_unmasked {
                        w::badge(ui, "UNMASKED", WHITE, Color32::from_rgb(0x2a, 0x6f, 0x97));
                    }
                    if v.user_keyworded && v.kw != KwState::Stable {
                        w::badge(ui, "KEYWORD ACCEPTED", WHITE, Color32::from_rgb(0x2a, 0x6f, 0x97));
                    }
                    if !v.visible(accepts_testing) {
                        ui.label(RichText::new(format!("{} not installable as-is", icon::LOCK)).color(TEXT_FAINT).size(12.0));
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.label(RichText::new(icon::DOTS_THREE).size(18.0).color(TEXT_DIM));
                        ui.label(w::dim(format!("EAPI {}", v.meta.get("EAPI"))));
                    });
                });
            });
            let resp = row.response.interact(Sense::click());
            if let Some((src, reason)) = &v.profile_mask
                && !v.user_unmasked
                && !reason.is_empty()
                && shown_reason.as_ref() != Some(reason)
            {
                w::callout(ui, ORANGE, icon::PROHIBIT, &format!("{}\n— {}", reason.join(" "), src.display()));
                shown_reason = Some(reason.clone());
            }
            let resp = resp.on_hover_cursor(egui::CursorIcon::PointingHand);
            egui::Popup::menu(&resp).show(|ui| {
                ui.set_min_width(300.0);
                version_menu(app, ui, d, v);
            });
            resp.context_menu(|ui| version_menu(app, ui, d, v));
            ui.add_space(3.0);
        }
    }
}

fn version_menu(app: &mut App, ui: &mut Ui, d: &PkgDetail, v: &VersionInfo) {
    let arch = app.data.arch();
    let cpv = v.cpv(&d.key);
    let eq = format!("={cpv}");
    let target = app.settings.target_file.clone();
    ui.label(RichText::new(&cpv).color(WHITE).family(theme::medium()));
    ui.separator();
    if ui.button(format!("{}  Install exactly this version", icon::DOWNLOAD_SIMPLE)).clicked() {
        app.push(Action::Pretend(EmergeOptions::default().targets(&[eq.as_str()])));
        ui.close();
    }
    if ui.button(format!("{}  View ebuild", icon::FILE_CODE)).clicked() {
        app.detail.ver = d.versions.iter().position(|x| x.ver == v.ver && x.repo == v.repo).unwrap_or(0);
        app.detail.tab = DTab::Ebuild;
        ui.close();
    }
    if ui.button(format!("{}  Show dependencies", icon::TREE_STRUCTURE)).clicked() {
        app.detail.ver = d.versions.iter().position(|x| x.ver == v.ver && x.repo == v.repo).unwrap_or(0);
        app.detail.tab = DTab::Deps;
        ui.close();
    }
    if ui.button(format!("{}  USE flags of this version", icon::TOGGLE_RIGHT)).clicked() {
        app.detail.ver = d.versions.iter().position(|x| x.ver == v.ver && x.repo == v.repo).unwrap_or(0);
        app.detail.tab = DTab::Use;
        ui.close();
    }
    let Some(cfg) = app.data.config.clone() else { return };
    ui.separator();
    ui.label(w::dim("Keywords (package.accept_keywords)"));
    let add = |app: &mut App, kind: ConfKind, line: String, title: String| {
        let ch = edits::add_lines(&cfg, &[(kind, line)], &target, None);
        app.push(Action::Write { title, changes: ch, reload: vec![Reload::Config], then: None });
    };
    if v.kw == KwState::Testing || v.ver.is_live() || v.kw == KwState::Missing {
        let kwv = if v.kw == KwState::Testing { format!("~{arch}") } else { "**".to_string() };
        if ui.button(format!("{}  Accept {kwv} for this version only", icon::FLASK)).clicked() {
            add(app, ConfKind::AcceptKeywords, format!("{eq} {kwv}"), format!("Accept keyword for {cpv}"));
            ui.close();
        }
        if ui.button(format!("{}  Accept {kwv} for this and newer (>=)", icon::FLASK)).clicked() {
            add(app, ConfKind::AcceptKeywords, format!(">={cpv} {kwv}"), format!("Accept keyword for >={cpv}"));
            ui.close();
        }
        if ui.button(format!("{}  Accept {kwv} for every version", icon::FLASK)).clicked() {
            add(app, ConfKind::AcceptKeywords, format!("{} {kwv}", d.key), format!("Accept keyword for {}", d.key));
            ui.close();
        }
    } else {
        ui.label(w::dim("  This version is stable for your arch."));
    }
    ui.separator();
    ui.label(w::dim("Masks (package.mask / package.unmask)"));
    if ui.button(format!("{}  Mask this version", icon::PROHIBIT)).clicked() {
        add(app, ConfKind::Mask, eq.clone(), format!("Mask {cpv}"));
        ui.close();
    }
    if ui
        .button(format!("{}  Mask newer versions (> {})", icon::PROHIBIT, v.ver))
        .on_hover_text("Stay at this version: Portage won't upgrade past it")
        .clicked()
    {
        add(app, ConfKind::Mask, format!(">{cpv}"), format!("Mask versions newer than {cpv}"));
        ui.close();
    }
    if ui.button(format!("{}  Unmask this version", icon::LOCK_OPEN)).on_hover_text("Lift a repository/profile mask").clicked() {
        add(app, ConfKind::Unmask, eq.clone(), format!("Unmask {cpv}"));
        ui.close();
    }
    let license = v.meta.get("LICENSE").to_string();
    if !license.is_empty() {
        ui.separator();
        if ui.button(format!("{}  Accept license(s): {}", icon::CERTIFICATE, license)).clicked() {
            let mut lics = Vec::new();
            dep::collect_leaves(&dep::parse(&license), None, &mut lics);
            lics.retain(|l| l != "||");
            add(app, ConfKind::License, format!("{eq} {}", lics.join(" ")), format!("Accept license for {cpv}"));
            ui.close();
        }
    }
    ui.separator();
    if ui.button(format!("{}  Open ebuild in editor", icon::ARROW_SQUARE_OUT)).clicked() {
        crate::portage::system::open_url(&v.ebuild_path.to_string_lossy());
        ui.close();
    }
    if ui.button(format!("{}  Copy =atom", icon::COPY)).clicked() {
        ui.ctx().copy_text(eq.clone());
        ui.close();
    }
}

fn version_picker(app: &mut App, ui: &mut Ui, d: &PkgDetail) {
    if d.versions.is_empty() {
        return;
    }
    let cur = &d.versions[app.detail.ver.min(d.versions.len() - 1)];
    egui::ComboBox::from_id_salt("ver-pick").selected_text(format!("{}  ::{}", cur.ver, cur.repo)).width(260.0).show_ui(ui, |ui| {
        for (i, v) in d.versions.iter().enumerate() {
            let mark = if v.installed { format!("  {}", icon::CHECK) } else { String::new() };
            ui.selectable_value(&mut app.detail.ver, i, format!("{}  ::{}{mark}", v.ver, v.repo));
        }
    });
}

fn use_flags(app: &mut App, ui: &mut Ui, d: &PkgDetail, installed: &[vdb::Installed]) {
    if d.versions.is_empty() {
        return;
    }
    let v = d.versions[app.detail.ver.min(d.versions.len() - 1)].clone();
    let usedesc = app.data.usedesc.clone();
    let cfg = app.data.config.clone();
    let env = app.data.env.clone();
    let user = cfg.as_ref().map(|c| edits::user_use_state(c, &d.cat, &d.pn)).unwrap_or_default();
    let inst = installed.iter().rev().find(|i| i.ver == v.ver).or(installed.last());
    let inst_use: Option<HashSet<String>> = inst.map(|i| i.read("USE").split_whitespace().map(String::from).collect());
    let global: HashSet<String> = env.as_ref().map(|e| e.global_use().into_iter().collect()).unwrap_or_default();
    let cap_tag = format!("use:{}", v.cpv(&d.key));
    let effective: Option<BTreeMap<String, bool>> = app.captures.get(&cap_tag).filter(|c| c.done).map(|c| {
        let mut m = BTreeMap::new();
        for line in c.output.lines() {
            if let Some(e) = crate::portage::pretend::parse_entry(line)
                && e.key == d.key
            {
                for (f, on, _) in e.use_changes() {
                    m.insert(f, on);
                }
            }
        }
        m
    });

    common::tip(
        app,
        ui,
        "USE flags switch optional features on or off. “Default” keeps what your profile and make.conf decide; On/Off writes a per-package override to package.use. Changes take effect the next time the package is built.",
    );
    ui.horizontal(|ui| {
        ui.label(w::dim("Version"));
        version_picker(app, ui, d);
        ui.add_space(10.0);
        ui.label(w::dim("Apply to"));
        let slot = v.meta.slot().split('/').next().unwrap_or("0").to_string();
        w::segmented(
            ui,
            &mut app.detail.use_scope,
            &[(UseScope::Package, "All versions"), (UseScope::Slot, &format!("Slot {slot}")), (UseScope::Version, "This version")],
        );
        ui.add_space(10.0);
        let running = app.captures.get(&cap_tag).is_some_and(|c| !c.done);
        if running {
            ui.add(egui::Spinner::new().color(ACCENT));
            ui.label(w::dim("Asking emerge…"));
        } else if w::button(ui, icon::MAGIC_WAND, "Ask emerge for effective flags")
            .on_hover_text("Runs emerge -pv --nodeps for this version (no changes)")
            .clicked()
        {
            app.push(Action::Capture {
                tag: cap_tag.clone(),
                argv: vec!["emerge".into(), "-pv".into(), "--nodeps".into(), "--color=n".into(), "--ask=n".into(), format!("={}", v.cpv(&d.key))],
                privilege: Priv::User,
            });
        }
        ui.add_space(6.0);
        w::search_box(ui, &mut app.detail.use_filter, "Filter flags", 180.0);
    });
    ui.add_space(6.0);

    let iuse = v.meta.iuse();
    if iuse.is_empty() {
        w::empty_state(ui, icon::TOGGLE_LEFT, "No USE flags", "This version has no optional features.");
        return;
    }
    // Group USE_EXPAND flags.
    let mut groups: BTreeMap<String, Vec<(String, Option<bool>)>> = BTreeMap::new();
    for (f, def) in iuse {
        let g = usedesc.as_ref().and_then(|u| u.split_expand(&f).map(|(p, _)| p.to_uppercase())).unwrap_or_else(|| "Features".to_string());
        groups.entry(g).or_default().push((f, def));
    }
    let filter = app.detail.use_filter.to_lowercase();
    let atom = match app.detail.use_scope {
        UseScope::Package => d.key.clone(),
        UseScope::Slot => format!("{}:{}", d.key, v.meta.slot().split('/').next().unwrap_or("0")),
        UseScope::Version => format!("={}", v.cpv(&d.key)),
    };
    let required_use = v.meta.get("REQUIRED_USE").to_string();
    let mut ordered: Vec<_> = groups.iter().collect();
    ordered.sort_by_key(|(g, _)| (g.as_str() != "Features", g.to_string()));
    for (g, flags) in ordered {
        let shown: Vec<_> = flags.iter().filter(|(f, _)| filter.is_empty() || f.contains(&filter)).collect();
        if shown.is_empty() {
            continue;
        }
        let header = if g == "Features" { format!("Features ({})", shown.len()) } else { format!("{g} ({})", shown.len()) };
        egui::CollapsingHeader::new(RichText::new(header).family(theme::bold()).color(WHITE))
            .id_salt(g)
            .default_open(g == "Features" || shown.len() <= 6)
            .show(ui, |ui| {
                egui::Grid::new(format!("use-{g}")).num_columns(4).striped(true).spacing([14.0, 6.0]).min_col_width(40.0).show(ui, |ui| {
                    for (flag, def) in shown {
                        let (desc, scope) = usedesc.as_ref().map(|u| u.describe(&d.key, flag, Some(&d.xml.flags))).unwrap_or_default();
                        let estimate = user.get(flag).map(|x| x.0).unwrap_or_else(|| global.contains(flag) || def.unwrap_or(false));
                        let now_on =
                            effective.as_ref().and_then(|m| m.get(flag).copied()).or_else(|| inst_use.as_ref().map(|s| s.contains(flag))).unwrap_or(estimate);
                        // Current tri state for *this* atom
                        let base = match user.get(flag) {
                            Some((on, a)) if *a == atom => {
                                if *on {
                                    Tri::On
                                } else {
                                    Tri::Off
                                }
                            }
                            _ => Tri::Default,
                        };
                        let mut state = *app.detail.use_edits.get(flag).unwrap_or(&base);
                        let display = if let Some(x) = usedesc.as_ref().and_then(|u| u.split_expand(flag)) { x.1.to_string() } else { flag.clone() };
                        ui.horizontal(|ui| {
                            let col = if now_on { GREEN_TEXT } else { TEXT_DIM };
                            ui.label(RichText::new(if now_on { icon::CHECK_SQUARE } else { icon::SQUARE }).color(col));
                            ui.label(RichText::new(&display).monospace().color(if now_on { WHITE } else { TEXT })).on_hover_text(flag);
                        });
                        let tri_resp = w::tri(ui, &mut state, *def);
                        if tri_resp.changed() {
                            if state == base {
                                app.detail.use_edits.remove(flag);
                            } else {
                                app.detail.use_edits.insert(flag.clone(), state);
                            }
                        }
                        ui.horizontal(|ui| {
                            let tag = match scope {
                                FlagScope::Local => Some(("local", ACCENT_DIM)),
                                FlagScope::Global => Some(("global", TEXT_FAINT)),
                                FlagScope::Expand => Some(("expand", PURPLE)),
                                FlagScope::Unknown => None,
                            };
                            if let Some((t, c)) = tag {
                                ui.label(RichText::new(t).size(10.5).color(c));
                            }
                            if let Some((on, a)) = user.get(flag)
                                && *a != atom
                            {
                                ui.label(RichText::new(format!("{} {} via {a}", icon::PUSH_PIN, if *on { "on" } else { "off" })).size(11.0).color(ORANGE));
                            }
                        });
                        ui.add(egui::Label::new(RichText::new(if desc.is_empty() { "—".to_string() } else { desc }).color(TEXT_DIM)).wrap());
                        ui.end_row();
                    }
                });
            });
    }
    if !required_use.is_empty() {
        w::section(ui, "Rules (REQUIRED_USE)");
        ui.label(w::mono(&required_use));
        ui.label(w::dim("^^ = exactly one, || = at least one, ?? = at most one, flag? ( … ) = only when flag is on."));
    }
    let pending = app.detail.use_edits.len();
    ui.add_space(10.0);
    w::card_tinted(if pending > 0 { GREEN } else { LINE }).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            if pending == 0 {
                ui.label(w::dim(format!("{} Change a flag above to create a package.use entry for {atom}", icon::INFO)));
                return;
            }
            let summary: Vec<String> = app
                .detail
                .use_edits
                .iter()
                .map(|(f, s)| match s {
                    Tri::On => f.clone(),
                    Tri::Off => format!("-{f}"),
                    Tri::Default => format!("(reset {f})"),
                })
                .collect();
            ui.vertical(|ui| {
                ui.label(w::strong(format!("{pending} pending change(s) for {atom}")));
                ui.label(w::mono(summary.join(" ")));
            });
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if w::big_button(ui, &format!("{}  SAVE", icon::FLOPPY_DISK), GRAD_GREEN, 120.0).clicked()
                    && let Some(cfg) = &cfg
                {
                    let changes: Vec<(String, Tri)> = app.detail.use_edits.iter().map(|(k, v)| (k.clone(), *v)).collect();
                    let fc = edits::set_use_flags(cfg, &atom, &changes, &app.settings.target_file);
                    let then = if app.detail.rebuild_after && !installed.is_empty() {
                        Some(Box::new(Action::Pretend(
                            EmergeOptions::default().with("--oneshot", Value::On).with("--changed-use", Value::On).targets(&[d.key.as_str()]),
                        )))
                    } else {
                        None
                    };
                    app.push(Action::Write { title: format!("USE flags for {atom}"), changes: fc, reload: vec![Reload::Config], then });
                    app.detail.use_edits.clear();
                }
                if w::button(ui, icon::X, "Discard").clicked() {
                    app.detail.use_edits.clear();
                }
                if !installed.is_empty() {
                    ui.checkbox(&mut app.detail.rebuild_after, "Preview a rebuild after saving");
                }
            });
        });
    });
}

fn deps(app: &mut App, ui: &mut Ui, d: &PkgDetail, installed: &[vdb::Installed]) {
    if d.versions.is_empty() {
        return;
    }
    let v = d.versions[app.detail.ver.min(d.versions.len() - 1)].clone();
    let vdb = app.data.vdb.clone();
    ui.horizontal(|ui| {
        ui.label(w::dim("Version"));
        version_picker(app, ui, d);
        ui.add_space(12.0);
        ui.label(w::dim("Only what applies to my USE flags"));
        w::toggle(ui, &mut app.detail.deps_active);
    });
    let enabled: HashSet<String> = {
        let inst = installed.iter().rev().find(|i| i.ver == v.ver).or(installed.last());
        match inst {
            Some(i) => i.read("USE").split_whitespace().map(String::from).collect(),
            None => {
                let mut s: HashSet<String> = app.data.env.as_ref().map(|e| e.global_use().into_iter().collect()).unwrap_or_default();
                for (f, def) in v.meta.iuse() {
                    if def == Some(true) {
                        s.insert(f);
                    }
                }
                s
            }
        }
    };
    let active = if app.detail.deps_active { Some(&enabled) } else { None };
    ui.add_space(6.0);
    for (var, title, help) in [
        ("RDEPEND", "Runtime dependencies", "Needed to run the package."),
        ("DEPEND", "Build dependencies (target)", "Needed while compiling, for the system being built for."),
        ("BDEPEND", "Build tools", "Programs run during the build (compilers, generators)."),
        ("PDEPEND", "Post dependencies", "Installed after the package (often plugins or circular helpers)."),
        ("IDEPEND", "Install-time dependencies", "Needed when merging the package."),
    ] {
        let text = v.meta.get(var).to_string();
        if text.trim().is_empty() {
            continue;
        }
        let tree = dep::parse(&text);
        let mut count = Vec::new();
        dep::collect_atoms(&tree, active, &mut count);
        egui::CollapsingHeader::new(RichText::new(format!("{title}  ({})", count.len())).family(theme::bold()).color(WHITE))
            .id_salt(var)
            .default_open(var == "RDEPEND" || var == "DEPEND")
            .show(ui, |ui| {
                ui.label(w::dim(help));
                dep_tree(app, ui, &tree, active, &enabled, vdb.as_deref());
            });
    }
    // Reverse dependencies
    w::section(ui, "Required by (installed packages)");
    let key = d.key.clone();
    let state = app.detail.rdeps.clone().filter(|(k, _)| *k == key);
    match state {
        None => {
            if w::button(ui, icon::MAGNIFYING_GLASS, "Find installed packages that depend on this").clicked()
                && let Some(vdb) = vdb.clone()
            {
                let slot: Shared<Vec<(String, String)>> = Arc::default();
                let s2 = slot.clone();
                let ctx = ui.ctx().clone();
                let k2 = key.clone();
                std::thread::spawn(move || {
                    let r = crate::data::reverse_deps(&vdb, &k2);
                    *s2.lock().unwrap() = Some(r);
                    ctx.request_repaint();
                });
                app.detail.rdeps = Some((key.clone(), slot));
            }
        }
        Some((_, slot)) => match slot.lock().unwrap().as_ref() {
            None => {
                ui.add(egui::Spinner::new().color(ACCENT));
            }
            Some(list) if list.is_empty() => {
                ui.label(w::dim("Nothing installed depends on it — it can be removed if you don't use it directly."));
            }
            Some(list) => {
                ui.horizontal_wrapped(|ui| {
                    for (cpv, var) in list {
                        let k = crate::portage::atom::split_cpv(cpv).map(|(c, p, _)| format!("{c}/{p}")).unwrap_or_else(|| cpv.clone());
                        if ui.add(egui::Button::new(RichText::new(format!("{cpv}  ({})", var.to_lowercase())).size(12.5))).clicked() {
                            app.push(Action::Open(k));
                        }
                    }
                });
            }
        },
    }
}

fn dep_tree(app: &mut App, ui: &mut Ui, nodes: &[DepNode], active: Option<&HashSet<String>>, enabled: &HashSet<String>, vdb: Option<&vdb::Vdb>) {
    for n in nodes {
        match n {
            DepNode::Leaf { text, atom } => match atom {
                Some(a) => {
                    ui.horizontal(|ui| {
                        let key = a.key();
                        let inst = vdb.is_some_and(|v| v.is_installed(&key));
                        if a.blocker > 0 {
                            ui.label(RichText::new(icon::PROHIBIT).color(RED)).on_hover_text("Blocker: cannot be installed together");
                        } else if inst {
                            ui.label(RichText::new(icon::CHECK_CIRCLE).color(GREEN)).on_hover_text("Installed");
                        } else {
                            ui.label(RichText::new(icon::CIRCLE).color(TEXT_FAINT)).on_hover_text("Not installed");
                        }
                        let r = ui.add(egui::Label::new(RichText::new(text).monospace().color(if a.blocker > 0 { RED } else { ACCENT })).sense(Sense::click()));
                        if r.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                            app.push(Action::Open(key));
                        }
                    });
                }
                None => {
                    ui.label(w::mono(text));
                }
            },
            DepNode::UseCond { flag, negate, children } => {
                let on = enabled.contains(flag) != *negate;
                if active.is_some() && !on {
                    continue;
                }
                let label = format!("{}{flag}?  {}", if *negate { "!" } else { "" }, if on { "(applies)" } else { "(does not apply)" });
                egui::CollapsingHeader::new(RichText::new(label).color(if on { GREEN_TEXT } else { TEXT_FAINT }).monospace())
                    .id_salt(ui.next_auto_id())
                    .default_open(on)
                    .show(ui, |ui| dep_tree(app, ui, children, active, enabled, vdb));
            }
            DepNode::AnyOf(c) => {
                egui::CollapsingHeader::new(RichText::new("any one of ( || )").color(YELLOW))
                    .id_salt(ui.next_auto_id())
                    .default_open(true)
                    .show(ui, |ui| dep_tree(app, ui, c, active, enabled, vdb));
            }
            DepNode::ExactlyOne(c) => {
                egui::CollapsingHeader::new(RichText::new("exactly one of ( ^^ )").color(YELLOW))
                    .id_salt(ui.next_auto_id())
                    .default_open(true)
                    .show(ui, |ui| dep_tree(app, ui, c, active, enabled, vdb));
            }
            DepNode::AtMostOne(c) => {
                egui::CollapsingHeader::new(RichText::new("at most one of ( ?? )").color(YELLOW))
                    .id_salt(ui.next_auto_id())
                    .default_open(true)
                    .show(ui, |ui| dep_tree(app, ui, c, active, enabled, vdb));
            }
            DepNode::All(c) => dep_tree(app, ui, c, active, enabled, vdb),
        }
    }
}

/// Very small bash/ebuild syntax highlighter.
pub fn highlight_bash(text: &str, font: FontId) -> egui::text::LayoutJob {
    use egui::text::{LayoutJob, TextFormat};
    let mut job = LayoutJob::default();
    let fmt = |c: Color32| TextFormat { font_id: font.clone(), color: c, ..Default::default() };
    const KW: &[&str] = &[
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "for",
        "in",
        "do",
        "done",
        "case",
        "esac",
        "while",
        "until",
        "function",
        "local",
        "export",
        "return",
        "inherit",
        "die",
        "use",
        "usex",
        "usev",
        "use_enable",
        "use_with",
        "default",
        "einfo",
        "ewarn",
        "eerror",
        "elog",
    ];
    for line in text.split_inclusive('\n') {
        let trimmed = line.trim_start();
        if trimmed.starts_with('#') {
            job.append(line, 0.0, fmt(Color32::from_rgb(0x6a, 0x99, 0x55)));
            continue;
        }
        // Top-level VAR= assignments
        if let Some(eq) = line.find('=')
            && line[..eq].chars().all(|c| c.is_ascii_uppercase() || c == '_' || c.is_ascii_digit())
            && eq > 0
        {
            job.append(&line[..eq], 0.0, fmt(ACCENT));
            job.append("=", 0.0, fmt(TEXT));
            highlight_rest(&mut job, &line[eq + 1..], &fmt, KW);
            continue;
        }
        if let Some(p) = trimmed.find("() {")
            && !trimmed[..p].contains(' ')
        {
            let lead = &line[..line.len() - trimmed.len()];
            job.append(lead, 0.0, fmt(TEXT));
            job.append(&trimmed[..p], 0.0, fmt(YELLOW));
            job.append(&trimmed[p..], 0.0, fmt(TEXT));
            continue;
        }
        highlight_rest(&mut job, line, &fmt, KW);
    }
    job
}

fn highlight_rest(job: &mut egui::text::LayoutJob, s: &str, fmt: &dyn Fn(Color32) -> egui::text::TextFormat, kw: &[&str]) {
    let chars: Vec<char> = s.chars().collect();
    let mut i = 0;
    let mut word = String::new();
    let flush_word = |job: &mut egui::text::LayoutJob, word: &mut String| {
        if word.is_empty() {
            return;
        }
        let c = if kw.contains(&word.as_str()) { PURPLE } else { TEXT };
        job.append(word, 0.0, fmt(c));
        word.clear();
    };
    while i < chars.len() {
        let c = chars[i];
        if c == '"' || c == '\'' {
            flush_word(job, &mut word);
            let q = c;
            let mut j = i + 1;
            while j < chars.len() && chars[j] != q {
                if chars[j] == '\\' {
                    j += 1;
                }
                j += 1;
            }
            let end = (j + 1).min(chars.len());
            let seg: String = chars[i..end].iter().collect();
            job.append(&seg, 0.0, fmt(ORANGE));
            i = end;
        } else if c == '$' {
            flush_word(job, &mut word);
            let mut j = i + 1;
            if j < chars.len() && chars[j] == '{' {
                while j < chars.len() && chars[j] != '}' {
                    j += 1;
                }
                j += 1;
            } else {
                while j < chars.len() && (chars[j].is_ascii_alphanumeric() || chars[j] == '_') {
                    j += 1;
                }
            }
            let end = j.min(chars.len());
            let seg: String = chars[i..end].iter().collect();
            job.append(&seg, 0.0, fmt(Color32::from_rgb(0x4e, 0xc9, 0xb0)));
            i = end;
        } else if c == '#' && (i == 0 || chars[i - 1].is_whitespace()) {
            flush_word(job, &mut word);
            let seg: String = chars[i..].iter().collect();
            job.append(&seg, 0.0, fmt(Color32::from_rgb(0x6a, 0x99, 0x55)));
            i = chars.len();
        } else if c.is_alphanumeric() || c == '_' || c == '-' {
            word.push(c);
            i += 1;
        } else {
            flush_word(job, &mut word);
            job.append(&c.to_string(), 0.0, fmt(TEXT));
            i += 1;
        }
    }
    flush_word(job, &mut word);
}

fn ebuild(app: &mut App, ui: &mut Ui, d: &PkgDetail) {
    if d.versions.is_empty() {
        return;
    }
    let v = d.versions[app.detail.ver.min(d.versions.len() - 1)].clone();
    ui.horizontal(|ui| {
        ui.label(w::dim("Version"));
        version_picker(app, ui, d);
        ui.label(w::mono(v.ebuild_path.to_string_lossy()));
        if w::button(ui, icon::ARROW_SQUARE_OUT, "Open in editor").clicked() {
            crate::portage::system::open_url(&v.ebuild_path.to_string_lossy());
        }
        if w::button(ui, icon::COPY, "Copy path").clicked() {
            ui.ctx().copy_text(v.ebuild_path.to_string_lossy().to_string());
        }
    });
    app.push(Action::LoadEbuild(v.ebuild_path.clone()));
    match app.ebuilds.get(&v.ebuild_path).cloned() {
        None => {
            ui.add(egui::Spinner::new().color(ACCENT));
        }
        Some(text) => {
            egui::Frame::new().fill(BG_DEEP).inner_margin(Margin::same(12)).corner_radius(4).show(ui, |ui| {
                ui.set_width(ui.available_width());
                let job = highlight_bash(&text, FontId::monospace(13.0));
                ui.add(egui::Label::new(job).selectable(true));
            });
        }
    }
    if !d.files.is_empty() {
        w::section(ui, &format!("files/ ({} patches & helpers)", d.files.len()));
        ui.horizontal_wrapped(|ui| {
            for f in &d.files {
                let path = d.pkg_dir.as_ref().map(|p| p.join("files").join(f));
                if ui.add(egui::Button::new(RichText::new(f).monospace().size(12.0))).clicked()
                    && let Some(p) = path
                {
                    crate::portage::system::open_url(&p.to_string_lossy());
                }
            }
        });
    }
}

fn files(app: &mut App, ui: &mut Ui, installed: &[vdb::Installed]) {
    let Some(inst) = installed.last() else { return };
    let cpv = inst.cpv();
    if app.detail.files.as_ref().is_none_or(|(k, _)| *k != cpv) {
        app.detail.files = Some((cpv.clone(), Arc::new(vdb::contents(&inst.dir))));
    }
    let list = app.detail.files.as_ref().unwrap().1.clone();
    ui.horizontal(|ui| {
        w::search_box(ui, &mut app.detail.files_filter, "Filter files", 320.0);
        let n_files = list.iter().filter(|e| e.kind == 'f').count();
        ui.label(w::dim(format!("{} files, {} entries total", n_files, list.len())));
    });
    let f = app.detail.files_filter.to_lowercase();
    let shown: Vec<&ContentEntry> = list.iter().filter(|e| f.is_empty() || e.path.to_lowercase().contains(&f)).collect();
    egui::Frame::new().fill(BG_DEEP).inner_margin(Margin::same(10)).corner_radius(4).show(ui, |ui| {
        ui.set_width(ui.available_width());
        for e in shown.iter().take(3000) {
            ui.horizontal(|ui| {
                let (ic, c) = match e.kind {
                    'd' => (icon::FOLDER, ACCENT_DIM),
                    'l' => (icon::LINK, PURPLE),
                    _ => (icon::FILE, TEXT_FAINT),
                };
                ui.label(RichText::new(ic).color(c));
                ui.label(w::mono(&e.path));
            });
        }
        if shown.len() > 3000 {
            ui.label(w::dim(format!("… {} more (use the filter)", shown.len() - 3000)));
        }
    });
}

fn history(app: &mut App, ui: &mut Ui, key: &str) {
    let Some(h) = app.data.history.clone() else { return };
    let recs = h.records_for(key);
    if recs.is_empty() {
        w::empty_state(ui, icon::CLOCK, "No history", "This package was never merged on this machine (according to emerge.log).");
        return;
    }
    if let Some(est) = h.estimate(key) {
        ui.label(w::strong(format!("Expected build time: {}", fmt_duration(est))));
    }
    let max = recs.iter().map(|r| r.duration().as_secs_f32()).fold(1.0, f32::max);
    egui::Grid::new("hist-pkg").num_columns(5).striped(true).spacing([16.0, 6.0]).show(ui, |ui| {
        for r in recs.iter().rev() {
            ui.label(crate::fmt::date(r.start));
            ui.label(RichText::new(&r.ver).color(WHITE));
            if r.ok {
                w::status_badge(ui, if r.binary { "BINARY" } else { "DONE" });
            } else {
                w::status_badge(ui, "FAILED");
            }
            ui.label(fmt_duration(r.duration()));
            let (rect, _) = ui.allocate_exact_size(egui::vec2(200.0, 10.0), Sense::hover());
            ui.painter().rect_filled(rect, 2.0, Color32::from_rgb(0x0e, 0x14, 0x1b));
            let fill = egui::Rect::from_min_size(rect.min, egui::vec2(rect.width() * r.duration().as_secs_f32() / max, rect.height()));
            ui.painter().rect_filled(fill, 2.0, if r.ok { ACCENT_DIM } else { RED });
            ui.end_row();
        }
    });
}

fn my_settings(app: &mut App, ui: &mut Ui, d: &PkgDetail) {
    let Some(cfg) = app.data.config.clone() else { return };
    let entries = cfg.entries_for(&d.cat, &d.pn);
    common::tip(app, ui, "Every line in /etc/portage that affects this package. Switch a line off to comment it out (it stays in the file), or delete it.");
    if entries.is_empty() {
        ui.label(w::dim("No package.* entries match this package yet."));
    }
    for (kind, path, idx, e) in &entries {
        w::card().show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                let mut on = !e.disabled;
                if w::toggle(ui, &mut on).on_hover_text(if on { "Active — click to comment out" } else { "Commented out — click to enable" }).changed() {
                    let mut ne = e.clone();
                    ne.disabled = !on;
                    let ch = edits::replace_entry(&cfg, path, *idx, Some(ne));
                    app.push(Action::Write {
                        title: format!("{} {}", if on { "Enable" } else { "Disable" }, e.atom_text),
                        changes: vec![ch],
                        reload: vec![Reload::Config],
                        then: None,
                    });
                }
                ui.label(RichText::new(kind.icon()).color(ACCENT));
                ui.vertical(|ui| {
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(kind.file_name()).size(11.5).family(theme::bold()).color(TEXT_DIM));
                        ui.label(RichText::new(path.to_string_lossy()).size(11.0).color(TEXT_FAINT));
                    });
                    ui.label(RichText::new(e.render().trim_start_matches('#')).monospace().color(if e.disabled { TEXT_FAINT } else { WHITE }));
                    if app.settings.beginner_tips {
                        ui.label(w::dim(e.describe(*kind)));
                    }
                });
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if w::icon_button(ui, icon::TRASH, "Delete this line").clicked() {
                        let ch = edits::replace_entry(&cfg, path, *idx, None);
                        app.push(Action::Write { title: format!("Delete {}", e.atom_text), changes: vec![ch], reload: vec![Reload::Config], then: None });
                    }
                });
            });
        });
        ui.add_space(3.0);
    }

    w::section(ui, "Add a setting for this package");
    let kinds = [ConfKind::AcceptKeywords, ConfKind::Mask, ConfKind::Unmask, ConfKind::License, ConfKind::Env, ConfKind::Use];
    if app.detail.quick_atom.is_empty() {
        app.detail.quick_atom = d.key.clone();
    }
    ui.horizontal_wrapped(|ui| {
        for k in kinds {
            if w::chip(ui, &format!("{} {}", k.icon(), k.title()), app.detail.quick_kind == Some(k)).clicked() {
                app.detail.quick_kind = Some(k);
                app.detail.quick_value = match k {
                    ConfKind::AcceptKeywords => format!("~{}", app.data.arch()),
                    ConfKind::License => d
                        .versions
                        .first()
                        .map(|v| {
                            v.meta.get("LICENSE").replace(['(', ')', '|'], " ").split_whitespace().filter(|t| !t.ends_with('?')).collect::<Vec<_>>().join(" ")
                        })
                        .unwrap_or_default(),
                    _ => String::new(),
                };
            }
        }
    });
    let Some(kind) = app.detail.quick_kind else { return };
    w::card().show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.label(w::dim(kind.explain()));
        ui.add_space(4.0);
        egui::Grid::new("quick-add").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
            ui.label("Applies to");
            ui.horizontal(|ui| {
                egui::ComboBox::from_id_salt("qa-atom").selected_text(app.detail.quick_atom.clone()).width(320.0).show_ui(ui, |ui| {
                    ui.selectable_value(&mut app.detail.quick_atom, d.key.clone(), format!("{} (all versions)", d.key));
                    for v in d.versions.iter().take(30) {
                        let cpv = v.cpv(&d.key);
                        ui.selectable_value(&mut app.detail.quick_atom, format!("={cpv}"), format!("={cpv}"));
                        ui.selectable_value(&mut app.detail.quick_atom, format!(">={cpv}"), format!(">={cpv} (and newer)"));
                    }
                    for s in d.slots() {
                        ui.selectable_value(&mut app.detail.quick_atom, format!("{}:{s}", d.key), format!("{}:{s} (slot)", d.key));
                    }
                });
                ui.add(egui::TextEdit::singleline(&mut app.detail.quick_atom).desired_width(220.0).font(egui::TextStyle::Monospace));
            });
            ui.end_row();
            if kind.has_values() {
                ui.label(kind.values_label());
                ui.vertical(|ui| {
                    ui.add(egui::TextEdit::singleline(&mut app.detail.quick_value).desired_width(420.0).font(egui::TextStyle::Monospace));
                    ui.horizontal_wrapped(|ui| {
                        let mut sugg: Vec<String> = kind.suggestions().iter().map(|s| s.to_string()).collect();
                        if kind == ConfKind::Env {
                            sugg = cfg.env_files.iter().map(|e| e.name.clone()).collect();
                        }
                        for s in sugg {
                            let has = app.detail.quick_value.split_whitespace().any(|x| x == s);
                            if w::chip(ui, &s, has).clicked() {
                                let mut parts: Vec<String> = app.detail.quick_value.split_whitespace().map(String::from).collect();
                                if has {
                                    parts.retain(|x| *x != s);
                                } else {
                                    parts.push(s.clone());
                                }
                                app.detail.quick_value = parts.join(" ");
                            }
                        }
                    });
                });
                ui.end_row();
            }
        });
        let line = if kind.has_values() && !app.detail.quick_value.trim().is_empty() {
            format!("{} {}", app.detail.quick_atom.trim(), app.detail.quick_value.trim())
        } else {
            app.detail.quick_atom.trim().to_string()
        };
        let entry = Entry::new(app.detail.quick_atom.trim(), app.detail.quick_value.split_whitespace().map(String::from).collect());
        ui.horizontal(|ui| {
            ui.label(w::mono(&line));
            if entry.atom.is_none() {
                ui.label(RichText::new(format!("{} not a valid atom", icon::WARNING)).color(RED));
            } else {
                ui.label(w::dim(format!("→ {}", entry.describe(kind))));
            }
        });
        let target = edits::target_path(&cfg, kind, &app.settings.target_file);
        ui.label(w::dim(format!("Will be written to {}", target.display())));
        if ui
            .add_enabled(
                entry.atom.is_some(),
                egui::Button::new(RichText::new(format!("{}  Add", icon::PLUS)).color(WHITE)).fill(Color32::from_rgb(0x1f, 0x6f, 0xb2)),
            )
            .clicked()
        {
            let ch = edits::add_lines(&cfg, &[(kind, line.clone())], &app.settings.target_file, None);
            app.push(Action::Write { title: format!("{}: {line}", kind.file_name()), changes: ch, reload: vec![Reload::Config], then: None });
        }
    });
}
