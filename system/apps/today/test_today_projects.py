"""Projects hold their tasks off the main list; anything due within two days rises to the top."""

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from today import actions, config, projects


def _item(key: str, **extra: Any) -> dict[str, Any]:
    return {
        "key": key,
        "status": "open",
        "state": "mine",
        "asked_at": "2026-10-01",
        **extra,
    }


def _state(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "items": {i["key"]: i for i in items},
        "goals": {},
        "today": {"order": [], "date": date.today().isoformat()},
    }


PROJECTS = (
    {"id": "launch", "title": "Spring launch", "about": "Getting the spring launch out."},
    {"id": "hiring", "title": "Hiring", "about": "Open roles and candidates."},
)


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = config.load()._replace(projects=PROJECTS)
    monkeypatch.setattr(config, "load", lambda: settings)


@pytest.fixture
def view(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def build(items: list[dict[str, Any]]) -> dict[str, Any]:
        state = _state(items)
        monkeypatch.setattr(actions.store, "mutate", lambda fn: fn(state))
        monkeypatch.setattr(actions.store, "roll_today", lambda s, d: None)
        return actions.view_state()

    return build


def test_project_tasks_collapse_into_one_row(view) -> None:  # type: ignore[no-untyped-def]
    v = view([_item("a", project="launch"), _item("b", project="launch"), _item("c")])
    assert [i["key"] for i in v["owed"]] == ["c"]
    assert [(p["key"], p["tasks"]) for p in v["projects"]] == [("project:launch", 2)]


def test_a_project_with_nothing_open_does_not_show(view) -> None:  # type: ignore[no-untyped-def]
    v = view([_item("c")])
    assert v["projects"] == []


def test_due_soon_project_task_stays_in_its_project(view) -> None:  # type: ignore[no-untyped-def]
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    later = (date.today() + timedelta(days=9)).isoformat()
    v = view(
        [
            _item("old"),
            _item("late", due=later),
            _item("soon", project="hiring", due=tomorrow, asked_at="2026-10-20"),
        ]
    )
    assert [i["key"] for i in v["owed"]] == ["old", "late"]
    assert v["owed"][1]["due_label"] == ""
    assert v["projects"][0]["due_label"] == "Due tomorrow"


def test_an_item_is_in_a_project_only_when_one_was_set() -> None:
    assert projects.of_item({"project": "launch"}) == "launch"
    assert projects.of_item({"project": ""}) == ""
    assert projects.of_item({}) == ""


def test_due_labels() -> None:
    assert [actions.due_label(d) for d in (-1, 0, 1, 2, 3, None)] == [
        "Overdue",
        "Due today",
        "Due tomorrow",
        "Due in 2 days",
        "",
        "",
    ]


@pytest.mark.frontend
def test_task_rows_have_a_check_off_circle() -> None:
    page = (Path(__file__).parent / "src" / "today" / "assets" / "app.html").read_text()
    assert (
        'function taskRow(i,todayBtn){return `<div class="row" data-key="${esc(i.key)}">${checkBtn(i.key)}'
        in page
    )
    assert page.count("bindChecks(app,") == 2
    assert 'api("/api/done",{key:b.dataset.check})' in page


def test_far_off_dates_sort_after_undated_with_a_quiet_date(view) -> None:  # type: ignore[no-untyped-def]
    far = (date.today() + timedelta(days=14)).isoformat()
    farther = (date.today() + timedelta(days=20)).isoformat()
    v = view(
        [
            _item("farther", due=farther, asked_at="2026-09-01"),
            _item("later", due=far, asked_at="2026-09-02"),
            _item("undated", asked_at="2026-10-20"),
        ]
    )
    assert [i["key"] for i in v["owed"]] == ["undated", "later", "farther"]
    assert v["owed"][1]["due_on"].startswith("Due ")
    assert v["owed"][1]["due_label"] == ""


@pytest.mark.frontend
def test_back_from_a_goal_returns_to_its_project() -> None:
    page = (Path(__file__).parent / "src/today/assets/app.html").read_text()
    assert "goalFrom[id]" in page
    assert '$("back").onclick=()=>go(back.hash)' in page


def test_there_are_no_projects_until_the_user_defines_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(config.store, "DATA_DIR", tmp_path)
    monkeypatch.delenv("TODAY_PROJECTS", raising=False)
    assert projects.defined() == []
    assert projects.prompt_lines() == ""
    (tmp_path / "settings.json").write_text(
        '{"projects": [{"id": "launch", "title": "Spring launch", "about": "The launch."}, {"title": "no id"}]}'
    )
    assert projects.defined() == [
        {"id": "launch", "title": "Spring launch", "about": "The launch."}
    ]
    listed = tmp_path / "projects.json"
    listed.write_text('[{"id": "hiring", "about": "Open roles."}]')
    monkeypatch.setenv("TODAY_PROJECTS", str(listed))
    assert projects.defined() == [
        {"id": "hiring", "title": "hiring", "about": "Open roles."}
    ]
    assert projects.prompt_lines() == '  "hiring": Open roles.'
