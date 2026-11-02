"""Gather what the user owes from Slack and email, clear what is already handled,
and have the assistant take each new item as far as it can.

Run on a schedule (``today-sync``). Sources, both configured in ``config``
and documented in the README:
- Slack: asks and promises captured as items (one markdown file each) in the
  Slack captures directory.
- Email: threads an email digest file puts in Reply needed (1), Decision needed
  (2) or TODO (4), one digest file per inbox.
A source that is not configured, or whose file is missing, is simply skipped.
"""

import argparse
import fcntl
import json
import logging
import re
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

from today import (
    ai,
    calendar_events,
    config,
    connectors,
    goals,
    gtd_link,
    intent,
    mail_store,
    store,
    work,
)

log = logging.getLogger("today.sync")

OWED_BUCKETS = {"1", "2", "4"}
OPEN = ("open", "working")
REOPEN_DAYS = 14
WORKERS = 4


def _slack_ref(url: str) -> dict[str, str]:
    m = re.search(r"/archives/([A-Z0-9]+)/p(\d+)", url)
    if not m:
        return {}
    digits = m.group(2)
    ref = {"channel": m.group(1), "ts": f"{digits[:-6]}.{digits[-6:]}"}
    t = re.search(r"thread_ts=([\d.]+)", url)
    if t:
        ref["thread_ts"] = t.group(1)
    return ref


def _names() -> dict[str, str]:
    """Slack user id -> display name, from the configured users file ({} without one)."""
    path = config.load().slack_users
    if path is None:
        return {}
    try:
        names = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return names if isinstance(names, dict) else {}


