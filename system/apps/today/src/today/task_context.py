"""Everything about one task that an AI working on it should know, in one place (the user,
2026-11-15: "any AI that runs on a card" sees the main task's card and every step's card, so
it has the full context for its decisions).

The picture of a task holds:
- its steps, each with everything the step holds (note, link, kind, what the user filled in);
- its notes: what the user told the assistant on any of its cards, and what the user asked it for
  that it needs to keep knowing, distilled into short lines (``task_notes``);
- what was done on each of its cards (the task's and every step's), newest kept.

What was done on a card is kept in the database, ``assistant_turns``, one row per card
(place ``task:<key>|<focus>``: "task" for the task's own card, a step's id for a step's), the
same table that keeps the project box's and the bottom bar's (the user, 2026-11-15: everything
in the database).

Notes live in their own table so a note can be crossed out, replaced or undone on its own:
nothing is deleted, a removed note only gets ``removed_at`` and who removed it. One the user
crossed out stays known as the user's to have removed, so it is never written again. Each note
keeps the words of the user's it came from (``said``) and which card the user said them on.
"""

import hashlib
import json
import sqlite3
from typing import Any

from today import attached, commitments, conversations, store

HISTORY_CHARS = 2500  # card history shown, newest kept; the notes carry what matters from older turns
TURN_CHARS = 300
NOTE_CHARS = 300
NOTES_MAX = 30  # open notes on one task, at most (the oldest give way)
REMOVED_SHOWN = 15

# Undo of a note: the only statements an undo runs on this table (project_book.UNDO_SQL).
HIDE_SQL = "UPDATE task_notes SET removed_at = ?, removed_by = ? WHERE id = ?"
SHOW_SQL = "UPDATE task_notes SET removed_at = '', removed_by = '' WHERE id = ?"


