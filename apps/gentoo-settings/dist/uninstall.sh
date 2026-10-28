#!/bin/sh
# Remove the Gentoo Settings program. Tweaks you applied stay in place:
# use "Revert all" in the app first if you want them gone too.
set -eu
prefix=${PREFIX:-/usr}
rm -f "$prefix/bin/gentoo-settings" /usr/libexec/gentoo-settings-helper \
	/usr/share/polkit-1/actions/org.gentoosettings.helper.policy \
	"$prefix/share/applications/gentoo-settings.desktop"
echo "Removed. Applied tweaks, /var/lib/gentoo-settings (history, backups) and their files in /etc were left alone."
