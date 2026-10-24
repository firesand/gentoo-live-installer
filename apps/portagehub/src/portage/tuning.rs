//! What the machine and toolchain look like, for make.conf suggestions:
//! threads and RAM (MAKEOPTS), GPUs (VIDEO_CARDS), what -march=native
//! resolves to (gcc), and the CPU_FLAGS_X86 that cpuid2cpuflags reports.

use std::path::Path;
use std::process::Command;

#[derive(Debug, Clone, Default)]
pub struct Hardware {
    pub threads: usize,
    pub mem_kib: u64,
    pub cpu_model: String,
    /// GPU vendors found on PCI: "nvidia", "amd", "intel".
    pub gpus: Vec<&'static str>,
    pub efi: bool,
}

impl Hardware {
    pub fn mem_gib(&self) -> f64 {
        self.mem_kib as f64 / 1024.0 / 1024.0
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct MarchInfo {
    pub march: String,
    pub mtune: String,
    pub avx512: bool,
}

#[derive(Debug, Clone, Default)]
pub struct Tuning {
    pub hw: Hardware,
    pub gcc_version: String,
    /// What -march=native means on this machine (None: no gcc).
    pub native: Option<MarchInfo>,
    /// cpuid2cpuflags output (None: not installed).
    pub cpu_flags: Option<Vec<String>>,
}

/// Gentoo's MAKEOPTS rule: jobs = min(threads, RAM / 2 GiB), load limit
/// slightly above the thread count.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MakeoptsSuggestion {
    pub jobs: usize,
    pub load: usize,
    /// RAM, not the thread count, sets the limit.
    pub ram_bound: bool,
    pub ram_jobs: usize,
}

pub fn suggest_makeopts(threads: usize, mem_kib: u64) -> MakeoptsSuggestion {
    let ram_jobs = ((mem_kib / 1024 / 1024) / 2).max(1) as usize;
    let threads = threads.max(1);
    MakeoptsSuggestion { jobs: threads.min(ram_jobs), load: threads, ram_bound: ram_jobs < threads, ram_jobs }
}

/// Parse `gcc -march=native -Q --help=target`.
pub fn parse_help_target(text: &str) -> Option<MarchInfo> {
    let mut info = MarchInfo::default();
    for line in text.lines() {
        let mut it = line.split_whitespace();
        let (Some(opt), Some(val)) = (it.next(), it.next()) else { continue };
        match opt {
            "-march=" => info.march = val.to_string(),
            "-mtune=" => info.mtune = val.to_string(),
            o if o.starts_with("-mavx512") && val == "[enabled]" => info.avx512 = true,
            _ => {}
        }
    }
    (!info.march.is_empty()).then_some(info)
}

/// Parse cpuid2cpuflags output: `CPU_FLAGS_X86: aes avx …`, or the
/// package.use form newer versions print (`*/* CPU_FLAGS_X86: aes avx …`).
pub fn parse_cpuid2cpuflags(text: &str) -> Option<Vec<String>> {
    let rest = text.lines().find_map(|l| l.split_once("CPU_FLAGS_X86:").map(|(_, r)| r.to_string()))?;
    Some(rest.split_whitespace().map(String::from).collect())
}

fn read(p: &str) -> String {
    std::fs::read_to_string(p).unwrap_or_default()
}

pub fn hardware() -> Hardware {
    let threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1);
    let mem_kib = read("/proc/meminfo")
        .lines()
        .find_map(|l| l.strip_prefix("MemTotal:").and_then(|v| v.split_whitespace().next()).and_then(|v| v.parse().ok()))
        .unwrap_or(0);
    let cpu_model = read("/proc/cpuinfo")
        .lines()
        .find_map(|l| l.strip_prefix("model name").map(|v| v.trim_start_matches([' ', '\t', ':']).trim().to_string()))
        .unwrap_or_default();
    let mut gpus = Vec::new();
    for e in std::fs::read_dir("/sys/bus/pci/devices").into_iter().flatten().flatten() {
        let class = std::fs::read_to_string(e.path().join("class")).unwrap_or_default();
        if !class.trim().starts_with("0x03") {
            continue;
        }
        let vendor = match std::fs::read_to_string(e.path().join("vendor")).unwrap_or_default().trim() {
            "0x10de" => "nvidia",
            "0x1002" => "amd",
            "0x8086" => "intel",
            _ => continue,
        };
        if !gpus.contains(&vendor) {
            gpus.push(vendor);
        }
    }
    Hardware { threads, mem_kib, cpu_model, gpus, efi: Path::new("/sys/firmware/efi").is_dir() }
}

fn output(prog: &str, args: &[&str]) -> Option<String> {
    let o = Command::new(prog).args(args).output().ok()?;
    o.status.success().then(|| String::from_utf8_lossy(&o.stdout).to_string())
}

pub fn load() -> Tuning {
    Tuning {
        hw: hardware(),
        gcc_version: output("gcc", &["-dumpfullversion"]).map(|v| v.trim().to_string()).unwrap_or_default(),
        native: output("gcc", &["-march=native", "-Q", "--help=target"]).and_then(|t| parse_help_target(&t)),
        cpu_flags: output("cpuid2cpuflags", &[]).and_then(|t| parse_cpuid2cpuflags(&t)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn makeopts_rule() {
        // i9-14900K: 32 threads, MemTotal 46.7 GiB -> RAM-bound at 23 jobs.
        let s = suggest_makeopts(32, 48_966_000);
        assert_eq!(s, MakeoptsSuggestion { jobs: 23, load: 32, ram_bound: true, ram_jobs: 23 });
        let s = suggest_makeopts(8, 64 * 1024 * 1024);
        assert_eq!((s.jobs, s.ram_bound), (8, false));
        assert_eq!(suggest_makeopts(4, 1024 * 1024).jobs, 1);
    }

    #[test]
    fn gcc_and_cpuid_output() {
        let t = "The following options are target specific:\n  -m128bit-long-double        \t[enabled]\n  -march=                     \talderlake\n  -mavx512f                   \t[disabled]\n  -mavxvnni                   \t[enabled]\n  -mtune=                     \talderlake\n";
        assert_eq!(parse_help_target(t), Some(MarchInfo { march: "alderlake".into(), mtune: "alderlake".into(), avx512: false }));
        assert!(parse_help_target("  -mavx512f [enabled]\n  -march= znver4\n").unwrap().avx512);
        assert_eq!(parse_cpuid2cpuflags("CPU_FLAGS_X86: aes avx avx2\n").unwrap(), vec!["aes", "avx", "avx2"]);
        assert_eq!(parse_cpuid2cpuflags("*/* CPU_FLAGS_X86: aes sha\n").unwrap(), vec!["aes", "sha"]);
        assert!(parse_cpuid2cpuflags("garbage").is_none());
    }
}
