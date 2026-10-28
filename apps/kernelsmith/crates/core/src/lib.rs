//! Kernelsmith core: everything except the user interface.
//!
//! * [`hw`] scans the machine (CPU, GPU, disks, init system, bootloader, Portage).
//! * [`sources`] finds kernel trees in /usr/src; [`kconfig`] reads configs and symbols.
//! * [`tweaks`], [`profiles`], [`slim`], [`requirements`] and [`patches`] describe choices.
//! * [`plan`] holds a saveable build plan; [`script`] and [`job`] turn it into a root script.
//! * [`library`], [`autofdo`], [`portage`] and [`tuning`] cover life after the build.

pub mod autofdo;
pub mod hw;
pub mod job;
pub mod kconfig;
pub mod library;
pub mod patches;
pub mod paths;
pub mod plan;
pub mod portage;
pub mod profiles;
pub mod requirements;
pub mod script;
pub mod slim;
pub mod sources;
pub mod tuning;
pub mod tweaks;
pub mod util;
