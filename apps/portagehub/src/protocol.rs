//! JSON-lines protocol spoken between the GUI and the root helper.
//!
//! The GUI writes one [`Request`] per line to the helper's stdin, the helper
//! answers with [`Event`]s, one per line, on its stdout.

use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Request {
    pub id: u64,
    /// Transaction (from [`Op::Begin`]) that file changes of this request belong to.
    /// Without one, the helper wraps each changing request in its own transaction.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tx: Option<String>,
    #[serde(flatten)]
    pub op: Op,
}

impl Request {
    pub fn new(id: u64, op: Op) -> Request {
        Request { id, tx: None, op }
    }
}

#[derive(Serialize, Deserialize, Debug, Clone)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Op {
    Ping,
    /// Spawn a program and stream its output back. Programs always run in `/`
    /// (`cwd` is accepted for compatibility and ignored).
    Run {
        argv: Vec<String>,
        #[serde(default)]
        env: Vec<(String, String)>,
        #[serde(default)]
        cwd: Option<String>,
    },
    /// Atomically replace a file (creating parent directories when needed).
    WriteFile {
        path: String,
        content: String,
        #[serde(default)]
        backup: bool,
    },
    DeleteFile {
        path: String,
    },
    Mkdir {
        path: String,
    },
    Rename {
        from: String,
        to: String,
    },
    /// Send a signal to the process group of a previously started `Run`.
    Signal {
        target: u64,
        signal: i32,
    },
    /// Resolve a CONFIG_PROTECT update: `path` must be a `._cfgNNNN_name`
    /// file below /etc. Accept replaces `name` with it, otherwise it is deleted.
    ConfigUpdate {
        path: String,
        accept: bool,
    },
    /// Read-only scan of root-only boot files (bootloader configs, /boot listings).
    BootScan,
    /// Run a bootloader/initramfs tool with arguments fixed by the helper.
    BootAction {
        action: BootAction,
        #[serde(default)]
        kver: Option<String>,
    },
    /// Record `kver` as the last known-good kernel in `state_dir` (rotating the
    /// previous one) and optionally back up its /boot files.
    MarkGood {
        kver: String,
        state_dir: String,
        #[serde(default)]
        backup: bool,
    },
    /// Delete files of a kernel no installed package owns any more
    /// (/boot images, /lib/modules/<kver>, /usr/src/linux-<kver>).
    /// With `old_only`, only the `*.old` copies in /boot are removed.
    RemoveKernelLeftovers {
        kver: String,
        #[serde(default)]
        old_only: bool,
    },
    /// Delete leftover build directories below PORTAGE_TMPDIR (refused while emerge runs).
    CleanBuildDirs,
    /// Mount or remount a boot partition (only /boot, /boot/efi, /efi) read-write or read-only.
    Remount {
        target: String,
        rw: bool,
    },
    /// Start a transaction; its id comes back as [`Event::Data`].
    Begin {
        label: String,
    },
    /// Close a transaction: its changes stay and can be undone later.
    ///
    /// The wire key is `tx_id`: a plain `tx` would be swallowed by [`Request::tx`]
    /// (the request is flattened around the op) and the variant would never parse.
    Commit {
        #[serde(rename = "tx_id")]
        tx: String,
    },
    /// Restore every file a transaction changed. For a committed transaction
    /// this is refused when one of its files was changed since, unless `force`.
    /// The transaction id travels as `tx_id` (see [`Op::Commit`]).
    Rollback {
        #[serde(rename = "tx_id")]
        tx: String,
        #[serde(default)]
        force: bool,
    },
}

/// Every operation name on the wire (the `op` tag), kept next to [`Op::name`].
pub const OP_NAMES: &[&str] = &[
    "ping",
    "run",
    "write_file",
    "delete_file",
    "mkdir",
    "rename",
    "signal",
    "config_update",
    "boot_scan",
    "boot_action",
    "mark_good",
    "remove_kernel_leftovers",
    "clean_build_dirs",
    "remount",
    "begin",
    "commit",
    "rollback",
];

/// The only mount points [`Op::Remount`] accepts.
pub const BOOT_MOUNTPOINTS: &[&str] = &["/boot", "/boot/efi", "/efi"];

