"""One assistant behind every card: options are worked out once and kept, what the user taps or
says is applied as one undoable batch with the next options, going back takes it back, and
picking the same option again re-applies it without asking the model again. Nothing sends."""

import json
from typing import Any

import pytest
from today import (
    act,
    ai,
    calendar_events,
    card,
    commitments,
    commitments_import,
    connectors,
    conversations,
    planning,
    project_book,
    runner,
    store,
    task_context,
    task_steps,
    work,
)
from today.project_book import Book

KEY = "slack:D1:1.0"



def _say(key: str, said: str, focus: str = "", label: str = "") -> dict[str, Any]:
    """What a task card's box sends when the user says or taps something."""
    return act.ask({"kind": act.TASK, "key": key, "focus": focus}, said, label)

def _never(*_a: Any, **_k: Any) -> None:
    raise AssertionError("nothing may be sent")


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    for name in ("slack_post_message", "gmail_send_reply", "gmail_send_new", "gmail_set_inbox"):
        monkeypatch.setattr(connectors, name, _never)
    monkeypatch.setattr(calendar_events, "brief", lambda **_: "")
    monkeypatch.setattr(calendar_events, "refresh", lambda: None)
    w: dict[str, Any] = {"answers": [], "prompts": [], "events": [], "deleted": []}

    def complete_json(prompt: str, **_k: Any) -> dict[str, Any]:
        w["prompts"].append(prompt)
        return w["answers"].pop(0)

    monkeypatch.setattr(ai, "complete_json", complete_json)
    monkeypatch.setattr(calendar_events, "add", lambda ev, source="": w["events"].append(ev) or {"id": f"ev{len(w['events'])}", "htmlLink": "https://cal/x"})
    monkeypatch.setattr(planning, "_cal_delete", lambda eid: w["deleted"].append(eid))
    item = {"key": KEY, "status": "open", "source": "slack", "kind": "promised", "state": "mine", "who": "Maya",
            "title": "Talk with Maya about the year plan", "ask_text": "I want to jam with Maya on the year plan",
            "asked_at": "2026-11-06T10:00:00"}
    store.mutate(lambda s: s["items"].update({KEY: item}))
    # Two projects the user set up in the settings (none ship by default).
    (store.DATA_DIR / "settings.json").write_text(json.dumps({"projects": [
        {"id": "onboarding", "title": "Customer onboarding", "about": "getting customers set up"},
        {"id": "fund", "title": "Fund I", "about": "the fund's investors and paperwork"}]}))
    with conversations.opened(store.DATA_DIR) as db:
        commitments_import.run(db, store.load(), {}, None, {})
    return w


def _cards() -> dict[str, Any]:
    return task_context.card_records_now(KEY)


def _item() -> dict[str, Any]:
    return store.load()["items"][KEY]


# ---- writing a card ahead (the old reply writer's job, the user 2026-11-15) ---------------------


def test_writing_ahead_drafts_and_offers_events_but_decides_nothing(world: dict[str, Any]) -> None:
    world["answers"].append({"did": "Wrote Maya a time and offered the jam.",
                             "operations": [{"op": "draft", "text": "Hey Maya, Thu 2pm?"},
                                            {"op": "calendar", "title": "Jam with Maya", "start": "2026-11-19T14:00", "end": "2026-11-19T15:00"},
                                            {"op": "done", "evidence": "x"}, {"op": "later"}, {"op": "title", "title": "New words"},
                                            {"op": "steps", "steps": ["a", "b"]}],
                             "next": [{"label": "Send it", "say": "send it"}]})
    out = act.write_ahead(KEY, "sig1")
    assert out is not None
    prompt = world["prompts"][0]
    assert "The user has not opened this card yet" in prompt and "What the user wants from this" in prompt
    assert world["events"] == []  # nothing goes on the user's calendar without the user's tap
    item = _item()
    assert item["draft"] == "Hey Maya, Thu 2pm?" and item["calendar"][0]["title"] == "Jam with Maya" and not item.get("not_now")
    with project_book.opened() as db:
        c = commitments_import.commitments.by_legacy_key(db, KEY)
        assert c and c["stage"] == "open" and c["title"] == "Talk with Maya about the year plan"
        assert Book(db).steps(c["id"]) == []
    assert out["turns"][0]["label"] == "Written ahead" and out["next"] == [{"label": "Send it", "say": "send it"}]
    # Once per conversation: the same signature asks nothing again.
    assert act.write_ahead(KEY, "sig1") is None and len(world["prompts"]) == 1
    # Other options takes it back like any answer.
    act.back(KEY)
    assert not _item().get("draft") and not _item().get("calendar")


