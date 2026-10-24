//! The user's Portage configuration below /etc/portage: package.* files
//! (file or directory form), env/, sets/, profile overrides and stray files.

use super::atom::{Atom, Op};
use std::path::{Path, PathBuf};

pub const ETC_PORTAGE: &str = "/etc/portage";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ConfKind {
    Use,
    AcceptKeywords,
    Mask,
    Unmask,
    License,
    Env,
    Properties,
    AcceptRestrict,
    Keywords,
    Sets,
    ProfileUseMask,
    ProfileUseForce,
    ProfileProvided,
}

impl ConfKind {
    pub const ALL: [ConfKind; 13] = [
        ConfKind::Use,
        ConfKind::AcceptKeywords,
        ConfKind::Mask,
        ConfKind::Unmask,
        ConfKind::License,
        ConfKind::Env,
        ConfKind::Properties,
        ConfKind::AcceptRestrict,
        ConfKind::Keywords,
        ConfKind::Sets,
        ConfKind::ProfileUseMask,
        ConfKind::ProfileUseForce,
        ConfKind::ProfileProvided,
    ];

    pub fn file_name(&self) -> &'static str {
        match self {
            ConfKind::Use => "package.use",
            ConfKind::AcceptKeywords => "package.accept_keywords",
            ConfKind::Mask => "package.mask",
            ConfKind::Unmask => "package.unmask",
            ConfKind::License => "package.license",
            ConfKind::Env => "package.env",
            ConfKind::Properties => "package.properties",
            ConfKind::AcceptRestrict => "package.accept_restrict",
            ConfKind::Keywords => "package.keywords",
            ConfKind::Sets => "sets",
            ConfKind::ProfileUseMask => "package.use.mask",
            ConfKind::ProfileUseForce => "package.use.force",
            ConfKind::ProfileProvided => "package.provided",
        }
    }

    pub fn path(&self) -> PathBuf {
        match self {
            ConfKind::ProfileUseMask | ConfKind::ProfileUseForce | ConfKind::ProfileProvided => Path::new(ETC_PORTAGE).join("profile").join(self.file_name()),
            _ => Path::new(ETC_PORTAGE).join(self.file_name()),
        }
    }

    pub fn title(&self) -> &'static str {
        match self {
            ConfKind::Use => "USE flags",
            ConfKind::AcceptKeywords => "Accept keywords",
            ConfKind::Mask => "Masks",
            ConfKind::Unmask => "Unmasks",
            ConfKind::License => "Licenses",
            ConfKind::Env => "Package environment",
            ConfKind::Properties => "Properties",
            ConfKind::AcceptRestrict => "Accept restrict",
            ConfKind::Keywords => "Keywords (legacy)",
            ConfKind::Sets => "Package sets",
            ConfKind::ProfileUseMask => "USE mask overrides",
            ConfKind::ProfileUseForce => "USE force overrides",
            ConfKind::ProfileProvided => "Provided packages",
        }
    }

    pub fn icon(&self) -> &'static str {
        use egui_phosphor::regular as i;
        match self {
            ConfKind::Use => i::TOGGLE_RIGHT,
            ConfKind::AcceptKeywords => i::FLASK,
            ConfKind::Mask => i::PROHIBIT,
            ConfKind::Unmask => i::LOCK_OPEN,
            ConfKind::License => i::CERTIFICATE,
            ConfKind::Env => i::SLIDERS,
            ConfKind::Properties => i::LIST_CHECKS,
            ConfKind::AcceptRestrict => i::SHIELD_WARNING,
            ConfKind::Keywords => i::CLOCK_COUNTER_CLOCKWISE,
            ConfKind::Sets => i::STACK,
            ConfKind::ProfileUseMask => i::EYE_SLASH,
            ConfKind::ProfileUseForce => i::LIGHTNING,
            ConfKind::ProfileProvided => i::PACKAGE,
        }
    }

    /// What goes after the atom on each line.
    pub fn values_label(&self) -> &'static str {
        match self {
            ConfKind::Use | ConfKind::ProfileUseMask | ConfKind::ProfileUseForce => "USE flags",
            ConfKind::AcceptKeywords | ConfKind::Keywords => "Keywords",
            ConfKind::License => "Licenses",
            ConfKind::Env => "Environment files",
            ConfKind::Properties => "Properties",
            ConfKind::AcceptRestrict => "Restrict tokens",
            ConfKind::Mask | ConfKind::Unmask | ConfKind::Sets | ConfKind::ProfileProvided => "",
        }
    }

    pub fn has_values(&self) -> bool {
        !self.values_label().is_empty()
    }

    pub fn is_advanced(&self) -> bool {
        matches!(
            self,
            ConfKind::Properties
                | ConfKind::AcceptRestrict
                | ConfKind::Keywords
                | ConfKind::ProfileUseMask
                | ConfKind::ProfileUseForce
                | ConfKind::ProfileProvided
        )
    }

    pub fn explain(&self) -> &'static str {
        match self {
            ConfKind::Use => {
                "Turn optional features (USE flags) on or off for single packages. A flag written as `flag` enables it, `-flag` disables it. This overrides the global USE in make.conf and your profile defaults for the matching packages only."
            }
            ConfKind::AcceptKeywords => {
                "Allow versions that are not marked stable for your architecture. `~amd64` accepts the testing branch, `**` accepts any keyword (including live 9999 ebuilds without keywords). An entry without keywords means \"accept ~ARCH\"."
            }
            ConfKind::Mask => {
                "Forbid Portage from installing the matching versions, even if they are stable. Typical use: keep a package below a certain version (`>=cat/pkg-2`)."
            }
            ConfKind::Unmask => {
                "Lift a mask set by your profile or a repository (package.mask), e.g. for packages masked for testing or removal. Unmasking does not accept keywords — combine with Accept keywords for testing versions."
            }
            ConfKind::License => {
                "Accept software licenses for single packages when ACCEPT_LICENSE in make.conf does not already include them. Values can be license names or groups like @BINARY-REDISTRIBUTABLE."
            }
            ConfKind::Env => "Apply environment files from /etc/portage/env/ (e.g. different CFLAGS, MAKEOPTS, FEATURES) to matching packages only.",
            ConfKind::Properties => "Accept ebuild PROPERTIES such as `interactive` or `live` per package (ACCEPT_PROPERTIES).",
            ConfKind::AcceptRestrict => "Accept ebuild RESTRICT tokens per package (ACCEPT_RESTRICT), e.g. allow `bindist` restricted packages.",
            ConfKind::Keywords => "Deprecated older name for package.accept_keywords. Portage still reads it, but new entries belong in Accept keywords.",
            ConfKind::Sets => "Your own package sets. Each file below /etc/portage/sets/ becomes a set named @filename that can be emerged as a group.",
            ConfKind::ProfileUseMask => "/etc/portage/profile/package.use.mask — mask (or with -flag unmask) USE flags for packages, overriding the profile.",
            ConfKind::ProfileUseForce => {
                "/etc/portage/profile/package.use.force — force (or with -flag unforce) USE flags for packages, overriding the profile."
            }
            ConfKind::ProfileProvided => {
                "/etc/portage/profile/package.provided — tell Portage a package version is provided outside of Portage. Exact versions only (no operators)."
            }
        }
    }

    pub fn wiki(&self) -> &'static str {
        match self {
            ConfKind::Use => "https://wiki.gentoo.org/wiki//etc/portage/package.use",
            ConfKind::AcceptKeywords | ConfKind::Keywords => "https://wiki.gentoo.org/wiki//etc/portage/package.accept_keywords",
            ConfKind::Mask => "https://wiki.gentoo.org/wiki//etc/portage/package.mask",
            ConfKind::Unmask => "https://wiki.gentoo.org/wiki//etc/portage/package.unmask",
            ConfKind::License => "https://wiki.gentoo.org/wiki//etc/portage/package.license",
            ConfKind::Env => "https://wiki.gentoo.org/wiki//etc/portage/package.env",
            ConfKind::Sets => "https://wiki.gentoo.org/wiki//etc/portage/sets",
            _ => "https://wiki.gentoo.org/wiki//etc/portage",
        }
    }

    /// Common value suggestions offered as one-click chips.
    pub fn suggestions(&self) -> &'static [&'static str] {
        match self {
            ConfKind::AcceptKeywords | ConfKind::Keywords => &["~amd64", "**", "~*", "-*"],
            ConfKind::License => &["@BINARY-REDISTRIBUTABLE", "@FREE", "@EULA", "*"],
            ConfKind::Properties => &["interactive", "live", "test_network", "*"],
            ConfKind::AcceptRestrict => &["bindist", "fetch", "mirror", "*"],
            _ => &[],
        }
    }

    pub fn from_file_name(n: &str) -> Option<ConfKind> {
        ConfKind::ALL.iter().copied().find(|k| k.file_name() == n && !matches!(k, ConfKind::Sets))
    }
}

