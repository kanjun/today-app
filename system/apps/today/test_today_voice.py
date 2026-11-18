"""Drafts are written against the user's own messages: one voice file (the voice_file setting),
with email items pointed at its email part and chat items at its Slack part; without it, the
older per-channel files; a missing file just leaves the section out."""

from pathlib import Path

import pytest
from today import work


def test_no_voice_is_set_by_default() -> None:
    assert work.VOICE is None and work.EMAIL_VOICE is None and work.CHAT_VOICE is None


def test_email_and_chat_items_are_pointed_at_their_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = tmp_path / "voice.txt"
    voice.write_text("EMAIL SAMPLE: Hey Dean!\nCHAT SAMPLE: ty!!")
    monkeypatch.setattr(work, "VOICE", voice)
    email = work.voice({"source": "email"})
    assert "CHAT SAMPLE" in email and "follow Part 1" in email
    for source in ("slack", "text"):
        chat = work.voice({"source": source})
        assert "EMAIL SAMPLE" in chat and "follow Part 2" in chat


def test_missing_voice_file_adds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(work, "VOICE", tmp_path / "absent.md")
    assert work.voice({"source": "slack"}) == ""
    monkeypatch.setattr(work, "VOICE", None)
    assert work.voice({"source": "slack"}) == ""
    # Outside Slack only the note about emoji codes is left.
    assert "<user_voice>" not in work.voice({"source": "email"})


def test_without_the_one_file_each_channel_uses_its_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "email.md").write_text("EMAIL ONLY")
    (tmp_path / "chat.md").write_text("CHAT ONLY")
    monkeypatch.setattr(work, "VOICE", None)
    monkeypatch.setattr(work, "EMAIL_VOICE", tmp_path / "email.md")
    monkeypatch.setattr(work, "CHAT_VOICE", tmp_path / "chat.md")
    email, chat = work.voice({"source": "email"}), work.voice({"source": "slack"})
    assert "EMAIL ONLY" in email and "CHAT ONLY" not in email and "Part 1" not in email
    assert "CHAT ONLY" in chat and "EMAIL ONLY" not in chat
