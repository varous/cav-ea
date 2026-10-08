"""Space routing tests (people map, catalog, resolver, delivery). No network."""
import unittest

import routing
from operating import apply_changes, import_ledger

PEOPLE = {
    "Alex": {"email": "alex@example.com", "display_name": "Alex Owner", "aliases": []},
    "Joy": {"email": "joy@example.com", "display_name": "Joy Yadav", "aliases": ["Joy Yadav"]},
    "Joyjeet": {"email": "joyjeet@example.com", "display_name": "Joyjeet Das", "aliases": []},
    "Akash": {"email": "akash@example.com", "display_name": "Akashdeep Majumdar",
              "aliases": ["Akashdeep", "Akash Deep"]},
}

LEDGER = """| ID | Project / task | Owner | Deadline | Dependency |
|---|---|---|---|---|
| T-101 | Fix the thing | PERSON_B | 2026-10-05 | dep |
### C-001 — x
"""


class People(unittest.TestCase):
    def test_full_name_first_name_alias_email(self):
        self.assertEqual(routing.resolve_person(PEOPLE, "Alex Owner")[0], "Alex")
        self.assertEqual(routing.resolve_person(PEOPLE, "Alex")[0], "Alex")
        self.assertEqual(routing.resolve_person(PEOPLE, "Joy Yadav")[0], "Joy")
        self.assertEqual(routing.resolve_person(PEOPLE, "Akash Deep")[0], "Akash")
        self.assertEqual(routing.resolve_person(PEOPLE, "akash@example.com")[0], "Akash")

    def test_joy_is_not_joyjeet(self):
        self.assertEqual(routing.resolve_person(PEOPLE, "Joy")[0], "Joy")
        self.assertEqual(routing.resolve_person(PEOPLE, "Joyjeet")[0], "Joyjeet")

    def test_unknown_and_ambiguous(self):
        self.assertIsNone(routing.resolve_person(PEOPLE, "Nobody Known"))
        self.assertIsNone(routing.resolve_person(PEOPLE, ""))
        ambiguous = {"A": {"display_name": "Same Name"}, "B": {"aliases": ["Same Name"]}}
        self.assertEqual(routing.resolve_person(ambiguous, "Same Name"), "AMBIGUOUS")

    def test_co_owned_is_not_a_single_person(self):
        self.assertIsNone(routing.resolve_person(PEOPLE, "Alex Owner + Joyjeet Das"))


class Catalog(unittest.TestCase):
    def test_keeps_spaces_and_groups_excludes_dms_and_ids(self):
        spaces = [
            {"name": "spaces/S1", "displayName": "Ops", "spaceType": "SPACE"},
            {"name": "spaces/G1", "displayName": "Team", "spaceType": "GROUP_CHAT"},
            {"name": "spaces/D1", "displayName": "DM", "spaceType": "DIRECT_MESSAGE"},
            {"name": "spaces/ME", "displayName": "my-ea", "spaceType": "SPACE"},
        ]
        catalog = routing.build_catalog(spaces, exclude_ids=["spaces/ME"])
        ids = [entry["space_id"] for entry in catalog]
        self.assertEqual(ids, ["spaces/S1", "spaces/G1"])

    def test_context_uses_supplied_snippet(self):
        spaces = [{"name": "spaces/S1", "displayName": "Ops", "spaceType": "SPACE"}]
        catalog = routing.build_catalog(spaces, contexts={"spaces/S1": "load-in crew chatter"})
        self.assertEqual(catalog[0]["context"], "load-in crew chatter")


