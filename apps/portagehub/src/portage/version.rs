//! Package versions and their ordering, following PMS section 3.2/3.3.

use std::cmp::Ordering;
use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Suffix {
    Alpha,
    Beta,
    Pre,
    Rc,
    P,
}

impl Suffix {
    fn parse(s: &str) -> Option<(Suffix, &str)> {
        // Longest names first so "pre" is not read as "p" + "re".
        for (name, suf) in [
            ("alpha", Suffix::Alpha),
            ("beta", Suffix::Beta),
            ("pre", Suffix::Pre),
            ("rc", Suffix::Rc),
            ("p", Suffix::P),
        ] {
            if let Some(rest) = s.strip_prefix(name) {
                return Some((suf, rest));
            }
        }
        None
    }
}

#[derive(Debug, Clone)]
pub struct Version {
    pub raw: String,
    numbers: Vec<String>,
    letter: Option<char>,
    suffixes: Vec<(Suffix, u64)>,
    pub revision: u64,
}

impl Version {
    pub fn parse(s: &str) -> Option<Version> {
        let raw = s.to_string();
        let (main, revision) = match s.rfind("-r") {
            Some(i) if !s[i + 2..].is_empty() && s[i + 2..].bytes().all(|b| b.is_ascii_digit()) => {
                (&s[..i], s[i + 2..].parse().ok()?)
            }
            _ => (s, 0),
        };
        let mut rest = main;
        let mut numbers = Vec::new();
        loop {
            let end = rest.bytes().position(|b| !b.is_ascii_digit()).unwrap_or(rest.len());
            if end == 0 {
                return None;
            }
            numbers.push(rest[..end].to_string());
            rest = &rest[end..];
            if let Some(r) = rest.strip_prefix('.') {
                if r.bytes().next().is_some_and(|b| b.is_ascii_digit()) {
                    rest = r;
                    continue;
                }
                return None;
            }
            break;
        }
        let mut letter = None;
        if let Some(c) = rest.chars().next()
            && c.is_ascii_lowercase()
        {
            letter = Some(c);
            rest = &rest[1..];
        }
        let mut suffixes = Vec::new();
        while let Some(r) = rest.strip_prefix('_') {
            let (suf, r) = Suffix::parse(r)?;
            let end = r.bytes().position(|b| !b.is_ascii_digit()).unwrap_or(r.len());
            let n = if end == 0 { 0 } else { r[..end].parse().ok()? };
            suffixes.push((suf, n));
            rest = &r[end..];
        }
        if !rest.is_empty() {
            return None;
        }
        Some(Version { raw, numbers, letter, suffixes, revision })
    }

    /// Version without the `-rN` part.
    pub fn without_revision(&self) -> &str {
        match self.raw.rfind("-r") {
            Some(i) if self.revision > 0 || self.raw[i + 2..].bytes().all(|b| b.is_ascii_digit()) => &self.raw[..i],
            _ => &self.raw,
        }
    }

    pub fn is_live(&self) -> bool {
        self.numbers.iter().any(|n| n.len() >= 4 && n.bytes().all(|b| b == b'9'))
    }

    pub fn is_prerelease(&self) -> bool {
        self.suffixes.iter().any(|(s, _)| *s < Suffix::P)
    }

    /// `~` operator: equal ignoring revision.
    pub fn eq_ignoring_revision(&self, other: &Version) -> bool {
        let mut a = self.clone();
        let mut b = other.clone();
        a.revision = 0;
        b.revision = 0;
        a.cmp(&b) == Ordering::Equal
    }
}

fn cmp_numbers(a: &[String], b: &[String]) -> Ordering {
    let first = |s: &String| s.trim_start_matches('0').to_string();
    // First component: plain integer comparison.
    let (fa, fb) = (first(&a[0]), first(&b[0]));
    let o = fa.len().cmp(&fb.len()).then_with(|| fa.cmp(&fb));
    if o != Ordering::Equal {
        return o;
    }
    for i in 1..a.len().max(b.len()) {
        let (Some(x), Some(y)) = (a.get(i), b.get(i)) else {
            return a.len().cmp(&b.len());
        };
        let o = if x.starts_with('0') || y.starts_with('0') {
            let x = x.trim_end_matches('0');
            let y = y.trim_end_matches('0');
            x.cmp(y)
        } else {
            x.len().cmp(&y.len()).then_with(|| x.cmp(y))
        };
        if o != Ordering::Equal {
            return o;
        }
    }
    Ordering::Equal
}

