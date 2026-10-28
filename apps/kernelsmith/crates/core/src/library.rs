//! Installed kernels: discovery, Kernelsmith manifests, and clean uninstall.

use crate::hw::SystemInfo;
use crate::job::{prepare_simple, PreparedJob};
use crate::paths;
use crate::util::shell_quote as q;
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// Written by the build script after `make install`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Manifest {
    pub release: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub profile: String,
    #[serde(default)]
    pub created: u64,
    #[serde(default)]
    pub status: String,
    pub tree: PathBuf,
    #[serde(default)]
    pub source: PathBuf,
    pub modules_dir: PathBuf,
    #[serde(default)]
    pub previous_symlink: String,
    #[serde(default)]
    pub autofdo_ready: bool,
    #[serde(default)]
    pub files: Vec<PathBuf>,
}

impl Manifest {
    pub fn plan_file(&self) -> PathBuf {
        self.tree.join(".kernelsmith/plan.toml")
    }

    /// Unstripped vmlinux kept for AutoFDO (or the build tree's own).
    pub fn vmlinux(&self) -> Option<PathBuf> {
        [self.tree.join(".kernelsmith/vmlinux"), self.tree.join("vmlinux")].into_iter().find(|p| p.is_file())
    }
}

#[derive(Debug, Clone)]
pub enum Origin {
    Kernelsmith(Box<Manifest>),
    /// Owned by a Portage package (dist-kernel).
    Portage(String),
    Manual,
}

#[derive(Debug, Clone)]
pub struct InstalledKernel {
    pub release: String,
    pub origin: Origin,
    pub running: bool,
    pub boot_image: Option<PathBuf>,
    pub build_tree: Option<PathBuf>,
    pub modules_dir: PathBuf,
}

impl InstalledKernel {
    pub fn manifest(&self) -> Option<&Manifest> {
        match &self.origin {
            Origin::Kernelsmith(m) => Some(m),
            _ => None,
        }
    }

    pub fn origin_label(&self) -> String {
        match &self.origin {
            Origin::Kernelsmith(_) => "Built with Kernelsmith".into(),
            Origin::Portage(p) => format!("Managed by Portage ({p})"),
            Origin::Manual => "Installed manually".into(),
        }
    }
}

pub fn load_manifests() -> HashMap<String, Manifest> {
    let mut out = HashMap::new();
    for e in std::fs::read_dir(paths::kernel_manifests()).into_iter().flatten().flatten() {
        let Ok(text) = std::fs::read_to_string(e.path()) else { continue };
        if let Ok(m) = toml::from_str::<Manifest>(&text) {
            out.insert(m.release.clone(), m);
        }
    }
    out
}

/// Which Portage package owns each `/lib/modules/<release>`.
fn portage_owners(sys: &SystemInfo) -> HashMap<String, String> {
    let mut out = HashMap::new();
    for p in &sys.dist_kernels {
        let contents = Path::new("/var/db/pkg").join(format!("{}-{}", p.atom, p.version)).join("CONTENTS");
        let Ok(text) = std::fs::read_to_string(contents) else { continue };
        for line in text.lines() {
            let path = line.split_whitespace().nth(1).unwrap_or("");
            let rest = path.strip_prefix("/lib/modules/").or_else(|| path.strip_prefix("/usr/lib/modules/"));
            if let Some(rel) = rest.and_then(|r| r.split('/').next()) {
                if !rel.is_empty() {
                    out.insert(rel.to_string(), format!("{}-{}", p.atom, p.version));
                    break;
                }
            }
        }
    }
    out
}

pub fn discover(sys: &SystemInfo) -> Vec<InstalledKernel> {
    let manifests = load_manifests();
    let owners = portage_owners(sys);
    let mut out = vec![];
    for e in std::fs::read_dir("/lib/modules").into_iter().flatten().flatten() {
        let release = e.file_name().to_string_lossy().into_owned();
        let boot = PathBuf::from(format!("/boot/vmlinuz-{release}"));
        let boot_image = boot.exists().then_some(boot);
        let build_tree = std::fs::canonicalize(e.path().join("build")).ok();
        // Directories with only leftovers (no kernel, no modules.dep) are noise.
        if boot_image.is_none() && !e.path().join("modules.dep").exists() && !manifests.contains_key(&release) {
            continue;
        }
        let origin = if let Some(m) = manifests.get(&release) {
            Origin::Kernelsmith(Box::new(m.clone()))
        } else if let Some(p) = owners.get(&release) {
            Origin::Portage(p.clone())
        } else {
            Origin::Manual
        };
        out.push(InstalledKernel {
            running: release == sys.running_kernel,
            modules_dir: e.path(),
            release,
            origin,
            boot_image,
            build_tree,
        });
    }
    out.sort_by(|a, b| {
        b.running
            .cmp(&a.running)
            .then_with(|| crate::sources::version_tuple(&b.release).cmp(&crate::sources::version_tuple(&a.release)))
            .then_with(|| b.release.cmp(&a.release))
    });
    out
}

