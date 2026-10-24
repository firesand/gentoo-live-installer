//! Symlink-resistant file operations confined below a directory.
//!
//! Every path is resolved with `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
//! RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV)` relative to a directory handle,
//! so neither a symlink (planted or legitimate, like `make.profile`) nor `..`
//! can make a privileged write land outside the tree. Renames and unlinks use
//! the resolved parent directory handles (`renameat2`, `unlinkat`).

use std::ffi::CString;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Component, Path, PathBuf};

/// A directory that confines all operations below it.
pub struct Beneath {
    dir: OwnedFd,
    pub path: PathBuf,
}

fn cstr(p: &Path) -> io::Result<CString> {
    CString::new(p.as_os_str().as_bytes()).map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "path contains NUL"))
}

/// Split a relative path into checked components (no `..`, no absolute parts).
fn components(rel: &Path) -> io::Result<Vec<&std::ffi::OsStr>> {
    let mut out = Vec::new();
    for c in rel.components() {
        match c {
            Component::Normal(n) => out.push(n),
            Component::CurDir => {}
            _ => return Err(io::Error::new(io::ErrorKind::PermissionDenied, format!("path not allowed: {}", rel.display()))),
        }
    }
    if out.is_empty() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "empty path"));
    }
    Ok(out)
}

fn openat2(dir: &OwnedFd, rel: &Path, flags: i32, mode: u32) -> io::Result<OwnedFd> {
    let c = cstr(rel)?;
    // `open_how` is non-exhaustive in libc: start zeroed, then fill in.
    let mut how: libc::open_how = unsafe { std::mem::zeroed() };
    how.flags = (flags | libc::O_CLOEXEC) as u64;
    how.mode = mode as u64;
    how.resolve = libc::RESOLVE_BENEATH | libc::RESOLVE_NO_SYMLINKS | libc::RESOLVE_NO_MAGICLINKS | libc::RESOLVE_NO_XDEV;
    let fd = unsafe { libc::syscall(libc::SYS_openat2, dir.as_raw_fd(), c.as_ptr(), &how as *const libc::open_how, std::mem::size_of::<libc::open_how>()) };
    if fd < 0 {
        let e = io::Error::last_os_error();
        // ELOOP: a symlink on the way; EXDEV: escaping the tree.
        return Err(match e.raw_os_error() {
            Some(libc::ELOOP) | Some(libc::EXDEV) => {
                io::Error::new(io::ErrorKind::PermissionDenied, format!("{}: refusing to follow a symlink or leave the directory", rel.display()))
            }
            _ => e,
        });
    }
    Ok(unsafe { OwnedFd::from_raw_fd(fd as i32) })
}

fn check(rc: libc::c_int) -> io::Result<()> {
    if rc < 0 { Err(io::Error::last_os_error()) } else { Ok(()) }
}

