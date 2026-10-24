//! Stopping a job politely, then firmly: SIGINT (like Ctrl+C), SIGTERM after ten seconds,
//! SIGKILL after another ten. Pure, so the schedule can be tested without a process.

use std::time::Duration;

/// How long a signal gets before the next, harder one follows.
pub const STEP: Duration = Duration::from_secs(10);

/// The last signal sent to a job.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stage {
    Interrupted,
    Terminated,
    Killed,
}

impl Stage {
    pub fn signal(self) -> i32 {
        match self {
            Stage::Interrupted => libc::SIGINT,
            Stage::Terminated => libc::SIGTERM,
            Stage::Killed => libc::SIGKILL,
        }
    }

    pub fn describe(self) -> &'static str {
        match self {
            Stage::Interrupted => "Stop requested (SIGINT, like Ctrl+C). emerge will stop after cleaning up.",
            Stage::Terminated => "Still running after 10 seconds: sent SIGTERM.",
            Stage::Killed => "Still running after another 10 seconds: sent SIGKILL. Build directories may be left behind (Maintenance can clean them).",
        }
    }
}

/// The next signal to send, given the last one and how long ago it was sent (`None`: nothing sent yet).
pub fn next_stop(last: Option<Stage>, since_last: Duration) -> Option<Stage> {
    match last {
        None => Some(Stage::Interrupted),
        Some(Stage::Interrupted) if since_last >= STEP => Some(Stage::Terminated),
        Some(Stage::Terminated) if since_last >= STEP => Some(Stage::Killed),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn secs(n: u64) -> Duration {
        Duration::from_secs(n)
    }

    #[test]
    fn the_signals_escalate_every_ten_seconds() {
        assert_eq!(next_stop(None, secs(0)), Some(Stage::Interrupted));
        assert_eq!(next_stop(Some(Stage::Interrupted), secs(0)), None);
        assert_eq!(next_stop(Some(Stage::Interrupted), secs(9)), None);
        assert_eq!(next_stop(Some(Stage::Interrupted), secs(10)), Some(Stage::Terminated));
        assert_eq!(next_stop(Some(Stage::Terminated), secs(9)), None);
        assert_eq!(next_stop(Some(Stage::Terminated), secs(10)), Some(Stage::Killed));
        assert_eq!(next_stop(Some(Stage::Killed), secs(3600)), None, "nothing is harder than SIGKILL");
    }

    #[test]
    fn only_signals_the_helper_accepts_are_used() {
        for stage in [Stage::Interrupted, Stage::Terminated, Stage::Killed] {
            assert!(portagehub::protocol::ALLOWED_SIGNALS.contains(&stage.signal()), "{stage:?}");
        }
    }
}
