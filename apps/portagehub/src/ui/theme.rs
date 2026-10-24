//! Steam-inspired palette, fonts and egui style.

use egui::{Color32, CornerRadius, FontData, FontDefinitions, FontFamily, FontId, Margin, Stroke, TextStyle, Visuals};
use std::sync::Arc;

pub const NAV: Color32 = Color32::from_rgb(0x17, 0x1a, 0x21);
pub const BG: Color32 = Color32::from_rgb(0x1b, 0x28, 0x38);
pub const BG_DEEP: Color32 = Color32::from_rgb(0x12, 0x1a, 0x24);
pub const SIDEBAR: Color32 = Color32::from_rgb(0x1f, 0x26, 0x31);
pub const PANEL: Color32 = Color32::from_rgb(0x20, 0x2d, 0x3e);
pub const CARD: Color32 = Color32::from_rgb(0x16, 0x20, 0x2d);
pub const CARD_HOVER: Color32 = Color32::from_rgb(0x2a, 0x47, 0x5e);
pub const FIELD: Color32 = Color32::from_rgb(0x31, 0x3c, 0x4a);
pub const LINE: Color32 = Color32::from_rgb(0x2f, 0x3d, 0x4f);

pub const TEXT: Color32 = Color32::from_rgb(0xc7, 0xd5, 0xe0);
pub const TEXT_DIM: Color32 = Color32::from_rgb(0x8f, 0x98, 0xa0);
pub const TEXT_FAINT: Color32 = Color32::from_rgb(0x61, 0x6e, 0x7c);
pub const WHITE: Color32 = Color32::from_rgb(0xf5, 0xf8, 0xfa);

pub const ACCENT: Color32 = Color32::from_rgb(0x66, 0xc0, 0xf4);
pub const ACCENT_DIM: Color32 = Color32::from_rgb(0x41, 0x7a, 0x9b);
pub const GREEN: Color32 = Color32::from_rgb(0xa4, 0xd0, 0x07);
pub const GREEN_DARK: Color32 = Color32::from_rgb(0x4c, 0x6b, 0x22);
pub const GREEN_TEXT: Color32 = Color32::from_rgb(0xbe, 0xee, 0x11);
pub const ORANGE: Color32 = Color32::from_rgb(0xe8, 0xa3, 0x3d);
pub const RED: Color32 = Color32::from_rgb(0xe3, 0x5d, 0x5b);
pub const PURPLE: Color32 = Color32::from_rgb(0xb3, 0x8b, 0xf5);
pub const YELLOW: Color32 = Color32::from_rgb(0xf0, 0xd0, 0x5a);

/// Left-to-right gradient pairs for big buttons.
pub const GRAD_GREEN: (Color32, Color32) = (Color32::from_rgb(0x75, 0xb0, 0x22), Color32::from_rgb(0x58, 0x8a, 0x1b));
pub const GRAD_BLUE: (Color32, Color32) = (Color32::from_rgb(0x47, 0xbf, 0xff), Color32::from_rgb(0x1a, 0x44, 0xc2));
pub const GRAD_GREY: (Color32, Color32) = (Color32::from_rgb(0x3d, 0x4c, 0x5d), Color32::from_rgb(0x2a, 0x35, 0x43));
pub const GRAD_RED: (Color32, Color32) = (Color32::from_rgb(0xc8, 0x4b, 0x4b), Color32::from_rgb(0x8a, 0x22, 0x22));

pub fn bold() -> FontFamily {
    FontFamily::Name("bold".into())
}

pub fn medium() -> FontFamily {
    FontFamily::Name("medium".into())
}

pub fn light() -> FontFamily {
    FontFamily::Name("light".into())
}

fn fc_match(pattern: &str) -> Option<String> {
    let out = std::process::Command::new("fc-match").args(["-f", "%{file}", pattern]).output().ok()?;
    let p = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (!p.is_empty() && std::path::Path::new(&p).is_file()).then_some(p)
}

fn load_font(candidates: &[&str], fc: &str) -> Option<Vec<u8>> {
    for c in candidates {
        if let Ok(b) = std::fs::read(c) {
            return Some(b);
        }
    }
    fc_match(fc).and_then(|p| std::fs::read(p).ok())
}

