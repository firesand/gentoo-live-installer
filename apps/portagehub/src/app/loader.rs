//! Background loading of Portage data, package details and the caches kept
//! in ~/.cache/portagehub.

use super::{App, ToastKind};
use crate::data::{self, DataMsg, PkgDetail, Reload};
use crate::portage::pretend::{self, Plan};
use crate::portage::{catalog, config, history, makeconf, repos, system, useflags, vdb};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

pub(super) fn world_cache_path() -> PathBuf {
    let base = std::env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".cache")))
        .unwrap_or_else(|| PathBuf::from("/tmp"));
    base.join("portagehub").join("world-check.txt")
}

/// First line: "<unix time> <exit code>", then the raw emerge output.
pub(super) fn load_world_cache() -> Option<(Arc<Plan>, i64)> {
    let text = std::fs::read_to_string(world_cache_path()).ok()?;
    let (head, body) = text.split_once('\n')?;
    let mut it = head.split_whitespace();
    let ts: i64 = it.next()?.parse().ok()?;
    let code: Option<i32> = it.next().and_then(|c| c.parse().ok());
    Some((Arc::new(pretend::parse(body, code)), ts))
}

pub(super) fn save_world_cache(ts: i64, code: Option<i32>, output: &str) {
    let p = world_cache_path();
    if let Some(d) = p.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let _ = std::fs::write(p, format!("{ts} {}\n{output}", code.map(|c| c.to_string()).unwrap_or_else(|| "-".into())));
}

pub(super) fn cleanup_cache_path(tag: &str) -> PathBuf {
    world_cache_path().with_file_name(format!("cleanup-{tag}.txt"))
}

pub(super) fn load_cleanup_cache() -> HashMap<String, (String, Option<i32>, i64)> {
    let mut m = HashMap::new();
    for tag in ["depclean", "distfiles", "binpkgs"] {
        if let Ok(text) = std::fs::read_to_string(cleanup_cache_path(tag))
            && let Some((head, body)) = text.split_once('\n')
        {
            let mut it = head.split_whitespace();
            let ts: i64 = it.next().and_then(|x| x.parse().ok()).unwrap_or(0);
            let code = it.next().and_then(|c| c.parse().ok());
            m.insert(tag.to_string(), (body.to_string(), code, ts));
        }
    }
    m
}

