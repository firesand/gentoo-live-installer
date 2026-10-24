//! Guided make.conf: what this machine is, the health check with one-click
//! fixes, and the compiler flags card. Every change goes through the normal
//! review → transaction → undo path; make.conf changes are additionally
//! checked by Portage's own parser before and after writing.

use crate::app::{Action, App, FileChange};
use crate::data::Reload;
use crate::portage::makeconf::{self, MakeConf};
use crate::portage::makeconf_check::{Finding, Severity};
use crate::portage::makeconf_validate;
use crate::portage::tuning;
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Color32, RichText, Ui};
use egui_phosphor::regular as icon;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

/// A background "what would Portage make of this" run: candidate text → result.
pub type PreviewSlot = Arc<Mutex<Option<Result<BTreeMap<String, String>, String>>>>;

pub const RELOAD_AFTER_FIX: [Reload; 4] = [Reload::MakeConf, Reload::Env, Reload::Config, Reload::Health];

fn severity_colors(s: Severity) -> (Color32, Color32) {
    match s {
        Severity::Critical => (WHITE, Color32::from_rgb(0xa3, 0x2e, 0x2e)),
        Severity::High => (Color32::from_rgb(0x2b, 0x1a, 0x00), ORANGE),
        Severity::Medium => (Color32::from_rgb(0x2b, 0x24, 0x00), YELLOW),
        Severity::Info => (TEXT, Color32::from_rgb(0x3a, 0x46, 0x55)),
    }
}

/// Load what the page needs the first time it is shown.
pub fn ensure_loaded(app: &mut App) {
    if app.data.tuning.is_none() && !app.is_loading(Reload::Tuning) {
        app.push(Action::Reload(Reload::Tuning));
    }
    if app.data.health.is_none() && !app.is_loading(Reload::Health) && app.data.makeconf.is_some() {
        app.push(Action::Reload(Reload::Health));
    }
}

pub fn machine_card(app: &mut App, ui: &mut Ui) {
    w::section(ui, "This machine");
    w::card().show(ui, |ui| {
        ui.set_width(ui.available_width());
        let Some(t) = app.data.tuning.clone() else {
            ui.horizontal(|ui| {
                ui.add(egui::Spinner::new().size(14.0));
                ui.label(w::dim("Detecting CPU, memory, GPU and compiler…"));
            });
            return;
        };
        let s = tuning::suggest_makeopts(t.hw.threads, t.hw.mem_kib);
        ui.horizontal_wrapped(|ui| {
            w::stat(ui, "CPU", if t.hw.cpu_model.is_empty() { "unknown" } else { &t.hw.cpu_model });
            ui.add_space(18.0);
            w::stat(ui, "Threads", &t.hw.threads.to_string());
            ui.add_space(18.0);
            w::stat(ui, "Memory", &format!("{:.1} GiB", t.hw.mem_gib()));
            ui.add_space(18.0);
            w::stat(ui, "GPU", &if t.hw.gpus.is_empty() { "none detected".into() } else { t.hw.gpus.join(", ") });
            ui.add_space(18.0);
            w::stat(ui, "GCC", if t.gcc_version.is_empty() { "not found" } else { &t.gcc_version });
        });
        ui.add_space(6.0);
        match &t.native {
            Some(n) => {
                ui.label(RichText::new(format!(
                    "-march=native means -march={} here (tuned for {}, {}). An explicit -march={} gives the same code and stays the same across GCC upgrades.",
                    n.march,
                    n.mtune,
                    if n.avx512 { "with AVX-512" } else { "no AVX-512" },
                    n.march
                ))
                .color(TEXT));
            }
            None => {
                ui.label(w::dim("gcc did not report what -march=native means."));
            }
        }
        ui.label(
            RichText::new(format!(
                "MAKEOPTS rule (jobs = min(threads, RAM ÷ 2 GiB)): min({}, {:.1} ÷ 2 = {}) = {} jobs{}.",
                t.hw.threads,
                t.hw.mem_gib(),
                s.ram_jobs,
                s.jobs,
                if s.ram_bound { ", limited by memory" } else { "" }
            ))
            .color(TEXT),
        );
        ui.horizontal_wrapped(|ui| match &t.cpu_flags {
            Some(f) => {
                ui.label(RichText::new("CPU_FLAGS_X86 (cpuid2cpuflags):").color(TEXT));
                ui.label(w::mono(f.join(" ")));
            }
            None => {
                ui.label(w::dim("cpuid2cpuflags is not installed — it detects CPU_FLAGS_X86 exactly."));
                if w::button(ui, icon::DOWNLOAD_SIMPLE, "Install cpuid2cpuflags").clicked() {
                    app.push(Action::Root {
                        title: "Install app-portage/cpuid2cpuflags".into(),
                        argv: vec!["emerge".into(), "--oneshot".into(), "--ask=n".into(), "app-portage/cpuid2cpuflags".into()],
                        reload: vec![Reload::Tuning],
                        kind: crate::app::JobKind::Generic,
                    });
                }
            }
        });
    });
}

