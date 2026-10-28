//! Kernelsmith — a guided, hardware-aware custom kernel builder for Gentoo.

mod cli;
mod gui;
mod helper;
mod runner;

use std::path::PathBuf;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = match args.first().map(String::as_str) {
        None | Some("gui") => match gui::run() {
            Ok(()) => 0,
            Err(e) => {
                eprintln!("kernelsmith: {e}");
                1
            }
        },
        Some("run-job") => {
            let Some(script) = args.get(1).map(PathBuf::from) else {
                eprintln!("usage: kernelsmith run-job SCRIPT [--log FILE] [--cancel-on-eof]");
                std::process::exit(2);
            };
            let log = args.iter().position(|a| a == "--log").and_then(|i| args.get(i + 1)).map(PathBuf::from);
            let cancel_on_eof = args.iter().any(|a| a == "--cancel-on-eof");
            helper::run_job(&script, log.as_deref(), cancel_on_eof)
        }
        Some("detect") => report(cli::detect(args.iter().any(|a| a == "--json"))),
        Some("sources") => report(cli::list_sources()),
        Some("list") => report(cli::list_kernels()),
        Some("plan") => report_code(cli::plan_or_build(&args[1..], false)),
        Some("build") => report_code(cli::plan_or_build(&args[1..], true)),
        Some("-h" | "--help" | "help") => {
            print!("{}", cli::USAGE);
            0
        }
        Some("--version" | "-V") => {
            println!("kernelsmith {}", env!("CARGO_PKG_VERSION"));
            0
        }
        Some(other) => {
            eprintln!("kernelsmith: unknown command '{other}'\n\n{}", cli::USAGE);
            2
        }
    };
    std::process::exit(code);
}

fn report(r: anyhow::Result<()>) -> i32 {
    report_code(r.map(|()| 0))
}

fn report_code(r: anyhow::Result<i32>) -> i32 {
    match r {
        Ok(c) => c,
        Err(e) => {
            eprintln!("kernelsmith: {e:#}");
            1
        }
    }
}
