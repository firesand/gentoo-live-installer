//! Command-line interface — handy for scripting rebuilds after `emerge -u`.

use crate::runner::{parse_line, Event};
use anyhow::{bail, Context, Result};
use kernelsmith_core::hw::{self, SystemInfo};
use kernelsmith_core::kconfig::scan_symbols;
use kernelsmith_core::plan::{self, BuildPlan, Level};
use kernelsmith_core::profiles::{self, ProfileId};
use kernelsmith_core::slim::SlimMode;
use kernelsmith_core::sources::{self, KernelSource};
use kernelsmith_core::util::{human_bytes, is_root, which};
use kernelsmith_core::patches::{self, Fetcher, PatchStatus, PlannedPatch};
use kernelsmith_core::{job, library, tweaks};
use std::io::{BufRead, BufReader, IsTerminal};
use std::path::PathBuf;
use std::process::{Command, Stdio};

pub const USAGE: &str = "\
Kernelsmith — guided custom kernels for Gentoo

USAGE:
    kernelsmith                     Open the graphical app
    kernelsmith detect [--json]     Show detected hardware and system setup
    kernelsmith sources             List kernel source trees in /usr/src
    kernelsmith list                List installed kernels
    kernelsmith plan [OPTIONS]      Prepare a build job and print the script path
    kernelsmith build [OPTIONS]     Prepare and run a build (asks for your password)
    kernelsmith run-job SCRIPT [--log FILE]   Run a prepared job script (used internally)

