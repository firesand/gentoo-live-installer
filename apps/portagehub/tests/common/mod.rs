//! Shared harness for the integration tests: the real helper binary, driven
//! over its JSON-lines pipe. It always runs unprivileged; tests that change
//! files point it at a scratch tree with `PORTAGEHUB_ROOT` (honoured only when
//! not root), so nothing outside the scratch tree is ever touched.
#![allow(dead_code)]

use portagehub::protocol::{Event, Op, Request};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex, mpsc};
use std::time::Duration;

pub struct Helper {
    pub child: std::process::Child,
    stdin: Option<std::process::ChildStdin>,
    rx: mpsc::Receiver<Event>,
    stderr: Arc<Mutex<String>>,
}

impl Helper {
    pub fn start() -> Helper {
        Helper::spawn(None, &[])
    }

    /// A helper that works on the scratch tree `root` instead of `/`.
    pub fn start_in(root: &Path) -> Helper {
        Helper::spawn(Some(root), &[])
    }

    pub fn spawn(root: Option<&Path>, env: &[(&str, &str)]) -> Helper {
        // As root the helper only talks to a socket whose peer is the user pkexec authenticated
        // (see peer.rs), and it ignores PORTAGEHUB_ROOT: these pipe-driven tests cannot work.
        assert!(unsafe { libc::geteuid() } != 0, "run the PortageHub integration tests as a normal user, not as root");
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_portagehub-helper"));
        cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        if let Some(root) = root {
            cmd.env("PORTAGEHUB_ROOT", root);
        }
        for (k, v) in env {
            cmd.env(k, v);
        }
        let mut child = cmd.spawn().expect("spawn helper");
        let stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let stderr = Arc::new(Mutex::new(String::new()));
        let (err, pipe) = (stderr.clone(), child.stderr.take().unwrap());
        std::thread::spawn(move || {
            for line in BufReader::new(pipe).lines().map_while(Result::ok) {
                let mut text = err.lock().unwrap();
                text.push_str(&line);
                text.push('\n');
            }
        });
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                let ev: Event = serde_json::from_str(&line).expect("valid event json");
                if tx.send(ev).is_err() {
                    break;
                }
            }
        });
        Helper { child, stdin: Some(stdin), rx, stderr }
    }

    pub fn send(&mut self, id: u64, op: Op) {
        self.send_request(Request::new(id, op));
    }

    /// A request that belongs to the open transaction `tx`.
    pub fn send_tx(&mut self, id: u64, tx: &str, op: Op) {
        self.send_request(Request { id, tx: Some(tx.to_string()), op });
    }

    pub fn send_request(&mut self, req: Request) {
        let mut line = serde_json::to_vec(&req).unwrap();
        line.push(b'\n');
        self.send_raw(&line);
    }

    /// Raw bytes exactly as given (no newline added).
    pub fn send_raw(&mut self, bytes: &[u8]) {
        let stdin = self.stdin.as_mut().expect("stdin is still open");
        stdin.write_all(bytes).unwrap();
        stdin.flush().unwrap();
    }

    /// End of input: the helper finishes its jobs and exits.
    pub fn close_stdin(&mut self) {
        self.stdin = None;
    }

    pub fn until(&self, mut pred: impl FnMut(&Event) -> bool, secs: u64) -> Vec<Event> {
        let mut seen = Vec::new();
        let deadline = std::time::Instant::now() + Duration::from_secs(secs);
        loop {
            let left = deadline.saturating_duration_since(std::time::Instant::now());
            let ev = self.rx.recv_timeout(left).unwrap_or_else(|_| panic!("timeout; saw {seen:?}"));
            let done = pred(&ev);
            seen.push(ev);
            if done {
                return seen;
            }
        }
    }

    /// Wait for the final `Ok` or `Err` of request `id`; `Ok(data)` carries the `Data` text, if any.
    pub fn finish(&self, id: u64) -> Result<String, String> {
        let evs = self.until(|e| matches!(e, Event::Ok { id: i } | Event::Err { id: i, .. } if *i == id), 30);
        let mut data = String::new();
        for e in evs {
            match e {
                Event::Data { id: i, text } if i == id => data = text,
                Event::Err { id: i, msg } if i == id => return Err(msg),
                _ => {}
            }
        }
        Ok(data)
    }

    /// Everything the helper wrote to stderr so far.
    pub fn stderr_text(&self) -> String {
        self.stderr.lock().unwrap().clone()
    }

    /// Wait up to `secs` for the helper to exit by itself.
    pub fn wait_exit(&mut self, secs: u64) -> Option<std::process::ExitStatus> {
        let deadline = std::time::Instant::now() + Duration::from_secs(secs);
        while std::time::Instant::now() < deadline {
            if let Some(st) = self.child.try_wait().unwrap() {
                std::thread::sleep(Duration::from_millis(50)); // let the stderr reader catch up
                return Some(st);
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        None
    }

    /// Close stdin and wait for the helper to exit.
    pub fn quit(mut self) -> std::process::ExitStatus {
        self.close_stdin();
        self.child.wait().unwrap()
    }
}

/// A scratch filesystem root with `etc/portage` in it; removed on drop.
pub struct Scratch(pub PathBuf);

impl Scratch {
    pub fn new(name: &str) -> Scratch {
        let d = std::env::temp_dir().join(format!("portagehub-it-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("etc/portage/package.use")).unwrap();
        Scratch(d)
    }

    pub fn path(&self, rel: &str) -> PathBuf {
        self.0.join(rel)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
