//! Steam-inspired look: deep navy panels, light-blue accents, green call-to-
//! action buttons, capsule cards and uppercase section headers.

use eframe::egui::{
    self, Align2, Color32, CornerRadius, FontData, FontDefinitions, FontFamily, FontId, Margin, Mesh, Pos2, Rect,
    Response, RichText, Sense, Shape, Stroke, StrokeKind, TextStyle, Ui, Vec2,
};
use std::sync::Arc;

pub const BG_TOP: Color32 = Color32::from_rgb(0x17, 0x1a, 0x21);
pub const BG: Color32 = Color32::from_rgb(0x1b, 0x28, 0x38);
pub const BG_DEEP: Color32 = Color32::from_rgb(0x10, 0x17, 0x20);
pub const PANEL: Color32 = Color32::from_rgb(0x16, 0x20, 0x2d);
pub const PANEL_HI: Color32 = Color32::from_rgb(0x22, 0x31, 0x44);
pub const PANEL_SEL: Color32 = Color32::from_rgb(0x2a, 0x3f, 0x5a);
pub const BORDER: Color32 = Color32::from_rgb(0x2a, 0x3a, 0x4d);
pub const ACCENT: Color32 = Color32::from_rgb(0x66, 0xc0, 0xf4);
pub const ACCENT_STRONG: Color32 = Color32::from_rgb(0x1a, 0x9f, 0xff);
pub const TEXT: Color32 = Color32::from_rgb(0xc7, 0xd5, 0xe0);
pub const TEXT_BRIGHT: Color32 = Color32::from_rgb(0xf2, 0xf5, 0xf8);
pub const TEXT_DIM: Color32 = Color32::from_rgb(0x8f, 0x98, 0xa0);
pub const GREEN: Color32 = Color32::from_rgb(0x8b, 0xc5, 0x3f);
pub const GREEN_A: Color32 = Color32::from_rgb(0x75, 0xb0, 0x22);
pub const GREEN_B: Color32 = Color32::from_rgb(0x4e, 0x7d, 0x14);
pub const BLUE_A: Color32 = Color32::from_rgb(0x47, 0xbf, 0xff);
pub const BLUE_B: Color32 = Color32::from_rgb(0x1a, 0x44, 0xc2);
pub const WARN: Color32 = Color32::from_rgb(0xe8, 0xb7, 0x3c);
pub const DANGER: Color32 = Color32::from_rgb(0xd9, 0x55, 0x55);
pub const DANGER_B: Color32 = Color32::from_rgb(0x8f, 0x2a, 0x2a);
pub const PURPLE: Color32 = Color32::from_rgb(0xa7, 0x8b, 0xfa);

pub fn bold(size: f32) -> FontId {
    FontId::new(size, FontFamily::Name("bold".into()))
}

pub fn regular(size: f32) -> FontId {
    FontId::proportional(size)
}

pub fn mono(size: f32) -> FontId {
    FontId::monospace(size)
}

fn load_font(paths: &[&str]) -> Option<Vec<u8>> {
    paths.iter().find_map(|p| std::fs::read(p).ok())
}

/// Use Noto Sans / JetBrains Mono when installed, falling back to egui's fonts.
pub fn install_fonts(ctx: &egui::Context) {
    let mut fonts = FontDefinitions::default();
    let fallback_prop = fonts.families.get(&FontFamily::Proportional).cloned().unwrap_or_default();
    let fallback_mono = fonts.families.get(&FontFamily::Monospace).cloned().unwrap_or_default();

    let regular = load_font(&["/usr/share/fonts/noto/NotoSans-Regular.ttf", "/usr/share/fonts/noto/NotoSans-Regular.otf"]);
    let bold = load_font(&[
        "/usr/share/fonts/noto/NotoSans-SemiBold.ttf",
        "/usr/share/fonts/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/noto/NotoSans-SemiBold.otf",
    ]);
    let monof = load_font(&[
        "/usr/share/fonts/jetbrains-mono/JetBrainsMono-Regular.ttf",
        "/usr/share/fonts/hack/Hack-Regular.ttf",
        "/usr/share/fonts/noto/NotoSansMono-Regular.ttf",
    ]);

    let mut prop = fallback_prop.clone();
    if let Some(data) = regular {
        fonts.font_data.insert("ks-regular".into(), Arc::new(FontData::from_owned(data)));
        prop.insert(0, "ks-regular".into());
    }
    let mut bold_family = prop.clone();
    if let Some(data) = bold {
        fonts.font_data.insert("ks-bold".into(), Arc::new(FontData::from_owned(data)));
        bold_family.insert(0, "ks-bold".into());
    }
    let mut mono_family = fallback_mono;
    if let Some(data) = monof {
        fonts.font_data.insert("ks-mono".into(), Arc::new(FontData::from_owned(data)));
        mono_family.insert(0, "ks-mono".into());
    }
    fonts.families.insert(FontFamily::Proportional, prop);
    fonts.families.insert(FontFamily::Name("bold".into()), bold_family);
    fonts.families.insert(FontFamily::Monospace, mono_family);
    ctx.set_fonts(fonts);
}

