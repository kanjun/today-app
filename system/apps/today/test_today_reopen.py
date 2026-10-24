"""A task Today cleared on its own is read again when the conversation moves on;
anything they finished, said no to, or that was folded into another task is left alone."""

from typing import Any

from today import sync


def _cleared(**extra: Any) -> dict[str, Any]:
    return {"status": "skipped", "source": "email", "kind": "asked", **extra}


def test_todays_own_clear_is_watched() -> None:
    assert sync.cleared_by_today(_cleared(cleared_reason="nothing for you to do"))


def test_user_own_finishes_are_never_reopened() -> None:
    assert not sync.cleared_by_today(_cleared(finished_at="2026-10-20T10:00:00"))
    assert not sync.cleared_by_today(_cleared(no={"at": "x"}))
    assert not sync.cleared_by_today(_cleared(dup_of="other"))
    assert not sync.cleared_by_today(
        _cleared(cleared_reason="marked done in your to-do list")
    )
    assert not sync.cleared_by_today(_cleared(kind="followup"))
    assert not sync.cleared_by_today(dict(_cleared(), status="done"))


def test_new_reading_can_reopen_a_today_clear(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    state = {"items": {"k": _cleared(cleared_reason="nothing for you to do")}}
    monkeypatch.setattr(sync.store, "mutate", lambda fn: fn(state))
    monkeypatch.setattr(sync.store, "now_iso", lambda: "2026-10-23T10:00:00")
    sync._apply("k", {"status": "open", "title": "Send Jamie your comments"})
    assert state["items"]["k"]["status"] == "open"


def test_user_done_is_not_reopened_by_a_reading(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    state = {"items": {"k": dict(_cleared(), status="done", finished_at="x")}}
    monkeypatch.setattr(sync.store, "mutate", lambda fn: fn(state))
    sync._apply("k", {"status": "open"})
    assert state["items"]["k"]["status"] == "done"
