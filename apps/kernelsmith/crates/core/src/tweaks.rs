//! The catalog of optimizations a user can choose from.
//!
//! Each tweak is a set of mutually exclusive choices. A choice knows:
//! * how to explain itself to someone who has never configured a kernel,
//! * whether it is available for the selected source tree / toolchain,
//! * which Kconfig changes it makes.
//!
//! Symbol names follow upstream Linux 7.x (plus the CachyOS / Gentoo extras
//! where noted). Ops for symbols a tree doesn't define are dropped when the
//! script is generated, and every requested value is verified after
//! `make olddefconfig`, so the catalog can be generous.

use crate::hw::{BranchSampling, CpuVendor, SystemInfo};
use crate::kconfig::SymbolIndex;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};
use std::sync::OnceLock;

/// A single change to the kernel configuration.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum Op {
    Y(String),
    M(String),
    N(String),
    /// Leave built-in if it already is, otherwise build as a module.
    AtLeastM(String),
    Str(String, String),
    Val(String, String),
}

impl Op {
    pub fn sym(&self) -> &str {
        match self {
            Op::Y(s) | Op::M(s) | Op::N(s) | Op::AtLeastM(s) | Op::Str(s, _) | Op::Val(s, _) => s,
        }
    }

    /// Human readable form, e.g. `CONFIG_NTSYNC=y`.
    pub fn describe(&self) -> String {
        match self {
            Op::Y(s) => format!("CONFIG_{s}=y"),
            Op::M(s) => format!("CONFIG_{s}=m"),
            Op::N(s) => format!("CONFIG_{s} off"),
            Op::AtLeastM(s) => format!("CONFIG_{s}=m (or y)"),
            Op::Str(s, v) => format!("CONFIG_{s}=\"{v}\""),
            Op::Val(s, v) => format!("CONFIG_{s}={v}"),
        }
    }
}

pub fn y(s: &str) -> Op {
    Op::Y(s.into())
}
pub fn m(s: &str) -> Op {
    Op::M(s.into())
}
pub fn n(s: &str) -> Op {
    Op::N(s.into())
}
pub fn atm(s: &str) -> Op {
    Op::AtLeastM(s.into())
}
pub fn sv(s: &str, v: &str) -> Op {
    Op::Str(s.into(), v.into())
}
pub fn val(s: &str, v: &str) -> Op {
    Op::Val(s.into(), v.into())
}

/// Enable one member of a Kconfig `choice` and switch the others off.
fn pick(sel: &str, group: &[&str]) -> Vec<Op> {
    let mut v = vec![y(sel)];
    v.extend(group.iter().filter(|g| **g != sel).map(|g| n(g)));
    v
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum Category {
    Compiler,
    Cpu,
    Scheduler,
    Latency,
    Memory,
    Gaming,
    Network,
    Storage,
    Size,
    Boot,
    Security,
}

impl Category {
    pub fn label(self) -> &'static str {
        match self {
            Category::Compiler => "Compiler & build",
            Category::Cpu => "Processor",
            Category::Scheduler => "CPU scheduler",
            Category::Latency => "Responsiveness",
            Category::Memory => "Memory",
            Category::Gaming => "Gaming",
            Category::Network => "Network",
            Category::Storage => "Storage",
            Category::Size => "Size & build speed",
            Category::Boot => "Boot",
            Category::Security => "Security",
        }
    }

    pub fn icon(self) -> &'static str {
        match self {
            Category::Compiler => "🔧",
            Category::Cpu => "⚡",
            Category::Scheduler => "🔀",
            Category::Latency => "⏱",
            Category::Memory => "🗄",
            Category::Gaming => "🎮",
            Category::Network => "🌐",
            Category::Storage => "💾",
            Category::Size => "📦",
            Category::Boot => "🚀",
            Category::Security => "🛡",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Risk {
    Safe,
    Moderate,
    Advanced,
}

impl Risk {
    pub fn label(self) -> &'static str {
        match self {
            Risk::Safe => "Safe",
            Risk::Moderate => "Some trade-offs",
            Risk::Advanced => "Experts only",
        }
    }
}

pub type Selection = BTreeMap<String, String>;

/// Everything a choice needs to decide whether it applies.
pub struct Ctx<'a> {
    pub sys: &'a SystemInfo,
    /// `None` while the source tree hasn't been scanned yet.
    pub symbols: Option<&'a SymbolIndex>,
    /// Symbols that selected patches will add.
    pub provided: &'a HashSet<String>,
    pub selection: &'a Selection,
}

impl Ctx<'_> {
    pub fn has(&self, sym: &str) -> bool {
        self.provided.contains(sym) || self.symbols.is_none_or(|s| s.has(sym))
    }

    pub fn sel(&self, id: &str) -> &str {
        self.selection.get(id).map(String::as_str).unwrap_or("")
    }

    pub fn clang(&self) -> bool {
        self.sel("compiler") == "clang"
    }

    fn need(&self, sym: &str, why: &str) -> Result<(), String> {
        if self.has(sym) {
            Ok(())
        } else {
            Err(why.to_string())
        }
    }
}

pub struct Choice {
    pub id: &'static str,
    pub label: &'static str,
    pub desc: &'static str,
    pub risk: Risk,
    pub ops: fn(&Ctx) -> Vec<Op>,
    pub avail: fn(&Ctx) -> Result<(), String>,
}

pub struct Tweak {
    pub id: &'static str,
    pub category: Category,
    pub title: &'static str,
    /// One line shown on the card.
    pub summary: &'static str,
    /// The "What does this mean?" text.
    pub explain: &'static str,
    /// Hidden unless "Show expert options" is on.
    pub advanced: bool,
    pub choices: Vec<Choice>,
}

impl Tweak {
    pub fn choice(&self, id: &str) -> Option<&Choice> {
        self.choices.iter().find(|c| c.id == id)
    }
}

fn ok(_: &Ctx) -> Result<(), String> {
    Ok(())
}
fn none(_: &Ctx) -> Vec<Op> {
    vec![]
}

