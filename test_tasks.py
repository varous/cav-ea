"""Google Tasks mirror tests (one-way, bot -> Tasks). No network."""
import unittest

import tasks
from operating import apply_changes, apply_tasks_sync, import_ledger

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


class ReadBack(unittest.TestCase):
    def synced_state(self):
        s = state()
        task = s["tasks"]["T-101"]
        task["google_task_id"] = "gt-1"
        task["google_synced"] = {"title": task["title"], "status": "needsAction",
                                 "due": tasks._date("2026-10-05"), "notes": tasks.notes_for(task),
                                 "updated": "2026-10-01T00:00:00.000Z"}
        return s

    def google(self, **over):
        task = {"id": "gt-1", "title": "Sample task one", "status": "needsAction",
                "due": "2026-10-05T00:00:00.000Z", "notes": "[ea-task:T-101]",
                "updated": "2026-10-07T10:00:00.000Z"}
        task.update(over)
        return task

    def test_completion_from_tasks(self):
        s = self.synced_state()
        session = Session(list_items=[self.google(status="completed", completed="2026-10-07T10:00:00.000Z")])
        changes = tasks.sync_from_tasks(session, "list-1", s, "Alex Owner")
        statuses = [c for c in changes if c["field"] == "status"]
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["value"], "COMPLETED")
        self.assertIn("confirmed by owner via Google Tasks", statuses[0]["reason"])
        applied = apply_tasks_sync(s, changes, "Alex Owner")
        self.assertEqual(applied["tasks"]["T-101"]["status"], "COMPLETED")
        self.assertEqual(applied["changes"][-1]["source"], "Google Tasks")
        self.assertIn("Google Tasks completed", applied["changes"][-1]["evidence_quote"])

    def test_reopen_reverts_and_preserves_history(self):
        s = self.synced_state()
        s["tasks"]["T-101"]["status"] = "COMPLETED"
        s["tasks"]["T-101"]["google_synced"]["status"] = "completed"
        s["changes"].append({"commit": "C-002", "time": "2026-10-06T00:00:00Z", "kind": "update",
                             "task_id": "T-101", "field": "status", "old": "OUTSTANDING", "new": "COMPLETED",
                             "reason": "done", "evidence_quote": "T-101 done", "authority": "x",
                             "source": "chat", "validation": False})
        session = Session(list_items=[self.google(status="needsAction")])
        changes = tasks.sync_from_tasks(session, "list-1", s, "Alex Owner")
        self.assertEqual([c["value"] for c in changes if c["field"] == "status"], ["OUTSTANDING"])
        applied = apply_tasks_sync(s, changes, "Alex Owner")
        self.assertEqual(applied["tasks"]["T-101"]["status"], "OUTSTANDING")
        history = [c for c in applied["changes"] if c["field"] == "status"]
        self.assertTrue(any(c["new"] == "COMPLETED" for c in history))
        self.assertTrue(any(c["new"] == "OUTSTANDING" for c in history))

    def test_echo_loop_prevention_no_change(self):
        s = self.synced_state()
        session = Session(list_items=[self.google()])
        self.assertEqual(tasks.sync_from_tasks(session, "list-1", s, "Alex Owner"), [])

    def test_idempotent_rerun_is_noop(self):
        s = self.synced_state()
        session = Session(list_items=[self.google(status="completed", completed="2026-10-07T10:00:00.000Z")])
        first = tasks.sync_from_tasks(session, "list-1", s, "Alex Owner")
        applied = apply_tasks_sync(s, first, "Alex Owner")
        second = tasks.sync_from_tasks(session, "list-1", applied, "Alex Owner")
        self.assertEqual(second, [])

    def test_owner_created_task_is_imported(self):
        s = state()
        created = {"id": "gt-new", "title": "Buy cables", "notes": "owner note",
                   "due": "2026-10-09T00:00:00.000Z", "status": "needsAction",
                   "updated": "2026-10-07T11:00:00.000Z"}
        session = Session(list_items=[created])
        changes = tasks.sync_from_tasks(session, "list-1", s, "Alex Owner")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["kind"], "create")
        self.assertTrue(changes[0]["mirror_back"])
        self.assertEqual(changes[0]["google_task_id"], "gt-new")
        applied = apply_tasks_sync(s, changes, "Alex Owner")
        new_id = changes[0].get("task_id") or applied["changes"][-1]["task_id"]
        task = applied["tasks"][new_id]
        self.assertEqual(task["title"], "Buy cables")
        self.assertEqual(task["details"], "owner note")
        self.assertEqual(task["deadline"], "2026-10-09")
        self.assertEqual(task["owner"], "Alex Owner")
        self.assertEqual(task["source"], "created in Google Tasks")
        self.assertEqual(task["google_task_id"], "gt-new")

    def test_owner_title_edit_is_applied(self):
        s = self.synced_state()
        session = Session(list_items=[self.google(title="Sample task one (renamed)")])
        changes = tasks.sync_from_tasks(session, "list-1", s, "Alex Owner")
        self.assertEqual([c for c in changes if c["field"] == "title"][0]["value"], "Sample task one (renamed)")


