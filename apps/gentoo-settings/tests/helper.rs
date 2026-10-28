//! The helper against scratch trees: an OpenRC and a systemd machine with fake
//! /proc, /sys and /etc. External programs are never run (scratch root).

use gentoo_settings::detect::{Init, System};
use gentoo_settings::plan::{self, Change};
use gentoo_settings::protocol::{Event, Request, VERSION};
use gentoo_settings::{history, managed, status};
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};

struct Tree(PathBuf);

impl Drop for Tree {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

impl Tree {
    fn new(name: &str, init: Init) -> Tree {
        let dir = std::env::temp_dir().join(format!("gentoo-settings-test-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let t = Tree(dir);
        match init {
            Init::OpenRc => {
                t.mkdir("run/openrc");
                t.put("etc/runlevels/boot/systemd-tmpfiles-setup", "");
                t.put("etc/runlevels/default/local", "");
            }
            Init::Systemd => {
                t.mkdir("run/systemd/system");
                t.put("usr/lib/systemd/system-generators/zram-generator", "");
            }
            Init::Unknown => {}
        }
        t.put("proc/sys/kernel/osrelease", "7.3.0-test\n");
        t.put("proc/sys/vm/swappiness", "60\n");
        t.put("proc/sys/vm/vfs_cache_pressure", "100\n");
        t.put("proc/sys/kernel/sysrq", "16\n");
        t.put("proc/swaps", "Filename\tType\tSize\tUsed\tPriority\n");
        t.put("proc/meminfo", "MemTotal:       16000000 kB\n");
        t.put("sys/kernel/mm/transparent_hugepage/defrag", "always defer defer+madvise [madvise] never\n");
        t.put("sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none", "511\n");
        t.put("sys/module/zram/initstate", "");
        t.put("usr/bin/systemd-tmpfiles", "");
        t.put("usr/bin/setpci", "");
        t.put("etc/group", "root:x:0:\naudio:x:18:edo\n");
        t
    }

    fn path(&self, rel: &str) -> PathBuf {
        self.0.join(rel.trim_start_matches('/'))
    }

    fn put(&self, rel: &str, text: &str) {
        let p = self.path(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, text).unwrap();
    }

    fn mkdir(&self, rel: &str) {
        std::fs::create_dir_all(self.path(rel)).unwrap();
    }

    fn read(&self, rel: &str) -> Option<String> {
        std::fs::read_to_string(self.path(rel)).ok()
    }

    fn sys(&self) -> System {
        System::detect_at(&self.0)
    }

    fn run(&self, changes: Vec<Change>) -> Vec<Event> {
        self.run_req(&Request { version: VERSION.into(), label: "test".into(), changes })
    }

    fn run_req(&self, req: &Request) -> Vec<Event> {
        let mut child = Command::new(env!("CARGO_BIN_EXE_gentoo-settings-helper"))
            .env("GENTOO_SETTINGS_ROOT", &self.0)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        child.stdin.take().unwrap().write_all(serde_json::to_string(req).unwrap().as_bytes()).unwrap();
        let out = child.wait_with_output().unwrap();
        String::from_utf8_lossy(&out.stdout).lines().map(|l| serde_json::from_str(l).unwrap_or_else(|e| panic!("bad event {l}: {e}"))).collect()
    }
}

fn on(id: &str, value: Option<&str>) -> Change {
    Change { id: id.into(), enable: true, value: value.map(String::from) }
}

fn off(id: &str) -> Change {
    Change { id: id.into(), enable: false, value: None }
}

fn done_ok(evs: &[Event]) -> bool {
    evs.iter().any(|e| matches!(e, Event::Done { ok: true, .. }))
}

fn errors(evs: &[Event]) -> Vec<String> {
    evs.iter()
        .filter_map(|e| match e {
            Event::Error { msg } => Some(msg.clone()),
            _ => None,
        })
        .collect()
}

#[test]
fn detects_init_systems() {
    assert_eq!(Tree::new("init-openrc", Init::OpenRc).sys().init, Init::OpenRc);
    assert_eq!(Tree::new("init-systemd", Init::Systemd).sys().init, Init::Systemd);
    let t = Tree::new("init-installed", Init::Unknown);
    t.put("usr/lib/systemd/systemd", "");
    assert_eq!(t.sys().init, Init::Systemd);
    t.put("sbin/openrc-run", "");
    assert_eq!(t.sys().init, Init::OpenRc, "both installed, /sbin/init not systemd");
}

#[test]
fn sysctl_and_sysfs_apply_and_revert() {
    let t = Tree::new("sysctl", Init::OpenRc);
    let evs = t.run(vec![on("vm.swappiness", Some("100")), on("thp-defrag", None), on("kernel.sysrq", None)]);
    assert!(done_ok(&evs), "{evs:?}");
    let conf = t.read(managed::SYSCTL).unwrap();
    assert!(conf.contains("# >>> gentoo-settings vm.swappiness = 100\n"), "{conf}");
    assert!(conf.contains("vm.swappiness = 100\n"));
    assert!(conf.contains("kernel.sysrq = 1\n"));
    // Catalog order: swappiness (memory) before sysrq (kernel).
    assert!(conf.find("vm.swappiness").unwrap() < conf.find("kernel.sysrq").unwrap());
    assert!(t.read(managed::TMPFILES).unwrap().contains("w! /sys/kernel/mm/transparent_hugepage/defrag - - - - defer+madvise"));
    // sysfs written now, old value remembered; sysctl --system is skipped on a scratch root.
    assert_eq!(t.read("sys/kernel/mm/transparent_hugepage/defrag").unwrap(), "defer+madvise");
    assert!(evs.iter().any(|e| matches!(e, Event::Command { text, skipped: true, .. } if text == "sysctl -q --system")));
    let state = t.read("var/lib/gentoo-settings/state.json").unwrap();
    assert!(state.contains("\"/proc/sys/vm/swappiness\": \"60\""), "{state}");
    assert!(state.contains("\"/sys/kernel/mm/transparent_hugepage/defrag\": \"madvise\""));

    // Status reads it back.
    let scan = status::scan(&t.sys());
    let s = scan.get("vm.swappiness").unwrap();
    assert_eq!(s.installed.as_ref().unwrap().value, "100");
    assert!(!s.installed.as_ref().unwrap().modified);

    // Applying the same again changes no file.
    let p = plan::plan(&t.sys(), &[on("vm.swappiness", Some("100"))]).unwrap();
    assert!(p.files.is_empty(), "{:?}", p.files);

    // Disable: files go away, values come back, history records both.
    let evs = t.run(vec![off("vm.swappiness"), off("thp-defrag"), off("kernel.sysrq")]);
    assert!(done_ok(&evs), "{evs:?}");
    assert!(t.read(managed::SYSCTL).is_none());
    assert!(t.read(managed::TMPFILES).is_none());
    assert_eq!(t.read("sys/kernel/mm/transparent_hugepage/defrag").unwrap(), "madvise");
    assert_eq!(t.read("proc/sys/vm/swappiness").unwrap(), "60");
    let h = history::load(&t.sys());
    assert_eq!(h.len(), 2);
    assert_eq!(h[0].undo[0], on("vm.swappiness", Some("100")), "undo of the disable re-enables with the old value");
    assert_eq!(h[1].undo[0], off("vm.swappiness"));
    let backup = h[0].backup.clone().unwrap();
    assert!(t.path(&backup).join("etc/sysctl.d/70-gentoo-settings.conf").is_file());
}

#[test]
fn foreign_lines_survive() {
    let t = Tree::new("foreign", Init::OpenRc);
    assert!(done_ok(&t.run(vec![on("vm.vfs_cache_pressure", None)])));
    let p = managed::SYSCTL.trim_start_matches('/');
    let mut text = t.read(p).unwrap();
    text.push_str("\n# mine\nnet.core.somaxconn = 8192\n");
    t.put(p, &text);
    assert!(done_ok(&t.run(vec![off("vm.vfs_cache_pressure")])));
    let after = t.read(p).expect("file with foreign text is kept");
    assert!(after.contains("net.core.somaxconn = 8192"));
    assert!(!after.contains("vfs_cache_pressure"));
}

#[test]
fn hand_edited_block_is_modified() {
    let t = Tree::new("modified", Init::OpenRc);
    assert!(done_ok(&t.run(vec![on("vm.swappiness", None)])));
    let p = managed::SYSCTL.trim_start_matches('/');
    t.put(p, &t.read(p).unwrap().replace("\nvm.swappiness = 100\n", "\nvm.swappiness = 7\n"));
    let scan = status::scan(&t.sys());
    assert_eq!(scan.get("vm.swappiness").unwrap().badge, status::Badge::Modified);
}

#[test]
fn zram_per_init_system() {
    let o = Tree::new("zram-openrc", Init::OpenRc);
    let evs = o.run(vec![on("zram", Some("lz4"))]);
    assert!(done_ok(&evs), "{evs:?}");
    let script = o.read(managed::OPENRC_ZRAM).unwrap();
    assert!(script.starts_with("#!/sbin/openrc-run\n"));
    assert!(script.contains("echo lz4 > /sys/block/zram0/comp_algorithm"));
    assert!(o.read(managed::UDEV_ZRAM).unwrap().contains("SYSCTL{vm.swappiness}=\"150\""));
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(std::fs::metadata(o.path(managed::OPENRC_ZRAM)).unwrap().permissions().mode() & 0o777, 0o755);
    let texts: Vec<String> = evs
        .iter()
        .filter_map(|e| match e {
            Event::Command { text, .. } => Some(text.clone()),
            _ => None,
        })
        .collect();
    assert!(texts.contains(&"rc-update add gentoo-settings-zram boot".to_string()), "{texts:?}");
    assert!(o.read(managed::ZRAM_GENERATOR).is_none());

    let s = Tree::new("zram-systemd", Init::Systemd);
    let evs = s.run(vec![on("zram", None)]);
    assert!(done_ok(&evs), "{evs:?}");
    assert!(s.read(managed::ZRAM_GENERATOR).unwrap().contains("compression-algorithm = zstd"));
    assert!(s.read(managed::OPENRC_ZRAM).is_none());
    let evs = s.run(vec![off("zram")]);
    assert!(evs.iter().any(|e| matches!(e, Event::Command { text, .. } if text == "systemctl stop systemd-zram-setup@zram0.service")));
    assert!(s.read(managed::ZRAM_GENERATOR).is_none());
}

#[test]
fn systemd_only_tweaks_are_refused_on_openrc() {
    let o = Tree::new("only-openrc", Init::OpenRc);
    let evs = o.run(vec![on("systemd-timeouts", None)]);
    assert!(!done_ok(&evs));
    assert!(errors(&evs)[0].contains("systemd only"), "{evs:?}");
    let s = Tree::new("only-systemd", Init::Systemd);
    assert!(done_ok(&s.run(vec![on("systemd-timeouts", None), on("nofile-limits", None)])));
    let conf = s.read(managed::SYSTEM_CONF).unwrap();
    assert!(conf.contains("DefaultTimeoutStopSec=10s") && conf.contains("DefaultLimitNOFILE=2048:2097152"), "{conf}");
    assert!(s.read(managed::LIMITS).is_none(), "systemd uses system.conf.d, not limits.d");
    let o2 = Tree::new("nofile-openrc", Init::OpenRc);
    assert!(done_ok(&o2.run(vec![on("nofile-limits", None)])));
    assert!(o2.read(managed::LIMITS).unwrap().contains("* hard nofile 1048576"));
}

#[test]
fn bad_requests_are_refused() {
    let t = Tree::new("bad", Init::OpenRc);
    assert!(errors(&t.run(vec![on("no-such-tweak", None)]))[0].contains("unknown tweak"));
    assert!(errors(&t.run(vec![on("vm.swappiness", Some("999"))]))[0].contains("between 0 and 200"));
    assert!(errors(&t.run(vec![on("vm.swappiness", Some("1\nkernel.modprobe = /tmp/x"))]))[0].contains("invalid value"));
    let old = t.run_req(&Request { version: "0.0.0".into(), label: String::new(), changes: vec![] });
    assert!(errors(&old)[0].contains("reinstall"));
    assert!(t.read(managed::SYSCTL).is_none());
    // Not root and no scratch root: refuses.
    let out = Command::new(env!("CARGO_BIN_EXE_gentoo-settings-helper")).env_remove("GENTOO_SETTINGS_ROOT").stdin(Stdio::null()).output().unwrap();
    assert!(!out.status.success());
}

#[test]
fn every_tweak_builds_into_managed_files() {
    for (name, init) in [("all-openrc", Init::OpenRc), ("all-systemd", Init::Systemd)] {
        let t = Tree::new(name, init);
        let sys = t.sys();
        for tw in gentoo_settings::catalog::CATALOG {
            let imp = (tw.build)(&tw.ctx(&sys, tw.default));
            for (path, body) in &imp.blocks {
                assert!(managed::spec(path).is_some(), "{} writes unmanaged {path}", tw.id);
                assert!(!body.lines().any(|l| l.starts_with(managed::BEGIN) || l.starts_with(managed::END)), "{} body has markers", tw.id);
            }
            if tw.only == Some(Init::Systemd) && init == Init::OpenRc {
                continue;
            }
            for c in imp.on_enable.iter().chain(&imp.before_disable).chain(&imp.after_disable) {
                if let plan::Cmd::Exec { prog: plan::Prog::RcUpdate | plan::Prog::RcService, .. } = c {
                    assert_eq!(init, Init::OpenRc, "{} uses OpenRC tools on systemd", tw.id);
                }
                if let plan::Cmd::Exec { prog: plan::Prog::Systemctl, .. } = c {
                    assert_eq!(init, Init::Systemd, "{} uses systemctl on OpenRC", tw.id);
                }
            }
        }
    }
}
