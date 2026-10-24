//! The emerge command lines the helper accepts.
//!
//! Portage parses its options with argparse, which accepts any unambiguous
//! prefix of a long option: `--config-r tmp/x`, `--sys=tmp/x` and `--pref=tmp/x`
//! set `--config-root`, `--sysroot` and `--prefix`. A deny-list of exact names
//! therefore cannot keep a privileged emerge away from a configuration tree or
//! install root chosen by the caller. The helper accepts only the options listed
//! here, spelled exactly, with a value of the shape that option takes, and only
//! package atoms and `@sets` as targets (never a path such as `tmp/x.tbz2`,
//! which emerge would install as a binary package).
//!
//! The table covers everything PortageHub generates: the option specs of the
//! GUI (`portage::emerge::SPECS`), its actions and the fixed run flags. A test in
//! the GUI crate fails when the two drift apart.

use regex::Regex;
use std::sync::OnceLock;

/// What may follow `=` in an option.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Value {
    /// A bare switch (`--update`).
    None,
    /// Bare, or `=y` / `=n` (`--with-bdeps=y`).
    YesNo,
    /// `=N`; with `plain`, the bare option is allowed too (`--jobs`).
    Number { plain: bool },
    /// One package atom (`--exclude=dev-libs/foo`).
    Atom,
    /// Exactly one of a fixed list (`--color=n`).
    Choice(&'static [&'static str]),
}

#[derive(Debug, Clone, Copy)]
pub struct Opt {
    pub name: &'static str,
    pub value: Value,
}

const fn sw(name: &'static str) -> Opt {
    Opt { name, value: Value::None }
}
const fn yn(name: &'static str) -> Opt {
    Opt { name, value: Value::YesNo }
}
const fn num(name: &'static str, plain: bool) -> Opt {
    Opt { name, value: Value::Number { plain } }
}
const fn atom(name: &'static str) -> Opt {
    Opt { name, value: Value::Atom }
}

/// Every long option the helper passes to emerge, spelled in full.
pub const LONG_OPTS: &[Opt] = &[
    // PortageHub's option specs.
    sw("--update"),
    sw("--deep"),
    sw("--newuse"),
    sw("--changed-use"),
    yn("--with-bdeps"),
    sw("--oneshot"),
    sw("--noreplace"),
    sw("--emptytree"),
    yn("--changed-deps"),
    yn("--changed-slot"),
    yn("--with-test-deps"),
    sw("--newrepo"),
    sw("--selective"),
    sw("--update-if-installed"),
    atom("--exclude"),
    atom("--reinstall-atoms"),
    atom("--useoldpkg-atoms"),
    num("--backtrack", false),
    sw("--complete-graph"),
    yn("--complete-graph-if-new-use"),
    yn("--complete-graph-if-new-ver"),
    yn("--rebuild-if-new-slot"),
    sw("--rebuild-if-new-rev"),
    sw("--rebuild-if-new-ver"),
    sw("--rebuild-if-unbuilt"),
    yn("--dynamic-deps"),
    yn("--ignore-built-slot-operator-deps"),
    sw("--nodeps"),
    sw("--onlydeps"),
    yn("--onlydeps-with-rdeps"),
    sw("--verbose-conflicts"),
    sw("--tree"),
    yn("--autounmask"),
    yn("--autounmask-use"),
    yn("--autounmask-license"),
    yn("--autounmask-keep-keywords"),
    yn("--autounmask-keep-masks"),
    yn("--autounmask-backtrack"),
    yn("--autounmask-unrestricted-atoms"),
    yn("--autounmask-continue"),
    num("--jobs", true),
    num("--load-average", false),
    sw("--keep-going"),
    sw("--fetchonly"),
    sw("--fetch-all-uri"),
    yn("--quiet-build"),
    sw("--quiet-fail"),
    sw("--skipfirst"),
    sw("--usepkg"),
    sw("--usepkgonly"),
    sw("--getbinpkg"),
    sw("--getbinpkgonly"),
    sw("--buildpkg"),
    sw("--buildpkgonly"),
    yn("--binpkg-respect-use"),
    yn("--binpkg-changed-deps"),
    yn("--rebuilt-binaries"),
    atom("--usepkg-exclude"),
    atom("--buildpkg-exclude"),
    sw("--verbose"),
    sw("--quiet"),
    sw("--unordered-display"),
    sw("--verbose-slot-rebuilds"),
    sw("--noconfmem"),
    sw("--ignore-default-opts"),
    sw("--nospinner"),
    Opt { name: "--color", value: Value::Choice(&["y", "n"]) },
    // Actions (`portage::emerge::Action::flag`).
    sw("--unmerge"),
    sw("--depclean"),
    sw("--prune"),
    sw("--deselect"),
    sw("--sync"),
    sw("--resume"),
    sw("--info"),
    sw("--config"),
    sw("--regen"),
    sw("--metadata"),
    sw("--check-news"),
    // Fixed run flags (previews and real runs).
    sw("--pretend"),
    yn("--ask"),
    // Harmless, and what the helper's own smoke test runs.
    sw("--version"),
];

