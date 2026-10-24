//! make.conf: a bash-like assignment file. We read assignments (with quote
//! handling and ${VAR} expansion) and can rewrite single assignments.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

pub const MAKE_CONF: &str = "/etc/portage/make.conf";

#[derive(Debug, Clone)]
pub struct Assignment {
    pub name: String,
    /// Value as written (quotes removed, not expanded).
    pub value: String,
    pub expanded: String,
    pub file: PathBuf,
    pub self_ref: bool,
}

#[derive(Debug, Clone, Default)]
pub struct MakeConf {
    pub files: Vec<(PathBuf, String)>,
    pub assignments: Vec<Assignment>,
    /// Files that could not be read as UTF-8 text, and why. They contribute no assignments and
    /// must never be rewritten: the editor would start from an empty text.
    pub unreadable: Vec<(PathBuf, String)>,
    /// Files that are symlinks: read as usual, but the helper never writes through one.
    pub symlinks: Vec<PathBuf>,
    /// Files pulled in with `source` (or `.`): Portage reads their variables too, so a value shown
    /// here may be overridden there.
    pub sourced: Vec<PathBuf>,
    /// Parts of a file that could not be understood (an unterminated quote), with the file.
    pub problems: Vec<(PathBuf, String)>,
}

impl MakeConf {
    pub fn last(&self, name: &str) -> Option<&Assignment> {
        self.assignments.iter().rev().find(|a| a.name == name)
    }

    pub fn value(&self, name: &str) -> String {
        self.last(name).map(|a| a.expanded.clone()).unwrap_or_default()
    }

    pub fn count(&self, name: &str) -> usize {
        self.assignments.iter().filter(|a| a.name == name).count()
    }

    pub fn names(&self) -> Vec<String> {
        let mut v: Vec<String> = Vec::new();
        for a in &self.assignments {
            if !v.contains(&a.name) {
                v.push(a.name.clone());
            }
        }
        v
    }

    pub fn main_file(&self) -> PathBuf {
        self.files.first().map(|f| f.0.clone()).unwrap_or_else(|| PathBuf::from(MAKE_CONF))
    }

    /// Why `file` must not be edited, if it must not.
    pub fn edit_blocked(&self, file: &Path) -> Option<String> {
        if let Some((_, why)) = self.unreadable.iter().find(|(p, _)| p == file) {
            return Some(why.clone());
        }
        self.symlinks.iter().any(|p| p == file).then(|| super::config::symlink_message(file))
    }

    pub fn text_of(&self, file: &Path) -> &str {
        self.files.iter().find(|f| f.0 == file).map(|f| f.1.as_str()).unwrap_or("")
    }
}

fn expand(s: &str, vars: &HashMap<String, String>) -> String {
    let mut out = String::new();
    let b: Vec<char> = s.chars().collect();
    let mut i = 0;
    while i < b.len() {
        if b[i] == '\\' && i + 1 < b.len() {
            if b[i + 1] != '\n' {
                out.push(b[i + 1]);
            }
            i += 2;
            continue;
        }
        if b[i] == '$' {
            if i + 1 < b.len() && b[i + 1] == '{' {
                if let Some(end) = b[i + 2..].iter().position(|&c| c == '}') {
                    let name: String = b[i + 2..i + 2 + end].iter().collect();
                    let name = name.split(":-").next().unwrap_or("").to_string();
                    out.push_str(vars.get(&name).map(|s| s.as_str()).unwrap_or(""));
                    i += end + 3;
                    continue;
                }
            } else {
                let mut j = i + 1;
                while j < b.len() && (b[j].is_ascii_alphanumeric() || b[j] == '_') {
                    j += 1;
                }
                if j > i + 1 {
                    let name: String = b[i + 1..j].iter().collect();
                    out.push_str(vars.get(&name).map(|s| s.as_str()).unwrap_or(""));
                    i = j;
                    continue;
                }
            }
        }
        out.push(b[i]);
        i += 1;
    }
    out
}

/// What a make.conf contains: assignments, `source`d files, and what could not be understood.
#[derive(Debug, Default)]
pub struct Scan {
    /// (name, value as written with the quotes removed, byte span from the name to the end of the value).
    pub assignments: Vec<(String, String, (usize, usize))>,
    /// Paths named by `source` / `.` lines, as written.
    pub sources: Vec<String>,
    /// Set when a quote is never closed: nothing after it can be trusted and no edit should be built.
    pub problem: Option<String>,
}

