"""Tests for the OAuth walkthrough script. No browser, no secrets."""
import unittest
from unittest.mock import Mock, patch

from scripts import oauth_setup


class ScopesAndSteps(unittest.TestCase):
    def test_scopes_are_expected(self):
        self.assertIn("https://www.googleapis.com/auth/chat.messages.readonly", oauth_setup.scopes_for("chat"))
        self.assertIn("https://www.googleapis.com/auth/gmail.readonly", oauth_setup.scopes_for("gmail"))
        self.assertIn("https://www.googleapis.com/auth/drive.file", oauth_setup.scopes_for("drive"))
        self.assertIn("https://www.googleapis.com/auth/tasks", oauth_setup.scopes_for("tasks"))
        with self.assertRaises(ValueError):
            oauth_setup.scopes_for("nope")

    def test_console_steps_mention_manual_pieces(self):
        text = " ".join(oauth_setup.console_steps("chat"))
        self.assertIn("Desktop OAuth client", text)
        self.assertIn("Chat app", text)


class StoreSecret(unittest.TestCase):
    def test_store_secret_uses_data_file_and_never_echoes_value(self):
        runner = Mock()
        argv = oauth_setup.store_secret("ea-chat-oauth", "/tmp/u.json", "sample-project", runner=runner)
        self.assertEqual(argv[:5], ["gcloud", "secrets", "versions", "add", "ea-chat-oauth"])
        self.assertIn("--data-file=/tmp/u.json", argv)
        self.assertIn("--project=sample-project", argv)
        runner.assert_called_once()


class DryRun(unittest.TestCase):
    def test_dry_run_runs_no_consent(self):
        with patch.object(oauth_setup, "run_consent") as consent:
            code = oauth_setup.main(["--dry-run", "--kind", "chat", "--secret", "s"])
        self.assertEqual(code, 0)
        consent.assert_not_called()

    def test_verify_prints_status_only(self):
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock(return_value=response)
        status = oauth_setup.verify_maintain("https://example/maintain", token="fake", opener=opener)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
