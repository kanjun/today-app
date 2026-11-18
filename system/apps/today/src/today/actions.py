"""What the buttons do: today list, send, done, answers, bring back."""

import contextvars
import html as htmllib
import itertools
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from email.utils import getaddresses
from typing import Any

from today import (
    calendar_events,
    commitments,
    config,
    connectors,
    conversations,
    ledger,
    mail_store,
    page,
    planning,
    project_book,
    projects,
    store,
    weekly,
)

log = logging.getLogger(__name__)

VISIBLE = ("open", "working")


class SendError(ValueError):
    """Why a message could not be sent, in words for the user."""


def _age(asked_at: str) -> str:
    try:
        delta = datetime.now() - datetime.fromisoformat(asked_at)
    except (TypeError, ValueError):
        return ""
    hours = delta.total_seconds() / 3600
    return f"{int(hours // 24)}d" if hours >= 24 else f"{max(int(hours), 0)}h"


URGENT_DAYS = 2


def due_days(due: str) -> int | None:
    """Days until a due date (negative when past), or None when there is none."""
    try:
        return (datetime.fromisoformat(due).date() - datetime.now().date()).days
    except (TypeError, ValueError):
        return None


def due_label(days: int | None) -> str:
    """The badge for something due within two days; nothing for later dates."""
    if days is None or days > URGENT_DAYS:
        return ""
    if days < 0:
        return "Overdue"
    return ("Due today", "Due tomorrow", "Due in 2 days")[days]


def _urgency(days: int | None) -> int:
    """Sort order: due within two days first, then undated, then later dates at the bottom."""
    if days is None:
        return 50
    return days if days <= URGENT_DAYS else 100 + days


def due_on(days: int | None) -> str:
    """A quiet date for something due later than two days out, like "Due Nov 6"."""
    if days is None or days <= URGENT_DAYS:
        return ""
    return "Due " + (datetime.now() + timedelta(days=days)).strftime("%b %-d")


def summary(item: dict[str, Any]) -> dict[str, Any]:
    days = due_days(item.get("due") or "")
    return {
        "due": item.get("due") or "",
        "due_days": days,
        "due_label": due_label(days),
        "due_on": due_on(days),
        "key": item["key"],
        "who": item.get("who") or "",
        "title": item.get("title") or item.get("ask_text", "")[:80],
        "state": "working"
        if item["status"] == "working"
        else (item.get("state") or "mine"),
        "age": _age(item.get("asked_at", "")),
        "source": item.get("source"),
        "draft": (item.get("draft") or "")[:240]
        if item.get("state") == "ready"
        else "",
        "can_send": item.get("state") == "ready"
        and not item.get("elsewhere")
        and item.get("source") in ("slack", "email"),
        "group": action_group(item),
        "platform": platform_of(item),
        "who_full": item.get("who_full") or "",
        # Came back from a set-aside: the day it returned, so the row can say so.
        "back_from": later_until(item)
        if item.get("later") and not is_set_aside(item)
        else "",
        "until": later_until(item),
        "next_step": (item.get("next_step") or {}).get("text", ""),
    }


# The main list groups what the user owes by the kind of action it takes, so the user can do
# all the signing at once, then all the scheduling, and so on. Plain word rules on
# the title (which always starts with the action), checked in this order; the
# first that matches wins.
ACTION_GROUPS: tuple[tuple[str, str], ...] = (
    ("Sign", r"\b(sign|docusign|countersign|e-?sign)\b"),
    (
        "Schedule",
        r"\b(meet|meeting|call|coffee|lunch|dinner|book|schedule|reschedule|calendar|"
        r"when you can|time to|availability|available|retreat|trip|plans)\b",
    ),
    ("Post or share", r"^(post|share|tweet|publish|forward|send .*(notes|deck|link))\b"),
    ("Read or try", r"^(read|review|try|look|watch|listen|check out|test)\b"),
    (
        "Reply or decide",
        r"^(reply|respond|answer|tell|decide|confirm|let|say|nudge|ask|follow up|get back|email|text|message|thank)\b",
    ),
)
OTHER_GROUP = "Other"


# Message requests on these platforms (people the user has never written back to) are mostly
# strangers and rarely urgent: their own group, last. Chats the user has replied in stay normal.
LOW_GROUP = "Low priority"
LOW_PLATFORMS = ("X (Twitter)",)


platform_of = page.platform_of


def action_group(item: dict[str, Any]) -> str:
    if item.get("request") and platform_of(item) in LOW_PLATFORMS:
        return LOW_GROUP
    title = str(item.get("title") or item.get("ask_text") or "").strip().lower()
    for name, pattern in ACTION_GROUPS:
        if re.search(pattern, title):
            return name
    return OTHER_GROUP


