//! Installed kernels, their boot files, known-good state, the bootloader in
//! use and the NVIDIA kernel module for each kernel — plus the boot check.

use super::vdb::Vdb;
use super::version::Version;
use portagehub::protocol::{BootAction, BootScan};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct NvidiaModule {
    pub path: PathBuf,
    pub version: String,
}

#[derive(Debug, Clone)]
pub struct KernelEntry {
    pub kver: String,
    /// Owning package (cpv) from its CONTENTS.
    pub pkg: Option<String>,
    pub image: Option<String>,
    pub image_mtime: i64,
    pub initramfs: Option<String>,
    pub old_files: Vec<String>,
    pub modules: bool,
    pub sources: bool,
    pub nvidia: Option<NvidiaModule>,
}

impl KernelEntry {
    pub fn bootable(&self) -> bool {
        self.image.is_some() && self.modules
    }

    fn sort_key(&self) -> Option<Version> {
        Version::parse(self.kver.split('-').next().unwrap_or(&self.kver))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Bootloader {
    Grub,
    SystemdBoot,
    Lilo,
    Refind,
    Limine,
    EfiStub,
}

impl Bootloader {
    pub fn name(&self) -> &'static str {
        match self {
            Bootloader::Grub => "GRUB",
            Bootloader::SystemdBoot => "systemd-boot",
            Bootloader::Lilo => "LILO",
            Bootloader::Refind => "rEFInd",
            Bootloader::Limine => "Limine",
            Bootloader::EfiStub => "EFI stub / UKI",
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct BootInfo {
    pub efi: bool,
    /// efibootmgr's BootCurrent entry (label, loader path).
    pub current: Option<(String, String)>,
    pub detected: Vec<Bootloader>,
    pub primary: Option<Bootloader>,
    pub installkernel_use: Vec<String>,
    pub initramfs_tool: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub struct NvidiaInfo {
    pub gpu: bool,
    /// Installed x11-drivers/nvidia-drivers version (without revision).
    pub drivers: Option<String>,
    pub dist_kernel: bool,
    pub fallback_script: Option<PathBuf>,
}

#[derive(Debug, Clone, Default)]
pub struct KernelState {
    pub running: String,
    /// Newest first.
    pub kernels: Vec<KernelEntry>,
    /// Kernel packages whose files are all gone (depclean candidates).
    pub orphan_packages: Vec<String>,
    pub state_dir: PathBuf,
    pub last_good: Option<String>,
    pub previous_good: Option<String>,
    pub eselected: Option<String>,
    pub boot: BootInfo,
    pub nvidia: NvidiaInfo,
    pub keep_set: String,
    pub keep_set_atoms: Vec<String>,
    pub keep_set_exists: bool,
    pub keep_set_in_world: bool,
    pub local_tools: Vec<PathBuf>,
}

impl KernelState {
    pub fn get(&self, kver: &str) -> Option<&KernelEntry> {
        self.kernels.iter().find(|k| k.kver == kver)
    }

    pub fn newest_bootable(&self) -> Option<&KernelEntry> {
        self.kernels.iter().find(|k| k.bootable())
    }

    pub fn is_protected(&self, kver: &str) -> bool {
        kver == self.running || self.last_good.as_deref() == Some(kver) || self.previous_good.as_deref() == Some(kver)
    }

    pub fn nvidia_status(&self, k: &KernelEntry) -> Option<NvStatus> {
        let want = self.nvidia.drivers.as_ref()?;
        if !self.nvidia.gpu || !k.modules {
            return None;
        }
        Some(match &k.nvidia {
            None => NvStatus::Missing,
            Some(m) if m.version != *want => NvStatus::Mismatch(m.version.clone()),
            Some(_) => NvStatus::Ok,
        })
    }

    /// Kernels that may be removed while keeping `keep_newest` newest bootable ones
    /// plus the running and known-good kernels.
    pub fn removable(&self, keep_newest: usize) -> Vec<&KernelEntry> {
        let mut kept = 0;
        let mut out = Vec::new();
        for k in &self.kernels {
            if self.is_protected(&k.kver) {
                continue;
            }
            if k.bootable() && kept < keep_newest {
                kept += 1;
                continue;
            }
            out.push(k);
        }
        out
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum NvStatus {
    Ok,
    Missing,
    Mismatch(String),
}

fn read_trim(p: &Path) -> String {
    std::fs::read_to_string(p).map(|s| s.trim().to_string()).unwrap_or_default()
}

fn mtime(p: &Path) -> i64 {
    std::fs::metadata(p).and_then(|m| m.modified()).ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs() as i64).unwrap_or(0)
}

fn find_nvidia_ko(dir: &Path, depth: usize) -> Option<PathBuf> {
    if depth > 6 {
        return None;
    }
    let mut subdirs = Vec::new();
    for e in std::fs::read_dir(dir).ok()?.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        let ft = e.file_type().ok()?;
        if ft.is_dir() {
            if name != "build" && name != "source" {
                subdirs.push(e.path());
            }
        } else if name == "nvidia.ko" || name.starts_with("nvidia.ko.") {
            return Some(e.path());
        }
    }
    subdirs.into_iter().find_map(|d| find_nvidia_ko(&d, depth + 1))
}

fn modinfo_version(path: &Path) -> Option<String> {
    let out = std::process::Command::new("modinfo").args(["-F", "version"]).arg(path).output().ok()?;
    let v = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (!v.is_empty()).then_some(v)
}

pub fn default_state_dir() -> PathBuf {
    if Path::new("/var/lib/kernel-fallback").is_dir() { PathBuf::from("/var/lib/kernel-fallback") } else { PathBuf::from("/var/lib/portagehub/kernels") }
}

fn detect_gpu() -> bool {
    for e in std::fs::read_dir("/sys/bus/pci/devices").into_iter().flatten().flatten() {
        let vendor = read_trim(&e.path().join("vendor"));
        let class = read_trim(&e.path().join("class"));
        if vendor == "0x10de" && class.starts_with("0x03") {
            return true;
        }
    }
    false
}

fn detect_boot(vdb: &Vdb) -> BootInfo {
    let mut b = BootInfo { efi: Path::new("/sys/firmware/efi").is_dir(), ..Default::default() };
    let installed = |k: &str| vdb.is_installed(k);
    let use_of = |k: &str| -> Vec<String> { vdb.installed(k).last().map(|p| p.read("USE").split_whitespace().map(String::from).collect()).unwrap_or_default() };
    if installed("sys-boot/grub") {
        b.detected.push(Bootloader::Grub);
    }
    let sd_boot = ["sys-apps/systemd", "sys-apps/systemd-utils"].iter().any(|k| use_of(k).iter().any(|u| u == "boot"))
        || Path::new("/boot/loader/loader.conf").exists()
        || Path::new("/efi/loader").is_dir();
    if sd_boot {
        b.detected.push(Bootloader::SystemdBoot);
    }
    if installed("sys-boot/lilo") {
        b.detected.push(Bootloader::Lilo);
    }
    if installed("sys-boot/refind") {
        b.detected.push(Bootloader::Refind);
    }
    if installed("sys-boot/limine") {
        b.detected.push(Bootloader::Limine);
    }
    let ik = use_of("sys-kernel/installkernel");
    if ik.iter().any(|u| u == "efistub" || u == "uki") {
        b.detected.push(Bootloader::EfiStub);
    }
    b.initramfs_tool = ["dracut", "ugrd"].iter().find(|t| ik.iter().any(|u| u == *t)).map(|s| s.to_string());
    b.installkernel_use = ik;
    if b.efi
        && let Ok(o) = std::process::Command::new("efibootmgr").arg("-v").output()
    {
        let text = String::from_utf8_lossy(&o.stdout).to_string();
        let cur = text.lines().find_map(|l| l.strip_prefix("BootCurrent: ")).map(|s| s.trim().to_string());
        if let Some(c) = cur {
            let prefix = format!("Boot{c}");
            if let Some(line) = text.lines().find(|l| l.starts_with(&prefix)) {
                let rest = line[prefix.len()..].trim_start_matches('*').trim();
                let (label, path) = match rest.split_once('\t') {
                    Some((l, p)) => (l.trim().to_string(), p.to_string()),
                    None => (rest.to_string(), String::new()),
                };
                let file = path.split(")/").last().unwrap_or("").to_string();
                b.current = Some((label, file));
            }
        }
    }
    let cur = b.current.as_ref().map(|c| c.1.to_lowercase()).unwrap_or_default();
    b.primary = if cur.contains("grub") {
        Some(Bootloader::Grub)
    } else if cur.contains("systemd-boot") || cur.contains("systemd\\") {
        Some(Bootloader::SystemdBoot)
    } else if cur.contains("refind") {
        Some(Bootloader::Refind)
    } else if cur.contains("limine") {
        Some(Bootloader::Limine)
    } else if cur.contains("vmlinuz") || cur.contains("\\linux\\") {
        Some(Bootloader::EfiStub)
    } else {
        b.detected.first().copied()
    };
    b
}

/// Read everything that does not need root.
pub fn load(vdb: &Vdb, keep_set: &str) -> KernelState {
    let mut st = KernelState {
        running: read_trim(Path::new("/proc/sys/kernel/osrelease")),
        state_dir: default_state_dir(),
        keep_set: keep_set.to_string(),
        ..Default::default()
    };
    st.last_good = Some(read_trim(&st.state_dir.join("last-good"))).filter(|s| !s.is_empty());
    st.previous_good = Some(read_trim(&st.state_dir.join("previous-good"))).filter(|s| !s.is_empty());
    st.eselected = std::fs::read_link("/usr/src/linux").ok().map(|t| t.to_string_lossy().trim_start_matches("linux-").to_string());

    let mut map: BTreeMap<String, KernelEntry> = BTreeMap::new();
    let entry = |m: &mut BTreeMap<String, KernelEntry>, k: &str| -> () {
        m.entry(k.to_string()).or_insert_with(|| KernelEntry {
            kver: k.to_string(),
            pkg: None,
            image: None,
            image_mtime: 0,
            initramfs: None,
            old_files: Vec::new(),
            modules: false,
            sources: false,
            nvidia: None,
        });
    };
    // Packages → kernel versions from CONTENTS.
    for p in vdb.pkgs.iter().filter(|p| p.cat == "sys-kernel") {
        let contents = std::fs::read_to_string(p.dir.join("CONTENTS")).unwrap_or_default();
        let mut kvers: Vec<String> = Vec::new();
        for l in contents.lines() {
            let k =
                l.strip_prefix("dir /lib/modules/").filter(|r| !r.contains('/')).or_else(|| l.strip_prefix("dir /usr/src/linux-").filter(|r| !r.contains('/')));
            if let Some(k) = k
                && !kvers.iter().any(|x| x == k)
            {
                kvers.push(k.to_string());
            }
        }
        if kvers.is_empty() {
            if contents.contains("/usr/src/linux-")
                || contents.contains("/lib/modules/")
                || p.pn.ends_with("-kernel")
                || p.pn.ends_with("-kernel-bin")
                || p.pn.ends_with("-sources")
            {
                // Kernel package whose files are gone (or not yet mapped).
                let any_file = contents.lines().any(|l| {
                    l.split_whitespace().nth(1).is_some_and(|f| (f.starts_with("/usr/src/linux-") || f.starts_with("/lib/modules/")) && Path::new(f).exists())
                });
                if !any_file && (p.pn.ends_with("-kernel") || p.pn.ends_with("-kernel-bin") || p.pn.ends_with("-sources")) {
                    st.orphan_packages.push(p.cpv());
                }
            }
            continue;
        }
        for k in kvers {
            entry(&mut map, &k);
            if let Some(e) = map.get_mut(&k) {
                e.pkg = Some(p.cpv());
            }
        }
    }
    // /lib/modules, /usr/src
    for e in std::fs::read_dir("/lib/modules").into_iter().flatten().flatten() {
        let k = e.file_name().to_string_lossy().to_string();
        if e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            entry(&mut map, &k);
            map.get_mut(&k).unwrap().modules = true;
        }
    }
    for e in std::fs::read_dir("/usr/src").into_iter().flatten().flatten() {
        let n = e.file_name().to_string_lossy().to_string();
        if let Some(k) = n.strip_prefix("linux-")
            && e.file_type().map(|t| t.is_dir()).unwrap_or(false)
        {
            entry(&mut map, k);
            map.get_mut(k).unwrap().sources = true;
        }
    }
    // /boot files
    for e in std::fs::read_dir("/boot").into_iter().flatten().flatten() {
        let n = e.file_name().to_string_lossy().to_string();
        if !e.file_type().map(|t| t.is_file()).unwrap_or(false) {
            continue;
        }
        for prefix in ["vmlinuz-", "kernel-", "bzImage-", "initramfs-", "initrd-"] {
            let Some(rest) = n.strip_prefix(prefix) else { continue };
            let (k, old) = match rest.strip_suffix(".old") {
                Some(r) => (r, true),
                None => (rest, false),
            };
            let k = k.strip_suffix(".img").unwrap_or(k);
            if !portagehub::protocol::valid_kver(k) {
                continue;
            }
            entry(&mut map, k);
            let ke = map.get_mut(k).unwrap();
            if old {
                ke.old_files.push(n.clone());
            } else if prefix.starts_with("init") {
                ke.initramfs = Some(n.clone());
            } else {
                ke.image = Some(n.clone());
                ke.image_mtime = mtime(&e.path());
            }
        }
        if let Some(k) = n.strip_prefix("System.map-").or_else(|| n.strip_prefix("config-"))
            && let Some(k) = k.strip_suffix(".old")
            && map.contains_key(k)
        {
            map.get_mut(k).unwrap().old_files.push(n.clone());
        }
    }
    // NVIDIA
    let gpu = detect_gpu();
    let nv_pkg = vdb.installed("x11-drivers/nvidia-drivers").last().cloned();
    st.nvidia = NvidiaInfo {
        gpu,
        drivers: nv_pkg.as_ref().map(|p| p.ver.without_revision().to_string()),
        dist_kernel: nv_pkg.as_ref().is_some_and(|p| p.read("USE").split_whitespace().any(|u| u == "dist-kernel")),
        fallback_script: ["/usr/local/sbin/kernel-fallback-nvidia", "/usr/local/bin/kernel-fallback-nvidia"].iter().map(PathBuf::from).find(|p| p.is_file()),
    };
    if gpu || nv_pkg.is_some() {
        for e in map.values_mut().filter(|e| e.modules) {
            if let Some(ko) = find_nvidia_ko(&Path::new("/lib/modules").join(&e.kver), 0) {
                let version = modinfo_version(&ko).unwrap_or_default();
                e.nvidia = Some(NvidiaModule { path: ko, version });
            }
        }
    }
    let mut kernels: Vec<KernelEntry> = Vec::new();
    for e in map.into_values() {
        // A package whose kernel files are all gone is only a depclean candidate.
        if e.image.is_none() && !e.modules && !e.sources && e.initramfs.is_none() && e.old_files.is_empty() {
            if let Some(p) = e.pkg {
                st.orphan_packages.push(p);
            }
            continue;
        }
        kernels.push(e);
    }
    kernels.sort_by(|a, b| match (b.sort_key(), a.sort_key()) {
        (Some(x), Some(y)) => x.cmp(&y).then(b.kver.cmp(&a.kver)),
        _ => b.kver.cmp(&a.kver),
    });
    st.kernels = kernels;
    st.boot = detect_boot(vdb);
    // Keep set
    let set_path = Path::new("/etc/portage/sets").join(keep_set);
    st.keep_set_exists = set_path.is_file();
    st.keep_set_atoms = std::fs::read_to_string(&set_path)
        .unwrap_or_default()
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .map(String::from)
        .collect();
    st.keep_set_in_world = vdb.world_sets.iter().any(|w| *w == format!("@{keep_set}"));
    for t in ["kernel-mark-good", "kernel-fallback-nvidia", "safe-kernel-clean"] {
        for d in ["/usr/local/sbin", "/usr/local/bin"] {
            let p = Path::new(d).join(t);
            if p.is_file() {
                st.local_tools.push(p);
            }
        }
    }
    st
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Severity {
    Ok,
    Info,
    Warn,
    Error,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Fix {
    ScanBoot,
    Boot(BootAction, Option<String>),
    /// emerge --config =cpv (re-runs installkernel: initramfs + bootloader entry)
    EmergeConfig(String),
    /// Reinstall a package (emerge -1 =cpv)
    Reinstall(String),
    MarkGood(String),
    WriteKeepSet,
    AddKeepSetToWorld,
    RebuildNvidia,
    RemoveOld(String),
    RemoveLeftovers(String),
    Depclean(Vec<String>),
}

#[derive(Debug, Clone)]
pub struct Finding {
    pub severity: Severity,
    pub title: String,
    pub detail: String,
    pub fixes: Vec<Fix>,
}

fn find(sev: Severity, title: impl Into<String>, detail: impl Into<String>, fixes: Vec<Fix>) -> Finding {
    Finding { severity: sev, title: title.into(), detail: detail.into(), fixes }
}

/// Atoms the keep set should contain for the known-good kernels.
pub fn wanted_keep_atoms(st: &KernelState, last: Option<&str>, prev: Option<&str>) -> Vec<String> {
    [last, prev].into_iter().flatten().filter_map(|k| st.get(k).and_then(|e| e.pkg.clone())).map(|cpv| format!("={cpv}")).fold(Vec::new(), |mut v, a| {
        if !v.contains(&a) {
            v.push(a);
        }
        v
    })
}

/// Inspect boot readiness. `scan` holds the root-only files (None = not read yet).
pub fn check(st: &KernelState, scan: Option<&BootScan>, protect: bool, min_bootable: usize, nvidia_alerts: bool) -> Vec<Finding> {
    let mut out = Vec::new();
    let bootable: Vec<&KernelEntry> = st.kernels.iter().filter(|k| k.bootable()).collect();
    let newest = bootable.first().map(|k| k.kver.clone());

    // Known-good state
    match &st.last_good {
        None => out.push(find(
            Severity::Info,
            "No kernel is marked known-good yet",
            "After booting a kernel and checking that everything works (desktop, network, GPU), mark it known-good so it is protected from removal and kept as a fallback.",
            vec![Fix::MarkGood(st.running.clone())],
        )),
        Some(lg) => {
            if st.get(lg).is_none_or(|k| k.image.is_none()) {
                let backup = scan
                    .and_then(|s| s.listing(&format!("/boot/kernel-backup/{lg}")))
                    .is_some_and(|l| l.entries.iter().any(|e| e.name.starts_with("vmlinuz-")));
                out.push(find(
                    Severity::Error,
                    format!("Known-good kernel {lg} is no longer bootable"),
                    if backup {
                        format!("Its /boot files are gone, but a copy exists in /boot/kernel-backup/{lg}.")
                    } else {
                        "Its /boot files are gone and there is no backup copy. Mark the running kernel as known-good once it works.".into()
                    },
                    vec![Fix::MarkGood(st.running.clone())],
                ));
            }
        }
    }
    if newest.as_deref() == Some(st.running.as_str()) && st.last_good.as_deref() != Some(st.running.as_str()) {
        out.push(find(
            Severity::Warn,
            format!("You are running the new kernel {} — is everything working?", st.running),
            "If the desktop, network and graphics work, mark it known-good. The previous known-good kernel stays as the fallback.",
            vec![Fix::MarkGood(st.running.clone())],
        ));
    } else if let Some(n) = &newest
        && *n != st.running
        && st.last_good.as_deref() != Some(n.as_str())
    {
        out.push(find(
            Severity::Info,
            format!("Kernel {n} is installed but not booted yet"),
            format!("Reboot into it to try it. You are running {}. If it fails, pick the previous kernel in the boot menu.", st.running),
            vec![],
        ));
    }
    if bootable.len() < min_bootable {
        out.push(find(
            Severity::Warn,
            format!("Only {} bootable kernel(s)", bootable.len()),
            format!("Keep at least {min_bootable} so you can fall back if an update breaks booting."),
            vec![],
        ));
    }
    // Keep set
    if protect {
        let want = wanted_keep_atoms(st, st.last_good.as_deref(), st.previous_good.as_deref());
        let missing: Vec<&String> = want.iter().filter(|a| !st.keep_set_atoms.contains(a)).collect();
        if !want.is_empty() && !missing.is_empty() {
            out.push(find(
                Severity::Warn,
                format!("@{} does not protect all known-good kernels", st.keep_set),
                format!(
                    "Missing: {}. Without it, emerge --depclean may remove a known-good kernel.",
                    missing.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", ")
                ),
                vec![Fix::WriteKeepSet],
            ));
        }
        if st.keep_set_exists && !st.keep_set_in_world {
            out.push(find(
                Severity::Warn,
                format!("@{} is not part of @world", st.keep_set),
                "Its kernels are not protected from depclean until the set is added to @world (emerge --noreplace).",
                vec![Fix::AddKeepSetToWorld],
            ));
        }
    }
    // Per-kernel files
    for k in &st.kernels {
        if k.image.is_some() && !k.modules {
            if k.pkg.is_some() {
                out.push(find(
                    Severity::Error,
                    format!("Kernel {} has no /lib/modules/{}", k.kver, k.kver),
                    "It would boot without drivers. Reinstall its package.",
                    k.pkg.iter().map(|p| Fix::Reinstall(p.clone())).collect(),
                ));
            } else {
                out.push(find(
                    Severity::Warn,
                    format!("Leftover kernel image {} (package removed)", k.kver),
                    "Its package and modules are gone but /boot still has the image, so the boot menu may still offer it.",
                    vec![Fix::RemoveLeftovers(k.kver.clone())],
                ));
            }
        }
        if k.image.is_some() && k.modules && k.initramfs.is_none() && st.boot.initramfs_tool.is_some() {
            out.push(find(
                Severity::Warn,
                format!("Kernel {} has no initramfs", k.kver),
                format!(
                    "installkernel is set up to use {}, but /boot/initramfs-{}.img is missing.",
                    st.boot.initramfs_tool.clone().unwrap_or_default(),
                    k.kver
                ),
                vec![Fix::Boot(BootAction::DracutRegen, Some(k.kver.clone()))],
            ));
        }
        if !k.old_files.is_empty() {
            out.push(find(
                Severity::Info,
                format!("{} old copies (*.old) for {} in /boot", k.old_files.len(), k.kver),
                format!("Left behind when the same kernel version was reinstalled: {}", k.old_files.join(", ")),
                vec![Fix::RemoveOld(k.kver.clone())],
            ));
        }
        if k.image.is_none() && k.pkg.is_none() && (k.modules || k.sources) {
            out.push(find(
                Severity::Info,
                format!("Leftovers of removed kernel {}", k.kver),
                format!(
                    "{}{} no package owns them any more.",
                    if k.modules { format!("/lib/modules/{} ", k.kver) } else { String::new() },
                    if k.sources { format!("/usr/src/linux-{} ", k.kver) } else { String::new() }
                ),
                if k.kver != st.running { vec![Fix::RemoveLeftovers(k.kver.clone())] } else { vec![] },
            ));
        }
    }
    if !st.orphan_packages.is_empty() {
        out.push(find(
            Severity::Info,
            format!("{} kernel package(s) whose files were already removed", st.orphan_packages.len()),
            format!("{} — depclean can drop them from the package database.", st.orphan_packages.join(", ")),
            vec![Fix::Depclean(st.orphan_packages.iter().map(|c| format!("={c}")).collect())],
        ));
    }
    // NVIDIA
    if nvidia_alerts && st.nvidia.gpu {
        match &st.nvidia.drivers {
            None => out.push(find(
                Severity::Info,
                "NVIDIA GPU found, nvidia-drivers not installed",
                "The open nouveau driver is used. Install x11-drivers/nvidia-drivers for full performance (VIDEO_CARDS=\"nvidia\").",
                vec![],
            )),
            Some(want) => {
                for k in bootable.iter() {
                    let important = k.kver == st.running || Some(&k.kver) == newest.as_ref() || st.last_good.as_deref() == Some(k.kver.as_str());
                    match st.nvidia_status(k) {
                        Some(NvStatus::Missing) => out.push(find(
                            if important { Severity::Error } else { Severity::Warn },
                            format!("No NVIDIA module for kernel {}", k.kver),
                            format!(
                                "nvidia-drivers {want} is not built for this kernel: it boots without GPU acceleration (black screen or software rendering)."
                            ),
                            vec![Fix::RebuildNvidia],
                        )),
                        Some(NvStatus::Mismatch(v)) => out.push(find(
                            if important { Severity::Error } else { Severity::Warn },
                            format!("NVIDIA module for {} is {v}, driver is {want}", k.kver),
                            "Module and userspace versions must match (\"API mismatch\" errors otherwise). Rebuild nvidia-drivers for this kernel.",
                            vec![Fix::RebuildNvidia],
                        )),
                        _ => {}
                    }
                }
            }
        }
        if let (Some(sel), Some(n)) = (&st.eselected, &newest)
            && sel != n
        {
            out.push(find(
                Severity::Info,
                format!("/usr/src/linux points to {sel}, newest kernel is {n}"),
                "External modules such as nvidia-drivers (without dist-kernel) are built for the kernel /usr/src/linux points to. Switch it with eselect kernel before rebuilding.",
                vec![],
            ));
        }
    }
    // Bootloader
    match scan {
        None => out.push(find(
            Severity::Info,
            "Boot menu not checked yet",
            "Reading the bootloader configuration needs the admin session (it is root-only).",
            vec![Fix::ScanBoot],
        )),
        Some(scan) if !scan.root => out.push(find(
            Severity::Info,
            "Boot menu could not be read",
            "The admin helper is not running as root, so the root-only bootloader files were not readable.",
            vec![Fix::ScanBoot],
        )),
        Some(scan) => out.extend(check_bootloader(st, scan, &bootable)),
    }
    if scan.is_some_and(|s| s.root) && out.iter().all(|f| f.severity <= Severity::Info) {
        out.insert(0, find(Severity::Ok, "Boot setup looks good", "Every bootable kernel has its files and a boot menu entry.", vec![]));
    }
    // Problems first, then the green summary, then notes.
    let rank = |s: Severity| match s {
        Severity::Error => 0,
        Severity::Warn => 1,
        Severity::Ok => 2,
        Severity::Info => 3,
    };
    out.sort_by_key(|f| rank(f.severity));
    out
}

fn check_bootloader(st: &KernelState, scan: &BootScan, bootable: &[&KernelEntry]) -> Vec<Finding> {
    let mut out = Vec::new();
    let primary = st.boot.primary.or(st.boot.detected.first().copied());
    let newest_image = bootable.iter().map(|k| k.image_mtime).max().unwrap_or(0);
    match primary {
        Some(Bootloader::Grub) => {
            let cfg = scan.file("/boot/grub/grub.cfg").or(scan.file("/boot/grub2/grub.cfg"));
            let Some(cfg) = cfg else {
                out.push(find(
                    Severity::Error,
                    "GRUB is used but grub.cfg was not found",
                    "Generate it with grub-mkconfig.",
                    vec![Fix::Boot(BootAction::GrubMkconfig, None)],
                ));
                return out;
            };
            let mut missing = Vec::new();
            for k in bootable {
                let img = k.image.clone().unwrap_or_default();
                if !cfg.content.contains(&img) {
                    missing.push(k.kver.clone());
                }
            }
            if !missing.is_empty() {
                out.push(find(
                    Severity::Error,
                    format!("GRUB menu is missing {}", missing.join(", ")),
                    format!("{} has no entry for these kernels, so you cannot boot them.", cfg.path),
                    vec![Fix::Boot(BootAction::GrubMkconfig, None)],
                ));
            }
            // Entries pointing at files that are gone.
            let mut stale = Vec::new();
            for line in cfg.content.lines() {
                let t = line.trim();
                if !(t.starts_with("linux ") || t.starts_with("linux\t") || t.starts_with("linuxefi ")) {
                    continue;
                }
                if let Some(p) = t.split_whitespace().nth(1) {
                    let name = p.rsplit('/').next().unwrap_or(p);
                    // Compare with what is really in /boot (kernel-*, *.old … included).
                    let present = match scan.listing("/boot") {
                        Some(l) => l.entries.iter().any(|e| e.name == name),
                        None => st.kernels.iter().any(|k| k.image.as_deref() == Some(name)),
                    };
                    if !present && (name.starts_with("vmlinuz-") || name.starts_with("kernel-")) && !stale.contains(&name.to_string()) {
                        stale.push(name.to_string());
                    }
                }
            }
            if !stale.is_empty() {
                out.push(find(
                    Severity::Warn,
                    format!("GRUB menu lists {} kernel(s) that no longer exist", stale.len()),
                    format!("Choosing them fails to boot: {}", stale.join(", ")),
                    vec![Fix::Boot(BootAction::GrubMkconfig, None)],
                ));
            }
            if missing.is_empty() && stale.is_empty() && cfg.mtime > 0 && cfg.mtime < newest_image {
                out.push(find(
                    Severity::Info,
                    "grub.cfg is older than the newest kernel",
                    "Entries are present, but regenerating keeps titles and options current.",
                    vec![Fix::Boot(BootAction::GrubMkconfig, None)],
                ));
            }
            let saved = scan.file("/boot/grub/grubenv").and_then(|f| f.content.lines().find_map(|l| l.strip_prefix("saved_entry=")).map(|s| s.to_string()));
            let default = scan
                .file("/etc/default/grub")
                .and_then(|f| f.content.lines().find_map(|l| l.trim().strip_prefix("GRUB_DEFAULT=")).map(|s| s.trim_matches('"').to_string()));
            if missing.is_empty() && stale.is_empty() {
                out.push(find(
                    Severity::Ok,
                    format!("GRUB menu has all {} bootable kernels", bootable.len()),
                    format!(
                        "Default entry: {}{}",
                        default.unwrap_or_else(|| "0 (first, the newest kernel)".into()),
                        saved.map(|s| format!(" · last saved: {s}")).unwrap_or_default()
                    ),
                    vec![],
                ));
            }
        }
        Some(Bootloader::SystemdBoot) => {
            let entries: Vec<&portagehub::protocol::ScannedFile> = scan.files.iter().filter(|f| f.path.contains("/loader/entries/")).collect();
            let ukis: Vec<String> = ["/boot/efi/EFI/Linux", "/efi/EFI/Linux", "/boot/EFI/Linux"]
                .iter()
                .filter_map(|d| scan.listing(d))
                .flat_map(|l| l.entries.iter().map(|e| e.name.clone()))
                .collect();
            let missing: Vec<String> = bootable
                .iter()
                .filter(|k| !entries.iter().any(|e| e.content.contains(&k.kver)) && !ukis.iter().any(|u| u.contains(&k.kver)))
                .map(|k| k.kver.clone())
                .collect();
            if missing.is_empty() {
                out.push(find(Severity::Ok, format!("systemd-boot has entries for all {} bootable kernels", bootable.len()), "", vec![]));
            } else {
                let fixes = missing.iter().filter_map(|k| st.get(k).and_then(|e| e.pkg.clone())).map(Fix::EmergeConfig).collect();
                out.push(find(
                    Severity::Error,
                    format!("systemd-boot has no entry for {}", missing.join(", ")),
                    "Re-run the kernel's install step (emerge --config) to create the loader entry.",
                    fixes,
                ));
            }
        }
        Some(Bootloader::Lilo) => match scan.file("/etc/lilo.conf") {
            None => out.push(find(Severity::Error, "LILO is installed but /etc/lilo.conf was not found", "", vec![])),
            Some(conf) => {
                let missing: Vec<String> = bootable.iter().filter(|k| !conf.content.contains(&k.kver)).map(|k| k.kver.clone()).collect();
                if !missing.is_empty() {
                    out.push(find(
                        Severity::Warn,
                        format!("/etc/lilo.conf does not mention {}", missing.join(", ")),
                        "Unless it boots through a /boot/vmlinuz symlink, add an image= section for them.",
                        vec![],
                    ));
                }
                let map = scan.listing("/boot").and_then(|l| l.entries.iter().find(|e| e.name == "map").map(|e| e.mtime)).unwrap_or(0);
                if map < newest_image {
                    out.push(find(
                        Severity::Error,
                        "LILO was not re-installed after the last kernel change",
                        "LILO stores disk locations of the kernel: run lilo after every kernel update or it may boot the wrong data.",
                        vec![Fix::Boot(BootAction::Lilo, None)],
                    ));
                }
            }
        },
        Some(b) => out.push(find(
            Severity::Info,
            format!("{} detected", b.name()),
            "PortageHub cannot inspect this bootloader's menu; make sure installkernel updates it.",
            vec![],
        )),
        None => out.push(find(
            Severity::Warn,
            "No bootloader detected",
            "Install one (sys-boot/grub, systemd-boot, …) and enable the matching installkernel USE flag.",
            vec![],
        )),
    }
    for e in &scan.errors {
        out.push(find(Severity::Info, "Could not read a boot file", e.clone(), vec![]));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k(kver: &str, image: bool, pkg: bool) -> KernelEntry {
        KernelEntry {
            kver: kver.into(),
            pkg: pkg.then(|| format!("sys-kernel/x-kernel-{}", kver.split('-').next().unwrap())),
            image: image.then(|| format!("vmlinuz-{kver}")),
            image_mtime: 100,
            initramfs: image.then(|| format!("initramfs-{kver}.img")),
            old_files: vec![],
            modules: image,
            sources: true,
            nvidia: None,
        }
    }

    fn state() -> KernelState {
        KernelState {
            running: "7.3.0-x".into(),
            kernels: vec![k("7.3.0-x", true, true), k("7.2.11-x", true, true), k("7.2.10-x", true, true), k("7.2.9-x", true, true)],
            last_good: Some("7.3.0-x".into()),
            previous_good: Some("7.2.11-x".into()),
            keep_set: "kernels-keep".into(),
            keep_set_atoms: vec!["=sys-kernel/x-kernel-7.3.0".into(), "=sys-kernel/x-kernel-7.2.11".into()],
            keep_set_exists: true,
            keep_set_in_world: true,
            boot: BootInfo { primary: Some(Bootloader::Grub), ..Default::default() },
            ..Default::default()
        }
    }

    #[test]
    fn removable_keeps_protected() {
        let st = state();
        let r: Vec<&str> = st.removable(1).iter().map(|k| k.kver.as_str()).collect();
        // 7.3.0 running/last-good, 7.2.11 previous-good, 7.2.10 is the one extra kept.
        assert_eq!(r, vec!["7.2.9-x"]);
    }

    #[test]
    fn grub_missing_entry() {
        let st = state();
        let scan = BootScan {
            root: true,
            files: vec![portagehub::protocol::ScannedFile {
                path: "/boot/grub/grub.cfg".into(),
                content: "linux /vmlinuz-7.3.0-x root=x\nlinux /vmlinuz-7.2.11-x\nlinux /vmlinuz-7.2.10-x\nlinux /vmlinuz-6.1.0-old\n".into(),
                mtime: 200,
            }],
            ..Default::default()
        };
        let f = check(&st, Some(&scan), true, 2, true);
        assert!(f.iter().any(|x| x.severity == Severity::Error && x.title.contains("7.2.9-x")), "{f:#?}");
        assert!(f.iter().any(|x| x.title.contains("no longer exist") && x.detail.contains("vmlinuz-6.1.0-old")));
    }

    #[test]
    fn grub_entries_for_existing_files_are_not_stale() {
        let st = state();
        let scan = BootScan {
            root: true,
            files: vec![portagehub::protocol::ScannedFile {
                path: "/boot/grub/grub.cfg".into(),
                content: "linux /root/boot/vmlinuz-7.3.0-x\nlinux /root/boot/vmlinuz-7.2.11-x\nlinux /root/boot/vmlinuz-7.2.10-x\nlinux /root/boot/vmlinuz-7.2.9-x\nlinux /root/boot/kernel-7.2.9-x\nlinux /root/boot/vmlinuz-7.2.9-x.old\n".into(),
                mtime: 200,
            }],
            listings: vec![portagehub::protocol::Listing {
                dir: "/boot".into(),
                entries: ["vmlinuz-7.3.0-x", "vmlinuz-7.2.11-x", "vmlinuz-7.2.10-x", "vmlinuz-7.2.9-x", "kernel-7.2.9-x", "vmlinuz-7.2.9-x.old"]
                    .iter()
                    .map(|n| portagehub::protocol::ListEntry { name: n.to_string(), ..Default::default() })
                    .collect(),
            }],
            ..Default::default()
        };
        let f = check(&st, Some(&scan), true, 2, true);
        assert!(!f.iter().any(|x| x.title.contains("no longer exist")), "{f:#?}");
        assert!(f.iter().any(|x| x.title.contains("GRUB menu has all 4")), "{f:#?}");
    }

    #[test]
    fn keep_set_and_new_kernel() {
        let mut st = state();
        st.last_good = Some("7.2.11-x".into());
        st.previous_good = Some("7.2.10-x".into());
        let f = check(&st, None, true, 2, true);
        assert!(f.iter().any(|x| x.title.contains("running the new kernel")));
        assert!(f.iter().any(|x| x.title.contains("does not protect")));
        assert_eq!(wanted_keep_atoms(&st, Some("7.3.0-x"), Some("7.2.11-x")), vec!["=sys-kernel/x-kernel-7.3.0", "=sys-kernel/x-kernel-7.2.11"]);
    }
}
