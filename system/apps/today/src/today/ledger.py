"""The switch: Today's lists come from commitments.

The page keeps its rows, drafts and buttons; what decides which list a row is in (and
whether it shows at all) is now the commitment behind it (commitment_lists.py). This module
is the bridge:

- Every item is a task's card (cards.py): the user's clicks go into the history as ``user``
  events, which the judge can't undo unless someone writes again.
- ``refresh`` runs the per-conversation judge on conversations with new messages.
- ``sections`` builds the page's lists; a commitment with no item behind it (one the judge
  found) gets a plain row of its own, and its buttons act on the commitment directly.
"""

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any

from today import (
    ai,
    commitment_lists,
    commitments,
    config,
    conversation_judge,
    conversation_matching,
    conversations,
    page,
    store,
)

log = logging.getLogger(__name__)

JUDGE_WORKERS = 4

JUDGED = """
CREATE TABLE IF NOT EXISTS judged (
    conversation_id TEXT PRIMARY KEY,
    last_at REAL NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS judge_asked (
    conversation_id TEXT PRIMARY KEY,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS judged_outside (
    conversation_id TEXT PRIMARY KEY,
    marks TEXT NOT NULL
);
"""


def is_commitment_key(key: str) -> bool:
    return len(key) == 11 and key.startswith("c") and ":" not in key


def act(key: str, what: str) -> str:
    """The user's click on a commitment with no item behind it."""
    stage = {"done": "done", "drop": "declined", "notnow": "parked", "undo": "open"}[what]
    with conversations.opened(store.DATA_DIR) as db:
        commitments.change(db, key, "user", {"stage": stage, "stage_reason": {"notnow": "Not now"}.get(what, "")},
                           reason="in Today")
    return {"done": "Marked done", "drop": "Dropped", "notnow": "Moved to Slipped", "undo": "Back in your list"}[what]


def ask_judging(conversation_id: str) -> None:
    """Have this conversation read whole on the next refresh, even with nothing open in it
    (the reply judge saw a promise there: the judge makes it a commitment, once)."""
    if not conversation_id:
        return
    with conversations.opened(store.DATA_DIR) as db:
        db.executescript(JUDGED)
        db.execute("INSERT OR REPLACE INTO judge_asked (conversation_id, at) VALUES (?, ?)",
                   (conversation_id, commitments.now_iso()))


def changed_conversations(db: sqlite3.Connection) -> list[str]:
    """Conversations behind open commitments with messages newer than their last judging,
    plus any the reply judge asked to have read."""
    db.executescript(JUDGED)
    rows = db.execute(
        "SELECT DISTINCT l.conversation_id, c.last_at FROM commitment_links l "
        "JOIN commitments k ON k.id = l.commitment_id AND k.stage = 'open' "
        "JOIN conversations c ON c.id = l.conversation_id "
        "LEFT JOIN judged j ON j.conversation_id = l.conversation_id "
        "WHERE j.last_at IS NULL OR c.last_at > j.last_at"
    ).fetchall()
    out = [r[0] for r in rows]
    asked = db.execute("SELECT a.conversation_id FROM judge_asked a JOIN conversations c ON c.id = a.conversation_id").fetchall()
    out += [r[0] for r in asked if r[0] not in out]
    return out + [c for c in kept_elsewhere(db) if c not in out]


def kept_elsewhere(db: sqlite3.Connection) -> list[str]:
    """Conversations with an open promise of the user's where something new turned up outside
    them (an email or message to the person, a calendar event with them): where a promise
    is kept, the conversation it was made in often stays quiet."""
    marks = dict(db.execute("SELECT conversation_id, marks FROM judged_outside").fetchall())
    convs = [r[0] for r in db.execute(
        "SELECT DISTINCT l.conversation_id FROM commitment_links l JOIN commitments k ON k.id = l.commitment_id "
        "JOIN conversations c ON c.id = l.conversation_id "
        "WHERE l.role = 'origin' AND k.stage = 'open' AND k.direction = 'mine'"
    )]
    return [c for c in convs if conversation_judge.outside_marks(db, c) != marks.get(c, "")]


