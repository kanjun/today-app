"""The reader's questions for the user (the user, 2026-11-14) reach the one card through its assistant,
which offers their answers as the card's options; tapping one writes the reply."""

from typing import Any

import pytest
from today import act, ai, calendar_events, store

KEY = "email:a@x.com:t1"


def test_the_readers_questions_become_the_cards_first_options(monkeypatch: pytest.MonkeyPatch) -> None:
    store.mutate(lambda s: s["items"].update({KEY: {
        "key": KEY, "status": "open", "source": "email", "state": "you", "who": "Ravi", "title": "Send Ravi his invite",
        "questions": [{"q": "There's no invite link on file for them. How should they get in?", "kind": "choice",
                       "options": ["I'll get an invite link from the team", "Point them to the waitlist sign-up"]}]}}))
    monkeypatch.setattr(calendar_events, "brief", lambda **_: "")
    prompts: list[str] = []

    def complete_json(prompt: str, **_k: Any) -> dict[str, Any]:
        prompts.append(prompt)
        return {"did": "", "operations": [], "next": [{"label": "Get a link from the team", "say": "I'll get an invite link from the team"}]}

    monkeypatch.setattr(ai, "complete_json", complete_json)
    out = act.opening(KEY)
    assert "How should they get in?" in prompts[0] and "Point them to the waitlist sign-up" in prompts[0]
    assert out["next"][0]["label"] == "Get a link from the team"
