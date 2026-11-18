"""The user's four choices: today, tomorrow (or a later day), not mine, drop. Set-aside items come back on their day."""

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from today import actions, store


def _item(key: str) -> dict[str, Any]:
    return {"key": key, "status": "open", "state": "mine", "asked_at": date.today().isoformat()}


def test_later_moves_a_task_out_of_owed_and_back(monkeypatch: pytest.MonkeyPatch, tasks: Any) -> None:
    state = _state()
    state["today"]["order"] = ["a"]
    _real(monkeypatch, state)
    actions.set_later("a", True)
    p = actions.view_state()["page"]
    assert [r["key"] for r in p["later"]] == ["a"]
    assert [r["key"] for r in p["now"]] == ["b"]  # off the user's picks too
    actions.set_later("a", False)
    p = actions.view_state()["page"]
    assert p["later"] == [] and sorted(r["key"] for r in p["now"]) == ["a", "b"]


def _state() -> dict[str, Any]:
    return {
        "items": {k: _item(k) for k in ("a", "b")},
        "goals": {},
        "corrections": [],
        "today": {"order": [], "date": date.today().isoformat()},
    }


def _wire(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    monkeypatch.setattr(actions.store, "mutate", lambda fn: fn(state))
    monkeypatch.setattr(actions.store, "load", lambda: state)
    monkeypatch.setattr(actions.store, "roll_today", lambda s, d: None)
    monkeypatch.setattr(actions, "_archive", lambda key: "")


def _real(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    """The state's items as real tasks in the scratch store, for clicks that change them."""
    store.mutate(lambda s: (s["items"].update(state["items"]), s.update(today=state["today"])))
    monkeypatch.setattr(actions, "_archive", lambda key: "")


def test_tomorrow_hides_it_until_tomorrow_then_it_comes_back(
    monkeypatch: pytest.MonkeyPatch, tasks: Any
) -> None:
    state = _state()
    _real(monkeypatch, state)
    assert actions.plan("a", "tomorrow") == "Back tomorrow"
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert tasks.get("a")["stage"] == "parked" and tasks.get("a")["detail"]["parked_until"] == tomorrow
    p = actions.view_state()["page"]
    assert [r["key"] for r in p["later"]] == ["a"]
    assert [r["key"] for r in p["now"]] == ["b"]
    # The day comes: it is back on its list by itself.
    tasks.set("a", detail=dict(tasks.get("a")["detail"], parked_until=date.today().isoformat()))
    p = actions.view_state()["page"]
    assert p["later"] == [] and "a" in [r["key"] for r in p["now"] + p["todos"]]


def test_next_week_returns_on_monday_or_the_day_user_picks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    _wire(monkeypatch, state)
    actions.plan("a", "nextweek")
    monday = date.fromisoformat(state["items"]["a"]["later_until"])
    assert monday.weekday() == 0 and monday > date.today()
    actions.plan("b", "nextweek", "2027-01-05")
    assert state["items"]["b"]["later_until"] == "2027-01-05"


def test_not_mine_closes_it_and_is_remembered_for_later_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state["items"]["a"].update(who="Robin", title="Ask support why", ask_text="you can ask support")
    _wire(monkeypatch, state)
    assert actions.plan("a", "notmine").startswith("Marked not yours")
    assert state["items"]["a"]["status"] == "skipped"
    (c,) = state["corrections"]
    assert c["kind"] == "not_mine" and c["who"] == "Robin" and c["ask_text"] == "you can ask support"


def test_rows_are_grouped_by_the_action_they_take() -> None:
    def g(title: str) -> str:
        return actions.action_group({"title": title})

    assert g("Sign Faye Joy's consultant agreement in Docusign") == "Sign"
    assert g("Tell Luis when you can meet at Dandelion") == "Schedule"
    assert g("Post Elsa's tweet about the event") == "Post or share"
    assert g("Review Robin's talking points") == "Read or try"
    assert g("Tell Elena whether to remove sourceress.co") == "Reply or decide"
    assert g("Send Hana the Series B preferred stock price") == "Other"
    assert g("Get Robin the user's gift") == "Other"


def test_old_items_sink_into_later_quietly_and_come_back_when_someone_writes(
    monkeypatch: pytest.MonkeyPatch, tasks: Any
) -> None:
    """Priority, not completionism: an old item with nothing due rests in Later with no
    review prompt, and comes back up on its own when it gets urgent."""
    state = _state()
    state["items"]["a"]["asked_at"] = (date.today() - timedelta(days=20)).isoformat()
    state["items"]["b"]["asked_at"] = date.today().isoformat()
    _wire(monkeypatch, state)
    tasks.seed(state)
    v = actions.view_state()
    assert "old" not in v
    assert [r["key"] for r in v["page"]["later"]] == ["a"]
    assert "a" not in [r["key"] for r in v["page"]["now"]]
    tasks.set("a", due=(date.today() + timedelta(days=1)).isoformat())
    assert actions.view_state()["page"]["later"] == []


def test_not_now_rests_until_someone_writes_and_undo_puts_it_back(
    monkeypatch: pytest.MonkeyPatch, tasks: Any
) -> None:
    state = _state()
    for k in ("a", "b"):
        state["items"][k]["asked_at"] = date.today().isoformat()
    _real(monkeypatch, state)
    assert actions.not_now("a") == "Moved to Slipped"
    assert [r["key"] for r in actions.view_state()["page"]["later"]] == ["a"]
    actions.not_now("a", on=False)
    assert actions.view_state()["page"]["later"] == []


@pytest.mark.frontend
def test_main_page_is_light_no_row_buttons_pills_or_review_box() -> None:
    """The main page: sections by name only, no per-row button rows, no status pills, no
    "Still matter?" box, and a tooltip on every control saying what it does."""
    page = (Path(actions.__file__).parent / "assets" / "app.html").read_text()
    main = page[page.index("function renderList(){") : page.index("// One handler set for every item")]
    assert "Still matter" not in page and "planBar(" not in main and "tag(" not in main
    # "Quick answers" is the quick-reply section's name (the user, 2026-11-13).
    order = ["This week", "Now", "Replies ready", "Quick answers", "Yours to do"]
    page_js = page[page.index("function weekHtml(") : page.index("// One handler set for every item")]
    spots = [page_js.index(name) for name in order]
    assert spots == sorted(spots)
    for fold in ("Waiting on others · ", "`Slipped · ", "Filed away · "):
        assert fold in main
    # Projects have their own tab, so the Today tab has no Projects fold (the user, 2026-11-14).
    assert "Projects · " not in main
    tips = page[page.index("const TIPS = {") : page.index("const openIn")]
    for word in ("send", "notnow", "more", "gdraft", "notmine", "drop", "thread", "choice", "tell", "check", "handled"):
        assert f"  {word}:" in tips
    card = (Path(actions.__file__).parent / "assets" / "card.js").read_text()
    assert card.count("title=") >= 6  # the one card's controls each say what they do


def test_ignore_drops_it_without_writing_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict[str, Any] = {
        "items": {"a": _item("a")},
        "goals": {},
        "today": {"order": ["a"], "date": date.today().isoformat()},
    }
    monkeypatch.setattr(actions.store, "mutate", lambda fn: fn(state))
    monkeypatch.setattr(actions.store, "load", lambda: state)
    monkeypatch.setattr(actions, "_archive", lambda key: "")
    assert actions.ignore("a") == "Ignored"
    item = state["items"]["a"]
    assert item["status"] == "skipped" and "no" not in item
    assert state["today"]["order"] == []