fn is_blank(b: u8) -> bool {
    b == b' ' || b == b'\t'
}

fn is_space(b: u8) -> bool {
    is_blank(b) || b == b'\n' || b == b'\r'
}

/// Index just past the character that starts at `i` (backslash escapes may escape a multi-byte one).
fn after_char(text: &str, i: usize) -> usize {
    text[i..].chars().next().map_or(i + 1, |c| i + c.len_utf8())
}

fn line_of(text: &str, at: usize) -> usize {
    text.as_bytes()[..at.min(text.len())].iter().filter(|b| **b == b'\n').count() + 1
}

/// Read one shell-style word starting at `start`: quoted and unquoted pieces glued together,
/// backslash escapes (also backslash-newline) kept raw for [`expand`]. Returns the raw value, the
/// index after the word, and whether every quote was closed.
fn read_word(text: &str, start: usize) -> (String, usize, bool) {
    let b = text.as_bytes();
    let mut i = start;
    let mut value = String::new();
    while i < b.len() && !is_space(b[i]) {
        match b[i] {
            q @ (b'"' | b'\'') => {
                i += 1;
                let from = i;
                while i < b.len() && b[i] != q {
                    // Inside double quotes a backslash protects the next character, e.g. `\"`.
                    i = if q == b'"' && b[i] == b'\\' && i + 1 < b.len() { after_char(text, i + 1) } else { after_char(text, i) };
                }
                if i >= b.len() {
                    return (value, b.len(), false);
                }
                value.push_str(&text[from..i]);
                i += 1;
            }
            b'\\' if i + 1 < b.len() => {
                let end = after_char(text, i + 1);
                value.push_str(&text[i..end]);
                i = end;
            }
            _ => {
                let end = after_char(text, i);
                value.push_str(&text[i..end]);
                i = end;
            }
        }
    }
    (value, i, true)
}

/// Parse `text` the way Portage reads make.conf: words separated by blanks, so `export  A=1`,
/// `A = 1`, two assignments on a line, values with escaped blanks (`A=a\ b`), backslash-newline
/// continuations and quotes spanning lines are all one assignment each; `source file` is noted.
pub fn scan_text(text: &str) -> Scan {
    let b = text.as_bytes();
    let n = b.len();
    let mut out = Scan::default();
    let mut i = 0;
    let skip_line = |mut i: usize| {
        while i < n && b[i] != b'\n' {
            i += 1;
        }
        i
    };
    while i < n {
        if is_space(b[i]) {
            i += 1;
            continue;
        }
        if b[i] == b'#' {
            i = skip_line(i);
            continue;
        }
        let mut j = i;
        while j < n && (b[j].is_ascii_alphanumeric() || b[j] == b'_') {
            j += 1;
        }
        let key = &text[i..j];
        // `export NAME=…` and `source file` / `. file`: keywords only when followed by a blank.
        if (key == "export" || key == "source") && j < n && is_blank(b[j]) || (j == i && b[i] == b'.' && i + 1 < n && is_blank(b[i + 1])) {
            let after_keyword = if j == i { i + 1 } else { j };
            let mut k = after_keyword;
            while k < n && is_blank(b[k]) {
                k += 1;
            }
            if key == "export" {
                i = k;
                continue;
            }
            if k < n && !is_space(b[k]) && b[k] != b'#' {
                let (path, end, closed) = read_word(text, k);
                if closed {
                    out.sources.push(path);
                }
                i = end.max(k + 1);
            } else {
                i = k;
            }
            continue;
        }
        let mut k = j;
        while k < n && is_blank(b[k]) {
            k += 1;
        }
        if key.is_empty() || key.as_bytes()[0].is_ascii_digit() || k >= n || b[k] != b'=' {
            // Not an assignment: step over the word.
            let (_, end, _) = read_word(text, i);
            i = end.max(i + 1);
            continue;
        }
        k += 1;
        let mut v = k;
        while v < n && is_blank(b[v]) {
            v += 1;
        }
        if v >= n || b[v] == b'\n' || b[v] == b'\r' || (v > k && b[v] == b'#') {
            out.assignments.push((key.to_string(), String::new(), (i, k)));
            i = v;
            continue;
        }
        let (value, end, closed) = read_word(text, v);
        if !closed {
            out.problem.get_or_insert_with(|| format!("a quote opened on line {} is never closed", line_of(text, v)));
            i = skip_line(v);
            continue;
        }
        out.assignments.push((key.to_string(), value, (i, end)));
        i = end;
    }
    out
}

