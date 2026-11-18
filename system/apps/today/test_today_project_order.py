"""Projects sort by what the user has to do (the user, 2026-11-16): a project with a task due comes
first, soonest first; then those with tasks and no date; one with nothing to do sits last."""

from today import project_book


def _row(title: str, due: str = "", open_: int = 1) -> dict:
    return {"title": title, "due": due, "open": open_}


def test_projects_sort_by_soonest_due_then_undated_then_empty() -> None:
    rows = [_row("empty", open_=0), _row("undated"), _row("later", "2026-12-02"), _row("soon", "2026-11-16")]
    assert [r["title"] for r in sorted(rows, key=project_book._urgency)] == ["soon", "later", "undated", "empty"]


def test_undated_projects_keep_user_order() -> None:
    rows = [_row("a"), _row("b", open_=0), _row("c")]
    assert [r["title"] for r in sorted(rows, key=project_book._urgency)] == ["a", "c", "b"]
