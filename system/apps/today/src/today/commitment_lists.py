"""Today's lists as plain filters over commitments.

No list hides a commitment because of where it sits: everything open that the user owes shows
in its list, labelled with its project. Anything open slips once its conversations have
been quiet two weeks, except a promise to a person with a date (or event) coming up, and
the user's own asks (the user, 2026-11-13: those are the important ones). Lists run oldest first,
except that anything due within the week goes on top, soonest first (the user, 2026-11-16).
"""

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from today import commitments

QUIET_DAYS = 14  # anything slips once its conversations are this quiet
DUE_SOON_DAYS = 7  # ...unless it is due within this many days


def _section(c: dict[str, Any]) -> str:
    """Where an open commitment the user owes belongs: reply, answer or todo."""
    d = c["detail"]
    if c["direction"] == "self" or d.get("state") == "mine":
        return "todos"
    if d.get("state") == "you":
        # A decision only the user has: the card offers the choices as buttons.
        return "answers"
    if d.get("state") == "ready":
        return "replies"
    return "todos"


def _project_of(c: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    seen = set()
    p = by_id.get(c.get("parent_id") or "")
    while p and p["id"] not in seen:
        if p["is_project"]:
            return p
        seen.add(p["id"])
        p = by_id.get(p.get("parent_id") or "")
    return None


def _last_activity(db: sqlite3.Connection) -> dict[str, float]:
    rows = db.execute(
        "SELECT l.commitment_id, max(c.last_at) FROM commitment_links l "
        "JOIN conversations c ON c.id = l.conversation_id GROUP BY l.commitment_id"
    ).fetchall()
    return {r[0]: r[1] or 0.0 for r in rows}


def _date_soon(c: dict[str, Any], today: date) -> bool:
    if c["detail"].get("due_event"):
        return True  # "before the IPO": coming up until it happens
    try:
        return bool(c["due"]) and date.fromisoformat(c["due"][:10]) <= today + timedelta(days=DUE_SOON_DAYS)
    except ValueError:
        return False


def _keeps(c: dict[str, Any], today: date) -> bool:
    """Never slips: a promise to a person with a date coming up."""
    return c["origin"] == "user_promised" and bool(c["counterparty"]) and _date_soon(c, today)


def _quiet(c: dict[str, Any], last: dict[str, float], today: date) -> bool:
    if _keeps(c, today) or _date_soon(c, today):
        return False
    at = last.get(c["id"]) or 0.0
    if not at:
        when = c["detail"].get("asked_at") or c["created_at"]
        at = datetime.fromisoformat(str(when)).timestamp()
    cutoff = datetime.combine(today - timedelta(days=QUIET_DAYS), datetime.min.time()).timestamp()
    return at < cutoff


def _age(c: dict[str, Any]) -> str:
    return str(c["detail"].get("asked_at") or c["created_at"])


def order_key(due: str, age: str, today: date) -> tuple[int, str]:
    """Due within the week first, soonest date on top; everything else oldest first.

    The user, 2026-11-16: Ian's dinner was due the next day and sat fifth, hidden under
    "+ 5 more", because the list ran oldest first only.
    """
    try:
        if due and date.fromisoformat(due[:10]) <= today + timedelta(days=DUE_SOON_DAYS):
            return (0, due[:10])
    except ValueError:
        pass
    return (1, age)


def lists(db: sqlite3.Connection, today: date | None = None) -> dict[str, list[dict[str, Any]]]:
    today = today or date.today()
    week = (today - timedelta(days=today.weekday())).isoformat()
    every = commitments.all_commitments(db)
    by_id = {c["id"]: c for c in every}
    last = _last_activity(db)
    children: dict[str, list[dict[str, Any]]] = {}
    for c in every:
        children.setdefault(c.get("parent_id") or "", []).append(c)
    out: dict[str, list[dict[str, Any]]] = {
        k: [] for k in ("replies", "answers", "todos", "waiting", "later", "pitches", "parked", "week", "projects")
    }
    for c in every:
        project = _project_of(c, by_id)
        row = dict(c, project=project["title"] if project else "", project_id=project["id"] if project else "")
        steps = [s for s in children.get(c["id"], []) if "step" in s["labels"]]
        if "step" in c["labels"] or "move" in c["labels"]:
            continue  # a step shows under what it is a step of; a weekly goal's move, in Now
        if c["detail"].get("week_goal") and c["week"] == week:
            out["week"].append(dict(row, steps=steps))
            continue
        if c["is_project"]:
            if c["stage"] == "open":
                kids = [k for k in children.get(c["id"], []) if k["stage"] == "open"]
                out["projects"].append(dict(row, open=len(kids)))
            continue
        back = c["stage"] == "parked" and str(c["detail"].get("parked_until") or "9999") <= today.isoformat()
        if c["stage"] == "parked" and not back:
            if not c["detail"].get("someday"):  # a Someday idea lives in the user's outline, not on a list
                out["parked"].append(row)
        elif c["stage"] != "open" and not back:
            continue
        elif "pitch" in c["labels"]:
            out["pitches"].append(row)
        elif c["direction"] == "theirs":
            out["waiting"].append(row)
        elif _quiet(c, last, today):
            out["later"].append(row)
        else:
            out[_section(c)].append(row)
    for name in ("replies", "answers", "todos", "waiting", "later"):
        out[name].sort(key=lambda c: order_key(c["due"], _age(c), today))
    out["week"].sort(key=lambda c: c["position"])
    return out
