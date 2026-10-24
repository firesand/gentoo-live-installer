//! Actions requested by the views, and the operations behind them.

use super::jobs::{Batch, Job, JobKind};
use super::{Action, App, Captured, FileChange, ReviewDialog, Tab, ToastKind};
use crate::data::{DataMsg, Reload};
use crate::exec::{HelperState, Priv};
use crate::portage::makeconf;
use crate::portage::progress::{self, Tracker};
use crate::views;
use portagehub::protocol::Op;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

impl App {
    pub(super) fn process_actions(&mut self) {
        let actions = std::mem::take(&mut self.actions);
        for a in actions {
            self.apply(a);
        }
    }

    fn apply(&mut self, a: Action) {
        match a {
            Action::Open(key) => {
                self.request_detail(&key);
                self.detail.open(&key);
                if self.tab != Tab::Library {
                    self.tab = Tab::Store;
                    self.store.show_detail = true;
                } else {
                    self.library.selected = Some(key);
                }
            }
            Action::Tab(t) => self.tab = t,
            Action::Search(q) => {
                self.tab = Tab::Store;
                self.store.query = q;
                self.store.show_detail = false;
                self.store.page = views::store::Page::Search;
            }
            Action::Pretend(opts) => {
                let mut env = opts.env.clone();
                env.retain(|(_, v)| !v.is_empty());
                let mut argv = vec!["emerge".to_string()];
                argv.extend(opts.pretend_args());
                let privilege = if self.settings.pretend_as_root { Priv::Root } else { Priv::User };
                let title = format!("Preview: {}", opts.command_line());
                let id = self.start_job(title, argv, env, privilege, JobKind::Pretend { opts: opts.clone() }, vec![]);
                self.emerge.pretend_job = Some(id);
                self.emerge.boot_override = false;
                self.emerge.plan = None;
                self.emerge.plan_opts = None;
                self.emerge.options = opts;
                self.emerge.page = views::emerge::Page::Review;
                self.tab = Tab::Emerge;
            }
            Action::RePretend => {
                if let Some(o) = self.emerge.plan_opts.clone().or_else(|| Some(self.emerge.options.clone())) {
                    self.apply(Action::Pretend(o));
                }
            }
            Action::Emerge(opts, planned) => {
                if self.running_emerge().is_some() {
                    self.toast(ToastKind::Warn, "Another emerge is already running. Wait for it to finish.");
                    return;
                }
                let touches_boot = planned
                    .iter()
                    .any(|(cpv, _, _)| crate::portage::atom::split_cpv(cpv).is_some_and(|(c, p, _)| crate::portage::mounts::touches_boot(&format!("{c}/{p}"))));
                if touches_boot && self.settings.boot_auto_remount {
                    self.invalidate_mounts();
                    let blocked: Vec<crate::portage::mounts::BootMount> = self.boot_mounts().into_iter().filter(|m| m.blocks_writes()).collect();
                    if !blocked.is_empty() {
                        for m in &blocked {
                            if m.fstab_ro && !self.remount_restore.contains(&m.target) {
                                self.remount_restore.push(m.target.clone());
                            }
                        }
                        let ops = blocked.iter().map(|m| Op::Remount { target: m.target.clone(), rw: true }).collect();
                        self.toast(
                            ToastKind::Info,
                            format!(
                                "Making {} writable for the kernel/bootloader update…",
                                blocked.iter().map(|m| m.target.as_str()).collect::<Vec<_>>().join(", ")
                            ),
                        );
                        self.apply(Action::Batch {
                            title: "Prepare boot partitions".into(),
                            ops,
                            reload: vec![],
                            then: Some(Box::new(Action::Emerge(opts, planned))),
                        });
                        self.invalidate_mounts();
                        return;
                    }
                }
                let history = self.data.history.clone().unwrap_or_default();
                let tracker = Box::new(Tracker::new(&planned, &history));
                let mut argv = vec!["emerge".to_string()];
                argv.extend(opts.run_args());
                let mut env = opts.env.clone();
                env.retain(|(_, v)| !v.is_empty());
                let title = format!("emerge {}", opts.args().join(" "));
                let kind = if opts.action == crate::portage::emerge::Action::Sync { JobKind::Sync } else { JobKind::Emerge { tracker } };
                let id = self.start_job(title, argv, env, Priv::Root, kind, vec![]);
                self.emerge.active_job = Some(id);
                self.emerge.page = views::emerge::Page::Activity;
                self.tab = Tab::Emerge;
            }
            Action::Root { title, argv, reload, kind } => {
                let id = self.start_job(title, argv, vec![], Priv::Root, kind, reload);
                self.emerge.selected_job = Some(id);
            }
            Action::Capture { tag, argv, privilege } => {
                self.start_job(argv.join(" "), argv, vec![], privilege, JobKind::Capture { tag: tag.clone() }, vec![]);
                self.captures.insert(tag, Captured::default());
            }
            Action::Batch { title, ops, reload, then } => {
                if !ops.is_empty() {
                    self.start_batch(Batch::new(title, ops, reload, then));
                }
            }
            Action::Write { title, changes, reload, then } => {
                if changes.is_empty() {
                    self.toast(ToastKind::Info, "Nothing to change.");
                    return;
                }
                // make.conf is checked with Portage's own parser before anything
                // is written: a broken file would be silently ignored by Portage.
                let mc: Vec<(PathBuf, String)> = changes
                    .iter()
                    .filter(|c| crate::portage::makeconf::is_make_conf(&c.path))
                    .filter_map(|c| c.new.clone().map(|n| (c.path.clone(), n)))
                    .collect();
                if !mc.is_empty()
                    && let crate::portage::makeconf_validate::ParseCheck::Error(e) = crate::portage::makeconf_validate::check_candidate(&mc)
                {
                    self.toast(ToastKind::Error, format!("Not saved — {e}"));
                    return;
                }
                if self.settings.review_writes {
                    self.review = Some(ReviewDialog { title, changes, reload, then, expanded: 0 });
                } else {
                    self.apply_changes(title, changes, reload, then);
                }
            }
            Action::Reload(r) => self.spawn_load(r),
            Action::Cancel(id) => {
                if let Some(j) = self.jobs.iter().find(|j| j.id == id) {
                    let p = j.privilege;
                    self.exec.cancel(id, p, libc::SIGINT);
                    self.toast(ToastKind::Info, "Stop requested (SIGINT). emerge will stop after cleaning up.");
                }
            }
            Action::StartHelper => {
                if !self.exec.root_ready() && self.exec.helper_state != HelperState::Starting {
                    self.exec.start_helper();
                }
            }
            Action::Job { title, argv, kind } => {
                self.start_job(title, argv, vec![], Priv::User, kind, vec![]);
            }
            Action::RootOp { title, op, kind, reload } => {
                let needs_new_helper = op.min_helper() > self.exec.helper_version;
                let id = self.exec.alloc_id();
                let label = format!("{op:?}").split([' ', '{']).next().unwrap_or("").to_string();
                self.jobs.push(Job::new(id, title, kind, Priv::Root, vec![format!("helper: {label}")], reload));
                if needs_new_helper && !self.exec.is_root && self.exec.helper_state == HelperState::Ready {
                    let ji = self.jobs.len() - 1;
                    self.jobs[ji].error =
                        Some("the installed admin helper is too old for this action — run sudo dist/install.sh, then restart PortageHub".into());
                    self.jobs[ji].ended = Some(Instant::now());
                    self.finish_job(ji);
                } else {
                    self.exec.root_op_with_id(id, op);
                }
            }
            Action::CheckWorld => {
                if self.world_job.is_some_and(|id| self.job(id).is_some_and(|j| j.running())) {
                    return;
                }
                let opts = (crate::portage::emerge::recipes()[0].build)();
                // Low priority: it only reads, and can take a while on big systems.
                let mut argv = vec!["nice".to_string(), "-n".into(), "19".into(), "emerge".into()];
                argv.extend(opts.pretend_args());
                let id = self.start_job("Checking @world updates (emerge --pretend)".into(), argv, vec![], Priv::User, JobKind::WorldCheck, vec![]);
                self.world_job = Some(id);
            }
            Action::LoadDetail(k) => self.request_detail(&k),
            Action::LoadEbuild(p) => {
                if !self.ebuilds.contains_key(&p) && self.ebuild_loading.insert(p.clone()) {
                    let tx = self.data_tx.clone();
                    let ctx = self.ctx.clone();
                    std::thread::spawn(move || {
                        let t = std::fs::read_to_string(&p).unwrap_or_else(|e| format!("# cannot read {}: {e}", p.display()));
                        let _ = tx.send(DataMsg::Ebuild(p, Arc::new(t)));
                        ctx.request_repaint();
                    });
                }
            }
        }
    }

