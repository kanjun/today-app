"""The user's weekly priorities: kept word for word per week, read for the #1 line and the goals,
and the goals' next moves fill Now."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from today import act, actions, connectors, page, store, weekly

NOTE = """My #1 priority this week: improve customer onboarding, with the goal of improving cohort retention toward "engaged WAU".

Goals:

Study consumer-grade chat agent onboardings: Instinct, Muse, and Grokbot
Work with a designer (Felix or Paul) to draft up a simpler version of our onboarding
Get feedback from 5+ users on why they haven't retained (stretch, am traveling to friend's wedding Thu/Fri)
Write out my thoughts on product direction and hypotheses
Get "Who does your AI serve?" to a near-publishable place
"""


def _say(key: str, said: str, focus: str = "", label: str = "") -> dict[str, Any]:
    """What a task card's box sends when the user says or taps something."""
    return act.ask({"kind": act.TASK, "key": key, "focus": focus}, said, label)


@pytest.fixture
def week_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(weekly, "WEEKLY_DIR", tmp_path / "Weekly priorities")
    monkeypatch.setattr(weekly, "WRITING_DIR", tmp_path / "canvas")
    monkeypatch.setattr(weekly, "WRITING_LINK", "http://writing.localhost/#doc/")
    monkeypatch.setattr(weekly, "WRITING_START", "http://writing.localhost/")
    (tmp_path / "canvas").mkdir()
    (tmp_path / "canvas" / "who-does-your-ai-serve.md").write_text(
        '---\ntitle: "Who does your AI serve?"\n---\n\nFirst line.\n\nWe should ask who it serves.\n'
        "If your technology serves **you**, what will you do with it?[^a]\n\n---\n# Acknowledgements\nThanks all.\n\n[^a]: a note\n"
    )
    return weekly.WEEKLY_DIR


def test_user_note_is_read_in_user_own_words() -> None:
    p = weekly.parse(NOTE)
    assert p["priority"].startswith("My #1 priority this week: improve customer onboarding")
    assert p["focus"] == "improve customer onboarding"
    texts = [g["text"] for g in p["goals"]]
    assert texts == [line for line in NOTE.splitlines()[4:] if line.strip()]
    stretch = p["goals"][2]
    assert stretch["stretch"] and stretch["away_days"] == [3, 4]  # Thu, Fri
    assert not any(g["stretch"] or g["away_days"] for g in p["goals"][:2] + p["goals"][3:])


def test_parsing_tolerates_format_drift() -> None:
    p = weekly.parse(
        "Top priority: ship the product launch post\n\n- draft the post\n* get Elsa's edits\n2) send it Wednesday\n"
    )
    assert p["priority"] == "Top priority: ship the product launch post"
    assert [g["text"] for g in p["goals"]] == ["draft the post", "get Elsa's edits", "send it Wednesday"]
    assert weekly.away_days("out Wed-Fri for a trip") == [2, 3, 4]
    assert weekly.away_days("traveling Thursday and Friday") == [3, 4]


def test_the_note_is_kept_verbatim_per_week_and_carries_over(week_dir: Path) -> None:
    path = weekly.save(NOTE)
    assert path.name == f"{weekly.week_start().isoformat()}.md" and path.read_text() == NOTE
    weekly.save(NOTE + "\nOne more goal\n")
    assert (week_dir / ".versions").is_dir() and len(list((week_dir / ".versions").iterdir())) == 1
    # Last week's note stays findable, and the newest one up to this week is the current one.
    old = week_dir / (weekly.week_start() - timedelta(days=7)).isoformat()
    old.with_suffix(".md").write_text("My #1 priority: rest\n")
    cur = weekly.current()
    assert cur is not None and cur["text"].endswith("One more goal\n")
    path.unlink()
    cur = weekly.current()
    assert cur is not None and cur["priority"] == "My #1 priority: rest"


def test_a_goal_tick_lives_on_that_weeks_task(week_dir: Path) -> None:
    weekly.save(NOTE)
    gid = weekly.parse(NOTE)["goals"][0]["id"]
    weekly.set_done(gid, True)
    assert list(weekly.done_goals(weekly.week_start().isoformat())) == [gid]
    assert not (week_dir / f"{weekly.week_start().isoformat()}.done.json").exists()  # nothing beside the user's note
    weekly.set_done(gid, False)
    assert weekly.done_goals(weekly.week_start().isoformat()) == {}


