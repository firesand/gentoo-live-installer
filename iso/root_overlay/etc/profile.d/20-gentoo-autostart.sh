# Start the repair tool or the text installer on the first console login when the boot
# menu asked for it.
#
# The boot menu entries "Repair a system (text console)" and "Install in text mode" add
# gentoo.autostart=rescue or gentoo.autostart=installer-text to the kernel command line.
# The automatic login on the first console (tty1) then runs
#
#     sudo gentoo-rescue                 or                 sudo gentoo-installer-text
#
# once. When the tool ends, or cannot start, you are at the normal shell. Other logins,
# scripts and the desktop are not affected.
#
# The installer service (gentoo-installerd) can start a moment after the first console, so
# the hook waits up to 20 seconds for its socket before it starts a tool.
#
# GENTOO_AUTOSTART_CMDLINE, _TTY, _STATE, _SOCKET and _WAIT are for the tests of the disc;
# they are not set on a real disc. This file is read by sh, bash and dash.
case $- in
	*i*)
		if [ -z "${DISPLAY-}" ] && [ -z "${WAYLAND_DISPLAY-}" ]; then
			gentoo_autostart_line=
			gentoo_autostart_file=${GENTOO_AUTOSTART_CMDLINE:-/proc/cmdline}
			if [ -r "${gentoo_autostart_file}" ]; then
				IFS= read -r gentoo_autostart_line < "${gentoo_autostart_file}" || :
			fi
			gentoo_autostart_tool=
			gentoo_autostart_name=
			case " ${gentoo_autostart_line} " in
				*" gentoo.autostart=rescue "*)
					gentoo_autostart_tool=gentoo-rescue
					gentoo_autostart_name="the repair tool"
					;;
				*" gentoo.autostart=installer-text "*)
					gentoo_autostart_tool=gentoo-installer-text
					gentoo_autostart_name="the text installer"
					;;
			esac
			# mkdir is the one-time switch: it succeeds for the first login only.
			if [ -n "${gentoo_autostart_tool}" ] \
				&& [ "$(tty 2> /dev/null)" = "${GENTOO_AUTOSTART_TTY:-/dev/tty1}" ] \
				&& mkdir "${GENTOO_AUTOSTART_STATE:-/tmp/.gentoo-autostart}" 2> /dev/null; then
				if command -v sudo > /dev/null 2>&1 && command -v "${gentoo_autostart_tool}" > /dev/null 2>&1; then
					gentoo_autostart_waited=0
					while [ ! -e "${GENTOO_AUTOSTART_SOCKET:-/run/gentoo-installer/server.sock}" ] \
						&& [ "${gentoo_autostart_waited}" -lt "${GENTOO_AUTOSTART_WAIT:-20}" ]; do
						if [ "${gentoo_autostart_waited}" -eq 0 ]; then
							printf '\nWaiting for the installer service ...\n'
						fi
						sleep 1
						gentoo_autostart_waited=$((gentoo_autostart_waited + 1))
					done
					printf '\nStarting %s: sudo %s\n' "${gentoo_autostart_name}" "${gentoo_autostart_tool}"
					printf 'When it ends you are back at this shell.\n\n'
					sudo "${gentoo_autostart_tool}"
					gentoo_autostart_status=$?
					if [ "${gentoo_autostart_status}" -eq 0 ]; then
						printf '\nYou are back at the shell. To start %s again: sudo %s\n' "${gentoo_autostart_name}" "${gentoo_autostart_tool}"
					else
						printf '\n%s stopped (exit status %s). You are at the shell. To try again: sudo %s\n' \
							"${gentoo_autostart_name}" "${gentoo_autostart_status}" "${gentoo_autostart_tool}"
					fi
				else
					printf '\n%s is not on this disc, so it cannot start by itself.\n' "${gentoo_autostart_tool}"
					printf 'Use the other command shown above, or restart and choose another menu entry.\n'
				fi
			fi
			unset gentoo_autostart_line gentoo_autostart_file gentoo_autostart_tool gentoo_autostart_name gentoo_autostart_status gentoo_autostart_waited
		fi
		;;
esac
