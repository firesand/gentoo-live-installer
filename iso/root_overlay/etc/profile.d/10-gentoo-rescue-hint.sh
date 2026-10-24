# Remind users of the text console that this disc can repair a system that does not boot.
case $- in
	*i*)
		if [ -z "${DISPLAY-}" ] && [ -z "${WAYLAND_DISPLAY-}" ]; then
			echo "Repair a broken system: gentoo-rescue    Install Gentoo: gentoo-installer"
		fi
		;;
esac
