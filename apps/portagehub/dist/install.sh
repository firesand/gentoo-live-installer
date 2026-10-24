#!/bin/sh
# Install PortageHub system-wide. Run as root after `cargo build --release`.
#
#   PREFIX   where the program and the helper go (default /usr; /usr/local works too)
#   DESTDIR  stage the files below this directory instead of the real root (packaging)
#
# polkit identifies the helper by its path, so the policy is written with the path the helper
# really gets below PREFIX. The policy itself always goes to /usr/share/polkit-1/actions,
# the one place polkit reads.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
prefix=${PREFIX:-/usr}
destdir=${DESTDIR:-}
release=${CARGO_TARGET_DIR:-$here/target}/release
helper=$prefix/libexec/portagehub-helper
policy_dir=$destdir/usr/share/polkit-1/actions

for f in portagehub portagehub-helper; do
    [ -f "$release/$f" ] || { echo "$release/$f is missing: run cargo build --release first" >&2; exit 1; }
done

install -Dm755 "$release/portagehub" "$destdir$prefix/bin/portagehub"
install -Dm755 "$release/portagehub-helper" "$destdir$helper"
install -d "$policy_dir"
sed "s|<annotate key=\"org.freedesktop.policykit.exec.path\">[^<]*<|<annotate key=\"org.freedesktop.policykit.exec.path\">$helper<|" \
    "$here/dist/org.portagehub.helper.policy" > "$policy_dir/org.portagehub.helper.policy"
chmod 644 "$policy_dir/org.portagehub.helper.policy"
install -Dm644 "$here/dist/portagehub.desktop" "$destdir$prefix/share/applications/portagehub.desktop"

echo "Installed. Start it from the application menu or run: portagehub"
echo "  program: $prefix/bin/portagehub"
echo "  helper:  $helper (polkit policy: $policy_dir/org.portagehub.helper.policy)"
