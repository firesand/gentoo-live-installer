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
            lmc_paths: &["sound/usb", "sound/core"],
            ops: |_| vec![atm("SND_USB_AUDIO"), atm("SND_SEQ"), atm("SND_RAWMIDI"), atm("SND_HRTIMER")],
            default_on: |_, p| p != ProfileId::Server,
        },
        KeepCategory {
            id: "webcam",
            icon: "📷",
            title: "Webcams & capture cards",
            desc: "USB webcams (UVC) and video capture devices.",
            lmc_paths: &["drivers/media/usb/uvc", "drivers/media/v4l2-core", "drivers/media/mc", "drivers/media/common"],
            ops: |_| vec![atm("MEDIA_SUPPORT"), y("MEDIA_USB_SUPPORT"), y("MEDIA_CAMERA_SUPPORT"), atm("USB_VIDEO_CLASS")],
            default_on: |_, p| p != ProfileId::Server,
        },
        KeepCategory {
            id: "network_extra",
            icon: "🔐",
            title: "VPN & virtual networking",
            desc: "WireGuard, OpenVPN (TUN), bridges — used by VPN apps and Steam Link.",
            lmc_paths: &["drivers/net/wireguard", "net/bridge"],
            ops: |_| vec![atm("WIREGUARD"), atm("TUN"), atm("BRIDGE"), atm("VETH")],
            default_on: |_, _| true,
        },
        KeepCategory {
            id: "virtualization",
            icon: "💻",
            title: "Virtual machines & containers",
            desc: "KVM/QEMU, VFIO passthrough, Docker, Podman, Flatpak sandboxes.",
            lmc_paths: &[
                "arch/x86/kvm", "virt", "drivers/vfio", "drivers/vhost", "net/netfilter",
                "net/ipv4/netfilter", "net/ipv6/netfilter", "fs/overlayfs", "net/bridge",
            ],
            ops: |s| {
                let mut v = vec![atm("KVM"), atm("VFIO"), atm("VFIO_PCI"), atm("VHOST_NET"), atm("TUN"), atm("BRIDGE"), atm("VETH"), atm("OVERLAY_FS")];
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
            ops: |_| vec![atm("USB4"), atm("TYPEC"), atm("USB_RTL8152"), atm("USB_NET_AX88179_178A"), atm("USB_NET_CDC_NCM")],
            default_on: |s, p| s.is_laptop() || p == ProfileId::Laptop,
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
