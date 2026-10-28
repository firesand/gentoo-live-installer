//! A build plan: everything the user chose, in a form that can be saved as a
//! preset, validated, turned into a script, and re-used for the next kernel
//! version.

use crate::hw::{Initramfs, InitSystem, SystemInfo};
use crate::kconfig::{KConfig, SymbolIndex};
use crate::patches::{self, PlannedPatch};
use crate::profiles::{self, ProfileId};
use crate::requirements;
use crate::slim::{self, SlimMode};
use crate::sources::{Flavor, KernelSource};
use crate::tweaks::{self, Op, Selection};
use crate::util::{self, slug};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};

pub const PLAN_FORMAT: u32 = 1;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Seed {
    /// The running kernel's /proc/config.gz — known to boot this machine.
    Running,
    /// The `.config` already present in the source tree.
    TreeConfig,
    /// Upstream `make defconfig`.
    Defconfig,
    File(PathBuf),
}

impl Seed {
    pub fn label(&self) -> String {
        match self {
            Seed::Running => "Your current kernel's configuration".into(),
            Seed::TreeConfig => "The source tree's .config".into(),
            Seed::Defconfig => "Upstream defaults (defconfig)".into(),
            Seed::File(p) => format!("File: {}", p.display()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InstallOpts {
    /// `make modules_install` + `make install` (runs Gentoo's installkernel).
    pub install: bool,
    /// `emerge @module-rebuild` for NVIDIA, VirtualBox, ZFS, …
    pub module_rebuild: bool,
    /// Keep out-of-tree modules for the other installed kernels working.
    pub preserve_old_modules: bool,
    /// Point /usr/src/linux at the new tree (`eselect kernel set`).
    pub set_symlink: bool,
    /// `make clean` afterwards to free disk space (vmlinux is kept for AutoFDO).
    pub clean_after: bool,
    /// Install the sysctl + udev runtime tuning pack.
    pub runtime_tuning: bool,
}

impl Default for InstallOpts {
    fn default() -> Self {
        InstallOpts {
            install: true,
            module_rebuild: true,
            preserve_old_modules: true,
            set_symlink: true,
            clean_after: true,
            runtime_tuning: false,
        }
    }
}

/// Profile files for an AutoFDO/Propeller re-optimization build.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProfileData {
    pub afdo: PathBuf,
    pub propeller_prefix: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BuildPlan {
    pub format: u32,
    /// Short name; becomes the kernel's LOCALVERSION (`-ks-<name>`).
    pub name: String,
    pub profile: ProfileId,
    pub source: PathBuf,
    pub seed: Seed,
    pub slim: SlimMode,
    pub keep: Vec<String>,
    pub use_modprobed_db: bool,
    /// Init systems the kernel must support.
    pub inits: Vec<InitSystem>,
    pub tweaks: Selection,
    pub patches: Vec<PlannedPatch>,
    pub install: InstallOpts,
    #[serde(default)]
    pub profile_data: Option<ProfileData>,
    pub jobs: usize,
    /// Where the build tree goes (default /usr/src). Only for build-only plans.
    #[serde(default)]
    pub build_root: Option<PathBuf>,
}

impl BuildPlan {
    pub fn new(sys: &SystemInfo, profile: ProfileId, source: &KernelSource) -> Self {
        let p = profiles::get(profile);
        let has_modules = !sys.external_modules.is_empty();
        BuildPlan {
            format: PLAN_FORMAT,
            name: default_name(profile),
            profile,
            source: source.path.clone(),
            seed: if sys.proc_config { Seed::Running } else { Seed::Defconfig },
            slim: p.slim,
            keep: slim::default_keep(sys, profile, p.slim),
            use_modprobed_db: sys.modprobed_db.is_some(),
            inits: vec![sys.init],
            tweaks: profiles::defaults(profile, sys),
            patches: vec![],
            install: InstallOpts { module_rebuild: has_modules, preserve_old_modules: has_modules, ..Default::default() },
            profile_data: None,
            jobs: sys.cpu.threads,
            build_root: None,
        }
    }

    /// Switch to another profile, keeping source, name and install options.
    pub fn apply_profile(&mut self, sys: &SystemInfo, profile: ProfileId) {
        let p = profiles::get(profile);
        self.profile = profile;
        self.tweaks = profiles::defaults(profile, sys);
        self.slim = p.slim;
        self.keep = slim::default_keep(sys, profile, p.slim);
        if self.name.is_empty() || ALL_DEFAULT_NAMES.contains(&self.name.as_str()) {
            self.name = default_name(profile);
        }
    }

    pub fn localversion(&self) -> String {
        format!("-ks-{}", slug(&self.name))
    }

    /// The `uname -r` the new kernel will report.
    pub fn release(&self, src: &KernelSource) -> String {
        format!("{}{}{}", src.version, src.extraversion, self.localversion())
    }

    pub fn dest_dir(&self, src: &KernelSource) -> PathBuf {
        let root = self.build_root.clone().unwrap_or_else(|| PathBuf::from(crate::sources::SRC_ROOT));
        root.join(format!("linux-{}", self.release(src)))
    }

    /// Build-only plans in a user-writable directory don't need root.
    pub fn needs_root(&self) -> bool {
        self.install.install || self.install.runtime_tuning || self.build_root.is_none()
    }

    /// Symbols that selected patches will add to the tree.
    pub fn provided_symbols(&self) -> HashSet<String> {
        self.patches
            .iter()
            .filter_map(|p| patches::find(&p.id))
            .flat_map(|p| p.provides.iter().map(|s| s.to_string()))
            .collect()
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        std::fs::write(path, toml::to_string_pretty(self)?)?;
        Ok(())
    }

    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        toml::from_str(&text).with_context(|| format!("parsing {}", path.display()))
    }
}

const ALL_DEFAULT_NAMES: [&str; 6] = ["gaming", "desktop", "creator", "workstation", "laptop", "server"];

fn default_name(p: ProfileId) -> String {
    match p {
        ProfileId::Gaming => "gaming",
        ProfileId::Desktop => "desktop",
        ProfileId::Creator => "creator",
        ProfileId::Workstation => "workstation",
        ProfileId::Laptop => "laptop",
        ProfileId::Server => "server",
    }
    .into()
}

/// One labelled batch of Kconfig changes.
#[derive(Debug, Clone)]
pub struct OpGroup {
    pub title: String,
    pub ops: Vec<Op>,
    pub critical: bool,
}

/// What the plan means for a specific source tree.
#[derive(Debug, Clone)]
pub struct Resolved {
    pub release: String,
    pub dest: PathBuf,
    pub groups: Vec<OpGroup>,
    pub lmc_keep: String,
    /// Final expected value per symbol, with whether a mismatch is fatal.
    pub expectations: BTreeMap<String, (Op, bool, String)>,
    /// Options skipped because the tree doesn't define them.
    pub unsupported: Vec<String>,
}

pub fn resolve(plan: &BuildPlan, sys: &SystemInfo, src: &KernelSource, symbols: Option<&SymbolIndex>) -> Resolved {
    let provided = plan.provided_symbols();
    let known = |s: &str| provided.contains(s) || symbols.is_none_or(|i| i.has(s));
    let mut unsupported = vec![];
    let mut filter = |title: &str, ops: Vec<Op>, report: bool| -> Vec<Op> {
        ops.into_iter()
            .filter(|op| {
                let k = known(op.sym());
                if !k && report && !matches!(op, Op::N(_)) {
                    unsupported.push(format!("{title}: {} (not in this source)", op.describe()));
                }
                k
            })
            .collect()
    };

    let mut groups = vec![];
    for (title, ops) in tweaks::ops_for(&plan.tweaks, sys, symbols, &provided) {
        let ops = filter(&title, ops, true);
        if !ops.is_empty() {
            groups.push(OpGroup { title, ops, critical: false });
        }
    }
    let mut lmc = vec![];
    if plan.slim != SlimMode::Off {
        for c in slim::categories().into_iter().filter(|c| plan.keep.iter().any(|k| k == c.id)) {
            lmc.extend(c.lmc_paths.iter().map(|p| p.to_string()));
            let title = format!("Keep: {}", c.title);
            let ops = filter(&title, (c.ops)(sys), false);
            if !ops.is_empty() {
                groups.push(OpGroup { title, ops, critical: false });
            }
        }
    }
    for g in requirements::groups(sys, &plan.inits) {
        let ops = filter(&g.title, g.ops, false);
        if !ops.is_empty() {
            groups.push(OpGroup { title: g.title, ops, critical: g.critical });
        }
    }
    groups.push(OpGroup {
        title: "Kernel name".into(),
        ops: vec![tweaks::sv("LOCALVERSION", &plan.localversion()), tweaks::n("LOCALVERSION_AUTO")],
        critical: true,
    });

    let mut expectations = BTreeMap::new();
    for g in &groups {
        for op in &g.ops {
            expectations.insert(op.sym().to_string(), (op.clone(), g.critical, g.title.clone()));
        }
    }
    lmc.sort();
    lmc.dedup();
    Resolved {
        release: plan.release(src),
        dest: plan.dest_dir(src),
        groups,
        lmc_keep: lmc.join(":"),
        expectations,
        unsupported,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level {
    Info,
    Warn,
    Error,
}

#[derive(Debug, Clone)]
pub struct Finding {
    pub level: Level,
    pub text: String,
}

fn f(level: Level, text: impl Into<String>) -> Finding {
    Finding { level, text: text.into() }
}

/// Everything worth telling the user before they press Build.
pub fn check(plan: &BuildPlan, sys: &SystemInfo, src: Option<&KernelSource>) -> Vec<Finding> {
    let mut out = vec![];
    let Some(src) = src else {
        out.push(f(Level::Error, "Choose a kernel source tree."));
        return out;
    };
    for (tool, pkg) in sys.toolchain.missing_essentials() {
        out.push(f(Level::Error, format!("Missing build tool: {tool} — install {pkg}.")));
    }
    if slug(&plan.name).is_empty() {
        out.push(f(Level::Error, "Give your kernel a name (letters, digits and dashes)."));
    }
    let rel = plan.release(src);
    if Path::new("/lib/modules").join(&rel).exists() || Path::new("/boot").join(format!("vmlinuz-{rel}")).exists() {
        out.push(f(Level::Error, format!("A kernel named {rel} is already installed. Pick another name or uninstall it from the Library.")));
    }
    if plan.dest_dir(src).exists() {
        out.push(f(Level::Error, format!("{} already exists. Pick another name.", plan.dest_dir(src).display())));
    }
    match &plan.seed {
        Seed::Running if !sys.proc_config => {
            out.push(f(Level::Error, "The running kernel doesn't expose /proc/config.gz; choose another base configuration."))
        }
        Seed::TreeConfig if !src.has_config => out.push(f(Level::Error, "This source tree has no .config to start from.")),
        Seed::File(p) if !p.is_file() => out.push(f(Level::Error, format!("Config file {} not found.", p.display()))),
        _ => {}
    }
    if !src.buildable {
        out.push(f(Level::Error, format!(
            "{} only contains headers for building modules, not the full kernel source. Install sys-kernel/gentoo-sources or sys-kernel/cachyos-sources.",
            src.dir_name
        )));
    } else if src.flavor == Flavor::DistPrepared {
        out.push(f(Level::Info, "This tree was prepared by Portage for a dist-kernel; Kernelsmith copies it and runs `make mrproper` on the copy, so the original is untouched."));
    }
    if sys.boot.secure_boot == Some(true) {
        out.push(f(Level::Warn, "Secure Boot is ON. A self-built kernel is unsigned and won't boot unless you sign it (sbctl / shim MOK) or disable Secure Boot."));
    }
    if plan.install.install {
        if sys.boot.installkernel.is_none() {
            out.push(f(Level::Warn, "sys-kernel/installkernel isn't installed: the kernel will be copied to /boot but your bootloader won't be updated. emerge sys-kernel/installkernel with USE=\"grub dracut\" (or systemd-boot)."));
        }
        if sys.boot.initramfs == Initramfs::None {
            if sys.root_on_dm {
                out.push(f(Level::Error, "Your root filesystem is on an encrypted/LVM device, which needs an initramfs, but installkernel isn't set up to build one. Enable USE=dracut (or ugrd) on sys-kernel/installkernel."));
            } else {
                out.push(f(Level::Info, "No initramfs will be generated (installkernel has no dracut/ugrd USE flag). That's fine: your root filesystem and disk drivers are built into the kernel."));
            }
        }
    } else {
        out.push(f(Level::Info, "Install is off: the kernel will be built but not installed."));
    }
    if sys.needs_nvidia_support() && plan.install.install && !plan.install.module_rebuild {
        out.push(f(Level::Warn, "NVIDIA driver detected but 'Rebuild driver modules' is off — the new kernel will boot without graphics acceleration until you run `emerge @module-rebuild`."));
    }
    if let Some(nv) = &sys.nvidia_driver {
        if nv.use_flags.iter().any(|u| u == "dist-kernel") {
            out.push(f(Level::Warn, "nvidia-drivers has USE=dist-kernel, which ties it to your Portage-managed kernel. Kernelsmith will point it at the new tree explicitly, but consider USE=-dist-kernel for custom kernels."));
        }
    }
    if sys.scx_installed && plan.tweaks.get("sched_ext").map(String::as_str) == Some("off") {
        out.push(f(Level::Warn, "sys-kernel/scx is installed but sched_ext is off — scx schedulers won't work on this kernel."));
    }
    if sys.root_fs == "zfs" {
        out.push(f(Level::Warn, "Root on ZFS: make sure sys-fs/zfs-kmod rebuilds (module rebuild on) and your initramfs includes ZFS."));
    }
    if plan.tweaks.get("march").map(String::as_str) == Some("native") {
        out.push(f(Level::Info, "Optimized for this exact CPU — the kernel may not boot if the disk is moved to a PC with an older processor."));
    }
    if plan.tweaks.get("lto").map(String::as_str) == Some("full") && sys.memory_gib() < 16.0 {
        out.push(f(Level::Warn, "Full LTO can need more than 16 GiB of RAM to link. ThinLTO is safer on this machine."));
    }
    if plan.slim != SlimMode::Off {
        if plan.use_modprobed_db && sys.modprobed_db.is_some() {
            out.push(f(Level::Info, "Using your modprobed-db history to decide which drivers to keep."));
        } else if sys.modprobed_db.is_none() {
            out.push(f(Level::Info, "Tip: sys-kernel/modprobed-db records every module you use over days or weeks, giving even safer slimming. Kernelsmith uses it automatically when present."));
        }
        if plan.slim == SlimMode::Aggressive {
            out.push(f(Level::Warn, "Minimal mode: devices that aren't connected right now may not work until you rebuild."));
        }
    }
    for p in &plan.patches {
        if !p.file.is_file() {
            out.push(f(Level::Error, format!("Patch file for '{}' is missing: {}", p.title, p.file.display())));
        }
    }
    let ids: HashSet<&str> = plan.patches.iter().map(|p| p.id.as_str()).collect();
    for p in patches::catalog() {
        if ids.contains(p.id) {
            for c in p.conflicts {
                if ids.contains(c) {
                    out.push(f(Level::Error, format!("'{}' conflicts with '{c}'. Choose one.", p.title)));
                }
            }
        }
    }
    if let Some(pd) = &plan.profile_data {
        if !pd.afdo.is_file() {
            out.push(f(Level::Error, format!("AutoFDO profile {} not found.", pd.afdo.display())));
        }
    }
    let est = estimate(plan, sys);
    let root = plan.build_root.clone().unwrap_or_else(|| PathBuf::from(crate::sources::SRC_ROOT));
    if let Some(free) = util::disk_free(&root) {
        if free < est.disk_bytes {
            out.push(f(Level::Error, format!(
                "Not enough free space in {}: need about {}, have {}.",
                root.display(),
                util::human_bytes(est.disk_bytes),
                util::human_bytes(free)
            )));
        }
    }
    out.sort_by_key(|f| std::cmp::Reverse(f.level));
    out
}

#[derive(Debug, Clone, Copy)]
pub struct Estimate {
    pub objects: u64,
    pub minutes_low: u64,
    pub minutes_high: u64,
    pub disk_bytes: u64,
}

/// Rough build-time and disk estimates for the review screen.
pub fn estimate(plan: &BuildPlan, sys: &SystemInfo) -> Estimate {
    let objects: u64 = match plan.slim {
        SlimMode::Off => {
            let enabled = KConfig::load_running().map(|c| c.counts()).map(|(y, m)| (y + m) as u64).unwrap_or(9000);
            enabled * 2
        }
        // Calibrated on a 7.3 build: 213 loaded modules + 9 keep families ≈ 7,000 files.
        SlimMode::Balanced => 3000 + sys.loaded_modules.len() as u64 * 15 + plan.keep.len() as u64 * 150,
        SlimMode::Aggressive => 2600 + sys.loaded_modules.len() as u64 * 13,
    };
    let threads = plan.jobs.max(1) as f64;
    let clang = plan.tweaks.get("compiler").map(String::as_str) == Some("clang");
    let per_thread_rate = if clang { 1.25 } else { 1.45 }; // objects / second / thread
    let mut secs = objects as f64 / (threads * per_thread_rate);
    secs += match plan.tweaks.get("lto").map(String::as_str) {
        Some("thin") => 90.0,
        Some("full") => 600.0,
        _ => 20.0,
    };
    let debug = plan.tweaks.get("debug").map(String::as_str) != Some("none");
    if debug {
        secs *= 1.25;
    }
    secs += 60.0; // config, modules_install, initramfs
    if plan.install.module_rebuild && !sys.external_modules.is_empty() {
        // NVIDIA takes about a minute; small drivers like r8125 a few seconds.
        secs += 20.0 * sys.external_modules.len() as f64 + if sys.nvidia_driver.is_some() { 60.0 } else { 0.0 };
    }
    let per_obj: u64 = if debug { 450_000 } else { 140_000 };
    let disk = 1_600_000_000 + objects * per_obj;
    let mins = (secs / 60.0).ceil() as u64;
    Estimate { objects, minutes_low: mins.max(1), minutes_high: (mins as f64 * 1.4).ceil() as u64, disk_bytes: disk }
}
