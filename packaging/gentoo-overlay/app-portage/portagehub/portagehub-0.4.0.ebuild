EAPI=8

inherit desktop multiprocessing

DESCRIPTION="Rust egui graphical front end for Gentoo Portage"
HOMEPAGE="https://wiki.gentoo.org/wiki/No_homepage"
SRC_URI="${P}.tar.zst"
S="${WORKDIR}/${P}"

LICENSE="GPL-2+"
SLOT="0"
KEYWORDS="amd64"

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
	dev-lang/rust-bin
	virtual/pkgconfig
"

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
