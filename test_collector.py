"""Collector tests (ported, no network). Adds the collector source dir to sys.path."""
import io
import json
import os
import pathlib
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).parent / "collector"))
import cloud_runner  # noqa: E402
import collector  # noqa: E402
import drive_delivery  # noqa: E402


class CloudControls(unittest.TestCase):
    def test_active_lease_withholds_execution(self):
        c = cloud_runner.CloudState(Mock(), "bucket")
        c.metadata = Mock(return_value={"generation": "10"})
        c.read = Mock(return_value=json.dumps({"expires": 10**12}).encode())
        c.upload = Mock()
        self.assertFalse(c.acquire())
        c.upload.assert_not_called()

    def test_lost_lease_withholds_persistence(self):
        c = cloud_runner.CloudState(Mock(), "bucket")
        c.lock_generation = "10"
        c.metadata = Mock(return_value={"generation": "11"})
        c.upload = Mock()
        with self.assertRaisesRegex(RuntimeError, "CLOUD_LOCK_LOST"):
            c.persist()
        c.upload.assert_not_called()

    def test_snapshot_contains_committed_state_but_no_credentials(self):
        with tempfile.TemporaryDirectory() as d, patch.object(collector, "ROOT", pathlib.Path(d)):
            conn = collector.db()
            with conn:
                collector.put(conn, "spaces/test", "checkpoint")
            (collector.ROOT / "google-token.json").write_text("synthetic-secret")
            c = cloud_runner.CloudState(Mock(), "bucket")
            c.generation = "10"
            c.fence = Mock()
            c.upload = Mock(return_value="11")
            c.persist()
            args = c.upload.call_args.args
            self.assertEqual(args[0], "state.tar.gz")
            self.assertEqual(args[2], "10")
            with tarfile.open(fileobj=io.BytesIO(args[1]), mode="r:gz") as archive:
                self.assertEqual(archive.getnames(), ["intake.db"])
                (collector.ROOT / "restore.db").write_bytes(archive.extractfile("intake.db").read())
            restored = sqlite3.connect(collector.ROOT / "restore.db")
            self.assertEqual(collector.state(restored, "spaces/test"), "checkpoint")
            restored.close()
            conn.close()

    def test_failed_durable_assignment_prevents_drive_upload(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("CREATE TABLE raw(id TEXT PRIMARY KEY,source TEXT,body TEXT,delivered INTEGER DEFAULT 0);")
        conn.execute("INSERT INTO raw(id,source,body) VALUES (?,?,?)", ("spaces/test/messages/1", "google_chat", "{}"))
        conn.commit()
        core = Mock()
        core.db.return_value = conn
        core.state.return_value = "folder"
        core.get.return_value = {"ids": ["file1"]}
        core.persist.side_effect = RuntimeError("CLOUD_STORAGE_UNAVAILABLE")
        with patch.object(drive_delivery.requests, "get") as get, patch.object(drive_delivery.requests, "post") as post:
            with self.assertRaisesRegex(RuntimeError, "CLOUD_STORAGE_UNAVAILABLE"):
                drive_delivery.deliver(core)
            get.assert_not_called()
            post.assert_not_called()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM batches WHERE done=0").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT delivered FROM raw").fetchone()[0], 0)
        conn.close()


class DedupAndIdentity(unittest.TestCase):
    def test_duplicate_message_resource_names_are_ignored(self):
        with tempfile.TemporaryDirectory() as d, patch.object(collector, "ROOT", pathlib.Path(d)):
            conn = collector.db()
            with conn:
                for _ in range(3):
                    conn.execute(
                        "INSERT OR IGNORE INTO raw(id,source,body) VALUES (?,?,?)",
                        ("spaces/test/messages/1", "google_chat", "{}"),
                    )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM raw").fetchone()[0], 1)
            conn.close()

    def test_wrong_owner_is_rejected(self):
        with patch.object(collector, "get", return_value={"email": "other@example.com", "email_verified": True}), \
                patch.dict(os.environ, {"OWNER_EMAIL": "owner@example.com"}):
            with self.assertRaisesRegex(RuntimeError, "WRONG_GOOGLE_ACCOUNT"):
                collector.verify(Mock())

    def test_missing_owner_email_is_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "OWNER_EMAIL_REQUIRED"):
                collector.owner_email()


if __name__ == "__main__":
    unittest.main()
