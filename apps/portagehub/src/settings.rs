//! User preferences persisted to ~/.config/portagehub/settings.json.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    /// File name used inside package.* directories for entries PortageHub writes.
    pub target_file: String,
    /// Show advanced/expert options everywhere.
    pub show_advanced: bool,
    /// Show the confirmation dialog with a diff before writing config files.
    pub review_writes: bool,
    /// Keep a `~` backup of every file before it is replaced.
    pub backup_on_write: bool,
    /// Run previews as root (more accurate for some setups, but asks for the password).
    pub pretend_as_root: bool,
    /// Re-run the preview automatically after applying suggested fixes.
    pub auto_repretend: bool,
    pub ui_scale: f32,
    pub font_family: Option<String>,
    /// Show plain-language explanations and tips.
    pub beginner_tips: bool,
    pub log_wrap: bool,
    /// Run `emerge -uDN @world --pretend` in the background to verify the update list.
    pub auto_world_check: bool,
    /// Keep known-good kernels in a package set that is part of @world.
    pub kernel_protect: bool,
    pub kernel_keep_set: String,
    /// Copy the kernel's /boot files to /boot/kernel-backup when marking it known-good.
    pub kernel_backup_on_mark: bool,
    /// Require a working `nvidia-smi` before marking a kernel known-good (NVIDIA systems).
    pub kernel_mark_requires_nvidia: bool,
    /// Newest bootable kernels kept when cleaning (besides running and known-good).
    pub kernel_keep_newest: usize,
    /// Refuse to clean old kernels while the newest one is not marked known-good.
    pub kernel_require_marked_newest: bool,
    /// After an emerge touches kernels, bootloaders or NVIDIA drivers, check the boot setup.
    pub boot_check_after_kernel: bool,
    pub nvidia_alerts: bool,
    /// Build logs older than this are offered for cleaning.
    pub log_keep_days: u32,
    /// Remount read-only boot partitions read-write automatically before kernel/bootloader emerges.
    pub boot_auto_remount: bool,
    /// Put boot partitions that fstab mounts read-only back to read-only after the emerge.
    pub boot_restore_ro: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            target_file: "zz-portagehub".into(),
            show_advanced: false,
            review_writes: true,
            backup_on_write: false,
            pretend_as_root: false,
            auto_repretend: true,
            ui_scale: 1.0,
            font_family: None,
            beginner_tips: true,
            log_wrap: false,
            auto_world_check: true,
            kernel_protect: true,
            kernel_keep_set: "kernels-keep".into(),
            kernel_backup_on_mark: true,
            kernel_mark_requires_nvidia: true,
            kernel_keep_newest: 1,
            kernel_require_marked_newest: true,
            boot_check_after_kernel: true,
            nvidia_alerts: true,
            log_keep_days: 30,
            boot_auto_remount: false,
            boot_restore_ro: true,
        }
    }
}

fn path() -> PathBuf {
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))
        .unwrap_or_else(|| PathBuf::from("/tmp"));
    base.join("portagehub").join("settings.json")
}

impl Settings {
    pub fn load() -> Settings {
        std::fs::read_to_string(path()).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default()
    }

    pub fn save(&self) {
        let p = path();
        if let Some(d) = p.parent() {
            let _ = std::fs::create_dir_all(d);
        }
        if let Ok(t) = serde_json::to_string_pretty(self) {
            let _ = std::fs::write(p, t);
        }
    }
}