class TasksUnavailable(unittest.TestCase):
    def test_sync_error_recorded_not_raised(self):
        from unittest.mock import Mock
        from app import Runtime
        runtime = object.__new__(Runtime)
        runtime.store = Mock()
        runtime.intake_chat = lambda state: None
        runtime.intake_gmail = lambda state: None
        runtime.intake_tasks = lambda state: (_ for _ in ()).throw(RuntimeError("TASKS_UNAVAILABLE"))
        state, _ = runtime.intake({"coverage": {}}, 0)
        self.assertEqual(state["coverage"]["tasks"]["status"], "ERROR")
        self.assertEqual(state["coverage"]["tasks"]["code"], "TASKS_UNAVAILABLE")


LEDGER_OWNERS = """| ID | Project / task | Owner | Deadline | Dependency |
|---|---|---|---|---|
| T-201 | Owner task | Alex Owner | 2026-10-05 | dep |
| T-202 | Other person | PERSON_B | 2026-10-06 | dep |
| T-203 | Co-owned | Alex Owner + PERSON_B | 2026-10-07 | dep |
| T-204 | Team task | Audio Team | UNKNOWN | dep |
| T-205 | Unassigned | NOT YET ASSIGNED | UNKNOWN | dep |
### C-001 — x
"""
OWNER_NAME = "Alex Owner"
OWNER_EMAIL = "alex@example.com"


def owner_state():
    return import_ledger(LEDGER_OWNERS)


class OwnerFilter(unittest.TestCase):
    def test_exact_match_only(self):
        s = owner_state()
        self.assertTrue(tasks.is_owner_task(s["tasks"]["T-201"], OWNER_NAME, OWNER_EMAIL))
        self.assertFalse(tasks.is_owner_task(s["tasks"]["T-202"], OWNER_NAME, OWNER_EMAIL))
        self.assertFalse(tasks.is_owner_task(s["tasks"]["T-203"], OWNER_NAME, OWNER_EMAIL))
        self.assertFalse(tasks.is_owner_task(s["tasks"]["T-204"], OWNER_NAME, OWNER_EMAIL))
        self.assertFalse(tasks.is_owner_task(s["tasks"]["T-205"], OWNER_NAME, OWNER_EMAIL))

    def test_assignee_only_for_owner(self):
        s = owner_state()
        want = tasks.desired(s["tasks"]["T-201"], OWNER_EMAIL)
        self.assertEqual(want["assignee"], OWNER_EMAIL)
        self.assertIn("Assignee: " + OWNER_EMAIL, want["notes"])

    def test_mirror_task_skips_non_owner(self):
        s = owner_state()
        session = Session()
        self.assertIsNone(tasks.mirror_task(session, "list-1", s, "T-202", OWNER_NAME, OWNER_EMAIL))
        self.assertEqual(session.calls, [])


class Backfill(unittest.TestCase):
    def test_creates_owner_tasks_once(self):
        s = owner_state()
        session = Session(list_items=[])
        first = tasks.mirror_all(session, "list-1", s, OWNER_NAME, OWNER_EMAIL)
        self.assertEqual([task_id for task_id, _ in first], ["T-201"])
        creates = [c for c in session.calls if c[0] == "POST" and c[1].endswith("/tasks")]
        self.assertEqual(len(creates), 1)
        tasks.mirror_all(session, "list-1", s, OWNER_NAME, OWNER_EMAIL)
        creates2 = [c for c in session.calls if c[0] == "POST" and c[1].endswith("/tasks")]
        self.assertEqual(len(creates2), 1)
        self.assertEqual(s["tasks"]["T-201"]["google_synced"]["assignee"], OWNER_EMAIL)

    def test_patches_changed_and_skips_unchanged(self):
        s = owner_state()
        task = s["tasks"]["T-201"]
        task["google_task_id"] = "gt-1"
        task["google_synced"] = dict(tasks.desired(task, OWNER_EMAIL), updated="2026-10-01T00:00:00Z")
        session = Session()
        tasks.mirror_all(session, "list-1", s, OWNER_NAME, OWNER_EMAIL)
        self.assertEqual([c for c in session.calls if c[0] in ("POST", "PATCH")], [])
        task["title"] = "Owner task (renamed)"
        session2 = Session()
        tasks.mirror_all(session2, "list-1", s, OWNER_NAME, OWNER_EMAIL)
        patches = [c for c in session2.calls if c[0] == "PATCH"]
        self.assertEqual(patches[0][2], {"title": "Owner task (renamed)"})

    def test_failure_does_not_raise(self):
        class Failing(Session):
            def post(self, url, json=None, timeout=None):
                self.calls.append(("POST", url, json))
                raise RuntimeError("TASKS_UNAVAILABLE")

        s = owner_state()
        results = tasks.mirror_all(Failing(list_items=[]), "list-1", s, OWNER_NAME, OWNER_EMAIL)
        self.assertEqual(results, [("T-201", "error")])
        self.assertEqual(s["tasks"]["T-201"]["tasks_sync_error"], "TASKS_UNAVAILABLE")

    def test_assignee_is_not_sent_as_an_api_field(self):
        s = owner_state()
        session = Session(list_items=[])
        tasks.mirror_all(session, "list-1", s, OWNER_NAME, OWNER_EMAIL)
        body = [c[2] for c in session.calls if c[0] == "POST" and c[1].endswith("/tasks")][0]
        self.assertNotIn("assignee", body)
        self.assertIn("Assignee: " + OWNER_EMAIL, body["notes"])


if __name__ == "__main__":
    unittest.main()