class Resolver(unittest.TestCase):
    CATALOG = [{"space_id": "spaces/S1", "name": "Ops", "context": "ops"},
               {"space_id": "spaces/S2", "name": "Stage", "context": "stage"}]

    def test_valid_space(self):
        complete = lambda s, u: {"space_id": "spaces/S2", "candidates": [{"space_id": "spaces/S1"}]}
        space_id, candidates = routing.resolve_space(complete, {"title": "x"}, self.CATALOG)
        self.assertEqual(space_id, "spaces/S2")
        self.assertEqual(candidates[0]["space_id"], "spaces/S1")

    def test_out_of_catalog_id_is_rejected(self):
        complete = lambda s, u: {"space_id": "spaces/NOPE", "candidates": []}
        self.assertEqual(routing.resolve_space(complete, {"title": "x"}, self.CATALOG)[0], None)

    def test_none_returns_candidates(self):
        complete = lambda s, u: {"space_id": None, "candidates": [{"space_id": "spaces/S1", "reason": "r"}]}
        space_id, candidates = routing.resolve_space(complete, {"title": "x"}, self.CATALOG)
        self.assertIsNone(space_id)
        self.assertEqual(candidates[0]["space_id"], "spaces/S1")

    def test_candidates_capped_and_deduped(self):
        complete = lambda s, u: {"space_id": "spaces/S1", "candidates": [{"space_id": "spaces/S1"},
                                                                          {"space_id": "spaces/S2"},
                                                                          {"space_id": "x"},
                                                                          {"space_id": "spaces/S2"}]}
        _, candidates = routing.resolve_space(complete, {"title": "x"}, self.CATALOG)
        self.assertEqual([c["space_id"] for c in candidates], ["spaces/S2"])


class Delivery(unittest.TestCase):
    def test_pill_and_text(self):
        self.assertEqual(routing.pill("12345"), "<users/12345>")
        text = routing.delivery_text("12345", {"title": "Fix the thing", "details": "line one\nline two"})
        self.assertTrue(text.startswith("<users/12345> please claim this task: Fix the thing."))
        self.assertIn("line one line two", text)
        self.assertIn("assignee", text)

    def test_member_match_is_unique_and_display_name_based(self):
        members = [{"member": {"type": "HUMAN", "name": "users/111", "displayName": "Joy Yadav"}},
                   {"member": {"type": "BOT", "name": "users/999", "displayName": "Joy Yadav"}},
                   {"member": {"type": "HUMAN", "name": "users/222", "displayName": "Someone Else"}}]
        person = {"display_name": "Joy Yadav", "aliases": []}
        self.assertEqual(routing.member_user_id(members, person), "111")
        dup = [{"member": {"type": "HUMAN", "name": "users/1", "displayName": "Joy Yadav"}},
               {"member": {"type": "HUMAN", "name": "users/2", "displayName": "Joy Yadav"}}]
        self.assertIsNone(routing.member_user_id(dup, person))

    def test_member_match_prefers_email_when_present(self):
        members = [{"member": {"type": "HUMAN", "name": "users/333", "email": "joy@example.com",
                               "displayName": "Totally Different Name"}}]
        person = {"email": "joy@example.com", "display_name": "Joy Yadav", "aliases": []}
        self.assertEqual(routing.member_user_id(members, person), "333")


