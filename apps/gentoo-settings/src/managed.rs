//! The files Gentoo Settings owns and the marker blocks inside them.
//!
//! Each tweak contributes one block per file it needs:
//!
//! ```text
//! # >>> gentoo-settings vm.swappiness = 100
//! vm.swappiness = 100
//! # <<< gentoo-settings vm.swappiness
//! ```
//!
//! Files are rebuilt from their blocks (in catalog order) whenever a tweak
//! changes; text outside the blocks is kept, so a hand-added line survives.
//! A file left with no blocks and no foreign text is deleted. Only the paths in
//! [`FILES`] are ever written — the helper refuses anything else.

pub const BEGIN: &str = "# >>> gentoo-settings ";
pub const END: &str = "# <<< gentoo-settings ";
pub const HEADER: &[&str] = &[
    "# Managed by Gentoo Settings (gentoo-settings). Blocks between the",
    "# \">>> gentoo-settings\" markers are rewritten when tweaks change;",
    "# lines outside them are kept.",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FileSpec {
    pub path: &'static str,
    pub mode: u32,
    /// First line of scripts.
    pub shebang: Option<&'static str>,
}

const fn conf(path: &'static str) -> FileSpec {
    FileSpec { path, mode: 0o644, shebang: None }
}

const fn script(path: &'static str, shebang: &'static str) -> FileSpec {
    FileSpec { path, mode: 0o755, shebang: Some(shebang) }
}

pub const SYSCTL: &str = "/etc/sysctl.d/70-gentoo-settings.conf";
pub const MODPROBE: &str = "/etc/modprobe.d/gentoo-settings.conf";
pub const MODULES_LOAD: &str = "/etc/modules-load.d/gentoo-settings.conf";
pub const TMPFILES: &str = "/etc/tmpfiles.d/gentoo-settings.conf";
pub const LIMITS: &str = "/etc/security/limits.d/20-gentoo-settings.conf";
pub const UDEV_AUDIO_PM: &str = "/etc/udev/rules.d/20-gentoo-settings-audio-pm.rules";
pub const UDEV_ZRAM: &str = "/etc/udev/rules.d/30-gentoo-settings-zram.rules";
pub const UDEV_HPET: &str = "/etc/udev/rules.d/40-gentoo-settings-hpet-permissions.rules";
pub const UDEV_SATA: &str = "/etc/udev/rules.d/50-gentoo-settings-sata.rules";
pub const UDEV_IOSCHED: &str = "/etc/udev/rules.d/60-gentoo-settings-ioschedulers.rules";
pub const UDEV_HDPARM: &str = "/etc/udev/rules.d/69-gentoo-settings-hdparm.rules";
pub const UDEV_NVIDIA: &str = "/etc/udev/rules.d/71-gentoo-settings-nvidia.rules";
pub const UDEV_REGDOM: &str = "/etc/udev/rules.d/85-gentoo-settings-iw-regulatory.rules";
pub const UDEV_DMA: &str = "/etc/udev/rules.d/99-gentoo-settings-cpu-dma-latency.rules";
pub const LIBEXEC: &str = "/usr/local/libexec/gentoo-settings";
pub const AUDIO_PM_CTL: &str = "/usr/local/libexec/gentoo-settings/audio-pm-ctl";
pub const PCI_LATENCY: &str = "/usr/local/libexec/gentoo-settings/pci-latency";
pub const IW_REGDOMAIN: &str = "/usr/local/libexec/gentoo-settings/iw-set-regdomain";
pub const GAME_PERFORMANCE: &str = "/usr/local/bin/game-performance";
pub const ZINK_RUN: &str = "/usr/local/bin/zink-run";
pub const DLSS_SWAPPER: &str = "/usr/local/bin/dlss-swapper";
// OpenRC
pub const OPENRC_ZRAM: &str = "/etc/init.d/gentoo-settings-zram";
pub const LOCALD_PCI_LATENCY: &str = "/etc/local.d/gentoo-settings-pci-latency.start";
// systemd
pub const ZRAM_GENERATOR: &str = "/etc/systemd/zram-generator.conf.d/gentoo-settings.conf";
pub const UNIT_PCI_LATENCY: &str = "/etc/systemd/system/gentoo-settings-pci-latency.service";
pub const SYSTEM_CONF: &str = "/etc/systemd/system.conf.d/gentoo-settings.conf";
pub const USER_CONF: &str = "/etc/systemd/user.conf.d/gentoo-settings.conf";
pub const JOURNALD: &str = "/etc/systemd/journald.conf.d/gentoo-settings.conf";
pub const TIMESYNCD: &str = "/etc/systemd/timesyncd.conf.d/gentoo-settings.conf";
pub const OOMD_SYSTEM: &str = "/etc/systemd/system/system.slice.d/gentoo-settings-oomd.conf";
pub const OOMD_USER: &str = "/etc/systemd/user/slice.d/gentoo-settings-oomd.conf";
pub const OOMD_SESSION: &str = "/etc/systemd/user/session.slice.d/gentoo-settings-oomd.conf";
pub const DELEGATE: &str = "/etc/systemd/system/user@.service.d/gentoo-settings-delegate.conf";
pub const RTKIT: &str = "/etc/systemd/system/rtkit-daemon.service.d/gentoo-settings.conf";
pub const NM_DNS: &str = "/etc/NetworkManager/conf.d/gentoo-settings-dns.conf";

pub const FILES: &[FileSpec] = &[
    conf(SYSCTL),
    conf(MODPROBE),
    conf(MODULES_LOAD),
    conf(TMPFILES),
    conf(LIMITS),
    conf(UDEV_AUDIO_PM),
    conf(UDEV_ZRAM),
    conf(UDEV_HPET),
    conf(UDEV_SATA),
    conf(UDEV_IOSCHED),
    conf(UDEV_HDPARM),
    conf(UDEV_NVIDIA),
    conf(UDEV_REGDOM),
    conf(UDEV_DMA),
    script(AUDIO_PM_CTL, "#!/bin/sh"),
    script(PCI_LATENCY, "#!/bin/sh"),
    script(IW_REGDOMAIN, "#!/bin/sh"),
    script(GAME_PERFORMANCE, "#!/bin/sh"),
    script(ZINK_RUN, "#!/bin/sh"),
    script(DLSS_SWAPPER, "#!/bin/sh"),
    script(OPENRC_ZRAM, "#!/sbin/openrc-run"),
    script(LOCALD_PCI_LATENCY, "#!/bin/sh"),
    conf(ZRAM_GENERATOR),
    conf(UNIT_PCI_LATENCY),
    conf(SYSTEM_CONF),
    conf(USER_CONF),
    conf(JOURNALD),
    conf(TIMESYNCD),
    conf(OOMD_SYSTEM),
    conf(OOMD_USER),
    conf(OOMD_SESSION),
    conf(DELEGATE),
    conf(RTKIT),
    conf(NM_DNS),
];

pub fn spec(path: &str) -> Option<&'static FileSpec> {
    FILES.iter().find(|f| f.path == path)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Block {
    pub id: String,
    /// Parameter value the block was written with.
    pub value: Option<String>,
    pub body: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Parsed {
    pub blocks: Vec<Block>,
    /// Text outside our header and blocks, kept on rewrite.
    pub foreign: String,
}

fn begin_line(id: &str, value: Option<&str>) -> String {
    match value {
        Some(v) => format!("{BEGIN}{id} = {v}"),
        None => format!("{BEGIN}{id}"),
    }
}

pub fn parse(text: &str) -> Parsed {
    let mut out = Parsed::default();
    let mut foreign: Vec<String> = Vec::new();
    let mut cur: Option<(String, Option<String>, Vec<&str>)> = None;
    for (i, line) in text.lines().enumerate() {
        if let Some((id, value, body)) = &mut cur {
            if line.trim_end() == format!("{END}{id}") {
                out.blocks.push(Block { id: id.clone(), value: value.take(), body: body.join("\n") });
                cur = None;
            } else {
                body.push(line);
            }
            continue;
        }
        if i == 0 && line.starts_with("#!") {
            continue;
        }
        if HEADER.contains(&line.trim_end()) {
            continue;
        }
        if let Some(rest) = line.trim_end().strip_prefix(BEGIN) {
            let (id, value) = match rest.split_once(" = ") {
                Some((id, v)) => (id.trim().to_string(), Some(v.trim().to_string())),
                None => (rest.trim().to_string(), None),
            };
            cur = Some((id, value, Vec::new()));
            continue;
        }
        foreign.push(line.to_string());
    }
    // An unterminated block (hand-edited file): keep its text as foreign.
    if let Some((id, value, body)) = cur {
        foreign.push(String::new());
        // Defused so it doesn't swallow the blocks we write after it next time.
        foreign.push(format!("# (unterminated) {}", begin_line(&id, value.as_deref()).trim_start_matches("# ")));
        foreign.extend(body.into_iter().map(String::from));
    }
    out.foreign = foreign.join("\n").trim().to_string();
    out
}

/// Rebuild a file from its foreign text and blocks; `None` means delete it.
pub fn compose(spec: &FileSpec, foreign: &str, blocks: &[Block]) -> Option<String> {
    if blocks.is_empty() && foreign.trim().is_empty() {
        return None;
    }
    let mut s = String::new();
    if let Some(sb) = spec.shebang {
        s.push_str(sb);
        s.push('\n');
    }
    for h in HEADER {
        s.push_str(h);
        s.push('\n');
    }
    if !foreign.trim().is_empty() {
        s.push('\n');
        s.push_str(foreign.trim());
        s.push('\n');
    }
    for b in blocks {
        s.push('\n');
        s.push_str(&begin_line(&b.id, b.value.as_deref()));
        s.push('\n');
        let body = b.body.trim_end_matches('\n');
        if !body.is_empty() {
            s.push_str(body);
            s.push('\n');
        }
        s.push_str(END);
        s.push_str(&b.id);
        s.push('\n');
    }
    Some(s)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_keeps_foreign_text() {
        let spec = spec(SYSCTL).unwrap();
        let blocks = vec![
            Block { id: "vm.swappiness".into(), value: Some("100".into()), body: "vm.swappiness = 100".into() },
            Block { id: "kernel.sysrq".into(), value: None, body: "# comment\nkernel.sysrq = 1".into() },
        ];
        let text = compose(spec, "net.core.somaxconn = 4096", &blocks).unwrap();
        let p = parse(&text);
        assert_eq!(p.blocks, blocks);
        assert_eq!(p.foreign, "net.core.somaxconn = 4096");
        assert_eq!(compose(spec, &p.foreign, &p.blocks).unwrap(), text);
    }

    #[test]
    fn scripts_keep_their_shebang_first() {
        let spec = spec(OPENRC_ZRAM).unwrap();
        let text = compose(spec, "", &[Block { id: "zram".into(), value: Some("zstd".into()), body: "start() { :; }".into() }]).unwrap();
        assert!(text.starts_with("#!/sbin/openrc-run\n# Managed by"));
        let p = parse(&text);
        assert_eq!(p.foreign, "");
        assert_eq!(p.blocks.len(), 1);
    }

    #[test]
    fn empty_file_is_deleted() {
        assert_eq!(compose(spec(SYSCTL).unwrap(), "  \n", &[]), None);
    }

    #[test]
    fn unterminated_block_becomes_foreign() {
        let p = parse("# >>> gentoo-settings x\nfoo = 1\n");
        assert!(p.blocks.is_empty());
        assert!(p.foreign.contains("foo = 1"));
    }

    #[test]
    fn file_paths_are_unique_and_absolute() {
        let mut seen = std::collections::HashSet::new();
        for f in FILES {
            assert!(f.path.starts_with('/'), "{}", f.path);
            assert!(seen.insert(f.path), "duplicate {}", f.path);
        }
    }
}
