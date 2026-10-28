//! Turns a build plan into the bash script that runs as root.
//!
//! The script is deliberately plain and commented: users can read it in the
//! Review step, save it, or run it themselves with `sudo bash build.sh`.
//! Progress is reported with `::stage::`, `::note::`, `::warn::`,
//! `::verify::`, `::error::` and `::done::` marker lines that the GUI parses;
//! in a terminal they're simply readable.

use crate::hw::SystemInfo;
use crate::plan::{BuildPlan, Resolved, Seed};
use crate::profiles::{self, ProfileId};
use crate::slim::SlimMode;
use crate::sources::KernelSource;
use crate::paths;
use crate::tuning;
use crate::tweaks::{self, Op};
use crate::util::{format_epoch, now_epoch, shell_quote as q};
use std::fmt::Write as _;
use std::path::Path;

/// Stage id, label, share of total time (for the progress bar).
pub const STAGES: &[(&str, &str, f32)] = &[
    ("preflight", "Checking your system", 0.01),
    ("prepare", "Preparing kernel source", 0.04),
    ("patches", "Applying patches", 0.01),
    ("config", "Configuring", 0.04),
    ("verify", "Verifying configuration", 0.01),
    ("build", "Compiling the kernel", 0.74),
    ("modules", "Installing modules", 0.03),
    ("kernel", "Installing kernel & updating bootloader", 0.04),
    ("modrebuild", "Rebuilding driver modules", 0.05),
    ("tuning", "Applying runtime tuning", 0.005),
    ("clean", "Freeing disk space", 0.015),
];

pub fn stage_label(id: &str) -> &'static str {
    STAGES.iter().find(|s| s.0 == id).map(|s| s.1).unwrap_or("Working")
}

/// Which stages this plan will run, in order.
pub fn stages_for(plan: &BuildPlan) -> Vec<&'static str> {
    let mut v = vec!["preflight", "prepare"];
    if !plan.patches.is_empty() {
        v.push("patches");
    }
    v.extend(["config", "verify", "build"]);
    if plan.install.install {
        v.extend(["modules", "kernel"]);
        if plan.install.module_rebuild {
            v.push("modrebuild");
        }
    }
    if plan.install.runtime_tuning {
        v.push("tuning");
    }
    if plan.install.clean_after {
        v.push("clean");
    }
    v
}

pub struct ScriptInputs<'a> {
    pub plan: &'a BuildPlan,
    pub sys: &'a SystemInfo,
    pub src: &'a KernelSource,
    pub resolved: &'a Resolved,
    /// Directory holding seed.config, modules.lst, patches/, plan.toml.
    pub job_dir: &'a Path,
    /// What the seed really is (see `plan::resolve_seed`).
    pub seed_label: &'a str,
    /// Keep the (untrimmed) seed with the kernel for builds on top of it.
    pub store_seed: bool,
}

fn expect_token(op: &Op) -> String {
    match op {
        Op::Y(_) => "y".into(),
        Op::M(_) | Op::AtLeastM(_) => "ym".into(),
        Op::N(_) => "n".into(),
        Op::Str(_, v) => format!("\"{v}\""),
        Op::Val(_, v) => v.clone(),
    }
}

/// Render `scripts/config` calls for one group of ops.
fn config_calls(ops: &[Op]) -> String {
    let mut args = vec![];
    let mut atleast = vec![];
    for op in ops {
        match op {
            Op::Y(s) => args.push(format!("-e {s}")),
            Op::M(s) => args.push(format!("-m {s}")),
            Op::N(s) => args.push(format!("-d {s}")),
            Op::AtLeastM(s) => atleast.push(s.clone()),
            Op::Str(s, v) => args.push(format!("--set-str {s} {}", q(v))),
            Op::Val(s, v) => args.push(format!("--set-val {s} {}", q(v))),
        }
    }
    let mut out = String::new();
    for chunk in args.chunks(8) {
        let _ = writeln!(out, "    kconf {}", chunk.join(" "));
    }
    for chunk in atleast.chunks(8) {
        let _ = writeln!(out, "    at_least_m {}", chunk.join(" "));
    }
    out
}