impl Op {
    /// The `op` tag this operation has on the wire (exhaustive on purpose: a new
    /// variant must be named here and listed in [`OP_NAMES`]).
    pub fn name(&self) -> &'static str {
        match self {
            Op::Ping => "ping",
            Op::Run { .. } => "run",
            Op::WriteFile { .. } => "write_file",
            Op::DeleteFile { .. } => "delete_file",
            Op::Mkdir { .. } => "mkdir",
            Op::Rename { .. } => "rename",
            Op::Signal { .. } => "signal",
            Op::ConfigUpdate { .. } => "config_update",
            Op::BootScan => "boot_scan",
            Op::BootAction { .. } => "boot_action",
            Op::MarkGood { .. } => "mark_good",
            Op::RemoveKernelLeftovers { .. } => "remove_kernel_leftovers",
            Op::CleanBuildDirs => "clean_build_dirs",
            Op::Remount { .. } => "remount",
            Op::Begin { .. } => "begin",
            Op::Commit { .. } => "commit",
            Op::Rollback { .. } => "rollback",
        }
    }

    /// Oldest helper version (major, minor) that understands this operation.
    pub fn min_helper(&self) -> (u32, u32) {
        match self {
            Op::BootScan | Op::BootAction { .. } | Op::MarkGood { .. } | Op::RemoveKernelLeftovers { .. } | Op::CleanBuildDirs => (0, 2),
            Op::Remount { .. } => (0, 3),
            Op::Begin { .. } | Op::Commit { .. } | Op::Rollback { .. } => (0, 4),
            _ => (0, 1),
        }
    }
}

/// "0.3.0" -> (0, 3); a helper that sends no version is 0.1.
pub fn parse_version(v: Option<&str>) -> (u32, u32) {
    let Some(v) = v else { return (0, 1) };
    let mut it = v.split('.').map(|x| x.parse::<u32>().unwrap_or(0));
    (it.next().unwrap_or(0), it.next().unwrap_or(0))
}

/// A request line the helper could not turn into a [`Request`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BadRequest {
    /// The request id when it could be recovered (0 otherwise), so the GUI can fail that request.
    pub id: u64,
    pub msg: String,
}

/// The `id` of a request from the start of its line, for lines that are too
/// long or not valid UTF-8 to parse as a whole. The GUI always writes `id` first.
pub fn id_from_prefix(line: &[u8]) -> u64 {
    let head = &line[..line.len().min(256)];
    let Some(at) = head.windows(5).position(|w| w == b"\"id\":") else { return 0 };
    let digits: Vec<u8> = head[at + 5..].iter().copied().skip_while(|b| *b == b' ').take_while(u8::is_ascii_digit).collect();
    std::str::from_utf8(&digits).ok().and_then(|d| d.parse().ok()).unwrap_or(0)
}

/// Parse one request line. A failure carries the request id and a message that
/// only blames an old helper when the operation itself is unknown.
pub fn parse_request(line: &str) -> Result<Request, BadRequest> {
    let err = match serde_json::from_str::<Request>(line) {
        Ok(r) => return Ok(r),
        Err(e) => e,
    };
    let value: Option<serde_json::Value> = serde_json::from_str(line).ok();
    let id = value.as_ref().and_then(|v| v.get("id")).and_then(|i| i.as_u64()).unwrap_or_else(|| id_from_prefix(line.as_bytes()));
    let msg = match value.as_ref().map(|v| v.get("op").and_then(|o| o.as_str())) {
        None => format!("request is not valid JSON ({err})"),
        Some(Some(op)) if !OP_NAMES.contains(&op) => {
            format!("unsupported request: unknown operation \"{op}\"; the installed helper may be older than PortageHub — run sudo dist/install.sh")
        }
        Some(_) => format!("invalid request ({err})"),
    };
    Err(BadRequest { id, msg })
}

#[derive(Serialize, Deserialize, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum BootAction {
    GrubMkconfig,
    Lilo,
    BootctlUpdate,
    DracutRegen,
}

impl BootAction {
    pub fn label(&self) -> &'static str {
        match self {
            BootAction::GrubMkconfig => "Regenerate GRUB menu (grub-mkconfig)",
            BootAction::Lilo => "Re-install LILO (lilo)",
            BootAction::BootctlUpdate => "Update systemd-boot (bootctl update)",
            BootAction::DracutRegen => "Rebuild initramfs (dracut)",
        }
    }
}

/// Directories where known-good kernel state may be kept.
pub const KERNEL_STATE_DIRS: &[&str] = &["/var/lib/kernel-fallback", "/var/lib/portagehub/kernels"];

