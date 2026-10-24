# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

inherit desktop multiprocessing

DESCRIPTION="Rust egui graphical front end for Gentoo Portage"
HOMEPAGE="https://wiki.gentoo.org/wiki/No_homepage"
# The distfile is made by the ISO builder (scripts/build-iso.sh) from this repository.
SRC_URI="${P}.tar.zst"
S="${WORKDIR}/${P}"

# The project is GPL-2+; the rest are the licences of the vendored Rust crates (Cargo.lock).
# Regenerate with pycargoebuild or `cargo metadata` whenever the dependencies change.
LICENSE="GPL-2+ 0BSD Apache-2.0 Apache-2.0-with-LLVM-exceptions Boost-1.0 BSD BSD-2 GPL-2 ISC LGPL-2.1+ MIT OFL-1.1 UbuntuFontLicense-1.0 Unicode-3.0 Unlicense ZLIB"
SLOT="0"
KEYWORDS="amd64"
RESTRICT="mirror fetch"

RDEPEND="
	dev-libs/wayland
	media-libs/mesa
	sys-auth/polkit
	x11-libs/libX11
	x11-libs/libXcursor
	x11-libs/libXi
	x11-libs/libXrandr
	x11-libs/libxcb
	x11-libs/libxkbcommon
"
DEPEND="${RDEPEND}"
BDEPEND="
	app-arch/zstd
	|| ( dev-lang/rust-bin dev-lang/rust )
	virtual/pkgconfig
"

pkg_nofetch() {
	einfo "${P}.tar.zst is made by the ISO builder from this repository's sources"
	einfo "(scripts/build-iso.sh) and must be in DISTDIR; it cannot be downloaded."
}

src_unpack() {
	tar --zstd -xf "${DISTDIR}/${P}.tar.zst" -C "${WORKDIR}" || die "failed to unpack ${P}.tar.zst"
}

src_compile() {
	export CARGO_HOME="${WORKDIR}/cargo-home"
	cargo build --jobs="$(makeopts_jobs)" --release --locked --offline
}

src_install() {
	dobin target/release/portagehub
	exeinto /usr/libexec
	doexe target/release/portagehub-helper
	domenu dist/portagehub.desktop
	insinto /usr/share/polkit-1/actions
	doins dist/org.portagehub.helper.policy
}
