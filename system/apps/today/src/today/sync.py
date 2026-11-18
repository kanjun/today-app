"""Gather what the user owes from Slack and email, clear what is already handled,
and have the assistant take each new item as far as it can.

Run on a schedule (``today-sync``). Sources:
- Slack: read by Today itself (slack_watch.py): every conversation with something new from
  the user, to the user or mentioning the user is saved and judged whole.
- Email: threads an email digest file puts in Reply needed (1), Decision needed
  (2) or TODO (4), one digest file per inbox (the ``email_digests`` setting, documented
  in the README). A digest that is not configured, or whose file is missing, is skipped;
  without Slack connected, the Slack part is skipped too.
"""

import argparse
import fcntl
import json
import logging
import re
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

from today import (
    act,
    ai,
    attached,
    calendar_events,
    config,
    connectors,
    conversations,
    ledger,
    mail_store,
    slack_watch,
    store,
    weekly,
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
    me_id = work._slack_me()
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
                # Which thread it is in ("" for a top-level message with no replies).
                "thread_ts": str(m.get("thread_ts") or ""),
            }
        )
    return {
        "source": "slack",
        "channel": channel,
        "channel_label": label,
        # An item made without a link (some promises) gets Slack's own link to the ask.
        "url": item.get("url")
        or connectors.slack_permalink(channel, str(item["ref"].get("ts") or "")),
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
        "url": item.get("url") or "",
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
                    # The email the ask is in: the task's source message.
                    "ref": {"thread_id": tid, "message_id": str(m.get("id") or "")},
                    "who": name.split()[0] if name else "",
                    "kind": "asked",
                    "ask_text": f"{m.get('subject', '')}: {m.get('snippet', '')}",
                    "asked_at": asked,
                    "title": m.get("subject", ""),
                }
            )
    return cands


# Cleared by the user (or by a rule they own) rather than by Today's own reading.
USER_CLEARS = ("marked done in your to-do list",)


FOLD_FIELDS = ("ref", "url", "ask_id", "ask_text")


def _conversation_item(state: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any] | None:
    """The item a new message in this conversation belongs to: its newest one going the
    same way (the user's waiting on them, or them on the user), if Today is still watching it. A new
    message never makes a second item while one is open or only cleared by Today; only
    after the user finished the last one does a new round get its own."""
    conv = conversations.conversation_id(cand)
    if not conv:
        return None
    follow = cand.get("kind") == "followup"
    same = [
        i for i in state["items"].values()
        if conversations.conversation_id(i) == conv
        and (i.get("kind") == "followup") == follow
        and i.get("kind") not in ("promised", "handed")
    ]
    if not same:
        return None
    newest = max(same, key=lambda i: str(i.get("created") or i.get("asked_at") or ""))
    return newest if newest["status"] in (*OPEN, "new", "error") or cleared_by_today(newest) else None


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
            item["status"] in store.FINISHED
            and fields.get("status", item["status"]) not in store.FINISHED
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


def _work_one(
    key: str,
    names: dict[str, str],
    *,
    recheck: bool,
    changes: dict[str, set[str] | None] | None = None,
) -> None:
    item = store.load()["items"][key]
    unchanged = False  # the saved copy is current: nothing to write back
    try:
        if recheck and _unchanged(item, changes):
            raw = store.load_raw(key) or {}
            unchanged = True
        else:
            raw = (
                slack_raw(item, names) if item["source"] == "slack" else email_raw(item)
            )
            if item["source"] == "slack":
                _apply(key, {"slack_fp": slack_fingerprint(item)})
        if not unchanged:
            store.save_raw(key, raw)
        sig = signature(raw)
        if item["status"] in store.FINISHED and sig == item.get("signature"):
            return  # cleared earlier and nothing new has been said since
        if recheck and sig == item.get("signature"):
            # Nothing new in the conversation. Whether it got done elsewhere (an answer by
            # email to a Slack ask, a meeting on the user's calendar) is the judge's call now.
            return
        # Their writing back is not proof it is done (they may have asked something back):
        # the assistant reads the reply and decides; "waiting" when the next move is theirs.
        if recheck and item.get("answers"):
            _apply(key, {"signature": sig})
            return
        _hand_to_judge(key, item, sig)
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