def test_writing_ahead_with_nothing_to_write_gets_the_first_options_ready(world: dict[str, Any]) -> None:
    world["answers"].append({"did": "Maya needs to pick a day first.", "operations": [],
                             "next": [{"label": "Offer Thu or Fri", "say": "offer Maya Thursday or Friday"}]})
    out = act.write_ahead(KEY, "sig1")
    assert out == {"turns": [], "next": [{"label": "Offer Thu or Fri", "say": "offer Maya Thursday or Friday"}], "ready": True}
    assert act.opening(KEY)["next"][0]["label"] == "Offer Thu or Fri" and len(world["prompts"]) == 1
    # On a card the user already worked on, nothing written leaves it as the user left it.
    world["answers"].append({"did": "Done.", "operations": [], "next": []})
    _say(KEY, "keep it")
    world["answers"].append({"did": "Still fits, left it.", "operations": [], "next": [{"label": "X", "say": "x"}]})
    assert act.write_ahead(KEY, "sig2") is not None
    assert [t["said"] for t in _cards()["task"]["turns"]] == ["keep it"] and _cards()["task"]["ahead"] == "sig2"


def test_writing_ahead_files_into_a_project_only_when_in_none(world: dict[str, Any]) -> None:
    world["answers"].append({"did": "", "operations": [{"op": "project", "project": "onboarding"}], "next": []})
    act.write_ahead(KEY, "sig1")
    with project_book.opened() as db:
        c = commitments_import.commitments.by_legacy_key(db, KEY)
        p = Book(db).project_of(c) if c else None
        assert p and p["title"] == "Customer onboarding"
    world["answers"].append({"did": "", "operations": [{"op": "project", "project": "fund"}], "next": []})
    act.write_ahead(KEY, "sig2")
    with project_book.opened() as db:
        c = commitments_import.commitments.by_legacy_key(db, KEY)
        p = Book(db).project_of(c) if c else None
        assert p and p["title"] == "Customer onboarding"


