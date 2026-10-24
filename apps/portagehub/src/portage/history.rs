//! /var/log/emerge.log: merge history and build-time estimates.

use super::atom::split_cpv;
use std::collections::HashMap;
use std::time::Duration;

pub const EMERGE_LOG: &str = "/var/log/emerge.log";

#[derive(Debug, Clone)]
pub struct MergeRecord {
    pub cpv: String,
    pub key: String,
    pub ver: String,
    pub start: i64,
    pub end: i64,
    pub ok: bool,
    pub binary: bool,
}

impl MergeRecord {
    pub fn duration(&self) -> Duration {
        Duration::from_secs((self.end - self.start).max(0) as u64)
    }
}

#[derive(Debug, Clone)]
pub struct Session {
    pub start: i64,
    pub args: String,
}

/// Why emerge.log has no history to offer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LogProblem {
    /// The file does not exist: no emerge has logged yet.
    Missing,
    /// It exists (0660 portage:portage) but the user is not in the portage group.
    Denied,
    Other(String),
}

impl LogProblem {
    pub fn of(e: &std::io::Error) -> LogProblem {
        match e.kind() {
            std::io::ErrorKind::NotFound => LogProblem::Missing,
            std::io::ErrorKind::PermissionDenied => LogProblem::Denied,
            _ => LogProblem::Other(e.to_string()),
        }
    }

    /// What to tell the user: (heading, advice).
    pub fn message(&self) -> (&'static str, String) {
        match self {
            LogProblem::Missing => ("No emerge.log yet", "Portage writes /var/log/emerge.log with the first emerge on this system; the history, the recently installed list and build-time estimates fill in from then on.".into()),
            LogProblem::Denied => (
                "emerge.log is not readable",
                "Add your user to the portage group (usermod -aG portage <user>, then log in again) to see the history, the recently installed list and build-time estimates.".into(),
            ),
            LogProblem::Other(e) => ("emerge.log could not be read", e.clone()),
        }
    }
}

#[derive(Debug, Default)]
pub struct History {
    pub merges: Vec<MergeRecord>,
    pub unmerges: Vec<(i64, String)>,
    pub syncs: Vec<(i64, String)>,
    pub sessions: Vec<Session>,
    /// key -> indices of successful source merges
    pub by_key: HashMap<String, Vec<usize>>,
    pub median_build: Duration,
    pub readable: bool,
    /// Set when the log could not be read at all (then `readable` is false).
    pub problem: Option<LogProblem>,
}

impl History {
    /// The history of a log that could not be read. Build-time estimates fall back to the same
    /// default a log without usable builds gets (a minute), not to zero.
    pub fn unavailable(problem: LogProblem) -> History {
        History { problem: Some(problem), median_build: Duration::from_secs(60), ..Default::default() }
    }

    /// Expected build time for `cat/pn`: weighted towards the latest builds.
    pub fn estimate(&self, key: &str) -> Option<Duration> {
        let idx = self.by_key.get(key)?;
        let recent: Vec<u64> = idx.iter().rev().take(4).map(|&i| self.merges[i].duration().as_secs()).collect();
        if recent.is_empty() {
            return None;
        }
        let (mut sum, mut wsum) = (0.0, 0.0);
        for (n, d) in recent.iter().enumerate() {
            let w = 1.0 / (n as f64 + 1.0);
            sum += *d as f64 * w;
            wsum += w;
        }
        Some(Duration::from_secs_f64(sum / wsum))
    }

    pub fn estimate_or_default(&self, key: &str) -> (Duration, bool) {
        match self.estimate(key) {
            Some(d) => (d, true),
            None => (self.median_build.max(Duration::from_secs(30)), false),
        }
    }

    pub fn records_for(&self, key: &str) -> Vec<&MergeRecord> {
        self.merges.iter().filter(|m| m.key == key).collect()
    }

    pub fn last_sync(&self) -> Option<i64> {
        self.syncs.last().map(|s| s.0)
    }
}

fn after_of(s: &str) -> Option<&str> {
    // "(3 of 8) cat/pf to /" -> "cat/pf"
    let rest = s.split_once(") ")?.1;
    Some(rest.split(" to ").next()?.trim())
}