/// Parse assignments in `text`. Returns (name, raw value, span).
pub fn scan(text: &str) -> Vec<(String, String, (usize, usize))> {
    scan_text(text).assignments
}

pub fn load() -> MakeConf {
    let base = Path::new(MAKE_CONF);
    let files: Vec<PathBuf> = if base.is_dir() {
        let mut v: Vec<PathBuf> = std::fs::read_dir(base)
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_file() && !p.to_string_lossy().ends_with('~'))
            .collect();
        v.sort();
        v
    } else {
        vec![base.to_path_buf()]
    };
    let mut mc = MakeConf::default();
    let mut vars: HashMap<String, String> = HashMap::new();
    for f in files {
        if std::fs::symlink_metadata(&f).is_ok_and(|m| m.file_type().is_symlink()) {
            mc.symlinks.push(f.clone());
        }
        let text = match super::config::read_utf8(&f) {
            Ok(t) => t,
            Err(why) => {
                mc.unreadable.push((f, why));
                continue;
            }
        };
        let parsed = scan_text(&text);
        let dir = f.parent().unwrap_or(Path::new("/"));
        mc.sourced.extend(parsed.sources.iter().map(|p| if Path::new(p).is_absolute() { PathBuf::from(p) } else { dir.join(p) }));
        if let Some(problem) = parsed.problem {
            mc.problems.push((f.clone(), problem));
        }
        for (name, value, _) in parsed.assignments {
            let expanded = expand(&value, &vars).split_whitespace().collect::<Vec<_>>().join(" ");
            let self_ref = value.contains(&format!("${{{name}}}")) || value.contains(&format!("${name}"));
            vars.insert(name.clone(), expanded.clone());
            mc.assignments.push(Assignment { name, value, expanded, file: f.clone(), self_ref });
        }
        mc.files.push((f, text));
    }
    mc
}

/// Rewrite the last assignment of `name` in `text` (or append one). The whole assignment
/// is replaced, however many lines it spans; what follows it on the line (a comment) stays. A file
/// with an unclosed quote is refused: nothing after the quote can be told apart from its value.
pub fn set_var(text: &str, name: &str, value: &str) -> Result<String, String> {
    let scanned = scan_text(text);
    if let Some(problem) = scanned.problem {
        return Err(format!("make.conf cannot be edited here: {problem}. Fix it in the text editor first."));
    }
    let quoted = format!("{name}=\"{}\"", value.replace('\\', "\\\\").replace('"', "\\\""));
    let found = scanned.assignments.into_iter().rfind(|(n, v, _)| n == name && !v.contains(&format!("${{{name}}}")) && !v.contains(&format!("${name}")));
    Ok(match found {
        Some((_, _, (s, e))) => format!("{}{}{}", &text[..s], quoted, &text[e..]),
        None => {
            let mut t = text.to_string();
            if !t.is_empty() && !t.ends_with('\n') {
                t.push('\n');
            }
            t.push_str(&quoted);
            t.push('\n');
            t
        }
    })
}