pub fn health(app: &mut App, ui: &mut Ui) {
    ui.horizontal(|ui| {
        w::section(ui, "Health check");
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            if app.is_loading(Reload::Health) {
                ui.add(egui::Spinner::new().size(14.0));
            } else if w::button(ui, icon::ARROW_CLOCKWISE, "Check again").clicked() {
                app.push(Action::Reload(Reload::Tuning));
                app.push(Action::Reload(Reload::Health));
            }
        });
    });
    let Some(findings) = app.data.health.clone() else {
        ui.label(w::dim("Checking make.conf against the Gentoo documentation…"));
        return;
    };
    let count = |s: Severity| findings.iter().filter(|f| f.severity == s).count();
    w::card().show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal_wrapped(|ui| {
            if findings.iter().all(|f| f.severity == Severity::Info) {
                ui.label(RichText::new(format!("{} Nothing against the documented advice.", icon::CHECK_CIRCLE)).color(GREEN));
            }
            for s in [Severity::Critical, Severity::High, Severity::Medium, Severity::Info] {
                let n = count(s);
                if n > 0 {
                    let (fg, bg) = severity_colors(s);
                    w::badge(ui, &format!("{n} {}", s.label()), fg, bg);
                }
            }
            ui.add_space(8.0);
            ui.checkbox(&mut app.config_view.mk_show_info, "show info");
        });
        ui.add(
            egui::Label::new(w::dim(
                "Each item says why, links the source, and — where it is safe to automate — offers a fix. Fixes show the exact diff first and can be undone from History → PortageHub changes.",
            ))
            .wrap(),
        );
    });
    let show_info = app.config_view.mk_show_info;
    for f in findings.iter().filter(|f| show_info || f.severity != Severity::Info) {
        finding_card(app, ui, f);
    }
}

fn finding_card(app: &mut App, ui: &mut Ui, f: &Finding) {
    let (fg, bg) = severity_colors(f.severity);
    w::card().show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            w::badge(ui, f.severity.label(), fg, bg);
            ui.label(RichText::new(&f.title).color(WHITE).family(theme::bold()));
            ui.label(w::mono(&f.var));
        });
        ui.add(egui::Label::new(RichText::new(&f.detail).color(TEXT)).wrap());
        let busy = app.is_loading(Reload::Health) || !app.batches.is_empty();
        ui.horizontal(|ui| {
            if let Some(fix) = &f.fix
                && ui
                    .add_enabled_ui(!busy, |ui| w::primary(ui, icon::WRENCH, &fix.label))
                    .inner
                    .on_hover_text(fix.changes.iter().map(|c| c.path.display().to_string()).collect::<Vec<_>>().join("\n"))
                    .clicked()
            {
                app.push(Action::Write { title: fix.label.clone(), changes: fix.changes.clone(), reload: RELOAD_AFTER_FIX.to_vec(), then: None });
            }
            w::link(ui, &format!("{} Read more", icon::BOOK_OPEN), f.link);
        });
    });
    ui.add_space(4.0);
}

