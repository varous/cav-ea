"""Read-only Chat retrieval with a durable transactional outbox.

Runs either on a host (under a private runtime directory) or as a Cloud Run job
(see cloud_runner.py). The code implements no Chat send, reply, delete or
read-state operation. Raw records are deduplicated by resource name and are only
checkpointed together with the durable transaction that stores them.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys

import requests

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
]


def runtime_root():
    if os.environ.get("COLLECTOR_RUNTIME"):
        return pathlib.Path(os.environ["COLLECTOR_RUNTIME"])
    return pathlib.Path(os.path.expanduser("~")) / ".ea-collector"


ROOT = runtime_root()


def owner_email():
    value = os.environ.get("OWNER_EMAIL", "").strip()
    if not value:
        raise RuntimeError("OWNER_EMAIL_REQUIRED")
    return value


def folder_name():
    return os.environ.get("COLLECTOR_FOLDER_NAME", "Chat intake")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def persist():
    """The cloud runner replaces this with an authoritative durable snapshot."""
    pass


@contextlib.contextmanager
def exclusive_run():
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "collector.lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("COLLECTOR_ALREADY_RUNNING") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def db():
    c = sqlite3.connect(ROOT / "intake.db")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    c.executescript(
        "CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY,v TEXT);"
        "CREATE TABLE IF NOT EXISTS raw(id TEXT PRIMARY KEY,source TEXT,body TEXT,delivered INTEGER DEFAULT 0);"
    )
    return c


def state(c, k, default=None):
    r = c.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def put(c, k, v):
    c.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (k, v))


def auth(delivery=False):
    p = ROOT / ("drive-token.json" if delivery else "google-token.json")
    if not p.exists():
        raise RuntimeError("AUTHENTICATION_REQUIRED")
    a = Credentials.from_authorized_user_file(p)
    if not a.valid:
        a.refresh(Request())
        temp = p.with_suffix(".tmp")
        temp.write_text(a.to_json())
        temp.replace(p)
    return a


def get(a, url, params=None):
    r = requests.get(url, headers={"Authorization": "Bearer " + a.token}, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def pages(a, url, key, params=None):
    p = dict(params or {}, pageSize=1000)
    while True:
        r = get(a, url, p)
        yield from r.get(key, [])
        token = r.get("nextPageToken")
        if not token:
            break
        p["pageToken"] = token


def verify(a):
    profile = get(a, "https://openidconnect.googleapis.com/v1/userinfo")
    if profile.get("email") != owner_email():
        raise RuntimeError("WRONG_GOOGLE_ACCOUNT")
    if profile.get("email_verified") is not True:
        raise RuntimeError("GOOGLE_EMAIL_NOT_VERIFIED")


def delivery_auth():
    a = auth(True)
    verify(a)
    return a


def initialize_delivery():
    a = delivery_auth()
    c = db()
    if state(c, "drive_folder"):
        return state(c, "drive_folder")
    folder_id = get(a, "https://www.googleapis.com/drive/v3/files/generateIds", {"count": 1})["ids"][0]
    # Persist the planned identity before issuing the write, for crash-safe retries.
    with c:
        put(c, "drive_folder", folder_id)
    ensure_folder(a, folder_id)
    return folder_id


def ensure_folder(a, folder_id):
    r = requests.get(
        "https://www.googleapis.com/drive/v3/files/" + folder_id,
        headers={"Authorization": "Bearer " + a.token},
        params={"fields": "id,mimeType,shared"},
        timeout=60,
    )
    if r.status_code == 404:
        r = requests.post(
            "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": "Bearer " + a.token},
            json={"id": folder_id, "name": folder_name(), "mimeType": "application/vnd.google-apps.folder"},
            timeout=60,
        )
    r.raise_for_status()
    return folder_id


def deliver():
    import drive_delivery

    drive_delivery.deliver(sys.modules[__name__])


def run():
    a = auth()
    verify(a)
    c = db()
    activation = state(c, "activation")
    if not activation:
        raise RuntimeError("ACTIVATION_REQUIRED")
    end = now()
    coverage = []
    for space in pages(a, "https://chat.googleapis.com/v1/spaces", "spaces"):
        name = space["name"]
        start = state(c, name, activation)
        try:
            # All pages commit together: failures cannot advance a checkpoint.
            with c:
                for message in pages(
                    a,
                    "https://chat.googleapis.com/v1/" + name + "/messages",
                    "messages",
                    {"filter": f'createTime > "{start}" AND createTime < "{end}"', "orderBy": "createTime ASC"},
                ):
                    # Resource names are the deduplication key.
                    c.execute(
                        "INSERT OR IGNORE INTO raw(id,source,body) VALUES (?,?,?)",
                        (message["name"], "google_chat", json.dumps(message)),
                    )
                # Leave a one-second overlap at every boundary; resource IDs deduplicate.
                put(
                    c,
                    name,
                    (dt.datetime.fromisoformat(end.replace("Z", "+00:00")) - dt.timedelta(seconds=1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                )
            coverage.append({"space": name, "type": space.get("spaceType"), "status": "retrieved"})
        except Exception as e:
            c.rollback()
            coverage.append(
                {
                    "space": name,
                    "type": space.get("spaceType"),
                    "status": "inaccessible_or_failed",
                    "error": type(e).__name__,
                    "http_status": getattr(getattr(e, "response", None), "status_code", None),
                }
            )
    (ROOT / "coverage.json").write_text(json.dumps({"time": end, "spaces": coverage}, indent=2))
    persist()
    deliver()
    if any(x["status"] != "retrieved" for x in coverage):
        raise RuntimeError("PARTIAL_COVERAGE")
    with c:
        put(c, "last_success", end)
    (ROOT / "status.json").write_text(
        json.dumps(
            {
                "time": now(),
                "status": "SUCCESS",
                "last_success": end,
                "raw_records": c.execute("SELECT COUNT(*) FROM raw").fetchone()[0],
                "delivered_records": c.execute("SELECT COUNT(*) FROM raw WHERE delivered=1").fetchone()[0],
                "conversation_count": len(coverage),
            }
        )
    )
    persist()


def main():
    p = argparse.ArgumentParser(description="Read-only Google Chat intake collector.")
    p.add_argument("command", choices=["authenticate", "authenticate-drive", "setup-delivery", "deliver", "activate", "run", "status"])
    p.add_argument("--client")
    p.add_argument("--backfill-from")
    args = p.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    if args.command in ("authenticate", "authenticate-drive"):
        delivery = args.command == "authenticate-drive"
        scopes = (
            ["openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/drive.file"]
            if delivery
            else SCOPES
        )
        try:
            a = InstalledAppFlow.from_client_secrets_file(args.client, scopes).run_local_server(
                port=0,
                login_hint=owner_email(),
                access_type="offline",
                prompt="select_account consent",
                authorization_prompt_message="",
                timeout_seconds=1200,
            )
        except AttributeError as e:
            if "replace" in str(e) and "NoneType" in str(e):
                raise RuntimeError("BROWSER_CALLBACK_TIMEOUT") from None
            raise
        verify(a)
        (ROOT / ("drive-token.json" if delivery else "google-token.json")).write_text(a.to_json())
        print("Correct Google account verified")
    elif args.command == "setup-delivery":
        print("Delivery folder: https://drive.google.com/drive/folders/" + initialize_delivery())
    elif args.command == "deliver":
        with exclusive_run():
            deliver()
    elif args.command == "activate":
        a = auth()
        verify(a)
        delivery_auth()
        c = db()
        if not state(c, "drive_folder"):
            raise RuntimeError("DELIVERY_SETUP_REQUIRED")
        with c:
            if args.backfill_from:
                start = dt.datetime.fromisoformat(args.backfill_from)
                if start.tzinfo is None:
                    raise RuntimeError("BACKFILL_TIMEZONE_REQUIRED")
                start = (start.astimezone(dt.timezone.utc) - dt.timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
                for (k,) in c.execute("SELECT k FROM state WHERE k LIKE 'spaces/%'").fetchall():
                    put(c, k, start)
                put(c, "activation", start)
            elif not state(c, "activation"):
                put(c, "activation", now())
        print("Activated; backfill start recorded" if args.backfill_from else "Activated from current time")
    elif args.command == "run":
        with exclusive_run():
            run()
    else:
        c = db()
        print(
            json.dumps(
                {
                    "activated": bool(state(c, "activation")),
                    "last_success": state(c, "last_success"),
                    "raw_records": c.execute("SELECT COUNT(*) FROM raw").fetchone()[0],
                    "delivered_records": c.execute("SELECT COUNT(*) FROM raw WHERE delivered=1").fetchone()[0],
                    "drive_folder": state(c, "drive_folder"),
                }
            )
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Never print provider response bodies, tokens or source content.
        code = str(e) if isinstance(e, RuntimeError) else type(e).__name__
        ROOT.mkdir(parents=True, exist_ok=True)
        (ROOT / "status.json").write_text(
            json.dumps(
                {
                    "time": now(),
                    "status": "ERROR",
                    "code": code,
                    "chat_credential_present": (ROOT / "google-token.json").exists(),
                    "drive_credential_present": (ROOT / "drive-token.json").exists(),
                    "command": sys.argv[1] if len(sys.argv) > 1 else None,
                }
            )
        )
        print("Collector blocked/error: " + code, file=sys.stderr)
        sys.exit(1)
