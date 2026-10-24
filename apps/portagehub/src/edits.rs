//! Build file changes for /etc/portage edits (always as whole-file rewrites
//! that keep comments and untouched lines intact).
//!
//! A change is only ever built from text that was read faithfully. A file that
//! cannot be read as UTF-8 text, or that sits behind a symlink the helper will not
//! write through, makes every function here return an error that says so: the
//! alternative (treating it as empty) would rewrite the whole file from nothing.

use crate::app::FileChange;
use crate::portage::config::{self, ConfFile, ConfKind, Entry, Line, PortageConfig};
use crate::ui::widgets::Tri;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// The text of `path` as the GUI knows it, or why it must not be edited.
fn current_text(cfg: &PortageConfig, path: &Path) -> Result<String, String> {
    if let Some(why) = cfg.edit_blocked_for(path) {
        return Err(why);
    }
    for s in &cfg.sets {
        if let Some(f) = s.file(path) {
            return Ok(f.raw.clone());
        }
    }
    config::read_utf8(path)
}

fn conf_file(cfg: &PortageConfig, path: &Path) -> Result<ConfFile, String> {
    if let Some(why) = cfg.edit_blocked_for(path) {
        return Err(why);
    }
    for s in &cfg.sets {
        if let Some(f) = s.file(path) {
            return Ok(f.clone());
        }
    }
    let f = ConfFile::read(path, path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default());
    f.edit_blocked().map_or(Ok(f), Err)
}

/// Where new entries of `kind` go.
pub fn target_path(cfg: &PortageConfig, kind: ConfKind, file_name: &str) -> PathBuf {
    match cfg.set(kind) {
        Some(s) => s.default_target(file_name),
        None => kind.path().join(file_name),
    }
}

/// Append lines to the target file of their kind (grouped per file).
pub fn add_lines(cfg: &PortageConfig, lines: &[(ConfKind, String)], file_name: &str, comment: Option<&str>) -> Result<Vec<FileChange>, String> {
    let mut per_file: BTreeMap<PathBuf, Vec<String>> = BTreeMap::new();
    for (kind, line) in lines {
        per_file.entry(target_path(cfg, *kind, file_name)).or_default().push(line.clone());
    }
    let mut out = Vec::new();
    for (path, new_lines) in per_file {
        let old = current_text(cfg, &path)?;
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
        if text != old {
            out.push(FileChange { path, old, new: Some(text) });
        }
    }
    Ok(out)
}

pub fn replace_entry(cfg: &PortageConfig, path: &Path, idx: usize, entry: Option<Entry>) -> Result<FileChange, String> {
    let f = conf_file(cfg, path)?;
    let new = config::replace_line(&f, idx, entry.map(Line::Entry));
    Ok(FileChange { path: path.to_path_buf(), old: f.raw.clone(), new: Some(new) })
}

/// Remove several lines (possibly across files) in one go.
pub fn remove_lines(cfg: &PortageConfig, lines: &[(PathBuf, usize)]) -> Result<Vec<FileChange>, String> {
    let mut per_file: BTreeMap<PathBuf, Vec<usize>> = BTreeMap::new();
    for (p, i) in lines {
        per_file.entry(p.clone()).or_default().push(*i);
    }
    let mut out = Vec::new();
    for (path, mut idx) in per_file {
        let f = conf_file(cfg, &path)?;
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
        out.push(FileChange { path, old, new: Some(new) });
    }
    Ok(out)
}

/// The flag tokens of a `package.use` line: the plain flags, then `VAR: …` sections.
///
/// Portage applies a `VAR:` token as a prefix to every token after it on the same line, so a
/// plain flag appended after one would be read as a flag of that variable. Keeping the parts
/// apart lets a flag always go where Portage will read it as intended.
#[derive(Debug, Default, PartialEq)]
struct UseLine {
    plain: Vec<String>,
    /// (`PYTHON_TARGETS:` as written, tokens without the prefix)
    sections: Vec<(String, Vec<String>)>,
}

impl UseLine {
    fn parse(values: &[String]) -> UseLine {
        let mut line = UseLine::default();
        for v in values {
            if v.ends_with(':') {
                line.sections.push((v.clone(), Vec::new()));
            } else if let Some((_, tokens)) = line.sections.last_mut() {
                tokens.push(v.clone());
            } else {
                line.plain.push(v.clone());
            }
        }
        line
    }

