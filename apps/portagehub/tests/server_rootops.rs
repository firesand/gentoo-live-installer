//! The helper's fixed privileged operations, run against scratch trees.

use portagehub::protocol::{Expect, Op};
use portagehub::rootops;
use portagehub::server::{Server, Sink};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

const STATE: &str = "/var/lib/kernel-fallback";

struct Root(PathBuf);

impl Root {
    fn new(name: &str) -> Root {
        let d = std::env::temp_dir().join(format!("portagehub-rootops-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("boot")).unwrap();
        std::fs::create_dir_all(d.join("var/lib")).unwrap();
        std::fs::create_dir_all(d.join("etc/portage")).unwrap();
        Root(d)
    }

    fn kernel(&self, kver: &str) {
        std::fs::write(self.0.join(format!("boot/vmlinuz-{kver}")), vec![1u8; 4096]).unwrap();
        std::fs::write(self.0.join(format!("boot/initramfs-{kver}.img")), vec![2u8; 8192]).unwrap();
        std::fs::write(self.0.join(format!("boot/config-{kver}")), "CONFIG_X=y\n").unwrap();
        std::fs::write(self.0.join(format!("boot/System.map-{kver}")), "map\n").unwrap();
    }

    fn mark(&self, kver: &str, free: u64) -> (Result<(), String>, Vec<String>) {
        let log = Mutex::new(Vec::new());
        let r = rootops::mark_good_with(&self.0, kver, STATE, true, &|l| log.lock().unwrap().push(l), &|_| Some(free));
        (r, log.into_inner().unwrap())
    }

    fn backups(&self) -> Vec<String> {
        let mut v: Vec<String> = std::fs::read_dir(self.0.join("boot/kernel-backup")).into_iter().flatten().flatten().map(|e| e.file_name().to_string_lossy().to_string()).collect();
        v.sort();
        v
    }

    fn state(&self, file: &str) -> String {
        std::fs::read_to_string(self.0.join(format!("var/lib/kernel-fallback/{file}"))).unwrap_or_default().trim().to_string()
    }
}

impl Drop for Root {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

const PLENTY: u64 = 10 << 30;

#[test]
fn only_the_last_good_and_the_previous_good_kernel_keep_a_backup() {
    let r = Root::new("prune");
    for k in ["6.1.1", "6.1.2", "6.1.3"] {
        r.kernel(k);
    }
    for k in ["6.1.1", "6.1.2", "6.1.3"] {
        r.mark(k, PLENTY).0.unwrap();
    }
    assert_eq!(r.backups(), vec!["6.1.2", "6.1.3"], "the oldest backup is dropped");
    assert_eq!((r.state("last-good").as_str(), r.state("previous-good").as_str()), ("6.1.3", "6.1.2"));
    // Marking the same kernel again changes nothing.
    r.mark("6.1.3", PLENTY).0.unwrap();
    assert_eq!(r.backups(), vec!["6.1.2", "6.1.3"]);
    assert_eq!(std::fs::read(r.0.join("boot/kernel-backup/6.1.3/vmlinuz-6.1.3")).unwrap(), vec![1u8; 4096]);
}

#[test]
fn without_room_the_backup_is_skipped_but_the_kernel_is_still_marked() {
    let r = Root::new("space");
    r.kernel("6.1.1");
    let (result, log) = r.mark("6.1.1", 1 << 20);
    result.unwrap();
    assert_eq!(r.state("last-good"), "6.1.1");
    assert!(r.backups().is_empty() && !r.0.join("boot/kernel-backup").exists());
    assert!(log.iter().any(|l| l.contains("backup skipped: not enough space")), "{log:?}");
    // The sources plus 16 MiB of slack decide: just enough works.
    let need = 4096 + 8192 + "CONFIG_X=y\n".len() as u64 + "map\n".len() as u64 + (16 << 20);
    r.mark("6.1.1", need - 1).0.unwrap();
    assert!(r.backups().is_empty());
    r.mark("6.1.1", need).0.unwrap();
    assert_eq!(r.backups(), vec!["6.1.1"]);
}

#[test]
fn a_backup_that_fails_half_way_leaves_no_partial_directory() {
    let r = Root::new("partial");
    r.kernel("6.1.1");
    r.kernel("6.1.2");
    r.mark("6.1.1", PLENTY).0.unwrap();
    // The last file of the next backup cannot be written: a directory sits where it belongs.
    std::fs::create_dir_all(r.0.join("boot/kernel-backup/6.1.2/System.map-6.1.2/x")).unwrap();
    let (result, _) = r.mark("6.1.2", PLENTY);
    assert!(result.unwrap_err().contains("System.map-6.1.2"), "the copy error names the file");
    assert_eq!(r.backups(), vec!["6.1.1"], "the partial directory of 6.1.2 is gone, the good backup stays");
}

#[test]
fn backups_are_private_and_keep_the_mode_of_their_source() {
    let r = Root::new("modes");
    r.kernel("6.1.1");
    std::fs::set_permissions(r.0.join("boot/initramfs-6.1.1.img"), std::fs::Permissions::from_mode(0o600)).unwrap();
    r.mark("6.1.1", PLENTY).0.unwrap();
    let mode = |p: &str| std::fs::metadata(r.0.join(p)).unwrap().permissions().mode() & 0o7777;
    assert_eq!(mode("boot/kernel-backup/6.1.1/initramfs-6.1.1.img"), 0o600);
    assert_eq!(mode("boot/kernel-backup/6.1.1/config-6.1.1"), 0o644);
    assert_eq!(mode("boot/kernel-backup"), 0o700);
    assert_eq!(mode("boot/kernel-backup/6.1.1"), 0o700);
}

#[test]
fn removing_a_kernels_leftovers_removes_its_backup_too() {
    let r = Root::new("leftovers");
    r.kernel("9.9.9-gone");
    r.mark("9.9.9-gone", PLENTY).0.unwrap();
    // Mark another kernel so 9.9.9-gone is neither last- nor previous-good any more.
    r.kernel("9.9.8");
    r.mark("9.9.8", PLENTY).0.unwrap();
    r.kernel("9.9.7");
    r.mark("9.9.7", PLENTY).0.unwrap();
    assert_eq!(r.backups(), vec!["9.9.7", "9.9.8"]);
    let removed = Mutex::new(Vec::new());
    rootops::remove_kernel_leftovers(&r.0, "9.9.8", false, &|l| removed.lock().unwrap().push(l)).expect_err("still the previous known-good kernel");
    // Make it an ordinary leftover by marking two newer ones.
    r.kernel("9.9.6");
    r.mark("9.9.6", PLENTY).0.unwrap();
    assert_eq!(r.backups(), vec!["9.9.6", "9.9.7"]);
    std::fs::create_dir_all(r.0.join("boot/kernel-backup/9.9.8")).unwrap();
    std::fs::write(r.0.join("boot/kernel-backup/9.9.8/vmlinuz-9.9.8"), b"old").unwrap();
    rootops::remove_kernel_leftovers(&r.0, "9.9.8", false, &|l| removed.lock().unwrap().push(l)).unwrap();
    assert!(!r.0.join("boot/vmlinuz-9.9.8").exists() && !r.0.join("boot/kernel-backup/9.9.8").exists());
    assert!(removed.lock().unwrap().iter().any(|l| l.contains("kernel-backup/9.9.8")));
}

// --- the FIFO that used to stall the helper ------------------------------------------------

fn within<T: Send + 'static>(secs: u64, f: impl FnOnce() -> T + Send + 'static) -> T {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(f());
    });
    rx.recv_timeout(std::time::Duration::from_secs(secs)).expect("the helper hung on a FIFO")
}

