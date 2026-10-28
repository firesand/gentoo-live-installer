//! Where each tweak stands: applied by us or not, whether the running system
//! behaves that way, and what else configures the same thing.

use crate::catalog::{self, After, LiveState, Na, Param, Tweak};
use crate::detect::System;
use crate::managed;
use crate::plan::{self, Files, Installed};
use crate::sysctl;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Badge {
    /// Applied and in effect.
    Active,
    /// Applied, waiting for a reboot / re-login.
    Pending(After),
    /// Applied, but something else wins.
    Overridden,
    /// Our blocks were edited or are incomplete.
    Modified,
    /// Not applied by us, but the system already behaves that way.
    AlreadySet,
    Off,
    NotAvailable,
}

impl Badge {
    pub fn label(&self) -> &'static str {
        match self {
            Badge::Active => "ACTIVE",
            Badge::Pending(After::Reboot) => "REBOOT NEEDED",
            Badge::Pending(_) => "RE-LOGIN NEEDED",
            Badge::Overridden => "OVERRIDDEN",
            Badge::Modified => "MODIFIED",
            Badge::AlreadySet => "ALREADY SET",
            Badge::Off => "OFF",
            Badge::NotAvailable => "N/A",
        }
    }

    pub fn applied(&self) -> bool {
        matches!(self, Badge::Active | Badge::Pending(_) | Badge::Overridden | Badge::Modified)
    }
}

#[derive(Debug, Clone)]
pub struct Status {
    pub tweak: &'static Tweak,
    pub available: Result<(), Na>,
    pub installed: Option<Installed>,
    pub live: catalog::Live,
    /// Other configuration touching the same setting.
    pub conflicts: Vec<String>,
    pub badge: Badge,
}

impl Status {
    /// The value in effect for display: the installed one, else the default.
    pub fn value(&self) -> String {
        self.installed.as_ref().map(|i| i.value.clone()).unwrap_or_else(|| self.tweak.default.to_string())
    }
}

pub struct Scan {
    pub statuses: Vec<Status>,
    pub sysctl: sysctl::Sources,
    pub unreadable: Vec<String>,
}

impl Scan {
    pub fn get(&self, id: &str) -> Option<&Status> {
        self.statuses.iter().find(|s| s.tweak.id == id)
    }
}

pub fn scan(sys: &System) -> Scan {
    let files = Files::load(sys);
    let sources = sysctl::load(sys);
    let mut statuses = Vec::new();
    for t in catalog::CATALOG {
        let available = t.availability(sys);
        let installed = plan::installed(sys, &files, t);
        let value = installed.as_ref().map(|i| i.value.clone()).unwrap_or_else(|| t.default.to_string());
        let live = (t.check)(&t.ctx(sys, &value));
        let mut conflicts = Vec::new();
        let mut overridden = false;
        if t.is_sysctl() {
            for s in sources.overrides(t.key, managed::SYSCTL) {
                if s.value != crate::detect::norm_ws(&value) {
                    overridden = true;
                }
                conflicts.push(format!("{} sets {} = {} after ours", s.file, s.key, s.value));
            }
            for s in sources.others(t.key, managed::SYSCTL) {
                if !sources.overrides(t.key, managed::SYSCTL).iter().any(|o| o.file == s.file) {
                    conflicts.push(format!("{} sets {} = {} (applied before ours)", s.file, s.key, s.value));
                }
            }
            if live.state == LiveState::Differs && installed.is_some() {
                for f in sources.unreadable_after(managed::SYSCTL) {
                    conflicts.push(format!("{f} is applied after ours and is only readable by root — it may set {}", t.key));
                }
            }
        }
        let badge = match (&installed, live.state) {
            (Some(i), _) if i.modified || i.partial => Badge::Modified,
            (Some(_), LiveState::Differs) if overridden => Badge::Overridden,
            (Some(_), LiveState::Differs) if t.after == After::Now => Badge::Overridden,
            (Some(_), LiveState::Differs) => Badge::Pending(t.after),
            (Some(_), _) => Badge::Active,
            (None, _) if available.is_err() => Badge::NotAvailable,
            (None, LiveState::Match) => Badge::AlreadySet,
            (None, _) => Badge::Off,
        };
        statuses.push(Status { tweak: t, available, installed, live, conflicts, badge });
    }
    let mut unreadable = files.unreadable;
    unreadable.extend(sources.unreadable.iter().cloned());
    Scan { statuses, sysctl: sources, unreadable }
}

/// Does the tweak take a value the user can change?
pub fn has_param(t: &Tweak) -> bool {
    !matches!(t.param, Param::Fixed)
}
