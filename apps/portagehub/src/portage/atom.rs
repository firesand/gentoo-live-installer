//! Package dependency atoms (`>=cat/pkg-1.0:2/2.1::repo[flag]`).

use super::version::{Version, split_pv};
use regex::Regex;
use std::collections::HashSet;
use std::fmt;
use std::sync::OnceLock;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Op {
    Any,
    Lt,
    Le,
    Eq,
    /// `=cat/pkg-1.2*`
    EqGlob,
    /// `~cat/pkg-1.2` (any revision)
    Tilde,
    Ge,
    Gt,
}

impl Op {
    pub fn as_str(&self) -> &'static str {
        match self {
            Op::Any => "",
            Op::Lt => "<",
            Op::Le => "<=",
            Op::Eq | Op::EqGlob => "=",
            Op::Tilde => "~",
            Op::Ge => ">=",
            Op::Gt => ">",
        }
    }

    pub fn label(&self) -> &'static str {
        match self {
            Op::Any => "Any version",
            Op::Lt => "Older than (<)",
            Op::Le => "This or older (<=)",
            Op::Eq => "Exactly (=)",
            Op::EqGlob => "Version prefix (=…*)",
            Op::Tilde => "Any revision (~)",
            Op::Ge => "This or newer (>=)",
            Op::Gt => "Newer than (>)",
        }
    }

    pub const ALL: [Op; 8] = [Op::Any, Op::Eq, Op::EqGlob, Op::Tilde, Op::Ge, Op::Gt, Op::Le, Op::Lt];
}

#[derive(Debug, Clone)]
pub struct Atom {
    /// 0 = none, 1 = `!` (weak), 2 = `!!` (strong)
    pub blocker: u8,
    pub op: Op,
    /// May be `*` in user configuration files.
    pub cat: String,
    /// May be `*` in user configuration files.
    pub pn: String,
    pub ver: Option<Version>,
    pub slot: Option<String>,
    pub subslot: Option<String>,
    /// `=` or `*` slot operator
    pub slot_op: Option<char>,
    pub repo: Option<String>,
    pub use_deps: Vec<String>,
}

fn valid_name(s: &str) -> bool {
    !s.is_empty()
        && s.chars().all(|c| c.is_ascii_alphanumeric() || "+_.-".contains(c))
        && !s.starts_with('-')
        && !s.starts_with('.')
}

impl Atom {
    /// Lenient parsing for atoms from Portage's own files and output (dependency strings, the
    /// world file, profiles). Use [`Atom::parse_user`] for what a person writes in `/etc/portage`.
    pub fn parse(input: &str) -> Option<Atom> {
        Atom::parse_with(input, false)
    }

    /// An atom as Portage accepts it in `package.*` files: `*` may stand inside a category or
    /// package name (`dev-qt/qt*`, `app-*/foo`), a version needs an operator (`app-misc/foo-1.0`
    /// is not an atom, `=app-misc/foo-1.0` is), and an empty slot, repository or USE dependency
    /// (`foo:`, `foo::`, `foo[]`) is an error. The checks are a port of Portage's own grammar and
    /// are tested against rows generated from it (`tests/fixtures/atoms.tsv`).
    pub fn parse_user(input: &str) -> Option<Atom> {
        let s = input.trim();
        let body = s.strip_prefix("!!").or_else(|| s.strip_prefix('!')).unwrap_or(s);
        if !portage_accepts(body) {
            return None;
        }
        Atom::parse_with(s, true)
    }

