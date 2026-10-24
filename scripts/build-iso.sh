#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=iso/pins.env
source "${ROOT}/iso/pins.env"
# shellcheck source=iso/lib/naming.sh
source "${ROOT}/iso/lib/naming.sh"

CACHE_DIR=${ISO_CACHE_DIR:-"${ROOT}/.cache/iso"}
DIST_DIR=${ISO_DIST_DIR:-"${ROOT}/dist"}
JOBS=${JOBS:-2}
LOAD_AVERAGE=${LOAD_AVERAGE:-$((JOBS * 2))}
BUILD_MEMORY=${BUILD_MEMORY:-8g}
BUILD_MIN_FREE_GIB=${BUILD_MIN_FREE_GIB:-45}
ISO_DIST_MIN_FREE_GIB=${ISO_DIST_MIN_FREE_GIB:-6}
DOCKER_MIN_FREE_GIB=${DOCKER_MIN_FREE_GIB:-10}
ISO_SECCOMP=${ISO_SECCOMP:-unconfined}
ISO_KEEP_STAGE1=${ISO_KEEP_STAGE1:-2}
PROC_MEMINFO=${PROC_MEMINFO:-/proc/meminfo}
CHECK_ONLY=0
CLEAN=0
CLEAN_PACKAGES=0
REUSE=auto
STAGE1_ONLY=0
VERIFY_ISO=
VERIFY_FLAVOR=release
TEST_KEY_FILE=
SIGN_KEY=

usage() {
	cat <<'EOF'
Usage: scripts/build-iso.sh [options]

Build the pinned Gentoo AMD64 installer and rescue ISO in Docker. No host block
devices are mounted. Environment: JOBS, LOAD_AVERAGE, ISO_CACHE_DIR, ISO_DIST_DIR,
ISO_LOG_DIR, BUILD_MEMORY (default 8g), BUILD_MIN_FREE_GIB (default 45), ISO_SECCOMP
(unconfined or default), ISO_KEEP_STAGE1 (default 2).

  --check              Validate prerequisites and inputs without building or creating dirs.
  --reuse MODE         auto (default): reuse the stage1 of the last build when nothing that
                       goes into it changed, reinstall only the project's own packages when
                       only those changed, otherwise build everything. none: always build
                       everything. stage1: like --stage2-only.
  --reuse-stage1       Same as --reuse auto (the default), for scripts that want to say it.
  --stage2-only        Run only stage2 from a stage1 archive whose receipt matches the
                       current inputs; fails when there is none.
  --stage1-only        Build the stage1 archive and its receipt, no ISO.
  --test-key-file FILE Build a TEST ISO with the ssh public key in FILE as a temporary root
                       login (for scripts/test-vm.py). Its file name and volume label end in
                       TESTKEY, and ISO_DIST_DIR must be set to a directory of your own.
                       The environment variable GENTOO_TEST_SSH_PUBLIC_KEY does the same.
  --sign-key KEYID     After a release build, sign the .sha512 and .manifest.json files
                       with your OpenPGP key (16 or 40 hex digits) on the host; the key
                       never enters the container. Refused together with --test-key-file.
  --verify ISO         Check an existing ISO (scripts/verify-iso.py) and exit. Use
                       --flavor release|testkey to say what it should be (default release).
  --clean              Remove retained chroots, partial files and stage1 archives beyond
                       ISO_KEEP_STAGE1 from the cache, then exit. --clean-packages also
                       empties the binary package cache (catalyst/packages).
EOF
}

die() {
	printf 'build-iso: %s\n' "$*" >&2
	exit 1
}

warn() {
	printf 'build-iso: warning: %s\n' "$*" >&2
}

note() {
	printf 'build-iso: %s\n' "$*"
}

