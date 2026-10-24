//! Transactions over the real helper pipe, against a scratch root.

mod common;

use common::{Helper, Scratch};
use portagehub::protocol::Op;

#[test]
fn begin_write_commit_rollback_over_the_pipe() {
    let root = Scratch::new("tx-pipe");
    let file = "/etc/portage/package.use/zz-test";
    let mut h = Helper::start_in(&root.0);

    h.send(1, Op::Begin { label: "pipe test".into() });
    let tx = h.finish(1).expect("begin");
    assert!(!tx.is_empty(), "Begin answers with the transaction id");

    h.send_tx(2, &tx, Op::WriteFile { path: file.into(), content: "app-misc/foo bar\n".into(), backup: false, expect: None });
    h.finish(2).expect("write inside the transaction");
    assert_eq!(std::fs::read_to_string(root.path("etc/portage/package.use/zz-test")).unwrap(), "app-misc/foo bar\n");

    // The variants that used to fail with "missing field tx".
    h.send(3, Op::Commit { tx: tx.clone() });
    h.finish(3).expect("commit");
    h.send(4, Op::Rollback { tx: tx.clone(), force: false });
    h.finish(4).expect("rollback of an unchanged, committed transaction");
    assert!(!root.path("etc/portage/package.use/zz-test").exists(), "rollback removes the file the transaction created");

    // A second rollback is refused by the journal, with its own message.
    h.send(5, Op::Rollback { tx, force: true });
    let e = h.finish(5).unwrap_err();
    assert!(e.contains("already undone"), "{e}");
    assert!(h.quit().success());
}

#[test]
fn rollback_of_an_open_transaction_restores_the_previous_content() {
    let root = Scratch::new("tx-open");
    std::fs::write(root.path("etc/portage/package.use/a"), "orig\n").unwrap();
    let mut h = Helper::start_in(&root.0);
    h.send(1, Op::Begin { label: "batch".into() });
    let tx = h.finish(1).unwrap();
    h.send_tx(2, &tx, Op::WriteFile { path: "/etc/portage/package.use/a".into(), content: "changed\n".into(), backup: false, expect: None });
    h.finish(2).unwrap();
    h.send(3, Op::Rollback { tx, force: true });
    h.finish(3).unwrap();
    assert_eq!(std::fs::read_to_string(root.path("etc/portage/package.use/a")).unwrap(), "orig\n");
    assert!(h.quit().success());
}

// --- compare-and-swap: edits made outside PortageHub are not lost ------------------------

use portagehub::protocol::{Expect, TxInfo};

fn use_file(name: &str) -> String {
    format!("/etc/portage/package.use/{name}")
}

#[test]
fn a_write_is_refused_when_the_file_changed_since_it_was_read() {
    let root = Scratch::new("cas-write");
    let on_disk = root.path("etc/portage/package.use/a");
    std::fs::write(&on_disk, "app-misc/foo bar\n").unwrap();
    let seen = Expect::of("app-misc/foo bar\n");
    let mut h = Helper::start_in(&root.0);

    // Someone edits the file by hand after PortageHub read it ...
    std::fs::write(&on_disk, "app-misc/foo bar\napp-misc/mine baz\n").unwrap();
    h.send(1, Op::WriteFile { path: use_file("a"), content: "app-misc/foo bar qux\n".into(), backup: false, expect: Some(seen) });
    let e = h.finish(1).unwrap_err();
    assert_eq!(e, format!("changed on disk: {}", use_file("a")));
    assert_eq!(std::fs::read_to_string(&on_disk).unwrap(), "app-misc/foo bar\napp-misc/mine baz\n", "the hand edit survives");

    // ... while a write based on what is there now goes through.
    h.send(2, Op::WriteFile { path: use_file("a"), content: "new\n".into(), backup: false, expect: Some(Expect::of("app-misc/foo bar\napp-misc/mine baz\n")) });
    h.finish(2).unwrap();
    assert_eq!(std::fs::read_to_string(&on_disk).unwrap(), "new\n");
    assert!(h.quit().success());
}

#[test]
fn absent_matches_a_missing_or_empty_file_only() {
    let root = Scratch::new("cas-absent");
    let mut h = Helper::start_in(&root.0);
    h.send(1, Op::WriteFile { path: use_file("new"), content: "x\n".into(), backup: false, expect: Some(Expect::Absent) });
    h.finish(1).expect("a missing file is absent");
    std::fs::write(root.path("etc/portage/package.use/empty"), "").unwrap();
    h.send(2, Op::WriteFile { path: use_file("empty"), content: "y\n".into(), backup: false, expect: Some(Expect::Absent) });
    h.finish(2).expect("an empty file is absent");
    // The GUI believed the file was empty (or could not read it) but it has content: refuse.
    h.send(3, Op::WriteFile { path: use_file("new"), content: "z\n".into(), backup: false, expect: Some(Expect::Absent) });
    assert!(h.finish(3).unwrap_err().starts_with("changed on disk"));
    assert_eq!(std::fs::read_to_string(root.path("etc/portage/package.use/new")).unwrap(), "x\n");
    // No expectation (an older GUI, or a file the GUI manages entirely) still writes blindly.
    h.send(4, Op::WriteFile { path: use_file("new"), content: "w\n".into(), backup: false, expect: None });
    h.finish(4).unwrap();
    assert!(h.quit().success());
}

