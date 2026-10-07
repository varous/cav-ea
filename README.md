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
- **Model-call resilience** — per-message fail-closed, telemetry, and a bounded reasoning budget.

## Layout

```
app.py                  HTTP service: routes, model call, chat/Gmail intake, recovery, briefs
operating.py            operating rules (templated), validation, change application, ledger export
storage.py              generation-fenced private object storage + execution lease
attachments.py          Chat attachment ingestion/extraction
bootstrap.py            parameterized provisioner (dry-run + idempotent)
deploy.sh               thin wrapper: ./deploy.sh up | ./deploy.sh --dry-run
test_controls.py        control/integration tests (fixture-driven)
test_attachments.py     attachment tests
test_deepseek.py        model-call tests
test_bootstrap.py       provisioner tests (no network/cloud)
fixtures/baseline.md    neutral test baseline (no real data)
config.example.json     example deployment config (config.json)
.env.example            documented environment overrides
requirements.txt        runtime dependencies
requirements-dev.txt    test-only dependencies (PyYAML)
docs/knowledge-pack.md  retrieval + knowledge-pack interface contract (the RAG seam)
.github/workflows/ci.yml  CI: python -m unittest discover on push + PR
```

## Setup

Prerequisites:

- `gcloud` installed and authenticated as an account with permission to create Cloud Run, Pub/Sub,
  Secret Manager, service accounts and Scheduler jobs in the target project.
- Python 3.11+ available locally.
- A Google Chat space where the owner and the assistant's bot are both members.

Provision everything in one command:

```sh
./deploy.sh up            # real, idempotent provisioning
./deploy.sh --dry-run     # print every command; change nothing
```

`bootstrap.py` is parameterized entirely by `config.json`, `.env` and CLI flags (project, region,
owner identity, space, bot, resource prefix, buckets, model block, recap hours). It:

1. Enables the required APIs.
2. Creates the state + chat-state buckets.
3. Creates the runtime and trigger service accounts with least-privilege roles.
4. Creates the model/chat/gmail secrets and prompts you to add values (never echoed).
5. Builds and deploys the assistant **privately** and limits invokers to the owner and trigger.
6. Creates the Pub/Sub topics and authenticated push subscriptions (`/events`, `/process`).
7. Creates the Workspace Events subscription for the space (message-created, resource names only).
8. Creates the schedulers (intake `:07`, renew `:15`, briefs `09:00`, briefs `10–22 Mon–Sat`) with
   OIDC auth to the trigger service account.
9. Writes `config.json` and uploads it to the state bucket.

Useful flags: `--project`, `--region`, `--prefix`, `--owner-email`, `--owner-user`, `--space-id`,
`--bot-id`, `--project-number`, `--service-url`, `--secret-file NAME=PATH` (add a secret version from a
file), `--baseline FILE` (seed the ledger state), `--print-config`, `--no-idempotent`.

### Manual steps

Two things cannot be fully automated and must be done by you:

1. **OAuth consent** — create a Desktop OAuth client for the owner account, complete the consent once,
   and store the resulting user JSON in the `chat-oauth` secret (and `gmail-oauth` if you want Gmail
   intake). Never commit these files.
2. **Workspace Events publisher grant** — allow the Workspace Events service to publish to the events
   topic:

   ```sh
   gcloud pubsub topics add-iam-policy-binding <prefix>-events \
     --member=serviceAccount:chat-api-push@system.gserviceaccount.com \
     --role=roles/pubsub.publisher --project=<project>
   ```

Then authorize the owner for the Workspace Events API and confirm the subscription reaches `/events`.

## Configuration

Identity and behaviour come from `config.json` (in the state bucket) plus documented environment
overrides. See `config.example.json` and `.env.example`. Required identity: `owner_email`,
`owner_user`, `space_id`, `bot_id`.

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
