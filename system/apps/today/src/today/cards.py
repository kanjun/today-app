"""Each task's card: everything Today keeps on a task besides what is true about it.

A task is one record in the database (commitments.py). What is true about it (its title,
date, where it stands, whose move it is) are the record's own fields, with a history of
who changed them. Its card (``commitments.card``) is the working material: the draft, the
emails behind it, the user's answers so far, where its conversation is, what the assistant did.
A task with no card is one Today never showed as a row of its own (a step, a project, a
person being onboarded).

The rest of Today still reads a task as an "item", the shape the old file kept: ``view``
puts the two halves together, and ``save`` takes an item a change was made to and writes
each part where it belongs. The truth goes in as an event naming who made the change; the
card is saved as it is. (the user, 2026-11-17: one record per task, and the old file goes.)
"""

import json
import logging
import sqlite3
import threading
from copy import deepcopy
from datetime import date
from typing import Any

from today import commitments, commitments_import, conversations

log = logging.getLogger(__name__)

FINISHED = ("sent", "done", "cleared", "skipped")
STAGE_OF_STATUS = {"done": "done", "sent": "done", "cleared": "stale", "skipped": "declined"}
STATUS_OF_STAGE = {"done": "done", "declined": "skipped", "stale": "cleared"}
# The truth, kept on the record itself; never saved on the card.
TRUTH = ("key", "title", "due")
# Where a task stands follows these on the item: closing or reopening it, or setting it aside.
SET_ASIDE = ("later_until", "not_now")


def stage_of(item: dict[str, Any]) -> tuple[str, str]:
    """Where a task stands, from an item's status and set-aside fields."""
    status = item.get("status")
    if status in FINISHED:
        return STAGE_OF_STATUS[status], str(item.get("cleared_reason") or "")
    if str(item.get("later_until") or "") > date.today().isoformat():
        return "parked", f"set aside until {item['later_until']}"
    if item.get("not_now"):
        return "parked", "Not now"
    return "open", ""


def card_of(c: dict[str, Any]) -> dict[str, Any] | None:
    """A task's card, None when it has none."""
    raw = c.get("card")
    if isinstance(raw, dict):
        return raw
    return json.loads(raw) if raw else None


def key_of(c: dict[str, Any]) -> str:
    return c["legacy_key"] or c["id"]


def view(c: dict[str, Any]) -> dict[str, Any]:
    """A task as an item: its card, with what is true about it on top."""
    card = card_of(c) or {}
    d = c["detail"]
    said = card.get("status") or "open"
    if c["stage"] in STATUS_OF_STAGE:
        status = said if said in FINISHED else STATUS_OF_STAGE[c["stage"]]
    else:
        status = "open" if said in FINISHED else said
    if c["direction"] == "theirs":
        state = "waiting"
    else:
        state = d.get("state") or card.get("state") or ""
        if state == "waiting":
            state = "mine"  # the task says the move is the user's
    return dict(
        card,
        key=key_of(c),
        title=c["title"],
        due=c["due"] or None,
        status=status,
        state=state,
        asked_at=card.get("asked_at") or d.get("asked_at") or card.get("created") or c["created_at"],
    )


_read: dict[str, Any] = {}
_read_lock = threading.Lock()


