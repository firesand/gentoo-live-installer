//! Every emerge option as data, so the GUI can render it as switches,
//! radios and number fields, and turn the selection back into argv.

use std::collections::BTreeMap;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Group {
    Update,
    Resolver,
    Autounmask,
    Build,
    Binary,
    Output,
}

impl Group {
    pub const ALL: [Group; 6] = [Group::Update, Group::Resolver, Group::Autounmask, Group::Build, Group::Binary, Group::Output];

    pub fn title(&self) -> &'static str {
        match self {
            Group::Update => "What to update",
            Group::Resolver => "Dependency resolution",
            Group::Autounmask => "Automatic unmasking",
            Group::Build => "Building",
            Group::Binary => "Binary packages",
            Group::Output => "Output & behaviour",
        }
    }

    pub fn icon(&self) -> &'static str {
        use egui_phosphor::regular as i;
        match self {
            Group::Update => i::ARROWS_CLOCKWISE,
            Group::Resolver => i::TREE_STRUCTURE,
            Group::Autounmask => i::LOCK_OPEN,
            Group::Build => i::HAMMER,
            Group::Binary => i::PACKAGE,
            Group::Output => i::TERMINAL_WINDOW,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub enum Kind {
    /// `--flag`
    Switch,
    /// `--flag=y|n` with a third "default" state
    YesNo,
    /// `--flag=N` (optional number; 0 / empty means plain `--flag` when allow_plain)
    Number { min: i64, max: i64, allow_plain: bool },
    /// `--flag atoms…`
    Atoms,
    /// `--flag=value` from a fixed list
    Choice(&'static [&'static str]),
}

#[derive(Debug, Clone, Copy)]
pub struct Spec {
    pub flag: &'static str,
    pub short: Option<char>,
    pub label: &'static str,
    pub help: &'static str,
    pub kind: Kind,
    pub group: Group,
    pub advanced: bool,
}

const fn sw(flag: &'static str, short: Option<char>, label: &'static str, help: &'static str, group: Group, advanced: bool) -> Spec {
    Spec { flag, short, label, help, kind: Kind::Switch, group, advanced }
}
const fn yn(flag: &'static str, label: &'static str, help: &'static str, group: Group, advanced: bool) -> Spec {
    Spec { flag, short: None, label, help, kind: Kind::YesNo, group, advanced }
}

