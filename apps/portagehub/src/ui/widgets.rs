//! Custom widgets: switches, tri-state toggles, segmented radios, gradient
//! buttons, badges, cards, progress bars …

use super::theme::{self, *};
use egui::{Align2, Color32, CornerRadius, FontId, Frame, Margin, Pos2, Rect, Response, RichText, Sense, Stroke, Ui, WidgetInfo, WidgetType, epaint::Mesh};
use egui_phosphor::regular as icon;

pub fn paint_hgradient(painter: &egui::Painter, rect: Rect, left: Color32, right: Color32) {
    let mut mesh = Mesh::default();
    mesh.colored_vertex(rect.left_top(), left);
    mesh.colored_vertex(rect.right_top(), right);
    mesh.colored_vertex(rect.left_bottom(), left);
    mesh.colored_vertex(rect.right_bottom(), right);
    mesh.add_triangle(0, 1, 2);
    mesh.add_triangle(1, 3, 2);
    painter.add(mesh);
}

pub fn paint_vgradient(painter: &egui::Painter, rect: Rect, top: Color32, bottom: Color32) {
    let mut mesh = Mesh::default();
    mesh.colored_vertex(rect.left_top(), top);
    mesh.colored_vertex(rect.right_top(), top);
    mesh.colored_vertex(rect.left_bottom(), bottom);
    mesh.colored_vertex(rect.right_bottom(), bottom);
    mesh.add_triangle(0, 1, 2);
    mesh.add_triangle(1, 3, 2);
    painter.add(mesh);
}

/// iOS/Steam-deck style on/off switch.
pub fn toggle(ui: &mut Ui, on: &mut bool) -> Response {
    let size = ui.spacing().interact_size.y * egui::vec2(1.9, 0.85);
    let (rect, mut resp) = ui.allocate_exact_size(size, Sense::click());
    if resp.clicked() {
        *on = !*on;
        resp.mark_changed();
    }
    resp.widget_info(|| WidgetInfo::selected(WidgetType::Checkbox, ui.is_enabled(), *on, ""));
    if ui.is_rect_visible(rect) {
        let t = ui.ctx().animate_bool_responsive(resp.id, *on);
        let radius = 0.5 * rect.height();
        let off_col = if resp.hovered() { Color32::from_rgb(0x4a, 0x58, 0x6a) } else { Color32::from_rgb(0x3a, 0x46, 0x55) };
        let bg = lerp_color(off_col, Color32::from_rgb(0x1a, 0x9f, 0xff), t);
        let bg = if ui.is_enabled() { bg } else { bg.gamma_multiply(0.5) };
        ui.painter().rect_filled(rect, radius, bg);
        let cx = egui::lerp((rect.left() + radius)..=(rect.right() - radius), t);
        ui.painter().circle_filled(Pos2::new(cx, rect.center().y), radius * 0.78, if ui.is_enabled() { WHITE } else { TEXT_DIM });
    }
    resp
}

pub fn lerp_color(a: Color32, b: Color32, t: f32) -> Color32 {
    let l = |x: u8, y: u8| (x as f32 + (y as f32 - x as f32) * t).round() as u8;
    Color32::from_rgb(l(a.r(), b.r()), l(a.g(), b.g()), l(a.b(), b.b()))
}

/// Small "?" with a tooltip, for beginner explanations.
pub fn help(ui: &mut Ui, text: &str) -> Response {
    ui.add(egui::Label::new(RichText::new(icon::QUESTION).color(TEXT_FAINT).size(14.0)).sense(Sense::hover())).on_hover_ui(|ui| {
        ui.set_max_width(340.0);
        ui.label(text);
    })
}

/// A full-width settings row: label (+ help) on the left, switch on the right.
pub fn switch_row(ui: &mut Ui, on: &mut bool, label: &str, hint: &str) -> Response {
    ui.horizontal(|ui| {
        ui.set_min_height(28.0);
        let r = ui.label(RichText::new(label).color(if *on { WHITE } else { TEXT }));
        if !hint.is_empty() {
            help(ui, hint);
        }
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| toggle(ui, on)).inner | r
    })
    .inner
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tri {
    Default,
    On,
    Off,
}

