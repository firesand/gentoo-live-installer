//! The "Tweaks" tab: categories on the left (Steam library style), an overview
//! hero with the CachyOS-defaults button, and a card per tweak with a switch.

use super::common::{self, page, tile};
use crate::app::{App, ToastKind};
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Color32, Margin, RichText, Ui};
use egui_phosphor::regular as icon;
use gentoo_settings::catalog::{After, Category, LiveState, Param, Risk};
use gentoo_settings::status::{self, Badge, Status};

pub fn show(app: &mut App, ui: &mut Ui) {
    egui::Panel::left("tweaks-side")
        .resizable(true)
        .default_size(270.0)
        .size_range(220.0..=380.0)
        .frame(egui::Frame::new().fill(SIDEBAR).inner_margin(Margin::symmetric(0, 10)))
        .show(ui, |ui| sidebar(app, ui));
    egui::CentralPanel::default().frame(egui::Frame::new().fill(BG)).show(ui, |ui| {
        if !app.query.trim().is_empty() {
            search(app, ui);
        } else {
            match app.category {
                None => overview(app, ui),
                Some(c) => category(app, ui, c),
            }
        }
    });
}

fn sidebar(app: &mut App, ui: &mut Ui) {
    egui::Frame::new().inner_margin(Margin::symmetric(10, 0)).show(ui, |ui| {
        w::search_box(ui, &mut app.query, "Search tweaks…", ui.available_width());
        ui.add_space(6.0);
        if w::side_item(ui, icon::HOUSE, "Overview", app.category.is_none() && app.query.is_empty(), None).clicked() {
            app.category = None;
            app.query.clear();
        }
    });
    ui.add_space(8.0);
    ui.horizontal(|ui| {
        ui.add_space(14.0);
        ui.label(RichText::new("CATEGORIES").size(11.0).family(theme::bold()).color(TEXT_DIM));
    });
    ui.add_space(2.0);
    egui::Frame::new().inner_margin(Margin::symmetric(10, 0)).show(ui, |ui| {
        for c in Category::ALL {
            let applied = app.scan.statuses.iter().filter(|s| s.tweak.category == c && s.badge.applied()).count();
            let avail = app.scan.statuses.iter().filter(|s| s.tweak.category == c && s.available.is_ok()).count();
            let r = w::side_item(ui, common::cat_icon(c), c.label(), app.category == Some(c) && app.query.is_empty(), Some(avail))
                .on_hover_text(format!("{applied} applied · {avail} available on this machine"));
            if r.clicked() {
                app.category = Some(c);
                app.query.clear();
            }
        }
    });
    ui.with_layout(egui::Layout::bottom_up(egui::Align::Min), |ui| {
        egui::Frame::new().inner_margin(Margin::symmetric(14, 4)).show(ui, |ui| {
            w::switch_row(
                ui,
                &mut app.show_unavailable,
                "Show unavailable",
                "Also list tweaks that don't apply to this machine or init system, with the reason.",
            );
        });
    });
}

// ───────────────────────── overview ─────────────────────────

fn overview(app: &mut App, ui: &mut Ui) {
    page(ui, "overview", |ui| {
        hero(app, ui);
        ui.add_space(12.0);
        w::callout(
            ui,
            ACCENT,
            icon::LIGHTBULB,
            "Gentoo Settings brings CachyOS's system tuning to Gentoo. Every tweak is a small file in /etc that you can turn off again — \
             flip the switches, press REVIEW & APPLY, and you'll see each file and command before anything changes.",
        );
        ui.add_space(4.0);
        attention(app, ui);
        w::section(ui, "Categories");
        let cols = ((ui.available_width() + 12.0) / 320.0).floor().clamp(1.0, 4.0) as usize;
        let mut clicked = None;
        for row in Category::ALL.chunks(cols) {
            ui.columns(cols, |cs| {
                for (i, c) in row.iter().enumerate() {
                    if category_card(app, &mut cs[i], *c) {
                        clicked = Some(*c);
                    }
                }
            });
            ui.add_space(6.0);
        }
        if let Some(c) = clicked {
            app.category = Some(c);
        }
    });
}

