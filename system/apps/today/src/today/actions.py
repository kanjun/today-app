"""What the buttons do: today list, send, done, answers, bring back."""

import html as htmllib
import logging
import re
import subprocess
import threading
from datetime import datetime, timedelta
from email.utils import getaddresses
from typing import Any

from today import (
    ai,
    calendar_events,
    config,
    connectors,
    goals,
    gtd_link,
    intent,
    projects,
    store,
    sync,
    work,
)

log = logging.getLogger(__name__)

VISIBLE = ("open", "working")


class NothingSaved(ValueError):
    """An item has no saved conversation to work from."""


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
    }


def view_state() -> dict[str, Any]:
    def roll(state: dict[str, Any]) -> dict[str, Any]:
        store.roll_today(state, datetime.now().date().isoformat())
        return state

    state = store.mutate(roll)
    items = state["items"]

    def visible(i: dict[str, Any]) -> bool:
        # An item being looked at again keeps showing while that happens.
        return i["status"] in VISIBLE or (
            i["status"] in ("new", "error") and bool(i.get("state"))
        )

    all_goals = {
        gid: g for gid, g in state.get("goals", {}).items() if g["status"] == "open"
    }
    goal_project = {gid: projects.of_goal(g, items) for gid, g in all_goals.items()}
    # Goals inside a project show on the project's page, not the main list.
    open_goals = {gid: g for gid, g in all_goals.items() if not goal_project[gid]}

    def project_of(i: dict[str, Any]) -> str:
        return projects.of_item(i) or goal_project.get(i.get("goal_id") or "", "")

    def project_key(pid: str) -> str:
        return f"project:{pid}"

    project_ids = projects.ids(state)
    order = [
        k
        for k in state["today"]["order"]
        if (k.startswith("project:") and k[8:] in project_ids)
        or (k.startswith("goal:") and k[5:] in all_goals)
        or (
            k in items
            and visible(items[k])
            and items[k].get("goal_id") not in open_goals
        )
    ]
    order = [k for k in order if not items.get(k, {}).get("no")]
    saying_no = [i for i in items.values() if i.get("no") and visible(i)]
    saying_no.sort(key=lambda i: i["no"]["at"])
    open_items = [
        i for k, i in items.items() if visible(i) and k not in order and not i.get("no")
    ]
    # A project's tasks stay inside it; its row carries the soonest due badge instead.
    owed = [
        i
        for i in open_items
        if i.get("goal_id") not in open_goals and not project_of(i)
    ]
    owed.sort(
        key=lambda i: (_urgency(due_days(i.get("due") or "")), i.get("asked_at", ""))
    )
    waiting = [i for i in owed if i.get("state") == "waiting"]
    owed = [i for i in owed if i.get("state") != "waiting"]
    goal_rows = [
        goal_summary(g, items)
        for gid, g in open_goals.items()
        if f"goal:{gid}" not in order
    ]
    goal_rows = [g for g in goal_rows if g]
    goal_rows.sort(key=lambda g: (_urgency(g["due_days"]), g["oldest"]))
    project_rows = []
    for p in projects.all_projects(state):
        mine = [
            i
            for i in open_items
            if project_of(i) == p["id"] and i.get("state") != "waiting"
        ]
        n_goals = sum(1 for gid in all_goals if goal_project[gid] == p["id"])
        if not (mine or n_goals):
            continue
        days = [due_days(i.get("due") or "") for i in mine]
        soonest = min((d for d in days if d is not None), default=None)
        row = {
            "key": project_key(p["id"]),
            "project": p["id"],
            "title": p["title"],
            "state": "mine",
            "tasks": len(mine),
            "due_days": soonest,
            "due_label": due_label(soonest),
            "due_on": due_on(soonest),
            "in_today": project_key(p["id"]) in order,
        }
        project_rows.append(row)
    project_rows.sort(
        key=lambda r: (
            _urgency(r["due_days"] if isinstance(r["due_days"], int) else None),
            str(r["title"]),
        )
    )
    by_key = {r["key"]: r for r in project_rows}
    cutoff = datetime.now().timestamp() - 36 * 3600
    cleared = []
    for i in items.values():
        if i["status"] in ("cleared", "skipped") and i.get("cleared_at"):
            try:
                if datetime.fromisoformat(i["cleared_at"]).timestamp() >= cutoff:
                    cleared.append(i)
            except ValueError:
                pass
    cleared.sort(key=lambda i: i.get("cleared_at", ""), reverse=True)
    pending = sum(
        1
        for i in items.values()
        if (i["status"] in ("new", "error") or i["status"] == "working")
        and not i.get("state")
    )
    sources = config.sources()
    return {
        "date": datetime.now().strftime("%A, %b %-d"),
        "last_sync": state.get("last_sync"),
        "sync_error": state.get("sync_error"),
        "pending": pending,
        # The inputs and whether each is there, so an empty list can say what to connect.
        "sources": sources,
        "has_source": any(s["found"] for s in sources),
        "settings_path": str(config.settings_path()),
        "rules": [r["text"] for r in state.get("draft_rules", [])],
        "today": [
            row
            for row in (
                by_key.get(k)
                if k.startswith("project:")
                else goal_summary(all_goals[k[5:]], items)
                if k.startswith("goal:")
                else summary(items[k])
                for k in order
            )
            if row
        ],
        "projects": [r for r in project_rows if not r["in_today"]],
        "goals": goal_rows,
        "owed": [summary(i) for i in owed],
        "no": [summary(i) for i in saying_no],
        "waiting": [dict(summary(i), did=i.get("did") or "") for i in waiting],
        "cleared": [
            dict(summary(i), reason=i.get("cleared_reason", "")) for i in cleared
        ]
        + [
            {
                "key": f"goal:{g['id']}",
                "who": "Goal",
                "title": g["title"],
                "state": "mine",
                "age": "",
                "reason": "all steps done",
                "source": "goal",
                "draft": "",
                "can_send": False,
            }
            for g in state.get("goals", {}).values()
            if g["status"] == "done"
            and (g.get("done_at") or "") >= datetime.fromtimestamp(cutoff).isoformat()
        ],
    }


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
    """Every item and goal, open or finished, whose words (or conversation) hold all the query's words."""
    terms = [t for t in q.lower().split() if t]
    if not terms:
        return {"items": [], "goals": []}
    state = store.load()

    def hit(text: str) -> bool:
        text = text.lower()
        return all(t in text for t in terms)

    goal_hits = [
        {"key": f"goal:{g['id']}", "title": g["title"], "status": g["status"]}
        for g in state.get("goals", {}).values()
        if g["status"] in ("open", "done")
        and hit(
            " ".join(
                [g["title"], g.get("done_when", "")] + [s["step"] for s in g["chain"]]
            )
        )
    ]
    out = []
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
        out.append(
            dict(
                summary(i),
                status=word,
                reason=i.get("cleared_reason", "") if word != "open" else "",
                when=i.get("asked_at", ""),
                only_in_conversation=not direct,
            )
        )
    # Open before finished; within each, items about it before ones that only mention it, newest first.
    out.sort(key=lambda r: r["when"], reverse=True)
    out.sort(key=lambda r: (r["status"] != "open", r["only_in_conversation"]))
    return {"items": out[:limit], "goals": goal_hits[:10]}


