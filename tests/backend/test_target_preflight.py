from __future__ import annotations

import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd import proxy
from gentoo_installerd.configure import check_secure_boot_material, live_input_problems
from gentoo_installerd.install import Installer

from tests.backend.helpers import FakeInspector
from tests.backend.target_helpers import ScriptedRunner, configurer, target_manifest


def live_root(base: Path) -> Path:
    """A fake live system with the lists the checks consult."""
    (base / "usr/share/zoneinfo/Asia").mkdir(parents=True)
    (base / "usr/share/zoneinfo/Asia/Jakarta").write_text("tz")
    (base / "usr/share/zoneinfo/UTC").write_text("tz")
    (base / "usr/share/keymaps/i386/qwertz").mkdir(parents=True)
    for name in ("us", "de-latin1", "de-latin1-nodeadkeys"):
        (base / "usr/share/keymaps/i386/qwertz" / f"{name}.map.gz").write_bytes(b"x")
    (base / "usr/share/i18n").mkdir(parents=True)
    (base / "usr/share/i18n/SUPPORTED").write_text("# comment\nen_US.UTF-8 UTF-8\nde_DE.UTF-8 UTF-8\nde_DE@euro ISO-8859-15\n")
    (base / "usr/share/consolefonts").mkdir(parents=True)
    (base / "usr/share/consolefonts/ter-132b.psf.gz").write_bytes(b"x")
    return base


class LiveInputTests(unittest.TestCase):
    def problems(self, **sections) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            return live_input_problems(target_manifest(**sections), root=live_root(Path(temporary)))

    def test_valid_choices_pass(self) -> None:
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.problems(system={"timezone": "Asia/Jakarta", "locale": "de_DE@euro", "keymap": "de-latin1", "console_font": "ter-132b"}), [])
        self.assertEqual(self.problems(system={"keymap": "", "keyboard_layout": "de", "keyboard_variant": "nodeadkeys"}), [])

    def test_typos_stop_the_install_before_the_disk_is_touched(self) -> None:
        self.assertEqual(self.problems(system={"timezone": "Asia/Jakartaa"}), ["The time zone 'Asia/Jakartaa' does not exist on this system"])
        keymap = self.problems(system={"keymap": "de-latin"})
        self.assertEqual(len(keymap), 1)
        self.assertIn("'de-latin' does not exist (similar: de-latin1", keymap[0])
        self.assertIn("'xx_XX.UTF-8' is not available", self.problems(system={"locale": "xx_XX.UTF-8"})[0])
        self.assertEqual(self.problems(system={"console_font": "lat9w-16"}), ["The console font 'lat9w-16' is not installed"])

    def test_lists_the_live_image_lacks_are_not_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(live_input_problems(target_manifest(system={"timezone": "Mars/Olympus", "keymap": "nope", "locale": "zz_ZZ"}), root=temporary), [])

    def test_c_locales_are_always_acceptable(self) -> None:
        self.assertEqual(self.problems(system={"locale": "C.UTF-8"}), [])

    def test_secure_boot_material_is_checked_with_the_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            key, certificate = base / "k.pem", base / "c.pem"
            key.write_text("key")
            certificate.write_text("cert")
            m = target_manifest(system={"secure_boot": True, "secure_boot_key": str(key), "secure_boot_certificate": str(certificate),
                                        "bootloader": "systemd-boot", "uki": True})
            runner = ScriptedRunner()
            runner.respond("openssl", "SAME")
            self.assertEqual(live_input_problems(m, root=live_root(base / "live"), runner=runner), [])
            self.assertEqual(check_secure_boot_material(runner, m.system), [])
            mismatched = ScriptedRunner()
            mismatched.respond("openssl x509", "A")
            mismatched.respond("openssl pkey", "B")
            self.assertEqual(check_secure_boot_material(mismatched, m.system), ["The Secure Boot certificate does not belong to the signing key"])
            key.unlink()
            self.assertIn("signing key is missing", live_input_problems(m, root=live_root(base / "live2"), runner=runner)[0])


class ClockSkewTests(unittest.TestCase):
    class Opener:
        def __init__(self, date: str | None, error: Exception | None = None) -> None:
            self.date, self.error = date, error

        def open(self, request, timeout):
            if self.error:
                raise self.error
            opener = self

            class Response:
                headers = {"Date": opener.date} if opener.date else {}

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *args):
                    return False

            self.request = request
            return Response()

    def skew(self, opener, now=lambda: 1_792_800_000.0):
        with patch("gentoo_installerd.proxy.build_opener", return_value=opener) as built:
            self.built = built
            return proxy.mirror_clock_skew("https://distfiles.gentoo.org", "http://proxy.example:3128", now=now)

    def test_skew_is_the_difference_to_the_servers_date_header(self) -> None:
        opener = self.Opener("Sun, 25 Oct 2026 00:00:00 GMT")  # 1792886400
        self.assertEqual(self.skew(opener, now=lambda: 1_792_886_400.0 + 3600), 3600)
        self.assertEqual(self.skew(opener, now=lambda: 1_792_886_400.0 - 90 * 24 * 3600), -90 * 24 * 3600)
        self.assertEqual(opener.request.get_method(), "HEAD")
        self.built.assert_called_with("http://proxy.example:3128")

    def test_unknown_when_the_mirror_cannot_be_asked(self) -> None:
        self.assertIsNone(self.skew(self.Opener(None)))
        self.assertIsNone(self.skew(self.Opener("not a date")))
        self.assertIsNone(self.skew(self.Opener("x", urllib.error.URLError("unreachable"))))
        self.assertIsNone(self.skew(self.Opener("x", TimeoutError())))


