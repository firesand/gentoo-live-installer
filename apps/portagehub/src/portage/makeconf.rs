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

/// Portage skips files named `.*` or `*~` in a make.conf directory.
pub fn portage_skips(p: &Path) -> bool {
    let n = p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    n.starts_with('.') || n.ends_with('~')
}

/// Is `p` the make.conf file or a file inside a make.conf directory?
pub fn is_make_conf(p: &Path) -> bool {
    p == Path::new(MAKE_CONF) || p.parent() == Some(Path::new(MAKE_CONF))
}

pub fn load() -> MakeConf {
    let base = Path::new(MAKE_CONF);
    let files: Vec<PathBuf> = if base.is_dir() {
        let mut v: Vec<PathBuf> =
            std::fs::read_dir(base).into_iter().flatten().flatten().map(|e| e.path()).filter(|p| p.is_file() && !portage_skips(p)).collect();
        v.sort();
        v
    } else {
        vec![base.to_path_buf()]
    };
    let mut mc = MakeConf::default();
    let mut vars: HashMap<String, String> = HashMap::new();
    for f in files {
        let text = std::fs::read_to_string(&f).unwrap_or_default();
        for (name, value, _) in scan(&text) {
            let expanded = expand(&value, &vars).split_whitespace().collect::<Vec<_>>().join(" ");
            let self_ref = value.contains(&format!("${{{name}}}")) || value.contains(&format!("${name}"));
            vars.insert(name.clone(), expanded.clone());
            mc.assignments.push(Assignment { name, value, expanded, file: f.clone(), self_ref });
        }
        mc.files.push((f, text));
    }
    mc
}

