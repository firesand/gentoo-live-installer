"""D14: output is streamed and bounded, never kept in full unless the caller asks for it."""
from __future__ import annotations

import sys
import tracemalloc
import unittest
from unittest.mock import patch

from gentoo_installerd import runner as runner_module
from gentoo_installerd.runner import RunnerError, SubprocessRunner


class BoundedCaptureTests(unittest.TestCase):
    def test_uncaptured_output_is_streamed_but_not_kept(self) -> None:
        seen = 0

        def count(_line: str) -> None:
            nonlocal seen
            seen += 1

        runner = SubprocessRunner(count)
        tracemalloc.start()
        try:
            result = runner.run([sys.executable, "-c", "[print('compiling object', i) for i in range(100000)]"], capture=False)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result.stdout, "")
        self.assertGreaterEqual(seen, 100000)
        self.assertLess(peak, 12 * 1024 * 1024, f"peak memory {peak} bytes is not bounded")

    def test_capture_is_the_default_and_returns_every_line(self) -> None:
        result = SubprocessRunner().run([sys.executable, "-c", "[print(i) for i in range(1000)]"])
        self.assertEqual(result.stdout.splitlines(), [str(i) for i in range(1000)])

    def test_a_failure_carries_the_last_lines_even_without_capture(self) -> None:
        runner = SubprocessRunner()
        with self.assertRaises(RunnerError) as caught:
            runner.run([sys.executable, "-c", "import sys; [print('line', i) for i in range(5000)]; sys.exit(7)"], capture=False)
        error = caught.exception
        self.assertEqual(error.returncode, 7)
        lines = error.output.splitlines()
        self.assertEqual(lines[-1], "line 4999")
        self.assertLessEqual(len(lines), runner_module.TAIL_LINES)
        self.assertIn("line 4999", str(error))

    def test_the_capture_limit_stops_keeping_but_not_streaming(self) -> None:
        seen: list[str] = []
        runner = SubprocessRunner(seen.append)
        with patch.object(runner_module, "MAX_CAPTURE_CHARS", 200):
            result = runner.run([sys.executable, "-c", "[print('x' * 40, i) for i in range(100)]"])
        self.assertLess(len(result.stdout), 400)
        self.assertEqual(sum(1 for line in seen if line.startswith("xxxx")), 100)


class LineHandlingTests(unittest.TestCase):
    def test_carriage_returns_and_crlf_end_lines_like_universal_newlines(self) -> None:
        result = SubprocessRunner().run([sys.executable, "-c", "import sys; sys.stdout.write('a\\rb\\r\\nc\\n')"])
        self.assertEqual(result.stdout.splitlines(), ["a", "b", "c"])

    def test_a_crlf_split_across_two_reads_is_one_line_end(self) -> None:
        script = "import sys, time; sys.stdout.write('first\\r'); sys.stdout.flush(); time.sleep(0.3); sys.stdout.write('\\nsecond\\n')"
        result = SubprocessRunner().run([sys.executable, "-c", script])
        self.assertEqual(result.stdout.splitlines(), ["first", "second"])

    def test_invalid_utf8_is_replaced_not_fatal(self) -> None:
        result = SubprocessRunner().run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'caf\\xc3\\xa9 \\xff\\xfe ok\\n')"])
        self.assertTrue(result.stdout.startswith("café "))
        self.assertTrue(result.stdout.endswith(" ok"))

    def test_a_line_without_a_newline_is_split_at_the_limit(self) -> None:
        seen: list[str] = []
        with patch.object(runner_module, "MAX_LINE_CHARS", 1000):
            SubprocessRunner(seen.append).run([sys.executable, "-c", "import sys; sys.stdout.write('x' * 2500)"])
        lengths = [len(line) for line in seen if line and not line.startswith("$")]
        self.assertEqual(lengths, [1000, 1000, 500])

    def test_the_last_line_without_a_newline_is_kept(self) -> None:
        result = SubprocessRunner().run([sys.executable, "-c", "import sys; sys.stdout.write('one\\ntwo')"])
        self.assertEqual(result.stdout.splitlines(), ["one", "two"])

    def test_stderr_is_merged_in_order(self) -> None:
        script = "import sys; print('out1', flush=True); print('err1', file=sys.stderr, flush=True); print('out2', flush=True)"
        result = SubprocessRunner().run([sys.executable, "-c", script])
        self.assertEqual(result.stdout.splitlines(), ["out1", "err1", "out2"])


if __name__ == "__main__":
    unittest.main()
