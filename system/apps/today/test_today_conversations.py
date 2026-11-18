"""The conversations database (conversations.py): every saved Slack, text and email record lands
message by message, a message seen again only gains what was missing, drafts stay out, and an
email thread is filed under its real Gmail thread id whatever the record's key."""

import json
import sqlite3
from pathlib import Path

import pytest
from today import conversations, store


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "today")
    return tmp_path / "today"


def text_record(note: str = "", text: str = "[voice message]") -> dict:
    return {
        "source": "text",
        "channel_label": "WhatsApp text with Theo",
        "chat_id": "!abc:chat.local",
        "messages": [
            {"id": "1", "ts": 100.0, "name": "Theo", "text": "Esalen in March?", "is_me": False},
            {"id": "2", "ts": 200.0, "name": "the user", "text": text, "note": note, "type": "VOICE", "is_me": True},
        ],
    }


def test_save_raw_writes_the_conversation(data_dir: Path) -> None:
    store.save_raw("text:!abc:chat.local:1", text_record())
    msgs = conversations.messages(data_dir, "text:!abc:chat.local")
    assert [(m["source_id"], m["is_user"], m["type"], m["text"]) for m in msgs] == [
        ("1", 0, "message", "Esalen in March?"),
        ("2", 1, "voice", "[voice message]"),
    ]
    db = sqlite3.connect(conversations.db_path(data_dir))
    assert db.execute("SELECT title, last_at, last_user_at FROM conversations").fetchone() == (
        "WhatsApp text with Theo",
        200.0,
        200.0,
    )


def test_a_message_seen_again_gains_and_never_loses(data_dir: Path) -> None:
    conversations.save_record(data_dir, "k", text_record())
    conversations.save_record(data_dir, "k", text_record(note="got a reaction ❤️"))
    conversations.save_record(data_dir, "k", text_record(text=""))
    msgs = conversations.messages(data_dir, "text:!abc:chat.local")
    assert len(msgs) == 2
    assert msgs[1]["text"] == "[voice message]"
    assert msgs[1]["note"] == "got a reaction ❤️"


def test_email_thread_id_and_drafts(data_dir: Path) -> None:
    record = {
        "source": "email",
        "account": "alex@acme.example",
        "subject": "hi from eliza",
        "url": "https://mail.google.com/mail/u/alex@acme.example/#all/19f151065ace03b8",
        "messages": [
            {"id": "19f151065ace03b8", "from": "Ada <e@x.edu>", "from_me": False, "internal_ms": 1000, "text": "hi"},
            {"id": "19f1510aaaaaaaaa", "from": "the user", "from_me": True, "draft": True, "internal_ms": 2000, "text": "draft"},
        ],
    }
    # A record keyed by something other than the thread (a person's address) still files under it.
    assert conversations.save_record(data_dir, "person:e@x.edu", record) == "email:alex@acme.example:19f151065ace03b8"
    msgs = conversations.messages(data_dir, "email:alex@acme.example:19f151065ace03b8")
    assert [m["text"] for m in msgs] == ["hi"]


def test_slack_mine_and_thread(data_dir: Path) -> None:
    record = {
        "source": "slack",
        "channel": "D078RR0330A",
        "channel_label": "DM with Robin",
        "messages": [
            {"ts": "1.5", "user": "U1", "name": "Robin", "text": "yes to hosting?"},
            {"ts": "2.5", "user": conversations.SLACK_ME, "name": "the user", "text": "yes", "thread_ts": "1.5"},
        ],
    }
    conversations.save_record(data_dir, "slack:D078RR0330A:1.5", record)
    msgs = conversations.messages(data_dir, "slack:D078RR0330A")
    assert [(m["is_user"], m["thread"]) for m in msgs] == [(0, ""), (1, "1.5")]


def test_raw_records_live_in_the_database() -> None:
    """The user, 2026-11-15: conversations in the database, nothing in files. A record is saved
    to today.db (its messages too), and an old raw/ file is never read."""
    store.save_raw("text:c:1", text_record())
    assert not (store.DATA_DIR / "raw").exists()  # nothing written to files
    assert store.load_raw("text:c:1") == text_record()
    with conversations.opened(store.DATA_DIR) as db:
        assert db.execute("SELECT count(*) FROM messages WHERE conversation_id = 'text:!abc:chat.local'").fetchone()[0] == 2
    (store.DATA_DIR / "raw").mkdir()
    (store.DATA_DIR / "raw" / "calendar.json").write_text(json.dumps({"events": [1]}))
    assert store.load_raw("calendar") is None
    store.save_raw("calendar", {"events": [2]})
    assert store.load_raw("calendar") == {"events": [2]}
