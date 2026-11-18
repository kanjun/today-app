"""The main page's shape: which three things Today picks for Now, and where everything
else goes (quick answers, replies ready, to-dos, this week's projects, and the quiet
folds at the bottom: other projects, waiting on others, Later, filed away).

Every message item is shown the same way everywhere: the person's name and their own
words, verbatim, plus a deadline only when a real one exists. Nothing here writes
prose of its own: the words come from the saved conversation, never from the reader.
"""

import html as htmllib
import re
from datetime import datetime, timedelta
from typing import Any

from today import config, connectors, links, store, weekly, work

NOW_N = 3
# A real deadline shows on a row only this close; further out it is just a date in the thread.
DEADLINE_DAYS = 14
# "This week": a project shows in Projects this week when something in it is due this soon
# or moved this recently.
WEEK_DAYS = 7
# Old and not due soon: it sinks into Later until it gets urgent or someone writes again.
LATER_DAYS = 14
LATER_DUE_DAYS = 7
# Filed away keeps what Today set aside on its own this long, so the user can pull it back.
FILED_DAYS = 7
# Waiting on someone: a nudge is offered only after this long.
NUDGE_DAYS = 14
# Platforms where an unanswered message request is a stranger's.
STRANGER_PLATFORMS = ("X (Twitter)",)


def _days_since(iso: str) -> float:
    try:
        return (datetime.now() - datetime.fromisoformat(str(iso)[:19])).total_seconds() / 86400
    except (TypeError, ValueError):
        return 0.0


# A due date more than this far past was a guess that time overtook: it no longer
# counts as a deadline (it would otherwise pin weeks-old items to the top as "overdue").
STALE_DUE_DAYS = 7


def due_days(item: dict[str, Any]) -> int | None:
    try:
        days = (datetime.fromisoformat(str(item.get("due") or "")).date() - datetime.now().date()).days
    except (TypeError, ValueError):
        return None
    return None if days < -STALE_DUE_DAYS else days


def deadline(item: dict[str, Any]) -> str:
    """A real deadline in plain words ("today", "by Sun", "by Nov 20"), or "" when there is none
    or it is far off."""
    days = due_days(item)
    if days is None or days > DEADLINE_DAYS:
        return ""
    if days < 0:
        return "overdue"
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    day = datetime.now() + timedelta(days=days)
    return "by " + (day.strftime("%a") if days < 7 else day.strftime("%b %-d"))


def due_spot(item: dict[str, Any]) -> str:
    """The words in a task's date spot, at the top right of its row (the user, 2026-11-15): its
    date ("Nov 29"), "today", or "overdue"; nothing when it has none. A guessed date long past
    is dropped as in ``due_days``; one the user set themselves stays overdue until the user changes it."""
    try:
        day = datetime.fromisoformat(str(item.get("due") or "")).date()
    except (TypeError, ValueError):
        return ""
    days = (day - datetime.now().date()).days
    if days < 0:
        return "overdue" if days >= -STALE_DUE_DAYS or item.get("due_set") else ""
    if days == 0:
        return "today"
    return day.strftime("%b %-d") + (day.strftime(" %Y") if days > 300 else "")


# ---- their own words ----------------------------------------------------------------


