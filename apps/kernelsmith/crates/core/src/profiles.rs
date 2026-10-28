//! Use-case profiles: a starting point for every tweak, explained in terms of
//! what the user wants to do rather than what the kernel options are called.

use crate::hw::SystemInfo;
use crate::slim::SlimMode;
use crate::tweaks::Selection;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ProfileId {
    Gaming,
    Desktop,
    Creator,
    Workstation,
    Laptop,
    Server,
}

pub struct Profile {
    pub id: ProfileId,
    pub title: &'static str,
    pub icon: &'static str,
    pub tagline: &'static str,
    pub description: &'static str,
    pub highlights: &'static [&'static str],
    /// Which well-known kernel this profile resembles, to build trust.
    pub similar_to: &'static str,
    pub slim: SlimMode,
    /// Patch ids suggested when the source doesn't already include them.
    pub suggested_patches: &'static [&'static str],
}

pub const ALL: [ProfileId; 6] = [
    ProfileId::Gaming,
    ProfileId::Desktop,
    ProfileId::Creator,
    ProfileId::Workstation,
    ProfileId::Laptop,
    ProfileId::Server,
];

pub fn get(id: ProfileId) -> Profile {
    match id {
        ProfileId::Gaming => Profile {
            id,
            title: "Gaming",
            icon: "🎮",
            tagline: "Smooth frame times, low input latency",
            description: "Tuned for Steam, Proton and Wine: the BORE scheduler keeps your game \
                          responsive even while something compiles in the background, NTSYNC \
                          speeds up Windows games, and 1000 Hz timers keep input snappy.",
            highlights: &["BORE scheduler", "NTSYNC", "1000 Hz", "ThinLTO + native CPU", "Controller support"],
            similar_to: "CachyOS default kernel",
            slim: SlimMode::Balanced,
            suggested_patches: &["bore"],
        },
        ProfileId::Desktop => Profile {
            id,
            title: "Desktop & Multimedia",
            icon: "🖥",
            tagline: "Snappy everyday desktop, video and music",
            description: "A responsive all-rounder for browsing, streaming, light gaming and media \
                          playback, with conservative memory settings and efficient CPU clocks.",
            highlights: &["BORE scheduler", "Efficient CPU clocks", "Compressed memory", "BBR networking"],
            similar_to: "linux-zen",
            slim: SlimMode::Balanced,
            suggested_patches: &["bore"],
        },
        ProfileId::Creator => Profile {
            id,
            title: "Creator & Pro Audio",
            icon: "🎧",
            tagline: "Low-latency audio, video editing, streaming",
            description: "For DAWs, PipeWire/JACK, OBS and video editing: threaded interrupts and \
                          full preemption keep audio buffers small without crackles, while webcams \
                          and USB audio interfaces stay supported.",
            highlights: &["threadirqs", "Full preemption", "1000 Hz", "USB audio & webcams"],
            similar_to: "linux-rt-lite / Ubuntu Studio lowlatency",
            slim: SlimMode::Balanced,
            suggested_patches: &[],
        },
        ProfileId::Workstation => Profile {
            id,
            title: "Workstation & Development",
            icon: "🛠",
            tagline: "Compiling, VMs, containers, rendering",
            description: "Throughput first: the upstream EEVDF scheduler with lazy preemption gets \
                          long jobs done fastest, and virtualization plus container support (KVM, \
                          VFIO, Docker/Podman) is kept.",
            highlights: &["Lazy preemption", "KVM & containers", "Huge pages", "ThinLTO"],
            similar_to: "Clear Linux kernel",
            slim: SlimMode::Balanced,
            suggested_patches: &[],
        },
        ProfileId::Laptop => Profile {
            id,
            title: "Laptop & Battery",
            icon: "🔋",
            tagline: "Long battery life, cool and quiet",
            description: "Moderate timer rate, efficient CPU scaling and tickless idle to stretch \
                          battery life, while keeping docks, Thunderbolt and phone tethering working.",
            highlights: &["300 Hz", "Schedutil", "Docks & Thunderbolt", "acpi_call"],
            similar_to: "Fedora / Ubuntu generic, tuned",
            slim: SlimMode::Balanced,
            suggested_patches: &["acpi_call"],
        },
        ProfileId::Server => Profile {
            id,
            title: "Server & Throughput",
            icon: "🗄",
            tagline: "Maximum throughput, rock-solid",
            description: "Low timer rate, lazy preemption and upstream scheduling for servers, \
                          NAS boxes and headless build machines. BBR speeds up network transfers.",
            highlights: &["250 Hz", "Lazy preemption", "BBR", "Huge pages"],
            similar_to: "linux-lts / server kernels",
            slim: SlimMode::Balanced,
            suggested_patches: &[],
        },
    }
}

