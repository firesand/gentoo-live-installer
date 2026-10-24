//! Loaded application data and the per-package detail model.

use crate::portage::atom::Atom;
use crate::portage::catalog::Catalog;
use crate::portage::config::{ConfKind, PortageConfig};
use crate::portage::history::History;
use crate::portage::makeconf::MakeConf;
use crate::portage::metadata::{self, EbuildMeta, PkgXml};
use crate::portage::profile::MaskDb;
use crate::portage::repos::{RemoteRepo, ReposConf};
use crate::portage::system::{Env, NewsItem, Profile};
use crate::portage::useflags::UseDescDb;
use crate::portage::vdb::Vdb;
use crate::portage::version::Version;
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::Arc;

#[derive(Clone, Default)]
pub struct Data {
    pub repos: Option<Arc<ReposConf>>,
    pub catalog: Option<Arc<Catalog>>,
    pub vdb: Option<Arc<Vdb>>,
    pub history: Option<Arc<History>>,
    pub config: Option<Arc<PortageConfig>>,
    pub makeconf: Option<Arc<MakeConf>>,
    pub env: Option<Arc<Env>>,
    pub usedesc: Option<Arc<UseDescDb>>,
    pub remote_repos: Option<Arc<Vec<RemoteRepo>>>,
    pub profiles: Option<Arc<Vec<Profile>>>,
    pub news: Option<Arc<Vec<NewsItem>>>,
    pub updates: Option<Arc<Vec<UpdateCandidate>>>,
    pub masks: Option<Arc<MaskDb>>,
    pub kernels: Option<Arc<crate::portage::kernel::KernelState>>,
    pub leftovers: Option<Arc<crate::portage::cleanup::Leftovers>>,
}

impl Data {
    /// Newest sync time from emerge.log or the repositories' own timestamps.
    pub fn last_sync(&self) -> Option<i64> {
        let from_log = self.history.as_ref().and_then(|h| h.last_sync());
        let from_repo = self.repos.as_ref().and_then(|r| {
            r.repos.iter().filter_map(|x| x.last_sync).max().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs() as i64)
        });
        from_log.max(from_repo)
    }

    pub fn arch(&self) -> String {
        self.env.as_ref().map(|e| e.arch().to_string()).unwrap_or_else(|| "amd64".into())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Reload {
    Repos,
    Catalog,
    Vdb,
    History,
    Config,
    MakeConf,
    Env,
    UseDesc,
    RemoteRepos,
    Profiles,
    News,
    Updates,
    Masks,
    Kernels,
    Leftovers,
}

pub enum DataMsg {
    Repos(Arc<ReposConf>),
    Catalog(Arc<Catalog>),
    Vdb(Arc<Vdb>),
    History(Arc<History>),
    Config(Arc<PortageConfig>),
    MakeConf(Arc<MakeConf>),
    Env(Arc<Env>),
    UseDesc(Arc<UseDescDb>),
    RemoteRepos(Arc<Vec<RemoteRepo>>),
    Profiles(Arc<Vec<Profile>>),
    News(Arc<Vec<NewsItem>>),
    Updates(Arc<Vec<UpdateCandidate>>),
    Masks(Arc<MaskDb>),
    Kernels(Arc<crate::portage::kernel::KernelState>),
    Leftovers(Arc<crate::portage::cleanup::Leftovers>),
    Detail(String, Arc<PkgDetail>),
    Ebuild(PathBuf, Arc<String>),
    /// A loader panicked: the page it feeds must stop waiting.
    Failed(Reload, String),
    /// Building the detail page of a package panicked.
    DetailFailed(String, String),
    /// Result of the `nvidia-smi -L` check before marking a kernel known-good.
    NvidiaProbe { kver: String, ok: bool, detail: String },
}

fn panic_text(payload: Box<dyn std::any::Any + Send>) -> String {
    payload.downcast_ref::<&str>().map(|s| s.to_string()).or_else(|| payload.downcast_ref::<String>().cloned()).unwrap_or_else(|| "unknown error".into())
}

/// Run a loader. A panic in it becomes [`DataMsg::Failed`]: otherwise its page would show
/// "loading" for ever, because only a finished load clears the flag.
pub fn run_loader(r: Reload, f: impl FnOnce() -> DataMsg) -> DataMsg {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(f)).unwrap_or_else(|p| DataMsg::Failed(r, panic_text(p)))
}

