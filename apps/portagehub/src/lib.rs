//! Shared pieces between the GUI (`portagehub`) and the privileged helper
//! (`portagehub-helper`): the JSON-lines protocol, process streaming, the
//! request handler and what it relies on (policy, confined file access,
//! transactions and the audit log).

pub mod audit;
pub mod emerge_opts;
pub mod journal;
pub mod policy;
pub mod protocol;
pub mod rootops;
pub mod safefs;
pub mod server;
pub mod stream;
