# Tell the user of a text console where the repair tool and the installers are.
# The desktop does not need this, so nothing is printed when a display exists, and scripts
# and scp (not interactive) must not see it. /etc/profile.d/20-gentoo-autostart.sh can start
# a tool by itself when the boot menu entry asked for it.
case $- in
	*i*)
		if [ -z "${DISPLAY-}" ] && [ -z "${WAYLAND_DISPLAY-}" ]; then
			echo "Repair a broken system: sudo gentoo-rescue"
			echo "Install Gentoo (desktop): gentoo-installer"
			echo "Install Gentoo (text mode): sudo gentoo-installer-text"
		fi
		;;
esac
