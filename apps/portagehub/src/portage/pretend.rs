//! Parser for `emerge --pretend --verbose` output: the merge list plus every
//! kind of problem report (autounmask suggestions, masks, blockers, slot
//! conflicts, REQUIRED_USE, circular deps, ambiguous names …).

use super::atom::{Atom, split_cpv};
use super::config::ConfKind;
use regex::Regex;
use std::sync::OnceLock;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EntryType {
    Ebuild,
    Binary,
    Blocks,
    Uninstall,
    NoMerge,
    Installed,
}

#[derive(Debug, Clone)]
pub struct PlanEntry {
    pub kind: EntryType,
    pub flags: String,
    pub cpv: String,
    pub key: String,
    pub version: String,
    pub slot: String,
    pub repo: String,
    pub old: Vec<String>,
    pub use_vars: Vec<(String, String)>,
    pub size: String,
    pub depth: usize,
}

impl PlanEntry {
    pub fn has(&self, c: char) -> bool {
        self.flags.contains(c)
    }

    pub fn status_label(&self) -> &'static str {
        if self.kind == EntryType::Blocks {
            return if self.has('B') { "BLOCKED" } else { "BLOCKER" };
        }
        if self.kind == EntryType::Uninstall {
            return "REMOVE";
        }
        if self.kind == EntryType::NoMerge {
            return "IN TREE";
        }
        if self.kind == EntryType::Installed {
            return "INSTALLED";
        }
        if self.has('N') && self.has('S') {
            "NEW SLOT"
        } else if self.has('N') {
            "NEW"
        } else if self.has('D') {
            "DOWNGRADE"
        } else if self.has('U') {
            "UPDATE"
        } else if self.has('R') || self.has('r') {
            "REBUILD"
        } else {
            "MERGE"
        }
    }

    /// Flags enabled as shown by emerge in USE="..." (with * and % markers removed).
    pub fn use_changes(&self) -> Vec<(String, bool, bool)> {
        let mut out = Vec::new();
        for (_, val) in &self.use_vars {
            for tok in val.split_whitespace() {
                let tok = tok.trim_matches(|c| c == '(' || c == ')');
                let changed = tok.ends_with('*') || tok.ends_with('%');
                let tok = tok.trim_end_matches(['*', '%']);
                let (name, on) = match tok.strip_prefix('-') {
                    Some(n) => (n, false),
                    None => (tok, true),
                };
                out.push((name.to_string(), on, changed));
            }
        }
        out
    }
}

#[derive(Debug, Clone)]
pub struct Suggestion {
    pub kind: ConfKind,
    /// The configuration line emerge suggests (atom + values).
    pub line: String,
    pub required_by: Vec<String>,
    pub note: Vec<String>,
    pub selected: bool,
}

#[derive(Debug, Clone)]
pub struct MaskedCandidate {
    pub cpv: String,
    pub repo: String,
    pub reasons: String,
}

impl MaskedCandidate {
    /// Configuration lines that would make this version installable.
    pub fn fixes(&self, arch: &str) -> Vec<(ConfKind, String)> {
        let atom = format!("={}", self.cpv);
        let mut out = Vec::new();
        let r = self.reasons.to_lowercase();
        if r.contains("missing keyword") || r.contains("-* keyword") || r.contains(&format!("-{arch} keyword")) {
            out.push((ConfKind::AcceptKeywords, format!("{atom} **")));
        } else if r.contains("keyword") {
            out.push((ConfKind::AcceptKeywords, format!("{atom} ~{arch}")));
        }
        if r.contains("package.mask") {
            out.push((ConfKind::Unmask, atom.clone()));
        }
        if let Some(i) = self.reasons.find(" license") {
            let lic = self.reasons[..i].rsplit([',', ':']).next().unwrap_or("").trim();
            if !lic.is_empty() {
                out.push((ConfKind::License, format!("{atom} {lic}")));
            }
        }
        out
    }
}

#[derive(Debug, Clone)]
pub struct SlotConflict {
    pub slot_key: String,
    pub candidates: Vec<(String, String)>,
    pub pulled_by: Vec<String>,
    pub raw: Vec<String>,
}