/// Three-way control: profile default / force on / force off.
pub fn tri(ui: &mut Ui, v: &mut Tri, default_on: Option<bool>) -> Response {
    let def_label = match default_on {
        Some(true) => format!("{} Default", icon::CHECK),
        Some(false) => format!("{} Default", icon::MINUS),
        None => "Default".to_string(),
    };
    segmented(ui, v, &[(Tri::Default, def_label.as_str()), (Tri::On, "On"), (Tri::Off, "Off")])
}

/// Pill-shaped radio group.
pub fn segmented<T: PartialEq + Copy>(ui: &mut Ui, value: &mut T, options: &[(T, &str)]) -> Response {
    let font = FontId::new(12.5, theme::medium());
    let pad = egui::vec2(10.0, 4.0);
    let galleys: Vec<_> = options.iter().map(|(_, l)| ui.painter().layout_no_wrap(l.to_string(), font.clone(), TEXT)).collect();
    let h = 24.0;
    let w: f32 = galleys.iter().map(|g| g.size().x + pad.x * 2.0).sum();
    let (rect, mut resp) = ui.allocate_exact_size(egui::vec2(w, h), Sense::hover());
    let painter = ui.painter_at(rect.expand(1.0));
    painter.rect_filled(rect, CornerRadius::same(4), BG_DEEP);
    let mut x = rect.left();
    for (i, ((val, _), g)) in options.iter().zip(galleys).enumerate() {
        let seg = Rect::from_min_size(Pos2::new(x, rect.top()), egui::vec2(g.size().x + pad.x * 2.0, h));
        x += seg.width();
        let id = resp.id.with(i);
        let r = ui.interact(seg, id, Sense::click());
        let selected = *value == *val;
        if r.clicked() && !selected {
            *value = *val;
            resp.mark_changed();
        }
        let fill = if selected {
            Color32::from_rgb(0x2f, 0x6f, 0xa3)
        } else if r.hovered() {
            Color32::from_rgb(0x2a, 0x37, 0x47)
        } else {
            Color32::TRANSPARENT
        };
        painter.rect_filled(seg.shrink(2.0), CornerRadius::same(3), fill);
        let col = if selected {
            WHITE
        } else if r.hovered() {
            TEXT
        } else {
            TEXT_DIM
        };
        painter.galley(seg.center() - g.size() / 2.0, g, col);
    }
    resp
}

/// Big Steam-style gradient call-to-action button.
pub fn big_button(ui: &mut Ui, text: &str, grad: (Color32, Color32), min_width: f32) -> Response {
    let font = FontId::new(16.0, theme::bold());
    let galley = ui.painter().layout_no_wrap(text.to_string(), font, WHITE);
    let size = egui::vec2((galley.size().x + 40.0).max(min_width), 40.0);
    let (rect, resp) = ui.allocate_exact_size(size, Sense::click());
    if ui.is_rect_visible(rect) {
        let enabled = ui.is_enabled();
        let (mut a, mut b) = grad;
        if resp.hovered() && enabled {
            a = lerp_color(a, WHITE, 0.15);
            b = lerp_color(b, WHITE, 0.15);
        }
        if !enabled {
            a = a.gamma_multiply(0.35);
            b = b.gamma_multiply(0.35);
        }
        let p = ui.painter();
        p.rect_filled(rect.translate(egui::vec2(0.0, 2.0)), CornerRadius::same(3), Color32::from_black_alpha(80));
        paint_hgradient(p, rect, a, b);
        p.galley(rect.center() - galley.size() / 2.0, galley, if enabled { WHITE } else { TEXT_DIM });
    }
    resp
}

