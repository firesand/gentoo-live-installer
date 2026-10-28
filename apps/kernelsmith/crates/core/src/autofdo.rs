//! Profile-guided re-optimization (AutoFDO, optionally Propeller).
//!
//! Workflow, following Documentation/dev-tools/autofdo.rst and propeller.rst:
//! 1. build with CONFIG_AUTOFDO_CLANG=y (the "Profile-ready" option),
//! 2. boot it and record branch samples with `perf` during a real workload,
//! 3. convert with `llvm-profgen` (and Propeller's generator),
//! 4. rebuild with CLANG_AUTOFDO_PROFILE / CLANG_PROPELLER_PROFILE_PREFIX.

use crate::hw::{BranchSampling, SystemInfo};
use crate::job::{prepare_simple, PreparedJob};
use crate::library::Manifest;
use crate::paths;
use crate::plan::{BuildPlan, ProfileData};
use crate::util::{gid, now_epoch, shell_quote as q, stamp, uid};
use anyhow::{bail, Result};
use std::path::PathBuf;

/// Sample period suggested by the kernel docs (a prime).
pub const PERIOD: u32 = 500_009;

pub fn perf_event_args(s: BranchSampling) -> Option<&'static str> {
    match s {
        BranchSampling::IntelLbr => Some("-e BR_INST_RETIRED.NEAR_TAKEN:k"),
        BranchSampling::AmdBrs | BranchSampling::AmdLbrV2 => Some("--pfm-events RETIRED_TAKEN_BRANCH_INSTRUCTIONS:k"),
        BranchSampling::None => None,
    }
}

/// Why profiling can't run right now, if it can't.
pub fn blockers(m: &Manifest, sys: &SystemInfo) -> Vec<String> {
    let mut v = vec![];
    if !m.autofdo_ready {
        v.push("This kernel wasn't built with the 'Profile-ready (AutoFDO)' option.".into());
    }
    if sys.running_kernel != m.release {
        v.push(format!("Boot into {} first — the profile must come from the kernel being optimized.", m.release));
    }
    if m.vmlinux().is_none() {
        v.push("The unstripped vmlinux for this kernel is missing (was the build tree cleaned manually?).".into());
    }
    if !sys.toolchain.perf {
        v.push("Install dev-util/perf.".into());
    }
    if !sys.toolchain.llvm_profgen {
        v.push("llvm-profgen was not found (part of llvm-core/llvm).".into());
    }
    if perf_event_args(sys.cpu.branch_sampling).is_none() {
        v.push("This CPU can't record branch samples.".into());
    }
    v
}

/// Root job: record for `minutes`, convert to an AutoFDO profile owned by the user.
pub fn record_job(m: &Manifest, sys: &SystemInfo, minutes: u32, propeller: bool) -> Result<(PreparedJob, PathBuf)> {
    let b = blockers(m, sys);
    if !b.is_empty() {
        bail!(b.join(" "));
    }
    let event = perf_event_args(sys.cpu.branch_sampling).unwrap_or_default();
    let vmlinux = m.vmlinux().unwrap_or_default();
    let out = paths::profiles_dir().join(format!("{}-{}", m.release, stamp(now_epoch())));
    std::fs::create_dir_all(&out)?;
    let afdo = out.join("kernel.afdo");
    let llvm_dir = sys.toolchain.llvm_bin.as_ref().map(|p| p.display().to_string()).unwrap_or_default();
    let propeller_cmd = if propeller && sys.toolchain.propeller_tool {
        String::from(
            r#"stage propeller "Creating the Propeller profile"
if command -v generate_propeller_profiles >/dev/null; then GEN=generate_propeller_profiles; else GEN=create_llvm_prof; fi
"$GEN" --binary="$VMLINUX" --profile="$OUT/perf.data" --format=propeller --propeller_output_module_name \
    --out="$OUT/propeller_cc_profile.txt" --propeller_symorder="$OUT/propeller_ld_profile.txt"
echo "::profile-propeller::$OUT/propeller""#,
        )
    } else {
        String::new()
    };
    let body = format!(
        r#"export PATH={llvm}:$PATH
OUT={out}
VMLINUX={vmlinux}
SECS={secs}
stage record "Recording for {minutes} minute(s) — use your PC as you normally would"
note "Play your games, compile, edit video… whatever you want this kernel to be fastest at."
# On hybrid Intel CPUs the generic event can fail; fall back to the P-core PMU.
perf record {event} -a -N -b -c {period} -o "$OUT/perf.data" -- sleep "$SECS" \
  || perf record -e cpu_core/br_inst_retired.near_taken/k -a -N -b -c {period} -o "$OUT/perf.data" -- sleep "$SECS"
stage convert "Creating the AutoFDO profile"
llvm-profgen --kernel --binary="$VMLINUX" --perfdata="$OUT/perf.data" -o "$OUT/kernel.afdo"
{propeller_cmd}
rm -f "$OUT/perf.data"
chown -R {uid}:{gid} "$OUT"
echo "::profile::$OUT/kernel.afdo""#,
        llvm = q(&llvm_dir),
        out = q(&out.display().to_string()),
        vmlinux = q(&vmlinux.display().to_string()),
        secs = minutes.max(1) * 60,
        period = PERIOD,
        uid = uid(),
        gid = gid(),
    );
    let mut stages = vec![("record", "Recording workload"), ("convert", "Creating profile")];
    if !propeller_cmd.is_empty() {
        stages.push(("propeller", "Creating Propeller profile"));
    }
    let job = prepare_simple(&format!("Profiling {}", m.release), "autofdo", &stages, &body)?;
    Ok((job, afdo))
}

/// Turn the kernel's saved plan into an optimized rebuild using a profile.
pub fn rebuild_plan(m: &Manifest, afdo: PathBuf) -> Result<BuildPlan> {
    let mut plan = BuildPlan::load(&m.plan_file())?;
    let prefix = afdo.parent().map(|d| d.join("propeller"));
    let has_propeller = prefix.as_ref().is_some_and(|p| PathBuf::from(format!("{}_cc_profile.txt", p.display())).exists());
    plan.profile_data = Some(ProfileData { afdo, propeller_prefix: if has_propeller { prefix } else { None } });
    if !plan.name.ends_with("-fdo") {
        plan.name = format!("{}-fdo", plan.name);
    }
    // The original source tree is untouched, so rebuild from it.
    if !m.source.as_os_str().is_empty() {
        plan.source = m.source.clone();
    }
    Ok(plan)
}

/// Previously recorded profiles for a release, newest first.
pub fn profiles_for(release: &str) -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = std::fs::read_dir(paths::profiles_dir())
        .into_iter()
        .flatten()
        .flatten()
        .filter(|e| e.file_name().to_string_lossy().starts_with(release))
        .map(|e| e.path().join("kernel.afdo"))
        .filter(|p| p.is_file())
        .collect();
    v.sort();
    v.reverse();
    v
}
