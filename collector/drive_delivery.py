"""Immutable, durable batches of raw source records; no interpretations."""
import hashlib
import json
import os
import uuid

import requests


def folder_name():
    return os.environ.get("COLLECTOR_FOLDER_NAME", "Chat intake")


def deliver(core):
    a = core.delivery_auth()
    c = core.db()
    folder = core.state(c, "drive_folder")
    if not folder:
        raise RuntimeError("DELIVERY_SETUP_REQUIRED")
    core.ensure_folder(a, folder)
    c.executescript(
        """CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,file_id TEXT UNIQUE,payload BLOB,done INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS batch_items(record_id TEXT PRIMARY KEY,batch_id TEXT);"""
    )
    while True:
        pending = c.execute("SELECT id,file_id,payload FROM batches WHERE done=0 ORDER BY rowid LIMIT 1").fetchone()
        if not pending:
            next_source = c.execute(
                "SELECT source FROM raw WHERE delivered=0 AND id NOT IN (SELECT record_id FROM batch_items) ORDER BY id LIMIT 1"
            ).fetchone()
            if not next_source:
                break
            source = next_source[0]
            rows = c.execute(
                "SELECT id,body FROM raw WHERE source=? AND delivered=0 AND id NOT IN (SELECT record_id FROM batch_items) ORDER BY id LIMIT 30",
                (source,),
            ).fetchall()
            selected = []
            parts = [
                "# Raw message intake\n\nSource: " + source
                + "\nInterpretations: none. Statements are source reports, not verified operational facts.\n"
            ]
            for record_id, body in rows:
                part = "\n## " + record_id + "\n\n```json\n" + json.dumps(json.loads(body), ensure_ascii=False, indent=2) + "\n```\n"
                if selected and sum(len(p.encode()) for p in parts) + len(part.encode()) > 50000:
                    break
                parts.append(part)
                selected.append(record_id)
            payload = "".join(parts).encode("utf-8")
            batch_id = hashlib.sha256(payload).hexdigest()
            file_id = core.get(a, "https://www.googleapis.com/drive/v3/files/generateIds", {"count": 1})["ids"][0]
            with c:
                c.execute("INSERT INTO batches(id,file_id,payload) VALUES (?,?,?)", (batch_id, file_id, payload))
                c.executemany("INSERT INTO batch_items VALUES (?,?)", [(r, batch_id) for r in selected])
            core.persist()
            pending = batch_id, file_id, payload
        batch_id, file_id, payload = pending
        url = "https://www.googleapis.com/drive/v3/files/" + file_id
        headers = {"Authorization": "Bearer " + a.token}
        r = requests.get(url, headers=headers, params={"fields": "id,md5Checksum,parents"}, timeout=60)
        if r.status_code == 404:
            boundary = "ea_" + uuid.uuid4().hex
            prefix = "chat-raw-"
            metadata = json.dumps(
                {"id": file_id, "name": prefix + batch_id[:24] + ".md", "parents": [folder], "mimeType": "text/plain"}
            )
            data = (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{metadata}"
                f"\r\n--{boundary}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n".encode()
                + payload
                + f"\r\n--{boundary}--\r\n".encode()
            )
            created = requests.post(
                "https://www.googleapis.com/upload/drive/v3/files",
                params={"uploadType": "multipart"},
                headers=headers | {"Content-Type": "multipart/related; boundary=" + boundary},
                data=data,
                timeout=60,
            )
            if created.status_code not in (200, 201, 409):
                created.raise_for_status()
            r = requests.get(url, headers=headers, params={"fields": "id,md5Checksum,parents"}, timeout=60)
        r.raise_for_status()
        receipt = r.json()
        # Remote checksum and parent validation precede atomic local acknowledgment.
        if receipt.get("md5Checksum") != hashlib.md5(payload).hexdigest() or folder not in receipt.get("parents", []):
            raise RuntimeError("DELIVERY_RECEIPT_MISMATCH")
        with c:
            c.execute("UPDATE batches SET done=1 WHERE id=?", (batch_id,))
            c.execute("UPDATE raw SET delivered=1 WHERE id IN (SELECT record_id FROM batch_items WHERE batch_id=?)", (batch_id,))
        core.persist()
    c.close()