def clean_text(text: str) -> str:
    """A message's text as the user would read it: HTML and Slack markup taken out, nothing reworded."""
    text = re.sub(r"<br\s*/?>", "\n", str(text or ""), flags=re.I)
    text = re.sub(r"<a [^>]*>(.*?)</a>", r"\1", text, flags=re.I | re.S)
    text = links.readable(text, urls=False)
    text = re.sub(r"<#[A-Z0-9]+\|?([^>]*)>", r"#\1", text)
    text = re.sub(r"<!(channel|here|everyone)>", r"@\1", text)
    text = re.sub(r"</?[a-z][^>]*>", "", text, flags=re.I)
    text = htmllib.unescape(text).replace("\xa0", " ")
    text = re.sub(r":([a-z0-9_+-]+):", lambda m: _EMOJI.get(m.group(1), m.group(0)), text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_EMOJI = {"slightly_smiling_face": ":)", "smile": ":)", "heart": "<3", "sob": ":'("}


def _is_me(m: dict[str, Any]) -> bool:
    return bool(m.get("from_me") or m.get("is_me")) or m.get("name") == config.name()


def _speaker(m: dict[str, Any]) -> str:
    return str(m.get("name") or m.get("from") or m.get("user") or "")


_words_cache: dict[tuple[str, str, str], dict[str, Any]] = {}


def their_words(item: dict[str, Any]) -> dict[str, Any]:
    """What the person actually said, verbatim: the ask and the messages they sent right
    after it (an email: their newest message). ``mine`` is true when the words are
    The user's own (a promise or a follow-up the user wrote)."""
    mark = (item["key"], str(item.get("signature") or ""), str(item.get("ask_text") or "")[:60])
    if mark in _words_cache:
        return _words_cache[mark]
    out = _read_words(item)
    if len(_words_cache) > 4000:
        _words_cache.clear()
    _words_cache[mark] = out
    return out


def _read_words(item: dict[str, Any]) -> dict[str, Any]:
    fallback = re.sub(r"^In the group [^:]+: ", "", str(item.get("ask_text") or ""))
    if item.get("manual"):
        return {"words": "", "mine": True}
    if item.get("source") == "goal":
        # A move toward one of the user's weekly goals: the user's own work, in its short line.
        return {"words": str(item.get("title") or ""), "mine": True}
    if ":promise:" in item["key"]:
        # Split out of a conversation from the user's own words: the quote is what the user said.
        return {"words": clean_text(fallback), "mine": True}
    raw = store.load_raw(item["key"]) or {}
    msgs = [m for m in raw.get("messages") or [] if not m.get("draft")]
    source = store.source_message(item["key"])
    if raw.get("source") == "email" or item.get("source") == "email":
        # The email the task came from, without the history quoted under it.
        m = next((m for m in msgs if store.is_source(m, source)), None)
        if m is None or not str(m.get("text") or "").strip():
            return {"words": clean_text(fallback.split(": ", 1)[-1]), "mine": item.get("kind") in ("promised", "followup")}
        return {"words": clean_text(connectors.strip_quoted(m["text"]))[:2000], "mine": _is_me(m)}
    if raw.get("source") == "slack":
        msgs = next(iter(work.threads(raw, source)), {"messages": []})["messages"]
    start = next((n for n, m in enumerate(msgs) if store.is_source(m, source)), None)
    if start is None:
        return {"words": clean_text(fallback), "mine": item.get("kind") in ("promised", "followup")}
    who = _speaker(msgs[start])
    run = [msgs[start]]
    for m in msgs[start + 1 :]:
        if _speaker(m) != who:
            break
        run.append(m)
    words = "\n".join(clean_text(m.get("text") or "") for m in run if str(m.get("text") or "").strip())
    return {"words": (words or clean_text(fallback))[:2000], "mine": _is_me(msgs[start])}


# ---- picking Now --------------------------------------------------------------------


def priority(item: dict[str, Any]) -> float:
    """How much an item deserves one of the three Now slots; higher first.

    1. A real deadline close by: overdue or due today scores highest, then each day out
       less, up to a week.
    2. A real person waiting on the user: a point per day they have waited, up to two weeks
       (a stranger's message request on X never counts as someone waiting).
    3. Weight: it moves one of the user's projects or goals, or the reply is already written so
       one read and Send finishes it, or it needs only a quick answer from the user.
    Pitches, things the user is waiting on, and Later items are never candidates at all."""
    score = 0.0
    days = due_days(item)
    if days is not None and days <= WEEK_DAYS:
        score += 100 - 10 * max(days, 0)
    if not is_stranger(item) and not item.get("manual"):
        score += min(_days_since(item.get("asked_at", "")), 14) * 3
    if item.get("goal_id") or item.get("project"):
        score += 15
    state = item.get("state")
    if state == "ready":
        score += 12
    elif state == "you":
        score += 8
    return score


def is_stranger(item: dict[str, Any]) -> bool:
    return bool(item.get("request")) and str(item.get("platform") or "") in STRANGER_PLATFORMS


def freshness(item: dict[str, Any]) -> str:
    """When it last moved: asked, someone wrote again, or the user brought it back."""
    return max(
        str(item.get("asked_at") or ""),
        str(item.get("moved_at") or ""),
        str(item.get("kept_at") or ""),
        str(item.get("later_until") or ""),
    )


def in_later(item: dict[str, Any]) -> bool:
    """Later: the user said Not now, or it is old and not due soon, or a stranger's request.
    Something comes back up only when it gets urgent or someone writes again."""
    days = due_days(item)
    urgent = days is not None and days <= 2
    if str(item.get("later_until") or "") > datetime.now().date().isoformat():
        return True  # set aside until a day the user picked
    if item.get("not_now"):
        return not urgent and not (str(item.get("moved_at") or "") > str(item["not_now"]))
    if days is not None and days <= LATER_DUE_DAYS:
        return False
    return _days_since(freshness(item)) >= LATER_DAYS or is_stranger(item)


def kind_of(item: dict[str, Any]) -> str:
    """Which section a loose item belongs to: answer, reply, todo, or waiting."""
    state = item.get("state")
    if state == "waiting":
        return "waiting"
    if item.get("manual") or state == "mine":
        return "todo"
    if state == "you" and item.get("questions"):
        return "answer"
    if state in ("ready", "you"):
        return "reply"
    return "todo"


def platform_of(item: dict[str, Any]) -> str:
    """Where the message came from, in the user's words: Slack, Email, Signal, X (Twitter)..."""
    if item.get("platform"):
        return str(item["platform"])
    source = item.get("source")
    if source == "slack":
        return "Slack"
    if source == "email":
        return "Email"
    if source == "text":
        chat = str((item.get("ref") or {}).get("chat_id") or "")
        m = re.search(r"local-([a-z]+)\.localhost", chat)
        if m:
            return {"facebook": "Messenger", "whatsapp": "WhatsApp"}.get(
                m.group(1), m.group(1).capitalize()
            )
        return "iMessage" if chat.startswith("imsg") else "Text"
    return ""


def row(item: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """One item as every list on the page shows it."""
    words = their_words(item)
    kind = kind_of(item)
    source = item.get("source")
    out = {
        "key": item["key"],
        "who": item.get("who") or "",
        "words": words["words"],
        "mine_words": words["mine"],
        # To-dos are the user's own tasks: they keep their short title.
        "title": item.get("title") or "",
        "kind": kind,
        "when": due_spot(item),
        "due": str(item.get("due") or "")[:10],
        "status": item.get("status"),
        "state": item.get("state") or "",
        "url": item.get("url") or "",
        "source": source,
        "open_label": {"slack": "Slack", "email": "Gmail"}.get(str(source), ""),
        # Where the conversation is (X, Signal, WhatsApp...), so a text thread says where to look.
        "via": platform_of(item),
        "draft": item.get("draft") or "",
        "nudge": item.get("nudge") or "",
        "questions": item.get("questions") or [],
        "emails": len(item.get("emails") or []),
        "can_draft": source == "email",
        "manual": bool(item.get("manual")),
        "filed": item.get("filed") or "",
        "error": item.get("error") or "",
        "next_step": (item.get("next_step") or {}).get("text", ""),
        # The user's own work shows no name: a name on a row always means someone is waiting.
        "own": item.get("source") == "goal" or bool(item.get("manual")) or item.get("filed") == "remark",
        "move": item.get("move") if item.get("source") == "goal" else None,
        "goal_id": item.get("weekly_goal") or "",
        "notes": item.get("notes") or "",
        "goal": item.get("goal_text") or "",
        "placeholder": bool(item.get("placeholder")),
        # A writing move shows the user's last few lines, word for word.
        "left_off": weekly.last_lines(item["move"]["doc"]["id"])
        if item.get("source") == "goal" and (item.get("move") or {}).get("doc")
        else "",
    }
    out.update(extra)
    return out


def can_send(item: dict[str, Any]) -> bool:
    if item.get("source") == "goal":
        return (item.get("move") or {}).get("kind") == "message"
    return (
        not item.get("elsewhere")
        and not item.get("manual")
        and item.get("source") in ("slack", "email", "text")
        and not item.get("emails")
    )


# Someone else's thing is urgent enough for the one Now slot it may take when a real deadline
# is this close, or a real person has waited this long on the user.
URGENT_DUE_DAYS = 2
URGENT_WAIT_DAYS = 5


def is_urgent(item: dict[str, Any]) -> bool:
    """A real deadline within two days, or a real person waiting over five days on something
    time-sensitive (it has a date this week). A question about next Tuesday is not urgent today."""
    days = due_days(item)
    if days is not None and days <= URGENT_DUE_DAYS:
        return True
    return (
        days is not None
        and days <= WEEK_DAYS
        and not is_stranger(item)
        and not item.get("manual")
        and kind_of(item) in ("answer", "reply")
        and _days_since(item.get("asked_at", "")) > URGENT_WAIT_DAYS
    )


def build(
    loose: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    picks: list[dict[str, Any]],
    all_items: dict[str, dict[str, Any]],
    goal_moves: list[dict[str, Any]] | None = None,
    goal_of: dict[str, str] | None = None,
    out_of_now: set[str] | None = None,
) -> dict[str, Any]:
    """The page's sections. ``loose`` are open items not inside a project or goal;
    ``candidates`` adds each project's and goal's next item (so a project's next move can
    be picked for Now); ``picks`` are what the user put on Today themselves, in the user's order.

    With a weekly note (``goal_moves`` is not None) the user's goals fill Now: the next moves
    toward them in the user's goal order, with at most one truly urgent thing from someone else. ``goal_of`` maps items that serve a weekly goal to its words."""
    served: dict[str, str] = goal_of or {}
    rows = lambda items: [  # noqa: E731
        row(i, sendable=can_send(i), goal=i.get("goal_text") or served.get(i["key"], "")) for i in items
    ]
    pitches = [i for i in loose if i.get("filed") == "pitch"]
    loose = [i for i in loose if i.get("filed") != "pitch"]
    later = [i for i in loose if in_later(i) and i.get("state") != "waiting"]
    later_keys = {i["key"] for i in later}
    waiting = [i for i in loose if i.get("state") == "waiting"]
    active = [
        i
        for i in candidates
        if i["key"] not in later_keys
        and i.get("state") != "waiting"
        and i.get("filed") != "pitch"
        and not in_later(i)
    ]
    pick_keys = [i["key"] for i in picks]
    # The user sent these back to their lists from Now today (actions.out_of_now).
    sent_back = out_of_now or set()
    auto = sorted(
        (i for i in active if i["key"] not in pick_keys and i["key"] not in sent_back),
        key=lambda i: (-priority(i), str(i.get("asked_at") or "")),
    )
    if goal_moves is None:
        now = picks + auto[: max(0, NOW_N - len(picks))]
    else:
        # The user's picks, then the user's goals' next moves (a placeholder holds a goal's place while its
        # move is worked out), then at most one truly urgent thing from someone else.
        now = list(picks)
        urgent = [i for i in auto if is_urgent(i)][:1]
        if any(i.get("source") != "goal" for i in now):
            urgent = []
        room = NOW_N - len(now) - len(urgent)
        now += [m for m in goal_moves if m["key"] not in pick_keys][: max(0, room)]
        now += urgent[: max(0, NOW_N - len(now))]
        # Nothing else: Now is about the user's priorities, so it may hold fewer than three, or none
        # (the user, 2026-11-12). Everything else is right below in its own section.
    now_keys = {i["key"] for i in now}
    rest = [i for i in loose if i["key"] not in now_keys and i["key"] not in later_keys and i.get("state") != "waiting"]
    # Oldest first in every list the user works through (the user, 2026-11-13: "so I'm always doing
    # oldest stuff first"); priority still picks Now.
    rest.sort(key=lambda i: str(i.get("asked_at") or i.get("created") or ""))
    cutoff = (datetime.now() - timedelta(days=FILED_DAYS)).isoformat(timespec="seconds")
    set_aside = [
        i
        for i in all_items.values()
        if i.get("filed") in ("not_yours", "remark")
        and i["status"] in ("skipped", "cleared")
        and not i.get("finished_at")
        and str(i.get("cleared_at") or "") >= cutoff
    ]
    set_aside.sort(key=lambda i: str(i.get("cleared_at") or ""), reverse=True)
    waiting.sort(key=lambda i: str(i.get("asked_at") or ""))
    return {
        "now": [dict(r, picked=r["key"] in pick_keys) for r in rows(now)],
        "answers": rows([i for i in rest if kind_of(i) == "answer"]),
        "replies": rows([i for i in rest if kind_of(i) == "reply"]),
        "todos": rows([i for i in rest if kind_of(i) == "todo"]),
        "waiting": [
            dict(r, nudge_ready=bool(r["nudge"]) and _days_since(i.get("asked_at", "")) >= NUDGE_DAYS)
            for r, i in zip(rows(waiting), waiting)
        ],
        "later": rows(sorted(later, key=lambda i: freshness(i), reverse=True)),
        "filed": {
            "pitch": rows(pitches),
            "not_yours": rows([i for i in set_aside if i.get("filed") == "not_yours"]),
            "remark": rows([i for i in set_aside if i.get("filed") == "remark"]),
        },
    }
