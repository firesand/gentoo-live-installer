//! Pieces shared by several pages.

use crate::ui::theme::*;
use crate::ui::widgets as w;
use egui::{Align2, Color32, CornerRadius, FontId, Rect, Sense, Ui};
use egui_phosphor::regular as icon;
use gentoo_settings::catalog::Category;

pub fn page(ui: &mut Ui, id: &str, add: impl FnOnce(&mut Ui)) {
    let mut area = egui::ScrollArea::vertical().id_salt(id).auto_shrink([false, false]);
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

pub fn cat_icon(c: Category) -> &'static str {
    match c {
        Category::Memory => icon::MEMORY,
        Category::Storage => icon::HARD_DRIVES,
        Category::Kernel => icon::SHIELD_CHECK,
        Category::Graphics => icon::MONITOR,
        Category::Audio => icon::SPEAKER_HIGH,
        Category::Gaming => icon::GAME_CONTROLLER,
        Category::Network => icon::WIFI_HIGH,
        Category::Services => icon::GEAR_SIX,
    }
}

/// "Cover art" colours per category, like PortageHub's package tiles.
pub fn cat_color(c: Category) -> Color32 {
    match c {
        Category::Memory => Color32::from_rgb(0x2a, 0x6f, 0x97),
        Category::Storage => Color32::from_rgb(0x9a, 0x5b, 0x2a),
        Category::Kernel => Color32::from_rgb(0x5b, 0x4b, 0x9e),
        Category::Graphics => Color32::from_rgb(0x2f, 0x7d, 0x5f),
        Category::Audio => Color32::from_rgb(0x8a, 0x34, 0x5e),
        Category::Gaming => Color32::from_rgb(0x6b, 0x7d, 0x2a),
        Category::Network => Color32::from_rgb(0x2a, 0x7f, 0x8a),
        Category::Services => Color32::from_rgb(0x4f, 0x5d, 0x6e),
    }
}

/// Square gradient tile with the category icon.
pub fn tile(ui: &mut Ui, c: Category, size: f32, dim: bool) -> egui::Response {
    let (rect, resp) = ui.allocate_exact_size(egui::vec2(size, size), Sense::hover());
    let base = cat_color(c);
    let (top, bottom) = (w::lerp_color(base, WHITE, 0.12), w::lerp_color(base, Color32::BLACK, 0.35));
    let (top, bottom) = if dim { (top.gamma_multiply(0.45), bottom.gamma_multiply(0.45)) } else { (top, bottom) };
    w::paint_vgradient(ui.painter(), rect, top, bottom);
    ui.painter().rect_stroke(rect, CornerRadius::same(2), egui::Stroke::new(1.0, Color32::from_white_alpha(18)), egui::StrokeKind::Inside);
    ui.painter().text(rect.center(), Align2::CENTER_CENTER, cat_icon(c), FontId::proportional(size * 0.48), if dim { TEXT_DIM } else { WHITE });
    resp
}

/// A wide banner with gradient and a big faint icon, like the Steam store hero.
pub fn hero_frame(ui: &mut Ui, height: f32, colors: (Color32, Color32), watermark: &str, add: impl FnOnce(&mut Ui)) {
    let width = ui.available_width();
    let (rect, _) = ui.allocate_exact_size(egui::vec2(width, height), Sense::hover());
    let p = ui.painter();
    w::paint_hgradient(p, rect, colors.0, colors.1);
    w::paint_vgradient(p, rect, Color32::from_black_alpha(0), Color32::from_black_alpha(90));
    p.text(
        rect.right_top() + egui::vec2(-80.0, height / 2.0),
        Align2::CENTER_CENTER,
        watermark,
        FontId::proportional(height * 0.8),
        Color32::from_white_alpha(16),
    );
    let inner: Rect = rect.shrink2(egui::vec2(28.0, 22.0));
    let mut child = ui.new_child(egui::UiBuilder::new().max_rect(inner).layout(egui::Layout::top_down(egui::Align::Min)));
    add(&mut child);
}