while (($#)); do
	case $1 in
		--check) CHECK_ONLY=1 ;;
		--reuse)
			(($# >= 2)) || die "--reuse needs auto, none or stage1"
			REUSE=$2
			shift
			;;
		--reuse-stage1) REUSE=auto ;;
		--stage2-only) REUSE=stage1 ;;
		--stage1-only) STAGE1_ONLY=1 ;;
		--test-key-file)
			(($# >= 2)) || die "--test-key-file needs a file"
			TEST_KEY_FILE=$2
			shift
			;;
		--sign-key)
			(($# >= 2)) || die "--sign-key needs the ID or fingerprint of your OpenPGP key"
			SIGN_KEY=$2
			shift
			;;
		--verify)
			(($# >= 2)) || die "--verify needs an ISO file"
			VERIFY_ISO=$2
			shift
			;;
		--flavor)
			(($# >= 2)) || die "--flavor needs release or testkey"
			VERIFY_FLAVOR=$2
			shift
			;;
		--clean) CLEAN=1 ;;
		--clean-packages)
			CLEAN=1
			CLEAN_PACKAGES=1
			;;
		-h | --help)
			usage
			exit 0
			;;
		*)
			usage >&2
			die "unknown argument: $1"
			;;
	esac
	shift
done

case ${REUSE} in
	auto | none | stage1) ;;
	*) die "--reuse must be auto, none or stage1" ;;
esac
[[ ${STAGE1_ONLY} -eq 0 || ${REUSE} != stage1 ]] || die "--stage1-only and --stage2-only exclude each other"
[[ ${VERIFY_FLAVOR} == release || ${VERIFY_FLAVOR} == testkey ]] || die "--flavor must be release or testkey"

if [[ -n ${VERIFY_ISO} ]]; then
	command -v python3 >/dev/null 2>&1 || die "missing prerequisite: python3"
	exec python3 "${ROOT}/scripts/verify-iso.py" "${VERIFY_ISO}" --flavor "${VERIFY_FLAVOR}" --workspace "${ROOT}"
fi

for command_name in docker python3 realpath sha512sum b2sum sha256sum flock df awk; do
	command -v "${command_name}" >/dev/null 2>&1 || die "missing prerequisite: ${command_name}"
done
if [[ -n ${SIGN_KEY} ]]; then
	[[ ${SIGN_KEY} =~ ^(0[xX])?[0-9A-Fa-f]{16}([0-9A-Fa-f]{24})?$ ]] \
		|| die "--sign-key must be a 16 or 40 digit hexadecimal key ID or fingerprint, not '${SIGN_KEY}'"
	[[ -z ${TEST_KEY_FILE} && -z ${GENTOO_TEST_SSH_PUBLIC_KEY:-} ]] \
		|| die "a test-key ISO is never signed: it has a login that must not be distributed"
	command -v gpg >/dev/null 2>&1 || die "missing prerequisite: gpg (needed by --sign-key)"
	gpg --batch --list-secret-keys -- "${SIGN_KEY}" >/dev/null 2>&1 \
		|| die "there is no secret key ${SIGN_KEY} in your GnuPG keyring (--sign-key)"
fi
[[ $(uname -m) == x86_64 ]] || die "an x86_64 host is required for this AMD64 builder"
[[ ${JOBS} =~ ^[1-9][0-9]*$ ]] || die "JOBS must be a positive integer"
[[ ${LOAD_AVERAGE} =~ ^[0-9]+([.][0-9]+)?$ ]] || die "LOAD_AVERAGE must be numeric"
[[ ${BUILD_MEMORY} =~ ^[1-9][0-9]*[mMgG]$ ]] || die "BUILD_MEMORY must look like 8g or 6144m"
[[ ${BUILD_MIN_FREE_GIB} =~ ^[0-9]+$ && ${ISO_DIST_MIN_FREE_GIB} =~ ^[0-9]+$ && ${DOCKER_MIN_FREE_GIB} =~ ^[0-9]+$ ]] \
	|| die "BUILD_MIN_FREE_GIB, ISO_DIST_MIN_FREE_GIB and DOCKER_MIN_FREE_GIB must be whole numbers"
