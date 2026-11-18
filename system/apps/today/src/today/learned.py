"""What Today has learned: the user's corrections from the feedback bar, kept for good.

Each is the user's words, verbatim, plus the items the user pointed at and how Today had read them
and what the user changed them to. They live in one table, ``learned``, in today.db, so both
readers use them: the reply reader (work.guidance puts ``prompt_text`` in every prompt)
and the per-conversation judge (conversation_judge.py), which can append the same
``prompt_text(db)`` to its prompt. The user can remove one; it is kept, marked removed.
"""

import json
import sqlite3
from typing import Any

from today import conversations, store

SHOWN_IN_PROMPT = 40  # newest corrections a reader sees

SCHEMA = """
CREATE TABLE IF NOT EXISTS learned (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    text TEXT NOT NULL,
    items TEXT NOT NULL DEFAULT '[]',
    removed_at TEXT NOT NULL DEFAULT ''
);
"""


def setup(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def add(db: sqlite3.Connection, text: str, items: list[dict[str, Any]]) -> int:
    """``items``: for each item the user pointed at, who and what it said, how Today read it
    (``was``) and what the user changed it to (``now``)."""
    setup(db)
    cur = db.execute(
        "INSERT INTO learned (at, text, items) VALUES (?, ?, ?)",
        (store.now_iso(), text, json.dumps(items, ensure_ascii=False)),
    )
    return int(cur.lastrowid or 0)


def set_removed(db: sqlite3.Connection, lid: int, removed: bool = True) -> None:
    setup(db)
    db.execute("UPDATE learned SET removed_at = ? WHERE id = ?", (store.now_iso() if removed else "", lid))


def active(db: sqlite3.Connection) -> list[dict[str, Any]]:
    setup(db)
    rows = db.execute("SELECT * FROM learned WHERE removed_at = '' ORDER BY id DESC").fetchall()
    return [dict(r, items=json.loads(r["items"])) for r in rows]


def prompt_text(db: sqlite3.Connection) -> str:
    """The user's corrections, as a reader's prompt shows them (empty when there are none)."""
    rows = active(db)[:SHOWN_IN_PROMPT]
    if not rows:
        return ""
    lines = []
    for r in rows:
        about = "; ".join(
            f'from {i.get("who") or "user"}: "{str(i.get("words") or "")[:200]}" '
            f"(Today read it as {i.get('was', '')}; the user changed it to {i.get('now', '')})"
            for i in r["items"]
        )
        lines.append(f'- the user said: "{r["text"]}"' + (f" About: {about}" if about else ""))
    return (
        "\nThe user's corrections to how Today read things (learn the pattern; read similar things the same way):\n"
        + "\n".join(lines)
        + "\n"
    )


def listed() -> list[dict[str, Any]]:
    """What Today has learned, for the page."""
    with conversations.opened(store.DATA_DIR) as db:
        return active(db)


def removed(lid: int, removed: bool = True) -> None:
    """The user took one away (or put it back)."""
    with conversations.opened(store.DATA_DIR) as db:
        set_removed(db, lid, removed)


def prompt_text_now() -> str:
    """``prompt_text`` from the data's own database, or "" when it can't be read."""
    try:
        with conversations.opened(store.DATA_DIR) as db:
            return prompt_text(db)
    except (sqlite3.Error, OSError):
        return ""
