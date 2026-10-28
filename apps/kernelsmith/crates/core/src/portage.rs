//! "Portage mode": instead of building a kernel by hand, hand the user's
//! choices to a Portage distribution kernel (gentoo-kernel, cachyos-kernel,
//! vanilla-kernel). kernel-build.eclass merges /etc/kernel/config.d/*.config
//! last, and the ebuilds apply /etc/portage/patches, so every `emerge -u`
//! rebuilds the kernel with the same customizations — zero maintenance.

use crate::hw::SystemInfo;
use crate::job::{prepare_simple, PreparedJob};
use crate::kconfig::SymbolIndex;
use crate::plan::BuildPlan;
use crate::sources::KernelSource;
use crate::tweaks::{self, Op};
use crate::util::{shell_quote as q, slug};
use anyhow::Result;

pub const DIST_KERNELS: [(&str, &str); 3] = [
    ("sys-kernel/gentoo-kernel", "Gentoo's distribution kernel (::gentoo)"),
    ("sys-kernel/cachyos-kernel", "CachyOS distribution kernel (CachyOS-kernels overlay)"),
    ("sys-kernel/vanilla-kernel", "Unpatched upstream distribution kernel (::gentoo)"),
];

pub struct Export {
    pub package: String,
    pub fragment_path: String,
    pub fragment: String,
    pub package_use: Option<String>,
    pub patch_dir: String,
}

/// Config fragment with the user's tweaks (requirements are already covered
/// by a full distribution config; hardware slimming doesn't apply here).
pub fn fragment(plan: &BuildPlan, sys: &SystemInfo, symbols: Option<&SymbolIndex>) -> String {
    let provided = plan.provided_symbols();
    let mut out = format!(
        "# Kernelsmith customizations: \"{}\" ({:?} profile)\n# Merged by kernel-build.eclass after the distribution config.\n",
        plan.name, plan.profile
    );
    for (title, ops) in tweaks::ops_for(&plan.tweaks, sys, symbols, &provided) {
        out.push_str(&format!("\n# {title}\n"));
        for op in ops {
            let line = match op {
                Op::Y(s) => format!("CONFIG_{s}=y"),
                Op::M(s) | Op::AtLeastM(s) => format!("CONFIG_{s}=m"),
                Op::N(s) => format!("# CONFIG_{s} is not set"),
                Op::Str(s, v) => format!("CONFIG_{s}=\"{v}\""),
                Op::Val(s, v) => format!("CONFIG_{s}={v}"),
            };
            out.push_str(&line);
            out.push('\n');
        }
    }
    out
}

/// USE flags for sys-kernel/cachyos-kernel matching the plan. Every flag of
/// each exclusive group is listed so REQUIRED_USE is always satisfied.
pub fn cachyos_use(plan: &BuildPlan) -> String {
    let sel = |k: &str| plan.tweaks.get(k).map(String::as_str).unwrap_or("");
    let mut flags: Vec<String> = vec![];
    let mut group = |chosen: &str, all: &[&str]| {
        for f in all {
            flags.push(if *f == chosen { f.to_string() } else { format!("-{f}") });
        }
    };
    let rt = sel("preempt") == "rt";
    let sched = match (sel("scheduler"), rt) {
        ("bore", true) => "rt-bore",
        (_, true) => "rt",
        ("bmq", _) => "bmq",
        ("pds", _) => "pds",
        ("eevdf", _) => "eevdf",
        _ => "bore",
    };
    group(sched, &["bore", "bmq", "pds", "muqss", "rt", "rt-bore", "eevdf"]);
    let lto = match sel("lto") {
        "thin" => "llvm-lto-thin",
        "full" => "llvm-lto-full",
        _ => "llvm-lto-none",
    };
    group(lto, &["llvm-lto-thin", "llvm-lto-full", "llvm-lto-thin-dist", "llvm-lto-none"]);
    let hz = format!("hz_ticks_{}", sel("hz"));
    group(&hz, &["hz_ticks_100", "hz_ticks_250", "hz_ticks_300", "hz_ticks_500", "hz_ticks_600", "hz_ticks_750", "hz_ticks_1000"]);
    let tick = format!("tickrate_{}", sel("tick"));
    group(&tick, &["tickrate_periodic", "tickrate_idle", "tickrate_full"]);
    group(if sel("preempt") == "lazy" { "preempt_lazy" } else { "preempt_full" }, &["preempt_full", "preempt_lazy"]);
    let opt = match sel("optimize") {
        "o3" => "o3",
        "os" => "os",
        _ => "",
    };
    group(opt, &["o3", "os"]);
    group(if sel("thp") == "always" { "hugepage_always" } else { "hugepage_madvise" }, &["hugepage_always", "hugepage_madvise"]);
    let march = match sel("march") {
        "native" => "mnative",
        "v3" => "mgeneric_v3",
        _ => "mgeneric",
    };
    group(march, &["mgeneric", "mgeneric_v1", "mgeneric_v2", "mgeneric_v3", "mgeneric_v4", "mnative", "mzen4"]);
    flags.push(if sel("compiler") == "clang" { "clang".into() } else { "-clang".into() });
    flags.push(if sel("governor") == "performance" { "per-gov".into() } else { "-per-gov".into() });
    flags.push(if sel("tcp") == "bbr3" { "bbr3".into() } else { "-bbr3".into() });
    let fdo = sel("autofdo");
    flags.push(if fdo != "off" { "autofdo".into() } else { "-autofdo".into() });
    flags.push(if fdo == "propeller" { "propeller".into() } else { "-propeller".into() });
    format!("sys-kernel/cachyos-kernel {}", flags.join(" "))
}

