# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

inherit desktop multiprocessing optfeature xdg

DESCRIPTION="Guided, hardware-aware custom kernel builder with a Steam-style GUI"
HOMEPAGE="https://github.com/firesand/kernelsmith"
# The distfile is made by the ISO builder (scripts/build-iso.sh) from apps/kernelsmith, which
# `scripts/sync-app.sh kernelsmith` fills from the KernelSmith repository (apps/kernelsmith/UPSTREAM
# says which commit). Upstream's own live ebuild builds from git; this one builds offline.
SRC_URI="${P}.tar.zst"
S="${WORKDIR}/${P}"

# The project is GPL-2+; the rest are the licences of the vendored Rust crates (Cargo.lock).
# Regenerate from `cargo metadata --locked --format-version 1` whenever the dependencies change.
LICENSE="GPL-2+ 0BSD Apache-2.0 Apache-2.0-with-LLVM-exceptions BSD BSD-2 Boost-1.0 CC0-1.0 ISC LGPL-2.1+ MIT MPL-2.0 OFL-1.1 UbuntuFontLicense-1.0 Unicode-3.0 Unlicense ZLIB"
SLOT="0"
KEYWORDS="amd64"
RESTRICT="mirror fetch"

# KernelSmith drives kernel builds, so the kernel's own build requirements are run-time
# dependencies here (as in upstream's ebuild). Clang/LLVM, pahole, perf and modprobed-db are
# optional and detected at run time; the kernel sources it installs itself (its Build page).
RDEPEND="
	app-alternatives/cpio
	dev-lang/perl
	dev-libs/openssl
	dev-libs/wayland
	media-libs/libglvnd
	sys-auth/polkit
	sys-devel/bc
	sys-devel/bison
	sys-devel/flex
	sys-devel/patch
	sys-kernel/installkernel
	virtual/libelf
	x11-libs/libX11
	x11-libs/libXcursor
	x11-libs/libXi
	x11-libs/libXrandr
	x11-libs/libxkbcommon[X]
	x11-misc/xdg-utils
	|| ( net-misc/curl net-misc/wget )
"
DEPEND="${RDEPEND}"
# Cargo.toml says rust-version = "1.85".
BDEPEND="
	app-arch/zstd
	|| ( >=dev-lang/rust-bin-1.85.0 >=dev-lang/rust-1.85.0 )
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
	cargo build --jobs="$(makeopts_jobs)" --release --locked --offline --package kernelsmith
}

src_install() {
	dobin target/release/kernelsmith
	domenu packaging/kernelsmith.desktop
	doicon -s scalable packaging/kernelsmith.svg
	# pkexec shows this action's message when a build or uninstall asks for the administrator password.
	sed "s|@BINDIR@|${EPREFIX}/usr/bin|" packaging/org.kernelsmith.policy > "${T}"/org.kernelsmith.policy || die
	insinto /usr/share/polkit-1/actions
	doins "${T}"/org.kernelsmith.policy
	dodoc README.md UPSTREAM
}

pkg_postinst() {
	xdg_pkg_postinst

	optfeature "Clang builds, ThinLTO and AutoFDO" "llvm-core/clang llvm-core/lld"
	optfeature "sched_ext and BTF support" dev-util/pahole
	optfeature "AutoFDO profiling" dev-util/perf
	optfeature "safer driver trimming from long-term module history" sys-kernel/modprobed-db
	optfeature "runtime-switchable BPF schedulers" sys-kernel/scx
}
