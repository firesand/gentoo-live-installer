#!/bin/bash
# Check that a commit of the Gentoo repository carries a good OpenPGP signature made by
# the key that iso/pins.env names (GENTOO_TREEISH_SIGNER_FPR).
#
#   verify-snapshot.sh REPOSITORY COMMIT SIGNER_FPR [RESULT.json]
#
# The developer keyring comes from sec-keys/openpgp-keys-gentoo-developers and
# -gentoo-auth (override the list of .asc files with GENTOO_OPENPGP_KEYS). The key that
# made the signature may be a signing subkey, so SIGNER_FPR may name either that key or
# its primary key. A commit that is unsigned, signed by an unknown key, signed by an
# expired or revoked key, or signed by somebody else fails. Used by iso/Containerfile
# (while the image is built) and by iso/builder-entrypoint.sh (for every build).
set -euo pipefail

die() {
	printf 'verify-snapshot: %s\n' "$*" >&2
	exit 1
}

[[ $# -ge 3 && $# -le 4 ]] || die "usage: verify-snapshot.sh REPOSITORY COMMIT SIGNER_FPR [RESULT.json]"
repository=$1
commit=$2
signer=${3// /}
signer=${signer^^}
result=${4:-}
[[ ${commit} =~ ^[0-9a-f]{40}$ ]] || die "the commit must be a full 40 digit ID"
[[ ${signer} =~ ^[0-9A-F]{40}$ ]] || die "the signer must be a 40 digit key fingerprint"
[[ -d ${repository} ]] || die "no such repository: ${repository}"

# Neither the user's nor the system's git configuration may change what "verified" means.
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
default_keys="/usr/share/openpgp-keys/gentoo-developers.asc /usr/share/openpgp-keys/gentoo-auth.asc"
read -r -a key_files <<<"${GENTOO_OPENPGP_KEYS:-${default_keys}}"

home=$(mktemp -d)
chmod 0700 "${home}"
cleanup() {
	GNUPGHOME=${home} gpgconf --kill all >/dev/null 2>&1 || true
	rm -rf -- "${home}"
}
trap cleanup EXIT
export GNUPGHOME=${home}

imported=0
for key_file in "${key_files[@]}"; do
	[[ -r ${key_file} ]] || continue
	gpg --batch --no-tty --quiet --import "${key_file}" >/dev/null 2>&1 && imported=$((imported + 1))
done
((imported > 0)) || die "none of the developer key files could be imported: ${key_files[*]}"

actual=$(git -C "${repository}" rev-parse --verify "${commit}^{commit}") || die "commit ${commit} is not in ${repository}"
[[ ${actual} == "${commit}" ]] || die "the repository resolved ${commit} to ${actual}"

status=$(git -C "${repository}" verify-commit --raw "${commit}" 2>&1) \
	|| die "the signature of ${commit} is missing or invalid: $(grep -m3 -E 'GNUPG:|error|fatal' <<<"${status}" | tr '\n' ' ')"
valid=$(grep -m1 '^\[GNUPG:\] VALIDSIG ' <<<"${status}" || true)
[[ -n ${valid} ]] || die "no valid signature found on ${commit}"
grep -q '^\[GNUPG:\] \(EXPKEYSIG\|REVKEYSIG\|BADSIG\|ERRSIG\)' <<<"${status}" && die "the signature of ${commit} was made by an expired or revoked key or is bad"

read -r -a fields <<<"${valid}"
# [GNUPG:] VALIDSIG <fpr> <date> <timestamp> <expire> <version> <reserved> <pubkey-algo> <hash-algo> <class> <primary-fpr>
signing_key=${fields[2]^^}
primary_key=${fields[${#fields[@]} - 1]^^}
if [[ ${signing_key} != "${signer}" && ${primary_key} != "${signer}" ]]; then
	die "${commit} is signed by ${primary_key} (signing key ${signing_key}), not by the pinned ${signer}"
fi

printf 'verify-snapshot: %s is signed by %s (signing key %s)\n' "${commit}" "${primary_key}" "${signing_key}"
if [[ -n ${result} ]]; then
	printf '{"verified": true, "commit": "%s", "signer_fpr": "%s", "signing_key_fpr": "%s", "pinned_fpr": "%s"}\n' \
		"${commit}" "${primary_key}" "${signing_key}" "${signer}" >"${result}"
fi
