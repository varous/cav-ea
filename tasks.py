"""Google Tasks one-way mirror (bot -> Tasks).

This module writes the operating task list to a dedicated Google Tasks list. It
is intentionally one-way: the Tasks -> bot read-back (completion, reopen,
import) is a later phase. Read/write uses the `tasks` OAuth scope.

The mirror is best-effort. Callers must treat a failure as a per-task sync
error (retried next run) rather than an operating failure. Creates are
idempotent: a hidden marker in each task's notes identifies the operating task,
so a retry after a lost acknowledgement adopts the existing Google task instead
of creating a duplicate.
"""
import datetime as dt
import re

TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
API = "https://tasks.googleapis.com/tasks/v1"
_MARKER_PREFIX = "[ea-task:"


def marker(task_id):
    return _MARKER_PREFIX + str(task_id) + "]"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _items(session, url, key, params=None):
    page = dict(params or {})
    while True:
        response = session.get(url, params=page, timeout=60)
        response.raise_for_status()
        data = response.json()
        yield from data.get(key, [])
        next_token = data.get("nextPageToken")
        if not next_token:
            break
        page["pageToken"] = next_token


def list_tasklists(session):
    return list(_items(session, API + "/users/@me/lists", "items"))


def ensure_tasklist(session, title, existing_id=None):
    """Return the id of the tasklist titled `title`, creating it when absent."""
    if existing_id:
        return existing_id
    for tasklist in list_tasklists(session):
        if (tasklist.get("title") or "").strip() == (title or "").strip():
            return tasklist["id"]
    response = session.post(API + "/users/@me/lists", json={"title": title}, timeout=60)
    response.raise_for_status()
    return response.json()["id"]


def find_by_marker(session, tasklist_id, task_id):
    want = marker(task_id)
    for task in _items(session, API + "/lists/" + tasklist_id + "/tasks", "items",
                       {"showCompleted": "true", "showHidden": "true"}):
        if want in (task.get("notes") or ""):
            return task
    return None


def due_rfc3339(deadline):
    """Google Tasks due: RFC3339 at midnight UTC, or None when the date is unknown."""
    value = (deadline or "").strip()
    if not value or value.upper() in ("UNKNOWN", "NOT YET ASSIGNED"):
        return None
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}T00:00:00.000Z"


def notes_for(task):
    base = (task.get("details") or "").strip()
    tag = marker(task["id"])
    return (base + "\n" if base else "") + tag


def desired(task):
    """The Google Tasks fields the bot last intends to hold for this task."""
    return {
        "title": (task.get("title") or "").strip(),
        "status": "completed" if task.get("status") == "COMPLETED" else "needsAction",
        "due": due_rfc3339(task.get("deadline")),
        "notes": notes_for(task),
    }


def mirror_task(session, tasklist_id, state, task_id):
    """Create or patch one operating task; record google_task_id + google_synced."""
    task = state["tasks"][task_id]
    want = desired(task)
    synced = dict(task.get("google_synced") or {})
    google_task_id = task.get("google_task_id")
    created = False
    if not google_task_id:
        existing = find_by_marker(session, tasklist_id, task_id)
        if existing:
            google_task_id = existing["id"]
        else:
            body = {k: v for k, v in want.items() if v is not None}
            response = session.post(API + "/lists/" + tasklist_id + "/tasks", json=body, timeout=60)
            response.raise_for_status()
            google_task_id = response.json()["id"]
            created = True
    if not created:
        patch = {k: v for k, v in want.items() if synced.get(k) != v}
        if patch:
            response = session.patch(API + "/lists/" + tasklist_id + "/tasks/" + google_task_id,
                                     json=patch, timeout=60)
            response.raise_for_status()
    task["google_task_id"] = google_task_id
    task["google_synced"] = dict(want, updated=now())
    return google_task_id
