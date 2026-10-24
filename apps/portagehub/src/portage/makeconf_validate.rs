//! Checking a make.conf before (and after) it is written.
//!
//! Portage reads make.conf with its own shlex-based parser, not bash, and a
//! syntax error is easy to miss: `portageq`/`emerge` print a warning and fall
//! back to the profile defaults for everything in the file. So every
//! make.conf write is checked here first:
//! 1. `precheck`: plain-language explanations of the usual mistakes;
//! 2. `strict_parse`: Portage's own `getconfig(tolerant=False)` as the authority;
//! 3. `preview`: the effective values the candidate would give, from the real
//!    Portage with a temporary PORTAGE_CONFIGROOT that uses the real profile.

use super::makeconf::MAKE_CONF;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Mistakes Portage's parser rejects (or silently mangles), with line numbers.
pub fn precheck(text: &str) -> Vec<(usize, String)> {
    let mut out = Vec::new();
    if text.contains("\r\n") {
        out.push((1, "Windows line endings (CRLF): save the file with Unix line endings".to_string()));
    }
    let mut quote: Option<char> = None;
    let mut quote_line = 0;
    let mut continued = false;
    for (n, raw) in text.lines().enumerate() {
        let line_no = n + 1;
        let line = raw.trim_end_matches('\r');
        let starts_inside = quote.is_some() || continued;
        let t = line.trim_start();
        if !starts_inside {
            if t.is_empty() || t.starts_with('#') {
                continue;
            }
            if let Some(rest) = t.strip_prefix("source ").or_else(|| t.strip_prefix(". ")) {
                if !rest.trim().trim_matches('"').starts_with('/') {
                    out.push((line_no, "`source` with a relative path: use an absolute path".to_string()));
                }
                continue;
            }
            let body = t.strip_prefix("export ").unwrap_or(t);
            let Some(eq) = body.find('=') else {
                out.push((line_no, format!("not an assignment: `{}` (expected NAME=\"value\")", t.chars().take(40).collect::<String>())));
                continue;
            };
            let name = &body[..eq];
            if name.is_empty() || !name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') || name.starts_with(|c: char| c.is_ascii_digit()) {
                out.push((line_no, format!("invalid variable name `{name}`: only letters, digits and _")));
                continue;
            }
        }
        // Walk the characters, tracking quotes, to find unquoted spaces and
        // shell features Portage does not support.
        let start = if starts_inside { 0 } else { line.find('=').map(|i| i + 1).unwrap_or(0) };
        let chars: Vec<char> = line.chars().collect();
        let mut i = line[..start].chars().count();
        let mut value_ended = false;
        continued = false;
        while i < chars.len() {
            let c = chars[i];
            let next = chars.get(i + 1).copied();
            if quote != Some('\'') {
                if c == '$' && next == Some('(') {
                    out.push((line_no, "command substitution $(…) is not supported in make.conf".to_string()));
                    break;
                }
                if c == '`' {
                    out.push((line_no, "backticks are not supported in make.conf".to_string()));
                    break;
                }
                if c == '$' && next == Some('{') {
                    let rest: String = chars[i + 2..].iter().take_while(|&&c| c != '}').collect();
                    if rest.contains([':', '#', '%', '/']) || rest.starts_with('!') {
                        out.push((line_no, format!("${{{rest}}}: only plain ${{NAME}} expansion is supported (no defaults or substitutions)")));
                        break;
                    }
                }
                if c == '\\' && next.is_none() {
                    continued = true;
                    break;
                }
                if c == '\\' {
                    i += 2;
                    continue;
                }
            }
            match (quote, c) {
                (None, '"') | (None, '\'') => {
                    quote = Some(c);
                    quote_line = line_no;
                }
                (Some(q), c) if c == q => quote = None,
                (None, c) if c.is_whitespace() => value_ended = true,
                (None, '#') if value_ended => break,
                (None, _) if value_ended => {
                    out.push((line_no, "unquoted space in a value: Portage keeps only the first word — put the value in double quotes".to_string()));
                    break;
                }
                _ => {}
            }
            i += 1;
        }
    }
    if quote.is_some() {
        out.push((quote_line, "a quote opened here is never closed".to_string()));
    }
    out
}

#[derive(Debug, Clone, PartialEq)]
pub enum ParseCheck {
    /// Parsed; the expanded variables.
    Ok(BTreeMap<String, String>),
    /// Portage rejects it.
    Error(String),
    /// Portage's Python modules could not be loaded (not a Gentoo system?).
    Unavailable(String),
}

const PARSE_SCRIPT: &str = r#"
import json, sys
try:
    from portage.util import getconfig
