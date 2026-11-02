"""A task title the user edits by hand is kept: later reads of the conversation leave it."""

from pathlib import Path
from typing import Any

import pytest
from today import actions, sync


def test_a_title_set_by_hand_survives_a_reread(monkeypatch: pytest.MonkeyPatch) -> None:
    item: dict[str, Any] = {"key": "a", "status": "open", "title": "Old"}
    state: dict[str, Any] = {"items": {"a": item}}
    monkeypatch.setattr(actions.store, "load", lambda: state)
    monkeypatch.setattr(actions.store, "mutate", lambda fn: fn(state))
    monkeypatch.setattr(actions, "_correction", lambda *a, **k: None)
    monkeypatch.setattr(sync.store, "mutate", lambda fn: fn(state))
    actions.rename_item("a", "Mine")
    item.update(due="2026-11-13", due_by_hand=True)
    sync._apply("a", {"title": "Rewritten by a read", "state": "ready", "due": ""})
    assert (item["title"], item["state"], item["due"]) == ("Mine", "ready", "2026-11-13")


@pytest.mark.frontend
def test_titles_are_edited_in_place() -> None:
    page = (Path(actions.__file__).parent / "assets" / "app.html").read_text()
    assert 'class="edtitle" id="ititle" contenteditable="true"' in page
    assert 'class="edtitle" id="gtitle" contenteditable="true"' in page
    assert "prompt(" not in page
