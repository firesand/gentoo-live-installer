//! Maintenance: kernels & boot (known-good kernels, bootloader check, NVIDIA
//! modules) and cleanup of everything that is no longer used.

use super::common::{self, page, subnav};
use crate::app::{Action, App, JobKind, Tab, ToastKind};
use crate::data::Reload;
use crate::edits;
use crate::exec::Priv;
use crate::portage::cleanup;
use crate::portage::emerge::{self, EmergeOptions, Value};
use crate::portage::kernel::{Fix, KernelState, NvStatus, Severity};
use crate::portage::pretend;
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Color32, RichText, Ui};
use egui_phosphor::regular as icon;
use portagehub::protocol::{BootAction, Op};
use std::collections::HashSet;
use std::path::PathBuf;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Page {
    #[default]
    Kernels,
    Cleanup,
}

#[derive(Default)]
pub struct State {
    pub page: Page,
    pub show_nvidia_help: bool,
    pub override_unmarked: bool,
    /// Depclean candidates the user un-ticked.
    pub depclean_skip: HashSet<String>,
    /// Selected obsolete /etc/portage lines.
    pub config_selected: HashSet<(PathBuf, usize)>,
    pub show_rules: bool,
}

pub fn show(app: &mut App, ui: &mut Ui) {
    let mut p = app.maintenance.page;
    subnav(ui, &mut p, &[(Page::Kernels, "Kernels & Boot"), (Page::Cleanup, "Cleanup")], |ui| {
        if w::button(ui, icon::ARROWS_CLOCKWISE, "Refresh").clicked() {
            app.push(Action::Reload(Reload::Kernels));
            app.push(Action::Reload(Reload::Leftovers));
        }
    });
    app.maintenance.page = p;
    match p {
        Page::Kernels => kernels(app, ui),
        Page::Cleanup => cleanup_page(app, ui),
    }
}

fn scan_boot(app: &mut App) {
    app.push(Action::RootOp { title: "Read boot configuration".into(), op: Op::BootScan, kind: JobKind::BootScan, reload: vec![] });
}

fn boot_action(app: &mut App, action: BootAction, kver: Option<String>) {
    let title = match &kver {
        Some(k) => format!("{} for {k}", action.label()),
        None => action.label().to_string(),
    };
    app.push(Action::RootOp { title, op: Op::BootAction { action, kver }, kind: JobKind::BootFix, reload: vec![] });
    app.emerge.page = super::emerge::Page::Activity;
}

fn apply_fix(app: &mut App, ks: &KernelState, fix: &Fix) {
    match fix {
        Fix::ScanBoot => scan_boot(app),
        Fix::Boot(a, k) => boot_action(app, *a, k.clone()),
        Fix::EmergeConfig(cpv) => {
            app.push(Action::Root {
                title: format!("emerge --config ={cpv}"),
                argv: vec!["emerge".into(), "--config".into(), "--ask=n".into(), format!("={cpv}")],
                reload: vec![],
                kind: JobKind::BootFix,
            });
        }
        Fix::Reinstall(cpv) => {
            let atom = format!("={cpv}");
            app.push(Action::Pretend(EmergeOptions::default().with("--oneshot", Value::On).targets(&[atom.as_str()])));
        }
        Fix::MarkGood(k) => app.mark_kernel_good(k.clone()),
        Fix::WriteKeepSet => {
            let atoms = crate::portage::kernel::wanted_keep_atoms(ks, ks.last_good.as_deref(), ks.previous_good.as_deref());
            let mut content = String::from("# Proven-good kernels protected from --depclean. Managed by PortageHub (same format as kernel-mark-good).\n");
            for a in atoms {
                content.push_str(&a);
                content.push('\n');
            }
            let path = PathBuf::from(format!("/etc/portage/sets/{}", ks.keep_set));
            match crate::portage::config::read_utf8(&path) {
                Ok(old) => app.push(Action::Write {
                    title: format!("Protect known-good kernels in @{}", ks.keep_set),
                    changes: vec![crate::app::FileChange { path, old, new: Some(content) }],
                    reload: vec![Reload::Kernels, Reload::Config],
                    then: None,
                }),
                Err(why) => app.toast(crate::app::ToastKind::Error, why),
            }
        }
        Fix::AddKeepSetToWorld => app.push(Action::Root {
            title: format!("Add @{} to @world", ks.keep_set),
            argv: vec!["emerge".into(), "--noreplace".into(), "--ask=n".into(), format!("@{}", ks.keep_set)],
            reload: vec![Reload::Vdb, Reload::Kernels],
            kind: JobKind::Generic,
        }),
        Fix::RebuildNvidia => app.maintenance.show_nvidia_help = true,
        Fix::RemoveOld(k) => app.push(Action::RootOp {
            title: format!("Remove *.old copies of {k}"),
            op: Op::RemoveKernelLeftovers { kver: k.clone(), old_only: true },
            kind: JobKind::BootFix,
            reload: vec![],
        }),
        Fix::RemoveLeftovers(k) => app.push(Action::RootOp {
            title: format!("Remove leftover files of kernel {k}"),
            op: Op::RemoveKernelLeftovers { kver: k.clone(), old_only: false },
            kind: JobKind::BootFix,
            reload: vec![],
        }),
        Fix::Depclean(atoms) => {
            let t: Vec<&str> = atoms.iter().map(|s| s.as_str()).collect();
            app.push(Action::Pretend(EmergeOptions { action: emerge::Action::Depclean, ..Default::default() }.targets(&t)));
        }
    }
}

