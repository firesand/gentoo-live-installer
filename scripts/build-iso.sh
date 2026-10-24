#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=iso/pins.env
source "${ROOT}/iso/pins.env"

CACHE_DIR=${ISO_CACHE_DIR:-"${ROOT}/.cache/iso"}
DIST_DIR=${ISO_DIST_DIR:-"${ROOT}/dist"}
JOBS=${JOBS:-2}
LOAD_AVERAGE=${LOAD_AVERAGE:-$((JOBS * 2))}
CHECK_ONLY=0

usage() {
	cat <<'EOF'
Usage: scripts/build-iso.sh [--check]

Build the pinned Gentoo AMD64 installer ISO in Docker. No host block devices
are mounted. Environment: JOBS, LOAD_AVERAGE, ISO_CACHE_DIR, ISO_DIST_DIR.

  --check  Validate prerequisites and inputs without building or creating dirs.
EOF
}

die() {
	printf 'build-iso: %s\n' "$*" >&2
	exit 1
}

while (($#)); do
	case $1 in
		--check) CHECK_ONLY=1 ;;
		-h|--help) usage; exit 0 ;;
		*) usage >&2; die "unknown argument: $1" ;;
	esac
	shift
done

for command_name in docker python3 realpath sha512sum b2sum; do
	command -v "${command_name}" >/dev/null 2>&1 || die "missing prerequisite: ${command_name}"
done
[[ $(uname -m) == x86_64 ]] || die "an x86_64 host is required for this AMD64 builder"
[[ ${JOBS} =~ ^[1-9][0-9]*$ ]] || die "JOBS must be a positive integer"
[[ ${LOAD_AVERAGE} =~ ^[0-9]+([.][0-9]+)?$ ]] || die "LOAD_AVERAGE must be numeric"
[[ ${GENTOO_TREEISH} =~ ^[0-9a-f]{40}$ ]] || die "GENTOO_TREEISH must be a full commit ID"
[[ ${STAGE3_SHA512} =~ ^[0-9a-f]{128}$ ]] || die "invalid stage3 SHA-512 pin"
[[ ${STAGE3_BLAKE2B} =~ ^[0-9a-f]{128}$ ]] || die "invalid stage3 BLAKE2b pin"
[[ ${ROOT} != *$'\n'* && ${ROOT} != *,* ]] || die "workspace path cannot contain commas or newlines"
[[ ${CACHE_DIR} != *$'\n'* && ${CACHE_DIR} != *,* && ${DIST_DIR} != *$'\n'* && ${DIST_DIR} != *,* ]] \
	|| die "cache/dist paths cannot contain commas or newlines"

required_files=(
	pyproject.toml
	iso/Containerfile
	iso/builder-entrypoint.sh
	iso/catalyst-no-loop.py
	iso/fsscript-stage2.sh
	iso/specs/livecd-stage1.spec
	iso/specs/livecd-stage2.spec
	packaging/gentoo-overlay/app-admin/gentoo-live-installer/gentoo-live-installer-0.1.0-r4.ebuild
	packaging/gentoo-overlay/app-misc/hwmonux/hwmonux-1.0.0.ebuild
	packaging/gentoo-overlay/app-portage/portagehub/portagehub-0.4.0.ebuild
	apps/hwmonux/pyproject.toml
	apps/hwmonux/hwmonux.py
	apps/hwmonux/sensors.py
	apps/portagehub/Cargo.toml
	apps/portagehub/Cargo.lock
	apps/portagehub/dist/portagehub.desktop
	apps/portagehub/dist/org.portagehub.helper.policy
)
for relative_path in "${required_files[@]}"; do
	[[ -f ${ROOT}/${relative_path} ]] || die "required input is missing: ${relative_path}"
done
[[ -d ${ROOT}/src/gentoo_installer ]] || die "src/gentoo_installer is missing"
[[ -d ${ROOT}/src/gentoo_installerd ]] || die "src/gentoo_installerd is missing"
[[ -d ${ROOT}/apps/portagehub/src ]] || die "apps/portagehub/src is missing"

docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 \
	|| die "Docker daemon is unavailable or not accessible to $(id -un)"
if docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless; then
	die "rootless Docker is unsupported; Catalyst requires isolated mount operations"
fi
available_kib=$(df -Pk "${ROOT}" | awk 'NR == 2 { print $4 }')
[[ ${available_kib} =~ ^[0-9]+$ ]] || die "could not determine free disk space"
(( available_kib >= 31457280 )) || die "at least 30 GiB free is required on the workspace filesystem"

if ((CHECK_ONLY)); then
	printf 'build-iso: prerequisites and pinned inputs look valid; no build started.\n'
	printf 'build-iso: stage3 %s\n' "${STAGE3_BASENAME}"
	printf 'build-iso: Gentoo snapshot %s\n' "${GENTOO_TREEISH}"
	exit 0
fi

CACHE_DIR=$(realpath -m -- "${CACHE_DIR}")
DIST_DIR=$(realpath -m -- "${DIST_DIR}")
[[ ! -L ${CACHE_DIR} && ! -L ${DIST_DIR} ]] || die "cache and dist paths must not be symlinks"
mkdir -p "${CACHE_DIR}/catalyst" "${CACHE_DIR}/distfiles" "${CACHE_DIR}/binpkgs" \
	"${CACHE_DIR}/cargo" "${DIST_DIR}"

image="localhost/gentoo-installer-catalyst:${GENTOO_TREEISH:0:12}"
printf 'build-iso: building pinned Gentoo builder image %s\n' "${image}"
if ! docker build --pull=false --build-arg "GENTOO_TREEISH=${GENTOO_TREEISH}" \
	--file "${ROOT}/iso/Containerfile" --tag "${image}" "${ROOT}/iso"; then
	die "Gentoo builder image creation failed; no ISO was accepted"
fi

printf 'build-iso: starting Catalyst; this is a long build.\n'
if ! docker run --rm --init --platform linux/amd64 \
	--cap-add SYS_ADMIN --cap-add NET_ADMIN --cap-add MKNOD --cap-add SYS_CHROOT \
	--security-opt apparmor=unconfined --security-opt seccomp=unconfined \
	--shm-size 1g --pids-limit 4096 \
	--cpus "${JOBS}" --memory 8g --memory-swap 8g \
	--env "JOBS=${JOBS}" --env "LOAD_AVERAGE=${LOAD_AVERAGE}" \
	--env "CATALYST_BUILDER_IMAGE=${image}" \
	--env "GENTOO_TEST_SSH_PUBLIC_KEY=${GENTOO_TEST_SSH_PUBLIC_KEY:-}" \
	--mount "type=bind,source=${ROOT},target=/workspace,readonly" \
	--mount "type=bind,source=${CACHE_DIR}/catalyst,target=/var/tmp/catalyst" \
	--mount "type=bind,source=${CACHE_DIR}/distfiles,target=/var/cache/catalyst/distfiles" \
	--mount "type=bind,source=${CACHE_DIR}/binpkgs,target=/var/cache/binpkgs" \
	--mount "type=bind,source=${CACHE_DIR}/cargo,target=/var/cache/cargo" \
	--mount "type=bind,source=${DIST_DIR},target=/dist" \
	"${image}"; then
	die "Catalyst build failed; artifacts were not accepted"
fi

iso="${DIST_DIR}/${ISO_NAME}"
for artifact in "${iso}" "${iso}.sha512" "${iso}.blake2b" "${iso}.manifest.json"; do
	[[ -s ${artifact} ]] || die "builder did not publish expected artifact: ${artifact}"
done
(cd "${DIST_DIR}" && sha512sum -c "${ISO_NAME}.sha512" && b2sum -c "${ISO_NAME}.blake2b")
python3 - "${iso}.manifest.json" "${ISO_NAME}" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    manifest = json.load(stream)
if manifest.get("iso", {}).get("name") != sys.argv[2]:
    raise SystemExit("manifest does not describe the published ISO")
PY
printf 'build-iso: verified ISO: %s\n' "${iso}"
printf 'build-iso: SHA-512: %s.sha512\n' "${iso}"
printf 'build-iso: BLAKE2b: %s.blake2b\n' "${iso}"
printf 'build-iso: manifest: %s.manifest.json\n' "${iso}"
