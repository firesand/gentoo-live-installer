# Copyright 1999-2024 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

# Vendored copy of ::guru gnome-extra/iio-sensor-proxy-3.9 (hardware item I01).
#   source:    https://github.com/gentoo-mirror/guru, master at 8106df7668a32b3fee7f09b982e7b9b9d16a290c
#              (metadata/timestamp.chk of 2026-10-21); ebuild, files/ and metadata.xml are unchanged
#              except KEYWORDS and this comment.
#   why:       it is the only ambient-light / auto-rotate daemon for GNOME and KDE, and it is not
#              in ::gentoo. The installer's companion overlay carries it so that an install does not
#              have to enable ::guru (never `eselect repository enable guru`, never a sync).
#   KEYWORDS:  GURU says ~amd64. Everything it depends on is stable amd64 in the pinned ::gentoo
#              (checked by tests/iso/test_vendored_ebuilds.py), so the copy is keyworded amd64 and
#              needs no accept_keywords entry.
#   distfile:  the tarball is pinned by the Manifest next to this file (BLAKE2B and SHA512 as in
#              ::guru, plus SHA256). The ISO builder fetches it once, checks it against that
#              Manifest and copies it to /usr/local/share/gentoo-installer/distfiles, so the
#              installed system builds it without a download. Refresh with docs/ISO.md section 11.
inherit xdg gnome2-utils meson udev

DESCRIPTION="IIO sensors to D-Bus proxy"
HOMEPAGE="https://gitlab.freedesktop.org/hadess/iio-sensor-proxy/"
SRC_URI="https://gitlab.freedesktop.org/hadess/iio-sensor-proxy/-/archive/${PV}/${P}.tar.bz2"

LICENSE="GPL-3"
SLOT="0"
KEYWORDS="amd64"
IUSE="systemd"

RDEPEND="
	dev-libs/glib:*
	gnome-base/gnome-common
	>=sys-auth/polkit-0.91
	>=dev-libs/libgudev-237
	systemd? (
		!sys-apps/openrc
		sys-apps/systemd
	)
	virtual/udev
"

DEPEND="
	${RDEPEND}
	dev-build/gtk-doc-am
	virtual/pkgconfig
"

src_prepare() {
	if use !systemd ; then
		# patch out call of systemd directory
		eapply "${FILESDIR}/no-systemd.patch"
	fi
	eapply_user
}

src_install() {
	meson_src_install
	doinitd "${FILESDIR}/iio-sensor-proxy"
}

pkg_postinst() {
	xdg_pkg_postinst
	gnome2_schemas_update
	udev_reload
	elog "iio-sensor-proxy comes from the Gentoo Live Installer's companion overlay (an"
	elog "unofficial copy of the ::guru ebuild), compiled on this PC. On OpenRC it is"
	elog "started with: rc-update add iio-sensor-proxy default"
}

pkg_postrm() {
	xdg_pkg_postrm
	gnome2_schemas_update
	udev_reload
}