/// Root script that removes a Kernelsmith-built kernel and nothing else.
pub fn uninstall_job(k: &InstalledKernel, sys: &SystemInfo) -> Result<PreparedJob> {
    let Some(m) = k.manifest() else {
        bail!("{} wasn't built by Kernelsmith; remove it with Portage or by hand.", k.release);
    };
    if k.running || k.release == sys.running_kernel {
        bail!("You are running {} right now. Boot another kernel first.", k.release);
    }
    prepare_simple(
        &format!("Uninstalling {}", m.release),
        &format!("uninstall-{}", m.release),
        &[("remove", "Removing kernel files"), ("bootloader", "Updating bootloader")],
        &uninstall_body(m),
    )
}

/// The body of the uninstall script for a manifest.
pub fn uninstall_body(m: &Manifest) -> String {
    let rel = &m.release;
    let mut files = String::new();
    for f in &m.files {
        files.push_str(&format!("    {} \\\n", q(&f.display().to_string())));
    }
    let tree_name = m.tree.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    format!(
        r#"REL={rel}
TREE={tree}
[[ "$(uname -r)" != "$REL" ]] || fail "You are running $REL right now. Boot another kernel first."

stage remove "Removing kernel files"
for f in \
{files}    ; do
    # Only ever touch boot files that carry this kernel's exact name.
    case "$f" in
        /boot/*|/efi/*)
            if [[ $f == *"$REL"* && ( -f $f || -L $f ) ]]; then
                rm -f -- "$f"
                note "Removed $f"
            fi
            ;;
    esac
    d=$(dirname "$f")
    while [[ $d == *"$REL"* && -d $d ]]; do rmdir -- "$d" 2>/dev/null || break; d=$(dirname "$d"); done
done
for d in "/lib/modules/$REL" "/usr/lib/modules/$REL"; do
    if [[ -d $d && ! -L $d ]]; then
        rm -rf --one-file-system -- "$d"
        note "Removed $d"
    fi
done
if [[ -d $TREE/.kernelsmith ]]; then
    rm -rf --one-file-system -- "$TREE"
    note "Removed build tree $TREE"
fi

stage bootloader "Updating bootloader"
if [[ -f /boot/grub/grub.cfg ]] && command -v grub-mkconfig >/dev/null; then
    grub-mkconfig -o /boot/grub/grub.cfg
elif command -v kernel-install >/dev/null && [[ -d /efi/loader/entries || -d /boot/loader/entries ]]; then
    kernel-install remove "$REL" || true
fi
if [[ "$(readlink /usr/src/linux 2>/dev/null)" == {tree_name} ]]; then
    PREV={prev}
    if [[ -n $PREV && -d /usr/src/$PREV ]]; then
        ln -sfn "$PREV" /usr/src/linux
        note "/usr/src/linux points to $PREV again"
    else
        rm -f /usr/src/linux
        warn "/usr/src/linux pointed to the removed tree; select another with: eselect kernel set <n>"
    fi
fi
rm -f "/var/lib/kernelsmith/kernels/$REL.toml"
note "{rel} has been uninstalled.""#,
        rel = q(rel),
        tree = q(&m.tree.display().to_string()),
        files = files,
        tree_name = q(&tree_name),
        prev = q(&m.previous_symlink),
    )
}

/// Read the kernel's config (installed copy first, then /proc for the running one).
pub fn kernel_config_path(k: &InstalledKernel) -> Option<PathBuf> {
    let boot = PathBuf::from(format!("/boot/config-{}", k.release));
    if boot.exists() {
        return Some(boot);
    }
    k.build_tree.as_ref().map(|t| t.join(".config")).filter(|p| p.exists())
}