def goal_summary(
    goal: dict[str, Any], items: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    p = goals.progress(goal, items)
    if p["next_index"] is None:
        return None
    step = p["steps"][p["next_index"]]
    first = next(
        (
            items[k]
            for k in step["covers"]
            if k in items and items[k]["status"] not in goals.FINISHED
        ),
        None,
    )
    days = [
        due_days(items[k].get("due") or "")
        for s in p["steps"]
        for k in s["covers"]
        if k in items and items[k]["status"] not in goals.FINISHED
    ]
    soonest = min((d for d in days if d is not None), default=None)
    return {
        "key": f"goal:{goal['id']}",
        "goal": True,
        "due_days": soonest,
        "due_label": due_label(soonest),
        "due_on": due_on(soonest),
        "title": goal["title"],
        "next": step["step"],
        "step_n": p["next_index"] + 1,
        "steps": len(p["steps"]),
        "waiting": len(p["waiting"]),
        "state": "working"
        if first and first["status"] == "working"
        # A next step no task covers is someone else's to do, or the user's.
        else (
            (first or {}).get("state")
            or ("waiting" if not first and step.get("who", "you") != "you" else "mine")
        ),
        "age": _age(p["oldest"]),
        "oldest": p["oldest"],
    }


def goal_detail(gid: str) -> dict[str, Any] | None:
    state = store.load()
    goal = state.get("goals", {}).get(gid)
    if goal is None:
        return None
    items = state["items"]
    p = goals.progress(goal, items)
    steps = []
    for n, s in enumerate(p["steps"]):
        members = [
            dict(summary(items[k]), status=items[k]["status"])
            for k in s["covers"]
            if k in items
        ]
        steps.append(
            {
                "id": s["id"],
                "step": s["step"],
                "who": s["who"],
                "done": s["done"],
                "next": n == p["next_index"],
                "members": members,
            }
        )
    others = [
        {"id": g["id"], "title": g["title"]}
        for g in state["goals"].values()
        if g["status"] == "open" and g["id"] != gid
    ]
    return {
        "id": gid,
        "title": goal["title"],
        "purpose": goal.get("purpose", ""),
        "story": goal.get("story", ""),
        "notes": goal.get("intent_notes") or [],
        "rethinking": bool(goal.get("rethinking")),
        "sources": intent.sources(goal, items),
        "done_when": goal.get("done_when", ""),
        "by": goal.get("by", ""),
        "status": goal["status"],
        "waiting": p["waiting"],
        "steps": steps,
        "in_today": f"goal:{gid}" in state["today"]["order"],
        "other_goals": others,
        # The project it sits in, so back returns there (none for a main-list goal).
        "project": {
            "key": f"project:{pid}",
            "title": next(
                (p["title"] for p in projects.all_projects(state) if p["id"] == pid),
                "Project",
            ),
        }
        if (pid := projects.of_goal(goal, items))
        else None,
    }


def _advance_soon(gid: str | None) -> None:
    """After a step finishes, get the goal's new next step going without waiting for the next update."""
    if not gid:
        return

    def run() -> None:
        state = store.load()
        goal = state.get("goals", {}).get(gid)
        if not goal or goal["status"] != "open":
            return
        store.mutate(goals.close_finished)
        p = goals.progress(goal, state["items"])
        if p["next_index"] is None:
            return
        step = goal["chain"][p["next_index"]]
        if step["id"] == goal.get("next_seen"):
            return
        store.mutate(lambda s: s["goals"][gid].update(next_seen=step["id"]))
        for k in step["covers"]:
            sync._rework(k)

    threading.Thread(target=run, daemon=True).start()


def mark_step_done(gid: str, step_id: str) -> None:
    goal = store.load()["goals"][gid]
    step = next(s for s in goal["chain"] if s["id"] == step_id)
    if not step["covers"]:

        def change(s: dict[str, Any]) -> None:
            for st in s["goals"][gid]["chain"]:
                if st["id"] == step_id:
                    st["done"] = True

        store.mutate(change)
    for k in step["covers"]:
        item = store.load()["items"].get(k)
        if item and item["status"] not in goals.FINISHED:
            _finish(
                k,
                "done",
                f"Marked done in Today as part of the goal: {goal['title']}.",
                advance=False,
            )
    _advance_soon(gid)


def _correction(kind: str, **fields: Any) -> None:
    store.mutate(
        lambda s: s.setdefault("corrections", []).append(
            dict(fields, kind=kind, at=store.now_iso())
        )
    )


def _reread_soon(gid: str) -> None:
    """Read the goal again in the background."""

    def run() -> None:
        sync.reread_goal(gid, force=True)
        store.mutate(goals.close_finished)
        _advance_soon(gid)

    threading.Thread(target=run, daemon=True).start()


def reorder_steps(gid: str, order: list[str]) -> None:
    """The user put the goal's steps in their order; the next step may change, so work it."""
    store.mutate(lambda s: intent.reorder(s, gid, order))
    _advance_soon(gid)


def add_goal_note(gid: str, text: str) -> None:
    """The user's word on what the goal is really about: the goal is read again with it."""
    store.mutate(lambda s: intent.add_note(s, gid, text))
    _reread_soon(gid)


def remove_goal_note(gid: str, at: str) -> None:
    store.mutate(lambda s: intent.remove_note(s, gid, at))
    _reread_soon(gid)


def rename_goal(gid: str, title: str) -> None:
    old = store.load()["goals"][gid]["title"]
    store.mutate(lambda s: s["goals"][gid].update(title=title, user_title=True))
    _correction("rename", goal=gid, title=title, was=old)


def remove_from_goal(key: str) -> None:
    gid = store.mutate(lambda s: goals._take_out(s, key))
    if gid:
        _correction(
            "remove",
            item=key,
            goal=gid,
            item_title=store.load()["items"][key].get("title"),
        )
        store.mutate(goals.close_finished)


def move_to_goal(key: str, gid: str) -> None:
    def change(s: dict[str, Any]) -> None:
        if s["items"][key].get("goal_id"):
            goals._take_out(s, key)
        goals._put_in(s, key, gid)

    store.mutate(change)
    _correction(
        "move", item=key, goal=gid, item_title=store.load()["items"][key].get("title")
    )


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
        connectors.gmail_send_new(account, to, cc, subject, text)

    def mark(s: dict[str, Any]) -> bool:
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
    """Put this item's reply (index None) or one of its emails in the user's Gmail
    drafts. Nothing is sent and the item stays open; pressing again updates the same draft."""
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
        if not to.strip() or "@" not in to:
            raise SendError("Add an email address in To first.")
        found = in_thread(item, to, cc, subject)
        thread = dict(found, thread_id=item["ref"]["thread_id"]) if found else {}
    slot = "reply" if index is None else f"email:{index}"
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


def _user_draft_id(item: dict[str, Any], message_id: str, text: str) -> str:
    """The user's draft's id, with its text replaced first when they edited it here."""
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
    """Keep the user's edits in their Gmail draft; nothing is sent."""
    item = store.load()["items"][key]
    if not text.strip():
        raise SendError("The draft is empty.")
    _user_draft_id(item, message_id, text)
    return "Your Gmail draft is updated"


def send_user_draft(key: str, message_id: str, text: str) -> str:
    """Send the user's own Gmail draft (with any edits made here), on their tap."""
    item = store.load()["items"][key]
    if not text.strip():
        raise SendError("The draft is empty.")
    draft_id = _user_draft_id(item, message_id, text)
    connectors.gmail_send_draft(_account(item), draft_id)
    store.mutate(
        lambda s: s["items"][key].update(sent_text=text, sent_at=store.now_iso())
    )
    archived = _finish(key, "sent", f"Sent your draft from Today: {text}")
    return f"Sent to {item.get('who') or 'them'}" + archived


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
    mine = config.my_emails()
    others = [
        f"{n} <{a}>" if n else a
        for n, a in getaddresses([last.get("to", ""), last.get("cc", "")])
        if a and a.lower() not in mine and a.lower() != sender_addr
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
    item = store.load()["items"].get(key)
    if item is None:
        return None
    state = store.load()
    goal = state.get("goals", {}).get(item.get("goal_id") or "")
    goal_info = None
    if goal and goal["status"] == "open":
        p = goals.progress(goal, state["items"])
        n = next((i for i, s in enumerate(goal["chain"]) if key in s["covers"]), 0)
        goal_info = {
            "id": goal["id"],
            "title": goal["title"],
            "step_n": n + 1,
            "steps": len(p["steps"]),
        }
    others = [
        {"id": g["id"], "title": g["title"]}
        for g in state.get("goals", {}).values()
        if g["status"] == "open" and g["id"] != (item.get("goal_id") or "")
    ]
    item = dict(
        item,
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
        goal=goal_info,
        other_goals=others,
        project=projects.of_item(item)
        or (projects.of_goal(goal, state["items"]) if goal_info and goal else ""),
        projects=[
            {"id": p["id"], "title": p["title"]} for p in projects.all_projects(state)
        ],
        due_days=due_days(str(item.get("due") or "")),
        due_label=due_label(due_days(str(item.get("due") or ""))),
    )


def set_item_project(key: str, project: str) -> None:
    """The user moves an item into a project (or out of all of them); later reads keep it there."""

    def change(state: dict[str, Any]) -> None:
        if project and project not in projects.ids(state):
            raise SendError("There's no such project.")
        state["items"][key].update(project=project, project_by_hand=True)

    store.mutate(change)


def project_page(pid: str) -> dict[str, Any] | None:
    """Everything open in one project: its goals and its loose tasks, soonest due first."""
    state = store.load()
    p = next((p for p in projects.all_projects(state) if p["id"] == pid), None)
    if p is None:
        return None
    items = state["items"]
    open_goals = {
        gid: g
        for gid, g in state.get("goals", {}).items()
        if g["status"] == "open" and projects.of_goal(g, items) == pid
    }
    goal_rows = [r for r in (goal_summary(g, items) for g in open_goals.values()) if r]
    goal_rows.sort(key=lambda g: (_urgency(g["due_days"]), g["oldest"]))
    tasks = [
        i
        for k, i in items.items()
        if i["status"] in VISIBLE
        and not i.get("no")
        and (
            projects.of_item(i) == pid
            or (
                i.get("goal_id") in state.get("goals", {})
                and projects.of_goal(state["goals"][i["goal_id"]], items) == pid
            )
        )
        and i.get("goal_id") not in open_goals
    ]
    tasks.sort(
        key=lambda i: (_urgency(due_days(i.get("due") or "")), i.get("asked_at", ""))
    )
    return {
        "id": pid,
        "title": p["title"],
        "goals": goal_rows,
        "tasks": [summary(i) for i in tasks],
        "in_today": f"project:{pid}" in state["today"]["order"],
    }


def set_today(key: str, on: bool) -> None:
    def change(state: dict[str, Any]) -> None:
        order = state["today"]["order"]
        if on and key not in order:
            order.append(key)
        if not on and key in order:
            order.remove(key)

    store.mutate(change)


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


def add_manual(text: str) -> str:
    key = f"manual:{datetime.now().timestamp():.0f}"

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
        state["today"]["order"].append(key)

    store.mutate(change)
    return key


def _finish(key: str, status: str, note: str, advance: bool = True) -> str:
    """Close an item the user finished. Returns " · archived" when its email left the inbox."""

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
    if item.get("gtd_id") and item.get("gtd_status") not in ("done", "trashed"):
        gtd_link.mark_done(item["gtd_id"], note)
    if advance:
        _advance_soon(item.get("goal_id"))
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
        _thread(other) == thread and other["status"] not in goals.FINISHED
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
    email goes back to the inbox. A sent message stays sent."""
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


def say_no(key: str) -> None:
    """The user won't do this: draft a kind no, or let it go when no reply is needed."""

    def change(state: dict[str, Any]) -> dict[str, Any]:
        item = state["items"][key]
        before = {
            k: item.get(k)
            for k in ("title", "state", "did", "draft", "to", "elsewhere", "questions")
        }
        item.update(
            no={"at": store.now_iso(), "before": before}, status="working", error=None
        )
        if key in state["today"]["order"]:
            state["today"]["order"].remove(key)
        return dict(item)

    item = store.mutate(change)

    def run() -> None:
        raw = store.load_raw(key) or {}
        try:
            if not raw:
                raise NothingSaved("no conversation saved for this one")
            sync._apply_result(key, work.say_no(item, raw), item.get("signature", ""))
        except (
            ai.ClaudeError,
            OSError,
            ValueError,
            KeyError,
            subprocess.TimeoutExpired,
        ) as e:
            # Nothing to write from: treat it as letting it go.
            message = None if not raw else f"Couldn't write a no: {str(e)[:200]}"
            store.mutate(
                lambda s: s["items"][key].update(
                    status="skipped",
                    cleared_reason="you said no",
                    cleared_at=store.now_iso(),
                    error=message,
                )
            )
        item_now = store.load()["items"][key]
        if item_now["status"] in goals.FINISHED:
            _finish(key, item_now["status"], "Said no in Today.")

    threading.Thread(target=run, daemon=True).start()


def let_go(key: str) -> str:
    """Drop it without sending anything."""
    store.mutate(
        lambda s: s["items"][key].update(
            cleared_reason="you said no; nothing sent", cleared_at=store.now_iso()
        )
    )
    return "Dropped" + _finish(key, "skipped", "Said no in Today; nothing sent.")


def send(key: str, text: str) -> str:
    item = store.load()["items"][key]
    target = send_target(item)
    if not target.get("can_send"):
        raise SendError("This one can't be sent from here.")
    if not text.strip():
        raise SendError("The message is empty.")
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


def answer(key: str, answers: list[str]) -> None:
    def change(state: dict[str, Any]) -> dict[str, Any]:
        state["items"][key].update(answers=answers, status="working", error=None)
        return dict(state["items"][key])

    item = store.mutate(change)

    def run() -> None:
        try:
            result = work.finish_with_answers(item, store.load_raw(key) or {})
            fields = {
                k: result[k]
                for k in ("state", "did", "draft", "elsewhere", "to", "emails")
            }
            fields.update(questions=item["questions"], status="open")
        except (
            ai.ClaudeError,
            OSError,
            ValueError,
            KeyError,
            subprocess.TimeoutExpired,
        ) as e:
            fields = {
                "status": "open",
                "error": f"Couldn't finish this: {str(e)[:200]}",
            }
        store.mutate(lambda s: s["items"][key].update(fields))

    threading.Thread(target=run, daemon=True).start()


def revise(key: str, note: str, remember: bool) -> None:
    """Redo this item's draft following the user's note; optionally keep the note as a rule for all drafts."""

    def change(state: dict[str, Any]) -> dict[str, Any]:
        item = state["items"][key]
        item.setdefault("feedback", []).append({"note": note, "at": store.now_iso()})
        item.update(status="working", error=None)
        if remember:
            state.setdefault("draft_rules", []).append(
                {"text": note, "at": store.now_iso(), "from": key}
            )
        return dict(item)

    item = store.mutate(change)

    def run() -> None:
        try:
            result = work.revise_draft(item, store.load_raw(key) or {}, note)
            fields = {
                k: result[k]
                for k in ("state", "did", "draft", "elsewhere", "to", "emails", "title")
            }
            fields.update(
                status="open", calendar=sync._keep_added(key, result["calendar"])
            )
            if result.get("searched"):
                fields["searched"] = result["searched"]
        except (
            ai.ClaudeError,
            OSError,
            ValueError,
            KeyError,
            subprocess.TimeoutExpired,
        ) as e:
            fields = {"status": "open", "error": f"Couldn't redo this: {str(e)[:200]}"}
        store.mutate(lambda s: s["items"][key].update(fields))

    threading.Thread(target=run, daemon=True).start()


def add_to_calendar(key: str, index: int) -> str:
    """Put a proposed event on the user's calendar (only on their tap)."""
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


def remove_rule(index: int) -> None:
    def change(state: dict[str, Any]) -> None:
        rules = state.setdefault("draft_rules", [])
        if 0 <= index < len(rules):
            rules.pop(index)

    store.mutate(change)
