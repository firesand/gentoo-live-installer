#!/bin/bash
# Runs inside the builder container (see iso/Containerfile and scripts/build-iso.sh).
#
# Environment from scripts/build-iso.sh: JOBS, LOAD_AVERAGE, BUILD_MODE, ISO_FLAVOR,
# GENTOO_TEST_SSH_PUBLIC_KEY, CATALYST_BUILDER_IMAGE, BUILDER_IMAGE_ID, HOST_UID, HOST_GID.
# BUILD_MODE: full (default), stage1-only, stage2-only, reseed or auto (choose from the
# receipt of the last stage1, see iso/stage_reuse.py: auto reuses a stage1 that nothing changed
# and builds stage1 fresh otherwise; it never reseeds, only BUILD_MODE=reseed does).
set -Eeuo pipefail

WORKSPACE=${WORKSPACE:-/workspace}
BUILD_DIR=${BUILD_DIR:-/build}
CATALYST_STORE=${CATALYST_STORE:-/var/tmp/catalyst}
DISTDIR=${DISTDIR:-/var/cache/catalyst/distfiles}
CARGO_CACHE=${CARGO_CACHE:-/var/cache/cargo}
DIST=${DIST:-/dist}
readonly WORKSPACE BUILD_DIR CATALYST_STORE DISTDIR CARGO_CACHE DIST

log() {
	printf '\n==> %s\n' "$*"
}

die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

# json_field NAME: print one top-level string field of the JSON document on stdin.
json_field() {
	python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

load_environment() {
	JOBS=${JOBS:-2}
	LOAD_AVERAGE=${LOAD_AVERAGE:-$((JOBS * 2))}
	BUILD_MODE=${BUILD_MODE:-full}
	[[ ${JOBS} =~ ^[1-9][0-9]*$ ]] || die "JOBS must be a positive integer"
	[[ ${LOAD_AVERAGE} =~ ^[0-9]+([.][0-9]+)?$ ]] || die "LOAD_AVERAGE must be numeric"
	case ${BUILD_MODE} in
		auto | full | stage1-only | stage2-only | reseed) ;;
		*) die "unknown BUILD_MODE '${BUILD_MODE}' (auto, full, stage1-only, stage2-only or reseed)" ;;
	esac
	set -a
	# shellcheck source=iso/pins.env
	source "${WORKSPACE}/iso/pins.env"
	set +a
	[[ ${GENTOO_TREEISH} =~ ^[0-9a-f]{40}$ ]] || die "invalid Gentoo treeish"
	[[ ${GENTOO_TREEISH_SIGNER_FPR} =~ ^[0-9A-Fa-f]{40}$ ]] || die "invalid GENTOO_TREEISH_SIGNER_FPR"
	[[ ${SOURCE_DATE_EPOCH} =~ ^[0-9]+$ ]] || die "invalid SOURCE_DATE_EPOCH"
	# shellcheck source=iso/lib/naming.sh
	source "${WORKSPACE}/iso/lib/naming.sh"
	# shellcheck source=iso/lib/overlay.sh
	source "${WORKSPACE}/iso/lib/overlay.sh"
	# shellcheck source=iso/lib/run.sh
	source "${WORKSPACE}/iso/lib/run.sh"

	# A test key makes this a test ISO, and the other way round.
	local flavor=release
	[[ -z ${GENTOO_TEST_SSH_PUBLIC_KEY:-} ]] || flavor=testkey
	if [[ -n ${ISO_FLAVOR:-} && ${ISO_FLAVOR} != "${flavor}" ]]; then
		die "ISO_FLAVOR=${ISO_FLAVOR} does not match the test key setting (${flavor})"
	fi
	iso_apply_flavor "${flavor}" || die "cannot derive the ${flavor} ISO names"
	export ISO_NAME ISO_VOLID ISO_FLAVOR
	STAGE1_ARCHIVE="${CATALYST_STORE}/builds/23.0-default/livecd-stage1-amd64-${VERSION_STAMP}.tar.gz"
	STAGE1_MODE=fresh
}

