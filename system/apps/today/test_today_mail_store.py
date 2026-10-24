"""Today reads mail from a synced local mail store when one is installed and current, and
asks Gmail otherwise. The store here is a small in-memory stand-in for the optional
``people_archive.gmail_store`` library, which is not part of this app."""

from datetime import datetime
from typing import Any

import pytest
from today import mail_store

WORK = "sam@acme.example"


class FakeStore:
    def __init__(self, reason: str | None, threads: dict[str, dict[str, Any]]) -> None:
        self.reason = reason
        self.threads = threads
        self.changes = [
            {"threadId": "t1", "kind": "added", "seq": 4},
            {"threadId": "t2", "kind": "labels", "seq": 5},
            {"threadId": "t3", "kind": "added", "seq": 6},
        ]
        self.closed = 0

    def unusable_reason(self, account: str, needs_labels: bool) -> str | None:
        return self.reason

    def close(self) -> None:
        self.closed += 1

    def thread(self, account: str, thread_id: str) -> dict[str, Any] | None:
        return self.threads.get(thread_id)

    def sent_since(self, account: str, since: datetime, limit: int) -> list[Any]:
        return [{"id": "m1"}][:limit]

    def latest_change(self, account: str) -> int:
        return 6

    def changes_since(self, account: str, cursor: int) -> list[dict[str, Any]] | None:
        return [c for c in self.changes if int(c["seq"]) > cursor] if cursor >= 3 else None


class FakeLibrary:
    def __init__(self, store: FakeStore | None) -> None:
        self.store = store
        self.fallbacks: list[tuple[str, str, str]] = []
        self.caught_up: list[str] = []

    def open_store(self) -> FakeStore | None:
        return self.store

    def log_fallback(self, who: str, account: str, reason: str) -> None:
        self.fallbacks.append((who, account, reason))

    def catch_up(self, account: str) -> bool:
        self.caught_up.append(account)
        return True


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def install(store: FakeStore | None) -> FakeLibrary:
        lib = FakeLibrary(store)
        monkeypatch.setattr(mail_store, "gmail_store", lib)
        monkeypatch.setattr(mail_store, "_fallbacks_logged", set())
        return lib

    return install


def test_without_the_library_every_read_goes_to_gmail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mail_store, "gmail_store", None)
    assert mail_store.thread("t1", WORK) is None
    assert mail_store.sent_since(WORK, datetime(2026, 1, 1), 10) is None
    assert mail_store.new_threads(WORK, 3) is None
    assert mail_store.latest_with(WORK, "a@b.example", datetime(2026, 1, 1)) == (
        False,
        None,
    )
    mail_store.catch_up((WORK,))  # nothing to catch up, and no error


def test_a_current_store_answers_and_is_closed(library) -> None:  # type: ignore[no-untyped-def]
    store = FakeStore(None, {"t1": {"id": "t1", "messages": []}})
    library(store)
    assert mail_store.thread("t1", WORK) == {"id": "t1", "messages": []}
    assert mail_store.sent_since(WORK, datetime(2026, 1, 1), 10) == [{"id": "m1"}]
    assert store.closed == 2


def test_new_threads_since_the_cursor(library) -> None:  # type: ignore[no-untyped-def]
    library(FakeStore(None, {}))
    # Only threads with mail added count; the cursor moves to the newest change seen.
    assert mail_store.new_threads(WORK, 3) == ({"t1", "t3"}, 6)
    # No cursor yet, or one too old: every thread is treated as changed.
    assert mail_store.new_threads(WORK, None) == (None, 6)
    assert mail_store.new_threads(WORK, 1) == (None, 6)


def test_a_store_that_fell_behind_falls_back_and_says_so_once(library) -> None:  # type: ignore[no-untyped-def]
    lib = library(FakeStore("behind by 2 hours", {"t1": {"id": "t1"}}))
    assert mail_store.thread("t1", WORK) is None
    assert mail_store.thread("t1", WORK) is None
    assert lib.fallbacks == [("today", WORK, "behind by 2 hours")]
    assert lib.store is not None and lib.store.closed == 2