fn hero(app: &mut App, ui: &mut Ui) {
    let applied = app.scan.statuses.iter().filter(|s| s.badge.applied()).count();
    let available = app.scan.statuses.iter().filter(|s| s.available.is_ok() && !s.badge.applied() && s.badge != Badge::AlreadySet).count();
    let already = app.scan.statuses.iter().filter(|s| s.badge == Badge::AlreadySet).count();
    let defaults = app
        .scan
        .statuses
        .iter()
        .filter(|s| s.tweak.recommended && s.available.is_ok() && matches!(s.badge, Badge::Off | Badge::Modified) && !app.pending.contains_key(s.tweak.id))
        .count();
    let init = app.sys.init.label();
    let kernel = app.sys.kernel.clone();
    let mut stage = false;
    let mut review = false;
    let mut revert = false;
    common::hero_frame(ui, 200.0, (Color32::from_rgb(0x1e, 0x4f, 0x78), Color32::from_rgb(0x2b, 0x2a, 0x5a)), icon::SLIDERS_HORIZONTAL, |ui| {
        ui.label(RichText::new("CACHYOS SETTINGS, THE GENTOO WAY").size(12.0).family(theme::bold()).color(ACCENT));
        ui.label(RichText::new("Tune your Gentoo system").size(30.0).family(theme::bold()).color(WHITE));
        ui.label(
            RichText::new(format!("{init}  ·  Linux {kernel}  ·  {applied} applied  ·  {available} available  ·  {already} already set by your system"))
                .color(TEXT),
        );
        ui.add_space(14.0);
        ui.horizontal(|ui| {
            let r = ui
                .add_enabled_ui(defaults > 0, |ui| w::big_button(ui, &format!("{}  APPLY CACHYOS DEFAULTS", icon::MAGIC_WAND), GRAD_GREEN, 260.0))
                .inner
                .on_hover_text(format!(
                    "Stage the {defaults} recommended tweaks that apply to this machine and aren't in effect yet. Settings your system already has are left alone. You review everything before it is applied."
                ));
            stage = r.clicked();
            ui.add_space(6.0);
            if !app.pending.is_empty() {
                review = w::big_button(ui, &format!("{}  REVIEW CHANGES ({})", icon::CHECK, app.pending.len()), GRAD_BLUE, 220.0).clicked();
            } else if applied > 0 {
                revert = w::big_button(ui, &format!("{}  REVERT ALL", icon::ARROW_COUNTER_CLOCKWISE), GRAD_GREY, 180.0)
                    .on_hover_text("Stage removal of every tweak Gentoo Settings applied")
                    .clicked();
            }
        });
    });
    if stage {
        let n = app.stage_defaults(None);
        app.toast(ToastKind::Info, format!("Staged {n} recommended tweak{} — review them in the bar below.", if n == 1 { "" } else { "s" }));
    }
    if review {
        app.review_pending();
    }
    if revert {
        app.stage_revert_all();
    }
}

fn attention(app: &mut App, ui: &mut Ui) {
    let items: Vec<Status> = app.scan.statuses.iter().filter(|s| matches!(s.badge, Badge::Overridden | Badge::Modified | Badge::Pending(_))).cloned().collect();
    if items.is_empty() {
        return;
    }
    w::section(ui, &format!("Needs attention ({})", items.len()));
    for s in items {
        let (tint, ic, text) = match s.badge {
            Badge::Overridden => (
                ORANGE,
                icon::WARNING,
                format!("{} is applied, but the system still has {}. {}", s.tweak.title, s.live.now, s.conflicts.first().cloned().unwrap_or_default()),
            ),
            Badge::Modified => {
                (ORANGE, icon::PENCIL_SIMPLE, format!("{}: the file was edited or is incomplete. Re-apply or turn it off to clean it up.", s.tweak.title))
            }
            Badge::Pending(a) => (YELLOW, icon::POWER, format!("{} takes full effect {}.", s.tweak.title, a.label())),
            _ => continue,
        };
        w::card_tinted(tint).show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                ui.label(RichText::new(ic).size(18.0).color(tint));
                ui.add(egui::Label::new(RichText::new(text).color(TEXT)).wrap());
            });
        });
        ui.add_space(4.0);
    }
}