    pub fn apply_changes(&mut self, title: String, changes: Vec<FileChange>, reload: Vec<Reload>, then: Option<Box<Action>>) {
        // Changes are whole-file rewrites computed from the text read earlier:
        // if a file changed since (another fix, an editor), writing would undo
        // that change. Refuse and let the view recompute.
        if let Some(c) = changes.iter().find(|c| match std::fs::read_to_string(&c.path) {
            Ok(now) => now != c.old,
            Err(e) => e.kind() == std::io::ErrorKind::NotFound && !c.old.is_empty(),
        }) {
            self.toast(ToastKind::Warn, format!("{} changed since this was prepared — nothing was written. Check again and retry.", c.path.display()));
            for r in reload {
                self.spawn_load(r);
            }
            return;
        }
        let backup = self.settings.backup_on_write;
        let verifies = changes.iter().any(|c| crate::portage::makeconf::is_make_conf(&c.path));
        let ops: Vec<Op> = changes
            .into_iter()
            .map(|c| match c.new {
                Some(content) => Op::WriteFile { path: c.path.to_string_lossy().to_string(), content, backup },
                None => Op::DeleteFile { path: c.path.to_string_lossy().to_string() },
            })
            .collect();
        let mut batch = Batch::new(title, ops, reload, then);
        // make.conf changes are checked once written and rolled back if Portage cannot read them.
        batch.verify = verifies;
        self.start_batch(batch);
    }

