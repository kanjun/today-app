"""Goals: open items grouped by one accomplishable outcome, with an ordered chain of steps.

Three passes keep them current:
- ``consolidate`` (every morning, and to start): looks at every open item and every goal,
  merges, splits, closes, and orders. The only pass allowed to reshuffle.
- ``assign`` (every sync): puts new items into an existing goal, or pairs them into a new
  one; never moves anything already placed.
- ``synthesize`` (when a goal's items change): rewrites that goal's chain.

The user's corrections (an item taken out of a goal, moved into one, a goal renamed) are
kept in ``state["corrections"]`` and every pass must respect them.
"""

import json
import uuid
from datetime import datetime
from typing import Any

from today import ai, calendar_events, config, gtd_link, store

FINISHED = ("sent", "done", "cleared", "skipped")

RULES_TEMPLATE = """Goals are SMART-style: each is one clearly accomplishable outcome with a plain end state (when it is true, the goal is done), like "2025 taxes filed and the federal balance paid" or "Sam's offer signed". Add a date only when one is actually known. A category ("Introductions", "Sam's asks", "Team health", "Admin") is NOT a goal: if items only share a category, type of task, or person, they are separate goals or standalone. Test every item: finishing it must directly move THIS goal's end state closer. If it would not, it does not belong.
Within that test, do not be timid: {name} wants related work pulled together. Good goals: "2025 taxes filed and paid" (questionnaire, signatures, payment); "Priya's role on the new team settled" (reading her proposal doc, and talking with her manager about timing); "Beta customers onboarded". A person's situation, a filing, a deal, an event, or a decision with several asks around it is a goal.
Group generously within that rule: whenever two or more items serve the same accomplishable outcome (the same filing, deal, event, hire, person's situation, launch, or decision), put them together. A goal needs at least two items.
Each goal has a CHAIN: the ordered steps to reach the end state. Every item key goes in exactly one step: the step that finishes it. Merge items that are the same action into one step (two people asking whether {name} signed the same forms is one step). Order steps by what depends on what (answer the questionnaire, then sign, then pay). Step text is a short action for {name}, verb first. "who" is "you" unless the step is someone else's to do.
{Possessive} calendar is given: use it to tell events apart. Asks about different events (different dates, hosts, or guests) are different goals, even when both are dinners; an ask about an event already on the calendar is about that event.
{Possessive} corrections always win: an item they took out of a goal must not go back into it; an item they moved into a goal stays there; a goal they renamed keeps their title."""


def rules() -> str:
    own = config.possessive()
    return RULES_TEMPLATE.format(
        name=config.name(), Possessive=own[0].upper() + own[1:]
    )


def _system(task: str) -> str:
    return f"{task} Answer with one JSON object only.\n" + rules()


def _row(i: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": i["key"],
        "who": i.get("who"),
        "title": i.get("title"),
        "ask": (i.get("ask_text") or "")[:240],
        "notes": (i.get("did") or "")[:240],
        "state": i.get("state"),
        "source": i["source"],
        "asked_at": i.get("asked_at"),
    }


def open_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        i
        for i in state["items"].values()
        if i["status"] not in FINISHED
        and i.get("state")
        and not i.get("manual")
        # A no is its own section, never a step toward a goal.
        and not i.get("no")
    ]


def _goal_brief(g: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": g["id"],
        "title": g["title"],
        "done_when": g.get("done_when", ""),
        "by": g.get("by", ""),
        "renamed_by_user": bool(g.get("user_title")),
        "items": [k for s in g["chain"] for k in s["covers"]],
    }


def _clean_chain(chain: Any, allowed: set[str]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for s in chain or []:
        if not isinstance(s, dict):
            continue
        covers = [k for k in s.get("covers", []) if k in allowed and k not in seen]
        seen.update(covers)
        if covers:
            out.append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "step": str(s.get("step", ""))[:140],
                    "who": str(s.get("who") or "you"),
                    "covers": covers,
                }
            )
    return out