def slack_raw(item: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    me_id = work.slack_me()
    me = config.name()
    ref = item["ref"]
    channel, ts = ref["channel"], ref["ts"]
    if ref.get("thread_ts"):
        msgs = connectors.slack_get(
            "conversations.replies",
            {"channel": channel, "ts": ref["thread_ts"], "limit": "200"},
        ).get("messages", [])
        # The rest of the conversation since: it often gets settled in a side thread.
        later = connectors.slack_get(
            "conversations.history",
            {
                "channel": channel,
                "oldest": ts,
                "inclusive": "false",
                "limit": "30",
            },
        ).get("messages", [])
        for m in reversed(later):
            if m["ts"] == ref["thread_ts"]:
                continue
            msgs.append(dict(m, in_thread=False, elsewhere=True))
            if m.get("reply_count"):
                replies = connectors.slack_get(
                    "conversations.replies",
                    {"channel": channel, "ts": m["ts"], "limit": "50"},
                ).get("messages", [])
                msgs.extend(
                    dict(r, in_thread=True, elsewhere=True) for r in replies[1:]
                )
    else:
        # The lead-up to the ask (ending with it), then the newest messages after it.
        before = connectors.slack_get(
            "conversations.history",
            {
                "channel": channel,
                "oldest": f"{float(ts) - 3 * 3600:.6f}",
                "latest": ts,
                "inclusive": "true",
                "limit": "15",
            },
        ).get("messages", [])
        after = connectors.slack_get(
            "conversations.history",
            {
                "channel": channel,
                "oldest": ts,
                "inclusive": "false",
                "limit": "50",
            },
        ).get("messages", [])
        msgs = []
        for m in list(reversed(before)) + list(reversed(after)):
            msgs.append(m)
            # Thread replies after the ask often settle it ("done, paid!").
            if m.get("reply_count") and float(m["ts"]) >= float(ts) - 3 * 3600:
                replies = connectors.slack_get(
                    "conversations.replies",
                    {"channel": channel, "ts": m["ts"], "limit": "50"},
                ).get("messages", [])
                msgs.extend(dict(r, in_thread=True) for r in replies[1:])
    label = (
        "direct message"
        if channel.startswith("D")
        else ("group message" if channel.startswith("G") else "channel")
    )
    out = []
    for m in msgs:
        if m.get("type") != "message":
            continue
        uid = str(m.get("user") or m.get("bot_id") or "?")
        name = me if uid == me_id else names.get(uid, uid)
        text = re.sub(
            r"<@([A-Z0-9]+)>",
            lambda mm: "@"
            + (me if mm.group(1) == me_id else names.get(mm.group(1), mm.group(1))),
            str(m.get("text") or ""),
        )
        out.append(
            {
                "ts": m["ts"],
                "user": uid,
                "name": name,
                "text": text,
                "is_ask": m["ts"] == ts,
                "in_thread": bool(m.get("in_thread")),
                # Which thread it is in ("" for a top-level message with no replies).
                "thread_ts": str(m.get("thread_ts") or ""),
                "later_in_conversation": bool(m.get("elsewhere")),
            }
        )
    return {
        "source": "slack",
        "channel": channel,
        "channel_label": label,
        "url": item["url"],
        "messages": out,
        "fetched_at": store.now_iso(),
    }


def email_raw(item: dict[str, Any]) -> dict[str, Any]:
    thread = connectors.gmail_thread(item["ref"]["thread_id"], item["account"])
    msgs = connectors.thread_messages(thread)
    return {
        "source": "email",
        "account": item["account"],
        "subject": msgs[0]["subject"] if msgs else "",
        "messages": msgs,
        "url": item["url"],
        "fetched_at": store.now_iso(),
        "drafts_marked": True,
    }


def signature(raw: dict[str, Any]) -> str:
    msgs = raw.get("messages", [])
    if not msgs:
        return "0"
    last = msgs[-1]
    sig = f"{len(msgs)}:{last.get('ts') or last.get('id')}"
    # A draft being sent (or thrown away) is news even when the thread looks the same.
    drafts = sum(1 for m in msgs if m.get("draft"))
    return f"{sig}:d{drafts}" if drafts else sig


def collect() -> list[dict[str, Any]]:
    """Candidate items from both sources (no network)."""
    cands = []
    for cap in gtd_link.slack_captures():
        ref = _slack_ref(cap["url"])
        if not ref:
            continue
        cands.append(
            {
                "key": f"slack:{ref['channel']}:{ref['ts']}",
                "source": "slack",
                "url": cap["url"],
                "ref": ref,
                "who": cap["person"],
                "kind": cap["kind"],
                "ask_text": cap["quote"] or cap["title"],
                "asked_at": datetime.fromtimestamp(float(ref["ts"])).isoformat(
                    timespec="seconds"
                ),
                "title": cap["title"],
                "gtd_id": cap["gtd_id"],
                "gtd_status": cap["status"],
            }
        )
    for account, path in config.load().email_digests:
        try:
            messages = json.loads(path.read_text()).get("messages", [])
        except (OSError, ValueError):
            continue
        threads: dict[str, dict[str, Any]] = {}
        for m in messages:
            if str(m.get("final_bucket")) in OWED_BUCKETS:
                threads.setdefault(m["threadId"], m)
        for tid, m in threads.items():
            name = parseaddr(m.get("from", ""))[0] or m.get("from_addr", "")
            try:
                asked = (
                    parsedate_to_datetime(m.get("date", ""))
                    .astimezone()
                    .replace(tzinfo=None)
                    .isoformat(timespec="seconds")
                )
            except (TypeError, ValueError):
                asked = store.now_iso()
            cands.append(
                {
                    "key": f"email:{account}:{tid}",
                    "source": "email",
                    "account": account,
                    "url": f"https://mail.google.com/mail/u/{account}/#all/{tid}",
                    "ref": {"thread_id": tid},
                    "who": name.split()[0] if name else "",
                    "kind": "asked",
                    "ask_text": f"{m.get('subject', '')}: {m.get('snippet', '')}",
                    "asked_at": asked,
                    "title": m.get("subject", ""),
                    "gtd_id": None,
                    "gtd_status": None,
                }
            )
    return cands


# Cleared by the user (or by a rule they own) rather than by Today's own reading.
USER_CLEARS = ("marked done in your to-do list",)


def cleared_by_today(item: dict[str, Any]) -> bool:
    """Today decided this needed nothing from the user: new messages in the conversation
    can change that, so it is read again. Never something they finished, said no to, or a
    copy."""
    return (
        item["status"] in ("cleared", "skipped")
        and not item.get("finished_at")
        and not item.get("no")
        and not item.get("dup_of")
        and item.get("kind") != "followup"
        and item.get("source") in ("email", "slack")
        and item.get("cleared_reason") not in USER_CLEARS
    )


def _apply(key: str, fields: dict[str, Any]) -> None:
    def change(state: dict[str, Any]) -> None:
        item = state["items"].get(key)
        if item is None:
            return
        if (
            item["status"] in goals.FINISHED
            and fields.get("status", item["status"]) not in goals.FINISHED
            and not cleared_by_today(item)
        ):
            # The user finished it while it was being worked on: their call stands.
            return
        # The user put it in a project by hand: a later read never moves it.
        # The same for a title or due date set by hand.
        keep = ("project",) if item.get("project_by_hand") else ()
        keep += ("title",) if item.get("title_by_hand") else ()
        keep += ("due",) if item.get("due_by_hand") else ()
        item.update({k: v for k, v in fields.items() if k not in keep})
        item["updated_at"] = store.now_iso()

    store.mutate(change)


REFETCH_HOURS = 6


def gmail_changes(state: dict[str, Any]) -> dict[str, set[str] | None]:
    """Per inbox, the threads with new messages since the last update (None when that
    is unknown, so every thread is fetched). Saves where to start next time. Read from
    the synced mail store's change feed when it is current, else from Gmail's history."""
    marks = dict(state.get("gmail_history") or {})
    seqs = dict(state.get("gmail_store_seq") or {})
    out: dict[str, set[str] | None] = {}
    for account, _ in config.load().email_digests:
        local = mail_store.new_threads(account, seqs.get(account))
        if local is not None:
            out[account], seqs[account] = local
            continue
        # Gmail's history: the store's position no longer says where this update left off.
        seqs.pop(account, None)
        start = marks.get(account)
        try:
            if not start:
                out[account] = None
                marks[account] = connectors.gmail_get("profile", account)["historyId"]
                continue
            threads: set[str] = set()
            params = {
                "startHistoryId": start,
                "historyTypes": "messageAdded",
                "maxResults": "500",
            }
            for _ in range(20):  # 500 changes a page; more than that means fetch all
                page = connectors.gmail_get("history", account, params)
                for h in page.get("history", []) or []:
                    for m in h.get("messagesAdded", []) or []:
                        threads.add(m["message"]["threadId"])
                if not page.get("nextPageToken"):
                    break
                params["pageToken"] = page["nextPageToken"]
            else:
                raise connectors.ConnectorError("too many changes to list")
            out[account] = threads
            marks[account] = page.get("historyId") or start
        except (connectors.ConnectorError, KeyError) as e:
            # An expired starting point (or any failure): fetch everything and start over.
            log.warning("gmail history for %s: %s", account, e)
            out[account] = None
            marks.pop(account, None)
    store.mutate(lambda st: st.update(gmail_history=marks, gmail_store_seq=seqs))
    return out


def slack_fingerprint(item: dict[str, Any]) -> str:
    """Cheap sign of whether an item's Slack conversation moved: the newest messages
    after the ask and the newest reply in each thread (one or two calls)."""
    ref = item["ref"]
    msgs = connectors.slack_get(
        "conversations.history",
        {
            "channel": ref["channel"],
            "oldest": f"{float(ref['ts']) - 3 * 3600:.6f}",
            "limit": "100",
        },
    ).get("messages", [])
    if ref.get("thread_ts"):
        msgs += connectors.slack_get(
            "conversations.replies",
            {"channel": ref["channel"], "ts": ref["thread_ts"], "limit": "1"},
        ).get("messages", [])[:1]
    return ",".join(
        sorted(
            f"{m['ts']}/{m.get('reply_count', 0)}/{m.get('latest_reply', '')}"
            for m in msgs
        )
    )


def _unchanged(
    item: dict[str, Any], changes: dict[str, set[str] | None] | None
) -> bool:
    """True when the saved copy of the conversation is still current, so it need not be
    downloaded again (and it was downloaded within the last few hours, as a backstop)."""
    raw = store.load_raw(item["key"])
    if not raw or not raw.get("fetched_at"):
        return False
    fresh = (datetime.now() - timedelta(hours=REFETCH_HOURS)).isoformat(
        timespec="seconds"
    )
    if raw["fetched_at"] < fresh:
        return False
    if raw.get("source") == "email" and not raw.get("drafts_marked"):
        return False
    if item["source"] == "email" and changes is not None:
        seen = changes.get(item.get("account", ""))
        return seen is not None and item["ref"].get("thread_id") not in seen
    if item["source"] == "slack" and item.get("slack_fp"):
        return slack_fingerprint(item) == item["slack_fp"]
    return False


class SentLog:
    """What the user sent since the oldest item's last check, fetched at most once per update."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        floor = datetime.now() - timedelta(hours=26)
        times = [
            datetime.fromisoformat(str(i.get("done_checked_at") or i.get("worked_at")))
            for i in items
            if i["status"] in OPEN
            and i.get("done_check")
            and (i.get("done_checked_at") or i.get("worked_at"))
        ]
        self.since = max(min(times, default=floor), floor) - timedelta(minutes=10)
        self._lock = threading.Lock()
        self._sent: list[dict[str, Any]] | None = None

    def get(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._sent is None:
                self._sent = work.recent_sent(self.since)
            return self._sent


def _work_one(
    key: str,
    names: dict[str, str],
    *,
    recheck: bool,
    sent_log: "SentLog | None" = None,
    changes: dict[str, set[str] | None] | None = None,
) -> None:
    item = store.load()["items"][key]
    try:
        if recheck and _unchanged(item, changes):
            raw = store.load_raw(key) or {}
        else:
            raw = (
                slack_raw(item, names) if item["source"] == "slack" else email_raw(item)
            )
            if item["source"] == "slack":
                _apply(key, {"slack_fp": slack_fingerprint(item)})
        store.save_raw(key, raw)
        sig = signature(raw)
        if item["status"] in goals.FINISHED and sig == item.get("signature"):
            return  # cleared earlier and nothing new has been said since
        if recheck and sig == item.get("signature"):
            # Nothing new in the conversation; every couple of hours, look again at
            # what the user has sent elsewhere since (an answer by email to a Slack ask).
            last = item.get("done_checked_at") or item.get("worked_at") or ""
            if not item.get("done_check") or last > (
                datetime.now() - timedelta(hours=2)
            ).isoformat(timespec="seconds"):
                return
            checked = datetime.fromisoformat(last).timestamp() if last else 0.0
            sent = (
                work.sent_matching(sent_log.get(), item["done_check"], checked)
                if sent_log
                else work.sent_since(item, item["done_check"])
            )
            seen = {json.dumps(h, sort_keys=True) for h in item.get("sent_found") or []}
            _apply(key, {"done_checked_at": store.now_iso()})
            if not [
                h for h in sent if json.dumps(h, sort_keys=True) not in seen
            ] or item.get("answers"):
                return
        # Their writing back is not proof it is done (they may have asked something back):
        # the assistant reads the reply and decides; "waiting" when the next move is theirs.
        if recheck and item.get("answers"):
            _apply(key, {"signature": sig})
            return
        _apply(key, {"status": "working"} if not recheck else {})
        _apply_result(key, work.take_it_further(item, raw, _context_for(item)), sig)
    except (
        connectors.ConnectorError,
        ai.ClaudeError,
        OSError,
        ValueError,
        KeyError,
        subprocess.TimeoutExpired,
    ) as e:
        log.warning("item %s failed: %s", key, e)
        _apply(
            key,
            {"status": "open" if item.get("state") else "error", "error": str(e)[:300]},
        )


def _context_for(item: dict[str, Any]) -> str:
    state = store.load()
    goal = state.get("goals", {}).get(item.get("goal_id") or "")
    return (
        goals.goal_context(goal, state["items"])
        if goal and goal["status"] == "open"
        else ""
    )


def _apply_result(key: str, result: dict[str, Any], sig: str) -> None:
    fields: dict[str, Any] = {
        "signature": sig,
        "error": None,
        "worked_at": store.now_iso(),
    }
    if not result["owed"]:
        fields.update(
            {
                "status": "skipped",
                "cleared_reason": result["resolved_reason"] or "nothing for you to do",
                "cleared_at": store.now_iso(),
            }
        )
    elif result["resolved"]:
        fields.update(
            {
                "status": "cleared",
                "cleared_reason": result["resolved_reason"] or "already handled",
                "cleared_at": store.now_iso(),
            }
        )
    else:
        fields.update(
            {
                k: result[k]
                for k in (
                    "title",
                    "state",
                    "did",
                    "draft",
                    "elsewhere",
                    "to",
                    "questions",
                )
            }
        )
        if result["who"]:
            fields["who"] = result["who"]
        if result.get("searched"):
            fields["searched"] = result["searched"]
        fields["due"] = result.get("due", "")
        fields["project"] = result.get("project", "")
        fields["done_check"] = result.get("done_check", [])
        fields["sent_found"] = result.get("sent_found", [])
        fields["done_checked_at"] = store.now_iso()
        fields["status"] = "open"
        fields["emails"] = result.get("emails", [])
        fields["calendar"] = _keep_added(key, result.get("calendar", []))
    _apply(key, fields)
    _add_promises(key, result.get("promises") or [])


def _keep_added(key: str, proposed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Events already put on the user's calendar stay listed; new proposals follow, minus repeats."""
    item = store.load()["items"].get(key) or {}
    added = [e for e in item.get("calendar") or [] if e.get("added")]
    seen = {(e["title"], e["start"]) for e in added}
    return added + [e for e in proposed if (e["title"], e["start"]) not in seen]


def _add_promises(key: str, promises: list[dict[str, str]]) -> None:
    """Each thing the user promised in the conversation becomes its own item, worked right away.

    One they asked someone else to do ("asked" names them) is theirs: it waits on them."""
    parent = store.load()["items"][key]
    raw = store.load_raw(key)
    if not promises or not raw or parent.get("kind") in ("promised", "handed"):
        return

    def change(state: dict[str, Any]) -> list[str]:
        parent = state["items"][key]
        seen = parent.setdefault("promise_quotes", [])
        added = []
        for p in promises:
            if p["quote"] in seen:
                continue
            seen.append(p["quote"])
            new_key = f"{key}:promise:{len(seen)}"
            state["items"][new_key] = {
                k: parent.get(k)
                for k in (
                    "source",
                    "account",
                    "url",
                    "ref",
                    "who",
                    "asked_at",
                    "ask_id",
                    "gtd_id",
                )
            } | {
                "key": new_key,
                "kind": "handed" if p.get("asked") else "promised",
                "title": p["title"],
                "ask_text": p["quote"] or p["title"],
                "status": "working",
                "created": store.now_iso(),
                "gtd_status": None,
            }
            if p.get("asked"):
                state["items"][new_key]["who"] = p["asked"]
            added.append(new_key)
        return added

    for new_key in store.mutate(change):
        store.save_raw(new_key, raw)
        item = store.load()["items"][new_key]
        try:
            _apply_result(new_key, work.take_it_further(item, raw), signature(raw))
        except (
            ai.ClaudeError,
            OSError,
            ValueError,
            KeyError,
            subprocess.TimeoutExpired,
        ) as e:
            _apply(new_key, {"status": "error", "error": str(e)[:300]})


def _rework(key: str) -> None:
    """Work an item again now that it is a goal's next step (earlier steps may be done)."""
    item = store.load()["items"].get(key)
    raw = store.load_raw(key)
    if not item or not raw or item["status"] != "open" or item.get("answers"):
        return
    try:
        _apply_result(
            key,
            work.take_it_further(item, raw, _context_for(item)),
            item.get("signature", ""),
        )
    except (
        connectors.ConnectorError,
        ai.ClaudeError,
        OSError,
        ValueError,
        KeyError,
        subprocess.TimeoutExpired,
    ) as e:
        log.warning("rework %s failed: %s", key, e)


def reread_goal(gid: str, force: bool = False) -> bool:
    """Read a goal whole again when anything it draws on changed (or when forced)."""
    state = store.load()
    goal = state["goals"].get(gid)
    if not goal or goal["status"] != "open":
        return False
    try:
        if not force:
            convs = intent.conversations(goal, state)
            if intent.signature(goal, state, convs) == goal.get("intent_sig"):
                return False
        result = intent.rethink(goal, state)
    except (
        ai.ClaudeError,
        connectors.ConnectorError,
        OSError,
        ValueError,
        KeyError,
        subprocess.TimeoutExpired,
    ) as e:
        log.warning("goal %s: reading failed: %s", gid, e)
        store.mutate(lambda s: s["goals"][gid].update(rethinking=False))
        return False

    def change(s: dict[str, Any]) -> None:
        if s["goals"].get(gid, {}).get("status") == "open":
            intent.apply(s, gid, result)

    store.mutate(change)
    return True


def goals_phase(consolidate_now: bool = False) -> dict[str, int]:
    """Place new items in goals, re-read the goals whose conversations, meetings or notes
    changed, close finished goals, and get each goal's next step as far as it can go."""
    counts = {"assigned": 0, "rewritten": 0, "reworked": 0}
    dups = goals.duplicates(store.load())
    deduped = store.mutate(lambda s: goals.apply_duplicates(s, dups)) if dups else []
    counts["duplicates"] = sum(len(d.get("drop", [])) for d in dups)
    state = store.load()
    touched: list[str] = list(deduped)
    if consolidate_now or not state.get("goals_consolidated"):
        result = goals.consolidate(state)
        store.mutate(lambda s: goals.apply_consolidation(s, result))
        touched += [
            gid
            for gid, g in store.load()["goals"].items()
            if g.get("dirty") and g["status"] == "open"
        ]
    else:
        new_keys = [i["key"] for i in goals.open_items(state) if not i.get("placed")]
        if new_keys:
            assignment = goals.assign(state, new_keys)
            touched += store.mutate(
                lambda s: goals.apply_assignment(s, assignment, new_keys)
            )
            counts["assigned"] = len(new_keys)
    # Every goal is read whole: again when it was just regrouped, else only when a
    # conversation, meeting or note it draws on changed.
    state = store.load()
    regrouped = set(touched)
    ids = [gid for gid, g in state["goals"].items() if g["status"] == "open"]
    # One at a time: each reading is a large prompt, and memory here is shared.
    counts["reread"] = sum(reread_goal(gid, gid in regrouped) for gid in ids)
    store.mutate(goals.close_finished)
    state = store.load()
    todo: list[str] = []
    for gid, goal in state["goals"].items():
        if goal["status"] != "open":
            continue
        p = goals.progress(goal, state["items"])
        if p["next_index"] is None:
            continue
        step = goal["chain"][p["next_index"]]
        if step["id"] != goal.get("next_seen"):
            todo += step["covers"]
            store.mutate(
                lambda s, gid=gid, sid=step["id"]: s["goals"][gid].update(next_seen=sid)
            )
    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(_rework, todo))
    counts["reworked"] = len(todo)
    return counts


