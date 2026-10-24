//! The "Store": discover packages — home, categories and search.

use super::common::{self, cover, package_row, page, subnav};
use crate::app::{Action, App, Tab};
use crate::data::Reload;
use crate::portage::emerge::{self, EmergeOptions};
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Color32, CornerRadius, FontId, Margin, RichText, Sense, Ui};
use egui_phosphor::regular as icon;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Page {
    #[default]
    Home,
    Categories,
    Search,
}

#[derive(Default)]
pub struct State {
    pub page: Page,
    pub query: String,
    pub show_detail: bool,
    pub category: Option<String>,
    pub in_desc: bool,
    pub only_installed: bool,
    pub repo_filter: Option<String>,
    cache_key: String,
    results: Vec<usize>,
}

pub fn show(app: &mut App, ui: &mut Ui) {
    if app.store.show_detail {
        super::details::show(app, ui, true);
        return;
    }
    let mut page_sel = app.store.page;
    let mut query = app.store.query.clone();
    let mut typed = false;
    subnav(ui, &mut page_sel, &[(Page::Home, "Home"), (Page::Categories, "Categories"), (Page::Search, "Search")], |ui| {
        let r = w::search_box(ui, &mut query, "Search packages (name or description)…", 340.0);
        typed = r.changed() && !query.is_empty();
    });
    if typed {
        page_sel = Page::Search;
    }
    app.store.page = page_sel;
    app.store.query = query;

    if app.data.catalog.is_none() {
        w::loading(ui, "Reading the package tree…");
        return;
    }
    match app.store.page {
        Page::Home => home(app, ui),
        Page::Categories => categories(app, ui),
        Page::Search => search(app, ui),
    }
}

fn hero(app: &mut App, ui: &mut Ui) {
    let width = ui.available_width();
    let height = 190.0;
    let (rect, _) = ui.allocate_exact_size(egui::vec2(width, height), Sense::hover());
    let p = ui.painter();
    w::paint_hgradient(p, rect, Color32::from_rgb(0x1e, 0x4f, 0x78), Color32::from_rgb(0x2b, 0x2a, 0x5a));
    w::paint_vgradient(p, rect, Color32::from_black_alpha(0), Color32::from_black_alpha(90));
    p.text(rect.right_top() + egui::vec2(-60.0, 95.0), egui::Align2::CENTER_CENTER, icon::CUBE, FontId::proportional(150.0), Color32::from_white_alpha(18));
    let inner = rect.shrink2(egui::vec2(28.0, 22.0));
    let mut child = ui.new_child(egui::UiBuilder::new().max_rect(inner).layout(egui::Layout::top_down(egui::Align::Min)));
    let ui = &mut child;
    ui.label(RichText::new("WELCOME BACK").size(12.0).family(theme::bold()).color(ACCENT));
    ui.label(RichText::new("Your Gentoo system").size(30.0).family(theme::bold()).color(WHITE));
    let mut facts = Vec::new();
    if let Some(v) = &app.data.vdb {
        facts.push(format!("{} packages installed", v.pkgs.len()));
        facts.push(format!("{} in @world", v.world_raw.len()));
    }
    if let Some((p, _)) = &app.world_plan {
        facts.push(format!("@world update: {} changes", p.merge_entries().count()));
    } else if let Some(u) = &app.data.updates {
        facts.push(format!("{} newer versions", u.len()));
    }
    if let Some(ts) = app.data.last_sync() {
        facts.push(format!("last sync {}", crate::fmt::ago(ts)));
    }
    ui.label(RichText::new(facts.join("  ·  ")).color(TEXT));
    ui.add_space(14.0);
    ui.horizontal(|ui| {
        if w::big_button(ui, &format!("{}  UPDATE SYSTEM", icon::ROCKET_LAUNCH), GRAD_GREEN, 200.0).on_hover_text("Preview emerge -uDN @world").clicked() {
            app.push(Action::Pretend((emerge::recipes()[0].build)()));
        }
        ui.add_space(6.0);
        if w::big_button(ui, &format!("{}  SYNC REPOSITORIES", icon::ARROWS_CLOCKWISE), GRAD_BLUE, 200.0)
            .on_hover_text("emerge --sync: download the latest ebuilds for all auto-sync repositories")
            .clicked()
        {
            app.push(Action::Emerge(EmergeOptions { action: emerge::Action::Sync, ..Default::default() }, vec![]));
        }
    });
}

