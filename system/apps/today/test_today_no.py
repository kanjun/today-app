"""A no the user gave covers the same thing when it turns up again in another conversation."""

import pytest
from today import goals, work

NO_ITEM = {
    "key": "slack:D1:1",
    "title": "Tell Jordan no on hosting the dinner",
    "who": "Sam",
    "status": "open",
    "state": "ready",
    "no": {
        "at": "2099-01-01T00:00:00",
        "before": {"title": "Ask Priya if Jordan can host"},
    },
}


def test_the_assistant_is_told_about_the_no(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        work.store, "load", lambda: {"items": {NO_ITEM["key"]: NO_ITEM}}
    )
    text = work.decided_no({"key": "email:x:1"})
    assert "Tell Jordan no on hosting the dinner" in text
    assert "you already said no" in text
    assert work.decided_no(NO_ITEM) == ""


def test_a_no_is_never_a_goal_step() -> None:
    other = dict(NO_ITEM, key="email:x:2")
    del other["no"]
    state = {"items": {NO_ITEM["key"]: NO_ITEM, other["key"]: other}}
    assert [i["key"] for i in goals.open_items(state)] == ["email:x:2"]