#[derive(Debug, Clone)]
pub enum Problem {
    Masked { atom: String, candidates: Vec<MaskedCandidate>, comments: Vec<String> },
    NoEbuilds { atom: String },
    Ambiguous { name: String, options: Vec<String> },
    SlotConflict(SlotConflict),
    Blocker { blocker: String, blocked: String, hard: bool, resolved: bool },
    RequiredUse { pkg: String, constraint: Vec<String>, use_line: String },
    Circular { lines: Vec<String>, use_fixes: Vec<(String, String)> },
    Unsatisfied { lines: Vec<String> },
    /// "One or more updates/rebuilds have been skipped due to a dependency conflict"
    Skipped { slot_key: String, candidate: String, required: Vec<(String, String)> },
    PreservedLibs,
    News { count: usize },
    ConfigUpdates { count: usize },
    Generic { title: String, lines: Vec<String> },
}

impl Problem {
    pub fn title(&self) -> String {
        match self {
            Problem::Masked { atom, .. } => format!("All versions of {atom} are masked"),
            Problem::NoEbuilds { atom } => format!("No package matches “{atom}”"),
            Problem::Ambiguous { name, .. } => format!("“{name}” exists in several categories"),
            Problem::SlotConflict(c) => format!("Slot conflict in {}", c.slot_key),
            Problem::Blocker { blocker, blocked, .. } => format!("{blocked} is blocked by {blocker}"),
            Problem::RequiredUse { pkg, .. } => format!("USE flag requirements of {pkg} are not met"),
            Problem::Circular { .. } => "Circular dependencies".to_string(),
            Problem::Unsatisfied { .. } => "Dependencies cannot be satisfied".to_string(),
            Problem::Skipped { candidate, .. } => format!("Update to {candidate} was skipped"),
            Problem::PreservedLibs => "Preserved libraries need rebuilding".to_string(),
            Problem::News { count } => format!("{count} unread news item(s)"),
            Problem::ConfigUpdates { count } => format!("{count} configuration file(s) need updating"),
            Problem::Generic { title, .. } => title.clone(),
        }
    }

    pub fn is_blocking(&self) -> bool {
        !matches!(self, Problem::PreservedLibs | Problem::News { .. } | Problem::ConfigUpdates { .. } | Problem::Skipped { .. })
            && !matches!(self, Problem::Blocker { resolved: true, .. })
    }
}

#[derive(Debug, Clone, Default)]
pub struct Plan {
    pub entries: Vec<PlanEntry>,
    pub suggestions: Vec<Suggestion>,
    pub problems: Vec<Problem>,
    pub total_line: String,
    pub download_size: String,
    pub depclean: Vec<String>,
    pub messages: Vec<String>,
    pub exit_code: Option<i32>,
    /// `--tree` output lists packages in reverse merge order.
    pub reverse: bool,
}

impl Plan {
    pub fn merge_entries(&self) -> impl Iterator<Item = &PlanEntry> {
        self.entries.iter().filter(|e| matches!(e.kind, EntryType::Ebuild | EntryType::Binary))
    }
}

/// A skipped update while it is being read: (slot key, the candidate that was skipped, what requires
/// the package it conflicts with as (atom, required by)).
type SkipGroup = (String, String, Vec<(String, String)>);

fn re(cell: &'static OnceLock<Regex>, pat: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pat).unwrap())
}

