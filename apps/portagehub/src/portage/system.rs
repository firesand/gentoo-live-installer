//! System-level facts: effective Portage variables (portageq), profiles,
//! news items and pending configuration-file updates.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;

pub const ENV_VARS: &[&str] = &[
    "ARCH",
    "ACCEPT_KEYWORDS",
    "ACCEPT_LICENSE",
    "USE",
    "FEATURES",
    "MAKEOPTS",
    "EMERGE_DEFAULT_OPTS",
    "CHOST",
    "CFLAGS",
    "CXXFLAGS",
    "LDFLAGS",
    "PORTAGE_TMPDIR",
    "DISTDIR",
    "PKGDIR",
    "PORT_LOGDIR",
    "USE_EXPAND",
    "VIDEO_CARDS",
    "INPUT_DEVICES",
    "PYTHON_TARGETS",
    "PYTHON_SINGLE_TARGET",
    "L10N",
    "CONFIG_PROTECT",
];

#[derive(Debug, Clone, Default)]
pub struct Env {
    pub vars: HashMap<String, String>,
    pub portage_version: String,
    pub ok: bool,
}

impl Env {
    pub fn get(&self, k: &str) -> &str {
        self.vars.get(k).map(|s| s.as_str()).unwrap_or("")
    }

    pub fn arch(&self) -> &str {
        let a = self.get("ARCH");
        if a.is_empty() { "amd64" } else { a }
    }

    pub fn accepts_testing(&self) -> bool {
        let arch = self.arch();
        self.get("ACCEPT_KEYWORDS").split_whitespace().any(|k| k == format!("~{arch}") || k == "~*" || k == "**")
    }

    pub fn global_use(&self) -> Vec<String> {
        self.get("USE").split_whitespace().map(String::from).collect()
    }
}

pub fn load_env() -> Env {
    let mut env = Env::default();
    let out = Command::new("portageq").arg("envvar").arg("-v").args(ENV_VARS).output();
    if let Ok(o) = out {
        let text = String::from_utf8_lossy(&o.stdout);
        for line in text.lines() {
            if let Some((k, v)) = line.split_once('=') {
                env.vars.insert(k.to_string(), v.trim_matches('\'').trim_matches('"').to_string());
            }
        }
        env.ok = !env.vars.is_empty();
    }
    if let Ok(o) = Command::new("portageq").arg("--version").output() {
        env.portage_version = String::from_utf8_lossy(&o.stdout).trim().trim_start_matches("Portage ").to_string();
    }
    if env.portage_version.is_empty()
        && let Ok(rd) = std::fs::read_dir("/var/db/pkg/sys-apps")
    {
        for e in rd.flatten() {
            let n = e.file_name().to_string_lossy().to_string();
            if let Some(v) = n.strip_prefix("portage-")
                && v.starts_with(|c: char| c.is_ascii_digit())
            {
                env.portage_version = v.to_string();
            }
        }
    }
    env
}

#[derive(Debug, Clone)]
pub struct Profile {
    pub num: usize,
    pub path: String,
    pub status: String,
    pub current: bool,
}

pub fn parse_profiles(text: &str) -> Vec<Profile> {
    let mut out = Vec::new();
    for l in text.lines() {
        let t = l.trim();
        let Some(rest) = t.strip_prefix('[') else { continue };
        let Some((n, rest)) = rest.split_once(']') else { continue };
        let Ok(num) = n.trim().parse() else { continue };
        let rest = rest.trim();
        let current = rest.ends_with('*');
        let rest = rest.trim_end_matches('*').trim();
        let (path, status) = match rest.split_once(" (") {
            Some((p, s)) => (p.trim().to_string(), s.trim_end_matches(')').to_string()),
            None => (rest.to_string(), String::new()),
        };
        out.push(Profile { num, path, status, current });
    }
    out
}

#[derive(Debug, Clone)]
pub struct NewsItem {
    pub name: String,
    pub title: String,
    pub posted: String,
    pub unread: bool,
    pub path: PathBuf,
}

