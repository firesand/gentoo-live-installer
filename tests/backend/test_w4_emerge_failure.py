"""B1 (wave 4): a failed emerge is explained from what current Portage prints and from the failed package's own build log.

tests/backend/fixtures/pyside_oom holds real output of the first VM acceptance run (a 4 GiB machine): build.log is the
build log of dev-python/pyside, whose compiler the kernel killed, and emerge-tail.txt is the last 200 lines the runner
kept of the emerge run. Mesa and portagehub were built after the failure, so the compiler's own error is no longer
in those lines, and the last three lines are the unrelated "GNU info directory index is up-to-date" notices.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from gentoo_installerd.configure import BUILD_LOG_TAIL_BYTES, PackageInstallError, Resources, explain_emerge_failure
from gentoo_installerd.runner import RunnerError

from tests.backend.target_helpers import FakePlan, ScriptedRunner, configurer, mark_installed, read, target_manifest
from tests.backend.test_w3_target_fakes import UUID, StageMixin

FIXTURES = Path(__file__).parent / "fixtures" / "pyside_oom"
BUILD_LOG = (FIXTURES / "build.log").read_text(encoding="utf-8")
EMERGE_TAIL = (FIXTURES / "emerge-tail.txt").read_text(encoding="utf-8")
PYSIDE = "dev-python/pyside-6.11.2"
PYSIDE_LOG = f"/var/tmp/portage/{PYSIDE}/temp/build.log"
KILLED = "x86_64-pc-linux-gnu-g++: fatal error: Killed signal terminated program cc1plus"
# What emerge prints when one package fails and --keep-going was not given (the base system stage): there is no
# summary block, only the messages of the package (real lines, with the colour codes of ebuild.sh) and the log file.
SINGLE_FAILURE = "\n".join([*BUILD_LOG.split("\n")[70:95], "", f">>> Failed to emerge {PYSIDE}, Log file:", "", f">>>  '{PYSIDE_LOG}'", ""])


def pyside_log(root: Path, text: str = BUILD_LOG) -> None:
    log = root / PYSIDE_LOG.lstrip("/")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")


class CurrentPortageOutputTests(unittest.TestCase):
    def test_the_failure_list_of_current_portage_names_the_package_and_its_log(self) -> None:
        diagnosis = explain_emerge_failure(EMERGE_TAIL)
        self.assertTrue(diagnosis.build_failure)
        self.assertEqual([(item.package, item.phase, item.log) for item in diagnosis.failures], [(PYSIDE, "compile", PYSIDE_LOG)])
        # hwmonux is in the list too, but only because keep-going dropped it: nothing of it was built
        self.assertEqual(diagnosis.dropped, ("app-misc/hwmonux-1.0.0-r1",))
        self.assertEqual(diagnosis.summary, f"{PYSIDE} failed to compile")

    def test_the_last_lines_of_the_run_are_never_the_explanation(self) -> None:
        for read_log in (None, lambda path: BUILD_LOG):
            summary = explain_emerge_failure(EMERGE_TAIL, read_log).summary
            self.assertNotIn("GNU info", summary)
            self.assertNotIn("news items", summary)

    def test_the_build_log_shows_that_the_compiler_was_killed(self) -> None:
        asked: list[str] = []

        def read_log(path: str) -> str:
            asked.append(path)
            return BUILD_LOG

        diagnosis = explain_emerge_failure(EMERGE_TAIL, read_log)
        self.assertEqual(asked, [PYSIDE_LOG])
        self.assertTrue(diagnosis.out_of_memory)
        self.assertEqual(diagnosis.kind, "memory")
        # the assembler's "Error:" lines come first in the log, but they are only what the kill left behind
        self.assertEqual(diagnosis.summary, f"{PYSIDE} ran out of memory while compiling: {KILLED}")

    def test_a_single_failure_has_no_summary_block_but_names_itself(self) -> None:
        diagnosis = explain_emerge_failure(SINGLE_FAILURE)
        self.assertEqual([(item.package, item.phase, item.log) for item in diagnosis.failures], [(PYSIDE, "compile", PYSIDE_LOG)])
        self.assertEqual((diagnosis.kind, diagnosis.dropped), ("build", ()))
        self.assertEqual(explain_emerge_failure(SINGLE_FAILURE, lambda path: BUILD_LOG).kind, "memory")

    def test_the_colour_codes_of_the_ebuild_messages_do_not_hide_the_error(self) -> None:
        self.assertIn("\x1b[31;01m", SINGLE_FAILURE)  # the fixture is the real, coloured text
        colourless = SINGLE_FAILURE.replace("\x1b[31;01m", "").replace("\x1b[0m", "")
        self.assertEqual(explain_emerge_failure(SINGLE_FAILURE), explain_emerge_failure(colourless))

    def test_the_log_is_found_by_its_default_place_when_emerge_printed_none(self) -> None:
        output = " * ERROR: app-misc/foo-1.0::gentoo failed (compile phase):\n *   emake failed\n"
        asked: list[str] = []
        explain_emerge_failure(output, lambda path: asked.append(path) or "")
        self.assertEqual(asked, ["/var/tmp/portage/app-misc/foo-1.0/temp/build.log"])

    def test_a_package_that_installed_but_failed_afterwards_is_named_with_its_step(self) -> None:
        output = ("\n * The following 2 packages have failed to build, install, or execute postinst:\n * \n"
                  " *  (app-misc/one-1.0:0/0::gentoo, ebuild scheduled for merge) (postinst failed), Log file:\n"
                  " *   '/var/tmp/portage/app-misc/one-1.0/temp/build.log'\n"
                  " *  (app-misc/two-2.0-1:0/0::gentoo, binary scheduled for merge)\n")
        diagnosis = explain_emerge_failure(output)
        self.assertEqual([(item.package, item.phase) for item in diagnosis.failures], [("app-misc/one-1.0", "postinst"), ("app-misc/two-2.0-1", "")])
        self.assertTrue(diagnosis.summary.startswith("app-misc/one-1.0 "))
        self.assertIn("app-misc/two-2.0-1", diagnosis.summary)

    def test_the_summary_block_of_older_portage_is_still_understood(self) -> None:
        diagnosis = explain_emerge_failure(" * The following 2 packages failed to build:\n * 1) dev-qt/qtwebengine-6.11.2::gentoo (/log)\n * 2) www-client/falkon-26.04.3::gentoo (/log)\n")
        self.assertEqual([item.package for item in diagnosis.failures], ["dev-qt/qtwebengine-6.11.2", "www-client/falkon-26.04.3"])
        self.assertEqual(diagnosis.summary, "failed to build: dev-qt/qtwebengine-6.11.2, www-client/falkon-26.04.3")


FAILED = " * ERROR: app-misc/foo-1.0::gentoo failed (compile phase):\n *   emake failed\n"


class BuildLogReasonTests(unittest.TestCase):
    def explain(self, log: str, output: str = FAILED):
        return explain_emerge_failure(output, lambda path: log)

    def test_every_way_a_compiler_dies_of_memory_is_recognised(self) -> None:
        for line in (
            KILLED,
            "g++: internal compiler error: Killed (program cc1plus)",
            "virtual memory exhausted: Cannot allocate memory",
            "cc1: out of memory allocating 65536 bytes after a total of 1073741824 bytes",
            "collect2: fatal error: ld terminated with signal 9 [Killed]",
            "terminate called after throwing an instance of 'std::bad_alloc'",
            "memory allocation of 8589934592 bytes failed",
            "error: could not compile `wgpu-core` (lib); process didn't exit successfully: `rustc --crate-name wgpu_core` (signal: 9, SIGKILL: kill)",
        ):
            with self.subTest(line=line):
                diagnosis = self.explain(f"[1/9] cc -c a.c\nwarning: unused variable\n{line}\nmake: *** [Makefile:3: all] Error 2\n")
                self.assertTrue(diagnosis.out_of_memory)
                self.assertEqual(diagnosis.kind, "memory")
                self.assertEqual(diagnosis.summary, f"app-misc/foo-1.0 ran out of memory while compiling: {line}")

    def test_the_word_killed_counts_only_as_the_shell_prints_it(self) -> None:
        self.assertTrue(self.explain("make[2]: *** [CMakeFiles/a.dir/build.make:76: a.o] Killed\n").out_of_memory)
        self.assertFalse(self.explain("test_child_process_killed ... FAILED\nsrc/a.c:1:1: error: boom\n").out_of_memory)

    def test_an_ordinary_compile_error_is_not_a_memory_problem(self) -> None:
        diagnosis = self.explain("cc -c a.c\nsrc/a.c:12:5: error: unknown type name 'bar'\nmake: *** [Makefile:3: a.o] Error 1\n")
        self.assertFalse(diagnosis.out_of_memory)
        self.assertEqual(diagnosis.kind, "build")
        self.assertEqual(diagnosis.summary, "app-misc/foo-1.0 failed to compile: src/a.c:12:5: error: unknown type name 'bar'")

    def test_the_first_error_is_named_not_the_make_line_after_it(self) -> None:
        log = "src/a.c:3:1: error: expected ';' before 'int'\nsrc/b.c:9:1: error: second\nmake[1]: *** [Makefile:55: a.o] Error 1\nmake: *** [all] Error 2\n"
        self.assertEqual(self.explain(log).summary, "app-misc/foo-1.0 failed to compile: src/a.c:3:1: error: expected ';' before 'int'")

    def test_a_build_command_line_is_not_taken_for_the_error(self) -> None:
        command = "cc -Wno-error=unused -Werror=format-security " + "-DSOME_DEFINE=1 " * 60
        log = f"FAILED: CMakeFiles/a.dir/a.c.o\n{command}\nsrc/a.c:1:1: error: nothing works\n"
        self.assertEqual(self.explain(log).summary, "app-misc/foo-1.0 failed to compile: src/a.c:1:1: error: nothing works")

    def test_a_log_without_a_clear_error_still_names_the_package(self) -> None:
        self.assertEqual(self.explain("some output\nmore output\n").summary, "app-misc/foo-1.0 failed to compile")
        self.assertEqual(self.explain("").summary, "app-misc/foo-1.0 failed to compile")

    def test_other_steps_are_named_in_plain_words(self) -> None:
        for phase, text in (("configure", "failed to configure"), ("install", "failed to install"), ("unpack", "failed to unpack"),
                            ("test", "failed its tests"), ("setup", "failed in its setup")):
            output = f" * ERROR: app-misc/foo-1.0::gentoo failed ({phase} phase):\n"
            self.assertEqual(explain_emerge_failure(output).summary, f"app-misc/foo-1.0 {text}", phase)

    def test_a_log_that_cannot_be_read_is_ignored(self) -> None:
        def broken(path: str) -> str:
            raise OSError("gone")

        diagnosis = explain_emerge_failure(FAILED, broken)
        self.assertEqual(diagnosis.summary, "app-misc/foo-1.0 failed to compile")

    def test_a_package_that_checks_its_memory_before_building_is_a_memory_problem(self) -> None:
        # check-reqs.eclass, as qtwebengine uses it: emerge stops before it builds anything, and fewer jobs are what fixes it
        output = (">>> Running pre-merge checks for dev-qt/qtwebengine-6.11.2-r1\n * Checking for at least 10 GiB RAM ...                [ !! ]\n"
                  " * There is NOT at least 10 GiB RAM\n * \n * Memory or space constraints set in the ebuild were not met!\n"
                  " * - enhance the memory (reduce MAKEOPTS, add swap), or\n * \n"
                  " * ERROR: dev-qt/qtwebengine-6.11.2-r1::gentoo failed (pretend phase):\n *   Build requirements not met!\n")
        diagnosis = explain_emerge_failure(output)
        self.assertEqual(diagnosis.kind, "memory")
        self.assertEqual(diagnosis.summary, "dev-qt/qtwebengine-6.11.2-r1 needs 10 GiB of memory for its compile jobs, and this PC has less")
        # a check that fails for another reason is just a failed check
        other = output.replace("There is NOT at least 10 GiB RAM", "There is NOT at least 11 GiB /var/tmp/portage")
        self.assertEqual(explain_emerge_failure(other).kind, "build")
        self.assertEqual(explain_emerge_failure(other).summary,
                         "dev-qt/qtwebengine-6.11.2-r1 failed its check before the build: There is NOT at least 11 GiB /var/tmp/portage")

    def test_the_memory_words_in_the_run_output_still_count_without_a_log(self) -> None:
        diagnosis = explain_emerge_failure(f"{KILLED}\n * 1) xfce-base/xfce4-meta-4.20::gentoo (/log)")
        self.assertEqual((diagnosis.kind, diagnosis.summary), ("memory", "failed to build: xfce-base/xfce4-meta-4.20 (the build ran out of memory)"))


class ReadingTheLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.c = configurer(self.root)

    def test_the_log_is_read_from_inside_the_new_system(self) -> None:
        pyside_log(self.root)
        self.assertEqual(self.c.build_log_tail(PYSIDE_LOG), BUILD_LOG)

    def test_a_missing_log_gives_nothing(self) -> None:
        self.assertEqual(self.c.build_log_tail(PYSIDE_LOG), "")
        self.assertEqual(self.c.build_log_tail("var/tmp/not-absolute.log"), "")

    def test_a_path_that_leaves_the_new_system_gives_nothing(self) -> None:
        outside = self.root.parent / "outside-the-new-system.log"
        outside.write_text("secret", encoding="utf-8")
        self.addCleanup(outside.unlink)
        (self.root / "var/tmp").mkdir(parents=True)
        (self.root / "var/tmp/link.log").symlink_to(outside)
        self.assertEqual(self.c.build_log_tail("/var/tmp/link.log"), "")
        self.assertEqual(self.c.build_log_tail("/var/tmp/../../../outside-the-new-system.log"), "")

    def test_only_a_file_is_read(self) -> None:
        os.mkfifo(self.root / "pipe.log")
        (self.root / "directory.log").mkdir()
        self.assertEqual(self.c.build_log_tail("/pipe.log"), "")
        self.assertEqual(self.c.build_log_tail("/directory.log"), "")

    def test_only_the_end_of_a_big_log_is_read(self) -> None:
        pyside_log(self.root, "first line\n" + "x" * (BUILD_LOG_TAIL_BYTES * 2) + "\nlast line\n")
        tail = self.c.build_log_tail(PYSIDE_LOG)
        self.assertLessEqual(len(tail.encode()), BUILD_LOG_TAIL_BYTES)
        self.assertTrue(tail.endswith("last line\n"))
        self.assertNotIn("first line", tail)

    def test_a_preview_reads_nothing(self) -> None:
        pyside_log(self.root)
        self.assertEqual(configurer(self.root, dry_run=True).build_log_tail(PYSIDE_LOG), "")

    def test_the_diagnosis_of_a_failed_run_uses_the_log_of_the_new_system(self) -> None:
        pyside_log(self.root)
        diagnosis = self.c.explain(RunnerError(("chroot", str(self.root), "emerge"), 1, EMERGE_TAIL))
        self.assertEqual(diagnosis.kind, "memory")
        self.assertEqual(diagnosis.summary, f"{PYSIDE} ran out of memory while compiling: {KILLED}")
        self.assertEqual(self.c.explain(EMERGE_TAIL).kind, "memory")  # plain text works too


class OutOfMemoryRetryTests(unittest.TestCase):
    """The retry after a killed build really runs with one compile job, and is not repeated when it cannot help."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runner = ScriptedRunner()
        self.c = configurer(self.root, self.runner)
        self.notes: list[str] = []
        self.c.notify = self.notes.append
        pyside_log(self.root)

    def emerges(self) -> list[tuple[str, dict[str, str]]]:
        return [(" ".join(command).removeprefix(f"chroot {self.root} emerge "), dict(env or {}))
                for command, env in zip(self.runner.commands, self.runner.envs) if command[2:3] == ("emerge",)]

    def test_the_base_system_is_built_again_with_one_job(self) -> None:
        self.runner.script("chroot", ("fail", SINGLE_FAILURE))
        self.c.install_essential(target_manifest())
        (first, first_env), (second, second_env) = self.emerges()
        self.assertNotIn("--jobs=1", first)
        self.assertNotIn("MAKEOPTS", first_env)
        self.assertTrue(second.startswith("--jobs=1 --noreplace "))
        self.assertEqual((second_env["MAKEOPTS"], second_env["NINJAOPTS"]), ("-j1", "-j1"))
        self.assertTrue(any("single compile job" in note and PYSIDE in note for note in self.notes), self.notes)

    def test_a_roomy_machine_builds_one_package_at_a_time_in_the_retry_too(self) -> None:
        self.c.resources = lambda: Resources(16, 32 * 1024**3, 30 * 1024**3)
        self.runner.script("chroot", ("fail", SINGLE_FAILURE))
        self.c.install_essential(target_manifest())
        (first, first_env), (second, second_env) = self.emerges()
        self.assertIn("--jobs=2", first_env["EMERGE_DEFAULT_OPTS"])
        self.assertIn("--jobs=1", second.split())  # the command line wins over EMERGE_DEFAULT_OPTS

    def test_the_base_system_stops_when_one_job_does_not_fit_either(self) -> None:
        self.runner.script("chroot", ("fail", SINGLE_FAILURE), ("fail", SINGLE_FAILURE), ("fail", SINGLE_FAILURE))
        with self.assertRaises(PackageInstallError) as context:
            self.c.install_essential(target_manifest())
        self.assertEqual(len(self.emerges()), 2)
        message = str(context.exception)
        self.assertIn(f"{PYSIDE} ran out of memory while compiling: {KILLED}", message)
        self.assertIn("with one compile job", message)

    def test_a_build_that_already_had_one_job_is_not_repeated(self) -> None:
        self.c.resources = lambda: Resources(4, 4 * 1024**3, 3 * 1024**3)  # one package at a time
        m = target_manifest(portage={"make_opts": "-j1"})
        self.c.configure_portage(m)
        self.runner.script("chroot", ("fail", SINGLE_FAILURE))
        with self.assertRaisesRegex(PackageInstallError, "with one compile job"):
            self.c.install_essential(m)
        self.assertEqual(len(self.emerges()), 1)

    def test_two_packages_built_at_once_are_a_reason_to_try_again_even_with_one_job_each(self) -> None:
        m = target_manifest(portage={"make_opts": "-j1"})
        self.c.configure_portage(m)  # the default machine of these tests builds two packages at once
        self.runner.script("chroot", ("fail", SINGLE_FAILURE))
        self.c.install_essential(m)
        self.assertEqual([("--jobs=1" in command.split()) for command, _ in self.emerges()], [False, True])

    def test_a_package_with_its_own_limit_counts_by_that_limit(self) -> None:
        self.c.resources = lambda: Resources(4, 4 * 1024**3, 3 * 1024**3)
        m = target_manifest(portage={"make_opts": "-j4"})
        self.c.configure_portage(m)
        (self.root / "etc/portage/env").mkdir(parents=True)
        (self.root / "etc/portage/package.env").mkdir(parents=True)
        (self.root / "etc/portage/package.env/installer-hardware").write_text("dev-python/pyside installer-memory\n")
        (self.root / "etc/portage/env/installer-memory").write_text('# written by the installer\nMAKEOPTS="-j1"\n')
        self.runner.script("chroot", ("fail", SINGLE_FAILURE))
        with self.assertRaisesRegex(PackageInstallError, "with one compile job"):
            self.c.install_essential(m)
        self.assertEqual(len(self.emerges()), 1)

    def test_a_group_with_keep_going_is_built_again_for_what_is_still_missing(self) -> None:
        mark_installed(self.root, "app-portage/portagehub")
        self.runner.script("chroot", ("fail", EMERGE_TAIL))
        result = self.c.emerge_keep_going(target_manifest(), ["app-misc/hwmonux", "app-portage/portagehub"], label="Optional packages", attempts=2)
        (first, first_env), (second, second_env) = self.emerges()
        self.assertIn("app-portage/portagehub", first)
        self.assertEqual(second, "--jobs=1 --noreplace --keep-going app-misc/hwmonux")
        self.assertEqual((second_env["MAKEOPTS"], second_env["NINJAOPTS"]), ("-j1", "-j1"))
        self.assertEqual(result.missing, [])  # the retry (a scripted success) built it
        self.assertTrue(any("single compile job" in note for note in self.notes), self.notes)

    def test_a_package_dropped_for_a_killed_dependency_is_reported_with_the_real_reason(self) -> None:
        mark_installed(self.root, "app-portage/portagehub")
        self.runner.script("chroot", ("fail", EMERGE_TAIL), ("fail", EMERGE_TAIL))
        result = self.c.emerge_keep_going(target_manifest(), ["app-misc/hwmonux", "app-portage/portagehub"], label="Optional packages", attempts=2)
        self.assertEqual(result.missing, ["app-misc/hwmonux"])
        self.assertEqual(result.kind, "memory")
        self.assertTrue(result.reason.startswith(f"{PYSIDE} ran out of memory while compiling: {KILLED}"), result.reason)
        self.assertIn("with one compile job", result.reason)
        self.assertNotIn("GNU info", result.reason)

    def test_the_desktop_stage_names_the_package_that_could_not_be_built(self) -> None:
        # the first VM run: hwmonux needs pyside, pyside was killed, and the old text was the last lines of the output
        bundle = self.root / "bundle"
        (bundle / "overlay/profiles").mkdir(parents=True)
        (bundle / "overlay/profiles/repo_name").write_text("gentoo-live-installer\n")
        (bundle / "distfiles").mkdir()
        (bundle / "distfiles/hwmonux-1.0.0.tar.zst").write_bytes(b"x")
        (bundle / "distfiles/portagehub-0.4.0.tar.zst").write_bytes(b"x")
        c = configurer(self.root, self.runner, companion_bundle=bundle)
        m = target_manifest(system={"desktop": "minimal"}, options={"pipewire": False, "bluetooth": False, "printing": False,
                                                                       "install_hwmonux": True, "install_portagehub": True})
        mark_installed(self.root, "app-portage/portagehub")
        self.runner.script("chroot", ("fail", EMERGE_TAIL), ("fail", EMERGE_TAIL))
        result = c.install_desktop(m)
        self.assertEqual(result.failed, ["app-misc/hwmonux"])
        self.assertEqual(len(result.degraded), 1)
        self.assertEqual(result.degraded, [f"app-misc/hwmonux could not be installed: {PYSIDE} ran out of memory while compiling: {KILLED}. "
                                           "It still ran out of memory with one compile job: this PC needs more memory or swap for it."])
        self.assertEqual(len(self.emerges()), 2)

    def test_a_failed_world_update_names_the_package_too(self) -> None:
        self.runner.script("chroot", ("fail", EMERGE_TAIL))
        m = target_manifest(system={"desktop": "minimal", "world_update": True}, options={"pipewire": False, "bluetooth": False, "printing": False})
        result = self.c.install_desktop(m)
        self.assertEqual(result.degraded, [f"The @world update did not finish: {PYSIDE} ran out of memory while compiling: {KILLED}"])

    def test_a_failure_of_the_dependency_check_is_still_a_resolution_failure(self) -> None:
        from tests.backend.test_target_packages import EmergeDiagnosisTests

        self.runner.script("chroot", ("fail", EmergeDiagnosisTests.USE_CHANGES))
        with self.assertRaisesRegex(PackageInstallError, "qtpositioning"):
            self.c.pretend(target_manifest(), ["kde-apps/kde-apps-meta"])


