"""The assistant pass: take one owed item as far as it can go.

Read-only by construction: the model gets the conversation (and, when it asks,
the results of up to two Gmail searches we run for it) and answers with JSON.
It never sends anything; sending happens only when the user presses Send.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from today import ai, calendar_events, config, connectors, store

SYSTEM_TEMPLATE = """You help <WHO> get through what they owe people. You get one thing someone asked of <NAME> (or something <NAME> promised), plus the conversation it came from (Slack messages marked later_in_conversation come after the ask in the same conversation, outside its thread; they often settle it, like "done, paid!"). Take it as far as you can without <NAME>. Answer with exactly one JSON object and nothing else.

Fields:
- "owed": false when there is nothing for <NAME> to do personally (an FYI, an automated or marketing message, someone else is the one who must act, or it is not really addressed to <NAME>). Otherwise true.
- "resolved": true when the conversation shows it is already handled: <NAME> already answered or did it (writing back is not enough by itself: <NAME> may have asked them something, said it would happen later, or only acknowledged it; then it is not resolved), someone else took care of it, or the moment has passed (a meeting time that is over). Otherwise false. Judge only the specific thing in the ask: when one message covers several things, others being handled does not resolve this one. When unsure, it is not resolved. A message marked UNSENT DRAFT was never sent: it is not a reply, does not resolve anything, and nothing in it has happened; you may reuse its words in your draft. When <POSS> newest message is an UNSENT DRAFT, <NAME> will see it first as the next step: start "did" by saying whether it still works as written or exactly what is off (a time that has passed, a placeholder left in, a question it misses).
- "resolved_reason": when resolved or not owed, a few plain words to <NAME> saying why, like "you answered in the thread", "the meeting already happened", "Sam is handling it", or "a newsletter, nothing to do". Else "".
- "title": a short plain action for <NAME>, at most 60 characters, starting with a verb and naming the person, like "Tell Sam whether to sign the tax returns".
- "who": first name of the person waiting on <NAME>.
- "state": "ready" when you can write the reply <NAME> would send and it needs no fact or decision you lack; "you" when you need one to three facts or decisions only <NAME> has; "mine" when it is something <NAME> has to do personally (a meeting, a call, a conversation, a task outside this thread); "waiting" when <NAME> has replied and the next move is theirs before it can be finished (<NAME> asked them for something, like which address they sent to), so nothing is owed right now: say in "did" what <NAME> is waiting for, and leave "draft" empty.
- "did": one plain sentence to <NAME> about what you did (refer to other people by name or as "they", never "he" or "she"), like "Sam's draft reads fine, so I wrote an approval." Only claim what you actually did: you read the conversation, and the search results if any were given. Never claim to have opened attachments, documents, calendars, or links.
- "draft": the message <NAME> would send, in <POSS> voice: short, warm, direct, plain words, no corporate filler, no em dashes, matching <POSS> own messages in the conversation. Never invent facts, numbers, dates, people, or commitments; if you would need to, ask instead (state "you"). For "mine", draft a message only if one clearly moves it forward (for example asking an assistant to schedule it); otherwise "".
- "emails": when the right action is one or more NEW emails rather than a reply here (an introduction, a note to someone outside this conversation), write each one: {"to": ["Full Name", ...], "cc": ["Full Name"], "subject": "...", "body": "..."}. For introductions, write ONE email per introduction, addressed to both people being introduced (an intro of Sam to Priya and to Jordan is two emails: Sam and Priya; Sam and Jordan). Use people's full names as the conversation gives them. When an introduction has been asked for or agreed to, ALWAYS write the intro emails, even if a last name or address is missing: use the name you have; <NAME> fills in missing addresses on screen. Each intro says in a line or two who each person is and why they should talk, from what the conversation says. When you write emails, leave "draft" empty, set state "ready", and ask no questions. Else [].
- "elsewhere": false when the draft is a reply in this same Slack conversation or email thread. true when it belongs somewhere else (an introduction email between two people, a message to a different person); then say where in "to".
- "to": who the draft reaches, in plain words, like "Sam" or "the team channel" or "a new email to Sam and Jordan".
- "questions": when state is "you", one to three short questions. Each is {"q": "...", "kind": "choice", "options": ["...", "..."]} with two to four short options, or {"q": "...", "kind": "text"} when the answer is free text. Prefer choices. Else [].
- "done_check": for anything owed, one or two short searches of <POSS> OWN sent messages that would show it was already done somewhere else (people often ask in Slack and get answered by email, or the reverse). Use the people's first names and one or two key words, like "Sam Jordan dinner" or "Priya tax". No operators; the date and sent-only limits are added for you. Else [].
- "promises": things <NAME> personally promised in this conversation that are not done yet and are separate from the ask above, like "I have some notes drafted that I can send this week" or "I'll ask Jordan to help". Each is {"title": "a short plain action starting with a verb, naming the person, at most 60 characters", "quote": "<POSS> words"}. Leave out what <NAME> has clearly already done later in the conversation, and anything that answering the ask would already do (if they asked for access and <NAME> said "I'll get you access", that is the ask, not a separate promise). Fill this in even when the ask itself is not owed or is resolved. Else [].
- "calendar": events to put on <POSS> calendar: when the conversation settles (or nearly settles) something <NAME> will attend (a meeting, party, dinner, call) and the calendar (given below) does not already have it, or when <NAME> asks you to. Each is {"title": "...", "start": "YYYY-MM-DDTHH:MM" (<ZONE> time), "end": "YYYY-MM-DDTHH:MM", "all_day": false, "location": "", "notes": "a line or two of context: who, why, what to bring, and where the details came from"}. Use "YYYY-MM-DD" for start and end with all_day true when there is no time. Start the title with "Maybe: " when <NAME> is unsure about going. When no end is known, make it one hour (a party: three). Never invent a date or time; if one is missing, ask (state "you"). Events go on the calendar only when <NAME> taps, so in "did" say an event is ready to add, never that you added it. Else [].
- "search": up to two Gmail search queries (Gmail search syntax) that would find a fact you need from <POSS> email, like "from:sam 1095-B" (a company by its domain, like from:example.com; OR for other spellings). Only when it would really help; else []."""


def _fill(template: str) -> str:
    """A prompt template with the user's name, possessive, and time zone filled in."""
    return (
        template.replace("<WHO>", config.who_they_are())
        .replace("<NAME>", config.name())
        .replace("<POSS>", config.possessive())
        .replace("<ZONE>", config.load().timezone)
    )


