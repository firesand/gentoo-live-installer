//! Helper operations that belong together run as one unit.
//!
//! Several package.use edits, or the three steps that turn a `package.use` file into a
//! directory (rename it away, create the directory, move the file in), must land completely or
//! not at all: a failure half way would leave the configuration broken. A batch whose leading
//! operations change files therefore runs inside a helper transaction:
//!
//! ```text
//! Begin → op → op → … → Commit          (any failure: Rollback, then report)
//! ```
//!
//! Programs (`Op::Run`) and the other operations that follow stay outside the transaction, and a
//! helper that does not know transactions (an older installed one) gets the plain sequence.
//! This module is the pure state machine; `App` sends the steps and feeds back the replies.

use portagehub::protocol::Op;
use std::collections::VecDeque;

/// Operations that change files and can be recorded in (and undone from) a transaction.
pub fn is_file_op(op: &Op) -> bool {
    matches!(op, Op::WriteFile { .. } | Op::DeleteFile { .. } | Op::Mkdir { .. } | Op::Rename { .. } | Op::ConfigUpdate { .. } | Op::MarkGood { .. })
}

/// A sequence of operations that is not atomic by itself: two or more file changes, or any
/// rename or directory creation (which only make sense together with their other steps).
fn needs_transaction(file_ops: &[&Op]) -> bool {
    file_ops.len() >= 2 || file_ops.iter().any(|op| matches!(op, Op::Rename { .. } | Op::Mkdir { .. }))
}

#[derive(Debug, PartialEq)]
pub enum Step {
    /// Send `op`, as part of transaction `tx` when given.
    Send { op: Op, tx: Option<String> },
    Finished(Finish),
}

#[derive(Debug, PartialEq)]
pub enum Finish {
    /// Everything ran. `tx` is the committed transaction (undo it with `Op::Rollback`);
    /// `warning` says what was not quite right although the changes are in place.
    Saved { tx: Option<String>, warning: Option<String> },
    /// A step failed and the transaction was rolled back: nothing changed.
    RolledBack { reason: String },
    /// A step failed and what ran before it stays in effect (a plain batch, steps after the
    /// commit, or a rollback that failed too). `tx` is the committed transaction, if there is one.
    Failed { reason: String, rollback_error: Option<String>, tx: Option<String> },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase {
    Beginning,
    InTransaction,
    Committing,
    RollingBack,
    Plain,
}

pub struct Run {
    title: String,
    pending: VecDeque<Op>,
    /// How many of the leading `pending` operations belong to the transaction.
    in_tx: usize,
    tx: Option<String>,
    /// The transaction id the helper sent for `Begin`.
    data: Option<String>,
    committed: Option<String>,
    failure: Option<String>,
    warning: Option<String>,
    phase: Phase,
    done: usize,
    total: usize,
}

impl Run {
    /// Plan a batch. `transactions` is false for a helper that cannot do them.
    pub fn new(title: &str, ops: Vec<Op>, transactions: bool) -> Run {
        let leading: Vec<&Op> = ops.iter().take_while(|op| is_file_op(op)).collect();
        let in_tx = if transactions && needs_transaction(&leading) { leading.len() } else { 0 };
        let total = ops.len();
        Run {
            title: title.to_string(),
            pending: ops.into(),
            in_tx,
            tx: None,
            data: None,
            committed: None,
            failure: None,
            warning: None,
            phase: if in_tx > 0 { Phase::Beginning } else { Phase::Plain },
            done: 0,
            total,
        }
    }

    /// (operations finished, operations in total) for the progress display.
    pub fn progress(&self) -> (usize, usize) {
        (self.done, self.total)
    }

    /// The first step (an empty batch finishes at once).
    pub fn first(&mut self) -> Step {
        match self.phase {
            Phase::Beginning => Step::Send { op: Op::Begin { label: self.title.chars().take(200).collect() }, tx: None },
            _ => self.next_plain(),
        }
    }

