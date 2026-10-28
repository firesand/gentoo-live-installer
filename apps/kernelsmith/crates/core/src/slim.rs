//! Hardware-specific slimming with `make localmodconfig`.
//!
//! localmodconfig disables every module that isn't currently loaded. That is
//! what makes a kernel small and fast to build — and also what breaks the USB
//! stick you plug in next week. "Keep" categories bring back whole driver
//! families (via `LMC_KEEP`, which matches Kconfig file paths) plus a few
//! explicit options.

use crate::hw::{CpuVendor, SystemInfo};
use crate::profiles::ProfileId;
use crate::tweaks::{atm, y, Op};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum SlimMode {
    /// Keep every driver the base config has.
    Off,
    /// localmodconfig + keep categories (recommended).
    Balanced,
    /// localmodconfig with only what's loaded right now.
    Aggressive,
}

impl SlimMode {
    pub fn label(self) -> &'static str {
        match self {
            SlimMode::Off => "Keep everything",
            SlimMode::Balanced => "Tailored (recommended)",
            SlimMode::Aggressive => "Minimal",
        }
    }

    pub fn explain(self) -> &'static str {
        match self {
            SlimMode::Off => "Keeps all drivers from the base configuration. Largest kernel and \
                              longest build (often 20–40 minutes), but supports any hardware.",
            SlimMode::Balanced => "Builds drivers for the hardware in this PC plus the device \
                                   families you tick below. Typically 4–8× faster to build and much \
                                   smaller, while everyday gadgets keep working.",
            SlimMode::Aggressive => "Only drivers that are loaded right now. Smallest and fastest, \
                                     but a device you plug in later may not work until you rebuild.",
        }
    }
}

pub struct KeepCategory {
    pub id: &'static str,
    pub icon: &'static str,
    pub title: &'static str,
    pub desc: &'static str,
    /// Kconfig path prefixes passed to localmodconfig through LMC_KEEP.
    pub lmc_paths: &'static [&'static str],
    pub ops: fn(&SystemInfo) -> Vec<Op>,
    pub default_on: fn(&SystemInfo, ProfileId) -> bool,
}

