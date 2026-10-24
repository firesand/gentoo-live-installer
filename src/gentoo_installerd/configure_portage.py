"""PORT: make.conf, package.use/env/license/accept_keywords, binhost and the PortageHub companions on the target."""

from __future__ import annotations

import re
import shlex
import shutil
from pathlib import Path
from typing import Mapping, MutableMapping, Sequence

from gentoo_installer.binhost import binhost_url, BINREPO_NAME, cpu_level_of, render_binrepos
from gentoo_installer.licenses import effective_accept_license, render_package_license
from gentoo_installer.makeopts import auto_make_opts as shared_auto_make_opts
from gentoo_installer.manifest import ATOM_RE, InstallManifest, USE_TOKEN_RE
from gentoo_installer.mirrors import mirror_list
from gentoo_installer.optimize import CPU_FLAGS_FILE, cpu_flags_of, cxxflags_value, native_wanted, render_cpu_flags, tuned_flags

from .configure_base import _plan_list, ConfigurationError, ConfigurerBase, shell_value
from .proxy import make_conf_settings, proxy_environment
from .runner import RunnerError


COMPANION_ATOMS = ("app-misc/hwmonux", "app-portage/portagehub")


# Written when the binary package signing keys could not be set up (getuto failed): every later render of make.conf then
# leaves the binary package host out, so packages are built from source and no unsigned package is ever accepted.
BINHOST_OFF_MARKER = "var/log/gentoo-installer/binhost-off"


# The Handbook's repos.conf for the Gentoo repository when it is synced with rsync, verification included.
REPOS_CONF_KEYS = (
    ("sync-type", "rsync"),
    ("auto-sync", "yes"),
    ("sync-rsync-verify-jobs", "1"),
    ("sync-rsync-verify-metamanifest", "yes"),
    ("sync-rsync-verify-max-age", "3"),
    ("sync-openpgp-key-path", "/usr/share/openpgp-keys/gentoo-release.asc"),
    ("sync-openpgp-keyserver", "hkps://keys.gentoo.org"),
    ("sync-openpgp-key-refresh-retry-count", "40"),
    ("sync-openpgp-key-refresh-retry-overall-timeout", "1200"),
    ("sync-openpgp-key-refresh-retry-delay-exp-base", "2"),
    ("sync-openpgp-key-refresh-retry-delay-max", "60"),
    ("sync-openpgp-key-refresh-retry-delay-mult", "4"),
    ("sync-webrsync-verify-signature", "yes"),
)
DEFAULT_RSYNC_URI = "rsync://rsync.gentoo.org/gentoo-portage"
REPOS_CONF_BASE = "[DEFAULT]\nmain-repo = gentoo\n\n[gentoo]\nlocation = /var/db/repos/gentoo\n"


def render_repos_conf(base: str, sync_uri: str) -> str:
    """gentoo.conf for rsync with verification, starting from the stage3's own /usr/share/portage/config/repos.conf.

    Every Handbook key is set (an existing value is replaced, a missing one appended to the [gentoo] section); verification
    is part of the list and cannot be switched off from here.
    """
    lines = (base or REPOS_CONF_BASE).splitlines()
    wanted = {**dict(REPOS_CONF_KEYS), "sync-uri": sync_uri}
    start = next((i for i, line in enumerate(lines) if line.strip() == "[gentoo]"), None)
    if start is None:
        lines = [*lines, "", "[gentoo]", "location = /var/db/repos/gentoo"]
        start = len(lines) - 2
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    seen: set[str] = set()
    for i in range(start + 1, end):
        key = lines[i].partition("=")[0].strip()
        if key in wanted:
            lines[i] = f"{key} = {wanted[key]}"
            seen.add(key)
    missing = [f"{key} = {value}" for key, value in wanted.items() if key not in seen]
    lines[end:end] = missing
    return "\n".join(lines).rstrip("\n") + "\n"


# A package.use key: an ordinary atom, or */* for USE_EXPAND lines such as "*/* VIDEO_CARDS: virgl".
PACKAGE_USE_ATOM = re.compile(r"\*/\*|" + ATOM_RE.pattern)


