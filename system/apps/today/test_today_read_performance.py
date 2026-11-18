"""Reads preserve today's list without rewriting the store or re-parsing unchanged sources."""

from datetime import date
from pathlib import Path

import pytest
from today import actions, store


def test_list_reads_only_write_at_day_rollover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    store.mutate(
        lambda s: s.update(today={"date": date.today().isoformat(), "order": []})
    )
    writes: list[object] = []
    real = store.mutate
    monkeypatch.setattr(store, "mutate", lambda fn, by="": writes.append(fn) or real(fn, by))
    actions.view_state()
    assert writes == []
    store.mutate(lambda s: s.update(today={"date": "2000-01-01", "order": ["old"]}))
    actions.view_state()
    assert store.load()["today"] == {"date": date.today().isoformat(), "order": []}
