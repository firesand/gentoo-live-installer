//! What the GUI sends the helper (one JSON [`Request`] on stdin) and what the
//! helper answers (JSON [`Event`]s, one per line, on stdout).

use crate::detect::Init;
use crate::plan::Change;
use serde::{Deserialize, Serialize};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Request {
    /// Must equal the helper's version: both compute plans from their own catalog.
    pub version: String,
    /// Shown in the history ("Apply CachyOS defaults", "Undo …").
    #[serde(default)]
    pub label: String,
    pub changes: Vec<Change>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FileAction {
    Create,
    Modify,
    Delete,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "ev", rename_all = "snake_case")]
pub enum Event {
    Ready { version: String, uid: u32, init: Init, dry_run: bool },
    Step { text: String },
    File { path: String, action: FileAction },
    Command { text: String, ok: bool, output: String, skipped: bool },
    Done { ok: bool, backup: Option<String>, warnings: Vec<String> },
    Error { msg: String },
}