def later_until(item: dict[str, Any]) -> str:
    """The day a set-aside item comes back to the list ("" when it is not set aside)."""
    if not item.get("later"):
        return ""
    if item.get("later_until"):
        return str(item["later_until"])
    # Set aside before return days existed: back the day after.
    try:
        return (datetime.fromisoformat(item["later"]).date() + timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        return ""


def is_set_aside(item: dict[str, Any]) -> bool:
    """Set aside and its return day has not come yet."""
    until = later_until(item)
    return bool(until) and until > datetime.now().date().isoformat()


ASKED_DAYS = 3


def is_asked(item: dict[str, Any]) -> bool:
    """A draft the user asked for in the last few days, still being written or waiting on the user."""
    at = str(item.get("asked_at_draft") or "")
    if not at or item.get("no") or item.get("state") == "waiting":
        return False
    try:
        age = datetime.now() - datetime.fromisoformat(at[:19])
    except ValueError:
        return False
    return age.days < ASKED_DAYS


def view_state() -> dict[str, Any]:
    """The page: Now, the user's lists, the This-week box and the plan, all read from the tasks."""

    def roll(state: dict[str, Any]) -> dict[str, Any]:
        store.roll_today(state, datetime.now().date().isoformat())
        return state

    state = store.load()
    if state["today"].get("date") != datetime.now().date().isoformat():
        state = store.mutate(roll)
    main_page = _main_page(state)
    sources = config.sources()
    return {
        "date": datetime.now().strftime("%A, %b %-d"),
        "last_sync": state.get("last_sync"),
        "sync_error": state.get("sync_error"),
        # The inputs and whether each is there, so an empty list can say what to connect.
        "sources": sources,
        "has_source": any(s["found"] for s in sources),
        "settings_path": str(config.settings_path()),
        "rules": [r["text"] for r in state.get("draft_rules", [])],
        "page": main_page,
        "plan": planning.summary(),
        "projects": [{"title": t} for t in main_page.pop("project_titles", [])],
    }


def _main_page(state: dict[str, Any]) -> dict[str, Any]:
    """Now, quick answers, replies, to-dos, Waiting on, Slipped and the This-week box.

    Now chooses from the tasks themselves (ledger.now_pool): the user's picks for today that are
    still open tasks, the user's weekly goals' next moves, and the tasks on the user's working lists."""
    items = state["items"]
    pool = ledger.now_pool(state)
    tasks, working = pool if pool is not None else ({}, set())
    picks = [tasks[k] for k in state["today"]["order"] if k in tasks]
    week = weekly.view(state)
    if week is not None and week["needs_plan"] and not week["error"]:
        weekly.plan_soon()  # a goal with no next move yet gets one in the background
    goal_picks = [
        items[k]
        for k in state["today"]["order"]
        if items.get(k, {}).get("source") == "goal" and items[k]["status"] in VISIBLE
    ]
    built = page.build(
        [],
        [tasks[k] for k in working],
        goal_picks + picks,
        items,
        goal_moves=week["moves"] if week is not None else None,
        goal_of=week["related"] if week is not None else None,
        out_of_now=set(state["today"].get("out", [])),
    )
    if week is not None:
        in_now = {r["key"] for r in built["now"]}
        moved = {m.get("weekly_goal") for m in week["moves"] if m["key"] in in_now}
        week = {k: v for k, v in week.items() if k not in ("moves", "related", "needs_plan")}
        # Every goal is one click from its next move: in Now, or shown in place.
        week["goals"] = [
            dict(
                g,
                in_now=g["id"] in moved,
                move=page.row(g["move"], sendable=page.can_send(g["move"]), goal=g["text"])
                if g.get("move")
                else None,
            )
            for g in week["goals"]
        ]
    built = dict(built, week=week)
    built = ledger.sections(built, state) or built
    return _with_projects(built, list(tasks.values()))


def _with_projects(built: dict[str, Any], shown: list[dict[str, Any]]) -> dict[str, Any]:
    """Each row names the project of the user's it is in (from commitments: an old goal is not a
    project of the user's, so its tasks are named by the project above it, if any), with that
    project's id so the name opens it; and, when filing is not automatic, the one Today
    guesses it is part of. Also counts the user's open projects for the Projects link, and names
    them for the Someday outline's links."""
    try:
        with project_book.opened() as db:
            project_book.auto_file_pending(db, shown)
            labels = project_book.main_list_labels(db)
            progress = project_book.step_progress(db)
            guesses = project_book.filing_suggestions(db, shown) if projects.file_or_suggest() == "suggest" else {}
            open_projects = [p for p in project_book.Book(db).projects() if p["stage"] == "open"]
    except (sqlite3.Error, OSError, project_book.ProjectError) as e:
        log.warning("project names for the list: %s", e)
        labels, guesses, open_projects, progress = {}, {}, [], {}

    def tag(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(r, project=labels.get(r["key"]), guess=guesses.get(r["key"]), steps_done=progress.get(r["key"], (0, 0))[0],
                     step_total=progress.get(r["key"], (0, 0))[1]) for r in rows]

    out = {k: tag(v) if isinstance(v, list) else v for k, v in built.items()}
    out["filed"] = {k: tag(v) for k, v in built["filed"].items()}
    out["projects_open"] = len(open_projects)
    out["project_titles"] = [p["title"] for p in open_projects]
    return out


STATUS_WORDS = {
    "open": "open",
    "working": "open",
    "new": "open",
    "error": "open",
    "sent": "sent",
    "done": "done",
    "cleared": "cleared",
    "skipped": "dropped",
}


def search(q: str, limit: int = 60) -> dict[str, Any]:
    """Every task, open or finished, whose words (or conversation) hold all the query's words."""
    terms = [t for t in q.lower().split() if t]
    if not terms:
        return {"items": []}
    state = store.load()

    def hit(text: str) -> bool:
        text = text.lower()
        return all(t in text for t in terms)

    out = []
    truth = ledger.by_item()
    for i in state["items"].values():
        fields = " ".join(
            str(i.get(k) or "")
            for k in (
                "who",
                "title",
                "ask_text",
                "draft",
                "did",
                "to",
                "cleared_reason",
                "sent_text",
            )
        )
        direct = hit(fields)
        if not direct:
            raw = store.load_raw(i["key"]) or {}
            convo = " ".join(
                f"{m.get('name') or m.get('from') or ''} {m.get('text') or ''}"
                for m in raw.get("messages", [])
            )
            if not hit(
                f"{fields} {raw.get('channel_label', '')} {raw.get('subject', '')} {convo}"
            ):
                continue
        word = STATUS_WORDS.get(i["status"], i["status"])
        reason = i.get("cleared_reason", "")
        c = truth.get(i["key"])
        if c:  # the commitment says what's true now
            word = {"open": "open", "parked": "open", "done": "done", "declined": "dropped", "stale": "cleared"}[c["stage"]]
            reason = c["stage_reason"] or reason
        if c and c["stage_reason"].startswith("same as"):
            continue
        out.append(
            dict(
                summary(i),
                title=c["title"] if c else i.get("title", ""),
                status=word,
                reason=reason if word != "open" else "",
                when=i.get("asked_at", ""),
                only_in_conversation=not direct,
            )
        )
    for key, c in truth.items():
        if key in state["items"] or not hit(f"{c['title']} {c['counterparty']} {c['stage_reason']}"):
            continue
        word = {"open": "open", "parked": "open", "done": "done", "declined": "dropped", "stale": "cleared"}[c["stage"]]
        out.append({"key": key, "who": c["counterparty"], "title": c["title"], "status": word,
                    "reason": c["stage_reason"] if word != "open" else "", "when": c["created_at"],
                    "only_in_conversation": False, "platform": ""})
    # Open before finished; within each, items about it before ones that only mention it, newest first.
    out.sort(key=lambda r: r["when"], reverse=True)
    out.sort(key=lambda r: (r["status"] != "open", r["only_in_conversation"]))
    return {"items": out[:limit]}


def _correction(kind: str, **fields: Any) -> None:
    store.mutate(
        lambda s: s.setdefault("corrections", []).append(
            dict(fields, kind=kind, at=store.now_iso())
        )
    )


def rename_item(key: str, title: str) -> None:
    """The user's own title for a task; later reads keep it."""
    old = store.load()["items"][key].get("title")
    store.mutate(
        lambda s: s["items"][key].update(title=title, title_by_hand=True)
    )
    _correction("retitle", item=key, title=title, was=old)


def send_email(key: str, index: int, to: str, cc: str, subject: str, text: str) -> str:
    item = store.load()["items"][key]
    if not to.strip() or "@" not in to:
        raise SendError("Add an email address in To first.")
    if not text.strip():
        raise SendError("The message is empty.")
    account = _account(item)
    thread = in_thread(item, to, cc, subject)
    if thread:
        connectors.gmail_send_reply(
            account,
            item["ref"]["thread_id"],
            to,
            cc,
            subject,
            thread["in_reply_to"],
            thread["references"],
            text,
        )
    else:
        earlier = thread_with(to)
        if earlier:
            connectors.gmail_send_reply(earlier["account"], earlier["thread_id"], to, cc, earlier["subject"] or subject,
                                        earlier["in_reply_to"], earlier["references"], text)
        else:
            connectors.gmail_send_new(account, to, cc, subject, text)

    def mark(s: dict[str, Any]) -> bool:
        if index == -1:
            # The task's own draft, sent as a new email (a to-do of the user's has no thread to answer):
            # sent is done, as a reply is.
            s["items"][key].update(sent_to=to, sent_text=text, sent_at=store.now_iso())
            return True
        emails = s["items"][key].get("emails") or []
        if 0 <= index < len(emails):
            emails[index].update(
                sent=True, sent_to=to, sent_text=text, sent_at=store.now_iso()
            )
        return bool(emails) and all(e.get("sent") for e in emails)

    archived = (
        _finish(key, "sent", f"Sent from Today: {subject}")
        if store.mutate(mark)
        else ""
    )
    return f"Sent to {to.split('<')[0].strip() or to}" + archived


def save_gmail_draft(
    key: str, index: int | None, to: str, cc: str, subject: str, text: str
) -> dict[str, str]:
    """Put this item's reply (index None) or one of its emails in the user's Gmail drafts.
    Nothing is sent and the item stays open; pressing again updates the same draft."""
    item = store.load()["items"][key]
    if not text.strip():
        raise SendError("The message is empty.")
    account = _account(item)
    if index is None:
        target = send_target(item)
        if item["source"] != "email" or not target.get("can_send"):
            raise SendError("This one isn't an email reply.")
        to, cc, subject = target["to"], target["cc"], target["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        thread = dict(target, thread_id=item["ref"]["thread_id"])
    else:
        # No address needed to keep a draft; Send asks for one (the user, Nov 15).
        found = in_thread(item, to, cc, subject) if to.strip() else {}
        thread = dict(found, thread_id=item["ref"]["thread_id"]) if found else {}
    slot = "reply" if index is None else "draft" if index == -1 else f"email:{index}"
    saved = (item.get("gmail_drafts") or {}).get(slot) or {}
    data = connectors.gmail_save_draft(
        account,
        thread.get("thread_id", ""),
        to,
        cc,
        subject,
        thread.get("in_reply_to", ""),
        thread.get("references", ""),
        text,
        saved.get("draft_id", "") if saved.get("account") == account else "",
    )
    thread_id = (data.get("message") or {}).get("threadId", "")
    record = {
        "draft_id": str(data.get("id", "")),
        "account": account,
        "thread_id": thread_id,
        "at": store.now_iso(),
        "url": f"https://mail.google.com/mail/u/{account}/#drafts",
    }

    def change(s: dict[str, Any]) -> None:
        s["items"][key].setdefault("gmail_drafts", {})[slot] = record
        if index == -1:
            # The user's edits are the draft now: the card shows what went to Gmail, on any device.
            s["items"][key]["draft"] = text

    store.mutate(change)
    return {
        "message": "Updated in your Gmail drafts"
        if saved
        else "Saved to your Gmail drafts",
        "url": record["url"],
    }


def _account(item: dict[str, Any]) -> str:
    """The inbox an email for this item goes out from: the item's own inbox for an
    email item, else the first configured account."""
    account = (item.get("account") if item.get("source") == "email" else "") or (
        config.default_account()
    )
    if not account:
        raise SendError("No email account is set up for Today yet (see its settings).")
    return account


def user_draft(item: dict[str, Any]) -> dict[str, Any] | None:
    """The user's own unsent Gmail draft when it is the newest thing in the item's
    thread: that is their next step, so the item leads with it."""
    if item.get("source") != "email":
        return None
    msgs = (store.load_raw(item["key"]) or {}).get("messages", [])
    if not msgs or not msgs[-1].get("draft"):
        return None
    m = msgs[-1]
    return {
        "message_id": m.get("id", ""),
        "to": m.get("to", ""),
        "cc": m.get("cc", ""),
        "date": m.get("date", ""),
        "text": _tidy(m.get("text", "")),
    }


def _tidy(text: str) -> str:
    """A draft written in Gmail's editor read as plain text: no HTML leftovers or
    runs of blank lines."""
    text = htmllib.unescape(text).replace("\xa0", " ")
    lines = [line.strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _her_draft_id(item: dict[str, Any], message_id: str, text: str) -> str:
    """The user's draft's id, with its text replaced first when the user edited it here."""
    found = user_draft(item)
    if not found or found["message_id"] != message_id:
        raise SendError("That draft changed in Gmail. Reload and try again.")
    account = _account(item)
    draft_id = connectors.gmail_find_draft(account, message_id)
    if text.strip() != found["text"].strip():
        new = connectors.gmail_rewrite_draft(account, draft_id, text)
        draft_id = str(new.get("id") or draft_id)
        new_msg = str((new.get("message") or {}).get("id") or message_id)
        # Gmail gives an edited draft a new message id; keep the saved thread in step.
        raw = store.load_raw(item["key"]) or {}
        for m in raw.get("messages", []):
            if m.get("id") == message_id:
                m.update(id=new_msg, text=text)
        store.save_raw(item["key"], raw)
    return draft_id


def save_user_draft(key: str, message_id: str, text: str) -> str:
    """Keep the user's edits in the user's Gmail draft; nothing is sent."""
    item = store.load()["items"][key]
    if not text.strip():
        raise SendError("The draft is empty.")
    _her_draft_id(item, message_id, text)
    return "Your Gmail draft is updated"


def user_draft_now(key: str) -> str:
    """The user's Gmail draft's message id after an edit (Gmail gives each edit a new one), so the
    next save as the user types names the right message."""
    found = user_draft(store.load()["items"][key])
    return found["message_id"] if found else ""


def send_user_draft(key: str, message_id: str, text: str) -> str:
    """Send the user's own Gmail draft (with any edits made here), on the user's tap."""
    item = store.load()["items"][key]
    if not text.strip():
        raise SendError("The draft is empty.")
    draft_id = _her_draft_id(item, message_id, text)
    connectors.gmail_send_draft(_account(item), draft_id)
    store.mutate(
        lambda s: s["items"][key].update(sent_text=text, sent_at=store.now_iso())
    )
    archived = _finish(key, "sent", f"Sent your draft from Today: {text}")
    return f"Sent to {item.get('who') or 'them'}" + archived


THREAD_LOOK_BACK_DAYS = 730


def thread_with(to: str) -> dict[str, str]:
    """The user's newest email thread with the one person this goes to, as reply headers, or {}.
    The user, 2026-11-14: always send on the existing thread. An email to several people
    (an introduction) is a new email."""
    people = [a.lower() for _, a in getaddresses([to]) if a and "@" in a]
    if len(people) != 1:
        return {}
    since = datetime.now() - timedelta(days=THREAD_LOOK_BACK_DAYS)
    best: tuple[int, str, str] | None = None
    for account in config.accounts():
        ok, latest = mail_store.latest_with(account, people[0], since)
        if ok and latest and latest.get("threadId"):
            at = int(latest.get("internalDate") or 0)
            if best is None or at > best[0]:
                best = (at, account, latest["threadId"])
    if best is None:
        return {}
    _, account, thread_id = best
    thread = mail_store.thread(thread_id, account) or {}
    msgs = thread.get("messages") or []
    if not msgs:
        return {}
    heads = {h["name"].lower(): h["value"] for h in (msgs[-1].get("payload") or {}).get("headers") or []}
    first = {h["name"].lower(): h["value"] for h in (msgs[0].get("payload") or {}).get("headers") or []}
    return {"account": account, "thread_id": thread_id, "subject": first.get("subject", ""),
            "in_reply_to": heads.get("message-id", ""), "references": heads.get("references", "")}


def _bare_subject(subject: str) -> str:
    return re.sub(r"^((re|fwd?):\s*)+", "", subject.strip(), flags=re.I).lower()


def in_thread(item: dict[str, Any], to: str, cc: str, subject: str) -> dict[str, Any]:
    """An email written for an email item goes on that item's thread when it is to the
    same people or about the same subject (an intro to someone new is a new email)."""
    if item.get("source") != "email" or not (item.get("ref") or {}).get("thread_id"):
        return {}
    target = send_target(dict(item, elsewhere=False))
    if not target.get("can_send"):
        return {}
    raw = store.load_raw(item["key"]) or {}
    people = {
        a.lower()
        for m in raw.get("messages", [])
        for _, a in getaddresses([m.get("from", ""), m.get("to", ""), m.get("cc", "")])
        if a
    } - config.my_emails()
    mine = {a.lower() for _, a in getaddresses([to, cc]) if a}
    same_subject = _bare_subject(subject) == _bare_subject(target.get("subject", ""))
    return target if same_subject or (mine & people) else {}


def send_target(item: dict[str, Any]) -> dict[str, Any]:
    """Where Send would post, spelled out for the user before they press it."""
    if item.get("source") == "goal":
        return weekly.target(item)
    if item.get("elsewhere") or item.get("manual"):
        return {"can_send": False}
    raw = store.load_raw(item["key"]) or {}
    if item["source"] == "slack":
        ref = item["ref"]
        # Always a reply in the thread of the message that asked, DMs included.
        thread = ref.get("thread_ts") or ref["ts"]
        where = "as a reply in the thread of the original message"
        return {
            "can_send": True,
            "label": f"Send to {item.get('who') or 'them'}",
            "detail": f"Posts as you {where}.",
            "channel": ref["channel"],
            "thread_ts": thread,
        }
    inbound = [m for m in raw.get("messages", []) if not m.get("from_me")]
    if not inbound:
        return {"can_send": False}
    last = inbound[-1]
    sender = getaddresses([last["from"]])
    sender_addr = sender[0][1].lower() if sender else ""
    others = [
        f"{n} <{a}>" if n else a
        for n, a in getaddresses([last.get("to", ""), last.get("cc", "")])
        if a and a.lower() not in config.my_emails() and a.lower() != sender_addr
    ]
    return {
        "can_send": True,
        "label": f"Send to {item.get('who') or 'them'}",
        "detail": f"Replies by email to {last['from']}"
        + (f", cc {', '.join(others)}" if others else "")
        + f", from {item['account']}.",
        "to": last["from"],
        "cc": ", ".join(others),
        "subject": last.get("subject", ""),
        "in_reply_to": last.get("message_id", ""),
        "references": last.get("references", ""),
    }


def item_detail(key: str) -> dict[str, Any] | None:
    if ledger.is_commitment_key(key):
        try:
            ledger.ensure_item(key)  # a task the judge found: its page needs an item behind it
        except commitments.CommitmentError:
            return None
    state = store.load()
    item = state["items"].get(key)
    if item is None:
        return None
    item = dict(
        item,
        platform=platform_of(item),
        emails=[
            dict(
                e,
                in_thread=bool(
                    in_thread(
                        item,
                        ", ".join(p.get("email", "") for p in e.get("to", [])),
                        ", ".join(p.get("email", "") for p in e.get("cc", [])),
                        e.get("subject", ""),
                    )
                ),
            )
            for e in item.get("emails") or []
        ],
    )
    return dict(
        item,
        age=_age(str(item.get("asked_at") or "")),
        target=send_target(item),
        user_draft=user_draft(item),
        in_today=key in state["today"]["order"],
        due_days=due_days(str(item.get("due") or "")),
        due_label=due_label(due_days(str(item.get("due") or ""))),
    )


def set_today(key: str, on: bool) -> None:
    def change(state: dict[str, Any]) -> None:
        order = state["today"]["order"]
        if on and key in state["items"]:
            state["items"][key].pop("later", None)
        if on and key not in order:
            order.append(key)
        if not on and key in order:
            order.remove(key)

    store.mutate(change)


def set_later(key: str, on: bool, until: str = "") -> None:
    """Set a task aside until a day (tomorrow by default), or bring it back now."""
    day = until or (datetime.now().date() + timedelta(days=1)).isoformat()

    def change(state: dict[str, Any]) -> None:
        item = state["items"][key]
        if on:
            item["later"] = store.now_iso()
            item["later_until"] = day
            if key in state["today"]["order"]:
                state["today"]["order"].remove(key)
        else:
            item.pop("later", None)
            item.pop("later_until", None)

    store.mutate(change)


def next_monday() -> str:
    today = datetime.now().date()
    return (today + timedelta(days=7 - today.weekday())).isoformat()


PLAN_CHOICES = frozenset({"today", "tomorrow", "nextweek", "notmine", "drop"})


def plan(key: str, choice: str, until: str = "") -> str:
    """The user's one decision on an item: today, tomorrow, next week (or a day), not mine, or drop."""
    if choice == "today":
        set_today(key, True)
        return "On today's list"
    if choice == "tomorrow":
        set_later(key, True)
        return "Back tomorrow"
    if choice == "nextweek":
        day = until or next_monday()
        set_later(key, True, day)
        return "Back " + datetime.fromisoformat(day).strftime("%a %b %-d")
    if choice == "notmine":
        return not_mine(key)
    if choice == "drop":
        return ignore(key)
    raise SendError("That isn't one of the choices.")


def not_mine(key: str) -> str:
    """Not the user's to do. Closed with nothing sent, and remembered so later reads learn from it."""
    item = store.load()["items"][key]
    _correction(
        "not_mine",
        key=key,
        who=item.get("who", ""),
        title=item.get("title", ""),
        ask_text=str(item.get("ask_text", ""))[:400],
        captured_kind=item.get("kind", ""),
    )
    store.mutate(
        lambda s: s["items"][key].update(
            cleared_reason="you said this isn't yours to do; nothing sent",
            cleared_at=store.now_iso(),
        )
    )
    return "Marked not yours" + _finish(key, "skipped", "Marked not the user's in Today; nothing sent.")


_proposing: set[str] = set()
_proposing_lock = threading.Lock()


def set_order(keys: list[str]) -> None:
    store.mutate(
        lambda s: s["today"].update(
            order=[
                k
                for k in keys
                if k.startswith("project:")
                or k in s["items"]
                or k[5:] in s.get("goals", {})
            ]
        )
    )


def add_manual(text: str, pick: bool = True, origin: dict[str, str] | None = None) -> str:
    """A to-do of the user's own. ``pick`` puts it on Today's own list too (the Add box does);
    an Inbox capture goes only to "Yours to do". ``origin`` says where it came from when
    another app filed it (a line from the user's notes app: ``{"from": "<app>", "text": <the user's line>}``)."""
    key = f"manual:{datetime.now().timestamp():.0f}"
    while key in store.load()["items"]:
        key += "x"

    def change(state: dict[str, Any]) -> None:
        state["items"][key] = {
            "key": key,
            "source": "manual",
            "manual": True,
            "who": "",
            "title": text,
            "ask_text": text,
            "state": "mine",
            "status": "open",
            "asked_at": store.now_iso(),
            "did": "",
            "draft": "",
            "questions": [],
            "created": store.now_iso(),
        }
        if origin:
            state["items"][key]["origin"] = origin
        if pick:
            state["today"]["order"].append(key)

    store.mutate(change)
    return key


def _finish(key: str, status: str, note: str) -> str:
    """Close an item the user finished. Returns " · archived" when its email left the user's inbox."""

    def change(state: dict[str, Any]) -> dict[str, Any]:
        item = state["items"][key]
        item.update(
            status=status,
            finished_at=store.now_iso(),
            was_in_today=key in state["today"]["order"],
        )
        if key in state["today"]["order"]:
            state["today"]["order"].remove(key)
        return item

    item = store.mutate(change)
    if item.get("source") == "goal" and status in ("done", "sent"):
        weekly.move_done(key)
    return _archive(key)


def _thread(item: dict[str, Any]) -> tuple[str, str] | None:
    thread_id = (item.get("ref") or {}).get("thread_id")
    if item.get("source") != "email" or not thread_id:
        return None
    return item.get("account") or config.default_account(), thread_id


def _archive(key: str) -> str:
    """Finishing an email item archives its thread, unless something else from the
    same thread is still owed."""
    state = store.load()
    item = state["items"][key]
    thread = _thread(item)
    if not thread:
        return ""
    if any(
        _thread(other) == thread and other["status"] not in store.FINISHED
        for k, other in state["items"].items()
        if k != key
    ):
        return ""
    try:
        connectors.gmail_set_inbox(*thread, inbox=False)
    except connectors.ConnectorError as e:
        log.warning("archive failed for %s: %s", key, e)
        return " · couldn't archive it in Gmail"
    store.mutate(lambda s: s["items"][key].update(archived_at=store.now_iso()))
    return " · archived"


def undo_finish(key: str) -> str:
    """Undo for the last tap: a done or dropped item comes back, and an archived
    email goes back to the user's inbox. A sent message stays sent."""
    if ledger.is_commitment_key(key):
        return ledger.act(key, "undo")
    item = store.load()["items"][key]
    thread = _thread(item)
    archived, status = bool(item.get("archived_at")), item["status"]
    if archived and thread:
        connectors.gmail_set_inbox(*thread, inbox=True)

    def change(state: dict[str, Any]) -> None:
        it = state["items"][key]
        it.pop("archived_at", None)
        if it["status"] in ("done", "skipped"):
            it.pop("no", None)
            it.update(status="open", state=it.get("state") or "mine")
            if it.pop("was_in_today", False) and key not in state["today"]["order"]:
                state["today"]["order"].append(key)

    store.mutate(change)
    if status == "sent":
        return "Back in your inbox"
    return "Back in your list" + (" and inbox" if archived else "")


def mark_done(key: str) -> str:
    if ledger.is_commitment_key(key):
        return ledger.act(key, "done")
    return "Marked done" + _finish(key, "done", "Marked done in Today.")


def bring_back(key: str) -> None:
    def change(s: dict[str, Any]) -> None:
        item = s["items"][key]
        no = item.pop("no", None)
        if no:
            # Taking back a no puts back what the item said before it.
            item.update(no.get("before") or {})
        item.update(status="open", state=item.get("state") or "mine")

    store.mutate(change)


def ignore(key: str) -> str:
    """Set it aside for good: nothing is written and nothing is sent."""
    if ledger.is_commitment_key(key):
        return ledger.act(key, "drop")
    store.mutate(
        lambda s: s["items"][key].update(
            cleared_reason="you ignored it; nothing sent", cleared_at=store.now_iso()
        )
    )
    return "Ignored" + _finish(key, "skipped", "Ignored in Today; nothing sent.")


def send(key: str, text: str) -> str:
    item = store.load()["items"][key]
    target = send_target(item)
    if not target.get("can_send"):
        raise SendError("This one can't be sent from here.")
    if not text.strip():
        raise SendError("The message is empty.")
    if item["source"] == "goal":
        label = weekly.send(item, text)
        store.mutate(
            lambda s: s["items"][key].update(sent_text=text, sent_at=store.now_iso())
        )
        _finish(key, "sent", f"Sent from Today: {text}")
        return label
    if item["source"] == "slack":
        connectors.slack_post_message(target["channel"], text, target.get("thread_ts"))
    else:
        connectors.gmail_send_reply(
            item["account"],
            item["ref"]["thread_id"],
            target["to"],
            target["cc"],
            target["subject"],
            target["in_reply_to"],
            target["references"],
            text,
        )
    store.mutate(
        lambda s: s["items"][key].update(sent_text=text, sent_at=store.now_iso())
    )
    archived = _finish(key, "sent", f"Replied from Today: {text}")
    return target["label"].replace("Send", "Sent") + archived


def save_draft_edit(key: str, text: str, index: int | None = None, to: str | None = None,
                    subject: str | None = None) -> None:
    """The user's edits to a drafted message, kept as the user types (the user, Nov 15: edits were lost on a
    reload). The edit replaces the draft; the old wording is not kept. ``index`` is one of the
    emails the assistant wrote; none is the card's own reply (or nudge, on Waiting on)."""
    def change(s: dict[str, Any]) -> None:
        item = s["items"].get(key)
        if item is None:
            raise SendError("That item isn't in Today.")
        if index is None:
            item["nudge" if item.get("state") == "waiting" else "draft"] = text
            if to is not None:
                item["draft_to"] = to
            if subject is not None:
                item["draft_subject"] = subject
            return
        emails = item.get("emails") or []
        if not 0 <= index < len(emails):
            raise SendError("That email isn't there any more.")
        emails[index]["body"] = text
        if subject is not None:
            emails[index]["subject"] = subject
        if to is not None:
            emails[index]["to"] = [t.strip() for t in to.split(",") if t.strip()]
    store.mutate(change)


def save_move_notes(key: str, text: str) -> str:
    """The user's notes on a goal's "go do" move, kept as the user types (beside the user's weekly notes)."""
    return weekly.save_notes(key, text)


def add_to_calendar(key: str, index: int) -> str:
    """Put a proposed event on the user's calendar (only on the user's tap)."""
    item = store.load()["items"][key]
    event = (item.get("calendar") or [])[index]
    if event.get("added"):
        return event.get("link", "")
    made = calendar_events.add(event, source=item.get("url") or "")
    store.mutate(
        lambda s: s["items"][key]["calendar"][index].update(
            added=True, event_id=made.get("id", ""), link=made.get("htmlLink", "")
        )
    )
    calendar_events.refresh()
    return str(made.get("htmlLink") or "")


def not_now(key: str, on: bool = True) -> str:
    """Not now: it sinks into Later (nothing is sent) and comes back up only when it gets
    urgent or someone writes again. ``on`` False is the Undo."""
    if ledger.is_commitment_key(key):
        return ledger.act(key, "notnow" if on else "undo")

    def change(state: dict[str, Any]) -> None:
        item = state["items"][key]
        if on:
            item["not_now"] = store.now_iso()
            if key in state["today"]["order"]:
                state["today"]["order"].remove(key)
        else:
            item.pop("not_now", None)

    store.mutate(change)
    return "Moved to Slipped" if on else "Back on your list"


def out_of_now(key: str, on: bool = True) -> str:
    """Not now on a row in Now: it goes back to its own list (the user, 2026-11-16). Off the user's picks
    for today, and Today doesn't put it back as the urgent one today either, or it would be right
    back. The list resets each morning, so nothing else remembers it. ``on`` False is the Undo."""

    def change(state: dict[str, Any]) -> None:
        today = state["today"]
        out = today.setdefault("out", [])
        if on:
            if key in today["order"]:
                today["order"].remove(key)
                today.setdefault("out_picked", []).append(key)
            if key not in out:
                out.append(key)
        else:
            if key in out:
                out.remove(key)
            if key in today.get("out_picked", []):
                today["out_picked"].remove(key)
                today["order"].append(key)

    store.mutate(change)
    return "Back in its list" if on else "Back in Now"


def pull_back(key: str) -> str:
    """The user pulled something back from Filed away: it is the user's (to reply to, or to do).
    Remembered, so a later read never files it away again."""

    def change(state: dict[str, Any]) -> dict[str, Any]:
        item = state["items"][key]
        was = item.get("filed", "")
        item.update(filed="", filed_by_hand=True, status="open")
        if not item.get("state") or was in ("not_yours", "remark"):
            item["state"] = "mine" if not item.get("draft") else item.get("state") or "ready"
        for k in ("cleared_reason", "cleared_at", "not_now"):
            item.pop(k, None)
        return dict(item, was=was)

    item = store.mutate(change)
    _correction(
        "pulled_back",
        key=key,
        filed=item["was"],
        who=item.get("who", ""),
        ask_text=str(item.get("ask_text", ""))[:400],
    )
    return "Moved to your list"


# Sends wait this long so Undo can stop them; nothing goes out before.
SEND_DELAY_SECONDS = 10.0
_pending: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()


_send_count = itertools.count()
SEND_KINDS = ("reply", "email", "user_draft")


def deliver(key: str, text: str, what: str = "reply", fields: dict[str, Any] | None = None) -> str:
    """Every Send in Today ends here, whatever was written: the reply in the item's own
    conversation, an email the assistant wrote, or the user's own Gmail draft.
    Each goes on the existing thread with the person whenever there is one. A send is the user's
    (the user pressed Send), whenever it goes out."""
    token = store.actor.set("user")
    try:
        return _deliver(key, text, what, fields or {})
    finally:
        store.actor.reset(token)


def _deliver(key: str, text: str, what: str, f: dict[str, Any]) -> str:
    if what == "reply":
        return send(key, text)
    if what == "email":
        return send_email(key, int(f.get("index") or 0), str(f.get("to") or ""), str(f.get("cc") or ""),
                          str(f.get("subject") or ""), text)
    if what == "user_draft":
        return send_user_draft(key, str(f.get("message_id") or ""), text)
    raise SendError("Unknown kind of send.")


def send_soon(
    key: str, text: str, delay: float = SEND_DELAY_SECONDS, what: str = "reply", fields: dict[str, Any] | None = None
) -> str:
    """Send exactly this text after a short wait, unless the user presses Undo first. Every Send
    button goes through here, so every send can be undone. Returns a token for
    ``cancel_send`` and ``send_status``."""
    if what not in SEND_KINDS:
        raise SendError("Unknown kind of send.")
    if not text.strip():
        raise SendError("The message is empty.")
    ledger.ensure_item(key)
    item = store.load()["items"][key]
    if what == "reply" and not send_target(item).get("can_send"):
        raise SendError("This one can't be sent from here.")
    if what == "email" and "@" not in str((fields or {}).get("to") or ""):
        raise SendError("Add an email address in To first.")
    token = f"{key}@{datetime.now().timestamp():.3f}#{next(_send_count)}"  # two sends in one millisecond stay apart
    with conversations.opened(store.DATA_DIR) as db:
        db.execute("INSERT INTO pending_sends (token, key, what, text, fields, due, state) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (token, key, what, text, json.dumps(fields or {}), time.time() + delay, "waiting"))
    _schedule(token, key, text, what, fields, delay)
    return token


def _record(token: str, entry: dict[str, Any]) -> None:
    """A send's state, kept in today.db too, so a restart neither drops a waiting send nor
    forgets what happened to one."""
    result = {k: v for k, v in entry.items() if k in ("message", "error")}
    with conversations.opened(store.DATA_DIR) as db:
        db.execute("UPDATE pending_sends SET state = ?, result = ? WHERE token = ?",
                   (entry["state"], json.dumps(result), token))


def _schedule(token: str, key: str, text: str, what: str, fields: dict[str, Any] | None, delay: float) -> None:
    def go() -> None:
        with _pending_lock:
            entry = _pending.get(token)
            if entry is None or entry["state"] != "waiting":
                return
            entry["state"] = "sending"
        _record(token, {"state": "sending"})
        try:
            result = {"state": "sent", "message": deliver(key, text, what, fields)}
        except (SendError, connectors.ConnectorError, KeyError, ValueError) as e:
            why = str(e)[:200]
            result = {"state": "failed", "error": why}
            store.mutate(lambda s: s["items"][key].update(error=f"Not sent: {why}") if key in s["items"] else None)
        with _pending_lock:
            _pending[token].update(result)
        _record(token, result)

    # The send is the user's (the user pressed Send): the timer runs as the user's request did.
    timer = threading.Timer(delay, contextvars.copy_context().run, args=(go,))
    timer.daemon = True
    with _pending_lock:
        _pending[token] = {"state": "waiting", "timer": timer, "key": key}
    timer.start()


# A send still waiting when Today stopped goes out when it starts again, unless it is this stale.
RESUME_WITHIN_SECONDS = 600.0


def resume_sends() -> int:
    """At start: sends the user pressed that were still waiting out Undo go out; a send that was
    mid-way when Today stopped may or may not have gone, so it is never repeated: the item
    says to check. Returns how many were picked up again."""
    now = time.time()
    with conversations.opened(store.DATA_DIR) as db:
        db.execute("DELETE FROM pending_sends WHERE due < ?", (now - 7 * 86400,))
        rows = db.execute("SELECT * FROM pending_sends WHERE state IN ('waiting', 'sending')").fetchall()
    resumed = 0
    for r in rows:
        if r["state"] == "waiting" and r["due"] > now - RESUME_WITHIN_SECONDS:
            _schedule(r["token"], r["key"], r["text"], r["what"], json.loads(r["fields"]), max(0.0, r["due"] - now))
            resumed += 1
            continue
        if r["state"] == "sending":
            why = "Today restarted while this was sending. Check your sent messages before sending again."
        else:
            why = "Today was stopped before this went out. Press Send again."
        _record(r["token"], {"state": "failed", "error": why})
        store.mutate(lambda s, k=r["key"], w=why: s["items"][k].update(error=f"Not sent: {w}")
                     if k in s["items"] else None)
    return resumed


def cancel_send(token: str) -> bool:
    """Undo a send that has not gone out yet. False when it already went (or is going)."""
    with _pending_lock:
        entry = _pending.get(token)
        if entry is None or entry["state"] != "waiting":
            return False
        entry["timer"].cancel()
        entry["state"] = "cancelled"
    _record(token, {"state": "cancelled"})
    return True


def send_status(token: str) -> dict[str, Any]:
    with _pending_lock:
        entry = dict(_pending.get(token) or {})
    entry.pop("timer", None)
    if entry:
        return entry
    with conversations.opened(store.DATA_DIR) as db:
        row = db.execute("SELECT key, state, result FROM pending_sends WHERE token = ?", (token,)).fetchone()
    if row is None:
        return {"state": "unknown"}
    return {"state": row["state"], "key": row["key"], **json.loads(row["result"])}
