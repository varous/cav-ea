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

    def test_resolved_space_delivers_with_pill(self):
        r = self.runtime(PEOPLE)
        r.space_catalog = lambda: [{"space_id": "spaces/S1", "name": "Ops", "context": "ops"}]
        r.model_json = lambda s, u: {"space_id": "spaces/S1", "candidates": []}
        r.member_user_id = lambda space, person: "555"
        posted = {}
        r.post_to_space = lambda space, text, mid, key: posted.update(space=space, text=text) or space + "/messages/M1"
        state = {"tasks": {"T-4": {"id": "T-4", "owner": "Joyjeet", "title": "Fix", "details": ""}}, "outbox": {}}
        r.route(state, "T-4")
        task = state["tasks"]["T-4"]
        self.assertEqual(task["route_status"], "delivered")
        self.assertEqual(task["route_message_id"], "spaces/S1/messages/M1")
        self.assertTrue(posted["text"].startswith("<users/555> please claim this task:"))

    def test_owner_reply_resolves_awaiting_task(self):
        r = self.runtime(PEOPLE)
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


if __name__ == "__main__":
    unittest.main()
