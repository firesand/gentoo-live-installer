//! Spawning a process and streaming its stdout/stderr line by line.

use crate::protocol::{Event, StreamKind};
use std::io::Read;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Command, Stdio};
use std::sync::Arc;

/// Remove ANSI escape sequences (CSI, OSC and lone ESC pairs).
pub fn strip_ansi(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c != '\u{1b}' {
            if c != '\u{7}' {
                out.push(c);
            }
            continue;
        }
        match chars.peek() {
            Some('[') => {
                chars.next();
                // CSI: parameters then a final byte in 0x40..=0x7e
                for c in chars.by_ref() {
                    if ('\u{40}'..='\u{7e}').contains(&c) {
                        break;
                    }
                }
            }
            Some(']') => {
                chars.next();
                // OSC: terminated by BEL or ESC \
                while let Some(c) = chars.next() {
                    if c == '\u{7}' {
                        break;
                    }
                    if c == '\u{1b}' {
                        chars.next();
                        break;
                    }
                }
            }
            Some(_) => {
                chars.next();
            }
            None => {}
        }
    }
    out
}

/// Spawn `argv` in its own process group and forward its output to `sink`.
///
/// Returns the pid. `Started` is emitted before any output, `Exit` after all of it.
pub fn spawn_streaming(
    id: u64,
    program: &str,
    args: &[String],
    env: &[(String, String)],
    cwd: Option<&str>,
    sink: Arc<dyn Fn(Event) + Send + Sync>,
) -> std::io::Result<u32> {
    let mut cmd = Command::new(program);
    cmd.args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    for (k, v) in env {
        cmd.env(k, v);
    }
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }
    let mut child = cmd.spawn()?;
    let pid = child.id();
    sink(Event::Started { id, pid });

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let mut readers = Vec::new();
    if let Some(out) = stdout {
        let sink = sink.clone();
        readers.push(std::thread::spawn(move || pump(id, StreamKind::Stdout, out, sink)));
    }
    if let Some(err) = stderr {
        let sink = sink.clone();
        readers.push(std::thread::spawn(move || pump(id, StreamKind::Stderr, err, sink)));
    }
    std::thread::spawn(move || {
        for r in readers {
            let _ = r.join();
        }
        let (code, signal) = match child.wait() {
            Ok(st) => (st.code(), st.signal()),
            Err(_) => (None, None),
        };
        sink(Event::Exit { id, code, signal });
    });
    Ok(pid)
}

fn pump(id: u64, stream: StreamKind, mut src: impl Read, sink: Arc<dyn Fn(Event) + Send + Sync>) {
    let mut buf = [0u8; 8192];
    let mut line: Vec<u8> = Vec::with_capacity(256);
    loop {
        let n = match src.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => n,
        };
        for &b in &buf[..n] {
            match b {
                b'\n' => {
                    let text = strip_ansi(&String::from_utf8_lossy(&line));
                    sink(Event::Line { id, stream, text });
                    line.clear();
                }
                b'\r' => {
                    if !line.is_empty() {
                        let text = strip_ansi(&String::from_utf8_lossy(&line));
                        if !text.trim().is_empty() {
                            sink(Event::Partial { id, stream, text });
                        }
                        line.clear();
                    }
                }
                _ => line.push(b),
            }
        }
    }
    if !line.is_empty() {
        let text = strip_ansi(&String::from_utf8_lossy(&line));
        sink(Event::Line { id, stream, text });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ansi() {
        assert_eq!(strip_ansi("\u{1b}[32;01m>>>\u{1b}[39;49;00m done"), ">>> done");
        assert_eq!(strip_ansi("\u{1b}]0;title\u{7}text"), "text");
        assert_eq!(strip_ansi("plain"), "plain");
    }

    #[test]
    fn streams_lines() {
        let got = Arc::new(std::sync::Mutex::new(Vec::new()));
        let g = got.clone();
        let (tx, rx) = std::sync::mpsc::channel();
        let sink: Arc<dyn Fn(Event) + Send + Sync> = Arc::new(move |e| {
            if matches!(e, Event::Exit { .. }) {
                let _ = tx.send(());
            }
            g.lock().unwrap().push(e);
        });
        spawn_streaming(1, "/bin/sh", &["-c".into(), "printf 'a\\nb\\rc\\n'; exit 3".into()], &[], None, sink).unwrap();
        rx.recv_timeout(std::time::Duration::from_secs(5)).unwrap();
        let ev = got.lock().unwrap();
        let lines: Vec<_> = ev
            .iter()
            .filter_map(|e| match e {
                Event::Line { text, .. } => Some(text.clone()),
                _ => None,
            })
            .collect();
        assert_eq!(lines, vec!["a", "c"]);
        assert!(ev.iter().any(|e| matches!(e, Event::Exit { code: Some(3), .. })));
    }
}
