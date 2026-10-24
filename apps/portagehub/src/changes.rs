//! The admin change log: what PortageHub did as root, read from the helper's
//! audit log and grouped into changes (transactions, which can be undone)
//! and commands (programs and other operations, which cannot).

use portagehub::audit::Record;
use std::collections::HashMap;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChangeState {
    /// Committed and still in place.
    Saved,
    /// Committed, then undone.
    Undone,
    /// A step failed and the transaction was rolled back.
    RolledBack,
    /// Begun but never committed (PortageHub closed mid-way).
    Incomplete,
}

#[derive(Debug, Clone)]
pub struct Change {
    pub tx: String,
    pub label: String,
    pub ts: i64,
    pub uid: u32,
    pub files: Vec<String>,
    /// The requests made in the transaction.
    pub steps: Vec<String>,
    pub state: ChangeState,
    pub undone_ts: Option<i64>,
}

impl Change {
    pub fn can_undo(&self) -> bool {
        matches!(self.state, ChangeState::Saved | ChangeState::Incomplete)
    }
}

#[derive(Debug, Clone)]
pub struct Command {
    pub ts: i64,
    pub uid: u32,
    pub what: String,
    pub ok: Option<bool>,
    pub detail: String,
}

#[derive(Debug, Clone, Default)]
pub struct ChangeLog {
    /// The audit log exists and could be read.
    pub readable: bool,
    /// Newest first.
    pub changes: Vec<Change>,
    /// Newest first.
    pub commands: Vec<Command>,
}

/// File operations: they show up as changes (through their transaction).
const FILE_OPS: &[&str] = &["write_file", "delete_file", "mkdir", "rename", "config_update", "mark_good"];
/// Bookkeeping that is not worth listing.
const QUIET_OPS: &[&str] = &["ping", "boot_scan", "begin", "commit", "rollback", "signal"];

pub fn summarize(records: &[Record]) -> ChangeLog {
    let mut changes: Vec<Change> = Vec::new();
    let mut by_tx: HashMap<String, usize> = HashMap::new();
    let mut commands: Vec<Command> = Vec::new();
    let mut pending: HashMap<u64, usize> = HashMap::new();
    let mut entry = |changes: &mut Vec<Change>, r: &Record, tx: &str| -> usize {
        *by_tx.entry(tx.to_string()).or_insert_with(|| {
            changes.push(Change {
                tx: tx.to_string(),
                label: r.what.clone(),
                ts: r.ts,
                uid: r.uid,
                files: Vec::new(),
                steps: Vec::new(),
                state: ChangeState::Incomplete,
                undone_ts: None,
            });
            changes.len() - 1
        })
    };
    for r in records {
        match (r.event.as_str(), r.tx.as_deref()) {
            ("begin", Some(tx)) => {
                entry(&mut changes, r, tx);
            }
            ("request", Some(tx)) if !QUIET_OPS.contains(&r.op.as_str()) => {
                let i = entry(&mut changes, r, tx);
                changes[i].steps.push(r.what.clone());
            }
            ("request", None) if !FILE_OPS.contains(&r.op.as_str()) && !QUIET_OPS.contains(&r.op.as_str()) => {
                commands.push(Command { ts: r.ts, uid: r.uid, what: r.what.clone(), ok: None, detail: String::new() });
                pending.insert(r.id, commands.len() - 1);
            }
            ("done", None) => {
                if let Some(i) = pending.remove(&r.id) {
                    commands[i].ok = r.ok;
                    commands[i].detail = r.detail.clone();
                }
            }
            ("commit", Some(tx)) => {
                let i = entry(&mut changes, r, tx);
                let c = &mut changes[i];
                c.state = ChangeState::Saved;
                c.files = r.files.clone();
                if !r.what.is_empty() {
                    c.label = r.what.clone();
                }
            }
            ("rollback", Some(tx)) => {
                let i = entry(&mut changes, r, tx);
                let c = &mut changes[i];
                c.state = if c.state == ChangeState::Saved { ChangeState::Undone } else { ChangeState::RolledBack };
                c.undone_ts = Some(r.ts);
                if c.files.is_empty() {
                    c.files = r.files.clone();
                }
            }
            _ => {}
        }
    }
    changes.reverse();
    commands.reverse();
    ChangeLog { readable: true, changes, commands }
}

pub fn load() -> ChangeLog {
    // Same root as the helper (PORTAGEHUB_ROOT for tests, never as root).
    match portagehub::audit::read(&portagehub::audit::path_for(&portagehub::server::system_root())) {
        Some(records) => summarize(&records),
        None => ChangeLog::default(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(event: &str, id: u64, tx: Option<&str>, op: &str, what: &str) -> Record {
        Record { ts: id as i64, id, event: event.into(), op: op.into(), tx: tx.map(String::from), what: what.into(), ..Default::default() }
    }

    #[test]
    fn groups_transactions_and_commands() {
        let mut commit = rec("commit", 0, Some("t1"), "", "Apply USE flags");
        commit.files = vec!["/etc/portage/package.use/zz-portagehub".into()];
        let mut implicit = rec("commit", 0, Some("t2"), "", "write /etc/portage/make.conf");
        implicit.files = vec!["/etc/portage/make.conf".into()];
        let mut done = rec("done", 5, None, "", "");
        done.ok = Some(true);
        let records = vec![
            rec("request", 1, None, "begin", "begin: Apply USE flags"),
            rec("begin", 1, Some("t1"), "", "Apply USE flags"),
            rec("request", 2, Some("t1"), "write_file", "write /etc/portage/package.use/zz-portagehub (10 bytes)"),
            commit,
            rec("request", 4, None, "write_file", "write /etc/portage/make.conf (5 bytes)"),
            implicit,
            rec("request", 5, None, "run", "run emerge --sync"),
            done,
            rec("begin", 6, Some("t3"), "", "Mark kernel"),
            rec("request", 7, Some("t3"), "mark_good", "mark 7.3 known-good"),
            rec("rollback", 8, Some("t2"), "", "write /etc/portage/make.conf"),
            rec("request", 9, None, "ping", "ping"),
        ];
        let log = summarize(&records);
        assert_eq!(log.changes.len(), 3);
        let t3 = &log.changes[0];
        assert_eq!((t3.tx.as_str(), t3.state, t3.steps.len()), ("t3", ChangeState::Incomplete, 1));
        assert!(t3.can_undo());
        let t2 = log.changes.iter().find(|c| c.tx == "t2").unwrap();
        assert_eq!(t2.state, ChangeState::Undone);
        assert!(!t2.can_undo());
        let t1 = log.changes.iter().find(|c| c.tx == "t1").unwrap();
        assert_eq!((t1.label.as_str(), t1.state, t1.files.len(), t1.steps.len()), ("Apply USE flags", ChangeState::Saved, 1, 1));
        assert_eq!(log.commands.len(), 1);
        assert_eq!((log.commands[0].what.as_str(), log.commands[0].ok), ("run emerge --sync", Some(true)));
    }

    #[test]
    fn failed_batch_is_rolled_back_not_undone() {
        let records =
            vec![rec("begin", 1, Some("t"), "", "Batch"), rec("request", 2, Some("t"), "write_file", "write x"), rec("rollback", 3, Some("t"), "", "Batch")];
        let log = summarize(&records);
        assert_eq!(log.changes[0].state, ChangeState::RolledBack);
        assert!(!log.changes[0].can_undo());
    }
}
