//! Jobs (one running command or helper operation each) and batches (a
//! sequence of helper operations run as one transaction). Plain state
//! machines, testable without a window.

use super::Action;
use crate::data::Reload;
use crate::exec::Priv;
use crate::portage::emerge::EmergeOptions;
use crate::portage::progress::Tracker;
use portagehub::protocol::{Event, Op};
use std::collections::VecDeque;
use std::time::Instant;

#[derive(Debug, Clone)]
pub enum JobKind {
    Generic,
    Pretend {
        opts: EmergeOptions,
    },
    Emerge {
        tracker: Box<Tracker>,
    },
    Sync,
    Capture {
        tag: String,
    },
    /// Background `emerge -uDN @world --pretend` for the Store's update list.
    WorldCheck,
    /// Root-only boot files (JSON from the helper).
    BootScan,
    /// Something that changed boot files: re-check afterwards.
    BootFix,
    /// `emerge --pretend --depclean`, eclean-* --pretend for the Cleanup page.
    CleanupScan {
        tag: String,
    },
    /// mount -o remount of a boot partition.
    Remount,
    /// Undo of a recorded change (helper transaction).
    Undo {
        tx: String,
    },
}

#[derive(Debug, Clone)]
pub struct Job {
    pub id: u64,
    pub title: String,
    pub kind: JobKind,
    pub privilege: Priv,
    pub argv: Vec<String>,
    pub lines: Vec<(bool, String)>,
    pub partial: Option<String>,
    pub started: Instant,
    pub ended: Option<Instant>,
    pub exit: Option<Option<i32>>,
    pub error: Option<String>,
    pub reload: Vec<Reload>,
}

impl Job {
    pub fn running(&self) -> bool {
        self.ended.is_none()
    }

    pub fn success(&self) -> bool {
        self.exit == Some(Some(0)) && self.error.is_none()
    }

    pub fn output(&self) -> String {
        let mut s = String::new();
        for (_, l) in &self.lines {
            s.push_str(l);
            s.push('\n');
        }
        s
    }

    pub fn status_text(&self) -> String {
        if self.running() {
            "Running".into()
        } else if let Some(e) = &self.error {
            format!("Error: {e}")
        } else {
            match self.exit {
                Some(Some(0)) => "Finished".into(),
                Some(Some(c)) => format!("Exited with code {c}"),
                Some(None) => "Killed".into(),
                None => "Finished".into(),
            }
        }
    }
}

impl Job {
    pub fn new(id: u64, title: String, kind: JobKind, privilege: Priv, argv: Vec<String>, reload: Vec<Reload>) -> Job {
        Job { id, title, kind, privilege, argv, lines: Vec::new(), partial: None, started: Instant::now(), ended: None, exit: None, error: None, reload }
    }
}

/// How a request ended, from the events the helper sends.
pub fn outcome(ev: &Event) -> Option<Result<(), String>> {
    match ev {
        Event::Ok { .. } | Event::Exit { code: Some(0), .. } => Some(Ok(())),
        Event::Exit { code: Some(c), .. } => Some(Err(format!("command failed (exit {c})"))),
        Event::Exit { signal, .. } => Some(Err(format!("command was stopped (signal {})", signal.unwrap_or(0)))),
        Event::Err { msg, .. } => Some(Err(msg.clone())),
        _ => None,
    }
}

/// Does an operation change files the helper can roll back?
pub fn changes_files(op: &Op) -> bool {
    matches!(op, Op::WriteFile { .. } | Op::DeleteFile { .. } | Op::Mkdir { .. } | Op::Rename { .. } | Op::ConfigUpdate { .. } | Op::MarkGood { .. })
}

#[derive(Debug, Clone, PartialEq)]
pub enum TxPhase {
    /// No transaction (nothing to roll back, or a helper without transactions).
    None,
    /// `Begin` sent; its id arrives as Data.
    Beginning(Option<String>),
    Open(String),
    /// All steps ran; the result is being checked before it is committed.
    Verifying(Option<String>),
    Committing(String),
    /// A step failed with the error; the rollback is running.
    RollingBack(String, String),
}

/// What the app has to do next for a batch.
#[derive(Debug, Clone, PartialEq)]
pub enum Step {
    /// Send this operation (in this transaction).
    Send(Op, Option<String>),
    /// Check the result now and report it with `on_outcome`; a failure rolls
    /// the transaction back.
    Verify,
    /// Every step succeeded.
    Done,
    /// A step failed (changes already rolled back when `rolled_back`).
    Failed { error: String, rolled_back: bool },
}

