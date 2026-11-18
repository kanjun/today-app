"""The switch (ledger.py): after the import, the page's lists come from commitments; the user
clicks on items are recorded as the user's decisions; a commitment the judge found (no item
behind it) shows as its own row and its buttons act on it; new items from the reply judge
get commitments, and items it finished are carried over."""

import time
from typing import Any

from today import (
    actions,
    cards,
    commitments,
    commitments_import,
    conversations,
    ledger,
    runner,
    store,
    sync,
)


def _seed() -> None:
    items = {
        "text:c:1": {"key": "text:c:1", "status": "open", "source": "text", "kind": "asked", "state": "ready",
                     "who": "Ravi", "title": "Send Ravi his invite", "draft": "Here it is", "ask_id": "1",
                     "ref": {"chat_id": "c"}, "asked_at": "2026-11-12T10:00:00"},
        "manual:x": {"key": "manual:x", "status": "open", "source": "manual", "manual": True, "state": "mine",
                     "title": "Book the rental car", "asked_at": "2026-11-12T10:00:00"},
    }
    store.mutate(lambda s: s["items"].update(items))
    with conversations.opened(store.DATA_DIR) as db:
        commitments_import.run(db, store.load(), {}, None, {})


def _built() -> dict[str, Any]:
    return {"now": [], "replies": [], "answers": [], "todos": [], "waiting": [], "later": [],
            "filed": {"pitch": [{"key": "p"}], "not_yours": [], "remark": []}}


def test_lists_come_from_commitments_and_judge_found_ones_get_rows() -> None:
    _seed()
    with conversations.opened(store.DATA_DIR) as db:
        found = commitments.create(db, "judge", {"title": "Wait for Gus to grab your package", "direction": "theirs",
                                                 "origin": "user_asked", "started_by": "user", "counterparty": "Gus"})
    out = ledger.sections(_built(), store.load())
    assert out is not None
    assert [(r["key"], r["draft"]) for r in out["replies"]] == [("text:c:1", "Here it is")]
    assert [r["title"] for r in out["todos"]] == ["Book the rental car"]
    assert [(r["key"], r["who"]) for r in out["waiting"]] == [(found, "Gus")]
    assert out["filed"]["pitch"] == []
    # The user's click on the judge's one acts on the commitment, as the user's decision.
    assert actions.mark_done(found) == "Marked done"
    with conversations.opened(store.DATA_DIR) as db:
        c = commitments.get(db, found)
        assert c and c["stage"] == "done" and c["user_decision_at"]
    assert actions.undo_finish(found) == "Back in your list"


def test_user_click_on_an_item_is_recorded_as_users() -> None:
    _seed()
    assert runner.app.test_client().post("/api/not-now", json={"key": "manual:x"}).status_code == 200
    assert store.actor.get() == "sync"  # the user's request's mark ends with it
    with conversations.opened(store.DATA_DIR) as db:
        c = commitments.by_legacy_key(db, "manual:x")
        assert c and c["stage"] == "parked" and commitments.history(db, c["id"])[-1]["actor"] == "user"
    out = ledger.sections(_built(), store.load())
    assert out and [r["key"] for r in out["later"]] == ["manual:x"]


def test_an_item_is_its_task_and_finishing_it_finishes_the_task() -> None:
    """The user, 2026-11-17: one record per task. A new item is a task at once; the refresh
    finishing it closes the task, as the refresh's own change."""
    _seed()
    store.mutate(lambda s: s["items"].update({
        "slack:D1:2.0": {"key": "slack:D1:2.0", "status": "open", "source": "slack", "kind": "asked", "state": "you",
                         "who": "Robin", "title": "Tell Robin yes", "ref": {"channel": "D1", "ts": "2.0"}},
    }))
    store.mutate(lambda s: s["items"]["text:c:1"].update(status="cleared", cleared_reason="you answered"))
    with conversations.opened(store.DATA_DIR) as db:
        les = commitments.by_legacy_key(db, "text:c:1")
        assert les and les["stage"] == "stale" and les["stage_reason"] == "you answered"
        assert commitments.history(db, les["id"])[-1]["actor"] == "sync"
        robin = commitments.by_legacy_key(db, "slack:D1:2.0")
        assert robin and (cards.card_of(robin) or {})["ref"] == {"channel": "D1", "ts": "2.0"}
    assert not (store.DATA_DIR / "state.json").exists()  # nothing is kept in a file


def test_a_new_item_goes_to_the_judge_not_to_a_writer(monkeypatch: Any) -> None:
    """The user, 2026-11-15: the old reply writer is gone. Something new in a conversation is
    handed to the judge: the item is open, its conversation is judged this refresh, and it gets
    its task at once, marked unread, for the judge to say whether it is real."""
    _seed()
    store.mutate(lambda s: s["items"].update({"slack:D9:5.0": {
        "key": "slack:D9:5.0", "status": "new", "source": "slack", "kind": "asked", "who": "Sam",
        "title": "Can you look at the deck?", "ask_text": "Can you look at the deck?", "ref": {"channel": "D9", "ts": "5.0"}}}))
    asked: list[str] = []
    monkeypatch.setattr(ledger, "ask_judging", asked.append)
    sync._hand_to_judge("slack:D9:5.0", store.load()["items"]["slack:D9:5.0"], "sig1")
    item = store.load()["items"]["slack:D9:5.0"]
    assert item["status"] == "open" and item["for_judge"] and item["signature"] == "sig1" and asked == ["slack:D9"]
    assert "worked_at" not in item  # so the one assistant writes its card ahead
    with conversations.opened(store.DATA_DIR) as db:
        c = commitments.by_legacy_key(db, "slack:D9:5.0")
        assert c and c["stage"] == "open" and c["detail"]["unread"] is True
    # Someone writing again on a finished one asks the judge to read it, and leaves it finished.
    store.mutate(lambda s: s["items"]["text:c:1"].update(status="cleared", signature="old"))
    sync._hand_to_judge("text:c:1", store.load()["items"]["text:c:1"], "new")
    les = store.load()["items"]["text:c:1"]
    assert les["status"] == "cleared" and les["moved_at"] and "for_judge" not in les