#[test]
fn a_fifo_where_a_config_file_belongs_does_not_stall_the_helper() {
    let r = Root::new("fifo");
    std::fs::create_dir_all(r.0.join("etc/portage/package.use")).unwrap();
    let pipe = r.0.join("etc/portage/package.use/pipe");
    let c = std::ffi::CString::new(pipe.to_str().unwrap()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o644) }, 0);
    let root = r.0.clone();
    let (write, delete) = within(5, move || {
        let server = Server::new(root);
        (
            server.write_file(None, "/etc/portage/package.use/pipe", b"x\n", false, Some(Expect::Absent)),
            server.write_file(None, "/etc/portage/package.use/pipe", b"x\n", false, None),
        )
    });
    assert!(write.is_err() && delete.is_err(), "{write:?} {delete:?}");
    // The request thread is free for the next request.
    let events = Arc::new(Mutex::new(Vec::new()));
    let sink: Sink = {
        let events = events.clone();
        Arc::new(move |e| events.lock().unwrap().push(e))
    };
    let root = r.0.clone();
    within(5, move || Server::new(root).handle(portagehub::protocol::Request::new(1, Op::Ping), sink));
    assert!(matches!(events.lock().unwrap().last(), Some(portagehub::protocol::Event::Ok { id: 1 })));
    let _ = Path::new("");
}
