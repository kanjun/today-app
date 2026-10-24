"""The re-check of the user's corrected cases: saving a case and judging an answer against it."""

import json
from typing import Any

from today import casebook


def test_check_reports_each_wrong_reading() -> None:
    expect: list[dict[str, Any]] = [
        {"field": "state", "is": "waiting"},
        {"field": "due", "between": ["2026-11-04", "2026-11-13"]},
        {"promise": {"asked": "Sam", "quote_has": "Priya"}},
        {"no_promise": {"asked": "", "quote_has": "Priya"}},
    ]
    right = {
        "state": "waiting",
        "due": "2026-11-05",
        "promises": [
            {"title": "Sam tells Priya and Jordan", "quote": "", "asked": "Sam"}
        ],
    }
    assert casebook.check(right, expect) == []
    wrong = {
        "state": "ready",
        "due": "",
        "promises": [
            {
                "title": "Tell Priya and Jordan",
                "quote": "can you let Priya know",
                "asked": "",
            }
        ],
    }
    assert len(casebook.check(wrong, expect)) == 4


def test_add_keeps_only_what_the_reader_is_given(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    item = {
        "key": "k",
        "kind": "asked",
        "ask_text": "hi",
        "state": "ready",
        "draft": "x",
    }
    monkeypatch.setattr(casebook.store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(casebook.store, "load", lambda: {"items": {"k": item}})
    monkeypatch.setattr(
        casebook.store, "load_raw", lambda key: {"messages": [{"text": "hi"}]}
    )
    monkeypatch.setattr(casebook.store, "now_iso", lambda: "2026-10-23T10:00:00")
    path = casebook.add("c", "k", "why", [{"field": "state", "is": "waiting"}])
    case = json.loads(path.read_text())
    assert case["item"] == {"key": "k", "kind": "asked", "ask_text": "hi"}
    assert case["raw"]["messages"][0]["text"] == "hi"


def test_run_replays_each_case_through_the_reader(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(casebook.store, "DATA_DIR", tmp_path)
    (tmp_path / "cases").mkdir()
    for name, state in (("a", "waiting"), ("b", "ready")):
        (tmp_path / "cases" / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "item": {"key": name, "s": state},
                    "raw": {},
                    "expect": [{"field": "state", "is": "waiting"}],
                }
            )
        )
    monkeypatch.setattr(
        casebook.work, "take_it_further", lambda item, raw: {"state": item["s"]}
    )
    results = casebook.run()
    assert results["a"] == [] and results["b"]