def test_a_slipped_item_with_a_draft_is_in_replies_ready() -> None:
    """The user, 2026-11-14: something in Slipped with a reply drafted belongs in Replies ready."""
    _seed()
    store.mutate(lambda s: s["items"]["text:c:1"].update(asked_at="2026-09-01T10:00:00"))
    with conversations.opened(store.DATA_DIR) as db:
        les = commitments.by_legacy_key(db, "text:c:1")
        assert les
        commitments.change(db, les["id"], "user", {"detail": dict(les["detail"], asked_at="2026-09-01T10:00:00")})
    conversations.save_record(store.DATA_DIR, "text:c:1", {
        "source": "text", "chat_id": "c", "channel_label": "X text with Ravi",
        "messages": [{"id": "1", "ts": 1000.0, "name": "Ravi", "text": "ravi@example.org", "is_me": False}],
    })
    store.save_raw("text:c:1", {"source": "text", "chat_id": "c", "messages": []})
    out = ledger.sections(_built(), store.load())
    assert out and [r["key"] for r in out["replies"]] == ["text:c:1"] and out["later"] == []


def test_a_conversation_asked_for_is_judged_once(monkeypatch: Any) -> None:
    """The reply judge saw a promise in a conversation with nothing open: it is read whole
    on the next refresh, then not again until it changes."""
    _seed()
    conversations.save_record(store.DATA_DIR, "text:q:1", {
        "source": "text", "chat_id": "q", "channel_label": "Signal text with Lina",
        "messages": [{"id": "1", "ts": time.time() - 60, "name": "the user", "text": "I'll send it tonight", "is_me": True}],
    })
    ledger.ask_judging("text:q")
    with conversations.opened(store.DATA_DIR) as db:
        assert "text:q" in ledger.changed_conversations(db)
    monkeypatch.setattr(ledger.conversation_judge, "judge", lambda db, conv: {"changes": [], "labels": {}, "versions": {}})
    assert "error" not in ledger._judge_one("text:q")
    with conversations.opened(store.DATA_DIR) as db:
        assert "text:q" not in ledger.changed_conversations(db)


def test_a_quiet_conversation_is_read_again_when_a_promise_may_be_kept_elsewhere(monkeypatch: Any) -> None:
    """The user, 2026-11-14: a promise is checked where it would be kept. Nobody writes in the
    conversation it was made in, but a calendar event with the person is added: it is read
    again once, and not again until something else turns up."""
    _seed()
    now = time.time()
    conversations.save_record(store.DATA_DIR, "text:q:1", {
        "source": "text", "chat_id": "q", "channel_label": "Signal text with Lina",
        "messages": [{"id": "1", "ts": now - 600, "name": "the user", "text": "I'll find us a time", "is_me": True}],
    })
    with conversations.opened(store.DATA_DIR) as db:
        commitments.create(db, "judge", {"title": "Schedule a call with Lina", "direction": "mine", "origin": "user_promised",
                                         "started_by": "user", "counterparty": "Lina"}, links=[("text:q", "1", "origin")])
    monkeypatch.setattr(ledger.conversation_judge, "judge", lambda db, conv: {"changes": [], "labels": {}, "versions": {}})
    ledger._judge_one("text:q")
    with conversations.opened(store.DATA_DIR) as db:
        assert "text:q" not in ledger.changed_conversations(db)
    added = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    store.save_raw("calendar", {"events": [
        {"id": "e1", "summary": "Lunch", "created": added, "attendees": [{"displayName": "Sam Lee"}]},
        {"id": "e2", "summary": "Call", "created": added, "attendees": [{"displayName": "Lina Vega"}]}]})
    with conversations.opened(store.DATA_DIR) as db:
        assert "text:q" in ledger.changed_conversations(db)
    ledger._judge_one("text:q")
    with conversations.opened(store.DATA_DIR) as db:
        assert "text:q" not in ledger.changed_conversations(db)


def test_opening_a_task_by_its_id_keeps_the_item_it_came_from() -> None:
    _seed()
    with conversations.opened(store.DATA_DIR) as db:
        c = commitments.by_legacy_key(db, "text:c:1")
        assert c and commitments.conversations_of(db, c["id"])
    ledger.ensure_item(c["id"])
    with conversations.opened(store.DATA_DIR) as db:
        again = commitments.get(db, c["id"])
        assert again and again["legacy_key"] == "text:c:1"
        assert commitments.by_legacy_key(db, "text:c:1") is not None