#[derive(Debug, Clone)]
pub struct Entry {
    pub atom_text: String,
    pub atom: Option<Atom>,
    pub values: Vec<String>,
    pub comment: Option<String>,
    /// Commented-out entry (`#cat/pkg flag`), shown as a switched-off row.
    pub disabled: bool,
}

impl Entry {
    pub fn new(atom: &str, values: Vec<String>) -> Entry {
        Entry { atom_text: atom.to_string(), atom: Atom::parse(atom), values, comment: None, disabled: false }
    }

    pub fn render(&self) -> String {
        let mut s = String::new();
        if self.disabled {
            s.push('#');
        }
        s.push_str(&self.atom_text);
        for v in &self.values {
            s.push(' ');
            s.push_str(v);
        }
        if let Some(c) = &self.comment {
            s.push_str(" #");
            s.push_str(c);
        }
        s
    }

    /// Plain-language explanation for beginners.
    pub fn describe(&self, kind: ConfKind) -> String {
        let target = match &self.atom {
            Some(a) => describe_atom(a),
            None => format!("“{}” (not a valid atom)", self.atom_text),
        };
        let vals = self.values.join(", ");
        match kind {
            ConfKind::Use => {
                let on: Vec<_> = self.values.iter().filter(|v| !v.starts_with('-')).cloned().collect();
                let off: Vec<_> = self.values.iter().filter_map(|v| v.strip_prefix('-')).map(String::from).collect();
                let mut parts = Vec::new();
                if !on.is_empty() {
                    parts.push(format!("enable {}", on.join(", ")));
                }
                if !off.is_empty() {
                    parts.push(format!("disable {}", off.join(", ")));
                }
                if parts.is_empty() { format!("No flags set for {target}") } else { format!("{} for {target}", capitalize(&parts.join(" and "))) }
            }
            ConfKind::AcceptKeywords | ConfKind::Keywords => {
                if self.values.is_empty() {
                    format!("Accept the testing (~arch) branch for {target}")
                } else if self.values.iter().any(|v| v == "**") {
                    format!("Accept every keyword, including unkeyworded/live ebuilds, for {target}")
                } else {
                    format!("Accept keywords {vals} for {target}")
                }
            }
            ConfKind::Mask => format!("Never install {target}"),
            ConfKind::Unmask => format!("Lift repository/profile masks on {target}"),
            ConfKind::License => format!("Accept license {vals} for {target}"),
            ConfKind::Env => format!("Build {target} with environment {vals}"),
            ConfKind::Properties => format!("Accept properties {vals} for {target}"),
            ConfKind::AcceptRestrict => format!("Accept restrictions {vals} for {target}"),
            ConfKind::Sets => format!("Member: {target}"),
            ConfKind::ProfileUseMask => format!("Mask flags {vals} for {target}"),
            ConfKind::ProfileUseForce => format!("Force flags {vals} for {target}"),
            ConfKind::ProfileProvided => format!("Pretend {target} is installed"),
        }
    }
}