pub fn categories() -> Vec<KeepCategory> {
    vec![
        KeepCategory {
            id: "usb_storage",
            icon: "💾",
            title: "USB drives & card readers",
            desc: "Flash drives, external disks, SD card readers.",
            lmc_paths: &["drivers/usb/storage", "drivers/mmc", "drivers/memstick"],
            ops: |_| vec![atm("USB_STORAGE"), atm("USB_UAS"), atm("MMC_BLOCK"), atm("MMC_SDHCI_PCI")],
            default_on: |_, _| true,
        },
        KeepCategory {
            id: "external_fs",
            icon: "📁",
            title: "Filesystems for external drives",
            desc: "FAT/exFAT/NTFS/ISO — what USB sticks, cameras and Windows disks use.",
            lmc_paths: &["fs/fat", "fs/exfat", "fs/ntfs3", "fs/isofs", "fs/udf", "fs/fuse", "fs/nls"],
            ops: |_| {
                ["VFAT_FS", "EXFAT_FS", "NTFS3_FS", "ISO9660_FS", "UDF_FS", "FUSE_FS", "NLS_CODEPAGE_437", "NLS_ISO8859_1", "NLS_UTF8"]
                    .iter()
                    .map(|s| atm(s))
                    .collect()
            },
            default_on: |_, _| true,
        },
        KeepCategory {
            id: "more_fs",
            icon: "🗂",
            title: "More filesystems",
            desc: "ext4/XFS/F2FS/Btrfs disks, squashfs & erofs (Flatpak, AppImage, Steam), HFS+, Windows (SMB) and NFS shares.",
            lmc_paths: &[
                "fs/ext4", "fs/xfs", "fs/f2fs", "fs/btrfs", "fs/squashfs", "fs/erofs", "fs/hfsplus", "fs/hfs",
                "fs/smb", "fs/nfs", "fs/nfsd", "fs/nfs_common", "fs/netfs", "fs/cachefiles", "fs/autofs", "net/sunrpc",
            ],
            ops: |_| {
                ["EXT4_FS", "XFS_FS", "F2FS_FS", "BTRFS_FS", "SQUASHFS", "EROFS_FS", "HFSPLUS_FS", "CIFS", "NFS_FS", "NFSD", "AUTOFS_FS"]
                    .iter()
                    .map(|s| atm(s))
                    .collect()
            },
            default_on: |_, _| true,
        },
        KeepCategory {
            id: "block_dm",
            icon: "🔒",
            title: "Encrypted, virtual & RAM disks",
            desc: "LUKS (dm-crypt), LVM, software RAID, loop devices for ISO images, zram, network block devices, and the ciphers they need.",
            // crypto/ and lib/crypto/ are kept whole: a LUKS volume opened next month needs
            // ciphers that are not loaded today.
            lmc_paths: &["drivers/md", "drivers/block", "crypto", "arch/x86/crypto", "lib/crypto"],
            ops: |_| {
                ["BLK_DEV_LOOP", "BLK_DEV_DM", "DM_CRYPT", "DM_SNAPSHOT", "BLK_DEV_NBD", "ZRAM", "CRYPTO_XTS", "CRYPTO_AES_NI_INTEL"]
                    .iter()
                    .map(|s| atm(s))
                    .collect()
            },
            default_on: |_, _| true,
        },
        KeepCategory {
            id: "input",
            icon: "⌨",
            title: "Keyboards, mice & controllers",
            desc: "Any USB or Bluetooth input device, including game controllers.",
            lmc_paths: &["drivers/hid", "drivers/input"],
            ops: |_| vec![atm("HID_GENERIC"), atm("USB_HID"), atm("INPUT_UINPUT"), y("HIDRAW")],
            default_on: |_, _| true,
        },
        KeepCategory {
            id: "bluetooth",
            icon: "📶",
            title: "Bluetooth",
            desc: "Wireless headsets, controllers, keyboards.",
            lmc_paths: &["net/bluetooth", "drivers/bluetooth"],
            ops: |_| vec![atm("BT"), atm("BT_HCIBTUSB"), atm("BT_RFCOMM"), atm("BT_HIDP")],
            default_on: |s, _| s.has_bluetooth,
        },
        KeepCategory {
            id: "usb_audio",
            icon: "🎧",
            title: "USB headsets, audio interfaces & MIDI",
            desc: "USB DACs, gaming headsets, audio interfaces, MIDI keyboards.",
            lmc_paths: &["sound/usb", "sound/core", "sound/drivers"],
            ops: |_| vec![atm("SND_USB_AUDIO"), atm("SND_SEQ"), atm("SND_RAWMIDI"), atm("SND_HRTIMER"), atm("SND_ALOOP"), atm("SND_VIRMIDI")],
            default_on: |_, p| p != ProfileId::Server,
        },
        KeepCategory {
            id: "webcam",
            icon: "📷",
            title: "Webcams & capture cards",
            desc: "USB webcams (UVC) and video capture devices.",
            lmc_paths: &["drivers/media/usb", "drivers/media/v4l2-core", "drivers/media/mc", "drivers/media/common"],
            ops: |_| vec![atm("MEDIA_SUPPORT"), y("MEDIA_USB_SUPPORT"), y("MEDIA_CAMERA_SUPPORT"), atm("USB_VIDEO_CLASS")],
            default_on: |_, p| p != ProfileId::Server,
        },
        KeepCategory {
            id: "network_extra",
            icon: "🔐",
            title: "VPN & virtual networking",
            desc: "WireGuard, OpenVPN (TUN), bridges, VLANs, traffic shaping — used by VPN apps, Tailscale and Steam Link.",
            // WIREGUARD lives in drivers/net/Kconfig and BRIDGE_NETFILTER in net/Kconfig, which
            // LMC_KEEP's per-Kconfig-file matching cannot single out: they are explicit ops.
            lmc_paths: &["drivers/net/wireguard", "net/bridge", "net/8021q", "net/sched", "net/tls", "net/xfrm", "net/key"],
            ops: |_| {
                vec![
                    atm("WIREGUARD"), atm("TUN"), atm("BRIDGE"), atm("VETH"), atm("BRIDGE_NETFILTER"), atm("VLAN_8021Q"),
                    atm("TLS"), atm("IFB"), atm("DUMMY"), atm("NET_SCH_CAKE"), atm("NET_CLS_BPF"), atm("NET_ACT_BPF"),
                ]
            },
            default_on: |_, _| true,
        },
        KeepCategory {
            id: "virtualization",
            icon: "💻",
            title: "Virtual machines & containers",
            desc: "KVM/QEMU, VFIO passthrough, Docker, Podman, Flatpak sandboxes.",
            lmc_paths: &[
                "arch/x86/kvm", "virt", "drivers/vfio", "drivers/vhost", "net/netfilter",
                "net/ipv4/netfilter", "net/ipv6/netfilter", "fs/overlayfs", "net/bridge", "net/vmw_vsock",
            ],
            ops: |s| {
                let mut v = vec![
                    atm("KVM"), atm("VFIO"), atm("VFIO_PCI"), atm("VHOST_NET"), atm("VSOCKETS"), atm("VHOST_VSOCK"), atm("TUN"), atm("BRIDGE"),
                    atm("VETH"), atm("OVERLAY_FS"), atm("BRIDGE_NETFILTER"), atm("MACVLAN"), atm("MACVTAP"), atm("IPVLAN"),
                    atm("VXLAN"), atm("TAP"),
                ];
                v.push(atm(if s.cpu.vendor == CpuVendor::Amd { "KVM_AMD" } else { "KVM_INTEL" }));
                v
            },
            default_on: |s, p| s.kvm_loaded || p == ProfileId::Workstation,
        },
        KeepCategory {
            id: "docks",
            icon: "🔌",
            title: "Docks, Thunderbolt & USB-C",
            desc: "Thunderbolt/USB4 docks, USB-C displays and dock network adapters.",
            lmc_paths: &["drivers/thunderbolt", "drivers/usb/typec"],
            // USB_NET_DRIVERS/USB_USBNET are the gates the dock NIC drivers depend on;
            // without them localmodconfig leaves the three requests unsatisfiable.
            ops: |_| {
                vec![
                    atm("USB4"), atm("TYPEC"), atm("USB_NET_DRIVERS"), atm("USB_USBNET"), atm("USB_RTL8152"),
                    atm("USB_NET_AX88179_178A"), atm("USB_NET_CDC_NCM"),
                ]
            },
            default_on: |s, p| s.is_laptop() || p == ProfileId::Laptop || s.loaded_modules.iter().any(|m| m == "thunderbolt"),
        },
        KeepCategory {
            id: "phones",
            icon: "📱",
            title: "Phone tethering & USB network",
            desc: "Share your phone's mobile data over USB.",
            lmc_paths: &["drivers/net/usb"],
            ops: |_| vec![atm("USB_USBNET"), atm("USB_NET_CDCETHER"), atm("USB_NET_RNDIS_HOST"), atm("USB_IPHETH")],
            default_on: |s, _| s.is_laptop(),
        },
        KeepCategory {
            id: "sensors",
            icon: "🌡",
            title: "Sensors, fans & RGB controllers",
            desc: "Motherboard sensors, AIO coolers, PSU monitoring and LED controllers (lm-sensors, liquidctl, OpenRGB).",
            lmc_paths: &["drivers/hwmon", "drivers/leds", "drivers/i2c"],
            ops: |_| vec![atm("I2C_CHARDEV")],
            default_on: |s, p| !s.is_laptop() && p != ProfileId::Server,
        },
        KeepCategory {
            id: "serial_optical",
            icon: "💿",
            title: "Optical drives & USB serial adapters",
            desc: "DVD/Blu-ray drives, Arduino and ESP boards, serial consoles, USB modems.",
            lmc_paths: &["drivers/usb/serial", "drivers/cdrom", "drivers/usb/class"],
            ops: |_| vec![atm("BLK_DEV_SR"), atm("CDROM"), atm("CHR_DEV_SG"), atm("USB_SERIAL"), atm("USB_ACM")],
            default_on: |_, p| p != ProfileId::Server,
        },
        KeepCategory {
            id: "printers",
            icon: "🖶",
            title: "USB printers",
            desc: "Older printers that need the kernel's usblp driver (most use CUPS + libusb).",
            lmc_paths: &["drivers/usb/class"],
            ops: |_| vec![atm("USB_PRINTER")],
            default_on: |_, _| false,
        },
    ]
}

pub fn default_keep(sys: &SystemInfo, profile: ProfileId, mode: SlimMode) -> Vec<String> {
    if mode == SlimMode::Aggressive {
        return vec!["input".into(), "usb_storage".into()];
    }
    categories()
        .into_iter()
        .filter(|c| (c.default_on)(sys, profile))
        .map(|c| c.id.to_string())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hw;

    #[test]
    fn categories_are_unique_and_complete() {
        let cats = categories();
        let mut ids = std::collections::HashSet::new();
        for c in &cats {
            assert!(ids.insert(c.id), "duplicate keep category {}", c.id);
            assert!(!c.lmc_paths.is_empty(), "{} keeps nothing", c.id);
        }
        let sys = hw::sample();
        let keep = default_keep(&sys, ProfileId::Gaming, SlimMode::Balanced);
        for wanted in ["block_dm", "more_fs", "network_extra", "sensors", "serial_optical"] {
            assert!(keep.iter().any(|k| k == wanted), "gaming default lacks {wanted}");
        }
        assert_eq!(default_keep(&sys, ProfileId::Gaming, SlimMode::Aggressive).len(), 2);
    }
}
