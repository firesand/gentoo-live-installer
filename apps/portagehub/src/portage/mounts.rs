//! Boot partitions (/boot, /boot/efi, /efi): are they mounted, and writable?
//! Kernel and bootloader updates write there, so a read-only ESP (common for
//! safety) must be remounted read-write first.

use portagehub::protocol::BOOT_MOUNTPOINTS;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct BootMount {
    pub target: String,
    pub device: String,
    pub fstype: String,
    pub in_fstab: bool,
    /// fstab asks for read-only (so the app restores ro afterwards).
    pub fstab_ro: bool,
    pub mounted: bool,
    pub ro: bool,
}

impl BootMount {
    /// Needs action before something can write to it.
    pub fn blocks_writes(&self) -> bool {
        (self.in_fstab && !self.mounted) || (self.mounted && self.ro)
    }

    pub fn state_text(&self) -> String {
        match (self.mounted, self.ro) {
            (false, _) => "not mounted".into(),
            (true, true) => "mounted read-only".into(),
            (true, false) => "mounted read-write".into(),
        }
    }
}

fn opts_ro(opts: &str) -> bool {
    opts.split(',').any(|o| o == "ro")
}

/// Unescape octal sequences in /proc/mounts fields (e.g. `\040` for space).
fn unescape(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'\\' && i + 4 <= b.len() && b[i + 1..i + 4].iter().all(|c| (b'0'..=b'7').contains(c)) {
            out.push(u8::from_str_radix(&s[i + 1..i + 4], 8).unwrap_or(b'?'));
            i += 4;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

pub fn parse(proc_mounts: &str, fstab: &str) -> Vec<BootMount> {
    let mut out: Vec<BootMount> = Vec::new();
    for target in BOOT_MOUNTPOINTS {
        let mut m = BootMount { target: target.to_string(), ..Default::default() };
        for line in fstab.lines() {
            let t = line.trim();
            if t.is_empty() || t.starts_with('#') {
                continue;
            }
            let f: Vec<&str> = t.split_whitespace().collect();
            if f.len() >= 4 && unescape(f[1]) == *target {
                m.in_fstab = true;
                m.device = f[0].to_string();
                m.fstype = f[2].to_string();
                m.fstab_ro = opts_ro(f[3]);
            }
        }
        // The last mount on a target is the visible one.
        for line in proc_mounts.lines() {
            let f: Vec<&str> = line.split_whitespace().collect();
            if f.len() >= 4 && unescape(f[1]) == *target {
                m.mounted = true;
                m.device = f[0].to_string();
                m.fstype = f[2].to_string();
                m.ro = opts_ro(f[3]);
            }
        }
        if m.in_fstab || m.mounted {
            out.push(m);
        }
    }
    out
}

pub fn load() -> Vec<BootMount> {
    parse(&std::fs::read_to_string("/proc/mounts").unwrap_or_default(), &std::fs::read_to_string("/etc/fstab").unwrap_or_default())
}

/// Boot mount points that fstab lists but nothing has mounted (a `noauto` /boot or ESP).
pub fn unmounted(mounts: &[BootMount]) -> Vec<String> {
    mounts.iter().filter(|m| m.in_fstab && !m.mounted).map(|m| m.target.clone()).collect()
}

/// Does installing/removing this package write to the boot partitions?
///
/// linux-firmware counts: with USE=initramfs (the installer's default) every update rebuilds the
/// initramfs and writes /boot, and without it a firmware update is rare enough that remounting
/// for it costs nothing. linux-headers only installs under /usr.
pub fn touches_boot(key: &str) -> bool {
    (key.starts_with("sys-kernel/") && key != "sys-kernel/linux-headers")
        || key.starts_with("sys-boot/")
        || matches!(key, "virtual/dist-kernel" | "sys-firmware/intel-microcode")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ro_esp() {
        let fstab = "UUID=x / btrfs rw 0 0\nUUID=C368-BF96 /boot/efi vfat ro,noatime,umask=0077 0 2\n";
        let mounts = "/dev/nvme0n1p2 / btrfs rw 0 0\n/dev/nvme0n1p1 /boot/efi vfat ro,noatime,fmask=0077 0 0\n";
        let m = parse(mounts, fstab);
        assert_eq!(m.len(), 1);
        assert_eq!(m[0].target, "/boot/efi");
        assert!(m[0].ro && m[0].mounted && m[0].fstab_ro && m[0].blocks_writes());
        let m = parse(&mounts.replace("vfat ro,", "vfat rw,"), fstab);
        assert!(!m[0].blocks_writes());
        // In fstab but not mounted (noauto /boot)
        let m = parse("", "/dev/sda1 /boot ext4 noauto,rw 0 2\n");
        assert!(m[0].blocks_writes() && !m[0].mounted);
        assert!(touches_boot("sys-kernel/gentoo-kernel"));
        assert!(touches_boot("sys-boot/grub"));
        assert!(!touches_boot("sys-kernel/linux-headers"));
        assert!(!touches_boot("app-misc/foo"));
    }

    #[test]
    fn firmware_and_microcode_write_to_boot() {
        // Every linux-firmware update with USE=initramfs reinstalls the initramfs into /boot.
        assert!(touches_boot("sys-kernel/linux-firmware"));
        assert!(touches_boot("sys-firmware/intel-microcode"));
        assert!(touches_boot("sys-kernel/installkernel"));
        assert!(touches_boot("virtual/dist-kernel"));
        // sys-firmware/amd-ucode is not in the tree; it is no longer special.
        assert!(!touches_boot("sys-firmware/amd-ucode"));
        assert!(!touches_boot("sys-kernel/linux-headers"));
    }

    #[test]
    fn unmounted_lists_noauto_partitions_only() {
        let fstab = "/dev/sda1 /boot ext4 noauto,rw 0 2\nUUID=x /boot/efi vfat rw 0 2\n";
        let mounts = "/dev/sdb2 /boot/efi vfat rw 0 0\n";
        let m = parse(mounts, fstab);
        assert_eq!(unmounted(&m), vec!["/boot".to_string()]);
        assert!(unmounted(&parse("/dev/sda1 /boot ext4 rw 0 0\n", fstab)).is_empty() || unmounted(&parse("/dev/sda1 /boot ext4 rw 0 0\n", fstab)) == vec!["/boot/efi".to_string()]);
        assert!(unmounted(&parse("", "")).is_empty());
    }
}
