//! The installed-package database in /var/db/pkg and the world file.

use super::atom::Atom;
use super::version::{Version, split_pv};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

pub const VDB: &str = "/var/db/pkg";
pub const WORLD: &str = "/var/lib/portage/world";
pub const WORLD_SETS: &str = "/var/lib/portage/world_sets";

#[derive(Debug, Clone)]
pub struct Installed {
    pub cat: String,
    pub pn: String,
    pub ver: Version,
    pub slot: String,
    pub repo: String,
    pub build_time: i64,
    pub size: u64,
    pub desc: String,
    pub dir: PathBuf,
}

impl Installed {
    pub fn key(&self) -> String {
        format!("{}/{}", self.cat, self.pn)
    }

    pub fn cpv(&self) -> String {
        format!("{}/{}-{}", self.cat, self.pn, self.ver)
    }

    pub fn read(&self, file: &str) -> String {
        read_trim(&self.dir.join(file))
    }

    pub fn main_slot(&self) -> &str {
        self.slot.split('/').next().unwrap_or(&self.slot)
    }
}

fn read_trim(p: &Path) -> String {
    std::fs::read_to_string(p).map(|s| s.trim().to_string()).unwrap_or_default()
}

#[derive(Debug, Default)]
pub struct Vdb {
    pub pkgs: Vec<Installed>,
    pub by_key: HashMap<String, Vec<usize>>,
    pub world: Vec<Atom>,
    pub world_raw: Vec<String>,
    /// `cat/pn` of every world atom, so "is it in @world?" is a lookup, not a scan.
    world_keys: HashSet<String>,
    pub world_sets: Vec<String>,
    pub total_size: u64,
}

impl Vdb {
    pub fn installed(&self, key: &str) -> Vec<&Installed> {
        self.by_key.get(key).map(|v| v.iter().map(|&i| &self.pkgs[i]).collect()).unwrap_or_default()
    }

    pub fn is_installed(&self, key: &str) -> bool {
        self.by_key.contains_key(key)
    }

    /// Is the package listed in @world (directly selected by the user)?
    pub fn in_world(&self, key: &str) -> bool {
        self.world_keys.contains(key)
    }

    /// Set the world file's lines (comments and blanks already removed).
    pub fn set_world(&mut self, lines: Vec<String>) {
        self.world = lines.iter().filter_map(|l| Atom::parse(l)).collect();
        self.world_keys = self.world.iter().map(Atom::key).collect();
        self.world_raw = lines;
    }
}

pub fn load() -> Vdb {
    let mut v = Vdb::default();
    let mut dirs: Vec<(String, String, PathBuf)> = Vec::new();
    for c in std::fs::read_dir(VDB).into_iter().flatten().flatten() {
        let cat = c.file_name().to_string_lossy().to_string();
        if cat.starts_with('.') || !c.path().is_dir() {
            continue;
        }
        for p in std::fs::read_dir(c.path()).into_iter().flatten().flatten() {
            let pf = p.file_name().to_string_lossy().to_string();
            // In-progress merges live in "-MERGING-" dirs.
            if pf.starts_with('-') || pf.starts_with('.') {
                continue;
            }
            dirs.push((cat.clone(), pf, p.path()));
        }
    }
    let threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4).clamp(2, 16);
    let chunk = dirs.len().div_ceil(threads).max(1);
    let mut pkgs: Vec<Installed> = std::thread::scope(|s| {
        let hs: Vec<_> = dirs
            .chunks(chunk)
            .map(|part| {
                s.spawn(move || {
                    part.iter()
                        .filter_map(|(cat, pf, dir)| {
                            let (pn, ver) = split_pv(pf)?;
                            Some(Installed {
                                cat: cat.clone(),
                                pn: pn.to_string(),
                                ver,
                                slot: read_trim(&dir.join("SLOT")),
                                repo: read_trim(&dir.join("repository")),
                                build_time: read_trim(&dir.join("BUILD_TIME")).parse().unwrap_or(0),
                                size: read_trim(&dir.join("SIZE")).parse().unwrap_or(0),
                                desc: read_trim(&dir.join("DESCRIPTION")),
                                dir: dir.clone(),
                            })
                        })
                        .collect::<Vec<_>>()
                })
            })
            .collect();
        hs.into_iter().flat_map(|h| h.join().unwrap_or_default()).collect()
    });
    pkgs.sort_by(|a, b| a.cat.cmp(&b.cat).then(a.pn.cmp(&b.pn)).then(a.ver.cmp(&b.ver)));
    for (i, p) in pkgs.iter().enumerate() {
        v.by_key.entry(p.key()).or_default().push(i);
        v.total_size += p.size;
    }
    v.pkgs = pkgs;
    let world = std::fs::read_to_string(WORLD).unwrap_or_default();
    v.set_world(world.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')).map(String::from).collect());
    v.world_sets = std::fs::read_to_string(WORLD_SETS)
        .unwrap_or_default()
        .lines()
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty())
        .collect();
    v
}

/// Parsed CONTENTS entry.
#[derive(Debug, Clone)]
pub struct ContentEntry {
    pub kind: char,
    pub path: String,
}

pub fn contents(dir: &Path) -> Vec<ContentEntry> {
    let t = std::fs::read_to_string(dir.join("CONTENTS")).unwrap_or_default();
    t.lines()
        .filter_map(|l| {
            let (kind, rest) = l.split_once(' ')?;
            let path = match kind {
                "obj" => {
                    // obj <path> <md5> <mtime>
                    let mut parts: Vec<&str> = rest.rsplitn(3, ' ').collect();
                    parts.pop()?.to_string()
                }
                "sym" => rest.split(" -> ").next()?.to_string(),
                _ => rest.to_string(),
            };
            let k = match kind {
                "obj" => 'f',
                "dir" => 'd',
                "sym" => 'l',
                _ => '?',
            };
            Some(ContentEntry { kind: k, path })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn in_world_agrees_with_the_scan_it_replaces() {
        let mut v = Vdb::default();
        v.set_world(
            ["app-editors/vim", "=sys-kernel/gentoo-kernel-6.12.8", "dev-lang/python:3.12", "dev-libs/*", "@kernels-keep", "~net-misc/curl-8.0", "not an atom"]
                .iter()
                .map(|s| s.to_string())
                .collect(),
        );
        let scan = |key: &str| v.world.iter().any(|a| a.key() == key);
        for key in [
            "app-editors/vim",
            "sys-kernel/gentoo-kernel",
            "dev-lang/python",
            "dev-libs/*",
            "dev-libs/openssl",
            "net-misc/curl",
            "app-editors/emacs",
            "kernels-keep",
            "@kernels-keep",
            "not an atom",
            "",
        ] {
            assert_eq!(v.in_world(key), scan(key), "{key}");
        }
        assert!(v.in_world("app-editors/vim") && v.in_world("dev-lang/python") && v.in_world("sys-kernel/gentoo-kernel"));
        assert!(!v.in_world("app-editors/emacs"));
        assert_eq!(v.world_raw.len(), 7, "every line is kept, parsable or not");
        // Replacing the world replaces the lookup too.
        v.set_world(vec!["app-misc/x".into()]);
        assert!(v.in_world("app-misc/x") && !v.in_world("app-editors/vim"));
    }
}