const LTO: [&str; 4] = ["LTO_NONE", "LTO_CLANG_THIN", "LTO_CLANG_FULL", "LTO_CLANG_THIN_DIST"];
const OPT: [&str; 3] = ["CC_OPTIMIZE_FOR_PERFORMANCE", "CC_OPTIMIZE_FOR_PERFORMANCE_O3", "CC_OPTIMIZE_FOR_SIZE"];
const MARCH: [&str; 5] = ["X86_NATIVE_CPU", "GENERIC_CPU", "MZEN4", "MNATIVE_INTEL", "MNATIVE_AMD"];
const HZ: [&str; 7] = ["HZ_100", "HZ_250", "HZ_300", "HZ_500", "HZ_600", "HZ_750", "HZ_1000"];
const PREEMPT: [&str; 5] = ["PREEMPT_NONE", "PREEMPT_VOLUNTARY", "PREEMPT", "PREEMPT_LAZY", "PREEMPT_RT"];
const TICK: [&str; 3] = ["HZ_PERIODIC", "NO_HZ_IDLE", "NO_HZ_FULL"];
const THP: [&str; 3] = ["TRANSPARENT_HUGEPAGE_ALWAYS", "TRANSPARENT_HUGEPAGE_MADVISE", "TRANSPARENT_HUGEPAGE_NEVER"];
const TCP_DEFAULT: [&str; 9] = [
    "DEFAULT_BIC", "DEFAULT_CUBIC", "DEFAULT_HTCP", "DEFAULT_HYBLA", "DEFAULT_VEGAS",
    "DEFAULT_VENO", "DEFAULT_WESTWOOD", "DEFAULT_DCTCP", "DEFAULT_RENO",
];
const QDISC: [&str; 5] = ["DEFAULT_FQ", "DEFAULT_FQ_CODEL", "DEFAULT_FQ_PIE", "DEFAULT_SFQ", "DEFAULT_PFIFO_FAST"];
const ZSWAP_COMP: [&str; 6] = [
    "ZSWAP_COMPRESSOR_DEFAULT_DEFLATE", "ZSWAP_COMPRESSOR_DEFAULT_LZO", "ZSWAP_COMPRESSOR_DEFAULT_842",
    "ZSWAP_COMPRESSOR_DEFAULT_LZ4", "ZSWAP_COMPRESSOR_DEFAULT_LZ4HC", "ZSWAP_COMPRESSOR_DEFAULT_ZSTD",
];
const DWARF: [&str; 4] = [
    "DEBUG_INFO_NONE", "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT", "DEBUG_INFO_DWARF4", "DEBUG_INFO_DWARF5",
];
const KCOMP: [&str; 7] = [
    "KERNEL_GZIP", "KERNEL_BZIP2", "KERNEL_LZMA", "KERNEL_XZ", "KERNEL_LZO", "KERNEL_LZ4", "KERNEL_ZSTD",
];
const MODCOMP: [&str; 4] = ["MODULE_COMPRESS_NONE", "MODULE_COMPRESS_GZIP", "MODULE_COMPRESS_XZ", "MODULE_COMPRESS_ZSTD"];
const GOV: [&str; 6] = [
    "CPU_FREQ_DEFAULT_GOV_PERFORMANCE", "CPU_FREQ_DEFAULT_GOV_POWERSAVE", "CPU_FREQ_DEFAULT_GOV_USERSPACE",
    "CPU_FREQ_DEFAULT_GOV_ONDEMAND", "CPU_FREQ_DEFAULT_GOV_CONSERVATIVE", "CPU_FREQ_DEFAULT_GOV_SCHEDUTIL",
];

/// Kernel-side debugging aids that cost speed and memory on a normal system.
pub const DEBUG_CHECKS: [&str; 22] = [
    "KASAN", "KCSAN", "KMSAN", "UBSAN", "LOCKDEP", "PROVE_LOCKING", "LOCK_STAT", "DEBUG_ATOMIC_SLEEP",
    "DEBUG_PREEMPT", "DEBUG_MUTEXES", "DEBUG_RT_MUTEXES", "DEBUG_SPINLOCK", "DEBUG_LIST", "DEBUG_SG",
    "DEBUG_NOTIFIERS", "DEBUG_KMEMLEAK", "DEBUG_PAGEALLOC", "PAGE_OWNER", "SLUB_DEBUG_ON", "DEBUG_VM",
    "DEBUG_OBJECTS", "LATENCYTOP",
];

// Each choice needs its own non-capturing closures, hence a macro.
macro_rules! hz_choice {
    ($hz:literal, $label:literal, $desc:literal) => {
        Choice {
            id: $hz,
            label: $label,
            desc: $desc,
            risk: Risk::Safe,
            ops: |_| {
                let mut v = pick(concat!("HZ_", $hz), &HZ);
                v.push(val("HZ", $hz));
                v
            },
            avail: |c| c.need(concat!("HZ_", $hz), "This tick rate needs the CachyOS patchset (use cachyos-sources)"),
        }
    };
}

pub fn catalog() -> &'static [Tweak] {
    static CAT: OnceLock<Vec<Tweak>> = OnceLock::new();
    CAT.get_or_init(build_catalog)
}

pub fn find(id: &str) -> Option<&'static Tweak> {
    catalog().iter().find(|t| t.id == id)
}