def _judge_one(conv: str) -> dict[str, Any]:
    try:
        with conversations.opened(store.DATA_DIR) as db:
            last = db.execute("SELECT last_at FROM conversations WHERE id = ?", (conv,)).fetchone()
            result = conversation_judge.judge(db, conv)
            marks = conversation_judge.outside_marks(db, conv)
        with conversations.opened(store.DATA_DIR) as db:
            db.executescript(JUDGED)
            out = conversation_judge.apply(db, conv, result)
            db.execute(
                "INSERT INTO judged (conversation_id, last_at, at) VALUES (?, ?, ?) "
                "ON CONFLICT (conversation_id) DO UPDATE SET last_at = excluded.last_at, at = excluded.at",
                (conv, (last[0] if last else 0) or 0, commitments.now_iso()),
            )
            db.execute("DELETE FROM judge_asked WHERE conversation_id = ?", (conv,))
            db.execute("INSERT OR REPLACE INTO judged_outside (conversation_id, marks) VALUES (?, ?)", (conv, marks))
        return {"conversation": conv, "applied": out}
    except (ai.ClaudeError, sqlite3.Error, OSError, ValueError) as e:
        log.warning("ledger: judging %s failed: %s", conv, e)
        return {"conversation": conv, "error": str(e)[:300]}


def refresh(limit: int | None = None) -> dict[str, Any]:
    """After a sync: changed conversations are judged."""
    # Conversations about a task they didn't start get attached to it first, so they are read with it.
    try:
        matched = conversation_matching.run()
    except (sqlite3.Error, OSError) as e:
        log.warning("ledger: matching conversations to tasks failed: %s", e)
        matched = {}
    with conversations.opened(store.DATA_DIR) as db:
        todo = changed_conversations(db)
    todo = todo[:limit] if limit else todo
    with ThreadPoolExecutor(JUDGE_WORKERS) as pool:
        results = list(pool.map(_judge_one, todo))
    # Replies are written ahead by the one assistant after this (act.write_ahead_due).
    return dict(judged=len(results), errors=sum("error" in r for r in results),
                attached=len(matched.get("linked") or []))


# ---- the page -------------------------------------------------------------------------


def _as_item(c: dict[str, Any], items: dict[str, dict[str, Any]], conv_source: dict[str, str]) -> dict[str, Any]:
    """A row-shaped item for a commitment: its item's fields (draft, url, questions) when it
    has one, with the commitment's truth on top (title, who, list, date)."""
    base = dict(items.get(c["legacy_key"] or "", {}))
    d = c["detail"]
    state = "waiting" if c["direction"] == "theirs" else d.get("state") or base.get("state") or "mine"
    if c["direction"] == "self":
        state = "mine"
    out = dict(
        base,
        key=c["legacy_key"] or c["id"],
        title=c["title"],
        who=c["counterparty"] or base.get("who", ""),
        state=state,
        status="open",
        due=c["due"] or "",
        due_set=d.get("due_set_by") == "user",
        asked_at=d.get("asked_at") or base.get("asked_at") or c["created_at"],
        source=base.get("source") or conv_source.get(c["id"], "manual"),
        project_title=c.get("project", ""),
    )
    if not base:
        out.update(manual=c["direction"] == "self", ask_text=c["title"], did=c.get("stage_reason", ""))
    return out


def task_dues() -> dict[str, str]:
    """Each open task's date from its commitment, keyed as its item is: the date the user sees on the
    row and sets in the picker. Now picks by it too, not by an older date left on the item
    (the user, 2026-11-16: the rental car sat in Now on a guess the picker had replaced). Empty
    before the switch."""
    try:
        with conversations.opened(store.DATA_DIR) as db:
            return {r[0] or r[1]: r[2] for r in db.execute(
                "SELECT legacy_key, id, due FROM commitments WHERE stage IN ('open', 'parked')")}
    except sqlite3.Error as e:
        log.warning("ledger: task dates unavailable: %s", e)
        return {}