/// Description of well-known variables shown as help text.
pub fn describe(name: &str) -> &'static str {
    match name {
        "COMMON_FLAGS" => "Compiler flags shared by CFLAGS/CXXFLAGS/FCFLAGS/FFLAGS in the default make.conf.",
        "CFLAGS" | "CXXFLAGS" => "C/C++ compiler optimisation flags. Keep them conservative (-O2 -pipe -march=native).",
        "LDFLAGS" => "Flags passed to the linker.",
        "MAKEOPTS" => "Options for make: -jN parallel jobs per package, -lN load limit. Rule of thumb: jobs = min(threads, RAM in GB / 2).",
        "EMERGE_DEFAULT_OPTS" => "Options added to every emerge command (e.g. --jobs, --keep-going, --with-bdeps=y).",
        "FEATURES" => "Portage features such as ccache, parallel-fetch, buildpkg or getbinpkg.",
        "USE" => "Global USE flags: optional features enabled or disabled for all packages.",
        "ACCEPT_KEYWORDS" => "Which branch to use system-wide: 'amd64' (stable) or '~amd64' (testing). Prefer per-package keywords.",
        "ACCEPT_LICENSE" => "Licenses accepted system-wide. '-* @FREE' accepts only free software; '*' accepts everything.",
        "VIDEO_CARDS" => "Graphics drivers to build support for (nvidia, amdgpu radeonsi, intel …).",
        "INPUT_DEVICES" => "Input drivers (libinput is the modern default).",
        "L10N" => "Languages for translations and documentation (e.g. en-US de).",
        "LINGUAS" => "Legacy translation selection; prefer L10N.",
        "GENTOO_MIRRORS" => "Mirrors used to download source archives (distfiles).",
        "CPU_FLAGS_X86" => "CPU instruction set flags; generate them with app-portage/cpuid2cpuflags.",
        "PORTAGE_NICENESS" => "Nice value for builds so the desktop stays responsive.",
        "PORTAGE_SCHEDULING_POLICY" => "CPU scheduling policy for builds (idle, batch, other).",
        "PYTHON_TARGETS" => "Python implementations packages are built for.",
        "PYTHON_SINGLE_TARGET" => "The Python implementation used by single-implementation packages.",
        "RUBY_TARGETS" => "Ruby implementations packages are built for.",
        "GRUB_PLATFORMS" => "Firmware platforms GRUB is built for (efi-64, pc …).",
        "RUSTFLAGS" => "Flags passed to rustc.",
        "PORT_LOGDIR" => "Directory where per-package build logs are saved.",
        "DISTDIR" => "Where downloaded source archives are stored.",
        "PKGDIR" => "Where binary packages are stored.",
        "PORTAGE_TMPDIR" => "Where packages are built (often a tmpfs).",
        "CCACHE_DIR" | "CCACHE_SIZE" => "ccache location and size (needs FEATURES=ccache).",
        "CHOST" => "The system's target triple. Do not change on an installed system.",
        "BINPKG_FORMAT" => "Binary package format (gpkg or xpak).",
        "LC_MESSAGES" => "Language of build output; C keeps it English for bug reports.",
        _ => "",
    }
}

