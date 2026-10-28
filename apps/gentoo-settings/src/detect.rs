//! What kind of system this is: init system, hardware, tools and kernel
//! features. Everything is read below `root` (normally `/`) so tests can run
//! against a scratch tree with fake `/proc`, `/sys` and `/etc`.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Init {
    OpenRc,
    Systemd,
    Unknown,
}

impl Init {
    pub fn label(&self) -> &'static str {
        match self {
            Init::OpenRc => "OpenRC",
            Init::Systemd => "systemd",
            Init::Unknown => "unknown init",
        }
    }

    pub fn parse(s: &str) -> Option<Init> {
        match s.to_ascii_lowercase().as_str() {
            "openrc" => Some(Init::OpenRc),
            "systemd" => Some(Init::Systemd),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CpuVendor {
    Intel,
    Amd,
    Other,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GpuVendor {
    Nvidia,
    Amd,
    Intel,
    Other,
}

impl GpuVendor {
    pub fn label(&self) -> &'static str {
        match self {
            GpuVendor::Nvidia => "NVIDIA",
            GpuVendor::Amd => "AMD",
            GpuVendor::Intel => "Intel",
            GpuVendor::Other => "Other",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Gpu {
    pub vendor: GpuVendor,
    /// Bound kernel driver ("nvidia", "amdgpu", "radeon", "i915", "xe" …), empty when none.
    pub driver: String,
    pub slot: String,
    pub device_id: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DiskKind {
    Nvme,
    Sata,
    Mmc,
    Other,
}

impl DiskKind {
    pub fn label(&self) -> &'static str {
        match self {
            DiskKind::Nvme => "NVMe",
            DiskKind::Sata => "SATA/SCSI",
            DiskKind::Mmc => "eMMC/SD",
            DiskKind::Other => "Other",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Disk {
    pub name: String,
    pub kind: DiskKind,
    pub rotational: bool,
    pub removable: bool,
    /// Attached over USB (sticks often claim to be rotational).
    pub usb: bool,
    pub model: String,
    pub size_bytes: u64,
    /// Selected I/O scheduler ("none" when the queue has none).
    pub scheduler: Option<String>,
    pub schedulers: Vec<String>,
}

impl Disk {
    /// Device class as CachyOS's 60-ioschedulers.rules sees it.
    pub fn class(&self) -> DiskClass {
        match (self.kind, self.rotational) {
            (DiskKind::Nvme, _) => DiskClass::Nvme,
            (_, true) => DiskClass::Hdd,
            _ => DiskClass::Ssd,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DiskClass {
    Hdd,
    Ssd,
    Nvme,
}

#[derive(Debug, Clone)]
pub struct System {
    pub root: PathBuf,
    pub init: Init,
    /// How the init system was recognised (shown in the UI).
    pub init_reason: String,
    pub kernel: String,
    pub cpu_model: String,
    pub cpu_vendor: CpuVendor,
    pub cpu_flags: Vec<String>,
    pub threads: usize,
    pub mem_total_kb: u64,
    pub gpus: Vec<Gpu>,
    pub battery: bool,
    pub wifi: Vec<String>,
    pub disks: Vec<Disk>,
    /// SCSI hosts that offer `link_power_management_policy`.
    pub sata_hosts: Vec<String>,
    pub profile: String,
    pub elogind: bool,
}

/// Selected value of a sysfs "choice" file: `always [madvise] never` → `madvise`.
pub fn selected(text: &str) -> String {
    let t = text.trim();
    if let Some(a) = t.find('[')
        && let Some(b) = t[a..].find(']')
    {
        return t[a + 1..a + b].to_string();
    }
    t.to_string()
}

/// Collapse runs of whitespace (`/proc/sys/kernel/printk` uses tabs).
pub fn norm_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

impl System {
    pub fn detect() -> System {
        System::detect_at(Path::new("/"))
    }

    pub fn detect_at(root: &Path) -> System {
        let mut s = System {
            root: root.to_path_buf(),
            init: Init::Unknown,
            init_reason: String::new(),
            kernel: String::new(),
            cpu_model: String::new(),
            cpu_vendor: CpuVendor::Other,
            cpu_flags: Vec::new(),
            threads: 0,
            mem_total_kb: 0,
            gpus: Vec::new(),
            battery: false,
            wifi: Vec::new(),
            disks: Vec::new(),
            sata_hosts: Vec::new(),
            profile: String::new(),
            elogind: false,
        };
        let (init, why) = s.detect_init();
        s.init = init;
        s.init_reason = why;
        s.kernel = s.read("/proc/sys/kernel/osrelease").unwrap_or_default();
        s.read_cpu();
        s.mem_total_kb = s
            .read("/proc/meminfo")
            .and_then(|m| m.lines().find(|l| l.starts_with("MemTotal:")).and_then(|l| l.split_whitespace().nth(1)?.parse().ok()))
            .unwrap_or(0);
        s.gpus = s.read_gpus();
        s.battery = s.read_battery();
        s.wifi = s.list("/sys/class/ieee80211");
        s.disks = s.read_disks();
        s.sata_hosts =
            s.list("/sys/class/scsi_host").into_iter().filter(|h| s.exists(&format!("/sys/class/scsi_host/{h}/link_power_management_policy"))).collect();
        s.profile = std::fs::read_link(s.path("/etc/portage/make.profile"))
            .ok()
            .map(|p| {
                let p = p.to_string_lossy().to_string();
                p.split_once("/profiles/").map(|x| x.1.to_string()).unwrap_or(p)
            })
            .unwrap_or_default();
        s.elogind = s.has_tool("elogind-inhibit") || s.exists("/run/elogind.pid");
        s
    }

    /// The init system. A running system is recognised by what PID 1 left in
    /// /run; a chroot or an unbooted tree by what is installed.
    fn detect_init(&self) -> (Init, String) {
        if self.is_dir("/run/systemd/system") {
            return (Init::Systemd, "/run/systemd/system exists (systemd is PID 1)".into());
        }
        if self.is_dir("/run/openrc") {
            return (Init::OpenRc, "/run/openrc exists (OpenRC manages services)".into());
        }
        let comm = self.read("/proc/1/comm").unwrap_or_default();
        match comm.as_str() {
            "systemd" => return (Init::Systemd, "PID 1 is systemd".into()),
            "openrc-init" => return (Init::OpenRc, "PID 1 is openrc-init".into()),
            _ => {}
        }
        let openrc = self.exists("/sbin/openrc-run") || self.exists("/usr/sbin/openrc-run") || self.exists("/usr/bin/openrc-run");
        let systemd = self.exists("/usr/lib/systemd/systemd") || self.exists("/lib/systemd/systemd");
        // /sbin/init pointing at systemd decides between both installed.
        if let Ok(t) = std::fs::read_link(self.path("/sbin/init"))
            && t.to_string_lossy().contains("systemd")
        {
            return (Init::Systemd, "/sbin/init links to systemd".into());
        }
        match (openrc, systemd) {
            (true, false) => (Init::OpenRc, "openrc-run is installed".into()),
            (false, true) => (Init::Systemd, "systemd is installed".into()),
            (true, true) => (Init::OpenRc, "OpenRC and systemd are installed; /sbin/init is not systemd".into()),
            _ => (Init::Unknown, "neither OpenRC nor systemd was found".into()),
        }
    }

    fn read_cpu(&mut self) {
        let info = self.read("/proc/cpuinfo").unwrap_or_default();
        let mut threads = 0;
        for l in info.lines() {
            let Some((k, v)) = l.split_once(':') else { continue };
            match k.trim() {
                "processor" => threads += 1,
                "model name" if self.cpu_model.is_empty() => self.cpu_model = v.trim().to_string(),
                "vendor_id" if self.cpu_vendor == CpuVendor::Other => {
                    self.cpu_vendor = match v.trim() {
                        "GenuineIntel" => CpuVendor::Intel,
                        "AuthenticAMD" => CpuVendor::Amd,
                        _ => CpuVendor::Other,
                    }
                }
                "flags" if self.cpu_flags.is_empty() => self.cpu_flags = v.split_whitespace().map(String::from).collect(),
                _ => {}
            }
        }
        self.threads = threads;
    }

    fn read_gpus(&self) -> Vec<Gpu> {
        let mut out = Vec::new();
        for dev in self.list("/sys/bus/pci/devices") {
            let base = format!("/sys/bus/pci/devices/{dev}");
            let class = self.read(&format!("{base}/class")).unwrap_or_default();
            if !class.starts_with("0x03") {
                continue;
            }
            let vendor = match self.read(&format!("{base}/vendor")).unwrap_or_default().as_str() {
                "0x10de" => GpuVendor::Nvidia,
                "0x1002" => GpuVendor::Amd,
                "0x8086" => GpuVendor::Intel,
                _ => GpuVendor::Other,
            };
            let driver = std::fs::read_link(self.path(&format!("{base}/driver")))
                .ok()
                .and_then(|p| p.file_name().map(|n| n.to_string_lossy().to_string()))
                .unwrap_or_default();
            out.push(Gpu { vendor, driver, slot: dev, device_id: self.read(&format!("{base}/device")).unwrap_or_default() });
        }
        out
    }

    fn read_battery(&self) -> bool {
        self.list("/sys/class/power_supply").iter().any(|p| {
            let base = format!("/sys/class/power_supply/{p}");
            // Wireless mice and pads report scope=Device; only system batteries count.
            self.read(&format!("{base}/type")).as_deref() == Some("Battery") && self.read(&format!("{base}/scope")).as_deref() != Some("Device")
        })
    }

    fn read_disks(&self) -> Vec<Disk> {
        let mut out = Vec::new();
        for name in self.list("/sys/block") {
            let kind = if name.starts_with("nvme") {
                DiskKind::Nvme
            } else if name.starts_with("sd") {
                DiskKind::Sata
            } else if name.starts_with("mmcblk") {
                DiskKind::Mmc
            } else {
                continue; // loop, zram, dm, md, sr …
            };
            let base = format!("/sys/block/{name}");
            let sched = self.read(&format!("{base}/queue/scheduler"));
            let schedulers =
                sched.as_deref().map(|s| s.split_whitespace().map(|x| x.trim_matches(|c| c == '[' || c == ']').to_string()).collect()).unwrap_or_default();
            out.push(Disk {
                kind,
                rotational: self.read(&format!("{base}/queue/rotational")).as_deref() == Some("1"),
                removable: self.read(&format!("{base}/removable")).as_deref() == Some("1"),
                usb: std::fs::read_link(self.path(&base)).map(|t| t.to_string_lossy().contains("/usb")).unwrap_or(false),
                model: self.read(&format!("{base}/device/model")).unwrap_or_default(),
                size_bytes: self.read(&format!("{base}/size")).and_then(|s| s.parse::<u64>().ok()).unwrap_or(0) * 512,
                scheduler: sched.as_deref().map(selected),
                schedulers,
                name,
            });
        }
        out.sort_by(|a, b| a.name.cmp(&b.name));
        out
    }

    /// `abs` (an absolute path on the real system) below the root.
    pub fn path(&self, abs: &str) -> PathBuf {
        self.root.join(abs.trim_start_matches('/'))
    }

    pub fn read(&self, abs: &str) -> Option<String> {
        std::fs::read_to_string(self.path(abs)).ok().map(|s| s.trim().to_string())
    }

    pub fn read_raw(&self, abs: &str) -> std::io::Result<String> {
        std::fs::read_to_string(self.path(abs))
    }

    pub fn exists(&self, abs: &str) -> bool {
        self.path(abs).exists()
    }

    pub fn is_dir(&self, abs: &str) -> bool {
        self.path(abs).is_dir()
    }

    /// Sorted entry names of a directory (empty when missing).
    pub fn list(&self, abs: &str) -> Vec<String> {
        let mut v: Vec<String> =
            std::fs::read_dir(self.path(abs)).map(|d| d.flatten().map(|e| e.file_name().to_string_lossy().to_string()).collect()).unwrap_or_default();
        v.sort();
        v
    }

    /// Absolute path of a program in the standard directories.
    pub fn tool(&self, name: &str) -> Option<String> {
        crate::PROGRAM_DIRS.iter().map(|d| format!("{d}/{name}")).find(|p| self.path(p).is_file())
    }

    pub fn has_tool(&self, name: &str) -> bool {
        self.tool(name).is_some()
    }

    /// Is `cat/pn` installed (from the Portage VDB)?
    pub fn has_pkg(&self, cat_pn: &str) -> bool {
        let Some((cat, pn)) = cat_pn.split_once('/') else { return false };
        self.list(&format!("/var/db/pkg/{cat}"))
            .iter()
            .any(|e| e.strip_prefix(pn).and_then(|r| r.strip_prefix('-')).is_some_and(|v| v.starts_with(|c: char| c.is_ascii_digit())))
    }

    /// Is `module` available for the running kernel (loaded, built in or on disk)?
    pub fn has_module(&self, module: &str) -> bool {
        if self.exists(&format!("/sys/module/{module}")) {
            return true;
        }
        let base = format!("/lib/modules/{}", self.kernel);
        let needle = format!("/{module}.ko");
        for f in ["modules.dep", "modules.builtin"] {
            if let Ok(t) = self.read_raw(&format!("{base}/{f}"))
                && t.lines().any(|l| l.split(':').next().is_some_and(|p| p.contains(&needle)))
            {
                return true;
            }
        }
        false
    }

    pub fn module_loaded(&self, module: &str) -> bool {
        self.read_raw("/proc/modules").map(|t| t.lines().any(|l| l.split_whitespace().next() == Some(module))).unwrap_or(false)
    }

    pub fn sysctl_path(key: &str) -> String {
        format!("/proc/sys/{}", key.replace('.', "/"))
    }

    pub fn has_sysctl(&self, key: &str) -> bool {
        self.exists(&System::sysctl_path(key))
    }

    pub fn has_gpu(&self, v: GpuVendor) -> bool {
        self.gpus.iter().any(|g| g.vendor == v)
    }

    /// Is an OpenRC service in a runlevel?
    pub fn in_runlevel(&self, service: &str) -> bool {
        self.list("/etc/runlevels").iter().any(|rl| self.exists(&format!("/etc/runlevels/{rl}/{service}")))
    }

    /// Is something applying /etc/tmpfiles.d at boot?
    pub fn tmpfiles_at_boot(&self) -> bool {
        match self.init {
            Init::Systemd => true,
            Init::OpenRc => ["systemd-tmpfiles-setup", "opentmpfiles-setup"].iter().any(|s| self.in_runlevel(s)),
            Init::Unknown => false,
        }
    }

    /// Supported x86-64 micro-architecture levels, from the CPU flags.
    pub fn x86_64_levels(&self) -> Vec<&'static str> {
        let has = |fs: &[&str]| fs.iter().all(|f| self.cpu_flags.iter().any(|c| c == f));
        let mut v = Vec::new();
        if !self.cpu_flags.is_empty() {
            v.push("x86-64");
        }
        if has(&["cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3"]) {
            v.push("x86-64-v2");
            if has(&["avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave"]) {
                v.push("x86-64-v3");
                if has(&["avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"]) {
                    v.push("x86-64-v4");
                }
            }
        }
        v
    }

    /// Group id of a group in /etc/group.
    pub fn gid_of(&self, group: &str) -> Option<u32> {
        self.read_raw("/etc/group").ok()?.lines().find_map(|l| {
            let mut it = l.split(':');
            (it.next()? == group).then(|| it.nth(1)?.parse().ok()).flatten()
        })
    }

    pub fn mem_gib(&self) -> f64 {
        self.mem_total_kb as f64 / 1024.0 / 1024.0
    }
}

/// Init system override for previewing the other implementation in the GUI
/// (`GENTOO_SETTINGS_INIT=systemd`). The helper never honours it as root.
pub fn init_override() -> Option<Init> {
    std::env::var("GENTOO_SETTINGS_INIT").ok().and_then(|v| Init::parse(&v))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selected_values() {
        assert_eq!(selected("always defer [defer+madvise] madvise never"), "defer+madvise");
        assert_eq!(selected("[none] mq-deadline kyber"), "none");
        assert_eq!(selected("42\n"), "42");
        assert_eq!(norm_ws("3\t3\t3\t3"), "3 3 3 3");
    }
}