#[test]
fn delete_and_rename_check_the_expectation_too() {
    let root = Scratch::new("cas-del");
    let f = root.path("etc/portage/package.use/a");
    std::fs::write(&f, "one\n").unwrap();
    let stale = Expect::of("one\n");
    std::fs::write(&f, "two\n").unwrap();
    let mut h = Helper::start_in(&root.0);
    h.send(1, Op::DeleteFile { path: use_file("a"), expect: Some(stale) });
    assert!(h.finish(1).unwrap_err().starts_with("changed on disk"));
    assert!(f.exists());
    h.send(2, Op::Rename { from: use_file("a"), to: use_file("b"), expect: Some(stale) });
    assert!(h.finish(2).unwrap_err().starts_with("changed on disk"));
    assert!(f.exists() && !root.path("etc/portage/package.use/b").exists());
    h.send(3, Op::Rename { from: use_file("a"), to: use_file("b"), expect: Some(Expect::of("two\n")) });
    h.finish(3).unwrap();
    h.send(4, Op::DeleteFile { path: use_file("b"), expect: Some(Expect::of("two\n")) });
    h.finish(4).unwrap();
    assert!(!root.path("etc/portage/package.use/b").exists());
    assert!(h.quit().success());
}

// --- a multi-step batch that fails half way leaves nothing behind ------------------------

#[test]
fn converting_package_use_to_a_directory_rolls_back_when_a_later_step_fails() {
    let root = Scratch::new("convert");
    let base = root.path("etc/portage/package.use");
    std::fs::remove_dir(&base).unwrap();
    std::fs::write(&base, "app-misc/foo bar\n").unwrap();
    let mut h = Helper::start_in(&root.0);

    h.send(1, Op::Begin { label: "Convert package.use to a directory".into() });
    let tx = h.finish(1).unwrap();
    let ops = portagehub_ops();
    for (i, op) in ops.into_iter().enumerate() {
        h.send_tx(10 + i as u64, &tx, op);
        h.finish(10 + i as u64).unwrap_or_else(|e| panic!("step {i}: {e}"));
    }
    assert!(base.is_dir() && base.join("main").is_file(), "the conversion itself worked");

    // The step after it fails (the file it expected is not there): the whole batch is undone.
    h.send_tx(20, &tx, Op::WriteFile { path: use_file("zz"), content: "x\n".into(), backup: false, expect: Some(Expect::Hash(1)) });
    assert!(h.finish(20).unwrap_err().starts_with("changed on disk"));
    h.send(21, Op::Rollback { tx, force: true });
    h.finish(21).unwrap();
    assert!(base.is_file(), "package.use is a file again");
    assert_eq!(std::fs::read_to_string(&base).unwrap(), "app-misc/foo bar\n");
    assert!(!root.path("etc/portage/package.use.portagehub-convert~").exists(), "no temporary file is left");
    assert!(h.quit().success());
}

/// The three steps `edits::convert_to_dir_ops` produces for /etc/portage/package.use.
fn portagehub_ops() -> Vec<Op> {
    let base = "/etc/portage/package.use";
    let tmp = format!("{base}.portagehub-convert~");
    vec![
        Op::Rename { from: base.into(), to: tmp.clone(), expect: None },
        Op::Mkdir { path: base.into() },
        Op::Rename { from: tmp, to: format!("{base}/main"), expect: None },
    ]
}

// --- the journal is reachable ------------------------------------------------------------

#[test]
fn recent_changes_can_be_listed_and_undone() {
    let root = Scratch::new("listtx");
    let mut h = Helper::start_in(&root.0);
    h.send(1, Op::Begin { label: "Two edits".into() });
    let tx = h.finish(1).unwrap();
    h.send_tx(2, &tx, Op::WriteFile { path: use_file("a"), content: "a\n".into(), backup: false, expect: Some(Expect::Absent) });
    h.finish(2).unwrap();
    h.send_tx(3, &tx, Op::WriteFile { path: use_file("b"), content: "b\n".into(), backup: false, expect: Some(Expect::Absent) });
    h.finish(3).unwrap();
    h.send(4, Op::Commit { tx: tx.clone() });
    h.finish(4).unwrap();

    h.send(5, Op::ListTx);
    let list: Vec<TxInfo> = serde_json::from_str(&h.finish(5).unwrap()).unwrap();
    let mine = list.iter().find(|t| t.id == tx).expect("the transaction is listed");
    assert_eq!((mine.label.as_str(), mine.state.as_str()), ("Two edits", "committed"));
    assert_eq!(mine.files, vec![use_file("a"), use_file("b")]);

    // Undo without force works while nothing changed since ...
    h.send(6, Op::Rollback { tx: tx.clone(), force: false });
    h.finish(6).unwrap();
    assert!(!root.path("etc/portage/package.use/a").exists() && !root.path("etc/portage/package.use/b").exists());
    h.send(7, Op::ListTx);
    let list: Vec<TxInfo> = serde_json::from_str(&h.finish(7).unwrap()).unwrap();
    assert_eq!(list.iter().find(|t| t.id == tx).unwrap().state, "rolled_back");
    assert!(h.quit().success());
}