def now_pool(state: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]] | None:
    """What Now chooses from, read from the tasks themselves: every open or parked task as a
    row-shaped item (keyed as the page keys it), and the keys of those on the user's working lists
    (Replies, Quick answers, To-dos) that Now may take on its own. A task the user finished is not
    here, whatever an old item behind it still says (the user, 2026-11-17: the side letter came
    back in Now the day after the user ticked it). None before the switch."""
    try:
        with conversations.opened(store.DATA_DIR) as db:
            lists = commitment_lists.lists(db)
            conv_source = {
                r[0]: r[1] for r in db.execute(
                    "SELECT l.commitment_id, c.source FROM commitment_links l JOIN conversations c ON c.id = l.conversation_id"
                )
            }
    except sqlite3.Error as e:
        log.warning("ledger: tasks unavailable for Now: %s", e)
        return None
    items = state["items"]
    tasks: dict[str, dict[str, Any]] = {}
    working: set[str] = set()
    for name in ("replies", "answers", "todos", "waiting", "later", "parked"):
        for c in lists[name]:
            it = _as_item(c, items, conv_source)
            tasks[it["key"]] = it
            if name in ("replies", "answers", "todos"):
                working.add(it["key"])
    return tasks, working


def sections(built: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """The page's lists from commitments; None before the switch."""
    try:
        with conversations.opened(store.DATA_DIR) as db:
            lists = commitment_lists.lists(db)
            conv_source = {
                r[0]: r[1] for r in db.execute(
                    "SELECT l.commitment_id, c.source FROM commitment_links l JOIN conversations c ON c.id = l.conversation_id"
                )
            }
            # Each task's date is its commitment's (the user sets it in the picker), Now's rows too.
            dates = {
                r["legacy_key"] or r["id"]: {"due": r["due"], "due_set": json.loads(r["detail"] or "{}").get("due_set_by") == "user"}
                for r in db.execute("SELECT id, legacy_key, due, detail FROM commitments WHERE stage IN ('open', 'parked')")
            }
    except sqlite3.Error as e:
        log.warning("ledger: lists unavailable, showing the old ones: %s", e)
        return None
    items = state["items"]
    now_keys = {r["key"] for r in built["now"]}

    def rows(name: str) -> list[dict[str, Any]]:
        out = []
        for c in lists[name]:
            it = _as_item(c, items, conv_source)
            if it["key"] in now_keys:
                continue
            out.append(page.row(it, sendable=page.can_send(it), goal="", project=c.get("project", ""),
                                project_id=c.get("project_id", "")))
        return out

    waiting = rows("waiting")
    # A slipped item with a reply already written is one read and Send away: it goes up to
    # Replies ready (the user, 2026-11-14).
    slipped = rows("later") + rows("parked")
    ready = [r for r in slipped if r["draft"]]
    replies = sorted(rows("replies") + ready, key=lambda r: commitment_lists.order_key(
        (dates.get(r["key"]) or {}).get("due") or "", str(items.get(r["key"], {}).get("asked_at") or ""), date.today()))
    def dated(r: dict[str, Any]) -> dict[str, Any]:
        d = dates.get(r["key"])
        return dict(r, when=page.due_spot(d), due=d["due"][:10]) if d is not None else r

    return dict(
        built,
        now=[dated(r) for r in built["now"]],
        replies=replies,
        answers=rows("answers"),
        todos=rows("todos"),
        waiting=[dict(r, nudge_ready=False) for r in waiting],
        later=[r for r in slipped if r not in ready],
        filed=dict(built["filed"], pitch=[]),
    )


def _raw_from_db(db: sqlite3.Connection, conv: str) -> dict[str, Any]:
    """A saved conversation record (the shape the reply writer reads), rebuilt from the
    conversations database."""
    head = db.execute("SELECT source, title, url FROM conversations WHERE id = ?", (conv,)).fetchone()
    rows = db.execute(
        "SELECT source_id, at, sender, is_user, text, note, thread, raw FROM messages WHERE conversation_id = ? ORDER BY at",
        (conv,),
    ).fetchall()
    source = head["source"] if head else conv.split(":", 1)[0]
    if source == "email":
        _, account, _thread = conv.split(":", 2)
        msgs = [json.loads(r["raw"]) for r in rows]
        return {"source": "email", "account": account, "subject": msgs[0].get("subject", "") if msgs else "",
                "messages": msgs, "fetched_at": commitments.now_iso()}
    msgs = [
        {"ts": r["at"], "id": r["source_id"], "name": config.name() if r["is_user"] else r["sender"], "text": r["text"],
         "note": r["note"], "is_me": bool(r["is_user"]),
         **({"thread_ts": r["thread"]} if r["thread"] else {})}
        for r in rows
    ]
    out: dict[str, Any] = {"source": source, "channel_label": head["title"] if head else "", "url": head["url"] if head else "",
                           "messages": msgs, "fetched_at": commitments.now_iso()}
    if source == "text":
        out["chat_id"] = conv.split(":", 1)[1]
    if source == "slack":
        out["channel"] = conv.split(":", 1)[1]
    return out


def ensure_item(key: str) -> None:
    """A commitment the judge found gets an item behind it the first time the user asks for help
    or sends, so the reply writer and Send work on it like on any other."""
    if not is_commitment_key(key) or key in store.load()["items"]:
        return
    with conversations.opened(store.DATA_DIR) as db:
        c = commitments.get(db, key)
        convs = commitments.conversations_of(db, key)
        if not c or not convs:
            raise commitments.CommitmentError("there is no conversation to write from")
        raw = _raw_from_db(db, convs[0])
        conv, source_id = commitments.origin(db, key)
        # Only a task with no item of its own takes its id as its key: overwriting a real one
        # loses the task's item, and every later load tries to make the task again.
        db.execute("UPDATE commitments SET legacy_key = ? WHERE id = ? AND COALESCE(legacy_key, '') = ''", (key, key))
    source = raw["source"]
    # The message the task came from (the user, 2026-11-17: the 409a task took Robin's newest
    # message, about Arc, and its reply would have gone on that thread); else their newest.
    theirs = [m for m in raw["messages"] if not (m.get("is_me") or m.get("from_me"))]
    origin = next((m for m in raw["messages"] if conv == convs[0] and store.is_source(m, source_id)), None)
    last = origin or (theirs[-1] if theirs else (raw["messages"][-1] if raw["messages"] else {}))
    ref: dict[str, Any] = {}
    if source == "text":
        ref = {"chat_id": raw["chat_id"]}
    elif source == "slack":
        ref = {"channel": raw["channel"], "ts": str(last.get("ts", "")), "thread_ts": str(last.get("thread_ts", "") or "")}
    elif source == "email":
        ref = {"thread_id": convs[0].split(":", 2)[2]}
    item = {
        "key": key, "source": source, "ref": ref, "account": raw.get("account", ""),
        "who": c["counterparty"], "title": c["title"], "status": "open",
        "state": c["detail"].get("state") or "you", "kind": "asked",
        "ask_text": str(last.get("text", ""))[:2000], "asked_at": c["created_at"], "created": c["created_at"],
        "did": c["stage_reason"], "draft": "", "questions": [], "from_ledger": True,
        # Which chat app (X, Signal, WhatsApp...) for the card's icon; the task knows it.
        "platform": c["detail"].get("platform") or raw.get("platform") or "",
    }
    store.save_raw(key, raw)
    store.mutate(lambda s: s["items"].setdefault(key, item))


def by_item() -> dict[str, dict[str, Any]]:
    """Commitments by the item they came from (and by their own id when there's no item),
    for search: what's true now, not what the old item last said."""
    try:
        with conversations.opened(store.DATA_DIR) as db:
            return {c["legacy_key"] or c["id"]: c for c in commitments.all_commitments(db)
                    if not c["is_project"] and "step" not in c["labels"]}
    except sqlite3.Error:
        return {}


def conversation_html_rows(key: str) -> list[tuple[str, str, str]] | None:
    """(who, when, text) for the whole conversation behind a commitment with no item."""
    with conversations.opened(store.DATA_DIR) as db:
        convs = commitments.conversations_of(db, key)
        if not convs:
            return None
        out = []
        for m in conversations.messages(store.DATA_DIR, convs[0]):
            who = config.name() if m["is_user"] else m["sender"]
            out.append((who, datetime.fromtimestamp(m["at"]).strftime("%a %b %-d, %-I:%M %p"), m["text"] + (f" [{m['note']}]" if m["note"] else "")))
        return out

