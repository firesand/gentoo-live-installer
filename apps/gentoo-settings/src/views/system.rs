//! The "System" tab: what Gentoo Settings detected — init system, hardware,
//! disks and schedulers, swap, kernel settings and who sets them. Includes the
//! information CachyOS's `kerver` prints.

use super::common::page;
use crate::app::{App, ToastKind};
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Color32, RichText, Ui};
use egui_phosphor::regular as icon;
use gentoo_settings::catalog::CATALOG;
use gentoo_settings::detect::{System, selected};
use gentoo_settings::managed;

pub fn show(app: &mut App, ui: &mut Ui) {
    let sys = app.sys.clone();
    page(ui, "system", |ui| {
        ui.horizontal(|ui| {
            w::title(ui, "This system");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if w::button(ui, icon::COPY, "Copy report").on_hover_text("Copy a plain-text summary (for bug reports and forum posts)").clicked() {
                    ui.ctx().copy_text(report(&sys, app));
                    app.toast(ToastKind::Success, "System report copied to the clipboard.");
                }
            });
        });
        w::subtitle(ui, "What Gentoo Settings detected. Tweaks use this to decide what applies here.");
        ui.add_space(10.0);

        let cols = ((ui.available_width() + 12.0) / 300.0).floor().clamp(1.0, 4.0) as usize;
        let cards: Vec<(&str, &str, String, String)> = vec![
            (icon::TREE_STRUCTURE, "Init system", sys.init.label().to_string(), format!("{}{}", sys.init_reason, if sys.elogind { " · elogind" } else { "" })),
            (icon::TERMINAL, "Kernel", sys.kernel.clone(), sched_ext(&sys)),
            (icon::CPU, "CPU", sys.cpu_model.clone(), format!("{} threads · {}", sys.threads, sys.x86_64_levels().last().copied().unwrap_or("?"))),
            (icon::MEMORY, "Memory", format!("{:.1} GiB", sys.mem_gib()), swap_summary(&sys)),
            (
                icon::MONITOR,
                "Graphics",
                if sys.gpus.is_empty() { "none".into() } else { sys.gpus.iter().map(|g| g.vendor.label()).collect::<Vec<_>>().join(" + ") },
                sys.gpus.iter().map(|g| format!("{} ({})", g.slot, if g.driver.is_empty() { "no driver" } else { &g.driver })).collect::<Vec<_>>().join(", "),
            ),
            (
                icon::POWER,
                "Form factor",
                if sys.battery { "Laptop (battery)".into() } else { "Desktop (no battery)".into() },
                if sys.wifi.is_empty() { "no Wi-Fi".into() } else { format!("Wi-Fi: {}", sys.wifi.join(", ")) },
            ),
            (icon::STACK, "Portage profile", if sys.profile.is_empty() { "unknown".into() } else { sys.profile.clone() }, String::new()),
        ];
        for row in cards.chunks(cols) {
            ui.columns(cols, |cs| {
                for (i, (ic, label, value, sub)) in row.iter().enumerate() {
                    stat_card(&mut cs[i], ic, label, value, sub);
                }
            });
            ui.add_space(8.0);
        }

        w::section(ui, "Disks and I/O schedulers");
        egui::Grid::new("disks").striped(true).num_columns(6).spacing([18.0, 6.0]).show(ui, |ui| {
            for h in ["DEVICE", "TYPE", "MODEL", "SIZE", "SCHEDULER", "AVAILABLE"] {
                ui.label(RichText::new(h).size(11.0).family(theme::bold()).color(TEXT_DIM));
            }
            ui.end_row();
            for d in &sys.disks {
                ui.label(w::mono(&d.name));
                ui.label(format!("{}{}", if d.usb { "USB" } else { d.kind.label() }, if d.rotational { " · HDD" } else { " · SSD" }));
                ui.label(w::dim(if d.model.is_empty() { "—" } else { &d.model }));
                ui.label(crate::fmt::size(d.size_bytes));
                ui.label(RichText::new(d.scheduler.clone().unwrap_or_else(|| "—".into())).color(GREEN_TEXT).monospace());
                ui.label(w::dim(d.schedulers.join(" ")));
                ui.end_row();
            }
        });

        w::section(ui, "Swap");
        let swaps = sys.read_raw("/proc/swaps").unwrap_or_default();
        let rows: Vec<Vec<String>> =
            swaps.lines().skip(1).map(|l| l.split_whitespace().map(String::from).collect()).filter(|v: &Vec<String>| v.len() >= 5).collect();
        if rows.is_empty() {
            ui.label(w::dim("No swap is active."));
        } else {
            egui::Grid::new("swaps").striped(true).num_columns(4).spacing([18.0, 6.0]).show(ui, |ui| {
                for h in ["DEVICE", "SIZE", "USED", "PRIORITY"] {
                    ui.label(RichText::new(h).size(11.0).family(theme::bold()).color(TEXT_DIM));
                }
                ui.end_row();
                for r in rows {
                    let kb = |s: &str| crate::fmt::size(s.parse::<u64>().unwrap_or(0) * 1024);
                    ui.label(w::mono(&r[0]));
                    ui.label(kb(&r[2]));
                    ui.label(kb(&r[3]));
                    ui.label(&r[4]);
                    ui.end_row();
                }
            });
        }

        w::section(ui, "Kernel settings (sysctl)");
        ui.label(w::dim(format!("Applied by sysctl --system in this order: drop-ins sorted by name, then /etc/sysctl.conf. Ours is {}.", managed::SYSCTL)));
        ui.add_space(4.0);
        egui::Grid::new("sysctls").striped(true).num_columns(4).spacing([18.0, 6.0]).show(ui, |ui| {
            for h in ["KEY", "NOW", "CACHYOS", "CONFIGURED IN"] {
                ui.label(RichText::new(h).size(11.0).family(theme::bold()).color(TEXT_DIM));
            }
            ui.end_row();
            for t in CATALOG.iter().filter(|t| t.is_sysctl()) {
                let now = sys.read(&System::sysctl_path(t.key)).map(|v| gentoo_settings::detect::norm_ws(&v));
                let st = app.status(t.id);
                let ok = st.is_some_and(|s| s.live.state == gentoo_settings::catalog::LiveState::Match);
                ui.label(w::mono(t.key));
                ui.label(RichText::new(now.unwrap_or_else(|| "—".into())).monospace().color(if ok { GREEN_TEXT } else { TEXT }));
                ui.label(w::dim(if t.at_least { format!("≥ {}", t.default) } else { t.default.to_string() }));
                let set_by: Vec<String> = app
                    .scan
                    .sysctl
                    .settings
                    .iter()
                    .filter(|s| s.key == t.key)
                    .map(|s| format!("{} ({})", s.file.rsplit('/').next().unwrap_or(&s.file), s.value))
                    .collect();
                ui.label(w::dim(if set_by.is_empty() { "kernel default".to_string() } else { set_by.join(" → ") }));
                ui.end_row();
            }
        });
        if !app.scan.sysctl.unreadable.is_empty() {
            ui.add_space(4.0);
            w::callout(
                ui,
                ORANGE,
                icon::LOCK,
                &format!("Only root can read {}; settings in them are not shown above but still apply.", app.scan.sysctl.unreadable.join(", ")),
            );
        }

        w::section(ui, "Transparent huge pages");
        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing.x = 28.0;
            for (label, path) in [
                ("Enabled", "/sys/kernel/mm/transparent_hugepage/enabled"),
                ("Defrag", "/sys/kernel/mm/transparent_hugepage/defrag"),
                ("Shrinker max_ptes_none", "/sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none"),
                ("zswap", "/sys/module/zswap/parameters/enabled"),
            ] {
                w::stat(ui, label, &sys.read(path).map(|v| selected(&v)).unwrap_or_else(|| "—".into()));
            }
        });

        w::section(ui, "Kernel command line");
        egui::Frame::new().fill(BG_DEEP).corner_radius(3).inner_margin(egui::Margin::symmetric(10, 6)).show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.add(
                egui::Label::new(RichText::new(sys.read("/proc/cmdline").unwrap_or_default()).monospace().color(Color32::from_rgb(0xd7, 0xe7, 0xb4))).wrap(),
            );
        });
        ui.label(w::dim("Gentoo Settings never changes the kernel command line; edit it in your bootloader configuration."));
    });
}

