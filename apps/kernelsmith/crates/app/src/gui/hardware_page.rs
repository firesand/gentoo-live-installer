//! Hardware: everything Kernelsmith detected, with why it matters.

use super::theme::*;
use super::App;
use eframe::egui::{self, Margin, RichText, ScrollArea, Ui};
use kernelsmith_core::hw::{BranchSampling, DiskKind, Initramfs, SystemInfo};
use kernelsmith_core::util::human_bytes;
use std::sync::Arc;

impl App {
    pub(super) fn hardware_page(&mut self, ui: &mut Ui, sys: &Arc<SystemInfo>) {
        ScrollArea::vertical().id_salt("hw").auto_shrink([false, false]).show(ui, |ui| {
            egui::Frame::new().inner_margin(Margin { left: 30, right: 30, top: 22, bottom: 30 }).show(ui, |ui| {
                ui.set_max_width(ui.available_width().min(1200.0));
                ui.horizontal(|ui| {
                    heading(ui, "Your hardware");
                    ui.add_space(16.0);
                    if button(ui, "↻  Rescan", Btn::Secondary).clicked() {
                        let ctx = ui.ctx().clone();
                        self.rescan_system(&ctx);
                    }
                });
                subheading(ui, "Kernelsmith uses this to pick drivers and optimizations. Nothing here leaves your PC.");
                ui.add_space(10.0);
                ui.columns(2, |cols| {
                    let (l, r) = cols.split_at_mut(1);
                    let l = &mut l[0];
                    let r = &mut r[0];

                    card_block(l, "⚡ Processor", |ui| {
                        egui::Grid::new("cpu").num_columns(2).spacing([16.0, 5.0]).show(ui, |ui| {
                            let c = &sys.cpu;
                            kv(ui, "Model", &c.model_name);
                            kv(ui, "Cores / threads", format!("{} / {}{}", c.cores, c.threads, if c.hybrid { " (P-cores + E-cores)" } else { "" }));
                            kv(ui, "Architecture", c.uarch_label());
                            kv(
                                ui,
                                "Branch sampling",
                                match c.branch_sampling {
                                    BranchSampling::IntelLbr => "Intel LBR — AutoFDO supported",
                                    BranchSampling::AmdLbrV2 => "AMD LBRv2 — AutoFDO supported",
                                    BranchSampling::AmdBrs => "AMD BRS — AutoFDO supported",
                                    BranchSampling::None => "Not available — no AutoFDO",
                                },
                            );
                        });
                        why(ui, "“Native” CPU optimization compiles the kernel for exactly this processor. Hybrid Intel CPUs also get Thread Director support so games land on fast P-cores.");
                    });
                    card_block(l, "🖥 Graphics", |ui| {
                        for g in &sys.gpus {
                            ui.label(RichText::new(&g.name).color(TEXT_BRIGHT));
                            dim(ui, &format!("PCI {} · driver: {}", g.slot, g.driver.as_deref().unwrap_or("none")));
                        }
                        if let Some(nv) = &sys.nvidia_driver {
                            status_line(ui, 0, &format!("x11-drivers/nvidia-drivers {} — rebuilt automatically for new kernels.", nv.version));
                        }
                        why(ui, "The proprietary NVIDIA driver needs a few kernel options (DRM KMS, framebuffer emulation, TTM helper) that slimming would otherwise drop. Kernelsmith always keeps them.");
                    });
                    card_block(l, "🗄 Memory & storage", |ui| {
                        dim(ui, &format!("{:.1} GiB RAM", sys.memory_gib()));
                        for d in &sys.storage {
                            let kind = match d.kind {
                                DiskKind::Nvme => "NVMe SSD",
                                DiskKind::Ssd => "SATA SSD",
                                DiskKind::Hdd => "Hard drive",
                                DiskKind::Usb => "USB drive",
                                DiskKind::Mmc => "SD / eMMC",
                                DiskKind::Virtual => "Virtual disk",
                            };
                            ui.horizontal(|ui| {
                                ui.label(RichText::new(&d.name).color(TEXT_BRIGHT));
                                dim(ui, &format!("{} · {kind} · {}", d.model, human_bytes(d.size_bytes)));
                            });
                        }
                        dim(ui, &format!("Root: {} ({}) · mounted: {}", sys.root_device, sys.root_fs, sys.mounted_fs.iter().cloned().collect::<Vec<_>>().join(", ")));
                        why(ui, "Your root filesystem and disk controllers are built directly into the kernel so it can always find its disk.");
                    });

                    card_block(r, "🚀 Boot & init", |ui| {
                        egui::Grid::new("boot").num_columns(2).spacing([16.0, 5.0]).show(ui, |ui| {
                            kv(ui, "Init system", sys.init.label());
                            kv(ui, "Bootloader", sys.boot.bootloader.label());
                            kv(ui, "Initramfs", if sys.boot.initramfs == Initramfs::None { "none".to_string() } else { sys.boot.initramfs.label().to_string() });
                            kv(
                                ui,
                                "installkernel",
                                sys.boot
                                    .installkernel
                                    .as_ref()
                                    .map(|(v, u)| {
                                        let relevant: Vec<&str> = u
                                            .iter()
                                            .map(String::as_str)
                                            .filter(|f| ["dracut", "ugrd", "grub", "systemd-boot", "uki", "efistub", "refind", "limine", "systemd"].contains(f))
                                            .collect();
                                        format!("{v} [{}]", relevant.join(" "))
                                    })
                                    .unwrap_or_else(|| "not installed".into()),
                            );
                            kv(ui, "Firmware", if sys.boot.uefi { "UEFI" } else { "BIOS" });
                            kv(
                                ui,
                                "Secure Boot",
                                match sys.boot.secure_boot {
                                    Some(true) => "on",
                                    Some(false) => "off",
                                    None => "unknown",
                                },
                            );
                            kv(ui, "Running kernel", &sys.running_kernel);
                        });
                        why(ui, "Kernelsmith installs through Gentoo's installkernel, which runs your initramfs generator and updates the boot menu exactly like `make install`.");
                    });
                    card_block(r, "🔧 Build tools", |ui| {
                        let t = &sys.toolchain;
                        status_line(ui, if t.gcc.is_some() { 0 } else { 2 }, &format!("GCC {}", t.gcc.as_deref().unwrap_or("missing")));
                        status_line(ui, if t.clang_ready() { 0 } else { 1 }, &t.clang.clone().unwrap_or_else(|| "Clang missing (no LTO/AutoFDO)".into()));
                        status_line(ui, if t.pahole { 0 } else { 1 }, if t.pahole { "pahole (BTF for sched_ext)" } else { "pahole missing — emerge dev-util/pahole" });
                        status_line(ui, if t.perf && t.llvm_profgen { 0 } else { 1 }, if t.perf && t.llvm_profgen { "perf + llvm-profgen (AutoFDO)" } else { "perf / llvm-profgen missing — AutoFDO unavailable" });
                        status_line(ui, if t.kmod_zstd { 0 } else { 1 }, if t.kmod_zstd { "kmod with zstd (compressed modules)" } else { "kmod without zstd support" });
                        for (name, pkg) in t.missing_essentials() {
                            status_line(ui, 2, &format!("{name} missing — emerge {pkg}"));
                        }
                    });
                    card_block(r, "🔩 Drivers in use", |ui| {
                        dim(ui, &format!("{} kernel modules loaded right now.", sys.loaded_modules.len()));
                        match &sys.modprobed_db {
                            Some(p) => status_line(ui, 0, &format!("modprobed-db history found: {}", p.display())),
                            None => status_line(ui, 1, "modprobed-db not used — it records modules over time for safer slimming."),
                        }
                        if !sys.external_modules.is_empty() {
                            ui.label(RichText::new("Out-of-tree drivers").color(TEXT_BRIGHT));
                            for p in &sys.external_modules {
                                dim(ui, &format!("• {}-{}", p.atom, p.version));
                            }
                        }
                        egui::CollapsingHeader::new("All loaded modules").id_salt("mods").show(ui, |ui| {
                            ui.label(RichText::new(sys.loaded_modules.join("  ")).font(mono(12.0)).color(TEXT_DIM));
                        });
                    });
                    card_block(r, "🌐 Network & audio", |ui| {
                        for n in &sys.network {
                            dim(ui, &format!("{} — {}{}", n.iface, n.driver.as_deref().unwrap_or("?"), if n.wireless { " (Wi-Fi)" } else { "" }));
                        }
                        for a in &sys.audio {
                            dim(ui, &format!("🔊 {a}"));
                        }
                        if sys.has_bluetooth {
                            dim(ui, "Bluetooth adapter present");
                        }
                    });
                });
            });
        });
    }
}

fn card_block(ui: &mut Ui, title_text: &str, add: impl FnOnce(&mut Ui)) {
    panel(ui, |ui| {
        ui.set_width(ui.available_width());
        title(ui, title_text);
        ui.add_space(4.0);
        add(ui);
    });
    ui.add_space(10.0);
}

fn why(ui: &mut Ui, text: &str) {
    ui.add_space(4.0);
    ui.add(egui::Label::new(RichText::new(format!("ℹ {text}")).color(ACCENT).size(12.5)).wrap());
}
