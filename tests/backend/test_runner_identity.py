"""B9 / root_identity: what credentials the child process gets."""
from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import patch

from gentoo_installerd import runner as runner_module
from gentoo_installerd.runner import DryRunRunner, SubprocessRunner

REAL_POPEN = subprocess.Popen


class IdentityOptionTests(unittest.TestCase):
    def run_as(self, euid: int, **options: object) -> dict[str, object]:
        captured: dict[str, object] = {}

        def popen(args, **kwargs):
            captured.update(kwargs)
            # Changing gid needs a real root; the options are what is under test.
            return REAL_POPEN(args, **{key: value for key, value in kwargs.items() if key not in ("group", "extra_groups")})

        with patch.object(runner_module.subprocess, "Popen", popen), patch.object(runner_module.os, "geteuid", return_value=euid):
            SubprocessRunner().run([sys.executable, "-c", "pass"], **options)  # type: ignore[arg-type]
        return captured

    def test_root_commands_never_inherit_the_live_group(self) -> None:
        self.assertEqual(self.run_as(0)["group"], 0)

    def test_unprivileged_runs_keep_their_own_credentials(self) -> None:
        captured = self.run_as(1000)
        self.assertNotIn("group", captured)
        self.assertNotIn("extra_groups", captured)
        self.assertNotIn("umask", captured)

    def test_root_identity_drops_supplementary_groups_and_sets_umask_022(self) -> None:
        captured = self.run_as(0, root_identity=True)
        self.assertEqual(captured["group"], 0)
        self.assertEqual(captured["extra_groups"], [])
        self.assertEqual(captured["umask"], 0o022)

    def test_root_identity_without_root_only_sets_the_umask(self) -> None:
        captured = self.run_as(1000, root_identity=True)
        self.assertEqual(captured["umask"], 0o022)
        self.assertNotIn("group", captured)
        self.assertNotIn("extra_groups", captured)

    def test_no_preexec_fn_is_used_in_the_threaded_daemon(self) -> None:
        self.assertNotIn("preexec_fn", self.run_as(0, root_identity=True))

    def test_the_umask_really_reaches_the_child(self) -> None:
        script = "import os; print(oct(os.umask(0)))"
        with patch.object(runner_module.os, "geteuid", return_value=1000):
            default = SubprocessRunner().run([sys.executable, "-c", script]).stdout
            forced = SubprocessRunner().run([sys.executable, "-c", script], root_identity=True).stdout
        self.assertEqual(forced, "0o22")
        self.assertRegex(default, r"^0o\d+$")

    def test_the_dry_run_runner_accepts_the_new_keywords(self) -> None:
        runner = DryRunRunner()
        runner.run(["true"], timeout=5, capture=False, root_identity=True)
        self.assertEqual(runner.commands, [("true",)])


if __name__ == "__main__":
    unittest.main()
