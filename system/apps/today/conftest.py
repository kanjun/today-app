from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_live_mail_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Today reads the synced mail store when it is current. A test must never read the
    workspace's real one, or run the Gmail sync: point them at an empty spot instead."""
    monkeypatch.setenv(
        "GMAIL_STORE_PATH", str(tmp_path / "no-mail-store" / "archive.sqlite")
    )
    monkeypatch.setenv("GMAIL_STORE_CATCH_UP", "0")