def test_a_draft_on_a_task_user_waits_on_is_a_nudge_and_emails_get_addresses(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    store.mutate(lambda s: s["items"][KEY].update(state="waiting"))
    monkeypatch.setattr(connectors, "find_address", lambda name, accounts: "maya@x.org" if name == "Maya Lee" else "")
    world["answers"].append({"did": "", "operations": [
        {"op": "draft", "text": "Any news?"},
        {"op": "email", "to": ["Maya Lee", "Sam <sam@y.org>"], "subject": "Intro", "body": "Hi both"}], "next": []})
    act.write_ahead(KEY, "s")
    item = _item()
    assert item["nudge"] == "Any news?" and not item.get("draft")
    assert item["emails"][0]["to"] == [{"name": "Maya Lee", "email": "maya@x.org"}, {"name": "Sam", "email": "sam@y.org"}]


def test_writing_ahead_knows_where_it_can_reply(world: dict[str, Any]) -> None:
    """A task with no conversation of its own gets an email, never a draft."""
    store.mutate(lambda s: s["items"][KEY].update(ask_text="ravi@example.org", source="manual"))
    world["answers"].append({"did": "Wrote Ravi an email.", "operations": [], "next": []})
    act.write_ahead(KEY, "s")
    prompt = world["prompts"][0]
    assert '"no_conversation_to_reply_in": true' in prompt and "never a draft" in prompt


def _talk(key: str, chat: str, text: str) -> None:
    """A saved text chat behind the task."""
    conversations.save_record(store.DATA_DIR, f"text:{chat}:1", {
        "source": "text", "chat_id": chat, "channel_label": "Signal text", "messages": [
            {"id": "1", "ts": 1762300000.0, "name": "Maya", "text": text, "is_me": False}]})
    with conversations.opened(store.DATA_DIR) as db:
        c = commitments.by_legacy_key(db, key)
        assert c is not None
        commitments.link(db, c["id"], f"text:{chat}", "1", "origin")


def test_which_cards_are_due_for_writing_ahead(world: dict[str, Any]) -> None:
    assert act.ahead_due() == []  # no conversation saved for it yet
    _talk(KEY, "k", "When can we jam?")
    due = act.ahead_due()
    assert [k for k, _ in due] == [KEY]
    world["answers"].append({"did": "", "operations": [], "next": []})
    assert act.write_ahead_due() == {"due": 1, "written": 1, "errors": 0}
    assert act.ahead_due() == []
    # A card the old reply writer already wrote counts as written, for what its conversation said then.
    other = "slack:D2:2.0"
    store.mutate(lambda s: s["items"].update({other: dict(_item(), key=other, worked_at="2026-11-10T10:00:00")}))
    _talk(other, "j", "hi")
    assert act.ahead_due() == [] and task_context.card_records_now(other)["task"]["ahead"]


def test_the_first_options_are_worked_out_once_and_kept(world: dict[str, Any]) -> None:
    world["answers"].append({"did": "", "operations": [], "next": [{"label": "Ask Maya for 30 min", "say": "ask Maya for 30 min this week"}]})
    first = act.opening(KEY)
    again = act.opening(KEY)
    assert first == again == {"turns": [], "next": [{"label": "Ask Maya for 30 min", "say": "ask Maya for 30 min this week"}], "ready": True}
    assert len(world["prompts"]) == 1
    assert "The user just opened the card" in world["prompts"][0] and "Talk with Maya" in world["prompts"][0]


def test_a_tap_applies_its_operations_and_offers_what_comes_next_and_back_takes_it_back(world: dict[str, Any]) -> None:
    world["answers"].append({"did": "Put 30 min on your calendar Thu 2pm.",
                             "operations": [{"op": "calendar", "title": "Jam with Maya", "start": "2026-11-19T14:00", "end": "2026-11-19T14:30"}],
                             "next": [{"label": "Tell Maya Thu 2pm works", "say": "tell Maya Thursday 2pm works"}]})
    out = _say(KEY, "put 30 min on my calendar", label="Put 30 min on my calendar")
    assert world["events"][0]["start"] == "2026-11-19T14:00"
    assert out["next"] == [{"label": "Tell Maya Thu 2pm works", "say": "tell Maya Thursday 2pm works"}]
    assert out["turns"][0]["label"] == "Put 30 min on my calendar"

    world["answers"].append({"did": "Wrote it.", "operations": [{"op": "draft", "text": "Hey Maya! Thursday 2pm work?"}], "next": []})
    _say(KEY, "tell Maya Thursday 2pm works")
    assert _item()["draft"] == "Hey Maya! Thursday 2pm work?" and _item()["state"] == "ready"
    assert "the user: Put 30 min on my calendar | assistant: Put 30 min on your calendar Thu 2pm." in world["prompts"][-1]

    back = act.back(KEY)
    assert "draft" not in _item() and _item()["state"] == "mine"  # the draft is taken back
    assert back["next"] == [{"label": "Tell Maya Thu 2pm works", "say": "tell Maya Thursday 2pm works"}]
    act.back(KEY)
    assert world["deleted"] == ["ev1"]  # the calendar block is taken off too

    # Picking it again re-applies what was made; the model is not asked again.
    asked = len(world["prompts"])
    again = _say(KEY, "put 30 min on my calendar", label="Put 30 min on my calendar")
    assert len(world["prompts"]) == asked
    assert again["next"] == [{"label": "Tell Maya Thu 2pm works", "say": "tell Maya Thursday 2pm works"}]
    assert len(world["events"]) == 2


def test_steps_and_a_steps_own_options(world: dict[str, Any]) -> None:
    world["answers"].append({"did": "Three steps.", "operations": [{"op": "steps", "steps": ["Jot what I want", "Ask Maya for a time", "List facilitators"]}], "next": []})
    _say(KEY, "break it into steps")
    with project_book.opened() as db:
        c = commitments_import.commitments.by_legacy_key(db, KEY)
        assert c is not None
        steps = Book(db).steps(c["id"])
    assert [s["title"] for s in steps] == ["Jot what I want", "Ask Maya for a time", "List facilitators"]
    world["answers"].append({"did": "", "operations": [], "next": [{"label": "List who's come up", "say": "list who has come up before"}]})
    on_third = act.opening(KEY, steps[2]["id"])
    assert on_third["next"][0]["label"] == "List who's come up"
    assert '"user_is_on_step": "S3: List facilitators"' in world["prompts"][-1]
    assert act.view(_cards())["turns"][0]["said"] == "break it into steps"  # each focus keeps its own record


def test_done_ends_the_card_and_nothing_comes_next(world: dict[str, Any]) -> None:
    world["answers"].append({"did": "Marked done.", "operations": [{"op": "done", "evidence": "the user said so"}],
                             "next": [{"label": "Something else", "say": "x"}]})
    out = _say(KEY, "this is done")
    assert out["gone"] is True and out["next"] == []
    assert _item()["status"] == "done"


def test_the_endpoints(world: dict[str, Any]) -> None:
    client = runner.app.test_client()
    world["answers"].append({"did": "", "operations": [], "next": [{"label": "Ask Maya", "say": "ask Maya"}]})
    card = {"kind": "task", "key": KEY, "focus": ""}
    assert client.post("/api/assistant/open", json={"key": KEY}).get_json()["next"][0]["label"] == "Ask Maya"
    assert client.post("/api/assistant", json={"scope": card, "say": "  "}).status_code == 400
    world["answers"].append({"did": "Wrote it.", "operations": [{"op": "draft", "text": "Hi Maya"}], "next": []})
    res = client.post("/api/assistant", json={"scope": card, "say": "ask Maya", "label": "Ask Maya"}).get_json()
    assert res["turns"][0]["label"] == "Ask Maya" and _item()["draft"] == "Hi Maya"
    assert client.post("/api/assistant/back", json={"scope": card}).get_json()["next"][0]["label"] == "Ask Maya"
    # The three old assistants' endpoints are gone (the user, 2026-11-15: one assistant everywhere).
    for old in ("/api/act", "/api/act/open", "/api/act/back", "/api/feedback", "/api/projects/help"):
        assert client.post(old, json={}).status_code in (404, 405)
    # The card's history is in the database, not on the user's old item (the user, 2026-11-15).
    assert json.dumps(_cards()).count("Ask Maya") >= 1 and "act" not in _item()


def test_the_card_reads_everything_it_shows_in_one_go(world: dict[str, Any]) -> None:
    store.save_raw(KEY, {"source": "slack", "channel_label": "DM with Robin", "messages": [
        {"ts": "1762300000.0", "name": "Robin", "text": "Maya asked about a plan for the year"},
        {"ts": "1762300100.0", "name": "the user", "from_me": True, "text": "I want to jam with Maya on the year plan"},
    ]})
    store.mutate(lambda s: s["items"][KEY].update(url="https://slack/x", draft="Hey Maya!", state="ready"))
    client = runner.app.test_client()
    d = client.get("/api/card?key=" + KEY).get_json()
    assert [(m["from"], m["me"], m["text"]) for m in d["thread"]["messages"]] == [
        ("Robin", False, "Maya asked about a plan for the year"), ("You", True, "I want to jam with Maya on the year plan")]
    assert d["service"] == "Slack" and d["url"] == "https://slack/x" and d["draft"] == "Hey Maya!"
    assert d["steps"] == [] and d["act"] == {"turns": [], "next": None, "ready": False}
    assert client.get("/api/card?key=nope").status_code == 404
    # A first step on a task makes its checklist.
    assert client.post("/api/projects/step/add", json={"task": "", "key": KEY, "title": "Jot what I want"}).status_code == 200
    d = client.get("/api/card?key=" + KEY).get_json()
    assert [s["title"] for s in d["steps"]] == ["Jot what I want"] and d["task"]
    assert list(d["step_act"]) == [d["steps"][0]["cid"]]


def test_the_card_opens_on_the_message_the_task_came_from(world: dict[str, Any]) -> None:
    # The user, 2026-11-15: show the relevant part of the conversation, not just the newest.
    # The user's promise sits in another thread than the one sync marked: the card opens on the message
    # recorded as the task's origin; newer replies stay below it.
    store.save_raw(KEY, {"source": "slack", "messages": [
        {"ts": "1.0", "thread_ts": "1.0", "name": "Ben", "text": "why name the channel that?"},
        {"ts": "2.0", "thread_ts": "1.0", "name": "Ben", "text": "so weird"},
        {"ts": "3.0", "thread_ts": "3.0", "name": "Rosa", "text": "can we have chat in the app?"},
        {"ts": "4.0", "thread_ts": "3.0", "name": "the user", "from_me": True, "text": "Agreed. I'll work with Iris next week to get it to a better design"},
        {"ts": "5.0", "thread_ts": "3.0", "name": "Ben", "text": "my main question is a design one"},
    ]})
    with project_book.opened() as db:
        c = commitments.by_legacy_key(db, KEY)
        assert c is not None
        cid = c["id"]
        db.execute("DELETE FROM commitment_links WHERE commitment_id = ? AND role = 'origin'", (cid,))
        commitments.link(db, cid, "slack:C1", "4.0", "origin")
        db.commit()
    t = runner.app.test_client().get("/api/card?key=" + KEY).get_json()["thread"]
    assert [m["text"] for m in t["messages"]][t["anchor"]].startswith("Agreed.")
    assert [m["from"] for m in t["messages"]] == ["Rosa", "You", "Ben"] and t["earlier"] == 0
    # With no origin recorded, the newest message (Today no longer guesses from a mark on the conversation).
    with project_book.opened() as db:
        db.execute("DELETE FROM commitment_links WHERE commitment_id = ? AND role = 'origin'", (cid,))
        db.commit()
    t = runner.app.test_client().get("/api/card?key=" + KEY).get_json()["thread"]
    assert [m["from"] for m in t["messages"]] == ["Ben", "Ben"] and t["anchor"] == 1
    # An ask that starts a thread: the channel's lead-up before it, its replies after, marked as replies.
    store.save_raw(KEY, {"source": "slack", "messages": [
        {"ts": "1.0", "thread_ts": "", "name": "Rosa", "text": "morning all"},
        {"ts": "2.0", "thread_ts": "2.0", "name": "Robin", "text": "Can you send Maya the plan?"},
        {"ts": "3.0", "thread_ts": "2.0", "name": "the user", "from_me": True, "text": "on it"},
        {"ts": "4.0", "thread_ts": "", "name": "Ben", "text": "lunch?"},
    ]})
    with project_book.opened() as db:
        commitments.link(db, cid, "slack:C1", "2.0", "origin")
        db.commit()
    t = runner.app.test_client().get("/api/card?key=" + KEY).get_json()["thread"]
    assert [(m["from"], bool(m.get("reply"))) for m in t["messages"]] == [("Rosa", False), ("Robin", False), ("You", True)]
    assert t["anchor"] == 1
    # A saved copy taken for another ask in the conversation, without the task's message: the
    # card reads the conversation's own messages instead (the user, 2026-11-17: the 409a card
    # showed another thread of the same DM).
    store.save_raw(KEY, {"source": "slack", "messages": [
        {"ts": "9.0", "thread_ts": "9.0", "name": "Robin", "text": "Arc is sunsetting"},
    ]})
    with project_book.opened() as db:
        db.execute("INSERT OR IGNORE INTO conversations (id, source) VALUES ('slack:C1', 'slack')")
        for ts, thread, who, text in ((1.0, "", "Rosa", "morning all"), (2.0, "2.0", "Robin", "Can you send Maya the plan?"),
                                      (3.0, "2.0", "the user", "on it"), (9.0, "9.0", "Robin", "Arc is sunsetting")):
            raw = {"ts": ts, "id": str(ts), "name": who, "text": text, "is_me": who == "the user"} | ({"thread_ts": thread} if thread else {})
            db.execute("INSERT OR REPLACE INTO messages (conversation_id, source_id, at, sender, is_user, text, thread, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       ("slack:C1", str(ts), ts, who, who == "the user", text, thread, json.dumps(raw)))
        db.commit()
    t = runner.app.test_client().get("/api/card?key=" + KEY).get_json()["thread"]
    assert [(m["from"], bool(m.get("reply"))) for m in t["messages"]] == [("Rosa", False), ("Robin", False), ("You", True)]
    assert t["anchor"] == 1
    # A mail server's delivery report never shows in the conversation.
    store.save_raw(KEY, {"source": "email", "messages": [
        {"id": "e1", "from": "Tess <k@x.com>", "text": "Can Oscar pay over time?"},
        {"id": "e2", "from": "Mail Delivery Subsystem <MAILER-DAEMON@mx.example.com>", "text": "** THIS IS A WARNING MESSAGE ONLY **"},
        {"id": "e3", "from": "<postmaster@x.onmicrosoft.com>", "text": "Delivery has failed"},
    ]})
    t = runner.app.test_client().get("/api/card?key=" + KEY).get_json()["thread"]
    assert [m["text"] for m in t["messages"]] == ["Can Oscar pay over time?"]
    # An email task's source is the email the digest flagged, saved when the task is made.
    assert commitments_import.origin_message({"source": "email", "ref": {"thread_id": "t1", "message_id": "m7"}}) == "m7"
    card_js = runner.app.test_client().get("/assets/card.js").get_data(as_text=True)
    assert "data-c5newer" in card_js and "CARD.threadDown[key] = true" in card_js


def test_every_list_and_project_page_opens_the_same_card() -> None:
    page = runner.app.test_client().get("/").get_data(as_text=True)
    card_js = runner.app.test_client().get("/assets/card.js").get_data(as_text=True)
    projects_js = runner.app.test_client().get("/assets/projects.js").get_data(as_text=True)
    assert '<script src="/assets/card.js"></script>' in page
    assert "if(usesCard(r, viewName)) return cardShell(r, viewName);" in page  # one body for every view
    assert "/api/card?key=" in card_js and "/api/assistant/open" in card_js and "/api/assistant/back" in card_js
    # One assistant everywhere (the user, 2026-11-15): the card, the project box and the bottom bar
    # all ask /api/assistant, and show its answer with the same component.
    assert 'scope: {kind: "task", key, focus}' in card_js and "function assistHtml(" in card_js
    assert 'scope: {kind: "project", pid: d.id' in projects_js and 'scope: {kind: "tasks", keys: DOCK.keys}' in projects_js
    assert projects_js.count("assistHtml(") == 2 and "/api/feedback" not in projects_js and "/api/projects/help" not in projects_js
    assert "sendSoon(" in card_js  # Send always has the undo
    assert 'dockRef(key, e.shiftKey);  // the bar at the bottom, on every page' in projects_js
    assert 'view.page === "proj"' in projects_js and "h += dockHtml();" in projects_js


def test_the_card_stays_quick_and_any_step_can_get_options() -> None:
    card_js = runner.app.test_client().get("/assets/card.js").get_data(as_text=True)
    assert "OPTQ.running < 1" in card_js  # options are worked out one at a time, never holding up a card
    assert 'document.addEventListener("mouseover"' in card_js  # a card's data is read on hover
    assert "if(!now && (CARD.down || typingIn(el))){ CARD.later.add(" in card_js  # no redraw under a press or while typing
    assert "st.done = done; openedSteps(key).delete(st.cid); drawCard(el, reload);" in card_js  # a tick shows at once
    assert 'data-c5gen="' in card_js and "Options for this step" in card_js


@pytest.mark.frontend
def test_emails_written_for_a_task_show_while_a_step_is_open() -> None:
    """A step's option can write an email; it shows on the card (with Send) whichever step is open."""
    card_js = runner.app.test_client().get("/assets/card.js").get_data(as_text=True)
    made = card_js[card_js.index("function c5made(d){"):card_js.index("function c5draft(d){")]
    assert "data-c5email=" in made and "data-c5sendemail=" in made and "data-c5cal=" in made
    assert "if(!focus) h += c5made(d);" in card_js  # on the task's own block when no step is open
    focused = card_js[card_js.index("if(!foc) h += c5act(r, d, \"\");"):card_js.index("h += c5notes(d);")]
    assert "h += c5made(d);" in focused  # and under the steps when one is


def test_new_steps_made_from_a_step_are_kept_with_the_task(world: dict[str, Any]) -> None:
    """A step's option that remakes the steps removes that step, so what was done shows on the task."""
    world["answers"].append({"did": "Two steps.", "operations": [{"op": "steps", "steps": ["First", "Second"]}], "next": []})
    _say(KEY, "break it into steps")
    with project_book.opened() as db:
        c = commitments_import.commitments.by_legacy_key(db, KEY)
        assert c is not None
        first = Book(db).steps(c["id"])[0]["id"]
    world["answers"].append({"did": "Better steps.", "operations": [{"op": "steps", "steps": ["Alpha", "Beta"]}], "next": []})
    _say(KEY, "redo the steps", focus=first)
    assert [t["said"] for t in act.view(_cards())["turns"]] == ["break it into steps", "redo the steps"]
    assert act.view(_cards(), first)["turns"] == []


def test_user_answers_reach_the_card_and_slack_emoji_codes_stay_in_slack(world: dict[str, Any]) -> None:
    """What the user already answered on a task is the user's to decide, never asked again; a text or an
    email gets a real emoji, never a Slack code (the user, 2026-11-14)."""
    key = next(iter(store.load()["items"]))
    item = dict(store.load()["items"][key], answers=["No, can't make it"])
    assert act._state(key, item, "")["user_answers"] == ["No, can't make it"]
    assert "Never guess a decision only the user can make" in act.SYSTEM
    # Already done but the user asked for something else: offered as a button, never done for the user.
    assert '"label": "Mark it done"' in act.system()
    assert "only work in Slack" in work.voice({"source": "text"})
    assert "only work in Slack" not in work.voice({"source": "slack"})


def test_search_covers_saved_chats_and_opens_notion_through_user_account(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The user, 2026-11-15: Nina's Notion doc was in Slack and the card only searched email.
    Slack has the user as "Nina", the card asks for "Nina Park": most of the words is enough.
    A search covers every conversation Today has saved too, and a Notion link it finds opens
    through the user's Notion account."""
    conversations.save_record(store.DATA_DIR, "text:c9:1", {
        "source": "text", "chat_id": "c9", "channel_label": "Slack with Nina",
        "messages": [{"id": "1", "ts": 1000.0, "name": "Nina", "is_me": False,
                      "text": "Trip questions: <https://app.notion.com/p/acme/NZ-trip-123|Here>"}],
    })
    monkeypatch.setattr(work, "_read_mail", lambda q: [])
    opened: list[str] = []
    monkeypatch.setattr(connectors, "notion_fetch", lambda url: opened.append(url) or "Q1: which dates do you fly?")
    world["answers"] += [
        {"did": "", "operations": [{"op": "search", "queries": ["Nina Park notion"]}], "next": []},
        {"did": "", "operations": [{"op": "open", "urls": ["https://app.notion.com/p/acme/NZ-trip-123|Here"]}], "next": []},
        {"did": "It asks which dates you fly.", "operations": [], "next": []},
    ]
    out = _say(KEY, "the doc is in Slack, find it")
    assert "NZ-trip-123" in world["prompts"][1] and "Slack with Nina" in world["prompts"][1]
    assert '"links": ["https://app.notion.com/p/acme/NZ-trip-123"]' in world["prompts"][1]
    assert "Here (https://app.notion.com" in world["prompts"][1]  # a Slack link reads as its word and address
    assert opened == ["https://app.notion.com/p/acme/NZ-trip-123"]
    assert "Q1: which dates do you fly?" in world["prompts"][2] and "without searching" in world["prompts"][2]
    assert out["turns"][-1]["did"] == "It asks which dates you fly."


def test_what_the_card_cant_do_is_said_plainly_and_kept(world: dict[str, Any]) -> None:
    """A button may offer what Today can't do yet; tapping it says so plainly, and the gap is
    kept so it can be built (the user, 2026-11-15)."""
    world["answers"].append({"did": "I can't do that yet: Today can't book flights.", "operations": [], "next": [],
                             "missing": "Book flights"})
    _say(KEY, "Book my flights")
    gap = act.missing_list()[0]
    assert gap["missing"] == "Book flights" and gap["said"] == "Book my flights" and gap["place"].startswith("task:")
    assert "I can't do that yet:" in act.SYSTEM


def test_a_link_the_assistant_finds_always_reaches_its_step() -> None:
    """The user, 2026-11-15: links must come through. However the assistant hands a step its
    link (on the step, in a separate list by number, in the words, as a Slack link, or loose),
    the step keeps it as a link."""
    doc = "https://app.notion.com/p/acme/Wedding-417a?source=copy_link"
    by_number = task_steps.steps_from_op({"steps": ["Open Nina's doc", "Pick dates"],
                                          "details": [{"step": 1, "kind": "link", "url": doc, "note": "3/7 to 3/9"}]}, "t")
    assert by_number[0]["url"] == doc and by_number[0]["note"] == "3/7 to 3/9" and "url" not in by_number[1]
    slack = task_steps.steps_from_op({"steps": [{"text": "Read <https://app.notion.com/p/x?a=1&amp;b=2|Nina's doc>"}]}, "t")
    assert slack[0] == {"text": "Read Nina's doc", "kind": "link", "note": "", "url": "https://app.notion.com/p/x?a=1&b=2"}
    loose = task_steps.steps_from_op({"steps": ["Open the doc", "Pick dates"], "url": doc}, "t")
    assert loose[0]["kind"] == "link" and loose[0]["url"] == doc
    plain_kind = task_steps.steps_from_op({"steps": [{"text": "Open the doc", "kind": "plain", "url": doc}]}, "t")
    assert plain_kind[0]["kind"] == "link"


def test_a_task_the_judge_found_opens_its_card() -> None:
    """Found 2026-11-15: a card for a commitment the judge found (its key is the commitment id,
    with no ":") failed to open."""
    with conversations.opened(store.DATA_DIR) as db:
        commitments_import.run(db, store.load(), {}, None, {})  # Today has switched to commitments
        cid = commitments.create(db, "judge", {"title": "Sign the side letter for Hugo", "direction": "mine",
                                               "origin": "they_asked", "started_by": "them", "counterparty": "Hugo"})
        step = commitments.create(db, "user", {"title": "Open the DocuSign", "direction": "self", "origin": "user_chose",
                                                 "started_by": "user", "parent_id": cid, "labels": ["step"]})
    out = card.steps_of(cid)
    assert out["task"] == cid and [s["cid"] for s in out["steps"]] == [step]
