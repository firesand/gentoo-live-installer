from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd.runner import RunnerError
from gentoo_installerd.stage3 import Stage3Archive, Stage3Error, Stage3Manager, latest_index_url, parse_digests, parse_latest_index

from tests.backend.helpers import FakeRunner


MIRROR = "https://distfiles.gentoo.org"
VARIANT = "amd64-openrc"
PATH = "20261024T120000Z/stage3-amd64-openrc-20261024T120000Z.tar.xz"


class VerifiedRunner(FakeRunner):
    def __init__(self, verified: str = "") -> None:
        super().__init__()
        self.verified = verified

    def run(self, argv, **kwargs):
        result = super().run(argv, **kwargs)
        if argv[0] == "gpg" and "--decrypt" in argv:
            Path(argv[argv.index("--output") + 1]).write_text(self.verified)
        return result


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
        self.assertEqual(runner.commands[0][0], "gpg")
        self.assertEqual(runner.commands[1][0], "gpgv")
        self.assertEqual(runner.commands[1][-1], str(manager.cache_dir / (Path(PATH).name + ".DIGESTS")))
        self.assertIn("--decrypt", runner.commands[2])
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


if __name__ == "__main__":
    unittest.main()