    /// Boot partitions as currently mounted (refreshed at most every 2 s).
    pub fn boot_mounts(&mut self) -> Vec<crate::portage::mounts::BootMount> {
        if self.boot_mounts_cache.0.elapsed() > Duration::from_secs(2) {
            self.boot_mounts_cache = (Instant::now(), crate::portage::mounts::load());
        }
        self.boot_mounts_cache.1.clone()
    }

    pub fn invalidate_mounts(&mut self) {
        self.boot_mounts_cache.0 = Instant::now() - Duration::from_secs(60);
    }

    /// Remount a boot partition through the helper; `restore` marks it to go
    /// back to read-only after the next emerge.
    pub fn remount_boot(&mut self, target: &str, rw: bool, restore: bool) {
        if rw && restore && !self.remount_restore.iter().any(|t| t == target) {
            self.remount_restore.push(target.to_string());
        }
        if !rw {
            self.remount_restore.retain(|t| t != target);
        }
        self.apply(Action::RootOp {
            title: format!("Make {target} {}", if rw { "writable" } else { "read-only" }),
            op: Op::Remount { target: target.to_string(), rw },
            kind: JobKind::Remount,
            reload: vec![],
        });
    }

    /// Toast the result of the boot check after a kernel-related change.
    pub(super) fn report_boot_check(&mut self) {
        self.kernel_attention = false;
        let Some(ks) = self.data.kernels.clone() else { return };
        let scan = self.boot_scan.as_ref().map(|(s, _)| s.clone());
        let findings = crate::portage::kernel::check(&ks, scan.as_deref(), self.settings.kernel_protect, 2, self.settings.nvidia_alerts);
        use crate::portage::kernel::Severity;
        let bad: Vec<_> = findings.iter().filter(|f| f.severity >= Severity::Warn).collect();
        if bad.is_empty() {
            self.toast(ToastKind::Success, "Boot check after the kernel/driver update: everything is in place.");
        } else {
            for f in bad.iter().take(3) {
                self.toast(
                    if f.severity == Severity::Error { ToastKind::Error } else { ToastKind::Warn },
                    format!("{} — open Maintenance → Kernels & Boot", f.title),
                );
            }
        }
    }

