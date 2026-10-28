//! Shared core of Gentoo Settings, used by the GUI (`gentoo-settings`) and the
//! privileged helper (`gentoo-settings-helper`):
//! - [`detect`]: init system, hardware, tools and kernel features;
//! - [`catalog`]: the tweaks, ported from CachyOS-Settings, with an OpenRC and
//!   a systemd implementation where both make sense;
//! - [`managed`]: the marker blocks that make up the files we own;
//! - [`plan`]: turning a set of changes into file edits and commands;
//! - [`status`]: what is applied, what is live, what overrides it;
//! - [`apply`]: carrying out a plan as root (helper side);
//! - [`protocol`] and [`history`]: what the GUI and the helper exchange and keep.

pub mod apply;
pub mod catalog;
pub mod detect;
pub mod history;
pub mod managed;
pub mod plan;
pub mod protocol;
pub mod status;
pub mod sysctl;

/// Directories programs are looked up in (never `$PATH`).
pub const PROGRAM_DIRS: &[&str] = &["/usr/bin", "/usr/sbin", "/bin", "/sbin"];

/// Where the helper keeps its state, backups and history (below the root).
pub const STATE_DIR: &str = "/var/lib/gentoo-settings";