fn home(app: &mut App, ui: &mut Ui) {
    page(ui, "store-home", |ui| {
        hero(app, ui);
        ui.add_space(12.0);
        kernel_banner(app, ui);
        common::tip(
            app,
            ui,
            "New to Gentoo? Packages are built from source by Portage. Browse here, open a package to see its versions and USE flags (optional features), then press INSTALL — PortageHub always shows you a preview of what emerge will do before anything changes.",
        );

        updates_section(app, ui);

        // News
        if let Some(news) = app.data.news.clone() {
            let unread: Vec<_> = news.iter().filter(|n| n.unread).collect();
            if !unread.is_empty() {
                w::section(ui, &format!("Unread news ({})", unread.len()));
                for n in unread.iter().take(4) {
                    w::card_tinted(ORANGE).show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        ui.horizontal(|ui| {
                            ui.label(RichText::new(icon::NEWSPAPER).size(20.0).color(ORANGE));
                            ui.vertical(|ui| {
                                ui.label(w::strong(&n.title));
                                ui.label(w::dim(format!("{} · {}", n.posted, n.name)));
                            });
                            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                if w::button(ui, icon::BOOK_OPEN, "Read").clicked() {
                                    app.config_view.open_news(&n.name);
                                    app.push(Action::Tab(Tab::Config));
                                }
                            });
                        });
                    });
                    ui.add_space(4.0);
                }
            }
        }

        // Category tiles
        w::section(ui, "Browse by category");
        let cats: Vec<(String, usize)> = app
            .data
            .catalog
            .as_ref()
            .map(|c| {
                let mut v: Vec<(String, usize)> = c.categories.iter().map(|(k, v)| (k.clone(), v.len())).collect();
                v.sort_by_key(|x| std::cmp::Reverse(x.1));
                v.into_iter().take(18).collect()
            })
            .unwrap_or_default();
        category_grid(app, ui, &cats);

        // Recently merged
        if let Some(h) = app.data.history.clone() {
            w::section(ui, "Recently installed or updated");
            let recent: Vec<_> = h.merges.iter().rev().filter(|m| m.ok).take(8).collect();
            for m in recent {
                let cat = m.key.split('/').next().unwrap_or("");
                let pn = m.key.split('/').nth(1).unwrap_or("");
                let r = package_row(
                    ui,
                    cat,
                    pn,
                    &format!("{} · built in {}", crate::fmt::ago(m.end), crate::portage::history::fmt_duration(m.duration())),
                    &m.ver,
                    if m.binary { "binary" } else { "source" },
                    None,
                    false,
                );
                if r.clicked() {
                    app.push(Action::Open(m.key.clone()));
                }
            }
        }
    });
}

/// Kernel/boot/NVIDIA problems worth seeing on the home page.
fn kernel_banner(app: &mut App, ui: &mut Ui) {
    use crate::portage::kernel::{Severity, check};
    let Some(ks) = app.data.kernels.clone() else { return };
    let scan = app.boot_scan.as_ref().map(|(s, _)| s.clone());
    let findings = check(&ks, scan.as_deref(), app.settings.kernel_protect, 2, app.settings.nvidia_alerts);
    let important: Vec<_> = findings.iter().filter(|f| f.severity >= Severity::Warn).collect();
    if important.is_empty() {
        return;
    }
    let worst = important.iter().map(|f| f.severity).max().unwrap_or(Severity::Warn);
    let tint = if worst == Severity::Error { RED } else { ORANGE };
    w::card_tinted(tint).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            ui.label(RichText::new(icon::CPU).size(24.0).color(tint));
            ui.vertical(|ui| {
                ui.label(w::strong(format!("Kernels & boot: {}", important[0].title)));
                if important.len() > 1 {
                    ui.label(w::dim(format!(
                        "+{} more: {}",
                        important.len() - 1,
                        important[1..].iter().map(|f| f.title.as_str()).collect::<Vec<_>>().join(" · ")
                    )));
                } else {
                    ui.label(w::dim(&important[0].detail));
                }
            });
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if w::primary(ui, icon::ARROW_RIGHT, "Open Kernels & Boot").clicked() {
                    app.maintenance.page = crate::views::maintenance::Page::Kernels;
                    app.push(Action::Tab(Tab::Maintenance));
                }
            });
        });
    });
    ui.add_space(8.0);
}

