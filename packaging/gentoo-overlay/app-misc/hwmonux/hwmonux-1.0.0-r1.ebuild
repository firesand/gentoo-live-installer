# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

PYTHON_COMPAT=( python3_14 )
DISTUTILS_USE_PEP517=setuptools
inherit desktop distutils-r1

DESCRIPTION="PySide6 hardware sensor monitor"
HOMEPAGE="https://wiki.gentoo.org/wiki/No_homepage"
# The distfile is made by the ISO builder (scripts/build-iso.sh) from this repository.
SRC_URI="${P}.tar.zst"
S="${WORKDIR}/${P}"

LICENSE="GPL-2+"
SLOT="0"
# liquidctl and hidapi are ~amd64 only, so they are behind the liquidctl flag: with the
# default flags the dependency graph solves on a stable system. HWMonux works without them
# and without NVIDIA's NVML. nvidia stays off by default: nvidia-ml-py depends on the
# licence-masked x11-drivers/nvidia-drivers. The installer enables it after the user accepts
# the NVIDIA licence.
KEYWORDS="amd64"
IUSE="nvidia liquidctl"
RESTRICT="mirror fetch"

RDEPEND="
	dev-python/pyside[gui,widgets,${PYTHON_USEDEP}]
	sys-apps/lm-sensors
	liquidctl? ( app-misc/liquidctl[${PYTHON_USEDEP}] )
	nvidia? ( dev-python/nvidia-ml-py[${PYTHON_USEDEP}] )
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
	domenu hwmonux.desktop
	# drivetemp exposes the temperature of SATA and SAS disks as a hwmon device. It only
	# reads SMART data, so loading it is harmless; a kernel without it just logs the failure.
	# The Super I/O drivers (nct6775, it87, ...) are deliberately not loaded here: probing
	# them can hang a laptop's embedded controller. See the HWMonux README.
	insinto /etc/modules-load.d
	newins "${FILESDIR}/hwmonux-modules.conf" hwmonux.conf
}