impl App {
    pub fn spawn_load(&mut self, r: Reload) {
        if self.loading.contains(&r) {
            // A load started from older data is still running: redo it when it lands.
            self.pending_reload.insert(r);
            return;
        }
        let snapshot = self.data.clone();
        let tx = self.data_tx.clone();
        let ctx = self.ctx.clone();
        match r {
            Reload::Catalog | Reload::UseDesc | Reload::News | Reload::Masks if snapshot.repos.is_none() => {
                self.spawn_load(Reload::Repos);
                return;
            }
            Reload::Kernels if snapshot.vdb.is_none() => return,
            Reload::Health if snapshot.makeconf.is_none() => return,
            Reload::Leftovers if snapshot.vdb.is_none() || snapshot.catalog.is_none() || snapshot.config.is_none() || snapshot.env.is_none() => return,
            Reload::Updates
                if snapshot.catalog.is_none() || snapshot.vdb.is_none() || snapshot.env.is_none() || snapshot.config.is_none() || snapshot.masks.is_none() =>
            {
                return;
            }
            _ => {}
        }
        self.loading.insert(r);
        let keep_set = self.settings.kernel_keep_set.clone();
        let log_days = self.settings.log_keep_days;
        std::thread::spawn(move || {
            let msg = match r {
                Reload::Repos => DataMsg::Repos(Arc::new(repos::load())),
                Reload::Catalog => DataMsg::Catalog(Arc::new(catalog::scan(snapshot.repos.as_ref().unwrap()))),
                Reload::Vdb => DataMsg::Vdb(Arc::new(vdb::load())),
                Reload::History => DataMsg::History(Arc::new(history::load())),
                Reload::Config => DataMsg::Config(Arc::new(config::load())),
                Reload::MakeConf => DataMsg::MakeConf(Arc::new(makeconf::load())),
                Reload::Env => DataMsg::Env(Arc::new(system::load_env())),
                Reload::UseDesc => {
                    let r = snapshot.repos.as_ref().unwrap();
                    let locs: Vec<&std::path::Path> = r.repos.iter().filter(|r| r.exists).map(|r| r.location.as_path()).collect();
                    DataMsg::UseDesc(Arc::new(useflags::load(&locs)))
                }
                Reload::RemoteRepos => {
                    let out = std::process::Command::new("eselect").args(["--colour=no", "repository", "list"]).output();
                    let text = out.map(|o| String::from_utf8_lossy(&o.stdout).to_string()).unwrap_or_default();
                    DataMsg::RemoteRepos(Arc::new(repos::parse_eselect_list(&text)))
                }
                Reload::Profiles => {
                    let out = std::process::Command::new("eselect").args(["--colour=no", "profile", "list"]).output();
                    let text = out.map(|o| String::from_utf8_lossy(&o.stdout).to_string()).unwrap_or_default();
                    DataMsg::Profiles(Arc::new(system::parse_profiles(&text)))
                }
                Reload::News => {
                    let r = snapshot.repos.as_ref().unwrap();
                    let mut all = Vec::new();
                    for repo in r.repos.iter().filter(|r| r.exists) {
                        all.extend(system::load_news(&repo.name, &repo.location));
                    }
                    all.sort_by(|a, b| b.posted.cmp(&a.posted));
                    DataMsg::News(Arc::new(all))
                }
                Reload::Updates => DataMsg::Updates(Arc::new(data::compute_updates(&snapshot))),
                Reload::Masks => DataMsg::Masks(Arc::new(crate::portage::profile::load(snapshot.repos.as_ref().unwrap()))),
                Reload::Kernels => DataMsg::Kernels(Arc::new(crate::portage::kernel::load(snapshot.vdb.as_ref().unwrap(), &keep_set))),
                Reload::Audit => DataMsg::Audit(Arc::new(crate::changes::load())),
                Reload::Tuning => DataMsg::Tuning(Arc::new(crate::portage::tuning::load())),
                Reload::Health => {
                    use crate::portage::{makeconf_check, makeconf_validate};
                    let empty = std::collections::HashMap::new();
                    let parse = makeconf_validate::strict_parse(std::path::Path::new(crate::portage::makeconf::MAKE_CONF));
                    let repo = snapshot.repos.as_ref().and_then(|r| r.get(&r.main_repo).map(|x| x.location.clone()));
                    let arch = snapshot.arch();
                    let input = makeconf_check::Input {
                        mc: snapshot.makeconf.as_ref().unwrap(),
                        cfg: snapshot.config.as_deref(),
                        arch: &arch,
                        effective: snapshot.env.as_ref().map(|e| &e.vars).unwrap_or(&empty),
                        tuning: snapshot.tuning.as_deref(),
                        usedesc: snapshot.usedesc.as_deref(),
                        vdb: snapshot.vdb.as_deref(),
                        repo: repo.as_deref(),
                        parse: Some(&parse),
                    };
                    DataMsg::Health(Arc::new(makeconf_check::check(&input)))
                }
                Reload::Leftovers => {
                    let env = snapshot.env.as_ref().unwrap();
                    DataMsg::Leftovers(Arc::new(crate::portage::cleanup::scan(
                        snapshot.vdb.as_ref().unwrap(),
                        snapshot.catalog.as_ref().unwrap(),
                        snapshot.config.as_ref().unwrap(),
                        env.arch(),
                        env.get("PORT_LOGDIR"),
                        log_days,
                    )))
                }
            };
            let _ = tx.send(msg);
            ctx.request_repaint();
        });
    }

    pub fn request_detail(&mut self, key: &str) {
        if self.details.contains_key(key) || self.detail_loading.contains(key) || self.data.catalog.is_none() {
            return;
        }
        self.detail_loading.insert(key.to_string());
        let snapshot = self.data.clone();
        let tx = self.data_tx.clone();
        let ctx = self.ctx.clone();
        let key = key.to_string();
        std::thread::spawn(move || {
            if let Some(d) = data::build_detail(&key, &snapshot) {
                let _ = tx.send(DataMsg::Detail(key, Arc::new(d)));
            } else {
                let _ = tx.send(DataMsg::Detail(
                    key.clone(),
                    Arc::new(PkgDetail {
                        key: key.clone(),
                        cat: key.split('/').next().unwrap_or("").into(),
                        pn: key.split('/').nth(1).unwrap_or("").into(),
                        versions: Vec::new(),
                        xml: Default::default(),
                        pkg_dir: None,
                        files: Vec::new(),
                    }),
                ));
            }
            ctx.request_repaint();
        });
    }

