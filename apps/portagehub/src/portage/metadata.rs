//! Per-ebuild metadata (from the md5-cache / edb dep cache, falling back to
//! reading the ebuild) and per-package `metadata.xml`.

use regex::Regex;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MetaSource {
    Md5Cache,
    EdbCache,
    /// Parsed straight from the ebuild: eclass-provided values are missing.
    Ebuild,
    Missing,
}

impl MetaSource {
    pub fn label(&self) -> &'static str {
        match self {
            MetaSource::Md5Cache => "repository metadata cache",
            MetaSource::EdbCache => "Portage dependency cache",
            MetaSource::Ebuild => "ebuild text (approximate — eclass values missing)",
            MetaSource::Missing => "unavailable",
        }
    }
}

#[derive(Debug, Clone)]
pub struct EbuildMeta {
    pub vars: HashMap<String, String>,
    pub source: MetaSource,
}

impl EbuildMeta {
    pub fn get(&self, k: &str) -> &str {
        self.vars.get(k).map(|s| s.as_str()).unwrap_or("")
    }

    pub fn slot(&self) -> &str {
        let s = self.get("SLOT");
        if s.is_empty() { "0" } else { s }
    }

    /// IUSE entries without +/- defaults.
    pub fn iuse(&self) -> Vec<(String, Option<bool>)> {
        self.get("IUSE")
            .split_whitespace()
            .map(|f| {
                if let Some(x) = f.strip_prefix('+') {
                    (x.to_string(), Some(true))
                } else if let Some(x) = f.strip_prefix('-') {
                    (x.to_string(), Some(false))
                } else {
                    (f.to_string(), None)
                }
            })
            .collect()
    }

    pub fn keywords(&self) -> Vec<String> {
        self.get("KEYWORDS").split_whitespace().map(String::from).collect()
    }
}

pub fn ebuild_path(repo_loc: &Path, cat: &str, pn: &str, ver: &str) -> PathBuf {
    repo_loc.join(cat).join(pn).join(format!("{pn}-{ver}.ebuild"))
}

fn parse_cache(text: &str) -> HashMap<String, String> {
    text.lines()
        .filter_map(|l| l.split_once('='))
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

pub fn load(repo_loc: &Path, cat: &str, pn: &str, ver: &str) -> EbuildMeta {
    let pf = format!("{pn}-{ver}");
    let md5 = repo_loc.join("metadata/md5-cache").join(cat).join(&pf);
    if let Ok(t) = std::fs::read_to_string(&md5) {
        return EbuildMeta { vars: parse_cache(&t), source: MetaSource::Md5Cache };
    }
    let mut edb = PathBuf::from("/var/cache/edb/dep");
    edb.push(repo_loc.strip_prefix("/").unwrap_or(repo_loc));
    edb.push(cat);
    edb.push(&pf);
    if let Ok(t) = std::fs::read_to_string(&edb) {
        return EbuildMeta { vars: parse_cache(&t), source: MetaSource::EdbCache };
    }
    match std::fs::read_to_string(ebuild_path(repo_loc, cat, pn, ver)) {
        Ok(t) => EbuildMeta { vars: parse_ebuild_vars(&t), source: MetaSource::Ebuild },
        Err(_) => EbuildMeta { vars: HashMap::new(), source: MetaSource::Missing },
    }
}

/// Best-effort extraction of top-level `VAR="value"` assignments from an ebuild.
pub fn parse_ebuild_vars(text: &str) -> HashMap<String, String> {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| {
        Regex::new(r#"(?ms)^(EAPI|DESCRIPTION|HOMEPAGE|SLOT|KEYWORDS|IUSE|LICENSE|RESTRICT|PROPERTIES|REQUIRED_USE|DEPEND|RDEPEND|BDEPEND|PDEPEND|IDEPEND|SRC_URI|INHERIT)=(?:"([^"]*)"|'([^']*)'|(\S*))"#)
            .unwrap()
    });
    static INHERIT: OnceLock<Regex> = OnceLock::new();
    let inh = INHERIT.get_or_init(|| Regex::new(r"(?m)^inherit\s+(.+)$").unwrap());
    let mut out = HashMap::new();
    for c in re.captures_iter(text) {
        let v = c.get(2).or(c.get(3)).or(c.get(4)).map(|m| m.as_str()).unwrap_or("");
        let v = v.split_whitespace().collect::<Vec<_>>().join(" ");
        out.insert(c[1].to_string(), v);
    }
    let inherits: Vec<&str> = inh.captures_iter(text).map(|c| c.get(1).unwrap().as_str().trim()).collect();
    if !inherits.is_empty() {
        out.insert("INHERIT".into(), inherits.join(" "));
    }
    out
}

#[derive(Debug, Clone, Default)]
pub struct Maintainer {
    pub kind: String,
    pub email: String,
    pub name: String,
    pub description: String,
}

#[derive(Debug, Clone, Default)]
pub struct PkgXml {
    pub maintainers: Vec<Maintainer>,
    pub longdescription: String,
    pub flags: HashMap<String, String>,
    pub upstream: Vec<(String, String)>,
    pub slots: Vec<(String, String)>,
    pub stabilize_allarches: bool,
}

fn strip_tags(s: &str) -> String {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"<[^>]+>").unwrap());
    let s = re.replace_all(s, "");
    let s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&apos;", "'").replace("&amp;", "&");
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub fn load_xml(pkg_dir: &Path) -> PkgXml {
    let Ok(t) = std::fs::read_to_string(pkg_dir.join("metadata.xml")) else {
        return PkgXml::default();
    };
    parse_xml(&t)
}

