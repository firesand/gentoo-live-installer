"""PORT: the emerge engine, package sets, failure diagnosis and the desktop install."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from gentoo_installer import recommend
from gentoo_installer.manifest import ATOM_RE, InstallManifest, USE_TOKEN_RE

from .configure_base import _plan_list, ConfigurationError
from .configure_boot import efi_arch
from .configure_portage import COMPANION_ATOMS, MEMORY_ENV_FILE, MEMORY_ENV_NAME, ninja_opts_from_make_opts, PACKAGE_ENV_FILE, PortageMixin
from .runner import CommandResult, RunnerError



class PackageInstallError(ConfigurationError):
    """A package set could not be installed; ``failed`` names what is missing."""

    def __init__(self, message: str, failed: Sequence[str] = (), resolution: bool = False) -> None:
        super().__init__(message)
        self.failed = list(failed)
        self.resolution = resolution


# What an out-of-memory failure says when the build already had one compile job: nothing is left to lower.
NOT_ENOUGH_MEMORY = "It ran out of memory even with one compile job: this PC needs more memory or swap for it."


# --- explaining a failed emerge ---------------------------------------------------------------------

# How much of the end of a failed package's build log is read: the compiler's own error is near the end of it.
BUILD_LOG_TAIL_BYTES = 256 * 1024


# Of that, the last lines are searched, and a longer line is a build command, not a message.
_LOG_LINES = 400


_LOG_LINE_CHARS = 300


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class FailedBuild:
    """A package that emerge could not build or install."""

    package: str  # category/name-version, without slot and repository
    phase: str = ""  # the Portage phase that failed (compile, configure, install, ...), "" when emerge did not say
    log: str = ""  # its build log as emerge names it: a path inside the new system


@dataclass(frozen=True)
class EmergeDiagnosis:
    resolution: bool  # the package set cannot be resolved; retrying cannot help
    summary: str  # short explanation for the user, empty when nothing recognisable was found
    build_failure: bool = False  # a package failed to compile or install
    transient: bool = False  # a download or network problem; a retry may well work
    out_of_memory: bool = False  # a build was killed for lack of memory; fewer parallel jobs may help
    failures: tuple[FailedBuild, ...] = ()  # the packages that failed themselves
    dropped: tuple[str, ...] = ()  # packages --keep-going left out because one they need failed

    @property
    def kind(self) -> str:
        if self.resolution:
            return "resolution"
        if self.out_of_memory:
            return "memory"
        if self.transient:
            return "transient"
        return "build" if self.build_failure else "unknown"


_RESOLUTION_MARKERS = (
    "USE changes are necessary", "REQUIRED_USE flag constraints are unsatisfied", "keyword changes are necessary",
    "license changes are necessary", "mask changes are necessary", "have been masked", "no ebuilds to satisfy",
    "Multiple package instances within a single package slot", "cannot be installed at the same time",
    "has unmet requirements", "Dependency problem", "circular dependencies", "Circular dependencies",
)


_TRANSIENT_PATTERN = re.compile(
    r"Fetch failed for|Temporary failure in name resolution|Could not resolve host|Name or service not known|"
    r"Connection (?:timed out|reset|refused)|Network is unreachable|Operation timed out|Couldn't connect|"
    r"Failed to connect|SSL connection|HTTP(?: error)? 5\d\d|503 Service|502 Bad Gateway", re.IGNORECASE)


_MEMORY_PATTERN = re.compile(
    r"signal terminated program|Killed\b|virtual memory exhausted|Cannot allocate memory|std::bad_alloc|"
    r"out of memory|memory exhausted|oom-kill", re.IGNORECASE)


# In a build log the signs are narrower: a compiler, linker or runtime that the kernel killed or that could not get memory.
_LOG_MEMORY_PATTERN = re.compile(
    r"signal terminated program|internal compiler error: Killed|virtual memory exhausted|Cannot allocate memory|std::bad_alloc|"
    r"memory allocation of \d+ bytes failed|out of memory|memory exhausted|oom-kill|terminated with signal 9|signal: 9, SIGKILL|(?-i:\bKilled\b)",
    re.IGNORECASE)


# The first of these that any line matches names the reason, so a compiler's "error:" beats a make or ninja summary line.
_LOG_ERROR_PATTERNS = (
    re.compile(r"(?<![-\w])(?:fatal )?error(?:\[\w+\])?:", re.IGNORECASE),
    re.compile(r"(?<![-\w])error(?![-\w=])|\bFAILED\b|undefined reference", re.IGNORECASE),
)


# What Portage prints about failed packages. Its summary block (only when several failed or --keep-going is on):
#   " *  (dev-python/pyside-6.11.2:6/6.11.2::gentoo, ebuild scheduled for merge), Log file:" and "'/var/tmp/.../build.log'"
# It is an error message, so it starts with " * ": a slot conflict names its packages in the very same words, without that prefix.
_FAILED_ENTRY = re.compile(r"\s*\*\s+\((?P<package>[^\s:,()]+)(?::[^\s,()]*)?::[^\s,()]+, (?:ebuild|binary) scheduled for merge[^)]*\)(?P<postinst> \(postinst failed\))?")


#   " * ERROR: dev-python/pyside-6.11.2::gentoo failed (compile phase):" (the messages of the package; always printed)
_FAILED_PHASE = re.compile(r"ERROR: (?P<package>\S+?)(?:::\S+)? failed \((?P<phase>\w+) phase\)")


#   ">>> Failed to emerge dev-python/pyside-6.11.2, Log file:"
_FAILED_TO_EMERGE = re.compile(r"Failed to emerge (?P<package>[^\s,]+),")


#   " * emerge --keep-going: app-misc/hwmonux-1.0.0-r1 dropped because it requires"
_DROPPED = re.compile(r"emerge --keep-going: (?P<package>\S+) dropped because")


# Every place that names a build log has its path: /var/tmp/portage/<category>/<name-version>/temp/build.log
_BUILD_LOG = re.compile(r"'(?P<path>/[^'\s]*?/(?P<package>[^/'\s]+/[^/'\s]+)/temp/build\.log)'")


# A package that checks its memory needs before it builds (check-reqs.eclass: qtwebengine wants 2.5 GB per job) stops emerge with
#   " * There is NOT at least 10 GiB RAM" and " * ERROR: dev-qt/qtwebengine-6.11.2::gentoo failed (pretend phase):"; fewer jobs fix that.
_SHORT_OF_RAM = re.compile(r"There is NOT at least (?P<size>\d+(?:\.\d+)? \w+) RAM")


_SHORT_OF_SOMETHING = re.compile(r"There is NOT at least .+")


# The old summary block: " * 1) dev-qt/qtwebengine-6.11.2::gentoo (/var/tmp/portage/...)".
_NUMBERED_ENTRY = re.compile(r"\s*\*\s+\d+\)\s+(?P<package>\S+?)::")


# phase -> (what the build was doing when it died, what to say about the failure)
_PHASES = {
    "setup": ("setting up", "failed in its setup"), "unpack": ("unpacking", "failed to unpack"), "prepare": ("preparing", "failed to prepare"),
    "configure": ("configuring", "failed to configure"), "compile": ("compiling", "failed to compile"), "test": ("testing", "failed its tests"),
    "install": ("installing", "failed to install"), "preinst": ("installing", "failed before it was installed"),
    "pretend": ("checking", "failed its check before the build"),
    "postinst": ("installing", "was installed, but its final step failed"),
}


def _build_log_findings(text: str) -> tuple[str, str]:
    """(the line that shows the build was killed for lack of memory, the first error line) of the end of a build log; either may be ''."""
    lines = [line for line in (_ANSI.sub("", line).strip() for line in text.splitlines()) if line][-_LOG_LINES:]
    short = [line for line in lines if len(line) <= _LOG_LINE_CHARS]  # a longer line is a build command, not a message
    sign = next((found for found in (_LOG_MEMORY_PATTERN.search(line) for line in lines) if found), None)
    if sign is not None:
        memory = sign.string.strip()
        if len(memory) > _LOG_LINE_CHARS:
            # cargo ends the line of the whole rustc command with the kill: name the error before it, and the sign
            before = next((line for line in short if _LOG_ERROR_PATTERNS[0].search(line)), "")
            memory = f"{before} ({sign.group(0)})" if before else sign.group(0)
        return memory, ""
    for pattern in _LOG_ERROR_PATTERNS:
        error = next((line for line in short if pattern.search(line)), "")
        if error:
            return "", error
    return "", ""


def _failure_sentence(failure: FailedBuild, memory: str, error: str) -> str:
    """What happened to one package, in a sentence that starts with its name ('' when nothing is known beyond the name)."""
    doing, failed = _PHASES.get(failure.phase, ("building", "failed to build"))
    if memory:
        return f"{failure.package} ran out of memory while {doing}: {memory[:160]}"
    if error:
        return f"{failure.package} {failed}: {error[:160]}"
    return f"{failure.package} {failed}" if failure.phase else ""


def explain_emerge_failure(output: str, read_log: Callable[[str], str] | None = None) -> EmergeDiagnosis:
    """Turn the tail of a failed emerge run into a short, actionable explanation.

    ``output`` is the end of what emerge printed. With ``read_log`` (the end of a build log, given the path emerge
    printed for it) the diagnosis also reads why a package failed: when other packages were built after the failure,
    as --keep-going does, the compiler's own error is no longer in the output.
    """
    output = _ANSI.sub("", output)
    lines = [line.rstrip() for line in output.splitlines()]
    items: list[str] = []
    resolution = any(marker in output for marker in _RESOLUTION_MARKERS)

    def following(marker: str, limit: int = 60) -> list[str]:
        for index, line in enumerate(lines):
            if marker in line:
                block: list[str] = []
                for follower in lines[index + 1:index + 1 + limit]:
                    if not follower.strip() and block:
                        break
                    if follower.strip():
                        block.append(follower.strip())
                return block
        return []

    for kind, marker in (("USE change needed", "USE changes are necessary"), ("licence change needed", "license changes are necessary"),
                         ("keyword change needed", "keyword changes are necessary"), ("mask change needed", "mask changes are necessary")):
        block = following(marker)
        reason = ""
        for entry in block:
            if entry.startswith("# required by"):
                reason = reason or entry.removeprefix("# required by").strip().split(" (")[0].split("::")[0]
            elif entry and not entry.startswith(("#", "(", "!!!")):
                items.append(f"{kind}: {entry}" + (f" (required by {reason})" if reason else ""))
                reason = ""
    phases: dict[str, str] = {}  # package -> phase, in the order the packages are first named
    for index, line in enumerate(lines):
        if "REQUIRED_USE flag constraints are unsatisfied" in line:
            package = next((lines[i].strip().lstrip("- ").split(" USE=")[0] for i in range(index - 1, max(index - 4, -1), -1)
                            if lines[i].strip().startswith("- ")), "a package")
            constraint = next((entry.strip() for entry in lines[index + 1:index + 4] if entry.strip()), "")
            items.append(f"{package} has unsatisfiable REQUIRED_USE: {constraint[:160]}")
        match = re.search(r'All ebuilds that could satisfy "([^"]+)" have been masked', line)
        if match:
            reason = next((entry.strip() for entry in lines[index + 1:index + 6] if "masked by" in entry), "")
            items.append(f"{match.group(1)} is masked" + (f" ({reason.split('masked by:')[-1].strip(' )')})" if reason else ""))
        match = re.search(r'no ebuilds to satisfy "([^"]+)"', line)
        if match:
            items.append(f"no package matches {match.group(1)}")
        if "[blocks B" in line:
            items.append("blocked packages: " + line.strip()[:160])
        if "Multiple package instances within a single package slot" in line:
            items.append("slot conflict between packages")
        for found in (_FAILED_ENTRY.match(line), _FAILED_PHASE.search(line), _FAILED_TO_EMERGE.search(line), _NUMBERED_ENTRY.match(line)):
            if found:
                groups = found.groupdict()
                package = groups["package"]
                phases[package] = groups.get("phase") or ("postinst" if groups.get("postinst") else "") or phases.get(package, "")
    dropped = tuple(dict.fromkeys(m.group("package") for m in _DROPPED.finditer(output)))
    logs = {m.group("package"): m.group("path") for m in _BUILD_LOG.finditer(output)}
    failures = tuple(FailedBuild(package, phase, logs.get(package) or f"/var/tmp/portage/{package}/temp/build.log")
                     for package, phase in phases.items() if package not in dropped)
    sentences: list[str] = []
    undescribed: list[str] = []
    killed = False
    short_of_ram = _SHORT_OF_RAM.search(output)
    short_of_something = _SHORT_OF_SOMETHING.search(output)
    for failure in failures[:4]:
        if failure.phase == "pretend" and short_of_something:
            killed = killed or bool(short_of_ram)
            sentences.append(f"{failure.package} needs {short_of_ram['size']} of memory for its compile jobs, and this PC has less" if short_of_ram
                             else f"{failure.package} {_PHASES['pretend'][1]}: {short_of_something.group(0).strip()[:160]}")
            continue
        text = ""
        if read_log is not None:
            try:
                text = read_log(failure.log) or ""
            except Exception:  # noqa: BLE001 - a log that cannot be read only costs the detail
                text = ""
        memory, error = _build_log_findings(text)
        killed = killed or bool(memory)
        sentence = _failure_sentence(failure, memory, error)
        (sentences if sentence else undescribed).append(sentence or failure.package)
    undescribed.extend(failure.package for failure in failures[4:])
    items = sentences + items
    if undescribed:
        items.append("failed to build: " + ", ".join(dict.fromkeys(undescribed)))
    fetch = [m.group(1) for line in lines for m in [re.search(r"Fetch failed for ([^\s,]+)", line)] if m]
    if fetch:
        items.append("download failed for " + ", ".join(dict.fromkeys(fetch)) + " (check the network connection)")
    unique = list(dict.fromkeys(items))
    summary = "; ".join(unique[:6]) + (f"; and {len(unique) - 6} more" if len(unique) > 6 else "")
    if not summary:
        tail = [line.strip() for line in lines if line.strip()][-3:]
        summary = " | ".join(tail)[:400]
    build_failure = bool(failures) or bool(re.search(r"\* ERROR: \S+ failed \(", output))
    memory_in_output = build_failure and bool(_MEMORY_PATTERN.search(output))
    if memory_in_output and not killed:
        summary += " (the build ran out of memory)"
    return EmergeDiagnosis(resolution, summary, build_failure, bool(fetch) or bool(_TRANSIENT_PATTERN.search(output)), killed or memory_in_output,
                           failures, dropped)


# emerge's own way out of a dependency cycle, for example "- dev-python/pillow-12.3.0 (Change USE: -truetype)".
_CYCLE_BREAK_RE = re.compile(r"^- (?P<cpv>[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+) \(Change USE: (?P<flags>[^)]+)\)\s*$", re.M)


CYCLE_BREAK_USE = "etc/portage/package.use/zz-installer-cycle-break"


CYCLE_BREAK_RECORD = "var/lib/gentoo-installer/cycle-breaks"


MAX_CYCLE_BREAKS = 3


def package_of(cpv: str) -> str:
    """'dev-python/pillow-12.3.0' -> 'dev-python/pillow' (a package name never ends in -<digit>...)."""
    category, _, rest = cpv.partition("/")
    match = re.match(r"(.+?)-[0-9]", rest)
    return f"{category}/{match.group(1) if match else rest}"


def cycle_break_suggestion(output: str) -> tuple[str, list[str]] | None:
    """The package and USE change emerge suggests to break a dependency cycle, or None.

    Building from source on a desktop profile can hit cycles a binary install never sees (in 2026
    glib -> docutils -> pillow[truetype] -> harfbuzz -> glib). emerge names a change that breaks the
    cycle; the first one it offers is used.
    """
    start = output.find("break this cycle")
    if start < 0 or "ircular dependencies" not in output:
        return None
    match = _CYCLE_BREAK_RE.search(output, start)
    if not match:
        return None
    flags = [flag.lstrip("+") for flag in match.group("flags").split()]
    if not flags or not all(USE_TOKEN_RE.fullmatch(flag) for flag in flags) or not ATOM_RE.fullmatch(package_of(match.group("cpv"))):
        return None
    return match.group("cpv"), flags



@dataclass
class KeepGoingResult:
    missing: list[str] = field(default_factory=list)  # requested packages that are still not installed
    reason: str = ""
    kind: str = ""  # "" (all installed) | EmergeDiagnosis.kind of the last failure



class PackagesMixin(PortageMixin):
    def flag_rebuild(self, manifest: InstallManifest, plan: object | None = None) -> tuple[str, list[tuple[str, str]]] | None:
        """The stage3 package whose USE flags the installer changes, and what must exist once they took effect.

        systemd (systemd-utils on OpenRC) is part of every stage3, so ``emerge --noreplace`` skips it and the
        flags written to package.use (cryptsetup for the initramfs, boot for systemd-boot, ukify for a Unified
        Kernel Image) would never apply. The package is rebuilt once with ``--update --newuse``. The pairs are a
        path in the target and the sentence for when that file is missing afterwards; None when nothing is needed.
        """
        system = manifest.system
        crypt_helper = system.init == "systemd" and manifest.storage.luks
        systemd_boot = system.bootloader == "systemd-boot"
        if not (crypt_helper or systemd_boot or system.uki):
            return None
        atom = self.systemd_package(system)
        arch = efi_arch(self._plan(plan))
        images = "usr/lib/systemd/boot/efi"

        def missing(name: str, flag: str, purpose: str) -> str:
            return f"{name} is missing after rebuilding {atom} with the {flag} USE flag, so {purpose}"

        expected: list[tuple[str, str]] = []
        if crypt_helper:
            expected.append(("usr/lib/systemd/systemd-cryptsetup", "systemd-cryptsetup is missing after enabling systemd[cryptsetup]"))
        if systemd_boot:
            purpose = "systemd-boot cannot be installed"
            if system.init == "openrc":  # systemd always has bootctl; its images come with the boot flag
                expected.append(("usr/bin/bootctl", missing("bootctl", "boot", purpose)))
            expected.append((f"{images}/systemd-boot{arch}.efi", missing(f"systemd-boot{arch}.efi", "boot", purpose)))
        if system.uki:
            purpose = "the Unified Kernel Image cannot be built"
            expected.append(("usr/bin/ukify", missing("ukify", "ukify", purpose)))
            expected.append((f"{images}/linux{arch}.efi.stub", missing(f"linux{arch}.efi.stub", "boot", purpose)))
        return atom, expected

    def essential_atoms(self, manifest: InstallManifest, plan: object | None = None) -> list[str]:
        """Packages without which the installed system cannot be managed from a console."""
        system, options = manifest.system, manifest.options
        atoms: list[str] = []
        network_package = {"networkmanager": "net-misc/networkmanager", "dhcpcd": "net-misc/dhcpcd",
                           "netifrc": "net-misc/netifrc"}.get(manifest.network.manager)
        if network_package:
            atoms.append(network_package)
        atoms.extend(self.early_boot_atoms(manifest, plan))
        atoms.append("app-admin/sudo" if manifest.accounts.privilege == "sudo" else "app-admin/doas")
        if options.ssh:
            atoms.append("net-misc/openssh")
        if options.chrony:
            atoms.append("net-misc/chrony")
        if self.wants_cron(manifest):
            atoms.append("sys-process/cronie")
        if options.file_indexing:
            atoms.append("sys-apps/mlocate")
        if options.shell_completion:
            atoms.append("app-shells/bash-completion")
        if options.logger:
            if system.init == "openrc":
                # syslog-ng writes /var/log/messages forever without a rotator.
                atoms.extend(("app-admin/syslog-ng", "app-admin/logrotate"))
            else:
                atoms.append("sys-apps/systemd")
        if system.console_font.startswith("ter-"):
            atoms.append("media-fonts/terminus-font")
        return list(dict.fromkeys(atoms))

    def package_atoms(self, manifest: InstallManifest, plan: object | None = None) -> list[str]:
        """Every package the installer asks for, in install order (the union of the stage sets)."""
        atoms = [*self.essential_atoms(manifest, plan), *self.bootloader_atoms(manifest),
                 *self.desktop_atoms(manifest, plan).everything()]
        return list(dict.fromkeys(atoms))

    # ----- installing packages --------------------------------------------------------------------

    def emerge(self, manifest: InstallManifest, arguments: Sequence[str], *, environment: Mapping[str, str] | None = None,
               capture: bool = False, one_job: bool = False) -> CommandResult:
        """Run emerge in the new system. ``one_job`` is the retry of a build that was killed for lack of memory: one compile job and one package at a time."""
        env = self.emerge_environment(manifest)
        env.update(environment or {})
        arguments = tuple(arguments)
        if one_job:
            # NINJAOPTS too: unset, ninja-utils derives it from MAKEOPTS, but an inherited value would win over that.
            env.update({"MAKEOPTS": "-j1", "NINJAOPTS": ninja_opts_from_make_opts("-j1")})
            arguments = ("--jobs=1", *arguments)  # the command line wins over --jobs in EMERGE_DEFAULT_OPTS
        return self.chroot(("emerge", *arguments), env=env, capture=capture)

    # ----- why a build failed --------------------------------------------------------------------------

    def build_log_tail(self, path: str) -> str:
        """The end of a failed package's build log. emerge names the log as the new system sees it (inside the chroot); '' when it cannot be read."""
        if self.dry_run or not path.startswith("/"):
            return ""
        try:
            log = self.path(path)
            if not stat.S_ISREG(log.stat().st_mode):
                return ""
            with log.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - BUILD_LOG_TAIL_BYTES))
                return handle.read(BUILD_LOG_TAIL_BYTES).decode("utf-8", errors="replace")
        except (OSError, ConfigurationError):
            return ""

    def explain(self, failure: RunnerError | str) -> EmergeDiagnosis:
        """Explain a failed emerge from its output and from the build logs of the packages that failed."""
        return explain_emerge_failure(failure if isinstance(failure, str) else failure.output, self.build_log_tail)

    def compile_jobs(self, package: str) -> int | None:
        """The compile jobs a package (category/name-version) gets: MAKEOPTS of the new system, or the lower limit of the hardware plan; None when unknown or unlimited."""
        try:
            jobs = recommend.make_opts_jobs(self._variable("etc/portage/make.conf", "MAKEOPTS"))
            listed = {fields[0] for line in self.read_text(PACKAGE_ENV_FILE).splitlines() for fields in [line.split("#")[0].split()]
                      if len(fields) > 1 and MEMORY_ENV_NAME in fields[1:]}
            limit = recommend.make_opts_jobs(self._variable(MEMORY_ENV_FILE, "MAKEOPTS")) if package_of(package) in listed else None
        except (OSError, ValueError, ConfigurationError):
            return None
        return limit if limit is not None and (jobs is None or limit < jobs) else jobs

    def one_job_cannot_help(self, manifest: InstallManifest, diagnosis: EmergeDiagnosis) -> bool:
        """Whether the packages that were killed already had one compile job and were built alone: a retry with one job would end the same way."""
        opts = self.emerge_environment(manifest).get("EMERGE_DEFAULT_OPTS", manifest.portage.emerge_opts)
        parallel = recommend.make_opts_jobs(opts)
        if parallel is None and re.search(r"(?:^|\s)(?:-j|--jobs)", opts) or parallel is not None and parallel > 1:
            return False  # other packages were built at the same time
        return bool(diagnosis.failures) and all(self.compile_jobs(failure.package) == 1 for failure in diagnosis.failures)

    def is_installed(self, atom: str) -> bool:
        if self.dry_run:
            return True
        category, _, name = atom.split(":")[0].partition("/")
        directory = self.path(f"var/db/pkg/{category}")
        return directory.is_dir() and any(re.fullmatch(re.escape(name) + r"-[0-9].*", entry.name) for entry in directory.iterdir())

    def pretend(self, manifest: InstallManifest, atoms: Sequence[str]) -> None:
        """Resolve the package set without installing it, so impossible choices fail in minutes.

        A dependency cycle that emerge knows how to break is broken here, before the stages run.
        """
        if not atoms:
            return
        # --noreplace never looks at a package that is installed, so the rebuild that the kernel stage does for
        # the flags of systemd(-utils) (see flag_rebuild) gets its own run; it shows what the new flags pull in.
        rebuild = self.flag_rebuild(manifest)
        for attempt in range(MAX_CYCLE_BREAKS + 1):
            try:
                # Same sources as the real install (binary packages included): the gate must predict
                # whether the installation would fail, not be stricter than it. scripts/lint-plan.py is
                # the strict, source-only check that runs in development.
                self.emerge(manifest, ("--pretend", "--noreplace", "--verbose=n", *atoms))
                if rebuild is not None:
                    self.emerge(manifest, ("--pretend", "--oneshot", "--update", "--newuse", "--verbose=n", rebuild[0]))
                return
            except RunnerError as exc:
                suggestion = cycle_break_suggestion(exc.output)
                if suggestion is not None and attempt < MAX_CYCLE_BREAKS:
                    self.break_cycle(manifest, *suggestion)
                    continue
                diagnosis = self.explain(exc)
                if diagnosis.resolution:
                    raise PackageInstallError(f"Portage cannot resolve the selected packages: {diagnosis.summary}", resolution=True) from exc
                self.notify("warning: the package resolution check could not run (" + diagnosis.summary[:200] + "); continuing")
                return

    def break_cycle(self, manifest: InstallManifest, cpv: str, flags: Sequence[str]) -> None:
        """Build one package with the USE change emerge suggested, as a Gentoo user breaks a cycle by hand.

        The change applies to that package only (a temporary package.use file), and the package is
        recorded in the new system so the desktop stage rebuilds it with the configured flags, also
        after a resume.
        """
        change = " ".join(flags)
        self.notify(f"Breaking a dependency cycle: building {cpv} once with USE=\"{change}\"")
        self.write(CYCLE_BREAK_USE, f"={cpv} {change}\n")
        try:
            self.emerge(manifest, ("--oneshot", f"={cpv}"))
        except RunnerError as exc:
            raise PackageInstallError(f"A dependency cycle could not be broken by building {cpv} with USE=\"{change}\": "
                                      + self.explain(exc).summary, resolution=True) from exc
        finally:
            if not self.dry_run:
                self.path(CYCLE_BREAK_USE).unlink(missing_ok=True)
        record = self.path(CYCLE_BREAK_RECORD)
        recorded = record.read_text().split() if record.is_file() else []
        if package_of(cpv) not in recorded:
            self.write(CYCLE_BREAK_RECORD, "\n".join([*recorded, package_of(cpv)]) + "\n")

    def restore_cycle_breaks(self, manifest: InstallManifest) -> list[str]:
        """Rebuild the packages a cycle break built with other flags; returns what is still left over."""
        record = self.path(CYCLE_BREAK_RECORD)
        if self.dry_run or not record.is_file():
            return []
        problems: list[str] = []
        for package in record.read_text().split():
            try:
                self.emerge(manifest, ("--oneshot", "--update", "--newuse", package))
            except RunnerError as exc:
                problems.append(f"{package} keeps the flags used to break a dependency cycle; later run: emerge --oneshot --newuse {package} "
                                f"({self.explain(exc).summary[:160]})")
        record.unlink(missing_ok=True)
        return problems

    def install_packages(self, manifest: InstallManifest, plan: object | None = None) -> None:
        """Install the whole package set with one emerge (the stages use install_essential/install_desktop)."""
        self.configure_companion_overlay(manifest)
        atoms = self.package_atoms(manifest, plan)
        if atoms:
            self.emerge(manifest, ("--noreplace", *atoms))

    def install_essential(self, manifest: InstallManifest, plan: object | None = None, attempts: int = 3) -> None:
        """Install the base system packages; this must succeed or the disk is not bootable.

        A dropped download is retried, a build that ran out of memory is retried with one compile
        job, anything else is reported at once.
        """
        atoms = self.essential_atoms(manifest, plan)
        if not atoms:
            return
        one_job = False
        for attempt in range(1, attempts + 1):
            try:
                self.emerge(manifest, ("--noreplace", *atoms), one_job=one_job)
                return
            except RunnerError as exc:
                diagnosis = self.explain(exc)
                hopeless = self.one_job_is_no_help(manifest, diagnosis, one_job)
                if diagnosis.kind in {"resolution", "build"} or attempt == attempts or hopeless:
                    raise PackageInstallError(f"The base system packages could not be installed: {self.memory_advice(diagnosis, hopeless)}",
                                              resolution=diagnosis.resolution) from exc
                if diagnosis.kind == "memory":
                    one_job = True
                    self.notify(f"Base system packages: {diagnosis.summary[:160]}; trying again with a single compile job ({attempt + 1}/{attempts})")
                else:
                    self.notify(f"Base system packages: {diagnosis.summary[:160]}; trying again ({attempt + 1}/{attempts})")
                self.sleep(5.0 * attempt)

    @staticmethod
    def memory_advice(diagnosis: EmergeDiagnosis, hopeless: bool) -> str:
        """The summary of a failed emerge; when one compile job was not enough either, it says so."""
        return diagnosis.summary.rstrip(". ") + f". {NOT_ENOUGH_MEMORY}" if hopeless else diagnosis.summary

    def one_job_is_no_help(self, manifest: InstallManifest, diagnosis: EmergeDiagnosis, one_job: bool) -> bool:
        """A build was killed for lack of memory, and the attempt already had one compile job (or had to have): another try would only fail the same way."""
        return diagnosis.kind == "memory" and (one_job or self.one_job_cannot_help(manifest, diagnosis))

    def emerge_keep_going(self, manifest: InstallManifest, atoms: Sequence[str], *, label: str, attempts: int = 3) -> KeepGoingResult:
        """Emerge atoms with --keep-going and retry what is still missing when a retry can help.

        Downloads that failed are retried; a build killed for lack of memory is retried with one
        compile job; a package that fails to build, or a set that cannot be resolved, is not (the
        same build would fail again, possibly after hours).
        """
        remaining = list(dict.fromkeys(atoms))
        result = KeepGoingResult(remaining, "", "unknown")
        one_job = False
        for attempt in range(1, attempts + 1):
            if not remaining:
                break
            try:
                self.emerge(manifest, ("--noreplace", "--keep-going", *remaining), one_job=one_job)
                return KeepGoingResult()
            except RunnerError as exc:
                diagnosis = self.explain(exc)
                remaining = [atom for atom in remaining if not self.is_installed(atom)]
                if not remaining:
                    self.notify(f"warning: {label}: some dependencies failed ({diagnosis.summary[:200]}) but everything requested is installed")
                    return KeepGoingResult()
                hopeless = self.one_job_is_no_help(manifest, diagnosis, one_job)
                result = KeepGoingResult(remaining, self.memory_advice(diagnosis, hopeless), diagnosis.kind)
                if diagnosis.kind in {"resolution", "build"} or attempt == attempts or hopeless:
                    return result
                if diagnosis.kind == "memory":
                    one_job = True
                    self.notify(f"{label}: {diagnosis.summary[:140]}; retrying with a single compile job ({attempt + 1}/{attempts})")
                else:
                    self.notify(f"{label}: {len(remaining)} package(s) failed ({diagnosis.summary[:160]}); retrying ({attempt + 1}/{attempts})")
                self.sleep(5.0 * attempt)
        return result

    def world_drift(self, manifest: InstallManifest) -> list[str]:
        """Installed packages that a USE change would rebuild; a drift between make.conf and the system.

        Informational: failures are ignored, and plain version upgrades (the tree may be newer than the
        stage3) are not drift.
        """
        try:
            output = self.emerge(manifest, ("--pretend", "--update", "--deep", "--newuse", "--usepkg=n", "--getbinpkg=n", "@world"), capture=True).stdout
        except RunnerError:
            return []
        rebuilds: list[str] = []
        for line in output.splitlines():
            match = re.match(r"\[ebuild\s+([A-Za-z ]*?)\s*\]\s+(\S+)", line)
            if match and "R" in match.group(1) and "U" not in match.group(1) and "N" not in match.group(1):
                rebuilds.append(match.group(2))
        return rebuilds