impl Beneath {
    /// Open the confining directory. The directory itself must not be a symlink.
    pub fn open(path: &Path) -> io::Result<Beneath> {
        let c = cstr(path)?;
        let fd = unsafe { libc::open(c.as_ptr(), libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Beneath { dir: unsafe { OwnedFd::from_raw_fd(fd) }, path: path.to_path_buf() })
    }

    /// Parent directory handle and the final name of `rel`, creating missing
    /// directories when `create` is set.
    fn parent(&self, rel: &Path, create: bool) -> io::Result<(OwnedFd, CString)> {
        let comps = components(rel)?;
        let (name, dirs) = comps.split_last().unwrap();
        let mut cur = openat2(&self.dir, Path::new("."), libc::O_PATH | libc::O_DIRECTORY, 0)?;
        for d in dirs {
            let next = Path::new(d);
            match openat2(&cur, next, libc::O_PATH | libc::O_DIRECTORY, 0) {
                Ok(fd) => cur = fd,
                Err(e) if create && e.kind() == io::ErrorKind::NotFound => {
                    let c = cstr(next)?;
                    let rc = unsafe { libc::mkdirat(cur.as_raw_fd(), c.as_ptr(), 0o755) };
                    if rc < 0 && io::Error::last_os_error().raw_os_error() != Some(libc::EEXIST) {
                        return Err(io::Error::last_os_error());
                    }
                    cur = openat2(&cur, next, libc::O_PATH | libc::O_DIRECTORY, 0)?;
                }
                Err(e) => return Err(e),
            }
        }
        Ok((cur, cstr(Path::new(name))?))
    }

    /// Open a regular file for reading (None when it does not exist).
    pub fn open_file(&self, rel: &Path) -> io::Result<Option<std::fs::File>> {
        match openat2(&self.dir, rel, libc::O_RDONLY | libc::O_NOFOLLOW, 0) {
            Ok(fd) => {
                let f = std::fs::File::from(fd);
                if !f.metadata()?.is_file() {
                    return Err(io::Error::new(io::ErrorKind::InvalidInput, format!("{} is not a regular file", rel.display())));
                }
                Ok(Some(f))
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Read a regular file and its permission bits (None when it does not exist).
    pub fn read_with_mode(&self, rel: &Path) -> io::Result<Option<(Vec<u8>, u32)>> {
        let Some(mut f) = self.open_file(rel)? else { return Ok(None) };
        let mode = std::os::unix::fs::PermissionsExt::mode(&f.metadata()?.permissions()) & 0o7777;
        let mut buf = Vec::new();
        f.read_to_end(&mut buf)?;
        Ok(Some((buf, mode)))
    }

    /// Read a regular file (None when it does not exist).
    pub fn read(&self, rel: &Path) -> io::Result<Option<Vec<u8>>> {
        Ok(self.read_with_mode(rel)?.map(|(b, _)| b))
    }

    /// Target of a symbolic link (None when `rel` is no symlink or does not exist).
    pub fn link_target(&self, rel: &Path) -> io::Result<Option<PathBuf>> {
        let (parent, name) = match self.parent(rel, false) {
            Ok(x) => x,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e),
        };
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        if unsafe { libc::fstatat(parent.as_raw_fd(), name.as_ptr(), &mut st, libc::AT_SYMLINK_NOFOLLOW) } != 0 || st.st_mode & libc::S_IFMT != libc::S_IFLNK {
            return Ok(None);
        }
        let mut buf = vec![0u8; libc::PATH_MAX as usize];
        let n = unsafe { libc::readlinkat(parent.as_raw_fd(), name.as_ptr(), buf.as_mut_ptr() as *mut libc::c_char, buf.len()) };
        if n < 0 {
            return Err(io::Error::last_os_error());
        }
        buf.truncate(n as usize);
        Ok(Some(PathBuf::from(std::ffi::OsString::from_vec(buf))))
    }

    /// Create a symbolic link `rel` → `target` (the target is stored, not resolved).
    pub fn symlink(&self, rel: &Path, target: &Path) -> io::Result<()> {
        let (parent, name) = self.parent(rel, true)?;
        let t = cstr(target)?;
        check(unsafe { libc::symlinkat(t.as_ptr(), parent.as_raw_fd(), name.as_ptr()) })
    }

    /// Is `rel` an existing directory (not a symlink to one)?
    pub fn is_dir(&self, rel: &Path) -> io::Result<bool> {
        match self.parent(rel, false) {
            Ok((p, name)) => Ok(self.mode_of(&p, &name).is_some_and(|(_, dir)| dir)),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(e) => Err(e),
        }
    }

    /// Mode of an existing entry (None when absent).
    fn mode_of(&self, parent: &OwnedFd, name: &CString) -> Option<(u32, bool)> {
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        let rc = unsafe { libc::fstatat(parent.as_raw_fd(), name.as_ptr(), &mut st, libc::AT_SYMLINK_NOFOLLOW) };
        (rc == 0).then_some((st.st_mode & 0o7777, (st.st_mode & libc::S_IFMT) == libc::S_IFDIR))
    }

    pub fn exists(&self, rel: &Path) -> io::Result<bool> {
        match self.parent(rel, false) {
            Ok((p, name)) => Ok(self.mode_of(&p, &name).is_some()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(e) => Err(e),
        }
    }

    /// Atomically replace (or create) a file: write a temporary file next to
    /// it, then rename it over the target. A symlink at the target is
    /// replaced, never followed. The existing mode is kept (new files: 0644).
    pub fn write_atomic(&self, rel: &Path, content: &[u8]) -> io::Result<()> {
        self.write_atomic_from(rel, &mut &content[..], None)
    }

    /// Like [`Beneath::write_atomic`], streaming from `src`, with an optional explicit mode.
    pub fn write_atomic_from(&self, rel: &Path, src: &mut dyn Read, mode: Option<u32>) -> io::Result<()> {
        let (parent, name) = self.parent(rel, true)?;
        let existing = self.mode_of(&parent, &name);
        if existing.is_some_and(|(_, dir)| dir) {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, format!("{} is a directory", rel.display())));
        }
        let mode = mode.or(existing.map(|(m, _)| m)).unwrap_or(0o644);
        let tmp_name = format!(
            ".portagehub-{}-{}.tmp~",
            std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0)
        );
        let tmp = cstr(Path::new(&tmp_name))?;
        let fd = unsafe {
            libc::openat(
                parent.as_raw_fd(),
                tmp.as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600 as libc::c_uint,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let mut f = std::fs::File::from(unsafe { OwnedFd::from_raw_fd(fd) });
        let r = (|| {
            io::copy(src, &mut f)?;
            f.flush()?;
            f.sync_all()?;
            check(unsafe { libc::fchmod(f.as_raw_fd(), mode) })?;
            check(unsafe { libc::renameat2(parent.as_raw_fd(), tmp.as_ptr(), parent.as_raw_fd(), name.as_ptr(), 0) })
        })();
        if r.is_err() {
            unsafe { libc::unlinkat(parent.as_raw_fd(), tmp.as_ptr(), 0) };
        }
        r
    }

    pub fn unlink(&self, rel: &Path) -> io::Result<()> {
        let (parent, name) = self.parent(rel, false)?;
        check(unsafe { libc::unlinkat(parent.as_raw_fd(), name.as_ptr(), 0) })
    }

    pub fn rmdir(&self, rel: &Path) -> io::Result<()> {
        let (parent, name) = self.parent(rel, false)?;
        check(unsafe { libc::unlinkat(parent.as_raw_fd(), name.as_ptr(), libc::AT_REMOVEDIR) })
    }

    pub fn mkdirs(&self, rel: &Path) -> io::Result<()> {
        let (parent, name) = self.parent(rel, true)?;
        let rc = unsafe { libc::mkdirat(parent.as_raw_fd(), name.as_ptr(), 0o755) };
        if rc < 0 && io::Error::last_os_error().raw_os_error() != Some(libc::EEXIST) {
            return Err(io::Error::last_os_error());
        }
        // Make sure what exists is a real directory, not a symlink.
        openat2(&self.dir, rel, libc::O_PATH | libc::O_DIRECTORY, 0).map(|_| ())
    }

    /// Rename without replacing an existing target (atomic check).
    pub fn rename_noreplace(&self, from: &Path, to: &Path) -> io::Result<()> {
        let (fp, fname) = self.parent(from, false)?;
        let (tp, tname) = self.parent(to, true)?;
        check(unsafe { libc::renameat2(fp.as_raw_fd(), fname.as_ptr(), tp.as_raw_fd(), tname.as_ptr(), libc::RENAME_NOREPLACE) })
    }

    /// Rename, replacing the target (used to accept `._cfg` updates).
    pub fn rename_replace(&self, from: &Path, to: &Path) -> io::Result<()> {
        let (fp, fname) = self.parent(from, false)?;
        let (tp, tname) = self.parent(to, false)?;
        check(unsafe { libc::renameat2(fp.as_raw_fd(), fname.as_ptr(), tp.as_raw_fd(), tname.as_ptr(), 0) })
    }

    /// Delete a directory tree (or a single file/symlink). Every level is
    /// opened relative to its already-open parent with `O_NOFOLLOW`, so a
    /// symlink swapped in during the walk is removed, never followed, and
    /// the walk never crosses into another filesystem.
    pub fn remove_tree(&self, rel: &Path) -> io::Result<()> {
        let (parent, name) = self.parent(rel, false)?;
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        check(unsafe { libc::fstat(parent.as_raw_fd(), &mut st) })?;
        remove_at(&parent, &name, st.st_dev, 0)
    }
}

fn remove_at(parent: &OwnedFd, name: &CString, dev: libc::dev_t, depth: usize) -> io::Result<()> {
    if depth > 256 {
        return Err(io::Error::other("directory tree too deep"));
    }
    let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
    if fd < 0 {
        let e = io::Error::last_os_error();
        return match e.raw_os_error() {
            // Not a directory (file or symlink): unlink the entry itself.
            Some(libc::ENOTDIR) | Some(libc::ELOOP) => check(unsafe { libc::unlinkat(parent.as_raw_fd(), name.as_ptr(), 0) }),
            _ => Err(e),
        };
    }
    let dir = unsafe { OwnedFd::from_raw_fd(fd) };
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    check(unsafe { libc::fstat(dir.as_raw_fd(), &mut st) })?;
    if st.st_dev != dev {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "refusing to delete across a mount point"));
    }
    for child in list_names(&dir)? {
        remove_at(&dir, &child, dev, depth + 1)?;
    }
    check(unsafe { libc::unlinkat(parent.as_raw_fd(), name.as_ptr(), libc::AT_REMOVEDIR) })
}

/// Entry names of an open directory (without `.` and `..`).
fn list_names(dir: &OwnedFd) -> io::Result<Vec<CString>> {
    let dup = unsafe { libc::fcntl(dir.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0) };
    if dup < 0 {
        return Err(io::Error::last_os_error());
    }
    let dp = unsafe { libc::fdopendir(dup) };
    if dp.is_null() {
        let e = io::Error::last_os_error();
        unsafe { libc::close(dup) };
        return Err(e);
    }
    let mut out = Vec::new();
    loop {
        let ent = unsafe { libc::readdir(dp) };
        if ent.is_null() {
            break;
        }
        let n = unsafe { std::ffi::CStr::from_ptr((*ent).d_name.as_ptr()) };
        if n.to_bytes() != b"." && n.to_bytes() != b".." {
            out.push(n.to_owned());
        }
    }
    unsafe { libc::closedir(dp) };
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    fn scratch(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("portagehub-safefs-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("etc/portage/package.use")).unwrap();
        std::fs::create_dir_all(d.join("outside")).unwrap();
        d
    }

    #[test]
    fn writes_stay_beneath_and_refuse_symlinks() {
        let d = scratch("w");
        let b = Beneath::open(&d.join("etc/portage")).unwrap();
        b.write_atomic(Path::new("package.use/zz"), b"a/b x\n").unwrap();
        assert_eq!(std::fs::read(d.join("etc/portage/package.use/zz")).unwrap(), b"a/b x\n");
        // Directory symlink (like make.profile) pointing outside.
        symlink(d.join("outside"), d.join("etc/portage/make.profile")).unwrap();
        assert!(b.write_atomic(Path::new("make.profile/package.use"), b"evil").is_err());
        assert!(!d.join("outside/package.use").exists());
        // Relative symlink climbing out, and nested symlinks.
        symlink("../../outside", d.join("etc/portage/package.use/up")).unwrap();
        assert!(b.write_atomic(Path::new("package.use/up/x"), b"evil").is_err());
        std::fs::create_dir_all(d.join("etc/portage/sets")).unwrap();
        symlink("../package.use/up", d.join("etc/portage/sets/nested")).unwrap();
        assert!(b.write_atomic(Path::new("sets/nested/x"), b"evil").is_err());
        assert!(std::fs::read_dir(d.join("outside")).unwrap().next().is_none());
        // `..` is rejected outright.
        assert!(b.write_atomic(Path::new("../outside/x"), b"evil").is_err());
        assert!(b.write_atomic(Path::new("/etc/passwd"), b"evil").is_err());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn symlink_target_is_replaced_not_followed() {
        let d = scratch("r");
        std::fs::write(d.join("outside/secret"), b"keep").unwrap();
        symlink(d.join("outside/secret"), d.join("etc/portage/package.use/link")).unwrap();
        let b = Beneath::open(&d.join("etc/portage")).unwrap();
        b.write_atomic(Path::new("package.use/link"), b"new").unwrap();
        assert_eq!(std::fs::read(d.join("outside/secret")).unwrap(), b"keep");
        assert!(!std::fs::symlink_metadata(d.join("etc/portage/package.use/link")).unwrap().file_type().is_symlink());
        // Reading through a symlink is refused too.
        symlink(d.join("outside/secret"), d.join("etc/portage/package.use/link2")).unwrap();
        assert!(b.read(Path::new("package.use/link2")).is_err());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn rename_and_unlink_are_confined() {
        let d = scratch("n");
        let b = Beneath::open(&d.join("etc/portage")).unwrap();
        b.write_atomic(Path::new("package.use/a"), b"1").unwrap();
        b.write_atomic(Path::new("package.use/b"), b"2").unwrap();
        assert!(b.rename_noreplace(Path::new("package.use/a"), Path::new("package.use/b")).is_err());
        b.rename_noreplace(Path::new("package.use/a"), Path::new("package.use/c")).unwrap();
        symlink(d.join("outside"), d.join("etc/portage/out")).unwrap();
        std::fs::write(d.join("outside/victim"), b"v").unwrap();
        assert!(b.unlink(Path::new("out/victim")).is_err());
        assert!(d.join("outside/victim").exists());
        assert!(b.rename_noreplace(Path::new("package.use/c"), Path::new("out/c")).is_err());
        // The base directory itself must not be a symlink.
        symlink(d.join("etc/portage"), d.join("etc/portage-link")).unwrap();
        assert!(Beneath::open(&d.join("etc/portage-link")).is_err());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn remove_tree_never_follows_symlinks() {
        let d = scratch("t");
        std::fs::create_dir_all(d.join("etc/portage/tree/a/b")).unwrap();
        std::fs::write(d.join("etc/portage/tree/a/b/f"), b"x").unwrap();
        std::fs::write(d.join("outside/keep"), b"k").unwrap();
        symlink(d.join("outside"), d.join("etc/portage/tree/a/link")).unwrap();
        let b = Beneath::open(&d.join("etc/portage")).unwrap();
        b.remove_tree(Path::new("tree")).unwrap();
        assert!(!d.join("etc/portage/tree").exists());
        assert!(d.join("outside/keep").exists());
        // A symlink as the top entry is unlinked, its target kept.
        symlink(d.join("outside"), d.join("etc/portage/top")).unwrap();
        b.remove_tree(Path::new("top")).unwrap();
        assert!(d.join("outside/keep").exists());
        // Streaming copy with an explicit mode.
        b.write_atomic_from(Path::new("m/f"), &mut &b"abc"[..], Some(0o600)).unwrap();
        assert_eq!(b.read_with_mode(Path::new("m/f")).unwrap(), Some((b"abc".to_vec(), 0o600)));
        assert!(b.is_dir(Path::new("m")).unwrap());
        let _ = std::fs::remove_dir_all(&d);
    }
}