/// Root operations run one after another. When one of them changes files,
/// the batch runs in a helper transaction: committed when every step
/// succeeded, rolled back when one failed — so a batch is all or nothing.
pub struct Batch {
    pub title: String,
    pub ops: VecDeque<Op>,
    /// Request in flight.
    pub current: Option<u64>,
    pub reload: Vec<Reload>,
    pub then: Option<Box<Action>>,
    pub done: usize,
    pub tx: TxPhase,
    /// Check the result before committing (make.conf writes).
    pub verify: bool,
}

impl Batch {
    pub fn new(title: String, ops: Vec<Op>, reload: Vec<Reload>, then: Option<Box<Action>>) -> Batch {
        Batch { title, ops: ops.into(), current: None, reload, then, done: 0, tx: TxPhase::None, verify: false }
    }

    /// First step. `transactions`: the helper supports Begin/Commit/Rollback.
    pub fn start(&mut self, transactions: bool) -> Step {
        if transactions && self.ops.iter().any(changes_files) {
            self.tx = TxPhase::Beginning(None);
            return Step::Send(Op::Begin { label: self.title.clone() }, None);
        }
        self.next()
    }

    fn next(&mut self) -> Step {
        let tx = match &self.tx {
            TxPhase::Open(t) => Some(t.clone()),
            _ => None,
        };
        match self.ops.pop_front() {
            Some(op) => Step::Send(op, tx),
            None if self.verify => {
                self.verify = false;
                self.tx = TxPhase::Verifying(tx);
                Step::Verify
            }
            None => match tx {
                Some(t) => {
                    self.tx = TxPhase::Committing(t.clone());
                    Step::Send(Op::Commit { transaction: t }, None)
                }
                None => Step::Done,
            },
        }
    }

    /// Structured data for the request in flight (the id from `Begin`).
    pub fn on_data(&mut self, text: &str) {
        if let TxPhase::Beginning(id) = &mut self.tx {
            *id = Some(text.trim().to_string());
        }
    }

