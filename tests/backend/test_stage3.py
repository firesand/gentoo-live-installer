from __future__ import annotations

import hashlib
import http.client
import io
import socket
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd.runner import CommandResult, InstallCancelled, RunnerError
from gentoo_installerd.stage3 import (
    Stage3Archive,
    Stage3Error,
    Stage3Manager,
    Stage3Truncated,
    latest_index_url,
    parse_digests,
    parse_latest_index,
    release_signing_fingerprints,
)

from tests.backend.helpers import FakeRunner


MIRROR = "https://distfiles.gentoo.org"
VARIANT = "amd64-openrc"
PATH = "20261024T120000Z/stage3-amd64-openrc-20261024T120000Z.tar.xz"


RELEASE_KEY = "13EBBDBEDE7A12775DFDB1BABB572E0E2D182910"
RELEASE_SUBKEY = "534E4209AB49EEE1C19D96162C44695DB9F6043D"
KEY_LISTING = (
    "pub:-:4096:1:DB6B8C1F96D8BF6D:1322197257:1846022400::-:::cSC::::::23::0:\n"
    "fpr:::::::::DCD05B71EAB94199527F44ACDB6B8C1F96D8BF6D:\n"
    "uid:-::::1769354028::A683::Gentoo ebuild repository signing key (Automated Signing Key) <infrastructure@gentoo.org>::::::::::0:\n"
    "pub:-:4096:1:BB572E0E2D182910:1251236664:1846022400::-:::scSC::::::23::0:\n"
    f"fpr:::::::::{RELEASE_KEY}:\n"
    "uid:-::::1769354028::279E::Gentoo Linux Release Engineering (Automated Weekly Release Key) <releng@gentoo.org>::::::::::0:\n"
    "sub:-:2048:1:2C44695DB9F6043D:1550923090:1846022400:::::s::::::23:\n"
    f"fpr:::::::::{RELEASE_SUBKEY}:\n"
)


def signature_status(*, fingerprint: str = RELEASE_KEY, signed_at: float | None = None, extra: str = "") -> str:
    stamp = int(time.time() if signed_at is None else signed_at)
    return (f"[GNUPG:] NEWSIG\n[GNUPG:] SIG_ID abc 2026-10-18 {stamp}\n"
            f"[GNUPG:] GOODSIG 2C44695DB9F6043D Gentoo Linux Release Engineering (Automated Weekly Release Key)\n"
            f"[GNUPG:] VALIDSIG {RELEASE_SUBKEY} 2026-10-18 {stamp} 0 4 0 1 10 01 {fingerprint}\n{extra}")


class VerifiedRunner(FakeRunner):
    """Plays gpg: lists the keyring, reports a good signature and writes the signed text."""

    def __init__(self, verified: str = "", status: str | None = None) -> None:
        super().__init__()
        self.verified = verified
        self.status = signature_status() if status is None else status

    def run(self, argv, **kwargs):
        result = super().run(argv, **kwargs)
        if argv[0] == "gpg" and "--decrypt" in argv:
            Path(argv[argv.index("--output") + 1]).write_text(self.verified)
        output = result.stdout
        if argv[0] == "gpg" and "--show-keys" in argv:
            output = KEY_LISTING
        elif "--status-fd" in argv:
            output = self.status
        return CommandResult(result.argv, result.returncode, output)


class Response(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200) -> None:
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Length": str(len(data))}
        if status == 206:
            self.headers["Content-Range"] = "bytes 3-6/6"

    def geturl(self) -> str:
        return "https://distfiles.gentoo.org/archive"


class Stage3ParsingTests(unittest.TestCase):
    def test_latest_index_selects_variant_and_urls(self) -> None:
        archive = parse_latest_index(f"# comment\n{PATH} 12345\n", MIRROR, VARIANT)
        self.assertEqual(archive.size, 12345)
        self.assertTrue(archive.url.endswith(PATH))
        self.assertEqual(archive.digests_url, archive.url + ".DIGESTS")
        self.assertEqual(archive.signature_url, archive.digests_url)
        self.assertEqual(latest_index_url(MIRROR, VARIANT), MIRROR + "/releases/amd64/autobuilds/latest-stage3-amd64-openrc.txt")

    def test_index_rejects_missing_ambiguous_and_escaping_entries(self) -> None:
        for text in ("", f"{PATH}\n{PATH.replace('20261024T', '20261025T')}\n", "../stage3-amd64-openrc-evil.tar.xz\n", "https://evil.test/stage3-amd64-openrc-evil.tar.xz\n"):
            with self.subTest(text=text), self.assertRaises(Stage3Error):
                parse_latest_index(text, MIRROR, VARIANT)

    def test_digest_parser_supports_gentoo_and_bsd_formats(self) -> None:
        name = PureName = Path(PATH).name
        sha256 = "a" * 64
        sha512 = "b" * 128
        self.assertEqual(parse_digests(f"{sha256}  {name}\n{sha512}  {name}\n", name), ("sha512", sha512))
        self.assertEqual(parse_digests(f"# SHA512 HASH\n{sha512}  {name}\n# BLAKE2B HASH\n{'c' * 128}  {name}\n", name), ("sha512", sha512))
        self.assertEqual(parse_digests(f"SHA256 ({name}) = {sha256.upper()}\n", name), ("sha256", sha256))
        with self.assertRaises(Stage3Error):
            parse_digests("d41d8cd98f00b204e9800998ecf8427e  " + name, name)


