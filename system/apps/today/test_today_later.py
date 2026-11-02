"""Later sets a task aside in its own section: off the owed list, not a no, and it comes back on request."""

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from today import actions


def _item(key: str) -> dict[str, Any]:
    return {"key": key, "status": "open", "state": "mine", "asked_at": "2026-10-01"}


def test_later_moves_a_task_out_of_owed_and_back(monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict[str, Any] = {
        "items": {k: _item(k) for k in ("a", "b")},
        "goals": {},
        "today": {"order": ["a"], "date": date.today().isoformat()},
    }
    monkeypatch.setattr(actions.store, "mutate", lambda fn: fn(state))
    monkeypatch.setattr(actions.store, "roll_today", lambda s, d: None)

    actions.set_later("a", True)
    v = actions.view_state()
    assert [i["key"] for i in v["later"]] == ["a"]
    assert [i["key"] for i in v["owed"]] == ["b"]
    assert v["today"] == [] and v["no"] == []

    actions.set_today("a", True)
    v = actions.view_state()
    assert v["later"] == [] and [i["key"] for i in v["today"]] == ["a"]

    actions.set_later("a", True)
    actions.set_later("a", False)
    v = actions.view_state()
    assert v["later"] == [] and sorted(i["key"] for i in v["owed"]) == ["a", "b"]


@pytest.mark.frontend
def test_each_row_has_one_menu_with_today_later_and_ignore() -> None:
    page = (Path(actions.__file__).parent / "assets" / "app.html").read_text()
    assert '[["today","Do today"],["later","Later"],["ignore","Ignore"]]' in page
    assert "data-no=" not in page
    assert page.index("<h2>Later") < page.index("<h2>Saying no")


def test_ignore_drops_it_without_writing_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict[str, Any] = {
        "items": {"a": _item("a")},
        "goals": {},
        "today": {"order": ["a"], "date": date.today().isoformat()},
    }
    monkeypatch.setattr(actions.store, "mutate", lambda fn: fn(state))
    monkeypatch.setattr(actions.store, "load", lambda: state)
    monkeypatch.setattr(actions, "_archive", lambda key: "")
    monkeypatch.setattr(actions, "_advance_soon", lambda gid: None)
    assert actions.ignore("a") == "Ignored"
    item = state["items"]["a"]
    assert item["status"] == "skipped" and "no" not in item
    assert state["today"]["order"] == []
