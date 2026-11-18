"""Everything one open task card shows, in one read: the conversation (their words, newest
last), the task's checklist, the reply or emails written, where Send goes, and what the
assistant has offered (act.py). Every list and every project page opens the same card.
"""

from datetime import date, datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from today import (
    act,
    actions,
    attached,
    commitments,
    config,
    connectors,
    ledger,
    page,
    project_book,
    store,
    task_context,
    work,
)
from today.project_book import Book

# The user, 2026-11-15: a few messages of lead-up, more room after (the ask progresses there).
THREAD_BEFORE = 12
THREAD_AFTER = 40
MESSAGE_CHARS = 4000


def _who(m: dict[str, Any]) -> tuple[str, bool]:
    me = bool(m.get("from_me") or m.get("is_me") or m.get("name") == config.name())
    name = "You" if me else str(m.get("name") or m.get("from") or m.get("user") or "")
    if "<" in name:
        name = name.split("<")[0].strip().strip('"') or name
    return name, me


def _when(m: dict[str, Any]) -> str:
    if m.get("ts"):
        try:
            return datetime.fromtimestamp(float(m["ts"])).strftime("%b %-d")
        except (TypeError, ValueError):
            pass
    try:
        return parsedate_to_datetime(str(m.get("date") or "")).strftime("%b %-d")
    except (TypeError, ValueError):
        return str(m.get("date") or "")[:16]


def _bounce(m: dict[str, Any]) -> bool:
    """A mail server's delivery report (MAILER-DAEMON, postmaster): not part of the conversation."""
    sender = parseaddr(str(m.get("from") or ""))[1].lower()
    return sender.split("@")[0] in ("mailer-daemon", "postmaster")


def thread(key: str, before: int = THREAD_BEFORE, after: int = THREAD_AFTER) -> dict[str, Any]:
    """The conversation word for word, oldest first, centred on the message the task came from
    (`anchor`; the newest when none is found), and how many came before the first one kept.
    A Slack channel holds many threads: the card shows the one the task's message is in. When
    that message starts a thread, the channel's lead-up comes before it and its replies after."""
    raw = store.task_raw(key)
    source = store.source_message(key)
    groups = [g["messages"] for g in work.threads(raw, source)] if raw.get("source") == "slack" and raw.get("messages") else [raw.get("messages") or []]
    kept: list[list[tuple[dict[str, Any], str]]] = []
    for group in groups:
        rows = []
        for m in group:
            # Delivery reports are left out (the user, 2026-11-16: they're noise on the card).
            if m.get("draft") or _bounce(m):
                continue
            body = m.get("text") or ""
            # An email's quoted history is the earlier messages, already shown above it.
            text = page.clean_text(connectors.strip_quoted(body) if raw.get("source") == "email" or "from" in m else body)
            if text:
                rows.append((m, text))
        kept.append(rows)
    # The message recorded as the task's origin, in whichever thread it is; the newest when none is.
    rows, at = next(((g, n) for g in kept for n, (m, _) in enumerate(g) if store.is_source(m, source)), (kept[0], None))
    if not rows:
        return {"messages": [], "anchor": -1, "earlier": 0}
    msgs = [_shown(m, text) for m, text in rows]
    at = len(msgs) - 1 if at is None else at
    start = rows[at][0]
    if start.get("thread_ts") and float(start["thread_ts"]) == float(start.get("ts") or 0):
        # The ask starts a thread: what was said in the channel just before it, then the replies.
        lead = [_shown(m, text) for g in kept if g and not g[0][0].get("thread_ts")
                for m, text in g if float(m.get("ts") or 0) < float(start["ts"])]
        for m in msgs[at + 1:]:
            m["reply"] = True
        msgs, at = lead + msgs[at:], len(lead)
    lo = max(0, at - before)
    return {"messages": msgs[lo:at + after + 1], "anchor": at - lo, "earlier": lo}


def _shown(m: dict[str, Any], text: str) -> dict[str, Any]:
    who, me = _who(m)
    return {"from": who, "me": me, "at": _when(m), "text": text[:MESSAGE_CHARS]}


def service(item: dict[str, Any]) -> str:
    """Which app the conversation lives in, for its icon: Slack, Gmail, Messages, or the chat app's name."""
    src = item.get("source")
    if src == "slack":
        return "Slack"
    if src == "email":
        return "Gmail"
    if src == "text":
        plat = page.platform_of(item) or ""
        return "Messages" if plat in ("", "iMessage", "SMS") else plat
    return ""


def _task_platform(key: str) -> str:
    """The chat app the user's task says the message came from, for a card copy made without it."""
    with project_book.opened() as db:
        c = commitments.by_legacy_key(db, key) or (commitments.get(db, key) if ledger.is_commitment_key(key) else None)
        return str(c["detail"].get("platform") or "") if c else ""


def steps_of(key: str) -> dict[str, Any]:
    """The task's checklist, with the commitment id the step buttons write to."""
    with project_book.opened() as db:
        # A task the judge found is its own commitment id (and its item, once made, has it as key).
        c = commitments.by_legacy_key(db, key) or (commitments.get(db, key) if ledger.is_commitment_key(key) else None)
        if c is None:
            return {"task": "", "steps": [], "attached": [], "notes": []}
        return {"task": c["id"], "attached": attached.for_card(c), "notes": task_context.for_card(db, c["id"]), "steps": [{"cid": s["id"], "title": s["title"], "done": s["stage"] != "open",
                                            "note": s["detail"].get("note", ""), "url": s["detail"].get("url", "")}
                                           for s in Book(db).steps(c["id"])]}