    /// Tokens in file order; a section left without tokens disappears with its header.
    fn tokens(self) -> Vec<String> {
        let mut out = self.plain;
        for (header, tokens) in self.sections {
            if !tokens.is_empty() {
                out.push(header);
                out.extend(tokens);
            }
        }
        out
    }

    /// Set `flag` (a full name such as `python_targets_python3_13`) to `state`.
    fn set(&mut self, flag: &str, state: Tri, use_expand: &[String]) {
        let bare = |t: &str| t.trim_start_matches('-').to_string();
        self.plain.retain(|t| bare(t) != flag);
        for (header, tokens) in &mut self.sections {
            let prefix = format!("{}_", header.trim_end_matches(':').to_lowercase());
            tokens.retain(|t| format!("{prefix}{}", bare(t)) != flag);
        }
        if state == Tri::Default {
            return;
        }
        // The longest USE_EXPAND variable whose name prefixes the flag owns it.
        let owner = use_expand
            .iter()
            .filter(|var| flag.strip_prefix(&format!("{}_", var.to_lowercase())).is_some_and(|rest| !rest.is_empty()))
            .max_by_key(|var| var.len());
        let (token, section) = match owner {
            Some(var) => {
                let rest = &flag[var.len() + 1..];
                (if state == Tri::Off { format!("-{rest}") } else { rest.to_string() }, Some(var.to_uppercase()))
            }
            None => (if state == Tri::Off { format!("-{flag}") } else { flag.to_string() }, None),
        };
        match section {
            None => self.plain.push(token),
            Some(var) => {
                let header = format!("{var}:");
                match self.sections.iter_mut().find(|(h, _)| h.to_lowercase() == header.to_lowercase()) {
                    Some((_, tokens)) => tokens.push(token),
                    None => self.sections.push((header, vec![token])),
                }
            }
        }
    }
}

/// Set per-package USE flags: updates the user's existing line for exactly
/// this atom when there is one, otherwise adds a new line. `use_expand` lists the
/// USE_EXPAND variables (`PYTHON_TARGETS`, `VIDEO_CARDS`, …): their flags are written
/// as `VAR: value` sections, never as bare tokens after a `VAR:` prefix.
pub fn set_use_flags(cfg: &PortageConfig, atom: &str, changes: &[(String, Tri)], file_name: &str, use_expand: &[String]) -> Result<Vec<FileChange>, String> {
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
    let apply = |values: &[String]| {
        let mut line = UseLine::parse(values);
        for (flag, state) in changes {
            line.set(flag, *state, use_expand);
        }
        line.tokens()
    };
    match found {
        Some((path, idx, mut e)) => {
            e.values = apply(&e.values);
            let entry = if e.values.is_empty() { None } else { Some(e) };
            Ok(vec![replace_entry(cfg, &path, idx, entry)?])
        }
        None => {
            let values = apply(&[]);
            if values.is_empty() {
                return Ok(Vec::new());
            }
            add_lines(cfg, &[(ConfKind::Use, format!("{atom} {}", values.join(" ")))], file_name, None)
        }
    }
}

