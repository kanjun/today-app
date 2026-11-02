"""Goals say what the user is trying to do, read every conversation and meeting linked to them,
re-read when any of those change, and take the user's notes on what a goal is really about."""

from pathlib import Path
from typing import Any

import pytest
from today import actions, calendar_events, goals, intent, work


def _item(key: str, status: str = "open", **extra: Any) -> dict[str, Any]:
    return {"key": key, "status": status, "state": "mine", "source": "text", **extra}


def _goal(chain: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "id": "g1",
        "title": "Alex looped in",
        "status": "open",
        "chain": chain,
        **extra,
    }


def _step(step: str, covers: list[str], **extra: Any) -> dict[str, Any]:
    return {"id": step[:4], "step": step, "who": "you", "covers": covers, **extra}


def test_conversation_ids() -> None:
    assert (
        intent.conversation_id({"source": "slack", "ref": {"channel": "C1", "ts": "1"}})
        == "slack:C1"
    )
    assert intent.conversation_id({"source": "text", "ref": {"chat_id": "!a:b"}}) == ""
    assert (
        intent.conversation_id(
            {"source": "email", "account": "k@x", "ref": {"thread_id": "t9"}}
        )
        == "email:k@x:t9"
    )
    assert intent.conversation_id({"source": "manual"}) == ""


def test_reading_is_written_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intent.store, "now_iso", lambda: "2026-10-23T10:00:00")
    state = {
        "items": {
            "call": _item("call", status="done"),
            "note": _item("note"),
            "morgan": _item("morgan"),
            "stale": _item("stale"),
        },
        "goals": {
            "g1": _goal(
                [
                    _step("Call Alex after missing 9pm", ["call"]),
                    _step("Voice note", ["note"]),
                    _step("Ask Morgan", ["morgan"]),
                    _step("Old ask", ["stale"]),
                ]
            )
        },
    }
    intent.apply(
        state,
        "g1",
        {
            "title": "Monday's dinner with Riley and Alex",
            "purpose": "Host a grounded dinner on AI and humans, with Alex included remotely.",
            "story": "Alex can't come in person; you called him Oct 22.",
            "steps": [
                {"step": "Call Alex", "who": "you", "covers": ["call"], "done": True},
                {
                    "step": "Host the dinner with Riley",
                    "who": "you",
                    "covers": [],
                    "done": False,
                },
                {
                    "step": "Leave Alex a voice note",
                    "who": "you",
                    "covers": ["note"],
                    "done": False,
                },
            ],
            "settled": [
                {"key": "stale", "reason": "Sam handled it in #ext-northwind"}
            ],
            "not_part": ["morgan"],
            "links": {"conversations": ["slack:C9"], "events": ["ev1"]},
            "signature": "sig",
        },
    )
    goal = state["goals"]["g1"]
    assert goal["title"] == "Monday's dinner with Riley and Alex"
    assert goal["purpose"].startswith("Host a grounded dinner")
    assert [s["step"] for s in goal["chain"]][:3] == [
        "Call Alex",
        "Host the dinner with Riley",
        "Leave Alex a voice note",
    ]
    assert state["items"]["stale"]["status"] == "cleared"
    assert "northwind" in state["items"]["stale"]["cleared_reason"]
    assert state["items"]["morgan"]["goal_id"] is None
    assert all("morgan" not in s["covers"] for s in goal["chain"])
    # The settled task is still listed, as finished; nothing is dropped silently.
    assert any(s["covers"] == ["stale"] for s in goal["chain"])
    assert goal["links"] == {"conversations": ["slack:C9"], "events": ["ev1"]}
    assert goal["intent_sig"] == "sig"


def test_user_title_survives_a_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intent.store, "now_iso", lambda: "x")
    state = {
        "items": {"a": _item("a")},
        "goals": {"g1": _goal([_step("A", ["a"])], title="Mine", user_title=True)},
    }
    intent.apply(
        state, "g1", {"title": "Theirs", "steps": [{"step": "A", "covers": ["a"]}]}
    )
    assert state["goals"]["g1"]["title"] == "Mine"


def test_steps_no_task_covers_count_when_done() -> None:
    goal = _goal([_step("Host the dinner", [], done=True), _step("Voice note", ["n"])])
    p = goals.progress(goal, {"n": _item("n")})
    assert [s["done"] for s in p["steps"]] == [True, False]
    assert p["next_index"] == 1