# What the installer writes under /etc/portage so that its own package choices resolve without
# autounmask. These are configuration, never left to the binary package host (CRITIC-0/1).
DESKTOP_USE: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    # kde-apps-meta -> kdeutils-meta -> kweather needs qtpositioning[geoclue]
    "kde": {"dev-qt/qtpositioning": ("geoclue",)},
    # samba -> libngtcp2 needs the GnuTLS crypto backend
    "gnome": {"net-libs/ngtcp2": ("gnutls",)},
    # xfce4-panel needs libdbusmenu[gtk3]
    "xfce": {"dev-libs/libdbusmenu": ("gtk3",)},
}


# The package.env of the hardware plan (hwplan.MEMORY_HEAVY_ATOMS): the compile jobs the memory-heavy packages may use.
MEMORY_ENV_NAME = "installer-memory"


MEMORY_ENV_FILE = f"etc/portage/env/{MEMORY_ENV_NAME}"


PACKAGE_ENV_FILE = "etc/portage/package.env/installer-hardware"


def ninja_opts_from_make_opts(make_opts: str) -> str:
    """Keep Ninja bounded even when Portage exports an empty NINJAOPTS."""
    try:
        tokens = shlex.split(make_opts)
    except ValueError as exc:
        raise ConfigurationError("Invalid MAKEOPTS") from exc
    jobs = 2
    load: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-j", "--jobs", "-l", "--load-average"} and index + 1 < len(tokens):
            candidate = tokens[index + 1]
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", candidate):
                token = token + ("=" if token.startswith("--") else "") + candidate
                index += 1
        if token in {"-j", "--jobs", "-j0", "--jobs=0"}:
            jobs = 2  # An unbounded make request must not unbound Ninja.
        match = re.fullmatch(r"(?:-j|--jobs=)([1-9][0-9]*)", token)
        if match:
            jobs = int(match.group(1))
        match = re.fullmatch(r"(?:-l|--load-average=)([0-9]+(?:\.[0-9]+)?)", token)
        if match:
            load = match.group(1)
        index += 1
    return f"-j{jobs}" + (f" -l{load}" if load is not None else "")


def auto_make_opts(cpus: int, mem_available: int, swap_mib: int = 0) -> str:
    """-j/-l for MAKEOPTS: about 2 GiB per compile job, never above the thread count or 32."""
    return shared_auto_make_opts(cpus, mem_available, swap_mib)  # the wizard shows the same value


def auto_emerge_jobs(cpus: int, mem_total: int) -> int:
    """How many packages Portage may build at the same time."""
    gib = mem_total / 1024**3
    if gib >= 60 and cpus >= 16:
        return 3
    if gib >= 14 and cpus >= 8:
        return 2
    return 1


# --- package.use helpers -------------------------------------------------------------------------


def _require_use(package_use: MutableMapping[str, list[str]], atom: str, *flags: str) -> None:
    """Make sure every flag is enabled for atom, overriding a user's -flag."""
    current = package_use.setdefault(atom, [])
    current[:] = [flag for flag in current if flag.lstrip("-") not in flags or not flag.startswith("-")]
    current.extend(flag for flag in flags if flag not in current)


def _default_use(package_use: MutableMapping[str, list[str]], atom: str, *flags: str) -> None:
    """Set a flag for atom unless the user already decided it, either way."""
    current = package_use.setdefault(atom, [])
    current.extend(flag for flag in flags if flag.lstrip("-") not in {item.lstrip("-") for item in current})


def _forbid_use(package_use: MutableMapping[str, list[str]], atom: str, *flags: str) -> None:
    """Make sure every flag is disabled for atom, overriding a user's flag."""
    current = package_use.setdefault(atom, [])
    current[:] = [flag for flag in current if flag not in flags]
    current.extend(f"-{flag}" for flag in flags if f"-{flag}" not in current)


# --- rendering -------------------------------------------------------------------------------------

# Variables whose values add to the profile's instead of replacing them when a plan supplies more.
_INCREMENTAL_KEYS = ("USE", "FEATURES", "EMERGE_DEFAULT_OPTS")


