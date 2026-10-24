//! Privileged operations with arguments fixed or validated here, never taken
//! verbatim from the GUI: boot scan, bootloader updates, known-good kernel
//! marking, kernel leftover removal and build-dir cleanup.
//!
//! Every function takes the filesystem `root` ("/" in production, a scratch
//! tree in tests); paths in messages and results stay the logical ones.
//! Writes and deletions go through [`Beneath`], so symlinks are never followed.

use crate::protocol::{BOOT_MOUNTPOINTS, BootAction, BootScan, KERNEL_STATE_DIRS, ListEntry, Listing, PROGRAM_DIRS, ScannedFile, valid_kver};
use crate::safefs::Beneath;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

/// `root` joined with an absolute logical path.
pub fn at(root: &Path, abs: impl AsRef<Path>) -> PathBuf {
    let abs = abs.as_ref();
    root.join(abs.strip_prefix("/").unwrap_or(abs))
}

fn is_real_root(root: &Path) -> bool {
    root == Path::new("/")
}

const MAX_FILE: u64 = 1 << 20;
const MAX_TOTAL: usize = 4 << 20;

const TEXT_FILES: &[&str] = &[
    "/boot/grub/grub.cfg",
    "/boot/grub2/grub.cfg",
    "/boot/grub/grubenv",
    "/boot/grub2/grubenv",
    "/etc/default/grub",
    "/etc/lilo.conf",
    "/boot/loader/loader.conf",
    "/boot/efi/loader/loader.conf",
    "/efi/loader/loader.conf",
    "/boot/refind_linux.conf",
    "/boot/efi/EFI/refind/refind.conf",
    "/efi/EFI/refind/refind.conf",
    "/boot/limine.conf",
    "/boot/limine/limine.conf",
    "/boot/efi/limine.conf",
];

const ENTRY_DIRS: &[&str] = &["/boot/loader/entries", "/boot/efi/loader/entries", "/efi/loader/entries"];
const EFI_DIRS: &[&str] = &["/boot/efi/EFI", "/efi/EFI", "/boot/EFI"];
const LIST_DIRS: &[&str] = &["/boot", "/boot/kernel-backup", "/boot/efi/EFI/Linux", "/efi/EFI/Linux", "/boot/EFI/Linux", "/boot/efi/EFI", "/efi/EFI"];

fn mtime(p: &Path) -> i64 {
    std::fs::metadata(p).and_then(|m| m.modified()).ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map(|d| d.as_secs() as i64).unwrap_or(0)
}

fn list(root: &Path, dir: &Path) -> Option<Listing> {
    let rd = std::fs::read_dir(at(root, dir)).ok()?;
    let mut entries: Vec<ListEntry> = rd
        .flatten()
        .map(|e| {
            let md = e.metadata().ok();
            ListEntry {
                name: e.file_name().to_string_lossy().to_string(),
                size: md.as_ref().map(|m| m.len()).unwrap_or(0),
                mtime: md.and_then(|m| m.modified().ok()).and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map(|d| d.as_secs() as i64).unwrap_or(0),
                is_dir: e.file_type().map(|t| t.is_dir()).unwrap_or(false),
            }
        })
        .collect();
    entries.sort_by(|a, b| a.name.cmp(&b.name));
    Some(Listing { dir: dir.to_string_lossy().to_string(), entries })
}

