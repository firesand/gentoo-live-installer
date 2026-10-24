#!/bin/sh
# Install PortageHub system-wide. Run as root after `cargo build --release`.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
prefix=${PREFIX:-/usr}

install -Dm755 "$here/target/release/portagehub" "$prefix/bin/portagehub"
install -Dm755 "$here/target/release/portagehub-helper" "$prefix/libexec/portagehub-helper"
install -Dm644 "$here/dist/org.portagehub.helper.policy" /usr/share/polkit-1/actions/org.portagehub.helper.policy
install -Dm644 "$here/dist/portagehub.desktop" "$prefix/share/applications/portagehub.desktop"
echo "Installed. Start it from the application menu or run: portagehub"