fn capitalize(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
        None => String::new(),
    }
}

pub fn describe_atom(a: &Atom) -> String {
    let who = if a.cat == "*" && a.pn == "*" {
        "all packages".to_string()
    } else if a.pn == "*" {
        format!("every package in {}", a.cat)
    } else {
        a.key()
    };
    let mut s = match (&a.ver, a.op) {
        (Some(v), Op::Eq) => format!("{who} version {v}"),
        (Some(v), Op::EqGlob) => format!("{who} versions {v}*"),
        (Some(v), Op::Tilde) => format!("{who} {v} (any revision)"),
        (Some(v), Op::Ge) => format!("{who} {v} and newer"),
        (Some(v), Op::Gt) => format!("{who} newer than {v}"),
        (Some(v), Op::Le) => format!("{who} {v} and older"),
        (Some(v), Op::Lt) => format!("{who} older than {v}"),
        _ => who,
    };
    if let Some(sl) = &a.slot {
        s.push_str(&format!(" in slot {sl}"));
    }
    if let Some(r) = &a.repo {
        s.push_str(&format!(" from ::{r}"));
    }
    s
}

// Most lines are entries, so boxing them would only add an allocation per line.
#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone)]
pub enum Line {
    Blank,
    Comment(String),
    Entry(Entry),
}

