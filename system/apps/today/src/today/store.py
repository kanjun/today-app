"""Today's state: one JSON file under DATA_DIR, guarded by a file lock.

The web app and the sync job both read-modify-write it, so every change goes
through ``mutate`` (lock, load, change, atomic write). Raw source records (the
Slack conversation or email thread an item came from) are kept per item under
``raw/`` so the view can always show the original.
"""

import fcntl
import json
import os
import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("TODAY_DATA_DIR", "data/.apps/today"))


def state_path() -> Path:
    return DATA_DIR / "state.json"


def empty_state() -> dict[str, Any]:
    return {
        "items": {},
        "today": {"date": "", "order": []},
        "last_sync": None,
        "sync_error": None,
    }


def load() -> dict[str, Any]:
    try:
        state = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return empty_state()
    base = empty_state()
    base.update(state)
    return base


@contextmanager
def _locked() -> Generator[None, None, None]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _write(state: dict[str, Any]) -> None:
    tmp = state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    os.replace(tmp, state_path())


def mutate(change: Callable[[dict[str, Any]], Any]) -> Any:
    with _locked():
        state = load()
        result = change(state)
        _write(state)
        return result


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def raw_path(key: str) -> Path:
    return DATA_DIR / "raw" / (re.sub(r"[^A-Za-z0-9._-]+", "_", key) + ".json")


def save_raw(key: str, record: dict[str, Any]) -> None:
    path = raw_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False))


def load_raw(key: str) -> dict[str, Any] | None:
    try:
        return json.loads(raw_path(key).read_text())
    except (OSError, ValueError):
        return None


def roll_today(state: dict[str, Any], today: str) -> None:
    """A new day starts with an empty Today list; nothing is lost, it all stays in Owed."""
    if state["today"].get("date") != today:
        state["today"] = {"date": today, "order": []}
