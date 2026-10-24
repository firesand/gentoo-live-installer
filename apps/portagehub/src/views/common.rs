//! Pieces shared by several pages.

use crate::app::{Action, App};
use crate::portage::emerge::{EmergeOptions, Value};
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Align2, Color32, CornerRadius, FontId, Pos2, Rect, RichText, Sense, Ui};
use egui_phosphor::regular as icon;

/// Stable pseudo-random colour per category, used for package "cover art".
pub fn category_color(cat: &str) -> Color32 {
    let mut h: u32 = 2166136261;
    for b in cat.bytes() {
        h = (h ^ b as u32).wrapping_mul(16777619);
    }
    let palette = [
        Color32::from_rgb(0x2a, 0x6f, 0x97),
        Color32::from_rgb(0x5b, 0x4b, 0x9e),
        Color32::from_rgb(0x2f, 0x7d, 0x5f),
        Color32::from_rgb(0x9a, 0x5b, 0x2a),
        Color32::from_rgb(0x8a, 0x34, 0x5e),
        Color32::from_rgb(0x3d, 0x5a, 0x9e),
        Color32::from_rgb(0x6b, 0x7d, 0x2a),
        Color32::from_rgb(0x2a, 0x7f, 0x8a),
        Color32::from_rgb(0x7d, 0x3f, 0x2a),
        Color32::from_rgb(0x4f, 0x5d, 0x6e),
    ];
    palette[(h % palette.len() as u32) as usize]
}

pub fn category_icon(cat: &str) -> &'static str {
    let c = cat.split('-').next().unwrap_or(cat);
    let sub = cat.split_once('-').map(|x| x.1).unwrap_or("");
    match (c, sub) {
        ("games", _) => icon::GAME_CONTROLLER,
        ("media", "sound") | ("media", "plugins") => icon::MUSIC_NOTES,
        ("media", "video") | ("media", "tv") => icon::FILM_STRIP,
        ("media", "gfx") => icon::PAINT_BRUSH,
        ("media", _) => icon::IMAGE,
        ("www", _) => icon::GLOBE,
        ("net", _) => icon::WIFI_HIGH,
        ("dev", "db") => icon::DATABASE,
        ("dev", "lang") => icon::CODE,
        ("dev", "python") => icon::FILE_PY,
        ("dev", "rust") | ("dev", "go") => icon::CODE,
        ("dev", _) => icon::BRACKETS_CURLY,
        ("sys", "kernel") => icon::CPU,
        ("sys", "apps") | ("sys", "process") => icon::GEAR,
        ("sys", "fs") => icon::HARD_DRIVES,
        ("sys", _) => icon::GEAR_SIX,
        ("app", "editors") => icon::NOTE_PENCIL,
        ("app", "office") | ("app", "text") => icon::FILE_TEXT,
        ("app", "crypt") => icon::KEY,
        ("app", "emulation") | ("app", "containers") => icon::CUBE_TRANSPARENT,
        ("app", "admin") => icon::WRENCH,
        ("app", "shells") | ("app", "misc") => icon::TERMINAL,
        ("app", "arch") => icon::ARCHIVE,
        ("x11", _) | ("kde", _) | ("gnome", _) | ("xfce", _) | ("lxqt", _) | ("gui", _) => icon::APP_WINDOW,
        ("virtual", _) => icon::GHOST,
        ("acct", _) => icon::USER,
        ("sci", _) => icon::ATOM,
        ("mail", _) => icon::ENVELOPE,
        ("sec", _) => icon::SHIELD_CHECK,
        ("gui-wm", _) => icon::SQUARES_FOUR,
        _ => icon::PACKAGE,
    }
}

/// Square "cover" tile with gradient and category icon.
pub fn cover(ui: &mut Ui, cat: &str, size: f32) -> egui::Response {
    let (rect, resp) = ui.allocate_exact_size(egui::vec2(size, size), Sense::click());
    let base = category_color(cat);
    w::paint_vgradient(ui.painter(), rect, w::lerp_color(base, WHITE, 0.12), w::lerp_color(base, Color32::BLACK, 0.35));
    ui.painter().text(rect.center(), Align2::CENTER_CENTER, category_icon(cat), FontId::proportional(size * 0.5), Color32::from_white_alpha(220));
    resp
}