pub fn install_style(ctx: &egui::Context) {
    ctx.set_theme(egui::Theme::Dark);
    ctx.style_mut_of(egui::Theme::Dark, |s| {
        s.text_styles = [
            (TextStyle::Heading, regular(24.0)),
            (TextStyle::Body, regular(15.0)),
            (TextStyle::Button, regular(15.0)),
            (TextStyle::Small, regular(12.5)),
            (TextStyle::Monospace, mono(13.0)),
        ]
        .into();
        s.spacing.item_spacing = Vec2::new(10.0, 8.0);
        s.spacing.button_padding = Vec2::new(14.0, 7.0);
        s.spacing.interact_size.y = 30.0;
        s.spacing.scroll.bar_width = 8.0;
        let v = &mut s.visuals;
        v.panel_fill = BG;
        v.window_fill = PANEL;
        v.extreme_bg_color = BG_DEEP;
        v.faint_bg_color = PANEL;
        v.window_stroke = Stroke::new(1.0_f32, BORDER);
        v.window_corner_radius = CornerRadius::same(6);
        v.override_text_color = Some(TEXT);
        v.hyperlink_color = ACCENT;
        v.selection.bg_fill = ACCENT_STRONG.linear_multiply(0.6);
        v.selection.stroke = Stroke::new(1.0_f32, TEXT_BRIGHT);
        let w = &mut v.widgets;
        for (state, fill) in [
            (&mut w.inactive, Color32::from_rgb(0x2a, 0x3a, 0x4f)),
            (&mut w.hovered, Color32::from_rgb(0x36, 0x4b, 0x66)),
            (&mut w.active, Color32::from_rgb(0x3d, 0x5a, 0x7d)),
            (&mut w.open, Color32::from_rgb(0x2a, 0x3a, 0x4f)),
        ] {
            state.bg_fill = fill;
            state.weak_bg_fill = fill;
            state.corner_radius = CornerRadius::same(3);
            state.bg_stroke = Stroke::NONE;
            state.fg_stroke = Stroke::new(1.0_f32, TEXT_BRIGHT);
        }
        w.noninteractive.bg_fill = PANEL;
        w.noninteractive.bg_stroke = Stroke::new(1.0_f32, BORDER);
        w.noninteractive.fg_stroke = Stroke::new(1.0_f32, TEXT);
    });
}

/// Fill a rect with a vertical gradient.
pub fn vgradient(painter: &egui::Painter, rect: Rect, top: Color32, bottom: Color32) {
    let mut m = Mesh::default();
    m.colored_vertex(rect.left_top(), top);
    m.colored_vertex(rect.right_top(), top);
    m.colored_vertex(rect.left_bottom(), bottom);
    m.colored_vertex(rect.right_bottom(), bottom);
    m.add_triangle(0, 1, 2);
    m.add_triangle(1, 2, 3);
    painter.add(Shape::mesh(m));
}

