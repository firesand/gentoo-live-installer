//! make.conf health check: what in the current make.conf goes against the
//! Gentoo documentation, how serious it is, where to read more, and — where
//! a fix is safe to automate — the exact file changes that fix it. Fixes go
//! through the normal review → transaction → undo path.

use super::config::{ConfKind, PortageConfig};
use super::makeconf::{self, MakeConf};
use super::makeconf_validate::{self, ParseCheck};
use super::tuning::{self, Tuning};
use super::useflags::UseDescDb;
use super::vdb::Vdb;
use crate::app::FileChange;
use crate::edits;
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Severity {
    /// Breaks or silently weakens the system.
    Critical,
    /// Likely to cause failures or surprises.
    High,
    /// Against the documented advice; worth changing.
    Medium,
    /// Good to know.
    Info,
}

impl Severity {
    pub fn label(&self) -> &'static str {
        match self {
            Severity::Critical => "CRITICAL",
            Severity::High => "HIGH",
            Severity::Medium => "MEDIUM",
            Severity::Info => "INFO",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Fix {
    pub label: String,
    pub changes: Vec<FileChange>,
}

#[derive(Debug, Clone)]
pub struct Finding {
    pub severity: Severity,
    pub var: String,
    pub title: String,
    pub detail: String,
    /// Where the advice comes from.
    pub link: &'static str,
    pub fix: Option<Fix>,
}

/// Documentation the findings point to.
pub mod links {
    pub const MAKE_CONF: &str = "https://wiki.gentoo.org/wiki//etc/portage/make.conf";
    pub const MAN: &str = "https://devmanual.gentoo.org/eclass-reference/make.conf/index.html";
    pub const GCC_OPT: &str = "https://wiki.gentoo.org/wiki/GCC_optimization";
    pub const SAFE_CFLAGS: &str = "https://wiki.gentoo.org/wiki/Safe_CFLAGS";
    pub const CHOST: &str = "https://wiki.gentoo.org/wiki/Changing_the_CHOST_variable";
    pub const HARDENED_FAQ: &str = "https://wiki.gentoo.org/wiki/Hardened/FAQ";
    pub const MAKEOPTS: &str = "https://wiki.gentoo.org/wiki/MAKEOPTS";
    pub const EMERGE_OPTS: &str = "https://wiki.gentoo.org/wiki/EMERGE_DEFAULT_OPTS";
    pub const FEATURES: &str = "https://wiki.gentoo.org/wiki/FEATURES";
    pub const CCACHE: &str = "https://wiki.gentoo.org/wiki/Ccache";
    pub const BINPKG: &str = "https://wiki.gentoo.org/wiki/Binary_package_guide";
    pub const KEYWORDS: &str = "https://wiki.gentoo.org/wiki/ACCEPT_KEYWORDS";
    pub const LICENSE: &str = "https://wiki.gentoo.org/wiki/Handbook:AMD64/Installation/Base#Optional:_Configure_the_ACCEPT_LICENSE_variable";
    pub const USE_FLAGS: &str = "https://wiki.gentoo.org/wiki/USE_flag";
    pub const CPU_FLAGS: &str = "https://wiki.gentoo.org/wiki/CPU_FLAGS_X86";
    pub const VIDEO_CARDS: &str = "https://wiki.gentoo.org/wiki/NVIDIA/nvidia-drivers";
    pub const L10N: &str = "https://wiki.gentoo.org/wiki/Localization/Guide";
    pub const PYTHON: &str = "https://wiki.gentoo.org/wiki/Project:Python/PYTHON_TARGETS";
    pub const LLVM: &str = "https://wiki.gentoo.org/wiki/LLVM";
    pub const RUST: &str = "https://wiki.gentoo.org/wiki/Rust";
    pub const TMPFS: &str = "https://wiki.gentoo.org/wiki/Portage_TMPDIR_on_tmpfs";
    pub const NICENESS: &str = "https://wiki.gentoo.org/wiki/Portage_niceness";
    pub const MIRRORS: &str = "https://wiki.gentoo.org/wiki/Mirrorselect";
    pub const GRUB: &str = "https://wiki.gentoo.org/wiki/GRUB";
    pub const NEWS_23: &str = "https://www.gentoo.org/support/news-items/2024-03-22-new-23-profiles.html";
}

/// Everything the checks look at. Built in the background (reading the
/// installed packages' LICENSE files is not free).
pub struct Input<'a> {
    pub mc: &'a MakeConf,
    pub cfg: Option<&'a PortageConfig>,
    pub arch: &'a str,
    pub effective: &'a HashMap<String, String>,
    pub tuning: Option<&'a Tuning>,
    pub usedesc: Option<&'a UseDescDb>,
    pub vdb: Option<&'a Vdb>,
    /// Location of the main (gentoo) repository.
    pub repo: Option<&'a Path>,
    /// Strict parse of the current make.conf.
    pub parse: Option<&'a ParseCheck>,
}

/// Variables Portage runs as commands, normally as root.
pub const COMMAND_VARS: &[&str] = &[
    "FETCHCOMMAND",
    "RESUMECOMMAND",
    "FETCH_WRAPPER",
    "PORTAGE_IONICE_COMMAND",
    "PORTAGE_LOGDIR_CLEAN",
    "PORTAGE_ELOG_COMMAND",
    "PORTAGE_GPG_SIGNING_COMMAND",
    "BINPKG_GPG_SIGNING_BASE_COMMAND",
    "BINPKG_GPG_VERIFY_BASE_COMMAND",
];

/// Variables the profile owns; setting them in make.conf is ignored or harmful.
pub const LEAVE_ALONE: &[&str] = &["CHOST", "CBUILD", "CTARGET", "ARCH", "ELIBC", "KERNEL", "USERLAND", "USE_ORDER"];

pub fn is_command_var(name: &str) -> bool {
    COMMAND_VARS.contains(&name) || name.starts_with("FETCHCOMMAND_") || name.starts_with("RESUMECOMMAND_")
}

pub fn is_leave_alone(name: &str) -> bool {
    LEAVE_ALONE.contains(&name) || name.starts_with("CHOST_")
}

/// Deprecated name → what replaces it (None: moved to another file).
pub fn deprecated(name: &str) -> Option<Option<&'static str>> {
    match name {
        "PORT_LOGDIR" => Some(Some("PORTAGE_LOGDIR")),
        "PORT_LOGDIR_CLEAN" => Some(Some("PORTAGE_LOGDIR_CLEAN")),
        "NOCOLOR" => Some(Some("NO_COLOR")),
        "PORTDIR" | "PORTDIR_OVERLAY" | "SYNC" | "PORTAGE_BINHOST" => Some(None),
        _ => None,
    }
}

pub const SAFETY_CRITICAL: &[&str] = &["sandbox", "usersandbox", "userpriv", "network-sandbox", "strict", "protect-owned"];
pub const SAFETY_HIGH: &[&str] = &["preserve-libs", "merge-sync", "ipc-sandbox", "pid-sandbox", "distlocks", "unmerge-orphans"];
const FLAG_VARS: &[&str] = &["COMMON_FLAGS", "CFLAGS", "CXXFLAGS", "FCFLAGS", "FFLAGS"];
const USE_EXPAND_MOVE: &[(&str, &str)] = &[("VIDEO_CARDS", "00video_cards"), ("INPUT_DEVICES", "00input_devices"), ("L10N", "00l10n")];
const PROFILE_MANAGED: &[&str] =
    &["PYTHON_TARGETS", "PYTHON_SINGLE_TARGET", "RUBY_TARGETS", "LLVM_TARGETS", "LLVM_SLOT", "ADA_TARGET", "LUA_TARGETS", "LUA_SINGLE_TARGET"];

fn words(s: &str) -> Vec<String> {
    s.split_whitespace().map(String::from).collect()
}

/// Apply `f` to the text of every make.conf file assigning `name`.
fn edit_files(mc: &MakeConf, name: &str, f: &dyn Fn(&str) -> String) -> Vec<FileChange> {
    let files: BTreeSet<PathBuf> = mc.assignments.iter().filter(|a| a.name == name).map(|a| a.file.clone()).collect();
    files
        .into_iter()
        .filter_map(|p| {
            let old = mc.text_of(&p).to_string();
            let new = f(&old);
            (new != old).then_some(FileChange { path: p, old, new: Some(new) })
        })
        .collect()
}

