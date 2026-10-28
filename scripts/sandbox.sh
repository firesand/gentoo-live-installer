#!/usr/bin/env bash
# Run one command of this project inside a bubblewrap sandbox, so that nothing it does can change the host system.
#
# Usage: scripts/sandbox.sh [--net] [--kvm] [--writable DIR]... [--] COMMAND [ARGUMENT]...
#
# Inside the sandbox:
#   * the host's file system is read-only. Only this checkout (the repository or worktree that holds this script), each
#     --writable DIR, fresh empty tmpfs mounts on /run and $HOME, and fresh empty /tmp and /var/tmp can be written.
#     /tmp and /var/tmp are directories on disk below the checkout's .cache/sandbox-tmp, not RAM: eleven parallel test
#     runs with RAM-backed /tmp once filled the PC's memory. Everything written there is removed when the command ends
#     (and a run that was killed is cleaned up by the next one). From a git worktree, the main checkout is visible
#     read-only (its .venv).
#   * /dev holds only null, zero, full, random, urandom, tty, shm and a private pts: no disk, partition, GPU or input
#     device exists, so no command can read or write a host disk. --kvm adds /dev/kvm for the VM harness.
#   * there is no network unless --net is given (the VM harness and a first `cargo fetch` need it).
#   * D-Bus, polkit, the Docker socket and the user's agents (all below /run or $HOME) are out of reach, every
#     capability is dropped and no_new_privs is set, so sudo, pkexec, su and the PortageHub helper cannot gain root.
#   * every namespace is private (user, mount, pid, ipc, uts, cgroup, network) and the command dies with this script.
#   * it runs at the lowest CPU and I/O priority and never on the CPUs kept for the user (scripts/low-priority.sh), so
#     the PC stays usable while tests and builds run.
#
# Cargo keeps its downloads in .cache/cargo-home inside the checkout (CARGO_HOME), never in the user's ~/.cargo.
# GENTOO_INSTALLER_SANDBOX=1 is set inside, so scripts can tell that they already run in it.
# Exit status: the command's own, 2 for a usage error, 127 when bubblewrap is missing.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
net=0
kvm=0
writable=()
while (($#)); do
	case $1 in
		--net) net=1 ;;
		--kvm) kvm=1 ;;
		--writable)
			(($# >= 2)) || { echo "sandbox.sh: --writable needs a directory" >&2; exit 2; }
			[[ -d $2 ]] || { echo "sandbox.sh: --writable $2: no such directory" >&2; exit 2; }
			writable+=("$(cd "$2" && pwd -P)")
			shift
			;;
		--) shift; break ;;
		-h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		-*) echo "sandbox.sh: unknown option: $1" >&2; exit 2 ;;
		*) break ;;
	esac
	shift
