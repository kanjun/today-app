"""Projects hold their tasks off the main list; anything due within two days rises to the top."""

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from today import actions, projects


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
        "projects": [{"id": "launch", "title": "Launch"}, {"id": "hiring", "title": "Hiring"}],
    }


@pytest.fixture
def view(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def build(items: list[dict[str, Any]], tasks: Any = None) -> dict[str, Any]:
        state = _state(items)
        if tasks is not None:
            tasks.seed(state)
        monkeypatch.setattr(actions.store, "mutate", lambda fn: fn(state))
        monkeypatch.setattr(actions.store, "load", lambda: state)
        monkeypatch.setattr(actions.store, "roll_today", lambda s, d: None)
        return actions.view_state()

    return build


def test_an_item_is_in_the_project_set_on_it_and_none_by_default() -> None:
    assert projects.of_item({"project": "launch"}) == "launch"
    assert projects.of_item({"group": "anything"}) == ""
    assert projects.of_item({"project": ""}) == ""


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
def test_every_row_has_an_already_handled_circle() -> None:
    """Message rows (Now, quick answers, replies, a project's next move) carry the same
    circle to-dos have: marks it done like "Done already", sends nothing, with Undo."""
    page = (Path(__file__).parent / "src" / "today" / "assets" / "app.html").read_text()
    item = page[page.index("function itemHtml(") : page.index("function srcHtml(")]
    assert item.count('class="check2" data-done=') == 2  # message rows and to-dos
    # Waiting-on rows too: the AI doesn't always see that they got back (the user, 2026-11-13).
    assert 'CHECKABLE = v => ["now","answers","replies","later","waiting"]' in page
    handler = page[page.index("const done=el.querySelector") : page.index("const pull=el.querySelector")]
    assert 'api("/api/done",{key})' in handler and '"Marked done. Nothing sent"' in handler
    assert "/api/undo-finish" in handler
    assert "Already handled it? Mark it done. Nothing is sent." in page


def test_lists_run_oldest_first_and_dropped_off_sits_under_yours_to_do(view, tasks) -> None:  # type: ignore[no-untyped-def]
    """The user, 2026-11-13: oldest first in Yours to do, quick answers and replies; Slipped
    right under Yours to do, bold."""
    today = date.today()
    keys = [f"k{n}" for n in range(6)]
    v = view([_item(k, asked_at=(today - timedelta(days=n + 1)).isoformat()) for n, k in enumerate(keys)], tasks)
    rows = [r["key"] for r in v["page"]["todos"]]
    assert len(rows) >= 3 and rows == sorted(rows, reverse=True)  # k5 is the oldest
    page = (Path(__file__).parent / "src" / "today" / "assets" / "app.html").read_text()
    main = page[page.index("function renderList(") :]
    assert main.index("`Slipped · ") < main.index("Waiting on others · ")
    assert main.index('id="add"') < main.index("`Slipped · ")
