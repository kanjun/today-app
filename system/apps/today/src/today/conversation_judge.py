"""The judge, per conversation.

It reads a whole conversation from the conversations database, plus every commitment
already linked to it, and answers with what changed: a commitment done, a new one, a
waiting-on that is really the user's, a date the conversation implies. Every change names the
messages behind it and is applied as a ``judge`` event, under the two rules in
commitments.py: a write against a version that changed meanwhile is refused, and the user's own
decisions are reopened only when the other person wrote after them.

It decides what is owed and by whom; writing replies stays with work.py for now.
"""

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from today import (
    ai,
    calendar_events,
    commitments,
    config,
    connectors,
    links,
    store,
    task_context,
    user_rules,
    work,
)

log = logging.getLogger(__name__)

MODEL = "sonnet"
SHOW_MESSAGES = 150  # newest messages shown; a commitment's own messages are always shown
RECENT_CLOSED_DAYS = 60  # closed commitments shown too, so the judge doesn't recreate them


SYSTEM = (
    """You keep the user's commitments straight. {ABOUT} You get one whole conversation (Slack channel or DM, a text chat, or an email thread), each message labelled like [m12], and the commitments already tracked from it. Decide what is true now and answer with exactly one JSON object.

A commitment has a direction:
- "mine": the user owes the other person something (they asked, or the user firmly promised).
- "theirs": the other person owes the user something THE USER asked of them. "Waiting on" holds only the user's own asks. Never put something there that the user themselves has to do, or something that waits on an event rather than a person.
- "self": something only the user cares about, with nobody waiting.

In a Slack conversation, a reply inside a thread says which thread it is in ("replying in the thread of [m12]"): it answers that thread's message, not whatever was said just before it in time.

"""
    + user_rules.RULES
    + """

Answer with {"changes": [...], "steps_done": [...]}. List ONLY commitments that are new or whose truth changed; leave correct ones out. Each change:
- "commitment": the id of a tracked commitment, or "new".
- "stage": "open", "done" (answered, given, happened, or settled out of band), "declined" (the user or they said no), or "stale" (the moment passed or it no longer matters; say why).
- "direction", "origin" ("they_asked", "user_asked", "user_promised", "handed" when the user delegated it), "counterparty" (first name), "title" (a short plain action starting with a verb and naming the person, at most 60 characters; for "theirs", "Wait for <name> to ..."): give these for a new one, or when the tracked one is wrong (wrong direction, wrong person, wrong words).
- "state": which of the user's lists an open "mine" belongs in: "ready" (Replies ready: the user has everything the user needs, and a reply in this conversation would finish it), "you" (Quick answers: a decision or fact only the user has is missing before the user can reply, like whether the user is going or which option the user wants), "mine" (Yours to do: the user has to do it themselves outside this conversation, like a call, a meeting, a document or a payment). Else "". The tracked "state" can be wrong or missing: give the right one, and list the commitment for that alone if needed.
- "due": "YYYY-MM-DD" or "". A date stated or clearly implied by the conversation ("when you land in Tampa Dec 29", "before Thanksgiving") counts. "due_kind": "stated", "implied", or "". "due_event": an event it must come before when no date is known ("before the IPO"), else "". "due_quote": the words the date or event comes from.
- "reason": a few plain words to the user, like "you answered by voice and Theo replied", "Nora picked Dec 6".
- "pitch": true for a "mine" that is a cold pitch: a sales, promotion, sponsorship, vendor, recruiting-agency, fundraising or "let's grab coffee" ask from someone with no real relationship with the user, even when the user politely asked for details. A pitch stays open (stage "open", state "ready"): it goes to the user's Pitches list with a short no ready, and the user decides whether to send it; never mark a pitch stale for being unsolicited. An invitation to speak, appear or take part (a talk, podcast, panel, dinner) is never a pitch: whether to go is the user's call. Else leave it out.
- "evidence": the labels of the messages that show it, like ["m12", "m15"]. Required for every change. For a new commitment, the first label is the message it came from: the ask itself, or the message holding the user's promise, never a reply around it.

A tracked commitment can carry:
- "steps": its checklist (S1, S2, ...), each with "done". In "steps_done", list each step NOT done yet that this conversation (or the user's messages elsewhere, the user's calendar, or the user's notes) clearly shows has now happened, that exact step: {"commitment": "<id>", "step": "S2", "reason": "a few plain words", "evidence": ["m14"]}. Never list a step that is already done, and never guess one from a step being likely. Else [].
- "notes": what the user themselves told Today about that task (labelled [n1], [n2]...): the user's facts and decisions, as true as the user's own messages. Read the conversation in their light, and cite a note's label as evidence when it shows something (a note that the user already called Maya can show the call happened).

Rules for tracked commitments:
- A tracked commitment marked "unread" was made from a new message before anyone read the conversation: its title, direction and person are only a first guess. Always list it. If there is no real obligation in it (an FYI, an automated or marketing message, an ongoing arrangement, something someone else said they would do or is their job, or the user's own passing remark nobody is waiting on), mark it "stale" with the reason. Otherwise give its right title, direction, origin, counterparty and state, and "pitch" when it is one. When it is the same as another tracked one, mark it "stale" with "same as: <title>".
- "The user already said no to" lists what the user declined lately, in any conversation. Something that is the same thing or part of one of those (answering it, its logistics, looping someone in on it) is not owed again: mark it "declined" with "you already said no: <title>", and never create it.
- One commitment per real obligation. If two tracked ones are the same thing, mark the extra one "stale" with reason "same as: <title>".
- A commitment the user decided themselves (marked USER DECISION) stays as the user left it unless someone else wrote after the user's decision.
- A tracked commitment with "attached_by_user" is a task the user themselves attached this conversation to, on that date ("attached_by_today": Today found this conversation is about that task, on that date): this conversation is where that task gets worked out, so read it for that task (progress, decisions, what is now the user's or someone else's to do). Messages from before that date are background: they can change that task, but never start a new commitment.
- Some tracked commitments were attached to this conversation by mistake. If one is not discussed here at all, leave it out: never mark it done or stale because this conversation doesn't mention it. Only what is said here counts as evidence.
- Read every message. Never decide by a shortcut ("the user replied, so it is done").
- Check where the promise would have been kept, not only this conversation:
  - "The user's messages elsewhere" (labelled [e1], [e2]...) are what the user sent since, in other conversations, emails and Slack messages, to or about the people in the user's open promises. A promise to tell, send, email, introduce or share is done when one of them clearly does that exact thing ("I'll tell Robin" and the user then told Robin that very thing; "I'll send you the deck" and an email to them carries it). Telling Robin something else is not doing it. If the promise was vague about what the user would tell, read it in the light of what the user then sent.
  - "The user's calendar" events are labelled [c1], [c2]... with the day each was added. A promise to schedule, meet, set up a call or find time is done when an event with that person (as a guest or named in the title) was added after the promise, or has already happened. An event added before the promise doesn't keep it.
  - "Signing notices" (labelled [s1], [s2]...) are mail a signing service (DocuSign, Dropbox Sign, ...) sent to either of the user's inboxes after this conversation started. A "Completed" notice for this same document (the same names and document) means everyone signed it: a promise or ask to sign it is done, and a new one is never made. A notice for a different document or person does not count.
  - Cite the [e], [c] or [s] label as evidence."""
)


