from pathlib import Path
from typing import Any

import pytest
from today import (
    commitments,
    commitments_import,
    conversation_judge,
    conversations,
    store,
)


@pytest.fixture(autouse=True)
def _no_live_mail_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Today reads the synced mail store when it is current. A test must never read the
    workspace's real one, or run the Gmail sync: point them at an empty spot instead."""
    monkeypatch.setenv(
        "GMAIL_STORE_PATH", str(tmp_path / "no-mail-store" / "archive.sqlite")
    )
    monkeypatch.setenv("GMAIL_STORE_CATCH_UP", "0")


@pytest.fixture(autouse=True)
def _no_live_today_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every write (state, raw records, the commitments database a click is mirrored into)
    goes to a scratch folder unless a test points it somewhere itself: a test must never
    write into the user's real Today data."""
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "today-data")


@pytest.fixture(autouse=True)
def _no_live_sent_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """The judge looks at everything the user sent (the user's mail and the user's Slack); a test never reads
    the real ones, unless it hands the judge its own."""
    monkeypatch.setattr(conversation_judge, "_sent", lambda since: [])


@pytest.fixture(autouse=True)
def _no_live_signing_notices(monkeypatch: pytest.MonkeyPatch) -> None:
    """The judge searches the user's inboxes for signing notices; a test never reads the real ones."""
    monkeypatch.setattr(conversation_judge, "_notices", lambda since: [])


class Tasks:
    """Tasks for a test, in the scratch database: ``seed`` brings a state's items in as tasks
    (the import the user's data went through), ``set`` changes one as the user's click would."""

    def seed(self, state: dict[str, Any]) -> None:
        with conversations.opened(store.DATA_DIR) as db:
            commitments_import.run(db, state, {}, None, {})

    def set(self, key: str, **fields: Any) -> None:
        with conversations.opened(store.DATA_DIR) as db:
            c = commitments.by_legacy_key(db, key)
            assert c is not None, key
            commitments.change(db, c["id"], "user", fields, reason="test")

    def get(self, key: str) -> dict[str, Any]:
        with conversations.opened(store.DATA_DIR) as db:
            c = commitments.by_legacy_key(db, key)
            assert c is not None, key
            return c


@pytest.fixture
def tasks() -> Tasks:
    return Tasks()
