"""Today's state, all in its database (today.db under DATA_DIR): its tasks (cards.py) and
the rest of what it keeps (``today_state``).

The web app and the sync job both read-modify-write it, so every change goes
through ``mutate`` (one database edit: read, change, save what changed).

Raw source records (the Slack conversation or email thread an item came from, and the
copies Today keeps of the user's calendar and the like) live in the database,
``today.db``, table ``raw_records`` (the user, 2026-11-15: everything in the database, nothing
new in files), so the view can always show the original. A conversation's messages also go,
one row each, into its ``messages`` table, which search and the judge read. (The ``raw/``
files from before were copied in and removed, 2026-11-15.)
"""

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from today import cards, commitments, conversations

DATA_DIR = Path(os.environ.get("TODAY_DATA_DIR", "data/.apps/today"))
FINISHED = ("sent", "done", "cleared", "skipped")  # an item in one of these is closed


def empty_state() -> dict[str, Any]:
    return {
        "today": {"date": "", "order": []},
        "last_sync": None,
        "sync_error": None,
    }


# The rest of Today's state (the user's picks for today, when each source was last read, which old
# messages never come back, the user's drafting rules...): one row per part, its value as JSON.
STATE = "CREATE TABLE IF NOT EXISTS today_state (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
_ready: set[Path] = set()


def _parts(db: sqlite3.Connection) -> dict[str, str]:
    path = conversations.db_path(DATA_DIR)
    if path not in _ready:
        db.execute(STATE)
        _ready.add(path)
    return {r[0]: r[1] for r in db.execute("SELECT name, value FROM today_state")}


def _state(parts: dict[str, str]) -> dict[str, Any]:
    state = empty_state()
    state.update({k: json.loads(v) for k, v in parts.items()})
    return state


def load() -> dict[str, Any]:
    """Today's state, with every task that has a card as ``items`` (cards.py). All of it is
    in the database: the tasks in theirs, the rest in ``today_state``."""
    with conversations.opened(DATA_DIR) as db:
        state = _state(_parts(db))
        state["items"] = cards.items(db)
    return state


# Who a change is by, for the history of each task it touches (commitments.ACTORS): a
# request from the user's page is the user's (runner.py sets it), the background refresh is "sync".
actor: ContextVar[str] = ContextVar("actor", default="sync")


def mutate(change: Callable[[dict[str, Any]], Any], by: str = "") -> Any:
    """Change the state, as one database edit: each part that changed is saved, and each
    task the change touched (cards.save), its truth as an event by ``by`` (else ``actor``)."""
    who = by or actor.get()
    with conversations.opened(DATA_DIR) as db:
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        parts = _parts(db)
        state = _state(parts)
        before = cards.items(db)
        state["items"] = deepcopy(before)
        result = change(state)
        items = state.pop("items")
        for key, item in items.items():
            if item != before.get(key):
                cards.save(db, before.get(key), dict(item, key=key), who)
        now = {k: json.dumps(v, ensure_ascii=False) for k, v in state.items()}
        db.executemany("INSERT OR REPLACE INTO today_state (name, value) VALUES (?, ?)",
                       [(k, v) for k, v in now.items() if parts.get(k) != v])
        db.executemany("DELETE FROM today_state WHERE name = ?", [(k,) for k in parts if k not in now])
    state["items"] = items
    return result


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


_local = threading.local()
_set_up: set[Path] = set()


def _raw_db() -> sqlite3.Connection:
    """This thread's own connection to today.db for raw records (cheap to reuse; a test that
    points DATA_DIR elsewhere gets a new one)."""
    path = conversations.db_path(DATA_DIR)
    held = conversations.held(DATA_DIR)
    if held is not None:
        return held  # inside a database edit: part of it (another connection would wait on it)
    if getattr(_local, "path", None) != path:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if path not in _set_up:
            conversations.connect(DATA_DIR).close()  # the tables exist (raw_records among them), once
            _set_up.add(path)
        db = sqlite3.connect(path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        _local.db, _local.path = db, path
    return _local.db


def save_raw(key: str, record: dict[str, Any]) -> None:
    db = _raw_db()
    row = (key, json.dumps(record, ensure_ascii=False), str(time.time_ns()))
    if db is conversations.held(DATA_DIR):
        db.execute("INSERT OR REPLACE INTO raw_records (key, record, saved_at) VALUES (?, ?, ?)", row)
    else:
        with db:
            db.execute("INSERT OR REPLACE INTO raw_records (key, record, saved_at) VALUES (?, ?, ?)", row)
    # A conversation record also lands, message by message, in the messages table.
    conversations.save_record(DATA_DIR, key, record)


def raw_version(key: str) -> str:
    """When a raw record last changed ("" when there is none): a cache can key on it. Reading
    never writes (it may run inside another connection's transaction)."""
    row = _raw_db().execute("SELECT saved_at FROM raw_records WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else ""


def source_message(key: str) -> str:
    """The message the task came from, as saved on its commitment ("" when none is)."""
    with conversations.opened(DATA_DIR) as db:
        return commitments.source_message(db, key)


# How far back, before the message a task came from, a conversation is read when the task's
# own saved copy doesn't hold that message.
LEAD_UP_SECONDS = 3 * 86400


def task_raw(key: str) -> dict[str, Any]:
    """A task's conversation as saved, holding the message the task came from. A task the
    judge found in a Slack conversation can have a saved copy taken for another ask there,
    without that message's thread (the user, 2026-11-17: the 409a card showed another thread);
    then the conversation is read from its messages: from a few days before the message on."""
    raw = load_raw(key) or {}
    with conversations.opened(DATA_DIR) as db:
        conv, source = commitments.origin(db, key)
        if not conv.startswith("slack:") or raw.get("source") not in ("slack", None) or any(is_source(m, source) for m in raw.get("messages") or []):
            return raw
        at = db.execute("SELECT at FROM messages WHERE conversation_id = ? AND source_id = ?", (conv, source)).fetchone()
        if at is None:
            return raw
        rows = db.execute("SELECT raw FROM messages WHERE conversation_id = ? AND at >= ? ORDER BY at",
                          (conv, at[0] - LEAD_UP_SECONDS)).fetchall()
    return {**raw, "source": "slack", "messages": [json.loads(r[0]) for r in rows]}


def is_source(m: dict[str, Any], source: str) -> bool:
    """Whether a saved conversation's message is the task's source message."""
    return bool(source) and source in (str(m.get("ts") or ""), str(m.get("id") or ""))


def load_raw(key: str) -> dict[str, Any] | None:
    version = raw_version(key)
    if not version:
        return None
    record = _read_raw(str(conversations.db_path(DATA_DIR)), key, version)
    return deepcopy(record) if record is not None else None


@lru_cache(maxsize=512)


def _read_raw(db_file: str, key: str, version: str) -> dict[str, Any] | None:
    """Reuse parsed records until the record changes, including changes by the sync job."""
    try:
        row = _raw_db().execute("SELECT record FROM raw_records WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
    except (OSError, ValueError):
        return None


def roll_today(state: dict[str, Any], today: str) -> None:
    """A new day starts with an empty Today list; nothing is lost, it all stays in Owed."""
    if state["today"].get("date") != today:
        state["today"] = {"date": today, "order": []}