/// Like [`run_loader`] for the detail page of `key`.
pub fn run_detail(key: &str, f: impl FnOnce() -> DataMsg) -> DataMsg {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(f)).unwrap_or_else(|p| DataMsg::DetailFailed(key.to_string(), panic_text(p)))
}

impl Reload {
    /// What the load is called in a message.
    pub fn name(&self) -> &'static str {
        match self {
            Reload::Repos => "repositories",
            Reload::Catalog => "the package tree",
            Reload::Vdb => "installed packages",
            Reload::History => "the build history",
            Reload::Config => "/etc/portage",
            Reload::MakeConf => "make.conf",
            Reload::Env => "the Portage settings",
            Reload::UseDesc => "USE flag descriptions",
            Reload::RemoteRepos => "the overlay list",
            Reload::Profiles => "profiles",
            Reload::News => "news",
            Reload::Updates => "the update check",
            Reload::Masks => "package masks",
            Reload::Kernels => "kernels",
            Reload::Leftovers => "leftovers",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KwState {
    Stable,
    Testing,
    /// No keyword for this arch at all.
    Missing,
    /// Explicitly broken (`-arch` or `-*`).
    Broken,
}

impl KwState {
    pub fn label(&self, arch: &str) -> String {
        match self {
            KwState::Stable => arch.to_string(),
            KwState::Testing => format!("~{arch}"),
            KwState::Missing => "no keyword".into(),
            KwState::Broken => format!("-{arch}"),
        }
    }
}

pub fn kw_state(keywords: &[String], arch: &str) -> KwState {
    if keywords.iter().any(|k| k == arch) {
        KwState::Stable
    } else if keywords.iter().any(|k| *k == format!("~{arch}")) {
        KwState::Testing
    } else if keywords.iter().any(|k| *k == format!("-{arch}") || k == "-*") {
        KwState::Broken
    } else {
        KwState::Missing
    }
}

#[derive(Debug, Clone)]
pub struct VersionInfo {
    pub ver: Version,
    pub repo: String,
    pub meta: EbuildMeta,
    pub kw: KwState,
    pub ebuild_path: PathBuf,
    /// What the user's /etc/portage says about this exact version.
    pub user_masked: bool,
    pub user_unmasked: bool,
    pub user_keyworded: bool,
    pub installed: bool,
    /// Masked by a repository or profile package.mask: (file, reason lines).
    pub profile_mask: Option<(PathBuf, Vec<String>)>,
}

impl VersionInfo {
    pub fn cpv(&self, key: &str) -> String {
        format!("{key}-{}", self.ver)
    }

    /// Local estimate (profile masks are not evaluated).
    pub fn masked(&self) -> bool {
        (self.user_masked || self.profile_mask.is_some()) && !self.user_unmasked
    }

    pub fn visible(&self, accepts_testing: bool) -> bool {
        if self.masked() {
            return false;
        }
        match self.kw {
            KwState::Stable => true,
            KwState::Testing => accepts_testing || self.user_keyworded,
            KwState::Missing | KwState::Broken => self.user_keyworded,
        }
    }
}

#[derive(Debug, Clone)]
pub struct PkgDetail {
    pub key: String,
    pub cat: String,
    pub pn: String,
    /// Highest version first.
    pub versions: Vec<VersionInfo>,
    pub xml: PkgXml,
    pub pkg_dir: Option<PathBuf>,
    pub files: Vec<String>,
}

impl PkgDetail {
    pub fn best_visible(&self, accepts_testing: bool) -> Option<&VersionInfo> {
        self.versions.iter().find(|v| v.visible(accepts_testing) && !v.ver.is_live())
    }

    pub fn slots(&self) -> Vec<String> {
        let mut v: Vec<String> = Vec::new();
        for x in &self.versions {
            let s = x.meta.slot().split('/').next().unwrap_or("0").to_string();
            if !v.contains(&s) {
                v.push(s);
            }
        }
        v
    }
}

fn user_flags(cfg: Option<&PortageConfig>, kind: ConfKind, cat: &str, pn: &str, ver: &Version, slot: &str, repo: &str) -> bool {
    let Some(cfg) = cfg else { return false };
    let Some(set) = cfg.set(kind) else { return false };
    set.files.iter().filter(|f| !f.ignored).any(|f| {
        f.entries().any(|(_, e)| !e.disabled && e.atom.as_ref().is_some_and(|a| a.matches(cat, pn, ver, Some(slot), Some(repo))))
    })
}

/// Keyword tokens the user's package.accept_keywords (and legacy package.keywords)
/// grant this version. An entry without tokens means "~arch".
fn user_keywords(cfg: Option<&PortageConfig>, cat: &str, pn: &str, ver: &Version, slot: &str, repo: &str, arch: &str) -> Vec<String> {
    let mut out = Vec::new();
    let Some(cfg) = cfg else { return out };
    for kind in [ConfKind::AcceptKeywords, ConfKind::Keywords] {
        let Some(set) = cfg.set(kind) else { continue };
        for f in set.files.iter().filter(|f| !f.ignored) {
            for (_, e) in f.entries() {
                if e.disabled || !e.atom.as_ref().is_some_and(|a| a.matches(cat, pn, ver, Some(slot), Some(repo))) {
                    continue;
                }
                if e.values.is_empty() {
                    out.push(format!("~{arch}"));
                } else {
                    out.extend(e.values.iter().cloned());
                }
            }
        }
    }
    out
}

/// Do the granted tokens make a version with this keyword state acceptable?
pub fn keywords_accept(kw: KwState, accepted: &[String], arch: &str) -> bool {
    if accepted.iter().any(|k| k == "**") {
        return true;
    }
    match kw {
        KwState::Stable => true,
        KwState::Testing => accepted.iter().any(|k| *k == format!("~{arch}") || k == "~*"),
        KwState::Missing => false,
        KwState::Broken => accepted.iter().any(|k| k == "-*" || *k == format!("-{arch}")),
    }
}

pub fn build_detail(key: &str, data: &Data) -> Option<PkgDetail> {
    let catalog = data.catalog.as_ref()?;
    let pkg = catalog.get(key)?;
    let arch = data.arch();
    let cfg = data.config.as_deref();
    let installed: HashSet<String> = data
        .vdb
        .as_ref()
        .map(|v| v.installed(key).iter().map(|i| i.ver.raw.clone()).collect())
        .unwrap_or_default();
    let mut versions = Vec::new();
    for e in pkg.ebuilds.iter().rev() {
        let Some(loc) = catalog.repo_location(&e.repo) else { continue };
        let meta = metadata::load(loc, &pkg.cat, &pkg.pn, &e.ver.raw);
        let kw = kw_state(&meta.keywords(), &arch);
        let slot = meta.slot().to_string();
        let ver = e.ver.clone();
        let accepted = user_keywords(cfg, &pkg.cat, &pkg.pn, &ver, &slot, &e.repo, &arch);
        let keyworded = keywords_accept(kw, &accepted, &arch);
        let profile_mask = data
            .masks
            .as_ref()
            .and_then(|m| m.masked(&pkg.cat, &pkg.pn, &ver, Some(&slot), Some(&e.repo)))
            .map(|m| (m.source.clone(), m.reason.clone()));
        versions.push(VersionInfo {
            profile_mask,
            ebuild_path: metadata::ebuild_path(loc, &pkg.cat, &pkg.pn, &e.ver.raw),
            user_masked: user_flags(cfg, ConfKind::Mask, &pkg.cat, &pkg.pn, &ver, &slot, &e.repo),
            user_unmasked: user_flags(cfg, ConfKind::Unmask, &pkg.cat, &pkg.pn, &ver, &slot, &e.repo),
            user_keyworded: keyworded,
            installed: installed.contains(&e.ver.raw),
            repo: e.repo.to_string(),
            kw,
            meta,
            ver,
        });
    }
    // metadata.xml from the repo providing the newest version, falling back to any.
    let mut xml = PkgXml::default();
    let mut pkg_dir = None;
    for v in &versions {
        if let Some(dir) = catalog.pkg_dir(&v.repo, &pkg.cat, &pkg.pn) {
            let x = metadata::load_xml(&dir);
            let has = !x.maintainers.is_empty() || !x.flags.is_empty() || !x.longdescription.is_empty();
            if pkg_dir.is_none() {
                pkg_dir = Some(dir.clone());
            }
            if has {
                xml = x;
                pkg_dir = Some(dir);
                break;
            }
        }
    }
    let mut files = Vec::new();
    if let Some(d) = &pkg_dir {
        fn walk(base: &std::path::Path, dir: &std::path::Path, out: &mut Vec<String>) {
            for e in std::fs::read_dir(dir).into_iter().flatten().flatten() {
                let p = e.path();
                if p.is_dir() {
                    walk(base, &p, out);
                } else if let Ok(r) = p.strip_prefix(base) {
                    out.push(r.to_string_lossy().to_string());
                }
            }
        }
        walk(&d.join("files"), &d.join("files"), &mut files);
        files.sort();
    }
    Some(PkgDetail { key: key.to_string(), cat: pkg.cat.clone(), pn: pkg.pn.clone(), versions, xml, pkg_dir, files })
}

#[derive(Debug, Clone)]
pub struct UpdateCandidate {
    pub key: String,
    pub installed: String,
    pub available: String,
    pub repo: String,
    pub slot: String,
    /// Installed packages whose dependencies rule out the new version: (cpv, atom).
    pub held_by: Vec<(String, String)>,
}

/// Installed packages with a newer visible version in the same slot (estimate).
pub fn compute_updates(data: &Data) -> Vec<UpdateCandidate> {
    let (Some(vdb), Some(_)) = (data.vdb.as_ref(), data.catalog.as_ref()) else { return Vec::new() };
    let testing = data.env.as_ref().is_some_and(|e| e.accepts_testing());
    let mut out = Vec::new();
    for (key, idx) in &vdb.by_key {
        let Some(detail) = build_detail(key, data) else { continue };
        for &i in idx {
            let inst = &vdb.pkgs[i];
            let slot = inst.main_slot().to_string();
            let best = detail
                .versions
                .iter()
                .filter(|v| !v.ver.is_live() && v.meta.slot().split('/').next() == Some(slot.as_str()))
                .find(|v| v.visible(testing));
            if let Some(b) = best
                && b.ver > inst.ver
                && !inst.ver.is_live()
            {
                out.push(UpdateCandidate {
                    key: key.clone(),
                    installed: inst.ver.raw.clone(),
                    available: b.ver.raw.clone(),
                    repo: b.repo.clone(),
                    slot: b.meta.slot().to_string(),
                    held_by: Vec::new(),
                });
            }
        }
    }
    out.sort_by(|a, b| a.key.cmp(&b.key));
    find_held_back(vdb, data.config.as_deref(), &mut out);
    out
}

/// Mark candidates whose new version is excluded by an installed package's
/// dependency (e.g. `<x11-libs/fltk-1.4:1` from tigervnc).
fn find_held_back(vdb: &Vdb, cfg: Option<&PortageConfig>, cands: &mut [UpdateCandidate]) {
    use crate::portage::dep;
    let keys: HashSet<&str> = cands.iter().map(|c| c.key.as_str()).collect();
    let mut by_key: std::collections::HashMap<String, Vec<(String, Atom)>> = Default::default();
    // Version pins in the world file and in user sets that @world includes.
    for a in &vdb.world {
        if keys.contains(a.key().as_str()) && a.ver.is_some() {
            by_key.entry(a.key()).or_default().push(("@world (world file)".into(), a.clone()));
        }
    }
    if let Some(sets) = cfg.and_then(|c| c.set(ConfKind::Sets)) {
        for f in sets.files.iter().filter(|f| !f.ignored) {
            let name = format!("@{}", f.rel);
            if !vdb.world_sets.iter().any(|w| *w == name) {
                continue;
            }
            for (_, e) in f.entries() {
                if let Some(a) = &e.atom
                    && !e.disabled
                    && a.ver.is_some()
                    && keys.contains(a.key().as_str())
                {
                    by_key.entry(a.key()).or_default().push((format!("your set {name}"), a.clone()));
                }
            }
        }
    }
    for p in &vdb.pkgs {
        let own = p.key();
        for file in ["RDEPEND", "DEPEND", "BDEPEND", "PDEPEND", "IDEPEND"] {
            let text = p.read(file);
            if text.is_empty() || !keys.iter().any(|k| text.contains(k)) {
                continue;
            }
            let tree = dep::parse(&text);
            let mut atoms = Vec::new();
            dep::collect_required_atoms(&tree, &mut atoms);
            for a in atoms {
                let k = a.key();
                if a.blocker == 0 && k != own && keys.contains(k.as_str()) {
                    let list = by_key.entry(k).or_default();
                    if !list.iter().any(|(c, x)| *c == p.cpv() && x.to_string() == a.to_string()) {
                        list.push((p.cpv(), a.clone()));
                    }
                }
            }
        }
    }
    for c in cands.iter_mut() {
        let Some(deps) = by_key.get(&c.key) else { continue };
        let (cat, pn) = c.key.split_once('/').unwrap_or(("", ""));
        let (Some(old), Some(new)) = (Version::parse(&c.installed), Version::parse(&c.available)) else { continue };
        let inst_slot = vdb.installed(&c.key).iter().find(|i| i.ver == old).map(|i| i.slot.clone()).unwrap_or_default();
        for (cpv, atom) in deps {
            if atom.matches(cat, pn, &old, Some(&inst_slot), None) && !atom.matches(cat, pn, &new, Some(&c.slot), None) {
                c.held_by.push((cpv.clone(), atom.to_string()));
            }
        }
    }
}

/// What `emerge -uDN @world` does with an installed package that has a newer version.
#[derive(Debug, Clone, PartialEq)]
pub enum UpdState {
    /// Part of the @world update.
    WillUpdate,
    /// emerge skipped it because of a conflict (text: what requires the old version).
    Skipped(String),
    /// Another installed package or one of your sets only allows the installed version.
    HeldBack(String),
    /// Nothing in @world pulls it in any more; depclean would remove it.
    NotNeeded,
    /// Not checked with emerge yet.
    Unknown,
}

impl UpdState {
    pub fn label(&self) -> &'static str {
        match self {
            UpdState::WillUpdate => "IN @WORLD UPDATE",
            UpdState::Skipped(_) => "SKIPPED",
            UpdState::HeldBack(_) => "HELD BACK",
            UpdState::NotNeeded => "NOT IN @WORLD",
            UpdState::Unknown => "",
        }
    }

    pub fn explain(&self) -> String {
        match self {
            UpdState::WillUpdate => "emerge -uDN @world will update this.".into(),
            UpdState::Skipped(why) => format!("emerge skipped this update because of a conflict: {why}"),
            UpdState::HeldBack(why) => format!("Held back: {why}. emerge keeps the installed version until that changes."),
            UpdState::NotNeeded => "Nothing you selected (@world) needs this package any more, so a world update leaves it alone. emerge --depclean would remove it; update it from its page if you still use it.".into(),
            UpdState::Unknown => "Not checked with emerge yet.".into(),
        }
    }
}

pub fn classify(c: &UpdateCandidate, plan: Option<&crate::portage::pretend::Plan>) -> UpdState {
    use crate::portage::pretend::Problem;
    let slot = c.slot.split('/').next().unwrap_or("");
    let held = || c.held_by.iter().map(|(by, atom)| format!("{by} needs {atom}")).collect::<Vec<_>>().join("; ");
    let Some(plan) = plan else {
        return if c.held_by.is_empty() { UpdState::Unknown } else { UpdState::HeldBack(held()) };
    };
    let updated = plan.merge_entries().any(|e| {
        e.key == c.key && e.version != c.installed && (e.slot.is_empty() || e.slot.split('/').next() == Some(slot))
    });
    if updated {
        return UpdState::WillUpdate;
    }
    for p in &plan.problems {
        if let Problem::Skipped { slot_key, required, .. } = p
            && slot_key.split(':').next() == Some(c.key.as_str())
        {
            let why = required.iter().map(|(a, by)| format!("{by} requires {a}")).collect::<Vec<_>>().join("; ");
            return UpdState::Skipped(why);
        }
    }
    if !c.held_by.is_empty() {
        return UpdState::HeldBack(held());
    }
    UpdState::NotNeeded
}

/// Atoms in RDEPEND of installed packages that reference `key` ("required by").
pub fn reverse_deps(vdb: &Vdb, key: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for p in &vdb.pkgs {
        if p.key() == key {
            continue;
        }
        for file in ["RDEPEND", "DEPEND", "BDEPEND", "PDEPEND"] {
            let text = p.read(file);
            if !text.contains(key) {
                continue;
            }
            let hit = text.split_whitespace().any(|tok| Atom::parse(tok).is_some_and(|a| a.blocker == 0 && a.key() == key));
            if hit {
                out.push((p.cpv(), file.to_string()));
                break;
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keyword_acceptance() {
        let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect::<Vec<_>>();
        assert!(keywords_accept(KwState::Testing, &s(&["~amd64"]), "amd64"));
        assert!(!keywords_accept(KwState::Missing, &s(&["~amd64"]), "amd64"));
        assert!(keywords_accept(KwState::Missing, &s(&["**"]), "amd64"));
        assert!(keywords_accept(KwState::Testing, &s(&["~*"]), "amd64"));
        assert!(!keywords_accept(KwState::Testing, &s(&[]), "amd64"));
        assert!(keywords_accept(KwState::Stable, &s(&[]), "amd64"));
    }

    #[test]
    fn a_panicking_loader_becomes_a_failure_not_a_spinner() {
        let ok = run_loader(Reload::Vdb, || DataMsg::Vdb(Arc::new(Default::default())));
        assert!(matches!(ok, DataMsg::Vdb(_)));
        let failed = run_loader(Reload::Config, || panic!("bad config file"));
        match failed {
            DataMsg::Failed(Reload::Config, why) => assert_eq!(why, "bad config file"),
            other => panic!("{}", matches!(other, DataMsg::Vdb(_))),
        }
        // String payloads (panic!("{x}")) are reported too.
        let x = 5;
        assert!(matches!(run_loader(Reload::Env, || panic!("value {x}")), DataMsg::Failed(Reload::Env, w) if w == "value 5"));
        assert!(matches!(run_detail("app-misc/foo", || panic!("boom")), DataMsg::DetailFailed(k, w) if k == "app-misc/foo" && w == "boom"));
        assert!(matches!(run_detail("app-misc/foo", || DataMsg::Detail("app-misc/foo".into(), Arc::new(PkgDetail { key: "app-misc/foo".into(), cat: "app-misc".into(), pn: "foo".into(), versions: vec![], xml: Default::default(), pkg_dir: None, files: vec![] }))), DataMsg::Detail(..)));
        assert_eq!(Reload::Kernels.name(), "kernels");
    }
}