def test_taking_a_task_out_keeps_worked_out_steps() -> None:
    state = {
        "items": {"a": _item("a", goal_id="g1"), "b": _item("b", goal_id="g1")},
        "goals": {
            "g1": _goal(
                [_step("Host the dinner", []), _step("A", ["a"]), _step("B", ["b"])]
            )
        },
    }
    goals._take_out(state, "a")
    assert [s["step"] for s in state["goals"]["g1"]["chain"]] == [
        "Host the dinner",
        "B",
    ]


def test_one_task_with_more_steps_is_still_a_goal() -> None:
    state = {
        "items": {"a": _item("a"), "b": _item("b")},
        "goals": {
            "g1": _goal([_step("Host the dinner", []), _step("A", ["a"])]),
            "g2": dict(_goal([_step("B", ["b"])]), id="g2"),
        },
    }
    goals.close_finished(state)
    assert state["goals"]["g1"]["status"] == "open"
    assert state["goals"]["g2"]["status"] == "dissolved"


def test_signature_moves_with_any_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calendar_events, "events", lambda ids: [])
    state = {"items": {"a": _item("a")}}
    goal = _goal([_step("A", ["a"])])
    convs = {"text:x": {"messages": [{"ts": 1.0}]}}
    base = intent.signature(goal, state, convs)
    assert (
        intent.signature(
            goal, state, {"text:x": {"messages": [{"ts": 1.0}, {"ts": 2.0}]}}
        )
        != base
    )
    noted = dict(
        goal, intent_notes=[{"text": "It's about staying close to Alex", "at": "t"}]
    )
    assert intent.signature(noted, state, convs) != base
    state["items"]["a"]["status"] = "done"
    assert intent.signature(goal, state, convs) != base


def test_user_note_is_kept_and_seen_by_regrouping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(intent.store, "now_iso", lambda: "t1")
    state: dict[str, Any] = {"items": {}, "goals": {"g1": _goal([])}}
    intent.add_note(state, "g1", "Alex is leaving the Bay; I want to stay close")
    assert state["goals"]["g1"]["intent_notes"][0]["text"].startswith("Alex is leaving")
    assert state["goals"]["g1"]["rethinking"] is True
    assert state["corrections"][-1]["kind"] == "intent"
    assert goals._goal_brief(state["goals"]["g1"])["user_notes"] == [
        "Alex is leaving the Bay; I want to stay close"
    ]
    intent.remove_note(state, "g1", "t1")
    assert state["goals"]["g1"]["intent_notes"] == []


def test_next_step_someone_else_does_waits_on_them() -> None:
    goal = _goal(
        [_step("Casey sends the summary", [], who="Casey"), _step("A", ["a"])]
    )
    row = actions.goal_summary(goal, {"a": _item("a")})
    assert row is not None and row["state"] == "waiting"


def test_settled_elsewhere_reads_what_came_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sam's question matches the search; the answer that settles it does not."""

    def slack_get(method: str, params: dict[str, str]) -> dict[str, Any]:
        if method == "search.messages":
            assert params["sort"] == "score"
            return {
                "messages": {
                    "matches": [
                        {
                            "ts": "1792696169.0",
                            "user": "UB",
                            "username": "samlee",
                            "channel": {"id": "CK", "name": "ext-northwind"},
                            "text": "can we repurpose the monthly northwind sync on Monday for this zoom?",
                        }
                    ]
                }
            }
        assert method == "conversations.history" and params["channel"] == "CK"
        return {
            "messages": [
                {
                    "ts": "1792704771.0",
                    "user": "UB",
                    "text": "sure 10a PT on Monday works for us. Will update the cal",
                }
            ]
        }

    monkeypatch.setattr(work.connectors, "slack_get", slack_get)
    monkeypatch.setattr(work.connectors, "gmail_search", lambda *a, **k: [])
    monkeypatch.setattr(work, "slack_me", lambda: "UK")
    monkeypatch.setattr(work, "_slack_names", lambda: {"UB": "Sam"})
    hits = work.sent_since({"asked_at": "2026-10-21T10:00:00"}, ["Northwind zoom"])
    assert any("from samlee" in h["where"] for h in hits)
    assert any(
        "right after, from Sam" in h["where"] and "10a PT" in h["text"] for h in hits
    )


def test_calendar_says_which_meetings_are_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {
        "fetched_at": "x",
        "events": [
            {
                "id": "ev1",
                "summary": "Monthly Northwind sync",
                "start": {"dateTime": "2026-10-26T10:00:00-07:00"},
                "hangoutLink": "https://meet.google.com/x",
            },
            {
                "id": "ev2",
                "summary": "Dinner",
                "start": {"dateTime": "2026-10-26T18:30:00-07:00"},
            },
        ],
    }
    monkeypatch.setattr(calendar_events.store, "load_raw", lambda key: record)
    brief = calendar_events.brief(ids=True)
    assert "[ev1]" in brief and "(call on Google Meet)" in brief
    assert "Dinner (call" not in brief
    assert [e["id"] for e in calendar_events.events(["ev2", "gone"])] == ["ev2"]


