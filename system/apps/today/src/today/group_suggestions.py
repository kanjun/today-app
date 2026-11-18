"""Today suggests groupings: one quick AI pass over the user's open tasks that are in no project.

A grouping is one effort with an end state (a deal, a hire, an event, a talk), never a
person or a category; two or more tasks; most tasks stay ungrouped. A grouping that is one
of the user's existing projects is never suggested: its tasks are filed into that project instead
(``sync`` events saying why, as for the reader's own filing). The user makes a suggestion a
project, or waves it off for good.

It runs at most once a day, when the user opens the Projects tab and the last run is older than
that, in the background; the page shows the last result meanwhile. Results are kept in
today.db (``suggested_groups``), with what the user dismissed.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any

from today import ai, commitments, project_book, store, user_projects

log = logging.getLogger(__name__)

MODEL = "sonnet"
EVERY = timedelta(days=1)
MAX_TASKS = 300

SYSTEM = (
    "You group the user's open tasks into projects the user might want to track. A project is one effort with a purpose "
    "and an end state (a deal, an event the user is organizing, a hire, a trip, a person's situation). Never group by "
    "person or category alone. Only propose groups of 2 or more tasks where finishing each task moves the same end "
    "state. Most tasks stay ungrouped; that is fine. Titles: short, plain, the outcome in the user's terms.\n"
    "You also get the user's existing projects. When a group's end state is the same effort as one of them (for example, "
    "getting customers set up on a product is the user's customer onboarding project), give that "
    'project\'s id in "project": its tasks will be filed there and it is not suggested. Otherwise "".\n'
    'Answer JSON only: {"groups": [{"title": str, "project": str, "keys": [task keys]}]}'
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS suggested_groups (
    id INTEGER PRIMARY KEY,
    run_at TEXT NOT NULL,
    title TEXT NOT NULL,
    keys TEXT NOT NULL,
    dismissed_at TEXT NOT NULL DEFAULT '',
    kept_as TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS suggestion_runs (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    tasks INTEGER NOT NULL,
    groups INTEGER NOT NULL,
    filed INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
"""

_running = threading.Lock()


