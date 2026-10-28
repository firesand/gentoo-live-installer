//! Patch catalog: well-known patchsets resolved against the selected kernel
//! version, plus the user's own patches.
//!
//! Remote patches come from the CachyOS kernel-patches repository and the
//! upstream BORE repository. Directory listings are fetched through the GitHub
//! API (cached for 12 hours to stay far below the anonymous rate limit) and
//! files are downloaded with curl or wget, which every Gentoo system has.

use crate::hw::SystemInfo;
use crate::kconfig::SymbolIndex;
use crate::paths;
use crate::profiles::ProfileId;
use crate::sources::{Flavor, KernelSource};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum PatchCategory {
    Scheduler,
    Performance,
    Hardware,
    Filesystem,
    Security,
    Local,
}

impl PatchCategory {
    pub fn label(self) -> &'static str {
        match self {
            PatchCategory::Scheduler => "Scheduler",
            PatchCategory::Performance => "Performance",
            PatchCategory::Hardware => "Hardware",
            PatchCategory::Filesystem => "Filesystem",
            PatchCategory::Security => "Security",
            PatchCategory::Local => "My patches",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub enum NameMatch {
    Exact(&'static str),
    Prefix(&'static str),
}

#[derive(Debug, Clone, Copy)]
pub enum Locator {
    /// `CachyOS/kernel-patches/<major.minor>/<dir>/<file>`
    Cachy { dir: &'static str, name: NameMatch },
    /// `firelzrd/bore-scheduler/patches/stable/0001-linux<ver>-bore-*.patch`
    BoreUpstream,
}

pub struct PatchInfo {
    pub id: &'static str,
    pub title: &'static str,
    pub icon: &'static str,
    pub category: PatchCategory,
    pub summary: &'static str,
    pub explain: &'static str,
    /// Kconfig symbols the patch adds (lets the tweak catalog offer them).
    pub provides: &'static [&'static str],
    /// If the source tree already defines this symbol, the patch is included.
    pub included_if: Option<&'static str>,
    pub conflicts: &'static [&'static str],
    pub locators: &'static [Locator],
    pub recommend: fn(&SystemInfo, ProfileId) -> bool,
    pub homepage: &'static str,
    /// Shown when no file is published for the selected version.
    pub fallback_hint: &'static str,
}

pub fn catalog() -> Vec<PatchInfo> {
    vec![
        PatchInfo {
            id: "bore",
            title: "BORE scheduler",
            icon: "⚡",
            category: PatchCategory::Scheduler,
            summary: "Burst-Oriented Response Enhancer — keeps games and the desktop smooth under load.",
            explain: "BORE extends the upstream EEVDF scheduler: tasks that run in short bursts \
                      (interactive apps, game threads) get priority over tasks that hog the CPU \
                      (compilers, encoders). The result is less stutter when your PC is busy. It is \
                      the default scheduler in CachyOS and is enabled with the 'CPU scheduler' option.",
            provides: &["SCHED_BORE"],
            included_if: Some("SCHED_BORE"),
            conflicts: &["prjc"],
            locators: &[
                Locator::Cachy { dir: "sched", name: NameMatch::Exact("0001-bore.patch") },
                Locator::BoreUpstream,
                Locator::Cachy { dir: "sched", name: NameMatch::Exact("0001-bore-cachy.patch") },
            ],
            recommend: |_, p| matches!(p, ProfileId::Gaming | ProfileId::Desktop | ProfileId::Laptop),
            homepage: "https://github.com/firelzrd/bore-scheduler",
            fallback_hint: "",
        },
        PatchInfo {
            id: "prjc",
            title: "Project C (BMQ / PDS)",
            icon: "🔬",
            category: PatchCategory::Scheduler,
            summary: "Alternative schedulers by Alfred Chen. For experimenters.",
            explain: "Replaces the whole CPU scheduler with BMQ or PDS, simple designs aimed at \
                      desktop latency. Some users report great results, others regressions; it \
                      cannot be combined with BORE or sched_ext.",
            provides: &["SCHED_ALT", "SCHED_BMQ", "SCHED_PDS"],
            included_if: Some("SCHED_ALT"),
            conflicts: &["bore"],
            locators: &[
                Locator::Cachy { dir: "sched", name: NameMatch::Exact("0001-prjc.patch") },
                Locator::Cachy { dir: "sched", name: NameMatch::Exact("0001-prjc-cachy.patch") },
            ],
            recommend: |_, _| false,
            homepage: "https://gitlab.com/alfredchen/projectc",
            fallback_hint: "",
        },
        PatchInfo {
            id: "cachyos_base",
            title: "CachyOS base patchset",
            icon: "🚀",
            category: PatchCategory::Performance,
            summary: "BBRv3, ADIOS I/O scheduler, -O3, 500/600/750 Hz, AMD fixes and more.",
            explain: "The bundle of performance patches CachyOS applies to every kernel. For Linux \
                      7.x CachyOS publishes it as a complete source tree rather than a patch; on \
                      Gentoo the easiest way to get it is sys-kernel/cachyos-sources from the \
                      CachyOS-kernels overlay, which Kernelsmith then uses as the source.",
            provides: &["CACHY", "TCP_CONG_BBR3", "MQ_IOSCHED_ADIOS", "CC_OPTIMIZE_FOR_PERFORMANCE_O3", "HZ_600", "HZ_750", "HZ_500"],
            included_if: Some("CACHY"),
            conflicts: &[],
            locators: &[Locator::Cachy { dir: "all", name: NameMatch::Exact("0001-cachyos-base-all.patch") }],
            recommend: |_, p| matches!(p, ProfileId::Gaming | ProfileId::Desktop),
            homepage: "https://github.com/CachyOS/linux-cachyos",
            fallback_hint: "Not published as a single patch for this version — emerge sys-kernel/cachyos-sources (CachyOS-kernels overlay) and pick it as your source.",
        },
        PatchInfo {
            id: "handheld",
            title: "Handheld gaming PCs",
            icon: "🎮",
            category: PatchCategory::Hardware,
            summary: "Steam Deck, ROG Ally, Legion Go and other handheld support.",
            explain: "Drivers and fixes for handheld gaming PCs: controllers, fan curves, TDP and \
                      battery controls, display orientation. Only useful on a handheld.",
            provides: &[],
            included_if: None,
            conflicts: &[],
            locators: &[Locator::Cachy { dir: "misc", name: NameMatch::Exact("0001-handheld.patch") }],
            recommend: |s, _| s.handheld.is_some(),
            homepage: "https://github.com/CachyOS/kernel-patches",
            fallback_hint: "",
        },
        PatchInfo {
            id: "acpi_call",
            title: "acpi_call",
            icon: "🔋",
            category: PatchCategory::Hardware,
            summary: "Lets tools like TLP set battery charge thresholds on some laptops.",
            explain: "Adds the acpi_call module, which power tools use to talk to laptop firmware — \
                      for example to stop charging at 80% on ThinkPads to extend battery life.",
            provides: &["ACPI_CALL"],
            included_if: Some("ACPI_CALL"),
            conflicts: &[],
            locators: &[Locator::Cachy { dir: "misc", name: NameMatch::Exact("0001-acpi-call.patch") }],
            recommend: |s, _| s.is_laptop(),
            homepage: "https://github.com/nix-community/acpi_call",
            fallback_hint: "",
        },
        PatchInfo {
            id: "aufs",
            title: "AUFS",
            icon: "📁",
            category: PatchCategory::Filesystem,
            summary: "Another Union FileSystem, used by some live systems and older Docker setups.",
            explain: "A union filesystem that stacks directories. Most modern tools use the \
                      built-in overlayfs instead; only add this if something you run needs AUFS.",
            provides: &["AUFS_FS"],
            included_if: Some("AUFS_FS"),
            conflicts: &[],
            locators: &[Locator::Cachy { dir: "misc", name: NameMatch::Prefix("0001-aufs-") }],
            recommend: |_, _| false,
            homepage: "https://aufs.sourceforge.net/",
            fallback_hint: "",
        },
        PatchInfo {
            id: "hardened",
            title: "linux-hardened",
            icon: "🛡",
            category: PatchCategory::Security,
            summary: "Extra exploit mitigations from the linux-hardened project. Costs performance.",
            explain: "Adds security hardening beyond upstream (stricter memory allocator checks, \
                      more randomization, restricted interfaces). Good for exposed servers; it makes \
                      gaming and desktop workloads slower and can break some software.",
            provides: &[],
            included_if: None,
            conflicts: &[],
            locators: &[Locator::Cachy { dir: "misc", name: NameMatch::Exact("0001-hardened.patch") }],
            recommend: |_, _| false,
            homepage: "https://github.com/anthraxx/linux-hardened",
            fallback_hint: "",
        },
    ]
}

pub fn find(id: &str) -> Option<PatchInfo> {
    catalog().into_iter().find(|p| p.id == id)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum PatchStatus {
    /// The source tree already contains it.
    Included,
    Available { url: String, file_name: String },
    NotPublished(String),
    Error(String),
}

/// A patch the user picked for a build, already downloaded.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlannedPatch {
    pub id: String,
    pub title: String,
    pub file: PathBuf,
    pub url: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct GhEntry {
    name: String,
    #[serde(default)]
    download_url: Option<String>,
    #[serde(rename = "type")]
    kind: String,
}

/// Downloads via curl/wget with an on-disk cache.
#[derive(Clone)]
pub struct Fetcher {
    pub cache: PathBuf,
    pub offline: bool,
}

impl Default for Fetcher {
    fn default() -> Self {
        Fetcher { cache: paths::cache_dir(), offline: false }
    }
}

impl Fetcher {
    fn get(&self, url: &str) -> Result<Vec<u8>> {
        if self.offline {
            bail!("offline mode");
        }
        let out = if crate::util::which("curl").is_some() {
            Command::new("curl")
                .args(["-fsSL", "--max-time", "60", "-A", "kernelsmith", url])
                .output()
        } else {
            Command::new("wget").args(["-q", "-T", "60", "-O", "-", url]).output()
        }
        .context("neither curl nor wget could be started")?;
        if !out.status.success() {
            bail!("download failed: {url} ({})", String::from_utf8_lossy(&out.stderr).trim());
        }
        Ok(out.stdout)
    }

    fn github_dir(&self, owner: &str, repo: &str, path: &str) -> Result<Vec<GhEntry>> {
        let key = format!("{owner}_{repo}_{}", path.replace('/', "_"));
        let cache_file = self.cache.join("listings").join(format!("{key}.json"));
        let fresh = std::fs::metadata(&cache_file)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.elapsed().ok())
            .is_some_and(|age| age.as_secs() < 12 * 3600);
        let body = if fresh || self.offline {
            std::fs::read(&cache_file).with_context(|| format!("no cached listing for {path}"))?
        } else {
            let url = format!("https://api.github.com/repos/{owner}/{repo}/contents/{path}");
            let fetched = match self.get(&url) {
                Ok(b) => Ok(b),
                // A missing directory (no patch for this version) is an answer, not an error.
                Err(e) if e.to_string().contains("404") => Ok(b"[]".to_vec()),
                Err(e) => Err(e),
            };
            match fetched {
                Ok(b) => {
                    let _ = std::fs::create_dir_all(cache_file.parent().unwrap());
                    let _ = std::fs::write(&cache_file, &b);
                    b
                }
                // Fall back to a stale listing rather than failing outright.
                Err(e) => std::fs::read(&cache_file).map_err(|_| e)?,
            }
        };
        // A missing directory comes back as a JSON object, not an array.
        Ok(serde_json::from_slice::<Vec<GhEntry>>(&body).unwrap_or_default())
    }

    /// Download `url` into the patch cache (once) and return the local path.
    pub fn download(&self, url: &str, subdir: &str, file_name: &str) -> Result<PathBuf> {
        let dest = self.cache.join("patches").join(subdir).join(file_name);
        if dest.is_file() && std::fs::metadata(&dest).map(|m| m.len() > 0).unwrap_or(false) {
            return Ok(dest);
        }
        let data = self.get(url)?;
        if !looks_like_patch(&data) {
            bail!("{file_name} does not look like a patch file");
        }
        std::fs::create_dir_all(dest.parent().unwrap())?;
        std::fs::write(&dest, data)?;
        Ok(dest)
    }
}

fn looks_like_patch(data: &[u8]) -> bool {
    let head = String::from_utf8_lossy(&data[..data.len().min(64 * 1024)]);
    head.contains("\n--- ") || head.starts_with("--- ") || head.contains("diff --git")
}

/// Resolve one catalog entry for a source tree.
pub fn resolve(info: &PatchInfo, src: &KernelSource, symbols: Option<&SymbolIndex>, fetcher: &Fetcher) -> PatchStatus {
    if let (Some(sym), Some(idx)) = (info.included_if, symbols) {
        if idx.has(sym) {
            return PatchStatus::Included;
        }
    }
    if info.id == "bore" && src.flavor == Flavor::CachyOs {
        return PatchStatus::Included;
    }
    let mm = src.major_minor();
    let mut last_err = None;
    for loc in info.locators {
        let found = match loc {
            Locator::Cachy { dir, name } => {
                let path = format!("{mm}/{dir}");
                fetcher.github_dir("CachyOS", "kernel-patches", &path).map(|entries| {
                    entries.into_iter().find(|e| {
                        e.kind == "file"
                            && match name {
                                NameMatch::Exact(n) => e.name == *n,
                                NameMatch::Prefix(p) => e.name.starts_with(p) && e.name.ends_with(".patch"),
                            }
                    })
                })
            }
            Locator::BoreUpstream => fetcher
                .github_dir("firelzrd", "bore-scheduler", "patches/stable")
                .map(|entries| pick_bore(&entries, &mm)),
        };
        match found {
            Ok(Some(e)) => {
                if let Some(url) = e.download_url {
                    return PatchStatus::Available { url, file_name: e.name };
                }
            }
            Ok(None) => {}
            Err(e) => last_err = Some(e.to_string()),
        }
    }
    match last_err {
        Some(e) => PatchStatus::Error(e),
        None if !info.fallback_hint.is_empty() => PatchStatus::NotPublished(info.fallback_hint.into()),
        None => PatchStatus::NotPublished(format!("No version of this patch is published for Linux {mm} yet.")),
    }
}

/// Choose the best upstream BORE patch for a `major.minor` version. File
/// names look like `0001-linux7.3-rc2-bore-7.0.0.patch`; prefer non-rc.
fn pick_bore(entries: &[GhEntry], mm: &str) -> Option<GhEntry> {
    let needle = format!("linux{mm}");
    let mut matches: Vec<&GhEntry> = entries
        .iter()
        .filter(|e| e.kind == "file" && e.name.ends_with(".patch"))
        .filter(|e| {
            e.name.find(&needle).is_some_and(|i| {
                let next = e.name[i + needle.len()..].chars().next();
                !next.is_some_and(|c| c.is_ascii_digit())
            })
        })
        .collect();
    matches.sort_by_key(|e| (e.name.contains("-rc"), std::cmp::Reverse(e.name.clone())));
    matches.first().map(|e| (*e).clone())
}

/// Test whether a patch applies cleanly to an untouched tree (read-only).
pub fn dry_run(tree: &Path, patch: &Path) -> (bool, String) {
    match crate::util::run_any(
        "patch",
        &["-p1", "--dry-run", "--batch", "-N", "-s", "-d", &tree.to_string_lossy(), "-i", &patch.to_string_lossy()],
    ) {
        Some((ok, out)) => {
            let out = if ok && out.trim().is_empty() { "Applies cleanly.".to_string() } else { out };
            (ok, out)
        }
        None => (false, "Could not run 'patch' (install sys-devel/patch).".into()),
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalPatch {
    pub path: PathBuf,
    pub name: String,
    pub size: u64,
    pub system: bool,
}

/// Patches dropped into ~/.local/share/kernelsmith/patches or /etc/kernelsmith/patches.
pub fn local_patches() -> Vec<LocalPatch> {
    let mut out = vec![];
    for (dir, system) in [(paths::user_patch_dir(), false), (PathBuf::from(paths::SYSTEM_PATCH_DIR), true)] {
        for e in std::fs::read_dir(&dir).into_iter().flatten().flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            if !(name.ends_with(".patch") || name.ends_with(".diff")) {
                continue;
            }
            let size = e.metadata().map(|m| m.len()).unwrap_or(0);
            out.push(LocalPatch { path: e.path(), name, size, system });
        }
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    out
}

/// Save a patch from a URL into the user's patch folder.
pub fn import_from_url(fetcher: &Fetcher, url: &str) -> Result<PathBuf> {
    let name = url
        .rsplit('/')
        .next()
        .filter(|n| !n.is_empty())
        .map(|n| n.split('?').next().unwrap_or(n).to_string())
        .unwrap_or_else(|| "imported.patch".into());
    let name = if name.ends_with(".patch") || name.ends_with(".diff") { name } else { format!("{name}.patch") };
    let data = fetcher.get(url)?;
    if !looks_like_patch(&data) {
        bail!("That URL didn't return a patch (expected a unified diff)");
    }
    let dir = paths::user_patch_dir();
    std::fs::create_dir_all(&dir)?;
    let dest = dir.join(name);
    std::fs::write(&dest, data)?;
    Ok(dest)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn e(name: &str) -> GhEntry {
        GhEntry { name: name.into(), download_url: Some(format!("https://x/{name}")), kind: "file".into() }
    }

    #[test]
    fn bore_picks_matching_version() {
        let entries = vec![
            e("0001-linux6.12.37-bore-7.0.0.patch"),
            e("0001-linux6.1.10-bore-5.0.0.patch"),
            e("0001-linux7.3-rc2-bore-7.0.0.patch"),
        ];
        assert_eq!(pick_bore(&entries, "6.12").unwrap().name, "0001-linux6.12.37-bore-7.0.0.patch");
        assert_eq!(pick_bore(&entries, "6.1").unwrap().name, "0001-linux6.1.10-bore-5.0.0.patch");
        assert_eq!(pick_bore(&entries, "7.3").unwrap().name, "0001-linux7.3-rc2-bore-7.0.0.patch");
        assert!(pick_bore(&entries, "7.4").is_none());
    }

    #[test]
    fn bore_prefers_final_release() {
        let entries = vec![e("0001-linux7.3-rc2-bore-7.0.0.patch"), e("0001-linux7.3.1-bore-7.0.1.patch")];
        assert_eq!(pick_bore(&entries, "7.3").unwrap().name, "0001-linux7.3.1-bore-7.0.1.patch");
    }

    #[test]
    fn patch_detection() {
        assert!(looks_like_patch(b"diff --git a/x b/x\n--- a/x\n+++ b/x\n"));
        assert!(!looks_like_patch(b"<html>404</html>"));
    }
}
