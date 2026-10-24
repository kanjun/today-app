"""What each goal is for, worked out from everything that touches it.

A goal is something the user is trying to do, not a pile of tasks. ``rethink`` reads
the whole of it: every conversation its tasks come from (anyone's messages, not only
theirs), the other conversations and meetings linked to it, their calendar, and their own
notes on what the goal is really about. It writes back the goal's purpose, where it stands and its
steps (including steps no task covers), settles tasks the evidence shows are done, and
links conversations and meetings it finds. Those links are the goal's sources: a new
message in any of them, a meeting moving or passing, or a note from the user changes the
goal's ``signature``, and each update re-reads only the goals whose signature changed.
"""

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from today import ai, calendar_events, config, connectors, store, work

log = logging.getLogger(__name__)

FINISHED = ("sent", "done", "cleared", "skipped")
PER_CONVERSATION = 8000
ALL_CONVERSATIONS = 60000
SEARCH_DAYS = 21

SYSTEM_TEMPLATE = """You keep one of <POSS> goals true to what <NAME> is actually trying to do. A goal is something <NAME> is trying to accomplish (a relationship, a project, an event, a decision, a deal), not a pile of tasks grouped by person. You get the goal as it stands, <POSS> own notes on what it is really about (they always win), its tasks and their status, the conversations linked to it in full (Slack and email; anyone's messages count, not only <POSS>), the meetings linked to it, and <POSS> calendar.

Answer with one JSON object:
- "title": the goal in a few words, naming what <NAME> is trying to do ("Northwind contract closed out on a good note", "Monday's team dinner with Priya and Jordan"). Never a list of tasks, never "X looped in".
- "purpose": one or two plain sentences on what <NAME> is trying to do and why, read from the conversations: what <NAME> said, asked for, and cares about.
- "done_when": one sentence: what is true once <NAME> has got there. Not a list of <POSS> tasks.
- "by": "YYYY-MM-DD" when a real date is known, else "".
- "story": where it stands now, one to three sentences with dates: what has already happened (by anyone, in any linked conversation or meeting) and what comes next.
- "steps": the steps toward done_when, in order, finished ones included. Each {"step": "a short action, verb first", "who": "you" or the first name of whoever does it, "covers": [keys of the tasks this step finishes], "done": true or false}. Every open task key goes in exactly one step. Add steps no task covers when the goal needs them, and mark a step done when the evidence shows it happened. Only steps that move done_when; keep it short. Word each step as what actually happened or has to happen; fix wording the conversations show is wrong.
- "not_part": keys of tasks that serve a different effort than this goal (they go back to <POSS> main list). Else [].
- "settled": open tasks the evidence shows are already done or no longer needed: [{"key": "...", "reason": "who did what and when, like 'Sam and Jordan moved the Northwind sync to Monday 10am on Google Meet in the shared project channel on Oct 22'"}]. Only on clear evidence.
- "links": {"conversations": [ids of the candidate conversations that are about this goal], "events": [ids of the calendar events that are part of it]}.
- "search": up to 3 short search queries (words, names) that would find other Slack or email conversations about this goal, like a channel where others are handling part of it. Only on the first reading; else [].
- "goal_done": true when done_when is already true.

Write "purpose", "story" and "done_when" to <NAME>, in the second person ("You're hosting a dinner...", "You asked Jordan on Oct 20..."), never by name or in the third person.

How to read it:
1. Who acts next is decided by the whole conversation: the latest open request or question wins, not the message a task came from.
2. Something <NAME> asked someone else to do is theirs: a step with "who" their name.
3. A step <NAME> has already done in the conversation (sent the reply, gave the answer) is done, even when a task still covers it; then the next move is usually the other side's. A call or meeting whose time has passed happened, unless something says it did not. A call leaves no messages behind, so silence after it means nothing.
4. A step tied to a meeting or date that has not come yet is not done (compare with today's date). Something <NAME> will do after an event is not due, and cannot be done, before that event.
5. Others handling it counts: when people settled it in another channel, thread or meeting, it is settled.
6. <POSS> notes on the goal win over your own reading.
7. When "steps_ordered_by_user" is true, <NAME> put the steps in that order: keep it, and put any new step where it fits."""


def system() -> str:
    """The reading prompt with the user's name and possessive filled in."""
    poss = config.possessive()
    return (
        SYSTEM_TEMPLATE.replace("6. <POSS>", "6. " + poss[0].upper() + poss[1:])
        .replace("<NAME>", config.name())
        .replace("<POSS>", poss)
    )


