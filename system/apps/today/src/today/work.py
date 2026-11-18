"""How every AI in Today reads a conversation and writes in the user's voice: a conversation as
text (``transcript``, threads apart, the ask marked, the user's unsent drafts marked), the user's voice
samples and the user's standing rules for drafts (``voice``, ``guidance``), the user's Gmail searched and
read (``_read_mail``, ``mail_block``) and what the user sent lately (``recent_sent``, for the judge).

The old reply writer that lived here is gone (the user, 2026-11-15): whether something is owed
is the judge's to decide, and the one assistant (act.py) writes replies, ahead and on request.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from today import (
    config,
    connectors,
    learned,
    links,
    mail_store,
    store,
)

_me: dict[str, str] = {}


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %b %d %-I:%M %p")


def threads(raw: dict[str, Any], source: str = "") -> list[dict[str, Any]]:
    """A Slack conversation split into its threads, so replies from different threads are
    never read as one exchange: the thread of the task's source message first (``source``,
    store.source_message), then each other thread and
    the channel's top-level messages, each in time order. A conversation saved before
    threads were recorded (or a text chat) is one group, as saved."""
    msgs = raw.get("messages", [])
    if not any("thread_ts" in m for m in msgs):
        return [{"label": "", "messages": msgs}]
    groups: dict[str, list[dict[str, Any]]] = {}
    for m in msgs:
        groups.setdefault(str(m.get("thread_ts") or ""), []).append(m)
    for g in groups.values():
        g.sort(key=lambda m: float(m["ts"]))
    ask = next((str(m.get("thread_ts") or "") for m in msgs if store.is_source(m, source)), "")
    order = sorted(groups, key=lambda k: (k != ask, float(groups[k][0]["ts"])))
    out = []
    for k in order:
        first = groups[k][0]
        if k == ask:
            label = (
                "The thread the ask is in" if k else "The conversation, outside threads"
            )
        elif k:
            label = f"A separate thread, started by {first.get('name')} on {_fmt_time(float(first['ts']))}"
        else:
            label = "Elsewhere in the channel, outside threads"
        out.append({"label": label, "messages": groups[k]})
    return out


def transcript(raw: dict[str, Any], source: str = "") -> str:
    """The conversation as plain lines, with the task's source message marked."""
    lines = []
    if raw.get("source") in ("slack", "text"):
        lines.append(
            f"{'Slack' if raw.get('source') == 'slack' else 'Texts'}, {raw.get('channel_label', '')}"
        )
        for group in threads(raw, source):
            if group["label"]:
                lines.append(f"--- {group['label']}:")
            for m in group["messages"]:
                mark = ">> " if store.is_source(m, source) else ""
                said = links.readable(m.get("text") or "")
                if m.get("note"):
                    said = f"{said} ({m['note']})" if said else f"({m['note']})"
                lines.append(f"{mark}[{_fmt_time(float(m['ts']))}] {m.get('name')}: {said}")
    else:
        lines.append(f"Email thread ({raw.get('account')}): {raw.get('subject', '')}")
        for m in raw.get("messages", []):
            who = config.name() if m.get("from_me") else m.get("from")
            if m.get("draft"):
                who = f"{config.name()}, UNSENT DRAFT (never sent, so the other side has not seen it)"
            lines.append(
                f"--- From: {who} | To: {m.get('to')} | Cc: {m.get('cc')} | {m.get('date')}\n{m.get('text')}"
            )
    return "\n".join(lines)


def guidance(item: dict[str, Any]) -> str:
    """The user's standing rules for all drafts, and the user's notes on this item's earlier drafts."""
    state = store.load()
    rules = [r["text"] for r in state.get("draft_rules", [])]
    notes = [f["note"] for f in item.get("feedback") or []]
    not_mine = [
        c for c in state.get("corrections", []) if c.get("kind") == "not_mine"
    ][-15:]
    out = ""
    if item.get("filed_by_hand"):
        out += (
            "\nThe user pulled this one back themselves: it IS the user's to answer or do. Set filed \"\", "
            "and never write a no unless the user asked for one.\n"
        )
    if not_mine:
        out += (
            "\nThings the user marked as NOT the user's to do (learn the pattern; similar asks are likely not the user's either):\n"
            + "\n".join(
                f'- From {c.get("who", "")}: "{c.get("ask_text", "")[:200]}" (it was read as: {c.get("title", "")})'
                for c in not_mine
            )
            + "\n"
        )
    out += learned.prompt_text_now()
    if rules:
        out += (
            "\nThe user's standing rules for every draft (always follow them):\n"
            + "\n".join(f"- {r}" for r in rules)
            + "\n"
        )
    if notes:
        out += (
            "\nThe user's notes on earlier drafts for this item (still apply):\n"
            + "\n".join(f"- {n}" for n in notes)
            + "\n"
        )
    return out


