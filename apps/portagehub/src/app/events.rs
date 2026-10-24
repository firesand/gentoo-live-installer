//! Running jobs and batches: starting them, feeding helper events in and
//! reacting when they finish.

use super::jobs::{self, Batch, Job, JobKind, Step};
use super::loader::{cleanup_cache_path, save_world_cache};
use super::{Action, App, ToastKind};
use crate::data::Reload;
use crate::exec::{ExecMsg, HelperState, Priv};
use crate::portage::pretend::{self, Plan};
use portagehub::protocol::{Event, Op};
use std::sync::Arc;
use std::time::Instant;

/// After a make.conf write, before it is committed: can Portage still read it?
/// (A broken make.conf is not an error for Portage — it warns and silently
/// falls back to the profile defaults.)
fn verify_make_conf() -> Result<(), String> {
    use crate::portage::makeconf_validate::{self, ParseCheck};
    match makeconf_validate::strict_parse(std::path::Path::new(crate::portage::makeconf::MAKE_CONF)) {
        ParseCheck::Error(e) => Err(format!("Portage cannot parse the new make.conf: {e}")),
        ParseCheck::Ok(_) | ParseCheck::Unavailable(_) => {
            makeconf_validate::effective(&["CFLAGS", "USE"]).map(|_| ()).map_err(|e| format!("portageq fails with the new make.conf: {e}"))
        }
    }
}

impl App {
    pub(super) fn start_job(
        &mut self,
        title: String,
        argv: Vec<String>,
        env: Vec<(String, String)>,
        privilege: Priv,
        kind: JobKind,
        reload: Vec<Reload>,
    ) -> u64 {
        let id = self.exec.run(argv.clone(), env, privilege);
        self.jobs.push(Job::new(id, title, kind, privilege, argv, reload));
        // Keep the job list bounded.
        if self.jobs.len() > 60 {
            let drop_n = self.jobs.len() - 60;
            let mut removed = 0;
            self.jobs.retain(|j| {
                if removed < drop_n && !j.running() {
                    removed += 1;
                    false
                } else {
                    true
                }
            });
        }
        id
    }

    pub(super) fn handle_exec(&mut self, msg: ExecMsg) {
        match msg {
            ExecMsg::HelperReady(version) => {
                self.exec.on_helper_ready(version.as_deref());
                let mine = env!("CARGO_PKG_VERSION");
                if self.exec.helper_outdated {
                    self.toast(
                        ToastKind::Warn,
                        format!(
                            "The installed admin helper ({}) is older than PortageHub {mine}. Newer actions need the matching helper: run sudo dist/install.sh.",
                            version.unwrap_or_else(|| "0.1".into())
                        ),
                    );
                } else {
                    self.toast(ToastKind::Success, "Administrator session started — privileged actions won't ask again.");
                }
            }
            ExecMsg::HelperGone(reason) => {
                let was = self.exec.helper_state.clone();
                self.exec.on_helper_gone(reason.clone());
                if was != HelperState::NotStarted {
                    self.toast(ToastKind::Warn, format!("Administrator session ended: {reason}"));
                }
            }
            ExecMsg::Event(ev, at) => self.handle_event(ev, at),
        }
    }

    fn handle_event(&mut self, ev: Event, at: Instant) {
        let Some(id) = ev.id() else { return };
        // Batches first.
        if let Some(bi) = self.batches.iter().position(|b| b.current == Some(id)) {
            if let Event::Data { text, .. } = &ev {
                self.batches[bi].on_data(text);
            }
            if let Some(o) = jobs::outcome(&ev) {
                let step = self.batches[bi].on_outcome(o);
                self.batch_step(bi, step);
            }
            return;
        }
        let Some(ji) = self.jobs.iter().position(|j| j.id == id) else { return };
        let history = self.data.history.clone().unwrap_or_default();
        let job = &mut self.jobs[ji];
        match ev {
            Event::Started { .. } | Event::Ready { .. } => {}
            Event::Data { text, .. } => {
                job.lines.push((false, text));
            }
            Event::Ok { .. } => {
                // Helper operations (not programs) finish with Ok.
                job.exit = Some(Some(0));
                job.ended = Some(at);
                self.finish_job(ji);
            }
            Event::Line { stream, text, .. } => {
                if let JobKind::Emerge { tracker, .. } = &mut job.kind {
                    tracker.feed(&text, &history, at);
                }
                job.partial = None;
                job.lines.push((stream == portagehub::protocol::StreamKind::Stderr, text));
                if job.lines.len() > 60_000 {
                    job.lines.drain(..10_000);
                }
            }
            Event::Partial { text, .. } => {
                if let JobKind::Emerge { tracker, .. } = &mut job.kind {
                    tracker.feed(&text, &history, at);
                }
                job.partial = Some(text);
            }
            Event::Err { msg, .. } => {
                job.error = Some(msg);
                job.ended = Some(at);
                self.finish_job(ji);
            }
            Event::Exit { code, .. } => {
                job.exit = Some(code);
                job.ended = Some(at);
                self.finish_job(ji);
            }
        }
    }

