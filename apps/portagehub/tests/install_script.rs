//! dist/install.sh: the helper lands where polkit is told to find it, for any PREFIX.

use std::path::{Path, PathBuf};
use std::process::Command;

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("portagehub-install-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(d.join("target/release")).unwrap();
    for f in ["portagehub", "portagehub-helper"] {
        std::fs::write(d.join("target/release").join(f), b"#!/bin/sh\n").unwrap();
    }
    d
}

fn install(dir: &Path, prefix: Option<&str>) -> std::process::Output {
    let script = Path::new(env!("CARGO_MANIFEST_DIR")).join("dist/install.sh");
    let mut cmd = Command::new("sh");
    cmd.arg(script).env("CARGO_TARGET_DIR", dir.join("target")).env("DESTDIR", dir.join("stage"));
    match prefix {
        Some(p) => cmd.env("PREFIX", p),
        None => cmd.env_remove("PREFIX"),
    };
    cmd.output().unwrap()
}

fn policy_path(dir: &Path) -> String {
    let xml = std::fs::read_to_string(dir.join("stage/usr/share/polkit-1/actions/org.portagehub.helper.policy")).unwrap();
    let key = "org.freedesktop.policykit.exec.path\">";
    let at = xml.find(key).expect("exec.path annotation") + key.len();
    xml[at..at + xml[at..].find('<').unwrap()].to_string()
}

#[test]
fn a_prefix_install_tells_polkit_where_the_helper_is() {
    let d = scratch("prefix");
    let out = install(&d, Some("/usr/local"));
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    assert!(d.join("stage/usr/local/libexec/portagehub-helper").is_file());
    assert!(d.join("stage/usr/local/bin/portagehub").is_file());
    assert!(d.join("stage/usr/local/share/applications/portagehub.desktop").is_file());
    assert_eq!(policy_path(&d), "/usr/local/libexec/portagehub-helper", "the path on the target system, not inside DESTDIR");
    let said = String::from_utf8_lossy(&out.stdout);
    assert!(said.contains("helper:  /usr/local/libexec/portagehub-helper"), "{said}");
    // (exec::helper_candidates, tested in the GUI crate, searches /usr/local/libexec too.)
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn the_default_install_keeps_the_shipped_policy_path() {
    let d = scratch("default");
    let out = install(&d, None);
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    assert!(d.join("stage/usr/libexec/portagehub-helper").is_file());
    assert_eq!(policy_path(&d), "/usr/libexec/portagehub-helper");
    // Same content as the shipped policy, apart from nothing.
    let shipped = std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("dist/org.portagehub.helper.policy")).unwrap();
    let installed = std::fs::read_to_string(d.join("stage/usr/share/polkit-1/actions/org.portagehub.helper.policy")).unwrap();
    assert_eq!(shipped, installed);
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn a_missing_build_is_explained() {
    let d = scratch("nobuild");
    std::fs::remove_file(d.join("target/release/portagehub-helper")).unwrap();
    let out = install(&d, None);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("run cargo build --release first"));
    assert!(!d.join("stage").exists(), "nothing is installed from half a build");
    let _ = std::fs::remove_dir_all(&d);
}