def run(limit: int | None = None, consolidate_now: bool = False) -> dict[str, int]:
    # Mail is read from the synced store; bring it up to the minute first (a few Gmail calls).
    mail_store.catch_up(tuple(account for account, _ in config.load().email_digests))
    calendar_events.refresh()
    names = _names()
    cands = collect()
    counts = {"new": 0, "recheck": 0, "cleared_by_gtd": 0}

    def merge(state: dict[str, Any]) -> tuple[list[str], list[str]]:
        store.roll_today(state, datetime.now().date().isoformat())
        new, recheck = [], []
        for c in cands:
            item = state["items"].get(c["key"])
            if item is None:
                if c["gtd_status"] in ("done", "trashed"):
                    continue
                item = dict(c, status="new", created=store.now_iso())
                state["items"][c["key"]] = item
                new.append(c["key"])
                continue
            item["gtd_status"] = c["gtd_status"]
            if item["status"] in OPEN and c["gtd_status"] in ("done", "trashed"):
                item.update(
                    status="cleared",
                    cleared_reason="marked done in your to-do list",
                    cleared_at=store.now_iso(),
                )
                counts["cleared_by_gtd"] += 1
            elif item["status"] in ("new", "error") or (
                item["status"] == "working" and not item.get("state")
            ):
                new.append(c["key"])
            elif item["status"] in OPEN or cleared_by_today(item):
                recheck.append(c["key"])
        return new, recheck

    new, recheck = store.mutate(merge)
    sent_log = SentLog(list(store.load()["items"].values()))
    changes = gmail_changes(store.load())
    if limit is not None:
        new = new[:limit]
    counts["new"], counts["recheck"] = len(new), len(recheck)
    with ThreadPoolExecutor(WORKERS) as pool:
        list(
            pool.map(
                lambda k: _work_one(k, names, recheck=False),
                new,
            )
        )
        list(
            pool.map(
                lambda k: _work_one(
                    k,
                    names,
                    recheck=True,
                    sent_log=sent_log,
                    changes=changes,
                ),
                recheck,
            )
        )

    # Every other open item is watched too, not just the ones the inbox digests or the
    # Slack captures still list: promises, a thread that aged out of the digest, a
    # conversation the user replied to. Otherwise an answer elsewhere never clears them.
    def leftovers(state: dict[str, Any]) -> list[str]:
        listed = set(new) | set(recheck)
        recent = (datetime.now() - timedelta(days=REOPEN_DAYS)).isoformat(
            timespec="seconds"
        )
        return [
            k
            for k, i in state["items"].items()
            if i["source"] in ("email", "slack")
            and k not in listed
            and (
                i["status"] in (*OPEN, "new", "error")
                # Email is cheap to look at again (unchanged threads are skipped),
                # so a recent clear of Today's own is watched for new messages.
                or (
                    i["source"] == "email"
                    and cleared_by_today(i)
                    and (i.get("cleared_at") or "") >= recent
                )
            )
        ]

    rest = store.mutate(leftovers)
    counts["watched"] = len(rest)
    with ThreadPoolExecutor(WORKERS) as pool:
        list(
            pool.map(
                lambda k: _work_one(
                    k,
                    names,
                    # Never worked yet, or failed last time: a full first pass.
                    recheck=store.load()["items"][k]["status"] in OPEN
                    or cleared_by_today(store.load()["items"][k]),
                    sent_log=sent_log,
                    changes=changes,
                ),
                rest,
            )
        )

    counts["gtd_closed"] = gtd_link.close_finished(
        store.load()["items"], goals.FINISHED
    )
    try:
        counts.update(goals_phase(consolidate_now))
    except (
        ai.ClaudeError,
        OSError,
        ValueError,
        KeyError,
        subprocess.TimeoutExpired,
    ) as e:
        log.warning("goals pass failed: %s", e)

    def done(state: dict[str, Any]) -> None:
        state["last_sync"] = store.now_iso()
        state["sync_error"] = None

    store.mutate(done)
    return counts


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Work at most this many new items (for trying it out).",
    )
    parser.add_argument(
        "--consolidate",
        action="store_true",
        help="Also run the full goals pass (the morning cleanup).",
    )
    args = parser.parse_args()
    started = time.time()
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # The 30-minute update and the morning cleanup take turns rather than both working the same items.
    lock = open(store.DATA_DIR / ".sync.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        counts = run(args.limit, args.consolidate)
    except (
        connectors.ConnectorError,
        ai.ClaudeError,
        OSError,
        ValueError,
        KeyError,
        subprocess.TimeoutExpired,
    ) as e:
        traceback.print_exc()
        message = str(e)[:300]
        store.mutate(lambda s: s.update(sync_error=message))
        sys.exit(1)
    log.info("sync done in %.0fs: %s", time.time() - started, counts)


if __name__ == "__main__":
    main()
