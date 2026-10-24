//! Profile and repository masks: walks the make.profile parent chain and
//! every repository's profiles/package.mask, keeping the comment block above
//! each entry as the human-readable reason.

use super::atom::Atom;
use super::repos::ReposConf;
use super::version::Version;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct MaskEntry {
    pub atom: Atom,
    pub reason: Vec<String>,
    pub source: PathBuf,
}

#[derive(Debug, Default)]
pub struct MaskDb {
    entries: Vec<MaskEntry>,
    by_key: HashMap<String, Vec<usize>>,
    wildcard: Vec<usize>,
    /// The resolved profile chain, parents first.
    pub chain: Vec<PathBuf>,
}

impl MaskDb {
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// The mask entry hiding this version, if any.
    pub fn masked(&self, cat: &str, pn: &str, ver: &Version, slot: Option<&str>, repo: Option<&str>) -> Option<&MaskEntry> {
        let key = format!("{cat}/{pn}");
        let cands = self.by_key.get(&key).into_iter().flatten().chain(self.wildcard.iter());
        cands.map(|&i| &self.entries[i]).find(|e| e.atom.matches(cat, pn, ver, slot, repo))
    }

    fn push(&mut self, e: MaskEntry) {
        let i = self.entries.len();
        if e.atom.is_wildcard() {
            self.wildcard.push(i);
        } else {
            self.by_key.entry(e.atom.key()).or_default().push(i);
        }
        self.entries.push(e);
    }

    fn remove(&mut self, text: &str) {
        // `-atom` in a later file removes an earlier identical atom.
        let before: Vec<usize> = self
            .entries
            .iter()
            .enumerate()
            .filter(|(_, e)| e.atom.to_string() == text)
            .map(|(i, _)| i)
            .collect();
        if before.is_empty() {
            return;
        }
        let keep: Vec<MaskEntry> = self.entries.drain(..).enumerate().filter(|(i, _)| !before.contains(i)).map(|(_, e)| e).collect();
        self.by_key.clear();
        self.wildcard.clear();
        for e in keep {
            self.push(e);
        }
    }
}

/// Parse one package.mask-style text: (atom text, reason block).
pub fn parse_mask_text(text: &str) -> Vec<(String, Vec<String>)> {
    let mut out = Vec::new();
    let mut block: Vec<String> = Vec::new();
    let mut last_was_entry = false;
    for line in text.lines() {
        let t = line.trim();
        if t.is_empty() {
            block.clear();
            last_was_entry = false;
            continue;
        }
        if let Some(c) = t.strip_prefix('#') {
            if last_was_entry {
                block.clear();
                last_was_entry = false;
            }
            block.push(c.trim().to_string());
            continue;
        }
        let atom = t.split_whitespace().next().unwrap_or("").to_string();
        out.push((atom, block.clone()));
        last_was_entry = true;
    }
    out
}

fn files_of(path: &Path) -> Vec<PathBuf> {
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

fn walk_chain(dir: &Path, repos: &ReposConf, out: &mut Vec<PathBuf>, depth: usize) {
    if depth > 30 {
        return;
    }
    let dir = match dir.canonicalize() {
        Ok(d) => d,
        Err(_) => return,
    };
    if let Ok(parents) = std::fs::read_to_string(dir.join("parent")) {
        for p in parents.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')) {
            // portage-2 format: "repo:path/below/profiles"
            let target = match p.split_once(':') {
                Some((repo, rel)) if !p.starts_with('/') => match repos.get(repo) {
                    Some(r) => r.location.join("profiles").join(rel),
                    None => continue,
                },
                _ => dir.join(p),
            };
            walk_chain(&target, repos, out, depth + 1);
        }
    }
    if !out.contains(&dir) {
        out.push(dir);
    }
}

pub fn load(repos: &ReposConf) -> MaskDb {
    let mut db = MaskDb::default();
    let mut chain = Vec::new();
    walk_chain(Path::new("/etc/portage/make.profile"), repos, &mut chain, 0);
    db.chain = chain.clone();
    // Order: repository-wide masks, then the profile chain, then /etc/portage/profile.
    let mut sources: Vec<PathBuf> = Vec::new();
    for r in repos.repos.iter().filter(|r| r.exists) {
        sources.push(r.location.join("profiles/package.mask"));
    }
    for p in &chain {
        sources.push(p.join("package.mask"));
    }
    sources.push(PathBuf::from("/etc/portage/profile/package.mask"));
    for src in sources {
        for file in files_of(&src) {
            let Ok(text) = std::fs::read_to_string(&file) else { continue };
            for (atom, reason) in parse_mask_text(&text) {
                if let Some(rest) = atom.strip_prefix('-') {
                    db.remove(rest);
                } else if let Some(a) = Atom::parse(&atom) {
                    db.push(MaskEntry { atom: a, reason, source: file.clone() });
                }
            }
        }
    }
    db
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reasons() {
        let t = "# header\n\n# Alice (2026-01-01)\n# Broken build.\n=a/b-1\n>=a/c-2\n\n# Bob\n-a/d\n";
        let p = parse_mask_text(t);
        assert_eq!(p.len(), 3);
        assert_eq!(p[0].0, "=a/b-1");
        assert_eq!(p[0].1, vec!["Alice (2026-01-01)", "Broken build."]);
        assert_eq!(p[1].1, p[0].1);
        assert_eq!(p[2].0, "-a/d");
    }

    #[test]
    fn incremental_remove() {
        let mut db = MaskDb::default();
        db.push(MaskEntry { atom: Atom::parse("a/b").unwrap(), reason: vec![], source: "/x".into() });
        db.push(MaskEntry { atom: Atom::parse("=a/c-1").unwrap(), reason: vec![], source: "/x".into() });
        let v = Version::parse("1").unwrap();
        assert!(db.masked("a", "b", &v, None, None).is_some());
        db.remove("a/b");
        assert!(db.masked("a", "b", &v, None, None).is_none());
        assert!(db.masked("a", "c", &v, None, None).is_some());
    }
}
