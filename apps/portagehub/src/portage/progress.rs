//! Live tracking of a running emerge: which package is at which stage, how
//! long it has been running and how long the rest will probably take.

use super::atom::split_cpv;
use super::history::History;
use regex::Regex;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stage {
    Queued,
    Fetching,
    Building,
    Installing,
    Done,
    Failed,
    Skipped,
}

impl Stage {
    pub fn label(&self) -> &'static str {
        match self {
            Stage::Queued => "Queued",
            Stage::Fetching => "Downloading",
            Stage::Building => "Building",
            Stage::Installing => "Installing",
            Stage::Done => "Done",
            Stage::Failed => "Failed",
            Stage::Skipped => "Skipped",
        }
    }

    pub fn active(&self) -> bool {
        matches!(self, Stage::Fetching | Stage::Building | Stage::Installing)
    }
}

#[derive(Debug, Clone)]
pub struct Item {
    pub cpv: String,
    pub key: String,
    pub stage: Stage,
    /// Build phase reported by the build log (unpack, configure, compile …).
    pub phase: String,
    pub started: Option<Instant>,
    pub finished: Option<Instant>,
    pub estimate: Duration,
    pub estimate_known: bool,
    pub binary: bool,
}

impl Item {
    pub fn elapsed(&self, now: Instant) -> Duration {
        match (self.started, self.finished) {
            (Some(s), Some(f)) => f - s,
            (Some(s), None) => now - s,
            _ => Duration::ZERO,
        }
    }

    /// Fraction of this package's own work done (0..1).
    pub fn fraction(&self, now: Instant) -> f32 {
        match self.stage {
            Stage::Done | Stage::Failed | Stage::Skipped => 1.0,
            Stage::Queued => 0.0,
            _ => {
                let e = self.elapsed(now).as_secs_f32();
                let est = self.estimate.as_secs_f32().max(1.0);
                let base = (e / est).min(0.97);
                if self.stage == Stage::Installing { base.max(0.9) } else { base }
            }
        }
    }

    /// `$PORTAGE_TMPDIR/portage/<cat>/<pf>/temp/build.log`
    pub fn build_dir_log(&self, tmpdir: &std::path::Path) -> Option<std::path::PathBuf> {
        let (cat, pn, ver) = split_cpv(&self.cpv)?;
        Some(tmpdir.join("portage").join(cat).join(format!("{pn}-{ver}")).join("temp/build.log"))
    }
}

#[derive(Debug, Clone)]
pub struct Tracker {
    pub items: Vec<Item>,
    pub total: usize,
    pub started: Instant,
    pub finished: Option<Instant>,
    pub last_status: String,
    pub jobs_line: String,
    pub messages_for: Vec<String>,
}

fn rx(cell: &'static OnceLock<Regex>, p: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(p).unwrap())
}

impl Tracker {
    /// `planned`: (cpv, repo, binary) from the preview, in merge order.
    pub fn new(planned: &[(String, String, bool)], history: &History) -> Tracker {
        let items = planned
            .iter()
            .map(|(cpv, _, binary)| {
                let key = split_cpv(cpv).map(|(c, p, _)| format!("{c}/{p}")).unwrap_or_else(|| cpv.clone());
                let (estimate, known) = if *binary { (Duration::from_secs(20), true) } else { history.estimate_or_default(&key) };
                Item {
                    cpv: cpv.clone(),
                    key,
                    stage: Stage::Queued,
                    phase: String::new(),
                    started: None,
                    finished: None,
                    estimate,
                    estimate_known: known,
                    binary: *binary,
                }
            })
            .collect::<Vec<_>>();
        Tracker {
            total: items.len(),
            items,
            started: Instant::now(),
            finished: None,
            last_status: "Starting emerge…".into(),
            jobs_line: String::new(),
            messages_for: Vec::new(),
        }
    }

