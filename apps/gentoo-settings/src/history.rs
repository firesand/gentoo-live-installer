//! The helper's record of every transaction, one JSON object per line, readable
//! by everyone so the GUI can show it and offer an undo.

use crate::detect::System;
use crate::plan::Change;
use serde::{Deserialize, Serialize};
use std::io::Write;

pub const HISTORY: &str = "/var/lib/gentoo-settings/history.jsonl";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entry {
    pub ts: i64,
    pub uid: u32,
    pub init: String,
    pub label: String,
    pub changes: Vec<Change>,
    /// Applying these puts the tweaks back as they were before.
    pub undo: Vec<Change>,
    pub files: Vec<String>,
    pub backup: Option<String>,
    pub ok: bool,
    #[serde(default)]
    pub warnings: Vec<String>,
}

/// Newest first.
pub fn load(sys: &System) -> Vec<Entry> {
    let mut v: Vec<Entry> = sys.read_raw(HISTORY).unwrap_or_default().lines().filter_map(|l| serde_json::from_str(l).ok()).collect();
    v.reverse();
    v
}

pub fn append(sys: &System, e: &Entry) -> std::io::Result<()> {
    let path = sys.path(HISTORY);
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&path)?;
    use std::os::unix::fs::PermissionsExt;
    let _ = f.set_permissions(std::fs::Permissions::from_mode(0o644));
    writeln!(f, "{}", serde_json::to_string(e).map_err(std::io::Error::other)?)
}
