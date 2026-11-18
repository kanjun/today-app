"""Planning (planning.py): the user's own next steps, Week's mornings sized from the user's calendar, one big
thing a morning as a calendar event, Inbox filing, and the Someday file. Every calendar write
is stubbed: a test never touches the user's real calendar."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from today import (
    actions,
    commitment_lists,
    commitments,
    connectors,
    conversations,
    planning,
    store,
    weekly,
    work,
)


@pytest.fixture
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "today")
    monkeypatch.setattr(planning, "SOMEDAY_FILE", tmp_path / "Projects" / "Someday.md")
    return tmp_path


@pytest.fixture
def calendar_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(args: list[str], account: str | None = None, timeout: int = 120) -> dict[str, Any]:
        calls.append(args)
        if args[:2] == ["-X", "POST"]:
            return {"id": f"ev{len(calls)}"}
        if args[:2] == ["-X", "DELETE"]:
            raise connectors.ConnectorError("not JSON: ")
        return {}

    monkeypatch.setattr(connectors, "_curl", fake)
    return calls


def _timed(title: str, start: str, end: str, resp: str = "", **extra: Any) -> dict[str, Any]:
    e: dict[str, Any] = {"id": title, "summary": title, "start": {"dateTime": start}, "end": {"dateTime": end}}
    if resp:
        e["attendees"] = [{"self": True, "responseStatus": resp}]
    e.update(extra)
    return e


def test_morning_is_the_longest_free_stretch_and_user_holds_count_as_open() -> None:
    day = "2026-11-18"
    record = {
        "tz": "America/Chicago",
        "events": [
            _timed("WRITE", f"{day}T08:00:00-06:00", f"{day}T12:00:00-06:00"),
            _timed("Coaching", f"{day}T10:00:00-06:00", f"{day}T11:00:00-06:00", "accepted"),
            _timed("Declined sync", f"{day}T08:30:00-06:00", f"{day}T09:00:00-06:00", "declined"),
            _timed("Unanswered invite", f"{day}T09:00:00-06:00", f"{day}T09:30:00-06:00", "needsAction"),
            _timed("Office Closed", f"{day}T08:00:00-06:00", f"{day}T09:00:00-06:00"),
            _timed("Busy-but-free", f"{day}T11:00:00-06:00", f"{day}T13:00:00-06:00", transparency="transparent"),
        ],
    }
    m = planning.morning(planning.events_on(day, record))
    assert (m["start"], m["end"], m["until"]) == ("08:00", "10:00", "Coaching")
    assert m["hold"]["title"] == "WRITE"
    assert [e["title"] for e in m["unanswered"]] == ["Unanswered invite"]


def test_times_follow_user_calendar_zone_and_switch_after_a_flight() -> None:
    record = {
        "tz": "America/Chicago",
        "events": [_timed("Flight to SFO (UA2764)", "2099-11-15T10:40:00-06:00", "2099-11-15T12:40:00-08:00")],
    }
    now = datetime(2099, 11, 13, tzinfo=timezone.utc)
    assert planning.zone_on("2099-11-14", record, now) == "America/Chicago"
    assert planning.zone_on("2099-11-16", record, now) == "America/Los_Angeles"
    # A many-day event ending in the small hours doesn't cover that last morning.
    trip = {"tz": "America/Chicago", "events": [_timed("Stay at hotel", "2099-11-12T02:00:00-06:00", "2099-11-14T02:00:00-06:00")]}
    assert [e["all_day"] for e in planning.events_on("2099-11-13", trip)] == [True]
    assert planning.events_on("2099-11-14", trip) == []


def _note(plan_dir: Path, monkeypatch: pytest.MonkeyPatch, *goals: str) -> str:
    """This week's note with these goals; returns the week (its Monday)."""
    folder = plan_dir / "weekly"
    folder.mkdir(exist_ok=True)
    monkeypatch.setattr(weekly, "WEEKLY_DIR", folder)
    week = weekly.week_start().isoformat()
    (folder / f"{week}.md").write_text("#1 priority: customer onboarding\nGoals:\n" + "".join(f"- {g}\n" for g in goals))
    return week