    fn item_mut(&mut self, cpv: &str, history: &History) -> &mut Item {
        if let Some(i) = self.items.iter().position(|it| it.cpv == cpv) {
            return &mut self.items[i];
        }
        let key = split_cpv(cpv).map(|(c, p, _)| format!("{c}/{p}")).unwrap_or_else(|| cpv.to_string());
        let (estimate, known) = history.estimate_or_default(&key);
        self.items.push(Item {
            cpv: cpv.to_string(),
            key,
            stage: Stage::Queued,
            phase: String::new(),
            started: None,
            finished: None,
            estimate,
            estimate_known: known,
            binary: false,
        });
        self.items.last_mut().unwrap()
    }

    /// `at`: when the line was produced (not when it is processed).
    pub fn feed(&mut self, line: &str, history: &History, at: Instant) {
        static EMERGING: OnceLock<Regex> = OnceLock::new();
        static INSTALLING: OnceLock<Regex> = OnceLock::new();
        static COMPLETED: OnceLock<Regex> = OnceLock::new();
        static FAILED: OnceLock<Regex> = OnceLock::new();
        static ERROR: OnceLock<Regex> = OnceLock::new();
        static JOBS: OnceLock<Regex> = OnceLock::new();
        static FETCH: OnceLock<Regex> = OnceLock::new();
        static DROPPED: OnceLock<Regex> = OnceLock::new();
        static MSGS: OnceLock<Regex> = OnceLock::new();
        let emerging = rx(&EMERGING, r">>> Emerging (binary )?\((\d+) of (\d+)\) (\S+?)(?:::(\S+))?\s*$");
        let installing = rx(&INSTALLING, r">>> Installing \((\d+) of (\d+)\) (\S+?)(?:::\S+)?\s*$");
        let completed = rx(&COMPLETED, r">>> Completed \((\d+) of (\d+)\) (\S+?)(?:::\S+)?\s*$");
        let failed = rx(&FAILED, r">>> Failed to (?:emerge|install) (\S+?)(?:::\S+)?[,\s]");
        let error = rx(&ERROR, r"\* ERROR: (\S+?)(?:::\S+)? failed \((\S+) phase\)");
        let jobs = rx(&JOBS, r"Jobs: (\d+) of (\d+) complete(.*)$");
        let fetch = rx(&FETCH, r">>> Fetching \((\d+) of (\d+)\) (\S+?)(?:::\S+)?\s*$");
        let dropped = rx(&DROPPED, r"emerge --keep-going: (\S+?)(?:::\S+)? dropped");
        let msgs = rx(&MSGS, r"\* Messages for package (\S+?)(?:::\S+)?:");
        static UNMERGING: OnceLock<Regex> = OnceLock::new();
        let unmerging = rx(&UNMERGING, r">>> Unmerging(?: in)? \((\d+) of (\d+)\) (\S+?)(?:\.\.\.)?\s*$");
        let now = at;
        let t = line.trim();

        if let Some(c) = unmerging.captures(t) {
            // Unmerges run one after another: the previous one is finished.
            for it in self.items.iter_mut().filter(|i| i.stage.active()) {
                it.stage = Stage::Done;
                it.finished = Some(now);
            }
            if let Ok(n) = c[2].parse::<usize>() {
                self.total = self.total.max(n);
            }
            let cpv = c[3].to_string();
            let it = self.item_mut(&cpv, history);
            it.stage = Stage::Installing;
            it.phase = "removing".into();
            it.started.get_or_insert(now);
            self.last_status = format!("Removing {} ({} of {})", cpv, &c[1], &c[2]);
        } else if let Some(c) = emerging.captures(t) {
            if let Ok(n) = c[3].parse::<usize>() {
                self.total = self.total.max(n);
            }
            let cpv = c[4].to_string();
            let binary = c.get(1).is_some();
            let it = self.item_mut(&cpv, history);
            it.stage = Stage::Building;
            it.binary |= binary;
            if it.started.is_none() {
                it.started = Some(now);
            }
            self.last_status = format!("Emerging {} ({} of {})", cpv, &c[2], &c[3]);
        } else if let Some(c) = fetch.captures(t) {
            let cpv = c[3].to_string();
            let it = self.item_mut(&cpv, history);
            if it.stage == Stage::Queued {
                it.stage = Stage::Fetching;
                it.started.get_or_insert(now);
            }
        } else if let Some(c) = installing.captures(t) {
            let cpv = c[3].to_string();
            let it = self.item_mut(&cpv, history);
            it.stage = Stage::Installing;
            it.started.get_or_insert(now);
            self.last_status = format!("Installing {} ({} of {})", cpv, &c[1], &c[2]);
        } else if let Some(c) = completed.captures(t) {
            let cpv = c[3].to_string();
            let it = self.item_mut(&cpv, history);
            it.stage = Stage::Done;
            it.started.get_or_insert(now);
            it.finished = Some(now);
            it.phase.clear();
            self.last_status = format!("Completed {} ({} of {})", cpv, &c[1], &c[2]);
        } else if let Some(c) = failed.captures(t).or_else(|| error.captures(t)) {
            let cpv = c[1].to_string();
            let phase = c.get(2).map(|m| m.as_str().to_string());
            let it = self.item_mut(&cpv, history);
            it.stage = Stage::Failed;
            it.finished = Some(now);
            if let Some(p) = phase {
                it.phase = format!("failed in {p}");
            }
            self.last_status = format!("Failed: {cpv}");
        } else if let Some(c) = dropped.captures(t) {
            let cpv = c[1].to_string();
            let it = self.item_mut(&cpv, history);
            if it.stage == Stage::Queued {
                it.stage = Stage::Skipped;
            }
        } else if let Some(c) = jobs.captures(t) {
            self.jobs_line = format!("Jobs: {} of {} complete{}", &c[1], &c[2], c[3].trim_end());
        } else if let Some(c) = msgs.captures(t) {
            self.messages_for.push(c[1].to_string());
        } else if let Some(phase) = phase_of(t) {
            // Non-quiet builds print phases inline; attach to the single active one.
            let active: Vec<usize> = self.items.iter().enumerate().filter(|(_, i)| i.stage == Stage::Building).map(|(i, _)| i).collect();
            if active.len() == 1 {
                self.items[active[0]].phase = phase.to_string();
            }
        }
    }

