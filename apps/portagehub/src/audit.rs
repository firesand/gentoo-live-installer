//! Append-only audit log of everything the helper is asked to do and how it
//! ended: `<root>/var/log/portagehub/audit.log`, one JSON object per line.

use serde::{Deserialize, Serialize};
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Record {
    pub ts: i64,
    /// The user whose session made the request (PKEXEC_UID / SUDO_UID).
    pub uid: u32,
    pub id: u64,
    /// "request", "done", "begin", "commit" or "rollback".
    pub event: String,
    /// Operation name (`write_file`, `run`, …) on "request" records.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub op: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tx: Option<String>,
    /// Short description of the operation (never file contents).
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub what: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ok: Option<bool>,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub detail: String,
    /// Files changed (on transaction commit/rollback).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub files: Vec<String>,
}

pub struct Audit {
    path: PathBuf,
    file: Mutex<Option<std::fs::File>>,
    uid: u32,
}

pub fn path_for(root: &Path) -> PathBuf {
    root.join("var/log/portagehub/audit.log")
}

/// The real user behind a privileged session.
pub fn session_uid() -> u32 {
    ["PKEXEC_UID", "SUDO_UID"].iter().find_map(|k| std::env::var(k).ok().and_then(|v| v.parse().ok())).unwrap_or_else(|| unsafe { libc::getuid() })
}

/// The log is rotated to `audit.log.1` when it grows past this.
const ROTATE_BYTES: u64 = 8 << 20;

impl Audit {
    pub fn open(root: &Path) -> Audit {
        let path = path_for(root);
        if std::fs::symlink_metadata(&path).is_ok_and(|m| m.is_file() && m.len() > ROTATE_BYTES) {
            let _ = std::fs::rename(&path, path.with_extension("log.1"));
        }
        let file = path
            .parent()
            .and_then(|d| std::fs::DirBuilder::new().recursive(true).mode(0o755).create(d).ok())
            .and_then(|_| std::fs::OpenOptions::new().append(true).create(true).mode(0o644).custom_flags(libc::O_NOFOLLOW).open(&path).ok());
        Audit { path, file: Mutex::new(file), uid: session_uid() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn log(&self, mut r: Record) {
        r.ts = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
        r.uid = self.uid;
        if let Some(f) = self.file.lock().unwrap().as_mut()
            && let Ok(line) = serde_json::to_string(&r)
        {
            let _ = writeln!(f, "{line}");
            let _ = f.flush();
        }
    }
}

/// Parse the log (oldest first). Unparseable lines are skipped; None when it
/// cannot be read (not created yet, or no permission).
pub fn read(path: &Path) -> Option<Vec<Record>> {
    let text = std::fs::read_to_string(path).ok()?;
    Some(text.lines().filter_map(|l| serde_json::from_str(l).ok()).collect())
}

/// The operation's name as it appears in the protocol (`write_file`, `run`, …).
pub fn op_name(op: &crate::protocol::Op) -> String {
    serde_json::to_value(op).ok().and_then(|v| v.get("op").and_then(|o| o.as_str()).map(String::from)).unwrap_or_default()
}