@pytest.mark.frontend
def test_goal_page_takes_her_word_on_intent() -> None:
    page = (Path(__file__).parent / "src/today/assets/app.html").read_text()
    assert "What you're trying to do" in page and "Where it stands" in page
    assert "/api/goal/note" in page and "Rethink this goal" in page
    assert "Read from:" in page


def test_a_linked_conversation_is_kept_and_labelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: dict[str, Any] = {}
    monkeypatch.setattr(intent.store, "load_raw", lambda key: saved.get(key))
    monkeypatch.setattr(
        intent.store, "save_raw", lambda key, raw: saved.__setitem__(key, raw)
    )
    monkeypatch.setattr(
        intent.connectors, "gmail_thread", lambda thread, account: {"id": thread}
    )
    monkeypatch.setattr(
        intent.connectors,
        "thread_messages",
        lambda thread: [
            {"subject": "Dinner Monday", "text": "see you at 6:30", "id": "m1"}
        ],
    )
    monkeypatch.setattr(calendar_events, "events", lambda ids: [])
    goal = _goal([], links={"conversations": ["email:k@x:t1"], "events": []})
    state: dict[str, Any] = {"items": {}}
    convs = intent.conversations(goal, state)
    assert convs["email:k@x:t1"]["subject"] == "Dinner Monday"
    assert saved["conv:email:k@x:t1"]["messages"][0]["text"] == "see you at 6:30"
    assert intent.sources(goal, {})[0]["label"] == "Email: Dinner Monday"


def test_a_finished_task_left_out_leaves_the_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its old wording ("after missing 9pm") must not come back as a step."""
    monkeypatch.setattr(intent.store, "now_iso", lambda: "x")
    state = {
        "items": {
            "call": _item("call", status="done", goal_id="g1"),
            "note": _item("note", goal_id="g1"),
        },
        "goals": {
            "g1": _goal(
                [
                    _step("Call Alex after missing 9pm", ["call"]),
                    _step("Note", ["note"]),
                ]
            )
        },
    }
    intent.apply(
        state,
        "g1",
        {"steps": [{"step": "Leave Alex a voice note", "covers": ["note"]}]},
    )
    assert [s["step"] for s in state["goals"]["g1"]["chain"]] == [
        "Leave Alex a voice note"
    ]
    assert state["items"]["call"]["goal_id"] is None


def test_newest_copy_of_an_email_thread_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raws = {
        "a": {"messages": [{"internal_ms": 1000}, {"internal_ms": 2000}]},
        "b": {
            "messages": [
                {"internal_ms": 1000},
                {"internal_ms": 2000},
                {"internal_ms": 9000},
            ]
        },
    }
    monkeypatch.setattr(intent.store, "load_raw", lambda key: raws.get(key))
    items = {
        k: {"key": k, "source": "email", "account": "k@x", "ref": {"thread_id": "t"}}
        for k in raws
    }
    assert len(intent._stored("email:k@x:t", items)["messages"]) == 3


def test_user_step_order_holds_through_a_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(intent.store, "now_iso", lambda: "x")
    state: dict[str, Any] = {
        "items": {"a": _item("a"), "b": _item("b")},
        "goals": {
            "g1": _goal(
                [_step("Do A", ["a"]), _step("Do B", ["b"]), _step("Host it", [])]
            )
        },
    }
    intent.reorder(
        state, "g1", ["Do B", "Host", "Do A"]
    )  # step ids are the first 4 letters
    goal = state["goals"]["g1"]
    assert [s["step"] for s in goal["chain"]] == ["Do B", "Host it", "Do A"]
    assert goal["ordered_by_user"] and state["corrections"][-1]["kind"] == "order"
    # A reading that puts them back in its own order, with one new step after "Do A".
    intent.apply(
        state,
        "g1",
        {
            "steps": [
                {"step": "Do A", "covers": ["a"]},
                {"step": "Tell them", "covers": []},
                {"step": "Do B", "covers": ["b"]},
                {"step": "Host it", "covers": []},
            ]
        },
    )
    assert [s["step"] for s in goal["chain"]] == [
        "Do B",
        "Host it",
        "Do A",
        "Tell them",
    ]


@pytest.mark.frontend
def test_goal_steps_can_be_moved() -> None:
    page = (Path(__file__).parent / "src/today/assets/app.html").read_text()
    assert "/api/goal/reorder" in page and "data-step=" in page and "dragStep" in page
