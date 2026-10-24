"""Drafts are written against the user's own messages: email items use the email voice
file, Slack items the chat voice file (both settings), and with neither set, or a missing
file, the section is left out."""

from pathlib import Path

import pytest
from today import config, work


def _voices(
    monkeypatch: pytest.MonkeyPatch, email: Path | None, chat: Path | None
) -> None:
    settings = config.load()._replace(email_voice=email, chat_voice=chat)
    monkeypatch.setattr(config, "load", lambda: settings)


def test_email_and_chat_items_get_their_own_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    email, chat = tmp_path / "email.md", tmp_path / "chat.md"
    email.write_text("EMAIL SAMPLE: Hey Jordan! Thank you for this")
    chat.write_text("CHAT SAMPLE: ty!!")
    _voices(monkeypatch, email, chat)
    assert "EMAIL SAMPLE" in work.voice({"source": "email"})
    assert "CHAT SAMPLE" in work.voice({"source": "slack"})
    assert "EMAIL SAMPLE" not in work.voice({"source": "slack"})


def test_no_voice_set_adds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _voices(monkeypatch, None, None)
    assert work.voice({"source": "email"}) == ""
    assert work.voice({"source": "slack"}) == ""


def test_voice_files_come_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.store, "DATA_DIR", tmp_path)
    monkeypatch.delenv("TODAY_EMAIL_VOICE", raising=False)
    monkeypatch.setenv("TODAY_CHAT_VOICE", "chat.md")
    (tmp_path / "settings.json").write_text('{"email_voice": "mail.md"}')
    s = config.load()
    assert (s.email_voice, s.chat_voice) == (Path("mail.md"), Path("chat.md"))


def test_missing_voice_file_adds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _voices(monkeypatch, tmp_path / "absent.md", None)
    assert work.voice({"source": "email"}) == ""
