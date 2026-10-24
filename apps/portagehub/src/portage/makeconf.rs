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
    /// Byte range of the whole assignment in the file text.
    pub span: (usize, usize),
    pub self_ref: bool,
}

#[derive(Debug, Clone, Default)]
pub struct MakeConf {
    pub files: Vec<(PathBuf, String)>,
    pub assignments: Vec<Assignment>,
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

/// Parse assignments in `text`. Returns (name, raw value, span).
pub fn scan(text: &str) -> Vec<(String, String, (usize, usize))> {
    let bytes = text.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        // At line start: skip whitespace
        let line_start = i;
        while i < bytes.len() && (bytes[i] == b' ' || bytes[i] == b'\t') {
            i += 1;
        }
        if i >= bytes.len() {
            break;
        }
        if bytes[i] == b'#' || bytes[i] == b'\n' {
            while i < bytes.len() && bytes[i] != b'\n' {
                i += 1;
            }
            i += 1;
            continue;
        }
        if text[i..].starts_with("export ") {
            i += 7;
        }
        let name_start = i;
        while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
            i += 1;
        }
        if i == name_start || i >= bytes.len() || bytes[i] != b'=' {
            while i < bytes.len() && bytes[i] != b'\n' {
                i += 1;
            }
            i += 1;
            continue;
        }
        let name = text[name_start..i].to_string();
        i += 1;
        let mut value = String::new();
        // Concatenation of quoted/unquoted segments until whitespace/newline.
        while i < bytes.len() && bytes[i] != b'\n' && bytes[i] != b' ' && bytes[i] != b'\t' {
            match bytes[i] {
                b'"' => {
                    i += 1;
                    let s = i;
                    while i < bytes.len() && bytes[i] != b'"' {
                        if bytes[i] == b'\\' {
                            i += 1;
                        }
                        i += 1;
                    }
                    value.push_str(&text[s..i.min(bytes.len())]);
                    i += 1;
                }
                b'\'' => {
                    i += 1;
                    let s = i;
                    while i < bytes.len() && bytes[i] != b'\'' {
                        i += 1;
                    }
                    value.push_str(&text[s..i.min(bytes.len())]);
                    i += 1;
                }
                _ => {
                    let s = i;
                    while i < bytes.len() && !matches!(bytes[i], b'\n' | b' ' | b'\t' | b'"' | b'\'') {
                        i += 1;
                    }
                    value.push_str(&text[s..i]);
                }
            }
        }
        // rest of the line (trailing comment)
        while i < bytes.len() && bytes[i] != b'\n' {
            i += 1;
        }
        let end = i.min(bytes.len());
        out.push((name, value, (line_start, end)));
        i += 1;
    }
    out
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
        let text = std::fs::read_to_string(&f).unwrap_or_default();
        for (name, value, span) in scan(&text) {
            let expanded = expand(&value, &vars).split_whitespace().collect::<Vec<_>>().join(" ");
            let self_ref = value.contains(&format!("${{{name}}}")) || value.contains(&format!("${name}"));
            vars.insert(name.clone(), expanded.clone());
            mc.assignments.push(Assignment { name, value, expanded, file: f.clone(), span, self_ref });
        }
        mc.files.push((f, text));
    }
    mc
}

/// Rewrite the last assignment of `name` in `text` (or append one).
pub fn set_var(text: &str, name: &str, value: &str) -> String {
    let quoted = format!("{name}=\"{}\"", value.replace('\\', "\\\\").replace('"', "\\\""));
    let found = scan(text).into_iter().filter(|(n, v, _)| n == name && !v.contains(&format!("${{{name}}}"))).last();
    match found {
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
    }
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
        let t2 = set_var(t, "MAKEOPTS", "-j16 -l30");
        assert!(t2.contains("MAKEOPTS=\"-j16 -l30\"\n"));
        assert!(!t2.contains("-j4"));
        // self-referencing USE line is skipped, the plain one gets replaced
        let t3 = set_var(t, "USE", "x y");
        assert!(t3.contains("USE=\"x y\"\nUSE=\"${USE} b\""));
        let t4 = set_var("A=1\n", "B", "2");
        assert_eq!(t4, "A=1\nB=\"2\"\n");
    }

    #[test]
    fn expansion() {
        let mut v = HashMap::new();
        v.insert("A".to_string(), "-O2".to_string());
        assert_eq!(expand("${A} -pipe $A", &v), "-O2 -pipe -O2");
    }
}