fn fix_label(fix: &Fix) -> (&'static str, String) {
    match fix {
        Fix::ScanBoot => (icon::MAGNIFYING_GLASS, "Check boot menu".into()),
        Fix::Boot(a, _) => (icon::WRENCH, a.label().to_string()),
        Fix::EmergeConfig(cpv) => (icon::GEAR, format!("Re-install boot entry (emerge --config ={cpv})")),
        Fix::Reinstall(cpv) => (icon::ARROWS_CLOCKWISE, format!("Reinstall {cpv}")),
        Fix::MarkGood(k) => (icon::SEAL_CHECK, format!("Mark {k} known-good")),
        Fix::WriteKeepSet => (icon::SHIELD_CHECK, "Update the keep set".into()),
        Fix::AddKeepSetToWorld => (icon::STAR, "Add the set to @world".into()),
        Fix::RebuildNvidia => (icon::CPU, "How to rebuild NVIDIA".into()),
        Fix::RemoveOld(_) => (icon::TRASH, "Remove the .old copies".into()),
        Fix::RemoveLeftovers(_) => (icon::TRASH, "Remove leftover files".into()),
        Fix::Depclean(_) => (icon::BROOM, "Preview depclean".into()),
    }
}

fn sev_style(s: Severity) -> (Color32, &'static str) {
    match s {
        Severity::Ok => (GREEN, icon::CHECK_CIRCLE),
        Severity::Info => (ACCENT, icon::INFO),
        Severity::Warn => (ORANGE, icon::WARNING),
        Severity::Error => (RED, icon::X_CIRCLE),
    }
}