def items(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every task with a card, as items keyed as the page keys them (a copy the caller may
    change). Read again only when a task changed since the last read."""
    where = db.execute("PRAGMA database_list").fetchone()[2]
    n = db.execute("SELECT n FROM commitments_seq").fetchone()[0]
    with _read_lock:
        if _read.get("at") == (where, n):
            return deepcopy(_read["items"])
    rows = db.execute("SELECT * FROM commitments WHERE card != ''").fetchall()
    out = {key_of(c): view(c) for c in map(commitments._row, rows)}
    if not db.in_transaction:  # only what is saved for good: a change rolled back never was
        with _read_lock:
            _read.update(at=(where, n), items=out)
        return deepcopy(out)
    return out


def find(db: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    return commitments.by_legacy_key(db, key) or commitments.get(db, key)


def _card(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k not in TRUTH}


def create(db: sqlite3.Connection, item: dict[str, Any], actor: str) -> str:
    """A new task for an item no task holds yet."""
    direction, origin, started_by = commitments_import.classify(item)
    conv = conversations.conversation_id(item)
    stage, why = stage_of(item)
    labels = ["pitch"] if item.get("filed") == "pitch" else []
    if item.get("source") == "goal":
        labels.append("move")  # a weekly goal's next move: shown in Now, never on a list
    cid = commitments.create(
        db, actor,
        {"title": item.get("title") or item.get("ask_text") or item["key"], "direction": direction,
         "origin": origin, "started_by": started_by,
         "counterparty": "" if direction == "self" else str(item.get("who") or ""),
         "labels": labels, "due": str(item.get("due") or ""), "stage": stage, "stage_reason": why,
         "detail": {k: item[k] for k in commitments_import.DETAIL_KEYS if item.get(k) not in (None, "", [], {})}
         | ({"unread": True} if not item.get("state") else {})  # a first guess until the judge reads it
         | ({"parked_until": item["later_until"]} if why.startswith("set aside until") else {})},
        legacy_key=item["key"], links=[(conv, commitments_import.origin_message(item), "origin")] if conv else [],
        reason=f"in Today: {item['key']}",
    )
    commitments.set_card(db, cid, _card(item))
    return cid


def save(db: sqlite3.Connection, before: dict[str, Any] | None, after: dict[str, Any], actor: str) -> None:
    """Write a changed item: its card as it is, and each change to what is true about the task
    as an event by ``actor``. ``before`` is the item as it was read (None for a new one)."""
    c = find(db, after["key"])
    if c is None:
        create(db, after, actor)
        return
    if before is None:
        before = view(c) if card_of(c) is not None else {}
    commitments.set_card(db, c["id"], _card(after))
    fields: dict[str, Any] = {}
    if after.get("title") and after.get("title") != before.get("title") and after["title"] != c["title"]:
        fields["title"] = after["title"]
    if (after.get("due") or "") != (before.get("due") or "") and (after.get("due") or "") != c["due"]:
        fields["due"] = str(after.get("due") or "")
    closed = (after.get("status") in FINISHED) != (before.get("status") in FINISHED)
    if closed or any(after.get(k) != before.get(k) for k in SET_ASIDE):
        stage, why = stage_of(after)
        until = str(after.get("later_until") or "") if stage == "parked" else ""
        if stage != c["stage"] or str(c["detail"].get("parked_until") or "") != until:
            fields.update(stage=stage, stage_reason=why)
            fields["detail"] = {k: v for k, v in c["detail"].items() if k != "parked_until"} | (
                {"parked_until": until} if until else {})
    if after.get("state") != before.get("state") and after.get("state") and after["state"] != c["detail"].get("state"):
        fields["detail"] = dict(fields.get("detail") or c["detail"], state=after["state"])
        if after["state"] == "waiting" and c["direction"] != "theirs":
            fields["direction"] = "theirs"
        elif before.get("state") == "waiting" and c["direction"] == "theirs":
            fields["direction"] = "mine"
    if not fields:
        return
    try:
        commitments.change(db, c["id"], actor, fields, reason=str(fields.get("stage_reason") or "") or "in Today")
    except commitments.Refused as e:
        # The user decided where it stands and no one has written since: the user's decision holds.
        log.info("cards: %s", e)
        rest = {k: v for k, v in fields.items() if k not in ("stage", "stage_reason")}
        if rest:
            commitments.change(db, c["id"], actor, rest, reason="in Today")


def attach(db: sqlite3.Connection, c: dict[str, Any], item: dict[str, Any]) -> None:
    """Give a task a card (moving an old item onto it). A second old item of the same task
    (its id once stood in for its key) adds what the first lacks, and its being sent or done."""
    card = _card(item)
    if card_of(c) is not None:
        first = dict(card_of(c) or {})
        for k, v in card.items():
            if first.get(k) in (None, "", [], {}):
                first[k] = v
        if card.get("status") in FINISHED and first.get("status") not in FINISHED:
            first["status"] = card["status"]
        card = first
    commitments.set_card(db, c["id"], card)
