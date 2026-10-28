//! Optional runtime tuning pack: sysctl and udev settings that complement the
//! kernel options (the "system-wide tuning" half of what made Clear Linux and
//! CachyOS fast). Works the same under OpenRC (sysctl service, eudev/udev) and
//! systemd (systemd-sysctl, systemd-udevd). Fully removable.

pub const SYSCTL_PATH: &str = "/etc/sysctl.d/90-kernelsmith.conf";
pub const UDEV_PATH: &str = "/etc/udev/rules.d/60-kernelsmith-ioschedulers.rules";

pub fn sysctl_conf(gaming: bool) -> String {
    let mut s = String::from(
        "# Kernelsmith runtime tuning. Remove from Kernelsmith → Library → Runtime tuning,\n\
         # or simply delete this file.\n\n\
         # Keep directory/inode caches around longer (faster file browsing and game loads).\n\
         vm.vfs_cache_pressure = 50\n\n\
         # Start writing dirty data earlier and in smaller batches: avoids multi-second\n\
         # stalls when copying big files.\n\
         vm.dirty_bytes = 268435456\n\
         vm.dirty_background_bytes = 67108864\n\n\
         # The NMI watchdog fires a periodic interrupt on every core; not needed on desktops.\n\
         kernel.nmi_watchdog = 0\n\n\
         # Larger network backlog for fast connections.\n\
         net.core.netdev_max_backlog = 4096\n",
    );
    if gaming {
        s.push_str(
            "\n# Some games (and Proton titles) need many memory mappings; SteamOS uses this value.\n\
             vm.max_map_count = 2147483642\n\n\
             # Don't deliberately slow down programs that trigger split locks (affects some\n\
             # older games). Only meaningful on CPUs with split-lock detection.\n\
             kernel.split_lock_mitigate = 0\n",
        );
    }
    s
}

pub fn udev_rules() -> String {
    String::from(
        "# Kernelsmith: pick the best I/O scheduler per disk type.\n\
         # Spinning hard drives: BFQ keeps the desktop responsive during heavy I/O.\n\
         ACTION==\"add|change\", KERNEL==\"sd[a-z]*\", ATTR{queue/rotational}==\"1\", ATTR{queue/scheduler}=\"bfq\"\n\
         # SATA SSDs and eMMC: mq-deadline.\n\
         ACTION==\"add|change\", KERNEL==\"sd[a-z]*|mmcblk[0-9]*\", ATTR{queue/rotational}==\"0\", ATTR{queue/scheduler}=\"mq-deadline\"\n\
         # NVMe drives are fastest without a scheduler.\n\
         ACTION==\"add|change\", KERNEL==\"nvme[0-9]*n[0-9]*\", ATTR{queue/scheduler}=\"none\"\n",
    )
}

/// Shell snippet that installs the pack and records it for removal.
pub fn install_snippet(gaming: bool) -> String {
    format!(
        "mkdir -p /etc/sysctl.d /etc/udev/rules.d {state}\n\
         cat > {sysctl} <<'KS_EOF'\n{sysctl_body}KS_EOF\n\
         cat > {udev} <<'KS_EOF'\n{udev_body}KS_EOF\n\
         printf 'files = [\"%s\", \"%s\"]\\ninstalled = %s\\n' {sysctl} {udev} \"$(date +%s)\" > {manifest}\n\
         sysctl -q -p {sysctl} || true\n\
         if command -v udevadm >/dev/null; then udevadm control --reload || true; udevadm trigger --subsystem-match=block --action=change || true; fi\n",
        state = crate::paths::SYSTEM_STATE,
        sysctl = SYSCTL_PATH,
        udev = UDEV_PATH,
        sysctl_body = sysctl_conf(gaming),
        udev_body = udev_rules(),
        manifest = crate::paths::tuning_manifest().display(),
    )
}

pub fn remove_snippet() -> String {
    format!(
        "rm -f {sysctl} {udev} {manifest}\n\
         if command -v udevadm >/dev/null; then udevadm control --reload || true; fi\n\
         echo '::note::Runtime tuning removed. Defaults return after a reboot.'\n",
        sysctl = SYSCTL_PATH,
        udev = UDEV_PATH,
        manifest = crate::paths::tuning_manifest().display(),
    )
}

pub fn is_installed() -> bool {
    crate::paths::tuning_manifest().exists() || std::path::Path::new(SYSCTL_PATH).exists()
}