class OwnerTaskNotRouted(unittest.TestCase):
    def runtime(self, people):
        from app import Runtime
        r = object.__new__(Runtime)
        r.settings = {"owner_name": "Alex Owner", "owner_email": "alex@example.com", "people": people}
        r.space = "spaces/ME"
        r.owner = "users/1"
        return r

    def test_owner_task_is_left_alone(self):
        r = self.runtime(PEOPLE)
        state = {"tasks": {"T-1": {"id": "T-1", "owner": "Alex Owner", "title": "x", "details": ""}}, "outbox": {}}
        r.route(state, "T-1")
        self.assertNotIn("route_status", state["tasks"]["T-1"])

    def test_unknown_owner_is_unresolved_without_network(self):
        r = self.runtime(PEOPLE)
        state = {"tasks": {"T-2": {"id": "T-2", "owner": "Nobody Known", "title": "x", "details": ""}}, "outbox": {}}
        r.route(state, "T-2")
        self.assertEqual(state["tasks"]["T-2"]["route_status"], "unresolved_person")

    def test_none_space_asks_owner_and_defers(self):
        r = self.runtime(PEOPLE)
        r.space_catalog = lambda: [{"space_id": "spaces/S1", "name": "Ops", "context": "ops"}]
        r.model_json = lambda s, u: {"space_id": None, "candidates": [{"space_id": "spaces/S1", "reason": "r"}]}
        state = {"tasks": {"T-3": {"id": "T-3", "owner": "Joyjeet", "title": "Fix", "details": ""}}, "outbox": {}}
        r.route(state, "T-3")
        self.assertEqual(state["tasks"]["T-3"]["route_status"], "awaiting")
        self.assertEqual(len(state["outbox"]), 1)
        self.assertIn("Ops", list(state["outbox"].values())[0]["text"])

    def test_resolved_candidate_is_asked_never_auto_delivered(self):
        r = self.runtime(PEOPLE)
        r.space_catalog = lambda: [{"space_id": "spaces/S1", "name": "Ops", "context": "ops"}]
        r.model_json = lambda s, u: {"space_id": "spaces/S1", "candidates": []}
        state = {"tasks": {"T-4": {"id": "T-4", "owner": "Joyjeet", "title": "Fix", "details": ""}}, "outbox": {}}
        r.route(state, "T-4")
        task = state["tasks"]["T-4"]
        self.assertEqual(task["route_status"], "awaiting")   # always ask, no auto-delivery
        self.assertEqual(len(state["outbox"]), 1)
        self.assertIn("Which group should this task go to?", list(state["outbox"].values())[0]["text"])
        r.route(state, "T-4")                                # idempotent: no duplicate ask
        self.assertEqual(len(state["outbox"]), 1)
        r.member_user_id = lambda space, person: "555"
        posted = {}
        r.post_to_space = lambda space, text, mid, key: posted.update(space=space, text=text) or space + "/messages/M1"
        r.apply_route_reply(state, "1")                      # owner answers the number
        self.assertEqual(task["route_status"], "delivered")
        self.assertEqual(task["route_message_id"], "spaces/S1/messages/M1")
        self.assertTrue(posted["text"].startswith("<users/555> please claim this task:"))

    def test_owner_reply_resolves_awaiting_task(self):
        r = self.runtime(PEOPLE)
        r.space_catalog = lambda: [{"space_id": "spaces/S1", "name": "Ops", "context": "ops"}]
        r.member_user_id = lambda space, person: "777"
        delivered = {}
        r.post_to_space = lambda space, text, mid, key: delivered.update(space=space) or space + "/messages/M2"
        state = {"tasks": {"T-5": {"id": "T-5", "owner": "Joyjeet", "title": "Fix", "details": "",
                                   "route_status": "awaiting",
                                   "route_candidates": [{"space_id": "spaces/S1", "name": "Ops"}]}}, "outbox": {}}
        r.apply_route_reply(state, "put it in Ops")
        self.assertEqual(state["tasks"]["T-5"]["route_status"], "delivered")
        self.assertEqual(delivered["space"], "spaces/S1")


class RouteFailureIsolation(unittest.TestCase):
    def test_route_failure_does_not_fail_change(self):
        state = import_ledger(LEDGER)
        changes = [{"kind": "update", "task_id": "T-101", "field": "owner", "value": "PERSON_B",
                    "evidence_quote": "assign", "reason": "assign"}]

        def boom(state, task_id):
            raise RuntimeError("ROUTE_UNAVAILABLE")

        applied = apply_changes(state, changes, "src", "Alex", None, boom)
        self.assertEqual(applied["tasks"]["T-101"]["owner"], "PERSON_B")
        self.assertEqual(applied["tasks"]["T-101"]["route_error"], "ROUTE_UNAVAILABLE")


class SpaceMatching(unittest.TestCase):
    CATALOG = [{"space_id": "S1", "name": "Ops Alpha"},
               {"space_id": "S2", "name": "Ops Beta"},
               {"space_id": "S3", "name": "Stage"}]

    def test_number_maps_to_candidate(self):
        self.assertEqual(routing.match_space("2", [{"space_id": "X"}, {"space_id": "Y"}], self.CATALOG),
                         {"status": "matched", "space_id": "Y"})

    def test_exact_name(self):
        self.assertEqual(routing.match_space("stage", [], self.CATALOG), {"status": "matched", "space_id": "S3"})

    def test_unique_substring(self):
        self.assertEqual(routing.match_space("post it to stage please", [], self.CATALOG),
                         {"status": "matched", "space_id": "S3"})

    def test_ambiguous_substring(self):
        self.assertEqual(routing.match_space("ops", [], self.CATALOG)["status"], "ambiguous")

    def test_unknown(self):
        self.assertEqual(routing.match_space("nowhere", [], self.CATALOG)["status"], "none")


