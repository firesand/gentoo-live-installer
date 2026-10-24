//! Settings that make Portage run commands as root.
//!
//! The helper's `/etc/portage` write confinement is a speed bump, not a sandbox: `make.conf` can
//! set `FETCHCOMMAND` (run for every download) or `PORTAGE_BASHRC` (sourced in every build phase),
//! and a `repos.conf` entry can point at a tree outside `/var/db/repos` whose eclasses run as
//! root. Legitimate setups do both (aria2 users, local overlays), so they are allowed, but the
//! review dialog says plainly that the lines it is about to write run commands as root.

use crate::app::FileChange;

/// Variables whose value Portage executes (or sources) as root.
const COMMAND_VARIABLES: &[&str] = &[
    "FETCHCOMMAND",
    "RESUMECOMMAND",
    "PORTAGE_ELOG_COMMAND",
    "PORTAGE_BASHRC",
    "PORTAGE_COMPRESS",
    "PORTAGE_RSYNC_EXTRA_OPTS",
    "PORTAGE_RSYNC_OPTS",
];

/// Where repositories normally live: a `location` elsewhere is code Portage will run as root.
const REPOS_ROOT: &str = "/var/db/repos/";

/// A line of a change that runs commands as root, and why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Warning {
    pub line: String,
    pub why: String,
}

fn assigned_variable(line: &str) -> Option<&str> {
    let t = line.trim_start();
    let t = t.strip_prefix("export").map(str::trim_start).unwrap_or(t);
    let (name, _) = t.split_once('=')?;
    let name = name.trim_end();
    (!name.is_empty() && name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')).then_some(name)
}

fn is_command_variable(name: &str) -> bool {
    COMMAND_VARIABLES.iter().any(|v| name == *v || (name.starts_with(&format!("{v}_")) && name[v.len() + 1..].bytes().all(|b| b.is_ascii_uppercase() || b == b'_')))
}

fn repository_location(line: &str) -> Option<&str> {
    let (key, value) = line.trim().split_once('=')?;
    (key.trim() == "location").then(|| value.trim().trim_matches(['"', '\'']))
}

/// The lines `change` adds that make Portage run commands as root (see the module docs).
pub fn flag_dangerous_lines(change: &FileChange) -> Vec<Warning> {
    let Some(new) = &change.new else { return Vec::new() };
    let old: std::collections::HashSet<&str> = change.old.lines().map(str::trim).collect();
    let mut out = Vec::new();
    for line in new.lines() {
        let t = line.trim();
        if t.is_empty() || t.starts_with('#') || old.contains(t) {
            continue;
        }
        if let Some(name) = assigned_variable(t)
            && is_command_variable(name)
        {
            out.push(Warning { line: t.to_string(), why: format!("{name} is run as root by Portage") });
        } else if let Some(loc) = repository_location(t)
            && !loc.starts_with(REPOS_ROOT)
        {
            out.push(Warning { line: t.to_string(), why: format!("the repository at {loc} is outside {REPOS_ROOT}: its ebuilds and eclasses run as root") });
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn change(old: &str, new: &str) -> FileChange {
        FileChange { path: PathBuf::from("/etc/portage/make.conf"), old: old.into(), new: Some(new.into()) }
    }

    fn lines(old: &str, new: &str) -> Vec<String> {
        flag_dangerous_lines(&change(old, new)).into_iter().map(|w| w.line).collect()
    }

    #[test]
    fn commands_assigned_in_make_conf_are_flagged() {
        let new = "MAKEOPTS=\"-j8\"\nFETCHCOMMAND=\"aria2c -d \\\"\\${DISTDIR}\\\" \\${URI}\"\nexport PORTAGE_ELOG_COMMAND='mail -s x root'\nPORTAGE_BASHRC=/etc/portage/hooks\nFETCHCOMMAND_HTTPS=\"curl\"\n  RESUMECOMMAND = \"x\"\n";
        let got = lines("MAKEOPTS=\"-j8\"\n", new);
        assert_eq!(got.len(), 5, "{got:?}");
        assert!(got[0].starts_with("FETCHCOMMAND=") && got[4].starts_with("RESUMECOMMAND"));
    }

    #[test]
    fn harmless_and_unchanged_lines_are_not_flagged() {
        assert!(lines("", "MAKEOPTS=\"-j8\"\nFEATURES=\"ccache\"\n# FETCHCOMMAND=\"x\"\nFETCHCOMMANDS=\"not a variable of Portage\"\n").is_empty());
        // A line that was already there is not the change's doing.
        assert!(lines("FETCHCOMMAND=\"aria2c\"\n", "FETCHCOMMAND=\"aria2c\"\nMAKEOPTS=\"-j4\"\n").is_empty());
        // Deleting a file adds nothing.
        assert!(flag_dangerous_lines(&FileChange { path: PathBuf::from("/etc/portage/make.conf"), old: "FETCHCOMMAND=x\n".into(), new: None }).is_empty());
        // Package names that merely contain the words.
        assert!(lines("", "app-misc/foo-1 FETCHCOMMAND\n").is_empty());
    }

    #[test]
    fn repositories_outside_the_repo_tree_are_flagged() {
        let ours = "[guru]\nlocation = /var/db/repos/guru\nsync-type = git\nsync-uri = https://anongit.gentoo.org/git/repo/proj/guru.git\n";
        assert!(lines("", ours).is_empty());
        let foreign = "[mine]\nlocation = /home/me/overlay\nsync-type = git\n";
        let w = flag_dangerous_lines(&change("", foreign));
        assert_eq!(w.len(), 1);
        assert_eq!(w[0].line, "location = /home/me/overlay");
        assert!(w[0].why.contains("/home/me/overlay") && w[0].why.contains("as root"));
        assert_eq!(lines("", "[x]\nlocation=\"/srv/overlay\"\n").len(), 1);
        // The location of a repository that was already configured is not new.
        assert!(lines(foreign, &format!("{foreign}auto-sync = no\n")).is_empty());
    }
}
