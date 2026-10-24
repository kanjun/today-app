"""Something the user asks someone else to do is theirs: it waits on them, it is not their task."""

from typing import Any

from today import sync, work


def test_ask_to_someone_else_becomes_a_wait_on_them(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    state: dict[str, Any] = {
        "items": {
            "k": {"key": "k", "kind": "asked", "source": "slack", "who": "Dana"}
        }
    }
    worked: list[dict[str, Any]] = []
    monkeypatch.setattr(sync.store, "load", lambda: state)
    monkeypatch.setattr(sync.store, "mutate", lambda fn: fn(state))
    monkeypatch.setattr(sync.store, "load_raw", lambda key: {"messages": [{}]})
    monkeypatch.setattr(sync.store, "save_raw", lambda key, raw: None)
    monkeypatch.setattr(sync.store, "now_iso", lambda: "2026-10-23T10:00:00")
    monkeypatch.setattr(
        sync.work, "take_it_further", lambda item, raw: worked.append(dict(item)) or {}
    )
    monkeypatch.setattr(sync, "_apply_result", lambda key, result, sig: None)
    monkeypatch.setattr(sync, "signature", lambda raw: "s")
    sync._add_promises(
        "k",
        [
            {
                "title": "Sam tells Priya and Jordan about the invite",
                "quote": "can you let Priya and Jordan know I got invited?",
                "asked": "Sam",
            },
            {
                "title": "Send Dana the notes",
                "quote": "I'll send notes",
                "asked": "",
            },
        ],
    )
    handed = state["items"]["k:promise:1"]
    assert (handed["kind"], handed["who"]) == ("handed", "Sam")
    mine = state["items"]["k:promise:2"]
    assert (mine["kind"], mine["who"]) == ("promised", "Dana")
    prompt = work._prompt(worked[0], {"messages": []}, "")
    assert "the user asked Sam to do this for them, so it is theirs to do" in prompt
    assert 'Set state "waiting"' in prompt


def test_asked_survives_cleaning() -> None:
    answer = work._clean(
        {
            "promises": [
                {
                    "title": "Sam books it",
                    "quote": "can you book?",
                    "asked": "Sam",
                }
            ]
        }
    )
    assert answer["promises"][0]["asked"] == "Sam"
