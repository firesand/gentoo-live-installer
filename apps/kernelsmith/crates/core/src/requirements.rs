//! Options that must survive any customization for the kernel to boot and for
//! the user's init system and drivers to work.
//!
//! These are applied after the user's tweaks and verified afterwards. Groups
//! marked `critical` abort the build if they don't stick — that is the
//! difference between "slightly slower than hoped" and "doesn't boot".

use crate::hw::{CpuVendor, DiskKind, GpuVendor, InitSystem, SystemInfo};
use crate::tweaks::{atm, n, val, y, Op};

pub struct ReqGroup {
    pub title: String,
    pub why: String,
    pub ops: Vec<Op>,
    pub critical: bool,
}

fn group(title: &str, why: &str, critical: bool, ops: Vec<Op>) -> ReqGroup {
    ReqGroup { title: title.into(), why: why.into(), ops, critical }
}

pub fn fs_symbol(fs: &str) -> Option<&'static str> {
    Some(match fs {
        "ext4" | "ext3" | "ext2" => "EXT4_FS",
        "btrfs" => "BTRFS_FS",
        "xfs" => "XFS_FS",
        "f2fs" => "F2FS_FS",
        "vfat" | "msdos" => "VFAT_FS",
        "exfat" => "EXFAT_FS",
        "ntfs3" | "ntfs" => "NTFS3_FS",
        "jfs" => "JFS_FS",
        "nilfs2" => "NILFS2_FS",
        "iso9660" => "ISO9660_FS",
        "udf" => "UDF_FS",
        "fuseblk" | "fuse" => "FUSE_FS",
        "squashfs" => "SQUASHFS",
        "erofs" => "EROFS_FS",
        _ => return None,
    })
}

