//! Hardware and system detection.
//!
//! Everything here reads from /proc, /sys and Portage's package database as an
//! unprivileged user, so the GUI can scan the machine without asking for a
//! password.

use crate::util::{read_trim, run, which};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeSet, HashMap};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum CpuVendor {
    Intel,
    Amd,
    Other,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum BranchSampling {
    /// Intel Last Branch Record — AutoFDO and Propeller work.
    IntelLbr,
    /// AMD Zen 3 Branch Sampling.
    AmdBrs,
    /// AMD Zen 4+ LBR v2.
    AmdLbrV2,
    None,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CpuInfo {
    pub vendor: CpuVendor,
    pub model_name: String,
    pub family: u32,
    pub model: u32,
    pub threads: usize,
    pub cores: usize,
    /// Intel hybrid (P-cores + E-cores), e.g. Alder/Raptor/Arrow Lake.
    pub hybrid: bool,
    /// Highest x86-64 micro-architecture level supported (1–4).
    pub x86_level: u8,
    /// What `-march=native` resolves to with GCC (e.g. "raptorlake").
    pub march_native: Option<String>,
    pub branch_sampling: BranchSampling,
    pub flags: BTreeSet<String>,
}

impl CpuInfo {
    pub fn uarch_label(&self) -> String {
        match &self.march_native {
            Some(m) => format!("{m} (x86-64-v{})", self.x86_level),
            None => format!("x86-64-v{}", self.x86_level),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum GpuVendor {
    Nvidia,
    Amd,
    Intel,
    Other,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PciDevice {
    pub slot: String,
    pub class: u32,
    pub vendor_id: u16,
    pub device_id: u16,
    pub name: String,
    pub driver: Option<String>,
}

impl PciDevice {
    pub fn gpu_vendor(&self) -> GpuVendor {
        match self.vendor_id {
            0x10de => GpuVendor::Nvidia,
            0x1002 => GpuVendor::Amd,
            0x8086 => GpuVendor::Intel,
            _ => GpuVendor::Other,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NetDevice {
    pub iface: String,
    pub driver: Option<String>,
    pub wireless: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum DiskKind {
    Nvme,
    Ssd,
    Hdd,
    Usb,
    Mmc,
    Virtual,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StorageDevice {
    pub name: String,
    pub model: String,
    pub kind: DiskKind,
    pub size_bytes: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum InitSystem {
    OpenRc,
    Systemd,
    Unknown,
}

impl InitSystem {
    pub fn label(self) -> &'static str {
        match self {
            InitSystem::OpenRc => "OpenRC",
            InitSystem::Systemd => "systemd",
            InitSystem::Unknown => "Unknown init",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Bootloader {
    Grub,
    SystemdBoot,
    Uki,
    Efistub,
    Refind,
    Limine,
    Unknown,
}

impl Bootloader {
    pub fn label(self) -> &'static str {
        match self {
            Bootloader::Grub => "GRUB",
            Bootloader::SystemdBoot => "systemd-boot",
            Bootloader::Uki => "Unified Kernel Image",
            Bootloader::Efistub => "EFI stub",
            Bootloader::Refind => "rEFInd",
            Bootloader::Limine => "Limine",
            Bootloader::Unknown => "Unknown bootloader",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Initramfs {
    Dracut,
    Ugrd,
    None,
}

impl Initramfs {
    pub fn label(self) -> &'static str {
        match self {
            Initramfs::Dracut => "dracut",
            Initramfs::Ugrd => "ugrd",
            Initramfs::None => "none",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BootInfo {
    pub bootloader: Bootloader,
    pub initramfs: Initramfs,
    /// `sys-kernel/installkernel` version and its USE flags, if installed.
    pub installkernel: Option<(String, Vec<String>)>,
    pub uefi: bool,
    pub secure_boot: Option<bool>,
    pub efi_mount: Option<PathBuf>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Toolchain {
    pub gcc: Option<String>,
    pub clang: Option<String>,
    pub clang_major: Option<u32>,
    /// Directory holding clang/ld.lld/llvm-ar (Gentoo: /usr/lib/llvm/<slot>/bin).
    pub llvm_bin: Option<PathBuf>,
    pub lld: bool,
    pub llvm_ar: bool,
    pub make: bool,
    pub patch: bool,
    pub bc: bool,
    pub flex: bool,
    pub bison: bool,
    pub perl: bool,
    pub cpio: bool,
    pub zstd: bool,
    pub pahole: bool,
    pub libelf: bool,
    pub openssl_headers: bool,
    pub perf: bool,
    pub llvm_profgen: bool,
    pub propeller_tool: bool,
    pub rustc: bool,
    pub kmod_zstd: bool,
    pub curl: bool,
    pub wget: bool,
}

impl Toolchain {
    pub fn clang_ready(&self) -> bool {
        self.clang.is_some() && self.lld && self.llvm_ar
    }

    /// Tools without which no kernel build can succeed: (name, Gentoo package).
    pub fn missing_essentials(&self) -> Vec<(&'static str, &'static str)> {
        let mut m = vec![];
        let checks = [
            (self.make, "make", "dev-build/make"),
            (self.gcc.is_some(), "gcc", "sys-devel/gcc"),
            (self.patch, "patch", "sys-devel/patch"),
            (self.bc, "bc", "sys-devel/bc"),
            (self.flex, "flex", "sys-devel/flex"),
            (self.bison, "bison", "sys-devel/bison"),
            (self.perl, "perl", "dev-lang/perl"),
            (self.cpio, "cpio", "app-alternatives/cpio"),
            (self.libelf, "libelf headers", "virtual/libelf"),
            (self.openssl_headers, "OpenSSL headers", "dev-libs/openssl"),
        ];
        for (ok, name, pkg) in checks {
            if !ok {
                m.push((name, pkg));
            }
        }
        m
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstalledPkg {
    pub atom: String,
    pub version: String,
    pub use_flags: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SystemInfo {
    pub hostname: String,
    pub cpu: CpuInfo,
    pub memory_bytes: u64,
    pub gpus: Vec<PciDevice>,
    pub audio: Vec<String>,
    pub network: Vec<NetDevice>,
    pub storage: Vec<StorageDevice>,
    pub has_bluetooth: bool,
    pub has_battery: bool,
    pub handheld: Option<String>,
    pub dmi_vendor: String,
    pub dmi_product: String,
    pub vm_guest: Option<String>,
    pub virt_capable: bool,
    pub kvm_loaded: bool,
    pub root_fs: String,
    pub root_device: String,
    pub root_on_dm: bool,
    pub mounted_fs: BTreeSet<String>,
    pub loaded_modules: Vec<String>,
    pub modprobed_db: Option<PathBuf>,
    pub init: InitSystem,
    pub boot: BootInfo,
    pub toolchain: Toolchain,
    pub running_kernel: String,
    pub proc_config: bool,
    /// Packages that install out-of-tree kernel modules (`@module-rebuild`).
    pub external_modules: Vec<InstalledPkg>,
    pub nvidia_driver: Option<InstalledPkg>,
    pub scx_installed: bool,
    pub dist_kernels: Vec<InstalledPkg>,
    pub source_packages: Vec<InstalledPkg>,
    pub gentoo: bool,
    /// NUMA nodes the firmware exposes (1 on every desktop and laptop).
    #[serde(default = "one")]
    pub numa_nodes: usize,
    /// Security modules active on the running kernel (`/sys/kernel/security/lsm`).
    #[serde(default)]
    pub active_lsms: Vec<String>,
    /// Firmware word size (`/sys/firmware/efi/fw_platform_size`); 64 on any modern PC.
    #[serde(default)]
    pub efi_bits: Option<u32>,
    /// An Intel VMD controller fronts the NVMe drives, so the root disk may sit behind it.
    #[serde(default)]
    pub vmd: bool,
}

fn one() -> usize {
    1
}

impl SystemInfo {
    pub fn has_gpu(&self, v: GpuVendor) -> bool {
        self.gpus.iter().any(|g| g.gpu_vendor() == v)
    }

    pub fn primary_gpu_name(&self) -> String {
        // Prefer a discrete card over an integrated one.
        let pick = self
            .gpus
            .iter()
            .find(|g| g.gpu_vendor() == GpuVendor::Nvidia)
            .or_else(|| self.gpus.iter().find(|g| g.gpu_vendor() == GpuVendor::Amd))
            .or_else(|| self.gpus.first());
        pick.map(|g| g.name.clone()).unwrap_or_else(|| "No GPU detected".into())
    }

    pub fn memory_gib(&self) -> f64 {
        self.memory_bytes as f64 / (1024.0 * 1024.0 * 1024.0)
    }

    pub fn is_laptop(&self) -> bool {
        self.has_battery
    }

    /// The proprietary NVIDIA driver needs a few kernel options that slimming
    /// would otherwise remove.
    pub fn needs_nvidia_support(&self) -> bool {
        self.nvidia_driver.is_some()
    }
}

/// A fixed, plausible system for tests and documentation examples.
#[doc(hidden)]
pub fn sample() -> SystemInfo {
    SystemInfo {
        hostname: "gentoo".into(),
        cpu: CpuInfo {
            vendor: CpuVendor::Intel,
            model_name: "Example CPU".into(),
            family: 6,
            model: 183,
            threads: 16,
            cores: 8,
            hybrid: true,
            x86_level: 3,
            march_native: Some("alderlake".into()),
            branch_sampling: BranchSampling::IntelLbr,
            flags: BTreeSet::new(),
        },
        memory_bytes: 32 << 30,
        gpus: vec![PciDevice { slot: "0000:01:00.0".into(), class: 0x030000, vendor_id: 0x10de, device_id: 0x2684, name: "NVIDIA GeForce RTX 4090".into(), driver: Some("nvidia".into()) }],
        audio: vec![],
        network: vec![],
        storage: vec![StorageDevice { name: "nvme0n1".into(), model: "SSD".into(), kind: DiskKind::Nvme, size_bytes: 1 << 40 }],
        has_bluetooth: true,
        has_battery: false,
        handheld: None,
        dmi_vendor: String::new(),
        dmi_product: String::new(),
        vm_guest: None,
        virt_capable: true,
        kvm_loaded: false,
        root_fs: "btrfs".into(),
        root_device: "/dev/nvme0n1p3".into(),
        root_on_dm: false,
        mounted_fs: ["btrfs".to_string(), "vfat".to_string()].into_iter().collect(),
        loaded_modules: vec!["nvidia".into(), "btusb".into()],
        modprobed_db: None,
        init: InitSystem::OpenRc,
        boot: BootInfo {
            bootloader: Bootloader::Grub,
            initramfs: Initramfs::Dracut,
            installkernel: Some(("68".into(), vec!["dracut".into(), "grub".into()])),
            uefi: true,
            secure_boot: Some(false),
            efi_mount: Some(PathBuf::from("/efi")),
        },
        toolchain: Toolchain {
            gcc: Some("16.2.1".into()),
            clang: Some("clang version 22.1.8".into()),
            clang_major: Some(22),
            llvm_bin: Some(PathBuf::from("/usr/lib/llvm/22/bin")),
            lld: true,
            llvm_ar: true,
            make: true,
            patch: true,
            bc: true,
            flex: true,
            bison: true,
            perl: true,
            cpio: true,
            zstd: true,
            pahole: true,
            libelf: true,
            openssl_headers: true,
            perf: true,
            llvm_profgen: true,
            propeller_tool: false,
            rustc: true,
            kmod_zstd: true,
            curl: true,
            wget: true,
        },
        running_kernel: "7.3.0-gentoo-dist".into(),
        proc_config: true,
        external_modules: vec![InstalledPkg { atom: "x11-drivers/nvidia-drivers".into(), version: "615.78.08".into(), use_flags: vec![] }],
        nvidia_driver: Some(InstalledPkg { atom: "x11-drivers/nvidia-drivers".into(), version: "615.78.08".into(), use_flags: vec![] }),
        scx_installed: true,
        dist_kernels: vec![],
        source_packages: vec![],
        gentoo: true,
        numa_nodes: 1,
        active_lsms: ["capability", "landlock", "lockdown", "yama", "bpf"].iter().map(|s| s.to_string()).collect(),
        efi_bits: Some(64),
        vmd: true,
    }
}

/// Scan the whole system. Takes ~100 ms; run it off the UI thread.
pub fn detect() -> SystemInfo {
    let cpu = detect_cpu();
    let pci = scan_pci();
    let names = PciNames::load();
    let mut gpus = vec![];
    for d in &pci {
        if d.class >> 16 == 0x03 {
            let mut g = d.clone();
            g.name = names.describe(d.vendor_id, d.device_id);
            gpus.push(g);
        }
    }
    let (root_fs, root_device, mounted_fs) = mounts();
    let root_on_dm = root_device.starts_with("/dev/mapper") || root_device.starts_with("/dev/dm-");
    let loaded_modules = loaded_modules();
    let kvm_loaded = loaded_modules.iter().any(|m| m == "kvm");
    let pkgs = PkgDb::scan();
    let dmi = |f: &str| read_trim(format!("/sys/class/dmi/id/{f}")).unwrap_or_default();
    let dmi_vendor = dmi("sys_vendor");
    let dmi_product = dmi("product_name");
    let toolchain = detect_toolchain();
    let boot = detect_boot(&pkgs);

    SystemInfo {
        hostname: read_trim("/proc/sys/kernel/hostname").unwrap_or_else(|| "localhost".into()),
        memory_bytes: meminfo_total(),
        audio: audio_cards(),
        network: net_devices(),
        storage: storage_devices(),
        has_bluetooth: Path::new("/sys/class/bluetooth").read_dir().map(|mut d| d.next().is_some()).unwrap_or(false),
        has_battery: has_battery(),
        handheld: detect_handheld(&dmi_vendor, &dmi_product),
        vm_guest: if cpu.flags.contains("hypervisor") {
            Some(read_trim("/sys/hypervisor/type").unwrap_or_else(|| dmi("product_name")))
        } else {
            None
        },
        virt_capable: cpu.flags.contains("vmx") || cpu.flags.contains("svm"),
        kvm_loaded,
        dmi_vendor,
        dmi_product,
        root_fs,
        root_device,
        root_on_dm,
        mounted_fs,
        modprobed_db: crate::paths::modprobed_db(),
        loaded_modules,
        init: detect_init(),
        running_kernel: run("uname", &["-r"]).unwrap_or_default(),
        proc_config: Path::new("/proc/config.gz").exists(),
        external_modules: pkgs.module_packages.clone(),
        nvidia_driver: pkgs.find("x11-drivers/nvidia-drivers"),
        scx_installed: pkgs.find("sys-kernel/scx").is_some(),
        dist_kernels: pkgs
            .kernels
            .iter()
            .filter(|p| p.atom.ends_with("-kernel") || p.atom.ends_with("-kernel-bin"))
            .cloned()
            .collect(),
        source_packages: pkgs
            .kernels
            .iter()
            .filter(|p| p.atom.ends_with("-sources"))
            .cloned()
            .collect(),
        gentoo: Path::new("/etc/gentoo-release").exists(),
        numa_nodes: numa_nodes(),
        active_lsms: read_trim("/sys/kernel/security/lsm")
            .map(|s| s.split(',').map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect())
            .unwrap_or_default(),
        efi_bits: read_trim("/sys/firmware/efi/fw_platform_size").and_then(|s| s.parse().ok()),
        // Driver bound, or an unbound Intel "RAID bus controller" (VMD shows up as class 0x0104).
        vmd: pci.iter().any(|d| d.driver.as_deref() == Some("vmd") || (d.vendor_id == 0x8086 && d.class >> 8 == 0x0104)),
        gpus,
        cpu,
        boot,
        toolchain,
    }
}

/// Memory nodes under /sys/devices/system/node (node0, node1, …).
fn numa_nodes() -> usize {
    std::fs::read_dir("/sys/devices/system/node")
        .map(|d| {
            d.flatten()
                .filter(|e| {
                    let n = e.file_name();
                    let n = n.to_string_lossy();
                    n.len() > 4 && n.starts_with("node") && n[4..].chars().all(|c| c.is_ascii_digit())
                })
                .count()
        })
        .unwrap_or(1)
        .max(1)
}

fn detect_cpu() -> CpuInfo {
    let text = std::fs::read_to_string("/proc/cpuinfo").unwrap_or_default();
    let mut vendor = CpuVendor::Other;
    let mut model_name = String::from("Unknown CPU");
    let (mut family, mut model) = (0, 0);
    let mut flags = BTreeSet::new();
    let mut threads = 0;
    let mut cores = BTreeSet::new();
    let mut phys = String::new();
    for line in text.lines() {
        let Some((k, v)) = line.split_once(':') else { continue };
        let (k, v) = (k.trim(), v.trim());
        match k {
            "processor" => threads += 1,
            "vendor_id" => {
                vendor = match v {
                    "GenuineIntel" => CpuVendor::Intel,
                    "AuthenticAMD" => CpuVendor::Amd,
                    _ => CpuVendor::Other,
                }
            }
            "model name" => model_name = v.split_whitespace().collect::<Vec<_>>().join(" "),
            "cpu family" => family = v.parse().unwrap_or(0),
            "model" => model = v.parse().unwrap_or(0),
            "physical id" => phys = v.to_string(),
            "core id" => {
                cores.insert(format!("{phys}:{v}"));
            }
            "flags" if flags.is_empty() => {
                flags = v.split_whitespace().map(String::from).collect();
            }
            _ => {}
        }
    }
    let has = |f: &str| flags.contains(f);
    let v2 = ["cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3"].iter().all(|f| has(f));
    let v3 = v2 && ["avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave"].iter().all(|f| has(f));
    let v4 = v3 && ["avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"].iter().all(|f| has(f));
    let x86_level = if v4 { 4 } else if v3 { 3 } else if v2 { 2 } else { 1 };

    let hybrid = Path::new("/sys/devices/cpu_core").exists() && Path::new("/sys/devices/cpu_atom").exists();
    let branch_sampling = match vendor {
        CpuVendor::Intel => {
            let caps = ["cpu", "cpu_core"]
                .iter()
                .any(|p| Path::new(&format!("/sys/bus/event_source/devices/{p}/caps/branches")).exists());
            if caps { BranchSampling::IntelLbr } else { BranchSampling::None }
        }
        CpuVendor::Amd if has("amd_lbr_v2") => BranchSampling::AmdLbrV2,
        CpuVendor::Amd if has("brs") => BranchSampling::AmdBrs,
        _ => BranchSampling::None,
    };

    CpuInfo {
        vendor,
        model_name,
        family,
        model,
        threads: threads.max(1),
        cores: cores.len().max(1),
        hybrid,
        x86_level,
        march_native: gcc_native_march(),
        branch_sampling,
        flags,
    }
}

/// Ask GCC what `-march=native` means on this machine.
fn gcc_native_march() -> Option<String> {
    let out = run("gcc", &["-march=native", "-Q", "--help=target"])?;
    out.lines()
        .find(|l| l.trim_start().starts_with("-march="))
        .and_then(|l| l.split_whitespace().nth(1))
        .map(String::from)
        .filter(|m| m != "native")
}

fn meminfo_total() -> u64 {
    std::fs::read_to_string("/proc/meminfo")
        .ok()
        .and_then(|t| {
            t.lines()
                .find(|l| l.starts_with("MemTotal:"))
                .and_then(|l| l.split_whitespace().nth(1))
                .and_then(|kb| kb.parse::<u64>().ok())
        })
        .map(|kb| kb * 1024)
        .unwrap_or(0)
}

fn scan_pci() -> Vec<PciDevice> {
    let mut out = vec![];
    let Ok(dir) = std::fs::read_dir("/sys/bus/pci/devices") else { return out };
    for e in dir.flatten() {
        let p = e.path();
        let hex = |f: &str| {
            read_trim(p.join(f)).and_then(|s| u32::from_str_radix(s.trim_start_matches("0x"), 16).ok())
        };
        let (Some(class), Some(vendor), Some(device)) = (hex("class"), hex("vendor"), hex("device")) else {
            continue;
        };
        out.push(PciDevice {
            slot: e.file_name().to_string_lossy().into_owned(),
            class,
            vendor_id: vendor as u16,
            device_id: device as u16,
            name: String::new(),
            driver: link_name(&p.join("driver")),
        });
    }
    out.sort_by(|a, b| a.slot.cmp(&b.slot));
    out
}

fn link_name(p: &Path) -> Option<String> {
    std::fs::read_link(p)
        .ok()
        .and_then(|l| l.file_name().map(|n| n.to_string_lossy().into_owned()))
}

/// Minimal pci.ids reader: only resolves the vendor/device pairs we ask for.
struct PciNames {
    text: String,
}

impl PciNames {
    fn load() -> Self {
        let text = ["/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids"]
            .iter()
            .find_map(|p| std::fs::read_to_string(p).ok())
            .unwrap_or_default();
        PciNames { text }
    }

    fn describe(&self, vendor: u16, device: u16) -> String {
        let vhex = format!("{vendor:04x}");
        let dhex = format!("\t{device:04x}");
        let mut vendor_name = None;
        let mut device_name = None;
        let mut in_vendor = false;
        for line in self.text.lines() {
            if line.starts_with('#') || line.is_empty() {
                continue;
            }
            if !line.starts_with('\t') {
                if in_vendor {
                    break;
                }
                if line.starts_with(&vhex) {
                    in_vendor = true;
                    vendor_name = Some(line[4..].trim().to_string());
                }
            } else if in_vendor && line.starts_with(&dhex) && !line.starts_with("\t\t") {
                device_name = Some(line[dhex.len()..].trim().to_string());
                break;
            }
        }
        let vendor_short = match vendor {
            0x10de => "NVIDIA".to_string(),
            0x1002 => "AMD".to_string(),
            0x8086 => "Intel".to_string(),
            _ => vendor_name.unwrap_or_else(|| format!("Vendor {vhex}")),
        };
        match device_name {
            // "AD102 [GeForce RTX 4090]" -> "GeForce RTX 4090"
            Some(d) => {
                let marketing = d
                    .split_once('[')
                    .and_then(|(_, r)| r.split_once(']'))
                    .map(|(m, _)| m.to_string())
                    .unwrap_or(d);
                format!("{vendor_short} {marketing}")
            }
            None => format!("{vendor_short} device {device:04x}"),
        }
    }
}

fn audio_cards() -> Vec<String> {
    std::fs::read_to_string("/proc/asound/cards")
        .unwrap_or_default()
        .lines()
        .filter(|l| l.trim_start().chars().next().is_some_and(|c| c.is_ascii_digit()))
        .filter_map(|l| l.split_once(" - ").map(|(_, n)| n.trim().to_string()))
        .collect()
}

fn net_devices() -> Vec<NetDevice> {
    let mut out = vec![];
    let Ok(dir) = std::fs::read_dir("/sys/class/net") else { return out };
    for e in dir.flatten() {
        let p = e.path();
        // Virtual interfaces (bridges, tun, veth) have no backing device.
        if !p.join("device").exists() {
            continue;
        }
        out.push(NetDevice {
            iface: e.file_name().to_string_lossy().into_owned(),
            driver: link_name(&p.join("device/driver")),
            wireless: p.join("wireless").exists() || p.join("phy80211").exists(),
        });
    }
    out.sort_by(|a, b| a.iface.cmp(&b.iface));
    out
}

fn storage_devices() -> Vec<StorageDevice> {
    let mut out = vec![];
    let Ok(dir) = std::fs::read_dir("/sys/block") else { return out };
    for e in dir.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if ["loop", "ram", "zram", "dm-", "md", "sr", "nbd"].iter().any(|p| name.starts_with(p)) {
            continue;
        }
        let p = e.path();
        let size = read_trim(p.join("size")).and_then(|s| s.parse::<u64>().ok()).unwrap_or(0) * 512;
        if size == 0 {
            continue;
        }
        let real = std::fs::canonicalize(&p).unwrap_or_default();
        let real = real.to_string_lossy();
        let rotational = read_trim(p.join("queue/rotational")).as_deref() == Some("1");
        let kind = if real.contains("/usb") {
            DiskKind::Usb
        } else if name.starts_with("nvme") {
            DiskKind::Nvme
        } else if name.starts_with("mmcblk") {
            DiskKind::Mmc
        } else if name.starts_with("vd") || name.starts_with("xvd") {
            DiskKind::Virtual
        } else if rotational {
            DiskKind::Hdd
        } else {
            DiskKind::Ssd
        };
        let model = read_trim(p.join("device/model")).unwrap_or_else(|| name.clone());
        out.push(StorageDevice { name, model, kind, size_bytes: size });
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    out
}

fn has_battery() -> bool {
    std::fs::read_dir("/sys/class/power_supply")
        .map(|d| {
            d.flatten().any(|e| {
                read_trim(e.path().join("type")).as_deref() == Some("Battery")
                    && read_trim(e.path().join("scope")).as_deref() != Some("Device")
            })
        })
        .unwrap_or(false)
}

fn detect_handheld(vendor: &str, product: &str) -> Option<String> {
    let known = [
        ("Jupiter", "Steam Deck LCD"),
        ("Galileo", "Steam Deck OLED"),
        ("ROG Ally", "ASUS ROG Ally"),
        ("RC71L", "ASUS ROG Ally"),
        ("RC72L", "ASUS ROG Ally X"),
        ("83E1", "Lenovo Legion Go"),
        ("Legion Go", "Lenovo Legion Go"),
        ("Claw", "MSI Claw"),
        ("AYANEO", "AYANEO handheld"),
        ("ONEXPLAYER", "OneXPlayer handheld"),
        ("GPD WIN", "GPD Win"),
    ];
    let hay = format!("{vendor} {product}");
    known.iter().find(|(k, _)| hay.contains(k)).map(|(_, n)| n.to_string())
}

fn mounts() -> (String, String, BTreeSet<String>) {
    let text = std::fs::read_to_string("/proc/mounts").unwrap_or_default();
    let mut root = (String::from("unknown"), String::new());
    let mut types = BTreeSet::new();
    for line in text.lines() {
        let f: Vec<&str> = line.split_whitespace().collect();
        if f.len() < 3 {
            continue;
        }
        if f[1] == "/" {
            root = (f[2].to_string(), f[0].to_string());
        }
        // Only filesystems backed by a real device matter for kernel config.
        if f[0].starts_with("/dev/") {
            types.insert(f[2].to_string());
        }
    }
    (root.0, root.1, types)
}

fn loaded_modules() -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_to_string("/proc/modules")
        .unwrap_or_default()
        .lines()
        .filter_map(|l| l.split_whitespace().next().map(String::from))
        .collect();
    v.sort();
    v
}

fn detect_init() -> InitSystem {
    if Path::new("/run/systemd/system").is_dir() {
        return InitSystem::Systemd;
    }
    if Path::new("/run/openrc").is_dir() {
        return InitSystem::OpenRc;
    }
    let profile = std::fs::read_link("/etc/portage/make.profile")
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();
    if profile.contains("systemd") {
        InitSystem::Systemd
    } else if Path::new("/sbin/openrc").exists() {
        InitSystem::OpenRc
    } else {
        InitSystem::Unknown
    }
}

fn detect_boot(pkgs: &PkgDb) -> BootInfo {
    let installkernel = pkgs
        .find("sys-kernel/installkernel")
        .map(|p| (p.version.clone(), p.use_flags.clone()));
    let uses = |f: &str| installkernel.as_ref().is_some_and(|(_, u)| u.iter().any(|x| x == f));
    let efi_mount = ["/efi", "/boot/efi", "/boot"]
        .iter()
        .map(PathBuf::from)
        .find(|p| p.join("EFI").is_dir());
    let exists = |p: &str| Path::new(p).exists();

    let bootloader = if uses("grub") {
        Bootloader::Grub
    } else if uses("systemd-boot") {
        Bootloader::SystemdBoot
    } else if uses("uki") {
        Bootloader::Uki
    } else if uses("efistub") {
        Bootloader::Efistub
    } else if uses("refind") {
        Bootloader::Refind
    } else if uses("limine") {
        Bootloader::Limine
    } else if exists("/boot/grub/grub.cfg") {
        Bootloader::Grub
    } else if exists("/boot/loader/entries") || exists("/efi/loader/entries") {
        Bootloader::SystemdBoot
    } else if exists("/boot/limine.conf") || exists("/boot/limine/limine.conf") {
        Bootloader::Limine
    } else if efi_mount.as_ref().is_some_and(|m| m.join("EFI/refind").is_dir()) {
        Bootloader::Refind
    } else {
        Bootloader::Unknown
    };

    let initramfs = if uses("dracut") {
        Initramfs::Dracut
    } else if uses("ugrd") {
        Initramfs::Ugrd
    } else {
        Initramfs::None
    };

    BootInfo {
        bootloader,
        initramfs,
        installkernel,
        uefi: exists("/sys/firmware/efi"),
        secure_boot: secure_boot_state(),
        efi_mount,
    }
}

fn secure_boot_state() -> Option<bool> {
    let p = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c";
    let bytes = std::fs::read(p).ok()?;
    // 4 bytes of attributes followed by the 1-byte value.
    bytes.get(4).map(|b| *b == 1)
}

fn detect_toolchain() -> Toolchain {
    let clang_path = which("clang");
    let clang = clang_path.as_ref().and_then(|p| run(&p.to_string_lossy(), &["--version"]));
    let clang_major = clang.as_ref().and_then(|v| {
        v.split_whitespace()
            .skip_while(|w| *w != "version")
            .nth(1)
            .and_then(|ver| ver.split('.').next())
            .and_then(|m| m.parse().ok())
    });
    let header = |h: &str| Path::new("/usr/include").join(h).exists();
    let kmod_zstd = run("kmod", &["--version"]).is_some_and(|v| v.contains("+ZSTD"));
    Toolchain {
        gcc: run("gcc", &["-dumpfullversion"]),
        clang: clang.map(|v| v.lines().next().unwrap_or_default().to_string()),
        clang_major,
        llvm_bin: clang_path
            .and_then(|p| std::fs::canonicalize(p).ok())
            .and_then(|p| p.parent().map(Path::to_path_buf)),
        lld: which("ld.lld").is_some(),
        llvm_ar: which("llvm-ar").is_some(),
        make: which("make").is_some(),
        patch: which("patch").is_some(),
        bc: which("bc").is_some(),
        flex: which("flex").is_some(),
        bison: which("bison").is_some(),
        perl: which("perl").is_some(),
        cpio: which("cpio").is_some(),
        zstd: which("zstd").is_some(),
        pahole: which("pahole").is_some(),
        libelf: header("libelf.h") || header("elfutils/libelf.h"),
        openssl_headers: header("openssl/opensslv.h"),
        perf: which("perf").is_some(),
        llvm_profgen: which("llvm-profgen").is_some(),
        propeller_tool: which("generate_propeller_profiles").is_some() || which("create_llvm_prof").is_some(),
        rustc: which("rustc").is_some(),
        kmod_zstd,
        curl: which("curl").is_some(),
        wget: which("wget").is_some(),
    }
}

/// A light view of /var/db/pkg — just what Kernelsmith needs.
struct PkgDb {
    all: HashMap<String, InstalledPkg>,
    kernels: Vec<InstalledPkg>,
    module_packages: Vec<InstalledPkg>,
}

impl PkgDb {
    fn scan() -> Self {
        let mut all = HashMap::new();
        let mut kernels = vec![];
        let mut module_packages = vec![];
        let root = Path::new("/var/db/pkg");
        for cat in std::fs::read_dir(root).into_iter().flatten().flatten() {
            let cat_name = cat.file_name().to_string_lossy().into_owned();
            for pkg in std::fs::read_dir(cat.path()).into_iter().flatten().flatten() {
                let pf = pkg.file_name().to_string_lossy().into_owned();
                let Some((name, version)) = split_pf(&pf) else { continue };
                let p = pkg.path();
                let atom = format!("{cat_name}/{name}");
                let interesting = cat_name == "sys-kernel"
                    || matches!(atom.as_str(), "x11-drivers/nvidia-drivers");
                let inherited = std::fs::read_to_string(p.join("INHERITED")).unwrap_or_default();
                let is_module = inherited.split_whitespace().any(|e| e == "linux-mod" || e == "linux-mod-r1");
                if !(interesting || is_module) {
                    all.insert(atom.clone(), InstalledPkg { atom, version: version.into(), use_flags: vec![] });
                    continue;
                }
                let use_flags: Vec<String> = read_trim(p.join("USE"))
                    .unwrap_or_default()
                    .split_whitespace()
                    .map(String::from)
                    .collect();
                let ip = InstalledPkg { atom: atom.clone(), version: version.into(), use_flags };
                if cat_name == "sys-kernel" {
                    kernels.push(ip.clone());
                }
                if is_module {
                    module_packages.push(ip.clone());
                }
                all.insert(atom, ip);
            }
        }
        PkgDb { all, kernels, module_packages }
    }

    fn find(&self, atom: &str) -> Option<InstalledPkg> {
        self.all.get(atom).cloned()
    }
}

/// Split a Portage "PF" (name-version[-rN]) into name and version.
fn split_pf(pf: &str) -> Option<(&str, &str)> {
    let bytes = pf.as_bytes();
    for i in 0..bytes.len().saturating_sub(1) {
        if bytes[i] == b'-' && bytes[i + 1].is_ascii_digit() {
            return Some((&pf[..i], &pf[i + 1..]));
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pf_split() {
        assert_eq!(split_pf("gentoo-sources-7.3.0"), Some(("gentoo-sources", "7.3.0")));
        assert_eq!(split_pf("cachyos-kernel-7.3.0-r1"), Some(("cachyos-kernel", "7.3.0-r1")));
        assert_eq!(split_pf("linux-firmware-20261019"), Some(("linux-firmware", "20261019")));
        assert_eq!(split_pf("g3-1.0"), Some(("g3", "1.0")));
    }

    #[test]
    fn detect_runs() {
        // Smoke test: must never panic, whatever machine runs the tests.
        let s = detect();
        assert!(s.cpu.threads >= 1);
    }
}