/// Compact flat button with optional icon.
pub fn button(ui: &mut Ui, icon_s: &str, text: &str) -> Response {
    let label = if icon_s.is_empty() {
        text.to_string()
    } else if text.is_empty() {
        icon_s.to_string()
    } else {
        format!("{icon_s}  {text}")
    };
    ui.add(egui::Button::new(RichText::new(label).color(TEXT)).min_size(egui::vec2(0.0, 28.0)))
}

/// Accent button (blue) for primary actions inside cards.
pub fn primary(ui: &mut Ui, icon_s: &str, text: &str) -> Response {
    let label = if icon_s.is_empty() { text.to_string() } else { format!("{icon_s}  {text}") };
    ui.add(
        egui::Button::new(RichText::new(label).color(WHITE).family(theme::medium())).fill(Color32::from_rgb(0x1f, 0x6f, 0xb2)).min_size(egui::vec2(0.0, 28.0)),
    )
}

pub fn danger(ui: &mut Ui, icon_s: &str, text: &str) -> Response {
    let label = if icon_s.is_empty() { text.to_string() } else { format!("{icon_s}  {text}") };
    ui.add(egui::Button::new(RichText::new(label).color(WHITE)).fill(Color32::from_rgb(0x8a, 0x2c, 0x2c)).min_size(egui::vec2(0.0, 28.0)))
}

pub fn icon_button(ui: &mut Ui, icon_s: &str, tooltip: &str) -> Response {
    ui.add(egui::Button::new(RichText::new(icon_s).size(16.0).color(TEXT)).frame(false).min_size(egui::vec2(26.0, 26.0))).on_hover_text(tooltip)
}

pub fn badge(ui: &mut Ui, text: &str, fg: Color32, bg: Color32) -> Response {
    let font = FontId::new(11.0, theme::bold());
    let g = ui.painter().layout_no_wrap(text.to_string(), font, fg);
    let size = g.size() + egui::vec2(12.0, 5.0);
    let (rect, resp) = ui.allocate_exact_size(size, Sense::hover());
    ui.painter().rect_filled(rect, CornerRadius::same(3), bg);
    ui.painter().galley(rect.center() - g.size() / 2.0, g, fg);
    resp
}

pub fn status_badge(ui: &mut Ui, label: &str) -> Response {
    let (fg, bg) = match label {
        "NEW" | "INSTALLED" | "STABLE" | "DONE" => (GREEN_TEXT, GREEN_DARK),
        "UPDATE" | "NEW SLOT" | "BUILDING" | "BINARY" | "INSTALLING" | "IN @WORLD UPDATE" => (WHITE, Color32::from_rgb(0x1f, 0x5f, 0x99)),
        "DOWNGRADE" | "TESTING" | "WARNING" | "MASKED?" | "HELD BACK" => (Color32::from_rgb(0x2b, 0x1a, 0x00), ORANGE),
        "REBUILD" | "QUEUED" | "LIVE" | "IN TREE" => (TEXT, Color32::from_rgb(0x3a, 0x46, 0x55)),
        "BLOCKED" | "FAILED" | "REMOVE" | "MASKED" | "ERROR" | "SKIPPED" => (WHITE, Color32::from_rgb(0xa3, 0x2e, 0x2e)),
        "BLOCKER" | "WORLD" => (WHITE, Color32::from_rgb(0x6b, 0x4f, 0xa8)),
        _ => (TEXT, Color32::from_rgb(0x3a, 0x46, 0x55)),
    };
    badge(ui, label, fg, bg)
}

/// Uppercase section heading with a faint rule, like Steam's library.
pub fn section(ui: &mut Ui, title: &str) {
    ui.add_space(6.0);
    ui.horizontal(|ui| {
        ui.label(RichText::new(title.to_uppercase()).size(12.0).family(theme::bold()).color(TEXT_DIM));
        let rest = ui.available_rect_before_wrap();
        let y = rest.center().y;
        ui.painter().hline(rest.left() + 4.0..=rest.right(), y, Stroke::new(1.0, LINE));
    });
    ui.add_space(2.0);
}

