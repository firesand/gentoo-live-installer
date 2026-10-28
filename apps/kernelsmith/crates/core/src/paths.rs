//! Where Kernelsmith keeps its files.
//!
//! User-side state lives in XDG directories; everything written as root lives
//! under `/var/lib/kernelsmith` so it can be listed and removed cleanly.

use std::path::PathBuf;

pub const APP: &str = "kernelsmith";

/// Root-owned state: one manifest per installed kernel, tuning and Portage
/// integration records.
pub const SYSTEM_STATE: &str = "/var/lib/kernelsmith";
pub const SYSTEM_PATCH_DIR: &str = "/etc/kernelsmith/patches";
/// Persistent module signing key: one PEM holding the private key followed by
/// its certificate (the layout `scripts/sign-file` and Portage's
/// MODULES_SIGN_KEY both accept). Created by the build script on first use.
pub const SIGNING_KEY: &str = "/etc/kernelsmith/keys/modules-signing.pem";
/// Portage pieces that keep rebuilt out-of-tree drivers signed with that key.
pub const PORTAGE_SIGN_ENV: &str = "/etc/portage/env/kernelsmith-modules-sign.conf";
pub const PORTAGE_SIGN_PKGENV: &str = "/etc/portage/package.env/kernelsmith-modules-sign";
pub const PORTAGE_SIGN_USE: &str = "/etc/portage/package.use/kernelsmith-modules-sign";

pub fn system_state() -> PathBuf {
    PathBuf::from(SYSTEM_STATE)
}

pub fn kernel_manifests() -> PathBuf {
    system_state().join("kernels")
}

pub fn tuning_manifest() -> PathBuf {
    system_state().join("tuning.toml")
}

pub fn portage_manifests() -> PathBuf {
    system_state().join("portage")
}

fn home() -> PathBuf {
    dirs::home_dir().unwrap_or_else(|| PathBuf::from("/tmp"))
}

/// `~/.config/kernelsmith` — saved presets and settings.
pub fn config_dir() -> PathBuf {
    dirs::config_dir().unwrap_or_else(|| home().join(".config")).join(APP)
}

pub fn presets_dir() -> PathBuf {
    config_dir().join("presets")
}

pub fn settings_file() -> PathBuf {
    config_dir().join("settings.toml")
}

/// `~/.local/share/kernelsmith` — job directories, logs, local patches.
pub fn data_dir() -> PathBuf {
    dirs::data_dir()
        .unwrap_or_else(|| home().join(".local/share"))
        .join(APP)
}

pub fn jobs_dir() -> PathBuf {
    data_dir().join("jobs")
}

pub fn user_patch_dir() -> PathBuf {
    data_dir().join("patches")
}

pub fn profiles_dir() -> PathBuf {
    data_dir().join("autofdo")
}

pub fn history_file() -> PathBuf {
    data_dir().join("history.json")
}

/// `~/.cache/kernelsmith` — downloaded patches and catalog listings.
pub fn cache_dir() -> PathBuf {
    dirs::cache_dir().unwrap_or_else(|| home().join(".cache")).join(APP)
}

/// modprobed-db has used two locations over its lifetime; check both.
pub fn modprobed_db() -> Option<PathBuf> {
    let candidates = [
        dirs::data_dir().map(|d| d.join("modprobed-db/modprobed.db")),
        dirs::config_dir().map(|d| d.join("modprobed.db")),
    ];
    candidates.into_iter().flatten().find(|p| p.is_file())
}