fn kernels(app: &mut App, ui: &mut Ui) {
    let Some(ks) = app.data.kernels.clone() else {
        w::loading(ui, "Looking at installed kernels…");
        return;
    };
    let scan = app.boot_scan.clone();
    let scanning = app.jobs.iter().any(|j| j.running() && matches!(j.kind, JobKind::BootScan));
    let findings = crate::portage::kernel::check(&ks, scan.as_ref().map(|(s, _)| s.as_ref()), app.settings.kernel_protect, 2, app.settings.nvidia_alerts);
    page(ui, "kernels", |ui| {
        ui.horizontal(|ui| {
            ui.label(RichText::new(icon::CPU).size(30.0).color(ACCENT));
            w::title(ui, "Kernels & Boot");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if scanning {
                    ui.add(egui::Spinner::new().color(ACCENT));
                } else if w::primary(ui, icon::MAGNIFYING_GLASS, if scan.is_some() { "Check boot menu again" } else { "Check boot menu" })
                    .on_hover_text("Reads the root-only bootloader files through the admin session (read-only)")
                    .clicked()
                {
                    scan_boot(app);
                }
                if let Some((_, ts)) = &scan {
                    ui.label(w::dim(format!("checked {}", crate::fmt::ago(*ts))));
                }
            });
        });
        ui.horizontal_wrapped(|ui| {
            w::stat(ui, "Running", &ks.running);
            ui.add_space(18.0);
            let bl = ks.boot.primary.map(|b| b.name().to_string()).unwrap_or_else(|| "unknown".into());
            let others: Vec<&str> = ks.boot.detected.iter().filter(|b| Some(**b) != ks.boot.primary).map(|b| b.name()).collect();
            w::stat(ui, "Bootloader", &if others.is_empty() { bl } else { format!("{bl} (also: {})", others.join(", ")) });
            ui.add_space(18.0);
            if let Some((label, path)) = &ks.boot.current {
                w::stat(ui, "Firmware boot entry", &format!("{label}  {path}"));
                ui.add_space(18.0);
            }
            w::stat(ui, "Initramfs", ks.boot.initramfs_tool.as_deref().unwrap_or("none"));
            ui.add_space(18.0);
            let nv = if ks.nvidia.gpu {
                match &ks.nvidia.drivers {
                    Some(v) => format!("nvidia-drivers {v}{}", if ks.nvidia.dist_kernel { " (dist-kernel)" } else { "" }),
                    None => "GPU found, driver not installed".into(),
                }
            } else {
                "no NVIDIA GPU".into()
            };
            w::stat(ui, "NVIDIA", &nv);
        });
        ui.add_space(6.0);
        common::tip(
            app,
            ui,
            "Gentoo keeps every kernel version you install until you remove it. The safe routine: boot the new kernel, check that everything works, mark it known-good — then the previous known-good kernel stays as your fallback in the boot menu, and both are protected from emerge --depclean.",
        );

        // Mark-good banner
        let newest = ks.newest_bootable().map(|k| k.kver.clone());
        if ks.last_good.as_deref() != Some(ks.running.as_str()) && newest.as_deref() == Some(ks.running.as_str()) {
            w::card_tinted(GREEN).show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    ui.label(RichText::new(icon::SEAL_QUESTION).size(30.0).color(GREEN));
                    ui.vertical(|ui| {
                        ui.label(RichText::new(format!("You are running the new kernel {}", ks.running)).size(17.0).family(theme::bold()).color(WHITE));
                        let mut what = vec!["records it as last-good (the old one becomes previous-good)".to_string()];
                        if app.settings.kernel_backup_on_mark {
                            what.push(format!("copies its files to /boot/kernel-backup/{}", ks.running));
                        }
                        if app.settings.kernel_protect {
                            what.push(format!("protects both in @{}", ks.keep_set));
                        }
                        if app.settings.kernel_mark_requires_nvidia && ks.nvidia.gpu && ks.nvidia.drivers.is_some() {
                            what.push("checks nvidia-smi first".into());
                        }
                        ui.label(w::dim(format!("Everything working? Marking it known-good {}.", what.join(", "))));
                    });
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if w::big_button(ui, &format!("{}  MARK KNOWN-GOOD", icon::SEAL_CHECK), GRAD_GREEN, 210.0).clicked() {
                            app.mark_kernel_good(ks.running.clone());
                        }
                    });
                });
            });
            ui.add_space(6.0);
        }

        // Boot partitions
        let mounts = app.boot_mounts();
        if !mounts.is_empty() {
            w::section(ui, "Boot partitions");
            let pending = app.jobs.iter().any(|j| j.running() && matches!(j.kind, JobKind::Remount));
            w::card().show(ui, |ui| {
                ui.set_width(ui.available_width());
                for m in &mounts {
                    ui.horizontal(|ui| {
                        let (ic, col) = if m.blocks_writes() { (icon::LOCK, ORANGE) } else { (icon::LOCK_OPEN, GREEN_TEXT) };
                        ui.label(RichText::new(ic).size(16.0).color(col));
                        ui.label(RichText::new(&m.target).monospace().color(WHITE));
                        ui.label(w::dim(format!("{} {} · {}{}", m.fstype, m.device, m.state_text(), if m.fstab_ro { " · read-only in fstab" } else { "" })));
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if pending {
                                ui.add(egui::Spinner::new().color(ACCENT));
                            } else if m.blocks_writes() {
                                if w::primary(ui, icon::LOCK_OPEN, if m.mounted { "Make writable" } else { "Mount" }).clicked() {
                                    let restore = m.fstab_ro && app.settings.boot_restore_ro;
                                    app.remount_boot(&m.target, true, restore);
                                }
                            } else if m.mounted && m.fstab_ro && w::button(ui, icon::LOCK, "Make read-only again").clicked() {
                                app.remount_boot(&m.target, false, false);
                            }
                        });
                    });
                    if m.mounted && !m.ro && m.fstab_ro && app.running_emerge().is_none() && !app.remount_restore.contains(&m.target) {
                        ui.label(RichText::new(format!("{} still writable — fstab mounts it read-only, so you probably want to lock it again.", m.target)).size(12.0).color(ORANGE));
                    }
                }
                ui.label(w::dim(
                    "Kernel and bootloader updates (installkernel, grub, bootctl) write here. A read-only partition is made writable from the emerge preview — or automatically, with the rule below — and locked again afterwards.",
                ));
            });
        }

        // Findings
        w::section(ui, "Boot check");
        for f in &findings {
            let (col, ic) = sev_style(f.severity);
            w::card_tinted(col).show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    ui.label(RichText::new(ic).size(20.0).color(col));
                    ui.vertical(|ui| {
                        ui.label(RichText::new(&f.title).size(14.5).family(theme::medium()).color(WHITE));
                        if !f.detail.is_empty() {
                            ui.add(egui::Label::new(RichText::new(&f.detail).color(TEXT_DIM)).wrap());
                        }
                        if !f.fixes.is_empty() {
                            ui.horizontal_wrapped(|ui| {
                                for fix in &f.fixes {
                                    let (ic, label) = fix_label(fix);
                                    if w::primary(ui, ic, &label).clicked() {
                                        apply_fix(app, &ks, fix);
                                    }
                                }
                            });
                        }
                    });
                });
            });
            ui.add_space(3.0);
        }
        if app.maintenance.show_nvidia_help {
            nvidia_help(app, ui, &ks);
        }

        // Kernel table
        w::section(ui, &format!("Installed kernels ({})", ks.kernels.len()));
        let grub_cfg = scan.as_ref().and_then(|(s, _)| s.file("/boot/grub/grub.cfg").or(s.file("/boot/grub2/grub.cfg")).map(|f| f.content.clone()));
        egui::Grid::new("kernel-grid").num_columns(7).striped(true).spacing([14.0, 7.0]).show(ui, |ui| {
            for h in ["Kernel", "Status", "Package", "Files", "NVIDIA", "Boot menu", ""] {
                ui.label(w::dim(h));
            }
            ui.end_row();
            for k in &ks.kernels {
                ui.label(RichText::new(&k.kver).monospace().color(if k.kver == ks.running { WHITE } else { TEXT }));
                ui.horizontal(|ui| {
                    if k.kver == ks.running {
                        w::badge(ui, "RUNNING", GREEN_TEXT, GREEN_DARK);
                    }
                    if ks.last_good.as_deref() == Some(k.kver.as_str()) {
                        w::badge(ui, "LAST-GOOD", WHITE, Color32::from_rgb(0x1f, 0x5f, 0x99));
                    }
                    if ks.previous_good.as_deref() == Some(k.kver.as_str()) {
                        w::badge(ui, "PREVIOUS-GOOD", WHITE, Color32::from_rgb(0x2a, 0x6f, 0x97));
                    }
                    if newest.as_deref() == Some(k.kver.as_str()) {
                        w::badge(ui, "NEWEST", TEXT, Color32::from_rgb(0x3a, 0x46, 0x55));
                    }
                    if ks.eselected.as_deref() == Some(k.kver.as_str()) {
                        w::badge(ui, "/usr/src/linux", TEXT, Color32::from_rgb(0x3a, 0x46, 0x55)).on_hover_text("eselect kernel target: external modules build against it");
                    }
                });
                match &k.pkg {
                    Some(p) => {
                        if ui.link(RichText::new(p).color(TEXT)).clicked() {
                            let key = crate::portage::atom::split_cpv(p).map(|(c, n, _)| format!("{c}/{n}")).unwrap_or_default();
                            app.push(Action::Open(key));
                        }
                    }
                    None => {
                        ui.label(RichText::new("no package (leftover)").color(ORANGE));
                    }
                }
                ui.horizontal(|ui| {
                    for (ok, label, tip) in [
                        (k.image.is_some(), "image", "kernel image in /boot"),
                        (k.initramfs.is_some(), "initramfs", "initramfs in /boot"),
                        (k.modules, "modules", "/lib/modules"),
                        (k.sources, "src", "/usr/src/linux-*"),
                    ] {
                        ui.label(RichText::new(format!("{} {label}", if ok { icon::CHECK } else { icon::X })).size(12.0).color(if ok { GREEN_TEXT } else { TEXT_FAINT }))
                            .on_hover_text(tip);
                    }
                    if !k.old_files.is_empty() {
                        ui.label(RichText::new(format!("+{} .old", k.old_files.len())).size(12.0).color(ORANGE));
                    }
                });
                match ks.nvidia_status(k) {
                    Some(NvStatus::Ok) => {
                        ui.label(RichText::new(format!("{} {}", icon::CHECK, k.nvidia.as_ref().map(|n| n.version.clone()).unwrap_or_default())).color(GREEN_TEXT));
                    }
                    Some(NvStatus::Missing) => {
                        ui.label(RichText::new(format!("{} missing", icon::X)).color(RED));
                    }
                    Some(NvStatus::Mismatch(v)) => {
                        ui.label(RichText::new(format!("{} {v} ≠ driver", icon::WARNING)).color(ORANGE));
                    }
                    None => {
                        ui.label(w::dim("—"));
                    }
                }
                match (&grub_cfg, &k.image) {
                    (Some(cfg), Some(img)) => {
                        let ok = cfg.contains(img.as_str());
                        ui.label(RichText::new(if ok { format!("{} in GRUB", icon::CHECK) } else { format!("{} not in GRUB", icon::X) }).color(if ok { GREEN_TEXT } else { RED }));
                    }
                    (None, Some(_)) => {
                        ui.label(w::dim("not checked"));
                    }
                    _ => {
                        ui.label(w::dim("—"));
                    }
                }
                ui.menu_button(RichText::new(icon::DOTS_THREE_VERTICAL).size(16.0), |ui| kernel_menu(app, ui, &ks, &k.kver));
                ui.end_row();
            }
        });
        if !ks.orphan_packages.is_empty() {
            ui.label(w::dim(format!("Kernel packages without files (only in the package database): {}", ks.orphan_packages.join(", "))));
        }

        // Clean old kernels
        w::section(ui, "Remove old kernels");
        clean_old_kernels(app, ui, &ks);

        // Rules
        w::section(ui, "Rules");
        rules(app, ui, &ks);
    });
}