/// The compiler flags variable this make.conf uses (COMMON_FLAGS, else CFLAGS).
fn flags_var(mc: &MakeConf) -> &'static str {
    if mc.last("COMMON_FLAGS").is_some() || mc.last("CFLAGS").is_none() { "COMMON_FLAGS" } else { "CFLAGS" }
}

pub fn compiler_card(app: &mut App, ui: &mut Ui, mc: &MakeConf) {
    w::section(ui, "Compiler flags");
    let var = flags_var(mc);
    let file = mc.last(var).map(|a| a.file.clone()).unwrap_or_else(|| mc.main_file());
    let text = mc.text_of(&file).to_string();
    let words: Vec<String> = mc.value(var).split_whitespace().map(String::from).collect();
    let cur_o = words.iter().find(|w| w.starts_with("-O")).cloned();
    let cur_march = words.iter().find_map(|w| w.strip_prefix("-march=")).map(String::from);
    let native = app.data.tuning.as_ref().and_then(|t| t.native.as_ref().map(|n| n.march.clone()));
    w::card().show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal_wrapped(|ui| {
            ui.label(w::mono(format!("{var}=\"{}\"", mc.value(var))));
            w::help(ui, makeconf::describe("COMMON_FLAGS"));
        });
        let state = &mut app.config_view;
        let o = state.mk_opt.get_or_insert_with(|| cur_o.clone().unwrap_or_else(|| "-O2".into()));
        ui.horizontal(|ui| {
            ui.label(RichText::new("Optimisation").color(TEXT));
            for (v, label, tip) in [
                ("-O2", "-O2 (recommended)", "The Gentoo default; what packages are tested with."),
                ("-O3", "-O3", "\"Not recommended outside of running test suites\" — bigger code, rarely faster, more breakage."),
                ("-Os", "-Os", "Optimise for size."),
            ] {
                ui.radio_value(o, v.to_string(), label).on_hover_text(tip);
            }
        });
        let m = state.mk_march.get_or_insert_with(|| cur_march.clone().unwrap_or_else(|| "native".into()));
        ui.horizontal_wrapped(|ui| {
            ui.label(RichText::new("CPU (-march)").color(TEXT));
            ui.radio_value(m, "native".to_string(), "native").on_hover_text("Whatever this CPU supports, decided by GCC at each build.");
            if let Some(n) = &native {
                ui.radio_value(m, n.clone(), n.as_str())
                    .on_hover_text("The explicit name of this CPU: same code as native, stable across GCC upgrades, usable for distcc helpers.");
            }
            ui.radio_value(m, "x86-64-v3".to_string(), "x86-64-v3")
                .on_hover_text("Portable to any CPU with AVX2 (Haswell/Zen and newer) — for binary packages shared with other machines.");
            if let Some(c) = &cur_march
                && Some(c) != native.as_ref()
                && !["native", "x86-64-v3"].contains(&c.as_str())
            {
                ui.radio_value(m, c.clone(), format!("{c} (current)"));
            }
        });
        let (o, m) = (o.clone(), m.clone());
        let new_text = with_opt_and_march(&text, var, &o, &m);
        let changed = new_text != text;
        ui.add_space(4.0);
        if !changed {
            ui.label(w::dim("Matches make.conf."));
            return;
        }
        let new_value = makeconf::scan(&new_text)
            .into_iter()
            .rfind(|(n, _, _)| n == var)
            .map(|(_, v, _)| v.replace("\\\n", " ").split_whitespace().collect::<Vec<_>>().join(" "))
            .unwrap_or_default();
        ui.label(w::mono(format!("{var}=\"{new_value}\"")));
        if m != "native" && native.as_deref().is_some_and(|n| n != m) && m != "x86-64-v3" {
            ui.label(
                RichText::new(format!(
                    "{} -march={m} is not what this CPU is ({}): programs may crash with \"illegal instruction\".",
                    icon::WARNING,
                    native.clone().unwrap_or_default()
                ))
                .color(ORANGE),
            );
        }
        if o == "-O3" {
            ui.label(
                RichText::new(format!("{} -O3 system-wide is against the GCC optimization guide; consider -O3 per package instead.", icon::WARNING))
                    .color(ORANGE),
            );
        }
        ui.label(w::dim("Only packages built from now on use the new flags; nothing is rebuilt by itself."));
        // Preview: what Portage itself would make of the candidate.
        let slot_matches = app.config_view.mk_preview.as_ref().is_some_and(|(t, _)| *t == new_text);
        ui.horizontal(|ui| {
            if w::button(ui, icon::EYE, "Preview with Portage")
                .on_hover_text("Runs portageq with the new make.conf in a temporary config root that uses your real profile.")
                .clicked()
            {
                let slot: PreviewSlot = Arc::default();
                let s2 = slot.clone();
                let changed = vec![(file.clone(), new_text.clone())];
                let ctx = ui.ctx().clone();
                std::thread::spawn(move || {
                    let r = makeconf_validate::preview(&changed, &["CFLAGS", "CXXFLAGS", "FCFLAGS", "LDFLAGS"]);
                    *s2.lock().unwrap() = Some(r);
                    ctx.request_repaint();
                });
                app.config_view.mk_preview = Some((new_text.clone(), slot));
            }
            if w::primary(ui, icon::FLOPPY_DISK, &format!("Save {var}")).clicked() {
                app.push(Action::Write {
                    title: format!("Set {var} in make.conf"),
                    changes: vec![FileChange { path: file.clone(), old: text.clone(), new: Some(new_text.clone()) }],
                    reload: RELOAD_AFTER_FIX.to_vec(),
                    then: None,
                });
                app.config_view.mk_opt = None;
                app.config_view.mk_march = None;
                app.config_view.mk_preview = None;
            }
        });
        if slot_matches && let Some((_, slot)) = &app.config_view.mk_preview {
            match slot.lock().unwrap().as_ref() {
                None => {
                    ui.horizontal(|ui| {
                        ui.add(egui::Spinner::new().size(12.0));
                        ui.label(w::dim("Asking Portage…"));
                    });
                }
                Some(Err(e)) => {
                    ui.label(RichText::new(format!("{} {e}", icon::X_CIRCLE)).color(RED));
                }
                Some(Ok(m)) => {
                    let env = app.data.env.clone();
                    egui::Grid::new("mk-preview").num_columns(3).spacing([12.0, 4.0]).show(ui, |ui| {
                        ui.label(w::dim("Effective"));
                        ui.label(w::dim("now"));
                        ui.label(w::dim("after saving"));
                        ui.end_row();
                        for (k, v) in m {
                            let now = env.as_ref().and_then(|e| e.vars.get(k)).cloned().unwrap_or_default();
                            ui.label(w::mono(k));
                            ui.label(w::mono(now.clone()));
                            ui.label(RichText::new(v).monospace().color(if *v != now { GREEN } else { TEXT_DIM }));
                            ui.end_row();
                        }
                    });
                }
            }
        }
    });
}

