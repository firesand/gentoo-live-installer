#!/bin/bash
set -Eeuo pipefail

readonly WORKSPACE=/workspace
readonly BUILD_DIR=/build
readonly CATALYST_STORE=/var/tmp/catalyst
readonly DISTDIR=/var/cache/catalyst/distfiles
readonly DIST=/dist
JOBS=${JOBS:-2}
LOAD_AVERAGE=${LOAD_AVERAGE:-$((JOBS * 2))}
# Catalyst replaces the copied make.conf before running emerge. Pass build
# options through its supported envvars channel instead of that file.
export EMERGE_DEFAULT_OPTS="--getbinpkg --binpkg-respect-use=y --usepkg-exclude=app-admin/gentoo-live-installer --usepkg-exclude=app-misc/hwmonux --usepkg-exclude=app-portage/portagehub --usepkg-exclude=acct-group/gentoo-installer"
export GENTOO_INSTALLER_MAKE_CONF_EXTRA="${BUILD_DIR}/portage/make.conf.extra"

log() {
	printf '\n==> %s\n' "$*"
}

die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

[[ ${JOBS} =~ ^[1-9][0-9]*$ ]] || die "JOBS must be a positive integer"
[[ ${LOAD_AVERAGE} =~ ^[0-9]+([.][0-9]+)?$ ]] || die "LOAD_AVERAGE must be numeric"
set -a
source "${WORKSPACE}/iso/pins.env"
set +a
[[ ${GENTOO_TREEISH} =~ ^[0-9a-f]{40}$ ]] || die "invalid Gentoo treeish"
[[ ${SOURCE_DATE_EPOCH} =~ ^[0-9]+$ ]] || die "invalid SOURCE_DATE_EPOCH"

prepare_source_archives() {
	local sources="${BUILD_DIR}/sources"
	local installer=gentoo-live-installer-0.1.0
	local hwmonux=hwmonux-1.0.0
	local portagehub=portagehub-0.4.0
	local package

	install -d "${sources}/${installer}" "${sources}/${hwmonux}" "${sources}/${portagehub}" "${DISTDIR}"
	tar -C "${WORKSPACE}" --exclude=__pycache__ --exclude='*.pyc' -cf - pyproject.toml src \
		| tar -C "${sources}/${installer}" -xf -
	tar -C "${WORKSPACE}/apps/hwmonux" --exclude=__pycache__ --exclude='*.pyc' -cf - \
		pyproject.toml hwmonux.py sensors.py hwmonux.desktop \
		| tar -C "${sources}/${hwmonux}" -xf -
	tar -C "${WORKSPACE}/apps/portagehub" -cf - Cargo.toml Cargo.lock src dist \
		| tar -C "${sources}/${portagehub}" -xf -

	install -d "${sources}/${portagehub}/.cargo"
	(
		cd "${sources}/${portagehub}"
		CARGO_HOME=/var/cache/cargo cargo vendor --locked --versioned-dirs vendor > .cargo/config.toml
	)

	for package in "${installer}" "${hwmonux}" "${portagehub}"; do
		tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --clamp-mtime \
			--owner=0 --group=0 --numeric-owner --format=gnu \
			-C "${sources}" -cf "${DISTDIR}/${package}.tar" "${package}"
		zstd -q -f -12 -T1 --rm "${DISTDIR}/${package}.tar"
	done
}

