//! Index of every package/ebuild available in the configured repositories.

use super::metadata;
use super::repos::ReposConf;
use super::version::Version;
use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::Arc;

#[derive(Debug, Clone)]
pub struct Ebuild {
    pub ver: Version,
    pub repo: Arc<str>,
}

#[derive(Debug, Clone)]
pub struct Pkg {
    pub cat: String,
    pub pn: String,
    pub desc: String,
    /// Sorted ascending by version.
    pub ebuilds: Vec<Ebuild>,
}

impl Pkg {
    pub fn key(&self) -> String {
        format!("{}/{}", self.cat, self.pn)
    }

    pub fn latest(&self) -> Option<&Ebuild> {
        self.ebuilds.iter().rev().find(|e| !e.ver.is_live()).or(self.ebuilds.last())
    }

    pub fn repos(&self) -> Vec<Arc<str>> {
        let mut v: Vec<Arc<str>> = Vec::new();
        for e in &self.ebuilds {
            if !v.contains(&e.repo) {
                v.push(e.repo.clone());
            }
        }
        v
    }
}

#[derive(Debug, Default)]
pub struct Catalog {
    pub pkgs: Vec<Pkg>,
    pub by_key: HashMap<String, usize>,
    /// category -> indices into `pkgs`
    pub categories: BTreeMap<String, Vec<usize>>,
    pub repo_locations: HashMap<String, PathBuf>,
    pub repo_counts: HashMap<String, usize>,
    pub ebuild_count: usize,
    pub scan_ms: u128,
}

impl Catalog {
    pub fn get(&self, key: &str) -> Option<&Pkg> {
        self.by_key.get(key).map(|&i| &self.pkgs[i])
    }

    pub fn repo_location(&self, repo: &str) -> Option<&Path> {
        self.repo_locations.get(repo).map(|p| p.as_path())
    }

    pub fn pkg_dir(&self, repo: &str, cat: &str, pn: &str) -> Option<PathBuf> {
        self.repo_location(repo).map(|l| l.join(cat).join(pn))
    }
}

const NON_CATEGORY: &[&str] = &["eclass", "profiles", "metadata", "licenses", "scripts", "files", "distfiles", "packages"];

fn categories_of(loc: &Path) -> Vec<String> {
    let mut cats: Vec<String> = match std::fs::read_to_string(loc.join("profiles/categories")) {
        Ok(t) => t.lines().map(|l| l.trim().to_string()).filter(|l| !l.is_empty() && !l.starts_with('#')).collect(),
        Err(_) => Vec::new(),
    };
    // Overlays often have no categories file; also pick up any category-like dir.
    for e in std::fs::read_dir(loc).into_iter().flatten().flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        if name.starts_with('.') || NON_CATEGORY.contains(&name.as_str()) || cats.contains(&name) {
            continue;
        }
        if e.file_type().map(|t| t.is_dir()).unwrap_or(false) && (name.contains('-') || name == "virtual") {
            cats.push(name);
        }
    }
    cats
}

fn scan_category(loc: &Path, cat: &str, repo: &Arc<str>, out: &mut Vec<(String, String, Ebuild)>) {
    let Ok(rd) = std::fs::read_dir(loc.join(cat)) else { return };
    for e in rd.flatten() {
        let pn = e.file_name().to_string_lossy().to_string();
        if pn.starts_with('.') || !e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            continue;
        }
        let Ok(files) = std::fs::read_dir(e.path()) else { continue };
        let prefix = format!("{pn}-");
        for f in files.flatten() {
            let n = f.file_name();
            let n = n.to_string_lossy();
            if let Some(stem) = n.strip_suffix(".ebuild")
                && let Some(v) = stem.strip_prefix(&prefix)
                && let Some(ver) = Version::parse(v)
            {
                out.push((cat.to_string(), pn.clone(), Ebuild { ver, repo: repo.clone() }));
            }
        }
    }
}

fn load_desc_index(loc: &Path, into: &mut HashMap<String, String>) -> bool {
    let Ok(t) = std::fs::read_to_string(loc.join("metadata/pkg_desc_index")) else { return false };
    for line in t.lines() {
        if let Some((head, desc)) = line.split_once(": ")
            && let Some(key) = head.split_whitespace().next()
        {
            into.entry(key.to_string()).or_insert_with(|| desc.to_string());
        }
    }
    true
}