/// A kernel release string such as `7.3.0-cachyos-dist`: one path component.
pub fn valid_kver(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.as_bytes()[0].is_ascii_digit()
        && s.chars().all(|c| c.is_ascii_alphanumeric() || "._+-".contains(c))
        && !s.contains("..")
}

#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct ScannedFile {
    pub path: String,
    pub content: String,
    pub mtime: i64,
}

#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct ListEntry {
    pub name: String,
    pub size: u64,
    pub mtime: i64,
    pub is_dir: bool,
}

#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct Listing {
    pub dir: String,
    pub entries: Vec<ListEntry>,
}

/// Result of [`Op::BootScan`], sent as JSON in an [`Event::Data`].
#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct BootScan {
    pub files: Vec<ScannedFile>,
    pub listings: Vec<Listing>,
    pub errors: Vec<String>,
    /// The scan ran as root (otherwise root-only files were unreadable).
    #[serde(default)]
    pub root: bool,
}

impl BootScan {
    pub fn file(&self, path: &str) -> Option<&ScannedFile> {
        self.files.iter().find(|f| f.path == path)
    }

    pub fn listing(&self, dir: &str) -> Option<&Listing> {
        self.listings.iter().find(|l| l.dir == dir)
    }
}

/// Validate a `._cfgNNNN_name` path below /etc and return (update, target),
/// both relative to /etc.
pub fn config_update_paths(path: &str) -> Option<(std::path::PathBuf, std::path::PathBuf)> {
    use std::path::{Component, Path};
    let p = Path::new(path);
    if !path.starts_with("/etc/") || p.components().any(|c| matches!(c, Component::ParentDir | Component::CurDir)) {
        return None;
    }
    let name = p.file_name()?.to_str()?;
    let rest = name.strip_prefix("._cfg")?;
    let (num, target) = rest.split_once('_')?;
    if num.len() != 4 || !num.bytes().all(|b| b.is_ascii_digit()) || target.is_empty() || target.contains('/') {
        return None;
    }
    let rel = p.strip_prefix("/etc").ok()?;
    let dir = rel.parent()?;
    Some((rel.to_path_buf(), dir.join(target)))
}

#[derive(Serialize, Deserialize, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum StreamKind {
    Stdout,
    Stderr,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
#[serde(tag = "ev", rename_all = "snake_case")]
pub enum Event {
    /// Sent once by the helper right after start-up.
    Ready {
        uid: u32,
        #[serde(default)]
        version: Option<String>,
    },
    /// Structured result of a request (JSON text).
    Data { id: u64, text: String },
    Started { id: u64, pid: u32 },
    /// A complete output line (ANSI escapes removed).
    Line { id: u64, stream: StreamKind, text: String },
    /// A carriage-return terminated partial line (progress meters).
    Partial { id: u64, stream: StreamKind, text: String },
    Exit { id: u64, code: Option<i32>, signal: Option<i32> },
    Ok { id: u64 },
    Err { id: u64, msg: String },
}

impl Event {
    pub fn id(&self) -> Option<u64> {
        match self {
            Event::Ready { .. } => None,
            Event::Started { id, .. }
            | Event::Line { id, .. }
            | Event::Data { id, .. }
            | Event::Partial { id, .. }
            | Event::Exit { id, .. }
            | Event::Ok { id }
            | Event::Err { id, .. } => Some(*id),
        }
    }
}

/// Programs the helper is willing to run as root.
pub const ALLOWED_PROGRAMS: &[&str] = &[
    "emerge",
    "emaint",
    "eselect",
    "egencache",
    "emerge-webrsync",
    "eix-update",
    "eix-sync",
    "etc-update",
    "dispatch-conf",
    "env-update",
    "revdep-rebuild",
    "glsa-check",
    "eclean",
    "eclean-dist",
    "eclean-pkg",
    "perl-cleaner",
    "smart-live-rebuild",
    "haskell-updater",
    "fixpackages",
    "quickpkg",
    "portageq",
    "eread",
];

/// Directories searched for allowed programs (never `$PATH`).
pub const PROGRAM_DIRS: &[&str] = &["/usr/bin", "/usr/sbin", "/bin", "/sbin", "/usr/local/bin"];

/// Root of the only tree the helper writes into directly.
pub const WRITE_ROOT: &str = "/etc/portage/";