pub fn parse_entry(line: &str) -> Option<PlanEntry> {
    static E: OnceLock<Regex> = OnceLock::new();
    let r = re(&E, r"^(\s*)\[(ebuild|binary|blocks|uninstall|nomerge|installed)\s?([^\]]*)\]\s+(\S+)(.*)$");
    let c = r.captures(line)?;
    let kind = match &c[2] {
        "ebuild" => EntryType::Ebuild,
        "binary" => EntryType::Binary,
        "blocks" => EntryType::Blocks,
        "uninstall" => EntryType::Uninstall,
        "nomerge" => EntryType::NoMerge,
        _ => EntryType::Installed,
    };
    let depth = c[1].len();
    let flags: String = c[3].chars().filter(|c| !c.is_whitespace()).collect();
    let target = c[4].to_string();
    let rest = c[5].to_string();
    let (mut cpv, mut slot, mut repo) = (target.clone(), String::new(), String::new());
    if let Some((a, b)) = cpv.clone().split_once("::") {
        cpv = a.to_string();
        repo = b.to_string();
    }
    if kind != EntryType::Blocks
        && let Some((a, b)) = cpv.clone().split_once(':')
    {
        cpv = a.to_string();
        slot = b.to_string();
    }
    let (key, version) = match split_cpv(&cpv) {
        Some((c, p, v)) => (format!("{c}/{p}"), v.raw),
        None => (Atom::parse(&cpv).map(|a| a.key()).unwrap_or(cpv.clone()), String::new()),
    };
    static OLD: OnceLock<Regex> = OnceLock::new();
    let old_re = re(&OLD, r"^\s*\[([^\]]+)\]");
    let mut old = Vec::new();
    let mut rest_s = rest.as_str();
    if let Some(o) = old_re.captures(rest_s) {
        old = o[1].split(", ").map(|s| s.trim().to_string()).collect();
        rest_s = &rest_s[o.get(0).unwrap().end()..];
    }
    static VARS: OnceLock<Regex> = OnceLock::new();
    let vars_re = re(&VARS, r#"([A-Z0-9_]+)="([^"]*)""#);
    let use_vars = vars_re.captures_iter(rest_s).map(|v| (v[1].to_string(), v[2].to_string())).collect();
    static SIZE: OnceLock<Regex> = OnceLock::new();
    let size_re = re(&SIZE, r"([\d,.]+ [KMG]i?B)\s*$");
    let size = size_re.captures(rest_s.trim_end()).map(|s| s[1].to_string()).unwrap_or_default();
    Some(PlanEntry { kind, flags, cpv, key, version, slot, repo, old, use_vars, size, depth })
}

fn suggestion_kind(header: &str) -> Option<ConfKind> {
    let h = header.trim();
    if !h.starts_with("The following") || !h.ends_with("necessary to proceed:") {
        return None;
    }
    Some(if h.contains("USE changes") {
        ConfKind::Use
    } else if h.contains("keyword changes") {
        ConfKind::AcceptKeywords
    } else if h.contains("license changes") {
        ConfKind::License
    } else if h.contains("mask changes") {
        ConfKind::Unmask
    } else {
        return None;
    })
}

fn looks_like_conf_line(t: &str) -> bool {
    t.split_whitespace().next().is_some_and(|f| f.contains('/') && Atom::parse(f).is_some())
}

pub fn parse(output: &str, exit_code: Option<i32>) -> Plan {
    let mut plan = Plan { exit_code, ..Default::default() };
    let lines: Vec<&str> = output.lines().collect();
    let mut i = 0;
    static MASKED_HDR: OnceLock<Regex> = OnceLock::new();
    let masked_hdr = re(&MASKED_HDR, r#"^!!! All ebuilds that could satisfy "(.+)" have been masked\."#);
    static MASKED_LINE: OnceLock<Regex> = OnceLock::new();
    let masked_line = re(&MASKED_LINE, r"^- (\S+?)(?:::(\S+))? \(masked by: (.+)\)\s*$");
    static NOEB: OnceLock<Regex> = OnceLock::new();
    let noeb = re(&NOEB, r#"^emerge: there are no (?:ebuilds|binary packages|ebuilds built with USE flags) to satisfy "(.+?)""#);
    static AMB: OnceLock<Regex> = OnceLock::new();
    let amb = re(&AMB, r#"^!!! The short ebuild name "(.+)" is ambiguous"#);
    static BLK: OnceLock<Regex> = OnceLock::new();
    let blk = re(&BLK, r#"\("(.+?)" is (soft |hard )?blocking (.+?)\)"#);
    static SLOTKEY: OnceLock<Regex> = OnceLock::new();
    let slotkey = re(&SLOTKEY, r"^(\S+/\S+:\S+)(?:::\S+)?$");
    static CAND: OnceLock<Regex> = OnceLock::new();
    let cand = re(&CAND, r"^\s+\((\S+?), ([^)]+)\).*pulled in by");
    static REQBY: OnceLock<Regex> = OnceLock::new();
    let reqby = re(&REQBY, r"^\s+(\S+) required by \((\S+?),");
    static CHUSE: OnceLock<Regex> = OnceLock::new();
    let chuse = re(&CHUSE, r"^- (\S+) \(Change USE: (.+)\)");
    static NEWS: OnceLock<Regex> = OnceLock::new();
    let news = re(&NEWS, r"IMPORTANT: (\d+) news items? need");
    static CFG: OnceLock<Regex> = OnceLock::new();
    let cfg = re(&CFG, r"IMPORTANT: config file '.+' needs updating");

    let mut cfg_updates = 0;
    while i < lines.len() {
        let line = lines[i];
        let t = line.trim();

        if let Some(e) = parse_entry(line) {
            if e.kind == EntryType::Blocks
                && let Some(b) = blk.captures(line)
            {
                plan.problems.push(Problem::Blocker {
                    blocker: b[1].to_string(),
                    blocked: b[3].to_string(),
                    hard: b.get(2).is_some_and(|m| m.as_str().starts_with("hard")),
                    resolved: !e.has('B'),
                });
            }
            plan.entries.push(e);
            i += 1;
            continue;
        }
        if t.starts_with("These are the packages that would be") {
            plan.reverse = t.contains("reverse order");
            i += 1;
            continue;
        }
        if t.starts_with("Total: ") {
            plan.total_line = t.to_string();
            if let Some(p) = t.find("Size of downloads: ") {
                plan.download_size = t[p + 19..].to_string();
            }
            i += 1;
            continue;
        }
        if let Some(kind) = suggestion_kind(t) {
            i += 1;
            let mut required = Vec::new();
            let mut notes = Vec::new();
            while i < lines.len() {
                let l = lines[i].trim();
                if l.is_empty() {
                    // A blank line ends the block once we've seen entries.
                    if plan.suggestions.last().is_some_and(|s| s.kind == kind) && !lines.get(i + 1).is_some_and(|n| n.trim().starts_with('#') || looks_like_conf_line(n.trim())) {
                        break;
                    }
                    i += 1;
                    continue;
                }
                if l.starts_with("(see ") {
                    i += 1;
                    continue;
                }
                if let Some(c) = l.strip_prefix('#') {
                    let c = c.trim();
                    if let Some(r) = c.strip_prefix("required by ") {
                        required.push(r.to_string());
                    } else {
                        notes.push(c.to_string());
                    }
                    i += 1;
                    continue;
                }
                if looks_like_conf_line(l) {
                    plan.suggestions.push(Suggestion {
                        kind,
                        line: l.to_string(),
                        required_by: std::mem::take(&mut required),
                        note: std::mem::take(&mut notes),
                        selected: true,
                    });
                    i += 1;
                    continue;
                }
                break;
            }
            continue;
        }
        if let Some(c) = masked_hdr.captures(t) {
            let atom = c[1].to_string();
            let mut candidates = Vec::new();
            let mut comments = Vec::new();
            i += 1;
            while i < lines.len() {
                let l = lines[i].trim();
                if let Some(m) = masked_line.captures(l) {
                    candidates.push(MaskedCandidate {
                        cpv: m[1].to_string(),
                        repo: m.get(2).map(|r| r.as_str().to_string()).unwrap_or_default(),
                        reasons: m[3].to_string(),
                    });
                } else if l.starts_with("For more information, see the MASKED") {
                    i += 1;
                    break;
                } else if !l.is_empty() && !l.starts_with("!!! One of the following") {
                    comments.push(l.to_string());
                }
                i += 1;
            }
            plan.problems.push(Problem::Masked { atom, candidates, comments });
            continue;
        }
        if let Some(c) = noeb.captures(t) {
            plan.problems.push(Problem::NoEbuilds { atom: c[1].to_string() });
            i += 1;
            continue;
        }
        if let Some(c) = amb.captures(t) {
            let name = c[1].to_string();
            let mut options = Vec::new();
            i += 1;
            while i < lines.len() {
                let l = lines[i].trim();
                if l.is_empty() && !options.is_empty() {
                    break;
                }
                if l.contains('/') && !l.starts_with('!') && Atom::parse(l.split_whitespace().next().unwrap_or("")).is_some() {
                    options.push(l.split_whitespace().next().unwrap().to_string());
                } else if l.starts_with("!!!") && !options.is_empty() {
                    break;
                }
                i += 1;
            }
            plan.problems.push(Problem::Ambiguous { name, options });
            continue;
        }
        if t.starts_with("WARNING: One or more updates/rebuilds have been skipped due to a dependency conflict") {
            static SKCAND: OnceLock<Regex> = OnceLock::new();
            let skcand = re(&SKCAND, r"^\s+\((\S+?)(?::\S+)?(?:::\S+)?, [^)]+\).*conflicts with");
            static SKREQ: OnceLock<Regex> = OnceLock::new();
            let skreq = re(&SKREQ, r"^\s+(\S+) required by (.+?)\s*$");
            i += 1;
            let mut current: Option<SkipGroup> = None;
            while i < lines.len() {
                let l = lines[i];
                let lt = l.trim();
                if lt.is_empty() || lt.chars().all(|c| c == '^' || c == ' ') {
                    i += 1;
                    continue;
                }
                if !l.starts_with(' ') {
                    if slotkey.is_match(lt) {
                        if let Some((k, c, r)) = current.take() {
                            plan.problems.push(Problem::Skipped { slot_key: k, candidate: c, required: r });
                        }
                        current = Some((lt.to_string(), String::new(), Vec::new()));
                        i += 1;
                        continue;
                    }
                    break;
                }
                if let Some(cur) = current.as_mut() {
                    if let Some(m) = skcand.captures(l) {
                        cur.1 = m[1].to_string();
                    } else if let Some(m) = skreq.captures(l) {
                        cur.2.push((m[1].to_string(), m[2].to_string()));
                    }
                }
                i += 1;
            }
            if let Some((k, c, r)) = current.take() {
                plan.problems.push(Problem::Skipped { slot_key: k, candidate: c, required: r });
            }
            continue;
        }
        if t.starts_with("!!! Multiple package instances within a single package slot") {
            i += 1;
            let mut current: Option<SlotConflict> = None;
            while i < lines.len() {
                let l = lines[i];
                let lt = l.trim();
                if lt.starts_with("It may be possible to solve") || lt.starts_with("NOTE: Use the '--verbose-conflicts'") || lt.starts_with("!!! Enabling --newuse") {
                    break;
                }
                if lt.starts_with("!!!") && current.is_none() {
                    i += 1;
                    continue;
                }
                if let Some(k) = slotkey.captures(lt)
                    && !l.starts_with(' ')
                {
                    if let Some(c) = current.take() {
                        plan.problems.push(Problem::SlotConflict(c));
                    }
                    current = Some(SlotConflict { slot_key: k[1].to_string(), candidates: Vec::new(), pulled_by: Vec::new(), raw: vec![lt.to_string()] });
                } else if let Some(c) = current.as_mut() {
                    if let Some(m) = cand.captures(l) {
                        c.candidates.push((m[1].to_string(), m[2].to_string()));
                    } else if let Some(m) = reqby.captures(l) {
                        let s = format!("{} (needs {})", &m[2], &m[1]);
                        if !c.pulled_by.contains(&s) {
                            c.pulled_by.push(s);
                        }
                    }
                    if !lt.is_empty() {
                        c.raw.push(l.to_string());
                    }
                }
                i += 1;
            }
            if let Some(c) = current.take() {
                plan.problems.push(Problem::SlotConflict(c));
            }
            continue;
        }
        if t.starts_with("!!! The ebuild selected to satisfy") && t.contains("has unmet requirements") {
            let mut pkg = String::new();
            let mut use_line = String::new();
            let mut constraint = Vec::new();
            i += 1;
            while i < lines.len() {
                let l = lines[i].trim();
                if let Some(r) = l.strip_prefix("- ") {
                    let mut parts = r.splitn(2, ' ');
                    pkg = parts.next().unwrap_or("").to_string();
                    use_line = parts.next().unwrap_or("").to_string();
                } else if l.starts_with("The following REQUIRED_USE flag constraints are unsatisfied") || l.starts_with("The above constraints are a subset") {
                    // Headings: nothing to record.
                } else if (l.is_empty() && !constraint.is_empty()) || l.starts_with("(dependency required by") || l.starts_with("!!!") {
                    break;
                } else if !l.is_empty() && !pkg.is_empty() {
                    constraint.push(l.to_string());
                }
                i += 1;
            }
            plan.problems.push(Problem::RequiredUse { pkg, constraint, use_line });
            continue;
        }
        if t.contains("Error: circular dependencies:") {
            let mut ls = Vec::new();
            let mut fixes = Vec::new();
            i += 1;
            while i < lines.len() {
                let l = lines[i];
                if let Some(c) = chuse.captures(l.trim()) {
                    fixes.push((c[1].to_string(), c[2].to_string()));
                }
                if l.trim().starts_with("Note that this change can be reverted") {
                    i += 1;
                    break;
                }
                if !l.trim().is_empty() {
                    ls.push(l.to_string());
                }
                i += 1;
                if ls.len() > 80 {
                    break;
                }
            }
            plan.problems.push(Problem::Circular { lines: ls, use_fixes: fixes });
            continue;
        }
        if t.starts_with("emerge: there are no ebuilds to satisfy") || t.starts_with("!!! Problem resolving dependencies") {
            plan.problems.push(Problem::Unsatisfied { lines: vec![t.to_string()] });
            i += 1;
            continue;
        }
        if t.contains("existing preserved libs") || t.contains("emerge @preserved-rebuild") {
            if !plan.problems.iter().any(|p| matches!(p, Problem::PreservedLibs)) {
                plan.problems.push(Problem::PreservedLibs);
            }
            i += 1;
            continue;
        }
        if let Some(c) = news.captures(t) {
            let n: usize = c[1].parse().unwrap_or(1);
            match plan.problems.iter_mut().find(|p| matches!(p, Problem::News { .. })) {
                Some(Problem::News { count }) => *count += n,
                _ => plan.problems.push(Problem::News { count: n }),
            }
            i += 1;
            continue;
        }
        if cfg.is_match(t) {
            cfg_updates += 1;
            i += 1;
            continue;
        }
        // depclean / unmerge preview
        if t.starts_with(">>> These are the packages that would be unmerged:") {
            i += 1;
            while i < lines.len() {
                let l = lines[i].trim();
                // Only the summary line; the per-package "selected:" lines repeat it.
                if let Some(list) = l.strip_prefix("All selected packages:") {
                    plan.depclean.extend(list.split_whitespace().map(|a| a.trim_start_matches('=').to_string()));
                }
                if l.starts_with("Number to remove:") || l.starts_with(">>> No packages selected for removal") {
                    break;
                }
                i += 1;
            }
            continue;
        }
        if t.starts_with("!!!") || t.starts_with("* Error:") || t.starts_with("* ERROR:") {
            plan.messages.push(t.to_string());
        }
        i += 1;
    }
    if cfg_updates > 0 {
        plan.problems.push(Problem::ConfigUpdates { count: cfg_updates });
    }
    // Unsatisfied problems that emerge reported but we didn't classify.
    if exit_code.is_some_and(|c| c != 0) && plan.problems.is_empty() && plan.suggestions.is_empty() && !plan.messages.is_empty() {
        plan.problems.push(Problem::Generic { title: "emerge reported errors".into(), lines: plan.messages.clone() });
    }
    plan
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"
These are the packages that would be merged, in order:

Calculating dependencies  ... done!
Dependency resolution took 3.21 s (backtrack: 0/20).

[ebuild     U  ] dev-lang/rust-1.82.0:stable/1.82::gentoo [1.81.0:stable/1.81::gentoo] USE="clippy -doc% rustfmt*" LLVM_TARGETS="(X86) -AArch64" 280,312 KiB
[ebuild  N     ] app-misc/foo-1.0::guru  USE="-bar" 12 KiB
[binary   R    ] sys-libs/zlib-1.3.1-r1:0/1::gentoo  USE="minizip -static-libs" 0 KiB
[ebuild  NS    ] sys-devel/gcc-14.2.1_p20241221:14::gentoo [13.3.1_p20241220:13::gentoo, 14.2.1_p20241116:14::gentoo] USE="cxx" 0 KiB
[blocks b      ] sys-apps/old ("sys-apps/old" is soft blocking sys-apps/new-2.0)

Total: 4 packages (1 upgrade, 1 new, 1 in new slot, 1 reinstall, 1 binary), Size of downloads: 280,324 KiB

The following keyword changes are necessary to proceed:
 (see "package.accept_keywords" in the portage(5) man page for more details)
# required by app-misc/foo-1.0::guru
# required by app-misc/foo (argument)
=dev-libs/bar-2.0 ~amd64

The following USE changes are necessary to proceed:
 (see "package.use" in the portage(5) man page for more details)
# required by app-misc/foo-1.0::guru
>=media-libs/baz-1.2 qt6 -qt5

 * IMPORTANT: 2 news items need reading for repository 'gentoo'.
"#;

    #[test]
    fn plan_entries() {
        let p = parse(SAMPLE, Some(1));
        assert_eq!(p.entries.len(), 5);
        let rust = &p.entries[0];
        assert_eq!(rust.key, "dev-lang/rust");
        assert_eq!(rust.version, "1.82.0");
        assert_eq!(rust.slot, "stable/1.82");
        assert_eq!(rust.repo, "gentoo");
        assert_eq!(rust.old, vec!["1.81.0:stable/1.81::gentoo"]);
        assert_eq!(rust.size, "280,312 KiB");
        assert_eq!(rust.status_label(), "UPDATE");
        assert!(rust.use_changes().iter().any(|(f, on, ch)| f == "rustfmt" && *on && *ch));
        assert_eq!(p.entries[1].status_label(), "NEW");
        assert_eq!(p.entries[2].kind, EntryType::Binary);
        assert_eq!(p.entries[2].status_label(), "REBUILD");
        assert_eq!(p.entries[3].status_label(), "NEW SLOT");
        assert_eq!(p.entries[3].old.len(), 2);
        assert!(p.total_line.starts_with("Total: 4 packages"));
        assert_eq!(p.download_size, "280,324 KiB");
        assert_eq!(p.suggestions.len(), 2);
        assert_eq!(p.suggestions[0].kind, ConfKind::AcceptKeywords);
        assert_eq!(p.suggestions[0].line, "=dev-libs/bar-2.0 ~amd64");
        assert_eq!(p.suggestions[0].required_by.len(), 2);
        assert_eq!(p.suggestions[1].kind, ConfKind::Use);
        assert_eq!(p.suggestions[1].line, ">=media-libs/baz-1.2 qt6 -qt5");
        assert!(p.problems.iter().any(|x| matches!(x, Problem::News { count: 2 })));
        assert!(p.problems.iter().any(|x| matches!(x, Problem::Blocker { resolved: true, .. })));
    }

    #[test]
    fn masked_and_slot() {
        let out = r#"
!!! All ebuilds that could satisfy "app-misc/foo" have been masked.
!!! One of the following masked packages is required to complete your request:
- app-misc/foo-9999::gentoo (masked by: missing keyword)
- app-misc/foo-1.1::gentoo (masked by: ~amd64 keyword)
- app-misc/foo-1.0::gentoo (masked by: package.mask, Unlicense license(s))
/var/db/repos/gentoo/profiles/package.mask:
# Removal on 2026-11-01

For more information, see the MASKED PACKAGES section in the emerge
man page or refer to the Gentoo Handbook.

!!! Multiple package instances within a single package slot have been pulled
!!! into the dependency graph, resulting in a slot conflict:

dev-libs/foo:0

  (dev-libs/foo-2.0:0/2::gentoo, ebuild scheduled for merge) USE="" pulled in by
    >=dev-libs/foo-2 required by (app-misc/bar-1.0:0/0::gentoo, installed) USE=""

  (dev-libs/foo-1.0:0/1::gentoo, installed) USE="" pulled in by
    <dev-libs/foo-2 required by (app-misc/baz-1.0:0/0::gentoo, installed) USE=""

It may be possible to solve this problem by using package.mask to
"#;
        let p = parse(out, Some(1));
        let Problem::Masked { candidates, .. } = &p.problems[0] else { panic!() };
        assert_eq!(candidates.len(), 3);
        assert_eq!(candidates[0].fixes("amd64")[0].1, "=app-misc/foo-9999 **");
        assert_eq!(candidates[1].fixes("amd64")[0].1, "=app-misc/foo-1.1 ~amd64");
        let f = candidates[2].fixes("amd64");
        assert_eq!(f[0], (ConfKind::Unmask, "=app-misc/foo-1.0".to_string()));
        assert_eq!(f[1], (ConfKind::License, "=app-misc/foo-1.0 Unlicense".to_string()));
        let Problem::SlotConflict(c) = &p.problems[1] else { panic!("{:?}", p.problems) };
        assert_eq!(c.slot_key, "dev-libs/foo:0");
        assert_eq!(c.candidates.len(), 2);
        assert_eq!(c.pulled_by.len(), 2);
    }

    #[test]
    fn skipped_update() {
        let out = "Total: 10 packages (6 upgrades, 4 reinstalls), Size of downloads: 566181 KiB\n\nWARNING: One or more updates/rebuilds have been skipped due to a dependency conflict:\n\nsys-kernel/cachyos-kernel:7.3.0\n\n  (sys-kernel/cachyos-kernel-7.3.0-r2:7.3.0/7.3.0::CachyOS-kernels, ebuild scheduled for merge) USE=\"autofdo\" ABI_X86=\"(64)\" conflicts with\n    =sys-kernel/cachyos-kernel-7.3.0-r1 required by @kernels-keep \n    ^                          ^^^^^^^^\n\n";
        let p = parse(out, Some(0));
        let Some(Problem::Skipped { slot_key, candidate, required }) = p.problems.first() else { panic!("{:?}", p.problems) };
        assert_eq!(slot_key, "sys-kernel/cachyos-kernel:7.3.0");
        assert_eq!(candidate, "sys-kernel/cachyos-kernel-7.3.0-r2");
        assert_eq!(required, &vec![("=sys-kernel/cachyos-kernel-7.3.0-r1".to_string(), "@kernels-keep".to_string())]);
        assert!(!p.problems[0].is_blocking());
    }

    #[test]
    fn depclean_list() {
        let out = ">>> These are the packages that would be unmerged:\n\n dev-lang/rust-bin\n    selected: 1.97.1 \n   protected: none \n     omitted: 1.98.1 \n\nAll selected packages: =dev-lang/rust-bin-1.97.1 =sys-devel/gcc-14.4.1_p20260716\n\n>>> 'Selected' packages are slated for removal.\nNumber to remove:     2\n";
        let p = parse(out, Some(0));
        assert_eq!(p.depclean, vec!["dev-lang/rust-bin-1.97.1", "sys-devel/gcc-14.4.1_p20260716"]);
    }

    #[test]
    fn ambiguous_and_missing() {
        let out = "\n!!! The short ebuild name \"vim\" is ambiguous. Please specify\n!!! one of the following fully-qualified ebuild names instead:\n\n    app-editors/vim\n    dev-python/vim\n\n";
        let p = parse(out, Some(1));
        let Problem::Ambiguous { options, .. } = &p.problems[0] else { panic!() };
        assert_eq!(options, &vec!["app-editors/vim".to_string(), "dev-python/vim".to_string()]);
        let p = parse("emerge: there are no ebuilds to satisfy \"nope\".\n", Some(1));
        assert!(matches!(&p.problems[0], Problem::NoEbuilds { atom } if atom == "nope"));
    }
}
