//! The tweaks. Each one mirrors a piece of CachyOS-Settings
//! (<https://github.com/CachyOS/CachyOS-Settings>) and says how to do the same
//! on Gentoo with OpenRC or with systemd. A tweak knows:
//! - whether it applies to this machine (`available`);
//! - the blocks it adds to our managed files and the commands that make it
//!   take effect now (`build`), for the running init system;
//! - how to tell whether the system already behaves that way (`check`).

use crate::detect::{DiskClass, GpuVendor, Init, System, norm_ws, selected};
use crate::managed::*;
use crate::plan::{Cmd, Prog};
use std::os::unix::fs::MetadataExt;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Category {
    Memory,
    Storage,
    Kernel,
    Graphics,
    Audio,
    Gaming,
    Network,
    Services,
}

impl Category {
    pub const ALL: [Category; 8] =
        [Category::Memory, Category::Storage, Category::Kernel, Category::Graphics, Category::Audio, Category::Gaming, Category::Network, Category::Services];

    pub fn label(&self) -> &'static str {
        match self {
            Category::Memory => "Memory & Swap",
            Category::Storage => "Storage & I/O",
            Category::Kernel => "Kernel & Security",
            Category::Graphics => "Graphics",
            Category::Audio => "Audio",
            Category::Gaming => "Gaming",
            Category::Network => "Network",
            Category::Services => "System Services",
        }
    }

    pub fn blurb(&self) -> &'static str {
        match self {
            Category::Memory => "Swap behaviour, compressed swap in RAM, dirty page writeback and transparent huge pages.",
            Category::Storage => "I/O schedulers per drive type and disk link power management.",
            Category::Kernel => "Watchdogs, console messages, magic SysRq and a few security knobs.",
            Category::Graphics => "NVIDIA and AMD driver options.",
            Category::Audio => "Realtime limits, timers and power saving for glitch-free sound.",
            Category::Gaming => "Wine/Proton synchronisation, file and memory-map limits, and launch helpers for Steam.",
            Category::Network => "Packet queues and the Wi-Fi regulatory domain.",
            Category::Services => "Service manager, journal, OOM handling and time sync. Mostly systemd-specific.",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Risk {
    Safe,
    Moderate,
    Advanced,
}

impl Risk {
    pub fn label(&self) -> &'static str {
        match self {
            Risk::Safe => "SAFE",
            Risk::Moderate => "MODERATE",
            Risk::Advanced => "ADVANCED",
        }
    }
}

/// What it takes for an applied tweak to be fully in effect.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum After {
    Now,
    Relogin,
    Reboot,
}

impl After {
    pub fn label(&self) -> &'static str {
        match self {
            After::Now => "immediately",
            After::Relogin => "after you log out and back in",
            After::Reboot => "after a reboot",
        }
    }
}

pub enum Param {
    Fixed,
    Int {
        min: i64,
        max: i64,
    },
    Choice(&'static [&'static str]),
    /// Choices read from the system (e.g. the I/O schedulers this kernel has).
    Dynamic(fn(&System) -> Vec<String>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Na {
    pub reason: String,
    /// What would make it available (a command to run, a package to emerge).
    pub fix: Option<String>,
}

fn na(reason: impl Into<String>) -> Result<(), Na> {
    Err(Na { reason: reason.into(), fix: None })
}

fn na_fix(reason: impl Into<String>, fix: impl Into<String>) -> Result<(), Na> {
    Err(Na { reason: reason.into(), fix: Some(fix.into()) })
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Impl {
    /// (managed file, block body)
    pub blocks: Vec<(&'static str, String)>,
    /// Run after the files are written when enabling (or changing the value).
    pub on_enable: Vec<Cmd>,
    /// Run before the files are removed when disabling (stop services …).
    pub before_disable: Vec<Cmd>,
    /// Run after the files are removed when disabling.
    pub after_disable: Vec<Cmd>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LiveState {
    /// The system behaves as the tweak wants.
    Match,
    Differs,
    /// Could not tell (e.g. needs root to read).
    Unknown,
    /// Nothing to measure: the tweak is a file (a launcher, a slice setting).
    NoLive,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Live {
    pub state: LiveState,
    pub now: String,
    pub want: String,
}

impl Live {
    fn compare(now: impl Into<String>, want: impl Into<String>) -> Live {
        let (now, want) = (now.into(), want.into());
        let state = if norm_ws(&now) == norm_ws(&want) { LiveState::Match } else { LiveState::Differs };
        Live { state, now, want }
    }

    fn unknown(why: impl Into<String>, want: impl Into<String>) -> Live {
        Live { state: LiveState::Unknown, now: why.into(), want: want.into() }
    }

    fn none() -> Live {
        Live { state: LiveState::NoLive, now: String::new(), want: String::new() }
    }

    fn of(ok: bool, now: impl Into<String>, want: impl Into<String>) -> Live {
        Live { state: if ok { LiveState::Match } else { LiveState::Differs }, now: now.into(), want: want.into() }
    }
}

pub struct Ctx<'a> {
    pub sys: &'a System,
    pub tweak: &'a Tweak,
    pub value: &'a str,
}

pub struct Tweak {
    pub id: &'static str,
    pub category: Category,
    pub title: &'static str,
    pub summary: &'static str,
    pub details: &'static str,
    /// The CachyOS-Settings file it mirrors.
    pub source: &'static str,
    pub risk: Risk,
    /// Part of the "CachyOS defaults" preset.
    pub recommended: bool,
    pub default: &'static str,
    pub param: Param,
    pub unit: &'static str,
    pub after: After,
    /// The sysctl key or sysfs path generic tweaks work on.
    pub key: &'static str,
    /// A limit: a higher live value also counts as in effect.
    pub at_least: bool,
    /// Only meaningful with this init system.
    pub only: Option<Init>,
    /// Extra advice shown with the details.
    pub notes: &'static str,
    pub available: fn(&Ctx) -> Result<(), Na>,
    pub build: fn(&Ctx) -> Impl,
    pub check: fn(&Ctx) -> Live,
}

impl Tweak {
    /// Validate a parameter value; `None`/empty means the default.
    pub fn validate(&self, sys: &System, value: Option<&str>) -> Result<String, String> {
        let v = value.map(str::trim).filter(|v| !v.is_empty()).unwrap_or(self.default);
        if matches!(self.param, Param::Fixed) && v == self.default {
            return Ok(v.to_string());
        }
        if !v.chars().all(|c| c.is_ascii_alphanumeric() || "+-_.".contains(c)) {
            return Err(format!("{}: invalid value {v:?}", self.id));
        }
        match &self.param {
            Param::Fixed if v == self.default => Ok(v.to_string()),
            Param::Fixed => Err(format!("{} has no options", self.id)),
            Param::Int { min, max } => match v.parse::<i64>() {
                Ok(n) if (*min..=*max).contains(&n) => Ok(n.to_string()),
                _ => Err(format!("{}: {v} is not a number between {min} and {max}", self.id)),
            },
            Param::Choice(opts) if opts.contains(&v) => Ok(v.to_string()),
            Param::Choice(opts) => Err(format!("{}: {v} is not one of {}", self.id, opts.join(", "))),
            Param::Dynamic(f) => {
                let opts = f(sys);
                if v == self.default || opts.iter().any(|o| o == v) {
                    Ok(v.to_string())
                } else {
                    Err(format!("{}: {v} is not one of {}", self.id, opts.join(", ")))
                }
            }
        }
    }

    pub fn choices(&self, sys: &System) -> Vec<String> {
        match &self.param {
            Param::Choice(o) => o.iter().map(|s| s.to_string()).collect(),
            Param::Dynamic(f) => {
                let mut v = f(sys);
                if !v.iter().any(|x| x == self.default) {
                    v.insert(0, self.default.to_string());
                }
                v
            }
            _ => Vec::new(),
        }
    }

    /// A plain sysctl tweak (its key is a sysctl name, not a sysfs path).
    pub fn is_sysctl(&self) -> bool {
        !self.key.is_empty() && !self.key.starts_with('/')
    }

    pub fn ctx<'a>(&'a self, sys: &'a System, value: &'a str) -> Ctx<'a> {
        Ctx { sys, tweak: self, value }
    }

    /// Available on this machine with this init system?
    pub fn availability(&self, sys: &System) -> Result<(), Na> {
        if let Some(only) = self.only
            && sys.init != only
        {
            return Err(Na { reason: format!("{} only — this system runs {}", only.label(), sys.init.label()), fix: None });
        }
        if sys.init == Init::Unknown {
            return na("the init system could not be detected");
        }
        (self.available)(&self.ctx(sys, self.default))
    }
}

impl std::fmt::Debug for Tweak {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Tweak").field("id", &self.id).finish_non_exhaustive()
    }
}

pub fn get(id: &str) -> Option<&'static Tweak> {
    CATALOG.iter().find(|t| t.id == id)
}

pub fn index_of(id: &str) -> usize {
    CATALOG.iter().position(|t| t.id == id).unwrap_or(usize::MAX)
}

// ───────────────────────── commands ─────────────────────────

fn exec(prog: Prog, args: &[&str]) -> Cmd {
    Cmd::Exec { prog, args: args.iter().map(|s| s.to_string()).collect() }
}

pub fn sysctl_system() -> Cmd {
    exec(Prog::Sysctl, &["-q", "--system"])
}

fn udev_reload() -> Cmd {
    exec(Prog::Udevadm, &["control", "--reload"])
}

fn udev_trigger(subsystems: &[&str]) -> Cmd {
    let mut args = vec!["trigger".to_string(), "--action=change".to_string()];
    args.extend(subsystems.iter().map(|s| format!("--subsystem-match={s}")));
    Cmd::Exec { prog: Prog::Udevadm, args }
}

fn modprobe(m: &str) -> Cmd {
    exec(Prog::Modprobe, &[m])
}

fn script(path: &str, args: &[&str]) -> Cmd {
    exec(Prog::Script(path.to_string()), args)
}

fn daemon_reload() -> Cmd {
    exec(Prog::Systemctl, &["daemon-reload"])
}

fn systemctl(args: &[&str]) -> Cmd {
    exec(Prog::Systemctl, args)
}

