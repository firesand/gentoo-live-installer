//! What the root helper agrees to do: programs, their arguments, environment
//! variables and writable paths. These checks run in the helper, never only
//! in the GUI.

use crate::emerge_opts;

/// Environment variables passed to programs (everything else is dropped).
pub const ENV_ALLOWED: &[&str] = &["FEATURES", "MAKEOPTS", "NOCOLOR", "COLUMNS", "TERM"];

pub fn env_allowed(key: &str) -> bool {
    ENV_ALLOWED.contains(&key)
}

/// Does a token of a program without its own argument grammar look like a
/// path? `~cat/pkg` is an atom, `~` and `~/x` are home-relative paths.
fn looks_like_path(a: &str) -> bool {
    a.starts_with('/') || a.starts_with("./") || a.starts_with("../") || a == "~" || a.starts_with("~/") || a == "." || a == ".." || a.contains("/../")
}

/// Validate the arguments for an allowed program. `program` is the basename.
pub fn check_args(program: &str, args: &[String]) -> Result<(), String> {
    match program {
        // An allow-list, not a deny-list: see emerge_opts for why.
        "emerge" => emerge_opts::check(args),
        "eselect" => {
            let mut it = args.iter().filter(|a| !a.starts_with("--colour") && !a.starts_with("--color"));
            let module = it.next().map(|s| s.as_str()).unwrap_or("");
            let rest: Vec<&String> = it.collect();
            match module {
                "profile" | "news" | "kernel" => {
                    if let Some(a) = rest.iter().find(|a| a.contains('/') && !(module == "profile" && !looks_like_path(a))) {
                        return Err(format!("eselect argument not allowed: {a}"));
                    }
                    Ok(())
                }
                "repository" => {
                    let sub = rest.first().map(|s| s.as_str()).unwrap_or("");
                    match sub {
                        "list" | "enable" | "disable" | "remove" => {
                            if let Some(a) = rest.iter().skip(1).find(|a| a.contains('/')) {
                                return Err(format!("eselect repository argument not allowed: {a}"));
                            }
                            Ok(())
                        }
                        "create" => {
                            // eselect repository create NAME [PATH]: PATH absolute, no `..`.
                            let name = rest.get(1).map(|s| s.as_str()).unwrap_or("");
                            if name.is_empty() || name.contains('/') || name.starts_with('.') || name.starts_with('-') {
                                return Err("invalid repository name".into());
                            }
                            match rest.get(2) {
                                None => Ok(()),
                                Some(p) if p.starts_with('/') && !p.split('/').any(|c| c == "..") && rest.len() == 3 => Ok(()),
                                Some(p) => Err(format!("invalid repository location {p}")),
                            }
                        }
                        _ => Err(format!("eselect repository {sub} is not allowed")),
                    }
                }
                _ => Err(format!("eselect module not allowed: {module}")),
            }
        }
        _ => {
            if let Some(a) = args.iter().find(|a| looks_like_path(a) || (a.starts_with('-') && a.split_once('=').is_some_and(|(_, v)| looks_like_path(v)))) {
                return Err(format!("path arguments are not allowed for {program}: {a}"));
            }
            Ok(())
        }
    }
}

