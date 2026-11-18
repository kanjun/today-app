"""Threads the user attaches to a task by hand (the user, 2026-11-15: "attach the two
project threads to the Sam task").

Any saved conversation can be attached to any task: an email thread, a Slack channel or DM. Attaching links the conversation to the task's commitment (role
"attached") and notes it on the commitment (``detail.attached``: which conversation, its
name, when), both in the card's undoable batch. From then on:
- the refresh keeps the conversation's messages fresh (email threads and Slack
  conversations are fetched here);
- the judge reads it with the task whenever something new is said there;
- the task's card shows it, linked to the whole saved conversation, and its assistant reads it.

There is nothing special about what is attached: the same reading as every other
conversation, with the task in view.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from today import commitments, config, connectors, conversations, store

log = logging.getLogger(__name__)


class AttachError(ValueError):
    """A conversation that can't be attached; the message says why, in plain words."""


ROLE = "attached"
DETACH_SQL = "DELETE FROM commitment_links WHERE commitment_id = ? AND conversation_id = ? AND message_id = '' AND role = 'attached'"
FOUND_MAX = 8
SLACK_MESSAGES = "100"
PREVIEW = 3


def _when(at: float | None) -> str:
    return datetime.fromtimestamp(at).strftime("%b %d, %Y") if at else ""


def _kind(conv: str, title: str) -> str:
    if conv.startswith("text:"):
        return title.split(" ", 1)[0] if title else "Text"
    return {"email": "Email", "slack": "Slack"}.get(conv.split(":", 1)[0], "")


def find(query: str) -> list[dict[str, Any]]:
    """Conversations whose name matches: the email threads and Slack conversations Today has
    saved. Newest first."""
    query = query.strip()
    if not query:
        return []
    found: dict[str, dict[str, Any]] = {}
    with conversations.opened(store.DATA_DIR) as db:
        for r in db.execute(
            "SELECT id, title, last_at FROM conversations WHERE title LIKE ? ORDER BY last_at DESC LIMIT ?",
            (f"%{query}%", FOUND_MAX),
        ):
            found.setdefault(r["id"], {"conversation": r["id"], "title": r["title"], "last": r["last_at"] or 0.0})
        out = sorted(found.values(), key=lambda f: -(f["last"] or 0))[:FOUND_MAX]
        for f in out:
            rows = db.execute(
                "SELECT sender, is_user, text FROM messages WHERE conversation_id = ? ORDER BY at DESC LIMIT ?",
                (f["conversation"], PREVIEW),
            ).fetchall()
            f["latest"] = [f"{config.name() if r['is_user'] else r['sender']}: {(r['text'] or '')[:200]}" for r in reversed(rows)]
            f["when"] = _when(f.pop("last"))
    return out


def fetch(conv: str) -> bool:
    """Bring a conversation's messages up to date in the conversations database. False when
    its source can't be reached now (what was saved before stays)."""
    try:
        if conv.startswith("email:"):
            _, account, thread = conv.split(":", 2)
            msgs = connectors.thread_messages(connectors.gmail_thread(thread, account))
            record = {"source": "email", "account": account, "subject": msgs[0]["subject"] if msgs else "",
                      "messages": msgs, "url": f"https://mail.google.com/mail/u/{account}/#all/{thread}",
                      "fetched_at": store.now_iso()}
            return bool(conversations.save_record(store.DATA_DIR, conv, record))
        if conv.startswith("slack:"):
            return bool(conversations.save_record(store.DATA_DIR, conv, _slack(conv.removeprefix("slack:"))))
    except (connectors.ConnectorError, OSError, KeyError, ValueError) as e:
        log.warning("attached: could not fetch %s: %s", conv, e)
    return False


def _slack(channel: str) -> dict[str, Any]:
    me = str(connectors.slack_get("auth.test", {})["user_id"])
    names = _slack_names()
    my_name = config.name()
    msgs = connectors.slack_get("conversations.history", {"channel": channel, "limit": SLACK_MESSAGES}).get("messages", [])
    out = []
    for m in reversed(msgs):
        if m.get("type") != "message":
            continue
        uid = str(m.get("user") or m.get("bot_id") or "?")
        text = re.sub(r"<@([A-Z0-9]+)>", lambda mm: "@" + (my_name if mm.group(1) == me else names.get(mm.group(1), mm.group(1))),
                      str(m.get("text") or ""))
        out.append({"ts": m["ts"], "user": uid, "name": my_name if uid == me else names.get(uid, uid), "text": text,
                    "thread_ts": str(m.get("thread_ts") or "")})
    label = "direct message" if channel.startswith("D") else ("group message" if channel.startswith("G") else "channel")
    return {"source": "slack", "channel": channel, "channel_label": label, "url": "", "messages": out, "fetched_at": store.now_iso()}


