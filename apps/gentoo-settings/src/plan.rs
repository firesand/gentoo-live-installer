//! Turning a set of tweak changes into file edits and commands. The GUI uses
//! this to preview a change, the helper to carry it out — both compute the
//! same plan from the same catalog, so the helper never takes file names or
//! contents from the GUI, only tweak ids and validated values.

use crate::catalog::{self, Impl, Tweak};
use crate::detect::System;
use crate::managed::{self, Block, Parsed};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Prog {
    Sysctl,
    Udevadm,
    Modprobe,
    RcUpdate,
    RcService,
    Systemctl,
    /// One of our own managed scripts.
    Script(String),
}

impl Prog {
    pub fn name(&self) -> &str {
        match self {
            Prog::Sysctl => "sysctl",
            Prog::Udevadm => "udevadm",
            Prog::Modprobe => "modprobe",
            Prog::RcUpdate => "rc-update",
            Prog::RcService => "rc-service",
            Prog::Systemctl => "systemctl",
            Prog::Script(p) => p,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Cmd {
    /// Remember the live value of a /proc/sys or /sys file (once) for a later restore.
    Save {
        path: String,
    },
    /// Save (if not saved yet) and write a value to a /proc/sys or /sys file.
    Write {
        path: String,
        value: String,
    },
    /// Put back the value saved for `path`, if any, and forget it.
    Restore {
        path: String,
    },
    Exec {
        prog: Prog,
        args: Vec<String>,
    },
}

impl Cmd {
    pub fn describe(&self) -> String {
        match self {
            Cmd::Save { path } => format!("remember the current value of {path}"),
            Cmd::Write { path, value } => format!("echo {value} > {path}"),
            Cmd::Restore { path } => format!("restore the remembered value of {path}"),
            Cmd::Exec { prog, args } => {
                let mut s = prog.name().to_string();
                for a in args {
                    s.push(' ');
                    s.push_str(a);
                }
                s
            }
        }
    }

    /// Order in which post-write commands run: values first (so `sysctl
    /// --system` sees saved state), then reloads, then services and scripts.
    fn rank(&self) -> u8 {
        match self {
            Cmd::Save { .. } | Cmd::Write { .. } | Cmd::Restore { .. } => 0,
            Cmd::Exec { prog: Prog::Systemctl, args } if args.first().is_some_and(|a| a == "daemon-reload") => 1,
            Cmd::Exec { prog: Prog::Sysctl, .. } => 2,
            Cmd::Exec { prog: Prog::Udevadm, args } if args.first().is_some_and(|a| a == "control") => 3,
            Cmd::Exec { prog: Prog::Udevadm, .. } => 4,
            Cmd::Exec { prog: Prog::Modprobe, .. } => 5,
            Cmd::Exec { prog: Prog::RcUpdate | Prog::RcService | Prog::Systemctl, .. } => 6,
            Cmd::Exec { prog: Prog::Script(_), .. } => 7,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Change {
    pub id: String,
    pub enable: bool,
    /// Parameter value when enabling (`None`: the default).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub value: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileChange {
    /// Absolute path on the real system.
    pub path: String,
    /// We could not read the file; `old` is reconstructed from our records.
    pub unreadable: bool,
    pub old: Option<String>,
    /// `None` deletes the file.
    pub new: Option<String>,
    pub mode: u32,
}

#[derive(Debug, Clone, Default)]
pub struct Plan {
    pub files: Vec<FileChange>,
    /// Run before files change (stopping services of tweaks being disabled).
    pub pre: Vec<Cmd>,
    /// Run after files change.
    pub post: Vec<Cmd>,
    /// The state before, as changes that would undo this plan.
    pub undo: Vec<Change>,
    /// Managed files that could not be read (not changed by this plan).
    pub unreadable: Vec<String>,
}

/// The managed files as they are now, parsed.
pub struct Files {
    pub parsed: BTreeMap<&'static str, Parsed>,
    pub raw: BTreeMap<&'static str, Option<String>>,
    pub unreadable: Vec<String>,
}

impl Files {
    pub fn load(sys: &System) -> Files {
        let mut parsed = BTreeMap::new();
        let mut raw = BTreeMap::new();
        let mut unreadable = Vec::new();
        let state = crate::apply::State::load(sys);
        for f in managed::FILES {
            match sys.read_raw(f.path) {
                Ok(t) => {
                    parsed.insert(f.path, managed::parse(&t));
                    raw.insert(f.path, Some(t));
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    parsed.insert(f.path, Parsed::default());
                    raw.insert(f.path, None);
                }
                Err(_) => {
                    // Reconstruct our blocks from the helper's records.
                    let mut p = Parsed::default();
                    for (id, value) in state.files.get(f.path).into_iter().flatten() {
                        let Some(t) = catalog::get(id) else { continue };
                        let v = value.clone().unwrap_or_else(|| t.default.to_string());
                        let imp = (t.build)(&t.ctx(sys, &v));
                        if let Some((_, body)) = imp.blocks.into_iter().find(|(p, _)| *p == f.path) {
                            p.blocks.push(Block { id: id.clone(), value: value.clone(), body });
                        }
                    }
                    raw.insert(f.path, managed::compose(f, &p.foreign, &p.blocks));
                    parsed.insert(f.path, p);
                    unreadable.push(f.path.to_string());
                }
            }
        }
        Files { parsed, raw, unreadable }
    }

    /// Blocks of `id` across all managed files: (file, block).
    pub fn blocks_of(&self, id: &str) -> Vec<(&'static str, &Block)> {
        self.parsed.iter().flat_map(|(p, f)| f.blocks.iter().filter(|b| b.id == id).map(move |b| (*p, b))).collect()
    }
}

/// What is installed for a tweak, judged from its blocks.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Installed {
    pub value: String,
    /// A block differs from what we would write (hand-edited, older version).
    pub modified: bool,
    /// Some blocks are missing (or belong to the other init system).
    pub partial: bool,
}

/// Lines that matter when comparing a block with what we would write.
fn significant(body: &str) -> Vec<String> {
    body.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')).map(crate::detect::norm_ws).collect()
}

pub fn installed(sys: &System, files: &Files, t: &Tweak) -> Option<Installed> {
    let have = files.blocks_of(t.id);
    if have.is_empty() {
        return None;
    }
    let value = have.iter().find_map(|(_, b)| b.value.clone()).unwrap_or_else(|| t.default.to_string());
    let value = t.validate(sys, Some(&value)).unwrap_or(value);
    let want = (t.build)(&t.ctx(sys, &value));
    let mut modified = false;
    let mut partial = false;
    for (path, body) in &want.blocks {
        match have.iter().find(|(p, _)| p == path) {
            Some((_, b)) => modified |= significant(&b.body) != significant(body),
            None => partial = true,
        }
    }
    // Blocks in files this init system doesn't use.
    partial |= have.iter().any(|(p, _)| !want.blocks.iter().any(|(w, _)| w == p));
    Some(Installed { value, modified, partial })
}

fn uses_value(t: &Tweak) -> bool {
    !matches!(t.param, catalog::Param::Fixed)
}

pub fn plan(sys: &System, changes: &[Change]) -> Result<Plan, String> {
    let files = Files::load(sys);
    let mut out = Plan { unreadable: files.unreadable.clone(), ..Default::default() };
    let mut parsed = files.parsed.clone();
    let mut post: Vec<Cmd> = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for ch in changes {
        let t = catalog::get(&ch.id).ok_or_else(|| format!("unknown tweak {}", ch.id))?;
        if !seen.insert(t.id) {
            return Err(format!("{} is changed twice", t.id));
        }
        let before = installed(sys, &files, t);
        out.undo.push(match &before {
            Some(i) => Change { id: t.id.into(), enable: true, value: uses_value(t).then(|| i.value.clone()) },
            None => Change { id: t.id.into(), enable: false, value: None },
        });
        // Remove the tweak's blocks everywhere (also leftovers of the other init system).
        for f in parsed.values_mut() {
            f.blocks.retain(|b| b.id != t.id);
        }
        if ch.enable {
            t.availability(sys).map_err(|na| format!("{}: {}", t.title, na.reason))?;
            let value = t.validate(sys, ch.value.as_deref())?;
            let imp: Impl = (t.build)(&t.ctx(sys, &value));
            for (path, body) in imp.blocks {
                let f = parsed.get_mut(path).ok_or_else(|| format!("{path} is not a managed file"))?;
                f.blocks.push(Block { id: t.id.to_string(), value: uses_value(t).then(|| value.clone()), body });
            }
            post.extend(imp.on_enable);
        } else {
            if before.is_none() {
                continue;
            }
            let value = before.as_ref().map(|i| i.value.clone()).unwrap_or_else(|| t.default.to_string());
            let imp = (t.build)(&t.ctx(sys, &value));
            out.pre.extend(imp.before_disable);
            post.extend(imp.after_disable);
        }
    }
    for spec in managed::FILES {
        let Some(f) = parsed.get_mut(spec.path) else { continue };
        // Catalog order, unknown ids (from a newer version) last in their old order.
        f.blocks.sort_by_key(|b| catalog::index_of(&b.id));
        let old = files.raw.get(spec.path).cloned().flatten();
        let new = managed::compose(spec, &f.foreign, &f.blocks);
        let unchanged = match (&old, &new) {
            (Some(o), Some(n)) => managed::parse(o) == managed::parse(n),
            (None, None) => true,
            _ => false,
        };
        if !unchanged {
            let unreadable = files.unreadable.iter().any(|u| u == spec.path);
            out.files.push(FileChange { path: spec.path.to_string(), unreadable, old, new, mode: spec.mode });
        }
    }
    // Stable order by rank, each command once.
    post.sort_by_key(Cmd::rank);
    let mut dedup: Vec<Cmd> = Vec::new();
    for c in post {
        if !dedup.contains(&c) {
            dedup.push(c);
        }
    }
    out.post = dedup;
    let mut pre: Vec<Cmd> = Vec::new();
    for c in std::mem::take(&mut out.pre) {
        if !pre.contains(&c) {
            pre.push(c);
        }
    }
    out.pre = pre;
    Ok(out)
}
