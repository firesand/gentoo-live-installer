//! Package dependency atoms (`>=cat/pkg-1.0:2/2.1::repo[flag]`).

use super::version::{Version, split_pv};
use std::fmt;

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
    !s.is_empty() && s.chars().all(|c| c.is_ascii_alphanumeric() || "+_.-".contains(c)) && !s.starts_with('-') && !s.starts_with('.')
}

impl Atom {
    pub fn parse(input: &str) -> Option<Atom> {
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
            use_deps = s[open + 1..s.len() - 1].split(',').map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect();
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
        if !(cat == "*" || valid_name(cat)) {
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
        if !(pn == "*" || valid_name(&pn)) {
            return None;
        }
        Some(Atom { blocker, op, cat: cat.to_string(), pn, ver, slot, subslot, slot_op, repo, use_deps })
    }

    pub fn key(&self) -> String {
        format!("{}/{}", self.cat, self.pn)
    }

    pub fn is_wildcard(&self) -> bool {
        self.cat == "*" || self.pn == "*"
    }

    pub fn matches_key(&self, cat: &str, pn: &str) -> bool {
        (self.cat == "*" || self.cat == cat) && (self.pn == "*" || self.pn == pn)
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
            Op::EqGlob => ver.raw.starts_with(&av.raw),
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

    #[test]
    fn cpv() {
        let (c, p, v) = split_cpv("app-editors/vim-9.1.1652-r2").unwrap();
        assert_eq!((c.as_str(), p.as_str(), v.raw.as_str()), ("app-editors", "vim", "9.1.1652-r2"));
    }
}