class Stage3VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.tar"
        with tarfile.open(self.source, "w") as tar:
            payload = b"stage3-data"
            info = tarfile.TarInfo("etc/gentoo-release")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        self.archive = Stage3Archive(PATH, self.source.stat().st_size, MIRROR + "/" + PATH, MIRROR + "/" + PATH + ".DIGESTS", MIRROR + "/" + PATH + ".DIGESTS.asc")
        self.keyring = self.root / "keyring.asc"
        self.keyring.write_text("test keyring")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manager(self, runner: FakeRunner) -> Stage3Manager:
        return Stage3Manager(runner, self.root / "cache", self.keyring)

    def test_signature_then_authenticated_hash_then_extraction(self) -> None:
        digest = hashlib.sha512(self.source.read_bytes()).hexdigest()
        runner = VerifiedRunner(f"{digest}  {Path(PATH).name}\n")
        manager = self.manager(runner)
        manager.discover = lambda mirror, variant: self.archive  # type: ignore[method-assign]
        manager._fetch_bytes = lambda url: b"clearsigned digest bytes"  # type: ignore[method-assign]
        downloaded: list[str] = []
        def download(url: str, destination: Path, progress: object = None) -> None:
            downloaded.append(url)
            destination.write_bytes(self.source.read_bytes())
        manager._download = download  # type: ignore[method-assign]
        target = self.root / "target"
        manager.install(MIRROR, VARIANT, target)
        self.assertEqual((target / "etc/gentoo-release").read_bytes(), b"stage3-data")
        self.assertEqual(runner.commands[0][:2], ("gpg", "--batch"))
        self.assertIn("--dearmor", runner.commands[0])
        self.assertIn("--show-keys", runner.commands[1])
        verify = next(command for command in runner.commands if command[0] == "gpgv")
        self.assertEqual(verify[-1], str(manager.cache_dir / (Path(PATH).name + ".DIGESTS")))
        self.assertIn("--status-fd", verify)
        self.assertTrue(any("--decrypt" in command for command in runner.commands))
        self.assertTrue(downloaded)

    def test_bad_signature_never_downloads_or_extracts(self) -> None:
        runner = VerifiedRunner()
        runner.failures["gpgv"] = 2
        manager = self.manager(runner)
        manager.discover = lambda mirror, variant: self.archive  # type: ignore[method-assign]
        manager._fetch_bytes = lambda url: b"bad"  # type: ignore[method-assign]
        manager._download = lambda *args: self.fail("download must not happen")  # type: ignore[method-assign]
        with self.assertRaises(RunnerError):
            manager.install(MIRROR, VARIANT, self.root / "target")
        self.assertFalse((self.root / "target").exists())

    def test_hash_mismatch_is_fatal_and_archive_is_removed(self) -> None:
        runner = VerifiedRunner(f"{'0' * 128}  {Path(PATH).name}\n")
        manager = self.manager(runner)
        manager.discover = lambda mirror, variant: self.archive  # type: ignore[method-assign]
        manager._fetch_bytes = lambda url: b"clearsigned digest bytes"  # type: ignore[method-assign]
        manager._download = lambda url, destination, progress=None: destination.write_bytes(self.source.read_bytes())  # type: ignore[method-assign]
        with self.assertRaisesRegex(Stage3Error, "verification failed"):
            manager.install(MIRROR, VARIANT, self.root / "target")
        self.assertFalse((self.root / "cache" / Path(PATH).name).exists())
        self.assertFalse((self.root / "target").exists())

    def test_missing_keyring_is_fatal(self) -> None:
        self.keyring.unlink()
        runner = FakeRunner()
        manager = self.manager(runner)
        manager.discover = lambda mirror, variant: self.archive  # type: ignore[method-assign]
        manager._fetch_bytes = lambda url: b"content"  # type: ignore[method-assign]
        with self.assertRaisesRegex(Stage3Error, "keyring"):
            manager.install(MIRROR, VARIANT, self.root / "target")
        self.assertEqual(runner.commands, [])

    def test_untrusted_cache_directory_is_rejected_before_writing(self) -> None:
        cache = self.root / "cache"
        cache.mkdir(mode=0o777)
        cache.chmod(0o777)
        victim = self.root / "victim"
        victim.write_bytes(b"unchanged")
        (cache / (Path(PATH).name + ".DIGESTS")).symlink_to(victim)
        manager = self.manager(FakeRunner())
        manager.discover = lambda mirror, variant: self.archive  # type: ignore[method-assign]
        manager._fetch_bytes = lambda url: b"digest"  # type: ignore[method-assign]
        with self.assertRaisesRegex(Stage3Error, "not private"):
            manager.install(MIRROR, VARIANT, self.root / "target")
        self.assertEqual(victim.read_bytes(), b"unchanged")

    def test_symlinked_cache_file_and_partial_are_rejected(self) -> None:
        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        victim = self.root / "victim"
        victim.write_bytes(b"unchanged")
        manager = self.manager(FakeRunner())
        manager.discover = lambda mirror, variant: self.archive  # type: ignore[method-assign]
        manager._fetch_bytes = lambda url: b"digest"  # type: ignore[method-assign]
        (cache / (Path(PATH).name + ".DIGESTS")).symlink_to(victim)
        with self.assertRaisesRegex(Stage3Error, "Unsafe preexisting"):
            manager.install(MIRROR, VARIANT, self.root / "target")
        (cache / (Path(PATH).name + ".DIGESTS")).unlink()
        partial = cache / (Path(PATH).name + ".part")
        partial.symlink_to(victim)
        with self.assertRaisesRegex(Stage3Error, "Unsafe preexisting"):
            manager._download("https://distfiles.gentoo.org/archive", cache / Path(PATH).name)
        self.assertEqual(victim.read_bytes(), b"unchanged")

    def test_writable_ancestor_rejected_before_controlled_cache_swap(self) -> None:
        parent = self.root / "untrusted-parent"
        parent.mkdir()
        parent.chmod(0o777)
        cache = parent / "cache"
        cache.mkdir(mode=0o700)
        replacement = parent / "replacement"
        replacement.mkdir()
        victim = self.root / "victim"
        victim.write_bytes(b"unchanged")
        (replacement / (Path(PATH).name + ".DIGESTS")).symlink_to(victim)
        manager = Stage3Manager(FakeRunner(), cache, self.keyring)
        manager.discover = lambda mirror, variant: self.archive
        manager._fetch_bytes = lambda url: b"unverified bytes"
        check_file = manager._safe_cache_file

        def swap_after_file_check(path):
            check_file(path)
            if path.name.endswith(".verified"):
                cache.rename(parent / "previous-cache")
                replacement.rename(cache)

        with patch.object(manager, "_safe_cache_file", side_effect=swap_after_file_check) as check:
            with self.assertRaisesRegex(Stage3Error, "Unsafe stage3 cache ancestor"):
                manager.install(MIRROR, VARIANT, self.root / "target")
        check.assert_not_called()
        self.assertEqual(victim.read_bytes(), b"unchanged")

    def test_symlink_ancestor_rejected_before_creating_descendants(self) -> None:
        destination = self.root / "outside"
        destination.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(destination, target_is_directory=True)
        manager = Stage3Manager(FakeRunner(), alias / "new-parent/cache", self.keyring)
        with self.assertRaisesRegex(Stage3Error, "Unsafe stage3 cache ancestor"):
            manager._secure_cache_dir()
        self.assertEqual(list(destination.iterdir()), [])

    def test_untrusted_ancestor_owner_rejected(self) -> None:
        import os
        import types
        parent = self.root / "owned-by-another-user"
        parent.mkdir(mode=0o700)
        manager = Stage3Manager(FakeRunner(), parent / "cache", self.keyring)
        real_lstat = Path.lstat

        def lstat(path, *args, **kwargs):
            metadata = real_lstat(path, *args, **kwargs)
            if path == parent:
                return types.SimpleNamespace(st_mode=metadata.st_mode, st_uid=os.geteuid() + 123456)
            return metadata

        with patch.object(Path, "lstat", lstat):
            with self.assertRaisesRegex(Stage3Error, "Unsafe stage3 cache ancestor"):
                manager._secure_cache_dir()
        self.assertFalse((parent / "cache").exists())

    def test_secure_nested_cache_is_created(self) -> None:
        manager = Stage3Manager(FakeRunner(), self.root / "private/nested/cache", self.keyring)
        manager._secure_cache_dir()
        self.assertEqual(manager.cache_dir.stat().st_mode & 0o777, 0o700)