    pub(super) fn finish_job(&mut self, ji: usize) {
        let job = self.jobs[ji].clone();
        let ok = job.success();
        for r in &job.reload {
            self.spawn_load(*r);
        }
        match &job.kind {
            JobKind::Pretend { opts } => {
                let mut plan: Plan = pretend::parse(&job.output(), job.exit.flatten());
                if let Some(e) = &job.error {
                    plan.messages.push(e.clone());
                }
                if self.emerge.pretend_job == Some(job.id) {
                    self.emerge.plan = Some(plan);
                    self.emerge.plan_opts = Some(opts.clone());
                }
            }
            JobKind::Emerge { .. } => {
                if let JobKind::Emerge { tracker, .. } = &mut self.jobs[ji].kind {
                    tracker.finish(ok);
                }
                if ok {
                    self.toast(ToastKind::Success, format!("{} finished successfully", job.title));
                } else {
                    self.toast(ToastKind::Error, format!("{} failed — see the log", job.title));
                }
                for r in [Reload::Vdb, Reload::History, Reload::Config] {
                    self.spawn_load(r);
                }
                if self.settings.auto_world_check {
                    self.actions.push(Action::CheckWorld);
                }
                self.spawn_load(Reload::Leftovers);
                let touched_kernel = match &job.kind {
                    JobKind::Emerge { tracker, .. } => tracker.items.iter().any(|i| {
                        i.stage == crate::portage::progress::Stage::Done
                            && (i.key.starts_with("sys-kernel/")
                                || i.key.starts_with("sys-boot/")
                                || i.key == "virtual/dist-kernel"
                                || i.key == "x11-drivers/nvidia-drivers")
                    }),
                    _ => false,
                };
                if !self.remount_restore.is_empty() && self.settings.boot_restore_ro {
                    let targets = std::mem::take(&mut self.remount_restore);
                    for t in targets {
                        self.actions.push(Action::RootOp {
                            title: format!("Make {t} read-only again"),
                            op: Op::Remount { target: t.clone(), rw: false },
                            kind: JobKind::Remount,
                            reload: vec![],
                        });
                    }
                }
                if touched_kernel && self.settings.boot_check_after_kernel {
                    self.kernel_attention = true;
                    self.toast(ToastKind::Info, "A kernel, bootloader or NVIDIA driver changed — checking the boot setup…");
                    self.spawn_load(Reload::Kernels);
                    if self.exec.root_ready() {
                        self.actions.push(Action::RootOp {
                            title: "Read boot configuration".into(),
                            op: Op::BootScan,
                            kind: JobKind::BootScan,
                            reload: vec![],
                        });
                    }
                }
                let out = job.output();
                if out.contains("needs updating") {
                    self.toast(ToastKind::Warn, "Some configuration files need updating (Configuration → Config updates).");
                }
                if out.contains("@preserved-rebuild") {
                    self.toast(ToastKind::Warn, "Preserved libraries detected: run “Rebuild preserved libraries”.");
                }
            }
            JobKind::Sync => {
                if ok {
                    self.toast(ToastKind::Success, format!("{} completed", job.title));
                } else {
                    self.toast(ToastKind::Error, format!("{} failed", job.title));
                }
                for r in [Reload::Repos, Reload::History] {
                    self.spawn_load(r);
                }
                if ok && self.settings.auto_world_check {
                    self.actions.push(Action::CheckWorld);
                }
            }
            JobKind::BootScan => {
                if let Some(text) = job.lines.iter().map(|l| l.1.as_str()).find(|l| l.starts_with('{'))
                    && let Ok(scan) = serde_json::from_str::<portagehub::protocol::BootScan>(text)
                {
                    self.boot_scan = Some((Arc::new(scan), crate::fmt::now_ts()));
                } else if let Some(e) = &job.error {
                    self.toast(ToastKind::Error, format!("Could not read the boot configuration: {e}"));
                }
                if self.kernel_attention {
                    self.report_boot_check();
                }
            }
            JobKind::BootFix => {
                if ok {
                    self.toast(ToastKind::Success, format!("{}: done", job.title));
                } else {
                    self.toast(ToastKind::Error, format!("{} failed: {}", job.title, job.error.clone().unwrap_or_else(|| "see the log".into())));
                }
                self.spawn_load(Reload::Kernels);
                self.spawn_load(Reload::Leftovers);
                if self.exec.root_ready() {
                    self.actions.push(Action::RootOp { title: "Read boot configuration".into(), op: Op::BootScan, kind: JobKind::BootScan, reload: vec![] });
                }
            }
            JobKind::Remount => {
                self.invalidate_mounts();
                if ok {
                    self.toast(ToastKind::Success, format!("{}: done", job.title));
                } else {
                    self.toast(
                        ToastKind::Error,
                        format!("{} failed: {}", job.title, job.error.clone().unwrap_or_else(|| job.output().lines().last().unwrap_or("").to_string())),
                    );
                }
            }
            JobKind::CleanupScan { tag } => {
                if job.error.is_none() {
                    let out = job.output();
                    let code = job.exit.flatten();
                    let ts = crate::fmt::now_ts();
                    let p = cleanup_cache_path(tag);
                    if let Some(d) = p.parent() {
                        let _ = std::fs::create_dir_all(d);
                    }
                    let _ = std::fs::write(&p, format!("{ts} {}\n{out}", code.map(|c| c.to_string()).unwrap_or_else(|| "-".into())));
                    self.cleanup_scans.insert(tag.clone(), (out, code, ts));
                } else {
                    self.toast(ToastKind::Error, format!("{} failed: {}", job.title, job.error.clone().unwrap_or_default()));
                }
            }
            JobKind::WorldCheck => {
                if self.world_job == Some(job.id) {
                    self.world_job = None;
                }
                if job.error.is_none() && job.exit.flatten().is_some() {
                    let out = job.output();
                    let code = job.exit.flatten();
                    let ts = crate::fmt::now_ts();
                    self.world_plan = Some((Arc::new(pretend::parse(&out, code)), ts));
                    save_world_cache(ts, code, &out);
                }
            }
            JobKind::Capture { tag } => {
                let c = self.captures.entry(tag.clone()).or_default();
                c.output = job.output();
                c.code = job.exit.flatten();
                c.done = true;
            }
            JobKind::Undo { tx } => {
                if ok {
                    self.toast(ToastKind::Success, format!("{}: done", job.title));
                    self.history_view.undo_conflict = None;
                } else if let Some(e) = job.error.as_deref().filter(|e| e.starts_with("changed since:")) {
                    self.history_view.undo_conflict = Some((tx.clone(), job.title.clone(), e.to_string()));
                } else {
                    self.toast(ToastKind::Error, format!("{} failed: {}", job.title, job.error.clone().unwrap_or_default()));
                }
                for r in [Reload::Audit, Reload::Config, Reload::MakeConf, Reload::Kernels] {
                    self.spawn_load(r);
                }
            }
            JobKind::Generic => {
                if ok {
                    self.toast(ToastKind::Success, format!("{}: done", job.title));
                } else {
                    let tail = job.lines.iter().rev().take(3).map(|l| l.1.clone()).collect::<Vec<_>>();
                    let reason = job.error.clone().unwrap_or_else(|| tail.into_iter().rev().collect::<Vec<_>>().join(" "));
                    self.toast(ToastKind::Error, format!("{} failed: {}", job.title, reason));
                }
            }
        }
    }