fn remove_fix(mc: &MakeConf, name: &str, note: &str) -> Vec<FileChange> {
    edit_files(mc, name, &|t| makeconf::remove_var(t, name, Some(note)))
}

/// Drop or replace words in the flag variables (and helper variables they reference).
fn edit_flag_words(mc: &MakeConf, f: &dyn Fn(&str) -> Option<String>) -> Vec<FileChange> {
    let mut names: Vec<String> = FLAG_VARS.iter().map(|s| s.to_string()).collect();
    for a in mc.assignments.iter().filter(|a| FLAG_VARS.contains(&a.name.as_str())) {
        for w in a.value.split_whitespace() {
            let w = w.trim_matches(|c| c == '"' || c == '\\');
            if let Some(n) = w.strip_prefix("${").and_then(|r| r.strip_suffix('}')).or_else(|| w.strip_prefix('$'))
                && !FLAG_VARS.contains(&n)
                && !names.iter().any(|x| x == n)
            {
                names.push(n.to_string());
            }
        }
    }
    let mut out: BTreeMap<PathBuf, FileChange> = BTreeMap::new();
    for (p, text) in &mc.files {
        let mut t = text.clone();
        for n in &names {
            t = makeconf::edit_words(&t, n, f);
        }
        if t != *text {
            out.insert(p.clone(), FileChange { path: p.clone(), old: text.clone(), new: Some(t) });
        }
    }
    out.into_values().collect()
}

/// Set (replace) one `*/* VAR: …` style line in a package.* file.
pub fn conf_line(cfg: &PortageConfig, kind: ConfKind, file_name: &str, prefix: &str, line: &str, comment: &str) -> FileChange {
    let path = edits::target_path(cfg, kind, file_name);
    let old = edits::current_text(cfg, &path);
    let mut lines: Vec<String> = old.lines().map(String::from).collect();
    match lines.iter().position(|l| l.trim_start().starts_with(prefix)) {
        Some(i) => lines[i] = line.to_string(),
        None => {
            if !old.contains(comment) {
                lines.push(format!("# {comment}"));
            }
            lines.push(line.to_string());
        }
    }
    let mut new = lines.join("\n");
    new.push('\n');
    FileChange { path, old, new: Some(new) }
}

/// Parse `-jN`, `-j N`, `--jobs=N`, `--jobs N` (and the same for load).
pub fn opt_num(tokens: &[String], short: &str, long: &str) -> Option<f64> {
    for (i, t) in tokens.iter().enumerate() {
        let val = if let Some(r) = t.strip_prefix(&format!("{long}=")) {
            Some(r.to_string())
        } else if t == long || (!short.is_empty() && t == short) {
            tokens.get(i + 1).cloned()
        } else if short.is_empty() {
            None
        } else {
            t.strip_prefix(short).filter(|r| !r.is_empty() && !r.starts_with('-')).map(String::from)
        };
        if let Some(v) = val.and_then(|v| v.parse::<f64>().ok()) {
            return Some(v);
        }
    }
    None
}

/// License names in a LICENSE string (operators and USE conditionals dropped).
fn license_names(s: &str) -> Vec<String> {
    s.split_whitespace().filter(|t| !matches!(*t, "||" | "(" | ")") && !t.ends_with('?')).map(String::from).collect()
}

/// profiles/license_groups with nested groups resolved.
pub fn license_groups(repo: &Path) -> HashMap<String, HashSet<String>> {
    let text = std::fs::read_to_string(repo.join("profiles/license_groups")).unwrap_or_default();
    let raw: HashMap<String, Vec<String>> = text
        .lines()
        .map(|l| l.split('#').next().unwrap_or("").trim())
        .filter(|l| !l.is_empty())
        .filter_map(|l| {
            let mut it = l.split_whitespace();
            Some((it.next()?.to_string(), it.map(String::from).collect()))
        })
        .collect();
    fn resolve(g: &str, raw: &HashMap<String, Vec<String>>, seen: &mut HashSet<String>, out: &mut HashSet<String>) {
        if !seen.insert(g.to_string()) {
            return;
        }
        for m in raw.get(g).into_iter().flatten() {
            match m.strip_prefix('@') {
                Some(sub) => resolve(sub, raw, seen, out),
                None => {
                    out.insert(m.clone());
                }
            }
        }
    }
    raw.keys()
        .map(|g| {
            let mut out = HashSet::new();
            resolve(g, &raw, &mut HashSet::new(), &mut out);
            (g.clone(), out)
        })
        .collect()
}

/// Is `lic` accepted by ACCEPT_LICENSE tokens (in order, later wins)?
pub fn license_accepted(lic: &str, accept: &[String], groups: &HashMap<String, HashSet<String>>) -> bool {
    let mut ok = false;
    for t in accept {
        let (neg, item) = match t.strip_prefix('-') {
            Some(r) => (true, r),
            None => (false, t.as_str()),
        };
        let hit = item == "*" || item == lic || item.strip_prefix('@').is_some_and(|g| groups.get(g).is_some_and(|s| s.contains(lic)));
        if hit {
            ok = !neg;
        }
    }
    ok
}