/// Read-only: bootloader configuration files and boot directory listings.
pub fn boot_scan(root: &Path) -> BootScan {
    let mut scan = BootScan { root: unsafe { libc::geteuid() } == 0, ..Default::default() };
    let mut total = 0usize;
    let mut paths: Vec<PathBuf> = TEXT_FILES.iter().map(PathBuf::from).collect();
    for d in ENTRY_DIRS {
        for e in std::fs::read_dir(at(root, d)).into_iter().flatten().flatten() {
            if e.path().extension().is_some_and(|x| x == "conf") {
                paths.push(Path::new(d).join(e.file_name()));
            }
        }
    }
    for d in EFI_DIRS {
        for e in std::fs::read_dir(at(root, d)).into_iter().flatten().flatten() {
            let g = Path::new(d).join(e.file_name()).join("grub.cfg");
            if at(root, &g).is_file() {
                paths.push(g);
            }
        }
    }
    for p in paths {
        let real = at(root, &p);
        let md = match std::fs::metadata(&real) {
            Ok(m) => m,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => {
                scan.errors.push(format!("{}: {e}", p.display()));
                continue;
            }
        };
        if !md.is_file() {
            continue;
        }
        if md.len() > MAX_FILE || total > MAX_TOTAL {
            scan.errors.push(format!("{} skipped (too large)", p.display()));
            continue;
        }
        match std::fs::read(&real) {
            Ok(b) => {
                total += b.len();
                scan.files.push(ScannedFile { path: p.to_string_lossy().to_string(), content: String::from_utf8_lossy(&b).to_string(), mtime: mtime(&real) });
            }
            Err(e) => scan.errors.push(format!("{}: {e}", p.display())),
        }
    }
    let mut dirs: Vec<PathBuf> = LIST_DIRS.iter().map(PathBuf::from).collect();
    for e in std::fs::read_dir(at(root, "/boot/kernel-backup")).into_iter().flatten().flatten() {
        if e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            dirs.push(Path::new("/boot/kernel-backup").join(e.file_name()));
        }
    }
    for d in dirs {
        if let Some(l) = list(root, &d) {
            scan.listings.push(l);
        }
    }
    scan
}

fn find_program(names: &[&str]) -> Option<String> {
    for n in names {
        for d in PROGRAM_DIRS {
            let p = Path::new(d).join(n);
            if p.is_file() {
                return Some(p.to_string_lossy().to_string());
            }
        }
    }
    None
}

/// Program and arguments for a bootloader/initramfs action.
pub fn boot_action_argv(root: &Path, action: BootAction, kver: Option<&str>) -> Result<(String, Vec<String>), String> {
    match action {
        BootAction::GrubMkconfig => {
            let prog = find_program(&["grub-mkconfig", "grub2-mkconfig"]).ok_or("grub-mkconfig is not installed")?;
            let target = ["/boot/grub/grub.cfg", "/boot/grub2/grub.cfg"]
                .iter()
                .find(|p| at(root, p).is_file())
                .or_else(|| ["/boot/grub/grub.cfg", "/boot/grub2/grub.cfg"].iter().find(|p| at(root, p).parent().is_some_and(|d| d.is_dir())))
                .ok_or("no /boot/grub directory")?;
            Ok((prog, vec!["-o".into(), target.to_string()]))
        }
        BootAction::Lilo => Ok((find_program(&["lilo"]).ok_or("lilo is not installed")?, vec![])),
        BootAction::BootctlUpdate => Ok((find_program(&["bootctl"]).ok_or("bootctl is not installed")?, vec!["update".into()])),
        BootAction::DracutRegen => {
            let k = kver.ok_or("kernel version required")?;
            if !valid_kver(k) || !at(root, "/lib/modules").join(k).is_dir() {
                return Err(format!("unknown kernel {k}"));
            }
            Ok((find_program(&["dracut"]).ok_or("dracut is not installed")?, vec!["--force".into(), "--kver".into(), k.to_string()]))
        }
    }
}

fn read_trim(p: &Path) -> String {
    std::fs::read_to_string(p).map(|s| s.trim().to_string()).unwrap_or_default()
}

fn boot_image_exists(root: &Path, kver: &str) -> bool {
    ["vmlinuz", "kernel", "bzImage"].iter().any(|p| at(root, "/boot").join(format!("{p}-{kver}")).is_file())
}

fn io_err(what: impl std::fmt::Display) -> impl Fn(std::io::Error) -> String {
    move |e| format!("{what}: {e}")
}