/// Enable a service at boot (OpenRC: in `runlevel`).
fn svc_enable(sys: &System, name: &str, runlevel: &str) -> Cmd {
    match sys.init {
        Init::Systemd => systemctl(&["enable", name]),
        _ => exec(Prog::RcUpdate, &["add", name, runlevel]),
    }
}

fn svc_disable(sys: &System, name: &str, runlevel: &str) -> Cmd {
    match sys.init {
        Init::Systemd => systemctl(&["disable", name]),
        _ => exec(Prog::RcUpdate, &["del", name, runlevel]),
    }
}

fn svc(sys: &System, verb: &str, name: &str) -> Cmd {
    match sys.init {
        Init::Systemd => systemctl(&[verb, name]),
        _ => exec(Prog::RcService, &[name, verb]),
    }
}

fn write(path: &str, value: &str) -> Cmd {
    Cmd::Write { path: path.to_string(), value: value.to_string() }
}

fn restore(path: &str) -> Cmd {
    Cmd::Restore { path: path.to_string() }
}

// ───────────────────────── generic behaviours ─────────────────────────

fn always(_: &Ctx) -> Result<(), Na> {
    Ok(())
}

fn no_live(_: &Ctx) -> Live {
    Live::none()
}

fn nothing(_: &Ctx) -> Impl {
    Impl::default()
}

fn sysctl_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.has_sysctl(c.tweak.key) { Ok(()) } else { na(format!("this kernel has no {} setting", c.tweak.key)) }
}

fn sysctl_build(c: &Ctx) -> Impl {
    let path = System::sysctl_path(c.tweak.key);
    Impl {
        blocks: vec![(SYSCTL, format!("# {}\n{} = {}", c.tweak.summary, c.tweak.key, c.value))],
        on_enable: vec![Cmd::Save { path: path.clone() }, sysctl_system()],
        before_disable: vec![],
        after_disable: vec![restore(&path), sysctl_system()],
    }
}

fn sysctl_check(c: &Ctx) -> Live {
    match c.sys.read(&System::sysctl_path(c.tweak.key)) {
        Some(now) if c.tweak.at_least => match (now.parse::<u64>(), c.value.parse::<u64>()) {
            (Ok(n), Ok(w)) => Live::of(n >= w, now, format!("≥ {w}")),
            _ => Live::compare(now, c.value),
        },
        Some(now) => Live::compare(norm_ws(&now), c.value),
        None => Live::unknown("not readable", c.value),
    }
}

/// A sysfs "w!" line in tmpfiles.d (applied at boot) plus an immediate write.
fn sysfs_available(c: &Ctx) -> Result<(), Na> {
    if !c.sys.exists(c.tweak.key) {
        return na(format!("{} does not exist on this kernel", c.tweak.key));
    }
    if !c.sys.has_tool("systemd-tmpfiles") && !c.sys.has_tool("tmpfiles") {
        return na_fix("nothing applies /etc/tmpfiles.d at boot", "emerge --ask sys-apps/systemd-utils (USE=tmpfiles)");
    }
    if !c.sys.tmpfiles_at_boot() {
        return na_fix("tmpfiles.d is not applied at boot", "rc-update add systemd-tmpfiles-setup boot");
    }
    Ok(())
}

fn sysfs_build(c: &Ctx) -> Impl {
    Impl {
        blocks: vec![(TMPFILES, format!("# {}\nw! {} - - - - {}", c.tweak.summary, c.tweak.key, c.value))],
        on_enable: vec![write(c.tweak.key, c.value)],
        before_disable: vec![],
        after_disable: vec![restore(c.tweak.key)],
    }
}

fn sysfs_check(c: &Ctx) -> Live {
    match c.sys.read(c.tweak.key) {
        Some(now) => Live::compare(selected(&now), c.value),
        None => Live::unknown("not readable", c.value),
    }
}

/// A launcher script in /usr/local/bin: installed means in effect.
fn launcher(path: &'static str, body: String) -> Impl {
    Impl { blocks: vec![(path, body)], ..Default::default() }
}

// ───────────────────────── memory & swap ─────────────────────────

fn swappiness_check(c: &Ctx) -> Live {
    let mut l = sysctl_check(c);
    let zram = c.sys.read_raw("/proc/swaps").unwrap_or_default().contains("/dev/zram");
    if l.state == LiveState::Differs && zram && l.now == "150" {
        l.state = LiveState::Match;
        l.now = "150 (raised by the zram rule)".into();
    }
    l
}

fn zram_algorithms(sys: &System) -> Vec<String> {
    match sys.read("/sys/block/zram0/comp_algorithm") {
        Some(t) => t.split_whitespace().map(|a| a.trim_matches(|c| c == '[' || c == ']').to_string()).collect(),
        None => vec!["zstd".into(), "lz4".into(), "lzo-rle".into()],
    }
}

fn zram_available(c: &Ctx) -> Result<(), Na> {
    if !c.sys.has_module("zram") && !c.sys.exists("/sys/class/zram-control") {
        return na("this kernel has no zram support (CONFIG_ZRAM)");
    }
    match c.sys.init {
        Init::Systemd if !c.sys.exists("/usr/lib/systemd/system-generators/zram-generator") => {
            na_fix("zram-generator is not installed", "emerge --ask sys-apps/zram-generator")
        }
        Init::OpenRc if c.sys.in_runlevel("zram-init") => na("zram is already managed by the zram-init service"),
        _ => Ok(()),
    }
}

const ZRAM_RULE: &str = "# While zram swap is active, prefer swapping anonymous pages (they are only
# compressed, not written to disk) and turn zswap off so it does not sit in front
# of zram and hide its statistics from zramctl.
ACTION==\"change\", KERNEL==\"zram0\", ATTR{initstate}==\"1\", SYSCTL{vm.swappiness}=\"150\", \\
    RUN+=\"/bin/sh -c 'echo N > /sys/module/zswap/parameters/enabled'\"";

fn zram_build(c: &Ctx) -> Impl {
    let s = c.sys;
    match s.init {
        Init::Systemd => Impl {
            blocks: vec![
                (ZRAM_GENERATOR, format!("[zram0]\ncompression-algorithm = {}\nzram-size = ram\nswap-priority = 100\nfs-type = swap", c.value)),
                (UDEV_ZRAM, ZRAM_RULE.into()),
            ],
            on_enable: vec![udev_reload(), daemon_reload(), systemctl(&["restart", "systemd-zram-setup@zram0.service"])],
            before_disable: vec![systemctl(&["stop", "systemd-zram-setup@zram0.service"])],
            after_disable: vec![udev_reload(), daemon_reload(), sysctl_system()],
        },
        _ => Impl {
            blocks: vec![(OPENRC_ZRAM, openrc_zram_script(c.value)), (UDEV_ZRAM, ZRAM_RULE.into())],
            on_enable: vec![
                udev_reload(),
                svc_enable(s, "gentoo-settings-zram", "boot"),
                // Restart so a new compression algorithm is picked up.
                svc(s, "restart", "gentoo-settings-zram"),
            ],
            before_disable: vec![svc(s, "stop", "gentoo-settings-zram"), svc_disable(s, "gentoo-settings-zram", "boot")],
            after_disable: vec![udev_reload(), sysctl_system()],
        },
    }
}

fn openrc_zram_script(algo: &str) -> String {
    format!(
        r#"# Compressed swap in RAM: one zram device as large as RAM, {algo}-compressed,
# used before any disk swap (priority 100). Same as CachyOS's zram-generator.conf.
description="Compressed swap in RAM (zram)"

depend() {{
	after modules
	before swap
}}

start() {{
	ebegin "Setting up zram swap ({algo})"
	if [ ! -e /sys/block/zram0 ]; then
		modprobe zram num_devices=1 || {{ eend 1 "cannot load the zram module"; return 1; }}
	fi
	if grep -q '^/dev/zram0 ' /proc/swaps; then
		eend 0
		return 0
	fi
	if [ "$(cat /sys/block/zram0/disksize)" != 0 ]; then
		eend 1 "/dev/zram0 is already in use by something else"
		return 1
	fi
	echo {algo} > /sys/block/zram0/comp_algorithm 2>/dev/null
	mem_kb=$(awk '/^MemTotal:/ {{ print $2 }}' /proc/meminfo)
	echo "$((mem_kb * 1024))" > /sys/block/zram0/disksize &&
		mkswap -L zram0 /dev/zram0 >/dev/null &&
		swapon -p 100 /dev/zram0
	eend $?
}}

stop() {{
	ebegin "Removing zram swap"
	if grep -q '^/dev/zram0 ' /proc/swaps; then
		swapoff /dev/zram0 || {{ eend 1 "swapoff failed (not enough free memory?)"; return 1; }}
	fi
	[ -w /sys/block/zram0/reset ] && echo 1 > /sys/block/zram0/reset
	eend 0
}}"#
    )
}

fn zram_check(c: &Ctx) -> Live {
    let swaps = c.sys.read_raw("/proc/swaps").unwrap_or_default();
    if !swaps.lines().any(|l| l.starts_with("/dev/zram")) {
        let disk = swaps.lines().skip(1).filter_map(|l| l.split_whitespace().next()).collect::<Vec<_>>().join(", ");
        return Live::of(false, if disk.is_empty() { "no swap".to_string() } else { format!("disk swap only ({disk})") }, format!("zram swap ({})", c.value));
    }
    let algo = c.sys.read("/sys/block/zram0/comp_algorithm").map(|a| selected(&a)).unwrap_or_default();
    Live::of(algo == c.value, format!("zram swap ({algo})"), format!("zram swap ({})", c.value))
}

// ───────────────────────── storage ─────────────────────────

fn nvme_schedulers(sys: &System) -> Vec<String> {
    let mut v: Vec<String> = Vec::new();
    for d in sys.disks.iter().filter(|d| d.class() == DiskClass::Nvme) {
        for s in &d.schedulers {
            if !v.contains(s) {
                v.push(s.clone());
            }
        }
    }
    if v.is_empty() {
        v = ["kyber", "none", "mq-deadline", "bfq"].iter().map(|s| s.to_string()).collect();
    }
    v
}

fn wanted_scheduler(class: DiskClass, nvme: &str) -> &str {
    match class {
        DiskClass::Hdd => "bfq",
        DiskClass::Ssd => "mq-deadline",
        DiskClass::Nvme => nvme,
    }
}