/// Default tweak choices for a profile. `normalize` later swaps out anything
/// the chosen source or toolchain can't do.
pub fn defaults(id: ProfileId, sys: &SystemInfo) -> Selection {
    let clang = sys.toolchain.clang_ready();
    let pahole = sys.toolchain.pahole;
    let laptop = sys.is_laptop();
    let mut s = Selection::new();
    let mut set = |k: &str, v: &str| {
        s.insert(k.to_string(), v.to_string());
    };

    // Shared baseline: the things that are a clear win on any modern PC.
    set("compiler", if clang { "clang" } else { "gcc" });
    set("lto", if clang { "thin" } else { "none" });
    set("optimize", "o3");
    set("autofdo", "off");
    set("march", "native");
    set("governor", if laptop { "schedutil" } else { "performance" });
    set("scheduler", "bore");
    set("sched_ext", if pahole { "on" } else { "off" });
    set("hz", "1000");
    set("preempt", "full");
    set("tick", "idle");
    set("thp", "madvise");
    set("mglru", "on");
    set("zswap", "on");
    set("ntsync", "on");
    set("gaming_input", "on");
    set("tcp", "bbr3");
    set("iosched", "all");
    set("debug", if pahole { "btf" } else { "none" });
    set("debug_checks", "off");
    set("compression", "zstd");
    set("rust", "off");
    set("cmdline", "desktop");
    set("mitigations", "on");
    set("signing", "sign");
    set("anticheat", "off");
    set("lsm_trim", "unused_off");
    set("hardening", "keep");
    set("cpu_trim", "this_pc");
    set("guest_trim", "bare_metal");
    set("platform_trim", "this_pc");

    match id {
        ProfileId::Gaming => {
            set("thp", "always");
        }
        ProfileId::Desktop => {
            set("governor", "schedutil");
            set("optimize", "o2");
        }
        ProfileId::Creator => {
            set("scheduler", "eevdf");
            set("cmdline", "audio");
            set("ntsync", "off");
            set("tcp", "cubic");
        }
        ProfileId::Workstation => {
            set("scheduler", "eevdf");
            set("hz", "300");
            set("preempt", "lazy");
            set("thp", "always");
            set("governor", "schedutil");
            set("ntsync", "off");
            set("gaming_input", "off");
            set("optimize", "o2");
        }
        ProfileId::Laptop => {
            set("hz", "300");
            set("governor", "schedutil");
            set("optimize", "o2");
        }
        ProfileId::Server => {
            set("scheduler", "eevdf");
            set("hz", "250");
            set("preempt", "lazy");
            set("thp", "always");
            set("ntsync", "off");
            set("gaming_input", "off");
            set("cmdline", "off");
            set("tcp", "bbr");
            set("optimize", "o2");
            // Servers get cloned, migrated and virtualised: keep the generic layout.
            set("cpu_trim", "keep");
            set("guest_trim", "keep");
            set("platform_trim", "keep");
            set("lsm_trim", "keep");
        }
    }
    s
}

/// Pick the profile that best matches the machine, for the "Recommended" badge.
pub fn recommend(sys: &SystemInfo) -> ProfileId {
    if sys.handheld.is_some() {
        return ProfileId::Gaming;
    }
    if sys.is_laptop() {
        return ProfileId::Laptop;
    }
    let discrete = sys.has_gpu(crate::hw::GpuVendor::Nvidia) || sys.has_gpu(crate::hw::GpuVendor::Amd);
    if sys.gpus.is_empty() {
        return ProfileId::Server;
    }
    if discrete {
        ProfileId::Gaming
    } else {
        ProfileId::Desktop
    }
}