pub fn card() -> Frame {
    Frame::new().fill(CARD).corner_radius(CornerRadius::same(4)).inner_margin(Margin::same(12)).stroke(Stroke::new(1.0, Color32::from_rgb(0x22, 0x30, 0x41)))
}

pub fn card_tinted(tint: Color32) -> Frame {
    Frame::new()
        .fill(lerp_color(CARD, tint, 0.10))
        .corner_radius(CornerRadius::same(4))
        .inner_margin(Margin::same(12))
        .stroke(Stroke::new(1.0, lerp_color(LINE, tint, 0.45)))
}

pub fn search_box(ui: &mut Ui, text: &mut String, hint: &str, width: f32) -> Response {
    let frame = Frame::new().fill(FIELD).corner_radius(CornerRadius::same(3)).inner_margin(Margin::symmetric(8, 3));
    frame
        .show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new(icon::MAGNIFYING_GLASS).color(TEXT_DIM));
                let r = ui.add(egui::TextEdit::singleline(text).hint_text(hint).frame(egui::Frame::NONE).desired_width(width - 60.0));
                if !text.is_empty() && ui.add(egui::Button::new(RichText::new(icon::X).color(TEXT_DIM)).frame(false)).clicked() {
                    text.clear();
                }
                r
            })
            .inner
        })
        .inner
}

/// Download-style progress bar with gradient fill and centred text.
pub fn progress(ui: &mut Ui, fraction: f32, height: f32, text: &str, animate: bool) -> Response {
    let w = ui.available_width();
    let (rect, resp) = ui.allocate_exact_size(egui::vec2(w, height), Sense::hover());
    let p = ui.painter();
    p.rect_filled(rect, CornerRadius::same(2), Color32::from_rgb(0x0e, 0x14, 0x1b));
    let f = fraction.clamp(0.0, 1.0);
    if f > 0.0 {
        let fill = Rect::from_min_size(rect.min, egui::vec2(rect.width() * f, rect.height()));
        paint_hgradient(p, fill, Color32::from_rgb(0x1a, 0x9f, 0xff), Color32::from_rgb(0x47, 0xbf, 0xff));
        if animate {
            // Moving sheen while work is ongoing.
            let t = ui.input(|i| i.time) as f32;
            let sx = fill.left() + ((t * 0.6).fract() * (fill.width() + 80.0)) - 40.0;
            let sheen = Rect::from_min_max(Pos2::new(sx.max(fill.left()), fill.top()), Pos2::new((sx + 40.0).min(fill.right()), fill.bottom()));
            if sheen.width() > 0.0 {
                p.rect_filled(sheen, 0.0, Color32::from_white_alpha(28));
            }
            ui.ctx().request_repaint_after(std::time::Duration::from_millis(33));
        }
    }
    if !text.is_empty() {
        let font = FontId::new((height * 0.62).clamp(10.0, 13.0), theme::medium());
        p.text(rect.center() + egui::vec2(1.0, 1.0), Align2::CENTER_CENTER, text, font.clone(), Color32::from_black_alpha(200));
        p.text(rect.center(), Align2::CENTER_CENTER, text, font, WHITE);
    }
    resp
}

/// Steam's "LAST PLAYED / PLAY TIME" style stat.
pub fn stat(ui: &mut Ui, label: &str, value: &str) {
    ui.vertical(|ui| {
        ui.spacing_mut().item_spacing.y = 1.0;
        ui.label(RichText::new(label.to_uppercase()).size(11.0).family(theme::bold()).color(TEXT_DIM));
        ui.label(RichText::new(value).size(14.0).color(WHITE));
    });
}