pub const SPECS: &[Spec] = &[
    // What to update
    sw(
        "--update",
        Some('u'),
        "Update to the best version",
        "Upgrade the targets (and with Deep, their dependencies) to the best available version instead of only installing missing ones.",
        Group::Update,
        false,
    ),
    sw(
        "--deep",
        Some('D'),
        "Deep",
        "Consider the whole dependency tree, not just the packages you named. Needed for full system updates.",
        Group::Update,
        false,
    ),
    sw(
        "--newuse",
        Some('N'),
        "New USE",
        "Rebuild packages whose USE flags changed, including flags newly added to or removed from the ebuild.",
        Group::Update,
        false,
    ),
    sw(
        "--changed-use",
        Some('U'),
        "Changed USE",
        "Rebuild only when the effective USE flags you build with changed (lighter than New USE).",
        Group::Update,
        false,
    ),
    yn(
        "--with-bdeps",
        "Include build-time deps",
        "Also update build-time dependencies (BDEPEND) of installed packages. Recommended: yes.",
        Group::Update,
        false,
    ),
    sw(
        "--oneshot",
        Some('1'),
        "One-shot (don't add to world)",
        "Install without recording the package in @world. Use for dependencies or temporary installs so --depclean can remove them later.",
        Group::Update,
        false,
    ),
    sw(
        "--noreplace",
        Some('n'),
        "Skip already installed",
        "Do not rebuild packages that are already installed. Combine with targets to add them to @world.",
        Group::Update,
        false,
    ),
    sw(
        "--emptytree",
        Some('e'),
        "Empty tree (rebuild everything)",
        "Pretend nothing is installed and rebuild every dependency. Very long; used after toolchain changes.",
        Group::Update,
        true,
    ),
    yn(
        "--changed-deps",
        "Changed dependencies",
        "Rebuild installed packages whose dependencies changed in the repository since they were built.",
        Group::Update,
        true,
    ),
    yn("--changed-slot", "Changed slot", "Rebuild when the SLOT of an installed package changed in the repository.", Group::Update, true),
    yn("--with-test-deps", "Include test deps", "Pull in test dependencies for packages built with FEATURES=test.", Group::Update, true),
    sw("--newrepo", None, "New repository", "Rebuild packages when the repository they come from changed.", Group::Update, true),
    sw("--selective", None, "Selective", "Same as --noreplace for the targets.", Group::Update, true),
    sw("--update-if-installed", None, "Update only if installed", "Like --update, but skip targets that are not installed yet.", Group::Update, true),
    Spec {
        flag: "--exclude",
        short: None,
        label: "Exclude packages",
        help: "Never merge these atoms, even as dependencies (space separated).",
        kind: Kind::Atoms,
        group: Group::Update,
        advanced: false,
    },
    Spec {
        flag: "--reinstall-atoms",
        short: None,
        label: "Force reinstall of",
        help: "Treat these atoms as if they were not installed.",
        kind: Kind::Atoms,
        group: Group::Update,
        advanced: true,
    },
    Spec {
        flag: "--useoldpkg-atoms",
        short: None,
        label: "Prefer old binary for",
        help: "Prefer existing binary packages for these atoms even if they are outdated.",
        kind: Kind::Atoms,
        group: Group::Update,
        advanced: true,
    },
    // Resolver
    Spec {
        flag: "--backtrack",
        short: None,
        label: "Backtrack attempts",
        help: "How hard the resolver tries alternatives when it hits a conflict (default 10). Raise to 30–100 for stubborn slot conflicts.",
        kind: Kind::Number { min: 0, max: 1000, allow_plain: false },
        group: Group::Resolver,
        advanced: false,
    },
    sw("--complete-graph", None, "Complete graph", "Consider the whole installed system when checking for breakage after an update.", Group::Resolver, true),
    yn("--complete-graph-if-new-use", "Complete graph if new USE", "Enable complete graph when USE of an installed package changes.", Group::Resolver, true),
    yn(
        "--complete-graph-if-new-ver",
        "Complete graph if new version",
        "Enable complete graph when an installed package version changes.",
        Group::Resolver,
        true,
    ),
    yn(
        "--rebuild-if-new-slot",
        "Rebuild on new slot",
        "Rebuild packages with slot-operator deps when a dependency moves to a new slot (default: yes).",
        Group::Resolver,
        true,
    ),
    sw("--rebuild-if-new-rev", None, "Rebuild on new revision", "Rebuild packages with build-time deps that got a new revision.", Group::Resolver, true),
    sw("--rebuild-if-new-ver", None, "Rebuild on new version", "Rebuild packages with build-time deps that got a new version.", Group::Resolver, true),
    sw("--rebuild-if-unbuilt", None, "Rebuild if unbuilt", "Rebuild packages when build-time deps were rebuilt from source.", Group::Resolver, true),
    yn(
        "--dynamic-deps",
        "Dynamic deps",
        "Use dependency information from the repository instead of what was recorded at install time (default: yes).",
        Group::Resolver,
        true,
    ),
    yn(
        "--ignore-built-slot-operator-deps",
        "Ignore slot-operator deps",
        "Ignore recorded slot-operator dependencies (may break packages; debugging only).",
        Group::Resolver,
        true,
    ),
    sw("--nodeps", Some('O'), "No dependencies", "Merge only the named packages and ignore dependencies. Can break your system.", Group::Resolver, true),
    sw("--onlydeps", Some('o'), "Only dependencies", "Install only the dependencies of the targets, not the targets themselves.", Group::Resolver, false),
    yn("--onlydeps-with-rdeps", "Only deps incl. runtime", "With Only dependencies, also include runtime dependencies (default: yes).", Group::Resolver, true),
    sw("--verbose-conflicts", None, "Verbose conflicts", "Show every package involved in a slot conflict, not only a summary.", Group::Resolver, false),
    sw("--tree", Some('t'), "Show dependency tree", "Print the plan as a tree so you can see what pulls in what.", Group::Resolver, false),
    // Autounmask
    yn(
        "--autounmask",
        "Suggest unmask changes",
        "Let emerge suggest keyword and mask changes when the only candidates are masked. emerge's default is no, except that USE and license suggestions (below) default to yes.",
        Group::Autounmask,
        false,
    ),
    yn("--autounmask-use", "Suggest USE changes", "Allow USE changes in suggestions.", Group::Autounmask, true),
    yn("--autounmask-license", "Suggest license changes", "Allow license changes in suggestions.", Group::Autounmask, true),
    yn("--autounmask-keep-keywords", "Never suggest keywords", "Do not suggest accepting testing keywords.", Group::Autounmask, true),
    yn("--autounmask-keep-masks", "Never suggest unmasks", "Do not suggest package.unmask changes.", Group::Autounmask, true),
    yn(
        "--autounmask-backtrack",
        "Backtrack with autounmask",
        "Keep backtracking when autounmask changes are needed (can find better solutions).",
        Group::Autounmask,
        true,
    ),
    yn("--autounmask-unrestricted-atoms", "Unrestricted atoms", "Write suggestions using >= instead of = atoms.", Group::Autounmask, true),
    yn(
        "--autounmask-continue",
        "Apply and continue",
        "Write the suggested changes and continue automatically (PortageHub prefers you review them first).",
        Group::Autounmask,
        true,
    ),
    // Build
    Spec {
        flag: "--jobs",
        short: Some('j'),
        label: "Parallel packages (--jobs)",
        help: "How many packages may build at the same time. Each one still uses MAKEOPTS -j. Empty = unlimited when enabled.",
        kind: Kind::Number { min: 1, max: 64, allow_plain: true },
        group: Group::Build,
        advanced: false,
    },
    Spec {
        flag: "--load-average",
        short: None,
        label: "Load limit",
        help: "Do not start new package builds while the system load is above this value.",
        kind: Kind::Number { min: 1, max: 256, allow_plain: false },
        group: Group::Build,
        advanced: false,
    },
    sw(
        "--keep-going",
        None,
        "Keep going after failures",
        "Continue with the remaining packages when one fails, then report the failures at the end.",
        Group::Build,
        false,
    ),
    sw("--fetchonly", Some('f'), "Download only", "Fetch all source archives but do not build anything. Good for slow/metered networks.", Group::Build, false),
    sw("--fetch-all-uri", Some('F'), "Download all sources", "Fetch every SRC_URI regardless of USE flags.", Group::Build, true),
    yn(
        "--quiet-build",
        "Quiet build",
        "Hide compiler output (it is still written to the build log). PortageHub shows the build log live either way.",
        Group::Build,
        false,
    ),
    sw("--quiet-fail", None, "Quiet failures", "Do not print the full build log tail when a build fails.", Group::Build, true),
    sw("--skipfirst", None, "Skip first (with resume)", "When resuming, skip the package that failed first.", Group::Build, false),
    // Binary
    sw("--usepkg", Some('k'), "Use binary packages", "Use local binary packages from PKGDIR when available.", Group::Binary, false),
    sw("--usepkgonly", Some('K'), "Only binary packages", "Never build from source; fail if no binary package exists.", Group::Binary, true),
    sw("--getbinpkg", Some('g'), "Download binary packages", "Fetch binary packages from the binhosts in binrepos.conf.", Group::Binary, false),
    sw("--getbinpkgonly", Some('G'), "Only remote binaries", "Like --usepkgonly but downloading from binhosts.", Group::Binary, true),
    sw("--buildpkg", Some('b'), "Create binary packages", "Save a binary package of everything that gets built.", Group::Binary, false),
    sw("--buildpkgonly", Some('B'), "Build binaries only", "Create binary packages without installing them.", Group::Binary, true),
    yn("--binpkg-respect-use", "Respect USE for binaries", "Ignore binary packages built with different USE flags (default: yes).", Group::Binary, true),
    yn("--binpkg-changed-deps", "Binary changed deps", "Ignore binary packages whose dependencies changed.", Group::Binary, true),
    yn("--rebuilt-binaries", "Rebuilt binaries", "Replace installed packages with binary packages that were rebuilt.", Group::Binary, true),
    Spec {
        flag: "--usepkg-exclude",
        short: None,
        label: "No binaries for",
        help: "Always build these atoms from source.",
        kind: Kind::Atoms,
        group: Group::Binary,
        advanced: true,
    },
    Spec {
        flag: "--buildpkg-exclude",
        short: None,
        label: "Don't package",
        help: "Never create binary packages for these atoms.",
        kind: Kind::Atoms,
        group: Group::Binary,
        advanced: true,
    },
    // Output
    sw("--verbose", Some('v'), "Verbose", "Show USE flags, sizes and versions in the plan (PortageHub always adds this to previews).", Group::Output, false),
    sw("--quiet", Some('q'), "Quiet", "Reduce emerge's own output.", Group::Output, true),
    sw("--unordered-display", None, "Unordered display", "With the tree view, don't sort by merge order.", Group::Output, true),
    sw("--verbose-slot-rebuilds", None, "Verbose slot rebuilds", "Explain slot-operator rebuilds.", Group::Output, true),
    sw("--noconfmem", None, "Forget config choices", "Reinstall config files that you deleted before.", Group::Output, true),
    sw("--ignore-default-opts", None, "Ignore EMERGE_DEFAULT_OPTS", "Do not add the options from make.conf for this run.", Group::Output, false),
    sw("--nospinner", None, "No spinner", "Disable the progress spinner.", Group::Output, true),
    Spec {
        flag: "--color",
        short: None,
        label: "Colour",
        help: "Coloured output (PortageHub strips colours itself).",
        kind: Kind::Choice(&["y", "n"]),
        group: Group::Output,
        advanced: true,
    },
];

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    On,
    Yes,
    No,
    Num(Option<i64>),
    Text(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Install,
    Unmerge,
    Depclean,
    Prune,
    Deselect,
    Select,
    Sync,
    Resume,
    Info,
    Config,
    Regen,
    Metadata,
    CheckNews,
}

