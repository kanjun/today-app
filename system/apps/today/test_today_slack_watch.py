"""Today reads the user's Slack itself (slack_watch.py, the user 2026-11-17: Slack moves from the to-do app to
Today): what is new from the user, to the user or mentioning the user is saved, conversation by conversation
with the threads it is in, and handed to the judge. Slack is stubbed; nothing is written to it."""

import time
from typing import Any

import pytest
from today import commitments, connectors, conversations, ledger, slack_watch, store

ME = "UT87G8X9V"


def _slack(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, dict[str, str]]]) -> None:
    now = time.time()
    a, b, c = f"{now - 600:.6f}", f"{now - 300:.6f}", f"{now - 60:.6f}"
    old = f"{now - 4 * 86400:.6f}"
    hits = {
        "from:me": [{"ts": c, "channel": {"id": "D1", "is_im": True}, "user": ME, "text": "yes thank you",
                     "permalink": f"https://x.slack.com/archives/D1/p1?thread_ts={a}"}],
        "to:me": [{"ts": b, "channel": {"id": "D1", "is_im": True}, "user": "U2", "text": "Arc is sunsetting"}],
        f"<@{ME}>": [{"ts": old, "channel": {"id": "C9", "name": "general"}, "user": "U3", "text": "old"}],
    }

    def get(method: str, params: dict[str, str]) -> dict[str, Any]:
        calls.append((method, params))
        if method == "search.messages":
            query = params["query"].split(" after:")[0]
            return {"ok": True, "messages": {"matches": hits[query], "paging": {"pages": 1}}}
        if method == "conversations.history":
            return {"messages": [{"type": "message", "ts": b, "user": "U2", "text": "Arc is sunsetting"},
                                 {"type": "message", "ts": a, "user": "U2", "text": f"Ok to docusign this, <@{ME}>?",
                                  "thread_ts": a, "reply_count": 1, "latest_reply": c}]}
        if method == "conversations.replies":
            return {"messages": [{"type": "message", "ts": a, "user": "U2", "text": "Ok to docusign this?", "thread_ts": a},
                                 {"type": "message", "ts": c, "user": ME, "text": "yes thank you", "thread_ts": a}]}
        if method == "users.info":
            return {"user": {"real_name": "Robin"}}
        raise AssertionError(f"Slack {method} may not be called here")

    monkeypatch.setattr(connectors, "slack_get", get)
    monkeypatch.setattr(slack_watch, "SLACK_USERS", store.DATA_DIR / "no-names.json")


def test_new_slack_messages_save_their_conversation_and_ask_the_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    _slack(monkeypatch, calls)
    out = slack_watch.run(ME)
    # The two new messages in the DM; the mention from four days ago is older than the first look.
    assert out == {"slack_messages": 2, "slack_conversations": 1}
    msgs = conversations.messages(store.DATA_DIR, "slack:D1")
    assert [(m["sender"], m["text"], bool(m["is_user"])) for m in msgs] == [
        ("Robin", "Ok to docusign this, @the user?", False), ("Robin", "Arc is sunsetting", False), ("the user", "yes thank you", True)]
    # The user's reply keeps the thread it is in, so the judge reads it as the answer to that ask.
    assert msgs[2]["thread"] == msgs[0]["source_id"]
    with conversations.opened(store.DATA_DIR) as db:
        assert ledger.changed_conversations(db) == ["slack:D1"]
    assert store.load()["slack_names"] == {"U2": "Robin"}
    # Only reads: nothing is posted to Slack.
    assert {m for m, _ in calls} == {"search.messages", "conversations.history", "conversations.replies", "users.info"}


def test_the_next_look_starts_where_the_last_one_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    _slack(monkeypatch, calls)
    slack_watch.run(ME)
    checked = store.load()["slack_checked"]
    calls.clear()
    out = slack_watch.run(ME)
    # The same messages come back from the overlap: saved again, never twice.
    assert out["slack_conversations"] == 1 and len(conversations.messages(store.DATA_DIR, "slack:D1")) == 3
    assert store.load()["slack_checked"] >= checked
    assert "users.info" not in {m for m, _ in calls}  # names already known are not looked up again


def test_a_task_the_judge_found_gets_its_card_on_the_message_it_came_from() -> None:
    """The user, 2026-11-17: the 409a task's card took Robin's newest message (about Arc), so a
    reply from it would have gone on the Arc thread."""
    conversations.save_record(store.DATA_DIR, "slack:D1", {"source": "slack", "channel": "D1", "messages": [
        {"ts": "1.0", "user": "U2", "name": "Robin", "text": "Ok to docusign this?", "thread_ts": "1.0"},
        {"ts": "2.0", "user": "U2", "name": "Robin", "text": "Arc is sunsetting", "thread_ts": "2.0"},
    ]})
    with conversations.opened(store.DATA_DIR) as db:
        cid = commitments.create(db, "judge", {"title": "Tell Robin whether to DocuSign", "direction": "mine",
                                               "origin": "they_asked", "started_by": "them", "counterparty": "Robin"},
                                 links=[("slack:D1", "1.0", "origin")])
    ledger.ensure_item(cid)
    item = store.load()["items"][cid]
    assert item["ref"] == {"channel": "D1", "ts": "1.0", "thread_ts": "1.0"} and item["ask_text"] == "Ok to docusign this?"
