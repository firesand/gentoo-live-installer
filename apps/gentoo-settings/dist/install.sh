#!/bin/sh
# Install Gentoo Settings system-wide. Run as root after `cargo build --release`.
# The helper must live at /usr/libexec/gentoo-settings-helper: the polkit
# policy names that path.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
prefix=${PREFIX:-/usr}

install -Dm755 "$here/target/release/gentoo-settings" "$prefix/bin/gentoo-settings"
install -Dm755 "$here/target/release/gentoo-settings-helper" /usr/libexec/gentoo-settings-helper
install -Dm644 "$here/dist/org.gentoosettings.helper.policy" /usr/share/polkit-1/actions/org.gentoosettings.helper.policy
install -Dm644 "$here/dist/gentoo-settings.desktop" "$prefix/share/applications/gentoo-settings.desktop"
echo "Installed. Start it from the application menu or run: gentoo-settings"