    /// Start a batch of root operations (see [`Batch`]).
    pub(super) fn start_batch(&mut self, mut batch: Batch) {
        let step = batch.start(self.exec.supports(&Op::Begin { label: String::new() }));
        self.batches.push(batch);
        let bi = self.batches.len() - 1;
        self.batch_step(bi, step);
    }

    fn batch_step(&mut self, bi: usize, step: Step) {
        match step {
            Step::Verify => {
                let r = verify_make_conf();
                let step = self.batches[bi].on_outcome(r);
                self.batch_step(bi, step);
            }
            Step::Send(op, tx) => {
                let id = self.exec.root_op_tx(op, tx);
                self.batches[bi].current = Some(id);
            }
            Step::Done => {
                let b = self.batches.remove(bi);
                self.toast(ToastKind::Success, format!("{} — saved", b.title));
                self.after_batch(&b.reload);
                if let Some(then) = b.then {
                    self.actions.push(*then);
                }
            }
            Step::Failed { error, rolled_back } => {
                let b = self.batches.remove(bi);
                let note = if rolled_back { " — nothing was changed" } else { "" };
                self.toast(ToastKind::Error, format!("{} failed: {error}{note}", b.title));
                self.after_batch(&b.reload);
            }
        }
    }

    fn after_batch(&mut self, reload: &[Reload]) {
        for r in reload {
            self.spawn_load(*r);
        }
        self.spawn_load(Reload::Audit);
    }
}