def consolidate(state: dict[str, Any]) -> dict[str, Any]:
    """The full pass. Returns {"goals": {...}, "standalone": [...]} for the caller to store."""
    items = open_items(state)
    keys = {i["key"] for i in items}
    current = [g for g in state.get("goals", {}).values() if g["status"] == "open"]
    if not items and not current:
        return {"goals": {}, "standalone": []}  # nothing to organize; no model call
    prompt = (
        f"Today is {datetime.now().strftime('%A, %B %d, %Y')}.\nExisting projects: {json.dumps(gtd_link.projects())}\n"
        f"{calendar_events.brief()}"
        f"Current goals (keep their ids when a goal continues; you may merge, split, rename unless renamed by the user, or drop):\n{json.dumps([_goal_brief(g) for g in current], ensure_ascii=False)}\n"
        f"The user's corrections:\n{json.dumps(state.get('corrections', [])[-80:], ensure_ascii=False)}\n"
        f"Open items:\n{json.dumps([_row(i) for i in items], ensure_ascii=False)}\n\n"
        'Answer: {"goals": [{"id": "existing id or empty for a new goal", "title": "...", "done_when": "one sentence end state", "by": "YYYY-MM-DD or empty", '
        '"chain": [{"step": "...", "who": "you", "covers": ["key"]}]}], "standalone": ["key"]}'
    )
    answer = ai.complete_json(
        prompt,
        system=_system(
            f"You organize {config.who_they_are()}'s open obligations into goals."
        ),
        timeout=900,
    )
    by_id = {g["id"]: g for g in current}
    goals: dict[str, Any] = {}
    placed: set[str] = set()
    for g in answer.get("goals", []) or []:
        chain = _clean_chain(g.get("chain"), keys - placed)
        if sum(len(s["covers"]) for s in chain) < 2:
            continue
        old = by_id.get(str(g.get("id") or ""))
        gid = old["id"] if old else "g" + uuid.uuid4().hex[:7]
        title = (
            old["title"]
            if old and old.get("user_title")
            else str(g.get("title") or "")[:90]
        )
        goals[gid] = {
            "id": gid,
            "title": title,
            "user_title": bool(old and old.get("user_title")),
            "done_when": str(g.get("done_when") or ""),
            "by": str(g.get("by") or ""),
            "chain": chain,
            "status": "open",
            "created": old["created"] if old else store.now_iso(),
            "updated": store.now_iso(),
        }
        placed.update(k for s in chain for k in s["covers"])
    return {"goals": goals, "standalone": sorted(keys - placed)}


def assign(state: dict[str, Any], new_keys: list[str]) -> dict[str, Any]:
    """Place new items: into an existing goal, or into a new goal with other unplaced items."""
    items = {i["key"]: i for i in open_items(state)}
    new = [items[k] for k in new_keys if k in items]
    if not new:
        return {"into": {}, "new_goals": []}
    goals = [g for g in state.get("goals", {}).values() if g["status"] == "open"]
    loose = [
        i for i in items.values() if not i.get("goal_id") and i["key"] not in new_keys
    ]
    prompt = (
        f"{calendar_events.brief()}"
        f"Goals:\n{json.dumps([_goal_brief(g) for g in goals], ensure_ascii=False)}\n"
        f"The user's corrections:\n{json.dumps(state.get('corrections', [])[-80:], ensure_ascii=False)}\n"
        f"Items not in any goal yet:\n{json.dumps([_row(i) for i in loose], ensure_ascii=False)}\n"
        f"NEW items to place:\n{json.dumps([_row(i) for i in new], ensure_ascii=False)}\n\n"
        "Place each NEW item: into an existing goal (by id), into a new goal together with at least one other NEW item or item not in any goal, or nowhere. "
        'Answer: {"into": {"<new item key>": "<goal id>"}, "new_goals": [{"title": "...", "done_when": "...", "by": "", "covers": ["key", "key"]}]}'
    )
    answer = ai.complete_json(
        prompt,
        system=_system(
            f"You keep {config.who_they_are()}'s goals up to date as new obligations arrive."
        ),
    )
    ids = {g["id"] for g in goals}
    into = {
        k: v
        for k, v in (answer.get("into") or {}).items()
        if k in new_keys and v in ids
    }
    allowed = {i["key"] for i in loose} | set(new_keys)
    new_goals = []
    for g in answer.get("new_goals", []) or []:
        covers = [k for k in g.get("covers", []) if k in allowed and k not in into]
        if len(covers) >= 2:
            new_goals.append(
                {
                    "title": str(g.get("title") or "")[:90],
                    "done_when": str(g.get("done_when") or ""),
                    "by": str(g.get("by") or ""),
                    "covers": covers,
                }
            )
    return {"into": into, "new_goals": new_goals}