/// Record a known-good kernel, rotating last-good into previous-good.
pub fn mark_good(root: &Path, kver: &str, state_dir: &str, backup: bool, log: &dyn Fn(String)) -> Result<(), String> {
    if !valid_kver(kver) {
        return Err(format!("invalid kernel version {kver}"));
    }
    if !KERNEL_STATE_DIRS.contains(&state_dir) {
        return Err(format!("state directory not allowed: {state_dir}"));
    }
    if !boot_image_exists(root, kver) {
        return Err(format!("no /boot/vmlinuz-{kver}"));
    }
    let dir = state_dir_handle(root, state_dir)?;
    let last = dir.read(Path::new("last-good")).ok().flatten().map(|b| String::from_utf8_lossy(&b).trim().to_string()).unwrap_or_default();
    if !last.is_empty() && last != kver {
        dir.write_atomic(Path::new("previous-good"), format!("{last}\n").as_bytes()).map_err(io_err(format!("{state_dir}/previous-good")))?;
        log(format!("previous-good = {last}"));
    }
    dir.write_atomic(Path::new("last-good"), format!("{kver}\n").as_bytes()).map_err(io_err(format!("{state_dir}/last-good")))?;
    log(format!("last-good = {kver}"));
    if backup {
        let boot = Beneath::open(&at(root, "/boot")).map_err(io_err("/boot"))?;
        for f in [format!("vmlinuz-{kver}"), format!("kernel-{kver}"), format!("initramfs-{kver}.img"), format!("config-{kver}"), format!("System.map-{kver}")]
        {
            let Some(mut src) = boot.open_file(Path::new(&f)).map_err(io_err(format!("/boot/{f}")))? else { continue };
            let dst = Path::new("kernel-backup").join(kver).join(&f);
            boot.write_atomic_from(&dst, &mut src, None).map_err(io_err(format!("/boot/{}", dst.display())))?;
            log(format!("backed up /boot/{f} → /boot/kernel-backup/{kver}"));
        }
    }
    Ok(())
}

/// The known-good state directory (created when missing), confined.
pub fn state_dir_handle(root: &Path, state_dir: &str) -> Result<Beneath, String> {
    if !KERNEL_STATE_DIRS.contains(&state_dir) {
        return Err(format!("state directory not allowed: {state_dir}"));
    }
    let var_lib = Beneath::open(&at(root, "/var/lib")).map_err(io_err("/var/lib"))?;
    let rel = Path::new(state_dir).strip_prefix("/var/lib").map_err(|_| format!("state directory not allowed: {state_dir}"))?;
    var_lib.mkdirs(rel).map_err(io_err(state_dir))?;
    Beneath::open(&at(root, state_dir)).map_err(io_err(state_dir))
}

/// Packages whose CONTENTS reference a kernel's module or source tree.
pub fn kernel_owners(root: &Path, kver: &str) -> Vec<String> {
    let needles = [format!(" /lib/modules/{kver}/"), format!(" /usr/src/linux-{kver}/")];
    let exact = [format!("dir /lib/modules/{kver}"), format!("dir /usr/src/linux-{kver}")];
    let mut owners = Vec::new();
    for c in std::fs::read_dir(at(root, "/var/db/pkg")).into_iter().flatten().flatten() {
        for p in std::fs::read_dir(c.path()).into_iter().flatten().flatten() {
            let Ok(text) = std::fs::read_to_string(p.path().join("CONTENTS")) else { continue };
            if needles.iter().any(|n| text.contains(n.as_str())) || text.lines().any(|l| exact.iter().any(|e| l == e)) {
                owners.push(format!("{}/{}", c.file_name().to_string_lossy(), p.file_name().to_string_lossy()));
            }
        }
    }
    owners
}

/// Delete `/boot/<name>` for each name that exists as a file or link; returns how many.
fn remove_boot_files(root: &Path, names: &[String], log: &dyn Fn(String)) -> Result<usize, String> {
    let Ok(boot) = Beneath::open(&at(root, "/boot")) else { return Ok(0) };
    let mut removed = 0;
    for name in names {
        let rel = Path::new(name);
        if boot.exists(rel).unwrap_or(false) && !boot.is_dir(rel).unwrap_or(true) {
            boot.unlink(rel).map_err(io_err(format!("/boot/{name}")))?;
            log(format!("removed /boot/{name}"));
            removed += 1;
        }
    }
    Ok(removed)
}

