from __future__ import annotations

import hashlib
import os
import posixpath
import re
import stat
import tarfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from copy import copy
from pathlib import Path, PurePosixPath
from typing import Callable

from .runner import InstallCancelled, Runner


class Stage3Error(RuntimeError):
    pass


@dataclass(frozen=True)
class Stage3Archive:
    path: str
    size: int | None
    url: str
    digests_url: str
    signature_url: str


Progress = Callable[[int, int | None], None]


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


class Stage3Manager:
    def __init__(
        self,
        runner: Runner,
        cache_dir: str | Path = "/var/tmp/gentoo-installer",
        keyring: str | Path = "/usr/share/openpgp-keys/gentoo-release.asc",
        cancel: threading.Event | None = None,
    ) -> None:
        self.runner = runner
        self.cache_dir = Path(cache_dir)
        self.keyring = Path(keyring)
        self.cancel = cancel or threading.Event()

    def install(self, mirror: str, variant: str, target_root: str | Path, progress: Progress | None = None) -> Path:
        archive = self.discover(mirror, variant)
        digests = self._fetch_bytes(archive.digests_url)
        self._secure_cache_dir()
        digest_path = self.cache_dir / (Path(archive.path).name + ".DIGESTS")
        verified_path = self.cache_dir / (Path(archive.path).name + ".DIGESTS.verified")
        self._safe_cache_file(digest_path)
        self._safe_cache_file(verified_path)
        digest_path.write_bytes(digests)
        digest_path.chmod(0o600)
        if not self.keyring.is_file():
            raise Stage3Error(f"Gentoo release keyring is missing: {self.keyring}")
        binary_keyring = self.cache_dir / "gentoo-release.gpg"
        self._safe_cache_file(binary_keyring)
        binary_keyring.unlink(missing_ok=True)
        self.runner.run(("gpg", "--batch", "--dearmor", "--output", str(binary_keyring), str(self.keyring)))
        gnupg_home = self.cache_dir / "gnupg-verify"
        self._safe_cache_dir(gnupg_home)
        gnupg_home.mkdir(mode=0o700, exist_ok=True)
        verified_path.unlink(missing_ok=True)
        try:
            self.runner.run(("gpgv", "--keyring", str(binary_keyring), str(digest_path)))
            self.runner.run(("gpg", "--batch", "--homedir", str(gnupg_home), "--no-default-keyring",
                             "--keyring", str(binary_keyring), "--decrypt", "--output", str(verified_path), str(digest_path)))
            # Only parse bytes written by gpgv after successful signature verification.
            algorithm, expected = parse_digests(verified_path.read_text(encoding="utf-8"), PurePosixPath(archive.path).name)
        finally:
            verified_path.unlink(missing_ok=True)
        archive_path = self.cache_dir / PurePosixPath(archive.path).name
        self._safe_cache_file(archive_path)
        self._download(archive.url, archive_path, progress)
        actual = self._hash(archive_path, algorithm)
        if actual != expected:
            archive_path.unlink(missing_ok=True)
            raise Stage3Error(f"Authenticated {algorithm} verification failed for {archive_path.name}")
        self.extract(archive_path, Path(target_root))
        return archive_path

    def discover(self, mirror: str, variant: str) -> Stage3Archive:
        text = self._fetch_bytes(latest_index_url(mirror, variant)).decode("utf-8")
        return parse_latest_index(text, mirror, variant)

    def _fetch_bytes(self, url: str) -> bytes:
        self._check_cancel()
        request = urllib.request.Request(url, headers={"User-Agent": "gentoo-live-installer/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status != 200 or response.geturl() and urlsplit_scheme(response.geturl()) != "https":
                    raise Stage3Error(f"Unauthenticated response for {url}")
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise Stage3Error(f"Unable to download {url}: {exc}") from exc

    def _download(self, url: str, destination: Path, progress: Progress | None = None) -> None:
        self._secure_cache_dir()
        if destination.parent != self.cache_dir:
            raise Stage3Error("Download destination must be inside the validated cache")
        partial = destination.with_name(destination.name + ".part")
        self._safe_cache_file(destination)
        self._safe_cache_file(partial)
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "gentoo-live-installer/0.1"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                if response.geturl() and urlsplit_scheme(response.geturl()) != "https":
                    raise Stage3Error("Stage3 download redirected away from HTTPS")
                if offset and response.status != 206:
                    offset = 0
                    mode = "wb"
                elif response.status in {200, 206}:
                    mode = "ab" if offset else "wb"
                else:
                    raise Stage3Error(f"Unexpected HTTP status {response.status} for stage3 download")
                total_header = response.headers.get("Content-Length", "")
                total = offset + int(total_header) if total_header.isdigit() else None
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
                partial.unlink()
                return self._download(url, destination, progress)
            raise Stage3Error(f"Stage3 download failed: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise Stage3Error(f"Stage3 download failed: {exc}") from exc
        partial.replace(destination)

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

    def extract(self, archive: Path, target_root: Path) -> None:
        root = target_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(archive, "r:*") as tar:
                members = tar.getmembers()
                for member in members:
                    self._checked_member(member, root)
                # The data filter changes ownership and strips SUID bits, which makes
                # a Gentoo stage3 unusable. Preserve archive metadata after checking
                # each destination and link against the mounted target root.
                tar.extractall(root, members=members, filter=lambda member, _: self._checked_member(member, root), numeric_owner=True)
        except (tarfile.TarError, OSError) as exc:
            raise Stage3Error(f"Safe stage3 extraction failed: {exc}") from exc

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