def _people(value: Any) -> str:
    """An email's To or Cc as one line: names as written, or "Name <address>" when the reader looked the address up."""
    people = value if isinstance(value, list) else [value] if value else []
    out = []
    for p in people:
        if isinstance(p, dict):
            name, address = str(p.get("name") or ""), str(p.get("email") or "")
            out.append(f"{name} <{address}>" if name and address else name or address)
        else:
            out.append(str(p))
    return ", ".join(x for x in out if x)


def card(key: str) -> dict[str, Any] | None:
    if ledger.is_commitment_key(key):
        ledger.ensure_item(key)
    item = store.load()["items"].get(key)
    if item is None:
        return None
    steps = steps_of(key)
    records = task_context.card_records_now(key)
    focus_views = {s["cid"]: act.view(records, s["cid"]) for s in steps["steps"]}
    try:
        target = actions.send_target(item) if item.get("source") in ("slack", "email", "text") else {}
    except (KeyError, TypeError):  # an item saved without where it came from can't be sent from here
        target = {}
    return {
        "key": key,
        "title": item.get("title") or "",
        "who": item.get("who") or "",
        "state": item.get("state") or "",
        "status": item.get("status") or "",
        "service": service(item if item.get("platform") or item.get("source") != "text" else {**item, "platform": _task_platform(key)}),
        "url": item.get("url") or "",
        "thread": thread(key),
        "ask_text": item.get("ask_text") or "",
        "manual": bool(item.get("manual")),
        "draft": item.get("draft") or "",
        "nudge": item.get("nudge") or "",
        "draft_to": item.get("draft_to") or "",
        # A draft meant for someone other than this conversation ("Iris (DM)"): not an email.
        "draft_for": str(item.get("to") or "") if item.get("elsewhere") and not item.get("manual") else "",
        "draft_subject": item.get("draft_subject") or "",
        "emails": [{"to": _people(e.get("to")), "cc": _people(e.get("cc")),
                    "subject": e.get("subject") or "", "body": e.get("body") or "", "sent": bool(e.get("sent"))}
                   for e in item.get("emails") or []],
        "can_send": bool(target.get("can_send")),
        "send_to": target.get("label", "") if target.get("can_send") else "",
        "error": item.get("error") or "",
        "user_draft": actions.user_draft(item) if item.get("status") in ("open", "working") else None,
        "calendar": [{"i": n, "title": e.get("title", ""), "start": e.get("start", ""), "added": bool(e.get("added")),
                      "link": e.get("link", "")} for n, e in enumerate(item.get("calendar") or [])],
        "finished": item.get("cleared_reason") or item.get("status", "") if item.get("status") not in ("open", "working") else "",
        "task": steps["task"],
        "steps": steps["steps"],
        "attached": steps["attached"],
        "notes": steps["notes"],
        "act": act.view(records),
        "step_act": focus_views,
    }


def remove_note(db: Any, nid: str) -> dict[str, Any]:
    """The user crossed out a note on the card: it is gone from every AI's picture of the task, and
    never written again. One undoable batch (the 10-second undo)."""
    task_context.setup(db)
    row = db.execute("SELECT text FROM task_notes WHERE id = ?", (nid,)).fetchone()
    if row is None or not task_context.hide_note(db, nid, "user"):
        raise project_book.ProjectError("That note isn't there any more.")
    b = project_book.Batch(db, "user", f"crossed out a note: {row[0][:60]}")
    b.ops.append({"t": "sql", "sql": task_context.SHOW_SQL, "args": [nid]})
    return {"batch": b.save()}


def set_due(db: Any, key: str, due: str) -> dict[str, Any]:
    """The user set or cleared a task's date in the picker (the user, 2026-11-15). The date is the user's
    from then on: the refresh doesn't move it. One undoable batch (the 10-second undo)."""
    due = (due or "").strip()
    if due:
        try:
            due = date.fromisoformat(due).isoformat()
        except ValueError:
            raise project_book.ProjectError("That isn't a date.") from None
    key = key.removeprefix("c:")
    c = task_context.task_of(db, key) or project_book.ensure_commitment(db, key)
    b = project_book.Batch(db, "user", f"set the date to {due}" if due else "cleared the date")
    b.change(c["id"], {"due": due, "detail": dict(c["detail"], due_set_by="user")},
             reason=f"the user set the date: {due}" if due else "the user cleared the date")
    return {"batch": b.save(), "due": due}


def detach_thread(db: Any, key: str, conv: str) -> dict[str, Any]:
    """The user took a thread off the task from the card (one the user attached, or one Today found):
    nothing reads it with the task any more, and Today never attaches it again. Undoable."""
    c = task_context.task_of(db, key)
    if c is None or not any(a["conversation"] == conv for a in attached.of(c)):
        raise project_book.ProjectError("That conversation isn't on this task any more.")
    b = project_book.Batch(db, "user", "took a conversation off the task")
    attached.detach(b, c, conv, "the user took it off the card")
    return {"batch": b.save()}