OPTIONS for plan/build:
    --profile NAME      gaming | desktop | creator | workstation | laptop | server
    --preset FILE       Start from a saved preset (~/.config/kernelsmith/presets/*.toml)
    --source PATH       Kernel source tree (default: newest full source tree)
    --latest            Use the newest source tree even if the preset names another
    --name NAME         Kernel name (becomes -ks-NAME)
    --set KEY=VALUE     Override an option, e.g. --set hz=500 --set scheduler=eevdf
    --patch ID          Add a catalog patch (bore, prjc, acpi_call, handheld, …)
    --patch-file FILE   Add your own patch file
    --slim MODE         off | balanced | aggressive
    --no-install        Only build; don't install
    --build-root DIR    Put the build tree in DIR instead of /usr/src (build-only)
    --print             Print the generated script
    --save FILE         Save the resulting plan as a preset
";

struct Opts {
    profile: Option<ProfileId>,
    preset: Option<PathBuf>,
    source: Option<PathBuf>,
    latest: bool,
    name: Option<String>,
    sets: Vec<(String, String)>,
    slim: Option<SlimMode>,
    no_install: bool,
    build_root: Option<PathBuf>,
    print: bool,
    save: Option<PathBuf>,
    patches: Vec<String>,
    patch_files: Vec<PathBuf>,
}

fn parse_opts(args: &[String]) -> Result<Opts> {
    let mut o = Opts {
        profile: None,
        preset: None,
        source: None,
        latest: false,
        name: None,
        sets: vec![],
        slim: None,
        no_install: false,
        build_root: None,
        print: false,
        save: None,
        patches: vec![],
        patch_files: vec![],
    };
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let mut val = || it.next().cloned().with_context(|| format!("{a} needs a value"));
        match a.as_str() {
            "--profile" => {
                let v = val()?;
                o.profile = Some(
                    profiles::ALL
                        .into_iter()
                        .find(|p| kernelsmith_core::script::profile_slug(*p) == v)
                        .with_context(|| format!("unknown profile {v}"))?,
                );
            }
            "--preset" => o.preset = Some(val()?.into()),
            "--source" => o.source = Some(val()?.into()),
            "--latest" => o.latest = true,
            "--name" => o.name = Some(val()?),
            "--set" => {
                let v = val()?;
                let (k, x) = v.split_once('=').with_context(|| format!("--set expects KEY=VALUE, got {v}"))?;
                o.sets.push((k.into(), x.into()));
            }
            "--slim" => {
                o.slim = Some(match val()?.as_str() {
                    "off" => SlimMode::Off,
                    "balanced" => SlimMode::Balanced,
                    "aggressive" => SlimMode::Aggressive,
                    v => bail!("unknown slim mode {v}"),
                })
            }
            "--no-install" => o.no_install = true,
            "--build-root" => o.build_root = Some(val()?.into()),
            "--print" => o.print = true,
            "--save" => o.save = Some(val()?.into()),
            "--patch" => o.patches.push(val()?),
            "--patch-file" => o.patch_files.push(val()?.into()),
            other => bail!("unknown option {other}\n\n{USAGE}"),
        }
    }
    Ok(o)
}

pub fn detect(json: bool) -> Result<()> {
    let s = hw::detect();
    if json {
        println!("{}", serde_json::to_string_pretty(&s)?);
        return Ok(());
    }
    println!("System      {} ({})", s.hostname, if s.gentoo { "Gentoo" } else { "not Gentoo" });
    println!("CPU         {} — {} cores / {} threads, {}{}", s.cpu.model_name, s.cpu.cores, s.cpu.threads, s.cpu.uarch_label(), if s.cpu.hybrid { ", hybrid" } else { "" });
    println!("Memory      {:.1} GiB", s.memory_gib());
    for g in &s.gpus {
        println!("GPU         {} (driver: {})", g.name, g.driver.as_deref().unwrap_or("none"));
    }
    for d in &s.storage {
        println!("Disk        {} {} {:?} {}", d.name, d.model, d.kind, human_bytes(d.size_bytes));
    }
    println!("Root fs     {} on {}", s.root_fs, s.root_device);
    println!("Init        {}", s.init.label());
    println!("Boot        {} + {} initramfs{}", s.boot.bootloader.label(), s.boot.initramfs.label(), if s.boot.uefi { " (UEFI)" } else { "" });
    if let Some((v, u)) = &s.boot.installkernel {
        println!("installkernel {v} [{}]", u.join(" "));
    }
    println!("Running     {}{}", s.running_kernel, if s.proc_config { " (/proc/config.gz available)" } else { "" });
    println!("Compilers   gcc {} | {}", s.toolchain.gcc.as_deref().unwrap_or("missing"), s.toolchain.clang.as_deref().unwrap_or("clang missing"));
    println!("Modules     {} loaded{}", s.loaded_modules.len(), if s.modprobed_db.is_some() { ", modprobed-db present" } else { "" });
    if !s.external_modules.is_empty() {
        let names: Vec<_> = s.external_modules.iter().map(|p| p.atom.as_str()).collect();
        println!("Out-of-tree {}", names.join(", "));
    }
    println!("Suggested   {} profile", profiles::get(profiles::recommend(&s)).title);
    Ok(())
}

pub fn list_sources() -> Result<()> {
    let list = sources::discover();
    if list.is_empty() {
        println!("No kernel sources in /usr/src. Try: emerge sys-kernel/gentoo-sources");
    }
    for s in list {
        println!(
            "{:<40} {:<10} {:<18}{}{}",
            s.dir_name,
            s.display(),
            s.flavor.label(),
            if s.is_current_symlink { " [current]" } else { "" },
            if s.buildable { "" } else { " (headers only — cannot build)" }
        );
    }
    Ok(())
}

pub fn list_kernels() -> Result<()> {
    let sys = hw::detect();
    for k in library::discover(&sys) {
        println!("{:<36} {}{}", k.release, k.origin_label(), if k.running { "  ← running" } else { "" });
    }
    Ok(())
}

fn choose_source(o: &Opts, plan_source: Option<PathBuf>) -> Result<KernelSource> {
    let path = if o.latest { None } else { o.source.clone().or(plan_source) };
    match path {
        Some(p) => sources::inspect(&p, false).with_context(|| format!("{} is not a kernel source tree", p.display())),
        None => sources::discover()
            .into_iter()
            .filter(|s| s.buildable && s.flavor != sources::Flavor::Kernelsmith)
            .min_by_key(|s| (std::cmp::Reverse(s.version_tuple()), s.recommended_rank()))
            .context("No full kernel source tree found in /usr/src. Install one first, e.g.: emerge sys-kernel/gentoo-sources"),
    }
}

fn make_plan(o: &Opts, sys: &SystemInfo) -> Result<(BuildPlan, KernelSource)> {
    let preset = o.preset.as_ref().map(|p| BuildPlan::load(p)).transpose()?;
    let src = choose_source(o, preset.as_ref().map(|p| p.source.clone()))?;
    let mut plan = match preset {
        Some(mut p) => {
            p.source = src.path.clone();
            p
        }
        None => BuildPlan::new(sys, o.profile.unwrap_or_else(|| profiles::recommend(sys)), &src),
    };
    if let (Some(p), true) = (o.profile, o.preset.is_some()) {
        plan.apply_profile(sys, p);
    }
    if let Some(n) = &o.name {
        plan.name = n.clone();
    }
    if let Some(s) = o.slim {
        plan.slim = s;
    }
    for (k, v) in &o.sets {
        let t = tweaks::find(k).with_context(|| format!("unknown option '{k}'"))?;
        if t.choice(v).is_none() {
            let ids: Vec<_> = t.choices.iter().map(|c| c.id).collect();
            bail!("'{v}' is not a valid value for {k} (choose from: {})", ids.join(", "));
        }
        plan.tweaks.insert(k.clone(), v.clone());
    }
    if o.no_install || o.build_root.is_some() {
        plan.install.install = false;
        plan.install.module_rebuild = false;
        plan.install.set_symlink = false;
        plan.install.runtime_tuning = false;
    }
    plan.build_root = o.build_root.clone();
    let fetcher = Fetcher::default();
    for id in &o.patches {
        let info = patches::find(id).with_context(|| format!("unknown patch '{id}'"))?;
        let symbols = scan_symbols(&src.path);
        match patches::resolve(&info, &src, Some(&symbols), &fetcher) {
            PatchStatus::Included => eprintln!("  {} is already included in this source", info.title),
            PatchStatus::Available { url, file_name } => {
                let file = fetcher.download(&url, &src.major_minor(), &file_name)?;
                eprintln!("  {} → {}", info.title, file.display());
                plan.patches.push(PlannedPatch { id: id.clone(), title: info.title.into(), file, url: Some(url) });
            }
            PatchStatus::NotPublished(why) | PatchStatus::Error(why) => bail!("{}: {why}", info.title),
        }
    }
    for f in &o.patch_files {
        let name = f.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        plan.patches.push(PlannedPatch { id: format!("local:{name}"), title: name, file: f.clone(), url: None });
    }
    Ok((plan, src))
}

pub fn plan_or_build(args: &[String], run: bool) -> Result<i32> {
    let o = parse_opts(args)?;
    let sys = hw::detect();
    let (mut plan, src) = make_plan(&o, &sys)?;
    eprintln!("Scanning {} …", src.path.display());
    let symbols = scan_symbols(&src.path);
    let provided = plan.provided_symbols();
    let notes = tweaks::normalize(&mut plan.tweaks, &sys, Some(&symbols), &provided);
    for n in notes {
        eprintln!("  note: {n}");
    }
    let findings = plan::check(&plan, &sys, Some(&src));
    let mut errors = 0;
    for f in &findings {
        let tag = match f.level {
            Level::Error => {
                errors += 1;
                "ERROR"
            }
            Level::Warn => "warn ",
            Level::Info => "info ",
        };
        eprintln!("  {tag} {}", f.text);
    }
    if let Some(path) = &o.save {
        plan.save(path)?;
        eprintln!("Saved preset to {}", path.display());
    }
    if errors > 0 {
        bail!("{errors} problem(s) must be fixed first");
    }
    let job = job::prepare_build(&plan, &sys, &src, Some(&symbols))?;
    if o.print {
        print!("{}", std::fs::read_to_string(&job.script)?);
    }
    let est = plan::estimate(&plan, &sys);
    eprintln!(
        "Prepared {} — about {}–{} min, {} disk.\nScript: {}",
        job.release.as_deref().unwrap_or("?"),
        est.minutes_low,
        est.minutes_high,
        human_bytes(est.disk_bytes),
        job.script.display()
    );
    if !run {
        return Ok(0);
    }
    run_in_terminal(&job.script, &job.log, plan.needs_root())
}

/// Run a job in this terminal, pretty-printing the progress markers.
fn run_in_terminal(script: &std::path::Path, log: &std::path::Path, needs_root: bool) -> Result<i32> {
    let exe = std::env::current_exe()?;
    let mut cmd = if needs_root && !is_root() {
        let tty_helper = ["sudo", "doas"].into_iter().find(|h| which(h).is_some());
        if let (true, Some(helper)) = (std::io::stdin().is_terminal(), tty_helper) {
            let mut c = Command::new(helper);
            c.arg(&exe);
            c
        } else {
            let mut c = Command::new("pkexec");
            c.arg(&exe);
            c
        }
    } else {
        Command::new(&exe)
    };
    cmd.arg("run-job").arg(script).arg("--log").arg(log).stdout(Stdio::piped());
    let mut child = cmd.spawn()?;
    let color = std::io::stdout().is_terminal();
    let (bold, dim, red, yellow, green, reset) =
        if color { ("\x1b[1;36m", "\x1b[2m", "\x1b[1;31m", "\x1b[33m", "\x1b[1;32m", "\x1b[0m") } else { ("", "", "", "", "", "") };
    let mut misses = vec![];
    if let Some(out) = child.stdout.take() {
        for line in BufReader::new(out).lines().map_while(Result::ok) {
            match parse_line(&line) {
                Event::Stage { label, .. } => println!("{bold}==> {label}{reset}"),
                Event::Note(n) => println!("  • {n}"),
                Event::Warn(w) => println!("{yellow}  ! {w}{reset}"),
                Event::Error(e) => println!("{red}  ✖ {e}{reset}"),
                Event::Verify { ok: false, sym, want, got, title } => misses.push(format!("CONFIG_{sym}: wanted {want}, got {got} ({title})")),
                Event::Verify { .. } | Event::Plan(_) | Event::ModRebuild(_) => {}
                Event::ConfigStats { y, m } => println!("  • Configuration: {y} built-in, {m} modules"),
                Event::Done(r) => println!("{green}✔ Finished: {r}{reset}"),
                Event::Profile(p) => println!("  • Profile written to {}", p.display()),
                Event::Line(l) => println!("{dim}{l}{reset}"),
            }
        }
    }
    if !misses.is_empty() {
        println!("{yellow}Options that did not take effect (dependencies not met):{reset}");
        for m in misses {
            println!("    {m}");
        }
    }
    Ok(child.wait()?.code().unwrap_or(1))
}
