"""Move Today's open items, projects, old goals, this week's priorities and the user's steps into
commitments, for data from before the task list (run by hand).

Every commitment made here gets one ``import`` event naming the item it came from, and a
link to the conversation (and message) it started in. Run it on a copy first:

    TODAY_DATA_DIR=<copy> uv run --no-sync python -m today.commitments_import <weekly dir>

It refuses to run twice into the same database.
"""

import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from today import commitments, conversations, projects

log = logging.getLogger(__name__)

# Pairs of item keys tracking the same thing (keep the first; the second becomes stale and
# points at it). Empty: fill it in only when migrating data that has known doubles.
MERGES: list[tuple[str, str]] = []

# What carries over from an item, besides the commitment's own fields.
DETAIL_KEYS = (
    "source", "state", "kind", "draft", "questions", "did", "notes", "url", "who", "who_full",
    "account", "asked_at", "ask_text", "platform", "request", "emails", "nudge", "moved_at",
    "kept_at", "not_now", "later_until", "manual", "from_gtd", "gtd_id", "ref",
    "notion_url", "error", "goal_text", "weekly_goal", "move", "filed",
)


def classify(item: dict[str, Any]) -> tuple[str, str, str]:
    """(direction, origin, started_by) of an open item."""
    kind, state = item.get("kind"), item.get("state")
    if state == "waiting" and item.get("from_gtd") != "waiting":
        return "theirs", ("handed" if kind == "handed" else "user_asked"), "user"
    if item.get("manual") or item.get("source") in ("goal", "manual") or not kind:
        return "self", "user_chose", "user"
    if kind == "promised":
        return "mine", "user_promised", "user"
    return "mine", "they_asked", "them"


def stage_of(item: dict[str, Any], today: str) -> tuple[str, str]:
    if str(item.get("later_until") or "") > today:
        return "parked", f"set aside until {item['later_until']}"
    if item.get("not_now"):
        return "parked", "Not now"
    return "open", ""


def origin_message(item: dict[str, Any]) -> str:
    if item.get("source") == "text":
        return str(item.get("ask_id") or "")
    if item.get("source") == "slack":
        return str((item.get("ref") or {}).get("ts") or "")
    if item.get("source") == "email":
        return str((item.get("ref") or {}).get("message_id") or "")
    return ""


