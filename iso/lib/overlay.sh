# shellcheck shell=bash
# Copy build inputs so that they are root-owned in the image.
#
# The workspace belongs to whoever runs the build (uid 1000 on most hosts) and Catalyst
# rsyncs root_overlay into the image as it finds it. Without this the live image carries
# the builder's uid on /etc and on everything in the overlay, which is wrong on
# principle (that uid is the live user) and makes the image depend on who built it.

# copy_tree_without_owner SOURCE DESTINATION: like cp -a, but ownership is not copied.
copy_tree_without_owner() {
	cp -a --no-preserve=ownership -- "$1" "$2"
}

# normalize_tree DIR: owner 0:0, directories 0755, executables 0755, other files 0644.
# The set-id bits and the sticky bit are dropped too; the overlay has no use for them.
normalize_tree() {
	local tree=$1
	chown -R 0:0 -- "${tree}"
	find "${tree}" -type d -exec chmod 0755 {} +
	find "${tree}" -type f -perm /111 -exec chmod 0755 {} +
	find "${tree}" -type f ! -perm /111 -exec chmod 0644 {} +
}
