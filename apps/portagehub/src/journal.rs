//! Transactions for privileged file changes: before a file is touched inside
//! a transaction, its previous state is saved, so a failed batch can be rolled
//! back and any committed change can be undone later.
//!
//! Layout: `<root>/var/lib/portagehub/journal/<id>/{manifest.json,files/N}`,
//! directory mode 0700 (it may hold copies of root-only files).

use crate::safefs::Beneath;
use serde::{Deserialize, Serialize};
use std::io;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

const KEEP_TX: usize = 50;
const KEEP_BYTES: u64 = 50 << 20;
/// Transactions left open (GUI crashed mid-batch) are dropped after a week.
const STALE_OPEN_SECS: i64 = 7 * 86400;

/// Maps a logical path (`/etc/portage/x`) to the directory that confines it
/// and the path relative to that directory.
pub type Resolve<'a> = &'a dyn Fn(&str) -> io::Result<(Beneath, PathBuf)>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TxState {
    Open,
    Committed,
    RolledBack,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Prior {
    /// The file existed; its content is in `saved`.
    File,
    /// Nothing existed at the path (rollback deletes what is there).
    Absent,
    /// The directory was created by the transaction (rollback removes it if empty).
    DirCreated,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entry {
    /// Logical absolute path, e.g. `/etc/portage/package.use/zz-portagehub`.
    pub path: String,
    pub prior: Prior,
    #[serde(default)]
    pub saved: Option<String>,
    #[serde(default)]
    pub mode: u32,
    /// Content hash right after commit (None: absent), to detect later edits before an undo.
    #[serde(default)]
    pub after: Option<Option<u64>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Manifest {
    pub id: String,
    pub label: String,
    pub created: i64,
    pub state: TxState,
    pub entries: Vec<Entry>,
}

impl Manifest {
    /// Logical paths of the files (not directories) the transaction touched.
    pub fn files(&self) -> Vec<String> {
        self.entries.iter().filter(|e| e.prior != Prior::DirCreated).map(|e| e.path.clone()).collect()
    }
}

pub struct Journal {
    dir: PathBuf,
    lock: Mutex<()>,
}

fn now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() < 64 && id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
}

/// FNV-1a: stable across builds (unlike `DefaultHasher`), enough to notice edits. Shared with the
/// GUI, which sends the hash of a file it read as the precondition of a change (`Expect`).
pub fn content_hash(data: &[u8]) -> u64 {
    data.iter().fold(0xcbf29ce484222325u64, |h, b| (h ^ *b as u64).wrapping_mul(0x100000001b3))
}

fn current_hash(base: &Beneath, rel: &Path) -> io::Result<Option<u64>> {
    if base.is_dir(rel)? {
        return Ok(Some(0));
    }
    Ok(base.read(rel)?.map(|b| content_hash(&b)))
}

fn invalid(msg: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, msg)
}

impl Journal {
    pub fn new(root: &Path) -> Journal {
        Journal { dir: root.join("var/lib/portagehub/journal"), lock: Mutex::new(()) }
    }

    fn tx_dir(&self, id: &str) -> io::Result<PathBuf> {
        if !valid_id(id) {
            return Err(invalid(format!("invalid transaction id {id}")));
        }
        Ok(self.dir.join(id))
    }

    fn load(&self, id: &str) -> io::Result<Manifest> {
        let t = std::fs::read_to_string(self.tx_dir(id)?.join("manifest.json"))
            .map_err(|e| if e.kind() == io::ErrorKind::NotFound { invalid(format!("unknown transaction {id}")) } else { e })?;
        serde_json::from_str(&t).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
    }

    fn save(&self, m: &Manifest) -> io::Result<()> {
        let d = self.tx_dir(&m.id)?;
        let tmp = d.join("manifest.json.tmp");
        let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&tmp)?;
        io::Write::write_all(&mut f, &serde_json::to_vec_pretty(m).unwrap_or_default())?;
        f.sync_all()?;
        std::fs::rename(tmp, d.join("manifest.json"))
    }

    pub fn get(&self, id: &str) -> io::Result<Manifest> {
        let _g = self.lock.lock().unwrap();
        self.load(id)
    }

    pub fn begin(&self, label: &str) -> io::Result<String> {
        let _g = self.lock.lock().unwrap();
        std::fs::DirBuilder::new().recursive(true).mode(0o700).create(&self.dir)?;
        std::fs::set_permissions(&self.dir, std::fs::Permissions::from_mode(0o700))?;
        let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
        let mut id = format!("{}-{:09}", now(), nanos);
        while self.dir.join(&id).exists() {
            id.push('x');
        }
        let d = self.dir.join(&id);
        std::fs::DirBuilder::new().mode(0o700).create(&d)?;
        std::fs::DirBuilder::new().mode(0o700).create(d.join("files"))?;
        let label: String = label.chars().take(200).collect();
        self.save(&Manifest { id: id.clone(), label, created: now(), state: TxState::Open, entries: Vec::new() })?;
        Ok(id)
    }

    fn open_tx(&self, tx: &str) -> io::Result<Manifest> {
        let m = self.load(tx)?;
        if m.state != TxState::Open {
            return Err(invalid(format!("transaction {tx} is not open")));
        }
        Ok(m)
    }

    /// Save the current state of `rel` (below `base`) before the first change in `tx`.
    pub fn record(&self, tx: &str, logical: &str, base: &Beneath, rel: &Path) -> io::Result<()> {
        let _g = self.lock.lock().unwrap();
        let mut m = self.open_tx(tx)?;
        if m.entries.iter().any(|e| e.path == logical) {
            return Ok(());
        }
        let entry = match base.read_with_mode(rel)? {
            Some((content, mode)) => {
                let n = format!("files/{}", m.entries.len());
                let mut f = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(self.tx_dir(tx)?.join(&n))?;
                io::Write::write_all(&mut f, &content)?;
                f.sync_all()?;
                Entry { path: logical.to_string(), prior: Prior::File, saved: Some(n), mode, after: None }
            }
            None => Entry { path: logical.to_string(), prior: Prior::Absent, saved: None, mode: 0, after: None },
        };
        m.entries.push(entry);
        self.save(&m)
    }

    pub fn record_dir_created(&self, tx: &str, logical: &str) -> io::Result<()> {
        let _g = self.lock.lock().unwrap();
        let mut m = self.open_tx(tx)?;
        if !m.entries.iter().any(|e| e.path == logical) {
            m.entries.push(Entry { path: logical.to_string(), prior: Prior::DirCreated, saved: None, mode: 0, after: None });
            self.save(&m)?;
        }
        Ok(())
    }

    pub fn commit(&self, tx: &str, resolve: Resolve) -> io::Result<Manifest> {
        let g = self.lock.lock().unwrap();
        let mut m = self.open_tx(tx)?;
        for e in m.entries.iter_mut().filter(|e| e.prior != Prior::DirCreated) {
            let (base, rel) = resolve(&e.path)?;
            e.after = Some(current_hash(&base, &rel)?);
        }
        m.state = TxState::Committed;
        self.save(&m)?;
        drop(g);
        self.prune();
        Ok(m)
    }

    /// Restore every recorded path, newest first. Undoing a committed
    /// transaction is refused when one of its files changed since, unless `force`.
    pub fn rollback(&self, tx: &str, force: bool, resolve: Resolve) -> io::Result<(Manifest, Vec<String>)> {
        let _g = self.lock.lock().unwrap();
        let mut m = self.load(tx)?;
        if m.state == TxState::RolledBack {
            return Err(invalid(format!("transaction {tx} was already undone")));
        }
        if m.state == TxState::Committed && !force {
            let mut changed = Vec::new();
            for e in &m.entries {
                if let Some(after) = e.after {
                    let (base, rel) = resolve(&e.path)?;
                    if current_hash(&base, &rel)? != after {
                        changed.push(e.path.clone());
                    }
                }
            }
            if !changed.is_empty() {
                return Err(io::Error::new(io::ErrorKind::AlreadyExists, format!("changed since: {}", changed.join(", "))));
            }
        }
        let mut done = Vec::new();
        for e in m.entries.iter().rev() {
            let (base, rel) = resolve(&e.path)?;
            match e.prior {
                Prior::File => {
                    // The path may have become an (empty) directory, e.g. a
                    // package.use file converted to a directory.
                    if base.is_dir(&rel)? {
                        base.rmdir(&rel)?;
                    }
                    let mut f = std::fs::File::open(self.tx_dir(tx)?.join(e.saved.as_deref().unwrap_or("")))?;
                    base.write_atomic_from(&rel, &mut f, Some(e.mode))?;
                    done.push(format!("restored {}", e.path));
                }
                Prior::Absent => {
                    if base.is_dir(&rel)? {
                        base.rmdir(&rel)?;
                        done.push(format!("removed {}/", e.path));
                    } else if base.exists(&rel)? {
                        base.unlink(&rel)?;
                        done.push(format!("removed {}", e.path));
                    }
                }
                Prior::DirCreated => {
                    if base.rmdir(&rel).is_ok() {
                        done.push(format!("removed {}/", e.path));
                    }
                }
            }
        }
        m.state = TxState::RolledBack;
        self.save(&m)?;
        Ok((m, done))
    }

    pub fn list(&self) -> Vec<Manifest> {
        let mut out: Vec<Manifest> = std::fs::read_dir(&self.dir)
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|e| self.load(&e.file_name().to_string_lossy()).ok())
            .collect();
        out.sort_by(|a, b| b.id.cmp(&a.id));
        out
    }

    /// Keep the newest transactions within the count and size caps.
    fn prune(&self) {
        let _g = self.lock.lock().unwrap();
        let mut bytes = 0u64;
        for (i, m) in self.list().iter().enumerate() {
            let d = self.dir.join(&m.id);
            let size: u64 = std::fs::read_dir(d.join("files")).into_iter().flatten().flatten().filter_map(|e| e.metadata().ok()).map(|md| md.len()).sum();
            bytes += size;
            let stale = m.state == TxState::Open && now() - m.created > STALE_OPEN_SECS;
            if stale || (m.state != TxState::Open && (i >= KEEP_TX || bytes > KEEP_BYTES)) {
                let _ = std::fs::remove_dir_all(d);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("portagehub-journal-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("etc/portage/package.use")).unwrap();
        d
    }

    fn resolver(root: &Path) -> impl Fn(&str) -> io::Result<(Beneath, PathBuf)> + '_ {
        move |p: &str| Ok((Beneath::open(&root.join("etc/portage"))?, PathBuf::from(p.strip_prefix("/etc/portage/").unwrap())))
    }

    #[test]
    fn rollback_restores_bytes_modes_and_removes_new_files() {
        let d = scratch("rb");
        let pu = d.join("etc/portage/package.use");
        std::fs::write(pu.join("a"), b"orig\n").unwrap();
        std::fs::set_permissions(pu.join("a"), std::fs::Permissions::from_mode(0o640)).unwrap();
        let j = Journal::new(&d);
        let b = Beneath::open(&d.join("etc/portage")).unwrap();
        let tx = j.begin("test").unwrap();
        j.record(&tx, "/etc/portage/package.use/a", &b, Path::new("package.use/a")).unwrap();
        b.write_atomic(Path::new("package.use/a"), b"changed\n").unwrap();
        j.record(&tx, "/etc/portage/package.use/new", &b, Path::new("package.use/new")).unwrap();
        b.write_atomic(Path::new("package.use/new"), b"x").unwrap();
        // Second record of the same path keeps the first snapshot.
        j.record(&tx, "/etc/portage/package.use/a", &b, Path::new("package.use/a")).unwrap();
        let r = resolver(&d);
        let (_, done) = j.rollback(&tx, false, &r).unwrap();
        assert_eq!(done.len(), 2);
        assert_eq!(std::fs::read(pu.join("a")).unwrap(), b"orig\n");
        assert_eq!(std::fs::metadata(pu.join("a")).unwrap().permissions().mode() & 0o777, 0o640);
        assert!(!pu.join("new").exists());
        assert!(j.rollback(&tx, false, &r).is_err(), "double rollback");
        let jm = std::fs::metadata(d.join("var/lib/portagehub/journal")).unwrap().permissions().mode() & 0o777;
        assert_eq!(jm, 0o700);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn undo_of_committed_change_detects_later_edits() {
        let d = scratch("undo");
        let j = Journal::new(&d);
        let b = Beneath::open(&d.join("etc/portage")).unwrap();
        let r = resolver(&d);
        let tx = j.begin("add").unwrap();
        j.record(&tx, "/etc/portage/package.use/zz", &b, Path::new("package.use/zz")).unwrap();
        b.write_atomic(Path::new("package.use/zz"), b"a/b x\n").unwrap();
        let m = j.commit(&tx, &r).unwrap();
        assert_eq!(m.files(), vec!["/etc/portage/package.use/zz".to_string()]);
        assert!(j.record(&tx, "/etc/portage/x", &b, Path::new("x")).is_err(), "committed tx is closed");
        b.write_atomic(Path::new("package.use/zz"), b"a/b x y\n").unwrap();
        let e = j.rollback(&tx, false, &r).unwrap_err();
        assert_eq!(e.kind(), io::ErrorKind::AlreadyExists);
        j.rollback(&tx, true, &r).unwrap();
        assert!(!d.join("etc/portage/package.use/zz").exists());
        assert!(j.begin("x").is_ok());
        assert!(j.get("../../etc").is_err());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn file_to_directory_conversion_rolls_back() {
        let d = scratch("conv");
        std::fs::remove_dir(d.join("etc/portage/package.use")).unwrap();
        std::fs::write(d.join("etc/portage/package.use"), b"a/b x\n").unwrap();
        let j = Journal::new(&d);
        let b = Beneath::open(&d.join("etc/portage")).unwrap();
        let r = resolver(&d);
        let tx = j.begin("convert").unwrap();
        // Same sequence as edits::convert_to_dir: rename away, mkdir, move in.
        j.record(&tx, "/etc/portage/package.use", &b, Path::new("package.use")).unwrap();
        j.record(&tx, "/etc/portage/package.use.tmp", &b, Path::new("package.use.tmp")).unwrap();
        b.rename_noreplace(Path::new("package.use"), Path::new("package.use.tmp")).unwrap();
        j.record_dir_created(&tx, "/etc/portage/package.use").unwrap();
        b.mkdirs(Path::new("package.use")).unwrap();
        j.record(&tx, "/etc/portage/package.use/main", &b, Path::new("package.use/main")).unwrap();
        b.rename_noreplace(Path::new("package.use.tmp"), Path::new("package.use/main")).unwrap();
        j.commit(&tx, &r).unwrap();
        j.rollback(&tx, false, &r).unwrap();
        assert_eq!(std::fs::read(d.join("etc/portage/package.use")).unwrap(), b"a/b x\n");
        assert!(!d.join("etc/portage/package.use.tmp").exists());
        let _ = std::fs::remove_dir_all(&d);
    }
}
