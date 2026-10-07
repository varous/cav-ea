"""Tests for the bootstrap provisioner. No network or cloud access."""
import json
import os
import pathlib
import tempfile
import unittest

from bootstrap import (
    APIS,
    SCHEDULES,
    Provisioner,
    Shell,
    build_config,
    denylist,
    load_settings,
    validate_config,
    write_config,
)

ROOT = pathlib.Path(__file__).parent

SAMPLE = {
    "project": "sample-project",
    "region": "asia-south1",
    "prefix": "ea",
    "project_number": "123456789012",
    "owner_email": "owner@example.com",
    "owner_user": "users/123456789012345678901",
    "space_id": "spaces/ABCDEFGHIJKL",
    "bot_id": "123456789012345678901",
    "state_bucket": "sample-project-ea-state",
    "chat_state_bucket": "sample-project-ea-chat-state",
}


def settings(**extra):
    overrides = dict(SAMPLE)
    overrides.update(extra)
    return load_settings(cwd=str(ROOT), env={}, overrides=overrides)


def flattened(steps):
    return "\n".join(" ".join(str(part) for part in step.argv) for step in steps)


class PlanTests(unittest.TestCase):
    def test_dry_run_plan_is_complete_and_clean(self):
        s = settings()
        steps = Provisioner(s, Shell(s, dry_run=True)).execute()
        blob = flattened(steps)
        for token in denylist():
            self.assertNotIn(token.lower(), blob.lower())
        self.assertNotIn("your-project", blob)  # overridden, not the example default

        kinds = {step.kind for step in steps}
        for kind in ("api", "bucket", "sa", "secret", "service", "topic",
                     "subscription", "events-sub", "scheduler", "config-upload"):
            self.assertIn(kind, kinds)
        self.assertEqual(len([s for s in steps if s.kind == "scheduler"]), len(SCHEDULES))
        for api in APIS:
            self.assertIn(api, blob)
        self.assertIn(s.runtime_sa, blob)
        self.assertIn(s.trigger_sa, blob)

        service = [step for step in steps if step.kind == "service"][0]
        self.assertIn("--no-allow-unauthenticated", service.argv)
        self.assertTrue(any(part.startswith("--set-secrets=") for part in service.argv))

    def test_dry_run_marks_no_skips(self):
        s = settings()
        steps = Provisioner(s, Shell(s, dry_run=True)).execute()
        self.assertFalse(any(step.skip for step in steps))

    def test_idempotent_skips_existing(self):
        s = settings()
        existing = {
            ("bucket", s.state_bucket),
            ("secret", s.model_secret),
            ("topic", s.events_topic),
            ("scheduler", s.prefix + "-briefs"),
        }
        steps = Provisioner(s, Shell(s, dry_run=True, existing=existing)).execute()
        skipped = {step.name for step in steps if step.skip}
        self.assertIn(s.state_bucket, skipped)
        self.assertIn(s.model_secret, skipped)
        self.assertIn(s.events_topic, skipped)
        self.assertIn(s.prefix + "-briefs", skipped)
        self.assertNotIn(s.work_topic, skipped)

    def test_events_subscription_body(self):
        s = settings()
        steps = Provisioner(s, Shell(s, dry_run=True)).execute()
        event = [step for step in steps if step.kind == "events-sub"][0]
        body = json.loads(event.argv[3])
        self.assertEqual(body["targetResource"], "//chat.googleapis.com/" + s.space_id)
        self.assertFalse(body["payloadOptions"]["includeResource"])
        self.assertEqual(
            body["notificationEndpoint"]["pubsubTopic"],
            "projects/" + s.project + "/topics/" + s.events_topic,
        )

    def test_secret_files_are_paths_not_values(self):
        s = settings()
        steps = Provisioner(s, Shell(s, dry_run=True),
                            secret_files=["model=/tmp/key.txt"]).execute()
        adds = [step for step in steps if step.kind == "secret-version"]
        self.assertEqual(len(adds), 1)
        self.assertIn("--data-file=/tmp/key.txt", adds[0].argv)

    def test_prefix_flows_into_resource_names(self):
        s = settings(prefix="widget")
        steps = Provisioner(s, Shell(s, dry_run=True)).execute()
        blob = flattened(steps)
        self.assertIn("widget-events-push", blob)
        self.assertIn("widget-work", blob)
        self.assertIn("widget-assistant", blob)
        self.assertIn("widget-intake-hourly", blob)


class ConfigTests(unittest.TestCase):
    def test_config_writer_roundtrip(self):
        s = settings()
        config = build_config(s)
        validate_config(config)
        self.assertEqual(config["project"], "sample-project")
        self.assertEqual(config["resource_prefix"], "ea")
        self.assertEqual(config["space_id"], "spaces/ABCDEFGHIJKL")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            write_config(path, config)
            self.assertEqual(json.loads(pathlib.Path(path).read_text()), config)
        for token in denylist():
            self.assertNotIn(token.lower(), json.dumps(config).lower())

    def test_missing_identity_rejected(self):
        with self.assertRaises(ValueError):
            validate_config({"owner_email": "", "owner_user": "users/1",
                             "space_id": "spaces/x", "bot_id": "1"})

    def test_invalid_space_rejected(self):
        with self.assertRaises(ValueError):
            validate_config({"owner_email": "owner@example.com",
                             "owner_user": "users/1", "space_id": "room/x", "bot_id": "1"})


class SchedulerTests(unittest.TestCase):
    def test_schedule_specs(self):
        names = [name for name, _, _ in SCHEDULES]
        self.assertEqual(names, ["intake-hourly", "renew-hourly", "briefs", "briefs-daytime"])
        crons = {name: cron for name, cron, _ in SCHEDULES}
        self.assertEqual(crons["intake-hourly"], "7 * * * *")
        self.assertEqual(crons["renew-hourly"], "15 * * * *")
        self.assertEqual(crons["briefs"], "0 9 * * *")
        self.assertEqual(crons["briefs-daytime"], "0 10-22 * * 1-6")


class CIWorkflowTests(unittest.TestCase):
    def test_workflow_present_and_sane(self):
        path = ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(path.exists(), "ci.yml missing")
        text = path.read_text()
        for needle in ("push", "pull_request", "3.11", "unittest discover",
                       "actions/checkout", "actions/setup-python"):
            self.assertIn(needle, text)
        try:
            import yaml
        except ImportError:
            return
        data = yaml.safe_load(text)
        triggers = data.get("on", data.get(True))
        self.assertIsNotNone(triggers)
        self.assertIn("jobs", data)


if __name__ == "__main__":
    unittest.main()
