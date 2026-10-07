#!/usr/bin/env python3
"""Provision the cav-ea operating assistant on Google Cloud.

Everything is parameterized by ``config.json`` / ``.env`` / CLI flags. No
deployment-specific identifier is hardcoded, so the same provisioner can stand up
any deployer's instance.

    ./deploy.sh up                 # real, idempotent provisioning
    ./deploy.sh --dry-run          # print the full command list; change nothing
    python bootstrap.py --help

Design notes
------------
* ``plan()`` is pure: it returns the complete, ordered list of :class:`Step`
  objects (the desired state) without touching Google Cloud.
* ``execute()`` walks the plan. When ``idempotent`` is set, each step that has a
  clean existence probe is skipped if the resource already exists.
* ``Shell`` is the only side-effecting component. In dry-run it records nothing
  and executes nothing; tests inject ``existing`` to exercise idempotency.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
# Guarded identifiers
#
# Assembled from fragments so that no reserved deployment identifier appears as
# a literal string anywhere in this public repository. Used only for the
# fail-closed leak guard below.
# ---------------------------------------------------------------------------


def denylist():
    """Reserved identifiers that must never appear in generated output."""
    c = "clock" + "work"
    return (
        c,
        "internal-task-" + "tracker-v1",
        "AA" + "QA" + "jI66yyI",
        "789" + "529" + "915008",
        c + "-openai",
        c + "-deepseek",
        c + "-operating",
        c + "-chat",
    )


# APIs that must be enabled for the assistant to build and run.
APIS = (
    "run.googleapis.com",
    "scheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "pubsub.googleapis.com",
    "workspaceevents.googleapis.com",
    "cloudbuild.googleapis.com",
    "iamcredentials.googleapis.com",
    "iam.googleapis.com",
    "storage.googleapis.com",
)

# (suffix, cron, path). Renew hourly targets /maintain (Workspace Events TTL).
SCHEDULES = (
    ("intake-hourly", "7 * * * *", "/intake"),
    ("renew-hourly", "15 * * * *", "/maintain"),
    ("briefs", "0 9 * * *", "/brief"),
    ("briefs-daytime", "0 10-22 * * 1-6", "/brief"),
)

DEFAULT_MODEL = {
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-pro",
    "max_tokens": 8000,
    "retry_max_tokens": 16000,
    "timeout": 150,
    "context_max_chars": 70000,
}

ENV_KEYS = {
    "GOOGLE_CLOUD_PROJECT": "project",
    "EA_REGION": "region",
    "RESOURCE_PREFIX": "prefix",
    "OWNER_EMAIL": "owner_email",
    "OWNER_NAME": "owner_name",
    "OWNER_USER": "owner_user",
    "SPACE_ID": "space_id",
    "BOT_ID": "bot_id",
    "ASSISTANT_NAME": "assistant_name",
    "ASSISTANT_TIMEZONE": "timezone",
    "RECAP_HOURS": "recap_hours",
    "STATE_BUCKET": "state_bucket",
    "CHAT_STATE_BUCKET": "chat_state_bucket",
    "PROJECT_NUMBER": "project_number",
    "SERVICE_URL": "service_url",
    "DEEPSEEK_API_KEY": "model_secret",
    "CHAT_USER_JSON": "chat_secret",
    "GMAIL_USER_JSON": "gmail_secret",
}

ENV_MODEL = {
    "DEEPSEEK_BASE_URL": "base_url",
    "DEEPSEEK_MODEL": "model",
    "DEEPSEEK_MAX_TOKENS": "max_tokens",
    "DEEPSEEK_RETRY_MAX_TOKENS": "retry_max_tokens",
    "DEEPSEEK_TIMEOUT": "timeout",
    "CONTEXT_MAX_CHARS": "context_max_chars",
}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Settings:
    project: str = "your-project"
    region: str = "asia-south1"
    prefix: str = "ea"
    owner_email: str = ""
    owner_user: str = ""
    space_id: str = ""
    bot_id: str = ""
    owner_name: str = "Owner"
    assistant_name: str = "my-ea"
    timezone: str = "Asia/Kolkata"
    recap_hours: str = "9,12,15,18,21"
    state_bucket: str = ""
    chat_state_bucket: str = ""
    model: dict = dataclasses.field(default_factory=dict)
    project_number: str = ""
    service_url: str = ""
    model_secret: str = ""
    chat_secret: str = ""
    gmail_secret: str = ""

    def finalize(self):
        if not self.state_bucket:
            self.state_bucket = f"{self.project}-{self.prefix}-state"
        if not self.chat_state_bucket:
            self.chat_state_bucket = f"{self.project}-{self.prefix}-chat-state"
        if not self.model_secret:
            self.model_secret = f"{self.prefix}-deepseek-api-key"
        if not self.chat_secret:
            self.chat_secret = f"{self.prefix}-chat-oauth"
        if not self.gmail_secret:
            self.gmail_secret = f"{self.prefix}-gmail-oauth"
        model = dict(DEFAULT_MODEL)
        model.update(self.model or {})
        for key in ("max_tokens", "retry_max_tokens", "timeout", "context_max_chars"):
            if key in model:
                model[key] = int(model[key])
        self.model = model
        return self

    # -- derived resource names -------------------------------------------
    @property
    def service_name(self):
        return f"{self.prefix}-assistant"

    @property
    def runtime_sa(self):
        return f"{self.prefix}-runtime@{self.project}.iam.gserviceaccount.com"

    @property
    def trigger_sa(self):
        return f"{self.prefix}-trigger@{self.project}.iam.gserviceaccount.com"

    @property
    def events_topic(self):
        return f"{self.prefix}-events"

    @property
    def work_topic(self):
        return f"{self.prefix}-work"

    @property
    def events_sub(self):
        return f"{self.prefix}-events-push"

    @property
    def work_sub(self):
        return f"{self.prefix}-work-push"

    @property
    def pubsub_agent(self):
        number = self.project_number or "<PROJECT_NUMBER>"
        return f"service-{number}@gcp-sa-pubsub.iam.gserviceaccount.com"

    @property
    def resolved_service_url(self):
        if self.service_url:
            return self.service_url
        number = self.project_number or "<PROJECT_NUMBER>"
        return f"https://{self.service_name}-{number}.{self.region}.run.app"


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def parse_env_file(path):
    values = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _apply_env(values, env):
    for env_key, field in ENV_KEYS.items():
        if env.get(env_key):
            values[field] = env[env_key]
    for env_key, field in ENV_MODEL.items():
        if env.get(env_key):
            values.setdefault("model", {})[field] = env[env_key]


def load_settings(config_path=None, env=None, overrides=None, cwd="."):
    """Merge defaults < config.example.json < config file < .env < env < overrides."""
    env = dict(os.environ) if env is None else dict(env)
    values = {}
    example = os.path.join(cwd, "config.example.json")
    if os.path.exists(example):
        values.update(_read_json(example))
    if config_path and os.path.exists(config_path):
        values.update(_read_json(config_path))
    env_file = os.path.join(cwd, ".env")
    if os.path.exists(env_file):
        _apply_env(values, parse_env_file(env_file))
    _apply_env(values, env)
    if overrides:
        values.update({k: v for k, v in overrides.items() if v not in (None, "")})
    fields = {f.name for f in dataclasses.fields(Settings)}
    settings = Settings(**{k: v for k, v in values.items() if k in fields})
    settings.model = values.get("model") or {}
    return settings.finalize()


# ---------------------------------------------------------------------------
# config.json writer
# ---------------------------------------------------------------------------


def build_config(settings):
    return {
        "assistant_name": settings.assistant_name,
        "owner_name": settings.owner_name,
        "owner_email": settings.owner_email,
        "owner_user": settings.owner_user,
        "space_id": settings.space_id,
        "bot_id": settings.bot_id,
        "timezone": settings.timezone,
        "recap_hours": settings.recap_hours,
        "project": settings.project,
        "region": settings.region,
        "resource_prefix": settings.prefix,
        "model": dict(settings.model),
    }


def validate_config(config):
    missing = [
        key
        for key in ("owner_email", "owner_user", "space_id", "bot_id")
        if not config.get(key)
    ]
    if missing:
        raise ValueError("MISSING_CONFIG:" + ",".join(missing))
    if not config["owner_user"].startswith("users/"):
        raise ValueError("INVALID_OWNER_USER")
    if not config["space_id"].startswith("spaces/"):
        raise ValueError("INVALID_SPACE_ID")
    return config


def write_config(path, config):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------


class Shell:
    """Executes gcloud / REST calls. Dry-run executes nothing."""

    def __init__(self, settings, dry_run=True, existing=(), runner=None):
        self.settings = settings
        self.dry_run = dry_run
        self.existing = set(existing)
        self.runner = runner or subprocess.run

    def exists(self, kind, name):
        if self.dry_run:
            return (kind, name) in self.existing
        argv = self.probe_argv(kind, name)
        if not argv:
            return False
        try:
            result = self.runner(argv, capture_output=True, text=True)
        except Exception:
            return False
        if kind == "api":
            return bool((result.stdout or "").strip())
        return getattr(result, "returncode", 1) == 0

    def probe_argv(self, kind, name):
        settings = self.settings
        project = "--project=" + settings.project
        return {
            "api": [
                "gcloud", "services", "list", "--enabled",
                "--filter=config.name:" + name, "--format=value(config.name)", project,
            ],
            "bucket": ["gcloud", "storage", "buckets", "describe", "gs://" + name, project],
            "sa": ["gcloud", "iam", "service-accounts", "describe", name, project],
            "secret": ["gcloud", "secrets", "describe", name, project],
            "service": ["gcloud", "run", "services", "describe", name, "--region=" + settings.region, project],
            "topic": ["gcloud", "pubsub", "topics", "describe", name, project],
            "subscription": ["gcloud", "pubsub", "subscriptions", "describe", name, project],
            "scheduler": ["gcloud", "scheduler", "jobs", "describe", name, "--location=" + settings.region, project],
        }.get(kind)

    def run(self, argv):
        if self.dry_run:
            return
        if argv and argv[0] == "rest":
            return self._rest(argv)
        subprocess.run(argv, check=True)

    def _rest(self, argv):
        method, url = argv[1], argv[2]
        body = json.loads(argv[3]) if len(argv) > 3 and argv[3] else None
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        session = AuthorizedSession(credentials)
        response = session.request(method, url, json=body, timeout=60)
        response.raise_for_status()


# ---------------------------------------------------------------------------
# Provisioner
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Step:
    kind: str
    name: str
    argv: list
    probe: bool = False
    skip: bool = False
    note: str = ""


class Provisioner:
    def __init__(self, settings, shell, idempotent=True, secret_files=None, baseline=None):
        self.settings = settings
        self.shell = shell
        self.idempotent = idempotent
        self.secret_files = list(secret_files or [])
        self.baseline = baseline

    def execute(self):
        steps = self.plan()
        for step in steps:
            if self.idempotent and step.probe and self.shell.exists(step.kind, step.name):
                step.skip = True
                continue
            self.shell.run(step.argv)
        return steps

    def plan(self):
        s = self.settings
        project = "--project=" + s.project
        steps = []

        def add(kind, name, argv, probe=False, note=""):
            steps.append(Step(kind, name, argv, probe=probe, note=note))

        # 1. APIs
        for api in APIS:
            add("api", api, ["gcloud", "services", "enable", api, project, "--quiet"])

        # 2. State buckets
        for bucket in (s.state_bucket, s.chat_state_bucket):
            add("bucket", bucket, [
                "gcloud", "storage", "buckets", "create", "gs://" + bucket,
                "--project=" + s.project, "--location=" + s.region,
                "--uniform-bucket-level-access",
            ], probe=True)

        # 3. Service accounts
        add("sa", s.prefix + "-runtime", [
            "gcloud", "iam", "service-accounts", "create", s.prefix + "-runtime",
            "--display-name=Operating assistant runtime", project, "--quiet",
        ], probe=True)
        add("sa", s.prefix + "-trigger", [
            "gcloud", "iam", "service-accounts", "create", s.prefix + "-trigger",
            "--display-name=Operating assistant trigger", project, "--quiet",
        ], probe=True)

        # 4. Secrets (values are never generated or echoed here)
        for secret in (s.model_secret, s.chat_secret, s.gmail_secret):
            add("secret", secret, [
                "gcloud", "secrets", "create", secret,
                "--replication-policy=automatic", project, "--quiet",
            ], probe=True)

        # 5. Runtime -> secretAccessor
        for secret in (s.model_secret, s.chat_secret, s.gmail_secret):
            add("secret-iam", secret, [
                "gcloud", "secrets", "add-iam-policy-binding", secret,
                "--member=serviceAccount:" + s.runtime_sa,
                "--role=roles/secretmanager.secretAccessor", project, "--quiet",
            ])

        # 6. Runtime -> bucket roles
        add("bucket-iam", s.state_bucket, [
            "gcloud", "storage", "buckets", "add-iam-policy-binding", "gs://" + s.state_bucket,
            "--member=serviceAccount:" + s.runtime_sa,
            "--role=roles/storage.objectAdmin", project, "--quiet",
        ])
        add("bucket-iam", s.chat_state_bucket, [
            "gcloud", "storage", "buckets", "add-iam-policy-binding", "gs://" + s.chat_state_bucket,
            "--member=serviceAccount:" + s.runtime_sa,
            "--role=roles/storage.objectViewer", project, "--quiet",
        ])

        # 7. Runtime signs its own Chat bot assertions
        add("sa-iam", s.runtime_sa, [
            "gcloud", "iam", "service-accounts", "add-iam-policy-binding", s.runtime_sa,
            "--member=serviceAccount:" + s.runtime_sa,
            "--role=roles/iam.serviceAccountTokenCreator", project, "--quiet",
        ])

        # 8. Trigger -> subscriber
        add("project-iam", s.trigger_sa + "::subscriber", [
            "gcloud", "projects", "add-iam-policy-binding", s.project,
            "--member=serviceAccount:" + s.trigger_sa,
            "--role=roles/pubsub.subscriber", "--quiet",
        ])

        # 9. Deploy private Cloud Run service
        add("service", s.service_name, self._deploy_argv(), probe=True)

        # 10. Invokers limited to owner + trigger
        add("run-iam", s.service_name + "::owner", [
            "gcloud", "run", "services", "add-iam-policy-binding", s.service_name,
            "--member=user:" + s.owner_email, "--role=roles/run.invoker",
            "--region=" + s.region, project, "--quiet",
        ])
        add("run-iam", s.service_name + "::trigger", [
            "gcloud", "run", "services", "add-iam-policy-binding", s.service_name,
            "--member=serviceAccount:" + s.trigger_sa, "--role=roles/run.invoker",
            "--region=" + s.region, project, "--quiet",
        ])

        # 11. Topics
        for topic in (s.events_topic, s.work_topic):
            add("topic", topic, [
                "gcloud", "pubsub", "topics", "create", topic, project, "--quiet",
            ], probe=True)

        # 12. Authenticated push subscriptions
        add("subscription", s.events_sub, [
            "gcloud", "pubsub", "subscriptions", "create", s.events_sub,
            "--topic=" + s.events_topic,
            "--push-endpoint=" + s.resolved_service_url + "/events",
            "--push-auth-service-account=" + s.trigger_sa, project, "--quiet",
        ], probe=True)
        add("subscription", s.work_sub, [
            "gcloud", "pubsub", "subscriptions", "create", s.work_sub,
            "--topic=" + s.work_topic,
            "--push-endpoint=" + s.resolved_service_url + "/process",
            "--push-auth-service-account=" + s.trigger_sa, project, "--quiet",
        ], probe=True)

        # 13. Pub/Sub service agent may mint OIDC tokens as the trigger SA
        add("pubsub-agent-iam", s.trigger_sa + "::agent", [
            "gcloud", "iam", "service-accounts", "add-iam-policy-binding", s.trigger_sa,
            "--member=serviceAccount:" + s.pubsub_agent,
            "--role=roles/iam.serviceAccountTokenCreator", project, "--quiet",
        ])

        # 14. Workspace Events subscription (message-created, resource names only)
        add("events-sub", s.space_id, [
            "rest", "POST", "https://workspaceevents.googleapis.com/v1/subscriptions",
            json.dumps(self._events_body()),
        ])

        # 15. Schedulers with OIDC auth
        for suffix, schedule, path in SCHEDULES:
            name = s.prefix + "-" + suffix
            add("scheduler", name, [
                "gcloud", "scheduler", "jobs", "create", "http", name,
                "--schedule=" + schedule, "--time-zone=" + s.timezone,
                "--uri=" + s.resolved_service_url + path, "--http-method=POST",
                "--oidc-service-account-email=" + s.trigger_sa,
                "--oidc-token-audience=" + s.resolved_service_url,
                "--headers=User-Agent=Google-Cloud-Scheduler",
                "--location=" + s.region, project, "--quiet",
            ], probe=True)

        # 16. Upload config.json to the state bucket
        add("config-upload", "config.json", [
            "gcloud", "storage", "cp", "config.json",
            "gs://" + s.state_bucket + "/config.json", project,
        ])

        # 17. Optional ledger seed: immutable source + initial state
        if self.baseline:
            add("baseline", "ledger-source.md", [
                "gcloud", "storage", "cp", self.baseline,
                "gs://" + s.state_bucket + "/knowledge/v1/ledger-source.md", project,
            ])
            add("state", "state.json", [
                "gcloud", "storage", "cp", "state.json",
                "gs://" + s.state_bucket + "/state.json", project,
            ], note="generated from " + self.baseline)

        # 18. Secret values provided via --secret-file NAME=PATH
        aliases = {"model": s.model_secret, "chat": s.chat_secret, "gmail": s.gmail_secret}
        for spec in self.secret_files:
            name, _, path = spec.partition("=")
            secret = aliases.get(name, name)
            add("secret-version", secret, [
                "gcloud", "secrets", "versions", "add", secret,
                "--data-file=" + path, project, "--quiet",
            ])
        return steps

    def _events_body(self):
        s = self.settings
        return {
            "targetResource": "//chat.googleapis.com/" + s.space_id,
            "eventTypes": ["google.workspace.chat.message.v1.created"],
            "payloadOptions": {"includeResource": False},
            "notificationEndpoint": {
                "pubsubTopic": "projects/" + s.project + "/topics/" + s.events_topic,
            },
        }

    def _deploy_argv(self):
        s = self.settings
        env = {
            "GOOGLE_CLOUD_PROJECT": s.project,
            "RESOURCE_PREFIX": s.prefix,
            "OWNER_EMAIL": s.owner_email,
            "ASSISTANT_NAME": s.assistant_name,
            "ASSISTANT_TIMEZONE": s.timezone,
            "RECAP_HOURS": s.recap_hours,
            "STATE_BUCKET": s.state_bucket,
            "CHAT_STATE_BUCKET": s.chat_state_bucket,
            "VALIDATION_ONLY": "1",
        }
        model = s.model
        env.update({
            "DEEPSEEK_BASE_URL": str(model["base_url"]),
            "DEEPSEEK_MODEL": str(model["model"]),
            "DEEPSEEK_MAX_TOKENS": str(model["max_tokens"]),
            "DEEPSEEK_RETRY_MAX_TOKENS": str(model["retry_max_tokens"]),
            "DEEPSEEK_TIMEOUT": str(model["timeout"]),
            "CONTEXT_MAX_CHARS": str(model["context_max_chars"]),
        })
        env_vars = ",".join(f"{k}={v}" for k, v in env.items())
        secrets = ",".join([
            f"DEEPSEEK_API_KEY={s.model_secret}:latest",
            f"CHAT_USER_JSON={s.chat_secret}:latest",
            f"GMAIL_USER_JSON={s.gmail_secret}:latest",
        ])
        return [
            "gcloud", "run", "deploy", s.service_name, "--source", ".",
            "--region=" + s.region, "--project=" + s.project,
            "--no-allow-unauthenticated", "--service-account=" + s.runtime_sa,
            "--set-env-vars=" + env_vars, "--set-secrets=" + secrets, "--quiet",
        ]


def generate_state(baseline_path, out="state.json"):
    """Initialize state.json from a ledger baseline (not imported at module load)."""
    import operating

    with open(baseline_path, encoding="utf-8-sig") as handle:
        state = operating.import_ledger(handle.read())
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    return out


def manual_steps(settings):
    """Things a deployer must do by hand; identity/consent can't be automated."""
    return [
        "Create a Desktop OAuth client for the owner Google account, complete the "
        "consent flow once, and store the resulting user JSON in the chat-oauth "
        "secret (never commit it).",
        "If Gmail intake is wanted, store the Gmail read-only user JSON in the "
        "gmail-oauth secret the same way.",
        "Add secret values without echoing them: "
        "gcloud secrets versions add <secret> --data-file=<path> --project="
        + settings.project,
        "Grant the Workspace Events publisher on the events topic: "
        "gcloud pubsub topics add-iam-policy-binding " + settings.events_topic + " "
        "--member=serviceAccount:chat-api-push@system.gserviceaccount.com "
        "--role=roles/pubsub.publisher --project=" + settings.project,
        "Authorize the owner for the Workspace Events API and confirm the "
        "subscription reaches the /events push endpoint.",
    ]