fn stat_card(ui: &mut Ui, ic: &str, label: &str, value: &str, sub: &str) {
    w::card().show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.set_min_height(70.0);
        ui.horizontal_top(|ui| {
            ui.label(RichText::new(ic).size(24.0).color(ACCENT));
            ui.vertical(|ui| {
                ui.spacing_mut().item_spacing.y = 2.0;
                ui.label(RichText::new(label.to_uppercase()).size(11.0).family(theme::bold()).color(TEXT_DIM));
                ui.add(egui::Label::new(RichText::new(value).size(15.0).color(WHITE).family(theme::medium())).wrap());
                if !sub.is_empty() {
                    ui.add(egui::Label::new(RichText::new(sub).size(12.0).color(TEXT_DIM)).wrap());
                }
            });
        });
    });
}

fn sched_ext(sys: &System) -> String {
    match sys.read("/sys/kernel/sched_ext/state") {
        Some(state) if state == "enabled" => format!("sched_ext: {}", sys.read("/sys/kernel/sched_ext/root/ops").unwrap_or_else(|| "enabled".into())),
        Some(_) => "sched_ext available, not in use".into(),
        None => "no sched_ext".into(),
    }
}

fn swap_summary(sys: &System) -> String {
    let swaps = sys.read_raw("/proc/swaps").unwrap_or_default();
    let devs: Vec<String> = swaps.lines().skip(1).filter_map(|l| l.split_whitespace().next()).map(|d| d.rsplit('/').next().unwrap_or(d).to_string()).collect();
    if devs.is_empty() { "no swap".into() } else { format!("swap: {}", devs.join(", ")) }
}

fn report(sys: &System, app: &App) -> String {
    let mut s = String::new();
    s.push_str(&format!("Gentoo Settings {}\n", gentoo_settings::protocol::VERSION));
    s.push_str(&format!("init: {} ({})\nkernel: {}\nprofile: {}\n", sys.init.label(), sys.init_reason, sys.kernel, sys.profile));
    s.push_str(&format!("cpu: {} ({} threads, {})\n", sys.cpu_model, sys.threads, sys.x86_64_levels().join(", ")));
    s.push_str(&format!("memory: {:.1} GiB, {}\n", sys.mem_gib(), swap_summary(sys)));
    for g in &sys.gpus {
        s.push_str(&format!("gpu: {} {} driver={}\n", g.vendor.label(), g.slot, g.driver));
    }
    for d in &sys.disks {
        s.push_str(&format!(
            "disk: {} {} rot={} scheduler={} ({})\n",
            d.name,
            d.kind.label(),
            d.rotational,
            d.scheduler.clone().unwrap_or_default(),
            d.schedulers.join(" ")
        ));
    }
    s.push_str(&format!("{}\ncmdline: {}\n\ntweaks:\n", sched_ext(sys), sys.read("/proc/cmdline").unwrap_or_default()));
    for st in &app.scan.statuses {
        s.push_str(&format!("  {:<34} {:<16} now={}\n", st.tweak.id, st.badge.label(), st.live.now));
    }
    s
}
