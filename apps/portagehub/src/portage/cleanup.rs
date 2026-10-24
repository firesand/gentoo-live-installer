//! Things left behind that are no longer needed: old build logs, failed build
//! directories, world-file entries for uninstalled packages, /etc/portage
//! lines that no longer match anything, and preserved libraries.

use super::catalog::Catalog;
use super::config::{ConfKind, PortageConfig};
use super::metadata;
use super::vdb::Vdb;
use crate::data::{KwState, kw_state};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

#[derive(Debug, Clone, Default)]
pub struct LogStats {
    pub dir: PathBuf,
    pub files: usize,
    pub bytes: u64,
    pub old_files: usize,
    pub old_bytes: u64,
    pub days: u32,
    pub readable: bool,
}

#[derive(Debug, Clone)]
pub struct ConfigHit {
    pub kind: ConfKind,
    pub path: PathBuf,
    pub line: usize,
    pub text: String,
    pub reason: String,
}

#[derive(Debug, Clone, Default)]
pub struct Leftovers {
    pub logs: LogStats,
    pub build_dirs: Vec<(PathBuf, u64)>,
    /// World entries that match no installed package.
    pub world_cruft: Vec<String>,
    /// Entries whose package exists in no repository and is not installed.
    pub dead_entries: Vec<ConfigHit>,
    /// Keyword entries whose matching versions are all stable now.
    pub redundant_keywords: Vec<ConfigHit>,
    pub preserved_libs: Vec<String>,
}

fn dir_size(p: &Path, depth: usize) -> u64 {
    if depth > 64 {
        return 0;
    }
    let mut total = 0;
    for e in std::fs::read_dir(p).into_iter().flatten().flatten() {
        let Ok(ft) = e.file_type() else { continue };
        if ft.is_dir() {
            total += dir_size(&e.path(), depth + 1);
        } else if let Ok(m) = e.metadata() {
            total += m.len();
        }
    }
    total
}

pub fn log_stats(dir: &Path, days: u32) -> LogStats {
    let mut s = LogStats { dir: dir.to_path_buf(), days, ..Default::default() };
    let cutoff = SystemTime::now() - Duration::from_secs(days as u64 * 86_400);
    let Ok(rd) = std::fs::read_dir(dir) else { return s };
    s.readable = true;
    for e in rd.flatten() {
        let Ok(m) = e.metadata() else { continue };
        if !m.is_file() {
            continue;
        }
        s.files += 1;
        s.bytes += m.len();
        if m.modified().is_ok_and(|t| t < cutoff) {
            s.old_files += 1;
            s.old_bytes += m.len();
        }
    }
    s
}

/// Scan everything; `log_dir` is PORT_LOGDIR (may be empty).
pub fn scan(vdb: &Vdb, catalog: &Catalog, cfg: &PortageConfig, arch: &str, log_dir: &str, log_days: u32) -> Leftovers {
    let mut l = Leftovers::default();
    if !log_dir.is_empty() {
        l.logs = log_stats(Path::new(log_dir), log_days);
    }
    for d in portagehub::rootops::build_dirs(Path::new("/")) {
        let size = dir_size(&d, 0);
        l.build_dirs.push((d, size));
    }
    for raw in vdb.world_raw.iter().filter(|r| !r.starts_with('@')) {
        let Some(atom) = super::atom::Atom::parse(raw) else { continue };
        let installed = vdb.installed(&atom.key()).iter().any(|p| atom.matches(&p.cat, &p.pn, &p.ver, Some(&p.slot), None));
        if !installed {
            l.world_cruft.push(raw.clone());
        }
    }
    for set in &cfg.sets {
        if matches!(set.kind, ConfKind::Sets) {
            continue;
        }
        for f in set.files.iter().filter(|f| !f.ignored) {
            for (i, e) in f.entries() {
                let Some(a) = &e.atom else { continue };
                if e.disabled || a.is_wildcard() {
                    continue;
                }
                let key = a.key();
                let in_tree = catalog.get(&key);
                if in_tree.is_none() && !vdb.is_installed(&key) {
                    l.dead_entries.push(ConfigHit {
                        kind: set.kind,
                        path: f.path.clone(),
                        line: i,
                        text: e.render(),
                        reason: format!("{key} is in no repository and not installed"),
                    });
                    continue;
                }
                if matches!(set.kind, ConfKind::AcceptKeywords | ConfKind::Keywords)
                    && let Some(pkg) = in_tree
                {
                    let testing_only = e.values.is_empty() || e.values.iter().all(|v| v == &format!("~{arch}"));
                    if !testing_only {
                        continue;
                    }
                    let matching: Vec<_> = pkg.ebuilds.iter().filter(|eb| a.matches(&pkg.cat, &pkg.pn, &eb.ver, None, Some(&eb.repo))).collect();
                    if matching.is_empty() {
                        continue;
                    }
                    let all_stable = matching.iter().all(|eb| {
                        catalog
                            .repo_location(&eb.repo)
                            .map(|loc| kw_state(&metadata::load(loc, &pkg.cat, &pkg.pn, &eb.ver.raw).keywords(), arch) == KwState::Stable)
                            .unwrap_or(false)
                    });
                    if all_stable {
                        l.redundant_keywords.push(ConfigHit {
                            kind: set.kind,
                            path: f.path.clone(),
                            line: i,
                            text: e.render(),
                            reason: format!("all {} matching version(s) are stable for {arch} now", matching.len()),
                        });
                    }
                }
            }
        }
    }
    if let Ok(o) = std::process::Command::new("portageq").args(["list_preserved_libs", "/"]).output() {
        l.preserved_libs = String::from_utf8_lossy(&o.stdout).lines().map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect();
    }
    l
}

/// Parse the total line of `eclean-dist/eclean-pkg --pretend`:
/// " [    8.6 G ] Total space from 195 files would be freed in the distfiles directory"
pub fn eclean_total(output: &str) -> Option<(String, usize)> {
    for l in output.lines() {
        let t = l.trim();
        if let Some(rest) = t.strip_prefix('[')
            && let Some((size, tail)) = rest.split_once(']')
            && tail.contains("Total space from")
        {
            let n = tail.split_whitespace().skip_while(|w| *w != "from").nth(1).and_then(|x| x.parse().ok()).unwrap_or(0);
            return Some((size.trim().to_string(), n));
        }
    }
    None
}

/// Individual files from eclean --pretend: (size text, name).
pub fn eclean_files(output: &str) -> Vec<(String, String)> {
    output
        .lines()
        .filter_map(|l| {
            let t = l.trim();
            let rest = t.strip_prefix('[')?;
            let (size, name) = rest.split_once(']')?;
            let name = name.trim();
            (!name.starts_with("Total space")).then(|| (size.trim().to_string(), name.to_string()))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn eclean() {
        let out = ">>> Here are the distfiles that would be deleted:\n [  172.7 M ] antigravity-2.23.1-amd64.tar.gz\n [   47.5 K ] antigravity-2.23.1.png\n [    8.6 G ] Total space from 195 files would be freed in the distfiles directory\n";
        assert_eq!(eclean_total(out), Some(("8.6 G".to_string(), 195)));
        let f = eclean_files(out);
        assert_eq!(f.len(), 2);
        assert_eq!(f[0], ("172.7 M".to_string(), "antigravity-2.23.1-amd64.tar.gz".to_string()));
    }
}