def setup(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS task_notes (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, step_id TEXT NOT NULL DEFAULT '', "
        "text TEXT NOT NULL, said TEXT NOT NULL DEFAULT '', focus TEXT NOT NULL DEFAULT '', by TEXT NOT NULL DEFAULT 'assistant', "
        "at TEXT NOT NULL, replaces TEXT NOT NULL DEFAULT '', removed_at TEXT NOT NULL DEFAULT '', "
        "removed_by TEXT NOT NULL DEFAULT '')"
    )
    db.execute("CREATE INDEX IF NOT EXISTS task_notes_task ON task_notes (task_id)")


# ---- the task and its steps ----------------------------------------------------------------


def task_of(db: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """The commitment behind a card's key (a Today item's key, or a commitment id)."""
    c = commitments.by_legacy_key(db, key)
    if c is None and len(key) == 11 and key.startswith("c") and ":" not in key:
        c = commitments.get(db, key)
    return c


def steps(db: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """The task's steps in order (a step replaced or undone is no longer one of them)."""
    rows = db.execute(
        "SELECT id FROM commitments WHERE parent_id = ? AND labels LIKE '%\"step\"%' AND stage NOT IN ('stale', 'declined') "
        "ORDER BY position, id", (task_id,),
    ).fetchall()
    return [c for c in (commitments.get(db, r[0]) for r in rows) if c is not None]


def step_labels(db: sqlite3.Connection, task_id: str) -> dict[str, str]:
    """Step id -> its label in the picture (S1, S2, ...)."""
    return {s["id"]: f"S{n}" for n, s in enumerate(steps(db, task_id), 1)}


# ---- notes ---------------------------------------------------------------------------------


def notes(db: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """Open notes on a task, oldest first."""
    setup(db)
    return [dict(r) for r in db.execute(
        "SELECT * FROM task_notes WHERE task_id = ? AND removed_at = '' ORDER BY at, rowid", (task_id,))]


def removed_by_user(db: sqlite3.Connection, task_id: str) -> list[str]:
    setup(db)
    return [r[0] for r in db.execute(
        "SELECT text FROM task_notes WHERE task_id = ? AND removed_by = 'user' ORDER BY removed_at DESC LIMIT ?",
        (task_id, REMOVED_SHOWN))]


def note_labels(db: sqlite3.Connection, task_id: str) -> dict[str, str]:
    """Note label in the picture (N1, N2, ...) -> note id."""
    return {f"N{n}": x["id"] for n, x in enumerate(notes(db, task_id), 1)}


def add_note(db: sqlite3.Connection, task_id: str, text: str, *, step_id: str = "", said: str = "", focus: str = "",
             by: str = "assistant", replaces: str = "") -> str | None:
    """Keep a note on a task; returns its id (None for an empty note or one already there, or
    one the user crossed out before)."""
    setup(db)
    text = " ".join(text.split())[:NOTE_CHARS]
    if not text:
        return None
    same = db.execute(
        "SELECT removed_by FROM task_notes WHERE task_id = ? AND lower(text) = lower(?) AND (removed_at = '' OR removed_by = 'user')",
        (task_id, text)).fetchone()
    if same is not None:
        return None
    at = store.now_iso()
    nid = "n" + hashlib.sha1(f"{task_id}|{text}|{at}".encode()).hexdigest()[:10]
    db.execute(
        "INSERT INTO task_notes (id, task_id, step_id, text, said, focus, by, at, replaces) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (nid, task_id, step_id, text, said[:2000], focus, by, at, replaces))
    old = notes(db, task_id)
    for x in old[:max(0, len(old) - NOTES_MAX)]:
        db.execute(HIDE_SQL, (at, "too_many", x["id"]))
    return nid


def hide_note(db: sqlite3.Connection, nid: str, by: str) -> bool:
    setup(db)
    cur = db.execute("UPDATE task_notes SET removed_at = ?, removed_by = ? WHERE id = ? AND removed_at = ''",
                     (store.now_iso(), by, nid))
    return cur.rowcount > 0


def for_card(db: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """The notes as the card shows them: the line, the step it is about, and the user's words it came from."""
    names = {s["id"]: s["title"] for s in steps(db, task_id)}
    return [{"id": x["id"], "text": x["text"], "step": names.get(x["step_id"], ""), "said": x["said"],
             "at": x["at"][:10]} for x in notes(db, task_id)]


# ---- what was done on its cards ------------------------------------------------------------


def _card_prefix(key: str) -> str:
    return f"task:{key}|"


def card_records(db: sqlite3.Connection, key: str) -> dict[str, dict[str, Any]]:
    """What was done on each card of a task, by focus ("task", or a step's id)."""
    prefix = _card_prefix(key)
    rows = db.execute("SELECT place, record FROM assistant_turns WHERE substr(place, 1, ?) = ?",
                      (len(prefix), prefix)).fetchall()
    return {r[0][len(prefix):]: json.loads(r[1]) for r in rows}


def card_records_now(key: str) -> dict[str, dict[str, Any]]:
    with conversations.opened(store.DATA_DIR) as db:
        return card_records(db, key)


def save_card_record(db: sqlite3.Connection, key: str, focus: str, rec: dict[str, Any]) -> None:
    db.execute("INSERT OR REPLACE INTO assistant_turns (place, record, at) VALUES (?, ?, ?)",
               (_card_prefix(key) + (focus or "task"), json.dumps(rec, ensure_ascii=False), store.now_iso()))


def history(records: dict[str, dict[str, Any]], labels: dict[str, str], budget: int = HISTORY_CHARS,
            keep_focus: str = "") -> list[str]:
    """What was said and done on every card of the task (the task's own and each step's),
    oldest first, cut from the oldest end to ``budget`` characters. The last two turns on
    ``keep_focus`` (the card the user is on) always stay."""
    turns = []
    for focus, rec in records.items():
        where = "task" if focus == "task" else labels.get(focus, "an earlier step")
        for t in (rec or {}).get("turns") or []:
            line = f"[{str(t.get('at') or '')[:10]}, on the {where} card] the user: {t.get('label') or t.get('said') or ''}"
            if t.get("did"):
                line += f" | assistant: {t['did']}"
            turns.append((str(t.get("at") or ""), focus, line[:TURN_CHARS]))
    turns.sort(key=lambda t: t[0])
    keep = {id(t) for t in [t for t in turns if t[1] == (keep_focus or "task")][-2:]}
    out: list[str] = []
    used = 0
    for t in reversed(turns):
        if id(t) in keep or used + len(t[2]) <= budget:
            out.append(t[2])
            used += len(t[2])
    return list(reversed(out))


# ---- the whole picture ---------------------------------------------------------------------


def _step_view(s: dict[str, Any], label: str) -> dict[str, Any]:
    out: dict[str, Any] = {"step": label, "text": s["title"], "done": s["stage"] != "open"}
    for k, v in (s.get("detail") or {}).items():
        if v not in (None, "", [], {}) and k != "text":
            out[k] = v
    if s["stage"] == "done" and s.get("stage_reason"):
        out["how_done"] = s["stage_reason"]
    return out


def picture(db: sqlite3.Connection, c: dict[str, Any], item: dict[str, Any] | None, *, focus: str = "",
            history_chars: int = HISTORY_CHARS) -> dict[str, Any]:
    """The task's steps, notes and card history, labelled for an AI (S1.. steps, N1.. notes).
    ``history_chars`` 0 leaves the card history out (the notes are its short form)."""
    labels = step_labels(db, c["id"])
    out: dict[str, Any] = {}
    # When it is due, and the words the date comes from (the user, 2026-11-15: the Maya task was due
    # the day of the planning call, and the assistant couldn't see it).
    d = c.get("detail") or {}
    if c.get("due"):
        out["due"] = c["due"]
    for k in ("due_event", "due_quote"):
        if d.get(k):
            out[k] = d[k]
    all_steps = steps(db, c["id"])
    if all_steps:
        out["steps"] = [_step_view(s, labels[s["id"]]) for s in all_steps]
    on = next((s for s in all_steps if s["id"] == focus), None)
    if on is not None:
        out["user_is_on_step"] = f"{labels[focus]}: {on['title']}"
    kept = notes(db, c["id"])
    if kept:
        out["notes"] = [{"note": f"N{n}", "text": x["text"], **({"about_step": labels[x["step_id"]]} if x["step_id"] in labels else {}),
                         "from": x["at"][:10]} for n, x in enumerate(kept, 1)]
    gone = removed_by_user(db, c["id"])
    if gone:
        out["notes_user_crossed_out"] = gone
    if attached.of(c):
        out["attached_threads"] = [{"thread": f"A{n}", "title": a.get("title", "")} for n, a in enumerate(attached.of(c), 1)]
    key = str((item or {}).get("key") or c.get("legacy_key") or c["id"])
    if history_chars:
        lines = history(card_records(db, key), labels, history_chars, focus)
        if lines:
            out["card_history"] = lines
    return out