except Exception as e:
    print("UNAVAILABLE " + str(e)); sys.exit(3)
try:
    d = getconfig(sys.argv[1], tolerant=False, allow_sourcing=True, expand=True, recursive=True)
except Exception as e:
    print("PARSE " + str(e).replace("\n", " ")); sys.exit(2)
print("OK " + json.dumps(d or {}))
"#;

/// Parse a make.conf file or directory with Portage's strict parser.
pub fn strict_parse(path: &Path) -> ParseCheck {
    let out = match Command::new("python3").arg("-I").arg("-c").arg(PARSE_SCRIPT).arg(path).output() {
        Ok(o) => o,
        Err(e) => return ParseCheck::Unavailable(format!("python3: {e}")),
    };
    let text = String::from_utf8_lossy(&out.stdout);
    let line = text.lines().last().unwrap_or("");
    if let Some(json) = line.strip_prefix("OK ") {
        return ParseCheck::Ok(serde_json::from_str(json).unwrap_or_default());
    }
    if let Some(msg) = line.strip_prefix("PARSE ") {
        // Portage names the temporary file; the user knows it as make.conf.
        let msg = msg.split("\", ").last().unwrap_or(msg);
        return ParseCheck::Error(msg.to_string());
    }
    ParseCheck::Unavailable(line.strip_prefix("UNAVAILABLE ").unwrap_or(&String::from_utf8_lossy(&out.stderr)).trim().to_string())
}

/// A private scratch directory, removed on drop.
struct Scratch(PathBuf);

