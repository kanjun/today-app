"""Conversations that belong to a task they didn't start (the user, 2026-11-15: "we want to be
going through net new conversations and seeing if they are associated with an existing task,
and do a single pass for old conversations").

A task is linked to the conversation it came from. The same matter often goes on elsewhere:
the Maya task came from the user's Slack DM with Robin, while the scheduling happened in an email
thread with Maya's assistant. Each refresh looks at conversations with new messages that no
open task reads yet, and asks which open task (if any) each is about. One that is gets
attached to the task the way the user attaches a thread themselves (attached.py), marked as Today's
doing (``by: "today"`` and why), so the judge reads it with the task, the card's assistant
reads it, and the card shows it with a way to take it off. One the user took off is never
attached again (``detail.detached``).

Cheap first: a conversation is only shown to the AI with the open tasks whose names it
mentions (a person or place in the task's title, or the person it is with), so most
conversations never cost a call.
"""

import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from today import ai, attached, commitments, config, conversations, store

log = logging.getLogger(__name__)

MODEL = "sonnet"
TIMEOUT = 240
BATCH = 8  # conversations per AI call
WORKERS = 3
RUN_MAX = 48  # conversations checked per refresh, newest first (the rest wait for the next one)
CANDIDATES = 6  # open tasks shown per conversation, best name matches first
RECENT_MESSAGES = 20
MESSAGE_CHARS = 500
COMMON_SHARE = 0.12  # a word in more than this share of open task titles is no name ("Launch", "Reply")
NOT_NAMES = {"Today", "Tell", "Reply", "Send", "Ask", "Wait", "Talk", "Draft", "Check", "Set",
             "Book", "Add", "Sign", "Fill", "Reach", "Onboard", "Email", "Text", "Call", "Meet", "Get", "Make", "Find",
             "The", "And", "For", "With", "About", "From", "Her", "His", "Their", "Your", "Nov", "Dec", "Jan"}

# Login and verification codes, and passing them along ("217903", "Lmk if code received!"), say
# nothing about a task (the user, 2026-11-15: "they just add noise").
CODE_ONLY = re.compile(r"^\W*(?:[A-Z]-?)?\d{4,8}\W*$")
CODE_TALK = re.compile(r"\b(?:verification|security|login|log-in|sign-in|one-time|confirmation|access|passcode|otp|2fa|code)\b"
                       r".{0,40}\b(?:code|is|received|got|sent)\b|\b(?:text|texts|sms) w(?:ith|/)? code\b|\bcode received\b"
                       r"|\b\d{4,8}\b.{0,30}\b(?:is your|verification|code)\b", re.I)


def is_code(text: str) -> bool:
    t = " ".join((text or "").split())
    return bool(CODE_ONLY.match(t)) or (len(t) < 200 and bool(CODE_TALK.search(t)))


CHECKED = (
    "CREATE TABLE IF NOT EXISTS match_checked (conversation_id TEXT PRIMARY KEY, last_at REAL NOT NULL, "
    "at TEXT NOT NULL, linked TEXT NOT NULL DEFAULT '')"
)

SYSTEM = """You connect the user's conversations to the user's open tasks in Today. {ABOUT} Each conversation below (an email thread, a Slack conversation, or a text chat, labelled C1, C2, ...) comes with the open tasks it might belong to (labelled T1, T2, ...), each with where it came from.

Login codes, verification codes, and messages passing them along are never about a task: leave a conversation that is mostly that out.

A conversation belongs to a task only when it is about that task's own matter: the same event, ask, plan or decision, so reading it would change what the user knows or has to do for that task (like an email with Maya's assistant scheduling the call the task "Talk with Maya about the year plan" is about). Sharing a person is not enough: the same person talks about many things. When unsure, leave it out.

Answer with exactly one JSON object and nothing else: {"matches": [{"conversation": "C1", "task": "T2", "why": "a few plain words, like: Clara schedules the Maya planning call"}]}. A conversation can belong to more than one task. No matches is fine: {"matches": []}."""


def _setup(db: sqlite3.Connection) -> None:
    db.execute(CHECKED)


# ---- names ---------------------------------------------------------------------------------


def _words(text: str) -> list[str]:
    return re.findall(r"\b[A-Z][A-Za-z'’]{2,}\b|\b[A-Z]{2,}\b", text or "")