fn iosched_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.disks.iter().any(|d| d.scheduler.is_some()) { Ok(()) } else { na("no NVMe, SATA or MMC disks found") }
}

fn iosched_build(c: &Ctx) -> Impl {
    let body = format!(
        "# HDD: BFQ keeps the desktop responsive while a disk is busy.
ACTION==\"add|change\", KERNEL==\"sd[a-z]*\", ENV{{DEVTYPE}}==\"disk\", ATTR{{queue/rotational}}==\"1\", \\
    ATTR{{queue/scheduler}}=\"bfq\"

# SATA SSD and eMMC/SD: mq-deadline.
ACTION==\"add|change\", KERNEL==\"sd[a-z]*|mmcblk[0-9]*\", ENV{{DEVTYPE}}==\"disk\", ATTR{{queue/rotational}}==\"0\", \\
    ATTR{{queue/scheduler}}=\"mq-deadline\"

# NVMe SSD.
ACTION==\"add|change\", KERNEL==\"nvme[0-9]*\", ENV{{DEVTYPE}}==\"disk\", ATTR{{queue/rotational}}==\"0\", \\
    ATTR{{queue/scheduler}}=\"{}\"",
        c.value
    );
    let mut on = Vec::new();
    let mut off = Vec::new();
    for d in &c.sys.disks {
        let want = wanted_scheduler(d.class(), c.value);
        if d.schedulers.iter().any(|s| s == want) {
            let p = format!("/sys/block/{}/queue/scheduler", d.name);
            on.push(write(&p, want));
            off.push(restore(&p));
        }
    }
    on.push(udev_reload());
    off.push(udev_reload());
    Impl { blocks: vec![(UDEV_IOSCHED, body)], on_enable: on, before_disable: vec![], after_disable: off }
}

fn iosched_check(c: &Ctx) -> Live {
    let mut now = Vec::new();
    let mut want = Vec::new();
    let mut ok = true;
    for d in c.sys.disks.iter().filter(|d| d.scheduler.is_some()) {
        let w = wanted_scheduler(d.class(), c.value);
        let cur = d.scheduler.clone().unwrap_or_default();
        // A scheduler this kernel doesn't offer for the disk can't be wrong.
        if cur != w && d.schedulers.iter().any(|s| s == w) {
            ok = false;
        }
        now.push(format!("{} {cur}", d.name));
        want.push(format!("{} {w}", d.name));
    }
    Live::of(ok, now.join(", "), want.join(", "))
}

fn sata_hosts(sys: &System) -> Vec<String> {
    sys.sata_hosts.iter().filter(|h| sys.read(&format!("/sys/class/scsi_host/{h}/link_power_management_supported")).is_none_or(|v| v == "1")).cloned().collect()
}

fn sata_available(c: &Ctx) -> Result<(), Na> {
    if sata_hosts(c.sys).is_empty() { na("no SATA links with power management") } else { Ok(()) }
}

fn sata_build(c: &Ctx) -> Impl {
    let mut on = Vec::new();
    let mut off = Vec::new();
    for h in sata_hosts(c.sys) {
        let p = format!("/sys/class/scsi_host/{h}/link_power_management_policy");
        on.push(write(&p, "max_performance"));
        off.push(restore(&p));
    }
    on.push(udev_reload());
    off.push(udev_reload());
    Impl {
        blocks: vec![(
            UDEV_SATA,
            "# SATA Active Link Power Management off: no latency spikes when a link wakes up.
ACTION==\"add\", SUBSYSTEM==\"scsi_host\", KERNEL==\"host*\", \\
    ATTR{link_power_management_supported}==\"1\", \\
    ATTR{link_power_management_policy}==\"*\", \\
    ATTR{link_power_management_policy}=\"max_performance\""
                .into(),
        )],
        on_enable: on,
        before_disable: vec![],
        after_disable: off,
    }
}

fn sata_check(c: &Ctx) -> Live {
    let hosts = sata_hosts(c.sys);
    let vals: Vec<String> = hosts.iter().map(|h| c.sys.read(&format!("/sys/class/scsi_host/{h}/link_power_management_policy")).unwrap_or_default()).collect();
    let mut uniq = vals.clone();
    uniq.dedup();
    Live::of(vals.iter().all(|v| v == "max_performance"), uniq.join(", "), "max_performance")
}

fn has_hdd(sys: &System) -> bool {
    sys.disks.iter().any(|d| d.kind == crate::detect::DiskKind::Sata && d.rotational && !d.usb)
}

fn hdparm_available(c: &Ctx) -> Result<(), Na> {
    if !has_hdd(c.sys) {
        return na("no spinning SATA disks");
    }
    if !c.sys.has_tool("hdparm") {
        return na_fix("hdparm is not installed", "emerge --ask sys-apps/hdparm");
    }
    Ok(())
}

fn hdparm_build(c: &Ctx) -> Impl {
    let hdparm = c.sys.tool("hdparm").unwrap_or_else(|| "/sbin/hdparm".into());
    Impl {
        blocks: vec![(
            UDEV_HDPARM,
            format!(
                "# Internal hard disks: highest APM level without spin-down (-B 254) and no
# standby timeout (-S 0), so they don't park heads or spin down constantly.
ACTION==\"add|change\", KERNEL==\"sd[a-z]\", ATTR{{queue/rotational}}==\"1\", \\
    ENV{{ID_USB_DRIVER}}==\"\", ENV{{ID_BUS}}==\"ata\", RUN+=\"{hdparm} -B 254 -S 0 /dev/%k\""
            ),
        )],
        on_enable: vec![udev_reload(), udev_trigger(&["block"])],
        before_disable: vec![],
        after_disable: vec![udev_reload()],
    }
}

// ───────────────────────── kernel ─────────────────────────

const WATCHDOGS: &[&str] = &["iTCO_wdt", "sp5100_tco", "wdat_wdt"];

fn watchdog_build(_: &Ctx) -> Impl {
    Impl {
        blocks: vec![(
            MODPROBE,
            "# Hardware watchdog timers: not needed on a desktop; one module less at boot.
blacklist iTCO_wdt
blacklist sp5100_tco
blacklist wdat_wdt"
                .into(),
        )],
        ..Default::default()
    }
}

fn watchdog_check(c: &Ctx) -> Live {
    let loaded: Vec<&str> = WATCHDOGS.iter().copied().filter(|m| c.sys.module_loaded(m)).collect();
    Live::of(loaded.is_empty(), if loaded.is_empty() { "none loaded".to_string() } else { format!("{} loaded", loaded.join(", ")) }, "none loaded")
}

// ───────────────────────── graphics ─────────────────────────

fn nvidia_available(c: &Ctx) -> Result<(), Na> {
    if !c.sys.has_gpu(GpuVendor::Nvidia) {
        return na("no NVIDIA GPU");
    }
    if !c.sys.has_module("nvidia") {
        return na_fix("the proprietary NVIDIA driver is not installed", "emerge --ask x11-drivers/nvidia-drivers");
    }
    Ok(())
}

fn nvidia_mem_build(_: &Ctx) -> Impl {
    Impl {
        blocks: vec![(
            MODPROBE,
            "# Don't zero system memory before handing it to the GPU (faster allocations,
# but freed memory of other processes may become visible to the GPU).
options nvidia NVreg_InitializeSystemMemoryAllocations=0"
                .into(),
        )],
        ..Default::default()
    }
}

fn nvidia_mem_check(c: &Ctx) -> Live {
    match c.sys.read_raw("/proc/driver/nvidia/params") {
        Ok(t) => {
            let v = t.lines().find_map(|l| l.strip_prefix("InitializeSystemMemoryAllocations:")).map(|v| v.trim().to_string()).unwrap_or_default();
            Live::compare(v, "0")
        }
        Err(_) => Live::unknown("driver not loaded", "0"),
    }
}

fn nvidia_pm_available(c: &Ctx) -> Result<(), Na> {
    nvidia_available(c)?;
    if !c.sys.battery {
        return na("for laptops with hybrid graphics — on a desktop the NVIDIA GPU drives the display");
    }
    Ok(())
}

fn nvidia_gpus(sys: &System) -> Vec<String> {
    sys.gpus
        .iter()
        .filter(|g| g.vendor == GpuVendor::Nvidia && g.driver == "nvidia")
        .map(|g| format!("/sys/bus/pci/devices/{}/power/control", g.slot))
        .collect()
}

fn nvidia_pm_build(c: &Ctx) -> Impl {
    let paths = nvidia_gpus(c.sys);
    let mut on: Vec<Cmd> = paths.iter().map(|p| write(p, "auto")).collect();
    on.push(udev_reload());
    let mut off: Vec<Cmd> = paths.iter().map(|p| restore(p)).collect();
    off.push(udev_reload());
    Impl {
        blocks: vec![(
            UDEV_NVIDIA,
            "# Let an idle NVIDIA GPU power down (runtime PM) while the driver is bound.
ACTION==\"add|bind\", SUBSYSTEM==\"pci\", DRIVERS==\"nvidia\", \\
    ATTR{vendor}==\"0x10de\", ATTR{class}==\"0x03[0-9]*\", \\
    TEST==\"power/control\", ATTR{power/control}=\"auto\"

ACTION==\"remove|unbind\", SUBSYSTEM==\"pci\", DRIVERS==\"nvidia\", \\
    ATTR{vendor}==\"0x10de\", ATTR{class}==\"0x03[0-9]*\", \\
    TEST==\"power/control\", ATTR{power/control}=\"on\""
                .into(),
        )],
        on_enable: on,
        before_disable: vec![],
        after_disable: off,
    }
}

fn nvidia_pm_check(c: &Ctx) -> Live {
    let vals: Vec<String> = nvidia_gpus(c.sys).iter().filter_map(|p| c.sys.read(p)).collect();
    if vals.is_empty() {
        return Live::unknown("no NVIDIA GPU bound to the driver", "auto");
    }
    Live::of(vals.iter().all(|v| v == "auto"), vals.join(", "), "auto")
}

fn amd_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.has_gpu(GpuVendor::Amd) { Ok(()) } else { na("no AMD GPU") }
}