class AskFlow(unittest.TestCase):
    def runtime(self):
        from app import Runtime
        r = object.__new__(Runtime)
        r.settings = {"owner_name": "Alex Owner", "owner_email": "alex@example.com", "people": PEOPLE}
        r.space = "spaces/ME"
        r.owner = "users/1"
        return r

    def task(self, tid="T-9"):
        return {"id": tid, "owner": "Joyjeet", "title": "Fix the thing", "details": ""}

    def test_exact_name_question_when_no_candidates(self):
        r = self.runtime()
        r.space_catalog = lambda: []
        r.model_json = lambda s, u: {"space_id": None, "candidates": []}
        state = {"tasks": {"T-9": self.task()}, "outbox": {}}
        r.route(state, "T-9")
        self.assertIn("exact group name", list(state["outbox"].values())[0]["text"])

    def test_numbered_list_question(self):
        r = self.runtime()
        r.space_catalog = lambda: [{"space_id": "S1", "name": "Stage", "context": ""}]
        r.model_json = lambda s, u: {"space_id": "S1", "candidates": []}
        state = {"tasks": {"T-9": self.task()}, "outbox": {}}
        r.route(state, "T-9")
        text = list(state["outbox"].values())[0]["text"]
        self.assertIn("1. Stage", text)

    def test_name_answer_resolves_and_delivers(self):
        r = self.runtime()
        r.space_catalog = lambda: [{"space_id": "spaces/S1", "name": "Stage", "context": ""}]
        r.member_user_id = lambda space, person: "42"
        r.post_to_space = lambda space, text, mid, key: space + "/messages/M9"
        state = {"tasks": {"T-9": {**self.task(), "route_status": "awaiting", "route_candidates": []}}, "outbox": {}}
        r.apply_route_reply(state, "put it in Stage")
        self.assertEqual(state["tasks"]["T-9"]["route_status"], "delivered")
        self.assertEqual(state["tasks"]["T-9"]["route_space_id"], "spaces/S1")

    def test_ambiguous_reasks(self):
        r = self.runtime()
        catalog = [{"space_id": "S1", "name": "Ops Alpha", "context": ""},
                   {"space_id": "S2", "name": "Ops Beta", "context": ""}]
        r.space_catalog = lambda: catalog
        state = {"tasks": {"T-9": {**self.task(), "route_status": "awaiting", "route_candidates": []}}, "outbox": {}}
        r.apply_route_reply(state, "ops")
        self.assertEqual(state["tasks"]["T-9"]["route_status"], "awaiting")
        self.assertEqual(state["tasks"]["T-9"]["route_ask_count"], 1)
        self.assertEqual(len(state["outbox"]), 1)

    def test_unknown_does_not_reask(self):
        r = self.runtime()
        r.space_catalog = lambda: [{"space_id": "S1", "name": "Ops", "context": ""}]
        state = {"tasks": {"T-9": {**self.task(), "route_status": "awaiting", "route_candidates": []}}, "outbox": {}}
        r.apply_route_reply(state, "nowhere at all")
        self.assertEqual(state["tasks"]["T-9"].get("route_ask_count", 0), 0)
        self.assertEqual(len(state["outbox"]), 0)

    def test_member_not_found_notifies_owner(self):
        r = self.runtime()
        r.space_catalog = lambda: [{"space_id": "spaces/S1", "name": "Stage", "context": ""}]
        r.member_user_id = lambda space, person: None
        state = {"tasks": {"T-9": {**self.task(), "route_status": "awaiting", "route_candidates": []}}, "outbox": {}}
        r.apply_route_reply(state, "Stage")
        self.assertEqual(state["tasks"]["T-9"]["route_status"], "unresolved_member")
        self.assertIn("couldn't find", list(state["outbox"].values())[0]["text"])

    def test_create_asks_exactly_once(self):
        r = self.runtime()
        r.space_catalog = lambda: [{"space_id": "S1", "name": "Ops", "context": ""}]
        r.model_json = lambda s, u: {"space_id": "S1", "candidates": []}
        state = {"tasks": {"T-9": self.task()}, "outbox": {}}
        r.route(state, "T-9")                                          # create -> one ask
        r.apply_route_reply(state, "Assign a demo task to Joyjeet")    # the create message itself
        self.assertEqual(len(state["outbox"]), 1)
        self.assertEqual(state["tasks"]["T-9"]["route_status"], "awaiting")

    def test_unrelated_message_does_not_reask(self):
        r = self.runtime()
        r.space_catalog = lambda: [{"space_id": "S1", "name": "Ops", "context": ""}]
        state = {"tasks": {"T-9": {**self.task(), "route_status": "awaiting", "route_candidates": []}}, "outbox": {}}
        r.apply_route_reply(state, "Please review the quarterly numbers for the event and budget before Friday")
        self.assertEqual(len(state["outbox"]), 0)

    def test_post_403_notifies_and_does_not_raise(self):
        r = self.runtime()
        r.space_catalog = lambda: [{"space_id": "spaces/S1", "name": "Stage", "context": ""}]
        r.member_user_id = lambda space, person: "42"

        def boom(space, text, mid, key):
            raise RuntimeError("GOOGLE_HTTP_403")

        r.post_to_space = boom
        state = {"tasks": {"T-9": {**self.task(), "route_status": "awaiting", "route_candidates": []}}, "outbox": {}}
        r.apply_route_reply(state, "Stage")
        task = state["tasks"]["T-9"]
        self.assertEqual(task["route_status"], "error")
        self.assertEqual(task["route_error"], "GOOGLE_HTTP_403")
        self.assertIn("is the bot a member", list(state["outbox"].values())[0]["text"])


