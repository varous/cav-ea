"""Route non-owner tasks to the relevant Chat space with a native pill @mention.

Owner tasks are mirrored to Google Tasks (E1/E3) and are never routed here. This
module is deliberately pure: the model call and all HTTP are injected by the
caller, so resolution and formatting are unit-testable without a network.

There is no API to create an assigned task; the bot posts a message that mentions
the responsible person with a native ``<users/{id}>`` pill, and the human claims
the task in the Chat UI.
"""
import json
import re


def normalize(value):
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def pill(user_id):
    return "<users/" + str(user_id) + ">"


def resolve_person(people, name):
    """Resolve an owner string to (canonical, record), 'AMBIGUOUS', or None.

    Matching precedence is by name set: canonical key, display name, aliases and
    email, all exact after whitespace/case normalisation. Exact matching keeps
    "Joy" distinct from "Joyjeet"; co-owned strings do not match a single person.
    """
    target = normalize(name)
    if not target:
        return None
    matches = []
    for canonical, record in (people or {}).items():
        names = {normalize(canonical), normalize(record.get("display_name"))}
        names |= {normalize(alias) for alias in (record.get("aliases") or [])}
        if record.get("email"):
            names.add(normalize(record["email"]))
        names.discard("")
        if target in names:
            matches.append(canonical)
    if not matches:
        return None
    if len(matches) > 1:
        return "AMBIGUOUS"
    return (matches[0], people[matches[0]])


def is_routable_space(space):
    return (space.get("spaceType") or "SPACE") in ("SPACE", "GROUP_CHAT")


def build_catalog(spaces, exclude_ids=(), contexts=None):
    """Keep spaces and group chats only; exclude DMs and the given ids."""
    contexts = contexts or {}
    excluded = {normalize(x) for x in (exclude_ids or ())}
    catalog = []
    for space in spaces or []:
        space_id = space.get("name") or space.get("space_id")
        if not space_id or normalize(space_id) in excluded:
            continue
        if not is_routable_space(space):
            continue
        name = (space.get("displayName") or "").strip()
        context = (contexts.get(space_id) or name or "").strip()
        catalog.append({"space_id": space_id, "name": name, "context": context[:280]})
    return catalog


def delivery_text(user_id, task):
    title = (task.get("title") or "").strip()
    details = re.sub(r"\s+", " ", (task.get("details") or "").strip())
    one = (" " + details[:160]).rstrip() if details else ""
    return (pill(user_id) + " please claim this task: " + title + "."
            + (one + "." if one else "")
            + " Open it and add yourself as the assignee (Chat → assign task → yourself).")


def member_user_id(members, person):
    """Resolve a Chat membership to a user id; None unless unique.

    Prefers an email match when the API surfaces one; otherwise matches the
    membership display name / aliases. Never invents an id.
    """
    emails = {normalize(person.get("email"))}
    emails.discard("")
    names = {normalize(person.get("display_name"))}
    names |= {normalize(alias) for alias in (person.get("aliases") or [])}
    names.discard("")
    found = []
    for membership in members or []:
        member = membership.get("member") or {}
        if member.get("type") != "HUMAN":
            continue
        user_id = (member.get("name") or "").split("/")[-1]
        if not user_id:
            continue
        if emails and normalize(member.get("email")) in emails:
            found.append(user_id)
        elif names and normalize(member.get("displayName")) in names:
            found.append(user_id)
    found = list(dict.fromkeys(found))
    return found[0] if len(found) == 1 else None


def match_space(text, candidates, catalog):
    """Resolve the owner's answer to a space id.

    Returns {"status": "matched", "space_id"} | {"status": "ambiguous", "matches": [...]}
    | {"status": "none"}. Numbers map to the candidate list; otherwise the full
    catalog is matched by exact name, then by unique substring.
    """
    normalized = normalize(text)
    if not normalized:
        return {"status": "none"}
    for index, candidate in enumerate(candidates or [], 1):
        if normalized == str(index):
            return {"status": "matched", "space_id": candidate["space_id"]}
    exact = [entry for entry in (catalog or []) if normalize(entry["name"]) == normalized]
    if len(exact) == 1:
        return {"status": "matched", "space_id": exact[0]["space_id"]}
    if len(exact) > 1:
        return {"status": "ambiguous", "matches": exact[:3]}
    partial = [entry for entry in (catalog or [])
               if normalize(entry["name"]) and (normalize(entry["name"]) in normalized
                                                or normalized in normalize(entry["name"]))]
    deduped = list({entry["space_id"]: entry for entry in partial}.values())
    if len(deduped) == 1:
        return {"status": "matched", "space_id": deduped[0]["space_id"]}
    if len(deduped) > 1:
        return {"status": "ambiguous", "matches": deduped[:3]}
    return {"status": "none"}


SPACE_SYSTEM = (
    "You route one operating task to the single best Google Chat space where the "
    "responsible person is likely active. Choose ONLY from the supplied catalog ids. "
    'If no space clearly fits, return space_id null. Reply with JSON only: '
    '{"space_id": <catalog id or null>, "candidates": [{"space_id": "<id>", "reason": "<short>"}]}. '
    "candidates lists the 2-3 best alternatives, best first."
)


def resolve_space(complete, task, catalog):
    """Ask the model for the best space. Returns (space_id or None, candidates).

    The result is validated against the catalog, so the model can never route to a
    space the caller did not offer.
    """
    if not catalog:
        return (None, [])
    context = {"task": {"title": task.get("title"), "details": task.get("details"), "owner": task.get("owner")},
               "spaces": catalog}
    result = complete(SPACE_SYSTEM, json.dumps(context, ensure_ascii=False))
    if not isinstance(result, dict):
        return (None, [])
    valid = {entry["space_id"] for entry in catalog}
    space_id = result.get("space_id")
    space_id = space_id if space_id in valid else None
    candidates = []
    seen = {space_id}
    for candidate in (result.get("candidates") or []):
        cid = (candidate or {}).get("space_id")
        if cid in valid and cid not in seen:
            seen.add(cid)
            candidates.append({"space_id": cid, "reason": (candidate.get("reason") or "")[:160]})
    return (space_id, candidates[:3])
