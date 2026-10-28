//! Discovering kernel source trees under /usr/src.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Flavor {
    Gentoo,
    Vanilla,
    CachyOs,
    Zen,
    Xanmod,
    Rt,
    Pf,
    Git,
    /// A tree Portage prepared for a distribution kernel (`*-dist`).
    DistPrepared,
    /// A tree Kernelsmith created for one of its own builds.
    Kernelsmith,
    Other,
}

impl Flavor {
    pub fn label(self) -> &'static str {
        match self {
            Flavor::Gentoo => "Gentoo",
            Flavor::Vanilla => "Vanilla",
            Flavor::CachyOs => "CachyOS",
            Flavor::Zen => "Zen",
            Flavor::Xanmod => "XanMod",
            Flavor::Rt => "Realtime",
            Flavor::Pf => "pf",
            Flavor::Git => "Git",
            Flavor::DistPrepared => "Dist-kernel tree",
            Flavor::Kernelsmith => "Kernelsmith build",
            Flavor::Other => "Custom",
        }
    }

    pub fn blurb(self) -> &'static str {
        match self {
            Flavor::Gentoo => "Upstream Linux plus Gentoo's small set of fixes. The safest starting point.",
            Flavor::Vanilla => "Unmodified upstream Linux from kernel.org.",
            Flavor::CachyOs => "Includes CachyOS performance patches (BORE, BBR3, ADIOS, -O3, extra tick rates).",
            Flavor::Zen => "Desktop-tuned kernel from the Zen project.",
            Flavor::Xanmod => "Performance-oriented kernel from the XanMod project.",
            Flavor::Rt => "Kernel with the full real-time patchset.",
            Flavor::Pf => "pf-kernel with an assorted patch collection.",
            Flavor::Git => "Development snapshot from git.",
            Flavor::DistPrepared => "Headers-only tree Portage installs so external modules can be built for a dist-kernel.",
            Flavor::Kernelsmith => "A tree from an earlier Kernelsmith build.",
            Flavor::Other => "Kernel source tree.",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KernelSource {
    pub path: PathBuf,
    pub dir_name: String,
    /// "7.3.0"
    pub version: String,
    pub extraversion: String,
    pub flavor: Flavor,
    pub is_current_symlink: bool,
    /// Contains build products (vmlinux, Module.symvers) — needs `mrproper`.
    pub is_built: bool,
    pub has_config: bool,
    /// Full sources (not a headers-only module-build tree).
    pub buildable: bool,
}

impl KernelSource {
    pub fn major_minor(&self) -> String {
        self.version.split('.').take(2).collect::<Vec<_>>().join(".")
    }

    /// Numeric (major, minor, patch) for sorting and comparisons.
    pub fn version_tuple(&self) -> (u32, u32, u32) {
        version_tuple(&self.version)
    }

    /// Name shown in the UI: "7.3.0-gentoo".
    pub fn display(&self) -> String {
        format!("{}{}", self.version, self.extraversion)
    }

    /// Trees we want users to pick by default.
    pub fn recommended_rank(&self) -> u32 {
        if !self.buildable {
            return 10;
        }
        match self.flavor {
            Flavor::CachyOs => 0,
            Flavor::Gentoo => 1,
            Flavor::Vanilla | Flavor::Zen | Flavor::Xanmod => 2,
            Flavor::DistPrepared => 4,
            Flavor::Kernelsmith => 9,
            _ => 3,
        }
    }
}

pub fn version_tuple(v: &str) -> (u32, u32, u32) {
    let mut it = v.split(|c: char| !c.is_ascii_digit()).filter(|s| !s.is_empty()).map(|s| s.parse().unwrap_or(0));
    (it.next().unwrap_or(0), it.next().unwrap_or(0), it.next().unwrap_or(0))
}

pub const SRC_ROOT: &str = "/usr/src";

/// Every `linux-*` tree in /usr/src, newest and most recommended first.
pub fn discover() -> Vec<KernelSource> {
    // KERNELSMITH_SRC_ROOT lets you point at sources kept elsewhere (or test trees).
    match std::env::var_os("KERNELSMITH_SRC_ROOT") {
        Some(root) => discover_in(Path::new(&root)),
        None => discover_in(Path::new(SRC_ROOT)),
    }
}

pub fn discover_in(root: &Path) -> Vec<KernelSource> {
    let current = std::fs::read_link(root.join("linux"))
        .ok()
        .map(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default());
    let mut out = vec![];
    for e in std::fs::read_dir(root).into_iter().flatten().flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        let Ok(meta) = std::fs::symlink_metadata(e.path()) else { continue };
        if !name.starts_with("linux-") || !meta.is_dir() {
            continue;
        }
        if let Some(src) = inspect(&e.path(), current.as_deref() == Some(name.as_str())) {
            out.push(src);
        }
    }
    out.sort_by(|a, b| {
        b.version_tuple()
            .cmp(&a.version_tuple())
            .then(a.recommended_rank().cmp(&b.recommended_rank()))
    });
    out
}