/// Normalise a path lexically and return it relative to [`WRITE_ROOT`]
/// (`/etc/portage/package.use/x` -> `package.use/x`). `..` is refused.
pub fn portage_rel(path: &str) -> Option<std::path::PathBuf> {
    use std::path::{Component, PathBuf};
    if !path.starts_with('/') {
        return None;
    }
    let mut out = PathBuf::from("/");
    for c in std::path::Path::new(path).components() {
        match c {
            Component::RootDir | Component::CurDir => {}
            Component::Normal(p) => out.push(p),
            Component::ParentDir | Component::Prefix(_) => return None,
        }
    }
    let rel = out.strip_prefix(WRITE_ROOT).ok()?;
    (!rel.as_os_str().is_empty()).then(|| rel.to_path_buf())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths() {
        assert_eq!(portage_rel("/etc/portage/package.use/foo").unwrap().to_str(), Some("package.use/foo"));
        assert_eq!(portage_rel("/etc/portage//./make.conf").unwrap().to_str(), Some("make.conf"));
        assert!(portage_rel("/etc/portage/../shadow").is_none());
        assert!(portage_rel("/etc/portage").is_none());
        assert!(portage_rel("/etc/portage/").is_none());
        assert!(portage_rel("/etc/portagex/y").is_none());
        assert!(portage_rel("/etc/passwd").is_none());
        assert!(portage_rel("etc/portage/x").is_none());
    }

    #[test]
    fn cfg_updates() {
        let (a, b) = config_update_paths("/etc/ssh/._cfg0001_sshd_config").unwrap();
        assert_eq!(a.to_str(), Some("ssh/._cfg0001_sshd_config"));
        assert_eq!(b.to_str(), Some("ssh/sshd_config"));
        let (a, b) = config_update_paths("/etc/._cfg0000_make.conf").unwrap();
        assert_eq!((a.to_str(), b.to_str()), (Some("._cfg0000_make.conf"), Some("make.conf")));
        assert!(config_update_paths("/etc/../root/._cfg0001_x").is_none());
        assert!(config_update_paths("/home/u/._cfg0001_x").is_none());
        assert!(config_update_paths("/etc/ssh/sshd_config").is_none());
        assert!(config_update_paths("/etc/._cfg01_x").is_none());
    }

    #[test]
    fn kvers() {
        assert!(valid_kver("7.3.0-cachyos-dist"));
        assert!(valid_kver("6.12.8-gentoo-dist"));
        assert!(!valid_kver("../etc"));
        assert!(!valid_kver("7.3/0"));
        assert!(!valid_kver("x7.3"));
        assert!(!valid_kver(""));
    }

    fn every_op() -> Vec<Op> {
        vec![
            Op::Ping,
            Op::Run { argv: vec!["emerge".into(), "--pretend".into()], env: vec![("FEATURES".into(), "x".into())], cwd: Some("/".into()) },
            Op::WriteFile { path: "/etc/portage/package.use/x".into(), content: "a/b c\n".into(), backup: true },
            Op::DeleteFile { path: "/etc/portage/package.use/x".into() },
            Op::Mkdir { path: "/etc/portage/package.use".into() },
            Op::Rename { from: "/etc/portage/a".into(), to: "/etc/portage/b".into() },
            Op::Signal { target: 3, signal: 2 },
            Op::ConfigUpdate { path: "/etc/._cfg0000_make.conf".into(), accept: true },
            Op::BootScan,
            Op::BootAction { action: BootAction::DracutRegen, kver: Some("6.1.0".into()) },
            Op::MarkGood { kver: "6.1.0".into(), state_dir: "/var/lib/kernel-fallback".into(), backup: true },
            Op::RemoveKernelLeftovers { kver: "6.1.0".into(), old_only: true },
            Op::CleanBuildDirs,
            Op::Remount { target: "/boot".into(), rw: true },
            Op::Begin { label: "x".into() },
            Op::Commit { tx: "1-2".into() },
            Op::Rollback { tx: "1-2".into(), force: true },
        ]
    }

    #[test]
    fn roundtrip_all_ops() {
        let ops = every_op();
        let mut names: Vec<&str> = ops.iter().map(Op::name).collect();
        names.sort_unstable();
        let mut listed = OP_NAMES.to_vec();
        listed.sort_unstable();
        assert_eq!(names, listed, "OP_NAMES and every_op() must list the same operations");
        for op in ops {
            for tx in [None, Some("9-9".to_string())] {
                let req = Request { id: 41, tx: tx.clone(), op: op.clone() };
                let text = serde_json::to_string(&req).unwrap();
                let wire: serde_json::Value = serde_json::from_str(&text).unwrap();
                assert_eq!(wire["op"], op.name(), "{text}");
                assert_eq!(wire.get("tx").and_then(|t| t.as_str()), tx.as_deref(), "{text}");
                let back = parse_request(&text).unwrap_or_else(|e| panic!("{} does not parse: {} ({text})", op.name(), e.msg));
                assert_eq!(back.id, 41);
                assert_eq!(back.tx, tx, "{text}");
                assert_eq!(serde_json::to_value(&back).unwrap(), wire, "{text}");
            }
        }
    }

    #[test]
    fn commit_and_rollback_keep_both_transaction_ids() {
        // `tx` is the transaction a request belongs to, `tx_id` the one being closed.
        let r = parse_request(r#"{"id":5,"tx":"A","op":"commit","tx_id":"B"}"#).unwrap();
        assert_eq!(r.tx.as_deref(), Some("A"));
        assert!(matches!(r.op, Op::Commit { ref tx } if tx == "B"));
        let r = parse_request(r#"{"id":6,"op":"rollback","tx_id":"C"}"#).unwrap();
        assert!(matches!(r.op, Op::Rollback { ref tx, force: false } if tx == "C"));
        // The old, never parsable spelling is an error that names the problem, not the helper's age.
        let e = parse_request(r#"{"id":7,"op":"commit","tx":"B"}"#).unwrap_err();
        assert_eq!(e.id, 7);
        assert!(e.msg.starts_with("invalid request") && !e.msg.contains("older"), "{}", e.msg);
    }

    #[test]
    fn bad_requests_keep_their_id() {
        let e = parse_request(r#"{"id":6,"op":"does_not_exist"}"#).unwrap_err();
        assert_eq!(e.id, 6);
        assert!(e.msg.contains("unsupported") && e.msg.contains("older"), "{}", e.msg);
        let e = parse_request(r#"{"id":8,"op":"write_file","path":"/etc/portage/x"}"#).unwrap_err();
        assert_eq!(e.id, 8);
        assert!(e.msg.contains("content"), "{}", e.msg);
        let e = parse_request("not json").unwrap_err();
        assert_eq!(e.id, 0);
        assert!(e.msg.contains("not valid JSON"));
        assert_eq!(id_from_prefix(br#"{"id":123,"op":"write_file","path":"/etc/port"#), 123);
        assert_eq!(id_from_prefix(b"\xff\xfe garbage"), 0);
    }

    #[test]
    fn roundtrip_all_events() {
        let events = vec![
            Event::Ready { uid: 1000, version: Some("0.4.0".into()) },
            Event::Data { id: 1, text: "{}".into() },
            Event::Started { id: 2, pid: 77 },
            Event::Line { id: 3, stream: StreamKind::Stderr, text: "x".into() },
            Event::Partial { id: 3, stream: StreamKind::Stdout, text: "50%".into() },
            Event::Exit { id: 4, code: Some(1), signal: None },
            Event::Ok { id: 5 },
            Event::Err { id: 6, msg: "no".into() },
        ];
        for ev in events {
            let text = serde_json::to_string(&ev).unwrap();
            let back: Event = serde_json::from_str(&text).unwrap();
            assert_eq!(serde_json::to_string(&back).unwrap(), text);
        }
    }

    #[test]
    fn roundtrip() {
        let r = Request::new(7, Op::Run { argv: vec!["emerge".into()], env: vec![], cwd: None });
        let s = serde_json::to_string(&r).unwrap();
        assert!(!s.contains("\"tx\""), "{s}");
        let back: Request = serde_json::from_str(&s).unwrap();
        assert_eq!(back.id, 7);
        assert!(back.tx.is_none());
        assert!(matches!(back.op, Op::Run { .. }));
        let r = Request { id: 8, tx: Some("1-2".into()), op: Op::DeleteFile { path: "/etc/portage/x".into() } };
        let back: Request = serde_json::from_str(&serde_json::to_string(&r).unwrap()).unwrap();
        assert_eq!(back.tx.as_deref(), Some("1-2"));
        assert_eq!(Op::Begin { label: String::new() }.min_helper(), (0, 4));
    }
}