def setup(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def last_run(db: sqlite3.Connection) -> str:
    setup(db)
    r = db.execute("SELECT max(at) FROM suggestion_runs").fetchone()
    return str(r[0] or "")


def due(db: sqlite3.Connection, now: datetime | None = None) -> bool:
    at = last_run(db)
    return not at or datetime.fromisoformat(at) < (now or datetime.now()) - EVERY


def ungrouped(book: project_book.Book) -> list[dict[str, Any]]:
    """The user's open tasks in no project of the user's: not filed away, not a pitch, not a step."""
    items = store.load()["items"]
    out = []
    for c in book.all:
        if c["stage"] != "open" or c["is_project"] or project_book.is_step(c) or "pitch" in c["labels"] or "move" in c["labels"] or c["detail"].get("week_goal"):
            continue
        if book.project_of(c):
            continue
        item = items.get(c["legacy_key"] or "") or {}
        filed_away = isinstance(item.get("filed"), str) and item["filed"]
        if filed_away or item.get("no") or item.get("status") in project_book.FINISHED:
            continue
        out.append(c)
    return out[-MAX_TASKS:]


def current(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """The latest run's suggestions the user hasn't dismissed or kept, with their open tasks."""
    setup(db)
    at = db.execute("SELECT max(run_at) FROM suggested_groups").fetchone()[0]
    if not at:
        return []
    book, items = project_book.Book(db), store.load()["items"]
    open_free = {c["id"] for c in ungrouped(book)}
    out = []
    for r in db.execute(
        "SELECT * FROM suggested_groups WHERE run_at = ? AND dismissed_at = '' AND kept_as = '' ORDER BY id", (at,)
    ):
        tasks = [book.by_id[k] for k in json.loads(r["keys"]) if k in open_free]
        if len(tasks) >= 2:
            out.append({"id": r["id"], "title": r["title"], "tasks": [t["title"] for t in tasks],
                        "keys": [project_book.task_link(db, t, items) for t in tasks]})
    return out


def _dismissed(db: sqlite3.Connection) -> list[tuple[str, set[str]]]:
    return [(r[0].lower(), set(json.loads(r[1]))) for r in db.execute(
        "SELECT title, keys FROM suggested_groups WHERE dismissed_at != ''")]


def _was_dismissed(title: str, keys: set[str], dismissed: list[tuple[str, set[str]]]) -> bool:
    return any(title.lower() == t or len(keys & k) * 2 >= len(keys) for t, k in dismissed)


def run() -> dict[str, Any]:
    """One pass: ask, file what belongs to the user's projects, keep the rest as suggestions."""
    with project_book.opened() as db:
        setup(db)
        book = project_book.Book(db)
        tasks = ungrouped(book)
        mine = [p for p in book.projects() if p["stage"] == "open"]
        rows = [{"key": c["id"], "who": c["counterparty"], "title": c["title"],
                 "ask": str(c["detail"].get("ask_text") or "")[:200]} for c in tasks]
        filing = dict(project_book.filer_lines(db))
        projects_in = [{"id": p["id"], "title": p["title"], "what_belongs": filing.get(user_projects.filer_id(p), "")[:700],
                        "user_words": project_book.goal_words(p)[:400]} for p in mine]
    if len(rows) < 2:
        # Nothing to group (a new install, or a nearly empty list): no model call.
        with project_book.opened() as db:
            setup(db)
            db.execute("INSERT INTO suggestion_runs (at, tasks, groups, filed) VALUES (?, ?, 0, 0)",
                       (store.now_iso(), len(rows)))
        return {"tasks": len(rows), "groups": 0, "filed": 0}
    prompt = json.dumps({"projects": projects_in, "tasks": rows}, ensure_ascii=False)
    try:
        answer = ai.complete_json(prompt, system=SYSTEM, model=MODEL, timeout=240)
    except (ai.ClaudeError, OSError) as e:
        with project_book.opened() as db:
            setup(db)
            db.execute("INSERT INTO suggestion_runs (at, tasks, groups, filed, error) VALUES (?, ?, 0, 0, ?)",
                       (store.now_iso(), len(rows), str(e)[:300]))
        raise
    valid = {r["key"] for r in rows}
    mine_ids = {p["id"]: p["title"] for p in projects_in}
    at = store.now_iso()
    groups, filed = 0, 0
    with project_book.opened() as db:
        setup(db)
        dismissed = _dismissed(db)
        for g in answer.get("groups") or []:
            if not isinstance(g, dict):
                continue
            keys = [k for k in g.get("keys") or [] if k in valid]
            title = str(g.get("title") or "").strip()[:120]
            if len(keys) < 2 or not title:
                continue
            pid = str(g.get("project") or "")
            if pid in mine_ids:
                # One of the user's projects already: never suggested; its tasks go in.
                for k in keys:
                    c = commitments.get(db, k)
                    if c is None or c["parent_id"] == pid or pid in (c["detail"].get("not_in") or []):
                        continue
                    commitments.change(
                        db, k, "sync", {"parent_id": pid}, kind="filed",
                        reason=f"Today grouped it with \"{title}\", part of {mine_ids[pid]}; filed there by itself "
                        "(the user, 2026-11-13). Move it out from the task's menu.",
                    )
                    filed += 1
                continue
            if _was_dismissed(title, set(keys), dismissed):
                continue
            db.execute("INSERT INTO suggested_groups (run_at, title, keys) VALUES (?, ?, ?)", (at, title, json.dumps(keys)))
            groups += 1
        db.execute("INSERT INTO suggestion_runs (at, tasks, groups, filed) VALUES (?, ?, ?, ?)",
                   (at, len(rows), groups, filed))
    return {"tasks": len(rows), "groups": groups, "filed": filed}


def run_soon_if_due() -> bool:
    """Start a pass in the background when the last one is a day old. True when started."""
    with project_book.opened() as db:
        if not due(db):
            return False
    if not _running.acquire(blocking=False):
        return False

    def go() -> None:
        try:
            run()
        except (ai.ClaudeError, OSError, sqlite3.Error, commitments.CommitmentError) as e:
            log.warning("suggesting groupings failed: %s", e)
        finally:
            _running.release()

    threading.Thread(target=go, daemon=True).start()
    return True


def running() -> bool:
    return _running.locked()


def keep(db: sqlite3.Connection, gid: int) -> dict[str, str]:
    """The user made a suggestion a project: a project of the user's with those tasks in it (undoable)."""
    setup(db)
    r = db.execute("SELECT * FROM suggested_groups WHERE id = ?", (gid,)).fetchone()
    if r is None or r["dismissed_at"] or r["kept_as"]:
        raise project_book.ProjectError("That suggestion isn't there any more.")
    last = db.execute("SELECT max(position) FROM commitments WHERE is_project = 1").fetchone()[0] or 0
    b = project_book.Batch(db, "user", f"made {r['title']} a project")
    pid = b.create({"title": r["title"], "direction": "self", "origin": "user_chose", "started_by": "user",
                    "is_project": 1, "position": int(last) + 1, "detail": {"from_suggestion": r["id"]}},
                   reason="the user made Today's suggested grouping one of the user's projects")
    for k in json.loads(r["keys"]):
        c = commitments.get(db, k)
        if c is not None and c["stage"] == "open":
            project_book.move_task(b, c, pid, "in the grouping the user made a project")
    db.execute("UPDATE suggested_groups SET kept_as = ? WHERE id = ?", (pid, gid))
    b.ops.append({"t": "sql", "sql": "UPDATE suggested_groups SET kept_as = '' WHERE id = ?", "args": [gid]})
    return {"id": pid, "batch": b.save()}


def dismiss(db: sqlite3.Connection, gid: int) -> dict[str, str]:
    """Not a project: never suggested again (undoable)."""
    setup(db)
    db.execute("UPDATE suggested_groups SET dismissed_at = ? WHERE id = ?", (store.now_iso(), gid))
    b = project_book.Batch(db, "user", "not a project")
    b.ops.append({"t": "sql", "sql": "UPDATE suggested_groups SET dismissed_at = '' WHERE id = ?", "args": [gid]})
    return {"batch": b.save()}