pub fn parse_line(raw: &str) -> Line {
    let t = raw.trim();
    if t.is_empty() {
        return Line::Blank;
    }
    let (disabled, body) = match t.strip_prefix('#') {
        Some(rest) => (true, rest.trim()),
        None => (false, t),
    };
    let (body, comment) = match body.find(" #").or_else(|| body.find("\t#")) {
        Some(i) => (body[..i].trim(), Some(body[i + 2..].to_string())),
        None => (body, None),
    };
    let mut toks = body.split_whitespace();
    let Some(first) = toks.next() else {
        return Line::Comment(raw.to_string());
    };
    let atom = Atom::parse(first);
    let looks_like_entry = first.contains('/') && atom.is_some();
    if disabled && !looks_like_entry {
        return Line::Comment(raw.to_string());
    }
    if !disabled && first.starts_with('@') {
        // Sets may include other sets.
        return Line::Entry(Entry { atom_text: first.to_string(), atom: None, values: toks.map(String::from).collect(), comment, disabled });
    }
    Line::Entry(Entry { atom_text: first.to_string(), atom, values: toks.map(String::from).collect(), comment, disabled })
}

#[derive(Debug, Clone)]
pub struct ConfFile {
    pub path: PathBuf,
    /// Name relative to the set's base (equal to the file name for file-form sets).
    pub rel: String,
    /// Portage skips files whose names start with `.` or end with `~`.
    pub ignored: bool,
    pub lines: Vec<Line>,
    pub raw: String,
}

impl ConfFile {
    pub fn read(path: &Path, rel: String) -> ConfFile {
        let raw = std::fs::read_to_string(path).unwrap_or_default();
        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let ignored = name.starts_with('.') || name.ends_with('~');
        let lines = raw.lines().map(parse_line).collect();
        ConfFile { path: path.to_path_buf(), rel, ignored, lines, raw }
    }

    pub fn entries(&self) -> impl Iterator<Item = (usize, &Entry)> {
        self.lines.iter().enumerate().filter_map(|(i, l)| match l {
            Line::Entry(e) => Some((i, e)),
            _ => None,
        })
    }

    pub fn render(&self) -> String {
        let mut s = String::new();
        let raw_lines: Vec<&str> = self.raw.lines().collect();
        for (i, l) in self.lines.iter().enumerate() {
            match l {
                Line::Blank => {}
                Line::Comment(c) => s.push_str(c),
                Line::Entry(e) => {
                    // Keep untouched lines byte-for-byte.
                    let orig = raw_lines.get(i).copied();
                    let rendered = e.render();
                    match orig {
                        Some(o) if matches!(parse_line(o), Line::Entry(ref oe) if oe.render() == rendered) => s.push_str(o),
                        _ => s.push_str(&rendered),
                    }
                }
            }
            s.push('\n');
        }
        s
    }
}

#[derive(Debug, Clone)]
pub struct ConfSet {
    pub kind: ConfKind,
    pub base: PathBuf,
    pub exists: bool,
    pub is_dir: bool,
    pub files: Vec<ConfFile>,
}

impl ConfSet {
    pub fn entry_count(&self) -> usize {
        self.files.iter().filter(|f| !f.ignored).map(|f| f.entries().filter(|(_, e)| !e.disabled).count()).sum()
    }