    pub fn set_phase_from_log(&mut self, idx: usize, log_tail: &str) {
        if let Some(it) = self.items.get_mut(idx) {
            for l in log_tail.lines().rev() {
                if let Some(p) = phase_of(l.trim()) {
                    it.phase = p.to_string();
                    break;
                }
            }
        }
    }

    pub fn finish(&mut self, ok: bool) {
        let now = Instant::now();
        self.finished = Some(now);
        for it in &mut self.items {
            if it.stage.active() {
                it.stage = if ok { Stage::Done } else { Stage::Failed };
                it.finished = Some(now);
            }
        }
    }

    pub fn now(&self) -> Instant {
        self.finished.unwrap_or_else(Instant::now)
    }

    pub fn elapsed(&self) -> Duration {
        self.now() - self.started
    }

    pub fn counts(&self) -> (usize, usize, usize) {
        let done = self.items.iter().filter(|i| matches!(i.stage, Stage::Done)).count();
        let failed = self.items.iter().filter(|i| matches!(i.stage, Stage::Failed | Stage::Skipped)).count();
        let active = self.items.iter().filter(|i| i.stage.active()).count();
        (done, failed, active)
    }

    /// Weighted overall progress (0..1) by expected build time.
    pub fn fraction(&self) -> f32 {
        let now = self.now();
        let total: f32 = self.items.iter().map(|i| i.estimate.as_secs_f32().max(1.0)).sum();
        if total <= 0.0 {
            return 0.0;
        }
        let done: f32 = self.items.iter().map(|i| i.fraction(now) * i.estimate.as_secs_f32().max(1.0)).sum();
        (done / total).clamp(0.0, 1.0)
    }

    /// Expected remaining wall-clock time.
    pub fn remaining(&self) -> Duration {
        let now = self.now();
        let mut left = 0.0f32;
        for i in &self.items {
            let est = i.estimate.as_secs_f32().max(1.0);
            left += est * (1.0 - i.fraction(now));
        }
        let (_, _, active) = self.counts();
        let par = active.max(1) as f32;
        Duration::from_secs_f32(left / par.min(4.0))
    }
}