    /// The helper answered a request with structured data (the id of a new transaction).
    pub fn note_data(&mut self, text: String) {
        self.data = Some(text);
    }

    /// The step that was sent has finished with `reply`; what next?
    pub fn advance(&mut self, reply: Result<(), String>) -> Step {
        match (self.phase, reply) {
            (Phase::Beginning, Ok(())) => match self.data.take().filter(|t| !t.is_empty()) {
                Some(tx) => {
                    self.tx = Some(tx);
                    self.phase = Phase::InTransaction;
                    self.next_in_tx()
                }
                None => self.finish_failed("the helper did not return a transaction id".into()),
            },
            // A helper from before transactions: run the plain sequence instead.
            (Phase::Beginning, Err(e)) if unsupported(&e) => {
                self.in_tx = 0;
                self.phase = Phase::Plain;
                self.next_plain()
            }
            (Phase::Beginning, Err(e)) => self.finish_failed(e),

            (Phase::InTransaction, Ok(())) => {
                self.done += 1;
                self.in_tx -= 1;
                if self.in_tx > 0 {
                    self.next_in_tx()
                } else {
                    self.phase = Phase::Committing;
                    Step::Send { op: Op::Commit { tx: self.tx.clone().unwrap_or_default() }, tx: None }
                }
            }
            (Phase::InTransaction, Err(e)) => {
                self.failure = Some(e);
                self.phase = Phase::RollingBack;
                Step::Send { op: Op::Rollback { tx: self.tx.clone().unwrap_or_default(), force: true }, tx: None }
            }

            (Phase::Committing, Ok(())) => {
                self.committed = self.tx.clone();
                self.phase = Phase::Plain;
                self.next_plain()
            }
            (Phase::Committing, Err(e)) => {
                // The files are written; only the record for undo is missing.
                self.warning = Some(if unsupported(&e) {
                    "The installed admin helper cannot record changes for undo — run sudo dist/install.sh to update it.".to_string()
                } else {
                    format!("The changes were saved, but could not be recorded for undo: {e}")
                });
                self.phase = Phase::Plain;
                self.next_plain()
            }

            (Phase::RollingBack, Ok(())) => Step::Finished(Finish::RolledBack { reason: self.failure.take().unwrap_or_default() }),
            (Phase::RollingBack, Err(e)) => {
                Step::Finished(Finish::Failed { reason: self.failure.take().unwrap_or_default(), rollback_error: Some(e), tx: None })
            }

            (Phase::Plain, Ok(())) => {
                self.done += 1;
                self.next_plain()
            }
            (Phase::Plain, Err(e)) => self.finish_failed(e),
        }
    }

    fn next_in_tx(&mut self) -> Step {
        let op = self.pending.pop_front().expect("a transaction step was planned");
        Step::Send { op, tx: self.tx.clone() }
    }

    fn next_plain(&mut self) -> Step {
        match self.pending.pop_front() {
            Some(op) => Step::Send { op, tx: None },
            None => Step::Finished(Finish::Saved { tx: self.committed.take(), warning: self.warning.take() }),
        }
    }