#[allow(clippy::too_many_lines)]
fn build_catalog() -> Vec<Tweak> {
    vec![
        // ───────────────────────────── Compiler ─────────────────────────────
        Tweak {
            id: "compiler",
            category: Category::Compiler,
            title: "Compiler",
            summary: "Which compiler turns the kernel source into the program your PC runs.",
            explain: "Both compilers produce fast, stable kernels. Clang (LLVM) unlocks extra \
                      whole-program optimizations — ThinLTO and profile-guided AutoFDO — and is what \
                      CachyOS, Android and ChromeOS ship. GCC is the most widely tested choice.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "clang",
                    label: "Clang / LLVM",
                    desc: "Required for LTO and AutoFDO. Recommended.",
                    risk: Risk::Safe,
                    ops: none,
                    avail: |c| {
                        if c.sys.toolchain.clang_ready() {
                            Ok(())
                        } else {
                            Err("Install llvm-core/clang and llvm-core/lld first".into())
                        }
                    },
                },
                Choice {
                    id: "gcc",
                    label: "GCC",
                    desc: "The traditional Linux compiler. Very safe.",
                    risk: Risk::Safe,
                    ops: none,
                    avail: |c| c.sys.toolchain.gcc.as_ref().map(|_| ()).ok_or_else(|| "GCC was not found".into()),
                },
            ],
        },
        Tweak {
            id: "lto",
            category: Category::Compiler,
            title: "Link-Time Optimization (LTO)",
            summary: "Optimize the kernel as one whole program instead of file by file.",
            explain: "Normally each source file is optimized on its own. With LTO the compiler sees \
                      the whole kernel at once, so it can inline across files and remove dead code. \
                      ThinLTO keeps builds parallel and is what CachyOS uses; Full LTO squeezes out a \
                      little more but links on a single core and needs lots of RAM. Requires Clang.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "thin",
                    label: "ThinLTO",
                    desc: "Whole-kernel optimization that still builds in parallel.",
                    risk: Risk::Safe,
                    ops: |_| pick("LTO_CLANG_THIN", &LTO),
                    avail: |c| {
                        if !c.clang() {
                            return Err("Needs the Clang compiler".into());
                        }
                        c.need("LTO_CLANG_THIN", "This source tree doesn't support Clang LTO")
                    },
                },
                Choice {
                    id: "full",
                    label: "Full LTO",
                    desc: "Maximum optimization; much slower single-threaded link.",
                    risk: Risk::Moderate,
                    ops: |_| pick("LTO_CLANG_FULL", &LTO),
                    avail: |c| {
                        if !c.clang() {
                            return Err("Needs the Clang compiler".into());
                        }
                        c.need("LTO_CLANG_FULL", "This source tree doesn't support Clang LTO")
                    },
                },
                Choice {
                    id: "none",
                    label: "Off",
                    desc: "Classic per-file optimization.",
                    risk: Risk::Safe,
                    ops: |c| if c.has("LTO_NONE") { pick("LTO_NONE", &LTO) } else { vec![] },
                    avail: ok,
                },
            ],
        },
        Tweak {
            id: "optimize",
            category: Category::Compiler,
            title: "Optimization level",
            summary: "How hard the compiler works to make the code fast.",
            explain: "-O2 is the upstream default. -O3 enables more aggressive loop and inlining \
                      optimizations; CachyOS ships it by default and it is well tested there, but the \
                      option only exists in CachyOS-patched sources. -Os makes the smallest kernel.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "o3",
                    label: "-O3",
                    desc: "More aggressive optimizations (CachyOS default).",
                    risk: Risk::Safe,
                    ops: |_| pick("CC_OPTIMIZE_FOR_PERFORMANCE_O3", &OPT),
                    avail: |c| c.need("CC_OPTIMIZE_FOR_PERFORMANCE_O3", "Only available in CachyOS-patched sources"),
                },
                Choice {
                    id: "o2",
                    label: "-O2",
                    desc: "Upstream default, extremely well tested.",
                    risk: Risk::Safe,
                    ops: |_| pick("CC_OPTIMIZE_FOR_PERFORMANCE", &OPT),
                    avail: ok,
                },
                Choice {
                    id: "os",
                    label: "-Os",
                    desc: "Smallest code size; slightly slower.",
                    risk: Risk::Safe,
                    ops: |_| pick("CC_OPTIMIZE_FOR_SIZE", &OPT),
                    avail: ok,
                },
            ],
        },
        Tweak {
            id: "autofdo",
            category: Category::Compiler,
            title: "Profile-guided optimization (AutoFDO)",
            summary: "Let the compiler learn from how *you* use your PC, then rebuild.",
            explain: "Much of Clear Linux's famous speed came from profile-guided compilation. The \
                      upstream kernel's version of this is AutoFDO (plus Propeller for code layout). \
                      It is a two-step process: build this kernel 'profile-ready', boot it and run \
                      your usual workload (a game, a compile…) while Kernelsmith records with `perf`, \
                      then rebuild with the recorded profile. CachyOS reports a few percent gains. \
                      Needs Clang, LTO, and a CPU that can record branch samples.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "off",
                    label: "Off",
                    desc: "Regular build.",
                    risk: Risk::Safe,
                    ops: |_| vec![n("AUTOFDO_CLANG"), n("PROPELLER_CLANG")],
                    avail: ok,
                },
                Choice {
                    id: "autofdo",
                    label: "Profile-ready (AutoFDO)",
                    desc: "Build so it can be profiled and re-optimized later.",
                    risk: Risk::Safe,
                    ops: |_| vec![y("AUTOFDO_CLANG"), n("PROPELLER_CLANG")],
                    avail: autofdo_avail,
                },
                Choice {
                    id: "propeller",
                    label: "AutoFDO + Propeller",
                    desc: "Also optimizes code layout. Needs Google's propeller tools to profile.",
                    risk: Risk::Moderate,
                    ops: |_| vec![y("AUTOFDO_CLANG"), y("PROPELLER_CLANG")],
                    avail: |c| {
                        autofdo_avail(c)?;
                        if c.sel("lto") == "full" {
                            return Err("Propeller works with ThinLTO, not Full LTO".into());
                        }
                        c.need("PROPELLER_CLANG", "Needs Linux 6.13+ and Clang 19+")
                    },
                },
            ],
        },
        // ───────────────────────────── Processor ─────────────────────────────
        Tweak {
            id: "march",
            category: Category::Cpu,
            title: "CPU instruction set",
            summary: "Let the compiler use every instruction your processor supports.",
            explain: "Generic kernels only use instructions every x86-64 CPU from 2003 understands. \
                      'Native' tunes the kernel for exactly this processor (e.g. AVX2, BMI2), which is \
                      ideal for a personal machine. The catch: the kernel may not boot if you move \
                      the disk to a computer with an older CPU. x86-64-v3 is a portable middle ground \
                      for any CPU from roughly 2015 onward.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "native",
                    label: "Native (this CPU)",
                    desc: "Fastest. Tuned for this exact processor.",
                    risk: Risk::Safe,
                    ops: |c| {
                        if c.has("X86_NATIVE_CPU") {
                            pick("X86_NATIVE_CPU", &MARCH)
                        } else if c.sys.cpu.vendor == CpuVendor::Amd {
                            pick("MNATIVE_AMD", &MARCH)
                        } else {
                            pick("MNATIVE_INTEL", &MARCH)
                        }
                    },
                    avail: |c| {
                        if c.has("X86_NATIVE_CPU") || c.has("MNATIVE_INTEL") || c.has("MNATIVE_AMD") {
                            Ok(())
                        } else {
                            Err("Needs Linux 6.16+ (or gentoo-sources with USE=experimental)".into())
                        }
                    },
                },
                Choice {
                    id: "v3",
                    label: "x86-64-v3",
                    desc: "Portable to any AVX2-capable CPU (≈2015+).",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = pick("GENERIC_CPU", &MARCH);
                        v.push(val("X86_64_VERSION", "3"));
                        v
                    },
                    avail: |c| {
                        if c.sys.cpu.x86_level < 3 {
                            return Err("This CPU doesn't support x86-64-v3".into());
                        }
                        c.need("X86_64_VERSION", "This source tree has no ISA-level option")
                    },
                },
                Choice {
                    id: "generic",
                    label: "Generic x86-64",
                    desc: "Runs on any 64-bit PC. Slowest.",
                    risk: Risk::Safe,
                    ops: |c| {
                        let mut v = pick("GENERIC_CPU", &MARCH);
                        if c.has("X86_64_VERSION") {
                            v.push(val("X86_64_VERSION", "1"));
                        }
                        v
                    },
                    avail: ok,
                },
            ],
        },
        Tweak {
            id: "governor",
            category: Category::Cpu,
            title: "Default CPU speed policy",
            summary: "How eagerly the CPU ramps up its clock speed.",
            explain: "'Schedutil' raises the clock as load grows — efficient and responsive. \
                      'Performance' keeps the CPU at its highest speed policy, giving the most \
                      consistent frame times at the cost of power and heat. You can always switch at \
                      runtime with tools like power-profiles-daemon or cpupower.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "schedutil",
                    label: "Schedutil",
                    desc: "Balanced and efficient.",
                    risk: Risk::Safe,
                    ops: |_| pick("CPU_FREQ_DEFAULT_GOV_SCHEDUTIL", &GOV),
                    avail: ok,
                },
                Choice {
                    id: "performance",
                    label: "Performance",
                    desc: "Maximum speed, more power use.",
                    risk: Risk::Safe,
                    ops: |_| pick("CPU_FREQ_DEFAULT_GOV_PERFORMANCE", &GOV),
                    avail: ok,
                },
            ],
        },
        // ───────────────────────────── Scheduler ─────────────────────────────
        Tweak {
            id: "scheduler",
            category: Category::Scheduler,
            title: "CPU scheduler",
            summary: "Decides which program gets the CPU next — key for smoothness.",
            explain: "The scheduler hands out CPU time. EEVDF is the upstream default and is fair to \
                      everything. BORE (Burst-Oriented Response Enhancer) notices which tasks are \
                      interactive — your game, your desktop — and lets them jump ahead of long \
                      background jobs like compiling, which noticeably reduces stutter under load. \
                      It's the CachyOS default. BMQ and PDS (Project C) replace the scheduler \
                      entirely and are for experimenters.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "bore",
                    label: "BORE",
                    desc: "Prioritizes interactive apps and games. CachyOS default.",
                    risk: Risk::Safe,
                    ops: |_| vec![y("SCHED_BORE"), n("SCHED_ALT")],
                    avail: |c| c.need("SCHED_BORE", "Add the BORE patch in the Patches step, or use cachyos-sources"),
                },
                Choice {
                    id: "eevdf",
                    label: "EEVDF",
                    desc: "Upstream default. Fair and well tested.",
                    risk: Risk::Safe,
                    ops: |_| vec![n("SCHED_BORE"), n("SCHED_ALT")],
                    avail: ok,
                },
                Choice {
                    id: "bmq",
                    label: "BMQ",
                    desc: "Project C BitMap Queue scheduler. Experimental.",
                    risk: Risk::Advanced,
                    ops: |_| vec![y("SCHED_ALT"), y("SCHED_BMQ"), n("SCHED_PDS"), n("SCHED_BORE")],
                    avail: |c| c.need("SCHED_ALT", "Add the Project C patch in the Patches step"),
                },
                Choice {
                    id: "pds",
                    label: "PDS",
                    desc: "Project C Priority and Deadline scheduler. Experimental.",
                    risk: Risk::Advanced,
                    ops: |_| vec![y("SCHED_ALT"), y("SCHED_PDS"), n("SCHED_BMQ"), n("SCHED_BORE")],
                    avail: |c| c.need("SCHED_ALT", "Add the Project C patch in the Patches step"),
                },
            ],
        },
        Tweak {
            id: "sched_ext",
            category: Category::Scheduler,
            title: "sched_ext (swappable BPF schedulers)",
            summary: "Switch schedulers at runtime with tools like scx_lavd or scx_bpfland.",
            explain: "sched_ext lets programs from the sys-kernel/scx package replace the scheduler \
                      while the system runs — handy to try gaming-focused schedulers such as \
                      scx_lavd without rebuilding. It needs BTF type information, which requires \
                      the 'pahole' tool (dev-util/pahole) and keeps debug info enabled during the build.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "on",
                    label: "On",
                    desc: "Keep runtime-switchable schedulers available.",
                    risk: Risk::Safe,
                    ops: |_| vec![y("BPF_SYSCALL"), y("BPF_JIT"), y("DEBUG_INFO_BTF"), y("SCHED_CLASS_EXT")],
                    avail: |c| {
                        if matches!(c.sel("scheduler"), "bmq" | "pds") {
                            return Err("Not compatible with Project C schedulers".into());
                        }
                        if !c.sys.toolchain.pahole {
                            return Err("Install dev-util/pahole to enable this".into());
                        }
                        c.need("SCHED_CLASS_EXT", "Needs Linux 6.12 or newer")
                    },
                },
                Choice {
                    id: "off",
                    label: "Off",
                    desc: "Smaller build, no BTF requirement.",
                    risk: Risk::Safe,
                    ops: |_| vec![n("SCHED_CLASS_EXT")],
                    avail: ok,
                },
            ],
        },
        // ─────────────────────────── Responsiveness ───────────────────────────
        Tweak {
            id: "hz",
            category: Category::Latency,
            title: "Timer frequency",
            summary: "How many times per second the kernel can switch tasks.",
            explain: "Higher values make the system react faster — smoother games, lower audio \
                      latency, snappier desktop — at a tiny cost in throughput and power. Lower values \
                      suit servers and long batch jobs. 1000 Hz is the usual desktop/gaming choice. \
                      500, 600 and 750 Hz are only offered by CachyOS-patched sources.",
            advanced: false,
            choices: vec![
                hz_choice!("1000", "1000 Hz", "Most responsive. Gaming, audio, desktop."),
                hz_choice!("750", "750 Hz", "Responsive with a bit less overhead."),
                hz_choice!("600", "600 Hz", "Balanced (CachyOS option)."),
                hz_choice!("500", "500 Hz", "Balanced desktop/workstation."),
                hz_choice!("300", "300 Hz", "Good for laptops and multimedia playback."),
                hz_choice!("250", "250 Hz", "Upstream default. Throughput leaning."),
                hz_choice!("100", "100 Hz", "Servers and batch processing."),
            ],
        },
        Tweak {
            id: "preempt",
            category: Category::Latency,
            title: "Preemption model",
            summary: "Whether the kernel may pause its own work to run something urgent.",
            explain: "'Full' lets urgent tasks (input, audio, your game's render thread) interrupt \
                      almost any kernel work — the lowest-latency desktop setting. 'Lazy' lets the \
                      scheduler delay interruptions a little to gain throughput. 'Real-time' \
                      guarantees deterministic latency for pro audio and robotics, but the \
                      proprietary NVIDIA driver does not officially support it.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "full",
                    label: "Full",
                    desc: "Lowest latency. Desktop and gaming.",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = pick("PREEMPT", &PREEMPT);
                        v.push(y("PREEMPT_DYNAMIC"));
                        v
                    },
                    avail: ok,
                },
                Choice {
                    id: "lazy",
                    label: "Lazy",
                    desc: "Scheduler-controlled. Better throughput.",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = pick("PREEMPT_LAZY", &PREEMPT);
                        v.push(y("PREEMPT_DYNAMIC"));
                        v
                    },
                    avail: |c| c.need("PREEMPT_LAZY", "Needs Linux 6.13 or newer"),
                },
                Choice {
                    id: "voluntary",
                    label: "Voluntary",
                    desc: "Classic server-ish model (older kernels only).",
                    risk: Risk::Safe,
                    ops: |_| pick("PREEMPT_VOLUNTARY", &PREEMPT),
                    avail: |c| c.need("PREEMPT_VOLUNTARY", "Replaced by 'Lazy' on x86 in this kernel"),
                },
                Choice {
                    id: "rt",
                    label: "Real-time",
                    desc: "Deterministic latency for pro audio. Not for NVIDIA.",
                    risk: Risk::Advanced,
                    ops: |_| pick("PREEMPT_RT", &PREEMPT),
                    avail: |c| {
                        if c.sys.needs_nvidia_support() {
                            return Err("The proprietary NVIDIA driver doesn't support PREEMPT_RT".into());
                        }
                        c.need("PREEMPT_RT", "Not available in this source tree")
                    },
                },
            ],
        },
        Tweak {
            id: "tick",
            category: Category::Latency,
            title: "Idle timer behaviour",
            summary: "Whether idle CPU cores keep waking up for the timer.",
            explain: "'Idle tickless' stops the timer on idle cores to save power — the right choice \
                      for nearly everyone. 'Full tickless' also stops it on busy cores, which helps \
                      very specialised low-jitter setups (CachyOS default) but adds overhead to every \
                      system call. 'Periodic' always ticks.",
            advanced: true,
            choices: vec![
                Choice {
                    id: "idle",
                    label: "Idle tickless",
                    desc: "Recommended for nearly all systems.",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = pick("NO_HZ_IDLE", &TICK);
                        v.push(y("NO_HZ"));
                        v
                    },
                    avail: ok,
                },
                Choice {
                    id: "full",
                    label: "Full tickless",
                    desc: "Lower jitter on busy cores; more syscall overhead.",
                    risk: Risk::Moderate,
                    ops: |_| {
                        let mut v = pick("NO_HZ_FULL", &TICK);
                        v.extend([y("NO_HZ"), y("CONTEXT_TRACKING"), y("NO_HZ_FULL_NODEF"), n("CONTEXT_TRACKING_FORCE")]);
                        v
                    },
                    avail: ok,
                },
                Choice {
                    id: "periodic",
                    label: "Periodic",
                    desc: "Always tick. Legacy behaviour.",
                    risk: Risk::Safe,
                    ops: |_| pick("HZ_PERIODIC", &TICK),
                    avail: ok,
                },
            ],
        },
        // ───────────────────────────── Memory ─────────────────────────────
        Tweak {
            id: "thp",
            category: Category::Memory,
            title: "Transparent Huge Pages",
            summary: "Use 2 MB memory pages to cut memory-lookup overhead.",
            explain: "Huge pages mean fewer address translations, which speeds up memory-hungry \
                      programs like games, browsers and compilers. 'Always' applies them everywhere \
                      (CachyOS default, best raw speed, slightly higher memory use). 'Only when asked' \
                      (madvise) uses them only for programs that request it — the conservative choice.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "always",
                    label: "Always",
                    desc: "Best performance, a bit more RAM use.",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = vec![y("TRANSPARENT_HUGEPAGE")];
                        v.extend(pick("TRANSPARENT_HUGEPAGE_ALWAYS", &THP));
                        v
                    },
                    avail: ok,
                },
                Choice {
                    id: "madvise",
                    label: "Only when asked",
                    desc: "Conservative. Apps opt in.",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = vec![y("TRANSPARENT_HUGEPAGE")];
                        v.extend(pick("TRANSPARENT_HUGEPAGE_MADVISE", &THP));
                        v
                    },
                    avail: ok,
                },
            ],
        },
        Tweak {
            id: "mglru",
            category: Category::Memory,
            title: "Multi-Gen LRU",
            summary: "Smarter decisions about what to evict when RAM runs low.",
            explain: "MGLRU tracks how recently memory was used in several generations, so the \
                      kernel keeps the right things in RAM under pressure. It reduces stutter when \
                      memory is nearly full and is enabled by default in ChromeOS and CachyOS.",
            advanced: false,
            choices: vec![
                Choice { id: "on", label: "On", desc: "Recommended.", risk: Risk::Safe,
                    ops: |_| vec![y("LRU_GEN"), y("LRU_GEN_ENABLED")], avail: ok },
                Choice { id: "off", label: "Off", desc: "Classic LRU.", risk: Risk::Safe,
                    ops: |_| vec![n("LRU_GEN_ENABLED")], avail: ok },
            ],
        },
        Tweak {
            id: "zswap",
            category: Category::Memory,
            title: "Compressed memory (zswap & zram)",
            summary: "Compress memory instead of swapping it to disk.",
            explain: "When RAM fills up, zswap compresses pages with zstd and keeps them in memory \
                      instead of writing them to your SSD — far faster than real swap. zram support \
                      is also built so you can use a compressed RAM disk as swap.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "on",
                    label: "On (zstd)",
                    desc: "Recommended.",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = vec![y("ZSWAP"), y("ZSWAP_DEFAULT_ON"), y("ZSWAP_SHRINKER_DEFAULT_ON"), y("CRYPTO_ZSTD")];
                        v.extend(pick("ZSWAP_COMPRESSOR_DEFAULT_ZSTD", &ZSWAP_COMP));
                        v.extend([atm("ZRAM"), y("ZRAM_BACKEND_ZSTD"), y("ZRAM_BACKEND_LZ4")]);
                        v
                    },
                    avail: ok,
                },
                Choice { id: "off", label: "Off", desc: "Leave as in the base config.", risk: Risk::Safe,
                    ops: none, avail: ok },
            ],
        },
        // ───────────────────────────── Gaming ─────────────────────────────
        Tweak {
            id: "ntsync",
            category: Category::Gaming,
            title: "NTSYNC for Wine & Proton",
            summary: "Native Windows synchronization in the kernel — smoother Windows games.",
            explain: "Windows games constantly use synchronization objects (mutexes, events, \
                      semaphores). NTSYNC implements them directly in the kernel so Wine and Proton \
                      don't have to emulate them, improving frame pacing in many titles. Wine 10+ \
                      and recent Proton use it automatically when /dev/ntsync exists. Needs Linux 6.14+.",
            advanced: false,
            choices: vec![
                Choice { id: "on", label: "On", desc: "Built in, always available.", risk: Risk::Safe,
                    ops: |_| vec![y("NTSYNC")],
                    avail: |c| c.need("NTSYNC", "Needs Linux 6.14 or newer") },
                Choice { id: "off", label: "Off", desc: "Not needed without Wine/Proton.", risk: Risk::Safe,
                    ops: none, avail: ok },
            ],
        },
        Tweak {
            id: "gaming_input",
            category: Category::Gaming,
            title: "Controllers & Steam support",
            summary: "Xbox, PlayStation, Nintendo and Steam controllers plus Steam Input.",
            explain: "Steam Input creates virtual controllers through 'uinput', and Proton's \
                      sandbox needs user namespaces. This keeps those plus drivers for popular game \
                      controllers, even if none is plugged in right now — so slimming the kernel \
                      won't break a controller you only use occasionally.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "on",
                    label: "Keep",
                    desc: "Recommended for anyone who games.",
                    risk: Risk::Safe,
                    ops: |_| {
                        vec![
                            y("USER_NS"), y("FUTEX"), y("HIDRAW"), y("INPUT_JOYSTICK"), y("USB_HIDDEV"),
                            atm("INPUT_UINPUT"), atm("UHID"), atm("INPUT_JOYDEV"), atm("INPUT_EVDEV"),
                            atm("JOYSTICK_XPAD"), y("JOYSTICK_XPAD_FF"), atm("HID_PLAYSTATION"), atm("HID_SONY"),
                            atm("HID_NINTENDO"), atm("HID_STEAM"), atm("HID_MICROSOFT"), atm("HID_LOGITECH"),
                        ]
                    },
                    avail: ok,
                },
                Choice { id: "off", label: "Don't force", desc: "Only keep what's detected.", risk: Risk::Safe,
                    ops: none, avail: ok },
            ],
        },
        // ───────────────────────────── Network ─────────────────────────────
        Tweak {
            id: "tcp",
            category: Category::Network,
            title: "TCP congestion control",
            summary: "How your PC paces downloads and uploads.",
            explain: "BBR measures the real bandwidth and delay of a connection instead of reacting \
                      to packet loss, giving faster downloads and lower latency on busy or Wi-Fi \
                      networks. It's paired with the 'fq' packet scheduler. BBRv3 is the newest \
                      revision (CachyOS patchset). CUBIC is the upstream default.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "bbr3",
                    label: "BBRv3",
                    desc: "Newest BBR (CachyOS patch).",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = vec![y("TCP_CONG_ADVANCED"), y("TCP_CONG_BBR3")];
                        let mut g: Vec<&str> = TCP_DEFAULT.to_vec();
                        g.extend(["DEFAULT_BBR", "DEFAULT_BBR3"]);
                        v.extend(pick("DEFAULT_BBR3", &g));
                        v.push(sv("DEFAULT_TCP_CONG", "bbr3"));
                        v.extend([y("NET_SCH_FQ"), y("NET_SCH_DEFAULT")]);
                        v.extend(pick("DEFAULT_FQ", &QDISC));
                        v
                    },
                    avail: |c| c.need("TCP_CONG_BBR3", "Only in CachyOS-patched sources"),
                },
                Choice {
                    id: "bbr",
                    label: "BBR",
                    desc: "Upstream BBR. Faster on lossy links.",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = vec![y("TCP_CONG_ADVANCED"), y("TCP_CONG_BBR")];
                        let mut g: Vec<&str> = TCP_DEFAULT.to_vec();
                        g.extend(["DEFAULT_BBR", "DEFAULT_BBR3"]);
                        v.extend(pick("DEFAULT_BBR", &g));
                        v.push(sv("DEFAULT_TCP_CONG", "bbr"));
                        v.extend([y("NET_SCH_FQ"), y("NET_SCH_DEFAULT")]);
                        v.extend(pick("DEFAULT_FQ", &QDISC));
                        v
                    },
                    avail: ok,
                },
                Choice {
                    id: "cubic",
                    label: "CUBIC",
                    desc: "Upstream default.",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = vec![y("TCP_CONG_CUBIC")];
                        let mut g: Vec<&str> = TCP_DEFAULT.to_vec();
                        g.extend(["DEFAULT_BBR", "DEFAULT_BBR3"]);
                        v.extend(pick("DEFAULT_CUBIC", &g));
                        v.push(sv("DEFAULT_TCP_CONG", "cubic"));
                        v
                    },
                    avail: ok,
                },
            ],
        },
        // ───────────────────────────── Storage ─────────────────────────────
        Tweak {
            id: "iosched",
            category: Category::Storage,
            title: "Disk I/O schedulers",
            summary: "Build every I/O scheduler in, so each disk can use the best one.",
            explain: "NVMe drives are fastest with no scheduler, SATA SSDs like mq-deadline (or \
                      CachyOS's ADIOS), and spinning hard drives feel much smoother with BFQ. \
                      Building them all in lets the optional runtime tuning pack pick per disk.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "all",
                    label: "Build all in",
                    desc: "mq-deadline, Kyber, BFQ (and ADIOS if available).",
                    risk: Risk::Safe,
                    ops: |_| vec![y("MQ_IOSCHED_DEADLINE"), y("MQ_IOSCHED_KYBER"), y("IOSCHED_BFQ"), y("MQ_IOSCHED_ADIOS")],
                    avail: ok,
                },
                Choice { id: "keep", label: "Leave as is", desc: "Use the base config.", risk: Risk::Safe,
                    ops: none, avail: ok },
            ],
        },
        // ───────────────────────────── Size ─────────────────────────────
        Tweak {
            id: "debug",
            category: Category::Size,
            title: "Debug information",
            summary: "Extra data for debuggers — the biggest factor in build time and disk use.",
            explain: "Debug info doesn't make the installed kernel slower, but it makes the build \
                      several times larger and slower. BTF is a compact type format that sched_ext \
                      and BPF tools need, and AutoFDO needs debug info to map profiles back to code. \
                      Choose 'None' for the fastest, smallest build when you need neither.",
            advanced: false,
            choices: vec![
                Choice {
                    id: "btf",
                    label: "Minimal + BTF",
                    desc: "Needed for sched_ext, BPF tools and AutoFDO.",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = pick("DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT", &DWARF);
                        v.extend([y("DEBUG_INFO_BTF"), y("DEBUG_INFO_BTF_MODULES"), n("DEBUG_INFO_REDUCED")]);
                        v
                    },
                    avail: |c| {
                        if c.sys.toolchain.pahole {
                            Ok(())
                        } else {
                            Err("Install dev-util/pahole for BTF".into())
                        }
                    },
                },
                Choice {
                    id: "none",
                    label: "None",
                    desc: "Fastest build, smallest files.",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = pick("DEBUG_INFO_NONE", &DWARF);
                        v.push(n("DEBUG_INFO_BTF"));
                        v
                    },
                    avail: |c| {
                        if c.sel("sched_ext") == "on" {
                            Err("sched_ext needs BTF — turn it off first".into())
                        } else if c.sel("autofdo") != "off" {
                            Err("AutoFDO needs debug info".into())
                        } else {
                            Ok(())
                        }
                    },
                },
                Choice { id: "keep", label: "Leave as is", desc: "Whatever the base config has.", risk: Risk::Safe,
                    ops: none, avail: ok },
            ],
        },
        Tweak {
            id: "debug_checks",
            category: Category::Size,
            title: "Developer self-checks",
            summary: "Turn off kernel-developer sanity checks that slow everything down.",
            explain: "Options like lockdep, KASAN or DEBUG_PREEMPT catch bugs while developing the \
                      kernel but add overhead to every lock, allocation or context switch. Distro \
                      configs occasionally leave a few on; normal users never need them.",
            advanced: false,
            choices: vec![
                Choice { id: "off", label: "Turn off", desc: "Recommended.", risk: Risk::Safe,
                    ops: |_| DEBUG_CHECKS.iter().map(|s| n(s)).collect(), avail: ok },
                Choice { id: "keep", label: "Leave as is", desc: "Keep base config settings.", risk: Risk::Safe,
                    ops: none, avail: ok },
            ],
        },
        Tweak {
            id: "compression",
            category: Category::Size,
            title: "Kernel & module compression",
            summary: "Compress the kernel image and its modules to save space.",
            explain: "zstd gives small files that decompress very quickly. LZ4 decompresses even \
                      faster (marginally quicker boot) but is larger. Compressed modules need kmod \
                      built with zstd support (sys-apps/kmod USE=zstd).",
            advanced: false,
            choices: vec![
                Choice {
                    id: "zstd",
                    label: "zstd",
                    desc: "Small and fast. Recommended.",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = pick("KERNEL_ZSTD", &KCOMP);
                        v.push(y("MODULE_COMPRESS"));
                        v.extend(pick("MODULE_COMPRESS_ZSTD", &MODCOMP));
                        v.push(y("MODULE_COMPRESS_ALL"));
                        v
                    },
                    avail: kmod_zstd,
                },
                Choice {
                    id: "lz4",
                    label: "LZ4 kernel + zstd modules",
                    desc: "Fastest boot decompression.",
                    risk: Risk::Safe,
                    ops: |_| {
                        let mut v = pick("KERNEL_LZ4", &KCOMP);
                        v.push(y("MODULE_COMPRESS"));
                        v.extend(pick("MODULE_COMPRESS_ZSTD", &MODCOMP));
                        v.push(y("MODULE_COMPRESS_ALL"));
                        v
                    },
                    avail: kmod_zstd,
                },
                Choice { id: "keep", label: "Leave as is", desc: "Use the base config.", risk: Risk::Safe,
                    ops: none, avail: ok },
            ],
        },
        Tweak {
            id: "rust",
            category: Category::Size,
            title: "Rust kernel support",
            summary: "Support for kernel drivers written in Rust.",
            explain: "Only a few drivers (such as the new 'nova' NVIDIA driver or Apple GPU support) \
                      need Rust today. Turning it off shortens the build and avoids needing an exact \
                      Rust toolchain version.",
            advanced: true,
            choices: vec![
                Choice { id: "off", label: "Off", desc: "Recommended unless you need a Rust driver.", risk: Risk::Safe,
                    ops: |_| vec![n("RUST")], avail: ok },
                Choice { id: "on", label: "On", desc: "Requires a matching rustc and bindgen.", risk: Risk::Moderate,
                    ops: |_| vec![y("RUST")],
                    avail: |c| if c.sys.toolchain.rustc { c.need("RUST", "Not supported by this tree") } else { Err("Install dev-lang/rust".into()) } },
            ],
        },
        // ───────────────────────────── Boot ─────────────────────────────
        Tweak {
            id: "cmdline",
            category: Category::Boot,
            title: "Built-in boot options (Clear Linux style)",
            summary: "Bake a few proven boot parameters into the kernel itself.",
            explain: "Clear Linux shipped tuned boot parameters with its kernel. These are added in \
                      front of whatever your bootloader passes, so you don't need to edit GRUB, and \
                      uninstalling the kernel removes them. 'Desktop' disables the lockup watchdogs \
                      (fewer timer interrupts) and shuffles free memory pages for better cache use. \
                      'Pro audio' also runs device interrupts in threads (threadirqs) so audio \
                      threads can be prioritised above them.",
            advanced: false,
            choices: vec![
                Choice { id: "off", label: "None", desc: "Use only your bootloader's options.", risk: Risk::Safe,
                    ops: none, avail: ok },
                Choice {
                    id: "desktop",
                    label: "Desktop",
                    desc: "nowatchdog nmi_watchdog=0 page_alloc.shuffle=1",
                    risk: Risk::Safe,
                    ops: |_| vec![y("CMDLINE_BOOL"), sv("CMDLINE", "nowatchdog nmi_watchdog=0 page_alloc.shuffle=1"), n("CMDLINE_OVERRIDE")],
                    avail: ok,
                },
                Choice {
                    id: "audio",
                    label: "Pro audio",
                    desc: "Desktop options + threadirqs",
                    risk: Risk::Safe,
                    ops: |_| vec![y("CMDLINE_BOOL"), sv("CMDLINE", "nowatchdog nmi_watchdog=0 page_alloc.shuffle=1 threadirqs"), n("CMDLINE_OVERRIDE")],
                    avail: ok,
                },
            ],
        },
        // ───────────────────────────── Security ─────────────────────────────
        Tweak {
            id: "mitigations",
            category: Category::Security,
            title: "CPU vulnerability mitigations",
            summary: "Protections against Spectre-style CPU flaws.",
            explain: "These protect you from malicious code (for example in a web page) reading \
                      memory it shouldn't. Turning them off can gain a few percent on older CPUs, \
                      much less on recent ones, and leaves you exposed. Keep them on unless this \
                      machine never runs untrusted code. You can also disable them temporarily with \
                      the 'mitigations=off' boot option instead.",
            advanced: true,
            choices: vec![
                Choice { id: "on", label: "Keep on", desc: "Recommended.", risk: Risk::Safe,
                    ops: |_| vec![y("CPU_MITIGATIONS")], avail: ok },
                Choice { id: "off", label: "Compile out", desc: "Faster, less secure.", risk: Risk::Advanced,
                    ops: |_| vec![n("CPU_MITIGATIONS")], avail: ok },
            ],
        },
    ]
}