# A hardware plan may tune these but must never widen licences or change mirrors.
_PLAN_FORBIDDEN_KEYS = {"ACCEPT_LICENSE", "GENTOO_MIRRORS", "PORTAGE_BINHOST"}


def render_make_conf(manifest: InstallManifest, plan: object | None = None, *, make_opts: str | None = None,
                     binhost: bool | None = None) -> str:
    """Render the target's make.conf.

    Empty values are left out so the profile defaults (CPU_FLAGS_X86, VIDEO_CARDS, ...) stay in
    force; a hardware plan may add or tune keys through ``plan.make_conf``. ``binhost`` False leaves the binary package host
    out although the settings ask for it (its signing keys could not be set up); the host itself is in binrepos.conf,
    not in make.conf.
    """
    p = manifest.portage
    plan_values = {key: str(value) for key, value in dict(getattr(plan, "make_conf", None) or {}).items()
                   if str(value).strip() and key not in _PLAN_FORBIDDEN_KEYS}
    jobs = make_opts if make_opts is not None else p.make_opts
    use_binhost = p.binhost if binhost is None else binhost and p.binhost
    # A signature is always required for a binary package, also when the host is left out.
    # When the host was switched off after a failed getuto, a getbinpkg the user typed into FEATURES is dropped too.
    own = [word for word in p.features if use_binhost or not p.binhost or word != "getbinpkg"]
    features = " ".join(dict.fromkeys([*own, *(["getbinpkg"] if use_binhost else []), "binpkg-request-signature"] if p.binhost else own))
    native = native_wanted(p, manifest.hardware.target == "portable")
    cflags, rustflags = tuned_flags(p.cflags, p.rustflags, native)
    if native:
        plan_values.pop("CPU_FLAGS_X86", None)  # native tuning writes CPU_FLAGS_X86 to package.use/00cpu-flags instead
    values: list[tuple[str, str]] = [
        ("COMMON_FLAGS", cflags),
        ("CFLAGS", "${COMMON_FLAGS}"),
        ("CXXFLAGS", cxxflags_value(p.cxxflags)),
        ("FCFLAGS", "${COMMON_FLAGS}"),
        ("FFLAGS", "${COMMON_FLAGS}"),
        ("RUSTFLAGS", rustflags),
        ("MAKEOPTS", jobs or plan_values.pop("MAKEOPTS", "")),
        ("LC_MESSAGES", "C.UTF-8"),
        ("EMERGE_DEFAULT_OPTS", p.emerge_opts),
        ("ACCEPT_LICENSE", effective_accept_license(p)),
        ("GENTOO_MIRRORS", " ".join(mirror_list(p.mirror, p.mirrors, p.mirror_fallbacks))),
        ("USE", " ".join(p.use_flags)),
        ("FEATURES", features),
        # "auto" is a request for detection, not a value Portage understands; native tuning moves the flags to package.use.
        ("CPU_FLAGS_X86", "" if p.cpu_flags == "auto" or native else p.cpu_flags),
        ("VIDEO_CARDS", "" if p.video_cards == "auto" else p.video_cards),
        ("GRUB_PLATFORMS", "pc efi-64"),
    ]
    values.extend(sorted(make_conf_settings(manifest.network.proxy).items()))
    plan_values.pop("MAKEOPTS", None)
    lines: list[str] = []
    emitted: set[str] = set()
    for key, value in values:
        if key in emitted:
            continue
        if key in plan_values:
            extra = plan_values.pop(key)
            value = f"{value} {extra}".strip() if key in _INCREMENTAL_KEYS else extra
        if not value.strip():
            continue
        emitted.add(key)
        lines.append(f"{key}={shell_value(value)}")
    for key, value in plan_values.items():
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) and key not in emitted:
            lines.append(f"{key}={shell_value(value)}")
    return "\n".join(lines) + "\n"


def render_package_use(package_use: Mapping[str, Sequence[str]]) -> str:
    lines: list[str] = []
    for atom, flags in sorted(package_use.items()):
        if not PACKAGE_USE_ATOM.fullmatch(atom) or any(not USE_TOKEN_RE.fullmatch(flag) for flag in flags):
            raise ConfigurationError(f"Invalid package.use entry: {atom}")
        lines.append(f"{atom} {' '.join(flags)}".rstrip())
    return "\n".join(lines) + ("\n" if lines else "")