    fn finish_failed(&mut self, reason: String) -> Step {
        Step::Finished(Finish::Failed { reason, rollback_error: None, tx: self.committed.take() })
    }
}

/// The answer of a helper that does not know the operation (see `parse_request`).
fn unsupported(error: &str) -> bool {
    error.contains("unsupported request")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(p: &str) -> Op {
        Op::WriteFile { path: format!("/etc/portage/package.use/{p}"), content: "x\n".into(), backup: false, expect: None }
    }

    fn run_cmd() -> Op {
        Op::Run { argv: vec!["emerge".into(), "--noreplace".into(), "@kernels-keep".into()], env: vec![], cwd: None }
    }

    fn send(step: &Step) -> (&'static str, Option<&str>) {
        match step {
            Step::Send { op, tx } => (op.name(), tx.as_deref()),
            Step::Finished(_) => ("finished", None),
        }
    }

    #[test]
    fn two_file_changes_run_in_one_transaction() {
        let mut run = Run::new("Two edits", vec![write("a"), write("b")], true);
        assert_eq!(run.progress(), (0, 2));
        assert_eq!(send(&run.first()), ("begin", None));
        run.note_data("123-1".into());
        assert_eq!(send(&run.advance(Ok(()))), ("write_file", Some("123-1")));
        assert_eq!(send(&run.advance(Ok(()))), ("write_file", Some("123-1")));
        assert_eq!(send(&run.advance(Ok(()))), ("commit", None));
        assert_eq!(run.progress(), (2, 2));
        assert_eq!(run.advance(Ok(())), Step::Finished(Finish::Saved { tx: Some("123-1".into()), warning: None }));
    }

    #[test]
    fn a_failure_rolls_back_and_nothing_changes() {
        let mut run = Run::new("Convert", crate::edits::convert_to_dir_ops(std::path::Path::new("/etc/portage/package.use")), true);
        assert_eq!(send(&run.first()), ("begin", None));
        run.note_data("9-9".into());
        assert_eq!(send(&run.advance(Ok(()))), ("rename", Some("9-9")));
        assert_eq!(send(&run.advance(Ok(()))), ("mkdir", Some("9-9")));
        // Step two fails: package.use is back in place after the rollback.
        let step = run.advance(Err("/etc/portage/package.use: Permission denied".into()));
        assert_eq!(step, Step::Send { op: Op::Rollback { tx: "9-9".into(), force: true }, tx: None });
        assert_eq!(run.advance(Ok(())), Step::Finished(Finish::RolledBack { reason: "/etc/portage/package.use: Permission denied".into() }));
    }

    #[test]
    fn a_failed_rollback_is_reported_with_the_original_failure() {
        let mut run = Run::new("x", vec![write("a"), write("b")], true);
        run.first();
        run.note_data("t".into());
        run.advance(Ok(()));
        run.advance(Err("disk full".into()));
        assert_eq!(
            run.advance(Err("cannot restore".into())),
            Step::Finished(Finish::Failed { reason: "disk full".into(), rollback_error: Some("cannot restore".into()), tx: None })
        );
    }

    #[test]
    fn a_lone_edit_is_already_atomic_and_stays_plain() {
        let mut run = Run::new("one", vec![write("a")], true);
        assert_eq!(send(&run.first()), ("write_file", None));
        assert_eq!(run.advance(Ok(())), Step::Finished(Finish::Saved { tx: None, warning: None }));
        // ... unless it is a rename, which only makes sense with its other steps.
        let mut run = Run::new("rename", vec![Op::Rename { from: "/etc/portage/a".into(), to: "/etc/portage/b".into(), expect: None }], true);
        assert_eq!(send(&run.first()), ("begin", None));
    }

    #[test]
    fn programs_stay_outside_the_transaction() {
        // Mark a kernel known-good: two file changes, then emerge.
        let mut run = Run::new("mark", vec![write("a"), write("b"), run_cmd()], true);
        assert_eq!(send(&run.first()), ("begin", None));
        run.note_data("t".into());
        assert_eq!(send(&run.advance(Ok(()))), ("write_file", Some("t")));
        assert_eq!(send(&run.advance(Ok(()))), ("write_file", Some("t")));
        assert_eq!(send(&run.advance(Ok(()))), ("commit", None));
        assert_eq!(send(&run.advance(Ok(()))), ("run", None));
        // emerge fails: the committed changes stay and can still be undone.
        assert_eq!(
            run.advance(Err("command failed (exit Some(1))".into())),
            Step::Finished(Finish::Failed { reason: "command failed (exit Some(1))".into(), rollback_error: None, tx: Some("t".into()) })
        );
        // A batch that starts with a program is plain from the start.
        let mut run = Run::new("p", vec![run_cmd(), write("a"), write("b")], true);
        assert_eq!(send(&run.first()), ("run", None));
    }

    #[test]
    fn an_old_helper_gets_the_plain_sequence() {
        let mut run = Run::new("old", vec![write("a"), write("b")], false);
        assert_eq!(send(&run.first()), ("write_file", None));
        assert_eq!(send(&run.advance(Ok(()))), ("write_file", None));
        assert_eq!(run.advance(Ok(())), Step::Finished(Finish::Saved { tx: None, warning: None }));
        // A helper that only turns out to be old when it answers Begin falls back the same way.
        let mut run = Run::new("older", vec![write("a"), write("b")], true);
        assert_eq!(send(&run.first()), ("begin", None));
        assert_eq!(send(&run.advance(Err("unsupported request (unknown variant `begin`); the installed helper may be older".into()))), ("write_file", None));
        assert_eq!(send(&run.advance(Ok(()))), ("write_file", None));
        assert_eq!(run.advance(Ok(())), Step::Finished(Finish::Saved { tx: None, warning: None }));
    }

    /// A batch of two writes, driven up to the point where its commit is the step in flight.
    fn at_commit() -> Run {
        let mut run = Run::new("x", vec![write("a"), write("b")], true);
        assert_eq!(send(&run.first()), ("begin", None));
        run.note_data("t".into());
        assert_eq!(send(&run.advance(Ok(()))), ("write_file", Some("t")));
        assert_eq!(send(&run.advance(Ok(()))), ("write_file", Some("t")));
        assert_eq!(send(&run.advance(Ok(()))), ("commit", None));
        run
    }

    #[test]
    fn a_commit_the_helper_cannot_parse_is_only_a_warning() {
        // The helper that gave us the transaction id could not parse the commit (the old key was `tx`).
        match at_commit().advance(Err("unsupported request (missing field `tx`)".into())) {
            Step::Finished(Finish::Saved { tx: None, warning: Some(w) }) => assert!(w.contains("dist/install.sh"), "{w}"),
            other => panic!("{other:?}"),
        }
        match at_commit().advance(Err("transaction t is not open".into())) {
            Step::Finished(Finish::Saved { tx: None, warning: Some(w) }) => assert!(w.contains("saved"), "{w}"),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn begin_failures_end_the_batch_before_anything_ran() {
        let mut run = Run::new("x", vec![write("a"), write("b")], true);
        run.first();
        assert_eq!(
            run.advance(Err("cannot start a transaction: Permission denied".into())),
            Step::Finished(Finish::Failed { reason: "cannot start a transaction: Permission denied".into(), rollback_error: None, tx: None })
        );
        let mut run = Run::new("x", vec![write("a"), write("b")], true);
        run.first();
        match run.advance(Ok(())) {
            Step::Finished(Finish::Failed { reason, .. }) => assert!(reason.contains("transaction id")),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn empty_and_plain_batches() {
        let mut run = Run::new("nothing", vec![], true);
        assert_eq!(run.first(), Step::Finished(Finish::Saved { tx: None, warning: None }));
        let mut run = Run::new("remount", vec![Op::Remount { target: "/boot".into(), rw: true }, Op::Remount { target: "/efi".into(), rw: true }], true);
        assert_eq!(send(&run.first()), ("remount", None));
        assert_eq!(send(&run.advance(Ok(()))), ("remount", None));
        assert_eq!(run.progress(), (1, 2));
        assert_eq!(run.advance(Err("mount: busy".into())), Step::Finished(Finish::Failed { reason: "mount: busy".into(), rollback_error: None, tx: None }));
    }

    #[test]
    fn config_update_batches_are_transactions() {
        let ops: Vec<Op> = (0..3).map(|i| Op::ConfigUpdate { path: format!("/etc/._cfg000{i}_x"), accept: true, expect: None }).collect();
        let mut run = Run::new("Accept all", ops, true);
        assert_eq!(send(&run.first()), ("begin", None));
    }
}
