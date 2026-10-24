# shellcheck shell=bash
# Run a long child process so that SIGTERM and SIGINT reach it.
#
# `docker stop` and Ctrl-C deliver the signal to the entrypoint. A foreground child of a
# bash script with `set -e` never sees it, so Catalyst would keep running until Docker
# kills the container. run_child starts the command in the background, waits for it with
# `wait` (which a trapped signal interrupts), forwards the signal and exits with 128+n.

RUN_CHILD_PID=

# run_child COMMAND [ARGUMENT...]: returns the command's exit status, or exits 130/143
# after forwarding INT/TERM.
run_child() {
	local status=0
	"$@" &
	RUN_CHILD_PID=$!
	trap '_run_child_forward INT 130' INT
	trap '_run_child_forward TERM 143' TERM
	wait "${RUN_CHILD_PID}" || status=$?
	trap - INT TERM
	RUN_CHILD_PID=
	return "${status}"
}

_run_child_forward() {
	local signal=$1 code=$2
	trap - INT TERM
	if [[ -n ${RUN_CHILD_PID} ]]; then
		kill -s "${signal}" "${RUN_CHILD_PID}" 2>/dev/null || true
		# Give the child time to unmount and clean up (up to 30 s), but not forever.
		local waited=0 limit=${RUN_CHILD_GRACE_TENTHS:-300}
		while kill -0 "${RUN_CHILD_PID}" 2>/dev/null && ((waited < limit)); do
			sleep 0.1
			waited=$((waited + 1))
		done
		kill -s KILL "${RUN_CHILD_PID}" 2>/dev/null || true
		wait "${RUN_CHILD_PID}" 2>/dev/null || true
	fi
	exit "${code}"
}
