//! Build file changes for /etc/portage edits (always as whole-file rewrites
//! that keep comments and untouched lines intact).

use crate::app::FileChange;
use crate::portage::config::{self, ConfFile, ConfKind, Entry, Line, PortageConfig};
use crate::ui::widgets::Tri;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

fn current_text(cfg: &PortageConfig, path: &Path) -> String {
    for s in &cfg.sets {
        if let Some(f) = s.file(path) {
            return f.raw.clone();
        }
    }
    std::fs::read_to_string(path).unwrap_or_default()
}

fn conf_file(cfg: &PortageConfig, path: &Path) -> ConfFile {
    for s in &cfg.sets {
        if let Some(f) = s.file(path) {
            return f.clone();
        }
    }
    ConfFile::read(path, path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default())
}

/// Where new entries of `kind` go.
pub fn target_path(cfg: &PortageConfig, kind: ConfKind, file_name: &str) -> PathBuf {
    match cfg.set(kind) {
        Some(s) => s.default_target(file_name),
        None => kind.path().join(file_name),
    }
}

/// Append lines to the target file of their kind (grouped per file).
pub fn add_lines(cfg: &PortageConfig, lines: &[(ConfKind, String)], file_name: &str, comment: Option<&str>) -> Vec<FileChange> {
    let mut per_file: BTreeMap<PathBuf, Vec<String>> = BTreeMap::new();
    for (kind, line) in lines {
        per_file.entry(target_path(cfg, *kind, file_name)).or_default().push(line.clone());
    }
    per_file
        .into_iter()
        .map(|(path, new_lines)| {
            let old = current_text(cfg, &path);
            let mut text = old.clone();
            if let Some(c) = comment {
                text = config::append_line(&text, &format!("# {c}"));
            }
            for l in new_lines {
                // Skip exact duplicates already present.
                if old.lines().any(|x| x.trim() == l.trim()) {
                    continue;
                }
                text = config::append_line(&text, &l);
            }
            FileChange { path, old, new: Some(text) }
        })
        .filter(|c| c.new.as_deref() != Some(c.old.as_str()))
        .collect()
}

pub fn replace_entry(cfg: &PortageConfig, path: &Path, idx: usize, entry: Option<Entry>) -> FileChange {
    let f = conf_file(cfg, path);
    let new = config::replace_line(&f, idx, entry.map(Line::Entry));
    FileChange { path: path.to_path_buf(), old: f.raw.clone(), new: Some(new) }
}

/// Remove several lines (possibly across files) in one go.
pub fn remove_lines(cfg: &PortageConfig, lines: &[(PathBuf, usize)]) -> Vec<FileChange> {
    let mut per_file: BTreeMap<PathBuf, Vec<usize>> = BTreeMap::new();
    for (p, i) in lines {
        per_file.entry(p.clone()).or_default().push(*i);
    }
    per_file
        .into_iter()
        .map(|(path, mut idx)| {
            let f = conf_file(cfg, &path);
            let old = f.raw.clone();
            let mut lines = f.lines.clone();
            let mut raw: Vec<&str> = old.lines().collect();
            idx.sort_unstable();
            idx.dedup();
            for i in idx.into_iter().rev() {
                if i < lines.len() {
                    lines.remove(i);
                }
                if i < raw.len() {
                    raw.remove(i);
                }
            }
            let nf = ConfFile { lines, raw: raw.join("\n"), ..f };
            let new = nf.render();
            FileChange { path, old, new: Some(new) }
        })
        .collect()
}

/// Set per-package USE flags: updates the user's existing line for exactly
/// this atom when there is one, otherwise adds a new line.
pub fn set_use_flags(cfg: &PortageConfig, atom: &str, changes: &[(String, Tri)], file_name: &str) -> Vec<FileChange> {
    let mut found: Option<(PathBuf, usize, Entry)> = None;
    if let Some(set) = cfg.set(ConfKind::Use) {
        for f in set.files.iter().filter(|f| !f.ignored) {
            for (i, e) in f.entries() {
                if !e.disabled && e.atom_text == atom {
                    found = Some((f.path.clone(), i, e.clone()));
                }
            }
        }
    }
    let apply = |values: &mut Vec<String>| {
        for (flag, state) in changes {
            values.retain(|v| v.trim_start_matches('-') != flag);
            match state {
                Tri::On => values.push(flag.clone()),
                Tri::Off => values.push(format!("-{flag}")),
                Tri::Default => {}
            }
        }
    };
    match found {
        Some((path, idx, mut e)) => {
            apply(&mut e.values);
            let entry = if e.values.is_empty() { None } else { Some(e) };
            vec![replace_entry(cfg, &path, idx, entry)]
        }
        None => {
            let mut values = Vec::new();
            apply(&mut values);
            if values.is_empty() {
                return Vec::new();
            }
            add_lines(cfg, &[(ConfKind::Use, format!("{atom} {}", values.join(" ")))], file_name, None)
        }
    }
}

