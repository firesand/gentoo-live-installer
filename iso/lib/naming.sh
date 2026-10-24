# shellcheck shell=bash
# Image flavours. Sourced by scripts/build-iso.sh and iso/builder-entrypoint.sh after
# iso/pins.env, so the host and the container derive exactly the same names.
#
#   release  the ISO that may be distributed: names exactly as in iso/pins.env.
#   testkey  an ISO with a temporary root SSH key for the VM test harness. It is never
#            to be handed to anybody; its file name and its volume label say so, so a test
#            image cannot be mistaken for a release by name, by `blkid` or by the boot menu.

readonly ISO_TESTKEY_NAME_SUFFIX=-TESTKEY
readonly ISO_TESTKEY_VOLID_SUFFIX=_TESTKEY

# iso_apply_flavor release|testkey: adjust ISO_NAME and ISO_VOLID in place (idempotent).
iso_apply_flavor() {
	case ${1:-} in
		release)
			ISO_FLAVOR=release
			;;
		testkey)
			ISO_FLAVOR=testkey
			if [[ ${ISO_NAME} != *"${ISO_TESTKEY_NAME_SUFFIX}.iso" ]]; then
				ISO_NAME="${ISO_NAME%.iso}${ISO_TESTKEY_NAME_SUFFIX}.iso"
			fi
			if [[ ${ISO_VOLID} != *"${ISO_TESTKEY_VOLID_SUFFIX}" ]]; then
				ISO_VOLID="${ISO_VOLID%_AMD64}${ISO_TESTKEY_VOLID_SUFFIX}"
			fi
			;;
		*)
			return 1
			;;
	esac
	# An ISO volume label has at most 32 characters.
	[[ ${#ISO_VOLID} -le 32 && ${ISO_VOLID} =~ ^[A-Z0-9_]+$ ]]
}