fn amdgpu_build(_: &Ctx) -> Impl {
    Impl {
        blocks: vec![(
            MODPROBE,
            "# Drive Southern Islands (GCN 1.0) and Sea Islands (GCN 2.x) cards with amdgpu
# instead of radeon: Vulkan (RADV) and better performance.
options amdgpu si_support=1 cik_support=1
options radeon si_support=0 cik_support=0"
                .into(),
        )],
        ..Default::default()
    }
}

fn amdgpu_check(c: &Ctx) -> Live {
    let drivers: Vec<String> =
        c.sys.gpus.iter().filter(|g| g.vendor == GpuVendor::Amd).map(|g| if g.driver.is_empty() { "no driver".into() } else { g.driver.clone() }).collect();
    Live::of(!drivers.iter().any(|d| d == "radeon"), drivers.join(", "), "amdgpu")
}

// ───────────────────────── audio ─────────────────────────

fn in_group(sys: &System, group: &str) -> Option<bool> {
    let gid = sys.gid_of(group)?;
    let status = sys.read_raw("/proc/self/status").ok()?;
    let groups = status.lines().find_map(|l| l.strip_prefix("Groups:"))?;
    Some(groups.split_whitespace().any(|g| g.parse::<u32>().ok() == Some(gid)))
}

/// (soft, hard) of a /proc/self/limits row, `u64::MAX` for unlimited.
fn self_limit(sys: &System, name: &str) -> Option<(u64, u64)> {
    let t = sys.read_raw("/proc/self/limits").ok()?;
    let rest = t.lines().find_map(|l| l.strip_prefix(name))?;
    let mut it = rest.split_whitespace();
    let p = |s: &str| if s == "unlimited" { Some(u64::MAX) } else { s.parse().ok() };
    Some((p(it.next()?)?, p(it.next()?)?))
}

fn rt_limits_build(_: &Ctx) -> Impl {
    Impl {
        blocks: vec![(
            LIMITS,
            "# Members of the audio group may use realtime priority and a raised nice level
# (JACK, PipeWire without rtkit, pro-audio software).
@audio - rtprio 99
@audio - nice -11"
                .into(),
        )],
        ..Default::default()
    }
}

fn rt_limits_check(c: &Ctx) -> Live {
    match in_group(c.sys, "audio") {
        Some(false) => Live::unknown("you are not in the audio group", "rtprio 99"),
        None => Live::unknown("unknown", "rtprio 99"),
        Some(true) => match self_limit(c.sys, "Max realtime priority") {
            Some((_, hard)) => Live::of(hard >= 99, format!("rtprio {}", if hard == u64::MAX { "unlimited".into() } else { hard.to_string() }), "rtprio 99"),
            None => Live::unknown("unknown", "rtprio 99"),
        },
    }
}

fn dev_group_ok(sys: &System, dev: &str, gid: u32, mode: Option<u32>) -> Option<bool> {
    let m = std::fs::metadata(sys.path(dev)).ok()?;
    Some(m.gid() == gid && mode.is_none_or(|want| m.mode() & 0o777 == want))
}

fn group_name(sys: &System, gid: u32) -> String {
    sys.read_raw("/etc/group")
        .ok()
        .and_then(|t| {
            t.lines().find_map(|l| {
                let f: Vec<&str> = l.split(':').collect();
                (f.get(2)?.parse::<u32>().ok()? == gid).then(|| f[0].to_string())
            })
        })
        .unwrap_or_else(|| gid.to_string())
}

fn hpet_check(c: &Ctx) -> Live {
    let Some(gid) = c.sys.gid_of("audio") else { return Live::unknown("no audio group", "group audio") };
    let mut now = Vec::new();
    let mut ok = true;
    for dev in ["/dev/rtc0", "/dev/hpet"] {
        if let Ok(m) = std::fs::metadata(c.sys.path(dev)) {
            now.push(format!("{} {}", dev.trim_start_matches("/dev/"), group_name(c.sys, m.gid())));
            ok &= dev_group_ok(c.sys, dev, gid, None).unwrap_or(false);
        }
    }
    if now.is_empty() {
        return Live::unknown("no rtc0/hpet device", "group audio");
    }
    Live::of(ok, now.join(", "), "group audio")
}

fn dma_check(c: &Ctx) -> Live {
    let Some(gid) = c.sys.gid_of("audio") else { return Live::unknown("no audio group", "audio, 0660") };
    match std::fs::metadata(c.sys.path("/dev/cpu_dma_latency")) {
        Ok(m) => Live::of(
            dev_group_ok(c.sys, "/dev/cpu_dma_latency", gid, Some(0o660)).unwrap_or(false),
            format!("{}, {:04o}", group_name(c.sys, m.gid()), m.mode() & 0o777),
            "audio, 0660",
        ),
        Err(_) => Live::unknown("no /dev/cpu_dma_latency", "audio, 0660"),
    }
}

fn hda_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.has_module("snd_hda_intel") { Ok(()) } else { na("no Intel HD Audio (snd-hda-intel) device") }
}

const AUDIO_PM_STATE: &str = "/run/gentoo-settings-hda-powersave";