pub fn generate(inp: &ScriptInputs) -> String {
    let ScriptInputs { plan, sys, src, resolved, job_dir, seed_label, store_seed } = inp;
    let job = job_dir.display().to_string();
    let profile = profiles::get(plan.profile);
    let clang = plan.tweaks.get("compiler").map(String::as_str) == Some("clang");
    let autofdo_ready = plan.tweaks.get("autofdo").is_some_and(|v| v != "off");
    let signing = plan.tweaks.get("signing").is_some_and(|v| v != "off");
    let anticheat = plan.tweaks.get("anticheat").map(String::as_str) == Some("ready");
    let stages = stages_for(plan);

    let mut path = String::new();
    if let Some(dir) = &sys.toolchain.llvm_bin {
        path.push_str(&dir.display().to_string());
        path.push(':');
    }
    path.push_str("/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin");

    let mut make = String::from(r#"MAKE=(make -C "$TREE" --no-print-directory -j"$JOBS" LOCALVERSION="#);
    if clang {
        make.push_str(" LLVM=1");
    }
    if let Some(pd) = &plan.profile_data {
        let _ = write!(make, " CLANG_AUTOFDO_PROFILE={}", q(&pd.afdo.display().to_string()));
        if let Some(pp) = &pd.propeller_prefix {
            let _ = write!(make, " CLANG_PROPELLER_PROFILE_PREFIX={}", q(&pp.display().to_string()));
        }
    }
    make.push(')');

    let mut s = String::new();
    let _ = writeln!(
        s,
        r#"#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Kernelsmith build script — generated {date} UTC
#
#  Kernel name : {rel}
#  Profile     : {ptitle}
#  Source      : {src}
#  Build tree  : {tree}
#
#  This is exactly what Kernelsmith runs as root. You can keep it and run it
#  yourself later with:   sudo bash {job}/build.sh
#  Your original source tree in /usr/src is never modified.
# ─────────────────────────────────────────────────────────────────────────────
set -Eeuo pipefail
exec 2>&1
umask 022
export LC_ALL=C LANG=C HOME=/root
export PATH={path}

JOB={jobq}
SRC={srcq}
TREE={treeq}
REL={relq}
JOBS={jobs}
STATE=/var/lib/kernelsmith
MANIFEST="$STATE/kernels/$REL.toml"
{make}

stage() {{ echo "::stage::$1::$2"; }}
note()  {{ echo "::note::$*"; }}
warn()  {{ echo "::warn::$*"; }}
fail()  {{ echo "::error::$*"; exit 1; }}
kconf() {{ "$TREE/scripts/config" --file "$TREE/.config" "$@"; }}
at_least_m() {{
    local s
    for s in "$@"; do
        grep -q "^CONFIG_$s=y" "$TREE/.config" || kconf -m "$s"
    done
}}

INSTALL_STARTED=0
TREE_CREATED=0
cleanup() {{
    local rc=$?
    if [[ $rc -ne 0 && $TREE_CREATED -eq 1 && $INSTALL_STARTED -eq 0 ]]; then
        echo "::note::Removing the unfinished build tree $TREE"
        rm -rf --one-file-system "$TREE"
    fi
}}
trap cleanup EXIT
trap 'echo "::error::Command failed (line $LINENO): $BASH_COMMAND"' ERR
trap 'echo "::error::Build cancelled"; exit 130' TERM INT

echo "::plan::{stages}""#,
        date = format_epoch(now_epoch()),
        rel = resolved.release,
        ptitle = profile.title,
        src = src.path.display(),
        tree = resolved.dest.display(),
        job = job,
        path = q(&path),
        jobq = q(&job),
        srcq = q(&src.path.display().to_string()),
        treeq = q(&resolved.dest.display().to_string()),
        relq = q(&resolved.release),
        jobs = plan.jobs.max(1),
        make = make,
        stages = stages.join(","),
    );

    // ── Preflight ──
    let mut tools = vec!["make", "patch", "bc", "flex", "bison", "perl", "cpio", "awk", "sed"];
    if clang {
        tools.extend(["clang", "ld.lld", "llvm-ar", "llvm-nm", "llvm-objcopy"]);
    } else {
        tools.push("gcc");
    }
    if plan.tweaks.get("debug").map(String::as_str) == Some("btf") || plan.tweaks.get("sched_ext").map(String::as_str) == Some("on") {
        tools.push("pahole");
    }
    if signing {
        tools.push("openssl");
    }
    let _ = writeln!(
        s,
        r#"
# ── 1. Preflight ─────────────────────────────────────────────────────────────
stage preflight "Checking your system"
{root_check}
for tool in {tools}; do
    command -v "$tool" >/dev/null || fail "Missing build tool: $tool"
done
[[ -d $SRC ]] || fail "Source tree $SRC not found."
[[ ! -e $TREE ]] || fail "$TREE already exists — pick another kernel name."
[[ ! -e /lib/modules/$REL ]] || fail "A kernel called $REL is already installed."
note "Building $REL with $JOBS parallel jobs""#,
        tools = tools.join(" "),
        root_check = if plan.needs_root() {
            r#"[[ $EUID -eq 0 ]] || fail "This script must run as root (try: sudo bash $0).""#
        } else {
            "# Build-only plan in a user-writable directory: root is not required."
        },
    );

    // ── Prepare ──
    let _ = writeln!(
        s,
        r#"
# ── 2. Prepare an untouched copy of the source ───────────────────────────────
stage prepare "Preparing kernel source"
note "Copying the source to $TREE (instant on btrfs/xfs thanks to reflinks)"
TREE_CREATED=1
cp -a --reflink=auto "$SRC" "$TREE"
mkdir -p "$TREE/.kernelsmith"
cp "$JOB/plan.toml" "$TREE/.kernelsmith/plan.toml"{store_seed}
note "Cleaning the copy (make mrproper)"
"${{MAKE[@]}}" -s mrproper
rm -f "$TREE"/localversion*"#,
        store_seed = if *store_seed {
            "\n# The untrimmed base configuration: a kernel built on top of this one starts from it,\n# not from this kernel's trimmed config.\ncp \"$JOB/seed.config\" \"$TREE/.kernelsmith/seed.config\""
        } else {
            ""
        },
    );

    // ── Patches ──
    if !plan.patches.is_empty() {
        let _ = writeln!(
            s,
            r#"
# ── 3. Patches ───────────────────────────────────────────────────────────────
stage patches "Applying {n} patch(es)"
apply_patch() {{
    note "Patch: $2"
    patch -d "$TREE" -p1 --dry-run --batch -N -s -i "$1" >/dev/null \
        || fail "Patch '$2' does not apply to this kernel version. Remove it and try again."
    patch -d "$TREE" -p1 --batch -N -s -i "$1"
    cp "$1" "$TREE/.kernelsmith/"
}}"#,
            n = plan.patches.len()
        );
        for (i, p) in plan.patches.iter().enumerate() {
            let file = job_dir.join("patches").join(format!("{:02}-{}", i + 1, p.file.file_name().unwrap_or_default().to_string_lossy()));
            let _ = writeln!(s, "apply_patch {} {}", q(&file.display().to_string()), q(&p.title));
        }
    }

    // ── Configure ──
    let seed = match &plan.seed {
        Seed::Defconfig => r#"note "Starting from upstream defaults (make defconfig)"
"${MAKE[@]}" -s defconfig"#
            .to_string(),
        _ => format!(
            "note {}\ncp \"$JOB/seed.config\" \"$TREE/.config\"",
            q(&format!("Starting from: {seed_label}"))
        ),
    };
    let mut apply = String::from("apply_choices() {\n");
    for g in &resolved.groups {
        let _ = writeln!(apply, "    # {}", g.title);
        apply.push_str(&config_calls(&g.ops));
    }
    apply.push_str("}\n");

    let slim = if plan.slim == SlimMode::Off {
        String::from("# Slimming is off: every driver from the base configuration is kept.")
    } else {
        format!(
            r#"note "Keeping only drivers for your hardware (make localmodconfig)"
( yes '' || true ) | "${{MAKE[@]}}" LSMOD="$JOB/modules.lst" LMC_KEEP={keep} localmodconfig \
    > "$TREE/.kernelsmith/localmodconfig.log" 2>&1 \
    || {{ tail -n 20 "$TREE/.kernelsmith/localmodconfig.log"; fail "localmodconfig failed"; }}"#,
            keep = q(&resolved.lmc_keep)
        )
    };

    let signing_setup = if signing {
        let key = if plan.needs_root() { paths::SIGNING_KEY.to_string() } else { format!("{job}/keys/modules-signing.pem") };
        format!(
            r#"
# ── Module signing key (created once, reused by every build) ─────────────────
SIGN_KEY={key}
SIGN_CERT=
# A key already configured for Portage (MODULES_SIGN_KEY in make.conf or an env file)
# wins: one key per machine, or drivers signed for one kernel get refused by the other.
if command -v portageq >/dev/null; then
    PORTAGE_SIGN_KEY=$(portageq envvar MODULES_SIGN_KEY 2>/dev/null || true)
    if [[ -n $PORTAGE_SIGN_KEY && -s $PORTAGE_SIGN_KEY ]]; then
        SIGN_KEY=$PORTAGE_SIGN_KEY
        SIGN_CERT=$(portageq envvar MODULES_SIGN_CERT 2>/dev/null || true)
        note "Using the module signing key Portage already knows: $SIGN_KEY"
    fi
fi
if [[ ! -s $SIGN_KEY ]]; then
    note "Creating the module signing key $SIGN_KEY"
    mkdir -p "$(dirname "$SIGN_KEY")"
    (
        umask 077
        openssl req -new -x509 -newkey rsa:4096 -sha512 -days 36500 -nodes -batch             -subj "/CN=Kernelsmith module signing key ($(cat /proc/sys/kernel/hostname 2>/dev/null || echo pc))/"             -keyout "$SIGN_KEY.tmp" -out "${{SIGN_KEY%.pem}}.crt" >/dev/null 2>&1
        # One PEM with key + certificate: what scripts/sign-file and Portage's MODULES_SIGN_KEY expect.
        cat "${{SIGN_KEY%.pem}}.crt" >> "$SIGN_KEY.tmp"
        mv "$SIGN_KEY.tmp" "$SIGN_KEY"
    )
    # Portage signs rebuilt drivers as the portage user, so the group may read the key.
    if getent group portage >/dev/null; then
        chgrp portage "$SIGN_KEY" "${{SIGN_KEY%.pem}}.crt" && chmod 0640 "$SIGN_KEY" && chmod 0644 "${{SIGN_KEY%.pem}}.crt"
    fi
fi
[[ -n $SIGN_CERT && -s $SIGN_CERT ]] || SIGN_CERT=${{SIGN_KEY%.pem}}.crt
[[ -s $SIGN_CERT ]] || openssl x509 -in "$SIGN_KEY" -out "$SIGN_CERT"
kconf --set-str MODULE_SIG_KEY "$SIGN_KEY"
"${{MAKE[@]}}" -s olddefconfig
SIGN_HASH=$(sed -n 's/^CONFIG_MODULE_SIG_HASH="\(.*\)"/\1/p' "$TREE/.config")
SIGN_HASH=${{SIGN_HASH:-sha512}}"#,
            key = q(&key),
        )
    } else {
        String::new()
    };

    let _ = writeln!(
        s,
        r#"
# ── 4. Configure ─────────────────────────────────────────────────────────────
stage config "Configuring"
{seed}
"${{MAKE[@]}}" -s olddefconfig
{slim}

{apply}
note "Applying your choices"
apply_choices
"${{MAKE[@]}}" -s olddefconfig
# Second pass: some options only become selectable once others are on.
apply_choices
"${{MAKE[@]}}" -s olddefconfig{signing_setup}"#
    );

    // ── Verify ──
    let _ = writeln!(
        s,
        r#"
# ── 5. Verify every requested option actually took effect ────────────────────
stage verify "Verifying configuration"
CRIT_MISS=()
verify() {{
    local got ok=0
    got=$(sed -n "s/^CONFIG_$1=//p" "$TREE/.config" | head -n1 || true)
    [[ -n $got ]] || got=n
    case "$2" in
        ym) [[ $got == y || $got == m ]] && ok=1 ;;
        *)  [[ $got == "$2" ]] && ok=1 ;;
    esac
    if (( ok )); then
        echo "::verify::ok::$1::$2::$got::$4"
    else
        echo "::verify::miss::$1::$2::$got::$4"
        [[ $3 == 1 ]] && CRIT_MISS+=("CONFIG_$1 ($4)")
    fi
    return 0
}}"#
    );
    for (sym, (op, critical, title)) in &resolved.expectations {
        let _ = writeln!(s, "verify {sym} {} {} {}", q(&expect_token(op)), u8::from(*critical), q(title));
    }
    let _ = writeln!(
        s,
        r#"(( ${{#CRIT_MISS[@]}} == 0 )) || fail "Required options could not be enabled: ${{CRIT_MISS[*]}}"
read -r CY CM < <(awk -F= '/^CONFIG_/ {{ if ($2=="y") y++; else if ($2=="m") m++ }} END {{ print y+0, m+0 }}' "$TREE/.config")
echo "::config-stats::$CY::$CM"
# olddefconfig leaves include/config/auto.conf stale; refresh it before asking
# for the release name.
"${{MAKE[@]}}" -s syncconfig
GOT=$("${{MAKE[@]}}" -s kernelrelease)
if [[ $GOT != "$REL" ]]; then
    warn "Kernel release is $GOT (expected $REL)"
    REL=$GOT
    MANIFEST="$STATE/kernels/$REL.toml"
    [[ ! -e /lib/modules/$REL ]] || fail "A kernel called $REL is already installed."
fi
cp "$TREE/.config" "$TREE/.kernelsmith/config""#
    );

    // ── Build ──
    let afdo_check = if plan.profile_data.is_some() {
        r#"
# The profile only helps if the compiler actually used it.
if grep -qs -- '-fprofile-sample-use' "$TREE/init/.main.o.cmd"; then
    note "AutoFDO profile applied to the build"
else
    warn "The AutoFDO profile was NOT applied (no -fprofile-sample-use in the compile commands)"
fi"#
    } else {
        ""
    };
    let _ = writeln!(
        s,
        r#"
# ── 6. Compile ───────────────────────────────────────────────────────────────
stage build "Compiling the kernel"
"${{MAKE[@]}}"
note "Kernel image: $("${{MAKE[@]}}" -s image_name)"{afdo_check}"#
    );

    if plan.install.install {
        let previous_link = if plan.install.set_symlink {
            "PREV_LINK=$(readlink /usr/src/linux 2>/dev/null || true)"
        } else {
            "PREV_LINK="
        };
        let _ = writeln!(
            s,
            r##"
# ── 7. Install ───────────────────────────────────────────────────────────────
{previous_link}
write_manifest() {{
    mkdir -p "$STATE/kernels"
    {{
        echo "# Written by Kernelsmith — used to uninstall this kernel cleanly."
        echo "release = \"$REL\""
        echo "name = {name}"
        echo "profile = {profile}"
        echo "created = $(date +%s)"
        echo "status = \"$1\""
        echo "tree = \"$TREE\""
        echo "source = \"$SRC\""
        echo "modules_dir = \"/lib/modules/$REL\""
        echo "previous_symlink = \"$PREV_LINK\""
        echo "autofdo_ready = {autofdo}"
        echo "autofdo_profile = {afdo_profile}"
        echo "signing_key = {signing_key}"
        echo "files = ["
        if [[ -f $TREE/.kernelsmith/boot.files ]]; then
            while read -r f; do printf '  "%s",\n' "$f"; done < "$TREE/.kernelsmith/boot.files"
        fi
        echo "]"
    }} > "$MANIFEST.tmp"
    mv "$MANIFEST.tmp" "$MANIFEST"
}}
boot_snapshot() {{
    # Only existing dirs (many systems have no /efi), and never let find's exit
    # status abort the install under pipefail.
    local d dirs=()
    for d in /boot /efi /boot/efi; do
        if [[ -d $d ]]; then dirs+=("$d"); fi
    done
    {{ find "${{dirs[@]}}" -xdev \( -type f -o -type l \) 2>/dev/null || true; }} | sort -u
}}

stage modules "Installing modules"
INSTALL_STARTED=1
write_manifest installing
"${{MAKE[@]}}" INSTALL_MOD_STRIP=1 modules_install
{sign_funcs}
stage kernel "Installing kernel & updating bootloader"
boot_snapshot > "$TREE/.kernelsmith/boot.before"
{eac_entry}
# Call Gentoo's installkernel directly (runs dracut/ugrd and updates GRUB or
# systemd-boot per its USE flags); `make install` is only the fallback.
if command -v installkernel >/dev/null; then
    installkernel "$REL" "$TREE/$("${{MAKE[@]}}" -s image_name)" "$TREE/System.map" /boot
else
    "${{MAKE[@]}}" install
fi"##,
            name = q(&format!("\"{}\"", plan.name.replace('"', ""))),
            profile = q(&format!("\"{}\"", profile_slug(plan.profile))),
            autofdo = autofdo_ready,
            afdo_profile = q(&format!("\"{}\"", plan.profile_data.as_ref().map(|p| p.afdo.display().to_string()).unwrap_or_default().replace('"', ""))),
            signing_key = if signing { r#"\"$SIGN_KEY\""# } else { r#""""# },
            sign_funcs = if signing { SIGN_FUNCS } else { "" },
            eac_entry = if anticheat { eac_entry_snippet(sys) } else { String::new() },
        );
        if sys.boot.installkernel.is_none() {
            let _ = writeln!(
                s,
                r#"if [[ -f /boot/grub/grub.cfg ]] && command -v grub-mkconfig >/dev/null; then
    note "Updating GRUB menu"
    grub-mkconfig -o /boot/grub/grub.cfg
else
    warn "installkernel is not installed — add the new kernel to your bootloader manually."
fi"#
            );
        }
        let _ = writeln!(
            s,
            r#"boot_snapshot > "$TREE/.kernelsmith/boot.after"
# Only files that are new AND carry this kernel's name belong to it.
comm -13 "$TREE/.kernelsmith/boot.before" "$TREE/.kernelsmith/boot.after" \
    | grep -F -- "$REL" > "$TREE/.kernelsmith/boot.files" || true
if [[ -f $TREE/.kernelsmith/extra.files ]]; then cat "$TREE/.kernelsmith/extra.files" >> "$TREE/.kernelsmith/boot.files"; fi
note "Installed: $(tr '\n' ' ' < "$TREE/.kernelsmith/boot.files")"
write_manifest installed"#
        );
        if plan.install.set_symlink {
            let _ = writeln!(
                s,
                r#"note "Pointing /usr/src/linux at the new tree"
if command -v eselect >/dev/null; then
    eselect kernel set "$(basename "$TREE")"
else
    ln -sfn "$(basename "$TREE")" /usr/src/linux
fi"#
            );
        }
        if plan.install.module_rebuild {
            let preserve = if plan.install.preserve_old_modules {
                r#"# Portage removes a package's old files when it reinstalls it, which would take
# the NVIDIA (etc.) modules away from your *other* kernels. Save and restore them.
PRESERVE=$(mktemp -d /var/tmp/kernelsmith-preserve.XXXXXX)
for d in /var/db/pkg/*/*; do
    grep -qwE 'linux-mod(-r1)?' "$d/INHERITED" 2>/dev/null || continue
    awk '$1=="obj" && $2 ~ "^(/usr)?/lib/modules/" {print $2}' "$d/CONTENTS" 2>/dev/null || true
done | while read -r f; do
    if [[ -e $f ]]; then cp -a --parents "$f" "$PRESERVE/"; fi
done"#
            } else {
                "PRESERVE="
            };
            let portage_sign = if signing { portage_sign_snippet(sys) } else { String::new() };
            let _ = writeln!(
                s,
                r#"
stage modrebuild "Rebuilding driver modules"
{preserve}
{portage_sign}
MODREBUILD_OK=1
note "Running: emerge @module-rebuild for the new kernel"
if ! KERNEL_DIR="$TREE" emerge --ask=n --oneshot --quiet-build=y --color=n --nospinner @module-rebuild; then
    MODREBUILD_OK=0
    warn "Some driver modules failed to rebuild. The kernel is installed; fix the error above and run: KERNEL_DIR=$TREE emerge @module-rebuild"
fi
{sign_call}
if [[ -n $PRESERVE && -d $PRESERVE ]]; then
    restored=0
    while read -r f; do
        dest="/${{f#./}}"
        if [[ ! -e $dest ]]; then
            mkdir -p "$(dirname "$dest")"
            cp -a "$PRESERVE/$f" "$dest"
            restored=$((restored + 1))
        fi
    done < <(cd "$PRESERVE" && find . -type f)
    if (( restored )); then
        for k in $(cd "$PRESERVE" && find . -type f | sed -nE 's#^\./(usr/)?lib/modules/([^/]+)/.*#\2#p' | sort -u); do
            if [[ $k != "$REL" ]]; then depmod -a "$k" || true; fi
        done
        note "Kept $restored driver file(s) working for your other kernels"
    fi
    rm -rf "$PRESERVE"
fi
echo "::modrebuild::$MODREBUILD_OK""#,
                sign_call = if signing { "sign_modules \"/lib/modules/$REL\"" } else { "" },
            );
        }
    }

    if plan.install.runtime_tuning {
        let _ = writeln!(
            s,
            "\n# ── Runtime tuning pack (sysctl + udev) ──────────────────────────────────────\nstage tuning \"Applying runtime tuning\"\n{}",
            tuning::install_snippet(plan.profile == ProfileId::Gaming)
        );
    }

    if plan.install.clean_after {
        let keep_vmlinux = if autofdo_ready {
            "note \"Keeping vmlinux for AutoFDO profiling\"\ncp \"$TREE/vmlinux\" \"$TREE/.kernelsmith/vmlinux\"\n"
        } else {
            ""
        };
        let _ = writeln!(
            s,
            r#"
# ── Free disk space (external modules can still be built against this tree) ──
stage clean "Freeing disk space"
{keep_vmlinux}"${{MAKE[@]}}" -s clean"#
        );
    }

    let _ = writeln!(
        s,
        r#"
sync
echo "::done::$REL""#
    );
    s
}

/// Shell functions for signing modules after the fact: every `.ko` under a
/// directory that lacks the kernel's signature trailer gets one, compressed
/// modules included. In-tree modules are already signed by `modules_install`.
const SIGN_FUNCS: &str = r#"
# ── Module signing helpers ────────────────────────────────────────────────────
has_sig() {
    # $1: module file, possibly compressed. The trailer is the last 28 bytes.
    case $1 in
        *.zst) zstd -dcq "$1" | tail -c 28 | grep -aq 'Module signature appended' ;;
        *.xz)  xz -dc "$1" | tail -c 28 | grep -aq 'Module signature appended' ;;
        *.gz)  gzip -dc "$1" | tail -c 28 | grep -aq 'Module signature appended' ;;
        *)     tail -c 28 "$1" | grep -aq 'Module signature appended' ;;
    esac
}
sign_modules() {
    local dir=$1 ko plain signed=0 total=0
    [[ -x $TREE/scripts/sign-file ]] || { warn "$TREE/scripts/sign-file is missing; out-of-tree modules were not signed"; return 0; }
    while IFS= read -r -d '' ko; do
        total=$((total + 1))
        has_sig "$ko" && continue
        plain=$ko
        case $ko in
            *.zst) plain=${ko%.zst}; zstd -dq --rm "$ko" -o "$plain" ;;
            *.xz)  plain=${ko%.xz};  xz -d "$ko" ;;
            *.gz)  plain=${ko%.gz};  gzip -d "$ko" ;;
        esac
        "$TREE/scripts/sign-file" "$SIGN_HASH" "$SIGN_KEY" "$SIGN_CERT" "$plain"
        signed=$((signed + 1))
        case $ko in
            *.zst) zstd -q --rm -19 "$plain" ;;
            *.xz)  xz "$plain" ;;
            *.gz)  gzip -9 "$plain" ;;
        esac
    done < <(find "$dir" -type f \( -name '*.ko' -o -name '*.ko.zst' -o -name '*.ko.xz' -o -name '*.ko.gz' \) -print0)
    note "Signed $signed of $total modules in $dir (the others already carried a signature)"
}
sign_modules "/lib/modules/$REL"
"#;

/// Portage configuration so that drivers rebuilt later (a new NVIDIA release,
/// `emerge @module-rebuild`) are signed with the same key by linux-mod-r1.
fn portage_sign_snippet(sys: &SystemInfo) -> String {
    let atoms: Vec<&str> = sys.external_modules.iter().map(|p| p.atom.as_str()).collect();
    if atoms.is_empty() {
        return String::new();
    }
    let pkgenv: Vec<String> = atoms.iter().map(|a| format!("{a} kernelsmith-modules-sign.conf")).collect();
    let pkguse: Vec<String> = atoms.iter().map(|a| format!("{a} modules-sign")).collect();
    format!(
        r#"# ── Keep Portage signing rebuilt drivers with the Kernelsmith key ──────────
if [[ -d /etc/portage && ( -d /etc/portage/package.use || ! -e /etc/portage/package.use ) ]]; then
    mkdir -p /etc/portage/env /etc/portage/package.env /etc/portage/package.use "$STATE/portage"
    cat > {env} <<KS_EOF
# Written by Kernelsmith: sign out-of-tree modules with the Kernelsmith key (linux-mod-r1).
MODULES_SIGN_KEY="$SIGN_KEY"
MODULES_SIGN_CERT="$SIGN_CERT"
MODULES_SIGN_HASH="$SIGN_HASH"
KS_EOF
    printf '%s\n' {pkgenv} > {pkgenv_file}
    printf '%s\n' {pkguse} > {pkguse_file}
    # Listed like a Portage-mode integration, so Library → "Runtime tuning & Portage" can remove it.
    printf '%s\n' {env} {pkgenv_file} {pkguse_file} > "$STATE/portage/signing.list"
    note "Portage will sign {n} driver package(s) with $SIGN_KEY on every rebuild"
else
    warn "/etc/portage/package.use is a file; not adding USE=modules-sign for your driver packages"
fi"#,
        env = q(paths::PORTAGE_SIGN_ENV),
        pkgenv = pkgenv.iter().map(|l| q(l)).collect::<Vec<_>>().join(" "),
        pkgenv_file = q(paths::PORTAGE_SIGN_PKGENV),
        pkguse = pkguse.iter().map(|l| q(l)).collect::<Vec<_>>().join(" "),
        pkguse_file = q(paths::PORTAGE_SIGN_USE),
        n = atoms.len(),
    )
}

/// A GRUB script that adds a "(EAC)" entry booting this kernel with the
/// command line Easy Anti-Cheat Linux v2 checks. Root device, subvolume and
/// early microcode are derived the way 10_linux does it.
fn eac_entry_snippet(sys: &SystemInfo) -> String {
    let cmdline = tweaks::eac_cmdline(sys);
    format!(
        r##"# ── Anti-cheat boot entry (same kernel, EAC command line) ──────────────────
if [[ -d /etc/grub.d ]] && command -v grub-mkconfig >/dev/null; then
    EAC_SCRIPT="/etc/grub.d/41_kernelsmith-eac-$REL"
    cat > "$EAC_SCRIPT" <<'KS_EOF'
#!/bin/sh
# Written by Kernelsmith: boots __REL__ with the options Easy Anti-Cheat Linux v2
# checks on the measured command line. The normal entry stays unlocked.
set -e
prefix="/usr"
exec_prefix="/usr"
datarootdir="/usr/share"
. "${{datarootdir}}/grub/grub-mkconfig_lib"
REL=__REL__
EAC_REQ="{cmdline}"
kernel=/boot/vmlinuz-$REL
[ -e "$kernel" ] || exit 0
initrd=
for i in /boot/initramfs-$REL.img /boot/initrd-$REL /boot/initrd.img-$REL; do
    [ -e "$i" ] && initrd=$i && break
done
case x"$GRUB_DEVICE_UUID" in
    x) LINUX_ROOT_DEVICE=${{GRUB_DEVICE}} ;;
    *) LINUX_ROOT_DEVICE=UUID=${{GRUB_DEVICE_UUID}} ;;
esac
case x"$GRUB_FS" in
    xbtrfs)
        rootsubvol="`make_system_path_relative_to_its_root /`"
        rootsubvol="${{rootsubvol#/}}"
        [ "x${{rootsubvol}}" != x ] && GRUB_CMDLINE_LINUX="rootflags=subvol=${{rootsubvol}} ${{GRUB_CMDLINE_LINUX}}"
        ;;
esac
early=
for i in ${{GRUB_EARLY_INITRD_LINUX_STOCK}} ${{GRUB_EARLY_INITRD_LINUX_CUSTOM}}; do
    [ -e "/boot/${{i}}" ] && early="$early $(make_system_path_relative_to_its_root "/boot/${{i}}")"
done
gettext_printf "Found anti-cheat entry kernel: %s\n" "$kernel" >&2
cat <<ENTRY
menuentry 'Gentoo (EAC) $REL' --class gentoo --class gnu-linux --class gnu --class os \$menuentry_id_option 'kernelsmith-eac-$REL' {{
	load_video
	insmod gzio
	insmod part_gpt
	insmod btrfs
ENTRY
[ x"$GRUB_DEVICE_BOOT" != x ] && prepare_grub_to_access_device "${{GRUB_DEVICE_BOOT}}" | grub_add_tab
cat <<ENTRY
	echo 'Loading Linux $REL (anti-cheat: lockdown=integrity, signed modules only, IOMMU on)'
	linux $(make_system_path_relative_to_its_root "$kernel") root=${{LINUX_ROOT_DEVICE}} ro ${{GRUB_CMDLINE_LINUX}} ${{GRUB_CMDLINE_LINUX_DEFAULT}} ${{EAC_REQ}}
ENTRY
if [ -n "$initrd" ]; then
    cat <<ENTRY
	echo 'Loading initial ramdisk ...'
	initrd${{early}} $(make_system_path_relative_to_its_root "$initrd")
ENTRY
fi
echo '}}'
KS_EOF
    sed -i "s|__REL__|$REL|g" "$EAC_SCRIPT"
    chmod 0755 "$EAC_SCRIPT"
    echo "$EAC_SCRIPT" >> "$TREE/.kernelsmith/extra.files"
    note "Added the anti-cheat GRUB entry script $EAC_SCRIPT"
else
    warn "No GRUB: add a boot entry for $REL yourself with these options: {cmdline}"
fi"##,
        cmdline = cmdline,
    )
}

pub fn profile_slug(p: ProfileId) -> &'static str {
    match p {
        ProfileId::Gaming => "gaming",
        ProfileId::Desktop => "desktop",
        ProfileId::Creator => "creator",
        ProfileId::Workstation => "workstation",
        ProfileId::Laptop => "laptop",
        ProfileId::Server => "server",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hw;
    use crate::job::simple_script;
    use crate::library::{uninstall_body, Manifest};
    use crate::patches::PlannedPatch;
    use crate::plan::{self, BuildPlan, ProfileData};
    use crate::profiles::ProfileId;
    use crate::sources::{Flavor, KernelSource};
    use std::path::{Path, PathBuf};
    use std::process::Command;

    fn bash_ok(script: &str) {
        let dir = std::env::temp_dir().join(format!("ks-test-{}-{}", std::process::id(), script.len()));
        std::fs::write(&dir, script).unwrap();
        let out = Command::new("bash").arg("-n").arg(&dir).output().unwrap();
        let _ = std::fs::remove_file(&dir);
        assert!(out.status.success(), "bash -n failed: {}", String::from_utf8_lossy(&out.stderr));
    }

    fn source() -> KernelSource {
        KernelSource {
            path: PathBuf::from("/usr/src/linux-7.3.0-gentoo"),
            dir_name: "linux-7.3.0-gentoo".into(),
            version: "7.3.0".into(),
            extraversion: "-gentoo".into(),
            flavor: Flavor::Gentoo,
            is_current_symlink: true,
            is_built: false,
            has_config: false,
            buildable: true,
        }
    }

    fn full_plan(sys: &hw::SystemInfo) -> BuildPlan {
        let mut p = BuildPlan::new(sys, ProfileId::Gaming, &source());
        p.install.runtime_tuning = true;
        p.patches.push(PlannedPatch { id: "bore".into(), title: "BORE scheduler".into(), file: PathBuf::from("/tmp/x/0001-bore.patch"), url: None });
        p.profile_data = Some(ProfileData { afdo: PathBuf::from("/home/u/kernel.afdo"), propeller_prefix: None });
        p.tweaks.insert("autofdo".into(), "autofdo".into());
        p.tweaks.insert("signing".into(), "enforce".into());
        p.tweaks.insert("anticheat".into(), "ready".into());
        p
    }

    #[test]
    fn build_script_is_valid_bash_with_safety_steps() {
        let sys = hw::sample();
        let src = source();
        let plan = full_plan(&sys);
        let resolved = plan::resolve(&plan, &sys, &src, None);
        assert_eq!(resolved.release, "7.3.0-gentoo-ks-gaming");
        let s = generate(&ScriptInputs { plan: &plan, sys: &sys, src: &src, resolved: &resolved, job_dir: Path::new("/tmp/job"), seed_label: "Your current kernel's configuration", store_seed: true });
        bash_ok(&s);
        assert!(s.contains("cp \"$JOB/seed.config\" \"$TREE/.kernelsmith/seed.config\""));
        for needle in [
            "set -Eeuo pipefail",
            "LLVM=1",
            "cp -a --reflink=auto \"$SRC\" \"$TREE\"",
            "mrproper",
            "localmodconfig",
            "syncconfig",
            "kernelrelease",
            "INSTALL_MOD_STRIP=1 modules_install",
            "@module-rebuild",
            "KERNEL_DIR=\"$TREE\"",
            "CLANG_AUTOFDO_PROFILE=/home/u/kernel.afdo",
            "grep -F -- \"$REL\"",
            "verify GENTOO_LINUX_INIT_SCRIPT y 1",
            "verify BTRFS_FS y 1",
            "verify DRM_FBDEV_EMULATION y 1",
            "verify VMD y 1",
            "verify MODULE_SIG_FORCE y 0",
            "verify SECURITY_LOCKDOWN_LSM_EARLY y 0",
            "verify NR_CPUS 16 0",
            "openssl req -new -x509",
            "kconf --set-str MODULE_SIG_KEY",
            "scripts/sign-file",
            "sign_modules \"/lib/modules/$REL\"",
            "portageq envvar MODULES_SIGN_KEY",
            "$STATE/portage/signing.list",
            "/etc/portage/env/kernelsmith-modules-sign.conf",
            "x11-drivers/nvidia-drivers modules-sign",
            "41_kernelsmith-eac-$REL",
            "lockdown=integrity lsm=landlock,lockdown,yama,integrity,bpf",
            "intel_iommu=on",
            "-fprofile-sample-use",
            "/etc/sysctl.d/90-kernelsmith.conf",
            "::done::$REL",
        ] {
            assert!(s.contains(needle), "script lacks: {needle}");
        }
        // The embedded GRUB script must itself be valid POSIX sh.
        let start = s.find("#!/bin/sh
# Written by Kernelsmith").expect("grub script");
        let end = start + s[start..].find("
KS_EOF
").expect("heredoc end");
        let grub = s[start..end].replace("__REL__", "7.3.0-gentoo-ks-gaming");
        bash_ok(&grub);
        // The cleanup trap must only ever delete a tree this run created.
        assert!(s.contains("$TREE_CREATED -eq 1 && $INSTALL_STARTED -eq 0"));
    }

    #[test]
    fn boot_snapshot_survives_missing_dirs_under_pipefail() {
        let sys = hw::sample();
        let src = source();
        let plan = full_plan(&sys);
        let resolved = plan::resolve(&plan, &sys, &src, None);
        let s = generate(&ScriptInputs { plan: &plan, sys: &sys, src: &src, resolved: &resolved, job_dir: Path::new("/tmp/job"), seed_label: "x", store_seed: false });
        let start = s.find("boot_snapshot() {").expect("snapshot function");
        let end = start + s[start..].find("\n}\n").expect("function end") + 3;
        let func = &s[start..end];
        let out = Command::new("bash")
            .arg("-c")
            .arg(format!("set -Eeuo pipefail\n{func}\nboot_snapshot >/dev/null\necho survived"))
            .output()
            .unwrap();
        assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "survived", "{}", String::from_utf8_lossy(&out.stderr));
        assert!(s.contains("installkernel \"$REL\""));
    }

    #[test]
    fn build_only_plan_skips_root_and_install() {
        let sys = hw::sample();
        let src = source();
        let mut plan = BuildPlan::new(&sys, ProfileId::Server, &src);
        plan.install.install = false;
        plan.build_root = Some(PathBuf::from("/tmp/ks-builds"));
        let resolved = plan::resolve(&plan, &sys, &src, None);
        let s = generate(&ScriptInputs { plan: &plan, sys: &sys, src: &src, resolved: &resolved, job_dir: Path::new("/tmp/job"), seed_label: "x", store_seed: false });
        bash_ok(&s);
        assert!(!s.contains(".kernelsmith/seed.config"));
        assert!(!s.contains("modules_install"));
        assert!(!s.contains("EUID -eq 0 ]] || fail"));
        assert!(s.contains("TREE=/tmp/ks-builds/linux-7.3.0-gentoo-ks-server"));
        // Signing is on by default; without root the key lives in the job directory.
        assert!(s.contains("SIGN_KEY=/tmp/job/keys/modules-signing.pem"));
        assert!(!s.contains("41_kernelsmith-eac"));
    }

    #[test]
    fn signing_off_leaves_no_trace() {
        let sys = hw::sample();
        let src = source();
        let mut plan = BuildPlan::new(&sys, ProfileId::Gaming, &src);
        plan.tweaks.insert("signing".into(), "off".into());
        plan.tweaks.insert("anticheat".into(), "off".into());
        let resolved = plan::resolve(&plan, &sys, &src, None);
        let s = generate(&ScriptInputs { plan: &plan, sys: &sys, src: &src, resolved: &resolved, job_dir: Path::new("/tmp/job"), seed_label: "x", store_seed: false });
        bash_ok(&s);
        for needle in ["SIGN_KEY=", "sign_modules", "kernelsmith-modules-sign", "41_kernelsmith-eac"] {
            assert!(!s.contains(needle), "script should not contain {needle}");
        }
    }

    #[test]
    fn uninstall_and_helper_scripts_are_valid() {
        let m = Manifest {
            release: "7.3.0-gentoo-ks-gaming".into(),
            name: "gaming".into(),
            profile: "gaming".into(),
            created: 0,
            status: "installed".into(),
            tree: PathBuf::from("/usr/src/linux-7.3.0-gentoo-ks-gaming"),
            source: PathBuf::from("/usr/src/linux-7.3.0-gentoo"),
            modules_dir: PathBuf::from("/lib/modules/7.3.0-gentoo-ks-gaming"),
            previous_symlink: "linux-7.3.0-gentoo".into(),
            autofdo_ready: false,
            files: vec![PathBuf::from("/boot/vmlinuz-7.3.0-gentoo-ks-gaming"), PathBuf::from("/boot/initramfs-7.3.0-gentoo-ks-gaming.img")],
        };
        let body = uninstall_body(&m);
        assert!(body.contains("uname -r"), "must refuse to remove the running kernel");
        bash_ok(&simple_script("Uninstall", &[("remove", "Removing")], &body));
        bash_ok(&simple_script("Tuning", &[("tuning", "Tuning")], &crate::tuning::install_snippet(true)));
        bash_ok(&simple_script("Tuning", &[("tuning", "Tuning")], &crate::tuning::remove_snippet()));
    }

    #[test]
    fn portage_fragment_and_use_flags() {
        let sys = hw::sample();
        let plan = BuildPlan::new(&sys, ProfileId::Gaming, &source());
        let frag = crate::portage::fragment(&plan, &sys, None);
        assert!(frag.contains("CONFIG_NTSYNC=y"));
        assert!(frag.contains("CONFIG_HZ_1000=y"));
        assert!(frag.contains("# CONFIG_HZ_300 is not set"));
        let use_line = crate::portage::cachyos_use(&plan);
        assert!(use_line.starts_with("sys-kernel/cachyos-kernel "));
        assert!(use_line.contains(" bore ") && use_line.contains(" -eevdf"));
        assert!(use_line.contains("llvm-lto-thin") && use_line.contains("hz_ticks_1000"));
    }
}