# The user's voice reference: real messages the user wrote. The ``voice_file`` setting is one file
# (shareable with other agents): the user's standing rules on top, Part 1 the user's emails, Part 2
# the user's chat messages (the model for texts and DMs too). Without it, the older ``email_voice`` and
# ``chat_voice`` settings (one file each) are used; with none set, drafts use no voice examples.
VOICE: Path | None = config.load().voice_file
EMAIL_VOICE: Path | None = config.load().email_voice
CHAT_VOICE: Path | None = config.load().chat_voice
VOICE_CHARS = 40000


def voice(item: dict[str, Any]) -> str:
    email = item.get("source") == "email"
    path = VOICE or (EMAIL_VOICE if email else CHAT_VOICE)
    try:
        text = path.read_text()[:VOICE_CHARS] if path else ""
    except OSError:
        text = ""
    part = (
        ""
        if VOICE is None
        else "This is an email: follow Part 1 (the user's emails). "
        if email
        else "This is a chat message: follow Part 2 (the user's Slack messages), not the user's emails. "
    )
    out = (
        "\nHow the user actually writes: the user's own messages. Every draft and email you write must sound like "
        "the closest of these (the user's words, length, greetings, sign-off, punctuation), not like you. "
        f"{part}Any standing rules at the top apply to everything.\n<user_voice>\n"
        + text
        + "\n</user_voice>\n"
    ) if text else ""
    if item.get("source") != "slack":
        # The user's Slack samples carry Slack's emoji codes, which show as raw text anywhere else.
        out += ("Emoji codes like :slightly_smiling_face: only work in Slack. This is not Slack: write an emoji as "
                "the emoji itself (like \U0001F642), never as a code.\n")
    return out


def _slack_me() -> str:
    if "id" not in _me:
        _me["id"] = str(connectors.slack_get("auth.test", {})["user_id"])
    return _me["id"]


# Words too common to tie a message to one person.
def _slack_names() -> dict[str, str]:
    path = config.load().slack_users
    try:
        names = json.loads(path.read_text()) if path else {}
    except (OSError, ValueError):
        return {}
    return names if isinstance(names, dict) else {}


def _sent_after(account: str, since: datetime) -> list[dict[str, Any]]:
    """The user's sent mail after then (newest 100, full): the synced store's copy, else Gmail's."""
    local = mail_store.sent_since(account, since, 100)
    if local is not None:
        return local
    listed = connectors.gmail_get(
        "messages",
        account,
        {"q": f"in:sent after:{int(since.timestamp())}", "maxResults": "100"},
    )
    return [
        connectors.gmail_get(f"messages/{ref['id']}", account, {"format": "full"})
        for ref in listed.get("messages", []) or []
    ]


def recent_sent(since: datetime) -> list[dict[str, Any]]:
    """Everything the user sent since then, fetched once per update: both inboxes' sent
    mail and the user's own Slack messages."""
    out: list[dict[str, Any]] = []
    for account in config.accounts():
        try:
            for m in _sent_after(account, since):
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
                        "thread_id": m.get("threadId", ""),
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
                "query": f"from:<@{_slack_me()}> after:{(since - timedelta(days=1)).date().isoformat()}",
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
                    "channel": str(channel.get("id") or ""),
                    "date": datetime.fromtimestamp(ts).isoformat(timespec="minutes"),
                    "text": text[:400],
                    "ts": ts,
                    "blob": f"{where} {text}",
                }
            )
    except connectors.ConnectorError:
        pass
    return out


# A whole email reaches the reader: a schedule or a table cut in half reads as complete
# (the user, Nov 15). The searches together get a budget; past it the longest messages are
# shortened, each saying how much is not shown, never the JSON cut mid-way.
MAIL_CHARS = 20000
MAIL_BUDGET = 90000


def mail_block(found: list[dict[str, Any]], budget: int = MAIL_BUDGET) -> str:
    """The searches' results as JSON within ``budget`` characters."""
    out = json.dumps(found, ensure_ascii=False)
    cap = MAIL_CHARS
    while len(out) > budget and cap > 500:
        cap //= 2
        found = [dict(f, results=[dict(t, messages=[dict(m, text=_shorten(m.get("text", ""), cap))
                                                     for m in t.get("messages", [])])
                                  for t in f.get("results", [])]) for f in found]
        out = json.dumps(found, ensure_ascii=False)
    return out[:budget]


def _shorten(text: str, cap: int) -> str:
    # Already-marked text keeps its count honest: cut the words, re-mark the total left out.
    body, _, rest = text.partition("\n[... ")
    left = int(rest.split(" ", 1)[0]) if rest[:1].isdigit() else 0
    if len(body) <= cap:
        return text
    return body[:cap] + f"\n[... {len(body) - cap + left} more characters of this message not shown]"


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
                    connectors.gmail_thread(ref["id"], account), limit=MAIL_CHARS, mark_cut=True
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
                                "text": m["text"],
                            }
                            for m in msgs[-6:]
                        ],
                    }
                )
        except connectors.ConnectorError as e:
            out.append({"inbox": account, "error": str(e)[:200]})
    return out