/// News items of a repository plus the unread list Portage keeps.
pub fn load_news(repo: &str, repo_loc: &Path) -> Vec<NewsItem> {
    let unread: Vec<String> = std::fs::read_to_string(format!("/var/lib/gentoo/news/news-{repo}.unread"))
        .unwrap_or_default()
        .lines()
        .map(|l| l.trim().to_string())
        .collect();
    let mut out = Vec::new();
    for e in std::fs::read_dir(repo_loc.join("metadata/news")).into_iter().flatten().flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        let path = e.path().join(format!("{name}.en.txt"));
        let Ok(text) = std::fs::read_to_string(&path) else { continue };
        let mut title = String::new();
        let mut posted = String::new();
        for l in text.lines().take(20) {
            if let Some(t) = l.strip_prefix("Title: ") {
                title = t.trim().to_string();
            } else if let Some(p) = l.strip_prefix("Posted: ") {
                posted = p.trim().to_string();
            }
        }
        out.push(NewsItem { unread: unread.contains(&name), name, title, posted, path });
    }
    out.sort_by(|a, b| b.posted.cmp(&a.posted));
    out
}

/// A pending `._cfgXXXX_name` file: the new version Portage saved beside the file you edited.
#[derive(Debug, Clone)]
pub struct PendingUpdate {
    /// The `._cfgXXXX_name` file.
    pub update: PathBuf,
    /// The file it would replace.
    pub target: PathBuf,
    /// Contents as the current user can read them (`None`: root-only, missing or too large to preview).
    pub old: Option<String>,
    pub new: Option<String>,
}

/// Largest file whose contents are kept for the diff.
const PREVIEW_LIMIT: u64 = 512 * 1024;

fn read_for_preview(p: &Path) -> Option<String> {
    let md = std::fs::metadata(p).ok()?;
    if !md.is_file() || md.len() > PREVIEW_LIMIT {
        return None;
    }
    std::fs::read_to_string(p).ok()
}

/// Files whose replacement can lock you out, change who may become root or break the boot. A
/// bulk "accept all" never includes them; each needs its own deliberate click. A trailing `/`
/// means everything below that directory.
pub const RISKY_PREFIXES: &[&str] = &[
    "/etc/ssh/",
    "/etc/pam.d/",
    "/etc/pam.conf",
    "/etc/sudoers",
    "/etc/sudoers.d/",
    "/etc/security/",
    "/etc/fstab",
    "/etc/portage/",
    "/etc/passwd",
    "/etc/shadow",
    "/etc/group",
    "/etc/gshadow",
    "/etc/polkit-1/",
    "/etc/default/grub",
];

/// Would accepting a new version of `target` be risky (see [`RISKY_PREFIXES`])?
pub fn is_risky(target: &Path) -> bool {
    let t = target.to_string_lossy();
    RISKY_PREFIXES.iter().any(|p| if p.ends_with('/') { t.starts_with(p) } else { t == *p })
}

/// Split into (fine for a bulk accept, needs its own click).
pub fn split_risky(updates: &[PendingUpdate]) -> (Vec<&PendingUpdate>, Vec<&PendingUpdate>) {
    updates.iter().partition(|u| !is_risky(&u.target))
}

/// Pending `._cfgXXXX_name` files below the CONFIG_PROTECT directories, with the contents the
/// current user can read (read here, off the UI thread, so the page never touches the disk per frame).
pub fn find_config_updates(protect: &str) -> Vec<PendingUpdate> {
    let mut found = Vec::new();
    let roots: Vec<&str> = if protect.trim().is_empty() { vec!["/etc"] } else { protect.split_whitespace().collect() };
    fn walk(dir: &Path, depth: usize, out: &mut Vec<(PathBuf, PathBuf)>) {
        if depth > 8 {
            return;
        }
        let Ok(rd) = std::fs::read_dir(dir) else { return };
        for e in rd.flatten() {
            let p = e.path();
            let name = e.file_name().to_string_lossy().to_string();
            let Ok(ft) = e.file_type() else { continue };
            if ft.is_dir() && !ft.is_symlink() {
                walk(&p, depth + 1, out);
            } else if name.starts_with("._cfg") && name.len() > 10 && name.as_bytes().get(9) == Some(&b'_') {
                let target = p.with_file_name(&name[10..]);
                out.push((p, target));
            }
        }
    }
    for r in roots {
        let p = Path::new(r);
        if p.is_dir() {
            walk(p, 0, &mut found);
        } else if let Some(parent) = p.parent()
            && let Ok(rd) = std::fs::read_dir(parent)
        {
            let base = p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
            for e in rd.flatten() {
                let n = e.file_name().to_string_lossy().to_string();
                if n.starts_with("._cfg") && n.ends_with(&base) {
                    found.push((e.path(), p.to_path_buf()));
                }
            }
        }
    }
    found.sort();
    found.dedup();
    found
        .into_iter()
        .map(|(update, target)| PendingUpdate { old: read_for_preview(&target), new: read_for_preview(&update), update, target })
        .collect()
}

