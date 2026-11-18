"""Not now and Now (the user, 2026-11-16): Not now fades a row without ticking it, since nothing is
done; in Now it sends the task back where it was; Now picks by the date on the task itself."""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from today import actions, ledger, page, store

ASSETS = Path(__file__).parent / "src" / "today" / "assets"


def _ago(days: float) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def _in(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _item(key: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "status": "open", "state": "ready", "source": "slack", "who": key.title(),
            "asked_at": _ago(1), "title": f"Reply to {key}", "ask_text": f"hi from {key}"} | extra


def test_a_task_user_sent_back_from_now_is_not_picked_again_today() -> None:
    items = {"due": _item("due", due=_in(0)), "other": _item("other", due=_in(1))}
    loose = list(items.values())
    assert [r["key"] for r in page.build(loose, loose, [], items)["now"]][0] == "due"
    built = page.build(loose, loose, [], items, out_of_now={"due"})
    assert "due" not in [r["key"] for r in built["now"]]
    assert "due" in [r["key"] for r in built["replies"]]  # back in its own list


def test_out_of_now_takes_it_off_user_picks_and_undo_puts_it_back(monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict[str, Any] = {"items": {"a": _item("a")}, "today": {"date": "", "order": ["a", "b"]}}
    monkeypatch.setattr(store, "mutate", lambda fn: fn(state))
    assert actions.out_of_now("a") == "Back in its list"
    assert state["today"]["order"] == ["b"] and state["today"]["out"] == ["a"]
    assert actions.out_of_now("a", on=False) == "Back in Now"
    assert state["today"]["order"] == ["b", "a"] and state["today"]["out"] == []
    # One Today put there itself just stays out of Now for the day.
    actions.out_of_now("c")
    assert state["today"]["out"] == ["c"] and state["today"]["order"] == ["b", "a"]


def test_the_list_reset_each_morning_forgets_what_was_sent_back() -> None:
    state: dict[str, Any] = {"today": {"date": "2026-11-15", "order": [], "out": ["a"]}}
    store.roll_today(state, "2026-11-16")
    assert "out" not in state["today"]


def test_now_picks_by_the_task_date_not_an_older_one_on_its_item(monkeypatch: pytest.MonkeyPatch, tasks: Any) -> None:
    """The rental car sat in Now as overdue on Nov 13, a guess left on its item, while its task
    (and its row) said Nov 27."""
    old = _item("car", due=_in(-2), state="mine", asked_at=_ago(2))
    state = store.empty_state() | {"items": {"car": old}}
    monkeypatch.setattr(store, "load", lambda: state)
    monkeypatch.setattr(store, "mutate", lambda fn: fn(state))
    tasks.seed(state)
    tasks.set("car", due=_in(11))
    (row,) = actions.view_state()["page"]["now"]
    assert row["key"] == "car" and row["due"] == _in(11) and row["when"] != "overdue"
    assert state["items"]["car"]["due"] == _in(-2)  # the view reads it; nothing is rewritten


def test_now_chooses_from_the_tasks_not_the_items_behind_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user ticked the Fund I side letter done on Nov 15; on Nov 16 it was back in Now as
    overdue, picked from the item still open behind the task. And the LP email, the user's and
    overdue on its task, was kept out of Now by an old item that said the user was waiting."""
    letter = _item("c3d8b606c60", due=_in(-6), state="mine", asked_at=_ago(7))
    lp = _item("manual:1", state="waiting", manual=True, title="Send the LP email")
    state = store.empty_state() | {"items": {"c3d8b606c60": letter, "manual:1": lp}}
    state["today"] = {"date": date.today().isoformat(), "order": ["c3d8b606c60"]}
    monkeypatch.setattr(store, "load", lambda: state)
    monkeypatch.setattr(store, "mutate", lambda fn: fn(state))
    monkeypatch.setattr(ledger, "task_dues", lambda: {})
    monkeypatch.setattr(ledger, "sections", lambda built, st: None)
    lp_task = dict(lp, state="mine", due=_in(-1))
    # The letter is done: no open task. The LP email is an open to-do of the user's, overdue.
    monkeypatch.setattr(ledger, "now_pool", lambda st: ({"manual:1": lp_task}, {"manual:1"}))
    now = [r["key"] for r in actions.view_state()["page"]["now"]]
    assert "c3d8b606c60" not in now  # neither picked by the user's order nor chosen as overdue
    assert now == ["manual:1"]
    # While the letter's task is open, the user's pick of it holds.
    monkeypatch.setattr(ledger, "now_pool", lambda st: (
        {"manual:1": lp_task, "c3d8b606c60": letter}, {"manual:1", "c3d8b606c60"}))
    assert [r["key"] for r in actions.view_state()["page"]["now"]][0] == "c3d8b606c60"