def system() -> str:
    return _fill(SYSTEM_TEMPLATE)


ANSWER_SCHEMA_HINT = (
    '{"owed": true, "resolved": false, "resolved_reason": "", "title": "", "who": "", "state": "ready", '
    '"did": "", "draft": "", "elsewhere": false, "to": "", "questions": [], "emails": [], "done_check": [], "promises": [], "calendar": [], "search": []}'
)
_me: dict[str, str] = {}


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %b %d %-I:%M %p")


def transcript(raw: dict[str, Any]) -> str:
    """The conversation as plain lines, with the ask marked."""
    lines = []
    if raw.get("source") == "slack":
        lines.append(f"Slack, {raw.get('channel_label', '')}")
        for m in raw.get("messages", []):
            mark = ">> " if m.get("is_ask") else ""
            lines.append(
                f"{mark}[{_fmt_time(float(m['ts']))}] {m.get('name')}: {m.get('text')}"
            )
    else:
        me = config.name()
        lines.append(f"Email thread ({raw.get('account')}): {raw.get('subject', '')}")
        for m in raw.get("messages", []):
            who = me if m.get("from_me") else m.get("from")
            if m.get("draft"):
                who = f"{me}, UNSENT DRAFT (never sent, so the other side has not seen it)"
            lines.append(
                f"--- From: {who} | To: {m.get('to')} | Cc: {m.get('cc')} | {m.get('date')}\n{m.get('text')}"
            )
    return "\n".join(lines)


def guidance(item: dict[str, Any]) -> str:
    """The user's standing rules for all drafts, and their notes on this item's earlier drafts."""
    rules = [r["text"] for r in store.load().get("draft_rules", [])]
    notes = [f["note"] for f in item.get("feedback") or []]
    out = ""
    own = config.possessive()
    own = own[0].upper() + own[1:]
    if rules:
        out += (
            f"\n{own} standing rules for every draft (always follow them):\n"
            + "\n".join(f"- {r}" for r in rules)
            + "\n"
        )
    if notes:
        out += (
            f"\n{own} notes on earlier drafts for this item (still apply):\n"
            + "\n".join(f"- {n}" for n in notes)
            + "\n"
        )
    return out


