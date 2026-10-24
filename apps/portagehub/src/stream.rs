//! Spawning a process and streaming its stdout/stderr line by line.

use crate::protocol::{Event, MAX_OUTPUT_LINE, StreamKind};
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
    // A GUI started from a background job (or by a launcher) can have SIGINT/SIGQUIT ignored, and
    // ignored signals stay ignored across exec: the Stop button (SIGINT, like Ctrl+C) would then do
    // nothing to emerge. Programs start with the default dispositions.
    unsafe {
        cmd.pre_exec(|| {
            libc::signal(libc::SIGINT, libc::SIG_DFL);
            libc::signal(libc::SIGQUIT, libc::SIG_DFL);
            Ok(())
        });
    }
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

/// How much of `line` can be sent now without cutting a UTF-8 sequence in two.
fn complete_prefix(line: &[u8]) -> usize {
    let mut cut = line.len();
    // At most the last 3 bytes can be the start of a sequence that continues in the next chunk.
    for back in 1..=line.len().min(3) {
        let b = line[line.len() - back];
        if b & 0xC0 == 0x80 {
            continue; // a continuation byte: keep looking for its lead byte
        }
        let need = if b >= 0xF0 { 4 } else if b >= 0xE0 { 3 } else if b >= 0xC0 { 2 } else { 1 };
        if need > back {
            cut = line.len() - back;
        }
        break;
    }
    cut
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
                _ => {
                    line.push(b);
                    // A program that prints megabytes without a newline must not grow this buffer
                    // (or the event that carries it) without limit: long lines are split.
                    if line.len() >= MAX_OUTPUT_LINE {
                        let cut = complete_prefix(&line);
                        let text = strip_ansi(&String::from_utf8_lossy(&line[..cut]));
                        sink(Event::Line { id, stream, text });
                        line.drain(..cut);
                    }
                }
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
    fn utf8_sequences_are_never_cut_by_a_split() {
        assert_eq!(complete_prefix(b"abc"), 3);
        assert_eq!(complete_prefix("abé".as_bytes()), 4);
        assert_eq!(complete_prefix(&"abé".as_bytes()[..3]), 2, "the lead byte of é without its continuation waits");
        assert_eq!(complete_prefix(&"€".as_bytes()[..1]), 0);
        assert_eq!(complete_prefix(&"€".as_bytes()[..2]), 0);
        assert_eq!(complete_prefix("€".as_bytes()), 3);
        assert_eq!(complete_prefix(&"😀".as_bytes()[..3]), 0);
        assert_eq!(complete_prefix(b""), 0);
    }

    fn collect_pump(input: &[u8]) -> Vec<Event> {
        let got = Arc::new(std::sync::Mutex::new(Vec::new()));
        let g = got.clone();
        pump(7, StreamKind::Stdout, input, Arc::new(move |e| g.lock().unwrap().push(e)));
        let v = got.lock().unwrap().clone();
        v
    }

    #[test]
    fn a_line_without_a_newline_is_split_instead_of_growing_forever() {
        let input = vec![b'x'; 200 * 1024];
        let lines: Vec<String> = collect_pump(&input).into_iter().filter_map(|e| if let Event::Line { text, .. } = e { Some(text) } else { None }).collect();
        assert!(lines.len() >= 4, "{} lines", lines.len());
        assert!(lines.iter().all(|l| l.len() <= MAX_OUTPUT_LINE));
        assert_eq!(lines.iter().map(String::len).sum::<usize>(), 200 * 1024, "nothing is lost");
    }

    #[test]
    fn splitting_keeps_multibyte_characters_whole() {
        let mut input = vec![b'x'; MAX_OUTPUT_LINE - 1];
        input.extend("é".bytes());
        input.extend(b"tail\n");
        let lines: Vec<String> = collect_pump(&input).into_iter().filter_map(|e| if let Event::Line { text, .. } = e { Some(text) } else { None }).collect();
        let joined: String = lines.concat();
        assert!(!joined.contains('\u{fffd}'), "no replacement characters");
        assert!(joined.ends_with("étail"));
    }

    #[test]
    fn programs_start_with_sigint_enabled_even_if_the_parent_ignores_it() {
        const CHILD: &str = "PORTAGEHUB_SIGINT_PROBE_CHILD";
        let me = "stream::tests::programs_start_with_sigint_enabled_even_if_the_parent_ignores_it";
        if std::env::var_os(CHILD).is_none() {
            // Run this test again in a copy of the test binary that starts with SIGINT ignored, as
            // a GUI started from a background job would. The signal state of this process (which
            // the other tests share) is never touched.
            let mut cmd = Command::new(std::env::current_exe().unwrap());
            cmd.args(["--exact", me, "--test-threads=1"]).env(CHILD, "1");
            unsafe {
                cmd.pre_exec(|| {
                    libc::signal(libc::SIGINT, libc::SIG_IGN);
                    Ok(())
                });
            }
            let out = cmd.output().unwrap();
            assert!(out.status.success(), "{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
            return;
        }
        // SigIgn in /proc/<pid>/status is a mask: bit 1 (value 2) is SIGINT.
        let ignored = |status: &str| {
            let line = status.lines().find(|l| l.starts_with("SigIgn")).expect("SigIgn line").to_string();
            (u64::from_str_radix(line.split_whitespace().last().unwrap(), 16).unwrap(), line)
        };
        let own = std::fs::read_to_string("/proc/self/status").unwrap();
        assert_ne!(ignored(&own).0 & 0b10, 0, "the probe must start with SIGINT ignored: {}", ignored(&own).1);

        let got = Arc::new(std::sync::Mutex::new(Vec::new()));
        let (tx, rx) = std::sync::mpsc::channel();
        let g = got.clone();
        let sink: Arc<dyn Fn(Event) + Send + Sync> = Arc::new(move |e| {
            if matches!(e, Event::Exit { .. }) {
                let _ = tx.send(());
            }
            g.lock().unwrap().push(e);
        });
        spawn_streaming(1, "/bin/sh", &["-c".into(), "grep '^SigIgn' /proc/self/status".into()], &[], None, sink).unwrap();
        rx.recv_timeout(std::time::Duration::from_secs(5)).unwrap();
        let ev = got.lock().unwrap();
        let line = ev.iter().find_map(|e| if let Event::Line { text, .. } = e { Some(text.clone()) } else { None }).expect("SigIgn line");
        let (mask, _) = ignored(&line);
        assert_eq!(mask & 0b11, 0, "SIGHUP and SIGINT must not be ignored in the child: {line}");
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