fn category_card(app: &App, ui: &mut Ui, c: Category) -> bool {
    let applied = app.scan.statuses.iter().filter(|s| s.tweak.category == c && s.badge.applied()).count();
    let avail = app.scan.statuses.iter().filter(|s| s.tweak.category == c && s.available.is_ok()).count();
    let total = app.scan.statuses.iter().filter(|s| s.tweak.category == c).count();
    let resp = w::card()
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.set_min_height(104.0);
            ui.horizontal_top(|ui| {
                tile(ui, c, 52.0, avail == 0);
                ui.vertical(|ui| {
                    ui.label(RichText::new(c.label()).size(16.0).family(theme::bold()).color(WHITE));
                    ui.label(RichText::new(format!("{applied} applied · {avail} of {total} available")).size(12.0).color(if applied > 0 {
                        GREEN_TEXT
                    } else {
                        TEXT_DIM
                    }));
                    ui.add(egui::Label::new(RichText::new(c.blurb()).size(12.5).color(TEXT_DIM)).wrap());
                });
            });
        })
        .response;
    let r = resp.interact(egui::Sense::click()).on_hover_cursor(egui::CursorIcon::PointingHand);
    if r.hovered() {
        ui.painter().rect_stroke(r.rect, 4, egui::Stroke::new(1.0, ACCENT_DIM), egui::StrokeKind::Inside);
    }
    r.clicked()
}

// ───────────────────────── category & search ─────────────────────────

fn category(app: &mut App, ui: &mut Ui, c: Category) {
    let ids: Vec<&'static str> = app.scan.statuses.iter().filter(|s| s.tweak.category == c).map(|s| s.tweak.id).collect();
    page(ui, &format!("cat-{c:?}"), |ui| {
        let stageable = app
            .scan
            .statuses
            .iter()
            .filter(|s| s.tweak.category == c && s.tweak.recommended && s.available.is_ok() && matches!(s.badge, Badge::Off | Badge::Modified))
            .count();
        ui.horizontal(|ui| {
            tile(ui, c, 64.0, false);
            ui.add_space(6.0);
            ui.vertical(|ui| {
                w::title(ui, c.label());
                w::subtitle(ui, c.blurb());
            });
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if stageable > 0 && w::primary(ui, icon::MAGIC_WAND, &format!("Stage {stageable} recommended")).clicked() {
                    app.stage_defaults(Some(c));
                }
            });
        });
        if c == Category::Services && app.sys.init != gentoo_settings::detect::Init::Systemd {
            ui.add_space(8.0);
            w::callout(
                ui,
                Color32::from_rgb(0xb3, 0x8b, 0xf5),
                icon::TREE_STRUCTURE,
                &format!(
                    "These CachyOS settings configure systemd itself. This system runs {}, so they don't apply; each card says what OpenRC uses instead.",
                    app.sys.init.label()
                ),
            );
        }
        let (avail, na): (Vec<&str>, Vec<&str>) = ids.iter().partition(|id| app.status(id).is_some_and(|s| s.available.is_ok() || s.installed.is_some()));
        w::section(ui, &format!("Available ({})", avail.len()));
        if avail.is_empty() {
            ui.label(w::dim("Nothing in this category applies to this machine."));
        }
        for id in avail {
            tweak_card(app, ui, id);
            ui.add_space(6.0);
        }
        if app.show_unavailable && !na.is_empty() {
            w::section(ui, &format!("Not available here ({})", na.len()));
            for id in na {
                tweak_card(app, ui, id);
                ui.add_space(6.0);
            }
        }
    });
}