def _prompt(item: dict[str, Any], raw: dict[str, Any], extra: str) -> str:
    kind = _fill(
        "<NAME> promised this"
        if item.get("kind") == "promised"
        else "Someone asked this of <NAME>"
    )
    if item.get("no"):
        kind = _fill(
            "<NAME> has decided to say NO to this (it will not happen). The thing"
        )
    return (
        f"Today is {datetime.now().strftime('%A, %B %d, %Y, %-I:%M %p')}.\n"
        f"{kind}: {item.get('ask_text', '')}\n"
        f"(Who: {item.get('who', '')}. When: {item.get('asked_at', '')}.)\n\n"
        f"The conversation (the marked line is the ask; later lines happened after it):\n{transcript(raw)[:24000]}\n"
        f"\n{calendar_events.brief()}{guidance(item)}{extra}\n\nAnswer with one JSON object shaped like: {ANSWER_SCHEMA_HINT}"
    )


def _clean(answer: dict[str, Any]) -> dict[str, Any]:
    state = (
        answer.get("state")
        if answer.get("state") in ("ready", "you", "mine", "waiting")
        else "mine"
    )
    questions = []
    for q in answer.get("questions") or []:
        if isinstance(q, dict) and q.get("q"):
            opts = [str(o) for o in (q.get("options") or [])][:4]
            questions.append(
                {
                    "q": str(q["q"]),
                    "kind": "choice" if opts else "text",
                    "options": opts,
                }
            )
    if state == "you" and not questions:
        state = "mine"
    return {
        "owed": bool(answer.get("owed", True)),
        "resolved": bool(answer.get("resolved", False)),
        "resolved_reason": str(answer.get("resolved_reason") or ""),
        "title": str(answer.get("title") or "")[:90],
        "who": str(answer.get("who") or ""),
        "state": state,
        "did": str(answer.get("did") or ""),
        "draft": str(answer.get("draft") or ""),
        "elsewhere": bool(answer.get("elsewhere", False)),
        "to": str(answer.get("to") or ""),
        "questions": questions[:3] if state == "you" else [],
        "search": [str(s) for s in (answer.get("search") or [])][:2],
        "done_check": [str(s) for s in (answer.get("done_check") or [])][:2],
        "emails": _clean_emails(answer.get("emails")),
        "calendar": _clean_calendar(answer.get("calendar")),
        "promises": [
            {"title": str(p["title"])[:90], "quote": str(p.get("quote") or "")[:400]}
            for p in (answer.get("promises") or [])
            if isinstance(p, dict) and p.get("title")
        ][:3],
    }


def _clean_calendar(value: Any) -> list[dict[str, Any]]:
    out = []
    for e in value or []:
        if not isinstance(e, dict) or not e.get("title") or not e.get("start"):
            continue
        out.append(
            {
                "title": str(e["title"])[:120],
                "start": str(e["start"])[:16],
                "end": str(e.get("end") or "")[:16],
                "all_day": bool(e.get("all_day")),
                "location": str(e.get("location") or "")[:200],
                "notes": str(e.get("notes") or "")[:1000],
                "added": False,
            }
        )
    return out[:3]


def _clean_emails(value: Any) -> list[dict[str, Any]]:
    out = []
    for e in value or []:
        if isinstance(e, dict) and e.get("body"):
            out.append(
                {
                    "to": [str(n) for n in (e.get("to") or [])][:6],
                    "cc": [str(n) for n in (e.get("cc") or [])][:6],
                    "subject": str(e.get("subject") or "")[:200],
                    "body": str(e["body"]),
                }
            )
    return out[:6]