def synthesize(
    goal: dict[str, Any], items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rewrite one goal's chain for its current items."""
    prompt = (
        f"Goal: {goal['title']}\nDone when: {goal.get('done_when', '')}\nBy: {goal.get('by', '')}\n"
        f"Its items:\n{json.dumps([_row(i) for i in items], ensure_ascii=False)}\n\n"
        'Write the chain. Answer: {"chain": [{"step": "...", "who": "you", "covers": ["key"]}]}'
    )
    answer = ai.complete_json(
        prompt,
        system=_system(
            f"You order the steps toward one of {config.who_they_are()}'s goals."
        ),
    )
    chain = _clean_chain(answer.get("chain"), {i["key"] for i in items})
    missing = [
        i["key"]
        for i in items
        if i["key"] not in {k for s in chain for k in s["covers"]}
    ]
    for k in missing:  # never drop an item silently
        it = next(i for i in items if i["key"] == k)
        chain.append(
            {
                "id": uuid.uuid4().hex[:8],
                "step": it.get("title") or "",
                "who": "you",
                "covers": [k],
            }
        )
    return chain


# ---- reading a goal's progress --------------------------------------------------


def progress(goal: dict[str, Any], items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Which steps are done, which is next, and who is waiting."""
    steps = []
    next_index = None
    for n, s in enumerate(goal["chain"]):
        members = [items[k] for k in s["covers"] if k in items]
        done = bool(members) and all(m["status"] in FINISHED for m in members)
        if not done and next_index is None:
            next_index = n
        steps.append(dict(s, done=done))
    open_members = [
        items[k]
        for s in goal["chain"]
        for k in s["covers"]
        if k in items and items[k]["status"] not in FINISHED
    ]
    waiting = sorted({m.get("who") or "" for m in open_members} - {""})
    oldest = min((m.get("asked_at") or "" for m in open_members), default="")
    return {
        "steps": steps,
        "next_index": next_index,
        "waiting": waiting,
        "oldest": oldest,
        "done": next_index is None and bool(steps),
    }


def goal_context(goal: dict[str, Any], items: dict[str, dict[str, Any]]) -> str:
    """What the assistant should know about the goal when working one of its steps."""
    p = progress(goal, items)
    lines = [
        f'This is part of {config.possessive()} goal "{goal["title"]}" (done when: {goal.get("done_when", "")}).'
    ]
    for n, s in enumerate(p["steps"], 1):
        lines.append(f"  Step {n} ({'done' if s['done'] else 'not done'}): {s['step']}")
    known = [
        f"  - {it.get('title', '')}: {it.get('cleared_reason') or (f'{config.name()} marked it done' if it['status'] in ('done', 'sent') else 'settled')}"
        for s in goal.get("chain", [])
        for k in s["covers"]
        if (it := items.get(k)) and it.get("status") in FINISHED
    ]
    if known:
        lines.append("Finished items in this goal, and how they were settled:")
        lines.extend(known)
    lines.append(
        "Take the earlier steps' status into account; finished steps happened. If a finished item shows this one is already done too (someone paid or signed it), resolve this one."
    )
    return "\n".join(lines)


def duplicates(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Open items for the same person that are one action (a promise restating the ask,
    the same ask captured twice). Returns [{"keep": key, "drop": [key, ...]}]; only
    looks at people whose set of open items changed since the last look."""
    by_who: dict[str, list[dict[str, Any]]] = {}
    for i in open_items(state):
        who = (i.get("who") or "").strip().lower()
        if who:
            by_who.setdefault(who, []).append(i)
    seen = state.get("dup_checked", {})
    groups = {
        who: items
        for who, items in by_who.items()
        if len(items) > 1 and seen.get(who) != sorted(i["key"] for i in items)
    }
    if not groups:
        return []
    rows = {
        who: [
            dict(
                _row(i), kind=i.get("kind", "asked"), draft=(i.get("draft") or "")[:200]
            )
            for i in items
        ]
        for who, items in groups.items()
    }
    answer = ai.complete_json(
        "Each group is the open items for one person.\n"
        f"{json.dumps(rows, ensure_ascii=False)}\n\n"
        f"Within each group, find items that are really ONE action for {config.name()}: doing one would also do the other "
        "(a promise they made that restates what they asked, the same ask captured twice, two steps that one reply covers). "
        "Different things for the same person stay separate. For each set of duplicates keep the one with the best draft "
        "or plan (prefer the item that is what they asked). "
        'Answer: {"same": [{"keep": "key", "drop": ["key"]}]}',
        system=f"You find duplicate to-dos in {config.possessive()} list. Answer with one JSON object only.",
    )
    keys = {i["key"] for items in groups.values() for i in items}
    out = []
    for s in answer.get("same") or []:
        keep = str(s.get("keep") or "")
        drop = [str(k) for k in s.get("drop") or [] if k in keys and k != keep]
        if keep in keys and drop:
            out.append({"keep": keep, "drop": drop})
    state_seen = {who: sorted(i["key"] for i in items) for who, items in groups.items()}
    return [{"seen": state_seen}] + out


def apply_duplicates(state: dict[str, Any], result: list[dict[str, Any]]) -> list[str]:
    """Settle the dropped copies, pointing at the one kept. Returns goals that changed."""
    touched = set()
    for r in result:
        if "seen" in r:
            state.setdefault("dup_checked", {}).update(r["seen"])
            continue
        keep = state["items"].get(r["keep"])
        if not keep or keep["status"] in FINISHED:
            continue
        for k in r["drop"]:
            item = state["items"].get(k)
            if not item or item["status"] in FINISHED:
                continue
            item.update(
                status="skipped",
                cleared_reason=f"same as: {keep.get('title', '')}",
                cleared_at=store.now_iso(),
                dup_of=r["keep"],
            )
            if k in state["today"]["order"]:
                state["today"]["order"].remove(k)
            if item.get("goal_id"):
                touched.add(item["goal_id"])
                _take_out(state, k)
    return sorted(touched)


# ---- applying results to the state (call inside store.mutate) ---------------------


def _enforce_corrections(state: dict[str, Any]) -> None:
    goals = state.setdefault("goals", {})
    for c in state.get("corrections", []):
        item = state["items"].get(c.get("item", ""))
        if item is None or item["status"] in FINISHED:
            continue
        if c["kind"] == "remove" and item.get("goal_id") == c.get("goal"):
            _take_out(state, item["key"])
        elif (
            c["kind"] == "move"
            and c.get("goal") in goals
            and goals[c["goal"]]["status"] == "open"
            and item.get("goal_id") != c["goal"]
        ):
            _take_out(state, item["key"])
            _put_in(state, item["key"], c["goal"])


def _take_out(state: dict[str, Any], key: str) -> str | None:
    item = state["items"][key]
    gid = item.get("goal_id")
    goal = state.get("goals", {}).get(gid or "")
    if goal:
        for s in goal["chain"]:
            if key in s["covers"]:
                s["covers"].remove(key)
        goal["chain"] = [s for s in goal["chain"] if s["covers"]]
        goal["dirty"] = True
    item["goal_id"] = None
    item["placed"] = True
    return gid


def _put_in(state: dict[str, Any], key: str, gid: str) -> None:
    goal = state["goals"][gid]
    item = state["items"][key]
    goal["chain"].append(
        {
            "id": uuid.uuid4().hex[:8],
            "step": item.get("title") or "",
            "who": "you",
            "covers": [key],
        }
    )
    goal["dirty"] = True
    item["goal_id"] = gid
    item["placed"] = True


def apply_consolidation(state: dict[str, Any], result: dict[str, Any]) -> None:
    goals = state.setdefault("goals", {})
    # Goals the user asked for themselves are theirs: the cleanup never dissolves or regroups them.
    pinned = {
        gid for gid, g in goals.items() if g.get("pinned") and g["status"] == "open"
    }
    result = dict(
        result,
        goals={gid: g for gid, g in result["goals"].items() if gid not in pinned},
    )
    for gid, g in list(goals.items()):
        if g["status"] == "open" and gid not in result["goals"] and gid not in pinned:
            g["status"] = "dissolved"
            g["updated"] = store.now_iso()
    for gid, g in result["goals"].items():
        goals[gid] = dict(goals.get(gid, {}), **g)
    members = {
        k: gid
        for gid, g in result["goals"].items()
        for s in g["chain"]
        for k in s["covers"]
    }
    for item in open_items(state):
        if item.get("goal_id") in pinned:
            continue
        item["goal_id"] = members.get(item["key"])
        item["placed"] = True
    _enforce_corrections(state)
    state["goals_consolidated"] = store.now_iso()


def apply_assignment(
    state: dict[str, Any], result: dict[str, Any], new_keys: list[str]
) -> list[str]:
    """Returns the ids of goals whose chain needs rewriting."""
    touched = set()
    for key, gid in result["into"].items():
        if key in state["items"] and gid in state["goals"]:
            _put_in(state, key, gid)
            touched.add(gid)
    for g in result["new_goals"]:
        gid = "g" + uuid.uuid4().hex[:7]
        for key in g["covers"]:
            if state["items"][key].get("goal_id"):
                _take_out(state, key)
        state["goals"][gid] = {
            "id": gid,
            "title": g["title"],
            "user_title": False,
            "done_when": g["done_when"],
            "by": g["by"],
            "chain": [],
            "status": "open",
            "created": store.now_iso(),
            "updated": store.now_iso(),
        }
        for key in g["covers"]:
            _put_in(state, key, gid)
        touched.add(gid)
    for key in new_keys:
        if key in state["items"]:
            state["items"][key]["placed"] = True
    _enforce_corrections(state)
    touched.update(
        gid
        for gid, g in state["goals"].items()
        if g.get("dirty") and g["status"] == "open"
    )
    return sorted(touched)


def close_finished(state: dict[str, Any]) -> None:
    """A goal whose steps are all done moves to the cleared list."""
    for g in state.get("goals", {}).values():
        if g["status"] != "open":
            continue
        keys = [k for s in g["chain"] for k in s["covers"]]
        if len(keys) < 2 and not g.get("pinned"):
            # A goal needs two items; once the user takes one out, the rest stands alone.
            for k in keys:
                if k in state["items"]:
                    _take_out(state, k)
            g["status"] = "dissolved"
            continue
        if progress(g, state["items"])["done"]:
            g["status"] = "done"
            g["done_at"] = store.now_iso()
