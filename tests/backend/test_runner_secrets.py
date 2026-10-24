"""S14: commands flagged secret stay diagnosable. Redaction gaps: quoting, URL schemes, key=value forms."""
from __future__ import annotations

import sys
import unittest

from gentoo_installerd.runner import DryRunRunner, RunnerError, SubprocessRunner, format_command, redact_text, stdin_secrets


class SecretCommandDiagnosticTests(unittest.TestCase):
    def test_failure_text_and_command_are_visible_but_the_secret_is_not(self) -> None:
        lines: list[str] = []
        runner = SubprocessRunner(lines.append)
        script = "import sys; data = sys.stdin.read(); print('cryptsetup: device busy'); print('echo ' + data.strip()); sys.exit(5)"
        with self.assertRaises(RunnerError) as caught:
            runner.run([sys.executable, "-c", script], stdin="hunter2-passphrase\n", secret=True)
        text = "\n".join(lines) + str(caught.exception) + caught.exception.output + " ".join(caught.exception.argv)
        self.assertIn("device busy", text)
        self.assertNotIn("hunter2-passphrase", text)
        self.assertEqual(caught.exception.returncode, 5)
        self.assertTrue(any(line.startswith("$ ") and "<redacted command>" not in line for line in lines))

    def test_the_result_of_a_secret_command_is_redacted(self) -> None:
        result = SubprocessRunner().run([sys.executable, "-c", "import sys; print('got', sys.stdin.read().strip())"], stdin="my-password\n", secret=True)
        self.assertNotIn("my-password", result.stdout)
        self.assertIn("got", result.stdout)

    def test_chpasswd_style_payload_is_scrubbed_after_the_colon(self) -> None:
        lines: list[str] = []
        runner = SubprocessRunner(lines.append)
        result = runner.run([sys.executable, "-c", "import sys; print('bad password:', sys.stdin.read().split(':', 1)[1])"], stdin="alice:s3cret-value\n", secret=True)
        self.assertNotIn("s3cret-value", "\n".join(lines))
        self.assertNotIn("s3cret-value", result.stdout)

    def test_manifest_secrets_are_still_scrubbed_from_non_secret_commands(self) -> None:
        lines: list[str] = []
        runner = SubprocessRunner(lines.append, secrets=["wifi-secret-value"])
        result = runner.run([sys.executable, "-c", "print('joined with wifi-secret-value')"])
        self.assertNotIn("wifi-secret-value", "\n".join(lines))
        # What callers parse is the real output; only the log and error text are scrubbed.
        self.assertIn("wifi-secret-value", result.stdout)

    def test_a_failing_command_never_leaks_a_manifest_secret_through_its_error(self) -> None:
        runner = SubprocessRunner(secrets=["super-secret-psk"])
        with self.assertRaises(RunnerError) as caught:
            runner.run([sys.executable, "-c", "import sys; print('bad key super-secret-psk'); sys.exit(2)", "--arg", "super-secret-psk"])
        self.assertNotIn("super-secret-psk", str(caught.exception))
        self.assertNotIn("super-secret-psk", caught.exception.output)
        self.assertNotIn("super-secret-psk", " ".join(caught.exception.argv))

    def test_the_dry_run_runner_shows_the_command_but_not_the_secret(self) -> None:
        lines: list[str] = []
        runner = DryRunRunner(lines.append)
        runner.run(["cryptsetup", "open", "--key-file=-", "/dev/sda3", "gentoo-root"], stdin="hunter2-passphrase", secret=True)
        self.assertIn("cryptsetup open", lines[0])
        self.assertNotIn("hunter2-passphrase", "\n".join(lines))


class StdinSecretTests(unittest.TestCase):
    def test_payload_lines_and_the_part_after_the_colon_are_secrets(self) -> None:
        values = stdin_secrets("alice:s3cret-value\nbob:other\n")
        for expected in ("alice:s3cret-value", "s3cret-value", "bob:other", "other"):
            self.assertIn(expected, values)
        self.assertEqual(stdin_secrets(None), [])
        self.assertEqual(stdin_secrets(""), [])


class RedactionGapTests(unittest.TestCase):
    def test_a_secret_with_a_quote_is_found_in_a_quoted_command_line(self) -> None:
        secret = "it's a secret"
        shown = format_command(["tool", "--value", secret], [secret])
        self.assertNotIn("secret", shown.replace("<redacted>", ""))

    def test_credentials_in_any_url_scheme_are_scrubbed(self) -> None:
        for url in ("ftp://alice:pw1@example.org/x", "rsync://alice:pw1@example.org/gentoo", "socks5://alice:pw1@127.0.0.1:1080", "git+https://alice:pw1@example.org/r.git", "HTTPS://alice:pw1@example.org"):
            with self.subTest(url=url):
                clean = redact_text(f"fetching {url} now")
                self.assertNotIn("pw1", clean)
                self.assertIn("<redacted>@", clean)
                self.assertIn(url.split("@", 1)[1].split("/")[0], clean)  # the host stays readable

    def test_a_raw_at_sign_inside_a_url_password_does_not_leak_its_tail(self) -> None:
        clean = redact_text("proxy http://alice:p@ss@proxy.lan:3128/path ok")
        self.assertNotIn("ss@", clean)
        self.assertIn("proxy.lan:3128/path", clean)

    def test_plain_user_at_host_is_left_alone(self) -> None:
        self.assertEqual(redact_text("scp root@example.org:/x"), "scp root@example.org:/x")
        self.assertEqual(redact_text("rsync://git@example.org/repo"), "rsync://git@example.org/repo")

    def test_key_value_forms_are_scrubbed(self) -> None:
        for text in ("password=hunter2xx", "PASSWORD: hunter2xx", "wifi_password = 'hunter2xx'", 'psk="hunter2xx"', "--passphrase hunter2xx", "--token=hunter2xx", '{"luks_passphrase": "hunter2xx"}', "root_password:hunter2xx"):
            with self.subTest(text=text):
                self.assertNotIn("hunter2xx", redact_text(text))

    def test_ordinary_words_that_merely_mention_a_password_are_kept(self) -> None:
        text = "passwd: password updated successfully; Authentication token manipulation error"
        self.assertEqual(redact_text(text), text)

    def test_the_longer_of_two_overlapping_secrets_is_replaced_whole(self) -> None:
        clean = redact_text("value=abcdef123456", ["abcdef", "abcdef123456"])
        self.assertNotIn("123456", clean)


if __name__ == "__main__":
    unittest.main()