pub fn install_fonts(ctx: &egui::Context, family_override: Option<&str>) {
    let mut fonts = FontDefinitions::default();
    let base = family_override.unwrap_or("Noto Sans");
    let noto = "/usr/share/fonts/noto/";
    let regular = load_font(&[&format!("{noto}NotoSans-Regular.ttf")], &format!("{base}:style=Regular"));
    let semibold = load_font(&[&format!("{noto}NotoSans-SemiBold.ttf"), &format!("{noto}NotoSans-Bold.ttf")], &format!("{base}:style=SemiBold"));
    let med = load_font(&[&format!("{noto}NotoSans-Medium.ttf")], &format!("{base}:style=Medium"));
    let lite = load_font(&[&format!("{noto}NotoSans-Light.ttf")], &format!("{base}:style=Light"));
    let mono = load_font(
        &[
            "/usr/share/fonts/jetbrains-mono/JetBrainsMonoNL-Regular.ttf",
            "/usr/share/fonts/hack/Hack-Regular.ttf",
            "/usr/share/fonts/jetbrains-mono/JetBrainsMono-Regular.ttf",
        ],
        "monospace",
    );

    let family_override_bytes = family_override.and_then(|f| fc_match(&format!("{f}:style=Regular"))).and_then(|p| std::fs::read(p).ok());
    let regular = family_override_bytes.or(regular);

    let mut put = |name: &str, bytes: Option<Vec<u8>>, family: FontFamily| {
        if let Some(b) = bytes {
            fonts.font_data.insert(name.into(), Arc::new(FontData::from_owned(b)));
            fonts.families.entry(family).or_default().insert(0, name.into());
        }
    };
    put("ui-regular", regular, FontFamily::Proportional);
    put("ui-mono", mono, FontFamily::Monospace);
    // Named families fall back to the proportional stack (emoji, symbols…).
    let prop = fonts.families.get(&FontFamily::Proportional).cloned().unwrap_or_default();
    for (name, bytes) in [("bold", semibold), ("medium", med), ("light", lite)] {
        let mut stack = prop.clone();
        if let Some(b) = bytes {
            fonts.font_data.insert(format!("ui-{name}"), Arc::new(FontData::from_owned(b)));
            stack.insert(0, format!("ui-{name}"));
        }
        fonts.families.insert(FontFamily::Name(name.into()), stack);
    }
    egui_phosphor::add_to_fonts(&mut fonts, egui_phosphor::Variant::Regular);
    // Noto Sans has no arrows/box symbols (U+2190…); fall back to a font that does.
    if let Some(b) = load_font(
        &[
            "/usr/share/fonts/liberation-fonts/LiberationSans-Regular.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/noto/NotoSansMono-Regular.ttf",
        ],
        "sans-serif:charset=2192",
    ) {
        fonts.font_data.insert("ui-symbols".into(), Arc::new(FontData::from_owned(b)));
        for fam in [FontFamily::Proportional, FontFamily::Name("bold".into()), FontFamily::Name("medium".into()), FontFamily::Name("light".into())] {
            if let Some(st) = fonts.families.get_mut(&fam) {
                st.push("ui-symbols".into());
            }
        }
    }
    // Icons inside bold/medium text too.
    for name in ["bold", "medium", "light"] {
        if let Some(st) = fonts.families.get_mut(&FontFamily::Name(name.into()))
            && !st.contains(&"phosphor".to_string())
        {
            let at = st.len().min(1);
            st.insert(at, "phosphor".into());
        }
    }
    if let Some(st) = fonts.families.get_mut(&FontFamily::Monospace)
        && !st.contains(&"phosphor".to_string())
    {
        st.push("phosphor".into());
    }
    ctx.set_fonts(fonts);
}

