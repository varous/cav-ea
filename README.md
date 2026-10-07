# cav-ea

A small, reusable **operating assistant** that tracks tasks from an authorized owner's chat
messages, keeps an append-only task ledger, posts concise briefs, and can ground answers in a
deployer-supplied **knowledge pack**.

This repository contains the reusable assistant core and its interfaces only. It ships **no**
operational data, credentials, or deployment-specific identifiers.

## What it does

- **Owner-authorized task ledger** — only verified owner messages in a designated space change tasks;
  every change is append-only with provenance.
- **Chat interface** — runs as a private service behind Google Chat events (Workspace Events → Pub/Sub),
  with a scheduled hourly intake fallback.
- **Briefs** — a Python-computed changelog every hour plus a pending recap at recap hours.
- **Knowledge pack** — a deployer-supplied bundle (operating context, optional people, optional
  documents) retrieved through a single pluggable interface (`retrieve_relevant`).
- **Optional collector** — a read-only Google Chat intake pipeline (Cloud Run job) that stores raw
  records durably and can feed the assistant's context.
- **Model-call resilience** — per-message fail-closed, telemetry, and a bounded reasoning budget.

## Layout

```
app.py                   HTTP service: routes, model call, chat/Gmail intake, recovery, briefs
operating.py             operating rules (templated), validation, change application, ledger export
storage.py               generation-fenced private object storage + execution lease
attachments.py           Chat attachment ingestion/extraction
bootstrap.py             parameterized provisioner: up + teardown, dry-run + idempotent
deploy.sh                thin wrapper: up | teardown | --dry-run
collector/               optional read-only Chat collector (job + scheduler); own Dockerfile/runbook
scripts/oauth_setup.py   scripted owner OAuth walkthrough (stores secrets without printing tokens)
test_controls.py         control/integration tests (fixture-driven)
test_attachments.py      attachment tests
test_deepseek.py         model-call tests
test_bootstrap.py        provisioner + teardown tests (no network/cloud)
test_collector.py        collector tests (no network)
test_oauth_setup.py      OAuth-script tests (no browser)
fixtures/baseline.md     neutral test baseline (no real data)
config.example.json      example deployment config (config.json)
.env.example             documented environment overrides
requirements.txt         runtime dependencies
requirements-dev.txt     test-only dependencies (PyYAML)
docs/knowledge-pack.md   retrieval + knowledge-pack interface contract (the RAG seam)
docs/clean-room.md       checklist for a throwaway-project proving run
.github/workflows/ci.yml CI: python -m unittest discover on push + PR
```

## Quickstart

Prerequisites:

- `gcloud` installed and authenticated as an account that can create Cloud Run, Pub/Sub,
  Secret Manager, service accounts and Scheduler jobs in the target project.
- Python 3.11+ available locally.
- A Google Chat space where the owner and the assistant's bot are both members.

1. **Configure.** Edit `config.example.json` (or pass flags / env). Required identity:
   `owner_email`, `owner_user`, `space_id`, `bot_id`. Preview the plan first:

   ```sh
   ./deploy.sh --dry-run          # prints every command, changes nothing
   ```

2. **Provision.**

   ```sh
   ./deploy.sh up                 # core assistant (idempotent)
   ./deploy.sh up --with-collector   # also the optional read-only chat collector
   ```

3. **OAuth.** The owner's browser consent is the one unavoidable manual step:

   ```sh
   python scripts/oauth_setup.py --kind chat  --secret <prefix>-chat-oauth  --client client.json --project <project>
   python scripts/oauth_setup.py --kind gmail --secret <prefix>-gmail-oauth --client client.json --project <project>
   # with --with-collector:
   python scripts/oauth_setup.py --kind chat  --secret <prefix>-collector-chat-oauth  --client client.json --project <project>
   python scripts/oauth_setup.py --kind drive --secret <prefix>-collector-drive-oauth --client client.json --project <project>
   ```

   The script prints the exact Console steps, runs the Desktop consent flow, and stores the user JSON
   in Secret Manager **without printing any token**.

4. **Grant the Workspace Events publisher** on the events topic (the one remaining manual grant):

   ```sh
   gcloud pubsub topics add-iam-policy-binding <prefix>-events \
     --member=serviceAccount:chat-api-push@system.gserviceaccount.com \
     --role=roles/pubsub.publisher --project=<project>
   ```

5. **Verify.** Call the private service with an identity token:

   ```sh
   gcloud auth print-identity-token | xargs -I{} curl -s -H "Authorization: Bearer {}" https://<service>/health
   python scripts/oauth_setup.py --kind chat --verify-url https://<service>/maintain   # one renewal
   ```

   Then confirm one `/intake`, one delivered `/brief`, and a healthy `/maintain` renewal. See
   `docs/clean-room.md` for the full throwaway-project checklist.

6. **Teardown.** Removes schedulers, subscriptions, the service (and job), and is idempotent:

   ```sh
   ./deploy.sh teardown --dry-run
   ./deploy.sh teardown --with-collector          # keep state + exports for audit
   ./deploy.sh teardown --purge                   # also remove secrets, topics and SAs
   ```

   State buckets and exports are always retained.

## Configuration

Identity and behaviour come from `config.json` (in the state bucket) plus documented environment
overrides. See `config.example.json` and `.env.example`. Defaults keep the core lean; the collector is
opt-in.

## Collector (optional)

The `collector/` component lists the conversations the owner account belongs to, stores raw message
records durably (deduplicated by resource name), and delivers immutable Markdown batches to a Drive
folder. It is read-only: no send, reply, delete or read-state operation exists. See
`collector/RUNBOOK.md`.

## Knowledge pack / retrieval

Documents, manuals and SOPs are **advisory context only** — they can inform an answer or a suggestion
but never authorize a task change. See `docs/knowledge-pack.md`.

## Tests

```sh
python -m unittest discover
```

The suite is fixture-driven and needs no secrets, network or cloud access. CI runs it on every push
and pull request.

## License

MIT — see `LICENSE`.