fn audio_pm_build(_: &Ctx) -> Impl {
    let script_body = format!(
        r#"# Turn snd-hda-intel power saving off while on AC power (stops crackles and
# pops on some codecs) and put the old value back on battery.
# Called by 20-gentoo-settings-audio-pm.rules with: init | ac | battery
param=/sys/module/snd_hda_intel/parameters/power_save
state={AUDIO_PM_STATE}

[ -w "$param" ] || exit 0

remember() {{
	cur=$(cat "$param")
	[ "$cur" != 0 ] && echo "$cur" > "$state"
	return 0
}}

# A machine without a mains supply (a desktop) counts as on AC.
on_ac() {{
	for t in /sys/class/power_supply/*/type; do
		[ -e "$t" ] || continue
		[ "$(cat "$t")" = Mains ] || continue
		[ "$(cat "${{t%/type}}/online" 2>/dev/null)" = 1 ] || return 1
	done
	return 0
}}

case "$1" in
init) on_ac && {{ remember; echo 0 > "$param"; }} ;;
ac) remember; echo 0 > "$param" ;;
battery) v=$(cat "$state" 2>/dev/null); echo "${{v:-10}}" > "$param" ;;
*) echo "usage: $0 init|ac|battery" >&2; exit 1 ;;
esac
exit 0"#
    );
    let rule = format!(
        "# Sound card appears: power saving off when on AC.
ACTION==\"add\", SUBSYSTEM==\"sound\", KERNEL==\"card*\", DRIVERS==\"snd_hda_intel\", TEST!=\"{AUDIO_PM_STATE}\", \\
    RUN+=\"{AUDIO_PM_CTL} init\"

ACTION==\"add|change\", SUBSYSTEM==\"power_supply\", ENV{{POWER_SUPPLY_TYPE}}==\"Mains\", ENV{{POWER_SUPPLY_ONLINE}}==\"0\", TEST==\"/sys/module/snd_hda_intel\", \\
    RUN+=\"{AUDIO_PM_CTL} battery\"

ACTION==\"add|change\", SUBSYSTEM==\"power_supply\", ENV{{POWER_SUPPLY_TYPE}}==\"Mains\", ENV{{POWER_SUPPLY_ONLINE}}==\"1\", TEST==\"/sys/module/snd_hda_intel\", \\
    RUN+=\"{AUDIO_PM_CTL} ac\""
    );
    Impl {
        blocks: vec![(AUDIO_PM_CTL, script_body), (UDEV_AUDIO_PM, rule)],
        on_enable: vec![udev_reload(), script(AUDIO_PM_CTL, &["init"])],
        before_disable: vec![script(AUDIO_PM_CTL, &["battery"])],
        after_disable: vec![udev_reload()],
    }
}

fn on_ac(sys: &System) -> bool {
    sys.list("/sys/class/power_supply").iter().all(|p| {
        let base = format!("/sys/class/power_supply/{p}");
        sys.read(&format!("{base}/type")).as_deref() != Some("Mains") || sys.read(&format!("{base}/online")).as_deref() == Some("1")
    })
}

fn audio_pm_check(c: &Ctx) -> Live {
    let Some(now) = c.sys.read("/sys/module/snd_hda_intel/parameters/power_save") else {
        return Live::unknown("snd-hda-intel not loaded", "0 on AC");
    };
    if on_ac(c.sys) { Live::compare(now, "0") } else { Live { state: LiveState::Match, now: format!("{now} (on battery)"), want: "0 on AC".into() } }
}

fn pci_latency_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.has_tool("setpci") { Ok(()) } else { na_fix("setpci is not installed", "emerge --ask sys-apps/pciutils") }
}

fn pci_latency_build(c: &Ctx) -> Impl {
    let s = c.sys;
    let setpci = s.tool("setpci").unwrap_or_else(|| "/usr/bin/setpci".into());
    let body = format!(
        "# Reset the PCI latency timer of every device to 20 cycles (0 for the host
# bridge) and give sound cards 80, so no device holds the bus long enough to
# cause audio gaps. Only conventional PCI has the timer; PCI Express ignores it.
{setpci} -s '*:*' latency_timer=20
{setpci} -s '0:0' latency_timer=0
{setpci} -d '*:*:04xx' latency_timer=80"
    );
    match s.init {
        Init::Systemd => Impl {
            blocks: vec![
                (PCI_LATENCY, body),
                (
                    UNIT_PCI_LATENCY,
                    format!(
                        "[Unit]\nDescription=Adjust latency timers for PCI peripherals\n\n[Service]\nType=oneshot\nExecStart={PCI_LATENCY}\n\n[Install]\nWantedBy=multi-user.target"
                    ),
                ),
            ],
            on_enable: vec![daemon_reload(), systemctl(&["enable", "gentoo-settings-pci-latency.service"]), script(PCI_LATENCY, &[])],
            before_disable: vec![systemctl(&["disable", "gentoo-settings-pci-latency.service"])],
            after_disable: vec![daemon_reload()],
        },
        _ => Impl {
            blocks: vec![(PCI_LATENCY, body), (LOCALD_PCI_LATENCY, format!("# Run at boot by OpenRC's \"local\" service.\n{PCI_LATENCY}"))],
            on_enable: vec![script(PCI_LATENCY, &[])],
            before_disable: vec![],
            after_disable: vec![],
        },
    }
}

fn pci_latency_local_available(c: &Ctx) -> Result<(), Na> {
    pci_latency_available(c)?;
    if c.sys.init == Init::OpenRc && !c.sys.in_runlevel("local") {
        return na_fix("OpenRC's \"local\" service (which runs /etc/local.d) is not enabled", "rc-update add local default");
    }
    Ok(())
}

// ───────────────────────── gaming ─────────────────────────

fn ntsync_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.has_module("ntsync") || c.sys.exists("/dev/ntsync") { Ok(()) } else { na("this kernel has no ntsync driver (CONFIG_NTSYNC, Linux 6.14+)") }
}

fn ntsync_build(_: &Ctx) -> Impl {
    Impl {
        blocks: vec![(MODULES_LOAD, "# NT synchronisation primitives for Wine/Proton (faster, more correct than esync/fsync).\nntsync".into())],
        on_enable: vec![modprobe("ntsync")],
        ..Default::default()
    }
}

fn ntsync_check(c: &Ctx) -> Live {
    let ok = c.sys.exists("/dev/ntsync");
    Live::of(ok, if ok { "/dev/ntsync present" } else { "not loaded" }, "/dev/ntsync present")
}

fn nofile_build(c: &Ctx) -> Impl {
    match c.sys.init {
        Init::Systemd => Impl {
            blocks: vec![(SYSTEM_CONF, "[Manager]\nDefaultLimitNOFILE=2048:2097152".into()), (USER_CONF, "[Manager]\nDefaultLimitNOFILE=1024:1048576".into())],
            ..Default::default()
        },
        _ => Impl {
            blocks: vec![(
                LIMITS,
                "# Allow programs to raise their open-file limit to 1048576 (Wine/Proton esync,
# game engines, build tools). The soft limit is left as it is.
* hard nofile 1048576"
                    .into(),
            )],
            ..Default::default()
        },
    }
}

fn nofile_check(c: &Ctx) -> Live {
    match self_limit(c.sys, "Max open files") {
        Some((soft, hard)) => Live::of(hard >= 1_048_576, format!("soft {soft}, hard {hard}"), "hard ≥ 1048576"),
        None => Live::unknown("unknown", "hard ≥ 1048576"),
    }
}

fn game_performance_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.has_tool("powerprofilesctl") || c.sys.has_tool("tlpctl") {
        Ok(())
    } else {
        na_fix("needs power-profiles-daemon (or TLP)", "emerge --ask sys-power/power-profiles-daemon")
    }
}

fn game_performance_build(_: &Ctx) -> Impl {
    launcher(
        GAME_PERFORMANCE,
        r#"# Run a game with the "performance" power profile for as long as it runs and
# keep the screen from blanking. Steam launch options: game-performance %command%
# GAME_PERFORMANCE_SCREENSAVER_ON=1 leaves the screensaver alone.
if command -v powerprofilesctl >/dev/null 2>&1; then
	profile=powerprofilesctl
elif command -v tlpctl >/dev/null 2>&1; then
	profile=tlpctl
else
	echo "game-performance: neither powerprofilesctl nor tlpctl is installed" >&2
	exec "$@"
fi

# CPU drivers without a performance profile: just run the game.
"$profile" list 2>/dev/null | grep -q 'performance:' || exec "$@"

if [ -z "$GAME_PERFORMANCE_SCREENSAVER_ON" ]; then
	# systemd-logind or elogind, whichever this system has.
	for inhibit in systemd-inhibit elogind-inhibit; do
		if command -v "$inhibit" >/dev/null 2>&1; then
			exec "$inhibit" --why "game-performance is running" \
				"$profile" launch -p performance -r "game-performance" -- "$@"
		fi
	done
fi
exec "$profile" launch -p performance -r "game-performance" -- "$@""#
            .into(),
    )
}

fn zink_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.exists("/usr/share/glvnd/egl_vendor.d/50_mesa.json") {
        Ok(())
    } else {
        na_fix("Mesa (with glvnd) is not installed", "emerge --ask media-libs/mesa")
    }
}

fn zink_build(_: &Ctx) -> Impl {
    launcher(
        ZINK_RUN,
        "# Run an OpenGL program on Zink (Mesa's OpenGL over Vulkan): zink-run %command%
export MESA_LOADER_DRIVER_OVERRIDE=zink
export GALLIUM_DRIVER=zink
export __GLX_VENDOR_LIBRARY_NAME=mesa
export __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/50_mesa.json
exec \"$@\""
            .into(),
    )
}

fn dlss_build(_: &Ctx) -> Impl {
    launcher(
        DLSS_SWAPPER,
        "# Proton/DXVK-NVAPI: use the newest DLSS presets for upscaling, ray reconstruction
# and frame generation, and let NGX update the DLSS libraries. dlss-swapper %command%
export PROTON_ENABLE_NGX_UPDATER=1
export DXVK_NVAPI_DRS_NGX_DLSS_RR_OVERRIDE=on
export DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE=on
export DXVK_NVAPI_DRS_NGX_DLSS_FG_OVERRIDE=on
export DXVK_NVAPI_DRS_NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION=render_preset_latest
export DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION=render_preset_latest
exec \"$@\""
            .into(),
    )
}

fn dlss_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.has_gpu(GpuVendor::Nvidia) { Ok(()) } else { na("no NVIDIA GPU") }
}

// ───────────────────────── network ─────────────────────────

fn regdomain_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.wifi.is_empty() {
        return na("no Wi-Fi adapter");
    }
    if !c.sys.has_tool("iw") {
        return na_fix("iw is not installed", "emerge --ask net-wireless/iw");
    }
    Ok(())
}

fn regdomain_build(c: &Ctx) -> Impl {
    let iw = c.sys.tool("iw").unwrap_or_else(|| "/usr/bin/iw".into());
    let body = format!(
        r#"# Set the Wi-Fi regulatory domain from the configured time zone so the card
# uses the channels and power levels that are legal where you are.
# COUNTRY=XX in /etc/iw-regdomain overrides the guess.
if [ -r /etc/iw-regdomain ]; then
	c=$(sed -n 's/^COUNTRY=//p' /etc/iw-regdomain | head -n 1)
	[ -n "$c" ] && exec {iw} reg set "$c"
fi

zone=
if [ -L /etc/localtime ]; then
	zone=$(readlink /etc/localtime | sed -e 's|.*/zoneinfo/||' -e 's|^posix/||' -e 's|^right/||')
elif [ -r /etc/timezone ]; then
	zone=$(cat /etc/timezone)
fi
[ -n "$zone" ] || {{ logger -t iw-set-regdomain "no time zone configured"; exit 0; }}

lookup() {{
	awk -v z="$1" '!/^#/ && $3 == z {{ print $1; exit }}' /usr/share/zoneinfo/zone.tab 2>/dev/null
}}
c=$(lookup "$zone")
# Backward-compatible names (Asia/Calcutta …) resolve through tzdata.zi links.
if [ -z "$c" ] && [ -r /usr/share/zoneinfo/tzdata.zi ]; then
	canon=$(awk -v z="$zone" '$1 == "L" && $3 == z {{ print $2; exit }}' /usr/share/zoneinfo/tzdata.zi)
	[ -n "$canon" ] && c=$(lookup "$canon")
fi
[ -n "$c" ] || {{ logger -t iw-set-regdomain "no country for time zone $zone"; exit 0; }}

logger -t iw-set-regdomain "setting regulatory domain $c (time zone $zone)"
exec {iw} reg set "$c""#
    );
    Impl {
        blocks: vec![
            (IW_REGDOMAIN, body),
            (
                UDEV_REGDOM,
                format!("# Set the regulatory domain whenever a Wi-Fi device appears.\nSUBSYSTEM==\"ieee80211\", ACTION==\"add\", RUN+=\"{IW_REGDOMAIN}\""),
            ),
        ],
        on_enable: vec![udev_reload(), script(IW_REGDOMAIN, &[])],
        before_disable: vec![],
        after_disable: vec![udev_reload()],
    }
}

fn nm_dns_available(c: &Ctx) -> Result<(), Na> {
    if !c.sys.has_tool("NetworkManager") {
        return na("NetworkManager is not installed");
    }
    if !c.sys.exists("/run/systemd/resolve") {
        return na_fix("systemd-resolved is not running", "systemctl enable --now systemd-resolved");
    }
    Ok(())
}

// ───────────────────────── services (systemd) ─────────────────────────

fn timeouts_build(_: &Ctx) -> Impl {
    let b = "[Manager]\nDefaultTimeoutStartSec=15s\nDefaultTimeoutStopSec=10s".to_string();
    Impl { blocks: vec![(SYSTEM_CONF, b.clone()), (USER_CONF, b)], ..Default::default() }
}

fn journald_build(c: &Ctx) -> Impl {
    Impl {
        blocks: vec![(JOURNALD, format!("[Journal]\nSystemMaxUse={}M", c.value))],
        on_enable: vec![systemctl(&["restart", "systemd-journald.service"])],
        before_disable: vec![],
        after_disable: vec![systemctl(&["restart", "systemd-journald.service"])],
    }
}

fn journald_check(c: &Ctx) -> Live {
    if !c.sys.is_dir("/var/log/journal") {
        return Live::unknown("journal is not persistent", format!("≤ {} MiB", c.value));
    }
    let used = dir_size(&c.sys.path("/var/log/journal"));
    let limit = c.value.parse::<u64>().unwrap_or(50) * 1024 * 1024;
    Live::of(used <= limit + limit / 10, format!("{} MiB on disk", used / 1024 / 1024), format!("≤ {} MiB", c.value))
}

fn dir_size(p: &std::path::Path) -> u64 {
    let Ok(rd) = std::fs::read_dir(p) else { return 0 };
    rd.flatten()
        .map(|e| match e.metadata() {
            Ok(m) if m.is_dir() => dir_size(&e.path()),
            Ok(m) => m.len(),
            Err(_) => 0,
        })
        .sum()
}

fn oomd_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.exists("/usr/lib/systemd/systemd-oomd") || c.sys.exists("/lib/systemd/systemd-oomd") { Ok(()) } else { na("systemd-oomd is not installed") }
}

fn oomd_build(_: &Ctx) -> Impl {
    let slice = "[Slice]\nManagedOOMSwap=kill\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=80%".to_string();
    Impl {
        blocks: vec![(OOMD_SYSTEM, slice.clone()), (OOMD_USER, slice), (OOMD_SESSION, "[Slice]\nManagedOOMPreference=omit".into())],
        on_enable: vec![daemon_reload()],
        before_disable: vec![],
        after_disable: vec![daemon_reload()],
    }
}

fn delegate_build(_: &Ctx) -> Impl {
    Impl {
        blocks: vec![(DELEGATE, "[Service]\nDelegate=cpu cpuset io memory pids".into())],
        on_enable: vec![daemon_reload()],
        before_disable: vec![],
        after_disable: vec![daemon_reload()],
    }
}

fn timesyncd_available(c: &Ctx) -> Result<(), Na> {
    let enabled =
        ["/etc/systemd/system/sysinit.target.wants/systemd-timesyncd.service", "/etc/systemd/system/multi-user.target.wants/systemd-timesyncd.service"]
            .iter()
            .any(|p| c.sys.exists(p));
    if enabled { Ok(()) } else { na("systemd-timesyncd is not enabled (chrony or ntpd keeps time here)") }
}

fn timesyncd_build(_: &Ctx) -> Impl {
    Impl {
        blocks: vec![(
            TIMESYNCD,
            "[Time]\nNTP=time.cloudflare.com\nFallbackNTP=time.google.com 0.gentoo.pool.ntp.org 1.gentoo.pool.ntp.org 2.gentoo.pool.ntp.org 3.gentoo.pool.ntp.org".into(),
        )],
        on_enable: vec![systemctl(&["restart", "systemd-timesyncd.service"])],
        before_disable: vec![],
        after_disable: vec![systemctl(&["restart", "systemd-timesyncd.service"])],
    }
}

fn coredump_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.exists("/usr/lib/systemd/systemd-coredump") || c.sys.exists("/lib/systemd/systemd-coredump") {
        Ok(())
    } else {
        na("systemd-coredump is not installed")
    }
}

