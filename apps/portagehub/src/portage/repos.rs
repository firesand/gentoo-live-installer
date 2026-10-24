//! `repos.conf` handling and the `eselect repository` overlay list.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

pub const DEFAULT_REPOS_CONF: &str = "/usr/share/portage/config/repos.conf";
pub const USER_REPOS_CONF: &str = "/etc/portage/repos.conf";

#[derive(Debug, Clone, Default)]
pub struct Repo {
    pub name: String,
    pub location: PathBuf,
    /// Every key from the section, in file order.
    pub keys: Vec<(String, String)>,
    /// The file the (last) section for this repo was read from.
    pub file: Option<PathBuf>,
    pub is_main: bool,
    pub exists: bool,
    pub last_sync: Option<SystemTime>,
    pub masters_layout: Vec<String>,
}

impl Repo {
    pub fn get(&self, key: &str) -> Option<&str> {
        self.keys.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
    }

    pub fn sync_type(&self) -> &str {
        self.get("sync-type").unwrap_or("none")
    }

    pub fn sync_uri(&self) -> &str {
        self.get("sync-uri").unwrap_or("")
    }

    pub fn auto_sync(&self) -> bool {
        self.get("auto-sync").is_none_or(|v| matches!(v.to_ascii_lowercase().as_str(), "yes" | "true")) && self.get("sync-type").is_some()
    }

    pub fn priority(&self) -> i32 {
        self.get("priority").and_then(|p| p.parse().ok()).unwrap_or(if self.is_main { -1000 } else { 0 })
    }

    pub fn masters(&self) -> String {
        match self.get("masters") {
            Some(m) => m.to_string(),
            None => self.masters_layout.join(" "),
        }
    }

    /// Repositories configured through a section in a user file can be
    /// edited; the default `gentoo` section lives in /usr/share.
    pub fn user_file(&self) -> Option<&Path> {
        self.file.as_deref().filter(|f| f.starts_with("/etc/portage"))
    }
}

#[derive(Debug, Clone, Default)]
pub struct ReposConf {
    pub main_repo: String,
    pub repos: Vec<Repo>,
    /// Parse problems to surface to the user.
    pub warnings: Vec<String>,
}

impl ReposConf {
    pub fn get(&self, name: &str) -> Option<&Repo> {
        self.repos.iter().find(|r| r.name == name)
    }
}

/// Very small INI reader matching Python's configparser for repos.conf.
pub fn parse_ini(text: &str) -> Vec<(String, Vec<(String, String)>)> {
    let mut out: Vec<(String, Vec<(String, String)>)> = Vec::new();
    for raw in text.lines() {
        let line = raw.trim_end();
        let t = line.trim();
        if t.is_empty() || t.starts_with('#') || t.starts_with(';') {
            continue;
        }
        if t.starts_with('[') && t.ends_with(']') {
            out.push((t[1..t.len() - 1].trim().to_string(), Vec::new()));
            continue;
        }
        // Continuation line (indented) appends to the previous value.
        if raw.starts_with(char::is_whitespace)
            && let Some((_, kv)) = out.last_mut()
            && let Some((_, v)) = kv.last_mut()
        {
            v.push(' ');
            v.push_str(t);
            continue;
        }
        if let Some((k, v)) = t.split_once('=').or_else(|| t.split_once(':'))
            && let Some((_, kv)) = out.last_mut()
        {
            kv.push((k.trim().to_string(), v.trim().to_string()));
        }
    }
    out
}

pub fn conf_files(path: &Path) -> Vec<PathBuf> {
    if path.is_dir() {
        let mut v: Vec<PathBuf> = std::fs::read_dir(path)
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                let n = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
                p.is_file() && !n.starts_with('.') && !n.ends_with('~')
            })
            .collect();
        v.sort();
        v
    } else if path.is_file() {
        vec![path.to_path_buf()]
    } else {
        Vec::new()
    }
}

fn last_sync(loc: &Path) -> Option<SystemTime> {
    for f in [".git/FETCH_HEAD", "metadata/timestamp.chk", "metadata/timestamp.x", ".git/HEAD"] {
        if let Ok(m) = std::fs::metadata(loc.join(f))
            && let Ok(t) = m.modified()
        {
            return Some(t);
        }
    }
    None
}