fn kernel_menu(app: &mut App, ui: &mut Ui, ks: &KernelState, kver: &str) {
    let Some(k) = ks.get(kver).cloned() else { return };
    ui.label(RichText::new(kver).color(WHITE));
    ui.separator();
    if kver == ks.running && ks.last_good.as_deref() != Some(kver) && ui.button(format!("{}  Mark known-good", icon::SEAL_CHECK)).clicked() {
        app.mark_kernel_good(kver.to_string());
        ui.close();
    }
    if k.modules && ui.button(format!("{}  Rebuild initramfs (dracut)", icon::WRENCH)).clicked() {
        boot_action(app, BootAction::DracutRegen, Some(kver.to_string()));
        ui.close();
    }
    if let Some(p) = &k.pkg {
        if ui.button(format!("{}  Re-run install step (emerge --config)", icon::GEAR)).on_hover_text("installkernel: regenerates initramfs and the boot entry").clicked() {
            apply_fix(app, ks, &Fix::EmergeConfig(p.clone()));
            ui.close();
        }
        ui.separator();
        let protected = ks.is_protected(kver);
        let r = ui.add_enabled(!protected, egui::Button::new(format!("{}  Remove this kernel (depclean preview)", icon::TRASH)));
        let r = if protected { r.on_disabled_hover_text("Running or known-good kernels are protected") } else { r };
        if r.clicked() {
            apply_fix(app, ks, &Fix::Depclean(vec![format!("={p}")]));
            ui.close();
        }
    } else if kver != ks.running && ui.button(format!("{}  Remove leftover files", icon::TRASH)).clicked() {
        apply_fix(app, ks, &Fix::RemoveLeftovers(kver.to_string()));
        ui.close();
    }
    if !k.old_files.is_empty() && ui.button(format!("{}  Remove {} .old copies", icon::TRASH, k.old_files.len())).clicked() {
        apply_fix(app, ks, &Fix::RemoveOld(kver.to_string()));
        ui.close();
    }
}

fn nvidia_help(app: &mut App, ui: &mut Ui, ks: &KernelState) {
    w::card_tinted(PURPLE).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            ui.label(RichText::new(icon::CPU).size(22.0).color(PURPLE));
            ui.label(w::strong("Rebuilding the NVIDIA kernel module"));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if w::icon_button(ui, icon::X, "Close").clicked() {
                    app.maintenance.show_nvidia_help = false;
                }
            });
        });
        let target = ks.eselected.clone().unwrap_or_else(|| "the /usr/src/linux kernel".into());
        if ks.nvidia.dist_kernel {
            ui.label(w::dim("nvidia-drivers is built with USE=dist-kernel, so rebuilding it targets your distribution kernels automatically."));
        } else {
            ui.label(RichText::new(format!(
                "nvidia-drivers is built without USE=dist-kernel: one emerge builds the module only for {target}, and Portage removes the copies it built earlier for other kernels."
            ))
            .color(ORANGE));
        }
        ui.horizontal_wrapped(|ui| {
            if w::primary(ui, icon::PLAY, &format!("Rebuild nvidia-drivers for {target}")).clicked() {
                app.push(Action::Pretend(EmergeOptions::default().with("--oneshot", Value::On).targets(&["x11-drivers/nvidia-drivers"])));
            }
            if w::button(ui, icon::PUZZLE_PIECE, "Rebuild all external modules (@module-rebuild)").clicked() {
                app.push(Action::Pretend(EmergeOptions::default().targets(&["@module-rebuild"])));
            }
        });
        if let Some(script) = &ks.nvidia.fallback_script {
            ui.add_space(4.0);
            ui.label(w::dim(format!("Your system has {} which builds the module for every installed kernel and keeps the other copies. It is your own script, so PortageHub does not run it as root — run it in a terminal:", script.display())));
            w::command_preview(ui, &format!("sudo {}", script.display()));
        } else if !ks.nvidia.dist_kernel {
            ui.label(w::dim("To keep the module for several kernels, switch the kernel with eselect kernel, rebuild, and repeat — or enable USE=dist-kernel for nvidia-drivers when you use distribution kernels."));
        }
    });
}