/// Fill a rect with a horizontal gradient.
pub fn hgradient(painter: &egui::Painter, rect: Rect, left: Color32, right: Color32) {
    let mut m = Mesh::default();
    m.colored_vertex(rect.left_top(), left);
    m.colored_vertex(rect.right_top(), right);
    m.colored_vertex(rect.left_bottom(), left);
    m.colored_vertex(rect.right_bottom(), right);
    m.add_triangle(0, 1, 2);
    m.add_triangle(1, 2, 3);
    painter.add(Shape::mesh(m));
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Btn {
    /// Green "Play / Install" call to action.
    Primary,
    /// Blue gradient.
    Blue,
    /// Flat grey-blue.
    Secondary,
    Danger,
}

fn btn_colors(kind: Btn) -> (Color32, Color32, Color32) {
    match kind {
        Btn::Primary => (GREEN_A, GREEN_B, Color32::WHITE),
        Btn::Blue => (BLUE_A, BLUE_B, Color32::WHITE),
        Btn::Secondary => (Color32::from_rgb(0x3d, 0x52, 0x6c), Color32::from_rgb(0x2a, 0x3a, 0x4f), TEXT_BRIGHT),
        Btn::Danger => (DANGER, DANGER_B, Color32::WHITE),
    }
}

/// A Steam-style gradient button. `height` ~32 for normal, ~46 for hero.
pub fn button_sized(ui: &mut Ui, text: &str, kind: Btn, enabled: bool, height: f32) -> Response {
    let font = bold(if height > 40.0 { 17.0 } else { 14.0 });
    let galley = ui.painter().layout_no_wrap(text.to_string(), font, Color32::WHITE);
    let pad = if height > 40.0 { 28.0 } else { 16.0 };
    let size = Vec2::new(galley.size().x + pad * 2.0, height);
    let (rect, resp) = ui.allocate_exact_size(size, if enabled { Sense::click() } else { Sense::hover() });
    if ui.is_rect_visible(rect) {
        let (mut a, mut b, mut fg) = btn_colors(kind);
        if !enabled {
            a = a.gamma_multiply(0.35);
            b = b.gamma_multiply(0.35);
            fg = fg.gamma_multiply(0.5);
        } else if resp.is_pointer_button_down_on() {
            std::mem::swap(&mut a, &mut b);
        } else if resp.hovered() {
            a = lighten(a, 0.18);
            b = lighten(b, 0.18);
        }
        let p = ui.painter();
        vgradient(p, rect, a, b);
        p.rect_stroke(rect, CornerRadius::same(2), Stroke::new(1.0_f32, Color32::from_black_alpha(60)), StrokeKind::Inside);
        let pos = rect.center() - galley.size() / 2.0;
        p.galley(pos, galley, fg);
    }
    if enabled {
        resp.on_hover_cursor(egui::CursorIcon::PointingHand)
    } else {
        resp
    }
}

pub fn button(ui: &mut Ui, text: &str, kind: Btn) -> Response {
    button_sized(ui, text, kind, true, 32.0)
}

pub fn button_enabled(ui: &mut Ui, text: &str, kind: Btn, enabled: bool) -> Response {
    button_sized(ui, text, kind, enabled, 32.0)
}

pub fn big_button(ui: &mut Ui, text: &str, kind: Btn, enabled: bool) -> Response {
    button_sized(ui, text, kind, enabled, 46.0)
}

pub fn lighten(c: Color32, amount: f32) -> Color32 {
    let f = |v: u8| (v as f32 + (255.0 - v as f32) * amount).round() as u8;
    Color32::from_rgb(f(c.r()), f(c.g()), f(c.b()))
}

/// Small rounded pill label.
pub fn badge(ui: &mut Ui, text: &str, color: Color32) -> Response {
    let font = bold(11.5);
    let galley = ui.painter().layout_no_wrap(text.to_uppercase(), font, color);
    let size = galley.size() + Vec2::new(14.0, 6.0);
    let (rect, resp) = ui.allocate_exact_size(size, Sense::hover());
    let p = ui.painter();
    p.rect_filled(rect, CornerRadius::same(3), color.linear_multiply(0.16));
    p.rect_stroke(rect, CornerRadius::same(3), Stroke::new(1.0_f32, color.linear_multiply(0.55)), StrokeKind::Inside);
    p.galley(rect.center() - galley.size() / 2.0, galley, color);
    resp
}

/// Steam store tag: flat grey-blue pill.
pub fn tag(ui: &mut Ui, text: &str) {
    let galley = ui.painter().layout_no_wrap(text.to_string(), regular(12.0), ACCENT);
    let size = galley.size() + Vec2::new(12.0, 6.0);
    let (rect, _) = ui.allocate_exact_size(size, Sense::hover());
    ui.painter().rect_filled(rect, CornerRadius::same(2), Color32::from_rgba_unmultiplied(0x67, 0xc1, 0xf5, 0x22));
    ui.painter().galley(rect.center() - galley.size() / 2.0, galley, ACCENT);
}

/// Uppercase section header with a faint rule, like Steam's store sections.
pub fn section(ui: &mut Ui, text: &str) {
    ui.add_space(6.0);
    ui.label(RichText::new(text.to_uppercase()).font(bold(13.0)).color(TEXT_DIM));
    let (rect, _) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 1.0), Sense::hover());
    hgradient(ui.painter(), rect, ACCENT.linear_multiply(0.5), Color32::TRANSPARENT);
    ui.add_space(4.0);
}