fn coredump_build(_: &Ctx) -> Impl {
    Impl { blocks: vec![(TMPFILES, "# Delete core dumps after 3 days instead of 2 weeks.\ne /var/lib/systemd/coredump - - - 3d".into())], ..Default::default() }
}

fn rtkit_available(c: &Ctx) -> Result<(), Na> {
    if c.sys.has_pkg("sys-auth/rtkit") { Ok(()) } else { na("rtkit is not installed") }
}

fn rtkit_build(_: &Ctx) -> Impl {
    Impl {
        blocks: vec![(RTKIT, "[Service]\nLogLevelMax=info".into())],
        on_enable: vec![daemon_reload()],
        before_disable: vec![],
        after_disable: vec![daemon_reload()],
    }
}

fn nm_dns_build(_: &Ctx) -> Impl {
    Impl {
        blocks: vec![(NM_DNS, "[main]\ndns=systemd-resolved".into())],
        on_enable: vec![systemctl(&["reload", "NetworkManager.service"])],
        before_disable: vec![],
        after_disable: vec![systemctl(&["reload", "NetworkManager.service"])],
    }
}

// ───────────────────────── the catalog ─────────────────────────

const BASE: Tweak = Tweak {
    id: "",
    category: Category::Kernel,
    title: "",
    summary: "",
    details: "",
    source: "",
    risk: Risk::Safe,
    recommended: true,
    default: "",
    param: Param::Fixed,
    unit: "",
    after: After::Now,
    key: "",
    at_least: false,
    only: None,
    notes: "",
    available: always,
    build: nothing,
    check: no_live,
};

const SYSCTL_BASE: Tweak =
    Tweak { source: "usr/lib/sysctl.d/70-cachyos-settings.conf", available: sysctl_available, build: sysctl_build, check: sysctl_check, ..BASE };

const SYSFS_BASE: Tweak = Tweak { category: Category::Memory, available: sysfs_available, build: sysfs_build, check: sysfs_check, ..BASE };

const SYSTEMD_BASE: Tweak = Tweak { category: Category::Services, only: Some(Init::Systemd), after: After::Reboot, ..BASE };

