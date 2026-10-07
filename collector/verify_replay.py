"""Live crash-recovery verification: replay one completed batch's durable receipt."""
import json

import collector


def main():
    c = collector.db()
    a = collector.delivery_auth()
    folder = collector.state(c, "drive_folder")

    def remote_ids():
        return {
            r["id"]
            for r in collector.pages(
                a,
                "https://www.googleapis.com/drive/v3/files",
                "files",
                {"q": "'" + folder + "' in parents and trashed=false", "fields": "files(id),nextPageToken"},
            )
        }

    if c.execute("SELECT count(*) FROM raw WHERE delivered=0").fetchone()[0]:
        raise RuntimeError("DELIVERY_STILL_PENDING")
    prior = remote_ids()
    total = c.execute("SELECT count(*) FROM raw").fetchone()[0]
    batch = c.execute("SELECT id FROM batches WHERE done=1 ORDER BY rowid LIMIT 1").fetchone()[0]
    # Model a crash after remote upload but before local delivery acknowledgment.
    with c:
        c.execute("UPDATE batches SET done=0 WHERE id=?", (batch,))
        c.execute("UPDATE raw SET delivered=0 WHERE id IN (SELECT record_id FROM batch_items WHERE batch_id=?)", (batch,))
    collector.persist()
    collector.deliver()
    after = remote_ids()
    result = {
        "remote_files_before": len(prior),
        "remote_files_after": len(after),
        "same_remote_ids": prior == after,
        "raw_records": total,
        "pending": c.execute("SELECT count(*) FROM raw WHERE delivered=0").fetchone()[0],
    }
    assert prior == after and result["pending"] == 0
    (collector.ROOT / "replay-verification.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