/// Remove a no-longer-installed kernel's files (or only its `.old` copies).
pub fn remove_kernel_leftovers(root: &Path, kver: &str, old_only: bool, log: &dyn Fn(String)) -> Result<(), String> {
    if !valid_kver(kver) {
        return Err(format!("invalid kernel version {kver}"));
    }
    let prefixes = ["vmlinuz", "kernel", "bzImage", "initramfs", "initrd", "System.map", "config"];
    let names = |suffixes: &[&str]| -> Vec<String> { prefixes.iter().flat_map(|p| suffixes.iter().map(move |s| format!("{p}-{kver}{s}"))).collect() };
    if old_only {
        let removed = remove_boot_files(root, &names(&[".old", ".img.old"]), log)?;
        return if removed == 0 { Err("nothing to remove".into()) } else { Ok(()) };
    }
    let running = read_trim(Path::new("/proc/sys/kernel/osrelease"));
    if kver == running {
        return Err(format!("{kver} is the running kernel"));
    }
    for d in KERNEL_STATE_DIRS {
        for f in ["last-good", "previous-good"] {
            if read_trim(&at(root, d).join(f)) == kver {
                return Err(format!("{kver} is marked known-good ({d}/{f})"));
            }
        }
    }
    if std::fs::read_link(at(root, "/usr/src/linux")).is_ok_and(|t| t.to_string_lossy().trim_end_matches('/').ends_with(&format!("linux-{kver}"))) {
        return Err(format!("/usr/src/linux points to linux-{kver} (eselect kernel); select another kernel first"));
    }
    let owners = kernel_owners(root, kver);
    if !owners.is_empty() {
        return Err(format!("still owned by {} — remove the package with depclean first", owners.join(", ")));
    }
    let mut removed = remove_boot_files(root, &names(&["", ".img", ".old", ".img.old"]), log)?;
    for (parent, name) in [("/lib/modules", kver.to_string()), ("/usr/src", format!("linux-{kver}"))] {
        let Ok(b) = Beneath::open(&at(root, parent)) else { continue };
        if b.is_dir(Path::new(&name)).unwrap_or(false) {
            b.remove_tree(Path::new(&name)).map_err(io_err(format!("{parent}/{name}")))?;
            log(format!("removed {parent}/{name}/"));
            removed += 1;
        }
    }
    if removed == 0 { Err("nothing to remove".into()) } else { Ok(()) }
}

/// Is an emerge or ebuild process running (other than this one)?
pub fn emerge_running() -> bool {
    let me = std::process::id().to_string();
    for e in std::fs::read_dir("/proc").into_iter().flatten().flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        if !name.bytes().all(|b| b.is_ascii_digit()) || name == me {
            continue;
        }
        let Ok(cmd) = std::fs::read(e.path().join("cmdline")) else { continue };
        let args: Vec<String> = cmd.split(|b| *b == 0).take(3).map(|a| String::from_utf8_lossy(a).to_string()).collect();
        if args.iter().any(|a| {
            let base = a.rsplit('/').next().unwrap_or("");
            base == "emerge" || base == "ebuild"
        }) {
            return true;
        }
    }
    false
}

/// PORTAGE_TMPDIR values: the global one and those set in /etc/portage/env/*.
pub fn tmpdirs(root: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if is_real_root(root)
        && let Some(prog) = find_program(&["portageq"])
        && let Ok(o) = std::process::Command::new(prog).args(["envvar", "PORTAGE_TMPDIR"]).output()
    {
        let v = String::from_utf8_lossy(&o.stdout).trim().to_string();
        if v.starts_with('/') {
            out.push(PathBuf::from(v));
        }
    }
    for e in std::fs::read_dir(at(root, "/etc/portage/env")).into_iter().flatten().flatten() {
        let Ok(t) = std::fs::read_to_string(e.path()) else { continue };
        for line in t.lines() {
            if let Some(v) = line.trim().strip_prefix("PORTAGE_TMPDIR=") {
                let v = v.trim().trim_matches('"').trim_matches('\'');
                if v.starts_with('/') && !v.contains('$') && !v.split('/').any(|c| c == "..") {
                    out.push(PathBuf::from(v));
                }
            }
        }
    }
    if out.is_empty() {
        out.push(PathBuf::from("/var/tmp"));
    }
    out.sort();
    out.dedup();
    out
}

fn portage_uid() -> Option<u32> {
    let name = c"portage";
    let pw = unsafe { libc::getpwnam(name.as_ptr()) };
    (!pw.is_null()).then(|| unsafe { (*pw).pw_uid })
}