fn search(app: &mut App, ui: &mut Ui) {
    let q = app.query.trim().to_lowercase();
    let ids: Vec<&'static str> = app
        .scan
        .statuses
        .iter()
        .filter(|s| {
            let t = s.tweak;
            [t.id, t.title, t.summary, t.details, t.source].iter().any(|f| f.to_lowercase().contains(&q))
        })
        .filter(|s| app.show_unavailable || s.available.is_ok() || s.installed.is_some())
        .map(|s| s.tweak.id)
        .collect();
    page(ui, "search", |ui| {
        w::title(ui, &format!("Search: {}", app.query.trim()));
        w::subtitle(ui, &format!("{} tweak{}", ids.len(), if ids.len() == 1 { "" } else { "s" }));
        ui.add_space(8.0);
        if ids.is_empty() {
            w::empty_state(ui, icon::MAGNIFYING_GLASS, "No tweaks match", "Try a sysctl name, a file path or a word like \"swap\".");
        }
        for id in ids {
            tweak_card(app, ui, id);
            ui.add_space(6.0);
        }
    });
}

// ───────────────────────── the card ─────────────────────────

fn tweak_card(app: &mut App, ui: &mut Ui, id: &'static str) {
    let Some(s) = app.status(id).cloned() else { return };
    let t = s.tweak;
    let pending = app.pending.get(id).cloned();
    let na = s.available.is_err() && s.installed.is_none();
    let expanded = app.expanded.contains(id);
    let frame = if pending.is_some() { w::card_tinted(ACCENT) } else { w::card() };
    let mut toggle_to: Option<bool> = None;
    let mut toggle_expand = false;
    frame.show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            tile(ui, t.category, 46.0, na);
            ui.add_space(4.0);
            let right = 90.0;
            ui.allocate_ui(egui::vec2(ui.available_width() - right, 0.0), |ui| {
                ui.vertical(|ui| {
                    ui.horizontal_wrapped(|ui| {
                        let title = ui
                            .add(
                                egui::Label::new(RichText::new(t.title).size(15.0).family(theme::bold()).color(if na { TEXT_DIM } else { WHITE }))
                                    .sense(egui::Sense::click()),
                            )
                            .on_hover_cursor(egui::CursorIcon::PointingHand);
                        toggle_expand |= title.clicked();
                        match &pending {
                            Some(p) if !p.enable => {
                                w::status_badge(ui, "WILL DISABLE");
                            }
                            Some(_) if s.installed.is_some() => {
                                w::status_badge(ui, "WILL CHANGE");
                            }
                            Some(_) => {
                                w::status_badge(ui, "WILL ENABLE");
                            }
                            None => {}
                        }
                        w::status_badge(ui, s.badge.label()).on_hover_text(badge_help(&s));
                        if t.risk != Risk::Safe {
                            w::status_badge(ui, t.risk.label()).on_hover_text(match t.risk {
                                Risk::Moderate => "Has a trade-off (power use, security or noise) — read the details.",
                                _ => "Trades away a guarantee for speed. Not part of the CachyOS defaults preset here.",
                            });
                        }
                        if let Some(only) = t.only {
                            w::status_badge(ui, &only.label().to_uppercase());
                        }
                    });
                    ui.label(RichText::new(t.summary).color(TEXT_DIM));
                    live_line(ui, &s);
                });
            });
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let caret = if expanded { icon::CARET_DOWN } else { icon::CARET_RIGHT };
                toggle_expand |= w::icon_button(ui, caret, if expanded { "Hide details" } else { "Details" }).clicked();
                ui.add_space(4.0);
                let mut on = app.wanted_on(&s);
                let can = !na || s.installed.is_some();
                let r = ui.add_enabled_ui(can, |ui| w::toggle(ui, &mut on)).inner;
                let r = if na { r.on_disabled_hover_text(s.available.as_ref().err().map(|n| n.reason.clone()).unwrap_or_default()) } else { r };
                if r.changed() {
                    toggle_to = Some(on);
                }
            });
        });
        if expanded {
            ui.add_space(6.0);
            details(app, ui, &s);
        }
    });
    if let Some(on) = toggle_to {
        app.set_on(id, on);
    }
    if toggle_expand && !app.expanded.remove(id) {
        app.expanded.insert(id);
    }
}