/// Map a build-log line to a short human phase name.
pub fn phase_of(l: &str) -> Option<&'static str> {
    Some(if l.starts_with(">>> Downloading") {
        "downloading sources"
    } else if l.starts_with(">>> Unpacking") {
        "unpacking"
    } else if l.starts_with(">>> Preparing source") || l.starts_with(">>> Source prepared") {
        "preparing"
    } else if l.starts_with(">>> Configuring source") || l.starts_with(">>> Source configured") {
        "configuring"
    } else if l.starts_with(">>> Compiling source") {
        "compiling"
    } else if l.starts_with(">>> Source compiled") {
        "compiled"
    } else if l.starts_with(">>> Test phase") {
        "running tests"
    } else if l.starts_with(">>> Install ") {
        "installing to image"
    } else if l.starts_with(">>> Completed installing") {
        "image ready"
    } else if l.starts_with(">>> Merging ") {
        "merging to system"
    } else if l.starts_with(">>> Safely unmerging") || l.starts_with(">>> Unmerging") {
        "removing old version"
    } else {
        return None;
    })
}

/// Read the tail of a (possibly large) file.
pub fn tail(path: &std::path::Path, bytes: u64) -> Option<String> {
    use std::io::{Read, Seek, SeekFrom};
    let mut f = std::fs::File::open(path).ok()?;
    let len = f.metadata().ok()?.len();
    let start = len.saturating_sub(bytes);
    f.seek(SeekFrom::Start(start)).ok()?;
    let mut buf = Vec::new();
    f.read_to_end(&mut buf).ok()?;
    let s = portagehub::stream::strip_ansi(&String::from_utf8_lossy(&buf));
    // Drop the first, probably partial, line.
    Some(if start > 0 { s.split_once('\n').map(|x| x.1.to_string()).unwrap_or(s) } else { s })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn feed_parallel() {
        let h = History::default();
        let mut t = Tracker::new(
            &[("app-misc/a-1".into(), "gentoo".into(), false), ("app-misc/b-2".into(), "gentoo".into(), false)],
            &h,
        );
        t.feed(">>> Emerging (1 of 2) app-misc/a-1::gentoo", &h, Instant::now());
        t.feed(">>> Emerging (2 of 2) app-misc/b-2::gentoo", &h, Instant::now());
        assert_eq!(t.counts().2, 2);
        t.feed(">>> Installing (1 of 2) app-misc/a-1::gentoo", &h, Instant::now());
        assert_eq!(t.items[0].stage, Stage::Installing);
        t.feed(">>> Completed (1 of 2) app-misc/a-1::gentoo", &h, Instant::now());
        t.feed(" * ERROR: app-misc/b-2::gentoo failed (compile phase):", &h, Instant::now());
        assert_eq!(t.items[0].stage, Stage::Done);
        assert_eq!(t.items[1].stage, Stage::Failed);
        assert_eq!(t.items[1].phase, "failed in compile");
        assert!((t.fraction() - 1.0).abs() < 1e-3);
    }

    #[test]
    fn unmerge_progress() {
        let h = History::default();
        let mut t = Tracker::new(&[("a/b-1".into(), String::new(), true), ("c/d-2".into(), String::new(), true)], &h);
        t.feed(">>> Unmerging (1 of 2) a/b-1...", &h, Instant::now());
        assert_eq!(t.items[0].stage, Stage::Installing);
        t.feed(">>> Unmerging (2 of 2) c/d-2...", &h, Instant::now());
        assert_eq!(t.items[0].stage, Stage::Done);
        assert_eq!(t.items[1].stage, Stage::Installing);
        t.finish(true);
        assert_eq!(t.counts().0, 2);
    }

    #[test]
    fn phases() {
        assert_eq!(phase_of(">>> Compiling source in /var/tmp/portage/x"), Some("compiling"));
        assert_eq!(phase_of("make: ok"), None);
    }
}