    fn parse_with(input: &str, user: bool) -> Option<Atom> {
        let mut s = input.trim();
        let mut blocker = 0;
        if let Some(r) = s.strip_prefix("!!") {
            blocker = 2;
            s = r;
        } else if let Some(r) = s.strip_prefix('!') {
            blocker = 1;
            s = r;
        }
        let (mut op, rest) = if let Some(r) = s.strip_prefix(">=") {
            (Op::Ge, r)
        } else if let Some(r) = s.strip_prefix("<=") {
            (Op::Le, r)
        } else if let Some(r) = s.strip_prefix('>') {
            (Op::Gt, r)
        } else if let Some(r) = s.strip_prefix('<') {
            (Op::Lt, r)
        } else if let Some(r) = s.strip_prefix('=') {
            (Op::Eq, r)
        } else if let Some(r) = s.strip_prefix('~') {
            (Op::Tilde, r)
        } else {
            (Op::Any, s)
        };
        let mut s = rest;

        let mut use_deps = Vec::new();
        if s.ends_with(']') {
            let open = s.find('[')?;
            use_deps = s[open + 1..s.len() - 1]
                .split(',')
                .map(|x| x.trim().to_string())
                .filter(|x| !x.is_empty())
                .collect();
            s = &s[..open];
        }
        let mut repo = None;
        if let Some(i) = s.find("::") {
            repo = Some(s[i + 2..].to_string());
            s = &s[..i];
        }
        let (mut slot, mut subslot, mut slot_op) = (None, None, None);
        if let Some(i) = s.find(':') {
            let mut sl = &s[i + 1..];
            s = &s[..i];
            if sl == "*" {
                slot_op = Some('*');
            } else if sl == "=" {
                slot_op = Some('=');
            } else {
                if let Some(x) = sl.strip_suffix('=') {
                    slot_op = Some('=');
                    sl = x;
                }
                match sl.split_once('/') {
                    Some((a, b)) => {
                        slot = Some(a.to_string());
                        subslot = Some(b.to_string());
                    }
                    None => slot = Some(sl.to_string()),
                }
            }
        }
        let (cat, pnv) = s.split_once('/')?;
        if !(user || cat == "*" || valid_name(cat)) {
            return None;
        }
        let (pn, ver) = if op == Op::Any {
            (pnv.to_string(), None)
        } else {
            let mut pv = pnv;
            if op == Op::Eq
                && let Some(x) = pv.strip_suffix('*')
            {
                op = Op::EqGlob;
                pv = x;
            }
            let (pn, ver) = split_pv(pv)?;
            (pn.to_string(), Some(ver))
        };
        if !(user || pn == "*" || valid_name(&pn)) {
            return None;
        }
        Some(Atom { blocker, op, cat: cat.to_string(), pn, ver, slot, subslot, slot_op, repo, use_deps })
    }

    pub fn key(&self) -> String {
        format!("{}/{}", self.cat, self.pn)
    }

    pub fn is_wildcard(&self) -> bool {
        self.cat.contains('*') || self.pn.contains('*')
    }

    pub fn matches_key(&self, cat: &str, pn: &str) -> bool {
        glob_match(&self.cat, cat) && glob_match(&self.pn, pn)
    }

    /// Does this atom select the given package version? (USE deps are ignored.)
    pub fn matches(&self, cat: &str, pn: &str, ver: &Version, slot: Option<&str>, repo: Option<&str>) -> bool {
        if !self.matches_key(cat, pn) {
            return false;
        }
        if let (Some(want), Some(have)) = (&self.slot, slot) {
            let have_slot = have.split('/').next().unwrap_or(have);
            if want != have_slot {
                return false;
            }
        }
        if let (Some(want), Some(have)) = (&self.repo, repo)
            && want != have
        {
            return false;
        }
        let Some(av) = &self.ver else { return true };
        match self.op {
            Op::Any => true,
            Op::Eq => ver == av,
            Op::EqGlob => eq_glob(&ver.raw, &av.raw),
            Op::Tilde => ver.eq_ignoring_revision(av),
            Op::Lt => ver < av,
            Op::Le => ver <= av,
            Op::Gt => ver > av,
            Op::Ge => ver >= av,
        }
    }
}