fn updates_section(app: &mut App, ui: &mut Ui) {
    use crate::data::{UpdState, classify};
    use crate::portage::pretend::{EntryType, Problem};
    let updates = app.data.updates.clone();
    let plan = app.world_plan.clone();
    let checking = app.world_job.and_then(|id| app.job(id)).filter(|j| j.running()).map(|j| j.started.elapsed().as_secs());
    w::section(ui, &format!("Updates{}", updates.as_ref().map(|u| format!(" ({} newer versions installed)", u.len())).unwrap_or_default()));

    // What emerge itself says.
    let tint = match &plan {
        Some((p, _)) if p.problems.iter().any(|x| x.is_blocking()) || p.exit_code.is_some_and(|c| c != 0) => ORANGE,
        Some(_) => GREEN,
        None => ACCENT,
    };
    w::card_tinted(tint).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            match (&plan, checking) {
                (_, Some(secs)) => {
                    ui.add(egui::Spinner::new().color(ACCENT));
                    ui.label(w::strong("Checking with emerge -uDN @world…"));
                    ui.label(w::dim(format!("{secs}s · runs in the background as your user, changes nothing")));
                }
                (Some((p, ts)), None) => {
                    let ups = p.merge_entries().filter(|e| matches!(e.status_label(), "UPDATE" | "DOWNGRADE" | "NEW SLOT")).count();
                    let rebuilds = p.merge_entries().filter(|e| e.status_label() == "REBUILD").count();
                    let new = p.merge_entries().filter(|e| e.status_label() == "NEW").count();
                    let skipped = p.problems.iter().filter(|x| matches!(x, Problem::Skipped { .. })).count();
                    let mut parts = vec![format!("{ups} upgrades"), format!("{rebuilds} rebuilds")];
                    if new > 0 {
                        parts.push(format!("{new} new dependencies"));
                    }
                    if skipped > 0 {
                        parts.push(format!("{skipped} skipped"));
                    }
                    ui.label(RichText::new(icon::CHECK_CIRCLE).size(18.0).color(tint));
                    ui.label(w::strong(format!("emerge -uDN @world: {}", parts.join(", "))));
                    ui.label(w::dim(format!("checked {}", crate::fmt::ago(*ts))));
                    if p.problems.iter().any(|x| x.is_blocking()) {
                        ui.label(RichText::new(format!("{} emerge reports problems — open the preview", icon::WARNING)).color(ORANGE));
                    }
                }
                (None, None) => {
                    ui.label(RichText::new(icon::INFO).size(18.0).color(ACCENT));
                    ui.label(w::strong("Quick estimate only"));
                    ui.label(w::dim("not yet confirmed with emerge"));
                }
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if w::primary(ui, icon::ROCKET_LAUNCH, "Review & update").on_hover_text("Open the full preview of emerge -uDN @world").clicked() {
                    app.push(Action::Pretend((emerge::recipes()[0].build)()));
                }
                if checking.is_none() && w::button(ui, icon::ARROWS_CLOCKWISE, if plan.is_some() { "Check again" } else { "Check with emerge" }).clicked() {
                    app.push(Action::CheckWorld);
                }
            });
        });
    });
    ui.add_space(4.0);

    let plan_ref = plan.as_ref().map(|(p, _)| p.as_ref());
    match &updates {
        None => {
            ui.horizontal(|ui| {
                ui.add(egui::Spinner::new().color(ACCENT));
                ui.label(w::dim("Comparing installed packages with the tree…"));
            });
        }
        Some(u) => {
            let mut cards: Vec<(crate::data::UpdateCandidate, UpdState)> = u.iter().map(|c| (c.clone(), classify(c, plan_ref))).collect();
            // Upgrades emerge plans that the quick estimate did not list.
            if let Some(p) = plan_ref {
                for e in p.merge_entries().filter(|e| matches!(e.status_label(), "UPDATE" | "DOWNGRADE" | "NEW SLOT")) {
                    if !cards.iter().any(|(c, _)| c.key == e.key) {
                        let old = e.old.first().map(|o| o.split([':']).next().unwrap_or("").to_string()).unwrap_or_default();
                        cards.push((
                            crate::data::UpdateCandidate {
                                key: e.key.clone(),
                                installed: old,
                                available: e.version.clone(),
                                repo: e.repo.clone(),
                                slot: e.slot.clone(),
                                held_by: vec![],
                            },
                            UpdState::WillUpdate,
                        ));
                    }
                }
            }
            let rank = |s: &UpdState| match s {
                UpdState::WillUpdate => 0,
                UpdState::Unknown => 1,
                UpdState::Skipped(_) => 2,
                UpdState::HeldBack(_) => 3,
                UpdState::NotNeeded => 4,
            };
            cards.sort_by(|a, b| rank(&a.1).cmp(&rank(&b.1)).then(a.0.key.cmp(&b.0.key)));
            if cards.is_empty() {
                ui.label(w::dim(format!("{} Everything installed is at the newest version you allow.", icon::CHECK_CIRCLE)));
            } else {
                // One list, top to bottom, grouped by what emerge does with each package.
                type Group = (&'static str, fn(&UpdState) -> bool);
                let groups: [Group; 5] = [
                    ("In the @world update", |s| matches!(s, UpdState::WillUpdate)),
                    ("Not checked with emerge yet", |s| matches!(s, UpdState::Unknown)),
                    ("Skipped by emerge", |s| matches!(s, UpdState::Skipped(_))),
                    ("Held back by another package or set", |s| matches!(s, UpdState::HeldBack(_))),
                    ("Not needed by @world (depclean would remove)", |s| matches!(s, UpdState::NotNeeded)),
                ];
                for (title, pred) in groups {
                    let rows: Vec<&(crate::data::UpdateCandidate, UpdState)> = cards.iter().filter(|(_, st)| pred(st)).collect();
                    if rows.is_empty() {
                        continue;
                    }
                    ui.add_space(4.0);
                    ui.label(RichText::new(format!("{} ({})", title.to_uppercase(), rows.len())).size(11.5).family(theme::bold()).color(TEXT_DIM));
                    for (c, st) in rows {
                        update_row(app, ui, c, st);
                    }
                }
            }
            // Rebuilds and new dependencies from emerge's plan.
            if let Some(p) = plan_ref {
                let others: Vec<_> = p.merge_entries().filter(|e| matches!(e.status_label(), "REBUILD" | "NEW")).collect();
                if !others.is_empty() {
                    ui.add_space(4.0);
                    ui.horizontal_wrapped(|ui| {
                        ui.label(w::dim("Also in the @world update:"));
                        for e in others {
                            let reason = rebuild_reason(e);
                            let label = format!(
                                "{} {}",
                                if e.status_label() == "NEW" { icon::PLUS } else { icon::ARROWS_CLOCKWISE },
                                e.key.rsplit('/').next().unwrap_or(&e.key)
                            );
                            let r = w::chip(ui, &label, false).on_hover_text(format!("{} — {}", e.key, reason));
                            if r.clicked() {
                                app.push(Action::Open(e.key.clone()));
                            }
                        }
                    });
                    let _ = EntryType::Ebuild;
                }
            }
            egui::CollapsingHeader::new(RichText::new(format!("{}  Why can this differ from what emerge -uDN @world shows?", icon::QUESTION)).color(TEXT_DIM))
                .id_salt("upd-why")
                .show(ui, |ui| {
                    for (head, text) in [
                        ("IN @WORLD UPDATE", "emerge will upgrade it as part of the world update."),
                        ("HELD BACK", "A newer version exists, but an installed package (or one of your sets) only accepts the installed one — e.g. tigervnc needs fltk < 1.4. It updates once that package allows it."),
                        ("SKIPPED", "emerge tried, found a conflict and skipped it (it says so under “WARNING: One or more updates/rebuilds have been skipped”)."),
                        ("NOT IN @WORLD", "Nothing you selected depends on it any more, so a world update ignores it. emerge --depclean would remove it; update it from its own page if you still use it."),
                        ("Rebuilds", "emerge also reinstalls the same version when USE flags changed (--newuse), a library it links to changed its sub-slot, or a better repository now provides it. Those are listed above as “Also in the @world update”."),
                    ] {
                        ui.horizontal_wrapped(|ui| {
                            ui.label(w::strong(head));
                            ui.label(w::dim(text));
                        });
                    }
                });
        }
    }
}