pub static CATALOG: &[Tweak] = &[
    // ── Memory & swap ──
    Tweak {
        id: "zram",
        category: Category::Memory,
        title: "Compressed swap in RAM (zram)",
        summary: "A swap device in RAM as large as RAM, compressed, used before disk swap.",
        details: "Instead of writing idle memory to disk, the kernel compresses it into a zram device in RAM. Compression is \
                  fast on modern CPUs and typically fits 2–4× more data, so you can run more before anything hits the disk. \
                  While zram is active, swappiness is raised to 150 and zswap is turned off (it would otherwise sit in front of zram). \
                  Your disk swap stays as a lower-priority fallback.",
        source: "usr/lib/systemd/zram-generator.conf, usr/lib/udev/rules.d/30-zram.rules",
        risk: Risk::Moderate,
        default: "zstd",
        param: Param::Dynamic(zram_algorithms),
        notes: "OpenRC: installs the gentoo-settings-zram service in the boot runlevel. systemd: a zram-generator drop-in (needs sys-apps/zram-generator).",
        available: zram_available,
        build: zram_build,
        check: zram_check,
        ..BASE
    },
    Tweak {
        id: "vm.swappiness",
        category: Category::Memory,
        title: "Swappiness",
        summary: "How readily the kernel swaps out anonymous memory instead of dropping page cache.",
        details: "Low values make the kernel prefer dropping cached files, high values make it prefer swapping out program memory. \
                  100 means both are assumed to cost the same — right for zram, where 'swapping' is just compressing. \
                  With only disk swap, values between 10 and 60 are more common.",
        key: "vm.swappiness",
        default: "100",
        param: Param::Int { min: 0, max: 200 },
        check: swappiness_check,
        ..SYSCTL_BASE
    },
    Tweak {
        id: "vm.vfs_cache_pressure",
        category: Category::Memory,
        title: "VFS cache pressure",
        summary: "Keep directory and inode caches longer.",
        details: "Below the default of 100 the kernel is less eager to reclaim the dentry and inode caches, so walking big \
                  directory trees (builds, Steam libraries, file managers) stays fast. Never set it to 0.",
        key: "vm.vfs_cache_pressure",
        default: "50",
        param: Param::Int { min: 1, max: 500 },
        ..SYSCTL_BASE
    },
    Tweak {
        id: "vm.dirty_bytes",
        category: Category::Memory,
        title: "Dirty data limit",
        summary: "A writing process flushes itself once 256 MiB of dirty data are pending.",
        details: "The default is a percentage of RAM, which on a big-memory machine lets gigabytes of unwritten data pile up and \
                  then stall the system while they are flushed. A fixed 256 MiB keeps writeback smooth.",
        key: "vm.dirty_bytes",
        default: "268435456",
        unit: "bytes",
        param: Param::Int { min: 16_777_216, max: 4_294_967_296 },
        ..SYSCTL_BASE
    },
    Tweak {
        id: "vm.dirty_background_bytes",
        category: Category::Memory,
        title: "Background writeback threshold",
        summary: "Background flusher threads start writing at 64 MiB of dirty data.",
        details: "Starts writing dirty pages to disk early and in the background, long before writers have to block.",
        key: "vm.dirty_background_bytes",
        default: "67108864",
        unit: "bytes",
        param: Param::Int { min: 4_194_304, max: 2_147_483_648 },
        ..SYSCTL_BASE
    },
    Tweak {
        id: "vm.dirty_writeback_centisecs",
        category: Category::Memory,
        title: "Writeback interval",
        summary: "Flusher threads wake every 15 s instead of every 5 s.",
        details: "Fewer periodic wake-ups of the writeback threads; the byte thresholds above still force writeback when needed.",
        key: "vm.dirty_writeback_centisecs",
        default: "1500",
        unit: "1/100 s",
        param: Param::Int { min: 100, max: 6000 },
        ..SYSCTL_BASE
    },
    Tweak {
        id: "vm.page-cluster",
        category: Category::Memory,
        title: "Swap read-ahead",
        summary: "Read one page at a time from swap (no read-ahead).",
        details: "Swap read-ahead helps spinning disks; with zram every extra page is wasted decompression. Use 1 for swap on an SSD \
                  and 2 for a hard disk if you don't use zram.",
        key: "vm.page-cluster",
        default: "0",
        param: Param::Int { min: 0, max: 3 },
        ..SYSCTL_BASE
    },
    Tweak {
        id: "thp-defrag",
        title: "Transparent huge pages: defrag",
        summary: "defer+madvise: compact memory in the background, synchronously only for madvise users.",
        details: "Programs that ask for huge pages (tcmalloc, jemalloc, many games and databases) get them reliably, while everyone \
                  else never stalls waiting for memory compaction.",
        source: "usr/lib/tmpfiles.d/thp.conf",
        key: "/sys/kernel/mm/transparent_hugepage/defrag",
        default: "defer+madvise",
        param: Param::Choice(&["defer+madvise", "madvise", "defer", "always", "never"]),
        notes: "Applied at boot by tmpfiles.d (OpenRC: the systemd-tmpfiles-setup service).",
        ..SYSFS_BASE
    },
    Tweak {
        id: "thp-shrinker",
        title: "Transparent huge pages: shrinker",
        summary: "Split huge pages that are more than 80 % zeros (max_ptes_none 409).",
        details: "With THP 'always', huge pages can waste a lot of memory in sparsely used areas. The THP shrinker (Linux 6.12+) \
                  splits any huge page with more than 409 of its 512 sub-pages empty, bringing memory use close to 'madvise' \
                  while keeping the speed-up.",
        source: "usr/lib/tmpfiles.d/thp-shrinker.conf",
        key: "/sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none",
        default: "409",
        param: Param::Int { min: 0, max: 511 },
        ..SYSFS_BASE
    },
    // ── Storage ──
    Tweak {
        id: "io-schedulers",
        category: Category::Storage,
        title: "I/O schedulers per drive type",
        summary: "BFQ for hard disks, mq-deadline for SATA SSDs, your choice for NVMe.",
        details: "Hard disks benefit from BFQ's fairness (the desktop stays responsive during big copies), SATA SSDs from the \
                  low-overhead mq-deadline. CachyOS uses kyber for NVMe; 'none' has the least overhead, 'adios' is CachyOS-kernel only.",
        source: "usr/lib/udev/rules.d/60-ioschedulers.rules",
        default: "kyber",
        param: Param::Dynamic(nvme_schedulers),
        available: iosched_available,
        build: iosched_build,
        check: iosched_check,
        ..BASE
    },
    Tweak {
        id: "sata-alpm",
        category: Category::Storage,
        title: "SATA link power: max performance",
        summary: "Keep SATA links fully powered.",
        details: "Aggressive SATA link power management saves a little power but adds latency when a link wakes up, and causes \
                  errors with some drives. Desktops lose nothing by turning it off; on a laptop it costs some battery.",
        source: "usr/lib/udev/rules.d/50-sata.rules",
        available: sata_available,
        build: sata_build,
        check: sata_check,
        ..BASE
    },
    Tweak {
        id: "hdparm",
        category: Category::Storage,
        title: "Hard disks: no spin-down",
        summary: "hdparm -B 254 -S 0 on internal hard disks.",
        details: "Stops internal hard disks from parking their heads and spinning down all the time, which costs seconds of \
                  latency on access and wears the drive.",
        source: "usr/lib/udev/rules.d/69-hdparm.rules",
        risk: Risk::Moderate,
        available: hdparm_available,
        build: hdparm_build,
        ..BASE
    },
    // ── Kernel ──
    Tweak {
        id: "kernel.nmi_watchdog",
        category: Category::Kernel,
        title: "NMI watchdog off",
        summary: "Disable the NMI lockup detector.",
        details: "The NMI watchdog periodically interrupts every CPU to detect hard lockups. Desktops don't need it; turning it \
                  off frees a performance counter and avoids the interrupts.",
        key: "kernel.nmi_watchdog",
        default: "0",
        ..SYSCTL_BASE
    },
    Tweak {
        id: "watchdog-blacklist",
        category: Category::Kernel,
        title: "Blacklist hardware watchdogs",
        summary: "Don't load the iTCO_wdt, sp5100_tco and wdat_wdt watchdog drivers.",
        details: "Hardware watchdog timers reboot a hung server. On a desktop they only add a module at boot and occasionally \
                  slow shutdown.",
        source: "usr/lib/modprobe.d/blacklist.conf",
        after: After::Reboot,
        notes: "If these modules are in your initramfs, rebuild it (dracut --force) for the blacklist to apply there too.",
        build: watchdog_build,
        check: watchdog_check,
        ..BASE
    },
    Tweak {
        id: "kernel.printk",
        category: Category::Kernel,
        title: "Quiet console",
        summary: "Only errors and worse reach the console.",
        details: "Keeps kernel warnings from scribbling over the boot splash and text consoles. Everything is still in dmesg.",
        key: "kernel.printk",
        default: "3 3 3 3",
        ..SYSCTL_BASE
    },
    Tweak {
        id: "kernel.kptr_restrict",
        category: Category::Kernel,
        title: "Hide kernel pointers",
        summary: "Kernel addresses in /proc are hidden from everyone.",
        details: "Makes it harder for an exploit to find kernel structures. Profilers like perf then need root to resolve kernel symbols.",
        key: "kernel.kptr_restrict",
        default: "2",
        param: Param::Int { min: 0, max: 2 },
        ..SYSCTL_BASE
    },
    Tweak {
        id: "kernel.sysrq",
        category: Category::Kernel,
        title: "Magic SysRq keys",
        summary: "Enable all Alt+SysRq functions (REISUB to reboot a frozen system safely).",
        details: "With everything enabled, Alt+SysRq+R,E,I,S,U,B unsticks the keyboard, ends processes, syncs and unmounts disks \
                  and reboots — far safer than the power button. Anyone at the physical keyboard can use it.",
        key: "kernel.sysrq",
        default: "1",
        risk: Risk::Moderate,
        param: Param::Int { min: 0, max: 511 },
        ..SYSCTL_BASE
    },
    Tweak {
        id: "kernel.unprivileged_userns_clone",
        category: Category::Kernel,
        title: "Unprivileged user namespaces",
        summary: "Let normal users create user namespaces.",
        details: "Needed by rootless containers (Podman, Distrobox), Flatpak/bubblewrap sandboxes, Steam's pressure-vessel runtime \
                  and Chromium's sandbox. Only exists on kernels carrying the Debian/Arch patch (CachyOS kernels do).",
        key: "kernel.unprivileged_userns_clone",
        default: "1",
        ..SYSCTL_BASE
    },
    Tweak {
        id: "fs.file-max",
        category: Category::Kernel,
        title: "System-wide open file limit",
        summary: "Up to 2097152 open files system-wide.",
        details: "Raises the kernel-wide cap on open file handles; pairs with the per-process limit under Gaming.",
        key: "fs.file-max",
        at_least: true,
        default: "2097152",
        param: Param::Int { min: 65_536, max: 9_223_372_036_854_775_807 },
        ..SYSCTL_BASE
    },
    // ── Graphics ──
    Tweak {
        id: "nvidia-memory-init",
        category: Category::Graphics,
        title: "NVIDIA: skip zeroing system memory",
        summary: "NVreg_InitializeSystemMemoryAllocations=0",
        details: "The driver normally clears system memory before giving it to the GPU. Skipping that makes allocations faster, \
                  but memory freed by other processes could become readable by GPU programs. CachyOS enables this by default; \
                  here it is opt-in because it trades away a security guarantee.",
        source: "usr/lib/modprobe.d/nvidia.conf",
        risk: Risk::Advanced,
        recommended: false,
        after: After::Reboot,
        notes: "If nvidia is in your initramfs (dracut includes /etc/modprobe.d), rebuild it: dracut --force.",
        available: nvidia_available,
        build: nvidia_mem_build,
        check: nvidia_mem_check,
        ..BASE
    },
    Tweak {
        id: "nvidia-runtime-pm",
        category: Category::Graphics,
        title: "NVIDIA: runtime power management",
        summary: "Let an idle NVIDIA GPU power down on hybrid-graphics laptops.",
        details: "On Optimus laptops the discrete GPU can switch off completely while the integrated GPU drives the screen, \
                  saving several watts of battery.",
        source: "usr/lib/udev/rules.d/71-nvidia.rules",
        available: nvidia_pm_available,
        build: nvidia_pm_build,
        check: nvidia_pm_check,
        ..BASE
    },
    Tweak {
        id: "amdgpu-si-cik",
        category: Category::Graphics,
        title: "AMD: amdgpu for GCN 1.0/2.x",
        summary: "Drive Southern/Sea Islands Radeons with amdgpu instead of radeon.",
        details: "Radeon HD 7000, R7/R9 200 and some 300 cards default to the old radeon driver. amdgpu gives them Vulkan (RADV) \
                  and better performance. Newer AMD cards already use amdgpu and are unaffected.",
        source: "usr/lib/modprobe.d/amdgpu.conf",
        after: After::Reboot,
        notes: "Rebuild the initramfs if it loads radeon early (dracut --force).",
        available: amd_available,
        build: amdgpu_build,
        check: amdgpu_check,
        ..BASE
    },
    // ── Audio ──
    Tweak {
        id: "audio-rt-limits",
        category: Category::Audio,
        title: "Realtime priority for the audio group",
        summary: "@audio: rtprio 99, nice -11.",
        details: "Lets members of the audio group run audio threads with realtime priority so sound doesn't drop out under load. \
                  Add yourself with: gpasswd -a $USER audio.",
        source: "etc/security/limits.d/20-audio.conf",
        after: After::Relogin,
        build: rt_limits_build,
        check: rt_limits_check,
        ..BASE
    },
    Tweak {
        id: "hpet-rtc-audio",
        category: Category::Audio,
        title: "Timers for the audio group",
        summary: "/dev/rtc0 and /dev/hpet owned by group audio.",
        details: "Gives audio software (and MIDI sequencers) access to high-resolution hardware timers.",
        source: "usr/lib/udev/rules.d/40-hpet-permissions.rules",
        build: |_| Impl {
            blocks: vec![(
                UDEV_HPET,
                "# High-resolution timers for audio software.\nKERNEL==\"rtc0\", GROUP=\"audio\"\nKERNEL==\"hpet\", GROUP=\"audio\"".into(),
            )],
            on_enable: vec![udev_reload(), udev_trigger(&["rtc", "misc"])],
            before_disable: vec![],
            after_disable: vec![udev_reload(), udev_trigger(&["rtc", "misc"])],
        },
        check: hpet_check,
        ..BASE
    },
    Tweak {
        id: "cpu-dma-latency",
        category: Category::Audio,
        title: "CPU latency control for the audio group",
        summary: "/dev/cpu_dma_latency writable by group audio.",
        details: "Lets audio servers keep CPUs out of deep sleep states while playing, avoiding wake-up latency crackles.",
        source: "usr/lib/udev/rules.d/99-cpu-dma-latency.rules",
        build: |_| {
            Impl {
            blocks: vec![(
                UDEV_DMA,
                "# Audio servers may keep CPUs out of deep idle states while playing.\nDEVPATH==\"/devices/virtual/misc/cpu_dma_latency\", OWNER=\"root\", GROUP=\"audio\", MODE=\"0660\"".into(),
            )],
            on_enable: vec![udev_reload(), udev_trigger(&["misc"])],
            before_disable: vec![],
            after_disable: vec![udev_reload(), udev_trigger(&["misc"])],
        }
        },
        check: dma_check,
        ..BASE
    },
    Tweak {
        id: "hda-power-save",
        category: Category::Audio,
        title: "HD Audio power saving off on AC",
        summary: "No codec power saving while plugged in (fixes pops and crackles).",
        details: "Intel HD Audio codecs power down after 10 seconds of silence and pop when they wake. On AC power that saving \
                  isn't worth it; on battery the previous value is restored.",
        source: "usr/lib/udev/rules.d/20-audio-pm.rules, usr/lib/udev/audio-pm-ctl",
        available: hda_available,
        build: audio_pm_build,
        check: audio_pm_check,
        ..BASE
    },
    Tweak {
        id: "pci-latency",
        category: Category::Audio,
        title: "PCI latency timers",
        summary: "Latency timer 20 for all PCI devices, 80 for sound cards.",
        details: "Stops conventional PCI devices with large latency timers from holding the bus and causing audio gaps. \
                  PCI Express devices ignore the timer, so on modern machines this mostly does nothing.",
        source: "usr/bin/pci-latency, usr/lib/systemd/system/pci-latency.service",
        risk: Risk::Moderate,
        recommended: false,
        notes: "OpenRC: runs from /etc/local.d at boot. systemd: a oneshot service.",
        available: pci_latency_local_available,
        build: pci_latency_build,
        ..BASE
    },
    // ── Gaming ──
    Tweak {
        id: "ntsync",
        category: Category::Gaming,
        title: "Load ntsync at boot",
        summary: "Windows NT synchronisation in the kernel for Wine/Proton.",
        details: "Wine 10+ and Proton use /dev/ntsync to implement Windows mutexes, semaphores and events exactly and quickly — \
                  better frame pacing than esync/fsync in many games.",
        source: "usr/lib/modules-load.d/ntsync.conf",
        notes: "OpenRC's modules service reads /etc/modules-load.d.",
        available: ntsync_available,
        build: ntsync_build,
        check: ntsync_check,
        ..BASE
    },
    Tweak {
        id: "vm.max_map_count",
        category: Category::Gaming,
        title: "Memory map limit",
        summary: "Allow 1048576 memory mappings per process.",
        details: "Gentoo keeps the kernel default of 65530; Arch (and so CachyOS) ships 1048576 because many games — CS2, \
                  Hogwarts Legacy, Star Citizen, DayZ — crash or fail to start below it.",
        source: "Arch's filesystem package (inherited by CachyOS)",
        key: "vm.max_map_count",
        at_least: true,
        default: "1048576",
        param: Param::Int { min: 65_530, max: 2_147_483_647 },
        ..SYSCTL_BASE
    },
    Tweak {
        id: "nofile-limits",
        category: Category::Gaming,
        title: "Per-process open file limit",
        summary: "Hard limit of 1048576 open files for user programs.",
        details: "Wine/Proton's esync and some game engines open huge numbers of file descriptors and raise their own soft limit \
                  up to the hard limit. OpenRC: a limits.d entry for PAM sessions. systemd: DefaultLimitNOFILE for the system \
                  and user managers.",
        source: "usr/lib/systemd/system.conf.d/10-limits.conf, usr/lib/systemd/user.conf.d/10-limits.conf",
        after: After::Relogin,
        notes: "systemd reads system.conf.d at boot: reboot for the system manager part.",
        build: nofile_build,
        check: nofile_check,
        ..BASE
    },
    Tweak {
        id: "game-performance",
        category: Category::Gaming,
        title: "game-performance launcher",
        summary: "Steam launch option that switches to the performance power profile while a game runs.",
        details: "Put `game-performance %command%` in a game's Steam launch options. The CPU runs in the performance profile \
                  until the game exits and the screen won't blank (through systemd-logind or elogind).",
        source: "usr/bin/game-performance",
        available: game_performance_available,
        build: game_performance_build,
        ..BASE
    },
    Tweak {
        id: "zink-run",
        category: Category::Gaming,
        title: "zink-run launcher",
        summary: "Run an OpenGL program on Mesa's Zink (OpenGL over Vulkan).",
        details: "Useful for old OpenGL games with buggy drivers: `zink-run %command%`.",
        source: "usr/bin/zink-run",
        available: zink_available,
        build: zink_build,
        ..BASE
    },
    Tweak {
        id: "dlss-swapper",
        category: Category::Gaming,
        title: "dlss-swapper launcher",
        summary: "Proton: newest DLSS presets and automatic DLSS updates.",
        details: "Sets the DXVK-NVAPI overrides that make games use the latest DLSS super resolution, ray reconstruction and \
                  frame generation presets, and enables Proton's NGX updater: `dlss-swapper %command%`.",
        source: "usr/bin/dlss-swapper",
        available: dlss_available,
        build: dlss_build,
        ..BASE
    },
    // ── Network ──
    Tweak {
        id: "net.core.netdev_max_backlog",
        category: Category::Network,
        title: "Network receive backlog",
        summary: "Queue up to 4096 incoming packets per CPU.",
        details: "A bigger queue between the network card and the protocol stack drops fewer packets during bursts (fast \
                  downloads, Steam updates).",
        key: "net.core.netdev_max_backlog",
        at_least: true,
        default: "4096",
        param: Param::Int { min: 1000, max: 65536 },
        ..SYSCTL_BASE
    },
    Tweak {
        id: "wifi-regdomain",
        category: Category::Network,
        title: "Wi-Fi regulatory domain from time zone",
        summary: "Use the channels and transmit power allowed in your country.",
        details: "Without a regulatory domain the card falls back to the most restrictive world settings, which can hide 5/6 GHz \
                  channels and lower range. The country is derived from your time zone; COUNTRY=XX in /etc/iw-regdomain overrides it.",
        source: "usr/lib/iw-set-regdomain, usr/lib/udev/rules.d/85-iw-regulatory.rules",
        available: regdomain_available,
        build: regdomain_build,
        ..BASE
    },
    Tweak {
        id: "nm-resolved",
        category: Category::Network,
        title: "NetworkManager DNS via systemd-resolved",
        summary: "dns=systemd-resolved",
        details: "Lets systemd-resolved handle DNS caching, split DNS for VPNs and DNS-over-TLS for NetworkManager connections.",
        source: "usr/lib/NetworkManager/conf.d/dns.conf",
        only: Some(Init::Systemd),
        available: nm_dns_available,
        build: nm_dns_build,
        ..BASE
    },
    // ── Services ──
    Tweak {
        id: "systemd-timeouts",
        title: "Shorter service timeouts",
        summary: "15 s to start, 10 s to stop instead of 90 s.",
        details: "A hanging service delays shutdown by at most 10 seconds instead of a minute and a half.",
        source: "usr/lib/systemd/system.conf.d/00-timeout.conf, usr/lib/systemd/user.conf.d/00-timeout.conf",
        notes: "OpenRC has no global service timeout; each init script sets its own.",
        build: timeouts_build,
        ..SYSTEMD_BASE
    },
    Tweak {
        id: "journald-size",
        title: "Journal size limit",
        summary: "Keep at most 50 MiB of persistent journal.",
        details: "The journal otherwise grows up to 10 % of the file system (capped at 4 GiB).",
        source: "usr/lib/systemd/journald.conf.d/00-journal-size.conf",
        default: "50",
        unit: "MiB",
        param: Param::Int { min: 16, max: 4096 },
        after: After::Now,
        notes: "OpenRC systems log through a syslog daemon (metalog, syslog-ng, sysklogd) with its own rotation.",
        build: journald_build,
        check: journald_check,
        ..SYSTEMD_BASE
    },
    Tweak {
        id: "systemd-oomd",
        title: "systemd-oomd per-slice defaults",
        summary: "Kill the worst offender when a slice is under 80 % memory pressure or swap is full.",
        details: "Rather than the whole desktop freezing when memory runs out, systemd-oomd ends the cgroup causing the pressure. \
                  The session slice (your desktop shell) is exempt.",
        source: "usr/lib/systemd/system/system.slice.d/10-oomd-per-slice-defaults.conf, usr/lib/systemd/user/slice.d/…",
        after: After::Now,
        notes: "Takes effect while systemd-oomd.service runs. On OpenRC, sys-apps/earlyoom or sys-apps/nohang play the same role.",
        available: oomd_available,
        build: oomd_build,
        ..SYSTEMD_BASE
    },
    Tweak {
        id: "systemd-delegate",
        title: "cgroup delegation to user sessions",
        summary: "Delegate cpu, cpuset, io, memory and pids controllers to user@.service.",
        details: "Lets rootless containers and per-app resource limits (systemd-run --user -p MemoryMax=…) control those resources.",
        source: "usr/lib/systemd/system/user@.service.d/delegate.conf",
        after: After::Relogin,
        build: delegate_build,
        ..SYSTEMD_BASE
    },
    Tweak {
        id: "timesyncd-ntp",
        title: "Time sync via Cloudflare NTP",
        summary: "systemd-timesyncd uses time.cloudflare.com, Gentoo pool as fallback.",
        details: "A fast, anycast NTP server with NTS support; the Gentoo NTP pool remains as fallback.",
        source: "usr/lib/systemd/timesyncd.conf.d/10-timesyncd.conf",
        after: After::Now,
        notes: "OpenRC systems usually run chrony or ntpd; configure servers in /etc/chrony/chrony.conf.",
        available: timesyncd_available,
        build: timesyncd_build,
        ..SYSTEMD_BASE
    },
    Tweak {
        id: "coredump-retention",
        title: "Core dump retention: 3 days",
        summary: "Delete core dumps after 3 days instead of 2 weeks.",
        details: "Crash dumps of big programs (browsers, games) can take gigabytes in /var/lib/systemd/coredump.",
        source: "usr/lib/tmpfiles.d/coredump.conf",
        after: After::Now,
        available: coredump_available,
        build: coredump_build,
        ..SYSTEMD_BASE
    },
    Tweak {
        id: "rtkit-loglevel",
        title: "Quieter rtkit-daemon",
        summary: "Only log rtkit messages at info level and above.",
        details: "rtkit-daemon logs every realtime priority grant at debug level, flooding the journal while audio plays.",
        source: "usr/lib/systemd/system/rtkit-daemon.service.d/override.conf",
        available: rtkit_available,
        build: rtkit_build,
        ..SYSTEMD_BASE
    },
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_unique_and_defaults_valid() {
        let sys = System::detect_at(std::path::Path::new("/nonexistent"));
        let mut seen = std::collections::HashSet::new();
        for t in CATALOG {
            assert!(seen.insert(t.id), "duplicate id {}", t.id);
            assert!(t.validate(&sys, None).is_ok(), "default of {} invalid", t.id);
        }
    }

    #[test]
    fn values_are_validated() {
        let sys = System::detect_at(std::path::Path::new("/nonexistent"));
        let t = get("vm.swappiness").unwrap();
        assert_eq!(t.validate(&sys, Some("60")).unwrap(), "60");
        assert!(t.validate(&sys, Some("201")).is_err());
        assert!(t.validate(&sys, Some("1; rm -rf /")).is_err());
        let z = get("thp-defrag").unwrap();
        assert!(z.validate(&sys, Some("madvise")).is_ok());
        assert!(z.validate(&sys, Some("sometimes")).is_err());
        assert!(get("kernel.nmi_watchdog").unwrap().validate(&sys, Some("1")).is_err());
    }
}