class CatalogExclude(unittest.TestCase):
    SPACES = [{"name": "spaces/T1", "displayName": "test_taskbot", "spaceType": "SPACE"},
              {"name": "spaces/T2", "displayName": "Bot_tests", "spaceType": "GROUP_CHAT"},
              {"name": "spaces/T3", "displayName": "Demo Space", "spaceType": "SPACE"},
              {"name": "spaces/G1", "displayName": "general-team-room", "spaceType": "SPACE"},
              {"name": "spaces/O1", "displayName": "ops-room", "spaceType": "SPACE"}]

    def test_default_pattern_excludes_test_bot_demo(self):
        ids = [entry["space_id"] for entry in routing.build_catalog(self.SPACES)]
        self.assertNotIn("spaces/T1", ids)
        self.assertNotIn("spaces/T2", ids)
        self.assertNotIn("spaces/T3", ids)
        self.assertIn("spaces/O1", ids)

    def test_config_pattern_and_id_excludes(self):
        by_pattern = [e["space_id"] for e in routing.build_catalog(self.SPACES, exclude={"patterns": ["general-team-room"]})]
        self.assertNotIn("spaces/G1", by_pattern)
        by_id = [e["space_id"] for e in routing.build_catalog(self.SPACES, exclude={"ids": ["spaces/O1"]})]
        self.assertNotIn("spaces/O1", by_id)


class ReplyContext(unittest.TestCase):
    def test_route_fields_absent_from_reply_context(self):
        from app import tasks_for_context
        s = import_ledger(LEDGER)
        s["tasks"]["T-101"].update({"route_status": "awaiting", "route_candidates": [{"space_id": "x"}],
                                    "route_message_id": "m", "route_target": "P", "route_error": "E",
                                    "route_ask_count": 1, "google_task_id": "g", "google_synced": {"title": "x"}})
        out = tasks_for_context(s, "T-101 fix", "conversation")
        task = out["T-101"]
        self.assertIsInstance(task, dict)
        for key in task:
            self.assertFalse(key.startswith("route_"))
            self.assertNotIn(key, ("google_task_id", "google_synced"))


if __name__ == "__main__":
    unittest.main()