/// Files below /etc/portage the helper refuses to create, change or move.
///
/// This is a speed bump, not a sandbox: an authenticated helper session can run
/// package-manager commands and rewrite /etc/portage, which is equivalent to
/// full administrator access (make.conf `FETCHCOMMAND`/`PORTAGE_ELOG_COMMAND`,
/// a `repos.conf` entry that points at a foreign tree, ... all run as root).
/// What the helper does refuse are the places where Portage runs shell code or
/// patches source for every build without anyone having asked for it by name,
/// which the GUI never writes:
///
/// * `bashrc`, `profile/profile.bashrc` and anything named `bashrc`;
/// * `postsync.d/` and `repo.postsync.d/` (run after every sync);
/// * `env/<category>/<package>` (sourced as bash in every phase of that package;
///   single-component `env/<name>` files for `package.env` stay writable);
/// * `patches/` (applied to the sources of matching packages);
/// * `._cfg` names, which would let a later "accept config update" move
///   arbitrary content into place.
///
/// The GUI's review dialog flags the remaining command-running settings
/// (see `danger::flag_dangerous_lines`).
pub fn portage_write_allowed(rel: &str) -> bool {
    let parts: Vec<&str> = rel.split('/').collect();
    let first = parts[0];
    let hook = matches!(first, "postsync.d" | "repo.postsync.d" | "patches") || rel == "profile/profile.bashrc" || parts.contains(&"bashrc");
    let env_hook = first == "env" && parts.len() >= 3;
    let cfg = parts.iter().any(|c| c.starts_with("._cfg"));
    !(hook || env_hook || cfg)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn emerge_args() {
        assert!(check_args("emerge", &v(&["--color=n", "-uDN", "--with-bdeps=y", "--exclude=app-misc/foo", "@world"])).is_ok());
        assert!(check_args("emerge", &v(&["--config", "=sys-kernel/x-1.0"])).is_ok());
        assert!(check_args("emerge", &v(&["--config-root=/home/u/evil", "@world"])).is_err());
        assert!(check_args("emerge", &v(&["--root", "x"])).is_err());
        assert!(check_args("emerge", &v(&["/home/u/evil.ebuild"])).is_err());
        assert!(check_args("emerge", &v(&["./evil.tbz2"])).is_err());
        assert!(check_args("emerge", &v(&["--usepkg-exclude=/tmp/x"])).is_err());
    }

    /// Spellings argparse resolves to --config-root, --sysroot and --prefix
    /// (verified against Portage 3.0.82 parse_opts; see tests/policy_oracle.rs),
    /// and relative file arguments emerge would install or build as root.
    #[test]
    fn emerge_bypass_spellings_are_refused() {
        for bad in [
            &["--config-=tmp/x", "@world"][..],
            &["--config-r", "tmp/x", "@world"],
            &["--config-ro", "tmp/x"],
            &["--config-roo=tmp/x"],
            &["--sys=tmp/x"],
            &["--sysr", "tmp/x"],
            &["--sysro=tmp/x"],
            &["--sysroo=tmp/x"],
            &["--pref=tmp/x"],
            &["--prefi=tmp/x"],
            &["--roo=tmp/x"],
            &["tmp/evil.tbz2"],
            &["tmp/evil.gpkg.tar"],
            &["tmp/x.ebuild"],
            &["evil.xpak"],
            &["--"],
            &["-"],
            &["-p", "@world"],
            &["--autounmask-write"],
            &["--quickpkg-direct-root=tmp/x"],
        ] {
            assert!(check_args("emerge", &v(bad)).is_err(), "{bad:?}");
        }
        // Programs that forwarded options to emerge are no longer launchable at all.
        for gone in ["eix-sync", "eix-update", "revdep-rebuild", "perl-cleaner", "etc-update", "dispatch-conf", "eclean", "quickpkg", "fixpackages", "eread", "haskell-updater", "smart-live-rebuild"] {
            assert!(crate::server::resolve_program(gone).is_none(), "{gone}");
        }
    }

    #[test]
    fn tilde_atoms_are_not_paths() {
        assert!(check_args("emerge", &v(&["--pretend", "~dev-lang/python-3.11.5"])).is_ok());
        assert!(check_args("emerge", &v(&["--exclude=~cat/pkg-1"])).is_ok());
        assert!(check_args("emerge", &v(&["~/x"])).is_err());
        assert!(check_args("emerge", &v(&["~"])).is_err());
        assert!(!looks_like_path("~dev-lang/python-3.11.5"));
        assert!(looks_like_path("~") && looks_like_path("~/x"));
        // Programs without their own grammar still refuse path-like tokens.
        assert!(check_args("emaint", &v(&["sync", "-r", "~/x"])).is_err());
        assert!(check_args("glsa-check", &v(&["-t", "~dev-lang/python-3.11.5"])).is_ok());
    }

    /// Every program the GUI starts as root passes the policy with the arguments it sends.
    #[test]
    fn every_gui_launch_is_allowed() {
        let launches: &[&[&str]] = &[
            &["emerge", "--config", "--ask=n", "=sys-kernel/gentoo-kernel-6.12.8"],
            &["emerge", "--noreplace", "--ask=n", "@kernels-keep"],
            &["emerge", "--deselect", "--ask=n", "dev-libs/foo"],
            &["emerge", "--deselect", "dev-libs/foo"],
            &["emerge", "--info"],
            &["emerge", "--regen"],
            &["emerge", "--pretend", "--depclean", "--color=n", "--nospinner", "--ask=n"],
            &["emerge-webrsync"],
            &["eselect", "news", "read", "new"],
            &["eselect", "news", "purge"],
            &["eselect", "profile", "set", "5"],
            &["eselect", "repository", "enable", "guru"],
            &["glsa-check", "-l", "affected"],
            &["eclean-dist", "--deep"],
            &["eclean-dist", "--deep", "--pretend"],
            &["eclean-pkg", "--deep", "--pretend"],
            &["env-update"],
            &["emaint", "sync", "-r", "guru"],
            &["emaint", "logs", "--clean", "--time", "30"],
            &["egencache", "--update", "--repo=guru", "--jobs=8"],
        ];
        for l in launches {
            let program = crate::server::resolve_program(l[0]);
            // The program may not be installed on the machine running the test; the name must be on the list.
            assert!(program.is_some() || crate::protocol::ALLOWED_PROGRAMS.contains(&l[0]), "{l:?}");
            assert!(check_args(l[0], &v(&l[1..])).is_ok(), "{l:?}: {:?}", check_args(l[0], &v(&l[1..])));
        }
    }

    #[test]
    fn eselect_args() {
        assert!(check_args("eselect", &v(&["--colour=no", "repository", "list"])).is_ok());
        assert!(check_args("eselect", &v(&["repository", "enable", "guru"])).is_ok());
        assert!(check_args("eselect", &v(&["repository", "create", "mine", "/var/db/repos/mine"])).is_ok());
        assert!(check_args("eselect", &v(&["repository", "create", "mine", "/home/u/repo"])).is_ok());
        assert!(check_args("eselect", &v(&["repository", "create", "mine", "/var/db/../../etc"])).is_err());
        assert!(check_args("eselect", &v(&["repository", "create", "mine", "relative"])).is_err());
        assert!(check_args("eselect", &v(&["repository", "create", "../x"])).is_err());
        assert!(check_args("eselect", &v(&["profile", "set", "12"])).is_ok());
        assert!(check_args("eselect", &v(&["profile", "set", "default/linux/amd64/23.0"])).is_ok());
        assert!(check_args("eselect", &v(&["news", "read", "--quiet", "all"])).is_ok());
        assert!(check_args("eselect", &v(&["kernel", "set", "linux-6.1"])).is_ok());
        assert!(check_args("eselect", &v(&["rc", "add", "x"])).is_err());
        assert!(check_args("eselect", &v(&["kernel", "set", "/boot/x"])).is_err());
    }

    #[test]
    fn other_programs_and_env() {
        assert!(check_args("emaint", &v(&["sync", "-r", "guru"])).is_ok());
        assert!(check_args("emaint", &v(&["logs", "--clean", "--time", "30"])).is_ok());
        assert!(check_args("eclean-dist", &v(&["--deep"])).is_ok());
        assert!(check_args("egencache", &v(&["--update", "--repo=guru", "--jobs=8"])).is_ok());
        assert!(check_args("egencache", &v(&["--cache-dir=/tmp/x"])).is_err());
        assert!(env_allowed("FEATURES") && env_allowed("MAKEOPTS"));
        for k in ["PORTAGE_CONFIGROOT", "ROOT", "EMERGE_DEFAULT_OPTS", "USE", "LD_PRELOAD", "PORTAGE_BASHRC", "PATH"] {
            assert!(!env_allowed(k), "{k}");
        }
    }

    #[test]
    fn hook_files() {
        assert!(portage_write_allowed("package.use/zz"));
        assert!(portage_write_allowed("env/notmpfs"));
        assert!(portage_write_allowed("make.conf"));
        assert!(portage_write_allowed("repos.conf/guru.conf"));
        assert!(portage_write_allowed("sets/kernels-keep"));
        assert!(portage_write_allowed("profile/package.use.mask"));
        assert!(!portage_write_allowed("bashrc"));
        assert!(!portage_write_allowed("postsync.d/99-evil"));
        assert!(!portage_write_allowed("repo.postsync.d/x"));
        assert!(!portage_write_allowed("profile/profile.bashrc"));
        assert!(!portage_write_allowed("._cfg0000_bashrc"));
        assert!(!portage_write_allowed("package.use/._cfg0001_zz"));
        // Per-package bash hooks, patches and nested bashrc files.
        assert!(!portage_write_allowed("env/app-misc/hello"));
        assert!(!portage_write_allowed("env/app-misc/hello:2"));
        assert!(!portage_write_allowed("env/app-misc/hello-1.0"));
        assert!(!portage_write_allowed("patches/sys-apps/coreutils/a.patch"));
        assert!(!portage_write_allowed("patches/x"));
        assert!(!portage_write_allowed("env/bashrc"));
        assert!(!portage_write_allowed("profile/bashrc"));
        assert!(!portage_write_allowed("package.use/bashrc"));
    }
}