/// Top navigation tab (large caps text, accent underline when selected).
pub fn nav_tab(ui: &mut Ui, text: &str, selected: bool) -> Response {
    let font = FontId::new(15.5, theme::bold());
    let col = if selected { ACCENT } else { TEXT };
    let g = ui.painter().layout_no_wrap(text.to_uppercase(), font, col);
    let size = g.size() + egui::vec2(18.0, 16.0);
    let (rect, resp) = ui.allocate_exact_size(size, Sense::click());
    let col = if selected {
        ACCENT
    } else if resp.hovered() {
        WHITE
    } else {
        TEXT
    };
    ui.painter().galley_with_override_text_color(rect.center() - g.size() / 2.0, g, col);
    if selected {
        let y = rect.bottom() - 2.0;
        ui.painter().hline(rect.left() + 9.0..=rect.right() - 9.0, y, Stroke::new(2.0, ACCENT));
    }
    resp
}

/// Sidebar row: icon, label, optional count.
pub fn side_item(ui: &mut Ui, icon_s: &str, text: &str, selected: bool, count: Option<usize>) -> Response {
    let w = ui.available_width();
    let (rect, resp) = ui.allocate_exact_size(egui::vec2(w, 30.0), Sense::click());
    let p = ui.painter();
    if selected {
        paint_hgradient(p, rect, Color32::from_rgb(0x3e, 0x6e, 0x95), Color32::from_rgb(0x2a, 0x47, 0x5e));
    } else if resp.hovered() {
        p.rect_filled(rect, 0.0, Color32::from_rgb(0x2a, 0x34, 0x42));
    }
    let col = if selected || resp.hovered() { WHITE } else { TEXT };
    p.text(Pos2::new(rect.left() + 12.0, rect.center().y), Align2::LEFT_CENTER, icon_s, FontId::proportional(15.0), if selected { WHITE } else { ACCENT_DIM });
    let max_text = rect.width() - 50.0 - if count.is_some() { 40.0 } else { 0.0 };
    let g = ui.painter().layout(text.to_string(), FontId::proportional(13.5), col, max_text);
    let g = if g.rows.len() > 1 {
        let mut job = egui::text::LayoutJob::simple_singleline(text.to_string(), FontId::proportional(13.5), col);
        job.wrap = egui::text::TextWrapping::truncate_at_width(max_text);
        ui.painter().layout_job(job)
    } else {
        g
    };
    p.galley(Pos2::new(rect.left() + 34.0, rect.center().y - g.size().y / 2.0), g, col);
    if let Some(c) = count {
        p.text(Pos2::new(rect.right() - 10.0, rect.center().y), Align2::RIGHT_CENTER, c.to_string(), FontId::proportional(12.0), TEXT_DIM);
    }
    resp
}

/// Toggleable chip, used for flag lists.
pub fn chip(ui: &mut Ui, text: &str, selected: bool) -> Response {
    let font = FontId::new(12.5, FontFamily::Proportional);
    let col = if selected { WHITE } else { TEXT };
    let g = ui.painter().layout_no_wrap(text.to_string(), font, col);
    let size = g.size() + egui::vec2(16.0, 8.0);
    let (rect, resp) = ui.allocate_exact_size(size, Sense::click());
    let fill = match (selected, resp.hovered()) {
        (true, true) => Color32::from_rgb(0x3a, 0x86, 0xc4),
        (true, false) => Color32::from_rgb(0x2f, 0x6f, 0xa3),
        (false, true) => Color32::from_rgb(0x3a, 0x46, 0x55),
        (false, false) => Color32::from_rgb(0x2a, 0x35, 0x43),
    };
    ui.painter().rect_filled(rect, CornerRadius::same(12), fill);
    ui.painter().galley(rect.center() - g.size() / 2.0, g, col);
    resp
}

use egui::FontFamily;

