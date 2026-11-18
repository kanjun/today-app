"""Save as Gmail draft puts the reply in the user's drafts, in its thread, and never sends."""

from email import message_from_bytes
from email.message import EmailMessage

import pytest
from today import actions, connectors

KEY = "email:alex@acme.example:t1"
ITEM = {
    "key": KEY,
    "source": "email",
    "account": "alex@acme.example",
    "ref": {"thread_id": "t1"},
    "who": "Jordan",
    "status": "open",
}
RAW = {
    "source": "email",
    "messages": [
        {
            "from": "Jordan <jordan@example.com>",
            "from_me": False,
            "to": "alex@acme.example",
            "cc": "",
            "subject": "Pilot",
            "message_id": "<m1@example.com>",
            "references": "",
        }
    ],
}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    state = {"items": {KEY: dict(ITEM)}}
    seen: list[list[str]] = []

    def fake_curl(args: list[str], account: str = "") -> dict:
        seen.append(args)
        return {"id": "d1", "message": {"id": "m9", "threadId": "t1"}}

    def mutate(fn):  # noqa: ANN001, ANN202
        return fn(state)

    monkeypatch.setattr(connectors, "_curl", fake_curl)
    monkeypatch.setattr(actions.store, "load", lambda: state)
    monkeypatch.setattr(actions.store, "load_raw", lambda key: RAW)
    monkeypatch.setattr(actions.store, "mutate", mutate)
    return seen


def test_reply_goes_to_drafts_in_its_thread(calls: list[list[str]]) -> None:
    result = actions.save_gmail_draft(KEY, None, "", "", "", "Sounds good")
    assert result["message"] == "Saved to your Gmail drafts"
    url = calls[0][-1]
    assert url.endswith("/drafts") and "send" not in url
    assert '"threadId": "t1"' in calls[0][calls[0].index("-d") + 1]


def test_second_press_updates_the_same_draft(calls: list[list[str]]) -> None:
    actions.save_gmail_draft(KEY, None, "", "", "", "v1")
    result = actions.save_gmail_draft(KEY, None, "", "", "", "v2")
    assert result["message"] == "Updated in your Gmail drafts"
    assert calls[1][1] == "PUT" and calls[1][-1].endswith("/drafts/d1")
    assert all("send" not in c[-1] for c in calls)


def test_sent_mail_keeps_every_line_break() -> None:
    """The user, 2026-11-13: the user's sign-off ("questions,\\nAlex") ran onto one line in the user's mail
    app, which joins single line breaks in plain text. Mail now carries an HTML copy too."""
    text = "Hi Gus, sorry for the slow follow-up.\n\nThanks much, and let me know if you have any questions,\nAlex"
    assert connectors.html_body(text) == (
        "<div>Hi Gus, sorry for the slow follow-up.</div><div><br></div>"
        "<div>Thanks much, and let me know if you have any questions,<br>Alex</div>"
    )
    assert connectors.html_body("a < b & c") == "<div>a &lt; b &amp; c</div>"
    msg = EmailMessage()
    connectors.set_body(msg, text)
    parsed = message_from_bytes(msg.as_bytes())
    parts = {p.get_content_type(): bytes(p.get_payload(decode=True)).decode() for p in parsed.walk() if not p.is_multipart()}
    assert parts["text/plain"].strip() == text
    assert "questions,<br>Alex" in parts["text/html"]