fn clean_old_kernels(app: &mut App, ui: &mut Ui, ks: &KernelState) {
    let keep = app.settings.kernel_keep_newest;
    let removable = ks.removable(keep);
    let newest = ks.newest_bootable().map(|k| k.kver.clone());
    let unmarked = app.settings.kernel_require_marked_newest && newest.is_some() && newest.as_deref() != ks.last_good.as_deref();
    w::card().show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.label(w::dim(format!(
            "Keeps the running kernel, the known-good kernels{} and the {keep} newest other bootable kernel(s). Kernel packages are removed through emerge --depclean (with a preview); files of kernels without a package are deleted directly.",
            match (&ks.last_good, &ks.previous_good) {
                (Some(a), Some(b)) => format!(" ({a}, {b})"),
                (Some(a), None) => format!(" ({a})"),
                _ => String::new(),
            }
        )));
        if removable.is_empty() {
            ui.label(RichText::new(format!("{} Nothing to remove under these rules.", icon::CHECK_CIRCLE)).color(GREEN_TEXT));
            return;
        }
        ui.horizontal_wrapped(|ui| {
            ui.label(w::dim("Would remove:"));
            for k in &removable {
                w::badge(ui, &k.kver, WHITE, Color32::from_rgb(0x6b, 0x2a, 0x2a)).on_hover_text(k.pkg.clone().unwrap_or_else(|| "leftover files only".into()));
            }
        });
        if unmarked {
            w::callout(
                ui,
                ORANGE,
                icon::WARNING,
                &format!(
                    "The newest kernel {} is not marked known-good yet. Boot it and mark it first, so you never remove the last kernel that is known to work.",
                    newest.clone().unwrap_or_default()
                ),
            );
            ui.checkbox(&mut app.maintenance.override_unmarked, "Remove anyway");
        }
        let allowed = !unmarked || app.maintenance.override_unmarked;
        let pkgs: Vec<String> = removable.iter().filter_map(|k| k.pkg.clone()).map(|p| format!("={p}")).collect();
        let leftovers: Vec<String> = removable.iter().filter(|k| k.pkg.is_none()).map(|k| k.kver.clone()).collect();
        ui.horizontal(|ui| {
            if !pkgs.is_empty() && ui.add_enabled_ui(allowed, |ui| w::danger(ui, icon::BROOM, &format!("Preview removing {} kernel package(s)", pkgs.len()))).inner.clicked() {
                apply_fix(app, ks, &Fix::Depclean(pkgs.clone()));
            }
            if !leftovers.is_empty() && ui.add_enabled_ui(allowed, |ui| w::danger(ui, icon::TRASH, &format!("Delete files of {} removed kernel(s)", leftovers.len()))).inner.clicked() {
                let ops: Vec<Op> = leftovers.iter().map(|k| Op::RemoveKernelLeftovers { kver: k.clone(), old_only: false }).collect();
                app.push(Action::Batch { title: "Remove kernel leftovers".into(), ops, reload: vec![Reload::Kernels], then: None });
            }
        });
        if ks.boot.primary == Some(crate::portage::kernel::Bootloader::Grub) {
            ui.label(w::dim("Afterwards run “Regenerate GRUB menu” (the boot check will offer it) so removed kernels disappear from the menu."));
        }
    });
}

fn rules(app: &mut App, ui: &mut Ui, ks: &KernelState) {
    w::card().show(ui, |ui| {
        ui.set_width(ui.available_width().min(900.0));
        let s = &mut app.settings;
        w::switch_row(ui, &mut s.kernel_protect, "Protect known-good kernels from depclean", "Keeps =package-version atoms of the last-good and previous-good kernels in a package set that is part of @world, so emerge --depclean never removes them.");
        ui.horizontal(|ui| {
            ui.label(w::dim("Keep set name"));
            ui.add(egui::TextEdit::singleline(&mut s.kernel_keep_set).desired_width(160.0));
            ui.label(w::dim(format!("→ /etc/portage/sets/{} ({})", s.kernel_keep_set, if ks.keep_set_in_world { "in @world" } else { "not in @world yet" })));
        });
        w::switch_row(ui, &mut s.kernel_backup_on_mark, "Back up kernel files when marking known-good", "Copies vmlinuz, initramfs, config and System.map to /boot/kernel-backup/<version>.");
        w::switch_row(ui, &mut s.kernel_mark_requires_nvidia, "Require a working NVIDIA driver before marking", "Runs nvidia-smi first on NVIDIA systems; a kernel without a working GPU driver is not marked known-good.");
        w::switch_row(ui, &mut s.kernel_require_marked_newest, "Only clean kernels after the newest one is marked", "Refuses to remove old kernels while the newest kernel has not been booted and marked known-good.");
        ui.horizontal(|ui| {
            ui.label("Extra newest kernels to keep");
            w::help(ui, "Besides the running and known-good kernels.");
            ui.add(egui::DragValue::new(&mut s.kernel_keep_newest).range(0..=6));
        });
        w::switch_row(ui, &mut s.boot_auto_remount, "Make read-only boot partitions writable automatically", "Before an emerge that installs kernels or bootloaders, remount read-only /boot, /boot/efi or /efi read-write (mount -o remount,rw) without asking.");
        w::switch_row(ui, &mut s.boot_restore_ro, "Lock them again after the emerge", "Partitions that fstab mounts read-only are remounted read-only when the emerge finishes.");
        w::switch_row(ui, &mut s.boot_check_after_kernel, "Check the boot setup after kernel updates", "After an emerge that touches sys-kernel/*, sys-boot/* or nvidia-drivers, re-read the boot menu and notify about problems.");
        w::switch_row(ui, &mut s.nvidia_alerts, "NVIDIA module alerts", "Warn when a kernel has no NVIDIA module, or one that doesn't match the installed driver version.");
        ui.label(w::dim(format!("Known-good state: {}/last-good and previous-good{}", ks.state_dir.display(), if ks.state_dir.starts_with("/var/lib/kernel-fallback") { " (shared with your kernel-mark-good script)" } else { "" })));
        if !ks.local_tools.is_empty() {
            ui.label(w::dim(format!(
                "Detected your own tools: {}. PortageHub uses the same state files and set, so both stay in sync.",
                ks.local_tools.iter().map(|p| p.display().to_string()).collect::<Vec<_>>().join(", ")
            )));
        }
    });
}

