//! Preferences and system information.

use super::common;
use crate::app::{Action, App};
use crate::exec::HelperState;
use crate::ui::theme::*;
use crate::ui::widgets as w;
use egui::{RichText, Ui};
use egui_phosphor::regular as icon;

#[derive(Default)]
pub struct State {
    pub font: String,
}

pub fn show(app: &mut App, ui: &mut Ui) {
    common::page(ui, "settings", |ui| {
        w::title(ui, "Settings");
        ui.add_space(6.0);
        w::section(ui, "Behaviour");
        w::card().show(ui, |ui| {
            ui.set_width(ui.available_width().min(820.0));
            let s = &mut app.settings;
            w::switch_row(ui, &mut s.beginner_tips, "Show explanations and tips", "Plain-language help boxes and per-entry descriptions throughout the app.");
            w::switch_row(ui, &mut s.show_advanced, "Show advanced options", "Expert emerge options, profile overrides, dev profiles and more.");
            w::switch_row(ui, &mut s.review_writes, "Review changes before writing", "Show a diff of every /etc/portage change before it is saved.");
            w::switch_row(ui, &mut s.backup_on_write, "Keep a backup of replaced files", "Saves file~ next to the original (Portage ignores ~ files).");
            w::switch_row(ui, &mut s.auto_repretend, "Re-check automatically after applying fixes", "After writing emerge's suggested changes, run the preview again.");
            w::switch_row(ui, &mut s.auto_world_check, "Check updates with emerge in the background", "After start-up (when the last check is older than your last sync or 6 hours), after syncs and after emerges, run emerge -uDN @world --pretend at low priority so the Store shows exactly what a world update will do.");
            w::switch_row(ui, &mut s.pretend_as_root, "Run previews as root", "Normally previews run as your user (no password). Enable if your setup needs root to resolve dependencies.");
            ui.horizontal(|ui| {
                ui.label("File name for entries PortageHub writes");
                w::help(ui, "Inside package.* directories new lines go to this file, so your own files stay untouched.");
                ui.add(egui::TextEdit::singleline(&mut s.target_file).desired_width(180.0));
            });
        });
        w::section(ui, "Appearance");
        w::card().show(ui, |ui| {
            ui.set_width(ui.available_width().min(820.0));
            ui.horizontal(|ui| {
                ui.label("Interface scale");
                let mut z = app.settings.ui_scale;
                if ui.add(egui::Slider::new(&mut z, 0.75..=2.0).step_by(0.05)).changed() {
                    app.settings.ui_scale = z;
                }
                if ui.button("Apply").clicked() {
                    ui.ctx().set_zoom_factor(app.settings.ui_scale);
                }
            });
            ui.horizontal(|ui| {
                ui.label("Interface font");
                if app.settings_view.font.is_empty() {
                    app.settings_view.font = app.settings.font_family.clone().unwrap_or_default();
                }
                ui.add(egui::TextEdit::singleline(&mut app.settings_view.font).hint_text("Noto Sans (default)").desired_width(220.0));
                if ui.button("Apply").clicked() {
                    let f = app.settings_view.font.trim().to_string();
                    app.settings.font_family = if f.is_empty() { None } else { Some(f) };
                }
                ui.label(w::dim("Any installed font family name (fc-list)."));
            });
            w::switch_row(ui, &mut app.settings.log_wrap, "Wrap long lines in logs", "");
        });
        w::section(ui, "Administrator access");
        w::card().show(ui, |ui| {
            ui.set_width(ui.available_width().min(820.0));
            let st = if app.exec.is_root {
                "PortageHub runs as root.".to_string()
            } else {
                match &app.exec.helper_state {
                    HelperState::Ready => "Admin session active (portagehub-helper via pkexec).".into(),
                    HelperState::Starting => "Waiting for authentication…".into(),
                    HelperState::NotStarted => "Not started — you'll be asked for your password the first time something needs root.".into(),
                    HelperState::Failed(e) => format!("Failed: {e}"),
                }
            };
            ui.label(st);
            ui.label(w::dim(format!(
                "Helper binary: {}",
                app.exec.helper_path.as_ref().map(|p| p.display().to_string()).unwrap_or_else(|| "not found".into())
            )));
            ui.label(w::dim("The helper only runs Portage tools (emerge, emaint, eselect, …), writes files only below /etc/portage, and otherwise only accepts or discards ._cfgXXXX_ configuration updates below /etc."));
            ui.horizontal(|ui| {
                if !app.exec.root_ready() && w::primary(ui, icon::LOCK_OPEN, "Start admin session now").clicked() {
                    app.push(Action::StartHelper);
                }
                if app.exec.helper_state == HelperState::Ready && !app.exec.is_root && w::button(ui, icon::LOCK, "End admin session").clicked() {
                    app.exec.stop_helper();
                }
            });
        });
        w::section(ui, "System");
        if let Some(env) = app.data.env.clone() {
            egui::Grid::new("sysinfo").num_columns(2).striped(true).spacing([16.0, 6.0]).show(ui, |ui| {
                ui.label(w::dim("Portage"));
                ui.label(&env.portage_version);
                ui.end_row();
                for k in ["ARCH", "CHOST", "ACCEPT_KEYWORDS", "MAKEOPTS", "EMERGE_DEFAULT_OPTS", "FEATURES", "PORTAGE_TMPDIR", "DISTDIR", "PKGDIR"] {
                    ui.label(RichText::new(k).monospace().color(ACCENT));
                    ui.add(egui::Label::new(RichText::new(env.get(k)).monospace().size(12.0)).wrap());
                    ui.end_row();
                }
                if let Ok(link) = std::fs::read_link("/etc/portage/make.profile") {
                    ui.label(RichText::new("profile").monospace().color(ACCENT));
                    ui.label(link.display().to_string());
                    ui.end_row();
                }
            });
        }
        if let Some(c) = &app.data.catalog {
            ui.label(w::dim(format!("Package tree scanned in {} ms.", c.scan_ms)));
        }
        ui.add_space(10.0);
        ui.label(w::dim(format!("PortageHub {} · settings in ~/.config/portagehub/settings.json", env!("CARGO_PKG_VERSION"))));
        ui.horizontal(|ui| {
            w::link(ui, "Gentoo Handbook", "https://wiki.gentoo.org/wiki/Handbook:AMD64");
            w::link(ui, "Portage wiki", "https://wiki.gentoo.org/wiki/Portage");
            w::link(ui, "emerge(1)", "https://dev.gentoo.org/~zmedico/portage/doc/man/emerge.1.html");
        });
    });
}