pub fn inspect(path: &Path, is_current: bool) -> Option<KernelSource> {
    let makefile = std::fs::read_to_string(path.join("Makefile")).ok()?;
    let field = |key: &str| {
        makefile
            .lines()
            .take(12)
            .find_map(|l| l.strip_prefix(key).map(|v| v.trim_start_matches([' ', '=']).trim().to_string()))
            .unwrap_or_default()
    };
    let (v, p, s) = (field("VERSION"), field("PATCHLEVEL"), field("SUBLEVEL"));
    if v.is_empty() {
        return None;
    }
    let dir_name = path.file_name()?.to_string_lossy().into_owned();
    let extraversion = field("EXTRAVERSION");
    let lower = dir_name.to_lowercase();
    let flavor = if path.join(".kernelsmith").is_dir() {
        Flavor::Kernelsmith
    } else if lower.ends_with("-dist") || path.join("dist-kernel").exists() {
        Flavor::DistPrepared
    } else if lower.contains("cachyos") || path.join("init/Kconfig").exists() && cachy_marker(path) {
        Flavor::CachyOs
    } else if lower.contains("gentoo") {
        Flavor::Gentoo
    } else if lower.contains("zen") {
        Flavor::Zen
    } else if lower.contains("xanmod") {
        Flavor::Xanmod
    } else if lower.contains("-rt") {
        Flavor::Rt
    } else if lower.contains("-pf") {
        Flavor::Pf
    } else if lower.contains("git") {
        Flavor::Git
    } else if extraversion.is_empty() || extraversion.starts_with("-rc") {
        Flavor::Vanilla
    } else {
        Flavor::Other
    };
    Some(KernelSource {
        path: path.to_path_buf(),
        dir_name,
        version: format!("{v}.{p}.{s}"),
        extraversion,
        flavor,
        is_current_symlink: is_current,
        is_built: path.join("vmlinux").exists() || path.join("Module.symvers").exists(),
        has_config: path.join(".config").exists(),
        // Dist-kernel trees only ship headers and scripts for building modules.
        buildable: path.join("kernel/fork.c").exists() && path.join("init/main.c").exists(),
    })
}

fn cachy_marker(path: &Path) -> bool {
    std::fs::read_to_string(path.join("init/Kconfig"))
        .map(|t| t.contains("config CACHY"))
        .unwrap_or(false)
}

/// Suggestions for getting more source trees through Portage.
pub fn source_packages() -> Vec<(&'static str, &'static str, &'static str)> {
    vec![
        ("sys-kernel/gentoo-sources", "Gentoo-patched upstream kernel (recommended baseline)", "::gentoo"),
        ("sys-kernel/cachyos-sources", "CachyOS patchset: BORE, BBR3, ADIOS, -O3 and more", "::CachyOS-kernels overlay"),
        ("sys-kernel/zen-sources", "Zen desktop-tuned kernel", "::gentoo"),
        ("sys-kernel/vanilla-sources", "Pristine kernel.org sources", "::gentoo"),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions() {
        assert_eq!(version_tuple("7.3.0"), (7, 3, 0));
        assert_eq!(version_tuple("6.12"), (6, 12, 0));
    }

    #[test]
    fn discover_does_not_panic() {
        let _ = discover();
    }
}
