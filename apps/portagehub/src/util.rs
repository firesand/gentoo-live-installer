//! Small helpers that do not belong to a page.

use std::io::Read;
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

/// Why [`run_with_timeout`] has no output.
#[derive(Debug)]
pub enum RunError {
    /// The program could not be started (not installed, no permission).
    Spawn(std::io::Error),
    /// It was still running after the limit and was killed.
    TimedOut,
}

impl std::fmt::Display for RunError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RunError::Spawn(e) => write!(f, "could not be started: {e}"),
            RunError::TimedOut => write!(f, "did not answer in time"),
        }
    }
}

/// Run `cmd` and collect its output, but give up after `limit`: a tool talking to a wedged
/// driver (`nvidia-smi`) would otherwise freeze whoever waits for it.
///
/// A program that ignores the kill (stuck in the kernel) is left to a reaper thread; the caller
/// is not held up by it.
pub fn run_with_timeout(mut cmd: Command, limit: Duration) -> Result<Output, RunError> {
    cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = cmd.spawn().map_err(RunError::Spawn)?;
    let readers: Vec<_> = [child.stdout.take().map(|p| Box::new(p) as Box<dyn Read + Send>), child.stderr.take().map(|p| Box::new(p) as Box<dyn Read + Send>)]
        .into_iter()
        .map(|pipe| {
            std::thread::spawn(move || {
                let mut buf = Vec::new();
                if let Some(mut p) = pipe {
                    let _ = p.read_to_end(&mut buf);
                }
                buf
            })
        })
        .collect();
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let mut bufs = readers.into_iter().map(|r| r.join().unwrap_or_default());
                return Ok(Output { status, stdout: bufs.next().unwrap_or_default(), stderr: bufs.next().unwrap_or_default() });
            }
            Ok(None) if started.elapsed() >= limit => {
                let _ = child.kill();
                // Waiting here could block as long as the program is stuck: reap it elsewhere.
                std::thread::spawn(move || {
                    let _ = child.wait();
                });
                return Err(RunError::TimedOut);
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(50)),
            Err(e) => return Err(RunError::Spawn(e)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sh(script: &str) -> Command {
        let mut c = Command::new("sh");
        c.args(["-c", script]);
        c
    }

    #[test]
    fn quick_commands_return_their_output_and_status() {
        let ok = run_with_timeout(sh("echo hello; echo oops >&2"), Duration::from_secs(5)).unwrap();
        assert!(ok.status.success());
        assert_eq!(String::from_utf8_lossy(&ok.stdout), "hello\n");
        assert_eq!(String::from_utf8_lossy(&ok.stderr), "oops\n");
        let bad = run_with_timeout(sh("exit 3"), Duration::from_secs(5)).unwrap();
        assert_eq!(bad.status.code(), Some(3));
        assert!(matches!(run_with_timeout(Command::new("/nonexistent/program"), Duration::from_secs(1)), Err(RunError::Spawn(_))));
    }

    #[test]
    fn a_hanging_command_is_killed_at_the_limit() {
        let pidfile = std::env::temp_dir().join(format!("portagehub-timeout-{}", std::process::id()));
        let started = Instant::now();
        let r = run_with_timeout(sh(&format!("echo $$ > {}; exec sleep 5", pidfile.display())), Duration::from_millis(300));
        assert!(matches!(r, Err(RunError::TimedOut)), "{r:?}");
        assert!(started.elapsed() < Duration::from_secs(2), "{:?}", started.elapsed());
        // The process is gone shortly after (the reaper collects it).
        let pid: i32 = std::fs::read_to_string(&pidfile).unwrap().trim().parse().unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        while unsafe { libc::kill(pid, 0) } == 0 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert_ne!(unsafe { libc::kill(pid, 0) }, 0, "the hung process was not killed");
        let _ = std::fs::remove_file(pidfile);
    }

    #[test]
    fn output_larger_than_a_pipe_does_not_deadlock() {
        let out = run_with_timeout(sh("head -c 300000 /dev/zero"), Duration::from_secs(5)).unwrap();
        assert_eq!(out.stdout.len(), 300_000);
    }
}