class ArchiveAndResumeTests(unittest.TestCase):
    def test_preserves_setuid_and_rewrites_contained_absolute_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "stage.tar"
            with tarfile.open(archive, "w") as tar:
                directory = tarfile.TarInfo("usr/bin")
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o755
                tar.addfile(directory)
                binary = tarfile.TarInfo("usr/bin/su")
                binary.mode = 0o4755
                binary.size = 2
                tar.addfile(binary, io.BytesIO(b"ok"))
                link = tarfile.TarInfo("bin")
                link.type = tarfile.SYMTYPE
                link.linkname = "/usr/bin"
                tar.addfile(link)
            target = root / "target"
            Stage3Manager(FakeRunner()).extract(archive, target)
            self.assertEqual((target / "usr/bin/su").stat().st_mode & 0o7777, 0o4755)
            self.assertEqual((target / "bin").readlink().as_posix(), "usr/bin")
            self.assertEqual((target / "bin/su").read_bytes(), b"ok")

    def test_traversal_and_escaping_links_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, link in (("../escape", ""), ("safe/link", "../../escape")):
                archive = root / (name.replace("/", "_") + ".tar")
                with tarfile.open(archive, "w") as tar:
                    info = tarfile.TarInfo(name)
                    if link:
                        info.type = tarfile.SYMTYPE
                        info.linkname = link
                        tar.addfile(info)
                    else:
                        info.size = 0
                        tar.addfile(info)
                with self.subTest(name=name), self.assertRaises(Stage3Error):
                    Stage3Manager(FakeRunner()).extract(archive, root / "target")

    def test_existing_target_symlink_cannot_redirect_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            target = root / "target"
            target.mkdir()
            (target / "etc").symlink_to(outside, target_is_directory=True)
            archive = root / "stage.tar"
            with tarfile.open(archive, "w") as tar:
                info = tarfile.TarInfo("etc/passwd")
                info.size = 3
                tar.addfile(info, io.BytesIO(b"bad"))
            with self.assertRaisesRegex(Stage3Error, "escapes target"):
                Stage3Manager(FakeRunner()).extract(archive, target)
            self.assertFalse((outside / "passwd").exists())

    def test_only_known_boot_device_nodes_are_allowed(self) -> None:
        root = Path("/tmp/stage3-check")
        allowed = tarfile.TarInfo("./dev/null")
        allowed.type = tarfile.CHRTYPE
        allowed.devmajor, allowed.devminor = 1, 3
        self.assertIsNotNone(Stage3Manager._checked_member(allowed, root))
        allowed.devminor = 4
        with self.assertRaisesRegex(Stage3Error, "Unexpected device"):
            Stage3Manager._checked_member(allowed, root)

    def test_resume_appends_only_http_206(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "stage.tar.xz"
            partial = root / "stage.tar.xz.part"
            partial.write_bytes(b"abc")
            manager = Stage3Manager(FakeRunner(), root)
            with patch("urllib.request.urlopen", return_value=Response(b"def", 206)) as opened:
                manager._download("https://distfiles.gentoo.org/archive", destination)
            self.assertEqual(destination.read_bytes(), b"abcdef")
            self.assertEqual(opened.call_args.args[0].get_header("Range"), "bytes=3-")

    def test_http_200_discards_stale_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "stage.part"
            partial.write_bytes(b"stale")
            with patch("urllib.request.urlopen", return_value=Response(b"fresh", 200)):
                Stage3Manager(FakeRunner(), root)._download("https://distfiles.gentoo.org/archive", root / "stage")
            self.assertEqual((root / "stage").read_bytes(), b"fresh")


# --- download robustness, trust chain and extraction (K10, K11, K12) ----------------------------------


def wire_response(total: int, sent: int, *, status: int = 200, content_range: str = "", url: str = "https://distfiles.gentoo.org/archive") -> http.client.HTTPResponse:
    """A real HTTP response whose connection drops after ``sent`` of ``total`` body bytes."""
    server, client = socket.socketpair()
    head = f"HTTP/1.1 {status} OK\r\nContent-Length: {total if not content_range else sent_total(content_range, total)}\r\n"
    if content_range:
        head += f"Content-Range: {content_range}\r\n"
    server.sendall((head + "\r\n").encode() + bytes(range(256)) * (sent // 256) + bytes(sent % 256))
    server.close()
    response = http.client.HTTPResponse(client)
    response.begin()
    response.url = url  # type: ignore[attr-defined]
    return response


def sent_total(content_range: str, default: int) -> int:
    first, last = content_range.split(" ")[1].split("/")[0].split("-")
    return int(last) - int(first) + 1


class DownloadRobustnessTests(unittest.TestCase):
    ARCHIVE_SIZE = 4096

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        patcher = patch("gentoo_installerd.stage3.BACKOFF_SECONDS", (0,))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = Stage3Manager(FakeRunner(), self.root / "cache", cancel=threading.Event())
        self.notes: list[str] = []
        self.manager.notify = self.notes.append
        self.manager._secure_cache_dir()
        self.destination = self.manager.cache_dir / "stage3.tar.xz"
        self.archive = Stage3Archive("2026/stage3.tar.xz", self.ARCHIVE_SIZE, "https://distfiles.gentoo.org/2026/stage3.tar.xz", "", "")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def body(self) -> bytes:
        return bytes(range(256)) * (self.ARCHIVE_SIZE // 256)

    def test_truncated_body_keeps_the_partial_file_and_the_retry_resumes_with_range(self) -> None:
        with patch("urllib.request.urlopen", side_effect=[
            wire_response(self.ARCHIVE_SIZE, 1280),
            wire_response(0, 2816, status=206, content_range=f"bytes 1280-{self.ARCHIVE_SIZE - 1}/{self.ARCHIVE_SIZE}"),
        ]) as opened:
            self.manager._download_with_retry(self.archive, self.destination, None)
        self.assertEqual(self.destination.read_bytes(), self.body())
        self.assertIsNone(opened.call_args_list[0].args[0].get_header("Range"))
        self.assertEqual(opened.call_args_list[1].args[0].get_header("Range"), "bytes=1280-")
        self.assertTrue(any("Connection interrupted" in note and "retrying (2/5)" in note for note in self.notes))
        self.assertFalse(self.destination.with_name("stage3.tar.xz.part").exists())

    def test_single_attempt_reports_truncation_and_leaves_the_partial_file(self) -> None:
        with patch("urllib.request.urlopen", return_value=wire_response(self.ARCHIVE_SIZE, 1000)):
            with self.assertRaises(Stage3Truncated):
                self.manager._download("https://distfiles.gentoo.org/2026/stage3.tar.xz", self.destination)
        self.assertEqual(self.destination.with_name("stage3.tar.xz.part").stat().st_size, 1000)
        self.assertFalse(self.destination.exists())

    def test_connection_reset_in_the_middle_of_the_body_is_retried(self) -> None:
        class Resetting(io.BytesIO):
            status = 200
            headers = {"Content-Length": "4096"}

            def geturl(self) -> str:
                return "https://distfiles.gentoo.org/a"

            def read(self, amount=-1):
                raise ConnectionResetError("reset by peer")

        with patch("urllib.request.urlopen", side_effect=[Resetting(), Response(self.body())]):
            self.manager._download_with_retry(self.archive, self.destination, None)
        self.assertEqual(self.destination.read_bytes(), self.body())

    def test_gives_up_after_five_attempts_on_a_single_mirror(self) -> None:
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("unreachable")) as opened:
            with self.assertRaisesRegex(Stage3Error, "unreachable"):
                self.manager._download_with_retry(self.archive, self.destination, None)
        self.assertEqual(opened.call_count, 5)

    def test_server_errors_are_retried_but_a_missing_file_is_not(self) -> None:
        error = urllib.error.HTTPError("https://x", 503, "unavailable", {}, None)
        with patch("urllib.request.urlopen", side_effect=[error, Response(self.body())]) as opened:
            self.manager._download_with_retry(self.archive, self.destination, None)
        self.assertEqual(opened.call_count, 2)
        self.destination.unlink()
        missing = urllib.error.HTTPError("https://x", 404, "not found", {}, None)
        with patch("urllib.request.urlopen", side_effect=missing) as opened:
            with self.assertRaisesRegex(Stage3Error, "HTTP 404"):
                self.manager._download_with_retry(self.archive, self.destination, None)
        self.assertEqual(opened.call_count, 1)

    def test_fallback_mirrors_are_used_after_two_failures_on_the_primary(self) -> None:
        self.manager.fallback_mirrors = ("https://mirror.example/gentoo",)
        with patch("urllib.request.urlopen", side_effect=[urllib.error.URLError("down"), urllib.error.URLError("down"),
                                                           Response(self.body())]) as opened:
            self.manager._download_with_retry(self.archive, self.destination, None)
        urls = [call.args[0].full_url for call in opened.call_args_list]
        self.assertEqual(urls[:2], ["https://distfiles.gentoo.org/2026/stage3.tar.xz"] * 2)
        self.assertEqual(urls[2], "https://mirror.example/gentoo/releases/amd64/autobuilds/2026/stage3.tar.xz")
        self.assertTrue(any("next mirror" in note for note in self.notes))

    def test_cancelling_during_the_backoff_stops_at_once(self) -> None:
        cancel = threading.Event()
        self.manager.cancel = cancel

        def fail_and_cancel(request, timeout):
            cancel.set()
            raise urllib.error.URLError("down")

        started = time.monotonic()
        with patch("gentoo_installerd.stage3.BACKOFF_SECONDS", (60,)), patch("urllib.request.urlopen", side_effect=fail_and_cancel):
            with self.assertRaises(InstallCancelled):
                self.manager._download_with_retry(self.archive, self.destination, None)
        self.assertLess(time.monotonic() - started, 5)

    def test_complete_partial_file_is_accepted_when_the_server_says_range_not_satisfiable(self) -> None:
        self.destination.with_name("stage3.tar.xz.part").write_bytes(self.body())
        error = urllib.error.HTTPError("https://x", 416, "range", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            self.manager._download_with_retry(self.archive, self.destination, None)
        self.assertEqual(self.destination.read_bytes(), self.body())

    def test_proxy_downloads_go_through_the_proxy_aware_opener(self) -> None:
        self.manager.proxy = "http://proxy.example:3128"
        opener = type("Opener", (), {"open": lambda self, request, timeout: Response(b"abc")})()
        with patch("gentoo_installerd.stage3.build_opener", return_value=opener) as built, \
                patch("urllib.request.urlopen", side_effect=AssertionError("must not bypass the proxy")):
            data = self.manager._fetch_bytes("https://distfiles.gentoo.org/x")
        self.assertEqual(data, b"abc")
        built.assert_called_once_with("http://proxy.example:3128")


class InstallFlowTests(unittest.TestCase):
    """install(): reuse of a verified download, and one retry of a corrupt one."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source.tar"
        with tarfile.open(source, "w") as tar:
            info = tarfile.TarInfo("etc/gentoo-release")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"abc"))
        self.payload = source.read_bytes()
        self.digest = hashlib.sha512(self.payload).hexdigest()
        self.keyring = self.root / "keyring.asc"
        self.keyring.write_text("test keyring")
        self.archive = Stage3Archive(PATH, len(self.payload), MIRROR + "/" + PATH, MIRROR + "/" + PATH + ".DIGESTS", MIRROR + "/" + PATH + ".DIGESTS")
        self.runner = VerifiedRunner(f"{self.digest}  {Path(PATH).name}\n")
        self.manager = Stage3Manager(self.runner, self.root / "cache", self.keyring)
        self.manager.discover = lambda mirror, variant: self.archive  # type: ignore[method-assign]
        self.manager._fetch_bytes = lambda url: b"clearsigned digest bytes"  # type: ignore[method-assign]
        self.notes: list[str] = []
        self.manager.notify = self.notes.append

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_verified_cached_archive_is_reused_without_any_download(self) -> None:
        self.manager._secure_cache_dir()
        (self.manager.cache_dir / Path(PATH).name).write_bytes(self.payload)
        self.manager._download = lambda *args: self.fail("no download expected")  # type: ignore[method-assign]
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network expected")):
            self.manager.install(MIRROR, VARIANT, self.root / "target")
        self.assertEqual((self.root / "target/etc/gentoo-release").read_bytes(), b"abc")
        self.assertTrue(any("already downloaded" in note for note in self.notes))

    def test_a_stale_cached_archive_is_replaced(self) -> None:
        self.manager._secure_cache_dir()
        (self.manager.cache_dir / Path(PATH).name).write_bytes(b"x" * len(self.payload))
        calls: list[str] = []

        def download(url, destination, progress=None):
            calls.append(url)
            destination.write_bytes(self.payload)

        self.manager._download = download  # type: ignore[method-assign]
        self.manager.install(MIRROR, VARIANT, self.root / "target")
        self.assertEqual(len(calls), 1)

    def test_corrupt_complete_download_is_fetched_again_once(self) -> None:
        attempts = [b"corrupted!", self.payload]

        def download(url, destination, progress=None):
            destination.write_bytes(attempts.pop(0))

        self.manager._download = download  # type: ignore[method-assign]
        self.manager.install(MIRROR, VARIANT, self.root / "target")
        self.assertEqual(attempts, [])
        self.assertTrue(any("once more" in note for note in self.notes))

    def test_two_corrupt_downloads_end_with_the_verification_error(self) -> None:
        calls: list[int] = []
        self.manager._download = lambda url, destination, progress=None: (calls.append(1), destination.write_bytes(b"bad"))  # type: ignore[method-assign]
        with self.assertRaisesRegex(Stage3Error, "verification failed"):
            self.manager.install(MIRROR, VARIANT, self.root / "target")
        self.assertEqual(len(calls), 2)

    def test_non_empty_target_root_is_reported_but_not_refused(self) -> None:
        target = self.root / "target"
        (target / "efi").mkdir(parents=True)
        (target / "lost+found").mkdir()
        (target / "old-system-file").write_text("stale")
        self.manager._download = lambda url, destination, progress=None: destination.write_bytes(self.payload)  # type: ignore[method-assign]
        self.manager.install(MIRROR, VARIANT, target, allowed_existing=["/efi"])
        warning = next(note for note in self.notes if "not empty" in note)
        self.assertIn("old-system-file", warning)
        self.assertNotIn("efi", warning.replace("not empty", ""))
        self.assertEqual((target / "etc/gentoo-release").read_bytes(), b"abc")
        self.assertEqual(Stage3Manager.unexpected_root_entries(target, ["/efi"]), ["etc", "old-system-file"])


class TrustChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.keyring = self.root / "keyring.asc"
        self.keyring.write_text("test keyring")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def check(self, status: str) -> None:
        manager = Stage3Manager(VerifiedRunner(), self.root / "cache", self.keyring)
        manager._secure_cache_dir()
        manager._prepare_keyring()
        manager._check_signature_status(status)

    def test_a_current_release_engineering_signature_is_accepted(self) -> None:
        self.check(signature_status())
        self.check(signature_status(signed_at=time.time() - 30 * 24 * 3600))

    def test_expired_revoked_and_unknown_keys_are_rejected_even_when_gpg_exits_cleanly(self) -> None:
        for keyword, fragment in (("EXPKEYSIG", "key has expired"), ("REVKEYSIG", "revoked"), ("BADSIG", "does not match"),
                                  ("EXPSIG", "signature itself has expired"), ("ERRSIG", "could not be checked"),
                                  ("NO_PUBKEY", "not in the Gentoo release keyring")):
            with self.subTest(keyword=keyword), self.assertRaisesRegex(Stage3Error, fragment):
                self.check(signature_status(extra=f"[GNUPG:] {keyword} 2C44695DB9F6043D\n"))

    def test_only_release_engineering_keys_may_sign_stage3_files(self) -> None:
        with self.assertRaisesRegex(Stage3Error, "Release Engineering"):
            self.check(signature_status(fingerprint="DCD05B71EAB94199527F44ACDB6B8C1F96D8BF6D"))

    def test_signature_age_window(self) -> None:
        with self.assertRaisesRegex(Stage3Error, "60 days old.*clock"):
            self.check(signature_status(signed_at=time.time() - 61 * 24 * 3600))
        with self.assertRaisesRegex(Stage3Error, "future.*clock"):
            self.check(signature_status(signed_at=time.time() + 3 * 24 * 3600))

    def test_exactly_one_good_signature_is_required(self) -> None:
        for status in ("", "[GNUPG:] NEWSIG\n", signature_status() + signature_status(), "[GNUPG:] VALIDSIG too short\n"):
            with self.subTest(status=status[:30]), self.assertRaises(Stage3Error):
                self.check(status)

    def test_a_clock_that_is_wrong_is_explained_by_the_error(self) -> None:
        manager = Stage3Manager(VerifiedRunner(), self.root / "cache", self.keyring)
        manager.now = lambda: time.time() + 90 * 24 * 3600
        manager._secure_cache_dir()
        manager._prepare_keyring()
        with self.assertRaisesRegex(Stage3Error, "clock is wrong"):
            manager._check_signature_status(signature_status())

    def test_only_release_engineering_keys_are_taken_from_the_keyring_listing(self) -> None:
        self.assertEqual(release_signing_fingerprints(KEY_LISTING), {RELEASE_KEY})
        revoked = KEY_LISTING.replace("pub:-:4096:1:BB572E0E2D182910", "pub:r:4096:1:BB572E0E2D182910")
        self.assertEqual(release_signing_fingerprints(revoked), set())
        self.assertEqual(release_signing_fingerprints(""), set())

    def test_a_keyring_without_release_engineering_keys_is_refused(self) -> None:
        class Plain(VerifiedRunner):
            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                return CommandResult(result.argv, 0, "" if "--show-keys" in argv else result.stdout)

        manager = Stage3Manager(Plain(), self.root / "cache", self.keyring)
        manager._secure_cache_dir()
        with self.assertRaisesRegex(Stage3Error, "no Release Engineering"):
            manager._prepare_keyring()

    def test_the_shipped_keyring_yields_the_release_engineering_keys_only(self) -> None:
        shipped = Path("/usr/share/openpgp-keys/gentoo-release.asc")
        if not shipped.is_file():
            self.skipTest("the Gentoo release keyring is not installed on this machine")
        import shutil
        import subprocess

        if not shutil.which("gpg"):
            self.skipTest("gpg is not installed")
        listing = subprocess.run(["gpg", "--batch", "--show-keys", "--with-colons", str(shipped)], capture_output=True, text=True, check=True).stdout
        fingerprints = release_signing_fingerprints(listing)
        self.assertIn(RELEASE_KEY, fingerprints)
        self.assertNotIn("DCD05B71EAB94199527F44ACDB6B8C1F96D8BF6D", fingerprints)  # ebuild repository key
        self.assertNotIn("EF9538C9E8E64311A52CDEDFA13D0EF1914E7A72", fingerprints)  # mirror CI key
        self.assertTrue(1 <= len(fingerprints) <= 3)

    def test_a_clearsigned_index_is_verified_and_the_signed_text_is_used(self) -> None:
        signed_index = ("-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n# Latest as of today\n"
                        f"{PATH} 12345\n-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----\n")
        runner = VerifiedRunner(f"{PATH} 12345\n")
        manager = Stage3Manager(runner, self.root / "cache", self.keyring)
        manager._fetch_bytes = lambda url: signed_index.encode()  # type: ignore[method-assign]
        archive = manager.discover(MIRROR, VARIANT)
        self.assertEqual(archive.size, 12345)
        self.assertTrue(any(command[0] == "gpgv" for command in runner.commands))

    def test_a_clearsigned_index_with_a_bad_signature_is_refused(self) -> None:
        runner = VerifiedRunner(f"{PATH} 12345\n", status=signature_status(extra="[GNUPG:] BADSIG 2C44695DB9F6043D x\n"))
        manager = Stage3Manager(runner, self.root / "cache", self.keyring)
        manager._fetch_bytes = lambda url: b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\nx\n"  # type: ignore[method-assign]
        with self.assertRaisesRegex(Stage3Error, "not trustworthy"):
            manager.discover(MIRROR, VARIANT)

    def test_an_unsigned_index_still_works_over_https(self) -> None:
        manager = Stage3Manager(VerifiedRunner(), self.root / "cache", self.keyring)
        manager._fetch_bytes = lambda url: f"# comment\n{PATH} 1\n".encode()  # type: ignore[method-assign]
        self.assertEqual(manager.discover(MIRROR, VARIANT).path, PATH)

    def test_the_index_falls_back_to_the_next_mirror(self) -> None:
        manager = Stage3Manager(VerifiedRunner(), self.root / "cache", self.keyring)
        manager.fallback_mirrors = ("https://mirror.example",)
        fetched: list[str] = []

        def fetch(url: str) -> bytes:
            fetched.append(url)
            if "distfiles.gentoo.org" in url:
                raise Stage3Error("HTTP 404")
            return f"{PATH} 1\n".encode()

        manager._fetch_bytes = fetch  # type: ignore[method-assign]
        archive = manager.discover(MIRROR, VARIANT)
        self.assertTrue(archive.url.startswith("https://mirror.example/"))
        self.assertEqual(len(fetched), 2)


class ExtractionHardeningTests(unittest.TestCase):
    def test_pax_xattrs_are_restored_so_file_capabilities_survive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "stage.tar"
            with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
                info = tarfile.TarInfo("usr/bin/ping")
                info.size = 2
                info.pax_headers = {"SCHILY.xattr.security.capability": "cap-bytes"}
                tar.addfile(info, io.BytesIO(b"ok"))
            applied: list[tuple[str, bytes]] = []
            manager = Stage3Manager(FakeRunner(), root)
            with patch("os.setxattr", lambda path, name, value, follow_symlinks=True: applied.append((name, value))):
                manager.extract(archive, root / "target")
            self.assertEqual(applied, [("security.capability", b"cap-bytes")])
            self.assertEqual((root / "target/usr/bin/ping").read_bytes(), b"ok")

    def test_unrestorable_attributes_only_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "stage.tar"
            with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
                info = tarfile.TarInfo("usr/bin/ping")
                info.size = 2
                info.pax_headers = {"SCHILY.xattr.security.capability": "x", "SCHILY.acl.access": "user::rw-"}
                tar.addfile(info, io.BytesIO(b"ok"))
            notes: list[str] = []
            manager = Stage3Manager(FakeRunner(), root)
            manager.notify = notes.append

            def refuse(*args, **kwargs):
                raise OSError(95, "Operation not supported")

            with patch("os.setxattr", refuse):
                manager.extract(archive, root / "target")
            self.assertTrue(any("could not restore security.capability" in note for note in notes))
            self.assertTrue(any("ACLs" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