    /// Re-run the make.conf health check if it has been shown.
    fn refresh_health(&mut self) {
        if self.data.health.is_some() || self.loading.contains(&Reload::Health) {
            self.spawn_load(Reload::Health);
        }
    }

    pub(super) fn invalidate_details(&mut self) {
        self.details.clear();
        self.detail_loading.clear();
    }

    pub(super) fn handle_data(&mut self, msg: DataMsg) {
        let done = |s: &mut Self, r: Reload| {
            s.loading.remove(&r);
        };
        match msg {
            DataMsg::Repos(r) => {
                done(self, Reload::Repos);
                for w in &r.warnings {
                    self.toast(ToastKind::Warn, w.clone());
                }
                self.data.repos = Some(r);
                self.spawn_load(Reload::Catalog);
                self.spawn_load(Reload::UseDesc);
                self.spawn_load(Reload::News);
                self.spawn_load(Reload::Masks);
            }
            DataMsg::Catalog(c) => {
                done(self, Reload::Catalog);
                self.data.catalog = Some(c);
                self.invalidate_details();
                self.spawn_load(Reload::Updates);
                self.spawn_load(Reload::Leftovers);
            }
            DataMsg::Vdb(v) => {
                done(self, Reload::Vdb);
                self.data.vdb = Some(v);
                self.refresh_health();
                self.invalidate_details();
                self.spawn_load(Reload::Updates);
                self.spawn_load(Reload::Kernels);
                self.spawn_load(Reload::Leftovers);
            }
            DataMsg::History(h) => {
                done(self, Reload::History);
                self.data.history = Some(h);
            }
            DataMsg::Config(c) => {
                done(self, Reload::Config);
                self.data.config = Some(c);
                self.refresh_health();
                self.invalidate_details();
                self.spawn_load(Reload::Updates);
                self.spawn_load(Reload::Leftovers);
            }
            DataMsg::MakeConf(m) => {
                done(self, Reload::MakeConf);
                self.data.makeconf = Some(m);
                self.refresh_health();
            }
            DataMsg::Env(e) => {
                done(self, Reload::Env);
                self.data.env = Some(e);
                self.refresh_health();
                self.invalidate_details();
                self.spawn_load(Reload::Updates);
                self.spawn_load(Reload::Leftovers);
            }
            DataMsg::UseDesc(u) => {
                done(self, Reload::UseDesc);
                self.data.usedesc = Some(u);
                self.refresh_health();
            }
            DataMsg::RemoteRepos(r) => {
                done(self, Reload::RemoteRepos);
                self.data.remote_repos = Some(r);
            }
            DataMsg::Profiles(p) => {
                done(self, Reload::Profiles);
                self.data.profiles = Some(p);
            }
            DataMsg::News(n) => {
                done(self, Reload::News);
                self.data.news = Some(n);
            }
            DataMsg::Kernels(k) => {
                done(self, Reload::Kernels);
                self.data.kernels = Some(k);
                self.maintenance.show_nvidia_help = false;
                if self.kernel_attention && !self.exec.root_ready() {
                    self.report_boot_check();
                }
            }
            DataMsg::Leftovers(l) => {
                done(self, Reload::Leftovers);
                self.data.leftovers = Some(l);
            }
            DataMsg::Audit(a) => {
                done(self, Reload::Audit);
                self.data.audit = Some(a);
            }
            DataMsg::Tuning(t) => {
                done(self, Reload::Tuning);
                self.data.tuning = Some(t);
                self.refresh_health();
            }
            DataMsg::Health(h) => {
                done(self, Reload::Health);
                self.data.health = Some(h);
            }
            DataMsg::Masks(m) => {
                done(self, Reload::Masks);
                self.data.masks = Some(m);
                self.invalidate_details();
                self.spawn_load(Reload::Updates);
            }
            DataMsg::Updates(u) => {
                done(self, Reload::Updates);
                self.data.updates = Some(u);
            }
            DataMsg::Detail(k, d) => {
                self.detail_loading.remove(&k);
                self.details.insert(k, d);
            }
            DataMsg::Ebuild(p, t) => {
                self.ebuild_loading.remove(&p);
                self.ebuilds.insert(p, t);
            }
        }
        let ready: Vec<Reload> = self.pending_reload.iter().filter(|r| !self.loading.contains(r)).copied().collect();
        for r in ready {
            self.pending_reload.remove(&r);
            self.spawn_load(r);
        }
    }
}