/// Flag chip coloured by state: green on, red off, grey default.
pub fn flag_chip(ui: &mut Ui, name: &str, on: bool, changed: bool) -> Response {
    let text = if on { name.to_string() } else { format!("-{name}") };
    let (fg, bg) =
        if on { (GREEN_TEXT, Color32::from_rgb(0x2d, 0x40, 0x16)) } else { (Color32::from_rgb(0xe8, 0x9a, 0x9a), Color32::from_rgb(0x40, 0x22, 0x22)) };
    let font = FontId::new(12.0, FontFamily::Monospace);
    let label = if changed { format!("{text}*") } else { text };
    let g = ui.painter().layout_no_wrap(label, font, fg);
    let size = g.size() + egui::vec2(12.0, 6.0);
    let (rect, resp) = ui.allocate_exact_size(size, Sense::hover());
    ui.painter().rect_filled(rect, CornerRadius::same(3), bg);
    if changed {
        ui.painter().rect_stroke(rect, CornerRadius::same(3), Stroke::new(1.0, YELLOW), egui::StrokeKind::Inside);
    }
    ui.painter().galley(rect.center() - g.size() / 2.0, g, fg);
    resp
}

pub fn empty_state(ui: &mut Ui, icon_s: &str, title: &str, text: &str) {
    ui.vertical_centered(|ui| {
        ui.add_space(40.0);
        ui.label(RichText::new(icon_s).size(54.0).color(TEXT_FAINT));
        ui.add_space(6.0);
        ui.label(RichText::new(title).size(18.0).family(theme::bold()).color(TEXT));
        ui.add_space(2.0);
        ui.label(RichText::new(text).color(TEXT_DIM));
        ui.add_space(20.0);
    });
}

pub fn loading(ui: &mut Ui, text: &str) {
    ui.vertical_centered(|ui| {
        ui.add_space(50.0);
        ui.add(egui::Spinner::new().size(28.0).color(ACCENT));
        ui.add_space(8.0);
        ui.label(RichText::new(text).color(TEXT_DIM));
    });
}

/// Monospace, selectable command line display with a copy button.
pub fn command_preview(ui: &mut Ui, cmd: &str) {
    Frame::new().fill(BG_DEEP).corner_radius(CornerRadius::same(3)).inner_margin(Margin::symmetric(10, 6)).show(ui, |ui| {
        ui.horizontal(|ui| {
            ui.label(RichText::new(icon::TERMINAL).color(GREEN));
            ui.add(egui::Label::new(RichText::new(cmd).monospace().color(Color32::from_rgb(0xd7, 0xe7, 0xb4))).wrap());
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if icon_button(ui, icon::COPY, "Copy command").clicked() {
                    ui.ctx().copy_text(cmd.to_string());
                }
            });
        });
    });
}

pub fn title(ui: &mut Ui, text: &str) {
    ui.label(RichText::new(text).size(24.0).family(theme::bold()).color(WHITE));
}

pub fn subtitle(ui: &mut Ui, text: &str) {
    ui.label(RichText::new(text).color(TEXT_DIM));
}

/// Coloured callout box.
pub fn callout(ui: &mut Ui, tint: Color32, icon_s: &str, text: &str) {
    card_tinted(tint).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            ui.label(RichText::new(icon_s).size(18.0).color(tint));
            ui.add(egui::Label::new(RichText::new(text).color(TEXT)).wrap());
        });
    });
}

pub fn dim(text: impl Into<String>) -> RichText {
    RichText::new(text.into()).color(TEXT_DIM)
}

pub fn strong(text: impl Into<String>) -> RichText {
    RichText::new(text.into()).color(WHITE).family(theme::medium())
}

pub fn mono(text: impl Into<String>) -> RichText {
    RichText::new(text.into()).monospace().color(TEXT)
}

pub fn link(ui: &mut Ui, text: &str, url: &str) -> Response {
    let r = ui.add(egui::Label::new(RichText::new(text).color(ACCENT)).sense(Sense::click())).on_hover_text(url);
    if r.clicked() {
        crate::portage::system::open_url(url);
    }
    r.on_hover_cursor(egui::CursorIcon::PointingHand)
}
