//! Dialogs.

use super::jobview::JobKind;
use super::theme::*;
use super::{list_presets, open_path, App};
use eframe::egui::{self, CornerRadius, Margin, RichText, ScrollArea, Stroke};
use kernelsmith_core::hw::SystemInfo;
use kernelsmith_core::profiles::{self, ProfileId};
use kernelsmith_core::{autofdo, library, paths, portage};
use std::sync::Arc;

pub enum Modal {
    Message { title: String, body: String },
    Script { title: String, text: String },
    ConfirmProfile(ProfileId),
    ConfirmUninstall(String),
    Portage { package: usize, rebuild: bool },
    Settings,
    Profiling { release: String, minutes: u32, propeller: bool },
    SavePreset { name: String },
    ConfirmQuit,
}

impl Modal {
    pub fn message(title: impl Into<String>, body: impl Into<String>) -> Self {
        Modal::Message { title: title.into(), body: body.into() }
    }
}

impl App {
    pub(super) fn modal_ui(&mut self, ctx: &egui::Context, sys: &Arc<SystemInfo>) {
        let Some(mut modal) = self.modal.take() else { return };
        let mut close = false;
        let mut next: Option<Modal> = None;
        let frame = egui::Frame::new()
            .fill(PANEL)
            .stroke(Stroke::new(1.0_f32, BORDER))
            .corner_radius(CornerRadius::same(6))
            .inner_margin(Margin::same(22));
        let resp = egui::Modal::new(egui::Id::new("ks-modal")).frame(frame).show(ctx, |ui| {
            ui.set_max_width(ui.available_width().min(860.0));
            match &mut modal {
                Modal::Message { title: t, body } => {
                    title(ui, t);
                    ui.add_space(6.0);
                    ui.add(egui::Label::new(RichText::new(body.as_str()).color(TEXT)).wrap());
                    ui.add_space(12.0);
                    if button(ui, "OK", Btn::Blue).clicked() {
                        close = true;
                    }
                }
                Modal::Script { title: t, text } => {
                    ui.set_min_width(820.0);
                    title(ui, t);
                    dim(ui, "Plain bash — you can read exactly what will run.");
                    ui.add_space(6.0);
                    egui::Frame::new().fill(BG_DEEP).corner_radius(CornerRadius::same(4)).inner_margin(Margin::same(10)).show(ui, |ui| {
                        ScrollArea::both().max_height(520.0).auto_shrink([false, true]).show(ui, |ui| {
                            ui.add(egui::Label::new(RichText::new(text.as_str()).font(mono(12.5)).color(TEXT)).extend());
                        });
                    });
                    ui.add_space(10.0);
                    ui.horizontal(|ui| {
                        if button(ui, "Copy", Btn::Secondary).clicked() {
                            ui.ctx().copy_text(text.clone());
                        }
                        if button(ui, "Close", Btn::Blue).clicked() {
                            close = true;
                        }
                    });
                }
                Modal::ConfirmProfile(id) => {
                    let pr = profiles::get(*id);
                    title(ui, &format!("Switch to {} {}?", pr.icon, pr.title));
                    ui.add_space(6.0);
                    ui.label("Your option changes will be replaced by this profile's recommended settings. Your source, patches and install options are kept.");
                    ui.add_space(12.0);
                    ui.horizontal(|ui| {
                        if button(ui, "Switch profile", Btn::Primary).clicked() {
                            if let Some(mut plan) = self.wiz.plan.take() {
                                super::wizard::apply_profile(&mut self.wiz, &mut plan, sys, *id);
                                self.wiz.plan = Some(plan);
                            }
                            close = true;
                        }
                        if button(ui, "Cancel", Btn::Secondary).clicked() {
                            close = true;
                        }
                    });
                }
                Modal::ConfirmUninstall(release) => {
                    let kernels = self.library.ready().cloned().unwrap_or_default();
                    let Some(k) = kernels.iter().find(|k| &k.release == release) else {
                        close = true;
                        return;
                    };
                    title(ui, &format!("Uninstall {release}?"));
                    ui.add_space(6.0);
                    ui.label("Kernelsmith will remove exactly what it installed for this kernel and update the boot menu:");
                    if let Some(m) = k.manifest() {
                        for f in &m.files {
                            dim(ui, &format!("  • {}", f.display()));
                        }
                        dim(ui, &format!("  • {}", m.modules_dir.display()));
                        dim(ui, &format!("  • {} (build tree)", m.tree.display()));
                    }
                    ui.add_space(12.0);
                    ui.horizontal(|ui| {
                        if button(ui, "Uninstall", Btn::Danger).clicked() {
                            match library::uninstall_job(k, sys) {
                                Ok(job) => self.start_job(job, JobKind::Uninstall { release: release.clone() }, true),
                                Err(e) => next = Some(Modal::message("Can't uninstall", format!("{e:#}"))),
                            }
                            close = true;
                        }
                        if button(ui, "Cancel", Btn::Secondary).clicked() {
                            close = true;
                        }
                    });
                }
                Modal::Portage { package, rebuild } => {
                    ui.set_min_width(780.0);
                    title(ui, "Let Portage maintain your kernel");
                    ui.add(
                        egui::Label::new(
                            "Kernelsmith writes your choices as a config fragment that Gentoo's distribution kernels merge automatically, \
                             plus any patches into /etc/portage/patches. Every future `emerge -u` rebuilds the kernel with them.",
                        )
                        .wrap(),
                    );
                    ui.add_space(8.0);
                    for (i, (atom, desc)) in portage::DIST_KERNELS.iter().enumerate() {
                        let installed = sys.dist_kernels.iter().any(|p| &p.atom == atom);
                        ui.horizontal(|ui| {
                            ui.radio_value(package, i, RichText::new(*atom).color(TEXT_BRIGHT));
                            dim(ui, desc);
                            if installed {
                                badge(ui, "Installed", GREEN);
                            }
                        });
                    }
                    let Some(plan) = self.wiz.plan.clone() else {
                        dim(ui, "Set up a plan in the Build wizard first.");
                        return;
                    };
                    let src = self.sources.ready().and_then(|v| v.iter().find(|s| s.path == plan.source).cloned());
                    let symbols = src.as_ref().and_then(|s| self.symbols.get(&s.path)).and_then(|l| l.ready()).cloned();
                    let atom = portage::DIST_KERNELS[*package].0;
                    let ex = portage::export(&plan, sys, src.as_ref(), symbols.as_deref(), atom);
                    ui.add_space(6.0);
                    dim(ui, &format!("Writes {}", ex.fragment_path));
                    if let Some(u) = &ex.package_use {
                        dim(ui, "and USE flags:");
                        ui.label(RichText::new(u).font(mono(12.0)).color(TEXT));
                    }
                    if !plan.patches.is_empty() {
                        dim(ui, &format!("and {} patch(es) into {}", plan.patches.len(), ex.patch_dir));
                    }
                    egui::Frame::new().fill(BG_DEEP).corner_radius(CornerRadius::same(4)).inner_margin(Margin::same(8)).show(ui, |ui| {
                        ScrollArea::vertical().max_height(220.0).auto_shrink([false, true]).show(ui, |ui| {
                            ui.label(RichText::new(&ex.fragment).font(mono(12.0)).color(TEXT_DIM));
                        });
                    });
                    ui.checkbox(rebuild, format!("Rebuild {atom} right away (takes as long as a full kernel build)"));
                    ui.add_space(10.0);
                    ui.horizontal(|ui| {
                        if button(ui, "Apply", Btn::Primary).clicked() {
                            match portage::install_job(&plan, &ex, *rebuild) {
                                Ok(job) => self.start_job(job, JobKind::Portage, true),
                                Err(e) => next = Some(Modal::message("Couldn't prepare", format!("{e:#}"))),
                            }
                            close = true;
                        }
                        if button(ui, "Cancel", Btn::Secondary).clicked() {
                            close = true;
                        }
                    });
                }
                Modal::Settings => {
                    ui.set_min_width(560.0);
                    title(ui, "Settings");
                    ui.add_space(6.0);
                    ui.horizontal(|ui| {
                        if switch(ui, &mut self.settings.show_advanced, true).changed() {
                            self.wiz.show_advanced = self.settings.show_advanced;
                            self.settings.save();
                        }
                        ui.label("Show expert options (mitigations, real-time, full tickless, Rust…)");
                    });
                    ui.horizontal(|ui| {
                        if switch(ui, &mut self.settings.offline, true).changed() {
                            self.settings.save();
                            self.patch_status.clear();
                        }
                        ui.label("Offline mode (use cached patch listings only)");
                    });
                    ui.add_space(8.0);
                    section(ui, "Folders");
                    ui.horizontal_wrapped(|ui| {
                        for (label, path) in [
                            ("Presets", paths::presets_dir()),
                            ("My patches", paths::user_patch_dir()),
                            ("Build jobs & logs", paths::jobs_dir()),
                            ("Download cache", paths::cache_dir()),
                        ] {
                            if ui.button(label).clicked() {
                                let _ = std::fs::create_dir_all(&path);
                                open_path(&path);
                            }
                        }
                    });
                    if ui.button("Clear patch cache").clicked() {
                        let _ = std::fs::remove_dir_all(paths::cache_dir().join("listings"));
                        let _ = std::fs::remove_dir_all(paths::cache_dir().join("patches"));
                        self.patch_status.clear();
                    }
                    ui.add_space(8.0);
                    section(ui, "About");
                    dim(ui, &format!("Kernelsmith {} — guided custom kernels for Gentoo. GPL-2.0-or-later.", env!("CARGO_PKG_VERSION")));
                    dim(ui, "Inspired by CachyOS, Clear Linux and the Gentoo kernel handbook. Patches © their authors.");
                    ui.add_space(12.0);
                    if button(ui, "Close", Btn::Blue).clicked() {
                        close = true;
                    }
                }
                Modal::Profiling { release, minutes, propeller } => {
                    ui.set_min_width(640.0);
                    title(ui, "Optimize with AutoFDO");
                    ui.add(
                        egui::Label::new(
                            "Kernelsmith will record which kernel code paths your real workload uses (with `perf`), turn that into an \
                             AutoFDO profile, and then rebuild this kernel so the compiler lays out and optimizes the hot paths. \
                             This is the profile-guided technique behind much of Clear Linux's speed.",
                        )
                        .wrap(),
                    );
                    ui.add_space(6.0);
                    let manifests = library::load_manifests();
                    let Some(m) = manifests.get(release.as_str()) else {
                        dim(ui, "This kernel has no Kernelsmith manifest.");
                        return;
                    };
                    let blockers = autofdo::blockers(m, sys);
                    for b in &blockers {
                        status_line(ui, 2, b);
                    }
                    ui.horizontal(|ui| {
                        ui.label("Record for");
                        ui.add(egui::Slider::new(minutes, 1..=30).suffix(" min"));
                    });
                    dim(ui, "Start your game or workload first, then press Start — keep using the PC normally until it finishes.");
                    if sys.toolchain.propeller_tool {
                        ui.checkbox(propeller, "Also create a Propeller profile (code layout)");
                    }
                    let existing = autofdo::profiles_for(release);
                    if !existing.is_empty() {
                        ui.add_space(6.0);
                        section(ui, "Recorded profiles");
                        for p in existing {
                            ui.horizontal(|ui| {
                                dim(ui, &p.display().to_string());
                                if ui.small_button("Rebuild with this").clicked() {
                                    self.start_autofdo_rebuild(release, p.clone());
                                    close = true;
                                }
                            });
                        }
                    }
                    ui.add_space(10.0);
                    ui.horizontal(|ui| {
                        if button_enabled(ui, "Start recording", Btn::Primary, blockers.is_empty()).clicked() {
                            match autofdo::record_job(m, sys, *minutes, *propeller) {
                                Ok((job, _)) => self.start_job(job, JobKind::Profile { release: release.clone() }, true),
                                Err(e) => next = Some(Modal::message("Can't start profiling", format!("{e:#}"))),
                            }
                            close = true;
                        }
                        if button(ui, "Cancel", Btn::Secondary).clicked() {
                            close = true;
                        }
                    });
                }
                Modal::ConfirmQuit => {
                    title(ui, "A job is still running");
                    ui.add_space(6.0);
                    ui.add(
                        egui::Label::new(
                            "Quitting cancels it. An unfinished build is cleaned up automatically; if installation had already \
                             started, you can remove the partial kernel from the Library later.",
                        )
                        .wrap(),
                    );
                    ui.add_space(12.0);
                    ui.horizontal(|ui| {
                        if button(ui, "Keep running", Btn::Primary).clicked() {
                            close = true;
                        }
                        if button(ui, "Cancel job and quit", Btn::Danger).clicked() {
                            self.allow_close = true;
                            if let Some(job) = self.job.as_mut() {
                                job.cancel();
                            }
                            ui.ctx().send_viewport_cmd(egui::ViewportCommand::Close);
                            close = true;
                        }
                    });
                }
                Modal::SavePreset { name } => {
                    title(ui, "Save as preset");
                    dim(ui, "Presets remember every choice so you can rebuild the same kernel for each new version in two clicks.");
                    ui.add_space(6.0);
                    ui.add(egui::TextEdit::singleline(name).desired_width(320.0));
                    ui.add_space(10.0);
                    ui.horizontal(|ui| {
                        let file = paths::presets_dir().join(format!("{}.toml", kernelsmith_core::util::slug(name)));
                        if button_enabled(ui, "Save", Btn::Primary, !kernelsmith_core::util::slug(name).is_empty()).clicked() {
                            if let Some(plan) = self.wiz.plan.as_ref() {
                                match plan.save(&file) {
                                    Ok(()) => {
                                        self.presets = list_presets();
                                        self.toast(format!("Saved {}", file.display()), GREEN);
                                    }
                                    Err(e) => next = Some(Modal::message("Couldn't save", format!("{e:#}"))),
                                }
                            }
                            close = true;
                        }
                        if button(ui, "Cancel", Btn::Secondary).clicked() {
                            close = true;
                        }
                    });
                }
            }
        });
        if resp.should_close() {
            close = true;
        }
        if let Some(n) = next {
            self.modal = Some(n);
        } else if !close && self.modal.is_none() {
            self.modal = Some(modal);
        }
    }
}
