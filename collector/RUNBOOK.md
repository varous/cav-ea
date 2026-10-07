# Collector runbook (optional component)

The collector is an **optional**, read-only Google Chat intake pipeline. It lists the
conversations the owner account is a member of, stores raw message records durably
(deduplicated by resource name), and delivers immutable Markdown batches to a Drive
folder. It is not required: `app.py` can run without it, and the assistant's `/intake`
consumes whatever the collector publishes into the chat-state bucket.

## Identity and scopes

- Chat OAuth scopes: `openid`, `userinfo.email`, `chat.spaces.readonly`,
  `chat.messages.readonly`.
- Drive OAuth scopes: `openid`, `userinfo.email`, `drive.file`.
- The code implements **no** Chat send, reply, delete or read-state operation.
- Every run verifies the signed-in account equals `OWNER_EMAIL`.

## Configuration

| Env | Meaning |
|---|---|
| `OWNER_EMAIL` | Account that must own both grants and be verified each run. |
| `COLLECTOR_RUNTIME` | Private scratch/runtime directory (host) or `/tmp/ea-collector` (job). |
| `COLLECTOR_STATE_BUCKET` | Private bucket holding `state.tar.gz` and safe status. |
| `COLLECTOR_FOLDER_NAME` | Drive folder name for delivered batches. |
| `COLLECTOR_CHAT_SECRET` / `COLLECTOR_DRIVE_SECRET` | Secret Manager secret **names**. |
| `VERIFY_REPLAY=1` | Run the lost-acknowledgment replay once instead of a normal pass. |

## Host commands

```sh
python collector.py authenticate --client <oauth-client.json>       # Chat grant
python collector.py authenticate-drive --client <oauth-client.json>  # Drive grant
python collector.py setup-delivery
python collector.py activate [--backfill-from 2026-09-01T00:00:00+05:30]
python collector.py run
python collector.py status
```

Never print token files or raw databases in diagnostics. No credentials, raw records or
exclusion lists belong in source control.

## Durability and replay

Per-conversation checkpoints advance only inside the SQLite transaction that stores the
source records. A one-second query overlap protects timestamp boundaries; resource names
deduplicate repeats. A failed conversation rolls back and keeps its checkpoint. Raw outbox
records and delivery receipts are separate; a batch's file ID and payload are durable before
upload, and remote checksum/parent validation precedes atomic acknowledgment. `verify_replay.py`
simulates a lost acknowledgment and confirms the remote file identity set is unchanged.

## Cloud job

`cloud_runner.py` restores the authoritative `state.tar.gz`, verifies the owner, runs one
pass, and republishes a generation-fenced snapshot. A one-hour lease makes a duplicate
execution skip; the task timeout is shorter than the lease. A crash can leave a lease until
expiry — do not delete an active lease by hand.

## Disable and remove

Pause the hourly scheduler first, then delete it and the job; retain the state bucket and
Drive history for audit. The two OAuth grants are revoked separately in the Google account
settings when the collector is permanently retired. `./deploy.sh teardown --with-collector`
performs the pausing/removal; add `--purge` only when the retention is no longer needed.