prepare_portage() {
	local ebuild
	cp -a "${WORKSPACE}/packaging/gentoo-overlay" "${BUILD_DIR}/overlay"
	cp -a "${WORKSPACE}/iso/portage" "${BUILD_DIR}/portage"
	cp -a "${WORKSPACE}/iso/root_overlay" "${BUILD_DIR}/root_overlay"
	cp -a "${WORKSPACE}/iso/fsscript-stage2.sh" "${BUILD_DIR}/fsscript-stage2.sh"
	install -d "${BUILD_DIR}/root_overlay/usr/local/share/gentoo-installer/distfiles"
	cp -a "${DISTDIR}"/gentoo-live-installer-*.tar.zst "${DISTDIR}"/hwmonux-*.tar.zst \
		"${DISTDIR}"/portagehub-*.tar.zst "${BUILD_DIR}/root_overlay/usr/local/share/gentoo-installer/distfiles/"
	for ebuild in "${BUILD_DIR}"/overlay/*/*/*.ebuild; do
		env DISTDIR="${DISTDIR}" ebuild "${ebuild}" manifest
	done
	# The installed target needs the same authenticated distfile manifests.
	cp -a "${BUILD_DIR}/overlay" "${BUILD_DIR}/root_overlay/usr/local/share/gentoo-installer/overlay"

	python3 - "${BUILD_DIR}" "${WORKSPACE}" "${JOBS}" "${LOAD_AVERAGE}" <<'PY'
import pathlib
import sys

build = pathlib.Path(sys.argv[1])
workspace = pathlib.Path(sys.argv[2])
values = {
    "@BUILD_DIR@": str(build),
    "@TREEISH@": __import__("os").environ["GENTOO_TREEISH"],
    "@VERSION_STAMP@": __import__("os").environ["VERSION_STAMP"],
    "@STAGE3_BASENAME@": __import__("os").environ["STAGE3_BASENAME"],
    "@ISO_NAME@": __import__("os").environ["ISO_NAME"],
    "@JOBS@": sys.argv[3],
    "@LOAD_AVERAGE@": sys.argv[4],
}
files = [
    (workspace / "iso/specs/livecd-stage1.spec", build / "livecd-stage1.spec"),
    (workspace / "iso/specs/livecd-stage2.spec", build / "livecd-stage2.spec"),
    (workspace / "iso/portage/make.conf", build / "portage/make.conf"),
]
for source, destination in files:
    text = source.read_text()
    for token, value in values.items():
        text = text.replace(token, value)
    if any(token in text for token in values):
        raise SystemExit(f"unexpanded token in {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)

# Catalyst creates make.conf anew at chroot setup and again at cleanup. The
# no-loop wrapper appends these explicit Portage settings after each rewrite.
# Keep the values in make.conf as the single source for the build and live ISO.
keys = (
    "MAKEOPTS", "NINJAOPTS", "EMERGE_DEFAULT_OPTS", "ACCEPT_LICENSE", "GRUB_PLATFORMS",
    "INPUT_DEVICES", "VIDEO_CARDS", "FEATURES", "PYTHON_TARGETS",
    "PYTHON_SINGLE_TARGET",
)
make_conf = (build / "portage/make.conf").read_text().splitlines()
assignments = {}
for line in make_conf:
    for key in keys:
        if line.startswith(f"{key}="):
            if key in assignments:
                raise SystemExit(f"duplicate {key} in Portage make.conf")
            assignments[key] = line
if set(assignments) != set(keys):
    raise SystemExit(f"missing Portage settings: {set(keys) - set(assignments)}")
(build / "portage/make.conf.extra").write_text(
    "# Portage settings preserved across Catalyst make.conf rewrites.\n"
    + "\n".join(assignments[key] for key in keys) + "\n"
)
PY
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
	catalyst -c "${BUILD_DIR}/catalyst.conf" -s "${GENTOO_TREEISH}"
	[[ -s ${CATALYST_STORE}/snapshots/gentoo-${GENTOO_TREEISH}.sqfs ]] \
		|| die "Catalyst did not create the pinned repository snapshot"
}

write_catalyst_config() {
	cat > "${BUILD_DIR}/catalyst.conf" <<EOF
storedir = "${CATALYST_STORE}"
distdir = "${DISTDIR}"
target_distdir = "${DISTDIR}"
target_pkgdir = "/var/cache/binpkgs"
target_logdir = "${CATALYST_STORE}/logs"
repos_storedir = "${CATALYST_STORE}/repos"
repo_openpgp_key_path = ""
digests = ["sha512", "blake2b"]
options = ["seedcache", "pkgcache"]
jobs = ${JOBS}
load-average = ${LOAD_AVERAGE}
envvars = { SOURCE_DATE_EPOCH = "${SOURCE_DATE_EPOCH}", EMERGE_DEFAULT_OPTS = "${EMERGE_DEFAULT_OPTS}" }
EOF
}

publish_iso() {
	local built="${CATALYST_STORE}/builds/23.0-default/${ISO_NAME}"
	local catalyst_version
	[[ -s ${built} ]] || die "Catalyst completed without producing ${built}"
	(( $(stat -c %s "${built}") > 10000000 )) || die "Catalyst ISO is implausibly small"
	isoinfo -d -i "${built}" >/dev/null
	install -m 0644 "${built}" "${DIST}/.${ISO_NAME}.partial"
	mv "${DIST}/.${ISO_NAME}.partial" "${DIST}/${ISO_NAME}"
	(
		cd "${DIST}"
		sha512sum "${ISO_NAME}" > "${ISO_NAME}.sha512"
		b2sum "${ISO_NAME}" > "${ISO_NAME}.blake2b"
		sha512sum -c "${ISO_NAME}.sha512"
		b2sum -c "${ISO_NAME}.blake2b"
	)
	catalyst_version=$(catalyst --version 2>&1 | head -n 1)
	python3 - "${DIST}" "${WORKSPACE}" "${catalyst_version}" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
workspace = pathlib.Path(sys.argv[2])
iso_name = os.environ["ISO_NAME"]
iso = out / iso_name

def digest(path, algorithm):
    h = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

inputs = {}
for root in (workspace / "iso", workspace / "packaging"):
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        inputs[str(path.relative_to(workspace))] = digest(path, "sha256")
build_script = workspace / "scripts/build-iso.sh"
inputs[str(build_script.relative_to(workspace))] = digest(build_script, "sha256")
archives = {}
for path in sorted(pathlib.Path("/var/cache/catalyst/distfiles").glob("*.tar.zst")):
    archives[path.name] = digest(path, "sha256")
manifest = {
    "schema_version": 1,
    "iso": {
        "name": iso_name,
        "size": iso.stat().st_size,
        "sha512": digest(iso, "sha512"),
        "blake2b": digest(iso, "blake2b"),
    },
    "source_date_epoch": int(os.environ["SOURCE_DATE_EPOCH"]),
    "test_ssh_enabled": bool(os.environ.get("GENTOO_TEST_SSH_PUBLIC_KEY")),
    "builder_image": os.environ["CATALYST_BUILDER_IMAGE"],
    "builder_base_image": os.environ["BUILDER_IMAGE"],
    "catalyst_version": sys.argv[3],
    "stage3": {
        "url": os.environ["STAGE3_URL_BASE"] + "/" + os.environ["STAGE3_BASENAME"],
        "sha512": os.environ["STAGE3_SHA512"],
        "blake2b": os.environ["STAGE3_BLAKE2B"],
        "signed_digests_verified": True,
    },
    "gentoo_repository": {
        "url": os.environ["GENTOO_REPOSITORY"],
        "commit": os.environ["GENTOO_TREEISH"],
        "verification": "exact commit ID and Git object connectivity",
    },
    "source_archives": archives,
    "build_inputs_sha256": inputs,
}
temporary = out / ("." + iso_name + ".manifest.partial")
temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
temporary.replace(out / (iso_name + ".manifest.json"))
PY
}

configure_test_access() {
	local key="${GENTOO_TEST_SSH_PUBLIC_KEY:-}"
	[[ -n ${key} ]] || return 0
	[[ ${key} == ssh-* && ${key} != *$'\n'* && ${key} != *$'\r'* ]] \
		|| die "GENTOO_TEST_SSH_PUBLIC_KEY must be one public key line"
	log "Enabling ephemeral SSH access for this test-only ISO; do not distribute it"
	install -d -m 0700 "${BUILD_DIR}/root_overlay/root/.ssh"
	printf '%s\n' "${key}" > "${BUILD_DIR}/root_overlay/root/.ssh/authorized_keys"
	chmod 0600 "${BUILD_DIR}/root_overlay/root/.ssh/authorized_keys"
	install -d "${BUILD_DIR}/root_overlay/etc/ssh/sshd_config.d"
	cat > "${BUILD_DIR}/root_overlay/etc/ssh/sshd_config.d/90-installer-vm-test.conf" <<'EOF'
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
	touch "${BUILD_DIR}/root_overlay/etc/gentoo-installer-vm-test"
	python3 - "${BUILD_DIR}/livecd-stage2.spec" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
needle = "\tgentoo-installerd|default\n"
if needle not in text:
    raise SystemExit("cannot find the live service list")
path.write_text(text.replace(needle, needle + "\tsshd|default\n", 1))
PY
}

main() {
	rm -rf "${BUILD_DIR}"
	install -d "${BUILD_DIR}" "${DIST}" /var/cache/cargo
	log "Preparing reproducible local source archives"
	prepare_source_archives
	prepare_portage
	configure_test_access
	write_catalyst_config
	fetch_and_verify_seed
	prepare_snapshot
	log "Building Catalyst livecd-stage1"
	python3 "${WORKSPACE}/iso/catalyst-no-loop.py" -c "${BUILD_DIR}/catalyst.conf" -f "${BUILD_DIR}/livecd-stage1.spec"
	log "Building Catalyst livecd-stage2"
	python3 "${WORKSPACE}/iso/catalyst-no-loop.py" -c "${BUILD_DIR}/catalyst.conf" -f "${BUILD_DIR}/livecd-stage2.spec"
	publish_iso
	log "Created ${DIST}/${ISO_NAME}"
}

main "$@"