done
(($#)) || { echo "usage: scripts/sandbox.sh [--net] [--kvm] [--writable DIR]... [--] COMMAND [ARGUMENT]..." >&2; exit 2; }
command -v bwrap >/dev/null 2>&1 || { echo "sandbox.sh: bubblewrap is not installed (emerge sys-apps/bubblewrap)" >&2; exit 127; }

home=${HOME:-}
[[ -n $home && $home != / ]] || home=/tmp/home
uid=$(id -u)
cargo_home=$root/.cache/cargo-home
mkdir -p "$cargo_home"

# Disk-backed /tmp and /var/tmp for this run. A run holds a lock on its directory's .lock file for as long as it (or
# anything it started) lives; a lock works across PID namespaces, which a process id does not: a nested sandbox
# cannot see the outer run's pid and must never take its /tmp away. A directory older than a minute whose lock is
# free belongs to a run that was killed, and is removed.
scratch_base=$root/.cache/sandbox-tmp
mkdir -p "$scratch_base"
for stale in "$scratch_base"/run.*; do
	[[ -d $stale && -n $(find "$stale" -maxdepth 0 -mmin +1 2>/dev/null) ]] || continue
	if flock --nonblock "$stale/.lock" true 2>/dev/null; then
		rm -rf -- "$stale"
	fi
done
scratch=$(mktemp -d "$scratch_base/run.XXXXXX")
exec 8>"$scratch/.lock"
flock 8
mkdir -m 0700 "$scratch/tmp" "$scratch/var-tmp"
cleanup() { [[ -n ${scratch:-} && $scratch == "$scratch_base"/run.* ]] && rm -rf -- "$scratch"; }
trap cleanup EXIT  # for the usage errors below; after the exec at the end the watcher takes over

# The root is bubblewrap's own empty tmpfs, owned by the sandbox user; only what programs need is bound into it,
# read-only. (Binding the host's / itself would show it as owned by "nobody" in the user namespace, and the
# installer rightly refuses to trust such directories.) /boot, /root, /mnt, /media and the rest of $HOME are absent.
args=()
for directory in usr etc opt var sys srv; do
	[[ -d /$directory && ! -L /$directory ]] && args+=(--ro-bind "/$directory" "/$directory")
done
for link in bin sbin lib lib32 lib64 libx32; do
	if [[ -L /$link ]]; then
		args+=(--symlink "$(readlink "/$link")" "/$link")
	elif [[ -d /$link ]]; then
		args+=(--ro-bind "/$link" "/$link")
	fi
done
args+=(
	--dir /mnt
	--dir /media
	--dev /dev
	--proc /proc
	--bind "$scratch/tmp" /tmp
	--bind "$scratch/var-tmp" /var/tmp
	--tmpfs /run
	--dir "/run/user/$uid"
	--tmpfs "$home"
)
# The main checkout of a git worktree (read-only): its .venv runs the tests of every worktree.
common=$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
main=${common%/.git}
if [[ -n $common && $main != "$common" && $main != "$root" && -d $main ]]; then
	args+=(--ro-bind "$main" "$main")
fi
args+=(--bind "$root" "$root")
for directory in "${writable[@]}"; do
	args+=(--bind "$directory" "$directory")
done
[[ -f $home/.gitconfig ]] && args+=(--ro-bind "$home/.gitconfig" "$home/.gitconfig")
if ((kvm)); then
	[[ -c /dev/kvm ]] || { echo "sandbox.sh: --kvm: /dev/kvm does not exist" >&2; exit 2; }
	args+=(--dev-bind /dev/kvm /dev/kvm)
fi
args+=(--unshare-all)
if ((net)); then
	args+=(--share-net)
	# A resolv.conf that points into /run (NetworkManager, systemd-resolved) would vanish under the empty /run.
	resolver=$(readlink -f /etc/resolv.conf 2>/dev/null || true)
	if [[ $resolver == /run/* && -f $resolver ]]; then
		args+=(--ro-bind "$resolver" "$resolver")
	fi
fi
args+=(
	--die-with-parent
	--new-session
	--cap-drop ALL
	--chdir "$PWD"
	--setenv HOME "$home"
	--setenv TMPDIR /tmp
	--setenv XDG_RUNTIME_DIR "/run/user/$uid"
	--setenv CARGO_HOME "$cargo_home"
	--setenv PYTHONDONTWRITEBYTECODE 1
	--setenv GENTOO_INSTALLER_SANDBOX 1
	--unsetenv DBUS_SESSION_BUS_ADDRESS
	--unsetenv DBUS_SYSTEM_BUS_ADDRESS
	--unsetenv SSH_AUTH_SOCK
	--unsetenv GPG_AGENT_INFO
	--unsetenv DISPLAY
	--unsetenv WAYLAND_DISPLAY
	--unsetenv XAUTHORITY
	--unsetenv DOCKER_HOST
	--unsetenv SUDO_ASKPASS
)
# bubblewrap replaces this script (exec, through low-priority.sh's own execs), so signals and the terminal reach it exactly as before. It must not be
# started as a background job: such a job ignores SIGINT, and every program inside would inherit that and could no
# longer be stopped with Ctrl-C. A detached watcher removes the scratch directory once this process (then bwrap) is
# gone; it keeps no stdout/stderr (callers that read our output would wait for it) and does not hold the lock.
trap - EXIT
(
	exec 8>&- </dev/null >/dev/null 2>&1
	# shellcheck disable=SC2016  # the script is for the new bash, with its own $1 and $2
	exec bash -c 'while kill -0 "$1" 2>/dev/null; do sleep 1; done
case $2 in */.cache/sandbox-tmp/run.*) rm -rf -- "$2" ;; esac' sandbox-cleanup "$$" "$scratch"
) &
exec "$root/scripts/low-priority.sh" bwrap "${args[@]}" -- "$@"