    pub fn file(&self, path: &Path) -> Option<&ConfFile> {
        self.files.iter().find(|f| f.path == path)
    }

    /// Where a new entry goes by default.
    pub fn default_target(&self, preferred_name: &str) -> PathBuf {
        if self.is_dir || !self.exists { self.base.join(preferred_name) } else { self.base.clone() }
    }
}

fn walk(dir: &Path, base: &Path, out: &mut Vec<ConfFile>) {
    let mut entries: Vec<_> = std::fs::read_dir(dir).into_iter().flatten().flatten().collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let p = e.path();
        let name = e.file_name().to_string_lossy().to_string();
        if p.is_dir() {
            if name.starts_with('.') || name == "CVS" {
                continue;
            }
            walk(&p, base, out);
        } else {
            let rel = p.strip_prefix(base).map(|r| r.to_string_lossy().to_string()).unwrap_or(name);
            out.push(ConfFile::read(&p, rel));
        }
    }
}

pub fn load_set(kind: ConfKind) -> ConfSet {
    let base = kind.path();
    let exists = base.exists();
    let is_dir = base.is_dir();
    let mut files = Vec::new();
    if is_dir {
        walk(&base, &base, &mut files);
    } else if exists {
        let name = base.file_name().unwrap().to_string_lossy().to_string();
        files.push(ConfFile::read(&base, name));
    }
    ConfSet { kind, base, exists, is_dir, files }
}

#[derive(Debug, Clone)]
pub struct EnvFile {
    pub name: String,
    pub path: PathBuf,
    pub content: String,
}

#[derive(Debug, Clone, PartialEq)]
pub enum StrayReason {
    /// e.g. `package.accept_keywordschatgpt-desktop`: Portage never reads it.
    Misnamed {
        kind: ConfKind,
        suggested_name: String,
    },
    Backup,
    Unknown,
}

#[derive(Debug, Clone)]
pub struct Stray {
    pub path: PathBuf,
    pub reason: StrayReason,
}

#[derive(Debug, Clone, Default)]
pub struct PortageConfig {
    pub sets: Vec<ConfSet>,
    pub env_files: Vec<EnvFile>,
    pub strays: Vec<Stray>,
}

impl PortageConfig {
    pub fn set(&self, kind: ConfKind) -> Option<&ConfSet> {
        self.sets.iter().find(|s| s.kind == kind)
    }

    /// Every entry (in any package.* file) whose atom covers `cat/pn`.
    pub fn entries_for(&self, cat: &str, pn: &str) -> Vec<(ConfKind, PathBuf, usize, Entry)> {
        let mut out = Vec::new();
        for s in &self.sets {
            if s.kind == ConfKind::Sets {
                continue;
            }
            for f in &s.files {
                for (i, e) in f.entries() {
                    if let Some(a) = &e.atom
                        && a.matches_key(cat, pn)
                    {
                        out.push((s.kind, f.path.clone(), i, e.clone()));
                    }
                }
            }
        }
        out
    }
}

pub fn load() -> PortageConfig {
    let mut cfg = PortageConfig { sets: ConfKind::ALL.iter().map(|&k| load_set(k)).collect(), ..Default::default() };
    let env_dir = Path::new(ETC_PORTAGE).join("env");
    let mut envs: Vec<_> = std::fs::read_dir(&env_dir).into_iter().flatten().flatten().collect();
    envs.sort_by_key(|e| e.file_name());
    for e in envs {
        let p = e.path();
        if p.is_file() {
            let name = e.file_name().to_string_lossy().to_string();
            if name.starts_with('.') || name.ends_with('~') {
                continue;
            }
            cfg.env_files.push(EnvFile { name, content: std::fs::read_to_string(&p).unwrap_or_default(), path: p });
        }
    }
    cfg.strays = find_strays();
    cfg
}

