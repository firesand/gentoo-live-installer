# Tell the user of a text console where the repair tool and the installers are.
# The desktop does not need this, so nothing is printed when a display exists, and scripts
# and scp (not interactive) must not see it. /etc/profile.d/20-gentoo-autostart.sh can start
# a tool by itself when the boot menu entry asked for it.
case $- in
	*i*)
		if [ -z "${DISPLAY-}" ] && [ -z "${WAYLAND_DISPLAY-}" ]; then
			echo "Repair a Gentoo system that does not start: sudo gentoo-rescue"
			if command -v gentoo-rescue-ai > /dev/null 2>&1; then
				echo "Repair with AI help (needs the internet): sudo gentoo-rescue-ai"
			fi
			echo "Install Gentoo (desktop): gentoo-installer"
			echo "Install Gentoo (text mode): sudo gentoo-installer-text"
			# The desktop entry of the boot menu (no "nox", no autostart tool) starts X on its own console. When this
			# first console is what the screen shows instead, X did not start: say so (the first test on a real PC
			# only showed Qt's abort when gentoo-installer was typed here). GENTOO_HINT_CMDLINE is for the tests.
			gentoo_hint_line=
			if [ -r "${GENTOO_HINT_CMDLINE:-/proc/cmdline}" ]; then
				IFS= read -r gentoo_hint_line < "${GENTOO_HINT_CMDLINE:-/proc/cmdline}" || :
			fi
			case " ${gentoo_hint_line} " in
				*" nox "* | *" gentoo.autostart="*) ;;
				*)
					if ! pgrep -x Xorg > /dev/null 2>&1 && ! pgrep -x X > /dev/null 2>&1; then
						echo
						echo "No desktop is running yet. If none appears within a minute, the graphics"
						echo "driver could not start it. Try: sudo rc-service display-manager restart"
						echo "Or restart and choose \"Safe graphics\" in the boot menu, or install in"
						echo "text mode (above). A short report for a bug report: sudo gentoo-live-report"
					fi
					;;
			esac
			unset gentoo_hint_line
		fi
		;;
esac
