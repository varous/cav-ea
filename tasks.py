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
import json
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


def is_owner_task(task, owner_name, owner_email):
    """True only when the task's owner is exactly the owner name or owner email."""
    owner = (task.get("owner") or "").strip()
    candidates = {value.strip() for value in (owner_name, owner_email) if value and value.strip()}
    return bool(owner) and owner in candidates


def notes_for(task, owner_email=None):
    parts = []
    base = (task.get("details") or "").strip()
    if base:
        parts.append(base)
    if owner_email:
        parts.append("Assignee: " + owner_email)
    parts.append(marker(task["id"]))
    return "\n".join(parts)


def desired(task, owner_email=None):
    """The Google Tasks fields the bot last intends to hold for this task.

    `assignee` is tracked for owner tasks only. The Tasks API exposes no writable
    assignee field, so it is carried in the notes and in google_synced rather than
    sent as an unknown (rejected) API field.
    """
    want = {
        "title": (task.get("title") or "").strip(),
        "status": "completed" if task.get("status") == "COMPLETED" else "needsAction",
        "due": due_rfc3339(task.get("deadline")),
        "notes": notes_for(task, owner_email),
    }
    if owner_email:
        want["assignee"] = owner_email
    return want


def _api_body(fields):
    """Drop fields the Tasks API does not accept (e.g. assignee)."""
    return {k: v for k, v in fields.items() if k != "assignee" and v is not None}


def mirror_task(session, tasklist_id, state, task_id, owner_name=None, owner_email=None):
    """Create or patch one operating task; record google_task_id + google_synced.

    When owner_name is given, tasks whose owner is not the owner are skipped
    (returns None, no API call).
    """
    task = state["tasks"][task_id]
    is_owner = owner_name is not None and is_owner_task(task, owner_name, owner_email)
    if owner_name is not None and not is_owner:
        return None
    want = desired(task, owner_email if is_owner else None)
    synced = dict(task.get("google_synced") or {})
    google_task_id = task.get("google_task_id")
    created = False
    if not google_task_id:
        existing = find_by_marker(session, tasklist_id, task_id)
        if existing:
            google_task_id = existing["id"]
        else:
            response = session.post(API + "/lists/" + tasklist_id + "/tasks",
                                    json=_api_body(want), timeout=60)
            response.raise_for_status()
            google_task_id = response.json()["id"]
            created = True
    if not created:
        patch = {k: v for k, v in _api_body(want).items() if synced.get(k) != v}
        if patch:
            response = session.patch(API + "/lists/" + tasklist_id + "/tasks/" + google_task_id,
                                     json=patch, timeout=60)
            response.raise_for_status()
    task["google_task_id"] = google_task_id
    task["google_synced"] = dict(want, updated=now())
    return google_task_id


def _error_code(error):
    return str(error) if isinstance(error, RuntimeError) and str(error).isupper() else type(error).__name__


def mirror_all(session, tasklist_id, state, owner_name, owner_email):
    """Backfill every owner task to Google Tasks. Never raises.

    Returns a list of (task_id, "synced"|"error"); per-task failures are recorded
    as tasks_sync_error and retried next run.
    """
    results = []
    for task_id, task in state["tasks"].items():
        if not is_owner_task(task, owner_name, owner_email):
            continue
        try:
            mirror_task(session, tasklist_id, state, task_id, owner_name, owner_email)
            task.pop("tasks_sync_error", None)
            results.append((task_id, "synced"))
        except Exception as error:
            code = _error_code(error)
            task["tasks_sync_error"] = code
            print(json.dumps({"status": "TASKS_SYNC_ERROR", "code": code, "task_id": task_id,
                              "time": now()}), flush=True)
            results.append((task_id, "error"))
    return results


# --- read-back (Tasks -> bot) -------------------------------------------------

_MARKER_RE = re.compile(r"\[ea-task:(T-[A-Za-z0-9]+)\]")


def marker_id(notes):
    match = _MARKER_RE.search(notes or "")
    return match.group(1) if match else None


def _strip_marker(notes):
    return _MARKER_RE.sub("", notes or "").rstrip()


def _date(value):
    text = (value or "").strip()
    return text[:10] if re.match(r"\d{4}-\d{2}-\d{2}", text) else None


def snapshot(google_task):
    """The Google-side state stored in google_synced for loop-safe diffing."""
    return {
        "title": (google_task.get("title") or "").strip(),
        "status": google_task.get("status") or "needsAction",
        "due": _date(google_task.get("due")),
        "notes": google_task.get("notes") or "",
        "updated": google_task.get("updated"),
    }


def _task_changes(task_id, synced, google_task):
    """Diff a Google task against the bot's last write. Empty when unchanged (no echo)."""
    snap = snapshot(google_task)
    previous = (synced or {}).get("status")
    stamp = google_task.get("updated") or ""
    changes = []
    # Status: the bot only mirrors completed/needsAction, so any divergence is the owner.
    if snap["status"] == "completed" and previous != "completed":
        changes.append(("status", "COMPLETED", "confirmed by owner via Google Tasks",
                        "Google Tasks completed " + (google_task.get("completed") or stamp)))
    elif snap["status"] != "completed" and previous == "completed":
        changes.append(("status", "OUTSTANDING", "reopened by owner via Google Tasks",
                        "Google Tasks reopened " + stamp))
    if synced:
        if snap["title"] != (synced.get("title") or ""):
            changes.append(("title", snap["title"], "owner edited the title in Google Tasks",
                            "Google Tasks edited " + stamp))
        if snap["due"] != _date(synced.get("due")):
            changes.append(("deadline", snap["due"] or "UNKNOWN",
                            "owner changed the due date in Google Tasks", "Google Tasks edited " + stamp))
        if snap["notes"] != (synced.get("notes") or ""):
            changes.append(("details", _strip_marker(snap["notes"]),
                            "owner edited notes in Google Tasks", "Google Tasks edited " + stamp))
    return changes


def sync_from_tasks(session, tasklist_id, state, owner_name):
    """Read-back: return deterministic changes for completion, reopen, edits and imports.

    Never returns a change for the bot's own writes: each task is diffed against the
    google_synced snapshot, and the snapshot is refreshed after applying.
    """
    changes = []
    for google_task in _items(session, API + "/lists/" + tasklist_id + "/tasks", "items",
                              {"showCompleted": "true", "showHidden": "true"}):
        google_task_id = google_task.get("id")
        task_id = marker_id(google_task.get("notes"))
        if task_id and task_id in state["tasks"]:
            task = state["tasks"][task_id]
            task["google_task_id"] = task.get("google_task_id") or google_task_id
            diffs = _task_changes(task_id, task.get("google_synced"), google_task)
            for index, (field, value, reason, evidence) in enumerate(diffs):
                change = {"kind": "update", "task_id": task_id, "field": field, "value": value,
                          "reason": reason, "evidence": evidence, "google_task_id": google_task_id}
                if index == len(diffs) - 1:
                    change["google_synced"] = snapshot(google_task)
                changes.append(change)
        elif not task_id:
            # No marker: the owner created it directly in Tasks -> import, then mirror back.
            snap = snapshot(google_task)
            changes.append({
                "kind": "create", "title": snap["title"], "details": _strip_marker(snap["notes"]),
                "deadline": snap["due"] or "UNKNOWN",
                "status": "COMPLETED" if snap["status"] == "completed" else "OUTSTANDING",
                "google_task_id": google_task_id, "mirror_back": True,
                "reason": "created in Google Tasks",
                "evidence": "Google Tasks created " + (google_task.get("updated") or ""),
            })
    return changes