pub fn groups(sys: &SystemInfo, inits: &[InitSystem]) -> Vec<ReqGroup> {
    let mut out = vec![];

    // ── Boot essentials ──
    let mut boot = vec![
        y("MODULES"), y("MODULE_UNLOAD"), y("BLK_DEV_INITRD"), y("RD_GZIP"), y("RD_XZ"), y("RD_ZSTD"), y("RD_LZ4"),
        y("DEVTMPFS"), y("DEVTMPFS_MOUNT"), y("TMPFS"), y("TMPFS_POSIX_ACL"), y("PROC_FS"), y("SYSFS"),
        y("BINFMT_ELF"), y("BINFMT_SCRIPT"), atm("BINFMT_MISC"), y("UNIX"), y("INET"),
        y("IKCONFIG"), y("IKCONFIG_PROC"), y("FW_LOADER"), y("FW_LOADER_COMPRESS"),
        y("FW_LOADER_COMPRESS_XZ"), y("FW_LOADER_COMPRESS_ZSTD"), y("MICROCODE"), n("LOCALVERSION_AUTO"),
    ];
    if sys.boot.uefi {
        boot.extend([y("EFI"), y("EFI_STUB"), y("EFI_PARTITION"), atm("EFIVAR_FS"), y("SYSFB_SIMPLEFB"),
            y("DRM"), y("DRM_SIMPLEDRM"), atm("VFAT_FS"), atm("NLS_CODEPAGE_437"), atm("NLS_ISO8859_1")]);
    }
    out.push(group(
        "Boot essentials",
        "Initramfs, device nodes, firmware loading and EFI boot. Also keeps /proc/config.gz so the \
         next rebuild can start from this kernel.",
        true,
        boot,
    ));

    // ── Root filesystem and the disk controller it lives on ──
    let mut root = vec![];
    match fs_symbol(&sys.root_fs) {
        Some(sym) => root.push(y(sym)),
        None if sys.root_fs == "zfs" => {}
        None => {}
    }
    if sys.root_on_dm {
        root.extend([y("MD"), atm("BLK_DEV_DM"), atm("DM_CRYPT")]);
    }
    if sys.storage.iter().any(|d| d.kind == DiskKind::Nvme) {
        root.extend([y("BLK_DEV_NVME")]);
    }
    if sys.vmd {
        // Intel VMD hides the NVMe drives behind its own controller; built in, the root
        // disk is reachable even if the initramfs forgets the module.
        root.push(y("VMD"));
    }
    if sys.storage.iter().any(|d| matches!(d.kind, DiskKind::Ssd | DiskKind::Hdd)) {
        root.extend([y("SCSI"), y("BLK_DEV_SD"), y("ATA"), y("SATA_AHCI")]);
    }
    if sys.storage.iter().any(|d| d.kind == DiskKind::Virtual) {
        root.extend([y("VIRTIO_PCI"), y("VIRTIO_BLK")]);
    }
    if sys.storage.iter().any(|d| d.kind == DiskKind::Mmc) {
        root.extend([y("MMC"), y("MMC_BLOCK")]);
    }
    if !root.is_empty() {
        out.push(group(
            &format!("Root filesystem ({}) & disk controllers", sys.root_fs),
            "Built directly into the kernel so your system can always find its disk, even if the \
             initramfs is missing a module.",
            true,
            root,
        ));
    }

    // Other mounted filesystems: keep them, at least as modules.
    let others: Vec<Op> = sys
        .mounted_fs
        .iter()
        .filter(|f| **f != sys.root_fs)
        .filter_map(|f| fs_symbol(f))
        .map(atm)
        .collect();
    if !others.is_empty() {
        out.push(group("Mounted filesystems", "Filesystems currently in use on this PC.", true, others));
    }

    // ── Init systems ──
    let inits: Vec<InitSystem> = if inits.is_empty() || inits.contains(&InitSystem::Unknown) {
        vec![InitSystem::OpenRc, InitSystem::Systemd]
    } else {
        inits.to_vec()
    };
    let portage_ns = ["CGROUPS", "NAMESPACES", "IPC_NS", "NET_NS", "PID_NS", "SYSVIPC", "USER_NS", "UTS_NS"];
    if inits.contains(&InitSystem::OpenRc) {
        let mut ops = vec![y("GENTOO_LINUX"), y("GENTOO_LINUX_UDEV"), y("GENTOO_LINUX_PORTAGE"), y("GENTOO_LINUX_INIT_SCRIPT")];
        ops.extend(
            ["BINFMT_SCRIPT", "CGROUPS", "EPOLL", "FILE_LOCKING", "INOTIFY_USER", "SIGNALFD", "TIMERFD", "SHMEM", "EVENTFD", "FHANDLE"]
                .iter()
                .map(|s| y(s)),
        );
        ops.extend(portage_ns.iter().map(|s| y(s)));
        out.push(group(
            "OpenRC support",
            "Everything OpenRC, udev and Portage's sandboxing need (the same set Gentoo's \
             GENTOO_LINUX_INIT_SCRIPT option selects).",
            true,
            ops,
        ));
    }
    if inits.contains(&InitSystem::Systemd) {
        let mut ops = vec![y("GENTOO_LINUX"), y("GENTOO_LINUX_UDEV"), y("GENTOO_LINUX_PORTAGE"), y("GENTOO_LINUX_INIT_SYSTEMD")];
        ops.extend(
            [
                "AUTOFS_FS", "BPF_SYSCALL", "CGROUP_BPF", "CGROUPS", "DMIID", "EPOLL", "FANOTIFY", "FHANDLE",
                "FILE_LOCKING", "INOTIFY_USER", "IPV6", "KCMP", "NET", "NET_NS", "PROC_FS", "SECCOMP",
                "SECCOMP_FILTER", "SIGNALFD", "SYSFS", "TIMERFD", "TMPFS_POSIX_ACL", "TMPFS_XATTR", "EVENTFD",
                "CRYPTO_USER_API_HASH",
            ]
            .iter()
            .map(|s| y(s)),
        );
        ops.extend(portage_ns.iter().map(|s| y(s)));
        out.push(group(
            "systemd support",
            "Everything systemd needs (the same set Gentoo's GENTOO_LINUX_INIT_SYSTEMD option selects).",
            true,
            ops,
        ));
    }

    // ── Graphics ──
    if sys.needs_nvidia_support() || sys.has_gpu(GpuVendor::Nvidia) {
        out.push(group(
            "NVIDIA driver requirements",
            "x11-drivers/nvidia-drivers refuses to build without these. DRM_QXL is a tiny virtual-GPU \
             driver kept only because it selects DRM_TTM and DRM_TTM_HELPER, promptless symbols that \
             nvidia_drm links against and that vanish once no in-tree GPU driver selects them.",
            sys.needs_nvidia_support(),
            vec![
                y("PROC_FS"), y("SYSVIPC"), y("X86_PAT"), y("MODULES"), y("DRM"), y("DRM_SIMPLEDRM"),
                y("DRM_FBDEV_EMULATION"), atm("DRM_QXL"), n("LOCKDEP"), n("PROVE_LOCKING"),
                n("SLUB_DEBUG_ON"), n("DEBUG_MUTEXES"),
            ],
        ));
    }
    for g in &sys.gpus {
        let ops = match (g.gpu_vendor(), g.driver.as_deref()) {
            (GpuVendor::Amd, _) => vec![atm("DRM_AMDGPU"), y("HSA_AMD")],
            (GpuVendor::Intel, Some("xe")) => vec![atm("DRM_XE")],
            (GpuVendor::Intel, Some("i915")) => vec![atm("DRM_I915")],
            _ => continue,
        };
        out.push(group(&format!("{} graphics", g.name), "Driver for your graphics card.", false, ops));
    }

    // ── CPU platform features ──
    let mut cpu = vec![y("SMP"), y("SCHED_MC")];
    match sys.cpu.vendor {
        CpuVendor::Intel => {
            cpu.extend([y("CPU_SUP_INTEL"), y("X86_INTEL_PSTATE"), y("INTEL_IDLE"), atm("SENSORS_CORETEMP"), atm("X86_PKG_TEMP_THERMAL")]);
            if sys.cpu.hybrid {
                cpu.extend([y("SCHED_MC_PRIO"), y("X86_THERMAL_VECTOR"), y("INTEL_HFI_THERMAL")]);
            }
        }
        CpuVendor::Amd => {
            cpu.extend([y("CPU_SUP_AMD"), y("X86_AMD_PSTATE"), val("X86_AMD_PSTATE_DEFAULT_MODE", "3"), y("SCHED_MC_PRIO"), atm("SENSORS_K10TEMP")]);
            if sys.is_laptop() {
                cpu.push(atm("AMD_PMC"));
            }
        }
        CpuVendor::Other => {}
    }
    let hybrid_note = if sys.cpu.hybrid { " Includes Thread Director support so games land on P-cores." } else { "" };
    out.push(group(
        "CPU power & topology",
        &format!("Frequency scaling, idle states and core-ranking for your {}.{hybrid_note}", sys.cpu.model_name),
        false,
        cpu,
    ));

    if sys.vm_guest.is_some() {
        out.push(group(
            "Virtual machine guest",
            "This system runs inside a VM; keep paravirtual drivers.",
            true,
            vec![y("HYPERVISOR_GUEST"), y("PARAVIRT"), y("KVM_GUEST"), y("VIRTIO_PCI"), y("VIRTIO_BLK"),
                atm("VIRTIO_NET"), atm("VIRTIO_CONSOLE"), atm("DRM_VIRTIO_GPU"), atm("VIRTIO_BALLOON")],
        ));
    }
    out
}
