//! USE flag descriptions: profiles/use.desc, use.local.desc and the
//! USE_EXPAND descriptions in profiles/desc/*.desc.

use std::collections::HashMap;
use std::path::Path;

#[derive(Debug, Default)]
pub struct UseDescDb {
    pub global: HashMap<String, String>,
    /// "cat/pn" -> flag -> description
    pub local: HashMap<String, HashMap<String, String>>,
    /// lowercase USE_EXPAND prefix (e.g. "python_targets") -> value -> description
    pub expand: HashMap<String, HashMap<String, String>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum FlagScope {
    Local,
    Global,
    Expand,
    #[default]
    Unknown,
}

impl UseDescDb {
    /// Split `python_targets_python3_12` into ("python_targets", "python3_12").
    pub fn split_expand<'a>(&self, flag: &'a str) -> Option<(&'a str, &'a str)> {
        let mut best: Option<(&str, &str)> = None;
        for (i, _) in flag.match_indices('_') {
            let (pre, val) = (&flag[..i], &flag[i + 1..]);
            if self.expand.contains_key(pre) && best.is_none_or(|(p, _)| pre.len() > p.len()) {
                best = Some((pre, val));
            }
        }
        best
    }

    pub fn describe(&self, key: &str, flag: &str, xml: Option<&HashMap<String, String>>) -> (String, FlagScope) {
        if let Some(d) = xml.and_then(|x| x.get(flag)) {
            return (d.clone(), FlagScope::Local);
        }
        if let Some(d) = self.local.get(key).and_then(|m| m.get(flag)) {
            return (d.clone(), FlagScope::Local);
        }
        if let Some((pre, val)) = self.split_expand(flag)
            && let Some(d) = self.expand.get(pre).and_then(|m| m.get(val))
        {
            return (d.clone(), FlagScope::Expand);
        }
        if let Some(d) = self.global.get(flag) {
            return (d.clone(), FlagScope::Global);
        }
        (String::new(), FlagScope::Unknown)
    }
}

fn parse_desc(text: &str, mut f: impl FnMut(&str, &str)) {
    for line in text.lines() {
        let l = line.trim();
        if l.is_empty() || l.starts_with('#') {
            continue;
        }
        if let Some((k, v)) = l.split_once(" - ") {
            f(k.trim(), v.trim());
        }
    }
}

pub fn load(repo_locations: &[&Path]) -> UseDescDb {
    let mut db = UseDescDb::default();
    for loc in repo_locations {
        let prof = loc.join("profiles");
        if let Ok(t) = std::fs::read_to_string(prof.join("use.desc")) {
            parse_desc(&t, |k, v| {
                db.global.entry(k.to_string()).or_insert_with(|| v.to_string());
            });
        }
        if let Ok(t) = std::fs::read_to_string(prof.join("use.local.desc")) {
            parse_desc(&t, |k, v| {
                if let Some((pkg, flag)) = k.split_once(':') {
                    db.local.entry(pkg.to_string()).or_default().entry(flag.to_string()).or_insert_with(|| v.to_string());
                }
            });
        }
        for e in std::fs::read_dir(prof.join("desc")).into_iter().flatten().flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            let Some(prefix) = name.strip_suffix(".desc") else { continue };
            if let Ok(t) = std::fs::read_to_string(e.path()) {
                let m = db.expand.entry(prefix.to_lowercase()).or_default();
                parse_desc(&t, |k, v| {
                    m.entry(k.to_string()).or_insert_with(|| v.to_string());
                });
            }
        }
    }
    db
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expand_split() {
        let mut db = UseDescDb::default();
        db.expand.entry("python_targets".into()).or_default().insert("python3_12".into(), "Build for 3.12".into());
        db.expand.entry("python".into()).or_default();
        assert_eq!(db.split_expand("python_targets_python3_12"), Some(("python_targets", "python3_12")));
        assert_eq!(db.describe("x/y", "python_targets_python3_12", None).1, FlagScope::Expand);
        assert_eq!(db.split_expand("nls"), None);
    }
}