pub fn apply(ctx: &egui::Context) {
    ctx.set_theme(egui::Theme::Dark);
    ctx.style_mut_of(egui::Theme::Dark, |style| {
        style.text_styles = [
            (TextStyle::Heading, FontId::new(22.0, bold())),
            (TextStyle::Body, FontId::new(14.5, FontFamily::Proportional)),
            (TextStyle::Button, FontId::new(14.0, FontFamily::Proportional)),
            (TextStyle::Small, FontId::new(12.0, FontFamily::Proportional)),
            (TextStyle::Monospace, FontId::new(13.0, FontFamily::Monospace)),
        ]
        .into();
        let s = &mut style.spacing;
        s.item_spacing = egui::vec2(8.0, 6.0);
        s.button_padding = egui::vec2(10.0, 5.0);
        s.interact_size = egui::vec2(36.0, 26.0);
        s.window_margin = Margin::same(14);
        s.menu_margin = Margin::same(6);
        s.indent = 18.0;
        s.combo_width = 160.0;
        s.text_edit_width = 260.0;
        s.scroll = egui::style::ScrollStyle::thin();
        style.interaction.tooltip_delay = 0.25;

        let mut v = Visuals::dark();
        v.override_text_color = None;
        v.panel_fill = BG;
        v.window_fill = PANEL;
        v.window_stroke = Stroke::new(1.0, LINE);
        v.window_corner_radius = CornerRadius::same(6);
        v.menu_corner_radius = CornerRadius::same(4);
        v.extreme_bg_color = BG_DEEP;
        v.faint_bg_color = Color32::from_rgb(0x1e, 0x2c, 0x3d);
        v.code_bg_color = BG_DEEP;
        v.hyperlink_color = ACCENT;
        v.selection.bg_fill = Color32::from_rgb(0x2d, 0x5e, 0x85);
        v.selection.stroke = Stroke::new(1.0, WHITE);
        v.warn_fg_color = ORANGE;
        v.error_fg_color = RED;
        v.window_shadow = egui::epaint::Shadow { offset: [0, 8], blur: 24, spread: 0, color: Color32::from_black_alpha(140) };
        v.popup_shadow = egui::epaint::Shadow { offset: [0, 4], blur: 14, spread: 0, color: Color32::from_black_alpha(120) };

        let r = CornerRadius::same(3);
        v.widgets.noninteractive.bg_fill = PANEL;
        v.widgets.noninteractive.weak_bg_fill = PANEL;
        v.widgets.noninteractive.bg_stroke = Stroke::new(1.0, LINE);
        v.widgets.noninteractive.fg_stroke = Stroke::new(1.0, TEXT);
        v.widgets.noninteractive.corner_radius = r;

        v.widgets.inactive.bg_fill = FIELD;
        v.widgets.inactive.weak_bg_fill = Color32::from_rgb(0x2a, 0x37, 0x47);
        v.widgets.inactive.bg_stroke = Stroke::NONE;
        v.widgets.inactive.fg_stroke = Stroke::new(1.0, TEXT);
        v.widgets.inactive.corner_radius = r;

        v.widgets.hovered.bg_fill = Color32::from_rgb(0x3d, 0x6c, 0x8d);
        v.widgets.hovered.weak_bg_fill = Color32::from_rgb(0x3d, 0x6c, 0x8d);
        v.widgets.hovered.bg_stroke = Stroke::new(1.0, ACCENT_DIM);
        v.widgets.hovered.fg_stroke = Stroke::new(1.5, WHITE);
        v.widgets.hovered.corner_radius = r;

        v.widgets.active.bg_fill = Color32::from_rgb(0x47, 0x8b, 0xbf);
        v.widgets.active.weak_bg_fill = Color32::from_rgb(0x47, 0x8b, 0xbf);
        v.widgets.active.bg_stroke = Stroke::new(1.0, ACCENT);
        v.widgets.active.fg_stroke = Stroke::new(1.5, WHITE);
        v.widgets.active.corner_radius = r;

        v.widgets.open.bg_fill = Color32::from_rgb(0x2a, 0x47, 0x5e);
        v.widgets.open.weak_bg_fill = Color32::from_rgb(0x2a, 0x47, 0x5e);
        v.widgets.open.fg_stroke = Stroke::new(1.0, WHITE);
        v.widgets.open.corner_radius = r;
        style.visuals = v;
    });
}