/// Current user setting for each flag on exactly matching or broader atoms.
pub fn user_use_state(cfg: &PortageConfig, cat: &str, pn: &str) -> BTreeMap<String, (bool, String)> {
    let mut out = BTreeMap::new();
    if let Some(set) = cfg.set(ConfKind::Use) {
        for f in set.files.iter().filter(|f| !f.ignored) {
            for (_, e) in f.entries() {
                if e.disabled || !e.atom.as_ref().is_some_and(|a| a.matches_key(cat, pn)) {
                    continue;
                }
                for v in &e.values {
                    let (flag, on) = match v.strip_prefix('-') {
                        Some(f) => (f.to_string(), false),
                        None => (v.clone(), true),
                    };
                    out.insert(flag, (on, e.atom_text.clone()));
                }
            }
        }
    }
    out
}

/// How a stray file (e.g. `package.accept_keywordschatgpt-desktop`) gets fixed.
pub struct StrayPlan {
    pub ops: Vec<portagehub::protocol::Op>,
    pub summary: String,
}

/// Move a stray file into the proper directory. When a file with the chosen
/// name already exists there, only the missing lines are appended to it.
pub fn plan_stray_fix(cfg: &PortageConfig, stray: &Path, kind: ConfKind, new_name: &str) -> StrayPlan {
    use portagehub::protocol::Op;
    let mut ops = Vec::new();
    let base = kind.path();
    let set = cfg.set(kind);
    if let Some(set) = set
        && set.exists
        && !set.is_dir
    {
        ops.extend(convert_to_dir_ops(&base));
    }
    let name = if new_name.trim().is_empty() { "migrated" } else { new_name.trim() };
    let target = base.join(name);
    let stray_s = stray.to_string_lossy().to_string();
    let existing = set.and_then(|s| s.file(&target)).map(|f| f.raw.clone()).or_else(|| std::fs::read_to_string(&target).ok());
    match existing {
        None => {
            ops.push(Op::Rename { from: stray_s, to: target.to_string_lossy().to_string() });
            StrayPlan { ops, summary: format!("Move to {}", target.display()) }
        }
        Some(old) => {
            let stray_text = std::fs::read_to_string(stray).unwrap_or_default();
            let have: std::collections::HashSet<&str> = old.lines().map(str::trim).collect();
            let missing: Vec<&str> = stray_text.lines().map(str::trim).filter(|l| !l.is_empty() && !have.contains(l)).collect();
            if missing.is_empty() {
                ops.push(Op::DeleteFile { path: stray_s });
                StrayPlan { ops, summary: format!("{} already contains every line — delete the stray copy", target.display()) }
            } else {
                let mut text = old.clone();
                for l in &missing {
                    text = config::append_line(&text, l);
                }
                ops.push(Op::WriteFile { path: target.to_string_lossy().to_string(), content: text, backup: false });
                ops.push(Op::DeleteFile { path: stray_s });
                StrayPlan { ops, summary: format!("Merge {} new line(s) into the existing {}", missing.len(), target.display()) }
            }
        }
    }
}

/// `/etc/portage/package.use` (file) → `/etc/portage/package.use/main`.
pub fn convert_to_dir_ops(base: &Path) -> Vec<portagehub::protocol::Op> {
    use portagehub::protocol::Op;
    let tmp = format!("{}.portagehub-convert~", base.display());
    vec![
        Op::Rename { from: base.to_string_lossy().to_string(), to: tmp.clone() },
        Op::Mkdir { path: base.to_string_lossy().to_string() },
        Op::Rename { from: tmp, to: base.join("main").to_string_lossy().to_string() },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::portage::config::{ConfSet, parse_line};

    fn cfg_with(raw: &str) -> PortageConfig {
        let f = ConfFile {
            path: "/etc/portage/package.use/a".into(),
            rel: "a".into(),
            ignored: false,
            lines: raw.lines().map(parse_line).collect(),
            raw: raw.into(),
            writable_by_user: false,
        };
        PortageConfig {
            sets: vec![ConfSet { kind: ConfKind::Use, base: "/etc/portage/package.use".into(), exists: true, is_dir: true, files: vec![f] }],
            ..Default::default()
        }
    }

    #[test]
    fn bulk_remove() {
        let cfg = cfg_with("# c\napp-misc/a x\napp-misc/b y\napp-misc/c z\n");
        let ch = remove_lines(&cfg, &[("/etc/portage/package.use/a".into(), 1), ("/etc/portage/package.use/a".into(), 3)]);
        assert_eq!(ch[0].new.as_deref(), Some("# c\napp-misc/b y\n"));
    }

    #[test]
    fn use_merge() {
        let cfg = cfg_with("# c\napp-misc/foo bar -baz\n");
        let ch = set_use_flags(&cfg, "app-misc/foo", &[("baz".into(), Tri::On), ("qux".into(), Tri::Off)], "zz-portagehub");
        assert_eq!(ch.len(), 1);
        assert_eq!(ch[0].new.as_deref(), Some("# c\napp-misc/foo bar baz -qux\n"));
        let ch = set_use_flags(&cfg, "app-misc/foo", &[("bar".into(), Tri::Default), ("baz".into(), Tri::Default)], "zz");
        assert_eq!(ch[0].new.as_deref(), Some("# c\n"));
        let ch = set_use_flags(&cfg, "app-misc/new", &[("x".into(), Tri::On)], "zz-portagehub");
        assert_eq!(ch[0].path, PathBuf::from("/etc/portage/package.use/zz-portagehub"));
        let st = user_use_state(&cfg, "app-misc", "foo");
        assert_eq!(st["baz"].0, false);
    }
}
