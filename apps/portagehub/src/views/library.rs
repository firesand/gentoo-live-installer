//! The "Library": installed packages, Steam-library style (list left, page right).

use super::common;
use crate::app::{Action, App};
use crate::data::UpdateCandidate;
use crate::portage::catalog::Catalog;
use crate::portage::emerge::{self, EmergeOptions, Value};
use crate::portage::history::fmt_duration;
use crate::portage::vdb::Vdb;
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Color32, Margin, RichText, Ui};
use egui_phosphor::regular as icon;
use std::collections::HashSet;
use std::hash::{Hash, Hasher};
use std::sync::Arc;

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
    /// The rows for the current query, filter, collapsed categories and data.
    cache: Option<(RowKey, Arc<Built>)>,
}

/// Which icon an installed package gets.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mark {
    Update,
    World,
    Plain,
}

#[derive(Debug)]
enum Row {
    Header(String, usize),
    Item { key: String, pn: String, mark: Mark },
}

#[derive(Debug)]
struct Built {
    rows: Vec<Row>,
    /// Distinct installed packages (the count under the filter box).
    packages: usize,
    /// Packages with a pending update (the filter's label).
    updates: usize,
}

/// Everything the list depends on. While it is unchanged, so are the rows: rebuilding 1600 rows
/// (and, for the World filter, looking each up in the world file) every frame cost ~15 ms.
#[derive(Debug, Clone, PartialEq, Eq)]
struct RowKey {
    query: String,
    filter: Filter,
    collapsed: u64,
    /// The data is replaced, not edited, so its address says whether it changed.
    vdb: usize,
    updates: usize,
    catalog: usize,
}

fn address<T>(a: Option<&Arc<T>>) -> usize {
    a.map_or(0, |a| Arc::as_ptr(a) as *const () as usize)
}

/// Order-independent hash of the collapsed categories.
fn collapsed_hash(set: &HashSet<String>) -> u64 {
    set.iter().fold(set.len() as u64, |acc, c| {
        let mut h = std::collections::hash_map::DefaultHasher::new();
        c.hash(&mut h);
        acc ^ h.finish()
    })
}

fn row_key(state: &State, vdb: &Arc<Vdb>, updates: Option<&Arc<Vec<UpdateCandidate>>>, catalog: Option<&Arc<Catalog>>) -> RowKey {
    RowKey { query: state.query.clone(), filter: state.filter, collapsed: collapsed_hash(&state.collapsed), vdb: address(Some(vdb)), updates: address(updates), catalog: address(catalog) }
}

/// The category headers and package rows for a filter and query (only the packages of expanded categories).
fn build_rows(vdb: &Vdb, catalog: Option<&Catalog>, updates: &HashSet<String>, filter: Filter, query: &str, collapsed: &HashSet<String>) -> Built {
    let q = query.to_lowercase();
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
        let keep = match filter {
            Filter::All => true,
            Filter::World => vdb.in_world(&key),
            Filter::Deps => !vdb.in_world(&key),
            Filter::Updates => updates.contains(&key),
            Filter::Orphans => catalog.is_some_and(|c| c.get(&key).is_none()),
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
        if !collapsed.contains(&p.cat) {
            let mark = if updates.contains(&key) {
                Mark::Update
            } else if vdb.in_world(&key) {
                Mark::World
            } else {
                Mark::Plain
            };
            rows.push(Row::Item { key, pn: p.pn.clone(), mark });
        }
    }
    if let Some(Row::Header(_, n)) = rows.get_mut(header_pos) {
        *n = cat_count;
    }
    Built { rows, packages: seen.len(), updates: updates.len() }
}