fn rebuild_reason(e: &crate::portage::pretend::PlanEntry) -> String {
    if e.status_label() == "NEW" {
        return "new dependency".into();
    }
    if let Some(old_repo) = e.old.first().and_then(|o| o.split("::").nth(1))
        && !e.repo.is_empty()
        && old_repo != e.repo
    {
        return format!("moves from ::{old_repo} to ::{}", e.repo);
    }
    let changed: Vec<String> = e.use_changes().into_iter().filter(|(_, _, ch)| *ch).map(|(f, on, _)| if on { f } else { format!("-{f}") }).collect();
    if !changed.is_empty() {
        return format!("USE flags changed: {}", changed.join(" "));
    }
    "rebuild because a library it uses changed (slot/sub-slot)".into()
}

/// One update as a full-width row (Steam download-queue style).
fn update_row(app: &mut App, ui: &mut Ui, c: &crate::data::UpdateCandidate, st: &crate::data::UpdState) {
    use crate::data::UpdState;
    let key = c.key.as_str();
    let (cat, pn) = key.split_once('/').unwrap_or(("", key));
    let h = 50.0;
    let width = ui.available_width();
    let (rect, resp) = ui.allocate_exact_size(egui::vec2(width, h), Sense::click());
    if !ui.is_rect_visible(rect) {
        return;
    }
    let dimmed = matches!(st, UpdState::NotNeeded | UpdState::HeldBack(_) | UpdState::Skipped(_));
    let p = ui.painter();
    p.rect_filled(rect.shrink2(egui::vec2(0.0, 1.5)), CornerRadius::same(2), if resp.hovered() { CARD_HOVER } else { CARD });
    let tile = egui::Rect::from_min_size(rect.min + egui::vec2(6.0, 6.0), egui::vec2(h - 12.0, h - 12.0));
    let base = common::category_color(cat);
    let base = if dimmed { w::lerp_color(base, Color32::from_rgb(0x30, 0x38, 0x44), 0.6) } else { base };
    w::paint_vgradient(p, tile, w::lerp_color(base, WHITE, 0.12), w::lerp_color(base, Color32::BLACK, 0.35));
    p.text(
        tile.center(),
        egui::Align2::CENTER_CENTER,
        common::category_icon(cat),
        FontId::proportional(18.0),
        Color32::from_white_alpha(if dimmed { 120 } else { 220 }),
    );
    let x = tile.right() + 12.0;
    let mut job = egui::text::LayoutJob::default();
    job.append(pn, 0.0, egui::text::TextFormat { font_id: FontId::new(14.5, theme::medium()), color: if dimmed { TEXT } else { WHITE }, ..Default::default() });
    job.append(&format!("   {cat}"), 0.0, egui::text::TextFormat { font_id: FontId::proportional(12.0), color: TEXT_FAINT, ..Default::default() });
    job.wrap = egui::text::TextWrapping::truncate_at_width(width * 0.35);
    p.galley(egui::pos2(x, rect.top() + 7.0), ui.painter().layout_job(job), WHITE);
    let ver = format!("{}  {}  {}   ::{}", c.installed, icon::ARROW_RIGHT, c.available, c.repo);
    p.text(egui::pos2(x, rect.top() + 29.0), egui::Align2::LEFT_TOP, ver, FontId::proportional(12.5), if dimmed { TEXT_DIM } else { GREEN_TEXT });
    // Reason in the middle-right, badge on the right.
    let reason = match st {
        UpdState::HeldBack(why) | UpdState::Skipped(why) => why.clone(),
        UpdState::NotNeeded => "nothing in @world depends on it".to_string(),
        _ => String::new(),
    };
    if !reason.is_empty() {
        let mut rj = egui::text::LayoutJob::simple_singleline(reason, FontId::proportional(12.0), TEXT_DIM);
        rj.wrap = egui::text::TextWrapping::truncate_at_width(width * 0.38);
        let g = ui.painter().layout_job(rj);
        p.galley(egui::pos2(rect.left() + width * 0.44, rect.center().y - g.size().y / 2.0), g, TEXT_DIM);
    }
    let label = st.label();
    if !label.is_empty() {
        let (fg, bg) = match st {
            UpdState::WillUpdate => (GREEN_TEXT, GREEN_DARK),
            UpdState::HeldBack(_) => (Color32::from_rgb(0x2b, 0x1a, 0x00), ORANGE),
            UpdState::Skipped(_) => (WHITE, Color32::from_rgb(0xa3, 0x2e, 0x2e)),
            _ => (TEXT, Color32::from_rgb(0x3a, 0x46, 0x55)),
        };
        let g = ui.painter().layout_no_wrap(label.to_string(), FontId::new(10.5, theme::bold()), fg);
        let br = egui::Rect::from_min_size(
            egui::pos2(rect.right() - 14.0 - g.size().x - 10.0, rect.center().y - (g.size().y + 6.0) / 2.0),
            g.size() + egui::vec2(10.0, 6.0),
        );
        p.rect_filled(br, CornerRadius::same(2), bg);
        p.galley(br.min + egui::vec2(5.0, 3.0), g, fg);
    }
    let resp = resp.on_hover_ui(|ui| {
        ui.set_max_width(380.0);
        ui.label(w::strong(format!("{key}  {} → {}", c.installed, c.available)));
        ui.label(st.explain());
        ui.label(w::dim("Click to open · right-click for actions"));
    });
    if resp.clicked() {
        app.push(Action::Open(key.to_string()));
    }
    resp.context_menu(|ui| common::package_menu(app, ui, key, true));
}

