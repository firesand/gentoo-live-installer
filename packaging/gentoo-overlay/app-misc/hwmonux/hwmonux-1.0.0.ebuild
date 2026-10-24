EAPI=8

PYTHON_COMPAT=( python3_14 )
DISTUTILS_USE_PEP517=setuptools
inherit desktop distutils-r1

DESCRIPTION="PySide6 hardware sensor monitor"
HOMEPAGE="https://wiki.gentoo.org/wiki/No_homepage"
SRC_URI="${P}.tar.zst"
S="${WORKDIR}/${P}"

LICENSE="GPL-2+"
SLOT="0"
KEYWORDS="amd64"
IUSE="nvidia"

RDEPEND="
	dev-python/pyside[${PYTHON_USEDEP}]
	nvidia? ( dev-python/nvidia-ml-py[${PYTHON_USEDEP}] )
	app-misc/liquidctl[${PYTHON_USEDEP}]
	sys-apps/lm-sensors
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
	domenu hwmonux.desktop
}