/// Rewrite the last assignment of `name` in `text` (or append one).
pub fn set_var(text: &str, name: &str, value: &str) -> String {
    let quoted = format!("{name}=\"{}\"", value.replace('\\', "\\\\").replace('"', "\\\""));
    let found = scan(text).into_iter().rfind(|(n, v, _)| n == name && !v.contains(&format!("${{{name}}}")));
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

/// Assignment spans of `name` in `text`, in order.
fn spans(text: &str, name: &str) -> Vec<(usize, usize)> {
    scan(text).into_iter().filter(|(n, _, _)| n == name).map(|(_, _, s)| s).collect()
}

/// Replace every assignment of `name` with a comment (`note`, when given) —
/// the variable goes back to the profile default.
pub fn remove_var(text: &str, name: &str, note: Option<&str>) -> String {
    let mut out = text.to_string();
    for (s, e) in spans(text, name).into_iter().rev() {
        let line_end = if out[e..].starts_with('\n') { e + 1 } else { e };
        let repl = note.map(|n| format!("# {n}\n")).unwrap_or_default();
        out.replace_range(s..line_end, &repl);
    }
    out
}

/// Rename every assignment of `old` to `new` (deprecated → current names).
pub fn rename_var(text: &str, old: &str, new: &str) -> String {
    let mut out = text.to_string();
    for (s, e) in spans(text, old).into_iter().rev() {
        let seg = &out[s..e];
        if let Some(i) = seg.find(&format!("{old}=")) {
            out.replace_range(s + i..s + i + old.len(), new);
        }
    }
    out
}

/// Edit the words of every assignment of `name` in place: `f` gets each word
/// (a run of characters without whitespace, quotes or line continuation) and
/// returns its replacement — `None` keeps it, `Some("")` drops it. Layout,
/// continuations, comments and `${VAR}` references are kept.
pub fn edit_words(text: &str, name: &str, f: &dyn Fn(&str) -> Option<String>) -> String {
    let mut out = text.to_string();
    for (s, e) in spans(text, name).into_iter().rev() {
        let seg = out[s..e].to_string();
        let Some(eq) = seg.find('=') else { continue };
        // Words of the value, up to an unquoted comment.
        let b = seg.as_bytes();
        let mut words: Vec<(usize, usize)> = Vec::new();
        let (mut i, mut quote) = (eq + 1, None::<u8>);
        while i < b.len() {
            let c = b[i];
            match c {
                b'"' | b'\'' => {
                    quote = if quote == Some(c) {
                        None
                    } else if quote.is_none() {
                        Some(c)
                    } else {
                        quote
                    };
                    i += 1;
                }
                b'#' if quote.is_none() && (i == 0 || b[i - 1].is_ascii_whitespace()) => break,
                b'\\' if i + 1 < b.len() && b[i + 1] == b'\n' => i += 2,
                c if c.is_ascii_whitespace() => i += 1,
                _ => {
                    let st = i;
                    while i < b.len() && !b[i].is_ascii_whitespace() && b[i] != b'"' && b[i] != b'\'' && !(b[i] == b'\\' && b.get(i + 1) == Some(&b'\n')) {
                        i += 1;
                    }
                    words.push((st, i));
                }
            }
        }
        let mut seg2 = seg.clone();
        for (ws, we) in words.into_iter().rev() {
            let Some(r) = f(&seg[ws..we]) else { continue };
            if r.is_empty() {
                // Drop the word and one separating space.
                let (mut ds, mut de) = (ws, we);
                if seg2[de..].starts_with(' ') {
                    de += 1;
                } else if ds > 0 && seg2[..ds].ends_with(' ') {
                    ds -= 1;
                }
                seg2.replace_range(ds..de, "");
            } else {
                seg2.replace_range(ws..we, &r);
            }
        }
        out.replace_range(s..e, &seg2);
    }
    out
}

/// Description of well-known variables shown as help text.
pub fn describe(name: &str) -> &'static str {
    match name {
        "COMMON_FLAGS" => {
            "Compiler flags shared by CFLAGS, CXXFLAGS, FCFLAGS and FFLAGS (a Handbook convention). The Gentoo wiki's sane value is \"-march=native -O2 -pipe\": -O3 is not recommended outside test suites and -Ofast/-ffast-math never system-wide. Changing them only affects packages built afterwards."
        }
        "CFLAGS" | "CXXFLAGS" | "FCFLAGS" | "FFLAGS" => {
            "Compiler optimisation flags, normally just \"${COMMON_FLAGS}\". Hardening (PIE, stack protector, FORTIFY) already comes from the toolchain and profile — do not add it by hand."
        }
        "LDFLAGS" => {
            "Linker flags. Not incremental: setting it replaces the profile's -Wl,-O1 -Wl,--as-needed -Wl,-z,pack-relative-relocs, so write LDFLAGS=\"${LDFLAGS} …\" or leave it unset."
        }
        "MAKEOPTS" => {
            "Options for make: -jN parallel compile jobs per package, -lN load limit. Gentoo's rule: jobs = the smaller of CPU threads and RAM ÷ 2 GiB; -l slightly above the thread count. Unset, Portage uses the thread count."
        }
        "EMERGE_DEFAULT_OPTS" => {
            "Options added to every emerge. --jobs N builds N packages at once — that multiplies with MAKEOPTS -j, so pair it with --load-average."
        }
        "FEATURES" => {
            "Portage features. Incremental: make.conf only adds (or removes with -name) on top of the defaults, which already include sandbox, userpriv, network-sandbox, strict and protect-owned — keep those."
        }
        "USE" => "Global USE flags. Incremental on top of the profile. Prefer package.use for flags that matter to only a few packages; never put -* here.",
        "ACCEPT_KEYWORDS" => {
            "Branch system-wide: the profile already sets stable (amd64). ~amd64 makes the whole system testing and is hard to undo — prefer package.accept_keywords per package."
        }
        "ACCEPT_LICENSE" => {
            "Licenses accepted system-wide; not incremental. The default is \"-* @FREE\". Groups are listed in profiles/license_groups; accept single EULAs per package in package.license."
        }
        "VIDEO_CARDS" => {
            "Graphics drivers to build support for. A USE_EXPAND variable: in make.conf it replaces the profile default, so the wiki prefers package.use (*/* VIDEO_CARDS: -* nvidia)."
        }
        "INPUT_DEVICES" => "Input drivers (libinput is the profile default). A USE_EXPAND variable — prefer package.use.",
        "L10N" => "Languages for translations and documentation as BCP-47 tags (en-US, pt-BR). A USE_EXPAND variable — prefer package.use.",
        "LINGUAS" => "Old translation selection that makes packages skip locales; use L10N instead.",
        "GENTOO_MIRRORS" => {
            "Mirrors for source archives (distfiles). The default is plain http; mirrorselect -S picks https mirrors. Downloads are checked against Manifest digests either way."
        }
        "CPU_FLAGS_X86" => {
            "Instruction sets your CPU supports, used to enable hand-written assembly in ebuilds (different from -march). Generate with app-portage/cpuid2cpuflags into package.use/00cpu-flags."
        }
        "PORTAGE_NICENESS" => "Nice value for builds. Unnecessary with PORTAGE_SCHEDULING_POLICY=\"idle\".",
        "PORTAGE_SCHEDULING_POLICY" => "CPU scheduling policy for builds; \"idle\" keeps the desktop responsive (Portage 3.0.35+).",
        "PORTAGE_IONICE_COMMAND" => "Command Portage runs (as root) to lower build I/O priority, e.g. \"ionice -c 3 -p \\${PID}\".",
        "PYTHON_TARGETS" => "Python versions packages are built for. The profile keeps this current — pinning it here blocks upgrades.",
        "PYTHON_SINGLE_TARGET" => "The Python version for single-implementation packages. Managed by the profile.",
        "RUBY_TARGETS" => "Ruby versions packages are built for. Managed by the profile.",
        "LLVM_TARGETS" | "LLVM_SLOT" => "LLVM backends/slot; the tree enables all targets and ebuilds pick the slot — overriding is not recommended.",
        "GRUB_PLATFORMS" => "Firmware platforms GRUB is built for: efi-64 for UEFI, pc for BIOS.",
        "RUSTFLAGS" => {
            "Flags passed to rustc. The wiki advises leaving it unset; Cargo's release profile already uses opt-level 3 and Portage strips binaries itself."
        }
        "PORT_LOGDIR" => "Deprecated name of PORTAGE_LOGDIR.",
        "PORTAGE_LOGDIR" => "Directory where per-package build logs are saved.",
        "DISTDIR" => "Where downloaded source archives are stored (default /var/cache/distfiles). Must be writable only by root/portage.",
        "PKGDIR" => "Where binary packages are stored (default /var/cache/binpkgs). Must be writable only by root/portage.",
        "PORTAGE_TMPDIR" => "Where packages are built (default /var/tmp). A tmpfs speeds little with -pipe; huge packages need a disk-backed override.",
        "CCACHE_DIR" | "CCACHE_SIZE" => "ccache location and size. The man page warns global ccache causes build failures; use it per package.",
        "CHOST" => "The system's target triple. Never set it in make.conf on 23.0 profiles; changing it can break the whole system.",
        "CBUILD" | "CTARGET" => "Cross-compilation triples. Leave alone on a normal system.",
        "BINPKG_FORMAT" => "Binary package format; gpkg (the default) supports signatures.",
        "BINPKG_COMPRESS" => "Compression for binary packages (zstd by default).",
        "LC_MESSAGES" => "Language of build output; C or C.utf8 keeps it English for bug reports.",
        "FETCHCOMMAND" | "RESUMECOMMAND" | "FETCH_WRAPPER" => "Download command Portage runs (as root) for distfiles.",
        "PORTAGE_ELOG_SYSTEM" | "PORTAGE_ELOG_CLASSES" => "Where build messages (elog) go and which classes are kept.",
        "PORTDIR" | "PORTDIR_OVERLAY" | "SYNC" => "Deprecated: repository locations and sync settings belong in /etc/portage/repos.conf.",
        "PORTAGE_BINHOST" => "Deprecated: binary package hosts belong in /etc/portage/binrepos.conf.",
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
    fn word_edits_keep_layout() {
        let t = "COMMON_FLAGS=\"-march=alderlake -O3 -pipe \\\n    -fomit-frame-pointer -flto ${WARNING_FLAGS}\" # mine\nCFLAGS=\"${COMMON_FLAGS}\"\n";
        let t2 = edit_words(t, "COMMON_FLAGS", &|w| (w == "-O3").then(|| "-O2".to_string()));
        assert_eq!(t2, t.replace("-O3", "-O2"));
        let t3 = edit_words(t, "COMMON_FLAGS", &|w| (w == "-flto").then(String::new));
        assert!(t3.contains("-fomit-frame-pointer ${WARNING_FLAGS}\" # mine"), "{t3}");
        assert!(scan(&t3).len() == 2);
        // A word inside the trailing comment is not touched.
        let t4 = edit_words("A=\"x\" # x\n", "A", &|w| (w == "x").then(|| "y".into()));
        assert_eq!(t4, "A=\"y\" # x\n");
    }

    #[test]
    fn remove_and_rename() {
        let t = "A=1\nVIDEO_CARDS=\"nvidia\"\nB=2\nPORT_LOGDIR=\"/var/log/portage\"\n";
        let r = remove_var(t, "VIDEO_CARDS", Some("VIDEO_CARDS moved to package.use"));
        assert_eq!(r, "A=1\n# VIDEO_CARDS moved to package.use\nB=2\nPORT_LOGDIR=\"/var/log/portage\"\n");
        assert_eq!(remove_var(t, "B", None), "A=1\nVIDEO_CARDS=\"nvidia\"\nPORT_LOGDIR=\"/var/log/portage\"\n");
        assert!(rename_var(t, "PORT_LOGDIR", "PORTAGE_LOGDIR").contains("\nPORTAGE_LOGDIR=\"/var/log/portage\"\n"));
        assert!(is_make_conf(Path::new("/etc/portage/make.conf")) && is_make_conf(Path::new("/etc/portage/make.conf/01-flags")));
        assert!(!is_make_conf(Path::new("/etc/portage/package.use/x")));
        assert!(portage_skips(Path::new("/etc/portage/make.conf/.hidden")) && !portage_skips(Path::new("/etc/portage/make.conf/a.bak")));
    }

    #[test]
    fn expansion() {
        let mut v = HashMap::new();
        v.insert("A".to_string(), "-O2".to_string());
        assert_eq!(expand("${A} -pipe $A", &v), "-O2 -pipe -O2");
    }
}