impl fmt::Display for Atom {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.blocker {
            1 => f.write_str("!")?,
            2 => f.write_str("!!")?,
            _ => {}
        }
        f.write_str(self.op.as_str())?;
        write!(f, "{}/{}", self.cat, self.pn)?;
        if let Some(v) = &self.ver {
            write!(f, "-{v}")?;
            if self.op == Op::EqGlob {
                f.write_str("*")?;
            }
        }
        if let Some(s) = &self.slot {
            write!(f, ":{s}")?;
            if let Some(ss) = &self.subslot {
                write!(f, "/{ss}")?;
            }
            if let Some(o) = self.slot_op {
                write!(f, "{o}")?;
            }
        } else if let Some(o) = self.slot_op {
            write!(f, ":{o}")?;
        }
        if let Some(r) = &self.repo {
            write!(f, "::{r}")?;
        }
        if !self.use_deps.is_empty() {
            write!(f, "[{}]", self.use_deps.join(","))?;
        }
        Ok(())
    }
}

/// Ports of `portage.dep`'s atom grammar (Portage 3.0.82, `Atom(s, allow_wildcard=True,
/// allow_repo=True)`), restricted to ASCII. [`Atom::parse_user`] checks a string against it, so
/// the configuration editor accepts exactly the atoms Portage accepts in `package.*` files.
struct Grammar {
    strict: Regex,
    wildcard: Regex,
    slot: Regex,
    usedep: Regex,
}

fn grammar() -> &'static Grammar {
    static G: OnceLock<Grammar> = OnceLock::new();
    G.get_or_init(|| {
        const CAT: &str = r"[A-Za-z0-9_][A-Za-z0-9_+.-]*";
        const PKG: &str = r"[A-Za-z0-9_][A-Za-z0-9_+-]*?";
        const VR: &str = r"[0-9]+(?:\.[0-9]+)*[a-z]?(?:_(?:pre|p|beta|alpha|rc)[0-9]*)*(?:-r[0-9]+)?";
        const XCAT: &str = r"[A-Za-z0-9_+*][A-Za-z0-9_+.*-]*";
        const XPKG: &str = r"[A-Za-z0-9_+*][A-Za-z0-9_+*-]*?";
        const SLOT: &str = r"[A-Za-z0-9_+./*=-]+";
        const REPO: &str = r"[A-Za-z0-9_][A-Za-z0-9_-]*";
        let tail = format!(r"(?::(?P<slot>{SLOT}))?(?:::(?P<repo>{REPO}))?");
        Grammar {
            strict: Regex::new(&format!(
                r"^(?:(?P<op>(?:[=~]|[<>]=?){CAT}/{PKG}-{VR})|(?P<star>={CAT}/{PKG}-{VR}\*)|(?P<simple>{CAT}/{PKG}(?P<version>-{VR})?)){tail}(?P<usedeps>\[.*\])?$"
            ))
            .expect("strict atom grammar"),
            wildcard: Regex::new(&format!(
                r"^(?:(?P<simple>{XCAT}/{XPKG}(?P<version>-{VR})?)|(?P<star>={XCAT}/{XPKG}-\*[A-Za-z0-9_]+\*)){tail}$"
            ))
            .expect("wildcard atom grammar"),
            slot: Regex::new(r"^(?:(?P<main>[A-Za-z0-9_][A-Za-z0-9_+.-]*)(?:/(?P<sub>[A-Za-z0-9_][A-Za-z0-9_+.-]*))?)?(?P<operator>[*=])?$").expect("slot grammar"),
            usedep: Regex::new(r"^(?P<prefix>[!-]?)(?P<flag>[A-Za-z0-9][A-Za-z0-9+_@-]*)(?P<default>\(\+\)|\(-\))?(?P<suffix>[?=]?)$").expect("USE dependency grammar"),
        }
    })
}