impl Action {
    pub const ALL: [Action; 13] = [
        Action::Install,
        Action::Depclean,
        Action::Unmerge,
        Action::Deselect,
        Action::Select,
        Action::Prune,
        Action::Sync,
        Action::Resume,
        Action::Info,
        Action::Config,
        Action::CheckNews,
        Action::Regen,
        Action::Metadata,
    ];

    pub fn flag(&self) -> Option<&'static str> {
        Some(match self {
            Action::Install => return None,
            Action::Unmerge => "--unmerge",
            Action::Depclean => "--depclean",
            Action::Prune => "--prune",
            Action::Deselect => "--deselect",
            Action::Select => "--noreplace",
            Action::Sync => "--sync",
            Action::Resume => "--resume",
            Action::Info => "--info",
            Action::Config => "--config",
            Action::Regen => "--regen",
            Action::Metadata => "--metadata",
            Action::CheckNews => "--check-news",
        })
    }

    pub fn label(&self) -> &'static str {
        match self {
            Action::Install => "Install / update",
            Action::Unmerge => "Unmerge (force remove)",
            Action::Depclean => "Depclean (safe remove)",
            Action::Prune => "Prune old versions",
            Action::Deselect => "Remove from @world",
            Action::Select => "Add to @world",
            Action::Sync => "Sync repositories",
            Action::Resume => "Resume last emerge",
            Action::Info => "System info",
            Action::Config => "Run pkg_config",
            Action::Regen => "Regenerate cache",
            Action::Metadata => "Transfer metadata",
            Action::CheckNews => "Check news",
        }
    }

    pub fn help(&self) -> &'static str {
        match self {
            Action::Install => "Install the targets and whatever they need. With Update/Deep/New USE it is the standard way to update the system.",
            Action::Unmerge => "Remove the named packages without checking whether something still needs them. Prefer Depclean.",
            Action::Depclean => "Remove the named packages (or, with no targets, everything not needed by @world) — but only if nothing depends on them.",
            Action::Prune => "Remove all but the highest installed version of the targets.",
            Action::Deselect => "Remove targets from the world file without uninstalling them; depclean can then clean them up.",
            Action::Select => "Record already-installed targets in the world file (emerge --noreplace).",
            Action::Sync => "Update the repositories (all with auto-sync, or only the named ones).",
            Action::Resume => "Continue an interrupted or failed emerge where it stopped.",
            Action::Info => "Print emerge --info, the information bug reports ask for.",
            Action::Config => "Run the package's post-install configuration step (pkg_config).",
            Action::Regen => "Regenerate the metadata cache for repositories without one (overlays).",
            Action::Metadata => "Copy repository metadata into Portage's cache.",
            Action::CheckNews => "Check for unread news items.",
        }
    }

    pub fn needs_targets(&self) -> bool {
        matches!(self, Action::Install | Action::Unmerge | Action::Prune | Action::Deselect | Action::Select | Action::Config)
    }

    /// Does this action change the system (and therefore need a preview/confirm)?
    pub fn previewable(&self) -> bool {
        matches!(self, Action::Install | Action::Unmerge | Action::Depclean | Action::Prune | Action::Resume)
    }

    pub fn dangerous(&self) -> bool {
        matches!(self, Action::Unmerge | Action::Prune)
    }
}

