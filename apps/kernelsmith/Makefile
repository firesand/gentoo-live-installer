# Kernelsmith — build and (un)install.
#
#   make                 build the release binary (as your normal user)
#   sudo make install    install to /usr/local (PREFIX=/usr for a system install)
#   sudo make uninstall  remove exactly what `make install` added
#   make test            run the test suite
#
# Kernels you built stay installed after `make uninstall`; remove them from
# the Library page first if you want them gone.

PREFIX  ?= /usr/local
DESTDIR ?=
BINDIR   = $(PREFIX)/bin
APPDIR   = $(PREFIX)/share/applications
ICONDIR  = $(PREFIX)/share/icons/hicolor/scalable/apps
# polkit only reads actions from /usr/share, whatever PREFIX is.
POLKITDIR = /usr/share/polkit-1/actions
BIN      = target/release/kernelsmith

.PHONY: all build install uninstall test clean

all: build

build:
	cargo build --release

$(BIN):
	@echo "Run 'make' as your normal user first." && exit 1

install: $(BIN)
	install -Dm755 -s $(BIN) $(DESTDIR)$(BINDIR)/kernelsmith
	install -Dm644 packaging/kernelsmith.desktop $(DESTDIR)$(APPDIR)/kernelsmith.desktop
	install -Dm644 packaging/kernelsmith.svg $(DESTDIR)$(ICONDIR)/kernelsmith.svg
	install -d $(DESTDIR)$(POLKITDIR)
	sed 's|@BINDIR@|$(BINDIR)|' packaging/org.kernelsmith.policy > $(DESTDIR)$(POLKITDIR)/org.kernelsmith.policy
	chmod 644 $(DESTDIR)$(POLKITDIR)/org.kernelsmith.policy
	-command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t $(DESTDIR)$(PREFIX)/share/icons/hicolor
	-command -v update-desktop-database >/dev/null && update-desktop-database -q $(DESTDIR)$(APPDIR)
	@echo "Installed. Start 'Kernelsmith' from your application menu or run: kernelsmith"

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/kernelsmith
	rm -f $(DESTDIR)$(APPDIR)/kernelsmith.desktop
	rm -f $(DESTDIR)$(ICONDIR)/kernelsmith.svg
	rm -f $(DESTDIR)$(POLKITDIR)/org.kernelsmith.policy
	@echo "Removed Kernelsmith. Your kernels, /var/lib/kernelsmith and ~/.config|.local/share|.cache/kernelsmith were left alone."

test:
	cargo test --workspace

clean:
	cargo clean
