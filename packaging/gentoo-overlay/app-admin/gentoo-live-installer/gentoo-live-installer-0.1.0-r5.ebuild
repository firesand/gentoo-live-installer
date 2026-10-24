EAPI=8

PYTHON_COMPAT=( python3_13 python3_14 )
DISTUTILS_USE_PEP517=setuptools
inherit desktop distutils-r1 systemd

DESCRIPTION="Guided AMD64 Gentoo live GUI installer and privileged daemon"
HOMEPAGE="https://wiki.gentoo.org/wiki/No_homepage"
SRC_URI="${P}.tar.zst"
S="${WORKDIR}/${P}"

LICENSE="GPL-2+"
SLOT="0"
KEYWORDS="amd64"

RDEPEND="
	app-crypt/gnupg
	acct-group/gentoo-installer
	dev-python/pyside[${PYTHON_USEDEP}]
	sec-keys/openpgp-keys-gentoo-release
	sys-apps/util-linux
	sys-auth/polkit
	sys-fs/cryptsetup
	sys-fs/lvm2
	sys-fs/mdadm
"
BDEPEND="
	app-arch/zstd
	dev-python/setuptools[${PYTHON_USEDEP}]
"

src_unpack() {
	tar --zstd -xf "${DISTDIR}/${P}.tar.zst" -C "${WORKDIR}" || die "failed to unpack ${P}.tar.zst"
}

src_install() {
	distutils-r1_src_install
	domenu "${FILESDIR}/gentoo-installer.desktop"
	newinitd "${FILESDIR}/gentoo-installerd.initd" gentoo-installerd
	systemd_dounit "${FILESDIR}/gentoo-installerd.service"
}