/// make.conf text with `-O` and `-march` set in the flags variable `var`:
/// existing words are replaced in place (layout, continuations and
/// `${VAR}` references kept); missing ones are put in front of the value.
pub fn with_opt_and_march(text: &str, var: &str, o: &str, m: &str) -> String {
    let edited = makeconf::edit_words(text, var, &|w| {
        if w.starts_with("-O") {
            Some(o.to_string())
        } else if w.starts_with("-march=") {
            Some(format!("-march={m}"))
        } else {
            None
        }
    });
    let own = format!("${{{var}}}");
    let value =
        makeconf::scan(&edited).into_iter().rfind(|(n, v, _)| n == var && !v.contains(&own)).map(|(_, v, _)| v.replace("\\\n", " ")).unwrap_or_default();
    let words: Vec<&str> = value.split_whitespace().collect();
    let (has_o, has_m) = (words.iter().any(|w| w.starts_with("-O")), words.iter().any(|w| w.starts_with("-march=")));
    if has_o && has_m {
        return edited;
    }
    let mut front = Vec::new();
    if !has_m {
        front.push(format!("-march={m}"));
    }
    if !has_o {
        front.push(o.to_string());
    }
    let rest = if words.is_empty() { "-pipe".to_string() } else { words.join(" ") };
    makeconf::set_var(&edited, var, &format!("{} {rest}", front.join(" ")))
}