[[ ${ISO_SECCOMP} == unconfined || ${ISO_SECCOMP} == default ]] || die "ISO_SECCOMP must be unconfined or default"
[[ ${ISO_KEEP_STAGE1} =~ ^[0-9]+$ ]] || die "ISO_KEEP_STAGE1 must be a whole number"
[[ ${GENTOO_TREEISH} =~ ^[0-9a-f]{40}$ ]] || die "GENTOO_TREEISH must be a full commit ID"
[[ ${GENTOO_TREEISH_SIGNER_FPR} =~ ^[0-9A-Fa-f]{40}$ ]] || die "GENTOO_TREEISH_SIGNER_FPR must be a 40 digit key fingerprint"
[[ ${STAGE3_SHA512} =~ ^[0-9a-f]{128}$ ]] || die "invalid stage3 SHA-512 pin"
[[ ${STAGE3_BLAKE2B} =~ ^[0-9a-f]{128}$ ]] || die "invalid stage3 BLAKE2b pin"
[[ ${ROOT} != *$'\n'* && ${ROOT} != *,* ]] || die "workspace path cannot contain commas or newlines"
[[ ${CACHE_DIR} != *$'\n'* && ${CACHE_DIR} != *,* && ${DIST_DIR} != *$'\n'* && ${DIST_DIR} != *,* ]] \
	|| die "cache/dist paths cannot contain commas or newlines"

# --- image flavour: release, or a test image with a temporary root ssh key -------------
TEST_KEY=${GENTOO_TEST_SSH_PUBLIC_KEY:-}
if [[ -n ${TEST_KEY_FILE} ]]; then
	[[ -f ${TEST_KEY_FILE} && ! -L ${TEST_KEY_FILE} ]] || die "--test-key-file must name a regular file: ${TEST_KEY_FILE}"
	! grep -q 'PRIVATE KEY' -- "${TEST_KEY_FILE}" || die "${TEST_KEY_FILE} holds a private key; pass the .pub file"
	TEST_KEY=$(awk 'NF { print; exit }' "${TEST_KEY_FILE}")
	[[ -n ${TEST_KEY} ]] || die "${TEST_KEY_FILE} holds no public key"
fi
if [[ -n ${TEST_KEY} ]]; then
	[[ ${TEST_KEY} =~ ^ssh-[a-z0-9-]+\ [A-Za-z0-9+/=]+(\ [^[:cntrl:]]*)?$ ]] \
		|| die "the test key must be one OpenSSH public key line (ssh-ed25519 AAAA... comment)"
	# A test image has a login that anybody with the key can use: it must never land in the
	# directory a release is taken from.
	[[ -n ${ISO_DIST_DIR:-} ]] \
		|| die "a test-key ISO must not use the default dist/: set ISO_DIST_DIR to a directory of its own (for example dist/test-only)"
	[[ $(realpath -m -- "${ISO_DIST_DIR}") != "$(realpath -m -- "${ROOT}/dist")" ]] \
		|| die "a test-key ISO must not be written to dist/, which is where releases go; choose another ISO_DIST_DIR"
	ISO_FLAVOR=testkey
else
	ISO_FLAVOR=release
fi
iso_apply_flavor "${ISO_FLAVOR}" || die "cannot derive the ${ISO_FLAVOR} ISO names"