/// `[a,-b,c(+)?]` the way `portage.dep._use_dep` validates it.
fn valid_use_deps(inner: &str) -> bool {
    let g = grammar();
    let (mut enabled, mut disabled, mut no_default) = (HashSet::new(), HashSet::new(), HashSet::new());
    for token in inner.split(',') {
        let Some(m) = g.usedep.captures(token) else { return false };
        if !matches!((&m["prefix"], &m["suffix"]), ("", "") | ("-", "") | ("", "?") | ("", "=") | ("!", "=") | ("!", "?")) {
            return false;
        }
        let flag = m["flag"].to_string();
        match m.name("default").map(|d| d.as_str()) {
            Some("(+)") if disabled.contains(&flag) || no_default.contains(&flag) => return false,
            Some("(+)") => {
                enabled.insert(flag);
            }
            Some(_) if enabled.contains(&flag) || no_default.contains(&flag) => return false,
            Some(_) => {
                disabled.insert(flag);
            }
            None if enabled.contains(&flag) || disabled.contains(&flag) => return false,
            None => {
                no_default.insert(flag);
            }
        }
    }
    true
}

/// Is `body` (an atom without its `!` blocker prefix) valid for Portage in a configuration file?
fn portage_accepts(body: &str) -> bool {
    let g = grammar();
    let (slot, usedeps) = if let Some(m) = g.strict.captures(body) {
        // A bare cat/pkg that ends in a version needs an operator: `app-misc/foo-1.0` is an error.
        if m.name("simple").is_some() && m.name("version").is_some() {
            return false;
        }
        (m.name("slot").map(|s| s.as_str()), m.name("usedeps").map(|u| u.as_str()))
    } else if let Some(m) = g.wildcard.captures(body) {
        // Portage notices a version on a wildcard atom only when it has a dotted part (its check
        // looks at one capture group inside the version): `qt*-1.0` is refused, `qt*-1` is not.
        if m.name("simple").is_some() && m.name("version").is_some_and(|v| v.as_str().contains('.')) {
            return false;
        }
        let cpv = m.name("simple").or(m.name("star")).map_or("", |c| c.as_str());
        if cpv.contains("**") {
            return false;
        }
        (m.name("slot").map(|s| s.as_str()), None)
    } else {
        return false;
    };
    if let Some(slot) = slot {
        let Some(s) = g.slot.captures(slot) else { return false };
        let (main, operator) = (s.name("main"), s.name("operator").map(|o| o.as_str()));
        if main.is_some() && operator == Some("*") || main.is_none() && operator.is_none() {
            return false;
        }
    }
    usedeps.is_none_or(|u| valid_use_deps(&u[1..u.len() - 1]))
}

/// Does `text` match `pattern`, where `*` stands for any run of characters? Portage matches an
/// extended `cat/pkg` the same way (`extended_cp_match`, with `*` as `[^/]*`); category and package
/// contain no `/`, so matching them one by one is equivalent.
pub fn glob_match(pattern: &str, text: &str) -> bool {
    let parts: Vec<&str> = pattern.split('*').collect();
    let ([first, middle @ .., last], true) = (parts.as_slice(), parts.len() > 1) else {
        return pattern == text;
    };
    let Some(mut tail) = text.strip_prefix(first) else { return false };
    for part in middle {
        match tail.find(part) {
            Some(i) => tail = &tail[i + part.len()..],
            None => return false,
        }
    }
    tail.ends_with(last)
}

/// Portage's `=cat/pkg-1.2*`: a prefix match on the version, counted only on a boundary between
/// version parts (bug 560466), so `1*` matches `1.5` but not `10.0`. Leading zeros of the first
/// number are ignored on both sides. `candidate` and `prefix` are version strings, with the
/// revision for the candidate (`1.2-r1`).
pub fn eq_glob(candidate: &str, prefix: &str) -> bool {
    fn unpadded(v: &str) -> String {
        let t = v.trim_start_matches('0');
        if t.chars().next().is_some_and(|c| c.is_ascii_digit()) { t.to_string() } else { format!("0{t}") }
    }
    let (candidate, prefix) = (unpadded(candidate), unpadded(prefix));
    let Some(rest) = candidate.strip_prefix(prefix.as_str()) else { return false };
    match rest.chars().next() {
        None => true,
        Some(next) => "._-".contains(next) || prefix.ends_with(|c: char| c.is_ascii_digit()) != next.is_ascii_digit(),
    }
}

