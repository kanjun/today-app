"""A task's date (the user, 2026-11-15): one picker at the top right of the task; plain words are
read at once, the rest by a small model; the date the user sets is the user's and can be undone."""

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from today import (
    act,  # noqa: F401  (first, as the app loads it: project_book and task_steps import each other)
    ai,
    commitments,
    conversation_judge,
    conversations,
    due_dates,
    page,
    project_book,
    runner,
    store,
)

SUN = date(2026, 11, 15)


@pytest.mark.parametrize(("words", "day"), [
    ("today", SUN), ("tomorrow", date(2026, 11, 16)), ("fri", date(2026, 11, 20)), ("Friday", date(2026, 11, 20)),
    ("next fri", date(2026, 11, 20)), ("fri after next", date(2026, 11, 27)), ("dec 15", date(2026, 12, 15)),
    ("December 3rd", date(2026, 12, 3)), ("15 dec", date(2026, 12, 15)), ("12/15", date(2026, 12, 15)),
    ("1/5", date(2027, 1, 5)), ("in 2 weeks", date(2026, 11, 29)), ("3 days", date(2026, 11, 18)),
    ("end of month", date(2026, 11, 30)), ("2026-12-01", date(2026, 12, 1)), ("by thu", date(2026, 11, 19)),
])
def test_plain_words_are_read_at_once(words: str, day: date) -> None:
    assert due_dates.parse(words, SUN) == day


def test_next_weekday_within_this_week_means_next_weeks() -> None:
    wed = date(2026, 11, 18)
    assert due_dates.parse("fri", wed) == date(2026, 11, 20)
    assert due_dates.parse("next fri", wed) == date(2026, 11, 27)


def test_only_words_it_cannot_read_go_to_the_small_model(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict[str, Any]] = []

    def fake(prompt: str, **kw: Any) -> dict[str, Any]:
        asked.append(dict(kw, prompt=prompt))
        return {"date": "2026-11-19"}

    monkeypatch.setattr(ai, "complete_json", fake)
    assert due_dates.understand("fri")["date"] and not asked
    out = due_dates.understand("the week before thanksgiving")
    assert out["date"] == "2026-11-19" and out["label"].startswith("Thu Nov 19")
    assert asked[0]["model"] == "haiku"
    assert due_dates.understand("the week before thanksgiving", use_model=False) == {"date": "", "label": ""}


def test_the_date_spot_says_the_date_today_or_overdue() -> None:
    def on(days: int, **kw: Any) -> str:
        return page.due_spot({"due": (datetime.now() + timedelta(days=days)).date().isoformat(), **kw})
    assert on(0) == "today"
    assert on(14) == (datetime.now() + timedelta(days=14)).strftime("%b %-d")
    assert on(-2) == "overdue"
    assert on(-30) == ""  # a guess time overtook
    assert on(-30, due_set=True) == "overdue"  # a date the user set stays
    assert page.due_spot({}) == ""


def _task() -> str:
    with conversations.opened(store.DATA_DIR) as db:
        return commitments.create(db, "user", {"title": "Book a rental car", "direction": "self", "origin": "user_chose",
                                                 "started_by": "user", "due": ""}, reason="test")


def test_setting_and_clearing_the_date_with_undo() -> None:
    cid = _task()
    client = runner.app.test_client()
    res = client.post("/api/card/due", json={"key": "c:" + cid, "due": "2026-12-29"}).get_json()
    assert res["ok"] and res["due"] == "2026-12-29"
    with project_book.opened() as db:
        c = commitments.get(db, cid)
        assert c and c["due"] == "2026-12-29" and c["detail"]["due_set_by"] == "user"
    cleared = client.post("/api/card/due", json={"key": cid, "due": ""}).get_json()
    with project_book.opened() as db:
        assert (commitments.get(db, cid) or {})["due"] == ""
    client.post("/api/undo", json={"batch": cleared["batch"]})
    with project_book.opened() as db:
        assert (commitments.get(db, cid) or {})["due"] == "2026-12-29"
    assert client.post("/api/card/due", json={"key": cid, "due": "soon"}).status_code >= 400


def test_the_refresh_leaves_a_date_user_set_alone() -> None:
    cur = {"title": "Book a rental car", "due": "2026-12-29", "detail": {"due_set_by": "user"}}
    fields = conversation_judge._fields({"due": "2026-12-20", "due_quote": "by the 20th"}, cur)
    assert "due" not in fields and "due_quote" not in (fields.get("detail") or {})
    assert conversation_judge._fields({"due": "2026-12-20"}, dict(cur, detail={}))["due"] == "2026-12-20"


def test_the_parse_endpoint_answers_plain_words_without_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai, "complete_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model")))
    out = runner.app.test_client().post("/api/due/parse", json={"text": "tomorrow", "model": False}).get_json()
    assert out["date"] == (due_dates.today() + timedelta(days=1)).isoformat()
