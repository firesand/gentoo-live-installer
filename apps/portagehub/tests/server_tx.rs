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

    h.send_tx(2, &tx, Op::WriteFile { path: file.into(), content: "app-misc/foo bar\n".into(), backup: false });
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
    h.send_tx(2, &tx, Op::WriteFile { path: "/etc/portage/package.use/a".into(), content: "changed\n".into(), backup: false });
    h.finish(2).unwrap();
    h.send(3, Op::Rollback { tx, force: true });
    h.finish(3).unwrap();
    assert_eq!(std::fs::read_to_string(root.path("etc/portage/package.use/a")).unwrap(), "orig\n");
    assert!(h.quit().success());
}