pub fn check(inp: &Input) -> Vec<Finding> {
    let mut out = Vec::new();
    let mc = inp.mc;
    let push = |out: &mut Vec<Finding>, severity, var: &str, title: String, detail: String, link, fix: Option<Fix>| {
        out.push(Finding { severity, var: var.to_string(), title, detail, link, fix: fix.filter(|f| !f.changes.is_empty()) });
    };
    let fix = |label: &str, changes: Vec<FileChange>| Some(Fix { label: label.to_string(), changes });

    // --- Can Portage read it at all?
    if let Some(ParseCheck::Error(e)) = inp.parse {
        push(
            &mut out,
            Severity::Critical,
            "make.conf",
            "Portage cannot parse make.conf".into(),
            format!(
                "{e}. Portage only prints a warning and then ignores the file's settings, falling back to the profile defaults. Fix the line in the text editor below."
            ),
            links::MAN,
            None,
        );
    }
    for (p, text) in &mc.files {
        if let Some((line, msg)) = makeconf_validate::precheck(text).into_iter().next() {
            push(
                &mut out,
                Severity::High,
                "make.conf",
                format!("{}, line {line}: {msg}", p.display()),
                "Portage's parser is not bash; this line is read differently from what it looks like.".into(),
                links::MAN,
                None,
            );
        }
    }

    // --- Variables the profile owns
    for name in mc.names() {
        if name.starts_with("CHOST") {
            let v = mc.value(&name);
            let standard = inp.arch == "amd64" && v == "x86_64-pc-linux-gnu";
            push(
                &mut out,
                Severity::Critical,
                &name,
                format!("{name} is set in make.conf"),
                "Since the 23.0 profiles CHOST must not be listed in make.conf — the profile sets it. Changing CHOST on an installed system can break the toolchain and every program (\"error while loading shared libraries\") and needs a full rebuild to recover.".into(),
                if standard { links::NEWS_23 } else { links::CHOST },
                if standard { fix("Remove the line (the profile sets the same value)", remove_fix(mc, &name, &format!("{name} removed: the 23.0 profile sets it"))) } else { None },
            );
        } else if is_leave_alone(&name) {
            push(
                &mut out,
                Severity::Medium,
                &name,
                format!("{name} is set in make.conf"),
                "This variable belongs to the profile; setting it in make.conf is ignored or harmful.".into(),
                links::MAN,
                fix("Remove the line", remove_fix(mc, &name, &format!("{name} removed: set by the profile"))),
            );
        }
        if let Some(repl) = deprecated(&name) {
            let (detail, f) = match (name.as_str(), repl) {
                (_, Some(new)) if mc.last(new).is_some() => (
                    format!("{name} is deprecated and {new} is already set."),
                    fix("Remove the old line", remove_fix(mc, &name, &format!("{name} removed: deprecated, {new} is used"))),
                ),
                (_, Some(new)) => (
                    format!("{name} is deprecated; the current name is {new}."),
                    fix(&format!("Rename to {new}"), edit_files(mc, &name, &|t| makeconf::rename_var(t, &name, new))),
                ),
                ("PORTAGE_BINHOST", None) => ("Binary package hosts are configured in /etc/portage/binrepos.conf now (with signature checking).".into(), None),
                ("PORTDIR", None) => {
                    let same = inp.repo.is_some_and(|r| Path::new(&mc.value("PORTDIR")) == r);
                    (
                        "Repository locations live in /etc/portage/repos.conf. Removing PORTDIR is safe only when repos.conf already points to the same tree."
                            .into(),
                        if same {
                            fix(
                                "Remove the line (repos.conf already uses this location)",
                                remove_fix(mc, &name, "PORTDIR removed: the location comes from repos.conf"),
                            )
                        } else {
                            None
                        },
                    )
                }
                _ => (format!("{name} is deprecated: repositories and their sync settings live in /etc/portage/repos.conf."), None),
            };
            push(&mut out, Severity::Medium, &name, format!("Deprecated variable {name}"), detail, links::MAN, f);
        }
        let assigns: Vec<_> = mc.assignments.iter().filter(|a| a.name == name).collect();
        if assigns.iter().filter(|a| !a.self_ref).count() > 1 {
            push(
                &mut out,
                Severity::Info,
                &name,
                format!("{name} is assigned {} times", assigns.len()),
                "Only the last assignment counts; earlier ones are dead text.".into(),
                links::MAKE_CONF,
                None,
            );
        }
    }

    // --- Compiler flags
    let flags: Vec<String> = ["COMMON_FLAGS", "CFLAGS", "CXXFLAGS"].iter().flat_map(|v| words(&mc.value(v))).collect();
    let has_flag = |p: &dyn Fn(&str) -> bool| flags.iter().any(|f| p(f));
    if has_flag(&|f| f == "-Ofast" || f == "-ffast-math" || f == "-funsafe-math-optimizations") {
        push(
            &mut out,
            Severity::High,
            "COMMON_FLAGS",
            "-Ofast / -ffast-math is set system-wide".into(),
            "The GCC optimization guide: \"Do not use this system-wide ever.\" -ffast-math breaks IEEE floating point rules, so programs compute wrong numbers silently instead of failing.".into(),
            links::GCC_OPT,
            fix("Use -O2 and drop -ffast-math", edit_flag_words(mc, &|w| match w {
                "-Ofast" => Some("-O2".into()),
                "-ffast-math" | "-funsafe-math-optimizations" => Some(String::new()),
                _ => None,
            })),
        );
    }
    if has_flag(&|f| f == "-O3") {
        push(
            &mut out,
            Severity::Medium,
            "COMMON_FLAGS",
            "-O3 is set system-wide".into(),
            "The Gentoo default and the wiki's sane setting is -O2; -O3 is \"not recommended outside of running test suites\". It makes binaries bigger, rarely faster, and is where compiler bugs and package breakage show up first. Use -O3 per package (package.env) where it is measured to help. Only packages built from now on change.".into(),
            links::GCC_OPT,
            fix("Use -O2", edit_flag_words(mc, &|w| (w == "-O3").then(|| "-O2".into()))),
        );
    }
    let is_graphite = |f: &str| f.starts_with("-floop-") || f.starts_with("-fgraphite");
    if has_flag(&|f| is_graphite(f)) {
        let helpers: Vec<String> = mc
            .assignments
            .iter()
            .filter(|a| !FLAG_VARS.contains(&a.name.as_str()) && !a.expanded.is_empty() && a.expanded.split_whitespace().all(is_graphite))
            .map(|a| a.name.clone())
            .collect();
        let mut changes = edit_flag_words(mc, &|w| {
            let bare = w.trim_start_matches("${").trim_start_matches('$').trim_end_matches('}');
            (is_graphite(w) || helpers.iter().any(|h| h == bare)).then(String::new)
        });
        for h in &helpers {
            let path = mc.last(h).map(|a| a.file.clone());
            if let Some(c) = path.and_then(|p| changes.iter_mut().find(|c| c.path == p))
                && let Some(t) = c.new.as_mut()
            {
                *t = makeconf::remove_var(t, h, Some(&format!("{h} removed: graphite flags are not supported system-wide")));
            }
        }
        push(
            &mut out,
            Severity::Medium,
            "COMMON_FLAGS",
            "Graphite loop optimisations (-floop-*) are set system-wide".into(),
            "Graphite is listed on the GCC optimization page but not endorsed; together with -O3 and LTO it stacks the least-tested options. Expect occasional miscompiles that are hard to trace.".into(),
            links::GCC_OPT,
            fix("Remove the graphite flags", changes),
        );
    }
    let hardening = |f: &str| {
        f.starts_with("-fstack-protector")
            || f.starts_with("-D_FORTIFY_SOURCE")
            || f == "-fstack-clash-protection"
            || f.starts_with("-fcf-protection")
            || f == "-fPIE"
            || f == "-pie"
    };
    if has_flag(&|f| hardening(f)) {
        push(
            &mut out,
            Severity::Medium,
            "COMMON_FLAGS",
            "Hardening flags are added by hand".into(),
            "PIE, the stack protector, FORTIFY_SOURCE, stack-clash protection and CET already come from the toolchain and the 23.0 profile. The Hardened FAQ calls adding them by hand \"not recommended\"; -fstack-protector-all can break glibc.".into(),
            links::HARDENED_FAQ,
            fix("Remove the hand-added hardening flags", edit_flag_words(mc, &|w| hardening(w).then(String::new))),
        );
    }
    let march = flags.iter().find_map(|f| f.strip_prefix("-march=")).map(String::from);
    let features: Vec<String> = words(&mc.value("FEATURES"));
    let eff_features: Vec<String> = words(inp.effective.get("FEATURES").map(|s| s.as_str()).unwrap_or(""));
    if march.as_deref() == Some("native") && eff_features.iter().any(|f| f == "distcc") {
        push(
            &mut out,
            Severity::High,
            "COMMON_FLAGS",
            "-march=native together with distcc".into(),
            "Helpers would compile for their own CPU. Use the explicit -march that native resolves to.".into(),
            links::SAFE_CFLAGS,
            None,
        );
    }
    if let (Some(m), Some(native)) = (march.as_deref(), inp.tuning.and_then(|t| t.native.as_ref()))
        && m != "native"
        && m != native.march
    {
        let detail = if m.starts_with("x86-64") {
            format!(
                "-march={m} runs on any x86-64 CPU of that level but leaves out instructions this CPU has; -march=native here means -march={}.",
                native.march
            )
        } else {
            format!(
                "-march=native resolves to -march={} on this CPU. If {m} needs instructions this CPU lacks, programs crash with \"illegal instruction\".",
                native.march
            )
        };
        push(&mut out, Severity::Info, "COMMON_FLAGS", format!("-march={m} differs from this CPU ({})", native.march), detail, links::SAFE_CFLAGS, None);
    }
    if march.is_none() && !flags.is_empty() {
        push(
            &mut out,
            Severity::Info,
            "COMMON_FLAGS",
            "No -march set".into(),
            "Without -march the compiler targets generic x86-64. The wiki's sane setting is -march=native -O2 -pipe.".into(),
            links::SAFE_CFLAGS,
            None,
        );
    }

    // --- LDFLAGS and RUSTFLAGS
    if let Some(a) = mc.last("LDFLAGS")
        && !mc.assignments.iter().any(|x| x.name == "LDFLAGS" && x.self_ref)
    {
        let kept: Vec<&str> = a.value.split_whitespace().filter(|w| *w != "\\" && *w != "-Wl,-O1" && *w != "-Wl,--as-needed").collect();
        let new_value = format!("${{LDFLAGS}}{}{}", if kept.is_empty() { "" } else { " " }, kept.join(" "));
        let file = a.file.clone();
        let text = mc.text_of(&file).to_string();
        push(
            &mut out,
            Severity::High,
            "LDFLAGS",
            "LDFLAGS replaces the profile's linker flags".into(),
            "LDFLAGS is not incremental: this drops the profile's -Wl,-O1 -Wl,--as-needed -Wl,-z,pack-relative-relocs (smaller, faster-loading binaries). Write LDFLAGS=\"${LDFLAGS} …\" to add to them instead.".into(),
            links::MAKE_CONF,
            fix(&format!("Write LDFLAGS=\"{new_value}\""), vec![FileChange { path: file, old: text.clone(), new: Some(makeconf::set_var(&text, "LDFLAGS", &new_value)) }]),
        );
    }
    if let Some(a) = mc.last("RUSTFLAGS") {
        let toks = words(&a.expanded);
        let mut kept: Vec<String> = Vec::new();
        let mut dropped: Vec<String> = Vec::new();
        let mut i = 0;
        while i < toks.len() {
            let (opt, consumed) =
                if toks[i] == "-C" { (toks.get(i + 1).cloned().unwrap_or_default(), 2) } else { (toks[i].trim_start_matches("-C").to_string(), 1) };
            if opt.starts_with("opt-level=") && opt != "opt-level=3" || opt.starts_with("strip=") {
                dropped.push(opt);
            } else {
                kept.extend(toks[i..(i + consumed).min(toks.len())].iter().cloned());
            }
            i += consumed;
        }
        if !dropped.is_empty() && !a.self_ref {
            let text = mc.text_of(&a.file).to_string();
            let new = if kept.is_empty() {
                makeconf::remove_var(&text, "RUSTFLAGS", Some("RUSTFLAGS removed: Cargo's release profile is used"))
            } else {
                makeconf::set_var(&text, "RUSTFLAGS", &kept.join(" "))
            };
            push(
                &mut out,
                Severity::Medium,
                "RUSTFLAGS",
                format!("RUSTFLAGS overrides {}", dropped.join(", ")),
                "The Rust wiki advises leaving RUSTFLAGS unset: Cargo's release profile already uses opt-level=3, and Portage strips binaries itself (strip= removes what splitdebug and QA checks need).".into(),
                links::RUST,
                fix(&format!("Drop {}", dropped.join(", ")), vec![FileChange { path: a.file.clone(), old: text, new: Some(new) }]),
            );
        }
    }

    // --- MAKEOPTS / EMERGE_DEFAULT_OPTS
    if let Some(t) = inp.tuning {
        let s = tuning::suggest_makeopts(t.hw.threads, t.hw.mem_kib);
        let mk = words(inp.effective.get("MAKEOPTS").map(|s| s.as_str()).unwrap_or(&mc.value("MAKEOPTS")));
        let jobs = opt_num(&mk, "-j", "--jobs").map(|v| v as usize).unwrap_or(t.hw.threads);
        let load = opt_num(&mk, "-l", "--load-average");
        let rule =
            format!("{} threads, {:.1} GiB RAM: min({}, {:.1} ÷ 2 = {}) = {}", t.hw.threads, t.hw.mem_gib(), t.hw.threads, t.hw.mem_gib(), s.ram_jobs, s.jobs);
        if jobs > s.jobs {
            let extra: Vec<String> = mk
                .iter()
                .filter(|w| {
                    !matches!(w.as_str(), "-j" | "-l" | "--jobs" | "--load-average")
                        && opt_num(std::slice::from_ref(w), "-j", "--jobs").is_none()
                        && opt_num(std::slice::from_ref(w), "-l", "--load-average").is_none()
                        && w.parse::<f64>().is_err()
                })
                .cloned()
                .collect();
            let mut value = format!("-j{} -l{}", s.jobs, s.load);
            if !extra.is_empty() {
                value = format!("{value} {}", extra.join(" "));
            }
            let file = mc.last("MAKEOPTS").map(|a| a.file.clone()).unwrap_or_else(|| mc.main_file());
            let ftext = mc.text_of(&file).to_string();
            push(
                &mut out,
                Severity::High,
                "MAKEOPTS",
                format!("{jobs} compile jobs can exhaust RAM"),
                format!(
                    "Gentoo's rule is jobs = min(threads, RAM ÷ 2 GiB) — {rule}. Large C++ builds (LLVM, Chromium, qtwebengine) need up to 2 GB per job; beyond that the OOM killer ends builds."
                ),
                links::MAKEOPTS,
                fix(
                    &format!("Set MAKEOPTS=\"{value}\""),
                    vec![FileChange { path: file, old: ftext.clone(), new: Some(makeconf::set_var(&ftext, "MAKEOPTS", &value)) }],
                ),
            );
        } else if load.is_none() {
            push(
                &mut out,
                Severity::Info,
                "MAKEOPTS",
                "MAKEOPTS has no load limit (-l)".into(),
                format!("A load limit slightly above the thread count (-l{}) keeps parallel package builds from overloading the machine.", s.load),
                links::MAKEOPTS,
                None,
            );
        }
        let eo = words(&mc.value("EMERGE_DEFAULT_OPTS"));
        let ejobs = opt_num(&eo, "-j", "--jobs").unwrap_or(1.0) as usize;
        let eload = opt_num(&eo, "", "--load-average");
        if ejobs > 1 && eload.is_none() {
            let l = (t.hw.threads as f64 * 0.9).floor() as usize;
            let file = mc.last("EMERGE_DEFAULT_OPTS").map(|a| a.file.clone()).unwrap_or_else(|| mc.main_file());
            let ftext = mc.text_of(&file).to_string();
            let mut new = eo.clone();
            new.push(format!("--load-average={l}"));
            push(
                &mut out,
                Severity::Medium,
                "EMERGE_DEFAULT_OPTS",
                format!("--jobs={ejobs} without --load-average"),
                format!(
                    "Each of the {ejobs} parallel packages may run {jobs} compile jobs ({} at once). The wiki pairs --jobs with --load-average near 0.9 × threads.",
                    ejobs * jobs
                ),
                links::EMERGE_OPTS,
                fix(
                    &format!("Add --load-average={l}"),
                    vec![FileChange { path: file, old: ftext.clone(), new: Some(makeconf::set_var(&ftext, "EMERGE_DEFAULT_OPTS", &new.join(" "))) }],
                ),
            );
        } else if ejobs * jobs > s.jobs {
            push(
                &mut out,
                Severity::Info,
                "EMERGE_DEFAULT_OPTS",
                format!("Up to {} compile jobs when {ejobs} packages build at once", ejobs * jobs),
                format!(
                    "--load-average limits this most of the time, but two RAM-hungry builds (Rust, LLVM, Chromium) at once can still run out of the {:.0} GiB. A \"lowjobs\" env file (Configuration → env) for those packages avoids it.",
                    t.hw.mem_gib()
                ),
                links::MAKEOPTS,
                None,
            );
        }
        if eo.iter().any(|w| w == "--binpkg-respect-use=n") {
            let file = mc.last("EMERGE_DEFAULT_OPTS").map(|a| a.file.clone()).unwrap_or_else(|| mc.main_file());
            let ftext = mc.text_of(&file).to_string();
            let new: Vec<String> = eo.iter().map(|w| if w == "--binpkg-respect-use=n" { "--binpkg-respect-use=y".into() } else { w.clone() }).collect();
            push(
                &mut out,
                Severity::Critical,
                "EMERGE_DEFAULT_OPTS",
                "--binpkg-respect-use=n".into(),
                "Binary packages built with other USE flags would be installed anyway; the binary host guide warns this can break the system.".into(),
                links::BINPKG,
                fix(
                    "Use --binpkg-respect-use=y",
                    vec![FileChange { path: file, old: ftext.clone(), new: Some(makeconf::set_var(&ftext, "EMERGE_DEFAULT_OPTS", &new.join(" "))) }],
                ),
            );
        }
    }

    // --- FEATURES
    let drop_features = |bad: &dyn Fn(&str) -> bool| edit_files(mc, "FEATURES", &|t| makeconf::edit_words(t, "FEATURES", &|w| bad(w).then(String::new)));
    if features.iter().any(|f| f == "-*") {
        push(
            &mut out,
            Severity::Critical,
            "FEATURES",
            "FEATURES=\"-* …\" removes every default feature".into(),
            "That includes sandbox, userpriv and network-sandbox, which protect the system from build scripts.".into(),
            links::FEATURES,
            fix("Remove -*", drop_features(&|w| w == "-*")),
        );
    }
    let crit: Vec<&str> = SAFETY_CRITICAL.iter().copied().filter(|s| features.iter().any(|f| f.strip_prefix('-') == Some(s))).collect();
    if !crit.is_empty() {
        push(
            &mut out,
            Severity::Critical,
            "FEATURES",
            format!("Safety features disabled: {}", crit.join(", ")),
            "sandbox/usersandbox stop build scripts from writing outside their directory, userpriv drops root while compiling, network-sandbox cuts network access during builds, strict and protect-owned guard digests and other packages' files. The man page says sandbox should not be disabled. Scope exceptions to the one package that needs them (package.env).".into(),
            links::FEATURES,
            fix("Re-enable them", drop_features(&|w| w.strip_prefix('-').is_some_and(|s| SAFETY_CRITICAL.contains(&s)))),
        );
    }
    let high: Vec<&str> = SAFETY_HIGH.iter().copied().filter(|s| features.iter().any(|f| f.strip_prefix('-') == Some(s))).collect();
    if !high.is_empty() {
        push(&mut out, Severity::High, "FEATURES", format!("Protective features disabled: {}", high.join(", ")), "preserve-libs keeps old libraries while programs still use them; the others isolate or synchronise builds. Disabling them trades safety for nothing measurable.".into(), links::FEATURES, fix("Re-enable them", drop_features(&|w| w.strip_prefix('-').is_some_and(|s| SAFETY_HIGH.contains(&s)))));
    }
    if features.iter().any(|f| f == "binpkg-ignore-signature") {
        push(
            &mut out,
            Severity::Critical,
            "FEATURES",
            "binpkg-ignore-signature".into(),
            "Binary packages would be installed without checking their signature — the man page calls this dangerous.".into(),
            links::BINPKG,
            fix("Remove it", drop_features(&|w| w == "binpkg-ignore-signature")),
        );
    }
    if eff_features.iter().any(|f| f == "getbinpkg") && !eff_features.iter().any(|f| f == "binpkg-request-signature") {
        let file = mc.last("FEATURES").map(|a| a.file.clone()).unwrap_or_else(|| mc.main_file());
        let ftext = mc.text_of(&file).to_string();
        let mut new = features.clone();
        new.push("binpkg-request-signature".into());
        push(
            &mut out,
            Severity::High,
            "FEATURES",
            "getbinpkg without binpkg-request-signature".into(),
            "Downloaded binary packages should be required to carry a valid signature.".into(),
            links::BINPKG,
            fix(
                "Add binpkg-request-signature",
                vec![FileChange { path: file, old: ftext.clone(), new: Some(makeconf::set_var(&ftext, "FEATURES", &new.join(" "))) }],
            ),
        );
    }
    if features.iter().any(|f| f == "ccache") {
        push(&mut out, Severity::Medium, "FEATURES", "ccache is enabled for every package".into(), "The man page warns global ccache is \"known to cause numerous compilation failures\"; the ccache wiki prefers enabling it per package (Configuration → env → ccache template).".into(), links::CCACHE, fix("Remove ccache from FEATURES", drop_features(&|w| w == "ccache")));
    }

    // --- Branch and licenses
    let kw = words(&mc.value("ACCEPT_KEYWORDS"));
    if !kw.is_empty() {
        let testing = format!("~{}", inp.arch);
        if kw.iter().any(|k| k == "*" || k == "**" || k == "~*") {
            push(
                &mut out,
                Severity::Critical,
                "ACCEPT_KEYWORDS",
                "ACCEPT_KEYWORDS contains * or **".into(),
                "These are only valid per package in package.accept_keywords; system-wide they accept untested and unkeyworded versions of everything.".into(),
                links::KEYWORDS,
                fix(
                    "Remove the line (back to the profile's stable branch)",
                    remove_fix(mc, "ACCEPT_KEYWORDS", "ACCEPT_KEYWORDS removed: the profile sets the stable branch"),
                ),
            );
        } else if kw.contains(&testing) {
            push(&mut out, Severity::High, "ACCEPT_KEYWORDS", format!("The whole system uses the testing branch ({testing})"), "The Handbook warns this brings hundreds of updates and \"may prove a challenge to revert\" (going back means downgrades, e.g. of glibc, which Portage cannot do safely). Prefer package.accept_keywords for the few packages that need testing versions.".into(), links::KEYWORDS, None);
        } else if kw == [inp.arch.to_string()] {
            push(
                &mut out,
                Severity::Info,
                "ACCEPT_KEYWORDS",
                format!("ACCEPT_KEYWORDS=\"{}\" repeats the profile default", inp.arch),
                "The profile already selects the stable branch; the line can go.".into(),
                links::KEYWORDS,
                fix("Remove the redundant line", remove_fix(mc, "ACCEPT_KEYWORDS", "ACCEPT_KEYWORDS removed: same as the profile default")),
            );
        }
    }
    let lic = words(&mc.value("ACCEPT_LICENSE"));
    if let Some(repo) = inp.repo {
        let groups = license_groups(repo);
        let unknown: Vec<&String> = lic.iter().filter(|t| t.trim_start_matches('-').strip_prefix('@').is_some_and(|g| !groups.contains_key(g))).collect();
        if !unknown.is_empty() {
            push(
                &mut out,
                Severity::Medium,
                "ACCEPT_LICENSE",
                format!("Unknown license group(s): {}", unknown.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(" ")),
                "License groups are defined in profiles/license_groups of the gentoo repository.".into(),
                links::LICENSE,
                None,
            );
        }
        let accepts_all = lic.last().is_some_and(|t| t == "*") || (lic.iter().any(|t| t == "*") && !lic.iter().any(|t| t == "-*"));
        if accepts_all {
            let target = vec!["-*".to_string(), "@FREE".into(), "@BINARY-REDISTRIBUTABLE".into()];
            // Every installed package whose licenses the new value would not accept
            // gets a package.license entry, so nothing installed becomes masked.
            let mut entries: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
            if let Some(vdb) = inp.vdb {
                for p in &vdb.pkgs {
                    for l in license_names(&p.read("LICENSE")) {
                        if !license_accepted(&l, &target, &groups) {
                            entries.entry(p.key()).or_default().insert(l);
                        }
                    }
                }
            }
            let mut changes = Vec::new();
            if let Some(a) = mc.last("ACCEPT_LICENSE") {
                let text = mc.text_of(&a.file).to_string();
                changes.push(FileChange { path: a.file.clone(), old: text.clone(), new: Some(makeconf::set_var(&text, "ACCEPT_LICENSE", &target.join(" "))) });
            }
            let lines: Vec<(ConfKind, String)> =
                entries.iter().map(|(k, ls)| (ConfKind::License, format!("{k} {}", ls.iter().cloned().collect::<Vec<_>>().join(" ")))).collect();
            let complete = inp.vdb.is_some() && inp.cfg.is_some();
            if let Some(cfg) = inp.cfg {
                changes.extend(edits::add_lines(
                    cfg,
                    &lines,
                    "zz-portagehub",
                    Some("Licenses of installed packages, accepted per package (ACCEPT_LICENSE narrowed by PortageHub)"),
                ));
            }
            push(
                &mut out,
                Severity::High,
                "ACCEPT_LICENSE",
                "ACCEPT_LICENSE=\"*\" accepts every license".into(),
                format!(
                    "That includes every end-user licence agreement (EULA) without you seeing it. The default is \"-* @FREE\"; the Handbook example adds @BINARY-REDISTRIBUTABLE. The fix keeps everything installed working: {} installed package(s) whose licence is outside those groups (e.g. NVIDIA drivers, firmware) get a line in package.license — listed in the diff.",
                    entries.len()
                ),
                links::LICENSE,
                if complete { fix("Narrow ACCEPT_LICENSE and accept installed EULAs per package", changes) } else { None },
            );
        }
    }

    // --- USE
    let use_words: Vec<String> = mc
        .assignments
        .iter()
        .filter(|a| a.name == "USE")
        .flat_map(|a| a.value.split_whitespace().map(|w| w.trim_matches(|c| c == '"' || c == '\\').to_string()).collect::<Vec<_>>())
        .filter(|w| !w.is_empty() && !w.starts_with('$'))
        .collect();
    if use_words.iter().any(|w| w == "-*") {
        push(
            &mut out,
            Severity::High,
            "USE",
            "USE=\"-* …\" removes the profile's USE flags".into(),
            "The Handbook calls -* in USE strongly discouraged: the profile's defaults prevent dependency conflicts.".into(),
            links::USE_FLAGS,
            None,
        );
    }
    let mut seen = HashSet::new();
    let dups: BTreeSet<String> = use_words.iter().filter(|w| !seen.insert(w.as_str())).cloned().collect();
    let use_files: BTreeSet<&PathBuf> = mc.assignments.iter().filter(|a| a.name == "USE").map(|a| &a.file).collect();
    if !dups.is_empty() {
        let counts: RefCell<HashMap<String, usize>> = RefCell::new(use_words.iter().fold(HashMap::new(), |mut m, w| {
            *m.entry(w.clone()).or_insert(0) += 1;
            m
        }));
        // Words are visited last to first: drop every occurrence but the first.
        let changes = edit_files(mc, "USE", &|t| {
            makeconf::edit_words(t, "USE", &|w| {
                let mut c = counts.borrow_mut();
                let n = c.get_mut(w)?;
                *n -= 1;
                (*n > 0).then(String::new)
            })
        });
        push(
            &mut out,
            Severity::Info,
            "USE",
            format!("Duplicate USE flags: {}", dups.iter().cloned().collect::<Vec<_>>().join(" ")),
            "Harmless, but noise.".into(),
            links::USE_FLAGS,
            // Words are visited last-to-first within one file: only offer the fix
            // when USE lives in a single file.
            if use_files.len() == 1 { fix("Remove the duplicates", changes) } else { None },
        );
    }
    if let Some(db) = inp.usedesc {
        let mut unknown = Vec::new();
        let mut local_only = Vec::new();
        for w in use_words.iter().map(|w| w.trim_start_matches(['-', '+'])) {
            if w == "*" || unknown.contains(&w.to_string()) || local_only.contains(&w.to_string()) {
                continue;
            }
            if db.global.contains_key(w) || db.split_expand(w).is_some() {
                continue;
            }
            if db.local.values().any(|m| m.contains_key(w)) {
                local_only.push(w.to_string());
            } else {
                unknown.push(w.to_string());
            }
        }
        if !unknown.is_empty() {
            push(
                &mut out,
                Severity::Medium,
                "USE",
                format!("Unknown USE flags: {}", unknown.join(" ")),
                "No package and no profile description knows these flags — a typo or a flag that was removed. They do nothing.".into(),
                links::USE_FLAGS,
                None,
            );
        }
        if !local_only.is_empty() {
            push(&mut out, Severity::Info, "USE", format!("Package-specific flags in global USE: {}", local_only.join(" ")), "These flags exist only for a few packages; setting them in package.use for those packages keeps their effect visible and avoids surprises when another package adopts the name.".into(), links::USE_FLAGS, None);
        }
    }

    // --- USE_EXPAND variables
    let pkg_use_flags = |var: &str| -> Option<Vec<String>> {
        let cfg = inp.cfg?;
        let set = cfg.set(ConfKind::Use)?;
        let prefix = format!("*/* {var}:");
        set.files
            .iter()
            .filter(|f| !f.ignored)
            .flat_map(|f| f.raw.lines())
            .find(|l| l.trim_start().starts_with(&prefix))
            .map(|l| words(l.trim_start().trim_start_matches(&prefix)).into_iter().filter(|w| w != "-*").collect())
    };
    for (var, file_name) in USE_EXPAND_MOVE {
        let Some(a) = mc.last(var) else { continue };
        let vals = words(&a.expanded);
        let known = inp.usedesc.and_then(|db| db.expand.get(&var.to_lowercase()));
        let bad: Vec<&String> = vals.iter().filter(|v| known.is_some_and(|k| !k.contains_key(v.trim_start_matches('-')))).collect();
        let mut detail = format!(
            "{var} in make.conf replaces the profile's default instead of adding to it; the wiki recommends */* {var}: -* … in package.use, where the reset is explicit. Moving it keeps the result identical."
        );
        if !bad.is_empty() {
            detail.push_str(&format!(" Unknown value(s): {}.", bad.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(" ")));
        }
        let changes = inp.cfg.map(|cfg| {
            let line = format!("*/* {var}: -* {}", vals.join(" "));
            let mut c = vec![conf_line(cfg, ConfKind::Use, file_name, &format!("*/* {var}:"), &line, &format!("{var} (moved from make.conf by PortageHub)"))];
            c.extend(remove_fix(mc, var, &format!("{var} moved to package.use/{file_name}")));
            c
        });
        push(
            &mut out,
            Severity::Medium,
            var,
            format!("{var} is set in make.conf"),
            detail,
            if *var == "L10N" { links::L10N } else { links::MAKE_CONF },
            changes.map(|c| Fix { label: format!("Move to package.use/{file_name}"), changes: c }),
        );
    }
    if let Some(t) = inp.tuning
        && t.hw.gpus.contains(&"nvidia")
    {
        let vc = pkg_use_flags("VIDEO_CARDS").unwrap_or_else(|| words(inp.effective.get("VIDEO_CARDS").map(|s| s.as_str()).unwrap_or("")));
        if !vc.iter().any(|v| v == "nvidia") && mc.last("VIDEO_CARDS").is_none() {
            push(
                &mut out,
                Severity::Medium,
                "VIDEO_CARDS",
                "An NVIDIA GPU is installed but VIDEO_CARDS has no nvidia".into(),
                "Without VIDEO_CARDS=nvidia, packages like mesa and xorg build without the proprietary driver integration.".into(),
                links::VIDEO_CARDS,
                inp.cfg.map(|cfg| Fix {
                    label: "Set */* VIDEO_CARDS: -* nvidia in package.use/00video_cards".into(),
                    changes: vec![conf_line(cfg, ConfKind::Use, "00video_cards", "*/* VIDEO_CARDS:", "*/* VIDEO_CARDS: -* nvidia", "VIDEO_CARDS (PortageHub)")],
                }),
            );
        }
    }
    // CPU_FLAGS_X86: where it is set, and whether it matches cpuid2cpuflags.
    let cpu_mc = mc.last("CPU_FLAGS_X86").map(|a| words(&a.expanded));
    let cpu_pkg = pkg_use_flags("CPU_FLAGS_X86");
    let detected = inp.tuning.and_then(|t| t.cpu_flags.clone());
    let current = cpu_mc.clone().or(cpu_pkg.clone());
    let sorted = |v: &[String]| v.iter().cloned().collect::<BTreeSet<String>>();
    let cpu_fix = |flags: &[String], label: &str| -> Option<Fix> {
        let cfg = inp.cfg?;
        let mut c = vec![conf_line(
            cfg,
            ConfKind::Use,
            "00cpu-flags",
            "*/* CPU_FLAGS_X86:",
            &format!("*/* CPU_FLAGS_X86: {}", flags.join(" ")),
            "CPU_FLAGS_X86 from cpuid2cpuflags",
        )];
        if cpu_mc.is_some() {
            c.extend(remove_fix(mc, "CPU_FLAGS_X86", "CPU_FLAGS_X86 moved to package.use/00cpu-flags"));
        }
        Some(Fix { label: label.into(), changes: c })
    };
    match (&current, &detected) {
        (Some(cur), Some(det)) if sorted(cur) != sorted(det) => {
            let missing: Vec<&String> = det.iter().filter(|f| !cur.contains(f)).collect();
            let extra: Vec<&String> = cur.iter().filter(|f| !det.contains(f)).collect();
            push(
                &mut out,
                Severity::Medium,
                "CPU_FLAGS_X86",
                "CPU_FLAGS_X86 does not match this CPU".into(),
                format!(
                    "cpuid2cpuflags reports a different set.{}{} Flags the CPU lacks make programs crash with \"illegal instruction\"; missing ones leave fast code paths unused.",
                    if missing.is_empty() { String::new() } else { format!(" Missing: {}.", missing.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(" ")) },
                    if extra.is_empty() {
                        String::new()
                    } else {
                        format!(" Not supported by this CPU: {}.", extra.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(" "))
                    }
                ),
                links::CPU_FLAGS,
                cpu_fix(det, "Write the detected flags to package.use/00cpu-flags"),
            );
        }
        (Some(_), Some(det)) if cpu_mc.is_some() => push(
            &mut out,
            Severity::Info,
            "CPU_FLAGS_X86",
            "CPU_FLAGS_X86 is in make.conf".into(),
            "It matches this CPU. The Handbook keeps it in package.use/00cpu-flags (as cpuid2cpuflags prints it); moving it is tidier, not required.".into(),
            links::CPU_FLAGS,
            cpu_fix(det, "Move to package.use/00cpu-flags"),
        ),
        (None, Some(det)) => push(
            &mut out,
            Severity::Medium,
            "CPU_FLAGS_X86",
            "CPU_FLAGS_X86 is not set".into(),
            "Ebuilds use it to enable hand-written SIMD code (it is separate from -march). cpuid2cpuflags detected the flags of this CPU.".into(),
            links::CPU_FLAGS,
            cpu_fix(det, "Write package.use/00cpu-flags"),
        ),
        _ => {}
    }
    for var in PROFILE_MANAGED {
        if mc.last(var).is_some() {
            let link = if var.starts_with("LLVM") { links::LLVM } else { links::PYTHON };
            push(
                &mut out,
                Severity::Medium,
                var,
                format!("{var} is pinned in make.conf"),
                format!(
                    "The profile keeps {var} current and in step with the tree; a pin here replaces it and blocks the move to newer versions until it is edited again. After removing it, the next emerge -uDN @world rebuilds the packages whose targets change."
                ),
                link,
                fix("Remove the pin (use the profile default)", remove_fix(mc, var, &format!("{var} removed: the profile manages it"))),
            );
        }
    }
    if mc.last("LINGUAS").is_some() {
        push(
            &mut out,
            Severity::Info,
            "LINGUAS",
            "LINGUAS is set".into(),
            "LINGUAS makes packages skip locales and can leave KDE partly untranslated; L10N (BCP-47 tags like en-US) is the current way to pick languages."
                .into(),
            links::L10N,
            None,
        );
    }
    if let (Some(t), Some(vdb)) = (inp.tuning, inp.vdb)
        && t.hw.efi
        && vdb.is_installed("sys-boot/grub")
    {
        let gp = words(inp.effective.get("GRUB_PLATFORMS").map(|s| s.as_str()).unwrap_or(&mc.value("GRUB_PLATFORMS")));
        if !gp.iter().any(|p| p.starts_with("efi")) {
            let file = mc.main_file();
            let text = mc.text_of(&file).to_string();
            push(
                &mut out,
                Severity::Medium,
                "GRUB_PLATFORMS",
                "UEFI system, but GRUB is not built for EFI".into(),
                "This machine boots with UEFI; GRUB needs GRUB_PLATFORMS=\"efi-64\" to install an EFI bootloader.".into(),
                links::GRUB,
                fix(
                    "Set GRUB_PLATFORMS=\"efi-64\"",
                    vec![FileChange { path: file, old: text.clone(), new: Some(makeconf::set_var(&text, "GRUB_PLATFORMS", "efi-64")) }],
                ),
            );
        }
    }

    // --- Commands run as root, paths, priority, mirrors
    for name in mc.names().into_iter().filter(|n| is_command_var(n)) {
        let v = mc.value(&name);
        let first = v.split_whitespace().next().unwrap_or("").to_string();
        if !first.is_empty() && !first.starts_with('/') && !first.starts_with('$') {
            let abs = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"].iter().map(|d| Path::new(d).join(&first)).find(|p| p.is_file());
            let f = abs.map(|p| {
                let p = p.to_string_lossy().to_string();
                let done = std::cell::Cell::new(false);
                Fix {
                    label: format!("Use {p}"),
                    changes: edit_files(mc, &name, &|t| {
                        done.set(false);
                        makeconf::edit_words(t, &name, &|w| (w == first && !done.replace(true)).then(|| p.clone()))
                    }),
                }
            });
            push(
                &mut out,
                Severity::Info,
                &name,
                format!("{name} runs \"{first}\" by name"),
                format!(
                    "Portage runs {name} as root. The wiki's own examples use the bare name, which Portage resolves through its PATH; an absolute path pins exactly which program runs: {v}"
                ),
                links::MAN,
                f,
            );
        }
    }
    for var in ["PORTAGE_TMPDIR", "DISTDIR", "PKGDIR"] {
        let Some(v) = inp.effective.get(var).cloned().or_else(|| mc.last(var).map(|a| a.expanded.clone())) else { continue };
        if v.is_empty() {
            continue;
        }
        if !v.starts_with('/') {
            push(&mut out, Severity::High, var, format!("{var} is not an absolute path"), format!("{var}=\"{v}\""), links::MAN, None);
            continue;
        }
        let dir = if var == "PORTAGE_TMPDIR" { Path::new(&v).join("portage") } else { PathBuf::from(&v) };
        let dir = if dir.exists() { dir } else { PathBuf::from(&v) };
        if let Ok(md) = std::fs::metadata(&dir) {
            use std::os::unix::fs::PermissionsExt;
            let mode = md.permissions().mode();
            if mode & 0o002 != 0 && mode & 0o1000 == 0 {
                push(
                    &mut out,
                    Severity::High,
                    var,
                    format!("{} is world-writable", dir.display()),
                    "The man page warns that anyone who can write here can subvert builds that run as root.".into(),
                    links::MAN,
                    None,
                );
            }
        }
        if var == "PORTAGE_TMPDIR"
            && let Ok(c) = std::ffi::CString::new(v.as_bytes())
        {
            let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
            if unsafe { libc::statvfs(c.as_ptr(), &mut st) } == 0 && st.f_flag & libc::ST_NOEXEC != 0 {
                push(
                    &mut out,
                    Severity::High,
                    var,
                    format!("{v} is mounted noexec"),
                    "Builds run configure scripts and test programs from PORTAGE_TMPDIR; with noexec they fail.".into(),
                    links::TMPFS,
                    None,
                );
            }
        }
    }
    if mc.last("PORTAGE_SCHEDULING_POLICY").is_none() && mc.last("PORTAGE_NICENESS").is_none() {
        let file = mc.main_file();
        let text = mc.text_of(&file).to_string();
        push(
            &mut out,
            Severity::Info,
            "PORTAGE_SCHEDULING_POLICY",
            "Builds run at normal CPU priority".into(),
            "PORTAGE_SCHEDULING_POLICY=\"idle\" lets builds use only otherwise idle CPU time, so the desktop stays responsive during long emerges (Portage 3.0.35+).".into(),
            links::NICENESS,
            fix("Set PORTAGE_SCHEDULING_POLICY=\"idle\"", vec![FileChange { path: file, old: text.clone(), new: Some(makeconf::set_var(&text, "PORTAGE_SCHEDULING_POLICY", "idle")) }]),
        );
    }
    let mirrors = words(&mc.value("GENTOO_MIRRORS"));
    if !mirrors.is_empty() && mirrors.iter().all(|m| m.starts_with("http://") || m.starts_with("ftp://")) {
        push(&mut out, Severity::Info, "GENTOO_MIRRORS", "Mirrors use plain http/ftp".into(), "Downloads are still verified against Manifest digests, but https mirrors also keep what you download private. mirrorselect -S -o lists https mirrors.".into(), links::MIRRORS, None);
    }

    out.sort_by_key(|f| f.severity);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::portage::makeconf::Assignment;

    fn mc_from(text: &str) -> MakeConf {
        let path = PathBuf::from(makeconf::MAKE_CONF);
        let mut vars: HashMap<String, String> = HashMap::new();
        let mut mc = MakeConf { files: vec![(path.clone(), text.to_string())], assignments: Vec::new() };
        for (name, value, _) in makeconf::scan(text) {
            let mut expanded = value.replace("\\\n", " ");
            for (k, v) in &vars {
                expanded = expanded.replace(&format!("${{{k}}}"), v);
            }
            let expanded = expanded.split_whitespace().collect::<Vec<_>>().join(" ");
            let self_ref = value.contains(&format!("${{{name}}}"));
            vars.insert(name.clone(), expanded.clone());
            mc.assignments.push(Assignment { name, value, expanded, file: path.clone(), self_ref });
        }
        mc
    }

    fn run(text: &str, tuning: Option<&Tuning>, effective: &HashMap<String, String>) -> Vec<Finding> {
        let mc = mc_from(text);
        check(&Input { mc: &mc, cfg: None, arch: "amd64", effective, tuning, usedesc: None, vdb: None, repo: None, parse: None })
    }

    fn find<'a>(f: &'a [Finding], var: &str, s: Severity) -> &'a Finding {
        f.iter().find(|x| x.var == var && x.severity == s).unwrap_or_else(|| panic!("no {s:?} finding for {var}: {f:#?}"))
    }

    fn applied(f: &Finding) -> String {
        f.fix.as_ref().expect("fix").changes[0].new.clone().unwrap()
    }

    fn machine() -> Tuning {
        Tuning {
            hw: tuning::Hardware { threads: 32, mem_kib: 48_993_456, cpu_model: "i9".into(), gpus: vec!["nvidia"], efi: false },
            gcc_version: "16".into(),
            native: Some(tuning::MarchInfo { march: "alderlake".into(), mtune: "alderlake".into(), avx512: false }),
            cpu_flags: Some(vec!["aes".into(), "avx".into(), "avx2".into()]),
        }
    }

    #[test]
    fn compiler_flag_findings_and_fixes() {
        let t = "COMMON_FLAGS=\"-march=alderlake -O3 -pipe \\\n    -flto ${GRAPHITE}\"\nGRAPHITE=\"-floop-interchange -floop-unroll-and-jam\"\nCFLAGS=\"${COMMON_FLAGS}\"\nCHOST=\"x86_64-pc-linux-gnu\"\n";
        // The helper variable must be defined before it is used.
        let t = t.replace("COMMON_FLAGS=\"-march", "GRAPHITE=\"-floop-interchange\"\nCOMMON_FLAGS=\"-march");
        let f = run(&t, Some(&machine()), &HashMap::new());
        assert!(applied(find(&f, "CHOST", Severity::Critical)).contains("# CHOST removed"));
        let o3 = applied(find(&f, "COMMON_FLAGS", Severity::Medium));
        assert!(o3.contains("-march=alderlake -O2 -pipe \\\n    -flto ${GRAPHITE}"), "{o3}");
        let g = f.iter().find(|x| x.title.contains("Graphite")).unwrap();
        let gt = applied(g);
        assert!(!gt.contains("${GRAPHITE}") && !gt.contains("floop"), "{gt}");
        assert_eq!(f[0].severity, Severity::Critical, "sorted by severity");
    }

    #[test]
    fn ldflags_rustflags_makeopts() {
        let t = "LDFLAGS=\"${COMMON_FLAGS} -Wl,-O1 -Wl,--as-needed -Wl,--hash-style=gnu\"\nRUSTFLAGS=\"-C target-cpu=x86-64-v3 -C strip=symbols -C opt-level=2\"\nMAKEOPTS=\"-j32\"\nEMERGE_DEFAULT_OPTS=\"--jobs=3 --keep-going\"\n";
        let f = run(t, Some(&machine()), &HashMap::new());
        assert!(applied(find(&f, "LDFLAGS", Severity::High)).contains("LDFLAGS=\"${LDFLAGS} ${COMMON_FLAGS} -Wl,--hash-style=gnu\""));
        assert!(applied(find(&f, "RUSTFLAGS", Severity::Medium)).contains("RUSTFLAGS=\"-C target-cpu=x86-64-v3\""));
        let mk = find(&f, "MAKEOPTS", Severity::High);
        assert!(mk.detail.contains("min(32, 46.7 ÷ 2 = 23) = 23"), "{}", mk.detail);
        assert!(applied(mk).contains("MAKEOPTS=\"-j23 -l32\""));
        assert!(applied(find(&f, "EMERGE_DEFAULT_OPTS", Severity::Medium)).contains("--jobs=3 --keep-going --load-average=28"));
        // Separated form: no stray -j left behind.
        let f = run("MAKEOPTS=\"-j 32 --output-sync\"\n", Some(&machine()), &HashMap::new());
        assert!(
            applied(find(&f, "MAKEOPTS", Severity::High)).contains("MAKEOPTS=\"-j23 -l32 --output-sync\""),
            "{}",
            applied(find(&f, "MAKEOPTS", Severity::High))
        );
    }

    #[test]
    fn features_keywords_use_and_expand() {
        let t = "FEATURES=\"-sandbox parallel-fetch ccache -preserve-libs\"\nACCEPT_KEYWORDS=\"amd64\"\nUSE=\"acl X acl -gtk\"\nVIDEO_CARDS=\"nvidia\"\nPYTHON_TARGETS=\"python3_13 python3_14\"\nPORT_LOGDIR=\"/var/log/portage\"\nCPU_FLAGS_X86=\"aes avx\"\n";
        let f = run(t, Some(&machine()), &HashMap::new());
        let crit = find(&f, "FEATURES", Severity::Critical);
        assert!(crit.title.contains("sandbox"));
        assert!(applied(crit).contains("FEATURES=\"parallel-fetch ccache -preserve-libs\""), "{}", applied(crit));
        assert!(applied(find(&f, "FEATURES", Severity::Medium)).contains("FEATURES=\"-sandbox parallel-fetch -preserve-libs\""));
        assert!(applied(find(&f, "ACCEPT_KEYWORDS", Severity::Info)).contains("# ACCEPT_KEYWORDS removed"));
        assert_eq!(applied(find(&f, "USE", Severity::Info)).lines().find(|l| l.starts_with("USE=")).unwrap(), "USE=\"acl X -gtk\"");
        assert!(applied(find(&f, "PYTHON_TARGETS", Severity::Medium)).contains("# PYTHON_TARGETS removed"));
        assert!(applied(find(&f, "PORT_LOGDIR", Severity::Medium)).contains("PORTAGE_LOGDIR=\"/var/log/portage\""));
        let cpu = find(&f, "CPU_FLAGS_X86", Severity::Medium);
        assert!(cpu.detail.contains("Missing: avx2"), "{}", cpu.detail);
        // Without a PortageConfig no package.use fix can be built.
        assert!(cpu.fix.is_none() && find(&f, "VIDEO_CARDS", Severity::Medium).fix.is_none());
        assert!(f.iter().any(|x| x.var == "PORTAGE_SCHEDULING_POLICY"));
    }

    #[test]
    fn licenses() {
        let mut groups = HashMap::new();
        groups.insert("FREE".to_string(), HashSet::from(["GPL-2".to_string(), "MIT".to_string()]));
        groups.insert("BINARY-REDISTRIBUTABLE".to_string(), HashSet::from(["GPL-2".to_string(), "linux-fw-redistributable".to_string()]));
        let acc: Vec<String> = ["-*", "@FREE", "@BINARY-REDISTRIBUTABLE"].iter().map(|s| s.to_string()).collect();
        assert!(license_accepted("MIT", &acc, &groups));
        assert!(license_accepted("linux-fw-redistributable", &acc, &groups));
        assert!(!license_accepted("NVIDIA-2025", &acc, &groups));
        assert!(license_accepted("NVIDIA-2025", &["*".to_string()], &groups));
        assert!(!license_accepted("MIT", &["*".to_string(), "-@FREE".to_string()], &groups));
        assert_eq!(license_names("|| ( MIT Apache-2.0 ) doc? ( CC-BY-4.0 )"), vec!["MIT", "Apache-2.0", "CC-BY-4.0"]);
        assert_eq!(opt_num(&words("--jobs 3 --load-average=24.5"), "-j", "--jobs"), Some(3.0));
        assert_eq!(opt_num(&words("--jobs 3 --load-average=24.5"), "", "--load-average"), Some(24.5));
        assert_eq!(opt_num(&words("-j16 -l30"), "-l", "--load-average"), Some(30.0));
    }
}
