//! Reading kernel `.config` files and indexing the Kconfig symbols a source
//! tree defines.
//!
//! Kernelsmith never tries to re-implement Kconfig dependency resolution: the
//! generated script uses `scripts/config` + `make olddefconfig` and then
//! verifies the result. This module only answers "what does this config say"
//! and "does this tree know about symbol X".

use anyhow::{Context, Result};
use flate2::read::GzDecoder;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};
use std::io::Read;
use std::path::Path;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum Val {
    Y,
    M,
    N,
    Str(String),
    /// Integers and hex values, kept verbatim.
    Raw(String),
}

impl Val {
    pub fn render(&self) -> String {
        match self {
            Val::Y => "y".into(),
            Val::M => "m".into(),
            Val::N => "n".into(),
            Val::Str(s) => format!("\"{s}\""),
            Val::Raw(r) => r.clone(),
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct KConfig {
    /// Symbol name without the `CONFIG_` prefix.
    pub entries: BTreeMap<String, Val>,
}

impl KConfig {
    pub fn parse(text: &str) -> Self {
        let mut entries = BTreeMap::new();
        for line in text.lines() {
            let line = line.trim();
            if let Some(rest) = line.strip_prefix("# CONFIG_") {
                if let Some(sym) = rest.strip_suffix(" is not set") {
                    entries.insert(sym.to_string(), Val::N);
                }
                continue;
            }
            let Some(rest) = line.strip_prefix("CONFIG_") else { continue };
            let Some((sym, v)) = rest.split_once('=') else { continue };
            let val = match v {
                "y" => Val::Y,
                "m" => Val::M,
                "n" => Val::N,
                s if s.starts_with('"') => Val::Str(s.trim_matches('"').replace("\\\"", "\"")),
                s => Val::Raw(s.to_string()),
            };
            entries.insert(sym.to_string(), val);
        }
        KConfig { entries }
    }

    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        Ok(Self::parse(&text))
    }

    /// The running kernel's config, if built with `CONFIG_IKCONFIG_PROC`.
    pub fn load_running() -> Result<Self> {
        Ok(Self::parse(&running_config_text()?))
    }

    pub fn get(&self, sym: &str) -> Option<&Val> {
        self.entries.get(sym)
    }

    pub fn is_set(&self, sym: &str) -> bool {
        matches!(self.get(sym), Some(Val::Y | Val::M))
    }

    pub fn is_y(&self, sym: &str) -> bool {
        matches!(self.get(sym), Some(Val::Y))
    }

    pub fn str_val(&self, sym: &str) -> Option<&str> {
        match self.get(sym) {
            Some(Val::Str(s)) => Some(s),
            Some(Val::Raw(s)) => Some(s),
            _ => None,
        }
    }

    /// (built-in, module) counts — a rough proxy for kernel size and build time.
    pub fn counts(&self) -> (usize, usize) {
        let y = self.entries.values().filter(|v| **v == Val::Y).count();
        let m = self.entries.values().filter(|v| **v == Val::M).count();
        (y, m)
    }
}

/// Kernelsmith stamps every kernel it builds with `LOCALVERSION="-ks-<name>"`,
/// so a config carrying that is already trimmed to one machine.
pub fn is_kernelsmith_config(text: &str) -> bool {
    KConfig::parse(text).str_val("LOCALVERSION").is_some_and(|v| v.starts_with("-ks-"))
}

/// Decompress `/proc/config.gz`.
pub fn running_config_text() -> Result<String> {
    let raw = std::fs::read("/proc/config.gz").context("the running kernel does not expose /proc/config.gz")?;
    let mut s = String::new();
    GzDecoder::new(&raw[..]).read_to_string(&mut s)?;
    Ok(s)
}

/// All Kconfig symbols defined by a source tree.
#[derive(Debug, Clone, Default)]
pub struct SymbolIndex {
    pub symbols: HashSet<String>,
    pub files_scanned: usize,
}

impl SymbolIndex {
    pub fn has(&self, sym: &str) -> bool {
        self.symbols.contains(sym)
    }
}

/// Walk every `Kconfig*` file in the tree. Roughly 1–2k files; ~100 ms warm.
pub fn scan_symbols(tree: &Path) -> SymbolIndex {
    let mut idx = SymbolIndex::default();
    let walker = walkdir::WalkDir::new(tree).follow_links(false).into_iter().filter_entry(|e| {
        let n = e.file_name().to_string_lossy();
        // Skip directories that never contain Kconfig files we care about.
        !(e.depth() == 1 && matches!(n.as_ref(), "Documentation" | "tools" | "samples" | ".git" | "LICENSES"))
    });
    for e in walker.flatten() {
        if !e.file_type().is_file() || !e.file_name().to_string_lossy().starts_with("Kconfig") {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(e.path()) else { continue };
        idx.files_scanned += 1;
        for line in text.lines() {
            let t = line.trim_start();
            let rest = t
                .strip_prefix("config ")
                .or_else(|| t.strip_prefix("menuconfig "));
            if let Some(sym) = rest.and_then(|r| r.split_whitespace().next()) {
                idx.symbols.insert(sym.to_string());
            }
        }
    }
    drop_unselectable_on_x86(tree, &mut idx);
    idx
}

/// Some symbols exist but can never be chosen on x86-64. Since 6.13-ish, x86
/// has lazy preemption and the old "none"/"voluntary" models depend on its
/// absence; offering them would only produce a verification failure.
fn drop_unselectable_on_x86(tree: &Path, idx: &mut SymbolIndex) {
    let Ok(text) = std::fs::read_to_string(tree.join("kernel/Kconfig.preempt")) else { return };
    let block = |sym: &str| -> String {
        let start = format!("config {sym}\n");
        text.split_once(&start)
            .map(|(_, rest)| rest.split("\nconfig ").next().unwrap_or("").to_string())
            .unwrap_or_default()
    };
    if block("PREEMPT_VOLUNTARY").contains("depends on !ARCH_HAS_PREEMPT_LAZY") {
        idx.symbols.remove("PREEMPT_VOLUNTARY");
    }
    if block("PREEMPT_NONE").contains("depends on ARCH_NO_PREEMPT") {
        idx.symbols.remove("PREEMPT_NONE");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_basic() {
        let c = KConfig::parse(
            "CONFIG_A=y\nCONFIG_B=m\n# CONFIG_C is not set\nCONFIG_HZ=1000\nCONFIG_LOCALVERSION=\"-x\"\n",
        );
        assert!(c.is_y("A"));
        assert!(c.is_set("B") && !c.is_y("B"));
        assert_eq!(c.get("C"), Some(&Val::N));
        assert_eq!(c.str_val("HZ"), Some("1000"));
        assert_eq!(c.str_val("LOCALVERSION"), Some("-x"));
        assert_eq!(c.counts(), (1, 1));
    }

    #[test]
    fn kernelsmith_configs_are_recognised() {
        assert!(is_kernelsmith_config("CONFIG_LOCALVERSION=\"-ks-max\"\n"));
        assert!(!is_kernelsmith_config("CONFIG_LOCALVERSION=\"-cachyos-dist\"\n"));
        assert!(!is_kernelsmith_config("CONFIG_X=y\n"));
    }
}