/// Short switches (`-uDN`): the one-letter forms of the switches above.
pub const SHORT_FLAGS: &str = "uDNU1neOotfFkKgGbBvq";

/// Arguments ending like this make emerge treat them as a file to install or
/// build, whatever else they look like.
pub const FILE_SUFFIXES: &[&str] = &[".tbz2", ".xpak", ".gpkg.tar", ".ebuild"];

const MAX_ARG_LEN: usize = 1024;

pub fn long_opt(name: &str) -> Option<&'static Opt> {
    LONG_OPTS.iter().find(|o| o.name == name)
}

fn atom_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(concat!(
            r"^!{0,2}",                                // blockers
            r"(?:>=|<=|>|<|=|~)?",                     // version operator
            r"(?:[A-Za-z0-9_][A-Za-z0-9_+.-]*/)?",     // category (`vim` alone is a valid target)
            r"[A-Za-z0-9_][A-Za-z0-9_+-]*",            // package name: no `.`, so never a file name
            r"(?:-[0-9]+(?:\.[0-9]+)*[a-z]?(?:_(?:alpha|beta|pre|rc|p)[0-9]*)*(?:-r[0-9]+)?\*?)?", // -version, `*` for =cat/pkg-1.2*
            r"(?::[A-Za-z0-9_+.*-]*(?:/[A-Za-z0-9_+.*-]*)?=?)?", // :slot/subslot=
            r"(?:::[A-Za-z0-9_-]+)?",                  // ::repo
            r"(?:\[[A-Za-z0-9_@+,()?!=*.-]*\])?$",     // [use,-deps]
        ))
        .expect("atom grammar")
    })
}

fn set_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^@[A-Za-z0-9_][A-Za-z0-9_.+-]*$").expect("set grammar"))
}

fn is_file_like(s: &str) -> bool {
    FILE_SUFFIXES.iter().any(|x| s.ends_with(x)) || s.starts_with('/') || s.starts_with("./") || s.starts_with("../")
}

/// Is `s` a package atom as emerge accepts it on the command line?
pub fn is_atom(s: &str) -> bool {
    s.len() <= MAX_ARG_LEN && !is_file_like(s) && atom_re().is_match(s)
}

/// A target: an `@set` or an atom, never a path.
pub fn check_target(s: &str) -> Result<(), String> {
    if s.len() <= MAX_ARG_LEN && !is_file_like(s) && (set_re().is_match(s) || atom_re().is_match(s)) {
        return Ok(());
    }
    Err(format!("emerge target not allowed: {s} (use a package atom such as dev-libs/foo or a set such as @world)"))
}

fn bad_value(arg: &str, expected: &str) -> String {
    format!("emerge option value not allowed: {arg} (expected {expected})")
}

fn check_value(opt: &Opt, arg: &str, value: Option<&str>) -> Result<(), String> {
    match (opt.value, value) {
        (Value::None, None) | (Value::YesNo, None) | (Value::Number { plain: true }, None) => Ok(()),
        (Value::None, Some(_)) => Err(bad_value(arg, "no value")),
        (Value::YesNo, Some("y" | "n")) => Ok(()),
        (Value::YesNo, Some(_)) => Err(bad_value(arg, "y or n")),
        (Value::Number { .. }, Some(v)) => {
            let digits = |s: &str, max: usize| !s.is_empty() && s.len() <= max && s.bytes().all(|b| b.is_ascii_digit());
            let ok = match v.split_once('.') {
                Some((int, frac)) => digits(int, 6) && digits(frac, 3),
                None => digits(v, 6),
            };
            if ok { Ok(()) } else { Err(bad_value(arg, "a number")) }
        }
        (Value::Number { plain: false }, None) => Err(bad_value(arg, "a number: write NAME=VALUE")),
        (Value::Atom, Some(v)) if is_atom(v) => Ok(()),
        (Value::Atom, _) => Err(bad_value(arg, "a package atom: write NAME=ATOM")),
        (Value::Choice(list), Some(v)) if list.contains(&v) => Ok(()),
        (Value::Choice(list), _) => Err(bad_value(arg, &format!("one of {}: write NAME=VALUE", list.join(", ")))),
    }
}

