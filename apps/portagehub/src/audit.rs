//! Append-only audit log of everything the helper is asked to do and how it
//! ended: `<root>/var/log/portagehub/audit.log`, one JSON object per line.
//!
//! The log is readable by the `portage` group only (`/var/log/portagehub` is 0750 and the file
//! 0640, both `root:portage`; without that group both are root-only), and it never fails
//! silently: whatever stops it recording is kept in [`Audit::problem`] for the session to show.
//! Policy stays fail-open (an unwritable log does not stop an upgrade), but the user is told.

use serde::{Deserialize, Serialize};
use std::io::Write;
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// The log is rotated once (to `audit.log.1`) when it grows past this.
pub const MAX_LOG_BYTES: u64 = 5 << 20;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Record {
    pub ts: i64,
    /// The user whose session made the request (PKEXEC_UID / SUDO_UID).
    pub uid: u32,
    pub id: u64,
    /// "request" or "done"
    pub event: String,
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

struct State {
    file: Option<std::fs::File>,
    /// Bytes in the current file.
    size: u64,
    /// Why records may be missing: the first thing that went wrong.
    problem: Option<String>,
}

pub struct Audit {
    path: PathBuf,
    state: Mutex<State>,
    uid: u32,
}

pub fn path_for(root: &Path) -> PathBuf {
    root.join("var/log/portagehub/audit.log")
}

/// The real user behind a privileged session.
pub fn session_uid() -> u32 {
    ["PKEXEC_UID", "SUDO_UID"]
        .iter()
        .find_map(|k| std::env::var(k).ok().and_then(|v| v.parse().ok()))
        .unwrap_or_else(|| unsafe { libc::getuid() })
}

/// The gid of the `portage` group, if the system has one.
fn portage_gid() -> Option<u32> {
    let gr = unsafe { libc::getgrnam(c"portage".as_ptr()) };
    (!gr.is_null()).then(|| unsafe { (*gr).gr_gid })
}

/// Give `fd` to group `gid`; whether it worked.
fn chown_group(fd: i32, gid: u32) -> bool {
    unsafe { libc::fchown(fd, u32::MAX, gid) == 0 }
}

/// Create (or open) the log with its directory. Returns the file and its current size.
fn open_log(path: &Path) -> Result<(std::fs::File, u64), String> {
    let gid = portage_gid();
    let dir = path.parent().ok_or("no directory")?;
    std::fs::DirBuilder::new().recursive(true).mode(0o750).create(dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    // Group access only if the group really owns what it is given access to.
    let group_ok = |fd: i32| gid.is_some_and(|g| chown_group(fd, g));
    let dir_fd = std::fs::File::open(dir).map_err(|e| format!("cannot open {}: {e}", dir.display()))?;
    let shared_dir = group_ok(dir_fd.as_raw_fd());
    std::fs::set_permissions(dir, std::fs::Permissions::from_mode(if shared_dir { 0o750 } else { 0o700 })).map_err(|e| format!("cannot protect {}: {e}", dir.display()))?;

    let file = std::fs::OpenOptions::new()
        .append(true)
        .create(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|e| format!("cannot open {}: {e}", path.display()))?;
    let shared_file = group_ok(file.as_raw_fd());
    // A log an older version made 0644 is tightened here.
    file.set_permissions(std::fs::Permissions::from_mode(if shared_file { 0o640 } else { 0o600 })).map_err(|e| format!("cannot protect {}: {e}", path.display()))?;
    let size = file.metadata().map(|m| m.len()).unwrap_or(0);
    Ok((file, size))
}

impl Audit {
    pub fn open(root: &Path) -> Audit {
        let path = path_for(root);
        let state = match open_log(&path) {
            Ok((file, size)) => State { file: Some(file), size, problem: None },
            Err(why) => State { file: None, size: 0, problem: Some(why) },
        };
        let audit = Audit { path, state: Mutex::new(state), uid: session_uid() };
        audit.rotate_if_needed();
        audit
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Why the log may be missing records, if it may: the first failure to open or write it.
    pub fn problem(&self) -> Option<String> {
        self.state.lock().unwrap().problem.clone()
    }

    fn rotate_if_needed(&self) {
        let mut st = self.state.lock().unwrap();
        if st.file.is_none() || st.size <= MAX_LOG_BYTES {
            return;
        }
        // One generation is kept: audit.log.1 is replaced.
        let mut old = self.path.clone().into_os_string();
        old.push(".1");
        st.file = None;
        match std::fs::rename(&self.path, &old).map_err(|e| format!("cannot rotate {}: {e}", self.path.display())).and_then(|_| open_log(&self.path)) {
            Ok((file, size)) => {
                st.file = Some(file);
                st.size = size;
            }
            Err(why) => {
                st.problem.get_or_insert(why);
            }
        }
    }

    pub fn log(&self, mut r: Record) {
        r.ts = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
        r.uid = self.uid;
        let Ok(line) = serde_json::to_string(&r) else { return };
        {
            let mut st = self.state.lock().unwrap();
            let Some(f) = st.file.as_mut() else { return };
            let written = writeln!(f, "{line}").and_then(|_| f.flush());
            match written {
                Ok(()) => st.size += line.len() as u64 + 1,
                Err(e) => {
                    let why = format!("cannot write {}: {e}", self.path.display());
                    st.problem.get_or_insert(why);
                }
            }
        }
        self.rotate_if_needed();
    }
}

/// Parse the log (newest last). Unparseable lines are skipped.
pub fn read(path: &Path) -> Vec<Record> {
    std::fs::read_to_string(path).unwrap_or_default().lines().filter_map(|l| serde_json::from_str(l).ok()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("portagehub-audit-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn rec(id: u64, event: &str) -> Record {
        Record { id, event: event.into(), what: "x".into(), ..Default::default() }
    }

    #[test]
    fn the_log_is_private_to_root_and_the_portage_group() {
        let d = scratch("modes");
        let a = Audit::open(&d);
        assert_eq!(a.problem(), None);
        a.log(rec(1, "request"));
        let mode = |p: &Path| std::fs::metadata(p).unwrap().permissions().mode() & 0o7777;
        let (dir_mode, file_mode) = (mode(a.path().parent().unwrap()), mode(a.path()));
        // 0750/0640 when the portage group could be applied, root-only otherwise: never open to others.
        assert!(dir_mode == 0o750 || dir_mode == 0o700, "{dir_mode:o}");
        assert!(file_mode == 0o640 || file_mode == 0o600, "{file_mode:o}");
        assert_eq!(read(a.path()).len(), 1);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn a_log_from_an_older_version_is_tightened() {
        let d = scratch("old");
        let dir = d.join("var/log/portagehub");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
        std::fs::write(dir.join("audit.log"), "{}\n").unwrap();
        std::fs::set_permissions(dir.join("audit.log"), std::fs::Permissions::from_mode(0o644)).unwrap();
        let a = Audit::open(&d);
        assert_eq!(a.problem(), None);
        assert_eq!(std::fs::metadata(&dir).unwrap().permissions().mode() & 0o007, 0, "no access for others");
        assert_eq!(std::fs::metadata(dir.join("audit.log")).unwrap().permissions().mode() & 0o007, 0);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn a_log_that_cannot_be_opened_is_reported_not_silent() {
        let d = scratch("blocked");
        // var/log is a plain file: the directory cannot be created.
        std::fs::create_dir_all(d.join("var")).unwrap();
        std::fs::write(d.join("var/log"), b"not a directory").unwrap();
        let a = Audit::open(&d);
        let why = a.problem().expect("the failure is recorded");
        assert!(why.contains("cannot create") && why.contains("portagehub"), "{why}");
        a.log(rec(1, "request")); // fail-open: logging into the void does not panic
        assert!(a.problem().is_some());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn a_write_failure_is_recorded_too() {
        let d = scratch("full");
        let a = Audit::open(&d);
        // Replace the handle by one that cannot write.
        a.state.lock().unwrap().file = Some(std::fs::File::open("/dev/null").unwrap());
        a.log(rec(1, "request"));
        assert!(a.problem().unwrap().contains("cannot write"), "{:?}", a.problem());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn the_log_rotates_to_a_single_older_generation() {
        let d = scratch("rotate");
        let a = Audit::open(&d);
        for i in 0..3 {
            a.log(rec(i, "request"));
        }
        a.state.lock().unwrap().size = MAX_LOG_BYTES + 1;
        a.log(rec(99, "request"));
        let older = PathBuf::from(format!("{}.1", a.path().display()));
        assert_eq!(read(&older).len(), 4, "everything up to and including the record that tripped it");
        a.log(rec(100, "done"));
        assert_eq!(read(a.path()).iter().map(|r| r.id).collect::<Vec<_>>(), vec![100]);
        assert_eq!(a.problem(), None);
        let _ = std::fs::remove_dir_all(&d);
    }
}