pub fn parse(text: &str) -> History {
    let mut h = History { readable: true, ..Default::default() };
    let mut open: HashMap<String, (i64, bool)> = HashMap::new();
    let close_failed = |open: &mut HashMap<String, (i64, bool)>, h: &mut History, ts: i64| {
        for (cpv, (start, binary)) in open.drain() {
            if let Some((c, p, v)) = split_cpv(&cpv) {
                h.merges.push(MergeRecord { key: format!("{c}/{p}"), ver: v.raw, cpv, start, end: ts, ok: false, binary });
            }
        }
    };
    for line in text.lines() {
        let Some((ts, msg)) = line.split_once(':') else { continue };
        let Ok(ts) = ts.trim().parse::<i64>() else { continue };
        let msg = msg.trim();
        if let Some(r) = msg.strip_prefix(">>> emerge (") {
            if let Some(cpv) = after_of(r) {
                open.insert(cpv.to_string(), (ts, false));
            }
        } else if let Some(r) = msg.strip_prefix("::: completed emerge (") {
            if let Some(cpv) = after_of(r)
                && let Some((start, binary)) = open.remove(cpv)
                && let Some((c, p, v)) = split_cpv(cpv)
            {
                h.merges.push(MergeRecord { key: format!("{c}/{p}"), ver: v.raw, cpv: cpv.to_string(), start, end: ts, ok: true, binary });
            }
        } else if msg.starts_with("=== (") && msg.contains("Merging Binary (") {
            if let Some(cpv) = msg.split("Merging Binary (").nth(1).and_then(|s| s.split("::").next())
                && let Some(o) = open.get_mut(cpv)
            {
                o.1 = true;
            }
        } else if let Some(r) = msg.strip_prefix(">>> unmerge success: ") {
            h.unmerges.push((ts, r.trim().to_string()));
        } else if let Some(r) = msg.strip_prefix("=== Sync completed for ") {
            h.syncs.push((ts, r.trim().to_string()));
        } else if msg.starts_with("=== Sync completed") {
            h.syncs.push((ts, "all".into()));
        } else if let Some(r) = msg.strip_prefix("*** emerge ") {
            h.sessions.push(Session { start: ts, args: r.to_string() });
        } else if msg == "*** terminating." || msg.starts_with("Started emerge on:") {
            close_failed(&mut open, &mut h, ts);
        }
    }
    let last_ts = text.lines().last().and_then(|l| l.split_once(':')).and_then(|(t, _)| t.trim().parse().ok()).unwrap_or(0);
    // Whatever is still open is either running right now or crashed; skip.
    let _ = last_ts;
    h.merges.sort_by_key(|m| m.end);
    let mut durations = Vec::new();
    for (i, m) in h.merges.iter().enumerate() {
        if m.ok && !m.binary {
            h.by_key.entry(m.key.clone()).or_default().push(i);
            durations.push(m.duration().as_secs());
        }
    }
    durations.sort_unstable();
    h.median_build = Duration::from_secs(durations.get(durations.len() / 2).copied().unwrap_or(60));
    h
}

pub fn load() -> History {
    load_from(std::path::Path::new(EMERGE_LOG))
}

pub fn load_from(path: &std::path::Path) -> History {
    match std::fs::read(path) {
        Ok(bytes) => parse(&String::from_utf8_lossy(&bytes)),
        Err(e) => History::unavailable(LogProblem::of(&e)),
    }
}

pub fn fmt_duration(d: Duration) -> String {
    let s = d.as_secs();
    if s < 60 {
        format!("{s}s")
    } else if s < 3600 {
        format!("{}m {:02}s", s / 60, s % 60)
    } else {
        format!("{}h {:02}m", s / 3600, (s % 3600) / 60)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_missing_log_and_an_unreadable_one_are_told_apart() {
        let d = std::env::temp_dir().join(format!("portagehub-history-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        let missing = load_from(&d.join("emerge.log"));
        assert!(!missing.readable);
        assert_eq!(missing.problem, Some(LogProblem::Missing));
        assert_eq!(missing.median_build, Duration::from_secs(60), "estimates do not collapse to zero");
        assert_eq!(missing.problem.as_ref().unwrap().message().0, "No emerge.log yet");
        // 0660 portage:portage for a user outside the group.
        let denied_path = d.join("denied.log");
        std::fs::write(&denied_path, "1: Started emerge on: x\n").unwrap();
        std::fs::set_permissions(&denied_path, std::os::unix::fs::PermissionsExt::from_mode(0o000)).unwrap();
        if std::fs::File::open(&denied_path).is_err() {
            let denied = load_from(&denied_path);
            assert_eq!(denied.problem, Some(LogProblem::Denied));
            let (title, advice) = denied.problem.unwrap().message();
            assert_eq!(title, "emerge.log is not readable");
            assert!(advice.contains("portage group"), "{advice}");
        }
        std::fs::set_permissions(&denied_path, std::os::unix::fs::PermissionsExt::from_mode(0o644)).unwrap();
        let ok = load_from(&denied_path);
        assert!(ok.readable && ok.problem.is_none());
        // A directory where the file should be is something else.
        let dir_log = d.join("dir.log");
        std::fs::create_dir_all(&dir_log).unwrap();
        assert!(matches!(load_from(&dir_log).problem, Some(LogProblem::Other(_))));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn parse_log() {
        let log = "\
1000: Started emerge on: Oct 23, 2026 05:18:58
1000:  *** emerge --verbose --update @world
1010:  >>> emerge (1 of 2) app-misc/foo-1.0 to /
1020:  >>> emerge (2 of 2) app-misc/bar-2.0 to /
1070:  ::: completed emerge (1 of 2) app-misc/foo-1.0 to /
1100:  *** terminating.
1200:  >>> unmerge success: app-misc/old-0.1
1300:  === Sync completed for gentoo
";
        let h = parse(log);
        assert_eq!(h.merges.len(), 2);
        let ok: Vec<_> = h.merges.iter().filter(|m| m.ok).collect();
        assert_eq!(ok[0].key, "app-misc/foo");
        assert_eq!(ok[0].duration().as_secs(), 60);
        assert!(h.merges.iter().any(|m| !m.ok && m.key == "app-misc/bar"));
        assert_eq!(h.estimate("app-misc/foo").unwrap().as_secs(), 60);
        assert_eq!(h.unmerges.len(), 1);
        assert_eq!(h.syncs[0].1, "gentoo");
        assert_eq!(h.sessions[0].args, "--verbose --update @world");
    }
}