fn category_grid(app: &mut App, ui: &mut Ui, cats: &[(String, usize)]) {
    let tile = egui::vec2(200.0, 74.0);
    let per_row = ((ui.available_width() + 8.0) / (tile.x + 8.0)).floor().max(1.0) as usize;
    for row in cats.chunks(per_row) {
        ui.horizontal_top(|ui| {
            for (c, n) in row {
                let (rect, resp) = ui.allocate_exact_size(tile, Sense::click());
                let base = common::category_color(c);
                let p = ui.painter();
                let (a, b) = if resp.hovered() {
                    (w::lerp_color(base, WHITE, 0.25), base)
                } else {
                    (w::lerp_color(base, WHITE, 0.08), w::lerp_color(base, Color32::BLACK, 0.4))
                };
                w::paint_hgradient(p, rect, a, b);
                p.text(
                    rect.left_center() + egui::vec2(14.0, 0.0),
                    egui::Align2::LEFT_CENTER,
                    common::category_icon(c),
                    FontId::proportional(26.0),
                    Color32::from_white_alpha(230),
                );
                p.text(rect.left_top() + egui::vec2(52.0, 18.0), egui::Align2::LEFT_TOP, c, FontId::new(14.0, theme::medium()), WHITE);
                p.text(
                    rect.left_top() + egui::vec2(52.0, 40.0),
                    egui::Align2::LEFT_TOP,
                    format!("{n} packages"),
                    FontId::proportional(12.0),
                    Color32::from_white_alpha(170),
                );
                if resp.clicked() {
                    app.store.category = Some(c.clone());
                    app.store.page = Page::Categories;
                }
            }
        });
        ui.add_space(4.0);
    }
}