# --- inputs -----------------------------------------------------------------------------
required_files=(
	pyproject.toml
	data/gentoo-installer.desktop
	data/gentoo-rescue.desktop
	iso/Containerfile
	iso/builder-entrypoint.sh
	iso/catalyst-no-loop.py
	iso/fsscript-stage2.sh
	iso/image-contract.json
	iso/make_conf_keys.py
	iso/render-config.py
	iso/stage_reuse.py
	iso/verify-snapshot.sh
	iso/write-manifest.py
	iso/README-live.txt
	iso/lib/naming.sh
	iso/lib/overlay.sh
	iso/lib/run.sh
	iso/cd_overlay/boot/grub/grub.cfg.in
	iso/cd_overlay/SECURE-BOOT-OFF.txt
	iso/specs/livecd-stage1.spec
	iso/specs/livecd-stage2.spec
	scripts/verify-iso.py
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
# Each local package has exactly one ebuild; its name carries the version and revision.
for package in app-admin/gentoo-live-installer app-misc/hwmonux app-portage/portagehub; do
	ebuilds=("${ROOT}/packaging/gentoo-overlay/${package}/${package#*/}"-[0-9]*.ebuild)
	[[ ${#ebuilds[@]} -eq 1 && -f ${ebuilds[0]} ]] \
		|| die "${package} needs exactly one ebuild in packaging/gentoo-overlay (remove the old revision in the same change)"
done
[[ -d ${ROOT}/src/gentoo_installer ]] || die "src/gentoo_installer is missing"
[[ -d ${ROOT}/src/gentoo_installerd ]] || die "src/gentoo_installerd is missing"
[[ -d ${ROOT}/apps/portagehub/src ]] || die "apps/portagehub/src is missing"

docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 \
	|| die "Docker daemon is unavailable or not accessible to $(id -un)"
docker_security=$(docker info --format '{{.SecurityOptions}}' 2>/dev/null || true)
if grep -q rootless <<<"${docker_security}"; then
	die "rootless Docker is unsupported; Catalyst requires isolated mount operations"
fi

# --- free space: cache, output and Docker's own storage are checked as the filesystems
# they live on (one filesystem is asked for the sum) -------------------------------------
nearest_existing() {
	local path=$1
	while [[ ! -e ${path} && ${path} != / ]]; do
		path=$(dirname -- "${path}")
	done
	printf '%s\n' "${path}"
}

check_free_space() {
	local -A required=() available=() labels=()
	local label path need row key
	local docker_root
	docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
	local entries=("cache|${CACHE_DIR}|${BUILD_MIN_FREE_GIB}" "output|${DIST_DIR}|${ISO_DIST_MIN_FREE_GIB}")
	if [[ -n ${docker_root} && -d ${docker_root} ]]; then
		entries+=("docker|${docker_root}|${DOCKER_MIN_FREE_GIB}")
	else
		warn "cannot read Docker's storage directory; its free space is not checked"
	fi
	local entry
	for entry in "${entries[@]}"; do
		IFS='|' read -r label path need <<<"${entry}"
		row=$(df -Pk -- "$(nearest_existing "$(realpath -m -- "${path}")")" | awk 'NR == 2 { print $1 "|" $4 "|" $6 }')
		[[ ${row} =~ \|[0-9]+\| ]] || die "could not determine free disk space for ${label} (${path})"
		key=${row%%|*}:${row##*|}
		available[${key}]=$(awk -F'|' '{ print $2 }' <<<"${row}")
		required[${key}]=$((${required[${key}]:-0} + need * 1024 * 1024))
		labels[${key}]+="${labels[${key}]:+, }${label}"
	done
	local failed=0
	for key in "${!required[@]}"; do
		note "free space on ${key##*:} (${labels[${key}]}): need $((required[${key}] / 1024 / 1024)) GiB, have $((available[${key}] / 1024 / 1024)) GiB"
		if ((available[${key}] < required[${key}])); then
			printf 'build-iso: not enough free space on %s for %s\n' "${key##*:}" "${labels[${key}]}" >&2
			failed=1
		fi
	done
	((failed == 0)) || die "free some disk space, or set BUILD_MIN_FREE_GIB / ISO_CACHE_DIR / ISO_DIST_DIR"
}
check_free_space

warn_if_memory_is_short() {
	local want_kib have_kib
	case ${BUILD_MEMORY: -1} in
		m | M) want_kib=$((${BUILD_MEMORY%?} * 1024)) ;;
		*) want_kib=$((${BUILD_MEMORY%?} * 1024 * 1024)) ;;
	esac
	have_kib=$(awk '/^MemAvailable:/ { print $2; exit }' "${PROC_MEMINFO}" 2>/dev/null || true)
	if [[ ${have_kib} =~ ^[0-9]+$ ]] && ((have_kib < want_kib + 2 * 1024 * 1024)); then
		warn "only $((have_kib / 1024)) MiB of memory are available; the build container may use ${BUILD_MEMORY} and the host needs 2 GiB more"
	fi
}
warn_if_memory_is_short

if ((CHECK_ONLY)); then
	printf 'build-iso: prerequisites and pinned inputs look valid; no build started.\n'
	printf 'build-iso: stage3 %s\n' "${STAGE3_BASENAME}"
	printf 'build-iso: Gentoo snapshot %s\n' "${GENTOO_TREEISH}"
	printf 'build-iso: flavour %s -> %s (volume %s)\n' "${ISO_FLAVOR}" "${ISO_NAME}" "${ISO_VOLID}"
	exit 0
fi

CACHE_DIR=$(realpath -m -- "${CACHE_DIR}")
DIST_DIR=$(realpath -m -- "${DIST_DIR}")
[[ ! -L ${CACHE_DIR} && ! -L ${DIST_DIR} ]] || die "cache and dist paths must not be symlinks"
mkdir -p "${CACHE_DIR}/catalyst" "${CACHE_DIR}/distfiles" "${CACHE_DIR}/cargo" "${DIST_DIR}"

# One build at a time per cache: the Catalyst store holds chroots that a second build
# would delete under the first one's feet.
exec 9>"${CACHE_DIR}/.build.lock"
flock -n 9 || die "another build is using ${CACHE_DIR}"

LOG_DIR=$(realpath -m -- "${ISO_LOG_DIR:-"${DIST_DIR}/logs"}")
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/build-$(date -u +%Y%m%dT%H%M%SZ).log"
: >"${LOG_FILE}"
note "log file: ${LOG_FILE}"
CONTAINER_NAME="gentoo-iso-$(printf '%s' "${CACHE_DIR}" | sha256sum | cut -c1-12)"

# run_logged COMMAND...: run a command, show its output and keep a copy in the log;
# returns the command's own exit status.
run_logged() {
	local status
	set +e
	"$@" 2>&1 | tee -a "${LOG_FILE}"
	status=${PIPESTATUS[0]}
	set -e
	return "${status}"
}

image="localhost/gentoo-installer-catalyst:${GENTOO_TREEISH:0:12}"
note "building pinned Gentoo builder image ${image}"
if ! run_logged docker build --pull=false --build-arg "GENTOO_TREEISH=${GENTOO_TREEISH}" \
	--build-arg "GENTOO_TREEISH_SIGNER_FPR=${GENTOO_TREEISH_SIGNER_FPR}" \
	--file "${ROOT}/iso/Containerfile" --tag "${image}" "${ROOT}/iso"; then
	die "Gentoo builder image creation failed; no ISO was accepted (log: ${LOG_FILE})"
fi
image_id=$(docker image inspect --format '{{.Id}}' "${image}" 2>/dev/null || true)

if ((CLEAN)); then
	note "cleaning ${CACHE_DIR}/catalyst (keeping ${ISO_KEEP_STAGE1} stage1 archives)"
	run_logged docker run --rm --init --platform linux/amd64 --entrypoint /bin/bash \
		--env "KEEP=${ISO_KEEP_STAGE1}" --env "CLEAN_PACKAGES=${CLEAN_PACKAGES}" \
		--mount "type=bind,source=${CACHE_DIR}/catalyst,target=/var/tmp/catalyst" \
		"${image}" -c '
set -eu
cd /var/tmp/catalyst
rm -rf -- tmp/* 2>/dev/null || true
find builds -name "*.partial" -o -name ".*.partial" | xargs -r rm -f --
# newest first; keep $KEEP archives with their receipts and digests
ls -1t builds/23.0-default/livecd-stage1-amd64-*.tar.gz 2>/dev/null | grep -v -- -completed | tail -n +$((KEEP + 1)) | while read -r archive; do
	rm -f -- "$archive" "$archive".*
done
rm -f -- builds/23.0-default/*-completed.tar.gz
if [ "$CLEAN_PACKAGES" = 1 ]; then rm -rf -- packages/*; fi
du -sh . 2>/dev/null || true
' || die "cleaning failed (log: ${LOG_FILE})"
	exit 0
fi

# --- the build --------------------------------------------------------------------------
if docker inspect --type container "${CONTAINER_NAME}" >/dev/null 2>&1; then
	die "a build container named ${CONTAINER_NAME} exists (running, or left over from a killed build): docker stop ${CONTAINER_NAME}; docker rm ${CONTAINER_NAME}"
fi

case ${REUSE} in
	auto) build_mode=auto ;;
	none) build_mode=full ;;
	stage1) build_mode=stage2-only ;;
esac
((STAGE1_ONLY == 0)) || build_mode=stage1-only

run_args=(run --init --platform linux/amd64 --name "${CONTAINER_NAME}"
	--cap-add SYS_ADMIN --cap-add NET_ADMIN --cap-add MKNOD --cap-add SYS_CHROOT
	--shm-size 1g --pids-limit 4096
	--cpus "${JOBS}" --memory "${BUILD_MEMORY}" --memory-swap "${BUILD_MEMORY}")
# Catalyst needs mount(2) and chroot(2). The seccomp profile stays open until a build has
# been shown to work with Docker's default one; AppArmor is only touched where it exists.
if [[ ${ISO_SECCOMP} == unconfined ]]; then
	run_args+=(--security-opt seccomp=unconfined)
fi
if grep -q apparmor <<<"${docker_security}"; then
	run_args+=(--security-opt apparmor=unconfined)
fi
run_args+=(
	--env "JOBS=${JOBS}" --env "LOAD_AVERAGE=${LOAD_AVERAGE}"
	--env "BUILD_MODE=${build_mode}" --env "ISO_FLAVOR=${ISO_FLAVOR}"
	--env "CATALYST_BUILDER_IMAGE=${image}" --env "BUILDER_IMAGE_ID=${image_id}"
	--env "GENTOO_TEST_SSH_PUBLIC_KEY=${TEST_KEY}"
	--env "HOST_UID=$(id -u)" --env "HOST_GID=$(id -g)"
	--mount "type=bind,source=${ROOT},target=/workspace,readonly"
	--mount "type=bind,source=${CACHE_DIR}/catalyst,target=/var/tmp/catalyst"
	--mount "type=bind,source=${CACHE_DIR}/distfiles,target=/var/cache/catalyst/distfiles"
	--mount "type=bind,source=${CACHE_DIR}/cargo,target=/var/cache/cargo"
	--mount "type=bind,source=${DIST_DIR},target=/dist"
	"${image}")

if [[ ${ISO_FLAVOR} == testkey ]]; then
	warn "TEST BUILD: this ISO has a temporary root ssh login and must never be distributed (${ISO_NAME})"
fi
note "starting Catalyst in container ${CONTAINER_NAME} (${BUILD_MEMORY} of memory, ${JOBS} jobs, mode ${build_mode}); this is a long build."

# Ctrl-C and `kill` stop the container (Catalyst gets its signal and the mounts go with
# the container) instead of leaving it running.
RUN_PID=
stop_container() {
	local signal=$1 code=$2
	trap - INT TERM
	warn "${signal} received; stopping container ${CONTAINER_NAME}"
	docker stop --time 120 "${CONTAINER_NAME}" >/dev/null 2>&1 || true
	[[ -z ${RUN_PID} ]] || wait "${RUN_PID}" 2>/dev/null || true
	docker rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true
	note "log file: ${LOG_FILE}"
	exit "${code}"
}
trap 'stop_container INT 130' INT
trap 'stop_container TERM 143' TERM

set +e
(
	docker "${run_args[@]}" 2>&1 | tee -a "${LOG_FILE}"
	exit "${PIPESTATUS[0]}"
) &
RUN_PID=$!
wait "${RUN_PID}"
build_status=$?
RUN_PID=
set -e
trap - INT TERM

state=$(docker inspect --format '{{.State.OOMKilled}}' "${CONTAINER_NAME}" 2>/dev/null || true)
docker rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true

if ((build_status != 0)); then
	printf 'build-iso: the build failed with status %s. Last lines of %s:\n' "${build_status}" "${LOG_FILE}" >&2
	tail -n 40 "${LOG_FILE}" >&2 || true
	if [[ ${state} == true ]]; then
		last_package=$(grep -a 'Emerging (' "${LOG_FILE}" | tail -n 1 || true)
		printf "build-iso: the build container was OOM-killed near '%s'; raise BUILD_MEMORY (now %s) or lower JOBS (now %s)\n" \
			"${last_package:-the start of the build}" "${BUILD_MEMORY}" "${JOBS}" >&2
	fi
	die "Catalyst build failed; artifacts were not accepted (log: ${LOG_FILE})"
fi

if ((STAGE1_ONLY)); then
	note "stage1 and its receipt are in ${CACHE_DIR}/catalyst/builds/23.0-default"
	exit 0
fi

iso="${DIST_DIR}/${ISO_NAME}"
for artifact in "${iso}" "${iso}.sha512" "${iso}.blake2b" "${iso}.manifest.json" "${iso}.packages.txt"; do
	[[ -s ${artifact} ]] || die "builder did not publish expected artifact: ${artifact}"
done
(cd "${DIST_DIR}" && sha512sum -c "${ISO_NAME}.sha512" && b2sum -c "${ISO_NAME}.blake2b")
python3 - "${iso}.manifest.json" "${ISO_NAME}" "${ISO_FLAVOR}" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    manifest = json.load(stream)
if manifest.get("iso", {}).get("name") != sys.argv[2]:
    raise SystemExit("manifest does not describe the published ISO")
if manifest.get("flavor") != sys.argv[3] or manifest.get("test_ssh_enabled") != (sys.argv[3] == "testkey"):
    raise SystemExit("manifest flavour does not match this build")
if not manifest.get("verified", {}).get("ok"):
    raise SystemExit("manifest does not record a passed image verification")
PY
if [[ -n ${SIGN_KEY} ]]; then
	# On the host, after the container is gone: the key and its passphrase never enter Docker.
	for signed in "${iso}.sha512" "${iso}.manifest.json"; do
		rm -f -- "${signed}.asc"
		gpg --batch --yes --local-user "${SIGN_KEY}" --armor --detach-sign --output "${signed}.asc" -- "${signed}" \
			|| die "could not sign ${signed} with key ${SIGN_KEY}"
		gpg --batch --verify "${signed}.asc" "${signed}" >/dev/null 2>&1 || die "the new signature ${signed}.asc does not verify"
	done
fi
note "verified ISO: ${iso}"
note "SHA-512: ${iso}.sha512"
note "BLAKE2b: ${iso}.blake2b"
note "manifest: ${iso}.manifest.json"
if [[ -n ${SIGN_KEY} ]]; then
	note "signatures: ${iso}.sha512.asc, ${iso}.manifest.json.asc (key ${SIGN_KEY})"
fi
note "installed packages: ${iso}.packages.txt"
note "build log: ${LOG_FILE}"
