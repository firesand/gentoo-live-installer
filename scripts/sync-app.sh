#!/usr/bin/env bash
# Bring one of the user's own applications into apps/, so the live CD carries its current version.
#
# Usage: scripts/sync-app.sh APP [--source DIR] [--source-name TEXT] [--dest DIR] [--patches DIR] [COMMIT]
#
#   APP            portagehub, gentoo-settings or kernelsmith; it chooses the defaults below
#   --source DIR   the application's git repository (PortageHub /home/edo/Downloads/Gentoo_package_mananger, Gentoo
#                  Settings /home/edo/Downloads/Gentoo-Settings, KernelSmith .cache/upstream/kernelsmith.git, a mirror of
#                  https://github.com/firesand/kernelsmith.git made with `git clone --mirror`)
#   --source-name TEXT  what UPSTREAM calls the source (default: its path; KernelSmith: the GitHub address)
#   --dest DIR     the directory to fill (default apps/APP)
#   --patches DIR  the live CD's patches to the application, checked against the new tree
#                  (default: files/ next to its ebuild in packaging/gentoo-overlay)
#   COMMIT         anything the source repository can resolve (default: its HEAD)
#
# The commit is exported with `git archive`, so only what upstream tracks arrives; of that, CI configuration
# (.github), .gitignore, screenshots and upstream's own Gentoo packaging (whose ebuilds build from GitHub) stay out,
# because this repository packages the applications itself. Untracked local directories of the source (target, reports,
# research_notes, ...) are never in an archive. The source repository is only read: nothing in it is written,
# checked out or run. An unknown commit stops the script before the destination is touched.
#
# The new tree replaces the old one (a `target` build directory in the destination is kept). UPSTREAM in the
# destination records the source, the commit, its date and its subject. The script prints a diffstat of the change,
# then tries every patch of --patches on the new tree and exits with 3 (tree updated) when one no longer applies.
# scripts/sync-portagehub.sh is `sync-app.sh portagehub`. Exit status: 0 done, 1 error, 2 usage.
set -Eeuo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
commit=
source_name=

die() {
	printf 'sync-app: %s\n' "$*" >&2
	exit 1
}

usage_error() {
	printf 'sync-app: %s\n' "$*" >&2
	printf 'usage: scripts/sync-app.sh APP [--source DIR] [--source-name TEXT] [--dest DIR] [--patches DIR] [COMMIT]\n' >&2
	exit 2
}

case ${1:-} in
	-h | --help)
		sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	portagehub)
		name=PortageHub
		source_dir=/home/edo/Downloads/Gentoo_package_mananger
		package=app-portage/portagehub
		;;
	gentoo-settings)
		name="Gentoo Settings"
		source_dir=/home/edo/Downloads/Gentoo-Settings
		package=app-admin/gentoo-settings
		;;
	kernelsmith)
		name=KernelSmith
		source_dir=${root}/.cache/upstream/kernelsmith.git
		source_name=https://github.com/firesand/kernelsmith.git
		package=sys-kernel/kernelsmith
		;;
	'') usage_error "name the application: portagehub, gentoo-settings or kernelsmith" ;;
	*) usage_error "unknown application: $1 (portagehub, gentoo-settings or kernelsmith)" ;;
esac
app=$1
shift
dest=${root}/apps/${app}
patches=${root}/packaging/gentoo-overlay/${package}/files

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
			sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
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
rm -rf -- "${work}/new/.github" "${work}/new/.gitignore" "${work}/new/dist/gentoo" "${work}/new/packaging/gentoo" \
	"${work}/new/docs/screenshots" "${work}/new/reports" "${work}/new/research_notes" "${work}/new/target"
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

printf '%s %s %s\n  from %s\n  into %s\n\n' "${name}" "${resolved:0:12}" "$(src show -s --format=%s "${resolved}")" "${source_dir}" "${dest}"
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