/// Validate the arguments (without the program name) of an `emerge` run.
pub fn check(args: &[String]) -> Result<(), String> {
    for a in args {
        if a.len() > MAX_ARG_LEN || a.contains('\0') {
            return Err("emerge argument is too long".into());
        }
        if let Some(long) = a.strip_prefix("--") {
            // `--` alone would end option parsing and make every later token a target.
            let (name, value) = match long.split_once('=') {
                Some((n, v)) => (n, Some(v)),
                None => (long, None),
            };
            let Some(opt) = long_opt(&format!("--{name}")) else {
                return Err(format!("emerge option not allowed: {a}"));
            };
            check_value(opt, a, value)?;
        } else if let Some(shorts) = a.strip_prefix('-') {
            if shorts.is_empty() || !shorts.chars().all(|c| SHORT_FLAGS.contains(c)) {
                return Err(format!("emerge option not allowed: {a}"));
            }
        } else {
            check_target(a)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn table_is_sane() {
        for (i, o) in LONG_OPTS.iter().enumerate() {
            assert!(o.name.starts_with("--") && o.name.len() > 2, "{}", o.name);
            assert!(!LONG_OPTS[..i].iter().any(|p| p.name == o.name), "duplicate {}", o.name);
        }
        for dangerous in ["--root", "--config-root", "--sysroot", "--prefix", "--quickpkg-direct-root", "--pkg-format", "--accept-properties", "--rebuilt-binaries-timestamp"] {
            assert!(long_opt(dangerous).is_none(), "{dangerous}");
        }
    }

    #[test]
    fn atoms() {
        for ok in [
            "dev-lang/python",
            "vim",
            "world",
            "=sys-kernel/x-1.0",
            "=sys-kernel/gentoo-kernel-6.12.8-r1",
            "~dev-lang/python-3.11.5",
            ">=dev-lang/python-3.12.1-r2:3.12/3.12=::gentoo[threads(+),-tk]",
            "=app-misc/foo-1.2*",
            "dev-libs/libsodium:=",
            "dev-libs/libsodium:*",
            "app-misc/foo::guru",
            "=net-misc/foo-1.0_rc1_p2",
            "@world",
            "@preserved-rebuild",
            "@kernels-keep",
            "@x11-module-rebuild",
            "virtual/dist-kernel",
            "dev-python/pyyaml",
            "dev-cpp/gtk++",
        ] {
            assert!(check_target(ok).is_ok(), "{ok}");
        }
        for bad in [
            "",
            "tmp/evil.tbz2",
            "tmp/x.ebuild",
            "evil.tbz2",
            "cat/pkg:x.tbz2",
            "cat/pkg:x.gpkg.tar",
            "cat/pkg[x].xpak",
            "/tmp/x",
            "./x",
            "../x",
            "~/x",
            "~",
            "a/b/c",
            "a b",
            "@world.ebuild",
            "@",
            "-uDN",
            "=",
            "cat/",
            "/",
            "tmp/evil-1.0.tbz2",
            "app-misc/foo\n",
        ] {
            assert!(check_target(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn abbreviations_and_unknown_options_are_refused() {
        // argparse resolves every one of these to a denied option (checked against Portage 3.0.82 parse_opts).
        for bad in [
            "--config-=tmp/x",
            "--config-r",
            "--config-ro",
            "--config-roo",
            "--config-root=tmp/x",
            "--sys=tmp/x",
            "--sysr=tmp/x",
            "--sysro=tmp/x",
            "--sysroo=tmp/x",
            "--sysroot=tmp/x",
            "--pref=tmp/x",
            "--prefi=tmp/x",
            "--prefix=tmp/x",
            "--root=tmp/x",
            "--roo",
            "--",
            "-",
            "-p",
            "-j4",
            "--ex=a/b",
            "--update=y",
            "--jobs=4x",
            "--backtrack",
            "--exclude",
            "--exclude=/tmp/x",
            "--exclude=tmp/x.tbz2",
            "--color",
            "--color=maybe",
            "--with-bdeps=maybe",
            "--ask=/x",
        ] {
            assert!(check(&v(&[bad])).is_err(), "{bad}");
        }
        // The value as a separate token is a target, and a path is no target.
        assert!(check(&v(&["--config-root", "tmp/x"])).is_err());
        assert!(check(&v(&["--jobs", "tmp/x.tbz2"])).is_err());
        assert!(check(&v(&["--", "x"])).is_err());
    }

    #[test]
    fn what_the_gui_sends_is_accepted() {
        for ok in [
            v(&["--color=n", "-uDN", "--with-bdeps=y", "--exclude=app-misc/foo", "@world"]),
            v(&["--pretend", "--verbose", "--color=n", "--nospinner", "--ask=n", "-uDN", "--with-bdeps=y", "@world"]),
            v(&["--config", "=sys-kernel/x-1.0"]),
            v(&["--config", "--ask=n", "=sys-kernel/gentoo-kernel-6.12.8"]),
            v(&["--noreplace", "--ask=n", "@kernels-keep"]),
            v(&["--deselect", "--ask=n", "dev-libs/foo"]),
            v(&["--pretend", "--depclean", "--color=n", "--nospinner", "--ask=n"]),
            v(&["--depclean"]),
            v(&["--info"]),
            v(&["--regen"]),
            v(&["--version"]),
            v(&["--sync"]),
            v(&["--resume", "--skipfirst"]),
            v(&["--jobs", "--keep-going", "@world"]),
            v(&["--jobs=4", "--load-average=3.5", "--backtrack=30"]),
            v(&["--exclude=a/b", "--exclude=c/d", "--usepkg-exclude=dev-libs/bar", "--buildpkg-exclude=dev-libs/baz"]),
            v(&["--pretend", "~dev-lang/python-3.11.5"]),
            v(&["--exclude=~dev-lang/python-3.11.5"]),
        ] {
            assert!(check(&ok).is_ok(), "{ok:?}: {:?}", check(&ok));
        }
    }
}
