"""The user's Slack, read by Today itself (the user, 2026-11-17: Slack moves from the to-do app to Today).

Each refresh searches Slack for what is new since the last look: messages the user sent, direct
messages to the user, and messages that mention the user. Every conversation they are in is saved
(the channel's messages around them and the threads they are in, one row per message in
``messages``) and handed to the judge, which reads it whole and says what the user owes or waits
on (conversation_judge.py). Nothing is decided here, and nothing is written to Slack.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from today import config, connectors, conversations, ledger, store

log = logging.getLogger(__name__)

# The optional Slack users file (the slack_users setting): user id -> name. Without it, names are
# looked up with users.info and kept.
SLACK_USERS: Path | None = config.load().slack_users
# Re-search a little before the last look, in case Slack indexed a message late.
OVERLAP_SECONDS = 15 * 60
# The first look reaches back this far.
FIRST_LOOK_DAYS = 2
LEAD_UP_SECONDS = 6 * 3600  # the channel's messages before the first new one
MAX_SEARCH_PAGES = 20


def _search(query: str, since: float) -> list[dict[str, Any]]:
    """Every search hit for ``query`` newer than ``since``, newest first."""
    after = (datetime.fromtimestamp(since) - timedelta(days=1)).date().isoformat()
    hits: list[dict[str, Any]] = []
    for page in range(1, MAX_SEARCH_PAGES + 1):
        data = connectors.slack_get("search.messages", {"query": f"{query} after:{after}", "count": "100",
                                                         "page": str(page), "sort": "timestamp", "sort_dir": "desc"})
        found = data.get("messages") or {}
        matches = found.get("matches") or []
        hits += [m for m in matches if float(m["ts"]) > since]
        if not matches or float(matches[-1]["ts"]) <= since or page >= int((found.get("paging") or {}).get("pages") or 1):
            break
    return hits


def _thread_of(hit: dict[str, Any]) -> str:
    return (parse_qs(urlparse(str(hit.get("permalink") or "")).query).get("thread_ts") or [""])[0]


def names() -> dict[str, str]:
    """Slack user ids to names: the configured names file (if any), then the ones Today looked up."""
    try:
        out = json.loads(SLACK_USERS.read_text()) if SLACK_USERS else {}
    except (OSError, ValueError):
        out = {}
    if not isinstance(out, dict):
        out = {}
    return out | (store.load().get("slack_names") or {})


def _learn_names(ids: set[str], known: dict[str, str]) -> dict[str, str]:
    new = {}
    for uid in sorted(i for i in ids - set(known) if i[:1] in "UW"):
        try:
            u = connectors.slack_get("users.info", {"user": uid})["user"]
            new[uid] = str(u.get("real_name") or u.get("name") or uid)
        except (connectors.ConnectorError, KeyError):
            new[uid] = uid
    if new:
        store.mutate(lambda s: s.setdefault("slack_names", {}).update(new))
    return known | new


def shape(msgs: list[dict[str, Any]], me_id: str, who: dict[str, str]) -> list[dict[str, Any]]:
    """Slack's messages as Today saves them: who wrote it by name, mentions as names, its thread."""
    out = []
    for m in msgs:
        if m.get("type") != "message":
            continue
        uid = str(m.get("user") or m.get("bot_id") or "?")
        text = re.sub(r"<@([A-Z0-9]+)>", lambda mm: "@" + (config.name() if mm.group(1) == me_id else who.get(mm.group(1), mm.group(1))),
                      str(m.get("text") or ""))
        out.append({"ts": m["ts"], "user": uid, "name": config.name() if uid == me_id else who.get(uid, uid), "text": text,
                    "thread_ts": str(m.get("thread_ts") or "")})
    return out


def _fetch(channel: str, hits: list[dict[str, Any]], since: float) -> list[dict[str, Any]]:
    """The channel's messages from a little before the first new one, and every thread a new
    message is in or that had a reply since."""
    oldest = min(float(h["ts"]) for h in hits) - LEAD_UP_SECONDS
    top = connectors.slack_get("conversations.history", {"channel": channel, "oldest": f"{oldest:.6f}",
                                                         "inclusive": "true", "limit": "200"}).get("messages") or []
    threads = {_thread_of(h) for h in hits} - {""}
    threads |= {str(m["ts"]) for m in top if m.get("reply_count") and float(m.get("latest_reply") or 0) > since}
    msgs = {str(m["ts"]): m for m in top}
    for ts in sorted(threads):
        for m in connectors.slack_get("conversations.replies", {"channel": channel, "ts": ts, "limit": "200"}).get("messages") or []:
            msgs.setdefault(str(m["ts"]), m)
    for h in hits:  # a hit the reads above missed still counts
        msgs.setdefault(str(h["ts"]), dict(h, type="message"))
    return sorted(msgs.values(), key=lambda m: float(m["ts"]))


def run(me_id: str) -> dict[str, int]:
    """Look at what is new in the user's Slack: save each conversation it is in, and have it judged."""
    started = time.time()
    checked = store.load().get("slack_checked")
    since = float(checked) - OVERLAP_SECONDS if checked else started - FIRST_LOOK_DAYS * 86400
    hits: dict[tuple[str, str], dict[str, Any]] = {}
    for query in ("from:me", "to:me", f"<@{me_id}>"):
        for h in _search(query, since):
            hits.setdefault((str(h["channel"]["id"]), str(h["ts"])), h)
    by_channel: dict[str, list[dict[str, Any]]] = {}
    for (channel, _), h in hits.items():
        by_channel.setdefault(channel, []).append(h)
    who = names()
    saved = 0
    for channel, found in by_channel.items():
        try:
            msgs = _fetch(channel, found, since)
        except connectors.ConnectorError as e:
            log.warning("slack: could not read %s: %s", channel, e)
            continue
        who = _learn_names({str(m.get("user") or "") for m in msgs} - {me_id}, who)
        first = found[0]["channel"]
        label = ("direct message" if first.get("is_im") or channel.startswith("D")
                 else "group message" if first.get("is_mpim") else "#" + str(first.get("name") or "channel"))
        record = {"source": "slack", "channel": channel, "channel_label": label, "url": "",
                  "messages": shape(msgs, me_id, who), "fetched_at": store.now_iso()}
        conv = conversations.save_record(store.DATA_DIR, f"slack:{channel}", record)
        if conv:
            ledger.ask_judging(conv)
            saved += 1
    store.mutate(lambda s: s.update(slack_checked=started))
    return {"slack_messages": len(hits), "slack_conversations": saved}