    /// The request in flight ended.
    pub fn on_outcome(&mut self, r: Result<(), String>) -> Step {
        match (std::mem::replace(&mut self.tx, TxPhase::None), r) {
            (TxPhase::Beginning(Some(id)), Ok(())) => {
                self.tx = TxPhase::Open(id);
                self.next()
            }
            // No transaction support after all: each step is still recorded on its own.
            (TxPhase::Beginning(_), _) => self.next(),
            (TxPhase::Open(t), Ok(())) => {
                self.tx = TxPhase::Open(t);
                self.done += 1;
                self.next()
            }
            (TxPhase::Open(t), Err(e)) => {
                self.tx = TxPhase::RollingBack(t.clone(), e);
                Step::Send(Op::Rollback { transaction: t, force: true }, None)
            }
            (TxPhase::Verifying(Some(t)), Ok(())) => {
                self.tx = TxPhase::Committing(t.clone());
                Step::Send(Op::Commit { transaction: t }, None)
            }
            (TxPhase::Verifying(Some(t)), Err(e)) => {
                self.tx = TxPhase::RollingBack(t.clone(), e);
                Step::Send(Op::Rollback { transaction: t, force: true }, None)
            }
            (TxPhase::Verifying(None), Ok(())) => Step::Done,
            (TxPhase::Verifying(None), Err(e)) => Step::Failed {
                error: format!("{e} — the change was written but could not be rolled back (no transaction support); check make.conf in the text editor"),
                rolled_back: false,
            },
            (TxPhase::Committing(_), Ok(())) => Step::Done,
            (TxPhase::Committing(_), Err(e)) => {
                Step::Failed { error: format!("changes were saved, but recording them for undo failed: {e}"), rolled_back: false }
            }
            (TxPhase::RollingBack(_, e), Ok(())) => Step::Failed { error: e, rolled_back: true },
            (TxPhase::RollingBack(_, e), Err(e2)) => Step::Failed { error: format!("{e}; undoing the earlier steps also failed: {e2}"), rolled_back: false },
            (TxPhase::None, Ok(())) => {
                self.done += 1;
                self.next()
            }
            (TxPhase::None, Err(e)) => Step::Failed { error: e, rolled_back: false },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(p: &str) -> Op {
        Op::WriteFile { path: p.into(), content: "x".into(), backup: false }
    }

    fn sent(s: &Step) -> (&Op, Option<&str>) {
        match s {
            Step::Send(op, tx) => (op, tx.as_deref()),
            other => panic!("expected Send, got {other:?}"),
        }
    }

    #[test]
    fn file_batches_run_in_one_transaction() {
        let mut b = Batch::new("Apply".into(), vec![write("/etc/portage/a"), write("/etc/portage/b")], vec![], None);
        let s = b.start(true);
        assert!(matches!(sent(&s).0, Op::Begin { label } if label == "Apply"));
        b.on_data("42-1\n");
        let s = b.on_outcome(Ok(()));
        assert_eq!(sent(&s), (&write("/etc/portage/a"), Some("42-1")));
        let s = b.on_outcome(Ok(()));
        assert_eq!(sent(&s).1, Some("42-1"));
        let s = b.on_outcome(Ok(()));
        assert!(matches!(sent(&s).0, Op::Commit { transaction } if transaction == "42-1"));
        assert_eq!(b.on_outcome(Ok(())), Step::Done);
        assert_eq!(b.done, 2);
    }

    #[test]
    fn a_failed_step_rolls_back_the_earlier_ones() {
        let mut b = Batch::new("Apply".into(), vec![write("/etc/portage/a"), write("/etc/portage/b"), write("/etc/portage/c")], vec![], None);
        b.start(true);
        b.on_data("7-7");
        b.on_outcome(Ok(()));
        b.on_outcome(Ok(()));
        let s = b.on_outcome(Err("disk full".into()));
        assert!(matches!(sent(&s).0, Op::Rollback { transaction, force: true } if transaction == "7-7"));
        assert_eq!(b.on_outcome(Ok(())), Step::Failed { error: "disk full".into(), rolled_back: true });
        assert_eq!(b.ops.len(), 1, "the remaining step never ran");
    }

    #[test]
    fn without_transactions_or_file_changes_steps_just_run() {
        let remount = Op::Remount { target: "/boot".into(), rw: true };
        let mut b = Batch::new("Prepare".into(), vec![remount.clone()], vec![], None);
        assert_eq!(b.start(true), Step::Send(remount, None));
        assert_eq!(b.on_outcome(Ok(())), Step::Done);

        // Old helper: no Begin at all.
        let mut b = Batch::new("Apply".into(), vec![write("/etc/portage/a")], vec![], None);
        assert_eq!(sent(&b.start(false)).1, None);
        assert_eq!(b.on_outcome(Err("nope".into())), Step::Failed { error: "nope".into(), rolled_back: false });

        // Begin refused (e.g. helper too old after all): continue without.
        let mut b = Batch::new("Apply".into(), vec![write("/etc/portage/a")], vec![], None);
        b.start(true);
        let s = b.on_outcome(Err("unsupported request".into()));
        assert_eq!(sent(&s), (&write("/etc/portage/a"), None));
        assert_eq!(b.on_outcome(Ok(())), Step::Done);
    }

    #[test]
    fn verification_runs_before_commit_and_rolls_back() {
        let mut b = Batch::new("make.conf".into(), vec![write("/etc/portage/make.conf")], vec![], None);
        b.verify = true;
        b.start(true);
        b.on_data("9-9");
        b.on_outcome(Ok(()));
        assert_eq!(b.on_outcome(Ok(())), Step::Verify, "check after the last step, before Commit");
        let s = b.on_outcome(Err("Portage cannot parse make.conf".into()));
        assert!(matches!(sent(&s).0, Op::Rollback { transaction, .. } if transaction == "9-9"));
        assert_eq!(b.on_outcome(Ok(())), Step::Failed { error: "Portage cannot parse make.conf".into(), rolled_back: true });

        let mut b = Batch::new("make.conf".into(), vec![write("/etc/portage/make.conf")], vec![], None);
        b.verify = true;
        b.start(true);
        b.on_data("9-9");
        b.on_outcome(Ok(()));
        b.on_outcome(Ok(()));
        assert!(matches!(sent(&b.on_outcome(Ok(()))).0, Op::Commit { .. }));
        assert_eq!(b.on_outcome(Ok(())), Step::Done);
    }

    #[test]
    fn rollback_failure_is_reported() {
        let mut b = Batch::new("Apply".into(), vec![write("/etc/portage/a")], vec![], None);
        b.start(true);
        b.on_data("1-1");
        b.on_outcome(Ok(()));
        b.on_outcome(Err("first".into()));
        match b.on_outcome(Err("second".into())) {
            Step::Failed { error, rolled_back: false } => assert!(error.contains("first") && error.contains("second")),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn outcomes() {
        assert_eq!(outcome(&Event::Exit { id: 1, code: Some(0), signal: None }), Some(Ok(())));
        assert!(outcome(&Event::Exit { id: 1, code: Some(1), signal: None }).unwrap().is_err());
        assert!(outcome(&Event::Exit { id: 1, code: None, signal: Some(2) }).unwrap().unwrap_err().contains("signal 2"));
        assert_eq!(outcome(&Event::Line { id: 1, stream: portagehub::protocol::StreamKind::Stdout, text: String::new() }), None);
    }
}
