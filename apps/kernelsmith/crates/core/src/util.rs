//! Small helpers shared by the detection and planning code.

use std::ffi::CString;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Read a file and trim whitespace; `None` if it can't be read.
pub fn read_trim(path: impl AsRef<Path>) -> Option<String> {
    std::fs::read_to_string(path).ok().map(|s| s.trim().to_string())
}

/// Run a command and return trimmed stdout when it exits successfully.
pub fn run(cmd: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(cmd).args(args).output().ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Like `run` but returns stdout+stderr regardless of exit status.
pub fn run_any(cmd: &str, args: &[&str]) -> Option<(bool, String)> {
    let out = Command::new(cmd).args(args).output().ok()?;
    let mut text = String::from_utf8_lossy(&out.stdout).to_string();
    text.push_str(&String::from_utf8_lossy(&out.stderr));
    Some((out.status.success(), text))
}

/// Locate an executable on PATH (plus Gentoo's LLVM and sbin dirs, which are
/// not always on a user's PATH).
pub fn which(name: &str) -> Option<PathBuf> {
    let mut dirs: Vec<PathBuf> = std::env::var_os("PATH")
        .map(|p| std::env::split_paths(&p).collect())
        .unwrap_or_default();
    for extra in ["/usr/sbin", "/sbin", "/usr/bin", "/bin"] {
        dirs.push(PathBuf::from(extra));
    }
    dirs.extend(llvm_bin_dirs());
    dirs.into_iter()
        .map(|d| d.join(name))
        .find(|p| is_executable(p))
}

fn is_executable(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(p)
        .map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

/// Gentoo installs LLVM slots under /usr/lib/llvm/<slot>/bin; newest first.
pub fn llvm_bin_dirs() -> Vec<PathBuf> {
    let mut slots: Vec<(u32, PathBuf)> = std::fs::read_dir("/usr/lib/llvm")
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| {
            let slot = e.file_name().to_string_lossy().parse::<u32>().ok()?;
            let bin = e.path().join("bin");
            bin.is_dir().then_some((slot, bin))
        })
        .collect();
    slots.sort_by_key(|s| std::cmp::Reverse(s.0));
    slots.into_iter().map(|(_, p)| p).collect()
}

/// Quote a string for safe use in a POSIX shell script.
pub fn shell_quote(s: &str) -> String {
    if !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || "-_./=:,+@%".contains(c))
    {
        return s.to_string();
    }
    format!("'{}'", s.replace('\'', r"'\''"))
}

pub fn human_bytes(bytes: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut v = bytes as f64;
    let mut i = 0;
    while v >= 1024.0 && i < UNITS.len() - 1 {
        v /= 1024.0;
        i += 1;
    }
    if i == 0 {
        format!("{bytes} B")
    } else {
        format!("{v:.1} {}", UNITS[i])
    }
}

/// Free space available to unprivileged users on the filesystem holding `path`.
pub fn disk_free(path: &Path) -> Option<u64> {
    let c = CString::new(path.as_os_str().to_string_lossy().as_bytes()).ok()?;
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::statvfs(c.as_ptr(), &mut st) };
    (rc == 0).then(|| st.f_bavail as u64 * st.f_frsize as u64)
}

pub fn is_root() -> bool {
    unsafe { libc::geteuid() == 0 }
}

pub fn uid() -> u32 {
    unsafe { libc::getuid() }
}

pub fn gid() -> u32 {
    unsafe { libc::getgid() }
}

/// Current time as seconds since the Unix epoch.
pub fn now_epoch() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Format an epoch timestamp as `YYYY-MM-DD HH:MM` (UTC) without extra deps.
pub fn format_epoch(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let (y, m, d) = civil_from_days(days);
    format!("{y:04}-{m:02}-{d:02} {:02}:{:02}", rem / 3600, (rem % 3600) / 60)
}

/// Compact timestamp for directory names: `YYYYMMDD-HHMMSS`.
pub fn stamp(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let (y, m, d) = civil_from_days(days);
    format!(
        "{y:04}{m:02}{d:02}-{:02}{:02}{:02}",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

// Howard Hinnant's days -> civil date algorithm.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// Sanitise a user-entered name into something valid for LOCALVERSION and
/// directory names.
pub fn slug(s: &str) -> String {
    let mut out = String::new();
    for c in s.trim().chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c.to_ascii_lowercase());
        } else if (c == '-' || c == '_' || c == ' ' || c == '.') && !out.ends_with('-') {
            out.push('-');
        }
    }
    out.trim_matches('-').chars().take(32).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quoting() {
        assert_eq!(shell_quote("abc-1.2"), "abc-1.2");
        assert_eq!(shell_quote("a b"), "'a b'");
        assert_eq!(shell_quote("it's"), r"'it'\''s'");
        assert_eq!(shell_quote(""), "''");
    }

    #[test]
    fn slugs() {
        assert_eq!(slug("My Gaming Kernel!"), "my-gaming-kernel");
        assert_eq!(slug("  --x__y "), "x-y");
    }

    #[test]
    fn dates() {
        assert_eq!(format_epoch(0), "1970-01-01 00:00");
        assert_eq!(stamp(1_700_000_000), "20231114-221320");
    }
}
