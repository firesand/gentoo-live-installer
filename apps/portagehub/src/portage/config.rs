//! The user's Portage configuration below /etc/portage: package.* files
//! (file or directory form), env/, sets/, profile overrides and stray files.

use super::atom::{Atom, Op};
use std::path::{Path, PathBuf};

pub const ETC_PORTAGE: &str = "/etc/portage";

/// Read a configuration file for editing. A file that does not exist is empty; one that cannot be
/// read, or is not valid UTF-8, is an error that says why. It must never be mistaken for an empty
/// file: the next edit would rewrite the whole file from nothing.
pub fn read_utf8(path: &Path) -> Result<String, String> {
    match std::fs::read(path) {
        Ok(bytes) => String::from_utf8(bytes).map_err(|e| {
            format!("{} is not valid UTF-8 (first bad byte at offset {}), so PortageHub will not edit it", path.display(), e.utf8_error().valid_up_to())
        }),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
        Err(e) => Err(format!("{} cannot be read by your user ({e}), so PortageHub will not edit it", path.display())),
    }
}

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
            ConfKind::ProfileUseMask | ConfKind::ProfileUseForce | ConfKind::ProfileProvided => {
                Path::new(ETC_PORTAGE).join("profile").join(self.file_name())
            }
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
            ConfKind::Use => "Turn optional features (USE flags) on or off for single packages. A flag written as `flag` enables it, `-flag` disables it. This overrides the global USE in make.conf and your profile defaults for the matching packages only.",
            ConfKind::AcceptKeywords => "Allow versions that are not marked stable for your architecture. `~amd64` accepts the testing branch, `**` accepts any keyword (including live 9999 ebuilds without keywords). An entry without keywords means \"accept ~ARCH\".",
            ConfKind::Mask => "Forbid Portage from installing the matching versions, even if they are stable. Typical use: keep a package below a certain version (`>=cat/pkg-2`).",
            ConfKind::Unmask => "Lift a mask set by your profile or a repository (package.mask), e.g. for packages masked for testing or removal. Unmasking does not accept keywords — combine with Accept keywords for testing versions.",
            ConfKind::License => "Accept software licenses for single packages when ACCEPT_LICENSE in make.conf does not already include them. Values can be license names or groups like @BINARY-REDISTRIBUTABLE.",
            ConfKind::Env => "Apply environment files from /etc/portage/env/ (e.g. different CFLAGS, MAKEOPTS, FEATURES) to matching packages only.",
            ConfKind::Properties => "Accept ebuild PROPERTIES such as `interactive` or `live` per package (ACCEPT_PROPERTIES).",
            ConfKind::AcceptRestrict => "Accept ebuild RESTRICT tokens per package (ACCEPT_RESTRICT), e.g. allow `bindist` restricted packages.",
            ConfKind::Keywords => "Deprecated older name for package.accept_keywords. Portage still reads it, but new entries belong in Accept keywords.",
            ConfKind::Sets => "Your own package sets. Each file below /etc/portage/sets/ becomes a set named @filename that can be emerged as a group.",
            ConfKind::ProfileUseMask => "/etc/portage/profile/package.use.mask — mask (or with -flag unmask) USE flags for packages, overriding the profile.",
            ConfKind::ProfileUseForce => "/etc/portage/profile/package.use.force — force (or with -flag unforce) USE flags for packages, overriding the profile.",
            ConfKind::ProfileProvided => "/etc/portage/profile/package.provided — tell Portage a package version is provided outside of Portage. Exact versions only (no operators).",
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
        Entry { atom_text: atom.to_string(), atom: Atom::parse_user(atom), values, comment: None, disabled: false }
    }

    /// The USE flags this line sets, the way Portage reads them: `(flag, enabled)`. A token that
    /// ends in `:` (`PYTHON_TARGETS:`) makes the tokens after it, to the end of the line, belong
    /// to that USE_EXPAND variable: `PYTHON_TARGETS: python3_12 -python3_11` sets
    /// `python_targets_python3_12` and clears `python_targets_python3_11`. A `-*` under such a
    /// prefix resets the variable; it is not a flag.
    pub fn flag_tokens(&self) -> Vec<(String, bool)> {
        let mut prefix = String::new();
        let mut out = Vec::new();
        for token in &self.values {
            if let Some(var) = token.strip_suffix(':') {
                prefix = format!("{}_", var.to_lowercase());
                continue;
            }
            let (name, enabled) = match token.strip_prefix('-') {
                Some(n) => (n, false),
                None => (token.as_str(), true),
            };
            if name.is_empty() || (name == "*" && !prefix.is_empty()) {
                continue;
            }
            out.push((format!("{prefix}{name}"), enabled));
        }
        out
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
                let flags = self.flag_tokens();
                let on: Vec<_> = flags.iter().filter(|(_, enabled)| *enabled).map(|(f, _)| f.clone()).collect();
                let off: Vec<_> = flags.iter().filter(|(_, enabled)| !*enabled).map(|(f, _)| f.clone()).collect();
                let mut parts = Vec::new();
                if !on.is_empty() {
                    parts.push(format!("enable {}", on.join(", ")));
                }
                if !off.is_empty() {
                    parts.push(format!("disable {}", off.join(", ")));
                }
                if parts.is_empty() {
                    format!("No flags set for {target}")
                } else {
                    format!("{} for {target}", capitalize(&parts.join(" and ")))
                }
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
    } else if a.is_wildcard() {
        format!("packages matching {}", a.key())
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

#[derive(Debug, Clone)]
pub enum Line {
    Blank,
    Comment(String),
    Entry(Box<Entry>),
}

pub fn parse_line(raw: &str) -> Line {
    parse_line_as(ConfKind::Use, raw)
}

/// Parse one line of a `kind` file. `package.provided` lists exact versions (`sys-apps/foo-1.0`),
/// which are not atoms; every other kind holds atoms as Portage reads them in `package.*` files.
pub fn parse_line_as(kind: ConfKind, raw: &str) -> Line {
    let t = raw.trim();
    if t.is_empty() {
        return Line::Blank;
    }
    // `Entry::render` writes a switched-off entry as `#atom …`, with no space after the `#`. A
    // comment such as `# dev-libs/foo is needed by bar` is prose, not an entry to switch on.
    let (disabled, body) = match t.strip_prefix('#') {
        Some(rest) if rest.starts_with(char::is_whitespace) => return Line::Comment(raw.to_string()),
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
    let atom = if kind == ConfKind::ProfileProvided { Atom::parse(first) } else { Atom::parse_user(first) };
    let looks_like_entry = first.contains('/') && !first.starts_with('!') && atom.is_some();
    if disabled && !looks_like_entry {
        return Line::Comment(raw.to_string());
    }
    if !disabled && first.starts_with('@') {
        // Sets may include other sets.
        return Line::Entry(Box::new(Entry { atom_text: first.to_string(), atom: None, values: toks.map(String::from).collect(), comment, disabled }));
    }
    Line::Entry(Box::new(Entry { atom_text: first.to_string(), atom, values: toks.map(String::from).collect(), comment, disabled }))
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
    /// Why the file could not be read as UTF-8 text (it then shows no entries and is never edited).
    pub unreadable: Option<String>,
    /// The path is a symlink: the helper never writes through one (the link target would decide
    /// where root writes), so the file is shown but not edited.
    pub symlink: bool,
}

impl ConfFile {
    pub fn read(path: &Path, rel: String) -> ConfFile {
        ConfFile::read_as(ConfKind::Use, path, rel)
    }

    pub fn read_as(kind: ConfKind, path: &Path, rel: String) -> ConfFile {
        let (raw, unreadable) = match read_utf8(path) {
            Ok(t) => (t, None),
            Err(e) => (String::new(), Some(e)),
        };
        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let ignored = name.starts_with('.') || name.ends_with('~');
        let lines = raw.lines().map(|l| parse_line_as(kind, l)).collect();
        let symlink = std::fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink());
        ConfFile { path: path.to_path_buf(), rel, ignored, lines, raw, unreadable, symlink }
    }

    /// Why this file must not be edited, if it must not.
    pub fn edit_blocked(&self) -> Option<String> {
        if let Some(e) = &self.unreadable {
            return Some(e.clone());
        }
        self.symlink.then(|| symlink_message(&self.path))
    }

    pub fn entries(&self) -> impl Iterator<Item = (usize, &Entry)> {
        self.lines.iter().enumerate().filter_map(|(i, l)| match l {
            Line::Entry(e) => Some((i, &**e)),
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
    /// The set's path (the `package.use` directory or file itself) is a symlink.
    pub symlink: bool,
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
        if self.is_dir || !self.exists {
            self.base.join(preferred_name)
        } else {
            self.base.clone()
        }
    }
}

/// The explanation for a path the helper will not write through.
pub fn symlink_message(path: &Path) -> String {
    format!(
        "{} is a symlink. PortageHub never writes through a symlink, because the link target would decide where root writes: point it at a real file or directory, or edit it by hand.",
        path.display()
    )
}

fn walk(kind: ConfKind, dir: &Path, base: &Path, out: &mut Vec<ConfFile>) {
    let mut entries: Vec<_> = std::fs::read_dir(dir).into_iter().flatten().flatten().collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let p = e.path();
        let name = e.file_name().to_string_lossy().to_string();
        if p.is_dir() {
            if name.starts_with('.') || name == "CVS" {
                continue;
            }
            walk(kind, &p, base, out);
        } else {
            let rel = p.strip_prefix(base).map(|r| r.to_string_lossy().to_string()).unwrap_or(name);
            out.push(ConfFile::read_as(kind, &p, rel));
        }
    }
}

pub fn load_set(kind: ConfKind) -> ConfSet {
    let base = kind.path();
    let exists = base.exists();
    let is_dir = base.is_dir();
    let mut files = Vec::new();
    if is_dir {
        walk(kind, &base, &base, &mut files);
    } else if exists {
        let name = base.file_name().unwrap().to_string_lossy().to_string();
        files.push(ConfFile::read_as(kind, &base, name));
    }
    let symlink = std::fs::symlink_metadata(&base).is_ok_and(|m| m.file_type().is_symlink());
    ConfSet { kind, base, exists, is_dir, symlink, files }
}

#[derive(Debug, Clone)]
pub struct EnvFile {
    pub name: String,
    pub path: PathBuf,
    pub content: String,
    /// Why the file could not be read as UTF-8 text (see [`ConfFile::unreadable`]).
    pub unreadable: Option<String>,
    pub symlink: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub enum StrayReason {
    /// e.g. `package.accept_keywordschatgpt-desktop`: Portage never reads it.
    Misnamed { kind: ConfKind, suggested_name: String },
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
    /// `/etc/portage` itself is a symlink: the helper opens it without following links.
    pub root_symlink: bool,
}

impl PortageConfig {
    pub fn set(&self, kind: ConfKind) -> Option<&ConfSet> {
        self.sets.iter().find(|s| s.kind == kind)
    }

    /// Why `path` must not be changed, if it must not: it (or a directory above it) is a symlink,
    /// or it could not be read as UTF-8 text and rewriting it would replace it from nothing.
    pub fn edit_blocked_for(&self, path: &Path) -> Option<String> {
        if self.root_symlink {
            return Some(symlink_message(Path::new(ETC_PORTAGE)));
        }
        for s in &self.sets {
            if s.symlink && path.starts_with(&s.base) {
                return Some(symlink_message(&s.base));
            }
            if let Some(m) = s.file(path).and_then(ConfFile::edit_blocked) {
                return Some(m);
            }
        }
        let env = self.env_files.iter().find(|e| e.path == path)?;
        env.unreadable.clone().or_else(|| env.symlink.then(|| symlink_message(&env.path)))
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
    cfg.root_symlink = std::fs::symlink_metadata(ETC_PORTAGE).is_ok_and(|m| m.file_type().is_symlink());
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
            let (content, unreadable) = match read_utf8(&p) {
                Ok(t) => (t, None),
                Err(e) => (String::new(), Some(e)),
            };
            let symlink = std::fs::symlink_metadata(&p).is_ok_and(|m| m.file_type().is_symlink());
            cfg.env_files.push(EnvFile { name, content, unreadable, symlink, path: p });
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
                if rest.is_empty() || ["backup", "bak", "old", "orig", "save", "dpkg-old"].iter().any(|b| lower == *b || lower.starts_with(&format!("{b}.")) || lower.ends_with(b)) {
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
        let f = ConfFile {
            path: "/x".into(),
            rel: "x".into(),
            ignored: false,
            lines: raw.lines().map(parse_line).collect(),
            raw: raw.into(),
            unreadable: None,
            symlink: false,
        };
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

    /// NOBS-19: only `#atom …` (how `Entry::render` writes a switched-off entry) is a disabled
    /// entry; prose that happens to start with something atom-shaped stays a comment.
    #[test]
    fn comments_are_not_mistaken_for_switched_off_entries() {
        for entry in ["#app-misc/foo bar", "#>=dev-lang/rust-1.80 clippy -doc", "#dev-qt/qt* qml"] {
            match parse_line(entry) {
                Line::Entry(e) => {
                    assert!(e.disabled, "{entry}");
                    assert_eq!(e.render(), entry, "round trip");
                }
                other => panic!("{entry}: {other:?}"),
            }
        }
        for prose in ["# app-misc/foo bar", "# app-misc/foo: needed for bar", "# dev-libs/foo is needed by bar", "# and/or maybe", "#  dev-libs/foo x", "#\tdev-libs/foo x", "# required by app-misc/foo-1.0::gentoo", "##app-misc/foo x", "#!app-misc/foo"] {
            assert!(matches!(parse_line(prose), Line::Comment(_)), "{prose}");
        }
        // Active lines are untouched by the rule.
        assert!(matches!(parse_line("app-misc/foo bar # why"), Line::Entry(e) if !e.disabled && e.comment.as_deref() == Some(" why")));
        for line in ["# header", "#app-misc/foo bar", "# dev-libs/foo is needed by bar", "app-misc/foo  bar", ""] {
            let f = ConfFile { path: "/x".into(), rel: "x".into(), ignored: false, lines: vec![parse_line(line)], raw: line.into(), unreadable: None, symlink: false };
            assert_eq!(f.render(), format!("{line}\n").replacen("\n\n", "\n", 1), "{line:?}");
        }
    }

    /// P7: a `VAR:` token prefixes everything after it on the line.
    #[test]
    fn use_expand_tokens_are_prefixed() {
        let flags = |line: &str| match parse_line(line) {
            Line::Entry(e) => e.flag_tokens(),
            other => panic!("{other:?}"),
        };
        assert_eq!(flags("dev-python/foo test -doc"), vec![("test".to_string(), true), ("doc".to_string(), false)]);
        assert_eq!(
            flags("dev-python/foo test PYTHON_TARGETS: python3_12 -python3_11"),
            vec![("test".to_string(), true), ("python_targets_python3_12".to_string(), true), ("python_targets_python3_11".to_string(), false)]
        );
        // The Handbook form: `-*` under a prefix resets the variable, it is not a flag.
        assert_eq!(flags("*/* VIDEO_CARDS: -* radeon radeonsi"), vec![("video_cards_radeon".to_string(), true), ("video_cards_radeonsi".to_string(), true)]);
        assert_eq!(flags("*/* CPU_FLAGS_X86: aes avx2 VIDEO_CARDS: nvidia").len(), 3);
        // The prefix is lower-cased like Portage does.
        assert_eq!(flags("*/* Python_Targets: python3_13")[0].0, "python_targets_python3_13");
        let e = match parse_line("dev-python/foo PYTHON_TARGETS: python3_12 -python3_11") {
            Line::Entry(e) => e,
            _ => panic!(),
        };
        assert_eq!(e.describe(ConfKind::Use), "Enable python_targets_python3_12 and disable python_targets_python3_11 for dev-python/foo");
    }

    #[test]
    fn atoms_in_entries_follow_portage() {
        // Partial wildcards are valid; a version without operator is not.
        for ok in ["dev-qt/qt* qml", "app-*/foo x", "*/* VIDEO_CARDS: radeon"] {
            assert!(matches!(parse_line(ok), Line::Entry(e) if e.atom.is_some()), "{ok}");
        }
        for bad in ["app-misc/foo-1.0 x", "app-misc/foo: x", "app-misc/foo[] x"] {
            assert!(matches!(parse_line(bad), Line::Entry(e) if e.atom.is_none()), "{bad}");
        }
        // package.provided lists exact versions, which are not atoms.
        assert!(matches!(parse_line_as(ConfKind::ProfileProvided, "sys-apps/foo-1.0"), Line::Entry(e) if e.atom.is_some()));
        assert!(matches!(parse_line_as(ConfKind::Use, "sys-apps/foo-1.0"), Line::Entry(e) if e.atom.is_none()));
        assert_eq!(describe_atom(&Atom::parse_user("dev-qt/qt*").unwrap()), "packages matching dev-qt/qt*");
        assert_eq!(describe_atom(&Atom::parse_user("app-*/*").unwrap()), "every package in app-*");
        assert_eq!(describe_atom(&Atom::parse_user("*/*").unwrap()), "all packages");
    }
}
