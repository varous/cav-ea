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
app.py              HTTP service: routes, model call, chat/Gmail intake, recovery, briefs
operating.py        operating rules (templated), validation, change application, ledger export
storage.py          generation-fenced private object storage + execution lease
attachments.py      Chat attachment ingestion/extraction
test_controls.py    control/integration tests (fixture-driven)
test_attachments.py attachment tests
test_deepseek.py    model-call tests
fixtures/baseline.md neutral test baseline (no real data)
config.example.json example deployment config (config.json)
.env.example        documented environment overrides
docs/knowledge-pack.md  retrieval + knowledge-pack interface contract (the RAG seam)
deploy.sh           bootstrap stub
```

## Configuration

Identity and behaviour come from `config.json` (in the state bucket) plus documented environment
overrides. See `config.example.json` and `.env.example`. Required identity: `owner_email`,
`owner_user`, `space_id`, `bot_id`.

## Knowledge pack / retrieval

Documents, manuals and SOPs are **advisory context only** — they can inform an answer or a suggestion
but never authorize a task change. See `docs/knowledge-pack.md`.

## Bootstrap

**Bootstrap coming (Phase C).** `deploy.sh` is currently a stub; provisioning (Cloud Run, Pub/Sub,
schedulers, secrets, state bucket) is not yet automated.

## Tests

```sh
python -m unittest test_controls test_attachments test_deepseek
```

## License

MIT — see `LICENSE`.