// ---------------------------------------------------------------------------
// Cleanup

fn scanning(app: &App, tag: &str) -> bool {
    app.jobs.iter().any(|j| j.running() && matches!(&j.kind, JobKind::CleanupScan { tag: t } if t == tag))
}

fn start_scan(app: &mut App, tag: &str, title: &str, argv: &[&str]) {
    let mut a: Vec<String> = vec!["nice".into(), "-n".into(), "19".into()];
    a.extend(argv.iter().map(|s| s.to_string()));
    app.push(Action::Job { title: title.to_string(), argv: a, kind: JobKind::CleanupScan { tag: tag.to_string() } });
}

fn scan_header(app: &mut App, ui: &mut Ui, tag: &str, title: &str, argv: &[&str]) -> Option<(String, Option<i32>, i64)> {
    let running = scanning(app, tag);
    let result = app.cleanup_scans.get(tag).cloned();
    ui.horizontal(|ui| {
        if running {
            ui.add(egui::Spinner::new().color(ACCENT));
            ui.label(w::dim("scanning… (read-only, low priority)"));
        } else {
            if let Some((_, _, ts)) = &result {
                ui.label(w::dim(format!("scanned {}", crate::fmt::ago(*ts))));
            }
            if w::button(ui, icon::MAGNIFYING_GLASS, if result.is_some() { "Scan again" } else { "Scan" }).clicked() {
                start_scan(app, tag, title, argv);
            }
        }
    });
    result
}

fn section_card(ui: &mut Ui, ic: &str, title: &str, summary: &str, tint: Color32, body: impl FnOnce(&mut Ui)) {
    w::card_tinted(tint).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            ui.label(RichText::new(ic).size(24.0).color(tint));
            ui.vertical(|ui| {
                ui.label(RichText::new(title).size(16.0).family(theme::bold()).color(WHITE));
                if !summary.is_empty() {
                    ui.label(w::dim(summary));
                }
            });
        });
        body(ui);
    });
    ui.add_space(6.0);
}

