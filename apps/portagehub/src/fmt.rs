//! Small formatting helpers.

use std::time::{SystemTime, UNIX_EPOCH};

pub fn now_ts() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

/// "3 days ago" style relative time for a unix timestamp.
pub fn ago(ts: i64) -> String {
    if ts <= 0 {
        return "never".into();
    }
    let d = now_ts() - ts;
    if d < 0 {
        return "just now".into();
    }
    let (n, unit) = if d < 60 {
        return "just now".into();
    } else if d < 3600 {
        (d / 60, "minute")
    } else if d < 86_400 {
        (d / 3600, "hour")
    } else if d < 86_400 * 30 {
        (d / 86_400, "day")
    } else if d < 86_400 * 365 {
        (d / (86_400 * 30), "month")
    } else {
        (d / (86_400 * 365), "year")
    };
    format!("{n} {unit}{} ago", if n == 1 { "" } else { "s" })
}

pub fn ago_system(t: SystemTime) -> String {
    ago(t.duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0))
}

pub fn date(ts: i64) -> String {
    use chrono::{Local, TimeZone};
    match Local.timestamp_opt(ts, 0) {
        chrono::LocalResult::Single(t) => t.format("%Y-%m-%d %H:%M").to_string(),
        _ => "?".into(),
    }
}

pub fn size(bytes: u64) -> String {
    let b = bytes as f64;
    if b >= 1024.0 * 1024.0 * 1024.0 {
        format!("{:.1} GiB", b / (1024.0 * 1024.0 * 1024.0))
    } else if b >= 1024.0 * 1024.0 {
        format!("{:.1} MiB", b / (1024.0 * 1024.0))
    } else if b >= 1024.0 {
        format!("{:.0} KiB", b / 1024.0)
    } else {
        format!("{bytes} B")
    }
}

/// Minimal LCS line diff: ('+'|'-'|' ', line).
pub fn line_diff(old: &str, new: &str) -> Vec<(char, String)> {
    let a: Vec<&str> = old.lines().collect();
    let b: Vec<&str> = new.lines().collect();
    if a.len() * b.len() > 4_000_000 {
        let mut out: Vec<(char, String)> = a.iter().map(|l| ('-', l.to_string())).collect();
        out.extend(b.iter().map(|l| ('+', l.to_string())));
        return out;
    }
    let mut dp = vec![vec![0u32; b.len() + 1]; a.len() + 1];
    for i in (0..a.len()).rev() {
        for j in (0..b.len()).rev() {
            dp[i][j] = if a[i] == b[j] { dp[i + 1][j + 1] + 1 } else { dp[i + 1][j].max(dp[i][j + 1]) };
        }
    }
    let (mut i, mut j) = (0, 0);
    let mut out = Vec::new();
    while i < a.len() && j < b.len() {
        if a[i] == b[j] {
            out.push((' ', a[i].to_string()));
            i += 1;
            j += 1;
        } else if dp[i + 1][j] >= dp[i][j + 1] {
            out.push(('-', a[i].to_string()));
            i += 1;
        } else {
            out.push(('+', b[j].to_string()));
            j += 1;
        }
    }
    out.extend(a[i..].iter().map(|l| ('-', l.to_string())));
    out.extend(b[j..].iter().map(|l| ('+', l.to_string())));
    // Collapse long unchanged runs to keep dialogs short.
    let mut collapsed = Vec::new();
    let mut run: Vec<(char, String)> = Vec::new();
    let flush = |run: &mut Vec<(char, String)>, collapsed: &mut Vec<(char, String)>| {
        if run.len() > 6 {
            collapsed.extend(run.drain(..2));
            let hidden = run.len() - 2;
            collapsed.push((' ', format!("… {hidden} unchanged lines …")));
            collapsed.extend(run.drain(run.len() - 2..));
            run.clear();
        } else {
            collapsed.append(run);
        }
    };
    for d in out {
        if d.0 == ' ' {
            run.push(d);
        } else {
            flush(&mut run, &mut collapsed);
            collapsed.push(d);
        }
    }
    flush(&mut run, &mut collapsed);
    collapsed
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn diff() {
        let d = line_diff("a\nb\nc\n", "a\nx\nc\n");
        assert_eq!(d, vec![(' ', "a".into()), ('-', "b".into()), ('+', "x".into()), (' ', "c".into())]);
    }
}