def task_names(tasks: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Task id -> the names it goes by, each with a weight: a name in its title or project
    counts 2, the person it is with 1. Words common to many tasks are left out."""
    counts: dict[str, int] = {}
    for t in tasks:
        for w in set(_words(t["title"])):
            counts[w] = counts.get(w, 0) + 1
    common = {w for w, n in counts.items() if n > max(2, COMMON_SHARE * len(tasks))}
    common.add(config.name())  # the user's own name ties a message to no one task
    out: dict[str, dict[str, int]] = {}
    for t in tasks:
        names: dict[str, int] = {}
        for w in _words(t["title"])[1:] + _words(t.get("project") or ""):
            if w not in NOT_NAMES and w not in common:
                names[w] = 2
        who = str(t.get("counterparty") or "").split(" ")[0]
        if len(who) >= 3 and who not in NOT_NAMES:
            names.setdefault(who, 1)
        if names:
            out[t["id"]] = names
    return out


def candidates_for(blob: str, names: dict[str, dict[str, int]]) -> list[tuple[str, int]]:
    """Open tasks whose names this conversation mentions, best first (a title name is needed:
    the person alone matches too much)."""
    found = []
    for tid, ns in names.items():
        hit = {w: n for w, n in ns.items() if re.search(rf"\b{re.escape(w)}\b", blob)}
        if any(n >= 2 for n in hit.values()):
            found.append((tid, sum(hit.values())))
    return sorted(found, key=lambda x: -x[1])[:CANDIDATES]


# ---- what is looked at ---------------------------------------------------------------------


def open_tasks(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT * FROM commitments WHERE stage = 'open' AND is_project = 0 AND labels NOT LIKE '%\"step\"%' AND labels NOT LIKE '%\"move\"%'"
    ).fetchall()
    tasks = [commitments._row(r) for r in rows]
    by_id = {t["id"]: t for t in tasks}
    for t in tasks:
        p = by_id.get(t.get("parent_id") or "")
        if p is None and t.get("parent_id"):
            p = commitments.get(db, t["parent_id"])
        t["project"] = p["title"] if p else ""
    return tasks


def _conversation(db: sqlite3.Connection, conv: str) -> dict[str, Any]:
    head = db.execute("SELECT title, source, last_at FROM conversations WHERE id = ?", (conv,)).fetchone()
    rows = db.execute(
        "SELECT at, sender, is_user, text FROM messages WHERE conversation_id = ? ORDER BY at DESC LIMIT ?",
        (conv, RECENT_MESSAGES)).fetchall()
    rows = [r for r in rows if not is_code(r["text"] or "")]
    lines = [f"{config.name() if r['is_user'] else r['sender']}: {' '.join((r['text'] or '').split())[:MESSAGE_CHARS]}"
             for r in reversed(rows)]
    return {"id": conv, "title": head["title"] if head else conv, "source": head["source"] if head else "",
            "last_at": (head["last_at"] if head else 0) or 0, "lines": lines}


def due(db: sqlite3.Connection, since_days: float | None, limit: int | None) -> list[str]:
    """Conversations with messages newer than their last check, that no open task reads yet,
    newest first; ``since_days`` keeps only ones active that recently."""
    _setup(db)
    cutoff = time.time() - since_days * 86400 if since_days else 0
    rows = db.execute(
        "SELECT c.id FROM conversations c LEFT JOIN match_checked m ON m.conversation_id = c.id "
        "WHERE c.last_at > ? AND (m.last_at IS NULL OR c.last_at > m.last_at) "
        "AND NOT EXISTS (SELECT 1 FROM commitment_links l JOIN commitments k ON k.id = l.commitment_id "
        "WHERE l.conversation_id = c.id AND k.stage = 'open' AND k.is_project = 0) "
        "ORDER BY c.last_at DESC", (cutoff,)).fetchall()
    out = [r[0] for r in rows]
    return out[:limit] if limit else out


# ---- asking and linking --------------------------------------------------------------------


def _ask(batch: list[dict[str, Any]], tasks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One AI call for up to BATCH conversations; returns matches as conversation id, task id, why."""
    parts, tlabels = [], {}
    for n, conv in enumerate(batch, 1):
        shown = []
        for tid in conv["candidates"]:
            label = tlabels.setdefault(tid, f"T{len(tlabels) + 1}")
            t = tasks[tid]
            shown.append({"task": label, "title": t["title"], "with": t.get("counterparty") or "",
                          "project": t.get("project") or "", "came_from": str(t["detail"].get("ask_text") or "")[:300]})
        parts.append(f"C{n}: {conv['title']} ({conv['source']}), newest last:\n" + "\n".join(conv["lines"])
                     + "\nTasks it might belong to:\n" + json.dumps(shown, ensure_ascii=False, indent=1))
    answer = ai.complete_json("\n\n".join(parts) + "\n\nAnswer with the JSON object.", system=SYSTEM, model=MODEL,
                              timeout=TIMEOUT)
    by_label = {v: k for k, v in tlabels.items()}
    out = []
    for m in answer.get("matches") or []:
        if not isinstance(m, dict):
            continue
        try:
            conv = batch[int(str(m.get("conversation") or "C0")[1:]) - 1]
        except (ValueError, IndexError):
            continue
        tid = by_label.get(str(m.get("task") or ""))
        if tid in conv["candidates"]:
            out.append({"conversation": conv["id"], "title": conv["title"], "task": tid, "why": str(m.get("why") or "")[:200]})
    return out


def link(db: sqlite3.Connection, task_id: str, conv: str, title: str, why: str) -> bool:
    """Attach a conversation to a task as Today's doing; never one the user took off."""
    c = commitments.get(db, task_id)
    if c is None or c["stage"] != "open":
        return False
    d = c["detail"]
    if conv in (d.get("detached") or []) or any(a["conversation"] == conv for a in attached.of(c)):
        return False
    entry = {"conversation": conv, "title": title, "at": store.now_iso(), "by": "today", "why": why}
    commitments.change(db, task_id, "judge", {"detail": dict(d, attached=[*attached.of(c), entry])},
                       reason=f"this conversation is about it: {why}")
    commitments.link(db, task_id, conv, "", attached.ROLE)
    return True


def run(since_days: float | None = 14, limit: int | None = RUN_MAX) -> dict[str, Any]:
    """Look at conversations with new messages and attach each to the open task it is about."""
    with conversations.opened(store.DATA_DIR) as db:
        convs = due(db, since_days, limit)
        tasks = {t["id"]: t for t in open_tasks(db)}
        names = task_names(list(tasks.values()))
        looked = [_conversation(db, c) for c in convs]
    for conv in looked:
        if not conv["lines"]:  # nothing but codes
            conv["candidates"] = []
            continue
        conv["candidates"] = [tid for tid, _ in candidates_for(conv["title"] + "\n" + "\n".join(conv["lines"]), names)]
    ask = [c for c in looked if c["candidates"]]
    batches = [ask[i:i + BATCH] for i in range(0, len(ask), BATCH)]
    matches: list[dict[str, Any]] = []
    failed: set[str] = set()

    def one(b: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            return _ask(b, tasks)
        except ai.ClaudeError as e:
            log.warning("matching conversations failed: %s", e)
            failed.update(c["id"] for c in b)
            return []

    with ThreadPoolExecutor(WORKERS) as pool:
        for found in pool.map(one, batches):
            matches += found
    linked = []
    with conversations.opened(store.DATA_DIR) as db:
        _setup(db)
        for m in matches:
            if link(db, m["task"], m["conversation"], m["title"], m["why"]):
                linked.append(m)
        for conv in looked:
            if conv["id"] in failed:
                continue  # asked again next time
            mine = ",".join(m["task"] for m in linked if m["conversation"] == conv["id"])
            db.execute("INSERT INTO match_checked (conversation_id, last_at, at, linked) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT (conversation_id) DO UPDATE SET last_at = excluded.last_at, at = excluded.at, "
                       "linked = CASE WHEN excluded.linked != '' THEN excluded.linked ELSE match_checked.linked END",
                       (conv["id"], conv["last_at"], store.now_iso(), mine))
    return {"looked": len(looked), "asked": len(ask), "linked": [
        {"task": tasks[m["task"]]["title"], "conversation": m["title"], "why": m["why"]} for m in linked]}