fn autofdo_avail(c: &Ctx) -> Result<(), String> {
    if !c.clang() {
        return Err("Needs the Clang compiler".into());
    }
    if !matches!(c.sel("lto"), "thin" | "full") {
        return Err("Needs ThinLTO or Full LTO".into());
    }
    if c.sys.cpu.branch_sampling == BranchSampling::None {
        return Err("This CPU can't record branch samples (needs Intel LBR or AMD BRS/LBRv2)".into());
    }
    c.need("AUTOFDO_CLANG", "Needs Linux 6.13 or newer")
}

fn kmod_zstd(c: &Ctx) -> Result<(), String> {
    if c.sys.toolchain.kmod_zstd {
        Ok(())
    } else {
        Err("sys-apps/kmod lacks zstd support (enable USE=zstd)".into())
    }
}

/// Make every selection valid: unknown or unavailable choices fall back to
/// the first available option. Returns human-readable notes for each change.
pub fn normalize(selection: &mut Selection, sys: &SystemInfo, symbols: Option<&SymbolIndex>, provided: &HashSet<String>) -> Vec<String> {
    let mut notes = vec![];
    // Two passes: later tweaks (debug) depend on earlier ones (sched_ext,
    // autofdo), and a fallback in pass one can unlock a choice in pass two.
    for _ in 0..2 {
        for t in catalog() {
            let current = selection.get(t.id).cloned().unwrap_or_default();
            let snapshot = selection.clone();
            let ctx = Ctx { sys, symbols, provided, selection: &snapshot };
            let valid = t.choice(&current).is_some_and(|c| (c.avail)(&ctx).is_ok());
            if valid {
                continue;
            }
            if let Some(fallback) = t.choices.iter().find(|c| (c.avail)(&ctx).is_ok()) {
                if !current.is_empty() {
                    let why = t
                        .choice(&current)
                        .and_then(|c| (c.avail)(&ctx).err())
                        .unwrap_or_else(|| "unknown option".into());
                    notes.push(format!("{}: using “{}” — {}", t.title, fallback.label, why));
                }
                selection.insert(t.id.to_string(), fallback.id.to_string());
            }
        }
    }
    notes.dedup();
    notes
}

/// The Kconfig ops for every selected choice, grouped by tweak title.
pub fn ops_for(selection: &Selection, sys: &SystemInfo, symbols: Option<&SymbolIndex>, provided: &HashSet<String>) -> Vec<(String, Vec<Op>)> {
    let ctx = Ctx { sys, symbols, provided, selection };
    catalog()
        .iter()
        .filter_map(|t| {
            let c = t.choice(ctx.sel(t.id))?;
            let ops = (c.ops)(&ctx);
            (!ops.is_empty()).then(|| (format!("{}: {}", t.title, c.label), ops))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_unique_and_choices_present() {
        let mut seen = HashSet::new();
        for t in catalog() {
            assert!(seen.insert(t.id), "duplicate tweak id {}", t.id);
            assert!(!t.choices.is_empty());
            let mut cs = HashSet::new();
            for c in &t.choices {
                assert!(cs.insert(c.id), "duplicate choice {} in {}", c.id, t.id);
            }
        }
    }

    #[test]
    fn pick_disables_siblings() {
        let ops = pick("HZ_1000", &HZ);
        assert_eq!(ops[0], y("HZ_1000"));
        assert!(ops.contains(&n("HZ_300")));
        assert!(!ops.contains(&n("HZ_1000")));
    }
}
