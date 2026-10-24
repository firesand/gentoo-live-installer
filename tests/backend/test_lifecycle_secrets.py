"""S13: a resume notices a changed secret instead of silently keeping the old one."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd.install import CheckpointStore, InstallError, Installer
from gentoo_installerd.storage import LayoutEntry

from tests.backend.helpers import FakeInspector, FakeRunner, manifest


class CheckpointSecretTests(unittest.TestCase):
    def store(self, path, **secrets):
        groups = {"luks": "", "accounts": "user-password\0root-password", "wifi": ""}
        groups.update(secrets)
        return CheckpointStore(path, "fingerprint", groups)

    def test_digests_are_stored_salted_and_never_the_secret_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "checkpoints.json")
            self.store(path, luks="luks-passphrase").complete("storage")
            text = path.read_text()
            for secret in ("luks-passphrase", "user-password", "root-password"):
                self.assertNotIn(secret, text)
            record = json.loads(text)["secrets"]
            self.assertEqual(set(record), {"salt", "luks", "accounts"})
            self.assertEqual(len(bytes.fromhex(record["salt"])), 16)
            self.assertEqual(json.loads(text)["version"], 1)

    def test_the_salt_is_random_per_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first, second = Path(temporary, "a.json"), Path(temporary, "b.json")
            self.store(first).complete("storage")
            self.store(second).complete("storage")
            self.assertNotEqual(json.loads(first.read_text())["secrets"]["salt"], json.loads(second.read_text())["secrets"]["salt"])

    def test_changed_account_password_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "checkpoints.json")
            first = self.store(path)
            first.complete("storage")
            first.complete("target")
            changed = self.store(path, accounts="new-user-password\0root-password")
            changed.load()
            self.assertEqual(changed.changed_secrets, {"accounts"})

    def test_unchanged_secrets_and_legacy_checkpoints_resume_normally(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "checkpoints.json")
            self.store(path).complete("storage")
            same = self.store(path)
            same.load()
            self.assertEqual(same.changed_secrets, set())
            legacy = Path(temporary, "legacy.json")
            legacy.write_text(json.dumps({"version": 1, "fingerprint": "fingerprint", "completed": ["storage"]}))
            old = self.store(legacy)
            old.load()
            self.assertEqual(old.changed_secrets, set())
            self.assertEqual(old.completed, {"storage"})

    def test_a_pending_secret_change_survives_stages_that_complete_before_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "checkpoints.json")
            first = self.store(path)
            first.complete("storage")
            first.complete("target")
            resumed = self.store(path, accounts="corrected\0root-password")
            resumed.load()
            resumed.completed.discard("target")
            resumed.complete("kernel")  # an earlier stage finishing must not "forget" the change
            again = self.store(path, accounts="corrected\0root-password")
            again.load()
            self.assertEqual(again.changed_secrets, {"accounts"})
            again.complete("target")
            final = self.store(path, accounts="corrected\0root-password")
            final.load()
            self.assertEqual(final.changed_secrets, set())

    def test_unreadable_digest_data_is_an_error_not_a_silent_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "checkpoints.json")
            path.write_text(json.dumps({"version": 1, "fingerprint": "fingerprint", "completed": [], "secrets": {"salt": "not-hex"}}))
            with self.assertRaises(InstallError):
                self.store(path).load()


class ResumeReconciliationTests(unittest.TestCase):
    def installer(self, temporary: str, m=None, **kwargs) -> Installer:
        return Installer(m or manifest(), runner=FakeRunner(), target_root=Path(temporary, "t"), state_dir=Path(temporary, "s"), inspector=FakeInspector(), **kwargs)

    def first_run(self, temporary: str, m) -> None:
        first = self.installer(temporary, m)
        first.checkpoints.complete("storage")
        first.checkpoints.complete("target")
        first._log_file.close()

    def test_a_changed_account_password_repeats_the_target_stage_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.first_run(temporary, manifest())
            changed = manifest()
            changed.accounts.password = "a-better-password"
            resumed = self.installer(temporary, changed, resume=True)
            resumed.checkpoints.load()
            resumed._reconcile_secrets()
            self.assertNotIn("target", resumed.checkpoints.completed)
            self.assertIn("storage", resumed.checkpoints.completed)
            resumed._log_file.close()

    def test_a_changed_luks_passphrase_refuses_to_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            m = manifest()
            m.storage.luks = True
            m.storage.luks_passphrase = "original passphrase"
            self.first_run(temporary, m)
            different = manifest()
            different.storage.luks = True
            different.storage.luks_passphrase = "different passphrase"
            events = []
            resumed = self.installer(temporary, different, resume=True, emit=events.append)
            with patch.object(resumed.storage, "restore") as restore:
                self.assertFalse(resumed.run())
                restore.assert_not_called()
            error = next(event for event in events if event.type == "error")
            self.assertIn("LUKS passphrase differs", error.error)
            self.assertNotIn("different passphrase", error.to_json())

    def test_a_resume_with_unchanged_secrets_goes_ahead(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.first_run(temporary, manifest())
            resumed = self.installer(temporary, manifest(), resume=True)
            resumed.checkpoints.load()
            resumed._reconcile_secrets()
            self.assertIn("target", resumed.checkpoints.completed)
            resumed._log_file.close()

    def test_the_checkpoint_fingerprint_covers_which_secrets_are_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plain = self.installer(temporary, manifest())
            encrypted = manifest()
            encrypted.storage.luks = True
            encrypted.storage.luks_passphrase = "a long passphrase"
            other = self.installer(temporary, encrypted)
            self.assertNotEqual(plain.checkpoints.fingerprint, other.checkpoints.fingerprint)
            plain._log_file.close()
            other._log_file.close()

    def test_dead_mount_verifier_is_gone(self) -> None:
        self.assertFalse(hasattr(Installer, "_verify_mounts"))


if __name__ == "__main__":
    unittest.main()