/// `<tmpdir>/portage` directories that really are Portage's build roots:
/// real directories owned by root or the portage user (a PORTAGE_TMPDIR set
/// in an env file must not make the cleanup delete somebody's own files).
fn build_roots(root: &Path) -> Vec<(PathBuf, PathBuf)> {
    let portage = portage_uid();
    let me = unsafe { libc::geteuid() };
    tmpdirs(root)
        .into_iter()
        .filter_map(|t| {
            let logical = t.join("portage");
            let real = at(root, &logical);
            let md = std::fs::symlink_metadata(&real).ok()?;
            let owner_ok = md.uid() == 0 || Some(md.uid()) == portage || (!is_real_root(root) && md.uid() == me);
            (md.is_dir() && owner_ok).then_some((logical, real))
        })
        .collect()
}

/// Build directories (`<tmpdir>/portage/<cat>/<pf>`) left behind by failed or
/// interrupted builds: (logical path, real base, path relative to the base).
fn build_dir_entries(root: &Path) -> Vec<(PathBuf, PathBuf, PathBuf)> {
    let mut out = Vec::new();
    for (logical, base) in build_roots(root) {
        for c in std::fs::read_dir(&base).into_iter().flatten().flatten() {
            let cat = c.file_name().to_string_lossy().to_string();
            if !(cat.contains('-') || cat == "virtual") || !c.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }
            for p in std::fs::read_dir(c.path()).into_iter().flatten().flatten() {
                let pf = p.file_name().to_string_lossy().to_string();
                // Portage always creates ${PF}/temp; a package directory of an
                // old /usr/portage tree (same <cat>/<pkg> shape) has none.
                let is_build_dir = std::fs::symlink_metadata(p.path().join("temp")).is_ok_and(|m| m.is_dir());
                if pf.contains('-') && p.file_type().map(|t| t.is_dir()).unwrap_or(false) && is_build_dir {
                    let rel = Path::new(&cat).join(&pf);
                    out.push((logical.join(&rel), base.clone(), rel));
                }
            }
        }
    }
    out
}

/// Leftover build directories (logical paths), for display.
pub fn build_dirs(root: &Path) -> Vec<PathBuf> {
    build_dir_entries(root).into_iter().map(|(l, _, _)| l).collect()
}

pub fn clean_build_dirs(root: &Path, log: &dyn Fn(String)) -> Result<(), String> {
    if emerge_running() {
        return Err("an emerge is running — try again when it has finished".into());
    }
    let dirs = build_dir_entries(root);
    if dirs.is_empty() {
        return Err("no leftover build directories".into());
    }
    for (logical, base, rel) in dirs {
        let b = Beneath::open(&base).map_err(io_err(base.display()))?;
        b.remove_tree(&rel).map_err(io_err(logical.display()))?;
        log(format!("removed {}", logical.display()));
        if let Some(cat) = rel.parent() {
            let _ = b.rmdir(cat);
        }
    }
    Ok(())
}

/// `mount -o remount,rw|ro <target>` (or a fresh mount from fstab when it is
/// not mounted) for one of the boot mount points.
pub fn remount_argv(root: &Path, target: &str, rw: bool) -> Result<(String, Vec<String>), String> {
    if !BOOT_MOUNTPOINTS.contains(&target) {
        return Err(format!("{target} is not a boot mount point"));
    }
    let mounts = std::fs::read_to_string("/proc/mounts").unwrap_or_default();
    let mounted = mounts.lines().any(|l| l.split_whitespace().nth(1) == Some(target));
    let in_fstab = std::fs::read_to_string(at(root, "/etc/fstab"))
        .unwrap_or_default()
        .lines()
        .any(|l| !l.trim_start().starts_with('#') && l.split_whitespace().nth(1) == Some(target));
    if !mounted && !in_fstab {
        return Err(format!("{target} is neither mounted nor in /etc/fstab"));
    }
    let prog = find_program(&["mount"]).ok_or("mount not found")?;
    let mode = if rw { "rw" } else { "ro" };
    let opts = if mounted { format!("remount,{mode}") } else { mode.to_string() };
    Ok((prog, vec!["-o".into(), opts, target.to_string()]))
}