#[derive(Debug, Clone)]
pub struct EmergeOptions {
    pub action: Action,
    pub targets: Vec<String>,
    pub values: BTreeMap<&'static str, Value>,
    /// Extra environment such as FEATURES or MAKEOPTS overrides.
    pub env: Vec<(String, String)>,
    pub extra_args: String,
}

impl Default for EmergeOptions {
    fn default() -> Self {
        EmergeOptions { action: Action::Install, targets: Vec::new(), values: BTreeMap::new(), env: Vec::new(), extra_args: String::new() }
    }
}

impl EmergeOptions {
    pub fn with(mut self, flag: &'static str, v: Value) -> Self {
        self.values.insert(flag, v);
        self
    }

    pub fn targets(mut self, t: &[&str]) -> Self {
        self.targets = t.iter().map(|s| s.to_string()).collect();
        self
    }

    /// Build argv (without the program name).
    pub fn args(&self) -> Vec<String> {
        let mut shorts = String::new();
        let mut longs: Vec<String> = Vec::new();
        if let Some(f) = self.action.flag() {
            longs.push(f.to_string());
        }
        for s in SPECS {
            let Some(v) = self.values.get(s.flag) else { continue };
            match (s.kind, v) {
                (Kind::Switch, Value::On) => match s.short {
                    Some(c) => shorts.push(c),
                    None => longs.push(s.flag.to_string()),
                },
                (Kind::YesNo, Value::Yes) => longs.push(format!("{}=y", s.flag)),
                (Kind::YesNo, Value::No) => longs.push(format!("{}=n", s.flag)),
                (Kind::Number { allow_plain, .. }, Value::Num(n)) => match n {
                    Some(n) => longs.push(format!("{}={n}", s.flag)),
                    None if allow_plain => longs.push(s.flag.to_string()),
                    None => {}
                },
                (Kind::Atoms, Value::Text(t)) if !t.trim().is_empty() => {
                    for a in t.split_whitespace() {
                        longs.push(format!("{}={a}", s.flag));
                    }
                }
                (Kind::Choice(_), Value::Text(t)) if !t.is_empty() => longs.push(format!("{}={t}", s.flag)),
                _ => {}
            }
        }
        let mut out = Vec::new();
        if !shorts.is_empty() {
            out.push(format!("-{shorts}"));
        }
        out.extend(longs);
        out.extend(self.extra_args.split_whitespace().map(String::from));
        out.extend(self.targets.iter().cloned());
        out
    }

