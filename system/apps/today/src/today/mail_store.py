"""The user's mail read from a synced local mail store instead of Gmail, when there is one.

An optional Gmail sync (a ``people_archive.gmail_store`` module, not part of this app) can keep
a copy of each inbox within a few minutes of Gmail. Today reads threads, what the user sent,
and what changed from it; each function returns None when the store
cannot answer (it has fallen behind, or the environment has no store), and the caller then asks
Gmail as it always did. Each fallback is logged once per process and account.
"""

import importlib
import logging
import threading
from datetime import datetime
from typing import Any

# The store's reader lives in a separate library; without it installed, every read goes to
# Gmail as before.
try:
    gmail_store: Any = importlib.import_module("people_archive.gmail_store")
except ImportError:
    gmail_store = None

log = logging.getLogger(__name__)

_lock = threading.Lock()
_fallbacks_logged: set[str] = set()


def _open(account: str) -> Any:
    """A store connection that can answer for this account now, or None."""
    if gmail_store is None:
        return None
    store = gmail_store.open_store()
    reason = (
        "no synced mail store"
        if store is None
        else store.unusable_reason(account, needs_labels=True)
    )
    if reason is None:
        return store
    if store is not None:
        store.close()
    with _lock:
        is_first = account not in _fallbacks_logged
        _fallbacks_logged.add(account)
    if is_first:
        log.info(
            "synced mail store unusable for %s (%s); reading Gmail", account, reason
        )
        gmail_store.log_fallback("today", account, reason)
    return None


def catch_up(accounts: tuple[str, ...]) -> None:
    """Have the sync apply what changed in Gmail just now, so this update sees the latest mail."""
    if gmail_store is None:
        return
    for account in accounts:
        if not gmail_store.catch_up(account):
            log.info(
                "could not catch the mail store up for %s; using it as last synced",
                account,
            )


def thread(thread_id: str, account: str) -> dict[str, Any] | None:
    """The thread as Gmail's ``threads.get?format=full`` returns it, or None."""
    store = _open(account)
    if store is None:
        return None
    try:
        return store.thread(account, thread_id)
    finally:
        store.close()


def sent_since(
    account: str, since: datetime, limit: int
) -> list[dict[str, Any]] | None:
    """The user's sent mail after then, full messages, newest first (``in:sent after:``), or None."""
    store = _open(account)
    if store is None:
        return None
    try:
        return store.sent_since(account, since, limit)
    finally:
        store.close()


def sent_containing(
    account: str, needles: list[str], since: datetime
) -> list[dict[str, Any]] | None:
    """The user's sent mail since then holding any of the strings, full messages, or None."""
    store = _open(account)
    if store is None:
        return None
    try:
        return store.sent_containing(account, needles, since)
    finally:
        store.close()


def latest_with(
    account: str, address: str, since: datetime
) -> tuple[bool, dict[str, Any] | None]:
    """(whether the store could answer, the user's newest message with the address since then as
    {id, threadId, internalDate} or None when there is none)."""
    store = _open(account)
    if store is None:
        return False, None
    try:
        return True, store.latest_with(account, address, since)
    finally:
        store.close()


def new_threads(account: str, cursor: int | None) -> tuple[set[str] | None, int] | None:
    """Threads with mail added since the cursor, and the cursor to use next time; the set is None
    when the cursor is unknown or too old (treat every thread as changed). None when the store
    cannot answer."""
    store = _open(account)
    if store is None:
        return None
    try:
        latest = store.latest_change(account)
        if cursor is None:
            return None, latest
        changes = store.changes_since(account, cursor)
        if changes is None:
            return None, latest
        return {c["threadId"] for c in changes if c["kind"] == "added"}, max(
            [cursor, *(c["seq"] for c in changes)]
        )
    finally:
        store.close()