fn cleanup_page(app: &mut App, ui: &mut Ui) {
    let left = app.data.leftovers.clone();
    let ks = app.data.kernels.clone();
    let cfg = app.data.config.clone();
    page(ui, "cleanup", |ui| {
        w::title(ui, "Cleanup");
        w::subtitle(ui, "Packages nothing needs any more, old kernels, downloaded sources, logs and other things left behind. Every scan is read-only; nothing is removed until you confirm.");
        ui.add_space(8.0);

        // Unused packages
        let dep = app.cleanup_scans.get("depclean").cloned();
        let plan = dep.as_ref().map(|(out, code, _)| pretend::parse(out, *code));
        let cands: Vec<String> = plan.as_ref().map(|p| p.depclean.clone()).unwrap_or_default();
        let summary = match &plan {
            None => "Not scanned yet".to_string(),
            Some(_) if cands.is_empty() => "Nothing to remove — every installed package is needed".to_string(),
            Some(_) => format!("{} package(s) nothing in @world needs", cands.len()),
        };
        let tint = if plan.is_none() { ACCENT } else if cands.is_empty() { GREEN } else { ORANGE };
        section_card(ui, icon::BROOM, "Unused packages", &summary, tint, |ui| {
            scan_header(app, ui, "depclean", "Scanning for unused packages (emerge --depclean --pretend)", &["emerge", "--pretend", "--depclean", "--color=n", "--nospinner", "--ask=n"]);
            if cands.is_empty() {
                return;
            }
            ui.label(w::dim("Untick what you want to keep, or press Keep to add it to @world (it then stops showing up here)."));
            let protected_pkgs: Vec<String> = ks
                .as_ref()
                .map(|k| k.kernels.iter().filter(|e| k.is_protected(&e.kver)).filter_map(|e| e.pkg.clone()).collect())
                .unwrap_or_default();
            egui::Grid::new("depclean-grid").num_columns(3).striped(true).spacing([14.0, 4.0]).show(ui, |ui| {
                for c in &cands {
                    let protected = protected_pkgs.contains(c);
                    let mut on = !protected && !app.maintenance.depclean_skip.contains(c);
                    let r = ui.add_enabled(!protected, egui::Checkbox::new(&mut on, RichText::new(c).color(if protected { TEXT_FAINT } else { TEXT })));
                    if r.changed() {
                        if on {
                            app.maintenance.depclean_skip.remove(c);
                        } else {
                            app.maintenance.depclean_skip.insert(c.clone());
                        }
                    }
                    if protected {
                        ui.label(RichText::new("protected: running or known-good kernel").size(12.0).color(ORANGE));
                    } else if c.starts_with("sys-kernel/") {
                        ui.label(RichText::new("old kernel").size(12.0).color(TEXT_DIM));
                    } else {
                        ui.label("");
                    }
                    let key = crate::portage::atom::split_cpv(c).map(|(a, b, _)| format!("{a}/{b}")).unwrap_or_default();
                    if !protected && ui.small_button(format!("{} Keep", icon::STAR)).on_hover_text(format!("emerge --noreplace {key}: add to @world")).clicked() {
                        app.push(Action::Root {
                            title: format!("Keep {key} (add to @world)"),
                            argv: vec!["emerge".into(), "--noreplace".into(), "--ask=n".into(), key.clone()],
                            reload: vec![Reload::Vdb],
                            kind: JobKind::Generic,
                        });
                        app.maintenance.depclean_skip.insert(c.clone());
                    } else if protected {
                        ui.label("");
                    }
                    ui.end_row();
                }
            });
            let selected: Vec<String> = cands.iter().filter(|c| !protected_pkgs.contains(c) && !app.maintenance.depclean_skip.contains(*c)).map(|c| format!("={c}")).collect();
            if ui.add_enabled_ui(!selected.is_empty(), |ui| w::danger(ui, icon::TRASH, &format!("Preview removing {} package(s)", selected.len()))).inner.clicked() {
                let t: Vec<&str> = selected.iter().map(|s| s.as_str()).collect();
                app.push(Action::Pretend(EmergeOptions { action: emerge::Action::Depclean, ..Default::default() }.targets(&t)));
            }
        });

        // Old kernels summary
        if let Some(ks) = &ks {
            let removable = ks.removable(app.settings.kernel_keep_newest);
            let old_files: usize = ks.kernels.iter().map(|k| k.old_files.len()).sum();
            let summary = format!(
                "{} kernel(s) can go under your rules · {} .old file(s) in /boot · {} kernel package(s) without files",
                removable.len(),
                old_files,
                ks.orphan_packages.len()
            );
            section_card(ui, icon::CPU, "Old kernels", &summary, if removable.is_empty() && old_files == 0 { GREEN } else { ORANGE }, |ui| {
                if w::button(ui, icon::ARROW_RIGHT, "Open Kernels & Boot").clicked() {
                    app.maintenance.page = Page::Kernels;
                }
            });
        }

        // Distfiles
        let dist = app.cleanup_scans.get("distfiles").cloned();
        let total = dist.as_ref().and_then(|(o, _, _)| cleanup::eclean_total(o));
        let summary = match (&dist, &total) {
            (None, _) => "Downloaded source archives in DISTDIR. Not scanned yet.".to_string(),
            (Some(_), Some((size, n))) => format!("{size}B in {n} files are not needed by installed packages (eclean-dist --deep)"),
            (Some(_), None) => "Nothing to clean".to_string(),
        };
        let tint = if dist.is_none() { ACCENT } else if total.is_some() { ORANGE } else { GREEN };
        section_card(ui, icon::ARCHIVE, "Downloaded sources (distfiles)", &summary, tint, |ui| {
            if !crate::exec_available("eclean-dist") {
                ui.label(w::dim("Install app-portage/gentoolkit to clean distfiles."));
                return;
            }
            let r = scan_header(app, ui, "distfiles", "Scanning distfiles (eclean-dist --pretend)", &["eclean-dist", "--deep", "--pretend"]);
            if let Some((out, _, _)) = r {
                let files = cleanup::eclean_files(&out);
                if !files.is_empty() {
                    egui::CollapsingHeader::new(format!("Show {} files", files.len())).id_salt("dist-files").show(ui, |ui| {
                        for (size, name) in files.iter().take(400) {
                            ui.label(w::mono(format!("{size:>9}  {name}")));
                        }
                    });
                    if w::danger(ui, icon::TRASH, "Clean distfiles").on_hover_text("eclean-dist --deep: keeps files of installed versions").clicked() {
                        app.push(Action::Root { title: "eclean-dist --deep".into(), argv: vec!["eclean-dist".into(), "--deep".into()], reload: vec![], kind: JobKind::Generic });
                        app.cleanup_scans.remove("distfiles");
                    }
                }
            }
        });

        // Binary packages
        if crate::exec_available("eclean-pkg") {
            let bin = app.cleanup_scans.get("binpkgs").cloned();
            let total = bin.as_ref().and_then(|(o, _, _)| cleanup::eclean_total(o));
            let summary = match (&bin, &total) {
                (None, _) => "Binary packages in PKGDIR. Not scanned yet.".to_string(),
                (Some(_), Some((size, n))) => format!("{size}B in {n} binary packages are obsolete"),
                (Some(_), None) => "Nothing to clean".to_string(),
            };
            let tint = if bin.is_none() { ACCENT } else if total.is_some() { ORANGE } else { GREEN };
            section_card(ui, icon::PACKAGE, "Binary packages", &summary, tint, |ui| {
                scan_header(app, ui, "binpkgs", "Scanning binary packages (eclean-pkg --pretend)", &["eclean-pkg", "--deep", "--pretend"]);
                if total.is_some() && w::danger(ui, icon::TRASH, "Clean binary packages").clicked() {
                    app.push(Action::Root { title: "eclean-pkg --deep".into(), argv: vec!["eclean-pkg".into(), "--deep".into()], reload: vec![], kind: JobKind::Generic });
                    app.cleanup_scans.remove("binpkgs");
                }
            });
        }

        let Some(left) = left else {
            w::loading(ui, "Looking for leftovers…");
            return;
        };
        // Logs
        let lg = &left.logs;
        if lg.readable {
            let summary = format!(
                "{} files, {} in {} · {} older than {} days ({})",
                lg.files,
                crate::fmt::size(lg.bytes),
                lg.dir.display(),
                lg.old_files,
                lg.days,
                crate::fmt::size(lg.old_bytes)
            );
            section_card(ui, icon::SCROLL, "Build logs", &summary, if lg.old_files > 0 { ORANGE } else { GREEN }, |ui| {
                ui.horizontal(|ui| {
                    ui.label(w::dim("Keep logs of the last"));
                    if ui.add(egui::DragValue::new(&mut app.settings.log_keep_days).range(1..=365).suffix(" days")).changed() {
                        app.push(Action::Reload(Reload::Leftovers));
                    }
                    if lg.old_files > 0 && w::danger(ui, icon::TRASH, &format!("Delete {} old logs", lg.old_files)).on_hover_text("emaint logs --clean --time N").clicked() {
                        app.push(Action::Root {
                            title: format!("Delete build logs older than {} days", app.settings.log_keep_days),
                            argv: vec!["emaint".into(), "logs".into(), "--clean".into(), "--time".into(), app.settings.log_keep_days.to_string()],
                            reload: vec![Reload::Leftovers],
                            kind: JobKind::Generic,
                        });
                    }
                });
            });
        }

        // Build dirs
        let bd_total: u64 = left.build_dirs.iter().map(|(_, s)| *s).sum();
        let summary = if left.build_dirs.is_empty() {
            "No leftover build directories".to_string()
        } else {
            format!("{} directories from failed or interrupted builds, {}", left.build_dirs.len(), crate::fmt::size(bd_total))
        };
        section_card(ui, icon::HAMMER, "Failed build leftovers", &summary, if left.build_dirs.is_empty() { GREEN } else { ORANGE }, |ui| {
            if left.build_dirs.is_empty() {
                return;
            }
            for (d, size) in &left.build_dirs {
                ui.label(w::mono(format!("{:>10}  {}", crate::fmt::size(*size), d.display())));
            }
            let running = app.running_emerge().is_some();
            if ui.add_enabled_ui(!running, |ui| w::danger(ui, icon::TRASH, "Delete build leftovers")).inner.on_disabled_hover_text("Wait until the running emerge has finished").clicked() {
                app.push(Action::RootOp { title: "Delete failed build directories".into(), op: Op::CleanBuildDirs, kind: JobKind::Generic, reload: vec![Reload::Leftovers] });
            }
        });

        // World cruft
        if !left.world_cruft.is_empty() {
            section_card(ui, icon::STAR_HALF, "World file entries for uninstalled packages", &format!("{} entries in /var/lib/portage/world match nothing installed", left.world_cruft.len()), ORANGE, |ui| {
                ui.label(w::dim("They make emerge -uDN @world install these packages again. Remove them if you don't want them."));
                ui.horizontal_wrapped(|ui| {
                    for a in &left.world_cruft {
                        if ui.button(format!("{}  {a}", icon::X)).on_hover_text(format!("emerge --deselect {a}")).clicked() {
                            app.push(Action::Root {
                                title: format!("Remove {a} from @world"),
                                argv: vec!["emerge".into(), "--deselect".into(), "--ask=n".into(), a.clone()],
                                reload: vec![Reload::Vdb, Reload::Leftovers],
                                kind: JobKind::Generic,
                            });
                        }
                    }
                });
            });
        }

        // Obsolete config lines
        let n = left.dead_entries.len() + left.redundant_keywords.len();
        let summary = if n == 0 { "Every /etc/portage entry still applies".to_string() } else { format!("{} dead entries, {} keyword entries no longer needed", left.dead_entries.len(), left.redundant_keywords.len()) };
        section_card(ui, icon::FILE_X, "Obsolete /etc/portage entries", &summary, if n == 0 { GREEN } else { ORANGE }, |ui| {
            if n == 0 {
                return;
            }
            ui.label(w::dim("Tick the lines to delete. Keyword entries for packages that are stable now are harmless, but they would pull in future testing versions."));
            for (title, list) in [("Package gone from every repository", &left.dead_entries), ("Already stable", &left.redundant_keywords)] {
                if list.is_empty() {
                    continue;
                }
                ui.label(w::strong(title));
                for h in list.iter() {
                    let key = (h.path.clone(), h.line);
                    let mut on = app.maintenance.config_selected.contains(&key);
                    ui.horizontal(|ui| {
                        if ui.checkbox(&mut on, RichText::new(&h.text).monospace().color(TEXT)).changed() {
                            if on {
                                app.maintenance.config_selected.insert(key.clone());
                            } else {
                                app.maintenance.config_selected.remove(&key);
                            }
                        }
                        ui.label(RichText::new(format!("{} · {}", h.path.display(), h.reason)).size(11.5).color(TEXT_FAINT));
                    });
                }
            }
            let sel: Vec<(PathBuf, usize)> = app.maintenance.config_selected.iter().cloned().collect();
            if ui.add_enabled_ui(!sel.is_empty(), |ui| w::danger(ui, icon::TRASH, &format!("Delete {} selected line(s)", sel.len()))).inner.clicked()
                && let Some(cfg) = &cfg
            {
                let ch = edits::remove_lines(cfg, &sel);
                app.push_write("Remove obsolete /etc/portage entries", ch, vec![Reload::Config, Reload::Leftovers], None);
                app.maintenance.config_selected.clear();
            }
        });

        // Preserved libs
        if !left.preserved_libs.is_empty() {
            section_card(ui, icon::BOOKS, "Preserved libraries", &format!("{} old librar(y/ies) kept because programs still use them", left.preserved_libs.len()), ORANGE, |ui| {
                for l in left.preserved_libs.iter().take(20) {
                    ui.label(w::mono(l));
                }
                if w::primary(ui, icon::WRENCH, "Preview emerge @preserved-rebuild").clicked() {
                    app.push(Action::Pretend(EmergeOptions::default().targets(&["@preserved-rebuild"])));
                }
            });
        }

        // News
        if let Some(news) = &app.data.news {
            let read = news.iter().filter(|n| !n.unread).count();
            if read > 0 {
                section_card(ui, icon::NEWSPAPER, "Read news items", &format!("{read} news items already read"), ACCENT, |ui| {
                    if w::button(ui, icon::TRASH, "Purge read news").on_hover_text("eselect news purge").clicked() {
                        app.push(Action::Root { title: "eselect news purge".into(), argv: vec!["eselect".into(), "news".into(), "purge".into()], reload: vec![Reload::News], kind: JobKind::Generic });
                    }
                });
            }
        }
        let _ = ToastKind::Info;
    });
}