fn categories(app: &mut App, ui: &mut Ui) {
    let Some(cat) = app.data.catalog.clone() else { return };
    if let Some(c) = app.store.category.clone() {
        let idx = cat.categories.get(&c).cloned().unwrap_or_default();
        let vdb = app.data.vdb.clone();
        egui::Frame::new().inner_margin(Margin::symmetric(24, 10)).show(ui, |ui| {
            ui.horizontal(|ui| {
                if w::button(ui, icon::ARROW_LEFT, "All categories").clicked() {
                    app.store.category = None;
                }
                cover(ui, &c, 34.0);
                ui.label(RichText::new(&c).size(22.0).family(theme::bold()).color(WHITE));
                ui.label(w::dim(format!("{} packages", idx.len())));
            });
            if let Some(repo_loc) = cat.repo_location("gentoo")
                && let Some(d) = crate::portage::catalog::category_description(repo_loc, &c)
            {
                ui.label(w::dim(d));
            }
        });
        let row_h = 54.0;
        egui::ScrollArea::vertical().id_salt("cat-list").auto_shrink([false, false]).show_rows(ui, row_h, idx.len(), |ui, range| {
            egui::Frame::new().inner_margin(Margin::symmetric(24, 0)).show(ui, |ui| {
                for i in range {
                    let p = &cat.pkgs[idx[i]];
                    let key = p.key();
                    let inst = vdb.as_ref().map(|v| v.installed(&key)).unwrap_or_default();
                    let inst_v = inst.last().map(|i| i.ver.raw.clone());
                    let latest = p.latest().map(|e| e.ver.raw.clone()).unwrap_or_default();
                    let repos: Vec<String> = p.repos().iter().map(|r| r.to_string()).collect();
                    let r = package_row(ui, &p.cat, &p.pn, &p.desc, &latest, &repos.join(" "), inst_v.as_deref(), false);
                    if r.clicked() {
                        app.push(Action::Open(key.clone()));
                    }
                    r.context_menu(|ui| common::package_menu(app, ui, &key, inst_v.is_some()));
                }
            });
        });
        return;
    }
    let mut cats: Vec<(String, usize)> = cat.categories.iter().map(|(k, v)| (k.clone(), v.len())).collect();
    let q = app.store.query.to_lowercase();
    if !q.is_empty() {
        cats.retain(|(c, _)| c.contains(&q));
    }
    page(ui, "cats", |ui| {
        w::title(ui, "Categories");
        w::subtitle(ui, &format!("{} categories across all repositories", cats.len()));
        ui.add_space(8.0);
        // Group by prefix (app-, dev-, media- …)
        let mut groups: std::collections::BTreeMap<String, Vec<(String, usize)>> = Default::default();
        for (c, n) in cats {
            let g = c.split('-').next().unwrap_or(&c).to_string();
            groups.entry(g).or_default().push((c, n));
        }
        for (g, list) in groups {
            w::section(ui, &g);
            category_grid(app, ui, &list);
        }
    });
}