class HardwareDriverStageTests(StageMixin, unittest.TestCase):
    def test_a_driver_that_cannot_be_built_is_reported_with_its_own_reason(self) -> None:
        installer = self.make(plan=FakePlan(driver_atoms=["x11-drivers/nvidia-drivers"]))
        self.runner.respond("blkid -s UUID -o value /dev/sda3", UUID + "\n")
        log = self.base / "target" / "var/tmp/portage/x11-drivers/nvidia-drivers-595.45/temp/build.log"
        log.parent.mkdir(parents=True)
        log.write_text("  CC [M]  nvidia/nv.o\nld: final link failed: Cannot allocate memory\n", encoding="utf-8")
        self.runner.script("*nvidia-drivers", ("fail", " * ERROR: x11-drivers/nvidia-drivers-595.45::gentoo failed (compile phase):\n *   emake failed\n"
                                              "\n * GNU info directory index is up-to-date.\n * Use eselect news read to view new items.\n"))
        installer._kernel()
        self.assertEqual(installer.degraded, ["Hardware drivers (x11-drivers/nvidia-drivers) could not be installed: x11-drivers/nvidia-drivers-595.45 ran out of memory "
                                              "while compiling: ld: final link failed: Cannot allocate memory"])
        self.assertTrue(any(line.startswith("dracut --force") for line in self.chroot_lines()))


if __name__ == "__main__":
    unittest.main()