def _tasks() -> list[dict[str, Any]]:
    with conversations.opened(store.DATA_DIR) as db:
        return commitments.all_commitments(db)


def _blocks(day: str) -> list[str]:
    return [b["ref"] for d, b in planning.all_placed() if d == day]


def test_a_weekly_goal_is_a_task_of_hers_for_the_week(plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    week = _note(plan_dir, monkeypatch, "Ship the essay", "Talk to users (stretch)")
    gid = weekly.goal_id("Ship the essay")
    planning.summary()
    goals = [c for c in _tasks() if c["detail"].get("week_goal")]
    assert [(c["title"], c["week"], c["direction"], c["labels"]) for c in goals] == [
        ("Ship the essay", week, "self", []), ("Talk to users (stretch)", week, "self", [])]
    weekly.set_done(gid, True)
    assert gid in weekly.done_goals(week)
    shown = weekly.view(store.load())
    assert shown is not None and shown["goals"][0]["done"]
    weekly.set_done(gid, False)
    assert weekly.done_goals(week) == {}
    # A goal the user takes out of the user's note goes quiet; the user's words stay the note's.
    (weekly.WEEKLY_DIR / f"{week}.md").write_text("Goals:\n- Talk to users (stretch)\n")
    planning.summary()
    gone = next(c for c in _tasks() if c["title"] == "Ship the essay")
    assert gone["stage"] == "stale" and gone["stage_reason"] == "no longer in your weekly note"


def test_user_steps_queue_and_the_next_one_takes_over(plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _note(plan_dir, monkeypatch, "Work with a designer")
    ref = "goal:" + weekly.goal_id("Work with a designer")
    assert planning.add_then(ref, "Share my thoughts on personas")["text"] == "Share my thoughts on personas"
    st = planning.add_then(ref, "Send it to Felix and Ben")
    assert st["then"] == ["Send it to Felix and Ben"]
    st = planning.step_done(ref)
    assert st["text"] == "Send it to Felix and Ben" and st["then"] == []
    assert [d["text"] for d in st["done"]] == ["Share my thoughts on personas"]
    planning.set_step(ref, "Rewritten in the user's words")
    assert planning.summary()["steps"][ref]["text"] == "Rewritten in the user's words"
    # The steps are the task's checklist, the same one a project page shows.
    goal = next(c for c in _tasks() if c["detail"].get("week_goal"))
    steps = [c for c in _tasks() if c["parent_id"] == goal["id"] and c["stage"] in ("open", "done")]
    assert [(c["title"], c["stage"], c["labels"]) for c in steps] == [
        ("Share my thoughts on personas", "done", ["step"]), ("Rewritten in the user's words", "open", ["step"])]
    assert goal["next_step"] == "Rewritten in the user's words"
    planning.set_step(ref, "")
    assert planning.summary()["steps"][ref]["text"] == ""


def test_a_todo_step_is_its_tasks_checklist(plan_dir: Path) -> None:
    key = actions.add_manual("Share the team cafe with Elsa", pick=False)
    assert key not in store.load()["today"]["order"]
    planning.set_step(f"todo:{key}", "Build the team cafe")
    task = next(c for c in _tasks() if c["legacy_key"] == key)
    assert task["next_step"] == "Build the team cafe"
    planning.set_step(f"todo:{key}", "")
    assert next(c for c in _tasks() if c["legacy_key"] == key)["next_step"] == ""
    assert "next_step" not in store.load()["items"][key]  # nothing is written to the old item


def test_one_big_thing_a_morning_on_user_calendar(plan_dir: Path, calendar_calls: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    _note(plan_dir, monkeypatch, "Write the product vision")
    store.save_raw("calendar", {"tz": "America/Chicago", "events": []})
    store.mutate(lambda s: s["items"].update(x={"key": "x", "status": "open", "title": "Build the cafe", "manual": True, "source": "manual"}))
    goal = "goal:" + weekly.goal_id("Write the product vision")
    p = planning.place(goal, "Write the product vision", "2026-11-18")
    assert (p["start"], p["end"], p["event_id"]) == ("08:00", "13:00", "ev1")
    assert '"timeZone": "America/Chicago"' in calendar_calls[0][5]
    # Moving it to another day this week updates the same event.
    planning.place(goal, "Write the product vision", "2026-11-19", "09:00", "11:00")
    assert calendar_calls[-1][:2] == ["-X", "PATCH"] and "ev1" in calendar_calls[-1][-1]
    assert [d for d, _ in planning.all_placed()] == ["2026-11-19"]
    # The block is kept on the task itself.
    task = next(c for c in _tasks() if c["detail"].get("week_goal"))
    assert [(b["day"], b["start"], b["event_id"]) for b in task["detail"]["blocks"]] == [("2026-11-19", "09:00", "ev1")]
    # A second thing on the same morning replaces the first, whose event is deleted.
    planning.place("todo:x", "Build the cafe", "2026-11-19")
    assert calendar_calls[-2][:2] == ["-X", "DELETE"]
    assert _blocks("2026-11-19") == ["todo:x"]
    with pytest.raises(planning.PlanningError):
        planning.retime("2026-11-19", "11:00", "10:00")
    planning.unplace("2026-11-19")
    assert list(planning.all_placed()) == []


def test_inbox_files_by_itself_and_never_into_week(plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(planning, "read_capture",
                        lambda text: {"dest": "someday" if "idea" in text else "today", "due": "", "project": ""})
    idea = planning.capture("idea: a podcast about bodies")
    todo = planning.capture("Call the dentist")
    assert planning.someday_text().startswith("- idea: a podcast about bodies\n")
    assert store.load()["items"][todo["ref"]]["title"] == "Call the dentist"
    assert planning.summary()["in_week"] == {}
    assert [c["text"] for c in planning.inbox()] == ["Call the dentist", "idea: a podcast about bodies"]
    # A Someday capture is set aside as Someday, never on Today's lists.
    with conversations.opened(store.DATA_DIR) as db:
        assert not any(c["title"].startswith("idea") for c in commitment_lists.lists(db)["parked"])
    planning.move_capture(todo["id"], "week")
    assert todo["ref"] in planning.summary()["in_week"]
    assert planning.inbox()[0]["dest"] == "week"
    planning.delete_capture(idea["id"])
    assert "podcast" not in planning.someday_text()
    planning.delete_capture(todo["id"])
    assert store.load()["items"][todo["ref"]]["status"] == "cleared"
    assert planning.summary()["in_week"] == {}
    assert planning.inbox() == []


def test_a_capture_takes_the_date_and_project_user_words_give(plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The user, 2026-11-17: "Ping Peter about running church again this weekend then text group"
    came in with no date. The capture's one model call reads the date (and a project of the user's)."""
    asked: list[str] = []

    def answer(prompt: str, **kw: Any) -> dict[str, str]:
        asked.append(kw["system"])
        return {"dest": "today", "due": "2099-11-20", "project": "nope"}

    monkeypatch.setattr(planning.ai, "complete_json", answer)
    got = planning.read_capture("Ping Peter about running church again this weekend then text group")
    assert got == {"dest": "today", "due": "2099-11-20", "project": "nope"}
    assert "Today is " in asked[0] and "{PROJECTS}" not in asked[0] and "{TODAY}" not in asked[0]
    # A date already past is a misreading, not a deadline.
    monkeypatch.setattr(planning.ai, "complete_json", lambda prompt, **kw: {"dest": "today", "due": "2001-01-01"})
    assert planning.read_capture("Call the dentist")["due"] == ""

    monkeypatch.setattr(planning, "read_capture", lambda text: {"dest": "today", "due": "2099-11-20", "project": ""})
    todo = planning.capture("Ping Peter about running church again this weekend then text group")
    with conversations.opened(store.DATA_DIR) as db:
        c = commitments.by_legacy_key(db, todo["ref"])
        assert c and c["due"] == "2099-11-20" and c["detail"]["due_set_by"] == "user"
        assert commitments.history(db, c["id"])[-1]["actor"] == "user"


def test_someday_is_user_outline_word_for_word(plan_dir: Path) -> None:
    assert planning.someday_text() == ""  # nothing yet: an empty outline, nothing copied in
    planning.save_someday("- New idea\n")
    assert planning.someday_text() == "- New idea\n"


def test_a_stretch_goal_is_treated_like_any_other() -> None:
    note = weekly.parse("Goals:\nShip the essay\nTalk to users (stretch)\n")
    assert note["goals"][1]["stretch"]
    assert [g["text"] for g in weekly.now_goals(note, {"done": {}})] == ["Ship the essay", "Talk to users (stretch)"]



def test_a_voice_memo_and_its_reaction_read_as_what_they_were() -> None:
    raw = {"source": "text", "channel_label": "WhatsApp text with Theo", "messages": [
        {"ts": 1.0, "id": "1", "name": "the user", "text": "[voice message]", "note": "got a reaction ❤️"},
    ]}
    assert "the user: [voice message] (got a reaction ❤️)" in work.transcript(raw)


def test_a_finished_big_thing_is_marked_done_for_the_morning_block() -> None:
    lists: dict[str, list[dict[str, Any]]] = {"goals": [], "also": []}
    week = {"goals": [{"id": "g1", "text": "Write the product vision", "done": True}]}
    steps = {"goal:g1": {"done": [{"text": "Draft it", "at": "2026-11-13T10:00:00"}, {"text": "Old", "at": "2026-11-12T10:00:00"}]}}
    it = planning.resolve("goal:g1", lists, {"title": "Write it", "day": "2026-11-13"}, week, {}, steps)
    assert it["done"] and it["text"] == "Write the product vision" and it["done_steps"] == ["Draft it"]
    todo = planning.resolve("todo:k", lists, {"title": "Build the cafe"}, None, {"k": {"status": "done"}}, {})
    assert todo["done"]


def test_a_finished_morning_stays_and_the_next_big_thing_stacks_under_it(
    plan_dir: Path, calendar_calls: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user, 2026-11-13: dragging a second big thing onto a morning whose first is done
    must not replace the done one; the second stacks under it, from where the first ended."""
    monkeypatch.setattr(weekly, "WEEKLY_DIR", plan_dir / "weekly")
    store.save_raw("calendar", {"tz": "America/Chicago", "events": []})
    day = (date.today() + timedelta(days=7)).isoformat()  # a day ahead: today's would start from now
    store.mutate(lambda s: s["items"].update(
        x={"key": "x", "status": "done", "title": "Book the rental car", "manual": True, "source": "manual"},
        y={"key": "y", "status": "open", "title": "Draft onboarding", "manual": True, "source": "manual"},
        z={"key": "z", "status": "open", "title": "Write the vision", "manual": True, "source": "manual"}))
    planning.place("todo:x", "Book the rental car", day, "08:00", "09:30")
    second = planning.place("todo:y", "Draft onboarding", day)
    assert second["stacked"] and (second["start"], second["end"]) == ("09:30", "13:00")
    assert _blocks(day) == ["todo:x", "todo:y"]
    # An unfinished stacked one is what a third replaces; the finished first one stays.
    planning.place("todo:z", "Write the vision", day)
    assert _blocks(day) == ["todo:x", "todo:z"]
    assert calendar_calls[-2][:2] == ["-X", "DELETE"]
    week = planning.week_view(day, None, store.load()["items"])
    shown = next(d for d in week["days"] if d["date"] == day)
    assert shown["placed"]["item"]["done"] and [q["ref"] for q in shown["stacked"]] == ["todo:z"]
    assert week["placed_refs"] == {"todo:x": day, "todo:z": day}
    planning.retime(day, "10:00", "12:00", "todo:z")
    assert next(b for d, b in planning.all_placed() if b["ref"] == "todo:z")["start"] == "10:00"
    # Taking the finished one off moves the stacked one up.
    planning.unplace(day, "todo:x")
    assert _blocks(day) == ["todo:z"]