fn run_search(app: &mut App) {
    let key = format!("{}|{}|{}|{:?}", app.store.query, app.store.in_desc, app.store.only_installed, app.store.repo_filter);
    if key == app.store.cache_key {
        return;
    }
    app.store.cache_key = key;
    let Some(cat) = app.data.catalog.clone() else { return };
    let vdb = app.data.vdb.clone();
    let q = app.store.query.trim().to_lowercase();
    let mut scored: Vec<(u32, usize)> = Vec::new();
    if q.is_empty() {
        app.store.results.clear();
        return;
    }
    let (qcat, qname) = match q.split_once('/') {
        Some((c, n)) => (Some(c.to_string()), n.to_string()),
        None => (None, q.clone()),
    };
    for (i, p) in cat.pkgs.iter().enumerate() {
        if let Some(c) = &qcat
            && !p.cat.contains(c.as_str())
        {
            continue;
        }
        if let Some(r) = &app.store.repo_filter
            && !p.ebuilds.iter().any(|e| &*e.repo == r)
        {
            continue;
        }
        let name = p.pn.to_lowercase();
        let score = if name == qname {
            1000
        } else if name.starts_with(&qname) {
            700 - (name.len() as u32).min(200)
        } else if name.contains(&qname) {
            400 - (name.len() as u32).min(200)
        } else if app.store.in_desc && p.desc.to_lowercase().contains(&qname) {
            100
        } else {
            continue;
        };
        if app.store.only_installed && !vdb.as_ref().is_some_and(|v| v.is_installed(&p.key())) {
            continue;
        }
        let bonus = if vdb.as_ref().is_some_and(|v| v.is_installed(&p.key())) { 50 } else { 0 };
        scored.push((score + bonus, i));
    }
    scored.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    app.store.results = scored.into_iter().map(|x| x.1).take(2000).collect();
}