impl Ord for Version {
    fn cmp(&self, other: &Self) -> Ordering {
        let o = cmp_numbers(&self.numbers, &other.numbers);
        if o != Ordering::Equal {
            return o;
        }
        let o = self.letter.cmp(&other.letter);
        if o != Ordering::Equal {
            return o;
        }
        let n = self.suffixes.len().min(other.suffixes.len());
        for i in 0..n {
            let (sa, na) = self.suffixes[i];
            let (sb, nb) = other.suffixes[i];
            let o = sa.cmp(&sb).then(na.cmp(&nb));
            if o != Ordering::Equal {
                return o;
            }
        }
        if self.suffixes.len() != other.suffixes.len() {
            let (longer, sign) = if self.suffixes.len() > other.suffixes.len() {
                (&self.suffixes, Ordering::Greater)
            } else {
                (&other.suffixes, Ordering::Less)
            };
            return if longer[n].0 == Suffix::P { sign } else { sign.reverse() };
        }
        self.revision.cmp(&other.revision)
    }
}

impl PartialOrd for Version {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl PartialEq for Version {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}
impl Eq for Version {}

impl fmt::Display for Version {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.raw)
    }
}

/// Split `pn-ver` (a package-version string without category) into its parts.
pub fn split_pv(s: &str) -> Option<(&str, Version)> {
    let bytes = s.as_bytes();
    let mut i = s.len();
    while let Some(pos) = s[..i].rfind('-') {
        if pos + 1 < bytes.len() && bytes[pos + 1].is_ascii_digit()
            && let Some(v) = Version::parse(&s[pos + 1..])
        {
            return Some((&s[..pos], v));
        }
        i = pos;
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(s: &str) -> Version {
        Version::parse(s).unwrap_or_else(|| panic!("parse {s}"))
    }

    #[test]
    fn ordering_table() {
        let less = [
            ("1.0", "1.0.1"),
            ("1.0", "1.0a"),
            ("1.0_alpha", "1.0_beta"),
            ("1.0_beta", "1.0_pre"),
            ("1.0_pre", "1.0_rc"),
            ("1.0_rc", "1.0"),
            ("1.0", "1.0_p1"),
            ("1.0_rc1", "1.0_rc2"),
            ("1.0", "1.0-r1"),
            ("1.0-r1", "1.0-r10"),
            ("1.2", "1.10"),
            ("1.01", "1.1"),
            ("1.010", "1.1"),
            ("1.0_alpha1", "1.0_alpha1_p1"),
            ("1.0_alpha1_beta", "1.0_alpha1"),
            ("2", "10"),
            ("9.1.1652-r2", "9999"),
            ("0.9", "1"),
        ];
        for (a, b) in less {
            assert!(v(a) < v(b), "{a} < {b}");
            assert!(v(b) > v(a), "{b} > {a}");
        }
        assert_eq!(v("1.0"), v("1.0"));
        assert_eq!(v("01"), v("1"));
        assert_eq!(v("1.0-r0"), v("1.0"));
    }

    #[test]
    fn parse_rejects() {
        for s in ["", "a", "1.", "1..2", "1.0_foo", "1.0-r", "1.0aa"] {
            assert!(Version::parse(s).is_none(), "{s}");
        }
    }

    #[test]
    fn split() {
        let (pn, ver) = split_pv("vim-9.1.1652-r2").unwrap();
        assert_eq!(pn, "vim");
        assert_eq!(ver.raw, "9.1.1652-r2");
        assert_eq!(ver.without_revision(), "9.1.1652");
        let (pn, ver) = split_pv("font-adobe-100dpi-1.0.4").unwrap();
        assert_eq!(pn, "font-adobe-100dpi");
        assert_eq!(ver.raw, "1.0.4");
        let (pn, _) = split_pv("python-3.12.1_p2").unwrap();
        assert_eq!(pn, "python");
        assert!(split_pv("noversion").is_none());
    }

    #[test]
    fn tilde() {
        assert!(v("1.0-r3").eq_ignoring_revision(&v("1.0")));
        assert!(!v("1.0.1").eq_ignoring_revision(&v("1.0")));
        assert!(v("9999").is_live());
    }
}
