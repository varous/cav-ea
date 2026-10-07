"""Cloud Run job wrapper: fenced snapshots, durable outbox, sanitized diagnostics.

Restores the authoritative SQLite state from the collector's private bucket,
runs one collection/delivery pass, and publishes a generation-fenced snapshot.
Credentials are copied from Secret Manager into the scratch runtime only.
"""
import io
import json
import os
import sqlite3
import sys
import tarfile
import time
import uuid
from urllib.parse import quote

import google.auth
from google.auth.transport.requests import AuthorizedSession

os.environ.setdefault("COLLECTOR_RUNTIME", "/tmp/ea-collector")
import collector


class CloudState:
    def __init__(self, session, bucket):
        self.session = session
        self.bucket = bucket
        self.generation = None
        self.lock_generation = None
        self.started = time.monotonic()
        self.owner = uuid.uuid4().hex

    def url(self, name):
        return "https://storage.googleapis.com/storage/v1/b/" + self.bucket + "/o/" + quote(name, safe="")

    def metadata(self, name):
        r = self.session.get(self.url(name), timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def upload(self, name, data, generation, content_type="application/octet-stream"):
        r = self.session.post(
            "https://storage.googleapis.com/upload/storage/v1/b/" + self.bucket + "/o",
            params={"uploadType": "media", "name": name, "ifGenerationMatch": generation},
            data=data,
            headers={"Content-Type": content_type},
            timeout=120,
        )
        r.raise_for_status()
        return r.json()["generation"]

    def read(self, name, generation):
        r = self.session.get(self.url(name), params={"alt": "media", "ifGenerationMatch": generation}, timeout=120)
        r.raise_for_status()
        return r.content

    def acquire(self):
        prior = self.metadata("execution-lock.json")
        generation = 0
        if prior:
            value = json.loads(self.read("execution-lock.json", prior["generation"]))
            if value["expires"] > time.time():
                return False
            generation = prior["generation"]
        value = json.dumps({"owner": self.owner, "expires": time.time() + 3600}).encode()
        try:
            self.lock_generation = self.upload("execution-lock.json", value, generation, "application/json")
        except Exception as e:
            if getattr(getattr(e, "response", None), "status_code", None) == 412:
                return False
            raise
        return True

    def fence(self):
        if time.monotonic() - self.started > 2280:
            raise RuntimeError("CLOUD_EXECUTION_DEADLINE")
        current = self.metadata("execution-lock.json")
        if not current or current["generation"] != self.lock_generation:
            raise RuntimeError("CLOUD_LOCK_LOST")

    def restore(self):
        meta = self.metadata("state.tar.gz")
        if not meta:
            raise RuntimeError("CLOUD_MIGRATION_STATE_REQUIRED")
        self.generation = meta["generation"]
        data = self.read("state.tar.gz", self.generation)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for item in archive.getmembers():
                if item.name not in ("intake.db", "status.json", "coverage.json") or not item.isfile():
                    raise RuntimeError("INVALID_STATE_ARCHIVE")
                (collector.ROOT / item.name).write_bytes(archive.extractfile(item).read())

    def persist(self):
        self.fence()
        snapshot = collector.ROOT / "snapshot.db"
        source = sqlite3.connect(collector.ROOT / "intake.db")
        destination = sqlite3.connect(snapshot)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            archive.add(snapshot, arcname="intake.db")
            for name in ("status.json", "coverage.json"):
                if (collector.ROOT / name).exists():
                    archive.add(collector.ROOT / name, arcname=name)
        self.generation = self.upload("state.tar.gz", stream.getvalue(), self.generation)

    def release(self):
        if self.lock_generation:
            r = self.session.delete(
                self.url("execution-lock.json"), params={"ifGenerationMatch": self.lock_generation}, timeout=60
            )
            if r.status_code not in (204, 404, 412):
                r.raise_for_status()


def secret(session, name):
    import base64

    r = session.get("https://secretmanager.googleapis.com/v1/" + name + ":access", timeout=60)
    r.raise_for_status()
    return base64.b64decode(r.json()["payload"]["data"])


def main():
    collector.ROOT.mkdir(parents=True, exist_ok=True)
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)
    cloud = CloudState(session, os.environ["COLLECTOR_STATE_BUCKET"])
    if not cloud.acquire():
        print(json.dumps({"status": "SKIPPED_ACTIVE_EXECUTION"}))
        return
    try:
        cloud.restore()
        for filename, env in [("google-token.json", "COLLECTOR_CHAT_SECRET"), ("drive-token.json", "COLLECTOR_DRIVE_SECRET")]:
            (collector.ROOT / filename).write_bytes(secret(session, os.environ[env]))
        collector.persist = cloud.persist
        # The cloud lock also fences delivery retry and generation publication.
        if os.environ.get("VERIFY_REPLAY") == "1":
            import verify_replay

            verify_replay.main()
            cloud.persist()
        else:
            collector.run()
        status = json.loads((collector.ROOT / "status.json").read_text())
        print(
            json.dumps(
                {
                    "status": "SUCCESS",
                    "last_success": status.get("last_success"),
                    "raw_records": status.get("raw_records"),
                    "delivered_records": status.get("delivered_records"),
                    "conversation_count": status.get("conversation_count"),
                    "state_generation": cloud.generation,
                }
            )
        )
    except Exception as e:
        # No response bodies, raw content, provider URLs or credentials in Cloud Logging.
        safe = str(e) if isinstance(e, RuntimeError) and str(e).isupper() else type(e).__name__
        status = {"time": collector.now(), "status": "ERROR", "code": safe}
        try:
            cloud.fence()
            cloud.upload(
                "last-error.json",
                json.dumps(status).encode(),
                (cloud.metadata("last-error.json") or {}).get("generation", 0),
                "application/json",
            )
        except Exception:
            pass
        print(json.dumps(status))
        return 1
    finally:
        cloud.release()
    return 0


if __name__ == "__main__":
    try:
        result = main()
    except Exception as e:
        print(json.dumps({"status": "ERROR", "code": type(e).__name__}))
        result = 1
    sys.exit(result)