def conversation_id(item: dict[str, Any]) -> str:
    """The conversation an item comes from: one Slack channel or DM, or one email thread."""
    ref = item.get("ref") or {}
    if item.get("source") == "slack" and ref.get("channel"):
        return f"slack:{ref['channel']}"
    if item.get("source") == "email" and ref.get("thread_id"):
        return f"email:{item.get('account', '')}:{ref['thread_id']}"
    return ""


def members(goal: dict[str, Any], items: dict[str, Any]) -> list[dict[str, Any]]:
    return [items[k] for s in goal.get("chain", []) for k in s["covers"] if k in items]


def _last_ts(raw: dict[str, Any]) -> float:
    msgs = raw.get("messages") or []
    if not msgs:
        return 0.0
    last = msgs[-1]
    try:
        if last.get("ts"):
            return float(last["ts"])
        if last.get("internal_ms"):  # an email
            return float(last["internal_ms"]) / 1000
    except (TypeError, ValueError):
        pass
    return float(len(msgs))


def _names() -> dict[str, str]:
    """Slack user id -> display name, from the configured users file ({} without one)."""
    return work._slack_names()


def _slack_channel(channel: str) -> dict[str, Any]:
    """The newest messages in a Slack channel or DM, as a raw conversation record."""
    names = _names()
    msgs = connectors.slack_get(
        "conversations.history", {"channel": channel, "limit": "40"}
    ).get("messages", [])
    me = work.slack_me()

    def who(uid: str) -> str:
        return config.name() if uid == me else names.get(uid, uid)

    try:
        info = connectors.slack_get("conversations.info", {"channel": channel})
        name = str((info.get("channel") or {}).get("name") or "")
    except connectors.ConnectorError:
        name = ""
    return {
        "source": "slack",
        "channel_label": f"#{name}"
        if name
        else f"channel {names.get(channel, channel)}",
        "messages": [
            {
                "ts": m["ts"],
                "name": who(str(m.get("user") or m.get("bot_id") or "?")),
                "text": re.sub(
                    r"<@([A-Z0-9]+)>",
                    lambda mm: "@" + who(mm.group(1)),
                    str(m.get("text") or ""),
                ),
            }
            for m in reversed(msgs)
            if m.get("type") == "message"
        ],
    }


def _stored(conv: str, items: dict[str, Any]) -> dict[str, Any]:
    """The freshest saved copy of a conversation among all the items that come from it."""
    best: dict[str, Any] = store.load_raw(f"conv:{conv}") or {}
    for item in items.values():
        if conversation_id(item) == conv:
            raw = store.load_raw(item["key"]) or {}
            if _last_ts(raw) > _last_ts(best):
                best = raw
    return best


