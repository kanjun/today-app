"""Finishing an email item archives its thread (never deletes), and Undo puts it back."""

from typing import Any

import pytest
from today import actions

KEY = "email:sam@acme.example:t1"


def _setup(
    monkeypatch: pytest.MonkeyPatch, others: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[tuple[str, str, bool]]]:
    item = {
        "key": KEY,
        "source": "email",
        "account": "sam@acme.example",
        "ref": {"thread_id": "t1"},
        "status": "open",
        "state": "you",
    }
    state: dict[str, Any] = {
        "items": {KEY: item, **(others or {})},
        "today": {"order": [KEY]},
    }
    calls: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(actions.store, "mutate", lambda fn: fn(state))
    monkeypatch.setattr(actions.store, "load", lambda: state)
    monkeypatch.setattr(actions, "_advance_soon", lambda gid: None)
    monkeypatch.setattr(
        actions.connectors,
        "gmail_set_inbox",
        lambda account, thread_id, inbox: calls.append((account, thread_id, inbox)),
    )
    return state, calls


def test_done_archives_and_undo_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    state, calls = _setup(monkeypatch)
    assert actions.mark_done(KEY) == "Marked done · archived"
    assert calls == [("sam@acme.example", "t1", False)]
    assert state["today"]["order"] == []
    assert actions.undo_finish(KEY) == "Back in your list and inbox"
    assert calls[-1] == ("sam@acme.example", "t1", True)
    assert state["items"][KEY]["status"] == "open"
    assert state["today"]["order"] == [KEY]


def test_thread_with_something_still_owed_stays_in_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = {
        "source": "email",
        "account": "sam@acme.example",
        "ref": {"thread_id": "t1"},
        "status": "open",
    }
    _, calls = _setup(monkeypatch, {KEY + ":promise:1": other})
    assert actions.mark_done(KEY) == "Marked done"
    assert calls == []


def test_non_email_items_never_touch_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    state, calls = _setup(monkeypatch)
    state["items"][KEY].update(source="slack", ref={"channel": "C1"})
    assert actions.let_go(KEY) == "Dropped"
    assert calls == []