    /// Record the running kernel as known-good and protect it (and the previous one) from depclean.
    pub fn mark_kernel_good(&mut self, kver: String) {
        let Some(ks) = self.data.kernels.clone() else { return };
        if kver != ks.running {
            self.toast(ToastKind::Warn, format!("Only the kernel you are running can be marked known-good — boot {kver} first."));
            return;
        }
        if self.settings.kernel_mark_requires_nvidia && ks.nvidia.gpu && ks.nvidia.drivers.is_some() {
            let ok = std::process::Command::new("nvidia-smi").arg("-L").output().is_ok_and(|o| o.status.success());
            if !ok {
                self.toast(
                    ToastKind::Error,
                    "nvidia-smi failed: the NVIDIA driver does not work on this kernel, so it is not marked known-good. Rebuild nvidia-drivers first.",
                );
                return;
            }
        }
        if !self.exec.supports(&Op::MarkGood { kver: String::new(), state_dir: String::new(), backup: false }) {
            self.toast(ToastKind::Error, "The installed admin helper is too old for this — run sudo dist/install.sh, then restart PortageHub.");
            return;
        }
        let new_prev = match &ks.last_good {
            Some(l) if *l != kver => Some(l.clone()),
            _ => ks.previous_good.clone(),
        };
        let mut ops =
            vec![Op::MarkGood { kver: kver.clone(), state_dir: ks.state_dir.to_string_lossy().to_string(), backup: self.settings.kernel_backup_on_mark }];
        if self.settings.kernel_protect {
            let atoms = crate::portage::kernel::wanted_keep_atoms(&ks, Some(&kver), new_prev.as_deref());
            let mut content = String::from("# Proven-good kernels protected from --depclean. Managed by PortageHub (same format as kernel-mark-good).\n");
            for a in &atoms {
                content.push_str(a);
                content.push('\n');
            }
            ops.push(Op::WriteFile { path: format!("/etc/portage/sets/{}", ks.keep_set), content, backup: false });
            if !ks.keep_set_in_world {
                ops.push(Op::Run { argv: vec!["emerge".into(), "--noreplace".into(), "--ask=n".into(), format!("@{}", ks.keep_set)], env: vec![], cwd: None });
            }
        }
        self.apply(Action::Batch { title: format!("Mark {kver} as known-good"), ops, reload: vec![Reload::Kernels, Reload::Config, Reload::Vdb], then: None });
    }

    /// PORTAGE_TMPDIR for a package, honouring package.env overrides (e.g. a "notmpfs" env file).
    pub fn tmpdir_for(&self, cpv: &str) -> PathBuf {
        use crate::portage::config::ConfKind;
        let mut dir = self.data.env.as_ref().map(|e| e.get("PORTAGE_TMPDIR").to_string()).filter(|s| !s.is_empty()).unwrap_or_else(|| "/var/tmp".into());
        if let (Some(cfg), Some((c, p, v))) = (self.data.config.as_ref(), crate::portage::atom::split_cpv(cpv))
            && let Some(set) = cfg.set(ConfKind::Env)
        {
            for f in set.files.iter().filter(|f| !f.ignored) {
                for (_, e) in f.entries() {
                    if e.disabled || !e.atom.as_ref().is_some_and(|a| a.matches(&c, &p, &v, None, None)) {
                        continue;
                    }
                    for name in &e.values {
                        if let Some(envf) = cfg.env_files.iter().find(|x| x.name == *name) {
                            for (k, val, _) in makeconf::scan(&envf.content) {
                                if k == "PORTAGE_TMPDIR" && !val.is_empty() {
                                    dir = val;
                                }
                            }
                        }
                    }
                }
            }
        }
        PathBuf::from(dir)
    }

    /// Tail build logs of running packages to show their phase.
    pub(super) fn poll_build_logs(&mut self) {
        if self.last_log_poll.elapsed() < Duration::from_millis(1500) {
            return;
        }
        self.last_log_poll = Instant::now();
        // Resolve each active package's build dir first (PORTAGE_TMPDIR can differ per package).
        let mut dirs: HashMap<String, PathBuf> = HashMap::new();
        for j in self.jobs.iter().filter(|j| j.running()) {
            if let JobKind::Emerge { tracker, .. } = &j.kind {
                for i in tracker.items.iter().filter(|i| i.stage.active()) {
                    dirs.insert(i.cpv.clone(), self.tmpdir_for(&i.cpv));
                }
            }
        }
        for j in self.jobs.iter_mut().filter(|j| j.running()) {
            if let JobKind::Emerge { tracker, .. } = &mut j.kind {
                let active: Vec<(usize, PathBuf)> = tracker
                    .items
                    .iter()
                    .enumerate()
                    .filter(|(_, i)| i.stage.active())
                    .filter_map(|(n, i)| dirs.get(&i.cpv).and_then(|d| i.build_dir_log(d)).map(|p| (n, p)))
                    .collect();
                for (n, p) in active {
                    if let Some(t) = progress::tail(&p, 16 * 1024) {
                        tracker.set_phase_from_log(n, &t);
                    }
                }
            }
        }
    }
}