    /// Arguments for a non-interactive preview run.
    pub fn pretend_args(&self) -> Vec<String> {
        let mut a = vec!["--pretend".to_string(), "--verbose".into(), "--color=n".into(), "--nospinner".into(), "--ask=n".into()];
        a.extend(self.args());
        a
    }

    /// Arguments for the real run (no prompt; we already reviewed the plan).
    pub fn run_args(&self) -> Vec<String> {
        let mut a = vec!["--color=n".to_string(), "--nospinner".into(), "--ask=n".into()];
        a.extend(self.args());
        a
    }

    pub fn command_line(&self) -> String {
        let mut s = String::new();
        for (k, v) in &self.env {
            if !v.is_empty() {
                s.push_str(&format!("{k}=\"{v}\" "));
            }
        }
        s.push_str("emerge");
        let args = self.args();
        let interactive = self.action.previewable() || self.action == Action::Sync;
        if interactive && self.action != Action::Sync {
            s.push_str(" --ask");
        }
        for a in args {
            s.push(' ');
            s.push_str(&a);
        }
        s
    }
}

/// Ready-made recipes for common tasks.
pub struct Recipe {
    pub title: &'static str,
    pub subtitle: &'static str,
    pub icon: &'static str,
    pub build: fn() -> EmergeOptions,
    pub recommended: bool,
}

