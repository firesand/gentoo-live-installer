//! Cross-checks the emerge argument policy against Portage's own option parser.
//!
//! Portage resolves abbreviated long options (`--sys=x` is `--sysroot`), so the
//! only reliable test of "no accepted command line can redirect emerge to
//! another root or configuration tree" is to ask `_emerge.main.parse_opts`.
//! Nothing is executed beyond that parser. The test is skipped when python3 or
//! Portage is not available (for example in a CI container).

use portagehub::emerge_opts::{LONG_OPTS, SHORT_FLAGS};
use portagehub::policy::check_args;
use std::io::Write;
use std::process::{Command, Stdio};

/// Options that point emerge at another tree.
const REDIRECTING: &[&str] = &["--root", "--config-root", "--sysroot", "--prefix", "--quickpkg-direct-root"];

const SCRIPT: &str = r#"
import json, sys
try:
    from _emerge.main import parse_opts
except Exception as e:
    print("SKIP " + repr(e))
    sys.exit(0)
redirecting = json.loads(sys.argv[1])
out = []
for argv in json.load(sys.stdin):
    try:
        action, opts, files = parse_opts(list(argv), silent=True)
        out.append({"error": None, "set": [o for o in redirecting if opts.get(o)], "files": files})
    except SystemExit as e:
        out.append({"error": "exit %s" % (e.code,), "set": [], "files": []})
    except Exception as e:
        out.append({"error": repr(e), "set": [], "files": []})
print(json.dumps(out))
"#;

/// Ask Portage about every argv; `None` when it cannot be asked.
fn ask_portage(argvs: &[Vec<String>]) -> Option<Vec<serde_json::Value>> {
    let mut child = Command::new("python3")
        .args(["-I", "-c", SCRIPT, &serde_json::to_string(REDIRECTING).unwrap()])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    child.stdin.take().unwrap().write_all(serde_json::to_string(argvs).unwrap().as_bytes()).ok()?;
    let out = child.wait_with_output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    if !out.status.success() || text.starts_with("SKIP") {
        eprintln!("SKIP policy oracle: {}", text.trim());
        return None;
    }
    serde_json::from_str(text.trim()).ok()
}

fn argv(a: &[&str]) -> Vec<String> {
    a.iter().map(|s| s.to_string()).collect()
}

/// Every command line the policy accepts that can be built from its own table.
fn accepted_candidates() -> Vec<Vec<String>> {
    let mut out = Vec::new();
    for o in LONG_OPTS {
        for form in [o.name.to_string(), format!("{}=y", o.name), format!("{}=n", o.name), format!("{}=4", o.name), format!("{}=dev-libs/foo", o.name), format!("{}=~dev-libs/foo-1.0", o.name)] {
            for v in [vec![form.clone()], vec![form.clone(), "@world".into()], vec!["-uDN".into(), form.clone(), "dev-libs/foo".into()]] {
                if check_args("emerge", &v).is_ok() {
                    out.push(v);
                }
            }
        }
    }
    for c in SHORT_FLAGS.chars() {
        out.push(vec![format!("-{c}")]);
    }
    for v in [
        argv(&["--pretend", "--verbose", "--color=n", "--nospinner", "--ask=n", "-uDN", "--with-bdeps=y", "@world"]),
        argv(&["--config", "--ask=n", "=sys-kernel/gentoo-kernel-6.12.8"]),
        argv(&["--noreplace", "--ask=n", "@kernels-keep"]),
        argv(&["--jobs", "--deep", "~dev-lang/python-3.11.5"]),
    ] {
        out.push(v);
    }
    out
}

#[test]
fn no_accepted_emerge_command_line_can_redirect_emerge() {
    let accepted = accepted_candidates();
    assert!(accepted.len() > 200, "{} candidates", accepted.len());
    for a in &accepted {
        assert!(check_args("emerge", a).is_ok(), "{a:?}");
    }
    let Some(answers) = ask_portage(&accepted) else { return };
    assert_eq!(answers.len(), accepted.len());
    for (a, ans) in accepted.iter().zip(&answers) {
        assert_eq!(ans["set"], serde_json::json!([]), "{a:?} sets a redirecting option in Portage: {ans}");
    }
}

#[test]
fn the_oracle_does_see_the_bypasses_the_policy_refuses() {
    let bypasses = [
        argv(&["--config-r", "tmp/x", "@world"]),
        argv(&["--config-=tmp/x"]),
        argv(&["--sys=tmp/x"]),
        argv(&["--sysro=tmp/x"]),
        argv(&["--pref=tmp/x"]),
        argv(&["--prefi=tmp/x"]),
        argv(&["--config-root=tmp/x"]),
    ];
    for b in &bypasses {
        assert!(check_args("emerge", b).is_err(), "{b:?} must be refused");
    }
    let Some(answers) = ask_portage(&bypasses) else { return };
    for (b, ans) in bypasses.iter().zip(&answers) {
        assert_ne!(ans["set"], serde_json::json!([]), "Portage no longer redirects for {b:?}; the oracle is out of date: {ans}");
    }
}

#[test]
fn relative_binary_package_arguments_are_refused_and_would_have_been_installed() {
    // Without the policy, emerge treats these as files (resolved against the job's cwd, `/`).
    let files = [argv(&["tmp/evil.tbz2"]), argv(&["tmp/evil.gpkg.tar"]), argv(&["tmp/x.ebuild"])];
    for f in &files {
        assert!(check_args("emerge", f).is_err(), "{f:?}");
    }
    let Some(answers) = ask_portage(&files) else { return };
    for (f, ans) in files.iter().zip(&answers) {
        assert_eq!(ans["files"], serde_json::json!([f[0]]), "{f:?}: {ans}");
    }
}