def _hand_to_judge(key: str, item: dict[str, Any], sig: str) -> None:
    """Something new in an item's conversation: whether it is a real ask of the user's, which list
    it belongs in and whether it is done are the judge's to decide (the user, 2026-11-14), and the
    reply is written ahead by the one assistant (2026-11-15: the old reply writer is gone). A
    new item becomes a task at once, marked unread, and its conversation is judged this refresh."""
    fields: dict[str, Any] = {"signature": sig, "error": None, "handed_at": store.now_iso()}
    if item.get("signature") and sig and sig != item["signature"]:
        # Someone wrote again: an item resting in Later comes back up.
        fields["moved_at"] = store.now_iso()
    if item["status"] in ("new", "error", "working"):
        fields.update(status="open", for_judge=True)
    _apply(key, fields)
    ledger.ask_judging(conversations.conversation_id(item))


def run(limit: int | None = None) -> dict[str, int]:
    # Mail is read from the synced store; bring it up to the minute first (a few Gmail calls).
    mail_store.catch_up(config.accounts())
    calendar_events.refresh()
    names = _names()
    cands = collect()
    # Email threads and Slack conversations attached to a task: kept fresh.
    attached.refresh()
    counts = {"new": 0, "recheck": 0}
    try:
        # Without Slack connected, this is skipped and email still works.
        counts.update(slack_watch.run(work._slack_me()))
    except (connectors.ConnectorError, KeyError) as e:
        log.warning("slack unavailable: %s", e)

    def merge(state: dict[str, Any]) -> tuple[list[str], list[str]]:
        store.roll_today(state, datetime.now().date().isoformat())
        new, recheck = [], []
        seen = state.setdefault("seen", {})
        for c in cands:
            if c["key"] in seen:
                # Read before: folded into its conversation's item, or deleted as a copy.
                continue
            item = state["items"].get(c["key"])
            if item is None:
                home = _conversation_item(state, c)
                if home is not None:
                    home.update({f: c[f] for f in FOLD_FIELDS if f in c})
                    if home["status"] not in OPEN:
                        home["asked_at"] = c["asked_at"]
                    seen[c["key"]] = home["key"]
                    recheck.append(home["key"])
                    continue
                item = dict(c, status="new", created=store.now_iso())
                state["items"][c["key"]] = item
                new.append(c["key"])
                continue
            if item["status"] in ("new", "error") or (
                item["status"] == "working" and not item.get("state")
            ):
                new.append(c["key"])
            elif item["status"] in OPEN or cleared_by_today(item):
                recheck.append(c["key"])
        recheck = [k for k in dict.fromkeys(recheck) if k not in new]
        return new, recheck

    new, recheck = store.mutate(merge)
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
                    changes=changes,
                ),
                recheck,
            )
        )

    # Every other open item is watched too, not just the ones the inbox digests still list:
    # promises, a thread that aged out of the digest, a conversation the user replied to.
    # Otherwise the user's answer elsewhere never clears them.
    def leftovers(state: dict[str, Any]) -> list[tuple[str, bool]]:
        listed = set(new) | set(recheck)
        recent = (datetime.now() - timedelta(days=REOPEN_DAYS)).isoformat(
            timespec="seconds"
        )
        return [
            # Never worked yet, or failed last time: a full first pass (recheck False).
            (k, i["status"] in OPEN or cleared_by_today(i))
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
                lambda kr: _work_one(
                    kr[0],
                    names,
                    recheck=kr[1],
                    changes=changes,
                ),
                rest,
            )
        )

    try:
        # A weekly goal with no live next move gets one (after a move was done elsewhere).
        counts["goal_moves"] = weekly.plan()
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        log.warning("weekly moves failed: %s", e)

    def done(state: dict[str, Any]) -> None:
        state["last_sync"] = store.now_iso()
        state["sync_error"] = None

    store.mutate(done)
    # The lists come from commitments now: new items get one, and conversations with new
    # messages are judged whole.
    counts.update({f"ledger_{k}": v for k, v in ledger.refresh().items() if isinstance(v, int)})
    # Then the one assistant gets the cards with something new ready (the reply, a nudge, an
    # intro, an event to offer, the first options), now that the judge has said what is owed.
    counts.update({f"ahead_{k}": v for k, v in act.write_ahead_due().items()})
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
    args = parser.parse_args()
    started = time.time()
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Two updates never work the same items at once.
    lock = open(store.DATA_DIR / ".sync.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        counts = run(args.limit)
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
