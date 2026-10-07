# Clean-room proving run

A checklist for standing the assistant up on a **throwaway** Google Cloud project and removing it
again. It proves the reusable path end to end without touching any real deployment. Nothing here runs
against a production project.

Two steps need the **owner's browser** and cannot be automated: the OAuth consent and the
`chat-api-push` publisher grant. Everything else is scripted.

## 0. Prerequisites

- A throwaway project `<throwaway>` and its project number.
- `gcloud` authenticated to an account with Owner/Editor on `<throwaway>`.
- A throwaway Google Chat space where the owner and the assistant bot are members.
- A Desktop OAuth client JSON for the owner account (throwaway client is fine).

## 1. Preview, then provision

```sh
./deploy.sh --dry-run --project <throwaway> --project-number <number> --prefix ea \
  --owner-email <owner> --owner-user users/<id> --space-id spaces/<space> --bot-id <bot> \
  --with-collector
./deploy.sh up --project <throwaway> --project-number <number> --prefix ea \
  --owner-email <owner> --owner-user users/<id> --space-id spaces/<space> --bot-id <bot> \
  --with-collector
```

Confirm: buckets, service accounts, secrets, private Cloud Run service, topics, push subscriptions,
Workspace Events subscription, four schedulers, and (with collector) the job + hourly scheduler.

## 2. OAuth (owner browser)

```sh
python scripts/oauth_setup.py --kind chat  --secret ea-chat-oauth  --client client.json --project <throwaway>
python scripts/oauth_setup.py --kind gmail --secret ea-gmail-oauth --client client.json --project <throwaway>
python scripts/oauth_setup.py --kind chat  --secret ea-collector-chat-oauth  --client client.json --project <throwaway>
python scripts/oauth_setup.py --kind drive --secret ea-collector-drive-oauth --client client.json --project <throwaway>
```

Owner action: complete each browser consent as the owner account. The script stores the JSON without
printing tokens.

## 3. Workspace Events publisher grant (owner/admin)

```sh
gcloud pubsub topics add-iam-policy-binding ea-events \
  --member=serviceAccount:chat-api-push@system.gserviceaccount.com \
  --role=roles/pubsub.publisher --project=<throwaway>
```

## 4. Verify

| Check | Command | Expected |
|---|---|---|
| Health | `curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" https://<service>/health` | `READY` |
| Intake | `curl -X POST .../intake` | `SUCCESS`, task count |
| Brief | `curl -X POST .../brief` (scheduler also fires) | `SUCCESS`, a delivered brief |
| Renewal | `python scripts/oauth_setup.py --kind chat --verify-url https://<service>/maintain` | HTTP 200 |
| Collector | wait for `ea-collector-hourly`, or run the job | `SUCCESS`, counts |

Record exactly which steps needed the owner's browser (2 and 3) and which were completed by tooling.

## 5. Teardown

```sh
./deploy.sh teardown --dry-run --with-collector --purge --project <throwaway>
./deploy.sh teardown --with-collector --purge --project <throwaway>
```

Confirm schedulers paused/deleted, subscriptions and service/job gone, secrets/topics/SAs removed
(under `--purge`), and the state buckets retained for audit. Then delete the throwaway project if the
retained buckets are no longer needed.
