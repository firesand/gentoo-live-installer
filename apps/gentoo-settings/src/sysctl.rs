//! Which sysctl configuration file wins: `sysctl --system` (procps) reads
//! `*.conf` from these directories, the first directory winning for equal
//! names, applies them sorted by file name and `/etc/sysctl.conf` last. A key
//! set by a later file overrides ours.

use crate::detect::System;

pub const DIRS: &[&str] = &["/etc/sysctl.d", "/run/sysctl.d", "/usr/local/lib/sysctl.d", "/usr/lib/sysctl.d", "/lib/sysctl.d"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Setting {
    pub file: String,
    pub key: String,
    pub value: String,
}

#[derive(Debug, Clone, Default)]
pub struct Sources {
    /// Every setting, in the order `sysctl --system` applies them.
    pub settings: Vec<Setting>,
    /// Files in the order they apply.
    pub files: Vec<String>,
    /// Files we could not read (root-only); they may set anything.
    pub unreadable: Vec<String>,
}

fn norm_key(k: &str) -> String {
    k.trim().trim_start_matches('-').replace('/', ".")
}

pub fn parse(text: &str, file: &str) -> Vec<Setting> {
    text.lines()
        .filter_map(|l| {
            let l = l.trim();
            if l.is_empty() || l.starts_with('#') || l.starts_with(';') {
                return None;
            }
            let (k, v) = l.split_once('=')?;
            Some(Setting { file: file.to_string(), key: norm_key(k), value: crate::detect::norm_ws(v) })
        })
        .collect()
}

pub fn load(sys: &System) -> Sources {
    let mut by_name: Vec<(String, String)> = Vec::new();
    for d in DIRS {
        // /lib → /usr/lib on merged-usr systems: the same file seen twice is still one name.
        for name in sys.list(d) {
            if name.ends_with(".conf") && !by_name.iter().any(|(n, _)| *n == name) {
                by_name.push((name.clone(), format!("{d}/{name}")));
            }
        }
    }
    by_name.sort_by(|a, b| a.0.cmp(&b.0));
    let mut order: Vec<String> = by_name.into_iter().map(|(_, p)| p).collect();
    if sys.exists("/etc/sysctl.conf") {
        order.push("/etc/sysctl.conf".into());
    }
    let mut out = Sources::default();
    for f in &order {
        match sys.read_raw(f) {
            Ok(t) => out.settings.extend(parse(&t, f)),
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => out.unreadable.push(f.clone()),
            Err(_) => {}
        }
    }
    out.files = order;
    out
}

impl Sources {
    /// Settings of `key` that apply after `our_file`, i.e. that override it.
    pub fn overrides(&self, key: &str, our_file: &str) -> Vec<&Setting> {
        let pos = self.files.iter().position(|f| f == our_file);
        self.settings
            .iter()
            .filter(|s| s.key == key && s.file != our_file)
            .filter(|s| match pos {
                Some(p) => self.files.iter().position(|f| *f == s.file).is_some_and(|q| q > p),
                // Not installed yet: where would it go?
                None => applies_after(&s.file, our_file),
            })
            .collect()
    }

    /// Unreadable files that apply after ours (might override anything).
    pub fn unreadable_after(&self, our_file: &str) -> Vec<&String> {
        self.unreadable.iter().filter(|f| applies_after(f, our_file)).collect()
    }

    /// Files other than ours that set `key`.
    pub fn others(&self, key: &str, our_file: &str) -> Vec<&Setting> {
        self.settings.iter().filter(|s| s.key == key && s.file != our_file).collect()
    }
}

fn base(p: &str) -> &str {
    p.rsplit('/').next().unwrap_or(p)
}

fn applies_after(file: &str, ours: &str) -> bool {
    file == "/etc/sysctl.conf" || base(file) > base(ours)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_sysctl_syntax() {
        let s = parse("# c\n; c\nvm.swappiness=10\n-kernel/sysrq = 1\nkernel.printk = 3\t3 3 3\n", "/f");
        assert_eq!(s.len(), 3);
        assert_eq!(s[0].key, "vm.swappiness");
        assert_eq!(s[1].key, "kernel.sysrq");
        assert_eq!(s[2].value, "3 3 3 3");
    }

    #[test]
    fn later_files_override() {
        assert!(applies_after("/etc/sysctl.d/99-gaming.conf", "/etc/sysctl.d/70-gentoo-settings.conf"));
        assert!(!applies_after("/usr/lib/sysctl.d/50-default.conf", "/etc/sysctl.d/70-gentoo-settings.conf"));
        assert!(applies_after("/etc/sysctl.conf", "/etc/sysctl.d/70-gentoo-settings.conf"));
    }
}