class ProxyHelperTests(unittest.TestCase):
    def test_environment_covers_the_tools_portage_uses(self) -> None:
        environment = proxy.proxy_environment("http://user:secret@proxy.example:3128")
        for name in ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            self.assertEqual(environment[name], "http://user:secret@proxy.example:3128")
        self.assertEqual(environment["no_proxy"], "localhost,127.0.0.1,::1")
        self.assertEqual(environment["RSYNC_PROXY"], "proxy.example:3128")
        self.assertEqual(proxy.proxy_environment("https://proxy.example")["RSYNC_PROXY"], "proxy.example:443")
        self.assertEqual(proxy.proxy_environment("http://proxy.example")["RSYNC_PROXY"], "proxy.example:80")
        self.assertEqual(proxy.proxy_environment(""), {})

    def test_credentials_are_never_kept_or_shown(self) -> None:
        self.assertEqual(proxy.make_conf_settings("http://proxy.example:3128")["https_proxy"], "http://proxy.example:3128")
        self.assertEqual(proxy.make_conf_settings("http://user:secret@proxy.example:3128"), {})
        self.assertEqual(proxy.make_conf_settings(""), {})
        self.assertTrue(proxy.has_credentials("http://user@proxy.example"))
        self.assertFalse(proxy.has_credentials("http://proxy.example"))
        self.assertEqual(proxy.redact_proxy("http://user:secret@proxy.example:3128"), "http://user:<redacted>@proxy.example:3128")
        self.assertEqual(proxy.redact_proxy("http://proxy.example:3128"), "http://proxy.example:3128")
        self.assertEqual(proxy.redact_proxy(""), "")

    def test_opener_uses_the_proxy_for_https(self) -> None:
        opener = proxy.build_opener("http://proxy.example:3128")
        handler = next(handler for handler in opener.handlers if handler.__class__.__name__ == "ProxyHandler")
        self.assertEqual(handler.proxies["https"], "http://proxy.example:3128")


class WorldDriftTests(unittest.TestCase):
    OUTPUT = """
[ebuild   R    ] dev-qt/qtpositioning-6.11.2  USE="geoclue*"
[ebuild     U  ] sys-apps/portage-3.0.80 [3.0.79]
[ebuild  N     ] media-libs/new-1.0
[ebuild   rR   ] x11-libs/cairo-1.18.4 USE="X*"
[ebuild  NS    ] sys-libs/slot-2
[ebuild   R  U ] dev-libs/both-3 [2]
"""

    def test_only_use_rebuilds_count_as_drift(self) -> None:
        runner = ScriptedRunner()
        runner.respond("chroot", self.OUTPUT)
        with tempfile.TemporaryDirectory() as temporary:
            drift = configurer(temporary, runner).world_drift(target_manifest())
        self.assertEqual(drift, ["dev-qt/qtpositioning-6.11.2", "x11-libs/cairo-1.18.4"])
        command = runner.commands[0]
        for flag in ("--pretend", "--update", "--deep", "--newuse", "@world"):
            self.assertIn(flag, command)

    def test_failures_are_ignored(self) -> None:
        runner = ScriptedRunner()
        runner.script("chroot", ("fail", "boom"))
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(configurer(temporary, runner).world_drift(target_manifest()), [])

    def test_source_installs_skip_the_drift_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = ScriptedRunner()
            installer = Installer(target_manifest(system={"desktop": "minimal"}, portage={"binhost": False},
                                                  options={"pipewire": False, "bluetooth": False, "printing": False}),
                                  runner=runner, target_root=Path(temporary, "t"), state_dir=Path(temporary, "s"), dry_run=True,
                                  inspector=FakeInspector(), emit=lambda event: None)
            installer._desktop()
            self.assertFalse(any("--newuse" in command for command in runner.commands))

    def test_the_desktop_stage_logs_the_drift_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = ScriptedRunner()
            runner.respond("*--newuse", self.OUTPUT)
            events: list = []
            installer = Installer(target_manifest(system={"desktop": "minimal"}, options={"pipewire": False, "bluetooth": False, "printing": False}),
                                  runner=runner, target_root=Path(temporary, "t"), state_dir=Path(temporary, "s"), dry_run=True,
                                  inspector=FakeInspector(), emit=events.append)
            installer._desktop()
            messages = [event.message for event in events if event.type == "log"]
            self.assertTrue(any("2 installed package(s) differ from the configured USE flags" in message for message in messages), messages)


if __name__ == "__main__":
    unittest.main()
