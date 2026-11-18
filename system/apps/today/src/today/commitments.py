"""Commitments: what the user tracks, each with a history of every change.

A commitment is a task framed as a promise. Every one has the same shape: who owes whom
(``direction``), how it started (``origin``), who started it, and where it stands
(``stage``). A project is a commitment to themselves with commitments under it; a weekly
priority is a task of the user's with ``week`` set, made from the user's weekly note (``detail.week_goal``).

Nothing is overwritten without a trace: every change is an event naming who made it
(``user``, ``judge``, ``sync``, ``import``, or ``assistant`` for an AI change the user asked for), why, and which messages back it. The
``commitments`` row is the saved result of its events, for fast reading.

Two rules protect the user's decisions:
- A write names the version it read; a write against an older version is refused
  (``StaleWrite``), so a slow background judgment can't undo a click made meanwhile.
- The judge can reopen something the user closed or parked only if the other person wrote
  in a linked conversation after the user's decision (``user_decision_at``).
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Any

DIRECTIONS = ("mine", "theirs", "self", "world")  # the user owes them / they owe the user / to themselves / to the world
ORIGINS = ("they_asked", "user_asked", "user_promised", "user_offered", "user_chose", "handed")
STAGES = ("open", "done", "declined", "stale", "parked")
ACTORS = ("user", "judge", "sync", "import", "assistant")
# "assistant" is a change Today's AI made because the user asked for it, in a project's box or the
# feedback bar (projects stage): it counts as the user's decision, like the user's own clicks.
USER_ACTORS = ("user", "assistant")

SCHEMA = """
CREATE TABLE IF NOT EXISTS commitments (
    id TEXT PRIMARY KEY,
    legacy_key TEXT UNIQUE,
    title TEXT NOT NULL,
    direction TEXT NOT NULL,
    origin TEXT NOT NULL,
    started_by TEXT NOT NULL,
    counterparty TEXT NOT NULL DEFAULT '',
    parent_id TEXT REFERENCES commitments(id),
    is_project INTEGER NOT NULL DEFAULT 0,
    labels TEXT NOT NULL DEFAULT '[]',
    stage TEXT NOT NULL DEFAULT 'open',
    stage_reason TEXT NOT NULL DEFAULT '',
    next_step TEXT NOT NULL DEFAULT '',
    due TEXT NOT NULL DEFAULT '',
    week TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}',
    user_decision_at TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS commitments_by_stage ON commitments (stage, direction);