pub fn scan(repos: &ReposConf) -> Catalog {
    let start = std::time::Instant::now();
    let mut jobs: Vec<(PathBuf, String, Arc<str>)> = Vec::new();
    let mut cat = Catalog::default();
    for r in repos.repos.iter().filter(|r| r.exists) {
        let name: Arc<str> = Arc::from(r.name.as_str());
        cat.repo_locations.insert(r.name.clone(), r.location.clone());
        for c in categories_of(&r.location) {
            jobs.push((r.location.clone(), c, name.clone()));
        }
    }

    let threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4).clamp(2, 16);
    let chunk = jobs.len().div_ceil(threads).max(1);
    let mut found: Vec<(String, String, Ebuild)> = Vec::new();
    std::thread::scope(|s| {
        let handles: Vec<_> = jobs
            .chunks(chunk)
            .map(|part| {
                s.spawn(move || {
                    let mut out = Vec::new();
                    for (loc, c, repo) in part {
                        scan_category(loc, c, repo, &mut out);
                    }
                    out
                })
            })
            .collect();
        for h in handles {
            found.extend(h.join().unwrap_or_default());
        }
    });

    let mut descs: HashMap<String, String> = HashMap::new();
    let mut have_index: Vec<String> = Vec::new();
    for r in repos.repos.iter().filter(|r| r.exists) {
        if load_desc_index(&r.location, &mut descs) {
            have_index.push(r.name.clone());
        }
    }

    let mut map: HashMap<String, Pkg> = HashMap::new();
    for (c, pn, eb) in found {
        *cat.repo_counts.entry(eb.repo.to_string()).or_default() += 1;
        cat.ebuild_count += 1;
        let key = format!("{c}/{pn}");
        map.entry(key).or_insert_with(|| Pkg { cat: c, pn, desc: String::new(), ebuilds: Vec::new() }).ebuilds.push(eb);
    }
    let mut pkgs: Vec<Pkg> = map.into_values().collect();
    pkgs.sort_by(|a, b| a.cat.cmp(&b.cat).then(a.pn.cmp(&b.pn)));
    for p in &mut pkgs {
        p.ebuilds.sort_by(|a, b| a.ver.cmp(&b.ver));
        let key = p.key();
        if let Some(d) = descs.get(&key) {
            p.desc = d.clone();
        }
    }
    // Packages only present in repos without pkg_desc_index: read their cache.
    let missing: Vec<usize> = pkgs.iter().enumerate().filter(|(_, p)| p.desc.is_empty()).map(|(i, _)| i).collect();
    let locs = &cat.repo_locations;
    let filled: Vec<(usize, String)> = std::thread::scope(|s| {
        let chunk = missing.len().div_ceil(threads).max(1);
        let pk = &pkgs;
        let handles: Vec<_> = missing
            .chunks(chunk)
            .map(|part| {
                s.spawn(move || {
                    part.iter()
                        .filter_map(|&i| {
                            let p = &pk[i];
                            let e = p.latest()?;
                            let loc = locs.get(&*e.repo)?;
                            let m = metadata::load(loc, &p.cat, &p.pn, &e.ver.raw);
                            Some((i, m.get("DESCRIPTION").to_string()))
                        })
                        .collect::<Vec<_>>()
                })
            })
            .collect();
        handles.into_iter().flat_map(|h| h.join().unwrap_or_default()).collect()
    });
    for (i, d) in filled {
        pkgs[i].desc = d;
    }

    for (i, p) in pkgs.iter().enumerate() {
        cat.by_key.insert(p.key(), i);
        cat.categories.entry(p.cat.clone()).or_default().push(i);
    }
    cat.pkgs = pkgs;
    cat.scan_ms = start.elapsed().as_millis();
    cat
}

/// Category descriptions from `<repo>/<category>/metadata.xml`.
pub fn category_description(repo_loc: &Path, category: &str) -> Option<String> {
    let t = std::fs::read_to_string(repo_loc.join(category).join("metadata.xml")).ok()?;
    let re = regex::Regex::new(r#"(?s)<longdescription(?:\s+lang="en")?\s*>(.*?)</longdescription>"#).ok()?;
    let c = re.captures(&t)?;
    Some(c[1].split_whitespace().collect::<Vec<_>>().join(" "))
}
