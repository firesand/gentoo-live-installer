#!/usr/bin/env bash
# Run one command so that the user's own programs always come first (the user's rule of 2026-10-28: "when you build or
# compile I can still use my PC"):
#   * the lowest CPU priority, nice 19: a busy desktop takes the CPUs back at once, an idle one lends them all;
#   * the idle I/O class (it only counts under the BFQ I/O scheduler; with "none" or mq-deadline it changes nothing);
#   * never the logical CPUs numbered below HOST_RESERVE_CPUS (default 8, on the i9-14900K four P-cores with their
#     second threads), so at most 24 of 32 threads.
# Children inherit all three. scripts/sandbox.sh runs every sandboxed command this way; scripts/host_share.py is the
# same rule for the Python VM harness, and scripts/build-iso.sh asks --cpus for the build container.
#
# Usage: scripts/low-priority.sh [--] COMMAND [ARGUMENT]...
#        scripts/low-priority.sh --cpus    print the CPUs a command may use (for docker run --cpuset-cpus)
#
# The CPUs are this process's own (Cpus_allowed_list) minus those numbered below HOST_RESERVE_CPUS, so a command that
# already runs this way (a sandbox inside the sandbox) keeps the same ones. If none would be left, all stay.
# Exit status: the command's own, 2 for a usage error.
set -euo pipefail

usage() {
	echo "usage: scripts/low-priority.sh [--] COMMAND [ARGUMENT]... | --cpus" >&2
	exit 2
}

reserve=${HOST_RESERVE_CPUS:-8}
[[ $reserve =~ ^[0-9]+$ ]] || { echo "low-priority.sh: HOST_RESERVE_CPUS must be a number of CPUs" >&2; exit 2; }

# The CPUs of $1 (a list like 0-31 or 0-3,8-11) numbered at least $reserve, as a list of ranges.
share_cpus() {
	local part cpu first last start= previous= out=
	local -a parts kept=()
	IFS=, read -ra parts <<<"$1"
	for part in "${parts[@]}"; do
		first=${part%-*}
		last=${part#*-}
		for ((cpu = first; cpu <= last; cpu++)); do
			((cpu < reserve)) || kept+=("$cpu")
		done
	done
	((${#kept[@]})) || { printf '%s\n' "$1"; return; }
	for cpu in "${kept[@]}" end; do
		if [[ -n $start && $cpu != end ]] && ((cpu == previous + 1)); then
			previous=$cpu
			continue
		fi
		if [[ -n $start ]]; then
			out+=${out:+,}$start
			((previous == start)) || out+=-$previous
		fi
		start=$cpu
		previous=$cpu
	done
	printf '%s\n' "$out"
}

allowed=$(awk '/^Cpus_allowed_list:/ { print $2; exit }' /proc/self/status)
[[ $allowed =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]] || { echo "low-priority.sh: cannot read this process's CPUs" >&2; exit 2; }

case ${1:-} in
	--cpus)
		(($# == 1)) || usage
		share_cpus "$allowed"
		exit 0
		;;
	-h | --help)
		sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	--) shift ;;
	-*) usage ;;
esac
(($#)) || usage
exec nice -n 19 ionice -t -c 3 taskset -c "$(share_cpus "$allowed")" "$@"