def with_addresses(emails: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Look up each named person's email address in the user's mail (blank when unsure)."""
    cache: dict[str, str] = {}

    def addr(name: str) -> str:
        if name not in cache:
            cache[name] = connectors.find_address(name, config.accounts())
        return cache[name]

    return [
        dict(
            e,
            to=[{"name": n, "email": addr(n)} for n in e["to"]],
            cc=[{"name": n, "email": addr(n)} for n in e["cc"]],
            sent=False,
        )
        for e in emails
    ]


def slack_me() -> str:
    """The user's own Slack id (asked once per process)."""
    if "id" not in _me:
        _me["id"] = str(connectors.slack_get("auth.test", {})["user_id"])
    return _me["id"]


def sent_since(item: dict[str, Any], queries: list[str]) -> list[dict[str, Any]]:
    """What the user personally sent since the ask that matches the queries: every
    inbox's sent mail, and their own Slack messages anywhere."""
    try:
        asked = datetime.fromisoformat(item.get("asked_at", ""))
    except ValueError:
        asked = datetime.now()
    day = asked.strftime("%Y/%m/%d")
    slack_after = (asked - timedelta(days=1)).date().isoformat()
    hits: list[dict[str, Any]] = []
    for q in queries:
        for account in config.accounts():
            try:
                for h in connectors.gmail_search(
                    f"in:sent after:{day} {q}", account, limit=3
                ):
                    hits.append(dict(h, where=f"email sent from {account}"))
            except connectors.ConnectorError:
                pass
        try:
            data = connectors.slack_get(
                "search.messages",
                {
                    "query": f"from:<@{slack_me()}> after:{slack_after} {q}",
                    "count": "5",
                    "sort": "timestamp",
                },
            )
            for m in (data.get("messages") or {}).get("matches", []) or []:
                if float(m.get("ts", 0)) >= asked.timestamp():
                    hits.append(
                        {
                            "where": f"Slack, {((m.get('channel') or {}).get('name')) or 'a conversation'}",
                            "date": datetime.fromtimestamp(float(m["ts"])).isoformat(
                                timespec="minutes"
                            ),
                            "text": str(m.get("text", ""))[:400],
                        }
                    )
        except connectors.ConnectorError:
            pass
    return hits


def _slack_names() -> dict[str, str]:
    path = config.load().slack_users
    if path is None:
        return {}
    try:
        names = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return names if isinstance(names, dict) else {}


def recent_sent(since: datetime) -> list[dict[str, Any]]:
    """Everything the user sent since then, fetched once per update: every inbox's sent
    mail and their own Slack messages. ``sent_matching`` checks items against it."""
    out: list[dict[str, Any]] = []
    for account in config.accounts():
        try:
            listed = connectors.gmail_get(
                "messages",
                account,
                {"q": f"in:sent after:{int(since.timestamp())}", "maxResults": "100"},
            )
            for ref in listed.get("messages", []) or []:
                m = connectors.gmail_get(
                    f"messages/{ref['id']}", account, {"format": "full"}
                )
                head = {
                    h: connectors.header(m, h)
                    for h in ("From", "To", "Cc", "Subject", "Date")
                }
                body = connectors.strip_quoted(
                    connectors._plain_text(m.get("payload", {}))
                )
                out.append(
                    {
                        "where": f"email sent from {account}",
                        "from": head["From"],
                        "subject": head["Subject"],
                        "date": head["Date"],
                        "snippet": m.get("snippet", ""),
                        "ts": int(m.get("internalDate") or 0) / 1000,
                        "blob": " ".join(head.values()) + " " + body,
                    }
                )
        except connectors.ConnectorError:
            pass
    try:
        data = connectors.slack_get(
            "search.messages",
            {
                "query": f"from:<@{slack_me()}> after:{(since - timedelta(days=1)).date().isoformat()}",
                "count": "100",
                "sort": "timestamp",
            },
        )
        for m in (data.get("messages") or {}).get("matches", []) or []:
            ts = float(m.get("ts", 0))
            if ts < since.timestamp():
                continue
            names = _slack_names()
            channel = m.get("channel") or {}
            where = names.get(str(channel.get("name")), "") or str(
                channel.get("name") or "a conversation"
            )
            text = re.sub(
                r"<@([A-Z0-9]+)>",
                lambda mm: "@" + names.get(mm.group(1), mm.group(1)),
                str(m.get("text", "")),
            )
            out.append(
                {
                    "where": f"Slack, {where}",
                    "date": datetime.fromtimestamp(ts).isoformat(timespec="minutes"),
                    "text": text[:400],
                    "ts": ts,
                    "blob": f"{where} {text}",
                }
            )
    except connectors.ConnectorError:
        pass
    return out


def sent_matching(
    sent: list[dict[str, Any]], queries: list[str], after: float
) -> list[dict[str, Any]]:
    """The messages in ``sent`` after ``after`` holding every word of one of the queries
    (the same words the per-item search would have used)."""
    out = []
    for h in sent:
        blob = h["blob"].lower()
        if h["ts"] > after and any(
            q.split() and all(w.lower() in blob for w in q.split()) for q in queries
        ):
            out.append({k: v for k, v in h.items() if k not in ("blob", "ts")})
    return out


SAY_NO = """
<NAME> pressed No: <NAME> has decided not to do this. Do not help do it; help let it go.
- If the person reasonably expects an answer (someone <NAME> knows or works with, a direct personal ask, a relationship worth keeping), write a short, kind, clear no in <POSS> voice in "draft": thank them if it fits, say no plainly, no invented excuses, no "maybe later" or offers the conversation does not support. Set owed true, resolved false, state "ready", and a title like "Tell Sam no on the panel".
- If no answer is needed (a cold pitch, a mass or automated email, a stranger, someone <NAME> clearly does not want a relationship with), set owed false and resolved_reason "you said no; no reply needed".
- If the conversation already shows it is off (they withdrew, it was cancelled), set resolved true.
questions [], emails [], promises [], search [], done_check []."""


def say_no(item: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """The user said no: either a kind decline to send, or nothing to send at all."""
    answer = _clean(
        ai.complete_json(_prompt(item, raw, _fill(SAY_NO)), system=system())
    )
    answer.update(questions=[], emails=[], promises=[], search=[], done_check=[])
    if answer["owed"] and not answer["resolved"]:
        answer["state"] = "ready" if answer["draft"] else "mine"
    return answer


def take_it_further(
    item: dict[str, Any], raw: dict[str, Any], context: str = ""
) -> dict[str, Any]:
    """First pass, then always a check of what the user already sent elsewhere, plus
    one round of Gmail searches when the model asks for them. ``context`` is what
    the assistant should know about the goal this item is a step of."""
    if item.get("no"):
        return say_no(item, raw)
    return _take_it_further(item, raw, context)


NO_DAYS = 60


def decided_no(item: dict[str, Any]) -> str:
    """What the user has already said no to lately, so a related ask or promise found
    later (often in another conversation with another person) is not put back to them."""
    cutoff = (datetime.now() - timedelta(days=NO_DAYS)).isoformat(timespec="seconds")
    nos = [
        i
        for i in store.load()["items"].values()
        if i.get("no")
        and i["key"] != item.get("key")
        and i["no"].get("at", "") >= cutoff
    ]
    if not nos:
        return ""
    return (
        _fill(
            "\n<NAME> has already said no to these (<POSS> decision; never ask to confirm or decide it again):\n"
        )
        + "\n".join(
            f"- {i.get('title')} (with {i.get('who') or 'someone'}): {((i['no'].get('before') or {}).get('title') or i.get('ask_text') or '')[:200]}"
            for i in nos
        )
        + "\nIf this item is the same thing or part of it (answering it, deciding it, its logistics, looping someone in on it), "
        'set resolved true with resolved_reason "you already said no: <that title>". Do not list promises about those either.\n'
    )


def _take_it_further(
    item: dict[str, Any], raw: dict[str, Any], context: str = ""
) -> dict[str, Any]:
    context = decided_no(item) + context
    first = _clean(ai.complete_json(_prompt(item, raw, context), system=system()))
    if not first["owed"] or first["resolved"]:
        return first
    sent = sent_since(item, first["done_check"]) if first["done_check"] else []
    sent_block = (
        _fill(
            "\nIMPORTANT: messages since the ask that may be related, found by searching <POSS> sent mail and Slack messages:\n"
        )
        + (json.dumps(sent, ensure_ascii=False)[:8000] if sent else "(none found)")
        + _fill(
            "\nIf these show it is already done, by <NAME> or by someone else on <POSS> behalf (for example <NAME> sent the intro by email, or Sam says the invoice is paid), "
            'set resolved to true, with resolved_reason saying who did what and when, like "you emailed Sam and Jordan on Oct 15" or "Sam paid it on Oct 15". '
            "Only resolve on a message that clearly says this specific thing is done.\n"
        )
    )
    found = []
    for query in first["search"]:
        found.append({"query": query, "results": _read_mail(query)})
    extra = context + sent_block
    if found:
        extra += (
            "\nYou asked to search the user's email (every inbox, spam and trash included). Results with each message's text:\n"
            + json.dumps(found, ensure_ascii=False)[:16000]
            + '\nUse them if they help. In "did", say what you found or that the search found nothing useful.'
        )
    extra += '\nNow answer. Leave "search" empty.'
    second = _clean(ai.complete_json(_prompt(item, raw, extra), system=system()))
    second["search"] = []
    second["searched"] = found
    second["sent_found"] = sent
    second["done_check"] = first["done_check"]
    second["promises"] = second["promises"] or first["promises"]
    if second["emails"]:
        second["emails"] = with_addresses(second["emails"])
    return second


def finish_with_answers(item: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """After the user answers the questions, write the message."""
    me = config.name()
    qa = "\n".join(
        f"Q: {q['q']}\n{me}: {a}"
        for q, a in zip(item.get("questions", []), item.get("answers", []))
    )
    extra = (
        f"\nEarlier you asked {me} some questions. The answers:\n{qa}\n"
        f'Now write the draft. Set state to "ready" (or "mine" if it is still something {me} must do personally), questions to [], search to []. '
        'In "did", say briefly what you wrote.'
    )
    answer = _clean(ai.complete_json(_prompt(item, raw, extra), system=system()))
    if answer["emails"]:
        answer["emails"] = with_addresses(answer["emails"])
    if answer["state"] == "you":
        answer["state"] = "ready" if answer["draft"] else "mine"
        answer["questions"] = []
    return answer


def _read_mail(query: str) -> list[dict[str, Any]]:
    """The threads matching a search in either inbox (spam and trash included), each with
    every message's own text, so what the other side said is there, not just the last reply."""
    out: list[dict[str, Any]] = []
    for account in config.accounts():
        try:
            listed = connectors.gmail_get(
                "threads",
                account,
                {"q": query, "maxResults": "3", "includeSpamTrash": "true"},
            )
            for ref in listed.get("threads", []) or []:
                msgs = connectors.thread_messages(
                    connectors.gmail_thread(ref["id"], account)
                )
                out.append(
                    {
                        "inbox": account,
                        "subject": msgs[0]["subject"] if msgs else "",
                        "messages": [
                            {
                                "from": m["from"]
                                + (
                                    " (unsent draft, never sent)"
                                    if m.get("draft")
                                    else ""
                                ),
                                "date": m["date"],
                                "text": m["text"][:2000],
                            }
                            for m in msgs[-6:]
                        ],
                    }
                )
        except connectors.ConnectorError as e:
            out.append({"inbox": account, "error": str(e)[:200]})
    return out


SEARCH_TIPS = (
    "Gmail search tips: a company's mail is best found by its domain (from:example.com); "
    "use OR for other spellings (lockup OR lock-up); keep each search to two or three key words."
)


def revise_draft(
    item: dict[str, Any], raw: dict[str, Any], note: str
) -> dict[str, Any]:
    """Do what the user's note on this item asks: change the draft, write emails, put something on their calendar."""
    current = json.dumps(
        {
            "title": item.get("title", ""),
            "draft": item.get("draft", ""),
            "emails": item.get("emails") or [],
            "calendar": item.get("calendar") or [],
        },
        ensure_ascii=False,
    )
    extra = (
        f"\nWhat you have so far for this item:\n{current}\n"
        f"{config.possessive()} instruction: {note}\n"
        "Do what it asks, changing only what the instruction is about and keeping the rest as it was. "
        'If it is about the message, rewrite "draft" (or "emails"). If it asks to put something on the calendar, fill in "calendar". '
        'If it is about what has to be done, update "title". Set questions to []. '
        'If doing it needs something from email (like finding a message the instruction mentions), put up to two Gmail searches in "search" '
        "(spam and trash are searched too; you get whole threads back and can search once more). "
        + SEARCH_TIPS
        + " Otherwise search []. "
        'In "did", say in a few words what you did.'
    )
    answer = _clean(ai.complete_json(_prompt(item, raw, extra), system=system()))
    searched: list[dict[str, Any]] = []
    for round_left in (1, 0):
        if not answer["search"]:
            break
        found = [{"query": q, "results": _read_mail(q)} for q in answer["search"]]
        searched += found
        extra += (
            "\nResults of your searches of the user's email (every inbox, including spam and trash; whole threads):\n"
            + json.dumps(found, ensure_ascii=False)[:24000]
            + (
                '\nIf these do not answer it, you may put up to two better searches in "search" for one more round. '
                + SEARCH_TIPS
                if round_left
                else "\nSet search to []."
            )
            + "\nNow do the instruction using what you found. Never claim to have found something these results do not show."
        )
        answer = _clean(ai.complete_json(_prompt(item, raw, extra), system=system()))
    answer["search"] = []
    if searched:
        answer["searched"] = searched
    if answer["emails"]:
        answer["emails"] = with_addresses(answer["emails"])
    if answer["state"] == "you":
        answer["state"] = "ready" if (answer["draft"] or answer["emails"]) else "mine"
        answer["questions"] = []
    return answer