pub fn heading(ui: &mut Ui, text: &str) {
    ui.label(RichText::new(text).font(bold(26.0)).color(TEXT_BRIGHT));
}

pub fn subheading(ui: &mut Ui, text: &str) {
    ui.label(RichText::new(text).font(regular(15.0)).color(TEXT_DIM));
}

pub fn title(ui: &mut Ui, text: &str) {
    ui.label(RichText::new(text).font(bold(17.0)).color(TEXT_BRIGHT));
}

pub fn dim(ui: &mut Ui, text: &str) -> Response {
    ui.label(RichText::new(text).color(TEXT_DIM))
}

/// A card that lightens on hover and outlines when selected. Returns the
/// card's response (click/hover) and the closure's value.
pub fn card<R>(ui: &mut Ui, id: impl std::hash::Hash, selected: bool, add: impl FnOnce(&mut Ui) -> R) -> (Response, R) {
    let id = ui.make_persistent_id(id);
    let hovered: bool = ui.ctx().data(|d| d.get_temp(id)).unwrap_or(false);
    let fill = if selected {
        PANEL_SEL
    } else if hovered {
        PANEL_HI
    } else {
        PANEL
    };
    let stroke = if selected { Stroke::new(2.0_f32, ACCENT) } else { Stroke::new(1.0_f32, BORDER) };
    let inner = egui::Frame::new()
        .fill(fill)
        .stroke(stroke)
        .corner_radius(CornerRadius::same(4))
        .inner_margin(Margin::same(14))
        .show(ui, |ui| ui.vertical(add).inner);
    let resp = ui.interact(inner.response.rect, id, Sense::click());
    ui.ctx().data_mut(|d| d.insert_temp(id, resp.hovered()));
    (resp.on_hover_cursor(egui::CursorIcon::PointingHand), inner.inner)
}

/// A non-interactive panel.
pub fn panel<R>(ui: &mut Ui, add: impl FnOnce(&mut Ui) -> R) -> R {
    egui::Frame::new()
        .fill(PANEL)
        .stroke(Stroke::new(1.0_f32, BORDER))
        .corner_radius(CornerRadius::same(4))
        .inner_margin(Margin::same(14))
        .show(ui, |ui| ui.vertical(add).inner)
        .inner
}

/// Coloured callout box (info / warning / error).
pub fn callout(ui: &mut Ui, color: Color32, icon: &str, text: &str) {
    egui::Frame::new()
        .fill(color.linear_multiply(0.10))
        .stroke(Stroke::new(1.0_f32, color.linear_multiply(0.45)))
        .corner_radius(CornerRadius::same(4))
        .inner_margin(Margin::symmetric(12, 9))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal_top(|ui| {
                ui.label(RichText::new(icon).color(color).font(bold(15.0)));
                ui.add(egui::Label::new(RichText::new(text).color(TEXT)).wrap());
            });
        });
}

/// iOS-style switch.
pub fn switch(ui: &mut Ui, on: &mut bool, enabled: bool) -> Response {
    let size = Vec2::new(42.0, 22.0);
    let (rect, mut resp) = ui.allocate_exact_size(size, if enabled { Sense::click() } else { Sense::hover() });
    if resp.clicked() {
        *on = !*on;
        resp.mark_changed();
    }
    let t = ui.ctx().animate_bool_responsive(resp.id, *on);
    let off_c = Color32::from_rgb(0x3a, 0x48, 0x5a);
    let mut track = lerp_color(off_c, ACCENT_STRONG, t);
    if !enabled {
        track = track.gamma_multiply(0.4);
    }
    let p = ui.painter();
    p.rect_filled(rect, CornerRadius::same(11), track);
    let x = egui::lerp(rect.left() + 11.0..=rect.right() - 11.0, t);
    p.circle_filled(Pos2::new(x, rect.center().y), 8.5, if enabled { Color32::WHITE } else { TEXT_DIM });
    resp
}