/// Current user setting for each flag on exactly matching or broader atoms. Flags are full
/// names: `PYTHON_TARGETS: python3_12` on a line is the flag `python_targets_python3_12`.
pub fn user_use_state(cfg: &PortageConfig, cat: &str, pn: &str) -> BTreeMap<String, (bool, String)> {
    let mut out = BTreeMap::new();
    if let Some(set) = cfg.set(ConfKind::Use) {
        for f in set.files.iter().filter(|f| !f.ignored) {
            for (_, e) in f.entries() {
                if e.disabled || !e.atom.as_ref().is_some_and(|a| a.matches_key(cat, pn)) {
                    continue;
                }
                for (flag, on) in e.flag_tokens() {
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
pub fn plan_stray_fix(cfg: &PortageConfig, stray: &Path, kind: ConfKind, new_name: &str) -> Result<StrayPlan, String> {
    use portagehub::protocol::{Expect, Op};
    let mut ops = Vec::new();
    let base = kind.path();
    let set = cfg.set(kind);
    if let Some(why) = cfg.edit_blocked_for(&base) {
        return Err(why);
    }
    if let Some(set) = set
        && set.exists
        && !set.is_dir
    {
        ops.extend(convert_to_dir_ops(&base));
    }
    let name = if new_name.trim().is_empty() { "migrated" } else { new_name.trim() };
    let target = base.join(name);
    let stray_s = stray.to_string_lossy().to_string();
    let existing = match set.and_then(|s| s.file(&target)) {
        Some(f) => f.edit_blocked().map_or(Ok(Some(f.raw.clone())), Err)?,
        None => match std::fs::symlink_metadata(&target) {
            Err(_) => None,
            Ok(_) => Some(config::read_utf8(&target)?),
        },
    };
    match existing {
        None => {
            ops.push(Op::Rename { from: stray_s, to: target.to_string_lossy().to_string(), expect: None });
            Ok(StrayPlan { ops, summary: format!("Move to {}", target.display()) })
        }
        Some(old) => {
            let stray_text = config::read_utf8(stray)?;
            let have: std::collections::HashSet<&str> = old.lines().map(str::trim).collect();
            let missing: Vec<&str> = stray_text.lines().map(str::trim).filter(|l| !l.is_empty() && !have.contains(l)).collect();
            if missing.is_empty() {
                ops.push(Op::DeleteFile { path: stray_s, expect: Some(Expect::of(&stray_text)) });
                Ok(StrayPlan { ops, summary: format!("{} already contains every line — delete the stray copy", target.display()) })
            } else {
                let mut text = old.clone();
                for l in &missing {
                    text = config::append_line(&text, l);
                }
                ops.push(Op::WriteFile { path: target.to_string_lossy().to_string(), content: text, backup: false, expect: Some(Expect::of(&old)) });
                ops.push(Op::DeleteFile { path: stray_s, expect: Some(Expect::of(&stray_text)) });
                Ok(StrayPlan { ops, summary: format!("Merge {} new line(s) into the existing {}", missing.len(), target.display()) })
            }
        }
    }
}

/// `/etc/portage/package.use` (file) → `/etc/portage/package.use/main`.
pub fn convert_to_dir_ops(base: &Path) -> Vec<portagehub::protocol::Op> {
    use portagehub::protocol::Op;
    let tmp = format!("{}.portagehub-convert~", base.display());
    vec![
        Op::Rename { from: base.to_string_lossy().to_string(), to: tmp.clone(), expect: None },
        Op::Mkdir { path: base.to_string_lossy().to_string() },
        Op::Rename { from: tmp, to: base.join("main").to_string_lossy().to_string(), expect: None },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::portage::config::{ConfSet, parse_line};

    fn conf(path: &str, raw: &str) -> ConfFile {
        ConfFile { path: path.into(), rel: path.rsplit('/').next().unwrap().into(), ignored: false, lines: raw.lines().map(parse_line).collect(), raw: raw.into(), unreadable: None, symlink: false }
    }

    fn cfg_with(raw: &str) -> PortageConfig {
        PortageConfig {
            sets: vec![ConfSet { kind: ConfKind::Use, base: "/etc/portage/package.use".into(), exists: true, is_dir: true, symlink: false, files: vec![conf("/etc/portage/package.use/a", raw)] }],
            ..Default::default()
        }
    }

    fn ue() -> Vec<String> {
        ["PYTHON_TARGETS", "PYTHON_SINGLE_TARGET", "VIDEO_CARDS", "CPU_FLAGS_X86"].iter().map(|s| s.to_string()).collect()
    }

    fn new_text(ch: &[FileChange]) -> &str {
        ch[0].new.as_deref().unwrap()
    }

    #[test]
    fn bulk_remove() {
        let cfg = cfg_with("# c\napp-misc/a x\napp-misc/b y\napp-misc/c z\n");
        let ch = remove_lines(&cfg, &[("/etc/portage/package.use/a".into(), 1), ("/etc/portage/package.use/a".into(), 3)]).unwrap();
        assert_eq!(ch[0].new.as_deref(), Some("# c\napp-misc/b y\n"));
    }

    #[test]
    fn use_merge() {
        let cfg = cfg_with("# c\napp-misc/foo bar -baz\n");
        let ch = set_use_flags(&cfg, "app-misc/foo", &[("baz".into(), Tri::On), ("qux".into(), Tri::Off)], "zz-portagehub", &[]).unwrap();
        assert_eq!(ch.len(), 1);
        assert_eq!(new_text(&ch), "# c\napp-misc/foo bar baz -qux\n");
        let ch = set_use_flags(&cfg, "app-misc/foo", &[("bar".into(), Tri::Default), ("baz".into(), Tri::Default)], "zz", &[]).unwrap();
        assert_eq!(new_text(&ch), "# c\n");
        let ch = set_use_flags(&cfg, "app-misc/new", &[("x".into(), Tri::On)], "zz-portagehub", &[]).unwrap();
        assert_eq!(ch[0].path, PathBuf::from("/etc/portage/package.use/zz-portagehub"));
        let st = user_use_state(&cfg, "app-misc", "foo");
        assert!(!st["baz"].0);
    }

    // --- P7: USE_EXPAND lines ----------------------------------------------------------

    #[test]
    fn plain_flags_go_before_use_expand_sections() {
        let cfg = cfg_with("dev-python/foo PYTHON_TARGETS: python3_12 -python3_11\n");
        let ch = set_use_flags(&cfg, "dev-python/foo", &[("test".into(), Tri::On)], "zz", &ue()).unwrap();
        assert_eq!(new_text(&ch), "dev-python/foo test PYTHON_TARGETS: python3_12 -python3_11\n");
    }

    #[test]
    fn use_expand_flags_go_into_their_section() {
        let cfg = cfg_with("dev-python/foo PYTHON_TARGETS: python3_12 -python3_11\n");
        let ch = set_use_flags(&cfg, "dev-python/foo", &[("python_targets_python3_13".into(), Tri::On)], "zz", &ue()).unwrap();
        assert_eq!(new_text(&ch), "dev-python/foo PYTHON_TARGETS: python3_12 -python3_11 python3_13\n");
        // Switching one off rewrites it in place; the other tokens of the section stay.
        let ch = set_use_flags(&cfg, "dev-python/foo", &[("python_targets_python3_12".into(), Tri::Off)], "zz", &ue()).unwrap();
        assert_eq!(new_text(&ch), "dev-python/foo PYTHON_TARGETS: -python3_11 -python3_12\n");
        // Resetting removes the mention, and an emptied section takes its header with it.
        let ch = set_use_flags(&cfg, "dev-python/foo", &[("python_targets_python3_12".into(), Tri::Default), ("python_targets_python3_11".into(), Tri::Default)], "zz", &ue()).unwrap();
        assert_eq!(ch[0].new.as_deref(), Some(""));
    }

    #[test]
    fn a_use_expand_flag_without_a_section_starts_one_at_the_end() {
        let cfg = cfg_with("dev-python/foo test\n");
        let ch = set_use_flags(&cfg, "dev-python/foo", &[("python_targets_python3_13".into(), Tri::On)], "zz", &ue()).unwrap();
        assert_eq!(new_text(&ch), "dev-python/foo test PYTHON_TARGETS: python3_13\n");
        // A different variable gets its own section; the previous one is not reused for it.
        let cfg = cfg_with("dev-python/foo PYTHON_TARGETS: python3_12\n");
        let ch = set_use_flags(&cfg, "dev-python/foo", &[("video_cards_radeon".into(), Tri::On)], "zz", &ue()).unwrap();
        assert_eq!(new_text(&ch), "dev-python/foo PYTHON_TARGETS: python3_12 VIDEO_CARDS: radeon\n");
        // The longest variable name wins: python_single_target_* is not python_targets_*.
        let ch = set_use_flags(&cfg_with("# c\n"), "dev-python/bar", &[("python_single_target_python3_13".into(), Tri::On)], "a", &ue()).unwrap();
        assert_eq!(new_text(&ch), "# c\ndev-python/bar PYTHON_SINGLE_TARGET: python3_13\n");
    }

    #[test]
    fn a_flag_is_never_appended_after_a_prefix_without_a_section() {
        // Even when the variable is unknown (portageq failed), nothing lands after `VAR:`.
        let cfg = cfg_with("dev-python/foo PYTHON_TARGETS: python3_12\n");
        let ch = set_use_flags(&cfg, "dev-python/foo", &[("python_targets_python3_13".into(), Tri::On), ("test".into(), Tri::Off)], "zz", &[]).unwrap();
        assert_eq!(new_text(&ch), "dev-python/foo python_targets_python3_13 -test PYTHON_TARGETS: python3_12\n");
    }

    #[test]
    fn user_state_reads_use_expand_lines_like_portage() {
        let cfg = cfg_with("*/* VIDEO_CARDS: -* radeon\ndev-python/foo test PYTHON_TARGETS: python3_12 -python3_11\n");
        let st = user_use_state(&cfg, "media-libs", "mesa");
        assert_eq!(st.keys().collect::<Vec<_>>(), vec!["video_cards_radeon"], "{st:?}");
        let st = user_use_state(&cfg, "dev-python", "foo");
        assert_eq!(st["test"].0, true);
        assert_eq!(st["python_targets_python3_12"].0, true);
        assert_eq!(st["python_targets_python3_11"].0, false);
        assert!(!st.contains_key("python3_12"), "bare token names must not leak: {st:?}");
    }

    // --- P6: never rewrite what could not be read ----------------------------------------

    fn scratch(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("portagehub-edits-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn a_non_utf8_file_is_not_rewritten_from_nothing() {
        let d = scratch("latin1");
        let p = d.join("zz");
        std::fs::write(&p, b"# caf\xe9\napp-misc/foo bar\n").unwrap();
        let f = ConfFile::read(&p, "zz".into());
        assert!(f.unreadable.as_deref().unwrap().contains("not valid UTF-8"), "{:?}", f.unreadable);
        assert!(f.lines.is_empty() && f.raw.is_empty());
        let cfg = PortageConfig { sets: vec![ConfSet { kind: ConfKind::Use, base: d.clone(), exists: true, is_dir: true, symlink: false, files: vec![f] }], ..Default::default() };
        assert!(add_lines(&cfg, &[(ConfKind::Use, "app-misc/new x".into())], "zz", None).unwrap_err().contains("not valid UTF-8"));
        assert!(replace_entry(&cfg, &p, 0, None).is_err());
        assert!(remove_lines(&cfg, &[(p.clone(), 0)]).is_err());
        assert!(set_use_flags(&cfg, "app-misc/foo", &[("x".into(), Tri::On)], "zz", &[]).is_err());
        assert!(cfg.edit_blocked_for(&p).is_some());
        // Files that read fine are still edited.
        let ok = d.join("other");
        std::fs::write(&ok, "app-misc/a x\n").unwrap();
        assert!(add_lines(&cfg, &[(ConfKind::Use, "app-misc/b y".into())], "other", None).is_ok());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn an_unreadable_file_is_reported_not_emptied() {
        use std::os::unix::fs::PermissionsExt;
        let d = scratch("eacces");
        let p = d.join("secret");
        std::fs::write(&p, "app-misc/foo bar\n").unwrap();
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o000)).unwrap();
        if std::fs::File::open(&p).is_ok() {
            eprintln!("SKIP: running as root, chmod 000 does not stop reads");
        } else {
            let f = ConfFile::read(&p, "secret".into());
            assert!(f.unreadable.as_deref().unwrap().contains("cannot be read"), "{:?}", f.unreadable);
            assert!(config::read_utf8(&p).is_err());
        }
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert_eq!(config::read_utf8(&d.join("missing")), Ok(String::new()), "a missing file is simply empty");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn symlinked_config_is_shown_but_not_edited() {
        let d = scratch("symlink");
        std::fs::write(d.join("real"), "app-misc/a x\n").unwrap();
        std::os::unix::fs::symlink(d.join("real"), d.join("link")).unwrap();
        let f = ConfFile::read(&d.join("link"), "link".into());
        assert!(f.symlink && f.unreadable.is_none());
        assert_eq!(f.lines.len(), 1, "reading through the link is fine");
        assert!(f.edit_blocked().unwrap().contains("symlink"));
        let cfg = PortageConfig { sets: vec![ConfSet { kind: ConfKind::Use, base: d.clone(), exists: true, is_dir: true, symlink: false, files: vec![f] }], ..Default::default() };
        assert!(replace_entry(&cfg, &d.join("link"), 0, None).unwrap_err().contains("symlink"));
        let mut root = PortageConfig::default();
        root.root_symlink = true;
        assert!(add_lines(&root, &[(ConfKind::Use, "app-misc/a x".into())], "zz", None).unwrap_err().contains("/etc/portage is a symlink"));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn stray_fix_reads_faithfully() {
        let d = scratch("stray");
        let stray = d.join("package.usechatgpt");
        std::fs::write(&stray, b"app-misc/foo caf\xe9\n").unwrap();
        let cfg = PortageConfig::default();
        // The target directory does not exist in this scratch config, so the move needs no read of it,
        // and the stray itself is only moved, never read.
        let plan = plan_stray_fix(&cfg, &stray, ConfKind::Use, "chatgpt").unwrap();
        assert!(matches!(plan.ops[0], portagehub::protocol::Op::Rename { .. }));
        let _ = std::fs::remove_dir_all(&d);
    }
}
