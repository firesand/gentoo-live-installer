# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

PYTHON_COMPAT=( python3_13 python3_14 )
DISTUTILS_USE_PEP517=setuptools
inherit desktop distutils-r1 systemd

DESCRIPTION="Guided Gentoo live GUI installer, repair tool and privileged daemon"
HOMEPAGE="https://wiki.gentoo.org/wiki/No_homepage"
# The distfile is made by the ISO builder (scripts/build-iso.sh) from this repository.
SRC_URI="${P}.tar.zst"
S="${WORKDIR}/${P}"

LICENSE="GPL-2+"
SLOT="0"
KEYWORDS="amd64"
IUSE="+btrfs +efi secureboot +xfs"
RESTRICT="mirror fetch"

# Everything the daemon and the repair tool run (see REQUIRED_TOOLS in
# src/gentoo_installerd/inspect.py and the rescue plans). Programs that every Gentoo
# system has anyway (coreutils, mount, ...) are not listed; those that a minimal system may
# lack are.
RDEPEND="
	acct-group/gentoo-installer
	app-arch/tar
	app-crypt/gnupg
	app-portage/cpuid2cpuflags
	dev-python/pyside[gui,widgets,${PYTHON_USEDEP}]
	sec-keys/openpgp-keys-gentoo-release
	sys-apps/gptfdisk
	sys-apps/nvme-cli
	sys-apps/pciutils
	sys-apps/smartmontools
	sys-apps/util-linux
	sys-block/parted
	sys-fs/cryptsetup
	sys-fs/dosfstools
	sys-fs/e2fsprogs
	sys-fs/lvm2
	sys-fs/mdadm
	sys-process/lsof
	sys-process/psmisc
	virtual/udev
	x11-apps/setxkbmap
	x11-apps/xrandr
	btrfs? ( sys-fs/btrfs-progs )
	efi? ( sys-boot/efibootmgr )
	secureboot? ( app-crypt/sbsigntools )
	xfs? ( sys-fs/xfsprogs )
"
BDEPEND="
	app-arch/zstd
	dev-python/setuptools[${PYTHON_USEDEP}]
"

pkg_nofetch() {
	einfo "${P}.tar.zst is made by the ISO builder from this repository's sources"
	einfo "(scripts/build-iso.sh) and must be in DISTDIR; it cannot be downloaded."
}

src_unpack() {
	tar --zstd -xf "${DISTDIR}/${P}.tar.zst" -C "${WORKDIR}" || die "failed to unpack ${P}.tar.zst"
}

src_install() {
	distutils-r1_src_install
	domenu "${FILESDIR}/gentoo-installer.desktop"
	# The repair tool belongs to the same Python package once the sources contain it. Its
	# launcher starts the installer GUI in repair mode; the console scripts (gentoo-rescue)
	# come from pyproject.toml and are installed by distutils-r1 when it declares them.
	if [[ -d ${S}/src/gentoo_installerd/rescue ]]; then
		domenu "${S}/data/gentoo-rescue.desktop"
	fi
	newinitd "${FILESDIR}/gentoo-installerd.initd" gentoo-installerd
	newconfd "${FILESDIR}/gentoo-installerd.confd" gentoo-installerd
	systemd_dounit "${FILESDIR}/gentoo-installerd.service"
}