fn badge_help(s: &Status) -> String {
    match s.badge {
        Badge::Active => "Applied by Gentoo Settings and in effect.".into(),
        Badge::Pending(a) => format!("Applied; takes full effect {}.", a.label()),
        Badge::Overridden => "Applied, but something else sets a different value (see details).".into(),
        Badge::Modified => "The managed file was edited by hand or is incomplete.".into(),
        Badge::AlreadySet => "Not applied by Gentoo Settings, but the system already behaves this way.".into(),
        Badge::Off => "Not applied.".into(),
        Badge::NotAvailable => s.available.as_ref().err().map(|n| n.reason.clone()).unwrap_or_default(),
    }
}

fn live_line(ui: &mut Ui, s: &Status) {
    if s.live.state == LiveState::NoLive || s.available.is_err() && s.installed.is_none() {
        return;
    }
    ui.horizontal_wrapped(|ui| {
        ui.spacing_mut().item_spacing.x = 4.0;
        let (ic, col) = match s.live.state {
            LiveState::Match => (icon::CHECK_CIRCLE, GREEN),
            LiveState::Differs => (icon::ARROW_RIGHT, YELLOW),
            _ => (icon::QUESTION, TEXT_DIM),
        };
        ui.label(RichText::new(ic).size(12.0).color(col));
        ui.label(RichText::new("now").size(11.5).color(TEXT_FAINT));
        ui.label(RichText::new(short(&s.live.now)).size(12.0).monospace().color(TEXT));
        if s.live.state != LiveState::Match {
            ui.label(RichText::new("· wants").size(11.5).color(TEXT_FAINT));
            ui.label(RichText::new(short(&s.live.want)).size(12.0).monospace().color(GREEN_TEXT));
        }
    });
}

fn short(s: &str) -> String {
    if s.chars().count() > 90 { format!("{}…", s.chars().take(88).collect::<String>()) } else { s.to_string() }
}