impl Scratch {
    fn new(tag: &str) -> std::io::Result<Scratch> {
        use std::os::unix::fs::DirBuilderExt;
        let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
        let p = std::env::temp_dir().join(format!("portagehub-{tag}-{}-{nanos}", std::process::id()));
        std::fs::DirBuilder::new().mode(0o700).create(&p)?;
        Ok(Scratch(p))
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// Write the candidate make.conf (single file, or the whole directory with
/// changed files replaced) below `dir`; returns its path.
fn materialize(dir: &Path, changed: &[(PathBuf, String)]) -> std::io::Result<PathBuf> {
    let target = dir.join("make.conf");
    let real = Path::new(MAKE_CONF);
    if real.is_dir() {
        std::fs::create_dir_all(&target)?;
        for e in std::fs::read_dir(real)?.flatten() {
            if e.path().is_file() {
                std::fs::copy(e.path(), target.join(e.file_name()))?;
            }
        }
        for (p, content) in changed {
            if let Some(name) = p.file_name() {
                std::fs::write(target.join(name), content)?;
            }
        }
    } else {
        let content = changed.iter().find(|(p, _)| p == real).map(|(_, c)| c.clone()).unwrap_or_else(|| std::fs::read_to_string(real).unwrap_or_default());
        std::fs::write(&target, content)?;
    }
    Ok(target)
}

/// Check candidate make.conf contents (paths are the real ones) without writing them.
pub fn check_candidate(changed: &[(PathBuf, String)]) -> ParseCheck {
    for (p, content) in changed {
        if let Some((line, msg)) = precheck(content).into_iter().next() {
            let name = if p == Path::new(MAKE_CONF) { "make.conf".to_string() } else { p.display().to_string() };
            return ParseCheck::Error(format!("{name}, line {line}: {msg}"));
        }
    }
    let scratch = match Scratch::new("check") {
        Ok(s) => s,
        Err(e) => return ParseCheck::Unavailable(e.to_string()),
    };
    match materialize(&scratch.0, changed) {
        Ok(path) => strict_parse(&path),
        Err(e) => ParseCheck::Unavailable(e.to_string()),
    }
}

/// Parse `portageq envvar -v` output (`NAME='value'` or `NAME=value`).
pub fn parse_envvar(text: &str) -> BTreeMap<String, String> {
    let mut m = BTreeMap::new();
    for line in text.lines() {
        if let Some((k, v)) = line.split_once('=')
            && !k.is_empty()
            && k.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
        {
            let v = v.strip_prefix('\'').and_then(|v| v.strip_suffix('\'')).map(|v| v.replace("'\\''", "'")).unwrap_or_else(|| v.to_string());
            m.insert(k.to_string(), v);
        }
    }
    m
}

fn envvar(configroot: Option<&Path>, vars: &[&str]) -> Result<BTreeMap<String, String>, String> {
    let mut cmd = Command::new("portageq");
    cmd.arg("envvar").arg("-v").args(vars);
    if let Some(r) = configroot {
        cmd.env("PORTAGE_CONFIGROOT", r);
    }
    let out = cmd.output().map_err(|e| format!("portageq: {e}"))?;
    let err = String::from_utf8_lossy(&out.stderr);
    // A parse error is only a warning to portageq; treat it as failure.
    if let Some(l) = err.lines().find(|l| l.contains("make.conf") && (l.contains("line") || l.contains("Error"))) {
        return Err(l.trim().to_string());
    }
    // portageq exits 1 when a variable is unset; only stderr means trouble.
    if !out.status.success() && !err.trim().is_empty() {
        return Err(err.trim().to_string());
    }
    Ok(parse_envvar(&String::from_utf8_lossy(&out.stdout)))
}

/// Effective values on the real system.
pub fn effective(vars: &[&str]) -> Result<BTreeMap<String, String>, String> {
    envvar(None, vars)
}

/// Effective values the candidate make.conf would give, computed by Portage
/// with a temporary config root that shares the real profile and repos.conf.
pub fn preview(changed: &[(PathBuf, String)], vars: &[&str]) -> Result<BTreeMap<String, String>, String> {
    let scratch = Scratch::new("preview").map_err(|e| e.to_string())?;
    let etc = scratch.0.join("etc/portage");
    std::fs::create_dir_all(&etc).map_err(|e| e.to_string())?;
    materialize(&etc, changed).map_err(|e| e.to_string())?;
    let profile = std::fs::canonicalize("/etc/portage/make.profile").map_err(|e| format!("make.profile: {e}"))?;
    std::os::unix::fs::symlink(profile, etc.join("make.profile")).map_err(|e| e.to_string())?;
    if Path::new("/etc/portage/repos.conf").exists() {
        std::os::unix::fs::symlink("/etc/portage/repos.conf", etc.join("repos.conf")).map_err(|e| e.to_string())?;
    }
    envvar(Some(&scratch.0), vars)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn first(t: &str) -> Option<(usize, String)> {
        precheck(t).into_iter().next()
    }

    #[test]
    fn prechecks_explain_the_usual_mistakes() {
        assert_eq!(first("A=\"x y\"\nB='c d' # note\nexport C=1\n# x y z\n\nD=\"multi \\\n  line\"\n"), None);
        assert!(first("MAKEOPTS=-j8 -l8\n").unwrap().1.contains("unquoted space"));
        assert_eq!(first("A=1\nMAKEOPTS=-j8 -l8\n").unwrap().0, 2);
        assert_eq!(first("A=1 # comment\n"), None);
        assert!(first("A=$(nproc)\n").unwrap().1.contains("command substitution"));
        assert!(first("A=`nproc`\n").unwrap().1.contains("backticks"));
        assert!(first("A=\"${B:-x}\"\n").unwrap().1.contains("only plain"));
        assert_eq!(first("A=\"${B} $C\"\n"), None);
        assert!(first("MY-VAR=1\n").unwrap().1.contains("invalid variable name"));
        assert!(first("hello\n").unwrap().1.contains("not an assignment"));
        assert_eq!(first("A=\"open\nB=2\n").unwrap(), (1, "a quote opened here is never closed".to_string()));
        assert!(first("A=1\r\n").unwrap().1.contains("CRLF"));
        assert!(first("source make.conf.lto\n").unwrap().1.contains("relative"));
        assert_eq!(first("source /etc/portage/make.conf.lto\n"), None);
        assert_eq!(first("A='$(literal)'\n"), None, "single quotes are literal");
    }

    #[test]
    fn envvar_output() {
        let m = parse_envvar("CFLAGS='-O2 -pipe'\nVIDEO_CARDS=nvidia\nX='it'\\''s'\n");
        assert_eq!(m["CFLAGS"], "-O2 -pipe");
        assert_eq!(m["VIDEO_CARDS"], "nvidia");
        assert_eq!(m["X"], "it's");
    }

    #[test]
    fn portage_parser_is_the_authority() {
        let dir = Scratch::new("test").unwrap();
        let p = dir.0.join("make.conf");
        std::fs::write(&p, "COMMON_FLAGS=\"-O2 -pipe\"\nCFLAGS=\"${COMMON_FLAGS}\"\n").unwrap();
        match strict_parse(&p) {
            ParseCheck::Unavailable(why) => eprintln!("Portage not available, skipping: {why}"),
            ParseCheck::Ok(m) => {
                assert_eq!(m["CFLAGS"], "-O2 -pipe");
                std::fs::write(&p, "BAD=a b\n").unwrap();
                assert!(matches!(strict_parse(&p), ParseCheck::Error(_)));
            }
            ParseCheck::Error(e) => panic!("valid file rejected: {e}"),
        }
    }
}
