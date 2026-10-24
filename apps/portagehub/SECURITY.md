# Security model

PortageHub runs as your user. Everything that needs root goes through
`portagehub-helper`, a small program started with `pkexec` the first time you
do something privileged. It stays until PortageHub exits (and any emerge it
started has finished).

## The helper can do what Portage can

Whatever can talk to the helper can make Portage run code as root. That is
what a package manager is for: adding an overlay and installing from it,
setting `PORTAGE_IONICE_COMMAND` in make.conf, or writing an
`/etc/portage/env` file are all ways to run commands as root, and PortageHub
offers all of them. The helper's checks (below) keep requests within what the
GUI offers and make mistakes harder. They do not make the helper safe to hand
to an untrusted program. **The security boundary is who can talk to the
helper.**

## Who can talk to the helper

- **A password for every helper start.** The polkit action
  `org.portagehub.helper` uses `auth_admin`, not `auth_admin_keep`. There is no
  "remembered for a few minutes" window in which another program of your
  session could start its own helper without a prompt.
- **A private channel.** The helper's stdin and stdout are one end of a
  socketpair whose other end only the PortageHub process holds. Unlike a pipe,
  a socket cannot be reopened through `/proc/<pid>/fd` (the open fails with
  `ENXIO`), so other processes of your user cannot inject requests. Taking the
  descriptor with `pidfd_getfd()` or `ptrace()` needs ptrace permission. Keep
  `kernel.yama.ptrace_scope` at 1 or higher: with 0, any of your processes can
  debug PortageHub itself anyway.
- **Peer checks.** As root, the helper refuses to start unless stdin and
  stdout are the same socket, the process at the other end belongs to the user
  pkexec authenticated (`PKEXEC_UID`, `SUDO_UID` or `DOAS_USER`), and that
  process is the one that started the helper.

## What the helper checks anyway

- **Programs:** a fixed list of Portage tools, looked up in fixed directories
  (never `$PATH`), always run in `/`.
  - Options that point emerge or other Portage tools at another configuration,
    root or repository set are refused: `--config-root`, `--root`,
    `--sysroot`, `--prefix`, `--repositories-configuration`, `--portdir*` and
    `--cache-dir`. Abbreviations of them are refused too, because argparse
    would expand them.
  - Path arguments are refused, and eselect is limited to its profile, news,
    kernel and repository modules.
  - Only `FEATURES`, `MAKEOPTS`, `NOCOLOR`, `COLUMNS` and `TERM` are passed
    through the environment.
- **Files:** writes go only below `/etc/portage`, plus a few operations of
  fixed shape:
  - accepting or discarding `._cfg` updates below `/etc`;
  - known-good kernel state in `/var/lib/kernel-fallback` or
    `/var/lib/portagehub/kernels`, and backups in `/boot/kernel-backup`;
  - removing files of uninstalled kernels and leftover build directories.

  Every path is resolved with
  `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV)`
  below a fixed base directory. The base may itself be a symlink, for
  example `/etc/portage` linked to a git checkout: it is root's choice and is
  resolved first.
  Neither `..` nor a symlink can lead a write out of its tree, which includes
  the legitimate `make.profile` link. A symlink at the target is replaced,
  never written through. Renames and deletions use the resolved parent
  directories. Recursive deletions never follow a link or cross into another
  filesystem.
- **Hooks:** the helper never writes the files Portage runs by itself
  (`bashrc`, `postsync.d`, `repo.postsync.d`, `profile/profile.bashrc`). It
  also never creates `._cfg` names, which could later be "accepted" into
  place.

## Recovery and audit

- **Undo.** Before a file is changed, its previous state is saved in the
  journal: content, mode, or the fact that it did not exist.
  - The journal lives in `/var/lib/portagehub/journal` (mode 0700, the newest
    50 changes up to 50 MB).
  - A batch of changes is all-or-nothing: if a step fails, the earlier steps
    are rolled back.
  - Any change can be undone later from History → PortageHub changes. Undo is
    refused if the files were edited again since, unless you confirm.
- **Audit log.** Every request and how it ended is appended to
  `/var/log/portagehub/audit.log`, one JSON object per line. Each entry has
  the time, the requesting uid and the operation. File contents are never
  logged. The log is world-readable, like `/etc/portage` itself, and is
  rotated at 8 MB.

## Testing

`cargo test` drives the real helper over a socketpair in a scratch tree
(`PORTAGEHUB_ROOT`, which the helper ignores when running as root). It covers:

- symlink and `..` escapes;
- byte-for-byte rollback, and undo of saved changes;
- the kernel and cleanup operations;
- the peer check (`PORTAGEHUB_STRICT_PEER=1` turns it on without root);
- that `/proc/<pid>/fd/0` of the helper cannot be opened.

## Reporting a problem

Please email firesand@gmail.com rather than opening a public issue.
