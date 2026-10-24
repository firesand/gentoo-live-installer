from __future__ import annotations

import hashlib
import http.client
import os
import posixpath
import re
import stat
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from copy import copy
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence, TypeVar

from .proxy import build_opener
from .runner import InstallCancelled, Runner

T = TypeVar("T")
# Seconds to wait before retry 2, 3, 4, ...; the last value repeats. Tests shorten it.
BACKOFF_SECONDS = (2, 4, 8, 16)
ATTEMPTS_SINGLE_MIRROR = 5
ATTEMPTS_PER_MIRROR = 2
# A stage3 is rebuilt weekly; a signature much older than that means a rolled-back mirror or a wrong clock.
MAX_SIGNATURE_AGE = 60 * 24 * 3600
MAX_SIGNATURE_FUTURE = 24 * 3600
MIB = 1024 * 1024
# Room the download needs on top of the archive itself, and what the unpacked stage3 takes in the target.
DOWNLOAD_MARGIN_BYTES = 64 * MIB
UNPACKED_STAGE3_BYTES = 4096 * MIB
# Used when the index does not announce the archive size (stage3 archives are 250 to 900 MiB).
UNKNOWN_ARCHIVE_BYTES = 1024 * MIB
BAD_SIGNATURE_STATUSES = {
    "BADSIG": "the signature does not match the file",
    "EXPSIG": "the signature itself has expired",
    "EXPKEYSIG": "the signing key has expired",
    "REVKEYSIG": "the signing key was revoked",
    "ERRSIG": "the signature could not be checked",
    "NO_PUBKEY": "the signing key is not in the Gentoo release keyring",
}


class Stage3Error(RuntimeError):
    pass


class Stage3Transient(Stage3Error):
    """A failure worth retrying: a dropped connection, a timeout or a server-side error."""


class Stage3Truncated(Stage3Transient):
    """The server closed the connection before the whole file arrived; the partial download is kept."""


@dataclass(frozen=True)
class Stage3Archive:
    path: str
    size: int | None
    url: str
    digests_url: str
    signature_url: str


Progress = Callable[[int, int | None], None]