CREATE INDEX IF NOT EXISTS commitments_by_parent ON commitments (parent_id);
CREATE TABLE IF NOT EXISTS commitment_links (
    commitment_id TEXT NOT NULL REFERENCES commitments(id),
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'origin',
    PRIMARY KEY (commitment_id, conversation_id, message_id, role)
);
CREATE INDEX IF NOT EXISTS links_by_conversation ON commitment_links (conversation_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(id),
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    change TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS events_by_commitment ON events (commitment_id, id);
"""

# Fields a change may set; everything else about a commitment is fixed at creation or
# lives in its links.
FIELDS = (
    "title", "direction", "origin", "started_by", "counterparty", "parent_id", "is_project",
    "labels", "stage", "stage_reason", "next_step", "due", "week", "position", "detail",
)
JSON_FIELDS = ("labels", "detail")


class CommitmentError(Exception):
    """A change that can't be made: an unknown commitment, field or value."""


class StaleWrite(CommitmentError):
    """The commitment changed since the writer read it."""


class Refused(CommitmentError):
    """The change breaks a rule that protects the user's decisions."""


def setup(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    # Each task's card (cards.py): what Today keeps on it besides what is true about it.
    if "card" not in {r[1] for r in db.execute("PRAGMA table_info(commitments)")}:
        db.execute("ALTER TABLE commitments ADD COLUMN card TEXT NOT NULL DEFAULT ''")
    # A count of every change to any task, so a reader can tell whether what it read is current.
    db.executescript("""
CREATE TABLE IF NOT EXISTS commitments_seq (n INTEGER NOT NULL);
INSERT INTO commitments_seq (n) SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM commitments_seq);
CREATE TRIGGER IF NOT EXISTS commitments_seq_i AFTER INSERT ON commitments BEGIN UPDATE commitments_seq SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS commitments_seq_u AFTER UPDATE ON commitments BEGIN UPDATE commitments_seq SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS commitments_seq_d AFTER DELETE ON commitments BEGIN UPDATE commitments_seq SET n = n + 1; END;
""")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id(seed: str) -> str:
    return "c" + hashlib.sha1(seed.encode()).hexdigest()[:10]


def _check(fields: dict[str, Any]) -> None:
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise CommitmentError(f"unknown commitment fields: {sorted(unknown)}")
    for name, allowed in (("direction", DIRECTIONS), ("origin", ORIGINS), ("stage", STAGES)):
        if name in fields and fields[name] not in allowed:
            raise CommitmentError(f"{name} must be one of {allowed}, not {fields[name]!r}")


def _stored(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS else v for k, v in fields.items()}


def _row(r: sqlite3.Row) -> dict[str, Any]:
    out = dict(r)
    for k in JSON_FIELDS:
        out[k] = json.loads(out[k] or ("[]" if k == "labels" else "{}"))
    return out  # its card stays as saved until something reads it (cards.card_of)


def set_card(db: sqlite3.Connection, cid: str, card: dict[str, Any] | None) -> None:
    """Save a task's card. A card is working material (a draft, the emails behind it, the user
    answers so far), not a decision: no event, and the version stays, so a write made from
    an earlier read of the task is still accepted."""
    db.execute("UPDATE commitments SET card = ? WHERE id = ?",
               ("" if card is None else json.dumps(card, ensure_ascii=False), cid))


def _event(
    db: sqlite3.Connection, cid: str, actor: str, kind: str, change: dict[str, Any], reason: str, evidence: Iterable[str]
) -> None:
    if actor not in ACTORS:
        raise CommitmentError(f"actor must be one of {ACTORS}")
    db.execute(
        "INSERT INTO events (commitment_id, at, actor, kind, change, reason, evidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cid, now_iso(), actor, kind, json.dumps(change, ensure_ascii=False), reason, json.dumps(list(evidence))),
    )


def create(
    db: sqlite3.Connection,
    actor: str,
    fields: dict[str, Any],
    *,
    cid: str = "",
    legacy_key: str | None = None,
    links: Iterable[tuple[str, str, str]] = (),
    reason: str = "",
) -> str:
    """A new commitment; links are (conversation id, message id, role)."""
    _check(fields)
    for need in ("title", "direction", "origin", "started_by"):
        if not fields.get(need):
            raise CommitmentError(f"a commitment needs {need}")
    cid = cid or new_id(legacy_key or f"{fields['title']}|{now_iso()}")
    at = now_iso()
    row = _stored(dict(fields, id=cid, legacy_key=legacy_key, created_at=at, updated_at=at))
    if actor in USER_ACTORS:
        row["user_decision_at"] = at
    cols = ", ".join(row)
    db.execute(f"INSERT INTO commitments ({cols}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
    links = list(links)
    db.executemany(
        "INSERT OR IGNORE INTO commitment_links (commitment_id, conversation_id, message_id, role) VALUES (?, ?, ?, ?)",
        [(cid, conv, msg, role) for conv, msg, role in links],
    )
    _event(db, cid, actor, "created", fields, reason, [f"{c}#{m}" for c, m, _ in links if m])
    return cid


def get(db: sqlite3.Connection, cid: str) -> dict[str, Any] | None:
    r = db.execute("SELECT * FROM commitments WHERE id = ?", (cid,)).fetchone()
    return _row(r) if r else None


def by_legacy_key(db: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    r = db.execute("SELECT * FROM commitments WHERE legacy_key = ?", (key,)).fetchone()
    return _row(r) if r else None


def source_message(db: sqlite3.Connection, key: str) -> str:
    """The message a task came from (its saved source), by commitment id or item key; "" when none is saved."""
    r = db.execute(
        "SELECT l.message_id FROM commitments c JOIN commitment_links l ON l.commitment_id = c.id "
        "WHERE (c.id = ? OR c.legacy_key = ?) AND l.role = 'origin' AND l.message_id != ''", (key, key),
    ).fetchone()
    return str(r[0]) if r else ""


def origin(db: sqlite3.Connection, key: str) -> tuple[str, str]:
    """The conversation and message a task came from, by commitment id or item key; ("", "") when none is saved."""
    r = db.execute(
        "SELECT l.conversation_id, l.message_id FROM commitments c JOIN commitment_links l ON l.commitment_id = c.id "
        "WHERE (c.id = ? OR c.legacy_key = ?) AND l.role = 'origin' AND l.message_id != ''", (key, key),
    ).fetchone()
    return (str(r[0]), str(r[1])) if r else ("", "")


def they_wrote_since(db: sqlite3.Connection, cid: str, since: str) -> bool:
    """Did anyone but the user write in a conversation linked to this commitment after ``since``?"""
    if not since:
        return True
    at = datetime.fromisoformat(since).timestamp()
    r = db.execute(
        "SELECT 1 FROM messages m JOIN commitment_links l ON l.conversation_id = m.conversation_id "
        "WHERE l.commitment_id = ? AND NOT m.is_user AND m.at > ? LIMIT 1",
        (cid, at),
    ).fetchone()
    return r is not None


def change(
    db: sqlite3.Connection,
    cid: str,
    actor: str,
    fields: dict[str, Any],
    *,
    expect_version: int | None = None,
    reason: str = "",
    evidence: Iterable[str] = (),
    kind: str = "changed",
) -> dict[str, Any]:
    """Apply a change as one event; returns the commitment after it."""
    _check(fields)
    cur = get(db, cid)
    if cur is None:
        raise CommitmentError(f"no commitment {cid}")
    if expect_version is not None and cur["version"] != expect_version:
        raise StaleWrite(f"{cid} is at version {cur['version']}, not {expect_version}")
    reopening = fields.get("stage") == "open" and cur["stage"] != "open"
    if actor not in USER_ACTORS and reopening and cur["user_decision_at"] and not they_wrote_since(db, cid, cur["user_decision_at"]):
        raise Refused(f"{cid}: the user decided this at {cur['user_decision_at']} and no one has written since")
    at = now_iso()
    sets = _stored(dict(fields, updated_at=at))
    if actor in USER_ACTORS:
        sets["user_decision_at"] = at
    assign = ", ".join(f"{k} = ?" for k in sets)
    db.execute(
        f"UPDATE commitments SET {assign}, version = version + 1 WHERE id = ?",
        (*sets.values(), cid),
    )
    _event(db, cid, actor, kind, fields, reason, evidence)
    out = get(db, cid)
    assert out is not None
    return out


def link(db: sqlite3.Connection, cid: str, conversation_id: str, message_id: str = "", role: str = "evidence") -> None:
    db.execute(
        "INSERT OR IGNORE INTO commitment_links (commitment_id, conversation_id, message_id, role) VALUES (?, ?, ?, ?)",
        (cid, conversation_id, message_id, role),
    )


def for_conversation(db: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
    """Every commitment linked to a conversation (one indexed lookup)."""
    rows = db.execute(
        "SELECT DISTINCT c.* FROM commitments c JOIN commitment_links l ON l.commitment_id = c.id "
        "WHERE l.conversation_id = ? ORDER BY c.created_at",
        (conversation_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def conversations_of(db: sqlite3.Connection, cid: str) -> list[str]:
    rows = db.execute(
        "SELECT DISTINCT conversation_id FROM commitment_links WHERE commitment_id = ?", (cid,)
    ).fetchall()
    return [r[0] for r in rows]


def history(db: sqlite3.Connection, cid: str) -> list[dict[str, Any]]:
    rows = db.execute("SELECT * FROM events WHERE commitment_id = ? ORDER BY id", (cid,)).fetchall()
    return [dict(r, change=json.loads(r["change"]), evidence=json.loads(r["evidence"])) for r in rows]


def all_commitments(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [_row(r) for r in db.execute("SELECT * FROM commitments ORDER BY position, created_at")]
