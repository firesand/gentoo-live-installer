//! What the root helper agrees to do: programs, their arguments, environment
//! variables and writable paths. These checks run in the helper, never only
//! in the GUI.

/// Environment variables passed to programs (everything else is dropped).
pub const ENV_ALLOWED: &[&str] = &["FEATURES", "MAKEOPTS", "NOCOLOR", "COLUMNS", "TERM"];

pub fn env_allowed(key: &str) -> bool {
    ENV_ALLOWED.contains(&key)
}

/// emerge options that redirect where Portage reads configuration or installs.
const EMERGE_DENIED: &[&str] = &["--config-root", "--root", "--sysroot", "--prefix"];

fn looks_like_path(a: &str) -> bool {
    a.starts_with('/') || a.starts_with("./") || a.starts_with("../") || a.starts_with('~') || a == "." || a == ".." || a.contains("/../")
}

/// Validate the arguments for an allowed program. `program` is the basename.
pub fn check_args(program: &str, args: &[String]) -> Result<(), String> {
    match program {
        "emerge" => {
            for a in args {
                let opt = a.split('=').next().unwrap_or(a);
                if EMERGE_DENIED.contains(&opt) {
                    return Err(format!("emerge option not allowed: {opt}"));
                }
                if looks_like_path(a) {
                    return Err(format!("emerge path arguments are not allowed: {a}"));
                }
                if a.starts_with('-') && a.split_once('=').is_some_and(|(_, v)| looks_like_path(v)) {
                    return Err(format!("path-valued emerge option not allowed: {a}"));
                }
            }
            Ok(())
        }
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

/// Files below /etc/portage the helper refuses to create, change or move:
/// hooks Portage runs as root by itself (bashrc, postsync.d) — the GUI never
/// writes them — and `._cfg` names, which would let a later "accept config
/// update" move arbitrary content into place.
pub fn portage_write_allowed(rel: &str) -> bool {
    let first = rel.split('/').next().unwrap_or("");
    let hook = rel == "bashrc" || first == "postsync.d" || first == "repo.postsync.d" || rel == "profile/profile.bashrc" || rel.ends_with("/bashrc");
    let cfg = rel.split('/').any(|c| c.starts_with("._cfg"));
    !(hook || cfg)
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
        assert!(!portage_write_allowed("bashrc"));
        assert!(!portage_write_allowed("postsync.d/99-evil"));
        assert!(!portage_write_allowed("repo.postsync.d/x"));
        assert!(!portage_write_allowed("profile/profile.bashrc"));
        assert!(!portage_write_allowed("._cfg0000_bashrc"));
        assert!(!portage_write_allowed("package.use/._cfg0001_zz"));
    }
}