fn details(app: &mut App, ui: &mut Ui, s: &Status) {
    let t = s.tweak;
    ui.separator();
    ui.add(egui::Label::new(RichText::new(t.details).color(TEXT)).wrap());
    ui.add_space(6.0);
    if let Err(na) = &s.available {
        w::callout(ui, ORANGE, icon::PROHIBIT, &format!("Not available: {}.", na.reason));
        if let Some(fix) = &na.fix {
            ui.add_space(4.0);
            w::command_preview(ui, fix);
        }
        ui.add_space(4.0);
    }
    if status::has_param(t) && (s.available.is_ok() || s.installed.is_some()) {
        param_editor(app, ui, s);
        ui.add_space(4.0);
    }
    ui.horizontal_wrapped(|ui| {
        ui.spacing_mut().item_spacing.x = 28.0;
        if s.live.state != LiveState::NoLive {
            w::stat(ui, "Now", &short(&s.live.now));
            w::stat(ui, "Wanted", &short(&s.live.want));
        }
        w::stat(
            ui,
            "Takes effect",
            match t.after {
                After::Now => "Immediately",
                After::Relogin => "After re-login",
                After::Reboot => "After reboot",
            },
        );
        ui.vertical(|ui| {
            ui.spacing_mut().item_spacing.y = 1.0;
            ui.label(RichText::new("PORTED FROM").size(11.0).family(theme::bold()).color(TEXT_DIM));
            let first = t.source.split(", ").next().unwrap_or("");
            if first.starts_with("usr/") || first.starts_with("etc/") {
                w::link(ui, t.source, &format!("https://github.com/CachyOS/CachyOS-Settings/blob/master/{first}"));
            } else {
                ui.label(RichText::new(t.source).size(14.0).color(WHITE));
            }
        });
    });
    if !t.notes.is_empty() {
        ui.add_space(6.0);
        w::callout(ui, ACCENT, icon::INFO, t.notes);
    }
    if !s.conflicts.is_empty() {
        ui.add_space(6.0);
        w::card_tinted(ORANGE).show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.label(RichText::new(format!("{}  Also configured elsewhere", icon::WARNING)).color(ORANGE).family(theme::medium()));
            for c in &s.conflicts {
                ui.label(RichText::new(format!("•  {c}")).color(TEXT).size(13.0));
            }
        });
    }
    if let Some(i) = &s.installed
        && (i.modified || i.partial)
    {
        ui.add_space(6.0);
        w::callout(
            ui,
            ORANGE,
            icon::PENCIL_SIMPLE,
            "The block in the file differs from what Gentoo Settings writes (edited by hand, an older version, or written for the other init system). Applying again replaces it; turning the tweak off removes it.",
        );
    }
    if s.available.is_ok() || s.installed.is_some() {
        let value = app.shown_value(s);
        let imp = (t.build)(&t.ctx(&app.sys, &value));
        if !imp.blocks.is_empty() {
            ui.add_space(6.0);
            ui.label(RichText::new(format!("FILES ON {}", app.sys.init.label().to_uppercase())).size(11.0).family(theme::bold()).color(TEXT_DIM));
            for (path, _) in &imp.blocks {
                ui.label(w::mono(format!("{}  {path}", icon::FILE_TEXT)));
            }
        }
    }
}

fn param_editor(app: &mut App, ui: &mut Ui, s: &Status) {
    let t = s.tweak;
    let id = t.id;
    let current = app.shown_value(s);
    ui.horizontal(|ui| {
        ui.label(RichText::new("VALUE").size(11.0).family(theme::bold()).color(TEXT_DIM));
        match &t.param {
            Param::Int { min, max } => {
                let mut v: i64 = current.parse().unwrap_or(0);
                let speed = ((*max - *min) as f64 / 400.0).max(1.0);
                let r = ui.add(egui::DragValue::new(&mut v).range(*min..=*max).speed(speed));
                if !t.unit.is_empty() {
                    ui.label(w::dim(t.unit));
                }
                if t.unit == "bytes" {
                    ui.label(w::dim(format!("({})", crate::fmt::size(v.max(0) as u64))));
                }
                if r.changed() {
                    app.set_value(id, v.to_string());
                }
            }
            Param::Choice(_) | Param::Dynamic(_) => {
                let opts = t.choices(&app.sys);
                let mut sel = current.clone();
                let pairs: Vec<(usize, &str)> = opts.iter().enumerate().map(|(i, o)| (i, o.as_str())).collect();
                let mut idx = opts.iter().position(|o| *o == sel).unwrap_or(0);
                if w::segmented(ui, &mut idx, &pairs).changed() {
                    sel = opts[idx].clone();
                    app.set_value(id, sel);
                }
            }
            Param::Fixed => {}
        }
        if current != t.default && w::button(ui, icon::ARROW_COUNTER_CLOCKWISE, &format!("CachyOS value ({})", t.default)).clicked() {
            app.set_value(id, t.default.to_string());
        }
    });
    if s.installed.is_none() && !app.wanted_on(s) && app.values.contains_key(id) {
        ui.label(w::dim("Turn the switch on to apply this value."));
    }
}