pub fn load() -> ReposConf {
    let mut sections: BTreeMap<String, Repo> = BTreeMap::new();
    let mut order: Vec<String> = Vec::new();
    let mut main_repo = "gentoo".to_string();
    let mut warnings = Vec::new();

    let mut files = vec![PathBuf::from(DEFAULT_REPOS_CONF)];
    files.extend(conf_files(Path::new(USER_REPOS_CONF)));
    for file in files {
        let Ok(text) = std::fs::read_to_string(&file) else {
            warnings.push(format!("Cannot read {}", file.display()));
            continue;
        };
        for (name, kv) in parse_ini(&text) {
            if name == "DEFAULT" {
                if let Some((_, v)) = kv.iter().find(|(k, _)| k == "main-repo") {
                    main_repo = v.clone();
                }
                continue;
            }
            let repo = sections.entry(name.clone()).or_insert_with(|| {
                order.push(name.clone());
                Repo { name: name.clone(), ..Default::default() }
            });
            repo.keys.extend(kv);
            repo.file = Some(file.clone());
        }
    }

    let mut repos: Vec<Repo> = Vec::new();
    for name in order {
        let mut r = sections.remove(&name).unwrap();
        match r.get("location") {
            Some(l) => r.location = PathBuf::from(l),
            None => {
                warnings.push(format!("Repository '{name}' has no location"));
                continue;
            }
        }
        r.is_main = r.name == main_repo;
        r.exists = r.location.is_dir();
        r.last_sync = last_sync(&r.location);
        if let Ok(layout) = std::fs::read_to_string(r.location.join("metadata/layout.conf")) {
            for line in layout.lines() {
                if let Some(v) = line.trim().strip_prefix("masters")
                    && let Some(v) = v.trim().strip_prefix('=')
                {
                    r.masters_layout = v.split_whitespace().map(String::from).collect();
                }
            }
        }
        if let Ok(rn) = std::fs::read_to_string(r.location.join("profiles/repo_name"))
            && rn.trim() != r.name
            && !rn.trim().is_empty()
        {
            warnings.push(format!("Repository section [{}] points to a tree whose profiles/repo_name is '{}'", r.name, rn.trim()));
        }
        repos.push(r);
    }
    repos.sort_by(|a, b| b.is_main.cmp(&a.is_main).then(a.name.to_lowercase().cmp(&b.name.to_lowercase())));
    ReposConf { main_repo, repos, warnings }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RemoteState {
    Available,
    /// `*` enabled through eselect-repository
    Enabled,
    /// `#` enabled, but configured differently from the official entry
    Custom,
    /// `@` only configured locally
    Local,
}

#[derive(Debug, Clone)]
pub struct RemoteRepo {
    pub name: String,
    pub homepage: String,
    pub state: RemoteState,
}

/// Parse `eselect repository list` output.
pub fn parse_eselect_list(text: &str) -> Vec<RemoteRepo> {
    let mut out = Vec::new();
    for line in text.lines() {
        let t = line.trim();
        let Some(rest) = t.strip_prefix('[') else { continue };
        let Some((_, rest)) = rest.split_once(']') else { continue };
        let rest = rest.trim();
        let (name, rest) = rest.split_once(char::is_whitespace).unwrap_or((rest, ""));
        let rest = rest.trim();
        let (state, rest) = match rest.chars().next() {
            Some('*') => (RemoteState::Enabled, rest[1..].trim()),
            Some('#') => (RemoteState::Custom, rest[1..].trim()),
            Some('@') => (RemoteState::Local, rest[1..].trim()),
            _ => (RemoteState::Available, rest),
        };
        let homepage = rest.trim_start_matches('(').trim_end_matches(')').to_string();
        out.push(RemoteRepo { name: name.to_string(), homepage, state });
    }
    out
}

/// Render a repos.conf section.
pub fn render_section(name: &str, keys: &[(String, String)]) -> String {
    let mut s = format!("[{name}]\n");
    for (k, v) in keys {
        if !v.trim().is_empty() {
            s.push_str(&format!("{k} = {v}\n"));
        }
    }
    s
}

/// Replace (or remove, when `replacement` is None) the `[name]` section in an INI text.
pub fn replace_section(text: &str, name: &str, replacement: Option<&str>) -> String {
    let mut out = String::new();
    let mut skipping = false;
    let mut replaced = false;
    for line in text.split_inclusive('\n') {
        let t = line.trim();
        if t.starts_with('[') && t.ends_with(']') {
            let sec = t[1..t.len() - 1].trim();
            if sec == name {
                skipping = true;
                if let Some(r) = replacement {
                    out.push_str(r);
                    replaced = true;
                }
                continue;
            }
            skipping = false;
        }
        if !skipping {
            out.push_str(line);
        }
    }
    if !replaced && let Some(r) = replacement {
        if !out.is_empty() && !out.ends_with("\n\n") {
            if !out.ends_with('\n') {
                out.push('\n');
            }
            out.push('\n');
        }
        out.push_str(r);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ini() {
        let s = parse_ini("[DEFAULT]\nmain-repo = gentoo\n\n# c\n[guru]\nlocation = /var/db/repos/guru\nsync-type = git\n");
        assert_eq!(s.len(), 2);
        assert_eq!(s[1].0, "guru");
        assert_eq!(s[1].1[1], ("sync-type".into(), "git".into()));
    }

    #[test]
    fn eselect() {
        let r = parse_eselect_list(
            "Available repositories:\n  [1]   2xsaiko (https://git.sr.ht/x)\n  [54]  asus-local @\n  [160] gentoo * (https://gentoo.org/)\n  [165] gentoo-zh # (https://github.com/gentoo-zh/overlay)\n",
        );
        assert_eq!(r.len(), 4);
        assert_eq!(r[0].state, RemoteState::Available);
        assert_eq!(r[0].homepage, "https://git.sr.ht/x");
        assert_eq!(r[1].state, RemoteState::Local);
        assert_eq!(r[2].state, RemoteState::Enabled);
        assert_eq!(r[3].state, RemoteState::Custom);
        assert_eq!(r[3].homepage, "https://github.com/gentoo-zh/overlay");
    }

    #[test]
    fn sections() {
        let t = "[a]\nx = 1\n\n[b]\ny = 2\n";
        assert_eq!(replace_section(t, "a", None), "[b]\ny = 2\n");
        assert_eq!(replace_section(t, "b", Some("[b]\ny = 3\n")), "[a]\nx = 1\n\n[b]\ny = 3\n");
        assert_eq!(replace_section("[a]\nx = 1\n", "c", Some("[c]\nz = 1\n")), "[a]\nx = 1\n\n[c]\nz = 1\n");
    }
}