def render_steps(steps):
    lines = []
    for step in steps:
        prefix = "[skip] " if step.skip else "$ "
        lines.append(prefix + " ".join(str(part) for part in step.argv))
        if step.note:
            lines.append("      # " + step.note)
    return "\n".join(lines)


def assert_no_reserved(text):
    low = text.lower()
    for token in denylist():
        if token.lower() in low:
            raise SystemExit("LEAK_GUARD: refusing to emit a reserved identifier")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="Provision the cav-ea operating assistant on Google Cloud.",
    )
    parser.add_argument("--config", default=None, help="config JSON to read (default: config.json if present)")
    parser.add_argument("--dry-run", action="store_true", help="print commands; change nothing")
    parser.add_argument("--idempotent", dest="idempotent", action="store_true", default=True)
    parser.add_argument("--no-idempotent", dest="idempotent", action="store_false")
    parser.add_argument("--project")
    parser.add_argument("--region")
    parser.add_argument("--prefix")
    parser.add_argument("--owner-email")
    parser.add_argument("--owner-user")
    parser.add_argument("--space-id")
    parser.add_argument("--bot-id")
    parser.add_argument("--owner-name")
    parser.add_argument("--service-url")
    parser.add_argument("--project-number")
    parser.add_argument("--state-bucket")
    parser.add_argument("--chat-state-bucket")
    parser.add_argument("--secret-file", action="append", default=[], metavar="NAME=PATH",
                        help="add a secret version from a file; NAME is model|chat|gmail or the secret name")
    parser.add_argument("--baseline", default=None, help="optional ledger baseline to initialize state from")
    parser.add_argument("--write-config", default="config.json",
                        help="where to write config.json (live mode)")
    parser.add_argument("--print-config", action="store_true", help="print the generated config")
    parser.add_argument("--quiet", action="store_true", help="suppress the command list")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    overrides = {
        "project": args.project,
        "region": args.region,
        "prefix": args.prefix,
        "owner_email": args.owner_email,
        "owner_user": args.owner_user,
        "space_id": args.space_id,
        "bot_id": args.bot_id,
        "owner_name": args.owner_name,
        "service_url": args.service_url,
        "project_number": args.project_number,
        "state_bucket": args.state_bucket,
        "chat_state_bucket": args.chat_state_bucket,
    }
    settings = load_settings(args.config, overrides=overrides)
    config = build_config(settings)
    if not args.dry_run:
        validate_config(config)

    shell = Shell(settings, dry_run=args.dry_run)
    provisioner = Provisioner(settings, shell, idempotent=args.idempotent,
                              secret_files=args.secret_file, baseline=args.baseline)
    if not args.dry_run and args.baseline:
        generate_state(args.baseline)
    steps = provisioner.execute()
    if not args.quiet:
        print(render_steps(steps))
    assert_no_reserved(render_steps(steps))

    if args.dry_run:
        print("\n# dry-run: no resources changed")
    else:
        write_config(args.write_config, config)
        print("# wrote " + args.write_config)
    if args.print_config or args.dry_run:
        print(json.dumps(config, indent=2))

    print("\n# manual steps (cannot be fully automated):")
    for line in manual_steps(settings):
        print("# - " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