# Signing services write when a document is fully signed; that notice is the proof, and it
# often lands in another inbox than the conversation (the user, 2026-11-16: the Fund I side
# letter's "Completed" notice sat in the user's personal Gmail while the judge kept the task open).
ESIGN_FROM = ("from:(docusign.net OR docusign.com OR hellosign.com OR dropboxsign.com "
              "OR pandadoc.net OR adobesign.com OR echosign.com)")
SIGN_WORDS = re.compile(r"\b(sign|signs|signed|signing|signature|docusign|countersign|e-?sign)\b", re.I)
NOTICES_MAX = 8


def _notices(since: float) -> list[dict[str, str]]:
    """Signing services' "Completed" notices in the user's inboxes since a time (a day's slack)."""
    day = datetime.fromtimestamp(since - 86400).strftime("%Y/%m/%d")
    out: list[dict[str, str]] = []
    for account in config.accounts():
        try:
            hits = connectors.gmail_search(f"after:{day} {ESIGN_FROM} subject:(completed OR signed)", account, limit=NOTICES_MAX)
        except connectors.ConnectorError as e:
            log.warning("judge: signing notices in %s unavailable: %s", account, e)
            continue
        out += [{"where": f"email:{account}:{h.get('thread_id', '')}", "date": h.get("date", ""), "from": h.get("from", ""),
                 "subject": h.get("subject", ""), "text": h.get("snippet", "")} for h in hits]
    return out


