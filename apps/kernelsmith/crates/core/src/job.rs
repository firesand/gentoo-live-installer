//! Job directories: everything a root script needs, prepared as the user.
//!
//! `~/.local/share/kernelsmith/jobs/<stamp>-<name>/` holds the plan, the seed
//! config, the module list for localmodconfig, copies of the patches, the
//! generated script and (written by the GUI) the full log.

use crate::hw::SystemInfo;
use crate::kconfig::{self, SymbolIndex};
use crate::paths;
use crate::plan::{self, BuildPlan, Seed};
use crate::script::{self, ScriptInputs};
use crate::slim::SlimMode;
use crate::sources::KernelSource;
use crate::util::{now_epoch, shell_quote as q, slug, stamp};
use anyhow::{Context, Result};
use std::collections::BTreeSet;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct PreparedJob {
    pub title: String,
    pub dir: PathBuf,
    pub script: PathBuf,
    pub log: PathBuf,
    pub stages: Vec<String>,
    pub release: Option<String>,
    pub est_objects: u64,
}

fn new_job_dir(name: &str) -> Result<PathBuf> {
    let dir = paths::jobs_dir().join(format!("{}-{}", stamp(now_epoch()), slug(name)));
    std::fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
    Ok(dir)
}

fn write_script(path: &Path, body: &str) -> Result<()> {
    std::fs::write(path, body)?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))?;
    Ok(())
}

/// Lines for localmodconfig's LSMOD file: loaded modules plus modprobed-db.
pub fn module_list(sys: &SystemInfo, use_db: bool) -> Vec<String> {
    let mut set: BTreeSet<String> = std::fs::read_to_string("/proc/modules")
        .map(|t| t.lines().filter_map(|l| l.split_whitespace().next().map(String::from)).collect())
        .unwrap_or_else(|_| sys.loaded_modules.iter().cloned().collect());
    if use_db {
        if let Some(db) = &sys.modprobed_db {
            if let Ok(t) = std::fs::read_to_string(db) {
                set.extend(t.lines().map(|l| l.trim().to_string()).filter(|l| !l.is_empty() && !l.starts_with('#')));
            }
        }
    }
    set.into_iter().collect()
}

pub fn prepare_build(plan: &BuildPlan, sys: &SystemInfo, src: &KernelSource, symbols: Option<&SymbolIndex>) -> Result<PreparedJob> {
    let dir = new_job_dir(&plan.name)?;
    plan.save(&dir.join("plan.toml"))?;

    match &plan.seed {
        Seed::Running => std::fs::write(dir.join("seed.config"), kconfig::running_config_text()?)?,
        Seed::TreeConfig => {
            std::fs::copy(src.path.join(".config"), dir.join("seed.config")).context("copying the tree's .config")?;
        }
        Seed::File(p) => {
            std::fs::copy(p, dir.join("seed.config")).with_context(|| format!("copying {}", p.display()))?;
        }
        Seed::Defconfig => {}
    }
    if plan.slim != SlimMode::Off {
        let mut list = module_list(sys, plan.use_modprobed_db).join("\n");
        list.push('\n');
        std::fs::write(dir.join("modules.lst"), list)?;
    }
    if !plan.patches.is_empty() {
        let pdir = dir.join("patches");
        std::fs::create_dir_all(&pdir)?;
        for (i, p) in plan.patches.iter().enumerate() {
            let name = format!("{:02}-{}", i + 1, p.file.file_name().unwrap_or_default().to_string_lossy());
            std::fs::copy(&p.file, pdir.join(name)).with_context(|| format!("copying patch {}", p.file.display()))?;
        }
    }

    let resolved = plan::resolve(plan, sys, src, symbols);
    let body = script::generate(&ScriptInputs { plan, sys, src, resolved: &resolved, job_dir: &dir });
    let script = dir.join("build.sh");
    write_script(&script, &body)?;
    Ok(PreparedJob {
        title: format!("Building {}", resolved.release),
        log: dir.join("output.log"),
        dir,
        script,
        stages: script::stages_for(plan).into_iter().map(String::from).collect(),
        release: Some(resolved.release),
        est_objects: plan::estimate(plan, sys).objects,
    })
}

/// Common header for the smaller root scripts (uninstall, tuning, sources…).
pub fn simple_script(title: &str, stages: &[(&str, &str)], body: &str) -> String {
    let path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
    let ids: Vec<&str> = stages.iter().map(|s| s.0).collect();
    format!(
        "#!/bin/bash\n\
         # Kernelsmith: {title}\n\
         # Generated {date} UTC. Run with: sudo bash <this file>\n\
         set -Eeuo pipefail\n\
         exec 2>&1\n\
         umask 022\n\
         export LC_ALL=C LANG=C HOME=/root PATH={path}\n\
         stage() {{ echo \"::stage::$1::$2\"; }}\n\
         note()  {{ echo \"::note::$*\"; }}\n\
         warn()  {{ echo \"::warn::$*\"; }}\n\
         fail()  {{ echo \"::error::$*\"; exit 1; }}\n\
         trap 'echo \"::error::Command failed (line $LINENO): $BASH_COMMAND\"' ERR\n\
         trap 'echo \"::error::Cancelled\"; exit 130' TERM INT\n\
         [[ $EUID -eq 0 ]] || fail \"This script must run as root.\"\n\
         echo \"::plan::{ids}\"\n\n\
         {body}\n\
         echo \"::done::ok\"\n",
        date = crate::util::format_epoch(now_epoch()),
        ids = ids.join(","),
    )
}

/// Write a small root script into its own job directory.
pub fn prepare_simple(title: &str, name: &str, stages: &[(&str, &str)], body: &str) -> Result<PreparedJob> {
    let dir = new_job_dir(name)?;
    let script = dir.join("run.sh");
    write_script(&script, &simple_script(title, stages, body))?;
    Ok(PreparedJob {
        title: title.to_string(),
        log: dir.join("output.log"),
        dir,
        script,
        stages: stages.iter().map(|s| s.0.to_string()).collect(),
        release: None,
        est_objects: 0,
    })
}

/// Install kernel sources through Portage.
pub fn install_sources(atom: &str) -> Result<PreparedJob> {
    let body = format!(
        "stage emerge {stage}\n\
         note {msg}\n\
         emerge --ask=n --noreplace --quiet-build=y --color=n --nospinner {atom}\n",
        stage = q(&format!("Installing {atom}")),
        msg = q(&format!("Running: emerge --noreplace {atom}")),
        atom = q(atom),
    );
    prepare_simple(&format!("Installing {atom}"), "sources", &[("emerge", "Installing kernel sources")], &body)
}

/// Install or remove the runtime tuning pack on its own.
pub fn tuning(install: bool, gaming: bool) -> Result<PreparedJob> {
    if install {
        let body = format!("stage tuning \"Installing runtime tuning\"\n{}", crate::tuning::install_snippet(gaming));
        prepare_simple("Install runtime tuning", "tuning", &[("tuning", "Installing runtime tuning")], &body)
    } else {
        let body = format!("stage tuning \"Removing runtime tuning\"\n{}", crate::tuning::remove_snippet());
        prepare_simple("Remove runtime tuning", "tuning-remove", &[("tuning", "Removing runtime tuning")], &body)
    }
}
