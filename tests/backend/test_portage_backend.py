"""PORT-03 to PORT-07, PORT-11 and PORT-12: what the daemon writes and runs for the Portage choices."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gentoo_installer.binhost import GENERIC_URL, V3_URL, binhost_url, choose_variant
from gentoo_installer.manifest import InstallManifest
from gentoo_installer.mirrors import mirror_list
from gentoo_installer.world import should_depclean, should_update_world, stage_profile, world_update_reason
from gentoo_installerd.configure import render_make_conf
from gentoo_installerd.news import news_summary, parse_news_list
from gentoo_installerd.configure_portage import BINHOST_OFF_MARKER, render_repos_conf

from tests.backend.target_helpers import FakePlan, ScriptedRunner, configurer, read, target_manifest
from tests.backend.test_target_install import StageHarness

HARDWARE_V3 = {"cpu": {"x86_64_level": 3, "cpu_flags_x86": ["aes", "avx", "avx2", "sse4_2"]}}
HARDWARE_V2 = {"cpu": {"x86_64_level": 2, "cpu_flags_x86": ["sse4_2"]}}


class BinaryPackageHostTests(unittest.TestCase):
    def test_binrepos_conf_is_the_handbook_file_with_signatures_and_replaces_the_stage3s(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configurer(root).configure_portage(target_manifest())
            text = read(root, "etc/portage/binrepos.conf/gentoobinhost.conf")
            self.assertEqual(text, "[gentoobinhost]\npriority = 9999\nlocation = /var/cache/binhost/gentoobinhost\n"
                                   f"sync-uri = {GENERIC_URL}\nverify-signature = true\n")
            self.assertEqual([p.name for p in (root / "etc/portage/binrepos.conf").iterdir()], ["gentoobinhost.conf"])  # nothing doubled up

    def test_make_conf_has_no_portage_binhost_but_requires_signatures(self) -> None:
        text = render_make_conf(target_manifest())
        self.assertNotIn("PORTAGE_BINHOST", text)
        self.assertIn("FEATURES='getbinpkg binpkg-request-signature'", text)

    def test_the_v3_packages_are_chosen_only_when_detection_says_so(self) -> None:
        manifest = target_manifest().portage
        self.assertEqual(binhost_url(manifest, 3)[0], V3_URL)
        self.assertEqual(binhost_url(manifest, 4)[0], V3_URL)
        self.assertEqual(binhost_url(manifest, 2)[0], GENERIC_URL)
        self.assertEqual(binhost_url(manifest, 0)[0], GENERIC_URL)  # nothing detected
        self.assertEqual(binhost_url(manifest, 3, portable=True)[0], GENERIC_URL)
        manifest.binhost_variant = "generic"
        self.assertEqual(binhost_url(manifest, 3)[0], GENERIC_URL)
        manifest.binhost_variant = "v3"
        url, note = binhost_url(manifest, 2)
        self.assertEqual(url, GENERIC_URL)
        self.assertIn("x86-64-v2", note)
        self.assertEqual(choose_variant("v3", 3, True)[0], "generic")

    def test_the_variant_reaches_binrepos_conf_from_the_detected_processor(self) -> None:
        for hardware, expected in ((HARDWARE_V3, V3_URL), (HARDWARE_V2, GENERIC_URL), (None, GENERIC_URL)):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                configurer(root).configure_portage(target_manifest(), hardware=hardware)
                self.assertIn(f"sync-uri = {expected}\n", read(root, "etc/portage/binrepos.conf/gentoobinhost.conf"))

    def test_a_portable_installation_always_gets_the_generic_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configurer(root).configure_portage(target_manifest(hardware={"target": "portable"}), hardware=HARDWARE_V3)
            self.assertIn(f"sync-uri = {GENERIC_URL}\n", read(root, "etc/portage/binrepos.conf/gentoobinhost.conf"))

    def test_a_host_typed_by_hand_is_used_as_it_is(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            notes = configurer(root).configure_portage(target_manifest(portage={"binhost_url": "https://binhost.example/x86-64/"}), hardware=HARDWARE_V3)
            self.assertIn("sync-uri = https://binhost.example/x86-64/\n", read(root, "etc/portage/binrepos.conf/gentoobinhost.conf"))
            self.assertEqual(notes, [])

    def test_no_binhost_writes_no_binrepos_and_no_getbinpkg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            c = configurer(root)
            c.configure_portage(target_manifest(portage={"binhost": False}))
            self.assertFalse((root / "etc/portage/binrepos.conf").exists())
            self.assertNotIn("getbinpkg", read(root, "etc/portage/make.conf"))
            self.assertEqual(c.prepare_binhost_trust(target_manifest(portage={"binhost": False})), "")
            self.assertEqual(c.runner.lines(), [])  # getuto is not run without the host

    def test_getuto_runs_in_the_chroot_before_the_first_emerge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = ScriptedRunner()
            c = configurer(temporary, runner)
            c.configure_portage(target_manifest())
            self.assertEqual(c.prepare_binhost_trust(target_manifest()), "")
            self.assertEqual(runner.lines(), [f"chroot {temporary} getuto"])

    def test_a_failing_getuto_falls_back_to_source_builds_and_never_accepts_unsigned_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = ScriptedRunner()
            runner.script("chroot", ("fail", "gpg: keyserver down"), ("fail", "gpg: keyserver down"))
            c = configurer(root, runner)
            manifest = target_manifest()
            c.configure_portage(manifest)
            warning = c.prepare_binhost_trust(manifest)
            self.assertIn("built from source", warning)
            self.assertIn("keyserver down", warning)
            make_conf = read(root, "etc/portage/make.conf")
            self.assertNotIn("getbinpkg", make_conf.replace("binpkg-request-signature", ""))  # the host is off
            self.assertIn("binpkg-request-signature", make_conf)  # and a signature is still required
            self.assertTrue((root / BINHOST_OFF_MARKER).is_file())
            # the next render (the portage stage runs again after an interruption) stays off, and getuto is not retried
            c.configure_portage(manifest)
            self.assertNotIn("getbinpkg", read(root, "etc/portage/make.conf").replace("binpkg-request-signature", ""))
            self.assertEqual(c.prepare_binhost_trust(manifest), "")
            self.assertEqual(len([line for line in runner.lines() if line.endswith("getuto")]), 2)  # one try and one retry

    def test_a_getbinpkg_typed_into_features_is_dropped_when_the_host_is_off(self) -> None:
        manifest = target_manifest(portage={"features": ["ccache", "getbinpkg"]})
        self.assertIn("FEATURES='ccache getbinpkg binpkg-request-signature'", render_make_conf(manifest).splitlines())
        off = render_make_conf(manifest, binhost=False)
        self.assertNotIn("getbinpkg", off.replace("binpkg-request-signature", ""))
        self.assertIn("ccache", off)

    def test_a_second_try_of_getuto_can_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = ScriptedRunner()
            runner.script("chroot", ("fail", "temporary failure"))
            c = configurer(temporary, runner)
            self.assertEqual(c.prepare_binhost_trust(target_manifest()), "")
            self.assertFalse((Path(temporary) / BINHOST_OFF_MARKER).exists())


class OptimizeTests(unittest.TestCase):
    def test_generic_is_the_default_and_adds_nothing(self) -> None:
        text = render_make_conf(target_manifest())
        self.assertIn("COMMON_FLAGS='-O2 -pipe'", text.splitlines())
        self.assertNotIn("native", text)
        self.assertNotIn("RUSTFLAGS", text)

    def test_native_tunes_cflags_and_rustflags_and_moves_cpu_flags_to_package_use(self) -> None:
        manifest = target_manifest(portage={"optimize": "native"})
        plan = FakePlan(make_conf={"CPU_FLAGS_X86": "aes avx2"})
        text = render_make_conf(manifest, plan)
        self.assertIn("COMMON_FLAGS='-O2 -pipe -march=native'", text.splitlines())
        self.assertIn("RUSTFLAGS='-C target-cpu=native'", text.splitlines())
        self.assertNotIn("CPU_FLAGS_X86", text)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configurer(root).configure_portage(manifest, plan)
            self.assertEqual(read(root, "etc/portage/package.use/00cpu-flags"), "*/* CPU_FLAGS_X86: aes avx2\n")
            self.assertNotIn("CPU_FLAGS_X86", read(root, "etc/portage/make.conf"))

    def test_native_with_the_binary_host_still_reads_the_flags_from_the_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configurer(root).configure_portage(target_manifest(portage={"optimize": "native"}), hardware=HARDWARE_V3)
            self.assertEqual(read(root, "etc/portage/package.use/00cpu-flags"), "*/* CPU_FLAGS_X86: aes avx avx2 sse4_2\n")

    def test_typed_cpu_flags_win_and_native_without_any_flags_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configurer(root).configure_portage(target_manifest(portage={"optimize": "native", "cpu_flags": "aes sse4_2"}), hardware=HARDWARE_V3)
            self.assertEqual(read(root, "etc/portage/package.use/00cpu-flags"), "*/* CPU_FLAGS_X86: aes sse4_2\n")
        with tempfile.TemporaryDirectory() as temporary:
            notes = configurer(temporary).configure_portage(target_manifest(portage={"optimize": "native"}))
            self.assertTrue(notes and "CPU flags" in notes[0])
            self.assertFalse((Path(temporary) / "etc/portage/package.use/00cpu-flags").exists())

    def test_an_explicit_march_is_kept(self) -> None:
        text = render_make_conf(target_manifest(portage={"optimize": "native", "cflags": "-O2 -pipe -march=znver4", "rustflags": "-C target-cpu=znver4"}))
        self.assertIn("COMMON_FLAGS='-O2 -pipe -march=znver4'", text.splitlines())
        self.assertIn("RUSTFLAGS='-C target-cpu=znver4'", text.splitlines())

    def test_portable_with_native_is_rejected_and_never_rendered(self) -> None:
        manifest = target_manifest(portage={"optimize": "native"}, hardware={"target": "portable"})
        self.assertTrue(any("portable" in error for error in manifest.validate()))
        self.assertNotIn("native", render_make_conf(manifest))
        manifest.portage.optimize = "generic"
        self.assertEqual([e for e in manifest.validate() if "portable" in e], [])

    def test_the_only_offered_optimisations_are_generic_and_native(self) -> None:
        manifest = target_manifest(portage={"optimize": "O3"})
        self.assertTrue(any("Optimisation" in error for error in manifest.validate()))

    def test_cxxflags_follow_common_flags_unless_set_on_purpose(self) -> None:
        for value in ("", "${CFLAGS}", "${COMMON_FLAGS}"):
            self.assertIn("CXXFLAGS='${COMMON_FLAGS}'", render_make_conf(target_manifest(portage={"cxxflags": value})).splitlines())
        self.assertIn("CXXFLAGS='-O2 -fno-exceptions'", render_make_conf(target_manifest(portage={"cxxflags": "-O2 -fno-exceptions"})).splitlines())


class LicenseTests(unittest.TestCase):
    def test_make_conf_lines_per_choice(self) -> None:
        cases = {"-* @FREE": "ACCEPT_LICENSE='-* @FREE'", "-* @FREE @BINARY-REDISTRIBUTABLE": "ACCEPT_LICENSE='-* @FREE @BINARY-REDISTRIBUTABLE'"}
        for value, line in cases.items():
            self.assertIn(line, render_make_conf(target_manifest(portage={"accept_license": value})).splitlines())

    def test_the_groups_of_a_settings_file_become_accept_license_only_while_it_is_untouched(self) -> None:
        groups = ["@FREE", "@BINARY-REDISTRIBUTABLE", "@GPL-COMPATIBLE"]
        self.assertIn("ACCEPT_LICENSE='-* @FREE @BINARY-REDISTRIBUTABLE @GPL-COMPATIBLE'",
                      render_make_conf(target_manifest(portage={"license_groups": groups})).splitlines())
        explicit = render_make_conf(target_manifest(portage={"license_groups": groups, "accept_license": "-* @FREE"}))
        self.assertIn("ACCEPT_LICENSE='-* @FREE'", explicit.splitlines())

    def test_licences_for_single_packages_go_to_package_license(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = target_manifest(portage={"package_license": {"games-util/steam-launcher": ["ValveSteamLicense"], "sys-kernel/linux-firmware": ["linux-fw-redistributable", "no-source-code"]}})
            self.assertEqual([e for e in manifest.validate() if "licence" in e.lower()], [])
            configurer(root).configure_portage(manifest)
            self.assertEqual(read(root, "etc/portage/package.license/installer"),
                             "games-util/steam-launcher ValveSteamLicense\nsys-kernel/linux-firmware linux-fw-redistributable no-source-code\n")

    def test_a_bad_licence_entry_is_refused(self) -> None:
        manifest = target_manifest(portage={"package_license": {"not an atom": ["X"]}})
        self.assertTrue(any("Invalid package licence entry" in e for e in manifest.validate()))

    def test_the_hardware_plan_still_cannot_widen_accept_license(self) -> None:
        text = render_make_conf(target_manifest(), FakePlan(make_conf={"ACCEPT_LICENSE": "*"}))
        self.assertIn("ACCEPT_LICENSE=@FREE", text.splitlines())


class MirrorTests(unittest.TestCase):
    def test_gentoo_mirrors_lists_the_chosen_mirrors_then_the_fallbacks(self) -> None:
        manifest = target_manifest(portage={"mirrors": ["https://a.example/gentoo", "https://b.example/gentoo/"],
                                            "mirror_fallbacks": ["https://c.example/gentoo", "https://a.example/gentoo"]})
        self.assertIn("GENTOO_MIRRORS='https://a.example/gentoo https://b.example/gentoo https://c.example/gentoo'", render_make_conf(manifest).splitlines())

    def test_the_single_mirror_of_older_settings_files_still_works(self) -> None:
        self.assertEqual(mirror_list("https://m.example/gentoo", [], ["https://f.example"]), ["https://m.example/gentoo", "https://f.example"])
        self.assertIn("GENTOO_MIRRORS=https://m.example/gentoo", render_make_conf(target_manifest(portage={"mirror": "https://m.example/gentoo"})).splitlines())

    def test_a_plain_http_mirror_is_refused(self) -> None:
        manifest = target_manifest(portage={"mirrors": ["http://insecure.example/gentoo"]})
        self.assertTrue(any("HTTPS" in error for error in manifest.validate()))


class StageThreeStageTests(StageHarness):
    def test_the_stage3_comes_from_the_first_chosen_mirror_with_the_others_as_fallbacks(self) -> None:
        installer = self.make(target_manifest(portage={"mirrors": ["https://a.example/gentoo", "https://b.example/gentoo"], "mirror_fallbacks": ["https://c.example/gentoo"]}))
        calls: list = []
        installer.stage3.install = lambda *args, **kwargs: calls.append((args, kwargs))  # type: ignore[method-assign]
        installer._stage3()
        (mirror, variant, *_), kwargs = calls[0]
        self.assertEqual((mirror, kwargs["fallback_mirrors"]), ("https://a.example/gentoo", ("https://b.example/gentoo", "https://c.example/gentoo")))
        self.assertEqual(variant, "amd64-desktop-openrc")  # KDE: the Desktop stage3

    def test_a_minimal_system_takes_the_standard_stage3_and_a_hardened_choice_is_kept(self) -> None:
        for settings, expected in (({"desktop": "minimal"}, "amd64-openrc"), ({"desktop": "xfce", "stage_variant": "amd64-hardened-openrc"}, "amd64-hardened-openrc"),
                                   ({"desktop": "kde", "stage_auto": False}, "amd64-openrc")):
            with self.subTest(settings=settings):
                installer = self.make(target_manifest(system=settings))
                calls: list = []
                installer.stage3.install = lambda *args, **kwargs: calls.append(args)  # type: ignore[method-assign]
                installer._stage3()
                self.assertEqual(calls[0][1], expected)


class RepositoryStageTests(StageHarness):
    def test_the_web_snapshot_is_the_default_and_writes_no_repos_conf(self) -> None:
        installer = self.make()
        installer._repository()
        self.assertIn("emerge-webrsync", self.chroot_lines())
        self.assertFalse((self.target / "etc/portage/repos.conf").exists())

    def test_sync_mode_writes_the_verified_repos_conf_and_uses_the_default_rsync_server(self) -> None:
        installer = self.make(target_manifest(portage={"rsync_mode": "sync"}))
        installer._repository()
        text = read(self.target, "etc/portage/repos.conf/gentoo.conf")
        for line in ("sync-type = rsync", "sync-uri = rsync://rsync.gentoo.org/gentoo-portage", "sync-rsync-verify-metamanifest = yes",
                     "sync-rsync-verify-jobs = 1", "sync-openpgp-key-path = /usr/share/openpgp-keys/gentoo-release.asc",
                     "sync-openpgp-keyserver = hkps://keys.gentoo.org", "sync-openpgp-key-refresh-retry-count = 40", "sync-webrsync-verify-signature = yes"):
            self.assertIn(line, text.splitlines())
        self.assertIn("emaint sync --auto", self.chroot_lines())
        self.assertNotIn("emerge-webrsync", self.chroot_lines())

    def test_an_https_rsync_mirror_is_refused(self) -> None:
        manifest = target_manifest(portage={"rsync_mode": "sync", "rsync_mirror": "https://git.example/gentoo.git"})
        self.assertTrue(any("rsync://" in error for error in manifest.validate()))
        with self.assertRaises(Exception):
            self.make(manifest)._repository()

    def test_blocked_rsync_falls_back_to_the_signed_snapshot_with_a_warning(self) -> None:
        runner = ScriptedRunner()
        runner.script("chroot", ("fail", "rsync: connection timed out (port 873)"))
        installer = self.make(target_manifest(portage={"rsync_mode": "sync"}), runner)
        installer._repository()
        self.assertEqual([line for line in self.chroot_lines() if line in {"emaint sync --auto", "emerge-webrsync"}], ["emaint sync --auto", "emerge-webrsync"])
        self.assertTrue(any("web snapshot" in line for line in installer.degraded))
        self.assertIn("sync-rsync-verify-metamanifest = yes", read(self.target, "etc/portage/repos.conf/gentoo.conf"))  # still verified

    def test_the_stages_own_repos_conf_is_the_starting_point(self) -> None:
        base = "[DEFAULT]\nmain-repo = gentoo\n\n[gentoo]\nlocation = /var/db/repos/gentoo\nsync-type = rsync\nsync-uri = rsync://old/\nsync-rsync-verify-metamanifest = no\nauto-sync = no\n[other]\nx = 1\n"
        text = render_repos_conf(base, "rsync://mirror.example/gentoo-portage")
        self.assertIn("sync-rsync-verify-metamanifest = yes", text.splitlines())
        self.assertNotIn("no\n", text.replace("auto-sync = no", ""))
        self.assertIn("sync-uri = rsync://mirror.example/gentoo-portage", text.splitlines())
        self.assertIn("auto-sync = yes", text.splitlines())
        self.assertLess(text.index("sync-openpgp-key-path"), text.index("[other]"))  # added to [gentoo], not to the next section

    def test_gentoo_news_are_read_after_the_sync_and_reported(self) -> None:
        runner = ScriptedRunner()
        runner.respond("*eselect news list", "News items:\n  [1]   N  2026-10-15  KDE Plasma 6.8 retires X11 session\n  [2]      2026-05-03  Portage binpkg changes\n")
        installer = self.make(runner=runner)
        installer._repository()
        news = [event.data["news"] for event in self.events if event.type == "progress" and "news" in event.data]
        self.assertEqual(news, [{"unread": 1, "titles": ["2026-10-15  KDE Plasma 6.8 retires X11 session"]}])

    def test_a_failing_news_command_never_stops_the_stage(self) -> None:
        runner = ScriptedRunner()
        runner.script("*eselect news", ("fail", "eselect: broken"))
        installer = self.make(runner=runner)
        installer._repository()
        self.assertFalse([event for event in self.events if event.type == "progress" and "news" in getattr(event, "data", {})])


class PortageStageTests(StageHarness):
    def test_getuto_runs_after_the_profile_and_before_the_resolve(self) -> None:
        installer = self.make()
        installer._portage()
        lines = self.chroot_lines()
        self.assertLess(lines.index("eselect profile set default/linux/amd64/23.0/desktop/plasma"), lines.index("getuto"))
        self.assertLess(lines.index("getuto"), next(i for i, line in enumerate(lines) if " --pretend " in f" {line} "))

    def test_a_profile_override_is_the_profile_that_is_set(self) -> None:
        installer = self.make(target_manifest(portage={"profile_override": "default/linux/amd64/23.0/desktop/gnome"}))
        installer._portage()
        self.assertEqual(self.chroot_lines()[0], "eselect profile set default/linux/amd64/23.0/desktop/gnome")

    def test_an_override_for_another_init_system_is_refused(self) -> None:
        manifest = target_manifest(portage={"profile_override": "default/linux/amd64/23.0/desktop/gnome/systemd"})
        self.assertTrue(any("init system" in error for error in manifest.validate()))

    def test_a_failed_getuto_is_a_warning_in_the_result_and_the_install_goes_on(self) -> None:
        runner = ScriptedRunner()
        runner.script("*getuto", ("fail", "no network"), ("fail", "no network"))
        installer = self.make(runner=runner)
        installer._portage()
        self.assertTrue(any("built from source" in line for line in installer.degraded))
        self.assertNotIn("getbinpkg", read(self.target, "etc/portage/make.conf").replace("binpkg-request-signature", ""))


class WorldUpdateTests(unittest.TestCase):
    def test_now_and_later_are_literal(self) -> None:
        self.assertTrue(should_update_world(target_manifest(system={"world_update": "now"})))
        self.assertFalse(should_update_world(target_manifest(system={"world_update": "later"})))

    def test_auto_updates_only_when_the_profile_or_use_differs_from_the_stage3(self) -> None:
        # KDE on the Desktop stage3: the Plasma profile differs from the stage3's desktop profile
        self.assertTrue(should_update_world(target_manifest(system={"world_update": "auto"})))
        self.assertIn("profile", world_update_reason(target_manifest(system={"world_update": "auto"})))
        # a minimal system on the Standard stage3 with the stage3's own profile and no USE changes: nothing to update
        same = target_manifest(system={"world_update": "auto", "desktop": "minimal"})
        self.assertFalse(should_update_world(same))
        same.portage.use_flags = ["-gtk"]
        self.assertTrue(should_update_world(same))
        self.assertEqual(world_update_reason(same), "USE flags were changed")

    def test_stage_profiles(self) -> None:
        self.assertEqual(stage_profile("amd64-openrc"), "default/linux/amd64/23.0")
        self.assertEqual(stage_profile("amd64-desktop-systemd"), "default/linux/amd64/23.0/desktop/systemd")
        self.assertEqual(stage_profile("amd64-hardened-openrc"), "default/linux/amd64/23.0/hardened")

    def test_depclean_needs_the_tick_and_a_finished_update(self) -> None:
        ticked = target_manifest(portage={"depclean": True}, system={"world_update": "now"})
        self.assertTrue(should_depclean(ticked, True))
        self.assertFalse(should_depclean(ticked, False))
        self.assertFalse(should_depclean(target_manifest(system={"world_update": "now"}), True))


class DesktopWorldUpdateTests(unittest.TestCase):
    def run_desktop(self, *, world: str, depclean: bool, fail_world: bool = False) -> tuple[list[str], list[str]]:
        runner = ScriptedRunner()
        if fail_world:
            runner.script("*@world", ("fail", "slot conflict"))
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary, runner)
            m = target_manifest(system={"desktop": "minimal", "world_update": world}, portage={"depclean": depclean},
                                options={"pipewire": False, "bluetooth": False, "printing": False})
            result = c.install_desktop(m)
        return [line for line in runner.lines() if " emerge " in line], result.degraded

    def test_depclean_runs_last_after_a_successful_update(self) -> None:
        emerges, degraded = self.run_desktop(world="now", depclean=True)
        self.assertTrue(emerges[0].endswith("@world") and emerges[-1].endswith("--depclean"))
        self.assertEqual(degraded, [])

    def test_depclean_is_skipped_after_a_failed_update(self) -> None:
        emerges, degraded = self.run_desktop(world="now", depclean=True, fail_world=True)
        self.assertFalse(any("--depclean" in line for line in emerges))
        self.assertTrue(degraded and "@world" in degraded[0])

    def test_nothing_runs_when_it_is_not_ticked_or_the_update_is_later(self) -> None:
        emerges, degraded = self.run_desktop(world="now", depclean=False)
        self.assertFalse(any("--depclean" in line for line in emerges))
        self.assertEqual(degraded, [])
        emerges, degraded = self.run_desktop(world="later", depclean=False)
        self.assertFalse(any("@world" in line for line in emerges))

    def test_a_ticked_depclean_that_cannot_run_is_not_silent(self) -> None:
        emerges, degraded = self.run_desktop(world="later", depclean=True)
        self.assertFalse(any("--depclean" in line for line in emerges))
        self.assertTrue(degraded and "no longer needed" in degraded[0])


class NewsParserTests(unittest.TestCase):
    SAMPLE = """News items:
  [1]      2018-08-07  Migration required for OpenSSH with LDAP
  [2]   N  2019-05-23  Change of ACCEPT_LICENSE default
  [41]  N  2026-10-15  KDE Plasma 6.8 retires X11 session