fn search(app: &mut App, ui: &mut Ui) {
    run_search(app);
    let Some(cat) = app.data.catalog.clone() else { return };
    let vdb = app.data.vdb.clone();
    egui::Frame::new().inner_margin(Margin::symmetric(24, 10)).show(ui, |ui| {
        ui.horizontal(|ui| {
            ui.label(RichText::new(format!("{} results", app.store.results.len())).size(18.0).family(theme::bold()).color(WHITE));
            if !app.store.query.is_empty() {
                ui.label(w::dim(format!("for “{}”", app.store.query)));
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let mut repos: Vec<String> = cat.repo_locations.keys().cloned().collect();
                repos.sort();
                egui::ComboBox::from_id_salt("repo-filter")
                    .selected_text(app.store.repo_filter.clone().map(|r| format!("::{r}")).unwrap_or_else(|| "All repositories".into()))
                    .show_ui(ui, |ui| {
                        ui.selectable_value(&mut app.store.repo_filter, None, "All repositories");
                        for r in repos {
                            ui.selectable_value(&mut app.store.repo_filter, Some(r.clone()), format!("::{r}"));
                        }
                    });
                ui.label(w::dim("Installed only"));
                w::toggle(ui, &mut app.store.only_installed);
                ui.label(w::dim("Search descriptions"));
                w::toggle(ui, &mut app.store.in_desc);
            });
        });
    });
    if app.store.query.trim().is_empty() {
        w::empty_state(ui, icon::MAGNIFYING_GLASS, "Search the tree", "Type a package name like “firefox”, or “www-client/” to list a category.");
        return;
    }
    if app.store.results.is_empty() {
        w::empty_state(ui, icon::SMILEY_SAD, "Nothing found", "Try enabling “Search descriptions”, or add an overlay that provides it.");
        if w::button(ui, icon::GIT_BRANCH, "Browse overlays").clicked() {
            app.push(Action::Tab(Tab::Overlays));
            app.push(Action::Reload(Reload::RemoteRepos));
        }
        return;
    }
    let results = app.store.results.clone();
    egui::ScrollArea::vertical().id_salt("search-res").auto_shrink([false, false]).show_rows(ui, 54.0, results.len(), |ui, range| {
        egui::Frame::new().inner_margin(Margin::symmetric(24, 0)).show(ui, |ui| {
            for i in range {
                let p = &cat.pkgs[results[i]];
                let key = p.key();
                let inst_v = vdb.as_ref().and_then(|v| v.installed(&key).last().map(|i| i.ver.raw.clone()));
                let latest = p.latest().map(|e| e.ver.raw.clone()).unwrap_or_default();
                let repos: Vec<String> = p.repos().iter().map(|r| format!("::{r}")).collect();
                let r = package_row(ui, &p.cat, &p.pn, &p.desc, &latest, &repos.join(" "), inst_v.as_deref(), false);
                if r.clicked() {
                    app.push(Action::Open(key.clone()));
                }
                r.context_menu(|ui| common::package_menu(app, ui, &key, inst_v.is_some()));
            }
        });
    });
}