pub fn open_url(url: &str) {
    let _ = Command::new("xdg-open").arg(url).spawn();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profiles() {
        let p = parse_profiles("Available profile symlink targets:\n  [1]   default/linux/amd64/23.0 (stable)\n  [12]  default/linux/amd64/23.0/desktop/plasma (stable) *\n  [40]  default/linux/amd64/23.0/no-multilib (dev)\n");
        assert_eq!(p.len(), 3);
        assert!(p[1].current);
        assert_eq!(p[1].path, "default/linux/amd64/23.0/desktop/plasma");
        assert_eq!(p[2].status, "dev");
    }

    fn pending(target: &str) -> PendingUpdate {
        let t = PathBuf::from(target);
        PendingUpdate { update: t.with_file_name(format!("._cfg0000_{}", t.file_name().unwrap().to_string_lossy())), target: t, old: None, new: None }
    }

    #[test]
    fn risky_files_are_kept_out_of_the_bulk_accept() {
        let all = [
            "/etc/ssh/sshd_config",
            "/etc/pam.d/system-auth",
            "/etc/pam.conf",
            "/etc/sudoers",
            "/etc/sudoers.d/wheel",
            "/etc/security/limits.conf",
            "/etc/fstab",
            "/etc/portage/make.conf",
            "/etc/passwd",
            "/etc/shadow",
            "/etc/group",
            "/etc/gshadow",
            "/etc/polkit-1/rules.d/50.rules",
            "/etc/default/grub",
            "/etc/conf.d/hostname",
            "/etc/init.d/sshd",
            "/etc/cups/cupsd.conf",
            "/etc/ssh-notes",
            "/etc/passwd-",
            "/etc/default/grubby",
            "/etc/env.d/00basic",
        ]
        .map(pending);
        let (fine, risky) = split_risky(&all);
        let names = |v: &[&PendingUpdate]| v.iter().map(|u| u.target.to_string_lossy().to_string()).collect::<Vec<_>>();
        assert_eq!(names(&risky)[..14], ["/etc/ssh/sshd_config", "/etc/pam.d/system-auth", "/etc/pam.conf", "/etc/sudoers", "/etc/sudoers.d/wheel", "/etc/security/limits.conf", "/etc/fstab", "/etc/portage/make.conf", "/etc/passwd", "/etc/shadow", "/etc/group", "/etc/gshadow", "/etc/polkit-1/rules.d/50.rules", "/etc/default/grub"]);
        assert!(names(&fine).contains(&"/etc/conf.d/hostname".to_string()));
        assert!(names(&fine).contains(&"/etc/init.d/sshd".to_string()));
        assert!(names(&fine).contains(&"/etc/env.d/00basic".to_string()));
        // Look-alikes are matched by their exact name or directory, never by a shared prefix.
        for lookalike in ["/etc/passwd-", "/etc/ssh-notes", "/etc/default/grubby", "/etc/fstab.d/x", "/etc/portage-notes"] {
            assert!(!is_risky(Path::new(lookalike)), "{lookalike}");
        }
        assert_eq!(fine.len() + risky.len(), all.len());
    }

    #[test]
    fn previews_keep_what_the_user_can_read() {
        let d = std::env::temp_dir().join(format!("portagehub-cfgupd-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("conf.d")).unwrap();
        std::fs::write(d.join("conf.d/hostname"), "hostname=\"a\"\n").unwrap();
        std::fs::write(d.join("conf.d/._cfg0000_hostname"), "hostname=\"b\"\n").unwrap();
        std::fs::write(d.join("conf.d/._cfg0000_missing"), "new\n").unwrap();
        let found = find_config_updates(&d.to_string_lossy());
        assert_eq!(found.len(), 2);
        let host = found.iter().find(|u| u.target.ends_with("hostname")).unwrap();
        assert_eq!((host.old.as_deref(), host.new.as_deref()), (Some("hostname=\"a\"\n"), Some("hostname=\"b\"\n")));
        let missing = found.iter().find(|u| u.target.ends_with("missing")).unwrap();
        assert_eq!((missing.old.as_deref(), missing.new.as_deref()), (None, Some("new\n")));
        let _ = std::fs::remove_dir_all(&d);
    }
}