"""

    def test_items_and_the_unread_marker(self) -> None:
        items = parse_news_list(self.SAMPLE)
        self.assertEqual([(i.number, i.unread) for i in items], [(1, False), (2, True), (41, True)])
        self.assertEqual(items[2].title, "KDE Plasma 6.8 retires X11 session")
        self.assertEqual(news_summary(items), {"unread": 2, "titles": ["2026-10-15  KDE Plasma 6.8 retires X11 session", "2019-05-23  Change of ACCEPT_LICENSE default"]})

    def test_empty_or_failed_output_gives_no_news_line(self) -> None:
        for text in ("", "No news is good news.\n", "!!! Warning: Bad option: --help\n", "News items:\n"):
            self.assertEqual(news_summary(parse_news_list(text)), {})
        self.assertEqual(news_summary(parse_news_list("News items:\n  [1]      2018-08-07  Read item\n")), {})


class ManifestRoundTripTests(unittest.TestCase):
    def test_package_license_survives_a_round_trip(self) -> None:
        manifest = InstallManifest()
        manifest.portage.package_license = {"games-util/steam-launcher": ["ValveSteamLicense"]}
        again = InstallManifest.from_dict(manifest.to_dict())
        self.assertEqual(again.portage.package_license, manifest.portage.package_license)
