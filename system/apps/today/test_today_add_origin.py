"""A to-do another app files from the user's notes app keeps the line it came from and stays off Today's own list."""

from typing import Any

import pytest
from today import actions, runner


def _wire(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"items": {}, "today": {"order": []}}
    monkeypatch.setattr(actions.store, "load", lambda: state)
    monkeypatch.setattr(actions.store, "mutate", lambda fn: fn(state))
    return state


def test_a_to_do_from_the_notes_app_keeps_its_line_and_waits_in_yours_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _wire(monkeypatch)
    line = "Draft Lucie note to marketing team"
    response = runner.app.test_client().post(
        "/api/add", json={"text": line, "pick": False, "origin": {"from": "notes", "text": line}}
    )
    key = response.get_json()["key"]
    assert state["items"][key]["origin"] == {"from": "notes", "text": line}
    assert state["items"][key]["title"] == line
    assert state["today"]["order"] == []


def test_the_add_box_still_puts_a_to_do_on_todays_list_with_no_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _wire(monkeypatch)
    key = runner.app.test_client().post("/api/add", json={"text": "Call the bank"}).get_json()["key"]
    assert state["today"]["order"] == [key]
    assert "origin" not in state["items"][key]