pub fn parse_xml(t: &str) -> PkgXml {
    static MAINT: OnceLock<Regex> = OnceLock::new();
    static FIELD: OnceLock<Regex> = OnceLock::new();
    static LONG: OnceLock<Regex> = OnceLock::new();
    static FLAG: OnceLock<Regex> = OnceLock::new();
    static UP: OnceLock<Regex> = OnceLock::new();
    static REMOTE: OnceLock<Regex> = OnceLock::new();
    static SLOT: OnceLock<Regex> = OnceLock::new();
    let maint = MAINT.get_or_init(|| Regex::new(r#"(?s)<maintainer(?:\s+type="([^"]*)")?[^>]*>(.*?)</maintainer>"#).unwrap());
    let field = FIELD.get_or_init(|| Regex::new(r"(?s)<(email|name|description)>(.*?)</(?:email|name|description)>").unwrap());
    let long = LONG.get_or_init(|| Regex::new(r#"(?s)<longdescription(?:\s+lang="([^"]*)")?\s*>(.*?)</longdescription>"#).unwrap());
    let flag = FLAG.get_or_init(|| Regex::new(r#"(?s)<flag\s+name="([^"]+)"[^>]*>(.*?)</flag>"#).unwrap());
    let up = UP.get_or_init(|| Regex::new(r"(?s)<upstream>(.*?)</upstream>").unwrap());
    let remote = REMOTE.get_or_init(|| {
        Regex::new(r#"(?s)<(remote-id|bugs-to|doc|changelog)(?:\s+type="([^"]*)")?[^>]*>(.*?)</(?:remote-id|bugs-to|doc|changelog)>"#).unwrap()
    });
    let slot = SLOT.get_or_init(|| Regex::new(r#"(?s)<slot\s+name="([^"]+)"\s*>(.*?)</slot>"#).unwrap());

    let mut x = PkgXml::default();
    for c in maint.captures_iter(t) {
        let mut m = Maintainer { kind: c.get(1).map(|m| m.as_str().to_string()).unwrap_or_default(), ..Default::default() };
        for f in field.captures_iter(&c[2]) {
            let v = strip_tags(&f[2]);
            match &f[1] {
                "email" => m.email = v,
                "name" => m.name = v,
                _ => m.description = v,
            }
        }
        x.maintainers.push(m);
    }
    for c in long.captures_iter(t) {
        let lang = c.get(1).map(|m| m.as_str()).unwrap_or("en");
        if lang == "en" || x.longdescription.is_empty() {
            x.longdescription = strip_tags(&c[2]);
        }
    }
    for c in flag.captures_iter(t) {
        x.flags.insert(c[1].to_string(), strip_tags(&c[2]));
    }
    if let Some(u) = up.captures(t) {
        for r in remote.captures_iter(&u[1]) {
            let label = match (&r[1], r.get(2)) {
                ("remote-id", Some(ty)) => ty.as_str().to_string(),
                (k, _) => k.to_string(),
            };
            x.upstream.push((label, strip_tags(&r[3])));
        }
    }
    for c in slot.captures_iter(t) {
        x.slots.push((c[1].to_string(), strip_tags(&c[2])));
    }
    x.stabilize_allarches = t.contains("<stabilize-allarches");
    x
}

/// Turn an upstream remote-id into a browsable URL where the scheme is known.
pub fn remote_url(kind: &str, value: &str) -> Option<String> {
    Some(match kind {
        "github" => format!("https://github.com/{value}"),
        "gitlab" => format!("https://gitlab.com/{value}"),
        "codeberg" => format!("https://codeberg.org/{value}"),
        "pypi" => format!("https://pypi.org/project/{value}/"),
        "crates-io" => format!("https://crates.io/crates/{value}"),
        "sourceforge" => format!("https://sourceforge.net/projects/{value}/"),
        "cpan" => format!("https://metacpan.org/dist/{value}"),
        "hackage" => format!("https://hackage.haskell.org/package/{value}"),
        "freedesktop-gitlab" => format!("https://gitlab.freedesktop.org/{value}"),
        "gnome-gitlab" => format!("https://gitlab.gnome.org/{value}"),
        "kde-invent" => format!("https://invent.kde.org/{value}"),
        "savannah" => format!("https://savannah.gnu.org/projects/{value}"),
        "bugs-to" | "doc" | "changelog" if value.starts_with("http") => value.to_string(),
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn xml() {
        let x = parse_xml(
            r#"<pkgmetadata><maintainer type="project"><email>vim@gentoo.org</email><name>Vim</name></maintainer>
            <longdescription lang="en">Long
            text</longdescription>
            <use><flag name="vim-pager">Install <pkg>app-editors/vim</pkg> pager</flag></use>
            <upstream><remote-id type="github">vim/vim</remote-id></upstream></pkgmetadata>"#,
        );
        assert_eq!(x.maintainers[0].email, "vim@gentoo.org");
        assert_eq!(x.maintainers[0].kind, "project");
        assert_eq!(x.longdescription, "Long text");
        assert_eq!(x.flags["vim-pager"], "Install app-editors/vim pager");
        assert_eq!(x.upstream[0], ("github".to_string(), "vim/vim".to_string()));
    }

    #[test]
    fn ebuild_vars() {
        let v = parse_ebuild_vars("EAPI=8\ninherit cargo xdg\nDESCRIPTION=\"A tool\"\nIUSE=\"+foo\n\tbar\"\nSLOT=\"0\"\n");
        assert_eq!(v["EAPI"], "8");
        assert_eq!(v["DESCRIPTION"], "A tool");
        assert_eq!(v["IUSE"], "+foo bar");
        assert_eq!(v["INHERIT"], "cargo xdg");
    }
}