class PortageMixin(ConfigurerBase):
    def effective_make_opts(self, manifest: InstallManifest, plan: object | None = None) -> str:
        """Explicit MAKEOPTS, else the hardware plan's, else a value sized for this machine."""
        if manifest.portage.make_opts:
            return manifest.portage.make_opts
        planned = str((getattr(self._plan(plan), "make_conf", None) or {}).get("MAKEOPTS", "")).strip()
        if planned:
            return planned
        resources = self.resources()
        return auto_make_opts(resources.cpus, resources.mem_available, manifest.storage.swap_mib)

    def emerge_environment(self, manifest: InstallManifest) -> dict[str, str]:
        """Environment for the installer's own chroot emerges: proxy and parallel package builds."""
        environment = proxy_environment(manifest.network.proxy)
        if "--jobs" not in manifest.portage.emerge_opts:
            resources = self.resources()
            jobs = auto_emerge_jobs(resources.cpus, resources.mem_total)
            if jobs > 1:
                environment["EMERGE_DEFAULT_OPTS"] = f"{manifest.portage.emerge_opts} --jobs={jobs} --load-average={resources.cpus}".strip()
        return environment

    def package_use_table(self, manifest: InstallManifest, plan: object | None = None) -> dict[str, list[str]]:
        """The installer's package.use: the user's entries plus everything the chosen options need."""
        package_use = {atom: list(flags) for atom, flags in manifest.portage.package_use.items()}
        system, options, storage = manifest.system, manifest.options, manifest.storage
        if system.initramfs == "dracut":
            # Distribution kernels with initramfs require this installkernel hook
            # before the kernel package is merged.
            _require_use(package_use, "sys-kernel/installkernel", "dracut")
        if system.bootloader == "grub":
            # Regenerate grub.cfg whenever a kernel is installed (layout=grub keeps the files in /boot).
            _require_use(package_use, "sys-kernel/installkernel", "grub")
        if system.bootloader == "grub" and system.dual_boot:
            # os-prober needs grub's mount support to read the other systems' filesystems.
            _require_use(package_use, "sys-boot/grub", "mount")
        if system.uki or system.bootloader == "systemd-boot":
            atom = self.systemd_package(system)
            boot_flags = ["boot", *(["kernel-install"] if system.init == "openrc" else []), *(["ukify"] if system.uki else [])]
            package_use[atom] = [flag for flag in package_use.get(atom, []) if flag not in ("-boot", "-kernel-install", "-ukify")]
            package_use[atom].extend(flag for flag in boot_flags if flag not in package_use[atom])
        if options.install_hwmonux:
            # Minimal profiles do not enable the GUI flags required by PySide/Qt.
            _require_use(package_use, "sys-apps/i2c-tools", "python")
            _require_use(package_use, "dev-qt/qtbase", "opengl")
            _require_use(package_use, "dev-qt/qttools", "opengl")
            _require_use(package_use, "x11-libs/libxkbcommon", "X")
            # NVIDIA sensors need NVML, which needs the licence-masked proprietary driver: the hardware plan
            # turns that flag on when the driver is really installed. USB sensors (liquidctl) are optional.
            _default_use(package_use, "app-misc/hwmonux", "-nvidia", "liquidctl")
        if options.install_hwmonux or options.install_portagehub:
            # Both companions can pull in Mesa[X], which requires libglvnd[X] on the minimal profile.
            _require_use(package_use, "media-libs/libglvnd", "X")
        # The official binary QtWebEngine (KDE pulls it in) is built with bindist; without the flag the
        # binary is ignored and Chromium is compiled from source for hours. bindist only leaves out
        # proprietary codecs in embedded web views. Set with and without the binary host, so switching
        # it later rebuilds nothing; a user's own choice for the flag wins.
        _default_use(package_use, "dev-qt/qtwebengine", "bindist")
        if manifest.network.manager == "networkmanager":
            # NetworkManager uses wpa_supplicant's D-Bus interface for Wi-Fi.
            _require_use(package_use, "net-wireless/wpa_supplicant", "dbus")
        if storage.lvm:
            # lvm2[-lvm] installs only device-mapper, without the tools needed to activate the root volume.
            _require_use(package_use, "sys-fs/lvm2", "lvm")
        if options.install_portagehub and system.init == "systemd":
            # PortageHub's polkit dependency needs systemd's policykit support even on the minimal profile.
            _require_use(package_use, "sys-apps/systemd", "policykit")
        if system.init == "systemd" and storage.luks:
            # Dracut's systemd-cryptsetup module needs the helper installed by systemd[cryptsetup].
            _require_use(package_use, "sys-apps/systemd", "cryptsetup")
        if manifest.accounts.privilege == "doas":
            # doas.conf uses "persist", which the package only honours when built with it.
            _require_use(package_use, "app-admin/doas", "persist")
        if system.desktop != "minimal":
            # Current binary libwebp[tiff] and tiff[webp] depend on each other.
            _forbid_use(package_use, "media-libs/libwebp", "tiff")
            for atom, flags in DESKTOP_USE.get(system.desktop, {}).items():
                _require_use(package_use, atom, *flags)
        return package_use

    def binhost_active(self, manifest: InstallManifest) -> bool:
        """True when the binary package host is used: it is wanted and its signing keys were set up (getuto did not fail)."""
        return manifest.portage.binhost and not self.path(BINHOST_OFF_MARKER).exists()

    def sync_mode(self, manifest: InstallManifest) -> bool:
        """True when the Portage tree is synced with rsync (verified); False for the web snapshot (emerge-webrsync, the default).

        A settings file from before ``rsync_mode`` existed asked for rsync by naming a mirror; that still counts.
        """
        return manifest.portage.rsync_mode == "sync" or bool(manifest.portage.rsync_mirror)

    def configure_portage(self, manifest: InstallManifest, plan: object | None = None, *, hardware: dict | None = None) -> list[str]:
        """Write /etc/portage. ``hardware`` is the daemon's probe of this computer (binary package variant, CPU flags).

        Returns notes for the user (a choice that could not be applied as asked). Writing the same files again is harmless.
        """
        plan = self._plan(plan)
        notes: list[str] = []
        p = manifest.portage
        portable = manifest.hardware.target == "portable"
        self.write("etc/portage/make.conf", render_make_conf(manifest, plan, make_opts=self.effective_make_opts(manifest, plan),
                                                             binhost=self.binhost_active(manifest)))
        self.write("etc/portage/package.use/installer", render_package_use(self.package_use_table(manifest, plan)))
        if native_wanted(p, portable):
            flags = (p.cpu_flags if p.cpu_flags not in {"", "auto"} else "") \
                or str((getattr(plan, "make_conf", None) or {}).get("CPU_FLAGS_X86", "")).strip() or " ".join(cpu_flags_of(hardware))
            if flags and "${" not in flags:
                self.write(CPU_FLAGS_FILE, render_cpu_flags(flags))
            else:
                notes.append("This processor's CPU flags could not be read, so the profile's defaults are used next to the native compiler flags.")
        if p.package_license:
            self.write("etc/portage/package.license/installer", render_package_license(p.package_license))
        hardware_use = getattr(plan, "package_use", None)
        if hardware_use:
            # Alphabetically after "installer", so within one directory the hardware lines win.
            self.write("etc/portage/package.use/installer-hardware", render_package_use(hardware_use))
        hardware_licenses = getattr(plan, "package_license", None)
        if hardware_licenses:
            self.write("etc/portage/package.license/installer-hardware", render_package_license(hardware_licenses))
        hardware_mask = _plan_list(plan, "package_mask")
        if hardware_mask:
            self.write("etc/portage/package.mask/installer-hardware", "".join(f"{entry}\n" for entry in hardware_mask))
        if p.binhost:
            url, note = binhost_url(p, cpu_level_of(hardware), portable)
            if note:
                notes.append(note)
            # The stage3 ships this very file: replacing it (instead of adding a second file) keeps one [gentoobinhost] section.
            self.write(f"etc/portage/binrepos.conf/{BINREPO_NAME}.conf", render_binrepos(url))
        if self.sync_mode(manifest):
            uri = p.rsync_mirror or DEFAULT_RSYNC_URI
            if not uri.startswith("rsync://"):
                raise ConfigurationError("The repository sync mirror must be an rsync:// address")
            self.write("etc/portage/repos.conf/gentoo.conf", render_repos_conf(self.read_text("usr/share/portage/config/repos.conf"), uri))
        return notes

    def prepare_binhost_trust(self, manifest: InstallManifest, plan: object | None = None, *, hardware: dict | None = None) -> str:
        """Set up the keys that check the signatures of binary packages (getuto) before the first emerge.

        Returns "" when all is well and a warning when it failed. Then the binary package host is switched off for the rest of
        the installation and everything is built from source: a package whose signature cannot be checked is never installed.
        """
        if not manifest.portage.binhost or self.dry_run or self.path(BINHOST_OFF_MARKER).exists():
            return ""
        error = ""
        for attempt in range(2):
            try:
                self.chroot(("getuto",))
                return ""
            except RunnerError as exc:
                error = str(exc.output).strip()[-200:] or f"exit status {exc.returncode}"
                if attempt == 0:
                    self.sleep(3)
        self.write(BINHOST_OFF_MARKER, f"getuto failed: {error}\n")
        self.configure_portage(manifest, plan, hardware=hardware)
        return ("The keys that check ready-made packages could not be set up (" + error + "), so no ready-made packages are used: "
                "everything is built from source, which takes much longer. Nothing unsigned was accepted.")

    def _variable(self, relative: str, name: str) -> str:
        """The value of NAME=... in a shell-style file of the new system ('' when it has none)."""
        for line in reversed(self.read_text(relative).splitlines()):
            if line.startswith(f"{name}="):
                return shlex.split(line)[0].partition("=")[2]
        return ""

    def prepare_companions(self, manifest: InstallManifest) -> str:
        """Make the live image's companion applications installable; returns a problem description or ""."""
        try:
            self.configure_companion_overlay(manifest)
        except (ConfigurationError, OSError) as exc:
            return str(exc)
        return ""

    def companion_archive(self, prefix: str) -> Path:
        """The one hwmonux-*.tar.zst / portagehub-*.tar.zst in the live image's companion bundle."""
        matches = sorted((self.companion_bundle / "distfiles").glob(f"{prefix}-*.tar.zst"))
        if len(matches) != 1:
            found = ", ".join(path.name for path in matches) or "none"
            raise ConfigurationError(f"Expected exactly one {prefix} source archive in {self.companion_bundle / 'distfiles'}, found: {found}")
        return matches[0]

    def configure_companion_overlay(self, manifest: InstallManifest) -> None:
        if not (manifest.options.install_hwmonux or manifest.options.install_portagehub):
            return
        if self.dry_run:
            return
        overlay = self.companion_bundle / "overlay"
        if not (overlay / "profiles/repo_name").is_file():
            raise ConfigurationError(f"Live companion overlay is missing: {overlay}")
        destination = self.path("var/db/repos/gentoo-live-installer")
        shutil.copytree(overlay, destination, dirs_exist_ok=True, symlinks=False)
        for prefix, enabled in (("hwmonux", manifest.options.install_hwmonux), ("portagehub", manifest.options.install_portagehub)):
            if enabled:
                source = self.companion_archive(prefix)
                cache = self.path("var/cache/distfiles")
                cache.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, cache / source.name)
        if manifest.options.install_hwmonux:
            # liquidctl is available for amd64 in the pinned Gentoo tree, but
            # carries only the testing keyword; HWMonux uses its USB sensors.
            self.write("etc/portage/package.accept_keywords/gentoo-installer-companions",
                       "app-misc/liquidctl ~amd64\ndev-python/hidapi ~amd64\n")
        self.write("etc/portage/repos.conf/gentoo-live-installer.conf",
                   "[gentoo-live-installer]\nlocation = /var/db/repos/gentoo-live-installer\nmasters = gentoo\nauto-sync = no\n")