def _slack_names() -> dict[str, str]:
    """Slack user id -> display name, from the configured users file (the one the sync reads)."""
    path = config.load().slack_users
    try:
        names = json.loads(Path(path).read_text()) if path else {}
    except (OSError, ValueError):
        return {}
    return names if isinstance(names, dict) else {}


def of(c: dict[str, Any]) -> list[dict[str, Any]]:
    """What the user attached to this commitment: conversation, title, when."""
    return [a for a in c["detail"].get("attached") or [] if isinstance(a, dict) and a.get("conversation")]


def attach(b: Any, c: dict[str, Any], conv: str, title: str, why: str) -> None:
    """Attach a conversation to a task, undoably (``b`` is a project_book.Batch, untyped here since project_book imports sync, which imports this module). Fetch it first (``fetch``, before the batch
    opens: it writes on its own connection); one with no messages saved can't be attached."""
    if any(a["conversation"] == conv for a in of(c)):
        return
    if b.db.execute("SELECT 1 FROM messages WHERE conversation_id = ? LIMIT 1", (conv,)).fetchone() is None:
        raise AttachError(f"couldn't read {title or conv} just now")
    row = b.db.execute("SELECT title FROM conversations WHERE id = ?", (conv,)).fetchone()
    entry = {"conversation": conv, "title": title or (row[0] if row else conv), "at": store.now_iso()}
    b.change(c["id"], {"detail": dict(c["detail"], attached=[*of(c), entry])}, reason=why)
    commitments.link(b.db, c["id"], conv, "", ROLE)
    b.ops.append({"t": "sql", "sql": DETACH_SQL, "args": [c["id"], conv]})
    b.count("attached")


def detach(b: Any, c: dict[str, Any], conv: str, why: str) -> None:
    kept = [a for a in of(c) if a["conversation"] != conv]
    if len(kept) == len(of(c)):
        return
    # Taken off stays off: Today never attaches it again on its own (conversation_matching).
    detached = [*[x for x in c["detail"].get("detached") or [] if x != conv], conv]
    b.change(c["id"], {"detail": dict(c["detail"], attached=kept, detached=detached)}, reason=why)
    b.db.execute(DETACH_SQL, (c["id"], conv))
    b.ops.append({"t": "sql", "sql": ATTACH_SQL, "args": [c["id"], conv]})
    b.count("detached")


ATTACH_SQL = "INSERT OR IGNORE INTO commitment_links (commitment_id, conversation_id, message_id, role) VALUES (?, ?, '', 'attached')"


CHAT_NAME = re.compile(r"^\S+ (?:group|text) with ")


def for_card(c: dict[str, Any]) -> list[dict[str, str]]:
    """Each attached thread by its own name ("Launch team", not "WhatsApp group with ...")."""
    return [{"conversation": a["conversation"], "title": CHAT_NAME.sub("", a.get("title") or "") or a["conversation"],
             "kind": _kind(a["conversation"], a.get("title") or ""), "by": a.get("by") or "user", "why": a.get("why") or ""}
            for a in of(c)]


def open_ones(db: sqlite3.Connection) -> list[str]:
    """Conversations attached to open tasks."""
    return [r[0] for r in db.execute(
        "SELECT DISTINCT l.conversation_id FROM commitment_links l JOIN commitments k ON k.id = l.commitment_id "
        "WHERE l.role = ? AND k.stage = 'open'", (ROLE,))]


def refresh() -> int:
    """Keep attached email threads and Slack conversations fresh. Returns how many were
    fetched."""
    with conversations.opened(store.DATA_DIR) as db:
        convs = [c for c in open_ones(db) if not c.startswith("text:")]
    return sum(fetch(c) for c in convs)


def recent_lines(conv: str, n: int = 40) -> list[str]:
    """The newest messages of a conversation, oldest first, for an assistant to read."""
    with conversations.opened(store.DATA_DIR) as db:
        rows = db.execute(
            "SELECT at, sender, is_user, text FROM messages WHERE conversation_id = ? ORDER BY at DESC LIMIT ?", (conv, n),
        ).fetchall()
    return [f"[{datetime.fromtimestamp(r['at']).strftime('%b %d %H:%M')}] {config.name() if r['is_user'] else r['sender']}: "
            f"{(r['text'] or '')[:1500]}" for r in reversed(rows)]
