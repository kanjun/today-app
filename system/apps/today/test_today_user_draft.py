"""The user's own unsent Gmail draft leads the item; Send sends that draft, edits go into it."""

import pytest
from today import actions, connectors

KEY = "email:alex@acme.example:t1"
ITEM = {
    "key": KEY,
    "source": "email",
    "kind": "asked",
    "account": "alex@acme.example",
    "ref": {"thread_id": "t1"},
    "who": "Sam",
    "status": "open",
}


def _raw() -> dict:
    return {
        "source": "email",
        "messages": [
            {"id": "a", "from_me": False, "draft": False, "text": "Can we talk?"},
            {
                "id": "m1",
                "from_me": True,
                "draft": True,
                "to": "Sam <sam@example.com>",
                "text": "Hi Sam,&nbsp;\n\n\n\nSure, 3:30?",
            },
        ],
    }


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls: list[tuple[str, str]] = []
    w = {
        "state": {"items": {KEY: dict(ITEM)}, "today": {"order": []}},
        "raw": _raw(),
        "calls": calls,
    }
    monkeypatch.setattr(actions.store, "load", lambda: w["state"])
    monkeypatch.setattr(actions.store, "load_raw", lambda key: w["raw"])
    monkeypatch.setattr(actions.store, "save_raw", lambda key, raw: None)
    monkeypatch.setattr(actions.store, "mutate", lambda fn: fn(w["state"]))
    monkeypatch.setattr(actions, "_advance_soon", lambda gid: None)
    monkeypatch.setattr(connectors, "gmail_find_draft", lambda a, m: "d1")

    def rewrite(account: str, draft_id: str, text: str) -> dict:
        calls.append(("rewrite", text))
        return {"id": "d1", "message": {"id": "m2"}}

    def send(account: str, draft_id: str) -> dict:
        calls.append(("send", draft_id))
        return {}

    monkeypatch.setattr(connectors, "gmail_rewrite_draft", rewrite)
    monkeypatch.setattr(connectors, "gmail_send_draft", send)
    return w


def test_the_newest_draft_is_shown_tidied(world: dict) -> None:
    found = actions.user_draft(ITEM)
    assert found is not None
    assert found["message_id"] == "m1"
    assert found["text"] == "Hi Sam,\n\nSure, 3:30?"


def test_sending_unedited_sends_the_draft_as_is(world: dict) -> None:
    actions.send_user_draft(KEY, "m1", "Hi Sam,\n\nSure, 3:30?")
    assert world["calls"] == [("send", "d1")]
    assert world["state"]["items"][KEY]["status"] == "sent"


def test_edits_go_into_the_draft_and_nothing_is_sent(world: dict) -> None:
    actions.save_user_draft(KEY, "m1", "Hi Sam, how about Tue 2pm?")
    assert world["calls"] == [("rewrite", "Hi Sam, how about Tue 2pm?")]
    assert world["state"]["items"][KEY]["status"] == "open"


def test_no_draft_lead_when_someone_wrote_after_it(world: dict) -> None:
    world["raw"]["messages"].append({"id": "b", "from_me": False, "draft": False})
    assert actions.user_draft(ITEM) is None