pub fn recipes() -> Vec<Recipe> {
    use egui_phosphor::regular as i;
    vec![
        Recipe {
            title: "Update everything",
            subtitle: "emerge -uDN @world — the standard full system update",
            icon: i::ROCKET_LAUNCH,
            recommended: true,
            build: || {
                EmergeOptions::default()
                    .with("--update", Value::On)
                    .with("--deep", Value::On)
                    .with("--newuse", Value::On)
                    .with("--with-bdeps", Value::Yes)
                    .targets(&["@world"])
            },
        },
        Recipe {
            title: "Quick update",
            subtitle: "emerge -u @world — only packages you selected",
            icon: i::LIGHTNING,
            recommended: false,
            build: || EmergeOptions::default().with("--update", Value::On).targets(&["@world"]),
        },
        Recipe {
            title: "Apply USE changes",
            subtitle: "emerge -uDU @world — rebuild what your USE edits affect",
            icon: i::TOGGLE_RIGHT,
            recommended: false,
            build: || EmergeOptions::default().with("--update", Value::On).with("--deep", Value::On).with("--changed-use", Value::On).targets(&["@world"]),
        },
        Recipe {
            title: "Rebuild preserved libraries",
            subtitle: "emerge @preserved-rebuild — fix packages using old libraries",
            icon: i::WRENCH,
            recommended: false,
            build: || EmergeOptions::default().targets(&["@preserved-rebuild"]),
        },
        Recipe {
            title: "Remove unneeded packages",
            subtitle: "emerge --depclean — clean orphaned dependencies",
            icon: i::BROOM,
            recommended: false,
            build: || EmergeOptions { action: Action::Depclean, ..Default::default() },
        },
        Recipe {
            title: "Download sources now",
            subtitle: "emerge -uDNf @world — fetch everything, build later",
            icon: i::CLOUD_ARROW_DOWN,
            recommended: false,
            build: || {
                EmergeOptions::default()
                    .with("--update", Value::On)
                    .with("--deep", Value::On)
                    .with("--newuse", Value::On)
                    .with("--fetchonly", Value::On)
                    .targets(&["@world"])
            },
        },
        Recipe {
            title: "Resume interrupted emerge",
            subtitle: "emerge --resume — continue where it stopped",
            icon: i::PLAY,
            recommended: false,
            build: || EmergeOptions { action: Action::Resume, ..Default::default() },
        },
        Recipe {
            title: "Rebuild everything",
            subtitle: "emerge -e @world — after compiler/toolchain changes (very long)",
            icon: i::ARROWS_COUNTER_CLOCKWISE,
            recommended: false,
            build: || EmergeOptions::default().with("--emptytree", Value::On).targets(&["@world"]),
        },
    ]
}

pub const COMMON_SETS: &[(&str, &str)] = &[
    ("@world", "Everything you selected plus @system and @profile"),
    ("@selected", "Packages and sets listed in the world file"),
    ("@system", "Core packages required by your profile"),
    ("@preserved-rebuild", "Packages linked against preserved old libraries"),
    ("@module-rebuild", "External kernel modules (rebuild after a kernel update)"),
    ("@x11-module-rebuild", "X server modules"),
    ("@live-rebuild", "Installed live (9999) packages"),
    ("@security", "Packages affected by unfixed GLSAs"),
    ("@installed", "Every installed package"),
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn argv() {
        let o = (recipes()[0].build)();
        assert_eq!(o.args(), vec!["-uDN", "--with-bdeps=y", "@world"]);
        let o = EmergeOptions::default()
            .with("--jobs", Value::Num(Some(4)))
            .with("--exclude", Value::Text("a/b c/d".into()))
            .with("--oneshot", Value::On)
            .targets(&["app-misc/foo"]);
        assert_eq!(o.args(), vec!["-1", "--exclude=a/b", "--exclude=c/d", "--jobs=4", "app-misc/foo"]);
        assert_eq!(o.pretend_args()[0], "--pretend");
        let o = EmergeOptions { action: Action::Depclean, ..Default::default() };
        assert_eq!(o.args(), vec!["--depclean"]);
        assert_eq!(o.command_line(), "emerge --ask --depclean");
    }
}
