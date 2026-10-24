#!/usr/bin/env bash
# Bring the user's own PortageHub into apps/portagehub, so the live CD carries the current application.
#
# Usage: scripts/sync-portagehub.sh [--source DIR] [--source-name TEXT] [--dest DIR] [--patches DIR] [COMMIT]
#
#   --source DIR   the PortageHub git repository (default /home/edo/Downloads/Gentoo_package_mananger)
#   --source-name TEXT  what UPSTREAM calls the source (default: its path); for a local mirror of it
#   --dest DIR     the directory to fill (default apps/portagehub)
#   --patches DIR  the live CD's patches to PortageHub, checked against the new tree
#                  (default packaging/gentoo-overlay/app-portage/portagehub/files)
#   COMMIT         anything the source repository can resolve (default: its HEAD)
#
# The commit is exported with `git archive`, so only what upstream tracks arrives; of that, CI configuration
# (.github), .gitignore and upstream's own Gentoo overlay (dist/gentoo, whose ebuild builds from GitHub) stay out,
# because this repository packages PortageHub itself. Untracked local directories of the source (target, reports,
# research_notes, ...) are never in an archive. The source repository is only read: nothing in it is written,
# checked out or run. An unknown commit stops the script before apps/portagehub is touched.
#
# The new tree replaces the old one (a `target` build directory in the destination is kept). apps/portagehub/UPSTREAM
# records the source path, the commit, its date and its subject. The script prints a diffstat of the change, then
# tries every patch of --patches on the new tree and exits with 3 (tree updated) when one no longer applies.
# Exit status: 0 done, 1 error, 2 usage.
set -Eeuo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source_dir=/home/edo/Downloads/Gentoo_package_mananger
dest=${root}/apps/portagehub
patches=${root}/packaging/gentoo-overlay/app-portage/portagehub/files
commit=
source_name=

die() {
	printf 'sync-portagehub: %s\n' "$*" >&2
	exit 1
}

usage_error() {
	printf 'sync-portagehub: %s\n' "$*" >&2
	printf 'usage: scripts/sync-portagehub.sh [--source DIR] [--source-name TEXT] [--dest DIR] [--patches DIR] [COMMIT]\n' >&2
	exit 2
}

while (($#)); do
	case $1 in
		--source | --source-name | --dest | --patches)
			(($# >= 2)) || usage_error "$1 needs an argument"
			case $1 in
				--source) source_dir=$2 ;;
				--source-name) source_name=$2 ;;
				--dest) dest=$2 ;;
				--patches) patches=$2 ;;
			esac
			shift
			;;
		-h | --help)
			sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
			exit 0
			;;
		-*) usage_error "unknown option: $1" ;;
		*)
			[[ -z ${commit} ]] || usage_error "only one commit can be given"
			commit=$1
			;;
	esac
	shift
done

# Upstream is only ever read, and git must not even refresh its index.
export GIT_OPTIONAL_LOCKS=0
src() { git -C "${source_dir}" "$@"; }

[[ -d ${source_dir} ]] || die "source ${source_dir} is not a directory"
source_dir=$(cd -- "${source_dir}" && pwd -P)
src rev-parse --git-dir > /dev/null 2>&1 || die "source ${source_dir} is not a git repository"
resolved=$(src rev-parse --verify --quiet "${commit:-HEAD}^{commit}") \
	|| die "unknown commit '${commit:-HEAD}' in ${source_dir}"
[[ -n ${dest} ]] || die "empty destination"
mkdir -p -- "${dest}"
dest=$(cd -- "${dest}" && pwd -P)
[[ ${dest} != / && ${dest} != "${source_dir}" ]] || die "refusing to replace ${dest}"

work=$(mktemp -d)
trap 'rm -rf -- "${work}"' EXIT
mkdir -p "${work}/new" "${work}/cmp/a" "${work}/cmp/b"

# Export, then drop what the live CD does not take.
src archive --format=tar "${resolved}" | tar -C "${work}/new" -xf -
rm -rf -- "${work}/new/.github" "${work}/new/.gitignore" "${work}/new/dist/gentoo" \
	"${work}/new/reports" "${work}/new/research_notes" "${work}/new/target"
[[ -f ${work}/new/Cargo.toml && -f ${work}/new/Cargo.lock ]] || die "commit ${resolved} has no Cargo.toml and Cargo.lock at its top"

{
	printf 'source: %s\n' "${source_name:-${source_dir}}"
	printf 'commit: %s\n' "${resolved}"
	printf 'date: %s\n' "$(src show -s --format=%ci "${resolved}")"
	printf 'subject: %s\n' "$(src show -s --format=%s "${resolved}")"
} > "${work}/new/UPSTREAM"

# Keep both trees for the diffstat, then replace; a build directory (target) stays where it is.
shopt -s dotglob nullglob
cp -a -- "${work}/new/." "${work}/cmp/b/"
for entry in "${dest}"/*; do
	[[ ${entry##*/} == target ]] && continue
	cp -a -- "${entry}" "${work}/cmp/a/"
done
for entry in "${dest}"/*; do
	[[ ${entry##*/} == target ]] && continue
	rm -rf -- "${entry}"
done
for entry in "${work}"/new/*; do
	mv -- "${entry}" "${dest}/"
done
shopt -u dotglob nullglob

printf 'PortageHub %s %s\n  from %s\n  into %s\n\n' "${resolved:0:12}" "$(src show -s --format=%s "${resolved}")" "${source_dir}" "${dest}"
# a/ is the old tree, b/ the new one; git prints "a/x => b/x" or "{a => b}/x" for what differs.
diffstat=$(
	cd -- "${work}/cmp" && git -c core.quotepath=off diff --no-index --numstat -- a b 2> /dev/null | awk -F'\t' '
		{
			path = $3
			note = ""
			if (path ~ /^\/dev\/null => /) { sub(/^\/dev\/null => b\//, "", path); note = " (new)" }
			else if (path ~ / => \/dev\/null$/) { sub(/^a\//, "", path); sub(/ => \/dev\/null$/, "", path); note = " (removed)" }
			else { sub(/^\{a => b\}\//, "", path) }
			if ($1 == "-") { printf " %-60s binary\n", path note; next }
			printf " %-60s +%-6d -%d\n", path note, $1, $2
			added += $1; removed += $2; files++
		}
		END { if (files) printf "\n %d files changed, %d insertions(+), %d deletions(-)\n", files, added, removed }'
) || true
if [[ -n ${diffstat} ]]; then
	printf '%s\n' "${diffstat}"
else
	printf 'no change\n'
fi

# The live CD's own changes must still apply to what was just imported.
status=0
if [[ -d ${patches} ]]; then
	shopt -s nullglob
	for patch in "${patches}"/*.patch; do
		if patch -p1 --dry-run --silent -d "${dest}" < "${patch}" > /dev/null 2>&1; then
			printf 'patch applies: %s\n' "${patch##*/}"
		else
			printf 'PATCH DOES NOT APPLY: %s\n' "${patch##*/}" >&2
			status=3
		fi
	done
	shopt -u nullglob
fi
exit "${status}"