pub fn export(plan: &BuildPlan, sys: &SystemInfo, src: Option<&KernelSource>, symbols: Option<&SymbolIndex>, package: &str) -> Export {
    let _ = src;
    let name = slug(&plan.name);
    let short = package.rsplit('/').next().unwrap_or(package);
    Export {
        package: package.to_string(),
        fragment_path: format!("/etc/kernel/config.d/60-kernelsmith-{name}.config"),
        fragment: fragment(plan, sys, symbols),
        package_use: (package == "sys-kernel/cachyos-kernel").then(|| cachyos_use(plan)),
        patch_dir: format!("/etc/portage/patches/sys-kernel/{short}"),
    }
}

/// Root job that installs the fragment, USE flags and patches, records them for
/// removal, and optionally rebuilds the package right away.
pub fn install_job(plan: &BuildPlan, ex: &Export, rebuild: bool) -> Result<PreparedJob> {
    let name = slug(&plan.name);
    let state = format!("/var/lib/kernelsmith/portage/{name}.list");
    let mut body = format!(
        "STATE={state}\nmkdir -p /etc/kernel/config.d /var/lib/kernelsmith/portage\n: > \"$STATE\"\n\
         stage files \"Writing Portage configuration\"\n\
         cat > {frag} <<'KS_EOF'\n{fragment}KS_EOF\n\
         echo {frag} >> \"$STATE\"\n\
         note \"Wrote {frag_plain}\"\n",
        state = q(&state),
        frag = q(&ex.fragment_path),
        frag_plain = ex.fragment_path,
        fragment = ex.fragment,
    );
    if let Some(use_line) = &ex.package_use {
        body.push_str(&format!(
            r#"if [[ -d /etc/portage/package.use || ! -e /etc/portage/package.use ]]; then
    mkdir -p /etc/portage/package.use
    echo {line} > /etc/portage/package.use/kernelsmith-{name}
    echo /etc/portage/package.use/kernelsmith-{name} >> "$STATE"
else
    printf '# >>> kernelsmith {name}\n%s\n# <<< kernelsmith {name}\n' {line} >> /etc/portage/package.use
    echo "block:/etc/portage/package.use:{name}" >> "$STATE"
fi
note "Set USE flags for {pkg}"
"#,
            line = q(use_line),
            name = name,
            pkg = ex.package,
        ));
    }
    if !plan.patches.is_empty() {
        body.push_str(&format!("mkdir -p {dir}\n", dir = q(&ex.patch_dir)));
        for p in &plan.patches {
            let dest = format!("{}/kernelsmith-{}", ex.patch_dir, p.file.file_name().unwrap_or_default().to_string_lossy());
            body.push_str(&format!(
                "cp {src} {dest}\necho {dest} >> \"$STATE\"\nnote {msg}\n",
                src = q(&p.file.display().to_string()),
                dest = q(&dest),
                msg = q(&format!("Added patch {}", p.title)),
            ));
        }
    }
    let mut stages = vec![("files", "Writing Portage configuration")];
    if rebuild {
        stages.push(("emerge", "Rebuilding the kernel with Portage"));
        body.push_str(&format!(
            "stage emerge \"Rebuilding {pkg} (this takes a while)\"\nemerge --ask=n --oneshot --quiet-build=y --color=n --nospinner {pkg}\n",
            pkg = q(&ex.package)
        ));
    }
    prepare_simple(&format!("Portage integration for {}", ex.package), &format!("portage-{name}"), &stages, &body)
}

/// Saved Portage integrations: (name, files).
pub fn integrations() -> Vec<(String, Vec<String>)> {
    let mut out = vec![];
    for e in std::fs::read_dir(crate::paths::portage_manifests()).into_iter().flatten().flatten() {
        let name = e.file_name().to_string_lossy().trim_end_matches(".list").to_string();
        let files = std::fs::read_to_string(e.path())
            .unwrap_or_default()
            .lines()
            .map(String::from)
            .filter(|l| !l.is_empty())
            .collect();
        out.push((name, files));
    }
    out
}

pub fn remove_job(name: &str) -> Result<PreparedJob> {
    let state = format!("/var/lib/kernelsmith/portage/{name}.list");
    let body = format!(
        r#"STATE={state}
stage remove "Removing Portage integration"
[[ -f $STATE ]] || fail "Nothing recorded for {name}"
while read -r entry; do
    case "$entry" in
        block:*)
            file=$(echo "$entry" | cut -d: -f2)
            sed -i '/^# >>> kernelsmith {name}$/,/^# <<< kernelsmith {name}$/d' "$file"
            note "Cleaned $file" ;;
        /etc/*)
            rm -f -- "$entry"
            note "Removed $entry" ;;
    esac
done < "$STATE"
rm -f "$STATE"
note "Done. The next emerge of your kernel package will build it without these changes.""#,
        state = q(&state),
        name = name,
    );
    prepare_simple(&format!("Remove Portage integration {name}"), &format!("portage-remove-{name}"), &[("remove", "Removing")], &body)
}