pub const COMMON_FEATURES: &[(&str, &str)] = &[
    ("ccache", "Cache compiler output to speed up rebuilds"),
    ("parallel-fetch", "Download sources while building"),
    ("parallel-install", "Merge several packages at the same time"),
    ("buildpkg", "Create binary packages of everything built"),
    ("getbinpkg", "Download binary packages from binhosts"),
    ("binpkg-request-signature", "Require signed binary packages"),
    ("sandbox", "Restrict builds from writing outside their dirs"),
    ("usersandbox", "Sandbox also when building as portage user"),
    ("network-sandbox", "No network access during builds"),
    ("split-log", "Keep separate logs per phase"),
    ("split-elog", "Keep separate elog messages"),
    ("clean-logs", "Remove old build logs automatically"),
    ("compress-build-logs", "Compress build logs"),
    ("fail-clean", "Remove the build dir of failed builds"),
    ("keepwork", "Keep the work dir after a successful build"),
    ("noclean", "Keep everything after building"),
    ("nodoc", "Do not install documentation"),
    ("noinfo", "Do not install info pages"),
    ("noman", "Do not install man pages"),
    ("splitdebug", "Install separated debug information"),
    ("installsources", "Install sources for debugging"),
    ("test", "Run package test suites"),
    ("candy", "Fancy spinner"),
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_and_set() {
        let t = "# c\nCOMMON_FLAGS=\"-O2 \\\n -pipe\"\nCFLAGS=\"${COMMON_FLAGS}\"\nexport MAKEOPTS='-j4'\nUSE=\"a\"\nUSE=\"${USE} b\" # more\n";
        let s = scan(t);
        let names: Vec<_> = s.iter().map(|x| x.0.as_str()).collect();
        assert_eq!(names, vec!["COMMON_FLAGS", "CFLAGS", "MAKEOPTS", "USE", "USE"]);
        assert_eq!(s[2].1, "-j4");
        let t2 = set_var(t, "MAKEOPTS", "-j16 -l30").unwrap();
        assert!(t2.contains("MAKEOPTS=\"-j16 -l30\"\n"));
        assert!(!t2.contains("-j4"));
        assert!(t2.contains("export MAKEOPTS="), "export stays: {t2}");
        // self-referencing USE line is skipped, the plain one gets replaced
        let t3 = set_var(t, "USE", "x y").unwrap();
        assert!(t3.contains("USE=\"x y\"\nUSE=\"${USE} b\""));
        let t4 = set_var("A=1\n", "B", "2").unwrap();
        assert_eq!(t4, "A=1\nB=\"2\"\n");
        // A comment after the assignment is not part of it.
        assert_eq!(set_var("A=1 # keep me\n", "A", "2").unwrap(), "A=\"2\" # keep me\n");
    }

    #[test]
    fn expansion() {
        let mut v = HashMap::new();
        v.insert("A".to_string(), "-O2".to_string());
        assert_eq!(expand("${A} -pipe $A", &v), "-O2 -pipe -O2");
    }

    /// Edit the variable `name` of `text` and check there is exactly one assignment of it left and
    /// nothing of the old one (no stray continuation line, no leftover word).
    fn edit_once(text: &str, name: &str) -> String {
        let new = set_var(text, name, "NEW VALUE").unwrap();
        let after = scan(&new);
        assert_eq!(after.iter().filter(|a| a.0 == name).count(), 1, "{text:?} -> {new:?}");
        assert_eq!(after.iter().find(|a| a.0 == name).unwrap().1, "NEW VALUE", "{new:?}");
        assert!(scan_text(&new).problem.is_none(), "{new:?}");
        new
    }

    #[test]
    fn valid_shell_forms_are_one_assignment_each() {
        // `export` followed by any run of blanks.
        let s = scan("export  F=f\nexport\tG=g\n");
        assert_eq!(s.iter().map(|a| (a.0.as_str(), a.1.as_str())).collect::<Vec<_>>(), vec![("F", "f"), ("G", "g")]);
        assert_eq!(edit_once("export  F=f\n", "F"), "export  F=\"NEW VALUE\"\n");
        // An escaped blank is part of the value.
        let s = scan("J=foo\\ bar\nK=after\n");
        assert_eq!((s[0].0.as_str(), s[0].1.as_str(), s[1].1.as_str()), ("J", "foo\\ bar", "after"));
        assert_eq!(edit_once("J=foo\\ bar\nK=after\n", "J"), "J=\"NEW VALUE\"\nK=after\n");
        // Backslash-newline continues an unquoted value.
        let s = scan("A=a\\\nb\nC=c\n");
        assert_eq!(s.len(), 2);
        assert_eq!(edit_once("A=a\\\nb\nC=c\n", "A"), "A=\"NEW VALUE\"\nC=c\n");
        // A quoted value may span lines, and so may a continuation inside it.
        assert_eq!(edit_once("A=\"one\ntwo\"\nB=c\n", "A"), "A=\"NEW VALUE\"\nB=c\n");
        assert_eq!(edit_once("FEATURES=\"parallel-fetch \\\n    candy\"\nB=1\n", "FEATURES"), "FEATURES=\"NEW VALUE\"\nB=1\n");
        // Blanks around `=`, two assignments on a line, concatenated quoting.
        let s = scan("A = b\nC= d\nE =f\nX=1 Y=2\nZ=\"a\"b'c'd\n");
        assert_eq!(s.iter().map(|a| (a.0.as_str(), a.1.as_str())).collect::<Vec<_>>(), vec![("A", "b"), ("C", "d"), ("E", "f"), ("X", "1"), ("Y", "2"), ("Z", "abcd")]);
        assert_eq!(edit_once("X=1 Y=2\n", "X"), "X=\"NEW VALUE\" Y=2\n");
        assert_eq!(edit_once("A = b\n", "A"), "A=\"NEW VALUE\"\n");
        // An empty value, CRLF line ends, indentation, a quoted `#`.
        assert_eq!(scan("A=\nB=2\n").iter().map(|a| a.1.as_str()).collect::<Vec<_>>(), vec!["", "2"]);
        assert_eq!(scan("A=1\r\nB=2\r\n").iter().map(|a| a.1.as_str()).collect::<Vec<_>>(), vec!["1", "2"]);
        assert_eq!(scan("   A=1\n\tB=\"#2\"\n").iter().map(|a| a.1.as_str()).collect::<Vec<_>>(), vec!["1", "#2"]);
        // Comments are not assignments, a `#` inside a word is part of it.
        assert_eq!(scan("# A=1\nB=x#y\nC=2 # c\n").iter().map(|a| (a.0.as_str(), a.1.as_str())).collect::<Vec<_>>(), vec![("B", "x#y"), ("C", "2")]);
        // Multi-byte text before and after an escape does not break the byte arithmetic.
        let s = scan("A=\"héllo \\é wörld\"\nB=ü\\ü\n");
        assert_eq!(s.len(), 2);
        assert_eq!(edit_once("A=\"héllo\"\nB=ü\\ü x\n", "B"), "A=\"héllo\"\nB=\"NEW VALUE\" x\n");
    }

    #[test]
    fn source_lines_are_noted_not_mistaken_for_assignments() {
        let s = scan_text("A=1\nsource /etc/portage/extra.conf\n. rel/path.conf\nB=2\nsourced=3\nsource\n");
        assert_eq!(s.sources, vec!["/etc/portage/extra.conf", "rel/path.conf"]);
        assert_eq!(s.assignments.iter().map(|a| a.0.as_str()).collect::<Vec<_>>(), vec!["A", "B", "sourced"]);
        assert!(s.problem.is_none());
        let s = scan_text("source \"/with space/x.conf\" # c\nexport\nexport=1\n");
        assert_eq!(s.sources, vec!["/with space/x.conf"]);
        assert_eq!(s.assignments.iter().map(|a| a.0.as_str()).collect::<Vec<_>>(), vec!["export"], "`export=1` is a variable named export");
    }

    #[test]
    fn an_unclosed_quote_is_reported_and_blocks_edits() {
        let t = "A=1\nB=\"never closed\nC=3\n";
        let s = scan_text(t);
        assert_eq!(s.problem.as_deref(), Some("a quote opened on line 2 is never closed"));
        assert_eq!(s.assignments[0].0, "A", "what is before the quote still reads: {:?}", s.assignments);
        assert!(!s.assignments.iter().any(|a| a.0 == "B"), "the broken assignment itself is not one");
        let e = set_var(t, "A", "9").unwrap_err();
        assert!(e.contains("never closed") && e.contains("text editor"), "{e}");
        // Nothing panics on odd input.
        for odd in ["", "=", "A", "A=\"", "A='", "A=\\", "\\", "\"", "A=\"\\", "export", ". ", "#", "A=b\\", "é=1", "A=é\\"] {
            let _ = scan_text(odd);
        }
    }

    /// Variables as `load()` expands them.
    fn evaluated(text: &str) -> std::collections::BTreeMap<String, String> {
        let mut vars: HashMap<String, String> = HashMap::new();
        for (name, value, _) in scan(text) {
            let expanded = expand(&value, &vars).split_whitespace().collect::<Vec<_>>().join(" ");
            vars.insert(name, expanded);
        }
        vars.into_iter().collect()
    }

    /// The same texts through `portage.util.getconfig` (tests/fixtures/makeconf.json).
    #[test]
    fn agrees_with_portage_on_the_fixture_cases() {
        let rows: Vec<serde_json::Value> = serde_json::from_str(include_str!("../../tests/fixtures/makeconf.json")).unwrap();
        assert!(rows.len() > 20);
        let squash = |m: &std::collections::BTreeMap<String, String>| -> std::collections::BTreeMap<String, String> { m.iter().map(|(k, v)| (k.clone(), v.split_whitespace().collect::<Vec<_>>().join(" "))).collect() };
        let mut wrong = Vec::new();
        for row in &rows {
            let text = row["text"].as_str().unwrap();
            let want: std::collections::BTreeMap<String, String> = row["vars"].as_object().unwrap().iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string())).collect();
            let got = evaluated(text);
            if squash(&got) != squash(&want) {
                wrong.push(format!("{text:?}\n   ours:    {got:?}\n   portage: {want:?}"));
            }
        }
        assert!(wrong.is_empty(), "{} of {} differ:\n{}", wrong.len(), rows.len(), wrong.join("\n"));
    }
}