#[test]
fn undo_is_refused_after_a_later_edit_unless_forced() {
    let root = Scratch::new("undo-later");
    let mut h = Helper::start_in(&root.0);
    h.send(1, Op::Begin { label: "edit".into() });
    let tx = h.finish(1).unwrap();
    h.send_tx(2, &tx, Op::WriteFile { path: use_file("a"), content: "a\n".into(), backup: false, expect: None });
    h.finish(2).unwrap();
    h.send(3, Op::Commit { tx: tx.clone() });
    h.finish(3).unwrap();
    std::fs::write(root.path("etc/portage/package.use/a"), "edited by hand\n").unwrap();
    h.send(4, Op::Rollback { tx: tx.clone(), force: false });
    let e = h.finish(4).unwrap_err();
    assert!(e.contains("changed since"), "{e}");
    assert_eq!(std::fs::read_to_string(root.path("etc/portage/package.use/a")).unwrap(), "edited by hand\n");
    h.send(5, Op::Rollback { tx, force: true });
    h.finish(5).unwrap();
    assert!(!root.path("etc/portage/package.use/a").exists());
    assert!(h.quit().success());
}

// --- configuration file updates -----------------------------------------------------------

#[test]
fn accepting_config_updates_is_one_undoable_transaction_and_checks_what_was_reviewed() {
    let root = Scratch::new("cfgupd");
    let conf = root.path("etc/conf.d");
    std::fs::create_dir_all(&conf).unwrap();
    for (name, old, new) in [("hostname", "hostname=\"old\"\n", "hostname=\"new\"\n"), ("keymaps", "keymap=\"us\"\n", "keymap=\"de\"\n")] {
        std::fs::write(conf.join(name), old).unwrap();
        std::fs::write(conf.join(format!("._cfg0000_{name}")), new).unwrap();
    }
    let mut h = Helper::start_in(&root.0);
    let accept = |name: &str, seen: &str| Op::ConfigUpdate { path: format!("/etc/conf.d/._cfg0000_{name}"), accept: true, expect: Some(Expect::of(seen)) };

    // One target changed after the diff was shown: that update is refused, nothing else is touched.
    std::fs::write(conf.join("keymaps"), "keymap=\"fr\"\n").unwrap();
    h.send(1, Op::Begin { label: "Accept 2 new configuration file versions".into() });
    let tx = h.finish(1).unwrap();
    h.send_tx(2, &tx, accept("hostname", "hostname=\"old\"\n"));
    h.finish(2).unwrap();
    h.send_tx(3, &tx, accept("keymaps", "keymap=\"us\"\n"));
    let e = h.finish(3).unwrap_err();
    assert_eq!(e, "changed on disk: /etc/conf.d/keymaps");
    // The GUI rolls the batch back: hostname is as it was, and its pending update is back.
    h.send(4, Op::Rollback { tx, force: true });
    h.finish(4).unwrap();
    assert_eq!(std::fs::read_to_string(conf.join("hostname")).unwrap(), "hostname=\"old\"\n");
    assert!(conf.join("._cfg0000_hostname").exists());
    assert_eq!(std::fs::read_to_string(conf.join("keymaps")).unwrap(), "keymap=\"fr\"\n");

    // With the right expectations both go through in one transaction ...
    h.send(5, Op::Begin { label: "Accept 2".into() });
    let tx = h.finish(5).unwrap();
    h.send_tx(6, &tx, accept("hostname", "hostname=\"old\"\n"));
    h.finish(6).unwrap();
    h.send_tx(7, &tx, accept("keymaps", "keymap=\"fr\"\n"));
    h.finish(7).unwrap();
    h.send(8, Op::Commit { tx: tx.clone() });
    h.finish(8).unwrap();
    assert_eq!(std::fs::read_to_string(conf.join("hostname")).unwrap(), "hostname=\"new\"\n");
    assert_eq!(std::fs::read_to_string(conf.join("keymaps")).unwrap(), "keymap=\"de\"\n");
    assert!(!conf.join("._cfg0000_hostname").exists());
    // ... and one undo restores both targets and brings the update files back.
    h.send(9, Op::Rollback { tx, force: false });
    h.finish(9).unwrap();
    assert_eq!(std::fs::read_to_string(conf.join("hostname")).unwrap(), "hostname=\"old\"\n");
    assert_eq!(std::fs::read_to_string(conf.join("keymaps")).unwrap(), "keymap=\"fr\"\n");
    assert_eq!(std::fs::read_to_string(conf.join("._cfg0000_hostname")).unwrap(), "hostname=\"new\"\n");
    assert!(h.quit().success());
}