/// VIDEO_CARDS/INPUT_DEVICES as written in package.use (`*/* VAR: -* …`), if there.
pub fn package_use_expand(app: &App, var: &str) -> Option<Vec<String>> {
    let set = app.data.config.as_ref()?.set(crate::portage::config::ConfKind::Use)?.clone();
    let prefix = format!("*/* {var}:");
    set.files
        .iter()
        .filter(|f| !f.ignored)
        .flat_map(|f| f.raw.lines().map(String::from).collect::<Vec<_>>())
        .find(|l| l.trim_start().starts_with(&prefix))
        .map(|l| l.trim_start().trim_start_matches(&prefix).split_whitespace().filter(|w| *w != "-*").map(String::from).collect())
}

/// Write a USE_EXPAND variable as `*/* VAR: -* values` in package.use (and
/// drop it from make.conf in the same transaction).
pub fn set_use_expand(app: &mut App, mc: &MakeConf, var: &str, values: &[String]) {
    let Some(cfg) = app.data.config.clone() else { return };
    let file_name = format!("00{}", var.to_lowercase());
    let mut changes = vec![crate::portage::makeconf_check::conf_line(
        &cfg,
        crate::portage::config::ConfKind::Use,
        &file_name,
        &format!("*/* {var}:"),
        &format!("*/* {var}: -* {}", values.join(" ")),
        &format!("{var} (PortageHub)"),
    )];
    let files: Vec<PathBuf> = mc.assignments.iter().filter(|a| a.name == var).map(|a| a.file.clone()).collect();
    for p in files {
        let old = mc.text_of(&p).to_string();
        let new = makeconf::remove_var(&old, var, Some(&format!("{var} moved to package.use/{file_name}")));
        if new != old && !changes.iter().any(|c| c.path == p) {
            changes.push(FileChange { path: p, old, new: Some(new) });
        }
    }
    app.push(Action::Write { title: format!("Set {var}"), changes, reload: RELOAD_AFTER_FIX.to_vec(), then: None });
}

#[cfg(test)]
mod tests {
    use super::with_opt_and_march;

    #[test]
    fn compiler_flags_are_edited_in_place_or_completed_cleanly() {
        let t = "COMMON_FLAGS=\"-march=alderlake -O3 -pipe \\\n    -flto ${WARNING_FLAGS}\"\n";
        assert_eq!(with_opt_and_march(t, "COMMON_FLAGS", "-O2", "alderlake"), t.replace("-O3", "-O2"));
        // Missing -march in a continued value: no stray backslash, new -O kept.
        let t = "COMMON_FLAGS=\"-O3 -pipe \\\n  -flto\"\n";
        assert_eq!(with_opt_and_march(t, "COMMON_FLAGS", "-O2", "alderlake"), "COMMON_FLAGS=\"-march=alderlake -O2 -pipe -flto\"\n");
        // -march present, -O missing: the new -march survives.
        let t = "COMMON_FLAGS=\"-march=x86-64 -pipe\"\n";
        assert_eq!(with_opt_and_march(t, "COMMON_FLAGS", "-O2", "native"), "COMMON_FLAGS=\"-O2 -march=native -pipe\"\n");
        // Nothing set yet.
        assert_eq!(with_opt_and_march("A=1\n", "COMMON_FLAGS", "-O2", "native"), "A=1\nCOMMON_FLAGS=\"-march=native -O2 -pipe\"\n");
    }
}
