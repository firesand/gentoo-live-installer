//! `kernelsmith run-job <script>` — the part that runs as root under pkexec.
//!
//! It runs a private copy of the script (so it can't be edited after the user
//! authenticated), puts it in its own process group, forwards and logs its
//! output, and cancels the whole group when asked on stdin.

use kernelsmith_core::util::is_root;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};

fn private_copy(script: &Path) -> std::io::Result<PathBuf> {
    let dir = Path::new("/run/kernelsmith");
    std::fs::create_dir_all(dir)?;
    std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
    let dest = dir.join(format!("job-{}.sh", std::process::id()));
    let body = std::fs::read(script)?;
    let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o700).open(&dest)?;
    f.write_all(&body)?;
    Ok(dest)
}

fn open_log(log: &Path) -> Option<std::fs::File> {
    let f = std::fs::OpenOptions::new().create(true).append(true).mode(0o644).open(log).ok()?;
    // Hand the log to whoever owns the job directory, not root.
    if is_root() {
        if let Some(meta) = log.parent().and_then(|d| std::fs::metadata(d).ok()) {
            if let Ok(c) = std::ffi::CString::new(log.as_os_str().as_encoded_bytes()) {
                unsafe { libc::chown(c.as_ptr(), meta.uid(), meta.gid()) };
            }
        }
    }
    Some(f)
}

pub fn run_job(script: &Path, log: Option<&Path>, cancel_on_eof: bool) -> i32 {
    if !script.is_file() {
        eprintln!("::error::Script {} not found", script.display());
        return 2;
    }
    let to_run = if is_root() {
        match private_copy(script) {
            Ok(p) => p,
            Err(e) => {
                println!("::error::Could not stage the script: {e}");
                return 2;
            }
        }
    } else {
        script.to_path_buf()
    };

    let mut child = match Command::new("/bin/bash")
        .arg(&to_run)
        .current_dir("/")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            println!("::error::Could not start bash: {e}");
            return 2;
        }
    };
    let pgid = child.id() as i32;

    // Cancellation: "cancel" on stdin, or stdin closing when the GUI goes away.
    std::thread::spawn(move || {
        let stdin = std::io::stdin();
        let mut line = String::new();
        loop {
            line.clear();
            match stdin.lock().read_line(&mut line) {
                Ok(0) if !cancel_on_eof => return,
                Ok(0) => break,
                Ok(_) if line.trim() == "cancel" => break,
                Ok(_) => continue,
                Err(_) => return,
            }
        }
        unsafe { libc::kill(-pgid, libc::SIGTERM) };
        std::thread::sleep(std::time::Duration::from_secs(10));
        unsafe { libc::kill(-pgid, libc::SIGKILL) };
    });

    let log = Arc::new(Mutex::new(log.and_then(open_log)));
    let mut threads = vec![];
    for stream in [child.stdout.take().map(|s| Box::new(s) as Box<dyn std::io::Read + Send>), child.stderr.take().map(|s| Box::new(s) as Box<dyn std::io::Read + Send>)]
        .into_iter()
        .flatten()
    {
        let log = Arc::clone(&log);
        threads.push(std::thread::spawn(move || {
            for line in BufReader::new(stream).lines().map_while(Result::ok) {
                let mut out = std::io::stdout().lock();
                let _ = writeln!(out, "{line}");
                let _ = out.flush();
                if let Ok(mut guard) = log.lock() {
                    if let Some(f) = guard.as_mut() {
                        let _ = writeln!(f, "{line}");
                    }
                }
            }
        }));
    }
    let status = child.wait();
    for t in threads {
        let _ = t.join();
    }
    if to_run != script {
        let _ = std::fs::remove_file(&to_run);
    }
    match status {
        Ok(s) => s.code().unwrap_or(130),
        Err(_) => 1,
    }
}
