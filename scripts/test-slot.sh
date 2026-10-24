#!/usr/bin/env bash
# Run one heavy command (a test suite, the quality gate) while holding one of this PC's test slots.
#
# Usage: scripts/test-slot.sh COMMAND [ARGUMENT]...
#
# Several agents work on this project in parallel. Each full pytest run needs about 1.3 GB of memory plus its
# temporary files, and eleven at once once filled the RAM and swap of a 48 GB PC, which killed the editor and the
# agents' session. The slots are lock files shared by every checkout and worktree of the user; at most
# GENTOO_TEST_SLOTS (default 3) commands hold one at a time, the others wait their turn (a line on stderr says so).
# The lock is released when COMMAND ends, however it ends. GENTOO_TEST_TIMEOUT (seconds, default 2700) ends a
# command that runs far longer than the whole suite does, so an orphaned run cannot hold a slot forever.
# Exit status: the command's own; 124 when the timeout ended it.
set -euo pipefail

(($#)) || { echo "usage: scripts/test-slot.sh COMMAND [ARGUMENT]..." >&2; exit 2; }
command -v flock >/dev/null 2>&1 || { echo "test-slot.sh: flock (sys-apps/util-linux) is missing" >&2; exit 127; }
slots=${GENTOO_TEST_SLOTS:-3}
[[ $slots =~ ^[1-9][0-9]*$ ]] || { echo "test-slot.sh: GENTOO_TEST_SLOTS must be a positive number" >&2; exit 2; }
limit=${GENTOO_TEST_TIMEOUT:-2700}
directory=${XDG_CACHE_HOME:-$HOME/.cache}/gentoo-installer-test-slots
mkdir -p "$directory"

said=0
while :; do
	for ((slot = 1; slot <= slots; slot++)); do
		exec 9>>"$directory/slot-$slot"
		if flock --nonblock 9; then
			# fd 9 stays open in the command and its children, so the slot is held until all of them are gone.
			exec timeout --kill-after=60 "$limit" "$@"
		fi
		exec 9>&-
	done
	if ((!said)); then
		echo "test-slot.sh: all $slots test slots are busy (other agents are testing); waiting for a free one" >&2
		said=1
	fi
	sleep 5
done
