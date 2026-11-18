"""The lighter Today: Today picks Now by priority, every item shows the person's own words,
stray items are filed at the source, and nothing is sent without the user's reading it first."""

import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from today import (
    actions,
    connectors,
    page,
    runner,
    store,
    work,
)


def _ago(days: float) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def _in(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _item(key: str, **extra: Any) -> dict[str, Any]:
    base = {
        "key": key,
        "status": "open",
        "state": "ready",
        "source": "slack",
        "who": key.title(),
        "asked_at": _ago(1),
        "title": f"Reply to {key}",
        "ask_text": f"hi from {key}",
    }
    return base | extra


# ---- the reader files strays at the source ------------------------------------------


def test_a_pulled_back_item_is_never_filed_away_again(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {
        "items": {"a": _item("a", filed="remark", status="skipped", state="mine")},
        "corrections": [],
        "today": {"order": [], "date": ""},
    }
    monkeypatch.setattr(store, "mutate", lambda fn: fn(state))
    monkeypatch.setattr(store, "load", lambda: state)
    monkeypatch.setattr(store, "load_raw", lambda key: None)
    assert actions.pull_back("a") == "Moved to your list"
    item = state["items"]["a"]
    assert item["status"] == "open" and item["filed"] == "" and item["filed_by_hand"]
    assert state["corrections"][-1]["kind"] == "pulled_back"
    assert "pulled this one back themselves" in work.guidance(item)

def test_their_words_are_the_ask_and_what_they_sent_right_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {
        "source": "slack",
        "messages": [
            {"ts": "1", "name": "the user", "text": "earlier", "thread_ts": ""},
            {"ts": "2", "name": "Robin", "text": "do you want me to avoid <b>Tuesday</b>?", "thread_ts": ""},
            {"ts": "3", "name": "Robin", "text": "like the fireside :slightly_smiling_face:", "thread_ts": ""},
            {"ts": "4", "name": "the user", "text": "hmm", "thread_ts": ""},
        ],
    }
    monkeypatch.setattr(store, "load_raw", lambda key: raw)
    monkeypatch.setattr(store, "source_message", lambda key: "2")
    w = page.their_words(_item("robin-1", signature="s1"))
    assert w == {"words": "do you want me to avoid Tuesday?\nlike the fireside :)", "mine": False}
    # Split out of the user's own words: the row is what the user said.
    promise = page.their_words(_item("x:promise:1", ask_text="I'll send notes tonight", signature="s2"))
    assert promise == {"words": "I'll send notes tonight", "mine": True}


def test_an_email_row_is_the_email_the_task_came_from(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = {
        "source": "email",
        "messages": [
            {"id": "e1", "from": "June <j@x.com>", "text": "We'd love you for a fireside chat"},
            {"id": "e2", "from": "the user <alex@acme.example>", "from_me": True, "text": "draft", "draft": True},
            {"id": "e3", "from": "June <j@x.com>", "text": "the user, what do you think?\n\nOn Sun, June wrote:\n> old"},
            {"id": "e4", "from": "Sam <s@x.com>", "text": "+1"},
        ],
    }
    monkeypatch.setattr(store, "load_raw", lambda key: raw)
    monkeypatch.setattr(store, "source_message", lambda key: "e3")
    w = page.their_words(_item("june", source="email", signature="s3"))
    assert w["words"] == "the user, what do you think?" and not w["mine"]


def test_deadlines_are_plain_and_stale_ones_disappear() -> None:
    assert page.deadline({"due": _in(0)}) == "today"
    assert page.deadline({"due": _in(1)}) == "tomorrow"
    assert page.deadline({"due": _in(3)}).startswith("by ")
    assert page.deadline({"due": _in(-2)}) == "overdue"
    assert page.deadline({"due": _in(-20)}) == ""  # a guess time overtook
    assert page.deadline({"due": _in(40)}) == ""
    assert page.deadline({}) == ""


def test_now_picks_three_by_priority_and_user_own_picks_come_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store, "load_raw", lambda key: None)
    items = {
        "due": _item("due", due=_in(1)),
        "long": _item("long", asked_at=_ago(10), state="you", questions=[{"q": "?", "options": ["a"]}]),
        "fresh": _item("fresh", asked_at=_ago(0.1)),
        "x": _item("x", asked_at=_ago(12), request=True, platform="X (Twitter)"),
        "pitch": _item("pitch", filed="pitch", due=_in(0)),
        "wait": _item("wait", state="waiting", asked_at=_ago(20), nudge="any news?"),
        "old": _item("old", asked_at=_ago(30)),
        "todo": _item("todo", state="mine", asked_at=_ago(2)),
    }
    loose = list(items.values())
    built = page.build(loose, loose, [], items)
    # Due tomorrow, then ten days waiting on a quick answer, then a written reply.
    assert [r["key"] for r in built["now"]] == ["due", "long", "fresh"]
    assert [r["key"] for r in built["todos"]] == ["todo"] and built["replies"] == []
    assert [r["key"] for r in built["filed"]["pitch"]] == ["pitch"]
    assert {r["key"] for r in built["later"]} == {"x", "old"}  # a stranger's request; old and not due
    (w,) = built["waiting"]
    assert w["key"] == "wait" and w["nudge_ready"]
    assert all(r["key"] not in ("pitch", "wait", "x", "old") for r in built["now"])
    # The user's own picks lead, in the user's order, and Today fills the rest.
    mine = page.build(loose, loose, [items["todo"]], items)
    assert [r["key"] for r in mine["now"]] == ["todo", "due", "long"]
    assert mine["now"][0]["picked"] and not mine["now"][1]["picked"]


def test_filed_away_shows_recent_remarks_and_someone_elses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "load_raw", lambda key: None)
    items = {
        "zoe": _item("zoe", status="skipped", filed="remark", cleared_at=_ago(1)),
        "omar": _item("omar", status="skipped", filed="not_yours", cleared_at=_ago(2)),
        "old": _item("old", status="skipped", filed="remark", cleared_at=_ago(30)),
        "mine": _item("mine", status="done", filed="remark", cleared_at=_ago(1), finished_at=_ago(1)),
    }
    built = page.build([], [], [], items)
    assert [r["key"] for r in built["filed"]["remark"]] == ["zoe"]
    assert [r["key"] for r in built["filed"]["not_yours"]] == ["omar"]


def test_view_state_has_the_page_and_projects_this_week(monkeypatch: pytest.MonkeyPatch, tasks: Any) -> None:
    state: dict[str, Any] = {
        "items": {
            "a": _item("a", due=_in(1)),
            "b": _item("b"),
            "c": _item("c", project="fund", due=_in(2)),
        },
        "projects": [{"id": "fund", "title": "Fund I"}],
        "goals": {},
        "corrections": [],
        "today": {"order": [], "date": date.today().isoformat()},
    }
    monkeypatch.setattr(actions.store, "load", lambda: state)
    monkeypatch.setattr(actions.store, "load_raw", lambda key: None)
    tasks.seed(state)
    p = actions.view_state()["page"]
    # A project's own task can be picked for Now like any other.
    assert {r["key"] for r in p["now"]} == {"a", "b", "c"}
    assert "projects" not in p and p["week"] is None


# ---- nothing goes out unread, and every send can be undone --------------------------


def test_send_waits_so_undo_can_stop_it(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []
    went = threading.Event()
    state = {"items": {"a": _item("a", ref={"channel": "C1", "ts": "1.0"})}}
    monkeypatch.setattr(actions.store, "load", lambda: state)
    monkeypatch.setattr(actions.store, "load_raw", lambda key: {})

    def send(key: str, text: str) -> str:
        sent.append((key, text))
        went.set()
        return "Sent to A"

    monkeypatch.setattr(actions, "send", send)
    token = actions.send_soon("a", "hello", delay=0.2)
    assert actions.send_status(token)["state"] == "waiting"
    assert actions.cancel_send(token) is True
    actions._pending[token]["timer"].join(2)
    assert sent == [] and actions.send_status(token)["state"] == "cancelled"
    token = actions.send_soon("a", "hello again", delay=0.05)
    assert went.wait(2)
    actions._pending[token]["timer"].join(2)
    assert sent == [("a", "hello again")]
    assert actions.send_status(token) == {"state": "sent", "key": "a", "message": "Sent to A"}
    assert actions.cancel_send(token) is False  # too late: it went
    with pytest.raises(actions.SendError):
        actions.send_soon("a", "   ")


def test_a_send_waiting_out_undo_survives_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user, 2026-11-15: the user's email to Tess never went -- Today restarted inside the
    undo wait and the waiting send lived only in memory."""
    state = {"items": {k: _item(k, ref={"channel": "C1", "ts": "1.0"}) for k in ("a", "b", "c")}}
    monkeypatch.setattr(actions.store, "load", lambda: state)
    monkeypatch.setattr(actions.store, "load_raw", lambda key: {})
    monkeypatch.setattr(actions.store, "mutate", lambda change: change(state))
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(actions, "send", lambda key, text: sent.append((key, text)) or "Sent to A")
    waiting = actions.send_soon("a", "hello", delay=60)
    midway = actions.send_soon("b", "half gone", delay=60)
    stale = actions.send_soon("c", "old", delay=60)
    for t in (waiting, midway, stale):  # the restart: the timers and memory are gone
        actions._pending.pop(t)["timer"].cancel()
    with actions.conversations.opened(actions.store.DATA_DIR) as db:
        db.execute("UPDATE pending_sends SET due = ? WHERE token = ?", (actions.time.time(), waiting))
        db.execute("UPDATE pending_sends SET state = 'sending' WHERE token = ?", (midway,))
        db.execute("UPDATE pending_sends SET due = ? WHERE token = ?", (actions.time.time() - 3600, stale))
    assert actions.send_status(waiting)["state"] == "waiting"  # still known while Today is down
    assert actions.resume_sends() == 1
    actions._pending[waiting]["timer"].join(2)
    assert sent == [("a", "hello")]  # only the one still waiting out Undo goes; nothing repeats
    assert actions.send_status(waiting) == {"state": "sent", "key": "a", "message": "Sent to A"}
    assert actions.send_status(midway)["state"] == "failed"
    assert "Check your sent messages" in state["items"]["b"]["error"]
    assert "Press Send again" in state["items"]["c"]["error"]
    assert actions.resume_sends() == 0  # each is picked up once
    assert sent == [("a", "hello")]


def test_every_kind_of_send_waits_for_undo_and_reaches_its_own_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    """An email and the user's own Gmail draft go through the same delayed send."""
    state = {"items": {"a": _item("a", source="email", ref={"thread_id": "t1"})}}
    monkeypatch.setattr(actions.store, "load", lambda: state)
    monkeypatch.setattr(actions.store, "load_raw", lambda key: {})
    calls: list[tuple] = []
    monkeypatch.setattr(actions, "send_email", lambda *a: calls.append(("email", *a)) or "Sent to Ari")
    monkeypatch.setattr(actions, "send_user_draft", lambda *a: calls.append(("user_draft", *a)) or "Sent to A")
    sends = [
        ("a", "email", {"index": 1, "to": "Ari <ari@x.com>", "cc": "", "subject": "Hi"}),
        ("a", "user_draft", {"message_id": "m1"}),
    ]
    tokens = [actions.send_soon(k, "hello", delay=0.01, what=w, fields=f) for k, w, f in sends]
    for t in tokens:
        actions._pending[t]["timer"].join(2)
    assert sorted(calls) == [("email", "a", 1, "Ari <ari@x.com>", "", "Hi", "hello"), ("user_draft", "a", "m1", "hello")]
    assert [actions.send_status(t)["state"] for t in tokens] == ["sent", "sent"]
    with pytest.raises(actions.SendError):
        actions.send_soon("a", "hello", what="email", fields={"to": "no address"})
    with pytest.raises(actions.SendError):
        actions.send_soon("a", "hello", what="fax")


def test_a_new_email_to_one_person_goes_on_user_newest_thread_with_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user, 2026-11-14: always send on the existing thread."""
    monkeypatch.setenv("TODAY_EMAIL_ACCOUNTS", "alex@acme.example,alex.rivera@mail.example")
    found = {"alex@acme.example": {"threadId": "old", "internalDate": "100"},
             "alex.rivera@mail.example": {"threadId": "new", "internalDate": "200"}}
    monkeypatch.setattr(actions.mail_store, "latest_with", lambda account, address, since: (True, found[account]))
    thread = {"messages": [
        {"payload": {"headers": [{"name": "Subject", "value": "Dinner"}, {"name": "Message-ID", "value": "<m1>"}]}},
        {"payload": {"headers": [{"name": "Subject", "value": "Re: Dinner"}, {"name": "Message-ID", "value": "<m2>"},
                                 {"name": "References", "value": "<m1>"}]}},
    ]}
    monkeypatch.setattr(actions.mail_store, "thread", lambda thread_id, account: thread if thread_id == "new" else None)
    assert actions.thread_with("Ari <ari@x.com>") == {"account": "alex.rivera@mail.example", "thread_id": "new", "subject": "Dinner",
                                                     "in_reply_to": "<m2>", "references": "<m1>"}
    assert actions.thread_with("ari@x.com, abe@x.com") == {}  # an introduction is a new email

    state = {"items": {"a": _item("a", source="slack", emails=[{"to": ["Ari"], "subject": "Hi", "body": "x"}])}}
    monkeypatch.setattr(actions.store, "load", lambda: state)
    monkeypatch.setattr(actions.store, "mutate", lambda change: change(state))
    replies, new = [], []
    monkeypatch.setattr(actions.connectors, "gmail_send_reply", lambda *a: replies.append(a) or {})
    monkeypatch.setattr(actions.connectors, "gmail_send_new", lambda *a: new.append(a) or {})
    monkeypatch.setattr(actions, "_finish", lambda *a, **k: "")
    actions.send_email("a", 0, "Ari <ari@x.com>", "", "Hi", "hello")
    assert new == [] and replies[0][:3] == ("alex.rivera@mail.example", "new", "Ari <ari@x.com>") and replies[0][4] == "Dinner"


def test_a_dry_run_instance_sends_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A preview runs with TODAY_DRY_SEND=1: every write to the outside world (Slack, Gmail,
    calendar, the to-do files) is logged in the data copy instead of happening."""
    monkeypatch.setenv("TODAY_DRY_SEND", "1")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("nothing may leave a dry run")

    monkeypatch.setattr(connectors.subprocess, "run", boom)
    assert connectors.slack_post_message("C1", "hi", None)["ok"]
    connectors.gmail_send_new("alex@acme.example", "a@b.com", "", "Hi", "hello")
    log = [json.loads(line) for line in (tmp_path / "dry_sends.jsonl").read_text().splitlines()]
    assert [e["what"] for e in log] == ["http POST", "http POST"]


def test_not_now_and_pull_back_are_wired_in_the_server() -> None:
    rules = {r.rule for r in runner.app.url_map.iter_rules()}
    assert {"/api/not-now", "/api/pull-back", "/api/send-soon", "/api/send-cancel", "/api/send-status"} <= rules
    assert "/api/old/keep" not in rules


# ---- one step, not two, in a goal ---------------------------------------------------
