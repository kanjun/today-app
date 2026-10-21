"""An unsent Gmail draft in a thread is shown as a draft and never counts as a reply."""

import pytest
from today import connectors, sync, work


def _msg(mid: str, sender: str, labels: list[str], text: str) -> dict:
    return {
        "id": mid,
        "labelIds": labels,
        "internalDate": "1760000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": "x@example.com"},
                {"name": "Subject", "value": "Hello"},
                {"name": "Date", "value": "Thu, 8 Oct 2026 10:00:00 -0700"},
            ],
            "body": {"data": ""},
        },
        "snippet": text,
    }


THREAD = {
    "messages": [
        _msg("a", "Sam <sam@example.com>", ["INBOX"], "When can we talk?"),
        _msg("b", "Alex <alex@acme.example>", ["DRAFT"], "How about [time]?"),
    ]
}


def test_draft_is_flagged_and_sent_mail_is_not() -> None:
    msgs = connectors.thread_messages(THREAD)
    assert [m["draft"] for m in msgs] == [False, True]


def test_signature_changes_when_a_draft_is_sent() -> None:
    msgs = connectors.thread_messages(THREAD)
    with_draft = sync.signature({"messages": msgs})
    sent = sync.signature({"messages": [msgs[0], dict(msgs[1], draft=False)]})
    assert with_draft != sent
    assert sent == "2:b"


@pytest.fixture(autouse=True)
def _user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TODAY_USER_NAME", "Alex Rivera")
    monkeypatch.setenv("TODAY_EMAIL_ACCOUNTS", "alex@acme.example")


def test_the_users_own_mail_is_marked_as_theirs() -> None:
    msgs = connectors.thread_messages(THREAD)
    assert [m["from_me"] for m in msgs] == [False, True]


def test_assistant_is_told_the_draft_was_never_sent() -> None:
    raw = {
        "source": "email",
        "account": "alex@acme.example",
        "subject": "Hello",
        "messages": connectors.thread_messages(THREAD),
    }
    text = work.transcript(raw)
    assert "Alex, UNSENT DRAFT" in text
    assert text.count("UNSENT DRAFT") == 1
