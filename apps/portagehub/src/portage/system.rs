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

/// Pending `._cfgXXXX_name` files below the CONFIG_PROTECT directories.
pub fn find_config_updates(protect: &str) -> Vec<(PathBuf, PathBuf)> {
    let mut out = Vec::new();
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
            walk(p, 0, &mut out);
        } else if let Some(parent) = p.parent()
            && let Ok(rd) = std::fs::read_dir(parent)
        {
            let base = p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
            for e in rd.flatten() {
                let n = e.file_name().to_string_lossy().to_string();
                if n.starts_with("._cfg") && n.ends_with(&base) {
                    out.push((e.path(), p.to_path_buf()));
                }
            }
        }
    }
    out.sort();
    out.dedup();
    out
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
}