def monday_of(iso: str) -> str:
    day = datetime.fromisoformat(iso).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def run(
    db: sqlite3.Connection,
    state: dict[str, Any],
    plan: dict[str, Any],
    note: dict[str, Any] | None,
    done: dict[str, str],
    today: str | None = None,
) -> dict[str, int]:
    """Import everything; returns counts by kind."""
    if db.execute("SELECT 1 FROM events WHERE actor = 'import' LIMIT 1").fetchone():
        raise commitments.CommitmentError("commitments were already imported into this database")
    today = today or date.today().isoformat()
    counts = {"projects": 0, "old_goals": 0, "items": 0, "week": 0, "steps": 0, "merged": 0, "parked": 0}
    items = state["items"]

    # Projects the user named, then the old Today goals still open, each under its project.
    project_ids: dict[str, str] = {}
    for n, p in enumerate(projects.all_projects(state)):
        project_ids[p["id"]] = commitments.create(
            db, "import",
            {"title": p["title"], "direction": "self", "origin": "user_chose", "started_by": "user",
             "is_project": 1, "position": n, "detail": {"about": p.get("about", "")}},
            legacy_key=f"project:{p['id']}", reason="a project the user named",
        )
        counts["projects"] += 1
    goal_ids: dict[str, str] = {}
    for g in state.get("goals", {}).values():
        if g.get("status") != "open":
            continue
        chain = g.get("chain") or []
        nxt = next((s for s in chain if s.get("id") == g.get("next_seen")), chain[0] if chain else {})
        goal_ids[g["id"]] = commitments.create(
            db, "import",
            {"title": g["title"], "direction": "self", "origin": "user_chose", "started_by": "user",
             "is_project": 1, "parent_id": project_ids.get(projects.of_goal(g, items)),
             "next_step": str(nxt.get("step") or ""), "due": str(g.get("by") or ""),
             "detail": {"done_when": g.get("done_when", ""), "old_goal": g["id"]}},
            legacy_key=f"oldgoal:{g['id']}", reason="an open Today goal",
        )
        counts["old_goals"] += 1

    # Every open item.
    in_week = plan.get("in_week") or {}
    for key, item in items.items():
        if item.get("status") not in ("open", "working") or item.get("source") == "goal":
            continue  # a goal's suggested move is remade each week from the user's priorities
        direction, origin, started_by = classify(item)
        stage, why = stage_of(item, today)
        parent = goal_ids.get(str(item.get("goal_id") or "")) or project_ids.get(projects.of_item(item))
        made = commitments.by_legacy_key(db, key)
        if made is not None:
            # The task was made when the item was (cards.py): it only joins its project.
            if parent and not made["parent_id"]:
                commitments.change(db, made["id"], "import", {"parent_id": parent}, reason=f"open item {key}")
            counts["items"] += 1
            continue
        conv = conversations.conversation_id(item)
        commitments.create(
            db, "import",
            {"title": item.get("title") or item.get("ask_text") or key, "direction": direction,
             "origin": origin, "started_by": started_by,
             "counterparty": "" if direction == "self" else str(item.get("who") or ""),
             "parent_id": parent, "labels": ["pitch"] if item.get("filed") == "pitch" else [],
             "stage": stage, "stage_reason": why,
             "next_step": str((item.get("next_step") or {}).get("text") or ""),
             "due": str(item.get("due") or ""),
             "week": monday_of(in_week[key]) if key in in_week else "",
             "detail": {k: item[k] for k in DETAIL_KEYS if item.get(k) not in (None, "", [], {})}
             | ({"parked_until": item["later_until"]} if why.startswith("set aside until") else {})},
            legacy_key=key, links=[(conv, origin_message(item), "origin")] if conv else [],
            reason=f"open item {key}",
        )
        counts["items"] += 1
        counts["parked"] += stage == "parked"

    # This week's priorities, in the user's order, ticked ones done.
    if note:
        for n, g in enumerate(note.get("goals") or []):
            commitments.create(
                db, "import",
                {"title": g["text"], "direction": "self", "origin": "user_chose", "started_by": "user",
                 "week": note["week"], "position": n,
                 "stage": "done" if g["id"] in done else "open",
                 "detail": {"week_goal": g["id"], "stretch": bool(g.get("stretch")),
                            "priority_line": note.get("priority", "")}},
                legacy_key=f"goal:{g['id']}", reason=f"weekly priority, week of {note['week']}",
            )
            counts["week"] += 1

    # The user's own steps: the next one, the ones after it, and the ones the user checked off.
    for ref, s in (plan.get("steps") or {}).items():
        parent = commitments.by_legacy_key(db, ref.removeprefix("todo:"))
        if not parent:
            log.warning("steps for %s: no commitment to put them under", ref)
            continue
        queue = [(s.get("text") or "", "open")] + [(t, "open") for t in s.get("then") or []]
        queue += [(d.get("text") or "", "done") for d in s.get("done") or []]
        for n, (text, stage) in enumerate(q for q in queue if q[0]):
            commitments.create(
                db, "import",
                {"title": text, "direction": parent["direction"], "origin": "user_chose", "started_by": "user",
                 "parent_id": parent["id"], "labels": ["step"], "stage": stage, "position": n},
                reason=f"the user's step on {ref}",
            )
            counts["steps"] += 1
        if s.get("text"):
            commitments.change(db, parent["id"], "import", {"next_step": s["text"]}, reason="the user's next step")

    for keep_key, drop_key in MERGES:
        keep, drop = commitments.by_legacy_key(db, keep_key), commitments.by_legacy_key(db, drop_key)
        if not keep or not drop or drop["stage"] != "open":
            continue
        for conv in commitments.conversations_of(db, drop["id"]):
            commitments.link(db, keep["id"], conv, role="evidence")
        commitments.change(
            db, drop["id"], "import",
            {"stage": "stale", "stage_reason": f"same as: {keep['title']}",
             "detail": dict(drop["detail"], merged_into=keep["id"])},
            reason="tracked twice", kind="merged",
        )
        counts["merged"] += 1
    return counts