# package_version CATEGORY/NAME: the version of the single ebuild in the overlay, without
# the revision. The ebuild file name is the one place a version is written down.
package_version() {
	local name=${1#*/} file
	local -a found
	found=("${WORKSPACE}/packaging/gentoo-overlay/$1/${name}"-[0-9]*.ebuild)
	[[ ${#found[@]} -eq 1 && -f ${found[0]} ]] || die "$1 needs exactly one ebuild in the overlay (remove the old revision)"
	file=${found[0]##*/}
	file=${file%.ebuild}
	file=${file#"${name}-"}
	printf '%s\n' "${file%-r[0-9]*}"
}

prepare_source_archives() {
	local sources="${BUILD_DIR}/sources"
	local installer hwmonux portagehub package
	installer="gentoo-live-installer-$(package_version app-admin/gentoo-live-installer)"
	hwmonux="hwmonux-$(package_version app-misc/hwmonux)"
	portagehub="portagehub-$(package_version app-portage/portagehub)"

	install -d "${sources}/${installer}" "${sources}/${hwmonux}" "${sources}/${portagehub}" "${DISTDIR}"
	tar -C "${WORKSPACE}" --exclude=__pycache__ --exclude='*.pyc' -cf - pyproject.toml src data \
		| tar -C "${sources}/${installer}" -xf -
	tar -C "${WORKSPACE}/apps/hwmonux" --exclude=__pycache__ --exclude='*.pyc' -cf - \
		pyproject.toml hwmonux.py sensors.py hwmonux.desktop \
		| tar -C "${sources}/${hwmonux}" -xf -
	tar -C "${WORKSPACE}/apps/portagehub" -cf - Cargo.toml Cargo.lock README.md SECURITY.md UPSTREAM src dist \
		| tar -C "${sources}/${portagehub}" -xf -

	install -d "${sources}/${portagehub}/.cargo"
	(
		cd "${sources}/${portagehub}"
		CARGO_HOME="${CARGO_CACHE}" cargo vendor --locked --versioned-dirs vendor > .cargo/config.toml
	)

	for package in "${installer}" "${hwmonux}" "${portagehub}"; do
		tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --clamp-mtime \
			--owner=0 --group=0 --numeric-owner --format=gnu \
			-C "${sources}" -cf "${DISTDIR}/${package}.tar" "${package}"
		zstd -q -f -12 -T1 --rm "${DISTDIR}/${package}.tar"
	done
}

# copy_vendored_distfiles OVERLAY DEST: the distfiles named by the committed Manifests of vendored
# packages (the fetch above verified them) go next to the project's own archives, so that an
# installation without a network builds those packages too.
copy_vendored_distfiles() {
	local manifest name
	for manifest in "$1"/*/*/Manifest; do
		[[ -f ${manifest} ]] || continue
		while read -r name; do
			[[ -n ${name} ]] || continue
			[[ -f ${DISTDIR}/${name} ]] || die "the distfile ${name} of ${manifest#"$1"/} is missing from ${DISTDIR}"
			cp -a --no-preserve=ownership -- "${DISTDIR}/${name}" "$2/"
		done < <(awk '$1 == "DIST" { print $2 }' "${manifest}")
	done
}

prepare_portage() {
	local ebuild overlay_copy="${BUILD_DIR}/root_overlay/usr/local/share/gentoo-installer/overlay"
	copy_tree_without_owner "${WORKSPACE}/packaging/gentoo-overlay" "${BUILD_DIR}/overlay"
	copy_tree_without_owner "${WORKSPACE}/iso/portage" "${BUILD_DIR}/portage"
	copy_tree_without_owner "${WORKSPACE}/iso/root_overlay" "${BUILD_DIR}/root_overlay"
	copy_tree_without_owner "${WORKSPACE}/iso/fsscript-stage2.sh" "${BUILD_DIR}/fsscript-stage2.sh"
	install -d "${BUILD_DIR}/root_overlay/usr/local/share/gentoo-installer/distfiles"
	cp -a --no-preserve=ownership -- "${DISTDIR}"/gentoo-live-installer-*.tar.zst "${DISTDIR}"/hwmonux-*.tar.zst \
		"${DISTDIR}"/portagehub-*.tar.zst "${BUILD_DIR}/root_overlay/usr/local/share/gentoo-installer/distfiles/"
	for ebuild in "${BUILD_DIR}"/overlay/*/*/*.ebuild; do
		if [[ -f ${ebuild%/*}/Manifest ]]; then
			# A vendored package (hardware item I01) ships its Manifest: that file is the pin. Fetching
			# checks the tarball against it and fails on any difference; `manifest` would write
			# whatever the server sent today.
			env DISTDIR="${DISTDIR}" ebuild "${ebuild}" fetch
		else
			env DISTDIR="${DISTDIR}" ebuild "${ebuild}" manifest
		fi
	done
	copy_vendored_distfiles "${BUILD_DIR}/overlay" "${BUILD_DIR}/root_overlay/usr/local/share/gentoo-installer/distfiles"
	# The installed target needs the same authenticated distfile manifests.
	copy_tree_without_owner "${BUILD_DIR}/overlay" "${overlay_copy}"

	# Specs, make.conf (+ make.conf.extra), README and the boot menu from their templates.
	python3 "${WORKSPACE}/iso/render-config.py" "${BUILD_DIR}" "${WORKSPACE}" "${JOBS}" "${LOAD_AVERAGE}"

	normalize_tree "${BUILD_DIR}/overlay"
	normalize_tree "${BUILD_DIR}/portage"
	normalize_tree "${BUILD_DIR}/root_overlay"
	normalize_tree "${BUILD_DIR}/cd_overlay"
	chmod 0755 "${BUILD_DIR}/fsscript-stage2.sh"
	chown 0:0 "${BUILD_DIR}/fsscript-stage2.sh"

	# The one definition of the emerge options is the rendered make.conf.
	EMERGE_DEFAULT_OPTS=$(python3 "${WORKSPACE}/iso/render-config.py" --get-make-conf-value EMERGE_DEFAULT_OPTS "${BUILD_DIR}/portage/make.conf")
	export EMERGE_DEFAULT_OPTS
	export GENTOO_INSTALLER_MAKE_CONF_EXTRA="${BUILD_DIR}/portage/make.conf.extra"
}

# A test ISO carries a temporary root SSH key for the VM harness. Its name, its volume
# label and its login screens say so; scripts/verify-iso.py insists on all of it. sshd itself
# is in livecd/rcadd of the spec on every flavour; the test drop-in 90-installer-vm-test.conf
# sorts before the live image's own 9999999gentoo-livecd.conf and wins (sshd takes the first value).
# It changes root's login only: password logins of the user gentoo (gentoo-remote-access on) must
# work as on a release image, where PAM asks for the password through keyboard-interactive
# (9999999gentoo-pam.conf turns PasswordAuthentication off), so neither method is switched off here.
configure_test_access() {
	local key="${GENTOO_TEST_SSH_PUBLIC_KEY:-}" banner file
	[[ -n ${key} ]] || return 0
	[[ ${key} == ssh-* && ${key} != *$'\n'* && ${key} != *$'\r'* ]] \
		|| die "GENTOO_TEST_SSH_PUBLIC_KEY must be one public key line"
	log "Enabling ephemeral SSH access for this test-only ISO; do not distribute it"
	# The entrypoint runs as root, so everything created here is root-owned.
	install -d -m 0700 "${BUILD_DIR}/root_overlay/root/.ssh"
	printf '%s\n' "${key}" > "${BUILD_DIR}/root_overlay/root/.ssh/authorized_keys"
	chmod 0600 "${BUILD_DIR}/root_overlay/root/.ssh/authorized_keys"
	install -d -m 0755 "${BUILD_DIR}/root_overlay/etc/ssh/sshd_config.d"
	cat > "${BUILD_DIR}/root_overlay/etc/ssh/sshd_config.d/90-installer-vm-test.conf" <<'EOF'
PermitRootLogin prohibit-password
EOF
	touch "${BUILD_DIR}/root_overlay/etc/gentoo-installer-vm-test"
	banner="*** TEST BUILD - root SSH is enabled with a temporary key. Never distribute this image. ***"
	for file in issue motd; do
		{ printf '\n%s\n' "${banner}"; cat "${BUILD_DIR}/root_overlay/etc/${file}"; } > "${BUILD_DIR}/${file}.banner"
		install -m 0644 "${BUILD_DIR}/${file}.banner" "${BUILD_DIR}/root_overlay/etc/${file}"
		rm -f "${BUILD_DIR}/${file}.banner"
	done
}

fetch_and_verify_seed() {
	local seed_dir="${CATALYST_STORE}/builds/23.0-default"
	local seed="${seed_dir}/${STAGE3_BASENAME}"
	local digests="${seed}.DIGESTS"
	local url="${STAGE3_URL_BASE}/${STAGE3_BASENAME}"
	local recorded
	install -d "${seed_dir}"
	if [[ ! -s ${seed} ]]; then
		log "Downloading pinned AMD64 stage3 seed"
		curl --proto '=https' --tlsv1.2 -fL --retry 4 -o "${seed}.partial" "${url}"
		mv "${seed}.partial" "${seed}"
	fi
	if [[ ! -s ${digests} ]]; then
		curl --proto '=https' --tlsv1.2 -fL --retry 4 -o "${digests}.partial" "${url}.DIGESTS"
		mv "${digests}.partial" "${digests}"
	fi
	gpg --batch --yes --dearmor --output "${BUILD_DIR}/gentoo-release.gpg" /usr/share/openpgp-keys/gentoo-release.asc
	gpgv --keyring "${BUILD_DIR}/gentoo-release.gpg" "${digests}"
	recorded=$(awk -v file="${STAGE3_BASENAME}" '$2 == file { print $1; exit }' "${digests}")
	[[ ${recorded} == "${STAGE3_SHA512}" ]] || die "signed stage3 digest differs from pins.env"
	(cd "${seed_dir}" && printf '%s  %s\n' "${STAGE3_SHA512}" "${STAGE3_BASENAME}" | sha512sum -c -)
	(cd "${seed_dir}" && printf '%s  %s\n' "${STAGE3_BLAKE2B}" "${STAGE3_BASENAME}" | b2sum -c -)
}

prepare_snapshot() {
	local repo="${CATALYST_STORE}/repos/gentoo.git"
	local snapshots="${CATALYST_STORE}/snapshots"
	local tree="${snapshots}/gentoo-${GENTOO_TREEISH}.tree"
	local temporary_tree
	local actual
	install -d "${CATALYST_STORE}/repos"
	if [[ ! -d ${repo} ]]; then
		git init --bare --quiet "${repo}"
		git -C "${repo}" remote add origin "${GENTOO_REPOSITORY}"
	else
		git -C "${repo}" remote set-url origin "${GENTOO_REPOSITORY}"
	fi
	if ! git -C "${repo}" cat-file -e "${GENTOO_TREEISH}^{commit}" 2>/dev/null; then
		log "Fetching pinned Gentoo repository snapshot"
		git -C "${repo}" fetch --depth=1 origin "${GENTOO_TREEISH}"
	fi
	actual=$(git -C "${repo}" rev-parse "${GENTOO_TREEISH}^{commit}")
	[[ ${actual} == "${GENTOO_TREEISH}" ]] || die "Gentoo repository resolved to the wrong commit"
	git -C "${repo}" fsck --connectivity-only --no-dangling
	# The commit must be signed by the developer key that iso/pins.env names.
	"${WORKSPACE}/iso/verify-snapshot.sh" "${repo}" "${GENTOO_TREEISH}" "${GENTOO_TREEISH_SIGNER_FPR}" \
		"${BUILD_DIR}/gentoo-signature.json"
	install -d "${snapshots}"
	if [[ ! -f ${tree}/.catalyst-snapshot.json ]]; then
		temporary_tree=$(mktemp -d "${snapshots}/.gentoo-${GENTOO_TREEISH}.XXXXXX")
		git -C "${repo}" archive "${GENTOO_TREEISH}" | tar -xf - -C "${temporary_tree}"
		[[ $(cat "${temporary_tree}/profiles/repo_name") == gentoo ]] || die "Pinned repository archive has the wrong name"
		[[ -f ${temporary_tree}/metadata/layout.conf ]] || die "Pinned repository archive is incomplete"
		printf '{"repo_name":"gentoo","treeish":"%s"}\n' "${GENTOO_TREEISH}" > "${temporary_tree}/.catalyst-snapshot.json"
		rm -rf -- "${tree}"
		mv -- "${temporary_tree}" "${tree}"
	fi
	# mktemp creates a 0700 top directory. Portage's unprivileged metadata
	# workers must traverse the repository, including cached extractions.
	chmod 0755 "${tree}"
	run_child catalyst -c "${BUILD_DIR}/catalyst.conf" -s "${GENTOO_TREEISH}"
	[[ -s ${CATALYST_STORE}/snapshots/gentoo-${GENTOO_TREEISH}.sqfs ]] \
		|| die "Catalyst did not create the pinned repository snapshot"
}

# write_catalyst_config
# seedcache lets stage2 start from the stage1 chroot that was just built. When the stage1
# archive is the input (stage2-only, reseed) it must not: a retained chroot directory from
# an earlier run would win over the archive that was verified against its receipt.
write_catalyst_config() {
	local options='["pkgcache"]'
	[[ ${PLAN} != full ]] || options='["seedcache", "pkgcache"]'
	cat > "${BUILD_DIR}/catalyst.conf" <<EOF
storedir = "${CATALYST_STORE}"
distdir = "${DISTDIR}"
target_distdir = "${DISTDIR}"
target_pkgdir = "/var/cache/binpkgs"
target_logdir = "${CATALYST_STORE}/logs"
repos_storedir = "${CATALYST_STORE}/repos"
repo_openpgp_key_path = ""
digests = ["sha512", "blake2b"]
options = ${options}
jobs = ${JOBS}
load-average = ${LOAD_AVERAGE}
# Names of the variables Catalyst copies from its own environment into the chroot; the values
# always come from the environment (catalyst/base/targetbase.py, wiki.gentoo.org/wiki/Catalyst).
envvars = ["SOURCE_DATE_EPOCH", "EMERGE_DEFAULT_OPTS"]
EOF
}

catalyst_version() {
	catalyst --version 2>&1 | head -n 1
}

# Decide what to do about stage1 from BUILD_MODE and the receipt of the last stage1.
# Sets PLAN (full, stage1-only, stage2-only or reseed), STAGE1_MODE (fresh, reuse or
# reseed) and RESEED_ATOMS.
RESEED_ATOMS=
resolve_build_plan() {
	local policy=auto decision action
	case ${BUILD_MODE} in
		full | stage1-only)
			PLAN=${BUILD_MODE}
			STAGE1_MODE=fresh
			return 0
			;;
		stage2-only) policy=stage1 ;;
	esac
	decision=$(python3 "${WORKSPACE}/iso/stage_reuse.py" decide --workspace "${WORKSPACE}" --distfiles "${DISTDIR}" \
		--archive "${STAGE1_ARCHIVE}" --catalyst-version "$(catalyst_version)" --policy "${policy}") \
		|| die "stage1 cannot be reused (see above); run without --stage2-only, or build once with --reuse none"
	action=$(json_field action <<<"${decision}")
	log "Stage1 decision: ${action} - $(json_field reason <<<"${decision}")"
	case ${action} in
		reuse)
			PLAN=stage2-only
			STAGE1_MODE=reuse
			;;
		reseed)
			if [[ ${BUILD_MODE} == auto ]]; then
				# The first real reseed did not reinstall the changed package (the reinstall options
				# were written into catalyst.conf, where Catalyst ignores values: envvars only names
				# variables of its environment), so the image kept the old installer while its
				# receipt claimed the new one. That is fixed, but until a reseed has been checked in
				# a real build, stage1 is built fresh: the binary package cache keeps that short and
				# the project's own packages always come from this tree. BUILD_MODE=reseed still
				# runs the reseed.
				log "Stage1 is built fresh, not reseeded ($(json_field reason <<<"${decision}")): the project's packages are built from this tree"
				PLAN=full
				STAGE1_MODE=fresh
			else
				PLAN=reseed
				STAGE1_MODE=reseed
				RESEED_ATOMS=$(python3 -c 'import json, sys; print(" ".join(json.load(sys.stdin)["reinstall"]))' <<<"${decision}")
			fi
			;;
		*)
			[[ ${BUILD_MODE} == auto ]] || die "BUILD_MODE=${BUILD_MODE} needs a usable earlier stage1: $(json_field reason <<<"${decision}")"
			PLAN=full
			STAGE1_MODE=fresh
			;;
	esac
}

run_catalyst() {
	run_child python3 "${WORKSPACE}/iso/catalyst-no-loop.py" -c "${BUILD_DIR}/catalyst.conf" -f "$1"
}

# Write the receipt that lets a later run reuse this stage1.
record_stage1_receipt() {
	local receipt="${STAGE1_ARCHIVE}.receipt.json"
	[[ -s ${STAGE1_ARCHIVE} ]] || die "Catalyst finished stage1 without ${STAGE1_ARCHIVE}"
	python3 "${WORKSPACE}/iso/stage_reuse.py" receipt --workspace "${WORKSPACE}" --distfiles "${DISTDIR}" \
		--archive "${STAGE1_ARCHIVE}" --catalyst-version "$(catalyst_version)" \
		--image-id "${BUILDER_IMAGE_ID:-}" > "${receipt}.partial"
	mv -- "${receipt}.partial" "${receipt}"
}

build_stage1() {
	local seed="${STAGE1_ARCHIVE%.tar.gz}-completed.tar.gz" seed_subpath
	# The old receipt describes the old archive, which is overwritten at the end of stage1.
	rm -f -- "${STAGE1_ARCHIVE}.receipt.json"
	if [[ ${PLAN} == reseed ]]; then
		log "Reseeding Catalyst livecd-stage1 from the previous stage1 (reinstalling: ${RESEED_ATOMS})"
		cp --reflink=auto -- "${STAGE1_ARCHIVE}" "${seed}"
		seed_subpath="23.0-default/livecd-stage1-amd64-${VERSION_STAMP}-completed"
		python3 "${WORKSPACE}/iso/stage_reuse.py" reseed-spec "${BUILD_DIR}/livecd-stage1.spec" "${seed_subpath}" \
			> "${BUILD_DIR}/livecd-stage1-reseed.spec"
		local atom reinstall= saved_options=${EMERGE_DEFAULT_OPTS} started
		for atom in ${RESEED_ATOMS}; do
			reinstall+="${reinstall:+ }--reinstall-atoms=${atom}"
		done
		# Catalyst hands EMERGE_DEFAULT_OPTS from this environment to emerge in the chroot
		# (envvars); the reinstall options apply to this run only.
		export EMERGE_DEFAULT_OPTS="${saved_options} ${reinstall}"
		write_catalyst_config
		started=$(date +%s)
		run_catalyst "${BUILD_DIR}/livecd-stage1-reseed.spec"
		export EMERGE_DEFAULT_OPTS="${saved_options}"
		rm -f -- "${seed}"
		# Catalyst is just as content with a reseed that reinstalled nothing (the first real one did
		# exactly that), so the archive has to show it: every package must have been built since the
		# reseed started. Without that proof there is no receipt, and the next build starts from scratch.
		# shellcheck disable=SC2086
		python3 "${WORKSPACE}/iso/stage_reuse.py" reinstalled --archive "${STAGE1_ARCHIVE}" --since "${started}" ${RESEED_ATOMS} \
			|| die "the reseed did not reinstall ${RESEED_ATOMS}; ${STAGE1_ARCHIVE} has no receipt and is not used again. Build once with --reuse none"
	else
		log "Building Catalyst livecd-stage1"
		run_catalyst "${BUILD_DIR}/livecd-stage1.spec"
	fi
	record_stage1_receipt
}

publish_iso() {
	local built="${CATALYST_STORE}/builds/23.0-default/${ISO_NAME}"
	local verified="${BUILD_DIR}/verify-iso.json" packages="${BUILD_DIR}/packages.txt" size previous=()
	[[ -s ${built} ]] || die "Catalyst completed without producing ${built}"
	size=$(stat -c %s "${built}")
	((size > 10000000)) || die "Catalyst ISO is implausibly small"
	isoinfo -d -i "${built}" > /dev/null
	# Secure Boot: shim, a signed GRUB and a signed kernel, with a key made for this ISO only
	# (destroyed again by the script). verify-iso.py checks the result like everything else.
	log "Signing the UEFI boot files for Secure Boot (a key for this ISO only)"
	rm -rf -- "${BUILD_DIR}/secure-boot"
	python3 "${WORKSPACE}/iso/sign-secure-boot.py" "${built}" --work "${BUILD_DIR}/secure-boot" \
		--cert-out "${BUILD_DIR}/secure-boot.cer" --summary "${BUILD_DIR}/secure-boot.json" \
		--notice-out "${BUILD_DIR}/secure-boot.txt" \
		|| die "the ISO could not be prepared for Secure Boot; nothing was published"
	rm -rf -- "${BUILD_DIR}/secure-boot"
	# An earlier build of the same ISO name is the reference for the "grew by more than 20
	# percent" warning; it is replaced only after the checks have passed.
	[[ ! -f ${DIST}/${ISO_NAME}.manifest.json ]] || previous=(--previous-manifest "${DIST}/${ISO_NAME}.manifest.json")
	log "Verifying the finished ISO (${ISO_FLAVOR})"
	python3 "${WORKSPACE}/scripts/verify-iso.py" "${built}" --flavor "${ISO_FLAVOR}" --workspace "${WORKSPACE}" \
		--spec "${BUILD_DIR}/livecd-stage2.spec" \
		--json "${verified}" --packages-out "${packages}" --workdir "${BUILD_DIR}/verify-iso" \
		${previous[@]+"${previous[@]}"} \
		|| die "the ISO failed the image checks; nothing was published"
	rm -rf -- "${BUILD_DIR}/verify-iso"
	install -m 0644 "${built}" "${DIST}/.${ISO_NAME}.partial"
	mv "${DIST}/.${ISO_NAME}.partial" "${DIST}/${ISO_NAME}"
	install -m 0644 "${packages}" "${DIST}/${ISO_NAME}.packages.txt"
	# The certificate MokManager enrolls and its fingerprint, published next to the ISO so that a user
	# can compare it with "View key 0" (iso/cd_overlay/SECURE-BOOT.txt).
	install -m 0644 "${BUILD_DIR}/secure-boot.cer" "${DIST}/${ISO_NAME}.secureboot.cer"
	install -m 0644 "${BUILD_DIR}/secure-boot.txt" "${DIST}/${ISO_NAME}.secureboot.txt"
	(
		cd "${DIST}"
		sha512sum "${ISO_NAME}" > "${ISO_NAME}.sha512"
		b2sum "${ISO_NAME}" > "${ISO_NAME}.blake2b"
		sha512sum -c "${ISO_NAME}.sha512"
		b2sum -c "${ISO_NAME}.blake2b"
	)
	python3 "${WORKSPACE}/iso/write-manifest.py" --dist "${DIST}" --workspace "${WORKSPACE}" \
		--catalyst-version "$(catalyst_version)" --flavor "${ISO_FLAVOR}" --volid "${ISO_VOLID}" \
		--verified "${verified}" --signature "${BUILD_DIR}/gentoo-signature.json" \
		--stage1-mode "${STAGE1_MODE}" --stage1-receipt "${STAGE1_ARCHIVE}.receipt.json" \
		--builder-image-id "${BUILDER_IMAGE_ID:-}" --distfiles "${DISTDIR}" --packages "${DIST}/${ISO_NAME}.packages.txt" \
		--secure-boot "${BUILD_DIR}/secure-boot.json"
}

# On failure: say whether the container ran out of memory, then hand the outputs back to
# the user who started the build (the Catalyst store stays root-owned: it holds chroots).
on_exit() {
	local status=$?
	if ((status != 0)); then
		printf '\nbuilder: failed with status %s\n' "${status}" >&2
		local events
		for events in /sys/fs/cgroup/memory.events /sys/fs/cgroup/memory/memory.oom_control; do
			if [[ -r ${events} ]]; then
				printf 'builder: %s\n' "${events}" >&2
				cat "${events}" >&2 || true
			fi
		done
	fi
	if [[ ${HOST_UID:-} =~ ^[0-9]+$ && ${HOST_GID:-} =~ ^[0-9]+$ ]]; then
		chown -R "${HOST_UID}:${HOST_GID}" "${DIST}" "${DISTDIR}" "${CARGO_CACHE}" 2>/dev/null || true
	fi
}

main() {
	load_environment
	trap on_exit EXIT
	rm -rf "${BUILD_DIR}"
	install -d "${BUILD_DIR}" "${DIST}" "${CARGO_CACHE}"
	log "Preparing reproducible local source archives"
	prepare_source_archives
	prepare_portage
	configure_test_access
	resolve_build_plan
	log "Build plan: ${PLAN} (BUILD_MODE=${BUILD_MODE}, flavour ${ISO_FLAVOR}, volume ${ISO_VOLID})"
	write_catalyst_config
	# Only a stage1 built from scratch needs the stage3 seed; stage2-only and reseed start
	# from the verified stage1 archive.
	if [[ ${PLAN} == full || ${PLAN} == stage1-only ]]; then
		fetch_and_verify_seed
	fi
	prepare_snapshot
	case ${PLAN} in
		full | stage1-only | reseed) build_stage1 ;;
	esac
	if [[ ${PLAN} == stage1-only ]]; then
		log "Stage1 is ready: ${STAGE1_ARCHIVE}"
		return 0
	fi
	log "Building Catalyst livecd-stage2"
	run_catalyst "${BUILD_DIR}/livecd-stage2.spec"
	publish_iso
	log "Created ${DIST}/${ISO_NAME}"
}

# Sourcing this file (the tests do) defines the functions without running a build.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
	main "$@"
fi