/// Split `cat/pn-ver` into `(cat, pn, version)`.
pub fn split_cpv(cpv: &str) -> Option<(String, String, Version)> {
    let (cat, pv) = cpv.split_once('/')?;
    let (pn, v) = split_pv(pv)?;
    Some((cat.to_string(), pn.to_string(), v))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_full() {
        let a = Atom::parse(">=dev-lang/python-3.12.1-r2:3.12/3.12=::gentoo[threads(+),-tk]").unwrap();
        assert_eq!(a.op, Op::Ge);
        assert_eq!(a.key(), "dev-lang/python");
        assert_eq!(a.ver.as_ref().unwrap().raw, "3.12.1-r2");
        assert_eq!(a.slot.as_deref(), Some("3.12"));
        assert_eq!(a.subslot.as_deref(), Some("3.12"));
        assert_eq!(a.slot_op, Some('='));
        assert_eq!(a.repo.as_deref(), Some("gentoo"));
        assert_eq!(a.use_deps, vec!["threads(+)", "-tk"]);
        assert_eq!(a.to_string(), ">=dev-lang/python-3.12.1-r2:3.12/3.12=::gentoo[threads(+),-tk]");
    }

    #[test]
    fn simple_and_blockers() {
        let a = Atom::parse("!!sys-apps/foo").unwrap();
        assert_eq!(a.blocker, 2);
        assert!(a.ver.is_none());
        assert!(Atom::parse("dev-libs/libsodium:=").unwrap().slot_op == Some('='));
        assert!(Atom::parse("*/*").unwrap().is_wildcard());
        assert!(Atom::parse("notanatom").is_none());
        assert!(Atom::parse(">=foo/bar").is_none());
    }

    #[test]
    fn matching() {
        let v = |s| Version::parse(s).unwrap();
        let a = Atom::parse("=app-misc/foo-1.2*").unwrap();
        assert_eq!(a.op, Op::EqGlob);
        assert!(a.matches("app-misc", "foo", &v("1.2.3"), None, None));
        assert!(!a.matches("app-misc", "foo", &v("1.3"), None, None));
        let a = Atom::parse("~app-misc/foo-1.2").unwrap();
        assert!(a.matches("app-misc", "foo", &v("1.2-r5"), None, None));
        let a = Atom::parse("<app-misc/foo-2:1").unwrap();
        assert!(a.matches("app-misc", "foo", &v("1.9"), Some("1/1.9"), None));
        assert!(!a.matches("app-misc", "foo", &v("1.9"), Some("0"), None));
        let a = Atom::parse("app-misc/*").unwrap();
        assert!(a.matches("app-misc", "anything", &v("1"), None, None));
    }

    /// Rows generated from Portage itself (tests/fixtures/gen_fixtures.py): the user-facing parser
    /// accepts and rejects exactly what `portage.dep.Atom(allow_wildcard=True, allow_repo=True)` does.
    #[test]
    fn parse_user_agrees_with_portage() {
        let mut wrong = Vec::new();
        let mut rows = 0;
        for line in include_str!("../../tests/fixtures/atoms.tsv").lines().filter(|l| !l.starts_with('#') && !l.is_empty()) {
            let (valid, atom) = line.split_once('\t').expect("valid<TAB>atom");
            rows += 1;
            if Atom::parse_user(atom).is_some() != (valid == "1") {
                wrong.push(format!("{atom:?}: Portage says {}", if valid == "1" { "valid" } else { "invalid" }));
            }
        }
        assert!(rows > 500, "{rows} rows");
        assert!(wrong.is_empty(), "{} of {rows} atoms disagree:\n{}", wrong.len(), wrong.join("\n"));
    }

    #[test]
    fn user_atoms() {
        for ok in ["dev-qt/qt*", "app-*/foo", "app-misc/fo*", "*/*", "app-misc/*", "*/foo", "=app-misc/foo-1.2*", "~dev-lang/python-3.11.5"] {
            assert!(Atom::parse_user(ok).is_some(), "{ok}");
        }
        for bad in ["app-misc/foo-1.0", "app-misc/foo:", "app-misc/foo::", "app-misc/foo[]", "app-misc/foo.bar", "dev-cpp/gtk++-3.0", "foo", ""] {
            assert!(Atom::parse_user(bad).is_none(), "{bad}");
        }
        // The same strings the lenient parser (for Portage's own output) takes.
        assert!(Atom::parse("app-misc/foo-1.0").is_some());
        let a = Atom::parse_user("dev-qt/qt*").unwrap();
        assert_eq!((a.cat.as_str(), a.pn.as_str()), ("dev-qt", "qt*"));
        assert!(a.matches_key("dev-qt", "qtbase") && a.matches_key("dev-qt", "qt") && !a.matches_key("dev-libs", "qtbase"));
        assert!(a.is_wildcard());
        let a = Atom::parse_user("app-*/f*o").unwrap();
        assert!(a.matches_key("app-misc", "foo") && a.matches_key("app-editors", "fo") && !a.matches_key("dev-misc", "foo") && !a.matches_key("app-misc", "bar"));
        assert!(!Atom::parse_user("app-misc/foo").unwrap().is_wildcard());
    }

    #[test]
    fn glob_matching() {
        for (p, t, want) in [
            ("*", "anything", true),
            ("a*", "abc", true),
            ("a*", "bc", false),
            ("*c", "abc", true),
            ("*c", "abd", false),
            ("a*c", "abc", true),
            ("a*c", "ac", true),
            ("a*a", "a", false),
            ("a*b*c", "aXbYc", true),
            ("a*b*c", "aXcYb", false),
            ("abc", "abc", true),
            ("abc", "abcd", false),
            ("", "", true),
            ("*", "", true),
        ] {
            assert_eq!(glob_match(p, t), want, "{p} vs {t}");
        }
    }

    /// `=cat/pkg-1.2*` follows Portage's match_from_list rule: a prefix of the version string that
    /// ends on a boundary between version parts. Rows come from Portage (tests/fixtures/eqglob.tsv).
    #[test]
    fn eq_glob_agrees_with_portage() {
        let mut wrong = Vec::new();
        let mut rows = 0;
        for line in include_str!("../../tests/fixtures/eqglob.tsv").lines().filter(|l| !l.starts_with('#') && !l.is_empty()) {
            let mut f = line.split('\t');
            let (want, atom, candidate) = (f.next().unwrap() == "1", f.next().unwrap(), f.next().unwrap());
            let a = Atom::parse_user(atom).unwrap_or_else(|| panic!("{atom}"));
            let v = Version::parse(candidate).unwrap_or_else(|| panic!("{candidate}"));
            rows += 1;
            if a.matches("app-misc", "foo", &v, None, None) != want {
                wrong.push(format!("{atom} vs {candidate}: Portage says {want}"));
            }
        }
        assert!(rows > 500, "{rows} rows");
        assert!(wrong.is_empty(), "{} of {rows} disagree:\n{}", wrong.len(), wrong.join("\n"));
    }

    #[test]
    fn eq_glob_cases() {
        // A kernel pin must not reach later series (the original bug: raw string prefix).
        assert!(eq_glob("6.1.190", "6.1"));
        assert!(!eq_glob("6.12.113", "6.1"));
        assert!(!eq_glob("6.18.57", "6.1"));
        assert!(eq_glob("1.2", "1.2") && eq_glob("1.2.3", "1.2") && eq_glob("1.2-r1", "1.2") && eq_glob("1.2_rc1", "1.2"));
        assert!(!eq_glob("1.20", "1.2") && !eq_glob("1.3", "1.2"));
        assert!(eq_glob("1.5", "1") && !eq_glob("10.0", "1"));
        assert!(!eq_glob("1.10", "1.1"));
        assert!(eq_glob("01.2", "1") && eq_glob("1.2", "01"));
    }

    #[test]
    fn cpv() {
        let (c, p, v) = split_cpv("app-editors/vim-9.1.1652-r2").unwrap();
        assert_eq!((c.as_str(), p.as_str(), v.raw.as_str()), ("app-editors", "vim", "9.1.1652-r2"));
    }
}