def test_where_user_left_off_is_user_last_lines_word_for_word(week_dir: Path) -> None:
    docs = weekly.writing_docs()
    assert docs == [{"id": "who-does-your-ai-serve", "title": "Who does your AI serve?"}]
    assert weekly.doc_for('Get "Who does your AI serve?" to a near-publishable place', docs) == docs[0]
    assert weekly.last_lines("who-does-your-ai-serve", 2) == (
        "We should ask who it serves.\nIf your technology serves **you**, what will you do with it?[^a]"
    )


def _wire(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    monkeypatch.setattr(store, "mutate", lambda fn: fn(state))
    monkeypatch.setattr(store, "load", lambda: state)
    monkeypatch.setattr(store, "load_raw", lambda key: None)
    monkeypatch.setattr(store, "roll_today", lambda s, d: None)
    monkeypatch.setattr(weekly, "_reachable", lambda url: "real" in url)
    monkeypatch.setattr(weekly, "slack_people", lambda: {"U1": "Felix Moreau"})
    monkeypatch.setattr(weekly, "plan_soon", lambda force=False: None)


def _plan(state: dict[str, Any]) -> None:
    note = weekly.current()
    assert note is not None
    answer = {
        "goals": [
            {"id": note["goals"][0]["id"], "serves_priority": True, "move": {"kind": "go_do", "title": "Try Instinct, Muse and Grokbot", "links": [{"label": "Muse", "url": "https://real.muse"}, {"label": "Made up", "url": "https://fake.x"}]}},
            {"id": note["goals"][1]["id"], "serves_priority": True, "move": {"kind": "message", "title": "Ask Felix or Paul to sketch it with you", "people": ["Felix", "Paul"]}},
            {"id": note["goals"][2]["id"], "move": {"kind": "message", "title": "Ask five users why they stopped", "people": ["Mei"]}},
            # The reader skipped the last two goals: they still get moves.
        ]
    }
    assert weekly.apply_plan(note, note["goals"], answer) == 5


def test_every_goal_gets_a_move_and_writing_goals_keep_writing(
    week_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, Any] = {"items": {}, "goals": {}, "today": {"order": [], "date": date.today().isoformat()}}
    _wire(monkeypatch, state)
    weekly.save(NOTE)
    _plan(state)
    moves = {i["goal_text"][:12]: i for i in state["items"].values()}
    assert moves["Study consum"]["move"]["links"] == [{"label": "Muse", "url": "https://real.muse"}]
    designer = moves["Work with a "]
    assert designer["state"] == "you" and designer["questions"][0]["options"] == ["Felix", "Paul"]
    assert designer["move"]["people"][0]["slack_id"] == "U1"
    product = moves["Write out my"]["move"]
    assert product["kind"] == "write" and product["doc"] is None and product["start_url"]
    essay = moves['Get "Who doe']
    assert essay["title"] == "Keep writing Who does your AI serve?"
    assert essay["move"]["doc"]["id"] == "who-does-your-ai-serve"


def test_goals_fill_now_in_user_order_and_only_true_urgency_cuts_in(
    week_dir: Path, monkeypatch: pytest.MonkeyPatch, tasks: Any
) -> None:
    def ago(days: float) -> str:
        return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")

    state: dict[str, Any] = {
        "items": {
            # Robin's question about next Tuesday, asked two days ago: not urgent today.
            "robin": {"key": "robin", "status": "open", "kind": "asked", "state": "you", "source": "slack", "who": "Robin", "asked_at": ago(2), "due": (date.today() + timedelta(days=4)).isoformat(), "questions": [{"q": "?", "options": ["a"]}]},
            "hana": {"key": "hana", "status": "open", "kind": "asked", "state": "you", "source": "slack", "who": "Hana", "asked_at": ago(9), "questions": [{"q": "?", "options": ["a"]}]},
        },
        "goals": {},
        "today": {"order": [], "date": date.today().isoformat()},
    }
    _wire(monkeypatch, state)
    tasks.seed(state)
    weekly.save(NOTE)
    _plan(state)
    monkeypatch.setattr(weekly, "away_today", lambda g, today=None: bool(g.get("away_days")))
    v = actions.view_state()["page"]
    now = [r["goal"][:12] or r["key"] for r in v["now"]]
    # The stretch goal (also away today) is skipped; goals fill all three slots in the user's order.
    assert now == ["Study consum", "Work with a ", "Write out my"]
    assert all(r["goal"] and not r["who"] for r in v["now"])
    assert {r["key"] for r in v["answers"]} == {"robin", "hana"}
    week = v["week"]
    assert [g["in_now"] for g in week["goals"]] == [True, True, False, True, False]
    assert week["goals"][4]["move"]["title"] == "Keep writing Who does your AI serve?"
    assert week["goals"][4]["status"] == "draft in progress"
    # A real deadline tomorrow takes one slot; the rest stay the user's goals.
    tasks.set("robin", due=(date.today() + timedelta(days=1)).isoformat())
    now = [r["key"] for r in actions.view_state()["page"]["now"]]
    assert len(now) == 3 and now[-1] == "robin" and sum(k.startswith("goalmove:") for k in now) == 2
    # Ticking a goal off takes its move out of Now; the next goal's move takes the slot.
    tasks.set("robin", due="")
    weekly.set_done(weekly.parse(NOTE)["goals"][0]["id"], True)
    v = actions.view_state()["page"]
    assert [r["goal"][:12] for r in v["now"]] == ["Work with a ", "Write out my", 'Get "Who doe']
    assert v["week"]["goals"][0]["done"]


def test_a_goal_message_is_written_for_the_chosen_person_then_sent_by_slack_dm(
    week_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state: dict[str, Any] = {"items": {}, "goals": {}, "today": {"order": [], "date": date.today().isoformat()}}
    _wire(monkeypatch, state)
    weekly.save(NOTE)
    _plan(state)
    key = next(k for k, i in state["items"].items() if i["move"]["kind"] == "message" and "designer" in i["goal_text"])
    # The card's assistant writes it for the person the user picked (the user, 2026-11-14: one assistant).
    monkeypatch.setattr(act.ai, "complete_json", lambda prompt, **k: {"did": "Wrote it.", "operations": [
        {"op": "draft", "text": "hey Felix, want to sketch it with me?", "to": "Felix"}], "next": []})
    monkeypatch.setattr(act.calendar_events, "brief", lambda **_: "")
    _say(key, "ask Felix")
    item = state["items"][key]
    assert item["state"] == "ready" and item["draft"] == "hey Felix, want to sketch it with me?"
    assert actions.send_target(item)["slack_id"] == "U1"
    # Sending: nothing leaves a dry run; the move is done and the goal's next move is planned.
    monkeypatch.setenv("TODAY_DRY_SEND", "1")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(actions, "_archive", lambda key: "")
    assert actions.send(key, item["draft"]) == "Sent to Felix"
    log = [json.loads(line) for line in (tmp_path / "dry_sends.jsonl").read_text().splitlines()]
    assert "conversations.open" in log[0]["detail"]["args"][-1]
    assert "hey Felix" in log[1]["detail"]["args"][-2]
    assert item["status"] == "sent"
    assert state["weekly"]["steps_done"][item["weekly_goal"]] == [item["title"]]


def test_user_notes_on_a_go_do_move_are_kept_beside_user_weekly_notes(
    week_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, Any] = {"items": {}, "goals": {}, "today": {"order": [], "date": date.today().isoformat()}}
    _wire(monkeypatch, state)
    weekly.save(NOTE)
    _plan(state)
    key = next(k for k, i in state["items"].items() if i["move"]["kind"] == "go_do" and "Study" in i["goal_text"])
    path = Path(actions.save_move_notes(key, "Muse asks for your name first."))
    assert path.parent == week_dir / "Notes" and "Muse asks for your name first." in path.read_text()
    assert state["items"][key]["notes"] == "Muse asks for your name first."


def test_slack_dm_channel_is_opened_by_post(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(connectors, "_curl", lambda args, account=None, timeout=120: seen.append(args) or {"ok": True, "channel": {"id": "D9"}})
    assert connectors.slack_open_dm("U1") == "D9"
    assert seen[0][-1].endswith("conversations.open")


def test_the_page_has_this_week_first_and_no_projects_section() -> None:
    html = (Path(page.__file__).parent / "assets" / "app.html").read_text()
    assert "Projects this week" not in html
    assert html.index("h+=weekHtml(P.week)") < html.index('<h2 class="sec">Now</h2>')
    assert "Mark this goal done. It stays on the list, crossed out." in html
    css = html[html.index("<style>") : html.index("@media (max-width:600px)")]
    assert set(__import__("re").findall(r"font-size:\s*([\d.]+px)", css)) == {"15px"}
    assert ".item.forgoal{border-left:3px solid #9b84c4}" in css


def _others(days_ago: float = 1) -> dict[str, dict[str, Any]]:
    """Things from other people waiting below Now: a quick answer, a reply and a to-do."""
    at = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    return {
        "todo": {"key": "todo", "status": "open", "kind": "asked", "state": "mine", "source": "slack", "who": "Rosa", "asked_at": at, "title": "Watch the takehomes"},
        "ask": {"key": "ask", "status": "open", "kind": "asked", "state": "you", "source": "slack", "who": "Hana", "asked_at": at, "questions": [{"q": "?", "options": ["a"]}]},
        "reply": {"key": "reply", "status": "open", "kind": "asked", "state": "ready", "source": "slack", "who": "Ben", "asked_at": at, "draft": "yes!"},
    }


def test_now_is_empty_when_every_goal_is_done(
    week_dir: Path, monkeypatch: pytest.MonkeyPatch, tasks: Any
) -> None:
    """All goals ticked off and nothing urgent: Now stays empty rather than filling with the
    things already listed below, and the This-week box shows every goal crossed out."""
    state: dict[str, Any] = {"items": _others(), "goals": {}, "today": {"order": [], "date": date.today().isoformat()}}
    _wire(monkeypatch, state)
    tasks.seed(state)
    weekly.save(NOTE)
    _plan(state)
    for g in weekly.parse(NOTE)["goals"]:
        weekly.set_done(g["id"], True)
    v = actions.view_state()["page"]
    assert v["now"] == []
    assert {r["key"] for r in v["answers"] + v["replies"] + v["todos"]} == {"ask", "reply", "todo"}
    assert all(g["done"] and not g["in_now"] for g in v["week"]["goals"])


def test_now_never_backfills_when_goal_moves_run_short(
    week_dir: Path, monkeypatch: pytest.MonkeyPatch, tasks: Any
) -> None:
    state: dict[str, Any] = {"items": _others(), "goals": {}, "today": {"order": [], "date": date.today().isoformat()}}
    _wire(monkeypatch, state)
    tasks.seed(state)
    weekly.save(NOTE)
    _plan(state)
    goals = weekly.parse(NOTE)["goals"]
    for g in goals[1:]:
        weekly.set_done(g["id"], True)
    now = [r["key"] for r in actions.view_state()["page"]["now"]]
    assert len(now) == 1 and now[0].startswith("goalmove:")
    # The one truly urgent thing from someone else still gets its slot.
    tasks.set("ask", due=(date.today() + timedelta(days=1)).isoformat())
    now = [r["key"] for r in actions.view_state()["page"]["now"]]
    assert len(now) == 2 and now[1] == "ask"


def test_a_placeholder_holds_the_goals_slot_until_its_next_move_arrives(
    week_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, Any] = {"items": _others(), "goals": {}, "today": {"order": [], "date": date.today().isoformat()}}
    _wire(monkeypatch, state)
    weekly.save(NOTE)
    _plan(state)
    note = weekly.current()
    assert note is not None
    first = note["goals"][0]
    key = state["weekly"]["moves"][first["id"]]
    monkeypatch.setattr(actions, "_archive", lambda key: "")
    # The user did the move: its goal keeps its place in Now with a quiet placeholder.
    actions.mark_done(key)
    v = actions.view_state()["page"]
    slot = v["now"][0]
    assert slot["placeholder"] and slot["key"] == f"pending:{first['id']}"
    assert slot["words"] == f"Working out the next step for {first['text']}..."
    assert v["week"]["goals"][0]["in_now"]
    # The next move arrives and takes the same place.
    weekly.apply_plan(note, [first], {"goals": [{"id": first["id"], "move": {"kind": "go_do", "title": "Try Grokbot next"}}]})
    v = actions.view_state()["page"]
    assert v["now"][0]["words"] == "Try Grokbot next" and not v["now"][0]["placeholder"]


def test_a_slow_next_move_drops_its_placeholder_after_a_minute(
    week_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, Any] = {"items": _others(), "goals": {}, "today": {"order": [], "date": date.today().isoformat()}}
    _wire(monkeypatch, state)
    weekly.save(NOTE)
    _plan(state)
    goals = weekly.parse(NOTE)["goals"]
    for g in goals[1:]:
        weekly.set_done(g["id"], True)
    key = state["weekly"]["moves"][goals[0]["id"]]
    state["items"][key]["status"] = "done"
    stale = (datetime.now() - timedelta(seconds=weekly.PENDING_SECONDS + 5)).isoformat(timespec="seconds")
    state["weekly"]["pending"][goals[0]["id"]] = stale
    # Nothing holds the slot any more, and nothing fills it: Now is empty.
    assert actions.view_state()["page"]["now"] == []


def test_the_page_draws_the_placeholder_quietly_and_keeps_checking() -> None:
    html = (Path(page.__file__).parent / "assets" / "app.html").read_text()
    assert "r.placeholder?placeholderHtml(r)" in html
    ph = html[html.index("function placeholderHtml(") : html.index("function srcHtml(")]
    assert "check2" not in ph and "data-item" not in ph  # nothing to press, nothing to open
    assert ".list .item.placeholder .words{color:#a39d92}" in (Path(page.__file__).parent / "assets" / "today_list.js").read_text()
    assert 'r.status==="working"||r.placeholder' in html  # the page checks back until it is replaced