pub fn show(app: &mut App, ui: &mut Ui) {
    let Some(vdb) = app.data.vdb.clone() else {
        w::loading(ui, "Reading installed packages…");
        return;
    };
    let catalog = app.data.catalog.clone();
    let update_list = app.data.updates.clone();
    // Rebuilt only when something the list depends on changed (see RowKey).
    let key = row_key(&app.library, &vdb, update_list.as_ref(), catalog.as_ref());
    let built = match &app.library.cache {
        Some((k, built)) if *k == key => built.clone(),
        _ => {
            let updates: HashSet<String> = update_list.as_ref().map(|u| u.iter().map(|c| c.key.clone()).collect()).unwrap_or_default();
            let built = Arc::new(build_rows(&vdb, catalog.as_deref(), &updates, app.library.filter, &app.library.query, &app.library.collapsed));
            app.library.cache = Some((key, built.clone()));
            built
        }
    };

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
                let up = format!("Updates {}", if built.updates == 0 { String::new() } else { built.updates.to_string() });
                w::segmented(
                    ui,
                    &mut app.library.filter,
                    &[(Filter::All, "All"), (Filter::World, "World"), (Filter::Deps, "Deps"), (Filter::Updates, up.as_str()), (Filter::Orphans, "Orphans")],
                );
                ui.add_space(4.0);
            });
            ui.horizontal(|ui| {
                ui.add_space(10.0);
                ui.label(w::dim(format!("{} packages", built.packages)));
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
            egui::ScrollArea::vertical().id_salt("lib-list").auto_shrink([false, false]).show_rows(ui, 30.0, built.rows.len(), |ui, range| {
                ui.spacing_mut().item_spacing.y = 0.0;
                for r in &built.rows[range] {
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
                        Row::Item { key, pn, mark } => {
                            let sel = app.library.selected.as_deref() == Some(key.as_str());
                            let ic = match mark {
                                Mark::Update => icon::ARROW_CIRCLE_UP,
                                Mark::World => icon::STAR,
                                Mark::Plain => icon::PACKAGE,
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::portage::vdb::Installed;
    use crate::portage::version::Version;

    fn pkg(cat: &str, pn: &str) -> Installed {
        Installed { cat: cat.into(), pn: pn.into(), ver: Version::parse("1.0").unwrap(), slot: "0".into(), repo: "gentoo".into(), build_time: 0, size: 0, desc: String::new(), dir: Default::default() }
    }

    fn vdb() -> Vdb {
        let mut v = Vdb::default();
        v.pkgs = vec![pkg("app-editors", "emacs"), pkg("app-editors", "vim"), pkg("dev-libs", "openssl"), pkg("dev-libs", "zlib"), pkg("sys-apps", "coreutils")];
        v.set_world(vec!["app-editors/vim".into(), "sys-apps/coreutils".into()]);
        v
    }

    fn items(b: &Built) -> Vec<&str> {
        b.rows.iter().filter_map(|r| if let Row::Item { key, .. } = r { Some(key.as_str()) } else { None }).collect()
    }

    #[test]
    fn filters_and_marks() {
        let v = vdb();
        let updates: HashSet<String> = ["dev-libs/zlib".to_string()].into();
        let none = HashSet::new();
        let all = build_rows(&v, None, &updates, Filter::All, "", &none);
        assert_eq!(items(&all), vec!["app-editors/emacs", "app-editors/vim", "dev-libs/openssl", "dev-libs/zlib", "sys-apps/coreutils"]);
        assert_eq!((all.packages, all.updates), (5, 1));
        let marks: Vec<Mark> = all.rows.iter().filter_map(|r| if let Row::Item { mark, .. } = r { Some(*mark) } else { None }).collect();
        assert_eq!(marks, vec![Mark::Plain, Mark::World, Mark::Plain, Mark::Update, Mark::World]);
        assert_eq!(items(&build_rows(&v, None, &updates, Filter::World, "", &none)), vec!["app-editors/vim", "sys-apps/coreutils"]);
        assert_eq!(items(&build_rows(&v, None, &updates, Filter::Deps, "", &none)), vec!["app-editors/emacs", "dev-libs/openssl", "dev-libs/zlib"]);
        assert_eq!(items(&build_rows(&v, None, &updates, Filter::Updates, "", &none)), vec!["dev-libs/zlib"]);
        let q = build_rows(&v, None, &updates, Filter::All, "SSL", &none);
        assert_eq!((items(&q), q.packages), (vec!["dev-libs/openssl"], 5), "the count is of installed packages, not of matches");
        // With an empty catalog nothing is known to any repository.
        let orphans = build_rows(&v, Some(&Catalog::default()), &updates, Filter::Orphans, "", &none);
        assert_eq!(orphans.packages, 5);
    }

    #[test]
    fn collapsed_categories_keep_their_header_and_count() {
        let v = vdb();
        let collapsed: HashSet<String> = ["app-editors".to_string()].into();
        let b = build_rows(&v, None, &HashSet::new(), Filter::All, "", &collapsed);
        assert!(matches!(&b.rows[0], Row::Header(c, 2) if c == "app-editors"), "{:?}", b.rows[0]);
        assert_eq!(items(&b), vec!["dev-libs/openssl", "dev-libs/zlib", "sys-apps/coreutils"]);
        assert_eq!(b.packages, 5);
    }

    #[test]
    fn the_cache_key_changes_exactly_when_the_rows_would() {
        let v = Arc::new(vdb());
        let base = State::default();
        let k0 = row_key(&base, &v, None, None);
        assert_eq!(k0, row_key(&base, &v, None, None));
        let mut s = State::default();
        s.query = "vim".into();
        assert_ne!(k0, row_key(&s, &v, None, None));
        let mut s = State::default();
        s.filter = Filter::World;
        assert_ne!(k0, row_key(&s, &v, None, None));
        let mut s = State::default();
        s.collapsed.insert("dev-libs".into());
        let k1 = row_key(&s, &v, None, None);
        assert_ne!(k0, k1);
        // The same set in another insertion order is the same key.
        let (mut a, mut b) = (State::default(), State::default());
        a.collapsed.extend(["x/a".to_string(), "x/b".to_string()]);
        b.collapsed.extend(["x/b".to_string(), "x/a".to_string()]);
        assert_eq!(row_key(&a, &v, None, None), row_key(&b, &v, None, None));
        // New data (a reload) is a new Arc.
        let reloaded = Arc::new(vdb());
        assert_ne!(k0, row_key(&base, &reloaded, None, None));
        let catalog = Arc::new(Catalog::default());
        assert_ne!(k0, row_key(&base, &v, None, Some(&catalog)));
        let updates = Arc::new(Vec::new());
        assert_ne!(k0, row_key(&base, &v, Some(&updates), None));
    }
}