/// Steam store-list style row. Returns the response for the whole row.
#[allow(clippy::too_many_arguments)]
pub fn package_row(ui: &mut Ui, cat: &str, pn: &str, desc: &str, version: &str, repos: &str, installed: Option<&str>, selected: bool) -> egui::Response {
    let w = ui.available_width();
    let h = 54.0;
    let (rect, resp) = ui.allocate_exact_size(egui::vec2(w, h), Sense::click());
    if !ui.is_rect_visible(rect) {
        return resp;
    }
    let p = ui.painter();
    let bg = if selected {
        Color32::from_rgb(0x2a, 0x47, 0x5e)
    } else if resp.hovered() {
        Color32::from_rgb(0x22, 0x36, 0x4a)
    } else {
        Color32::from_rgb(0x16, 0x20, 0x2d)
    };
    p.rect_filled(rect.shrink2(egui::vec2(0.0, 1.5)), CornerRadius::same(2), bg);
    let tile = Rect::from_min_size(rect.min + egui::vec2(6.0, 6.0), egui::vec2(h - 12.0, h - 12.0));
    let base = category_color(cat);
    w::paint_vgradient(p, tile, w::lerp_color(base, WHITE, 0.12), w::lerp_color(base, Color32::BLACK, 0.35));
    p.text(tile.center(), Align2::CENTER_CENTER, category_icon(cat), FontId::proportional(20.0), Color32::from_white_alpha(220));

    let x = tile.right() + 12.0;
    let right_w = 250.0;
    let text_w = (rect.right() - right_w - x).max(80.0);
    let name_job = {
        let mut job = egui::text::LayoutJob::default();
        job.append(pn, 0.0, egui::TextFormat { font_id: FontId::new(15.0, theme::medium()), color: WHITE, ..Default::default() });
        job.append(&format!("   {cat}"), 0.0, egui::TextFormat { font_id: FontId::proportional(12.0), color: TEXT_FAINT, ..Default::default() });
        job.wrap = egui::text::TextWrapping::truncate_at_width(text_w);
        job
    };
    let g = ui.painter().layout_job(name_job);
    p.galley(Pos2::new(x, rect.top() + 8.0), g, WHITE);
    let mut dj = egui::text::LayoutJob::simple_singleline(desc.to_string(), FontId::proportional(12.5), TEXT_DIM);
    dj.wrap = egui::text::TextWrapping::truncate_at_width(text_w);
    let g = ui.painter().layout_job(dj);
    p.galley(Pos2::new(x, rect.top() + 30.0), g, TEXT_DIM);

    let rx = rect.right() - 12.0;
    p.text(Pos2::new(rx, rect.top() + 16.0), Align2::RIGHT_CENTER, version, FontId::new(14.0, theme::medium()), TEXT);
    p.text(Pos2::new(rx, rect.top() + 37.0), Align2::RIGHT_CENTER, repos, FontId::proportional(11.5), TEXT_FAINT);
    if let Some(iv) = installed {
        let label = format!("{} {}", icon::CHECK, iv);
        let g = ui.painter().layout_no_wrap(label, FontId::new(11.5, theme::bold()), GREEN_TEXT);
        let br = Rect::from_min_size(Pos2::new(rx - 130.0 - g.size().x, rect.center().y - 10.0), g.size() + egui::vec2(12.0, 6.0));
        p.rect_filled(br, CornerRadius::same(3), GREEN_DARK);
        p.galley(br.min + egui::vec2(6.0, 3.0), g, GREEN_TEXT);
    }
    resp
}

/// Common "do something with this package" menu items.
pub fn package_menu(app: &mut App, ui: &mut Ui, key: &str, installed: bool) {
    if ui.button(format!("{}  Open details", icon::INFO)).clicked() {
        app.push(Action::Open(key.to_string()));
        ui.close();
    }
    ui.separator();
    if !installed {
        if ui.button(format!("{}  Install", icon::DOWNLOAD_SIMPLE)).clicked() {
            app.push(Action::Pretend(EmergeOptions::default().targets(&[key])));
            ui.close();
        }
        if ui.button(format!("{}  Install as dependency (--oneshot)", icon::DOWNLOAD_SIMPLE)).clicked() {
            app.push(Action::Pretend(EmergeOptions::default().with("--oneshot", Value::On).targets(&[key])));
            ui.close();
        }
    } else {
        if ui.button(format!("{}  Update", icon::ARROW_CIRCLE_UP)).clicked() {
            app.push(Action::Pretend(EmergeOptions::default().with("--update", Value::On).targets(&[key])));
            ui.close();
        }
        if ui.button(format!("{}  Rebuild (--oneshot)", icon::ARROWS_CLOCKWISE)).clicked() {
            app.push(Action::Pretend(EmergeOptions::default().with("--oneshot", Value::On).targets(&[key])));
            ui.close();
        }
        if ui.button(format!("{}  Remove safely (--depclean)", icon::TRASH)).clicked() {
            app.push(Action::Pretend(EmergeOptions { action: crate::portage::emerge::Action::Depclean, ..Default::default() }.targets(&[key])));
            ui.close();
        }
    }
    ui.separator();
    if ui.button(format!("{}  Copy name", icon::COPY)).clicked() {
        ui.ctx().copy_text(key.to_string());
        ui.close();
    }
}

/// Sub-navigation band under the main nav (Steam's second bar).
pub fn subnav<T: PartialEq + Copy>(ui: &mut Ui, current: &mut T, items: &[(T, &str)], right: impl FnOnce(&mut Ui)) {
    egui::Frame::new().fill(Color32::from_rgb(0x1f, 0x2e, 0x40)).inner_margin(egui::Margin::symmetric(16, 4)).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            for (v, label) in items {
                let sel = *current == *v;
                let r = ui.add(
                    egui::Button::new(RichText::new(*label).size(13.5).color(if sel { WHITE } else { TEXT }))
                        .fill(if sel { Color32::from_rgb(0x2f, 0x5b, 0x80) } else { Color32::TRANSPARENT })
                        .min_size(egui::vec2(0.0, 28.0)),
                );
                if r.clicked() {
                    *current = *v;
                }
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), right);
        });
    });
}

/// Page container with consistent margins and vertical scrolling.
pub fn page(ui: &mut Ui, id: &str, add: impl FnOnce(&mut Ui)) {
    let mut area = egui::ScrollArea::vertical().id_salt(id).auto_shrink([false, false]);
    // PORTAGEHUB_SCRIPT "scroll=N" (screenshots of long pages).
    if let Some(off) = ui.ctx().data_mut(|d| d.remove_temp::<f32>(egui::Id::new("script-scroll"))) {
        area = area.vertical_scroll_offset(off);
    }
    area.show(ui, |ui| {
        egui::Frame::new().inner_margin(egui::Margin::symmetric(24, 16)).show(ui, |ui| {
            ui.set_width(ui.available_width());
            add(ui);
        });
    });
}

pub fn tip(app: &App, ui: &mut Ui, text: &str) {
    if app.settings.beginner_tips {
        w::callout(ui, ACCENT, icon::LIGHTBULB, text);
        ui.add_space(6.0);
    }
}
