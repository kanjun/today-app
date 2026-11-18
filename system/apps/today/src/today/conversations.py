"""Every conversation Today has read, message by message, in one SQLite file (today.db).

The conversations are the source that commitments will be
judged from, so they are kept whole and never edited. A refresh only adds messages; a
message seen again only gains what was missing (a reaction it got, a voice memo's label).

One conversation is one Slack channel or DM, one chat, or one email thread, with the same
ids as ``conversation_id`` ("slack:<channel>", "text:<chat id>",
"email:<account>:<thread id>"). Each message keeps its source record in ``raw``.
"""

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from today import commitments, config, links

log = logging.getLogger(__name__)


def conversation_id(item: dict[str, Any]) -> str:
    """The conversation an item comes from: one Slack channel or DM, one chat, one email thread."""
    ref = item.get("ref") or {}
    if item.get("source") == "slack" and ref.get("channel"):
        return f"slack:{ref['channel']}"
    if item.get("source") == "text" and ref.get("chat_id"):
        return f"text:{ref['chat_id']}"
    if item.get("source") == "email" and ref.get("thread_id"):
        return f"email:{item.get('account', '')}:{ref['thread_id']}"
    return ""

# The user's Slack user id (every Slack message of the user's in Today's saved records carries it).
SLACK_ME = "UT87G8X9V"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    last_at REAL,
    last_user_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    source_id TEXT NOT NULL,
    at REAL NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    is_user INTEGER NOT NULL DEFAULT 0,
    type TEXT NOT NULL DEFAULT 'message',
    text TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    thread TEXT NOT NULL DEFAULT '',
    raw TEXT NOT NULL DEFAULT '{}',
    UNIQUE (conversation_id, source_id)
);
CREATE INDEX IF NOT EXISTS messages_by_time ON messages (conversation_id, at);
CREATE TABLE IF NOT EXISTS raw_records (
    key TEXT PRIMARY KEY,
    record TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_sends (
    token TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    what TEXT NOT NULL,
    text TEXT NOT NULL,
    fields TEXT NOT NULL DEFAULT '{}',
    due REAL NOT NULL,
    state TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS assistant_turns (
    place TEXT PRIMARY KEY,
    record TEXT NOT NULL,
    at TEXT NOT NULL
);
"""

# A message seen again keeps what it had and gains what was missing: words for a message
# that arrived blank, a note once it gets a reaction, a type once it is known.
UPSERT = """
INSERT INTO messages (conversation_id, source_id, at, sender, is_user, type, text, note, thread, raw)
VALUES (:conversation_id, :source_id, :at, :sender, :is_user, :type, :text, :note, :thread, :raw)
ON CONFLICT (conversation_id, source_id) DO UPDATE SET
    text = CASE WHEN excluded.text != '' THEN excluded.text ELSE messages.text END,
    note = CASE WHEN excluded.note != '' THEN excluded.note ELSE messages.note END,
    type = CASE WHEN excluded.type != 'message' THEN excluded.type ELSE messages.type END,
    thread = CASE WHEN excluded.thread != '' THEN excluded.thread ELSE messages.thread END,
    raw = CASE WHEN length(excluded.raw) > length(messages.raw) THEN excluded.raw ELSE messages.raw END
"""


def db_path(data_dir: Path) -> Path:
    return data_dir / "today.db"


def connect(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path(data_dir), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA)
    commitments.setup(db)
    return db


_open = threading.local()


def held(data_dir: Path) -> sqlite3.Connection | None:
    """The connection this thread has open on Today's database, if any (see ``opened``)."""
    return (_open.__dict__.get("held") or {}).get(db_path(data_dir))


@contextmanager
def opened(data_dir: Path) -> Generator[sqlite3.Connection, None, None]:
    """A connection that commits on success, rolls back on error, and always closes. Opened
    again inside itself (in the same thread), it hands back the same connection, so what
    the inner code reads and writes is part of the outer change: a second connection would
    wait for the first one's write to finish, and the first is waiting on it."""
    path = db_path(data_dir)
    held: dict[Path, sqlite3.Connection] = _open.__dict__.setdefault("held", {})
    if path in held:
        yield held[path]
        return
    db = connect(data_dir)
    held[path] = db
    try:
        with db:
            yield db
    finally:
        held.pop(path, None)
        db.close()


def _parts(hint: str) -> list[str]:
    return [p for p in re.split(r"[:_]", hint) if p]


GMAIL_ID = re.compile(r"[0-9a-f]{12,20}")


def _thread_id(record: dict[str, Any], hint: str) -> str:
    """A Gmail thread id: the key's last part when it is one, else the thread link's, else
    the first message's (Gmail names a thread after its first message)."""
    last = _parts(hint.removesuffix(".json"))[-1] if hint else ""
    if GMAIL_ID.fullmatch(last):
        return last
    link = str(record.get("url") or "").rsplit("/", 1)[-1]
    if GMAIL_ID.fullmatch(link):
        return link
    msgs = sorted(record.get("messages") or [], key=lambda m: float(m.get("internal_ms") or 0))
    return str(msgs[0]["id"]) if msgs else ""


def _conversation(record: dict[str, Any], hint: str) -> tuple[str, str]:
    """The conversation id and title of a saved record; hint is its key or file name."""
    source = record.get("source")
    if source == "text" and record.get("chat_id"):
        return f"text:{record['chat_id']}", str(record.get("channel_label") or "")
    if source == "slack":
        channel = record.get("channel") or next(
            (p for p in _parts(hint) if re.fullmatch(r"[CDG][A-Z0-9]{6,}", p)), ""
        )
        return (f"slack:{channel}" if channel else ""), str(record.get("channel_label") or "")
    if source == "email" and record.get("account"):
        thread = _thread_id(record, hint)
        return (f"email:{record['account']}:{thread}" if thread else ""), str(record.get("subject") or "")
    return "", ""


def _message(source: str, m: dict[str, Any]) -> dict[str, Any] | None:
    if source == "text":
        raw = m.get("raw") or m
        return {
            "source_id": str(m["id"]),
            "at": float(m["ts"]),
            "sender": str(m.get("name") or ""),
            "is_user": int(bool(m.get("is_me"))),
            "type": str(m.get("type") or "message").lower(),
            "text": str(m.get("text") or ""),
            "note": str(m.get("note") or ""),
            "thread": "",
            "raw": json.dumps(raw, ensure_ascii=False),
        }
    if source == "slack":
        return {
            "source_id": str(m["ts"]),
            "at": float(m["ts"]),
            "sender": str(m.get("name") or ""),
            "is_user": int(m.get("user") == SLACK_ME),
            "type": "message",
            "text": str(m.get("text") or ""),
            "note": "",
            "thread": str(m.get("thread_ts") or ""),
            "raw": json.dumps(m, ensure_ascii=False),
        }
    if source == "email":
        if m.get("draft"):
            return None  # a draft is not a message anyone sent; it changes until it goes
        return {
            "source_id": str(m["id"]),
            "at": float(m.get("internal_ms") or 0) / 1000,
            "sender": str(m.get("from") or ""),
            "is_user": int(bool(m.get("from_me"))),
            "type": "email",
            "text": str(m.get("text") or ""),
            "note": "",
            "thread": "",
            "raw": json.dumps(m, ensure_ascii=False),
        }
    return None


def _save(db: sqlite3.Connection, record: dict[str, Any], hint: str) -> str:
    conv, title = _conversation(record, hint)
    if not conv:
        return ""
    source = str(record["source"])
    db.execute(
        "INSERT INTO conversations (id, source, title, url) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET title = CASE WHEN excluded.title != '' "
        "THEN excluded.title ELSE conversations.title END",
        (conv, source, title, str(record.get("url") or "") if source != "email" else ""),
    )
    for m in record.get("messages") or []:
        row = _message(source, m)
        if row:
            db.execute(UPSERT, dict(row, conversation_id=conv))
    db.execute(
        "UPDATE conversations SET "
        "last_at = (SELECT max(at) FROM messages WHERE conversation_id = :c), "
        "last_user_at = (SELECT max(at) FROM messages WHERE conversation_id = :c AND is_user) "
        "WHERE id = :c",
        {"c": conv},
    )
    return conv


def save_record(data_dir: Path, key: str, record: dict[str, Any]) -> str:
    """Add a saved Slack, text or email record's messages; returns its conversation id
    ("" for any other record). A database problem is logged, never lost silently, and never
    stops the raw record from being saved."""
    if record.get("source") not in ("text", "slack", "email") or not record.get("messages"):
        return ""
    try:
        with opened(data_dir) as db:
            return _save(db, record, key)
    except sqlite3.Error as e:
        log.warning("conversations: could not save %s: %s", key, e)
        return ""


def save_chats(data_dir: Path, records: Iterable[dict[str, Any]]) -> int:
    """Add every recent chat at once (one transaction); returns how many were saved."""
    saved = 0
    try:
        with opened(data_dir) as db:
            for record in records:
                saved += bool(_save(db, record, ""))
    except sqlite3.Error as e:
        log.warning("conversations: could not save chats: %s", e)
    return saved


def messages(data_dir: Path, conversation_id: str) -> list[dict[str, Any]]:
    """A whole conversation, oldest first."""
    with opened(data_dir) as db:
        rows = db.execute(
            "SELECT source_id, at, sender, is_user, type, text, note, thread FROM messages "
            "WHERE conversation_id = ? ORDER BY at",
            (conversation_id,),
        ).fetchall()
    return [dict(r) for r in rows]


SEARCH_CONVERSATIONS = 5
SEARCH_MESSAGES = 6


def search(db: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    """Saved messages (Slack, texts, email) matching ``query``, grouped by conversation: the
    ones holding the most of its words first, then the newest. Most of the words must be
    there, not all, since names are saved differently in different places ("Nina" in Slack,
    "Nina Park" in texts). Gmail-style operators are read as plain words ("from:amy notion"
    finds Nina's messages that say notion)."""
    words = [w.split(":", 1)[-1].strip("\"'()") for w in query.split()]
    words = list(dict.fromkeys(w.lower() for w in words if len(w) > 1 and w.upper() not in ("OR", "AND")))[:6]
    if not words:
        return []
    hay = "lower(m.text || ' ' || m.sender || ' ' || c.title)"
    score = " + ".join(f"({hay} LIKE ?)" for _ in words)
    need = (len(words) + 1) // 2
    rows = db.execute(
        f"SELECT m.conversation_id, c.source, c.title, c.url, m.at, m.sender, m.is_user, m.text, ({score}) AS hits "
        f"FROM messages m JOIN conversations c ON c.id = m.conversation_id WHERE hits >= ? "
        "ORDER BY hits DESC, m.at DESC LIMIT 200",
        [f"%{w}%" for w in words] + [need],
    ).fetchall()
    heads: dict[str, dict[str, Any]] = {}
    said: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r[0] not in heads:
            if len(heads) >= SEARCH_CONVERSATIONS:
                continue
            heads[r[0]], said[r[0]] = {"where": f"{r[1]}: {r[2]}", "url": r[3]}, []
        if len(said[r[0]]) < SEARCH_MESSAGES:
            text = links.readable(r[7])
            msg: dict[str, Any] = {"at": r[4], "from": config.name() if r[6] else r[5], "text": text[:2000]}
            found = links.find(text)
            if found:
                msg["links"] = found  # spelled out, so a link behind a word like "Here" is never missed
            said[r[0]].append(msg)
    return [dict(h, messages=said[c]) for c, h in heads.items()]
