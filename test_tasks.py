"""Google Tasks mirror tests (one-way, bot -> Tasks). No network."""
import unittest

import tasks
from operating import apply_changes, import_ledger

LEDGER = """| ID | Project / task | Owner | Deadline | Dependency |
|---|---|---|---|---|
| T-101 | Sample task one | PERSON_A | 2026-10-05 | dep |
### C-001 — x
"""


def state():
    return import_ledger(LEDGER)


class Resp:
    def __init__(self, payload=None):
        self._payload = payload or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class Session:
    def __init__(self, list_items=None):
        self.calls = []
        self.list_items = list_items or []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        return Resp({"items": self.list_items})

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        return Resp({"id": "gt-new"})

    def patch(self, url, json=None, timeout=None):
        self.calls.append(("PATCH", url, json))
        return Resp({})


class Desired(unittest.TestCase):
    def test_status_due_and_notes_marker(self):
        task = {"id": "T-101", "title": "Call venue", "status": "OUTSTANDING",
                "deadline": "2026-10-05", "details": "Confirm load-in"}
        want = tasks.desired(task)
        self.assertEqual(want["title"], "Call venue")
        self.assertEqual(want["status"], "needsAction")
        self.assertEqual(want["due"], "2026-10-05T00:00:00.000Z")
        self.assertIn("Confirm load-in", want["notes"])
        self.assertIn(tasks.marker("T-101"), want["notes"])

    def test_completed_maps_to_completed(self):
        task = {"id": "T-101", "title": "x", "status": "COMPLETED", "deadline": "UNKNOWN", "details": ""}
        want = tasks.desired(task)
        self.assertEqual(want["status"], "completed")
        self.assertIsNone(want["due"])

    def test_due_only_for_absolute_dates(self):
        self.assertIsNone(tasks.due_rfc3339("today"))
        self.assertIsNone(tasks.due_rfc3339("UNKNOWN"))
        self.assertEqual(tasks.due_rfc3339("2026-10-05, time unknown"), "2026-10-05T00:00:00.000Z")


class Mirror(unittest.TestCase):
    def test_create_records_snapshot(self):
        s = state()
        session = Session(list_items=[])
        gid = tasks.mirror_task(session, "list-1", s, "T-101")
        self.assertEqual(gid, "gt-new")
        posts = [c for c in session.calls if c[0] == "POST" and c[1].endswith("/tasks")]
        self.assertEqual(len(posts), 1)
        task = s["tasks"]["T-101"]
        self.assertEqual(task["google_task_id"], "gt-new")
        self.assertEqual(task["google_synced"]["status"], "needsAction")
        self.assertEqual(task["google_synced"]["due"], "2026-10-05T00:00:00.000Z")
        self.assertIn("updated", task["google_synced"])

    def test_patch_only_changed_fields(self):
        s = state()
        task = s["tasks"]["T-101"]
        task["google_task_id"] = "gt-1"
        task["google_synced"] = dict(tasks.desired(task), updated="2026-10-01T00:00:00Z")
        task["title"] = "Call venue (renamed)"
        session = Session()
        tasks.mirror_task(session, "list-1", s, "T-101")
        patches = [c for c in session.calls if c[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0][2], {"title": "Call venue (renamed)"})
        self.assertEqual(task["google_synced"]["title"], "Call venue (renamed)")

    def test_completion_then_reopen(self):
        s = state()
        task = s["tasks"]["T-101"]
        task["google_task_id"] = "gt-1"
        task["google_synced"] = dict(tasks.desired(task), updated="2026-10-01T00:00:00Z")
        task["status"] = "COMPLETED"
        session = Session()
        tasks.mirror_task(session, "list-1", s, "T-101")
        self.assertEqual([c[2] for c in session.calls if c[0] == "PATCH"], [{"status": "completed"}])
        task["status"] = "OUTSTANDING"
        session2 = Session()
        tasks.mirror_task(session2, "list-1", s, "T-101")
        self.assertEqual([c[2] for c in session2.calls if c[0] == "PATCH"], [{"status": "needsAction"}])

    def test_idempotent_create_adopts_existing_marker(self):
        s = state()
        existing = {"id": "gt-existing", "notes": "stuff " + tasks.marker("T-101")}
        session = Session(list_items=[existing])
        gid = tasks.mirror_task(session, "list-1", s, "T-101")
        self.assertEqual(gid, "gt-existing")
        self.assertFalse([c for c in session.calls if c[0] == "POST"])
        self.assertEqual(s["tasks"]["T-101"]["google_task_id"], "gt-existing")

    def test_ensure_tasklist_reuses_by_title(self):
        session = Session(list_items=[{"id": "list-existing", "title": "my-ea"}])
        self.assertEqual(tasks.ensure_tasklist(session, "my-ea"), "list-existing")
        self.assertFalse([c for c in session.calls if c[0] == "POST"])


class FailureIsolation(unittest.TestCase):
    def test_tasks_failure_does_not_fail_change(self):
        s = state()
        changes = [{"kind": "update", "task_id": "T-101", "field": "status", "value": "COMPLETED",
                    "evidence_quote": "T-101 done", "reason": "done"}]

        def boom(state, task_id):
            raise RuntimeError("TASKS_UNAVAILABLE")

        applied = apply_changes(s, changes, "owner message", "Alex", boom)
        self.assertEqual(applied["tasks"]["T-101"]["status"], "COMPLETED")
        self.assertEqual(applied["tasks"]["T-101"]["tasks_sync_error"], "TASKS_UNAVAILABLE")
        self.assertEqual(len(applied["changes"]), 1)

    def test_successful_mirror_clears_error(self):
        s = state()
        changes = [{"kind": "update", "task_id": "T-101", "field": "status", "value": "COMPLETED",
                    "evidence_quote": "T-101 done", "reason": "done"}]
        applied = apply_changes(s, changes, "owner message", "Alex", lambda state, tid: None)
        self.assertNotIn("tasks_sync_error", applied["tasks"]["T-101"])


class Schema(unittest.TestCase):
    def test_import_carries_google_fields(self):
        task = state()["tasks"]["T-101"]
        self.assertIsNone(task["google_task_id"])
        self.assertIsNone(task["google_synced"])

    def test_create_carries_google_fields(self):
        s = state()
        created = apply_changes(s, [{"kind": "create", "task_id": None, "field": "title",
                                     "value": "New to-do", "evidence_quote": "add new to-do",
                                     "reason": "new", "new_task": {}}], "owner message")
        task = created["tasks"]["T-102"]
        self.assertIsNone(task["google_task_id"])
        self.assertIsNone(task["google_synced"])


if __name__ == "__main__":
    unittest.main()