pub fn find_strays() -> Vec<Stray> {
    let mut out = Vec::new();
    let known: Vec<&str> = ConfKind::ALL
        .iter()
        .filter(|k| !matches!(k, ConfKind::Sets | ConfKind::ProfileUseMask | ConfKind::ProfileUseForce | ConfKind::ProfileProvided))
        .map(|k| k.file_name())
        .collect();
    for e in std::fs::read_dir(ETC_PORTAGE).into_iter().flatten().flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        if !name.starts_with("package.") || known.contains(&name.as_str()) {
            continue;
        }
        // Longest known prefix wins (package.accept_keywords over package.a…).
        let prefix = known.iter().filter(|k| name.starts_with(*k)).max_by_key(|k| k.len());
        let reason = match prefix {
            Some(k) => {
                let rest = name[k.len()..].trim_matches(|c| c == '.' || c == '-' || c == '_' || c == '~');
                let lower = rest.to_lowercase();
                if rest.is_empty()
                    || ["backup", "bak", "old", "orig", "save", "dpkg-old"]
                        .iter()
                        .any(|b| lower == *b || lower.starts_with(&format!("{b}.")) || lower.ends_with(b))
                {
                    StrayReason::Backup
                } else {
                    StrayReason::Misnamed { kind: ConfKind::from_file_name(k).unwrap_or(ConfKind::Use), suggested_name: rest.to_string() }
                }
            }
            None => StrayReason::Unknown,
        };
        out.push(Stray { path: e.path(), reason });
    }
    out.sort_by(|a, b| a.path.cmp(&b.path));
    out
}

/// Append an entry to the text of a file, keeping a trailing newline.
pub fn append_line(text: &str, line: &str) -> String {
    let mut s = text.to_string();
    if !s.is_empty() && !s.ends_with('\n') {
        s.push('\n');
    }
    s.push_str(line);
    s.push('\n');
    s
}

/// Text of `file` with line `idx` replaced (`None` removes it).
pub fn replace_line(file: &ConfFile, idx: usize, new: Option<Line>) -> String {
    let mut f = file.clone();
    match new {
        Some(l) => {
            if idx < f.lines.len() {
                f.lines[idx] = l;
            }
        }
        None => {
            if idx < f.lines.len() {
                f.lines.remove(idx);
                // Keep `raw` aligned so untouched lines still render verbatim.
                let mut raw: Vec<&str> = file.raw.lines().collect();
                if idx < raw.len() {
                    raw.remove(idx);
                }
                f.raw = raw.join("\n");
            }
        }
    }
    f.render()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lines() {
        match parse_line(">=app-misc/foo-1.0 bar -baz # keep") {
            Line::Entry(e) => {
                assert_eq!(e.atom_text, ">=app-misc/foo-1.0");
                assert_eq!(e.values, vec!["bar", "-baz"]);
                assert_eq!(e.comment.as_deref(), Some(" keep"));
                assert!(!e.disabled);
            }
            _ => panic!(),
        }
        match parse_line("#app-misc/foo ~amd64") {
            Line::Entry(e) => assert!(e.disabled),
            _ => panic!(),
        }
        assert!(matches!(parse_line("# required by app-misc/foo-1.0::gentoo"), Line::Comment(_)));
        assert!(matches!(parse_line("   "), Line::Blank));
    }

    #[test]
    fn roundtrip_and_edit() {
        let raw = "# header\napp-misc/foo  bar\n\n#app-misc/baz -x\n";
        let f = ConfFile { path: "/x".into(), rel: "x".into(), ignored: false, lines: raw.lines().map(parse_line).collect(), raw: raw.into() };
        // Untouched lines keep their original spacing.
        assert_eq!(f.render(), raw);
        let mut e = match &f.lines[3] {
            Line::Entry(e) => e.clone(),
            _ => panic!(),
        };
        e.disabled = false;
        assert_eq!(replace_line(&f, 3, Some(Line::Entry(e))), "# header\napp-misc/foo  bar\n\napp-misc/baz -x\n");
        assert_eq!(replace_line(&f, 1, None), "# header\n\n#app-misc/baz -x\n");
    }

    #[test]
    fn describe() {
        let e = Entry::new(">=dev-lang/rust-1.80", vec!["clippy".into(), "-doc".into()]);
        assert_eq!(e.describe(ConfKind::Use), "Enable clippy and disable doc for dev-lang/rust 1.80 and newer");
    }
}