def conversations(
    goal: dict[str, Any], state: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Every conversation the goal draws on, newest copy of each."""
    items = state["items"]
    ids = [conversation_id(i) for i in members(goal, items)]
    ids += (goal.get("links") or {}).get("conversations", [])
    out: dict[str, dict[str, Any]] = {}
    for conv in dict.fromkeys(c for c in ids if c):
        raw = _stored(conv, items)
        kind, _, rest = conv.partition(":")
        try:
            if kind == "slack" and not raw:
                raw = _slack_channel(rest)
            elif kind == "slack" and conv in (goal.get("links") or {}).get(
                "conversations", []
            ):
                raw = _slack_channel(rest)
            elif kind == "email" and (
                not raw or conv in (goal.get("links") or {}).get("conversations", [])
            ):
                account, _, thread = rest.partition(":")
                msgs = connectors.thread_messages(
                    connectors.gmail_thread(thread, account)
                )
                raw = {
                    "source": "email",
                    "account": account,
                    "subject": msgs[0]["subject"] if msgs else "",
                    "messages": msgs,
                }
        except (connectors.ConnectorError, OSError, ValueError, KeyError) as e:
            log.warning("goal %s: could not read %s: %s", goal.get("id"), conv, e)
        if raw:
            out[conv] = raw
            if conv not in {conversation_id(i) for i in members(goal, items)}:
                # A conversation only the goal links to is kept too, so its page can show it.
                store.save_raw(f"conv:{conv}", raw)
    return out


def signature(
    goal: dict[str, Any], state: dict[str, Any], convs: dict[str, dict[str, Any]]
) -> str:
    """Changes whenever anything the goal is read from changes."""
    linked = calendar_events.events((goal.get("links") or {}).get("events", []))
    now = datetime.now().isoformat(timespec="minutes")
    parts = {
        "convs": sorted(
            (c, len(r.get("messages") or []), _last_ts(r)) for c, r in convs.items()
        ),
        "items": sorted(
            (i["key"], i["status"], i.get("state") or "")
            for i in members(goal, state["items"])
        ),
        # A meeting moving, or its time passing, is news.
        "events": sorted(
            (
                e.get("id", ""),
                e.get("updated", ""),
                ((e.get("end") or {}).get("dateTime") or "") < now,
            )
            for e in linked
        ),
        "notes": [n["text"] for n in goal.get("intent_notes") or []],
    }
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def _search(queries: list[str]) -> list[dict[str, Any]]:
    """Other conversations, anyone's, that may be about the goal: Slack and every inbox."""
    after = (datetime.now() - timedelta(days=SEARCH_DAYS)).date()
    names = _names()
    hits: list[dict[str, Any]] = []
    for q in queries[:3]:
        try:
            data = connectors.slack_get(
                "search.messages",
                {
                    "query": f"{q} after:{after.isoformat()}",
                    "count": "8",
                    "sort": "score",
                },
            )
            for m in (data.get("messages") or {}).get("matches", []) or []:
                channel = m.get("channel") or {}
                hits.append(
                    {
                        "conversation": f"slack:{channel.get('id', '')}",
                        "where": f"Slack, {names.get(str(channel.get('name')), '') or channel.get('name') or 'a conversation'}",
                        "from": m.get("username", ""),
                        "date": datetime.fromtimestamp(float(m.get("ts", 0))).isoformat(
                            timespec="minutes"
                        ),
                        "text": str(m.get("text", ""))[:300],
                    }
                )
        except connectors.ConnectorError:
            pass
        for account in config.accounts():
            try:
                for h in connectors.gmail_search(
                    f"after:{after.strftime('%Y/%m/%d')} {q}", account, limit=3
                ):
                    hits.append(
                        dict(
                            h, conversation=f"email:{account}:{h.get('thread_id', '')}"
                        )
                    )
            except connectors.ConnectorError:
                pass
    return hits


def _prompt(
    goal: dict[str, Any],
    state: dict[str, Any],
    convs: dict[str, dict[str, Any]],
    extra: str,
) -> str:
    items = state["items"]
    tasks = [
        {
            "key": i["key"],
            "title": i.get("title", ""),
            "who": i.get("who", ""),
            "status": "open"
            if i["status"] not in FINISHED
            else f"finished ({i['status']})",
            "what_we_know": (i.get("cleared_reason") or i.get("did") or "")[:300],
            "conversation": conversation_id(i),
            "due": i.get("due", ""),
        }
        for i in members(goal, items)
    ]
    budget = ALL_CONVERSATIONS
    blocks = []
    for conv, raw in convs.items():
        text = work.transcript(raw)[-min(PER_CONVERSATION, budget) :]
        budget -= len(text)
        blocks.append(f"=== conversation {conv}\n{text}")
        if budget <= 0:
            break
    linked = calendar_events.events((goal.get("links") or {}).get("events", []))
    notes = [n["text"] for n in goal.get("intent_notes") or []]
    current = {
        "title": goal.get("title", ""),
        "renamed_by_user": bool(goal.get("user_title")),
        "purpose": goal.get("purpose", ""),
        "done_when": goal.get("done_when", ""),
        "story": goal.get("story", ""),
        "steps_ordered_by_user": bool(goal.get("ordered_by_user")),
        "steps": [
            {
                "step": s["step"],
                "who": s.get("who", "you"),
                "covers": s["covers"],
                "done": bool(s.get("done")),
            }
            for s in goal.get("chain", [])
        ],
    }
    return (
        f"Today is {datetime.now().strftime('%A, %B %d, %Y, %-I:%M %p')} ({config.load().timezone}).\n"
        f"The goal now:\n{json.dumps(current, ensure_ascii=False)}\n"
        + (
            f"{config.possessive()} notes on what this goal is really about (they win):\n"
            + "\n".join(f"- {n}" for n in notes)
            + "\n"
            if notes
            else ""
        )
        + f"Its tasks:\n{json.dumps(tasks, ensure_ascii=False)}\n"
        + "Meetings linked to it:\n"
        + json.dumps(
            [
                {
                    "id": e.get("id"),
                    "title": e.get("summary"),
                    "start": (e.get("start") or {}).get("dateTime")
                    or (e.get("start") or {}).get("date"),
                    "call": calendar_events.video(e),
                    "with": [
                        a.get("displayName") or a.get("email")
                        for a in e.get("attendees") or []
                    ][:10],
                }
                for e in linked
            ],
            ensure_ascii=False,
        )
        + f"\n{calendar_events.brief(ids=True)}\nThe conversations:\n"
        + "\n\n".join(blocks)
        + f"\n{extra}\nAnswer with one JSON object."
    )


def rethink(goal: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Read the goal whole and say what it is for, where it stands, and its steps."""
    convs = conversations(goal, state)
    first = ai.complete_json(
        _prompt(goal, state, convs, ""), system=system(), timeout=600
    )
    queries = [str(q) for q in first.get("search") or [] if q][:3]
    if not queries:
        return dict(first, signature=signature(goal, state, convs))
    hits = _search(queries)
    extra = (
        f"\nCandidate conversations found by searching Slack and {config.possessive()} email (link the ones about "
        "this goal by their conversation id; read what they say):\n"
        + json.dumps(hits, ensure_ascii=False)[:12000]
        + '\nThis is the second reading: answer with "search": [].\n'
    )
    second = ai.complete_json(
        _prompt(goal, state, convs, extra), system=system(), timeout=600
    )
    found = {h["conversation"] for h in hits}
    links = second.get("links") or {}
    new = [c for c in links.get("conversations") or [] if c in found and c not in convs]
    if new:
        # Read the newly linked conversations in full before settling anything.
        goal = dict(
            goal,
            links={
                "conversations": list(convs) + new,
                "events": links.get("events") or [],
            },
        )
        convs = conversations(goal, state)
        second = ai.complete_json(
            _prompt(
                goal,
                state,
                convs,
                '\nThis is the final reading: answer with "search": [].\n',
            ),
            system=system(),
            timeout=600,
        )
        second.setdefault("links", {})
        second["links"]["conversations"] = list(
            dict.fromkeys((second["links"].get("conversations") or []) + new)
        )
    return dict(second, signature=signature(goal, state, convs))


def apply(state: dict[str, Any], gid: str, result: dict[str, Any]) -> None:
    """Write a reading back onto the goal (call inside store.mutate)."""
    goal = state["goals"][gid]
    items = state["items"]
    keys = {i["key"] for i in members(goal, items)}
    open_keys = {k for k in keys if items[k]["status"] not in FINISHED}
    settled_now = set()
    for s in result.get("settled") or []:
        k = str((s or {}).get("key") or "")
        if k in open_keys and s.get("reason"):
            settled_now.add(k)
            items[k].update(
                status="cleared",
                cleared_reason=str(s["reason"])[:300],
                cleared_at=store.now_iso(),
            )
    elsewhere = {str(k) for k in result.get("not_part") or []} & keys
    for k in elsewhere:
        items[k].update(goal_id=None, placed=True)
    keys -= elsewhere
    chain, seen = [], set()
    for s in result.get("steps") or []:
        if not isinstance(s, dict) or not s.get("step"):
            continue
        covers = [k for k in s.get("covers") or [] if k in keys and k not in seen]
        seen.update(covers)
        chain.append(
            {
                "id": uuid.uuid4().hex[:8],
                "step": str(s["step"])[:140],
                "who": str(s.get("who") or "you"),
                "covers": covers,
                "done": bool(s.get("done")) and not covers,
            }
        )
    for k in sorted(keys - seen):
        if items[k]["status"] in FINISHED and k not in settled_now:
            # Finished earlier and left out of the new steps: it no longer belongs here.
            items[k]["goal_id"] = None
            continue
        # Never drop an open task (or one this reading just settled) silently.
        chain.append(
            {
                "id": uuid.uuid4().hex[:8],
                "step": items[k].get("title") or "",
                "who": "you",
                "covers": [k],
                "done": False,
            }
        )
    if goal.get("ordered_by_user"):
        chain = _keep_user_order(goal.get("chain", []), chain)
    # Keep a step's id when it is the same step, so the next step is not re-worked for nothing.
    old = {(s["step"], tuple(s["covers"])): s["id"] for s in goal.get("chain", [])}
    for s in chain:
        s["id"] = old.get((s["step"], tuple(s["covers"])), s["id"])
    links = result.get("links") or {}
    convs = [str(c) for c in links.get("conversations") or [] if c]
    events = [str(e) for e in links.get("events") or [] if e]
    goal.update(
        purpose=str(result.get("purpose") or goal.get("purpose") or "")[:500],
        done_when=str(result.get("done_when") or goal.get("done_when") or "")[:300],
        by=str(result.get("by") or ""),
        story=str(result.get("story") or "")[:600],
        chain=chain,
        links={"conversations": convs, "events": events},
        intent_sig=result.get("signature", ""),
        rethought_at=store.now_iso(),
        rethinking=False,
        dirty=False,
        updated=store.now_iso(),
    )
    if not goal.get("user_title") and result.get("title"):
        goal["title"] = str(result["title"])[:90]


def _keep_user_order(
    old: list[dict[str, Any]], new: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Steps that were there before keep the order the user gave them; a new step stays
    right after the step it followed in the reading."""
    place: list[float] = []
    for n, s in enumerate(new):
        match = next(
            (
                i
                for i, o in enumerate(old)
                if (s["covers"] and set(s["covers"]) & set(o["covers"]))
                or o["step"] == s["step"]
            ),
            None,
        )
        place.append(
            float(match)
            if match is not None
            else (place[-1] + 0.001 if place else -1.0)
        )
    return [s for _, s in sorted(zip(place, new), key=lambda pair: pair[0])]


def reorder(state: dict[str, Any], gid: str, order: list[str]) -> None:
    """The user's order for the goal's steps (step ids); steps not listed keep theirs after."""
    goal = state["goals"][gid]
    by_id = {s["id"]: s for s in goal["chain"]}
    chain = [by_id[i] for i in order if i in by_id]
    chain += [s for s in goal["chain"] if s["id"] not in set(order)]
    goal["chain"] = chain
    goal["ordered_by_user"] = True
    state.setdefault("corrections", []).append(
        {
            "kind": "order",
            "goal": gid,
            "steps": [s["step"] for s in chain],
            "at": store.now_iso(),
        }
    )


def add_note(state: dict[str, Any], gid: str, text: str) -> None:
    """The user's word on what the goal is really about; the next reading follows it."""
    goal = state["goals"][gid]
    goal.setdefault("intent_notes", []).append(
        {"text": text[:1000], "at": store.now_iso()}
    )
    goal["rethinking"] = True
    state.setdefault("corrections", []).append(
        {
            "kind": "intent",
            "goal": gid,
            "goal_title": goal.get("title"),
            "text": text[:1000],
            "at": store.now_iso(),
        }
    )


def remove_note(state: dict[str, Any], gid: str, at: str) -> None:
    goal = state["goals"][gid]
    goal["intent_notes"] = [n for n in goal.get("intent_notes") or [] if n["at"] != at]
    goal["rethinking"] = True


def sources(goal: dict[str, Any], items: dict[str, Any]) -> list[dict[str, str]]:
    """The conversations and meetings the goal is read from, each with a way back to it."""
    names = _names()
    ids = [conversation_id(i) for i in members(goal, items)]
    ids += (goal.get("links") or {}).get("conversations", [])
    out = []
    for conv in dict.fromkeys(c for c in ids if c):
        kind, _, rest = conv.partition(":")
        raw = _stored(conv, items)
        if kind == "slack":
            label = raw.get("channel_label") or "Slack"
            name = names.get(rest, "") if not label.startswith("#") else ""
            out.append(
                {
                    "label": f"Slack: {name or label}",
                    "url": f"https://slack.com/app_redirect?channel={rest}",
                }
            )
        elif kind == "email":
            account, _, thread = rest.partition(":")
            out.append(
                {
                    "label": f"Email: {raw.get('subject') or 'a thread'}",
                    "url": f"https://mail.google.com/mail/u/?authuser={account}#all/{thread}",
                }
            )
    for e in calendar_events.events((goal.get("links") or {}).get("events", [])):
        start = (
            (e.get("start") or {}).get("dateTime")
            or (e.get("start") or {}).get("date")
            or ""
        )
        try:
            when = datetime.fromisoformat(start).strftime("%a %b %-d, %-I:%M %p")
        except ValueError:
            when = start
        out.append(
            {
                "label": f"Meeting: {e.get('summary', '')}, {when}"
                + (
                    f" ({calendar_events.video(e)})" if calendar_events.video(e) else ""
                ),
                "url": e.get("htmlLink", ""),
            }
        )
    return out
