"""Dates the user promised survive when a duplicate task is folded into the one kept."""

import pytest
from today import goals


def test_folded_copy_hands_its_date_to_the_kept_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goals.store, "now_iso", lambda: "2026-10-23T09:00:00")
    state = {
        "items": {
            "keep": {
                "key": "keep",
                "status": "open",
                "title": "Read Taylor's outline",
            },
            "copy": {"key": "copy", "status": "open", "due": "2026-10-21"},
        },
        "today": {"order": []},
    }
    goals.apply_duplicates(state, [{"keep": "keep", "drop": ["copy"]}])
    assert state["items"]["keep"]["due"] == "2026-10-21"
    assert state["items"]["copy"]["status"] == "skipped"


def test_earlier_date_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(goals.store, "now_iso", lambda: "2026-10-23T09:00:00")
    state = {
        "items": {
            "keep": {"key": "keep", "status": "open", "due": "2026-10-30"},
            "copy": {"key": "copy", "status": "open", "due": "2026-10-24"},
        },
        "today": {"order": []},
    }
    goals.apply_duplicates(state, [{"keep": "keep", "drop": ["copy"]}])
    assert state["items"]["keep"]["due"] == "2026-10-24"