def _fmt(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%a %b %d %Y %H:%M")


def _when(thread: str) -> str:
    """When a thread not shown here began (a Slack thread is named by its first message's time)."""
    try:
        return _fmt(float(thread))
    except ValueError:
        return "earlier"


def context(db: sqlite3.Connection, conversation_id: str) -> tuple[str, dict[str, str], list[dict[str, Any]]]:
    """The prompt body, the label -> message id map, and the commitments shown."""
    linked = commitments.for_conversation(db, conversation_id)
    cutoff = datetime.now().timestamp() - RECENT_CLOSED_DAYS * 86400
    shown = [
        c for c in linked
        if "step" not in c["labels"] and not c["is_project"]
        and (c["stage"] in ("open", "parked") or datetime.fromisoformat(c["updated_at"]).timestamp() >= cutoff)
    ]
    keep = {
        r[0] for r in db.execute(
            "SELECT message_id FROM commitment_links WHERE conversation_id = ? AND message_id != ''", (conversation_id,)
        )
    }
    rows = db.execute(
        "SELECT source_id, at, sender, is_user, type, text, note, thread FROM messages WHERE conversation_id = ? ORDER BY at",
        (conversation_id,),
    ).fetchall()
    rows = [r for n, r in enumerate(rows) if n >= len(rows) - SHOW_MESSAGES or r["source_id"] in keep]
    conv = db.execute("SELECT title, source FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    labels: dict[str, str] = {}
    origin_of_thread: dict[str, str] = {}
    lines = []
    for n, r in enumerate(rows, 1):
        label = f"m{n}"
        labels[label] = r["source_id"]
        who = config.name() if r["is_user"] else r["sender"]
        text = links.readable(r["text"] or "")
        note = f" [{r['note']}]" if r["note"] else ""
        # Which thread a reply is in, by its first message's label: replies in different threads
        # read alike otherwise (the user, 2026-11-17: the user "yes thank you" on the 409a thread was
        # read as part of another thread, and the ask stayed open).
        reply = bool(r["thread"]) and r["thread"] != r["source_id"]
        starter = origin_of_thread.get(r["thread"]) if reply else None
        thread = f" (replying in the thread of [{starter}])" if starter else f" (in a thread started {_when(r['thread'])})" if reply else ""
        if r["thread"] == r["source_id"]:
            origin_of_thread[r["source_id"]] = label
        lines.append(f"[{label}] {_fmt(r['at'])} {who}{thread}: {text[:3000]}{note}")
    origin_label = {v: k for k, v in labels.items()}
    tracked = []
    for c in shown:
        origin = db.execute(
            "SELECT message_id FROM commitment_links WHERE commitment_id = ? AND conversation_id = ? AND role = 'origin'",
            (c["id"], conversation_id),
        ).fetchone()
        tracked.append({
            "id": c["id"], "title": c["title"], "direction": c["direction"], "origin": c["origin"],
            "counterparty": c["counterparty"], "stage": c["stage"], "due": c["due"],
            "state": c["detail"].get("state", "") if c["direction"] == "mine" else "",
            "due_event": c["detail"].get("due_event", ""),
            "from_message": origin_label.get(origin[0], "") if origin else "",
            "user_decision": c["user_decision_at"] or "",
            **({"pitch": True} if "pitch" in c["labels"] else {}),
            **({"unread": True} if c["detail"].get("unread") else {}),
        })
        on = next((a for a in c["detail"].get("attached") or [] if isinstance(a, dict) and a.get("conversation") == conversation_id), None)
        if on:
            tracked[-1]["attached_by_today" if on.get("by") == "today" else "attached_by_user"] = str(on.get("at") or "")[:10]
        # Its checklist and the user's notes (task_context): the same picture of the task the card's assistant has.
        steps = task_context.steps(db, c["id"])
        if steps:
            c["_steps"] = {f"S{n}": s["id"] for n, s in enumerate(steps, 1)}
            tracked[-1]["steps"] = [{"step": f"S{n}", "text": s["title"], "done": s["stage"] != "open",
                                     **({"note": s["detail"]["note"]} if s["detail"].get("note") else {})}
                                    for n, s in enumerate(steps, 1)]
        kept = task_context.notes(db, c["id"])
        if kept:
            tracked[-1]["notes"] = []
            for x in kept:
                label = f"n{sum(k.startswith('n') for k in labels) + 1}"
                labels[label] = f"note#{x['id']}"
                tracked[-1]["notes"].append(f"[{label}] {x['text']}")
    elsewhere = _elsewhere(db, conversation_id, shown)
    cal = _calendar(db, conversation_id, [c for c in shown if c["stage"] == "open" and c["direction"] == "mine"], labels)
    for n, m in enumerate(elsewhere, 1):
        labels[f"e{n}"] = f"{m['conversation_id']}#{m['source_id']}"
    signing = any(SIGN_WORDS.search(r["text"] or "") for r in rows) or any(SIGN_WORDS.search(c["title"]) for c in shown)
    notices = _notices(rows[0]["at"] if rows else datetime.now().timestamp() - 30 * 86400) if signing else []
    for n, m in enumerate(notices, 1):
        labels[f"s{n}"] = m["where"]
    body = (
        f"Now: {_fmt(datetime.now().timestamp())}\n"
        f"Conversation: {conv['title'] if conv else conversation_id} ({conv['source'] if conv else ''})\n\n"
        + "\n".join(lines)
        + "\n\nTracked commitments (USER DECISION shows when the user decided one themselves):\n"
        + json.dumps([dict(t, user_decision=("USER DECISION " + t["user_decision"]) if t["user_decision"] else "") for t in tracked], ensure_ascii=False, indent=1)
        + (
            "\n\nThe user's messages elsewhere since the user's open promises here:\n"
            + "\n".join(f"[e{n}] {_fmt(m['at'])} in {m['where']}: {m['text'][:800]}" for n, m in enumerate(elsewhere, 1))
            if elsewhere else ""
        )
        + (
            "\n\nSigning notices in the user's inboxes since this conversation started:\n"
            + "\n".join(f"[s{n}] {m['date']} from {m['from']}: {m['subject']} -- {m['text'][:300]}" for n, m in enumerate(notices, 1))
            if notices else ""
        )
        + f"\n\nThe user's calendar (recent and upcoming; times are {calendar_events.ZONE_NAME}):\n"
        + (cal or "(not available)")
        + _said_no(db, {c["id"] for c in shown})
    )
    return body, labels, shown


SAID_NO_DAYS = 60


def _said_no(db: sqlite3.Connection, here: set[str]) -> str:
    """What the user declined lately in other conversations, so it isn't put back to the user."""
    cutoff = (datetime.now() - timedelta(days=SAID_NO_DAYS)).isoformat(timespec="seconds")
    rows = db.execute("SELECT id, title, counterparty FROM commitments WHERE stage = 'declined' AND updated_at >= ? "
                      "ORDER BY updated_at DESC LIMIT 40", (cutoff,)).fetchall()
    lines = [f"- {r[1]}" + (f" (with {r[2]})" if r[2] else "") for r in rows if r[0] not in here]
    return "\n\nThe user already said no to:\n" + "\n".join(lines) if lines else ""


CALENDAR_DAYS = 7  # past events shown, at least; back to the user's oldest open promise here otherwise
CALENDAR_MAX = 250
LOOK_BACK_DAYS = 31  # the calendar copy reaches a month back; so does the search of what the user sent


def _promised_at(db: sqlite3.Connection, conversation_id: str, promises: list[dict[str, Any]]) -> float:
    """When the oldest of these promises was made: its first message here (a commitment
    found by the import was created later than it was said), at most a month back."""
    times = []
    for c in promises:
        row = db.execute(
            "SELECT m.at FROM commitment_links l JOIN messages m ON m.conversation_id = l.conversation_id "
            "AND m.source_id = l.message_id WHERE l.commitment_id = ? AND l.conversation_id = ? AND l.role = 'origin'",
            (c["id"], conversation_id),
        ).fetchone()
        times.append(row[0] if row and row[0] else datetime.fromisoformat(c["created_at"]).timestamp())
    return max(min(times), datetime.now().timestamp() - LOOK_BACK_DAYS * 86400)


def _calendar(db: sqlite3.Connection, conversation_id: str, promises: list[dict[str, Any]], labels: dict[str, str]) -> str:
    """The user's calendar from a week back (or the user's oldest open promise here) onward, one labelled
    line per event the user hasn't declined, with the day it was added, so a promise to meet or
    schedule can be checked against it. Upcoming events are never cut off by old ones."""
    record = store.load_raw(calendar_events.KEY) or {}
    since = datetime.now().timestamp() - CALENDAR_DAYS * 86400
    if promises:
        since = min(since, _promised_at(db, conversation_id, promises))
    kept = []
    for e in record.get("events", []):
        people = e.get("attendees") or []
        if any(a.get("self") and a.get("responseStatus") == "declined" for a in people):
            continue
        start = e.get("start") or {}
        try:
            at = datetime.fromisoformat(start.get("dateTime") or start.get("date", "")).timestamp()
        except ValueError:
            continue
        if at >= since:
            kept.append(e)
    lines = []
    for e in kept[-CALENDAR_MAX:]:  # too many: the oldest go, never the upcoming
        people = e.get("attendees") or []
        guests = [a.get("displayName") or a.get("email", "") for a in people if not a.get("self") and not a.get("resource")]
        label = f"c{len(lines) + 1}"
        labels[label] = f"calendar#{e.get('id', '')}"
        line = f"[{label}] {calendar_events._when(e.get('start') or {})}: {e.get('summary', '(no title)')}"
        if guests:
            line += f" (with {', '.join(guests[:8])}{' +more' if len(guests) > 8 else ''})"
        if e.get("created"):
            line += f" [added {datetime.fromisoformat(e['created'].replace('Z', '+00:00')).astimezone().strftime('%a %b %d')}]"
        lines.append(line)
    return "\n".join(lines)


ELSEWHERE_MAX = 20  # the user's messages from other conversations shown, newest kept


SENT_FRESH_SECONDS = 600
_sent_lock = threading.Lock()
_sent_cache: dict[str, Any] = {"at": 0.0, "items": []}


def _sent(since: datetime) -> list[dict[str, Any]]:
    """Everything the user sent since then: both inboxes' sent mail and the user's Slack messages
    anywhere, including conversations Today never saved. Fetched once for a whole refresh."""
    with _sent_lock:
        if time.time() - _sent_cache["at"] > SENT_FRESH_SECONDS:
            _sent_cache["items"] = work.recent_sent(datetime.now() - timedelta(days=LOOK_BACK_DAYS))
            _sent_cache["at"] = time.time()
        items = _sent_cache["items"]
    return [h for h in items if h["ts"] >= since.timestamp()]


def open_promises(db: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
    return [c for c in commitments.for_conversation(db, conversation_id)
            if c["stage"] == "open" and c["direction"] == "mine" and "step" not in c["labels"] and not c["is_project"]]


def outside_marks(db: sqlite3.Connection, conversation_id: str) -> str:
    """A fingerprint of what outside this conversation could show the user's open promises here
    kept: the user's messages elsewhere to or about the people in them, and calendar events with
    those people added since. When it changes, the conversation is read again, though
    nobody wrote in it."""
    promises = open_promises(db, conversation_id)
    if not promises:
        return ""
    marks = [f"{m['conversation_id']}#{m['source_id']}" for m in _elsewhere(db, conversation_id, promises)]
    # A note the user gave Today on one of them can settle it too.
    marks += [f"note#{x['id']}" for c in promises for x in task_context.notes(db, c["id"])]
    names = set().union(*(_people(c) for c in promises))
    named = _naming(names, flags=re.IGNORECASE)
    since = _promised_at(db, conversation_id, promises)
    for e in (store.load_raw(calendar_events.KEY) or {}).get("events", []):
        try:
            added = datetime.fromisoformat(str(e.get("created", "")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        who = " ".join([str(e.get("summary", ""))] + [f"{a.get('displayName', '')} {a.get('email', '')}" for a in e.get("attendees") or []]).lower()
        if names and added >= since and named.search(who):
            marks.append(f"calendar#{e.get('id', '')}")
    return hashlib.sha256("\n".join(sorted(marks)).encode()).hexdigest()[:16] if marks else ""


def _people(c: dict[str, Any]) -> set[str]:
    """First names in a promise: its counterparty and any capitalised name in its title."""
    words = [w.strip(".,:;!?'\u2019s()") for w in c["title"].split()[1:]]
    names = {w for w in words if len(w) > 2 and w[0].isupper()}
    if c["counterparty"]:
        names.add(c["counterparty"].split()[0])
    return names


def _naming(names: set[str], flags: int = 0) -> re.Pattern[str]:
    """Matches any of the names as a whole word ("Ravi", never "Lessons")."""
    return re.compile(r"\b(?:" + "|".join(re.escape(n) for n in sorted(names)) + r")\b", flags)


def _elsewhere(db: sqlite3.Connection, conversation_id: str, shown: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the user wrote in other conversations after the user's open promises here: in a
    conversation with one of the people they name, mentioning one of them, or linking back
    here. A promise kept somewhere else ("I'll tell Robin", then a DM to Robin) is only
    visible this way."""
    promises = [c for c in shown if c["stage"] == "open" and c["direction"] == "mine"]
    if not promises:
        return []
    since = _promised_at(db, conversation_id, promises)
    names = set().union(*(_people(c) for c in promises))
    if not names:
        return []
    # Conversations with one of them: they are the only other person writing there, or the title names them.
    with_them: dict[str, str] = {}
    for conv, title, senders in db.execute(
        "SELECT m.conversation_id, c.title, GROUP_CONCAT(DISTINCT m.sender) FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id WHERE m.is_user = 0 AND m.conversation_id != ? "
        "GROUP BY m.conversation_id", (conversation_id,)
    ):
        others = {x.split()[0] for x in (senders or "").split(",") if x.strip()}
        for n in names:
            if others == {n} or n in (title or "").split():
                with_them[conv] = f"{title} with {n}" if title in ("direct message", "") else title
    link = conversation_id.split(":", 1)[1]
    named = _naming(names)
    out: list[dict[str, Any]] = []
    for r in db.execute(
        "SELECT m.conversation_id, m.source_id, m.at, m.text, c.title FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id "
        "WHERE m.is_user = 1 AND m.at >= ? AND m.conversation_id != ? ORDER BY m.at", (since, conversation_id)
    ):
        text = r[3] or ""
        if r[0] in with_them or link in text or named.search(text):
            out.append({"conversation_id": r[0], "source_id": r[1], "at": r[2], "text": text,
                        "where": with_them.get(r[0]) or r[4] or r[0]})
    # Mail and Slack messages the saved conversations don't hold: a new email to them, a DM.
    have = {(m["conversation_id"], round(m["at"])) for m in out}
    for h in _sent(datetime.fromtimestamp(since)):
        if h.get("thread_id"):
            conv = f"email:{h['where'].removeprefix('email sent from ')}:{h['thread_id']}"
            where = f"email \"{h.get('subject', '')}\""
            text = h["blob"]
        else:
            conv, where, text = f"slack:{h.get('channel', '')}", h["where"], h.get("text", "")
        if conv == conversation_id or (conv, round(h["ts"])) in have or not named.search(h["blob"]):
            continue
        have.add((conv, round(h["ts"])))
        out.append({"conversation_id": conv, "source_id": str(h["ts"]), "at": h["ts"], "text": text, "where": where})
    out.sort(key=lambda m: m["at"])
    return out[-ELSEWHERE_MAX:]


def judge(db: sqlite3.Connection, conversation_id: str, model: str = MODEL) -> dict[str, Any]:
    """Ask for the changes (nothing applied)."""
    body, labels, shown = context(db, conversation_id)
    answer = ai.complete_json(body, system=SYSTEM, model=model)
    return {"changes": answer.get("changes") or [], "steps_done": answer.get("steps_done") or [], "labels": labels,
            "versions": {c["id"]: c["version"] for c in shown}, "steps": {c["id"]: c.get("_steps") or {} for c in shown}}




def _fields(ch: dict[str, Any], cur: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ("title", "direction", "origin", "counterparty", "stage", "due"):
        if k == "due" and cur is not None and (cur.get("detail") or {}).get("due_set_by") == "user":
            continue  # a date the user set in the picker stays the user's
        v = ch.get(k)
        if v not in (None, "") and (cur is None or cur.get(k) != v):
            out[k] = v
    if ch.get("reason") and out.get("stage") in ("stale", "declined", "done"):
        out["stage_reason"] = str(ch["reason"])
    set_by_user = cur is not None and (cur.get("detail") or {}).get("due_set_by") == "user"
    extra = {k: ch[k] for k in ("state", "due_kind", "due_event", "due_quote")
             if ch.get(k) not in (None,) and not (set_by_user and k != "state")}
    detail = dict((cur or {}).get("detail") or {})
    changed = {k: v for k, v in extra.items() if detail.get(k, "") != v}
    if changed or detail.get("unread"):
        # Once judged, a task made from a new message is no longer a first guess.
        out["detail"] = {k: v for k, v in dict(detail, **changed).items() if k != "unread"}
    if "pitch" in ch:
        labels = list((cur or {}).get("labels") or [])
        if bool(ch["pitch"]) != ("pitch" in labels):
            out["labels"] = [*labels, "pitch"] if ch["pitch"] else [x for x in labels if x != "pitch"]
    return out


def apply(db: sqlite3.Connection, conversation_id: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply the judge's changes as events; returns what happened to each."""
    labels, versions = result["labels"], result["versions"]
    done = []
    for ch in result["changes"]:
        cited = [e for e in ch.get("evidence") or [] if e in labels]
        # [mN] is a message here; [eN] one of the user's elsewhere and [cN] a calendar event, already
        # "conversation#message" and "calendar#event".
        evidence = [f"{conversation_id}#{labels[e]}" if e.startswith("m") else labels[e] for e in cited]
        msg_ids = [labels[e] for e in cited if e.startswith("m")]
        reason = str(ch.get("reason") or "")
        cid = str(ch.get("commitment") or "")
        try:
            if cid == "new":
                fields = _fields(ch, None)
                if not all(fields.get(k) for k in ("title", "direction", "origin")):
                    done.append({"change": ch, "outcome": "skipped: incomplete"})
                    continue
                fields["started_by"] = "them" if fields["origin"] == "they_asked" else "user"
                new = commitments.create(
                    db, "judge", fields,
                    links=[(conversation_id, msg_ids[0] if msg_ids else "", "origin")] + [(conversation_id, m, "evidence") for m in msg_ids[1:]],
                    reason=reason,
                )
                done.append({"change": ch, "outcome": "created", "id": new})
                continue
            if cid not in versions:
                done.append({"change": ch, "outcome": "skipped: not a commitment of this conversation"})
                continue
            cur = commitments.get(db, cid)
            fields = _fields(ch, cur)
            if not fields:
                done.append({"change": ch, "outcome": "no change"})
                continue
            commitments.change(db, cid, "judge", fields, expect_version=versions[cid], reason=reason, evidence=evidence)
            for m in msg_ids:
                commitments.link(db, cid, conversation_id, m, "evidence")
            done.append({"change": ch, "outcome": "changed", "id": cid, "fields": fields})
        except commitments.Refused as e:
            done.append({"change": ch, "outcome": f"refused: {e}"})
        except commitments.StaleWrite as e:
            done.append({"change": ch, "outcome": f"stale: {e}"})
        except commitments.CommitmentError as e:
            done.append({"change": ch, "outcome": f"rejected: {e}"})
    for st in result.get("steps_done") or []:
        done.append({"change": st, "outcome": _tick_step(db, conversation_id, result, st)})
    for cid in versions:
        # An unread one the judge left as it was is read now too.
        cur = commitments.get(db, cid)
        if cur is not None and cur["detail"].get("unread"):
            try:
                commitments.change(db, cid, "judge", {"detail": {k: v for k, v in cur["detail"].items() if k != "unread"}},
                                   reason="read by the judge, left as it was")
            except commitments.CommitmentError as e:
                log.warning("judge: %s: %s", cid, e)
    return done


def _tick_step(db: sqlite3.Connection, conversation_id: str, result: dict[str, Any], st: Any) -> str:
    """A step the conversation shows has happened: ticked, as a ``judge`` event with what shows it.
    One the user unticked themselves stays unticked unless someone wrote after."""
    if not isinstance(st, dict):
        return "skipped: not a step"
    cid = str(st.get("commitment") or "")
    sid = (result.get("steps") or {}).get(cid, {}).get(str(st.get("step") or ""))
    labels = result["labels"]
    cited = [e for e in st.get("evidence") or [] if e in labels]
    if not sid or not cited:
        return "skipped: no such step, or nothing shows it"
    s = commitments.get(db, sid)
    if s is None or s["stage"] != "open":
        return "skipped: already done"
    # Only the user's unticking a step is the user's decision to keep it open (a step the assistant made is not).
    unticked = [e for e in commitments.history(db, sid) if e["actor"] in commitments.USER_ACTORS and e["change"].get("stage") == "open"]
    if unticked and not commitments.they_wrote_since(db, cid, unticked[-1]["at"]):
        return "refused: the user unticked this step themselves"
    evidence = [f"{conversation_id}#{labels[e]}" if e.startswith("m") else labels[e] for e in cited]
    reason = str(st.get("reason") or "the conversation shows it happened")
    commitments.change(db, sid, "judge", {"stage": "done", "stage_reason": reason}, reason=reason, evidence=evidence)
    left = [x for x in task_context.steps(db, cid) if x["stage"] == "open"]
    task = commitments.get(db, cid)
    nxt = left[0]["title"] if left else ""
    if task is not None and task["next_step"] != nxt:
        commitments.change(db, cid, "judge", {"next_step": nxt}, reason="its first step not done")
    return "step done"