pub fn lerp_color(a: Color32, b: Color32, t: f32) -> Color32 {
    let l = |x: u8, y: u8| (x as f32 + (y as f32 - x as f32) * t).round() as u8;
    Color32::from_rgb(l(a.r(), b.r()), l(a.g(), b.g()), l(a.b(), b.b()))
}

/// Segmented control. `options`: (id, label, Err(reason) if unavailable).
/// Returns true when the selection changed.
pub fn segmented(ui: &mut Ui, options: &[(String, String, Result<(), String>, bool)], selected: &mut String) -> bool {
    let mut changed = false;
    ui.horizontal_wrapped(|ui| {
        ui.spacing_mut().item_spacing.x = 4.0;
        for (id, label, avail, recommended) in options {
            let is_sel = selected == id;
            let ok = avail.is_ok();
            let text = if *recommended { format!("{label}  ★") } else { label.clone() };
            let galley = ui.painter().layout_no_wrap(text, regular(14.0), TEXT_BRIGHT);
            let size = galley.size() + Vec2::new(24.0, 14.0);
            let (rect, resp) = ui.allocate_exact_size(size, if ok { Sense::click() } else { Sense::hover() });
            let fill = if is_sel {
                ACCENT_STRONG
            } else if !ok {
                Color32::from_rgb(0x1e, 0x29, 0x37)
            } else if resp.hovered() {
                Color32::from_rgb(0x36, 0x4b, 0x66)
            } else {
                Color32::from_rgb(0x2a, 0x3a, 0x4f)
            };
            let p = ui.painter();
            p.rect_filled(rect, CornerRadius::same(3), fill);
            let fg = if is_sel {
                Color32::WHITE
            } else if ok {
                TEXT
            } else {
                TEXT_DIM.gamma_multiply(0.6)
            };
            p.galley(rect.center() - galley.size() / 2.0, galley, fg);
            if ok && resp.clicked() && !is_sel {
                *selected = id.clone();
                changed = true;
            }
            match avail {
                Err(why) => {
                    resp.on_hover_text(format!("Unavailable: {why}"));
                }
                Ok(()) if *recommended => {
                    resp.on_hover_text("Recommended for your profile").on_hover_cursor(egui::CursorIcon::PointingHand);
                }
                Ok(()) => {
                    resp.on_hover_cursor(egui::CursorIcon::PointingHand);
                }
            }
        }
    });
    changed
}

/// Steam download-style progress bar.
pub fn progress(ui: &mut Ui, frac: f32, height: f32, color_a: Color32, color_b: Color32) {
    let (rect, _) = ui.allocate_exact_size(Vec2::new(ui.available_width(), height), Sense::hover());
    let p = ui.painter();
    p.rect_filled(rect, CornerRadius::same(2), Color32::from_rgb(0x0f, 0x16, 0x1f));
    let f = frac.clamp(0.0, 1.0);
    if f > 0.0 {
        let filled = Rect::from_min_size(rect.min, Vec2::new(rect.width() * f, rect.height()));
        hgradient(p, filled, color_a, color_b);
    }
    p.rect_stroke(rect, CornerRadius::same(2), Stroke::new(1.0_f32, BORDER), StrokeKind::Inside);
}

/// Status icon with label: 0 green ✔, 1 amber !, 2 red ✖, 3 blue ℹ (info).
pub fn status_line(ui: &mut Ui, level: u8, text: &str) {
    let (icon, color) = match level {
        0 => ("✔", GREEN),
        1 => ("!", WARN),
        3 => ("ℹ", ACCENT),
        _ => ("✖", DANGER),
    };
    ui.horizontal_top(|ui| {
        ui.label(RichText::new(icon).font(bold(14.0)).color(color));
        ui.add(egui::Label::new(text).wrap());
    });
}

/// Paint a big emoji/icon centred in a rect with a soft glow.
pub fn icon_capsule(ui: &mut Ui, rect: Rect, icon: &str, a: Color32, b: Color32) {
    let p = ui.painter();
    vgradient(p, rect, a, b);
    p.text(rect.center(), Align2::CENTER_CENTER, icon, regular(rect.height() * 0.42), Color32::from_white_alpha(230));
}

/// Two-column key/value grid row helper.
pub fn kv(ui: &mut Ui, key: &str, value: impl Into<egui::WidgetText>) {
    ui.label(RichText::new(key).color(TEXT_DIM));
    ui.add(egui::Label::new(value).wrap());
    ui.end_row();
}