def free_bytes(path: Path) -> int:
    """Bytes a normal process can still write below ``path`` (the nearest existing directory is asked)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    stats = os.statvfs(probe)
    return stats.f_bavail * stats.f_frsize


def latest_index_url(mirror: str, variant: str) -> str:
    if not mirror.startswith("https://"):
        raise Stage3Error("Stage3 mirror must use HTTPS")
    return mirror.rstrip("/") + f"/releases/amd64/autobuilds/latest-stage3-{variant}.txt"


def parse_latest_index(text: str, mirror: str, variant: str) -> Stage3Archive:
    candidates: list[tuple[str, int | None]] = []
    marker = f"stage3-{variant}-"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        path = fields[0]
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or marker not in pure.name or not pure.name.endswith((".tar.xz", ".tar.zst")):
            continue
        size = None
        if len(fields) > 1 and fields[1].isdigit():
            size = int(fields[1])
        candidates.append((path, size))
    if len(candidates) != 1:
        raise Stage3Error(f"Latest-stage3 index does not identify exactly one {variant} archive")
    path, size = candidates[0]
    base = mirror.rstrip("/") + "/releases/amd64/autobuilds/"
    url = urllib.parse.urljoin(base, path)
    if urlsplit_scheme(url) != "https" or urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(base).netloc:
        raise Stage3Error("Stage3 index points outside the authenticated HTTPS mirror")
    # Gentoo publishes a clearsigned .DIGESTS, not a detached .DIGESTS.asc.
    return Stage3Archive(path, size, url, url + ".DIGESTS", url + ".DIGESTS")


def urlsplit_scheme(url: str) -> str:
    return urllib.parse.urlsplit(url).scheme


def parse_digests(text: str, archive_name: str) -> tuple[str, str]:
    algorithms: dict[str, str] = {}
    section: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        heading = re.fullmatch(r"# (SHA512|SHA256|BLAKE2B) HASH", line)
        if heading:
            section = heading.group(1).lower()
            continue
        bsd = re.fullmatch(r"(SHA512|SHA256) \((.+)\) = ([0-9a-fA-F]+)", line)
        if bsd and PurePosixPath(bsd.group(2)).name == archive_name:
            algorithms[bsd.group(1).lower()] = bsd.group(3).lower()
            continue
        fields = line.split()
        if len(fields) == 2 and re.fullmatch(r"[0-9a-fA-F]+", fields[0]) and PurePosixPath(fields[1].lstrip("*")).name == archive_name:
            algorithm = section or {64: "sha256", 128: "sha512"}.get(len(fields[0]))
            if algorithm == "blake2b":
                continue
            if algorithm not in {"sha512", "sha256"} or len(fields[0]) != {"sha512": 128, "sha256": 64}[algorithm]:
                raise Stage3Error("Signed DIGESTS contains a malformed archive hash")
            if algorithm:
                algorithms[algorithm] = fields[0].lower()
    for algorithm in ("sha512", "sha256"):
        if algorithm in algorithms:
            return algorithm, algorithms[algorithm]
    raise Stage3Error("Signed DIGESTS contains no SHA-512 or SHA-256 hash for the selected archive")


def release_signing_fingerprints(colon_listing: str) -> set[str]:
    """Primary fingerprints of the Release Engineering keys in ``gpg --show-keys --with-colons`` output.

    The keyring also carries the ebuild-repository and mirror signing keys; only Release
    Engineering signs stage3 files, so only those keys are accepted for them.
    """
    allowed: set[str] = set()
    fingerprint = ""
    matches = False
    usable = True
    for line in [*colon_listing.splitlines(), "pub::::::::::::"]:
        fields = line.split(":")
        if fields[0] == "pub":
            if fingerprint and matches and usable:
                allowed.add(fingerprint)
            fingerprint, matches = "", False
            usable = fields[1] not in {"r", "e", "i", "d"} if len(fields) > 1 else True
        elif fields[0] == "fpr" and not fingerprint and len(fields) > 9:
            fingerprint = fields[9]
        elif fields[0] == "uid" and len(fields) > 9 and "Release Engineering" in fields[9]:
            matches = True
    return allowed


class Stage3Manager:
    def __init__(
        self,
        runner: Runner,
        cache_dir: str | Path = "/var/tmp/gentoo-installer",
        keyring: str | Path = "/usr/share/openpgp-keys/gentoo-release.asc",
        cancel: threading.Event | None = None,
        *,
        proxy: str = "",
    ) -> None:
        self.runner = runner
        self.cache_dir = Path(cache_dir)
        self.keyring = Path(keyring)
        self.cancel = cancel or threading.Event()
        self.proxy = proxy
        self.fallback_mirrors: tuple[str, ...] = ()
        self.notify: Callable[[str], None] = lambda message: None
        self.now: Callable[[], float] = time.time
        self.free_bytes: Callable[[Path], int] = free_bytes
        self._binary_keyring: Path | None = None
        self._allowed_signers: set[str] = set()
        self._expected_size: int | None = None  # size announced by the index, for the download in progress

    def install(self, mirror: str, variant: str, target_root: str | Path, progress: Progress | None = None, *,
                proxy: str | None = None, fallback_mirrors: Iterable[str] = (),
                allowed_existing: Iterable[str] = ()) -> Path:
        if proxy is not None:
            self.proxy = proxy
        self.fallback_mirrors = tuple(fallback_mirrors)
        archive = self.discover(mirror, variant)
        name = PurePosixPath(archive.path).name
        digests = self._fetch_with_retry(self._source_urls(archive, ".DIGESTS", archive.digests_url), "the signed checksums")
        self._secure_cache_dir()
        digest_path = self.cache_dir / (name + ".DIGESTS")
        verified_path = self.cache_dir / (name + ".DIGESTS.verified")
        self._safe_cache_file(digest_path)
        self._safe_cache_file(verified_path)
        digest_path.write_bytes(digests)
        digest_path.chmod(0o600)
        self._prepare_keyring()
        verified_path.unlink(missing_ok=True)
        try:
            self._verify_signed(digest_path, verified_path)
            # Only parse bytes written by gpg after successful signature verification.
            algorithm, expected = parse_digests(verified_path.read_text(encoding="utf-8"), name)
        finally:
            verified_path.unlink(missing_ok=True)
        archive_path = self.cache_dir / name
        self._safe_cache_file(archive_path)
        cached = self._cached_archive_is_valid(archive_path, archive, algorithm, expected)
        # Before hours of work, not after a download that fills the live system's memory.
        self._require_space(self.cache_dir, self._download_bytes(archive, archive_path, cached), "download the stage3 archive",
                            "The live system keeps this folder in memory, so close other programs or use a computer with more memory.")
        self._require_space(Path(target_root), UNPACKED_STAGE3_BYTES, "unpack the stage3 into the new system",
                            "Choose a larger disk or partition for the root filesystem.")
        if not cached:
            for round_number in (1, 2):
                self._download_with_retry(archive, archive_path, progress)
                if self._hash(archive_path, algorithm) == expected:
                    break
                archive_path.unlink(missing_ok=True)
                if round_number == 2:
                    raise Stage3Error(f"Authenticated {algorithm} verification failed for {archive_path.name}")
                self.notify("The downloaded stage3 failed its checksum; downloading it once more")
        leftovers = self.unexpected_root_entries(Path(target_root), allowed_existing)
        if leftovers:
            self.notify("WARNING: the target filesystem is not empty (" + ", ".join(leftovers[:8]) +
                        (", ..." if len(leftovers) > 8 else "") + "); the stage3 is unpacked over the existing files")
        self.extract(archive_path, Path(target_root))
        return archive_path

    def _download_bytes(self, archive: Stage3Archive, destination: Path, cached: bool) -> int:
        """Space still needed to fetch the archive: nothing when a verified copy exists, less when a partial one does."""
        if cached:
            return 0
        partial = destination.with_name(destination.name + ".part")
        have = partial.stat().st_size if partial.is_file() else 0
        return max((archive.size or UNKNOWN_ARCHIVE_BYTES) - have, 0) + DOWNLOAD_MARGIN_BYTES

    def _require_space(self, where: Path, needed: int, action: str, advice: str) -> None:
        if needed <= 0:
            return
        try:
            available = self.free_bytes(where)
        except OSError:
            return  # cannot tell; the download or the unpacking will report the real problem
        if available < needed:
            raise Stage3Error(f"There is not enough free space to {action}: about {needed // MIB} MiB are needed in {where}, "
                              f"but only {available // MIB} MiB are free. {advice}")

    def discard(self, archive: Path) -> None:
        """Remove a downloaded archive and its checksum file, once the unpacked stage3 is safe on the disk."""
        for path in (archive, archive.with_name(archive.name + ".part"), archive.with_name(archive.name + ".DIGESTS")):
            if path.parent != self.cache_dir:
                raise Stage3Error("Refusing to remove a file outside the stage3 cache")
            self._safe_cache_file(path)
            path.unlink(missing_ok=True)

    def discover(self, mirror: str, variant: str) -> Stage3Archive:
        mirrors = [mirror, *(m for m in self.fallback_mirrors if m != mirror)]
        error: Stage3Error | None = None
        attempts = ATTEMPTS_SINGLE_MIRROR if len(mirrors) == 1 else ATTEMPTS_PER_MIRROR
        for candidate in mirrors:
            try:
                text = self._fetch_with_retry([latest_index_url(candidate, variant)], "the stage3 index", attempts=attempts).decode("utf-8")
                if text.lstrip().startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
                    text = self._verified_text(text)
                return parse_latest_index(text, candidate, variant)
            except Stage3Error as exc:
                error = exc
                if candidate != mirrors[-1]:
                    self.notify(f"Mirror {candidate} did not work ({exc}); trying the next one")
        raise error or Stage3Error("No stage3 mirror is configured")

    # ----- signatures -------------------------------------------------------------------------

    def _prepare_keyring(self) -> None:
        if self._binary_keyring is not None:
            return
        if not self.keyring.is_file():
            raise Stage3Error(f"Gentoo release keyring is missing: {self.keyring}")
        binary_keyring = self.cache_dir / "gentoo-release.gpg"
        self._safe_cache_file(binary_keyring)
        binary_keyring.unlink(missing_ok=True)
        self.runner.run(("gpg", "--batch", "--dearmor", "--output", str(binary_keyring), str(self.keyring)))
        listing = self.runner.run(("gpg", "--batch", "--show-keys", "--with-colons", str(self.keyring))).stdout
        allowed = release_signing_fingerprints(listing)
        if not allowed:
            raise Stage3Error("The Gentoo release keyring contains no Release Engineering signing key")
        self._binary_keyring, self._allowed_signers = binary_keyring, allowed

    def _verify_signed(self, signed: Path, plaintext: Path) -> None:
        """Verify a clearsigned file twice (gpgv, then gpg --decrypt to extract the signed text)."""
        assert self._binary_keyring is not None
        keyring = str(self._binary_keyring)
        gnupg_home = self.cache_dir / "gnupg-verify"
        self._safe_cache_dir(gnupg_home)
        gnupg_home.mkdir(mode=0o700, exist_ok=True)
        status = self.runner.run(("gpgv", "--status-fd", "1", "--keyring", keyring, str(signed))).stdout
        self._check_signature_status(status)
        status = self.runner.run(("gpg", "--batch", "--status-fd", "1", "--homedir", str(gnupg_home), "--no-default-keyring",
                                  "--keyring", keyring, "--decrypt", "--output", str(plaintext), str(signed))).stdout
        self._check_signature_status(status)

    def _verified_text(self, clearsigned: str) -> str:
        self._secure_cache_dir()
        self._prepare_keyring()
        path = self.cache_dir / "latest-index.asc"
        plaintext = self.cache_dir / "latest-index.verified"
        for item in (path, plaintext):
            self._safe_cache_file(item)
            item.unlink(missing_ok=True)
        path.write_text(clearsigned, encoding="utf-8")
        path.chmod(0o600)
        try:
            self._verify_signed(path, plaintext)
            return plaintext.read_text(encoding="utf-8")
        finally:
            path.unlink(missing_ok=True)
            plaintext.unlink(missing_ok=True)

    def _check_signature_status(self, status: str) -> None:
        """Accept only a good, current signature by a Release Engineering key.

        gpgv and gpg exit with success for some bad cases (an expired or revoked key), so the
        machine-readable status lines decide.
        """
        good = valid = 0
        for line in status.splitlines():
            if not line.startswith("[GNUPG:] "):
                continue
            fields = line[len("[GNUPG:] "):].split()
            if not fields:
                continue
            keyword = fields[0]
            if keyword in BAD_SIGNATURE_STATUSES:
                raise Stage3Error(f"The stage3 checksums are not trustworthy: {BAD_SIGNATURE_STATUSES[keyword]}")
            if keyword == "GOODSIG":
                good += 1
            elif keyword == "VALIDSIG":
                valid += 1
                if len(fields) < 11 or not fields[3].isdigit():
                    raise Stage3Error("The stage3 signature status is malformed")
                if fields[-1] not in self._allowed_signers:
                    raise Stage3Error("The stage3 checksums were not signed by a Gentoo Release Engineering key")
                signed_at, now = int(fields[3]), self.now()
                if now - signed_at > MAX_SIGNATURE_AGE:
                    raise Stage3Error("The stage3 signature is more than 60 days old: the mirror is serving an outdated image or this computer's clock is wrong")
                if signed_at - now > MAX_SIGNATURE_FUTURE:
                    raise Stage3Error("The stage3 signature is dated in the future: this computer's clock is wrong")
        if good != 1 or valid != 1:
            raise Stage3Error("The stage3 checksums do not carry exactly one valid Gentoo signature")

    # ----- network ---------------------------------------------------------------------------------

    def _open(self, request: urllib.request.Request, timeout: int):
        if self.proxy:
            return build_opener(self.proxy).open(request, timeout=timeout)
        return urllib.request.urlopen(request, timeout=timeout)

    def _source_urls(self, archive: Stage3Archive, suffix: str, primary: str) -> list[str]:
        """The same file on the primary and on the fallback mirrors."""
        urls = [primary]
        for mirror in self.fallback_mirrors:
            url = mirror.rstrip("/") + "/releases/amd64/autobuilds/" + archive.path + suffix
            if url not in urls:
                urls.append(url)
        return urls

    def _backoff(self, attempt: int) -> None:
        delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
        if self.cancel.wait(delay):
            raise InstallCancelled("Installation cancelled")

    def _retry(self, operation: Callable[[str], T], urls: Sequence[str], what: str, *, attempts: int | None = None) -> T:
        """Run ``operation(url)`` with bounded retries, then on the next mirror."""
        per_mirror = attempts or (ATTEMPTS_SINGLE_MIRROR if len(urls) == 1 else ATTEMPTS_PER_MIRROR)
        error: Stage3Error | None = None
        for url in urls:
            for attempt in range(1, per_mirror + 1):
                self._check_cancel()
                try:
                    return operation(url)
                except Stage3Transient as exc:
                    error = exc
                    if attempt < per_mirror:
                        self.notify(f"Connection interrupted while fetching {what}; retrying ({attempt + 1}/{per_mirror})")
                        self._backoff(attempt)
                except Stage3Error as exc:  # permanent for this mirror (HTTP 4xx, redirect away from HTTPS)
                    error = exc
                    break
            if url != urls[-1]:
                self.notify(f"Switching to the next mirror for {what}")
        assert error is not None
        raise error

    def _fetch_with_retry(self, urls: Sequence[str], what: str, *, attempts: int | None = None) -> bytes:
        return self._retry(self._fetch_bytes, urls, what, attempts=attempts)

    def _fetch_bytes(self, url: str) -> bytes:
        self._check_cancel()
        request = urllib.request.Request(url, headers={"User-Agent": "gentoo-live-installer/0.1"})
        try:
            with self._open(request, 60) as response:
                if response.status != 200 or response.geturl() and urlsplit_scheme(response.geturl()) != "https":
                    raise Stage3Error(f"Unauthenticated response for {url}")
                return response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, url) from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise Stage3Transient(f"Unable to download {url}: {exc}") from exc

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError, url: str) -> Stage3Error:
        message = f"Unable to download {url}: HTTP {exc.code}"
        return Stage3Transient(message) if exc.code >= 500 or exc.code in {408, 425, 429} else Stage3Error(message)

    def _cached_archive_is_valid(self, path: Path, archive: Stage3Archive, algorithm: str, expected: str) -> bool:
        if not path.is_file() or (archive.size is not None and path.stat().st_size != archive.size):
            return False
        if self._hash(path, algorithm) == expected:
            self.notify("Using the stage3 archive that was already downloaded and verified")
            return True
        path.unlink(missing_ok=True)
        return False

    def _download_with_retry(self, archive: Stage3Archive, destination: Path, progress: Progress | None) -> None:
        urls = self._source_urls(archive, "", archive.url)
        self._expected_size = archive.size
        self._retry(lambda url: self._download(url, destination, progress), urls, "the stage3 archive")

    def _download(self, url: str, destination: Path, progress: Progress | None = None) -> None:
        """One download attempt: resumes a partial file and raises Stage3Truncated on a short body."""
        expected_size = self._expected_size
        self._secure_cache_dir()
        if destination.parent != self.cache_dir:
            raise Stage3Error("Download destination must be inside the validated cache")
        partial = destination.with_name(destination.name + ".part")
        self._safe_cache_file(destination)
        self._safe_cache_file(partial)
        offset = partial.stat().st_size if partial.exists() else 0
        if expected_size is not None and offset > expected_size:
            partial.unlink()
            offset = 0
        headers = {"User-Agent": "gentoo-live-installer/0.1"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            with self._open(request, 120) as response:
                if response.geturl() and urlsplit_scheme(response.geturl()) != "https":
                    raise Stage3Error("Stage3 download redirected away from HTTPS")
                if offset and response.status != 206:
                    offset = 0
                    mode = "wb"
                elif response.status in {200, 206}:
                    mode = "ab" if offset else "wb"
                else:
                    raise Stage3Error(f"Unexpected HTTP status {response.status} for stage3 download")
                total = self._total_size(response, offset, expected_size)
                with partial.open(mode) as output:
                    while True:
                        self._check_cancel()
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        offset += len(chunk)
                        if progress:
                            progress(offset, total)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and partial.exists():
                if expected_size is not None and partial.stat().st_size == expected_size:
                    partial.replace(destination)
                    return
                partial.unlink()
                raise Stage3Transient("The partial download was out of range; starting again") from exc
            raise self._http_error(exc, url) from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise Stage3Transient(f"Stage3 download failed: {exc}") from exc
        if total is not None and offset != total:
            raise Stage3Truncated(f"The connection closed after {offset} of {total} bytes")
        partial.replace(destination)

    @staticmethod
    def _total_size(response: object, offset: int, expected_size: int | None) -> int | None:
        """The full size of the file: from Content-Range, else Content-Length, else the index."""
        headers = getattr(response, "headers", {})
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", str(headers.get("Content-Range", "")))
        if match:
            return int(match.group(3))
        length = str(headers.get("Content-Length", ""))
        if length.isdigit():
            return offset + int(length)
        return expected_size

    def _secure_cache_dir(self) -> None:
        # Trust every parent before creating or opening its child. A private
        # leaf is insufficient: an untrusted parent owner can rename it and
        # replace the entire cache after a file's lstat check. Root-owned
        # sticky directories are safe because their other users cannot rename
        # an installer-owned child (the normal /tmp and /var/tmp case).
        if not self.cache_dir.is_absolute() or self.cache_dir == Path("/") or ".." in self.cache_dir.parts:
            raise Stage3Error("Stage3 cache must be an absolute non-root path without '..'")
        current = Path(self.cache_dir.anchor)
        for component in self.cache_dir.parts[1:]:
            metadata = current.lstat()
            writable = bool(metadata.st_mode & 0o022)
            protected_sticky = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
            if (not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid not in {0, os.geteuid()}
                    or (writable and not protected_sticky)):
                raise Stage3Error(f"Unsafe stage3 cache ancestor: {current}")
            current = current / component
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
        self._safe_cache_dir(self.cache_dir)

    @staticmethod
    def _safe_cache_dir(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise Stage3Error(f"Stage3 cache directory is not private and owned by the installer: {path}")

    @staticmethod
    def _safe_cache_file(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise Stage3Error(f"Unsafe preexisting stage3 cache file: {path}")

    def _hash(self, path: Path, algorithm: str) -> str:
        digest = hashlib.new(algorithm)
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self._check_cancel()
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def unexpected_root_entries(root: Path, allowed: Iterable[str] = ()) -> list[str]:
        """Entries already in the target root that are neither lost+found nor a mount point of the layout."""
        permitted = {"lost+found", *(item.strip("/").split("/")[0] for item in allowed if item.strip("/"))}
        try:
            return sorted(entry.name for entry in root.iterdir() if entry.name not in permitted)
        except OSError:
            return []

    def extract(self, archive: Path, target_root: Path) -> None:
        root = target_root.resolve()
        root.mkdir(parents=True, exist_ok=True)

        def checked(member: tarfile.TarInfo, _path: str) -> tarfile.TarInfo | None:
            self._check_cancel()  # unpacking takes minutes on a slow disk: Cancel is looked at between members
            return self._checked_member(member, root)

        try:
            with tarfile.open(archive, "r:*") as tar:
                members: list[tarfile.TarInfo] = []
                for member in tar:  # the first pass over the whole archive (every header is read)
                    self._check_cancel()
                    self._checked_member(member, root)
                    members.append(member)
                # The data filter changes ownership and strips SUID bits, which makes
                # a Gentoo stage3 unusable. Preserve archive metadata after checking
                # each destination and link against the mounted target root.
                tar.extractall(root, members=members, filter=checked, numeric_owner=True)
                self._restore_extended_attributes(members, root)
        except (tarfile.TarError, OSError) as exc:
            raise Stage3Error(f"Safe stage3 extraction failed: {exc}") from exc

    def _restore_extended_attributes(self, members: Sequence[tarfile.TarInfo], root: Path) -> None:
        """CPython's tarfile ignores PAX xattr records; apply them so file capabilities survive."""
        for member in members:
            for key, value in member.pax_headers.items():
                if key.startswith("SCHILY.acl."):
                    self.notify(f"WARNING: ACLs of {member.name} are not restored by this installer")
                elif key.startswith("SCHILY.xattr."):
                    name = key[len("SCHILY.xattr."):]
                    data = value if isinstance(value, bytes) else str(value).encode("utf-8", "surrogateescape")
                    try:
                        os.setxattr(root / member.name, name, data, follow_symlinks=False)
                    except OSError as exc:
                        self.notify(f"WARNING: could not restore {name} on {member.name}: {exc}")

    @staticmethod
    def _checked_member(member: tarfile.TarInfo, root: Path) -> tarfile.TarInfo | None:
        pure = PurePosixPath(member.name)
        if member.isdir() and not pure.parts:
            return None
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise Stage3Error(f"Unsafe archive path: {member.name}")
        boot_devices = {"dev/console": (5, 1), "dev/null": (1, 3)}
        if member.ischr():
            if boot_devices.get(str(pure)) != (member.devmajor, member.devminor):
                raise Stage3Error(f"Unexpected device in stage3 archive: {member.name}")
        elif member.isdev() or member.isfifo() or not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise Stage3Error(f"Special file in stage3 archive: {member.name}")
        destination = root / str(pure)
        if os.path.commonpath((str(root), str(destination.resolve(strict=False)))) != str(root):
            raise Stage3Error(f"Archive path escapes target: {member.name}")
        if member.issym() or member.islnk():
            link = PurePosixPath(member.linkname)
            combined = posixpath.normpath(str(link if link.is_absolute() or member.islnk() else pure.parent / link))
            combined = combined.lstrip("/")
            if combined == ".." or combined.startswith("../"):
                raise Stage3Error(f"Archive link escapes target: {member.name}")
            target = root / combined
            if os.path.commonpath((str(root), str(target.resolve(strict=False)))) != str(root):
                raise Stage3Error(f"Archive link escapes target: {member.name}")
            if member.issym() and link.is_absolute():
                member = copy(member)
                member.linkname = posixpath.relpath(combined or ".", str(pure.parent))
            elif member.islnk():
                member = copy(member)
                member.linkname = combined
        return member

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise InstallCancelled("Installation cancelled")
