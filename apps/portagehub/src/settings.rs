//! User preferences persisted to ~/.config/portagehub/settings.json.

use serde::{Deserialize, Serialize};
use std::io::Write;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
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
    /// Settings from the user's file, and a note when something was wrong with it.
    pub fn load() -> (Settings, Option<String>) {
        Settings::load_from(&path())
    }

    /// A missing file means defaults. A file that cannot be parsed is kept as `<name>.bad` and
    /// replaced by defaults, saying so; a parsable file with absurd values (a zero interface scale
    /// used to crash egui at start-up, on every start) is corrected value by value.
    pub fn load_from(path: &Path) -> (Settings, Option<String>) {
        let text = match std::fs::read_to_string(path) {
            Ok(t) => t,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return (Settings::default(), None),
            Err(e) => return (Settings::default(), Some(format!("{} could not be read ({e}): using the defaults.", path.display()))),
        };
        match serde_json::from_str::<Settings>(&text) {
            Ok(mut s) => {
                let fixed = s.sanitize();
                (s, (!fixed.is_empty()).then(|| format!("Corrected values in {}: {}.", path.display(), fixed.join(", "))))
            }
            Err(e) => {
                let mut bad = path.as_os_str().to_owned();
                bad.push(".bad");
                let kept = std::fs::copy(path, &bad).is_ok();
                let note = format!("{} is not valid ({e}): using the defaults{}.", path.display(), if kept { ", the old file is kept as settings.json.bad" } else { "" });
                (Settings::default(), Some(note))
            }
        }
    }

    /// Put absurd values back to something the program can run with. Returns what was changed.
    pub fn sanitize(&mut self) -> Vec<&'static str> {
        let d = Settings::default();
        let mut fixed = Vec::new();
        if !self.ui_scale.is_finite() || self.ui_scale <= 0.0 {
            self.ui_scale = d.ui_scale;
            fixed.push("interface scale");
        } else if !(0.5..=3.0).contains(&self.ui_scale) {
            self.ui_scale = self.ui_scale.clamp(0.5, 3.0);
            fixed.push("interface scale");
        }
        if !(1..=3650).contains(&self.log_keep_days) {
            self.log_keep_days = self.log_keep_days.clamp(1, 3650);
            fixed.push("days to keep build logs");
        }
        if self.kernel_keep_newest > 20 {
            self.kernel_keep_newest = 20;
            fixed.push("kernels to keep");
        }
        // Files Portage ignores (leading '.', trailing '~') or that would leave the directory are a trap.
        let bad_name = |n: &str| n.trim().is_empty() || n.contains('/') || n.starts_with('.') || n.ends_with('~');
        if bad_name(&self.target_file) {
            self.target_file = d.target_file;
            fixed.push("file name for new entries");
        }
        if bad_name(&self.kernel_keep_set) {
            self.kernel_keep_set = d.kernel_keep_set;
            fixed.push("kernel keep set name");
        }
        if self.font_family.as_deref().is_some_and(|f| f.trim().is_empty()) {
            self.font_family = None;
        }
        fixed
    }

    pub fn save(&self) {
        let _ = self.save_to(&path());
    }

    /// Write through a temporary file and a rename, so a crash or a full disk never leaves half a file.
    pub fn save_to(&self, path: &Path) -> std::io::Result<()> {
        if let Some(d) = path.parent() {
            std::fs::create_dir_all(d)?;
        }
        let text = serde_json::to_string_pretty(self).map_err(std::io::Error::other)?;
        let mut tmp = path.as_os_str().to_owned();
        tmp.push(".tmp");
        let tmp = PathBuf::from(tmp);
        let result = (|| {
            let mut f = std::fs::File::create(&tmp)?;
            f.write_all(text.as_bytes())?;
            f.sync_all()?;
            std::fs::rename(&tmp, path)
        })();
        if result.is_err() {
            let _ = std::fs::remove_file(&tmp);
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("portagehub-settings-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn absurd_values_are_corrected_not_fatal() {
        for (json, scale) in [
            (r#"{"ui_scale": 0.0}"#, 1.0),
            (r#"{"ui_scale": -2.0}"#, 1.0),
            (r#"{"ui_scale": 99.0}"#, 3.0),
            (r#"{"ui_scale": 0.1}"#, 0.5),
            (r#"{"ui_scale": 1.25}"#, 1.25),
        ] {
            let d = scratch("scale");
            let p = d.join("settings.json");
            std::fs::write(&p, json).unwrap();
            let (s, note) = Settings::load_from(&p);
            assert_eq!(s.ui_scale, scale, "{json}");
            assert_eq!(note.is_some(), scale != 1.25, "{json}: {note:?}");
            let _ = std::fs::remove_dir_all(&d);
        }
        let mut s = Settings { ui_scale: f32::NAN, ..Default::default() };
        assert_eq!(s.sanitize(), vec!["interface scale"]);
        assert_eq!(s.ui_scale, 1.0);
        s.ui_scale = f32::INFINITY;
        s.sanitize();
        assert_eq!(s.ui_scale, 1.0);
    }

    #[test]
    fn other_fields_are_kept_in_range() {
        let mut s = Settings { log_keep_days: 0, kernel_keep_newest: 500, target_file: "  ".into(), kernel_keep_set: "../etc".into(), font_family: Some(" ".into()), ..Default::default() };
        let fixed = s.sanitize();
        assert_eq!(fixed, vec!["days to keep build logs", "kernels to keep", "file name for new entries", "kernel keep set name"]);
        assert_eq!((s.log_keep_days, s.kernel_keep_newest), (1, 20));
        assert_eq!((s.target_file.as_str(), s.kernel_keep_set.as_str()), ("zz-portagehub", "kernels-keep"));
        assert_eq!(s.font_family, None);
        s.log_keep_days = 100_000;
        s.sanitize();
        assert_eq!(s.log_keep_days, 3650);
        // Names Portage would silently ignore are not valid targets.
        for bad in [".hidden", "backup~", "a/b"] {
            let mut s = Settings { target_file: bad.into(), ..Default::default() };
            assert_eq!(s.sanitize(), vec!["file name for new entries"], "{bad}");
        }
        assert!(Settings::default().sanitize().is_empty(), "the defaults are valid");
    }

    #[test]
    fn a_corrupt_file_is_kept_and_replaced_by_defaults() {
        let d = scratch("corrupt");
        let p = d.join("settings.json");
        std::fs::write(&p, "{ not json").unwrap();
        let (s, note) = Settings::load_from(&p);
        assert_eq!(s, Settings::default());
        let note = note.unwrap();
        assert!(note.contains("is not valid") && note.contains("settings.json.bad"), "{note}");
        assert_eq!(std::fs::read_to_string(d.join("settings.json.bad")).unwrap(), "{ not json");
        // A missing file is simply the defaults, silently.
        let (s, note) = Settings::load_from(&d.join("missing.json"));
        assert_eq!((s, note), (Settings::default(), None));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn saving_is_atomic_and_round_trips() {
        let d = scratch("save");
        let p = d.join("sub/settings.json");
        let s = Settings { ui_scale: 1.5, beginner_tips: false, font_family: Some("Noto Sans".into()), ..Default::default() };
        s.save_to(&p).unwrap();
        assert!(!d.join("sub/settings.json.tmp").exists(), "no temporary file is left");
        let (back, note) = Settings::load_from(&p);
        assert_eq!((back, note), (s, None));
        // Overwriting keeps a valid file at every moment: the rename replaces it whole.
        let s2 = Settings { ui_scale: 0.75, ..Default::default() };
        s2.save_to(&p).unwrap();
        assert_eq!(Settings::load_from(&p).0.ui_scale, 0.75);
        // A directory in the way makes the save fail without leaving the temporary file around.
        let blocked = d.join("blocked");
        std::fs::create_dir_all(blocked.join("settings.json")).unwrap();
        assert!(s2.save_to(&blocked.join("settings.json")).is_err());
        assert!(!blocked.join("settings.json.tmp").exists());
        let _ = std::fs::remove_dir_all(&d);
    }
}
