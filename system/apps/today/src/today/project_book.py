"""The user's projects, kept as commitments.

A project is a commitment to themselves (``is_project=1``, direction ``self``, origin
``user_chose``) with the user's tasks under it (``parent_id``). The old Today goals came over in the
import as projects too; they are not the user's: one that sits under one of the user's projects just holds
some of that project's tasks. Today's suggested groupings come from a fresh AI pass instead
(group_suggestions.py).

Every write goes through ``commitments.create`` and ``commitments.change`` (one event
each, with a reason), and every batch of writes can be undone, the Undo itself being more
events. The user's words (the box text, the user's purpose) are kept verbatim in the project's
``detail``.

Until the page reads commitments (stage 4), Today's list still reads the JSON items, so
a change to a task that came from an item is mirrored onto that item (``mirror_item``),
never with anything leaving Today: no send, no archive, no GTD write.
"""

import json
import re
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any

from today import (
    commitments,
    commitments_import,
    config,
    conversations,
    learned,
    ledger,
    page,
    projects,
    store,
    user_projects,
)


def seeds() -> tuple[dict[str, str], ...]:
    """The projects the user set up in the ``projects`` setting (config.py), in their order; none
    by default. ``legacy`` is the key an item filed into it carries (``project:<id>``); ``folder``
    holds the user's Goal.md, ``agent`` names the project agent whose playbook is read with it."""
    return tuple(
        {"legacy": f"project:{p['id']}", "title": p["title"], "short": p.get("short", ""), "about": p.get("about", ""),
         "folder": p.get("folder", ""), "agent": p.get("agent", "")}
        for p in config.load().projects
    )


SEED_REASON = "one of the user's projects, from Today's settings"
# The only statements an Undo may run besides commitment events (the suggestion marks).
UNDO_SQL = (
    "UPDATE suggested_groups SET kept_as = '' WHERE id = ?",
    "UPDATE suggested_groups SET dismissed_at = '' WHERE id = ?",
    # A thread the user attached to a task, and one the user took off (attached.py).
    "DELETE FROM commitment_links WHERE commitment_id = ? AND conversation_id = ? AND message_id = '' AND role = 'attached'",
    "INSERT OR IGNORE INTO commitment_links (commitment_id, conversation_id, message_id, role) VALUES (?, ?, '', 'attached')",
    # A note kept on a task, and one taken off (task_context.py).
    "UPDATE task_notes SET removed_at = ?, removed_by = ? WHERE id = ?",
    "UPDATE task_notes SET removed_at = '', removed_by = '' WHERE id = ?",
)
FINISHED = ("done", "sent", "skipped", "cleared")
ABSENT = "__absent__"


class ProjectError(ValueError):
    """Something the user asked for that can't be done, in words for the user."""


@contextmanager
def opened() -> Generator[sqlite3.Connection, None, None]:
    with conversations.opened(store.DATA_DIR) as db:
        setup(db)
        ensure_seeded(db)  # cheap once done
        yield db


def setup(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS undo_batches (id TEXT PRIMARY KEY, at TEXT NOT NULL, label TEXT NOT NULL, "
        "ops TEXT NOT NULL, undone_at TEXT NOT NULL DEFAULT '')"
    )


# Before the commitments import has run there is nothing to show, and making projects here
# would block the import.
is_old_goal = user_projects.is_old_goal
is_users = user_projects.is_users
goal_words = user_projects.goal_words
goal_path = user_projects.goal_path
playbook_path = user_projects.playbook_path
filer_lines = user_projects.filer_lines


# ---- what is a project, and what is in one --------------------------------------------


def is_step(c: dict[str, Any]) -> bool:
    return "step" in c["labels"]


class Book:
    """Every commitment, read once, with the lookups the pages need."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.all = commitments.all_commitments(db)
        self.by_id = {c["id"]: c for c in self.all}
        self.by_legacy = {c["legacy_key"]: c for c in self.all if c["legacy_key"]}
        self.kids: dict[str, list[dict[str, Any]]] = {}
        for c in self.all:
            self.kids.setdefault(c.get("parent_id") or "", []).append(c)

    def project_of(self, c: dict[str, Any] | None) -> dict[str, Any] | None:
        """The project of the user's a commitment sits in (through any old goal), or None."""
        seen: set[str] = set()
        p = self.by_id.get((c or {}).get("parent_id") or "")
        while p and p["id"] not in seen:
            if is_users(p):
                return p
            seen.add(p["id"])
            p = self.by_id.get(p.get("parent_id") or "")
        return None

    def projects(self) -> list[dict[str, Any]]:
        mine = [c for c in self.all if is_users(c) and c["stage"] in ("open", "done")]
        return sorted(mine, key=lambda c: (c["position"], c["created_at"]))

    def tasks(self, pid: str) -> list[dict[str, Any]]:
        """Every task in a project, through the old goals that hold some of them; steps and
        projects of the user's own inside it are not tasks."""
        out, todo, seen = [], list(self.kids.get(pid, [])), {pid}
        while todo:
            c = todo.pop(0)
            if c["id"] in seen or is_step(c):
                continue
            seen.add(c["id"])
            if is_old_goal(c):
                todo.extend(self.kids.get(c["id"], []))
            elif not c["is_project"]:
                out.append(c)
        return out

    def steps(self, cid: str) -> list[dict[str, Any]]:
        # A step replaced by new ones, or undone, is no longer one of its steps.
        return sorted((s for s in self.kids.get(cid, []) if is_step(s) and s["stage"] not in ("stale", "declined")),
                      key=lambda s: (s["position"], s["id"]))

    def by_filer_id(self, fid: str) -> dict[str, Any] | None:
        return next((p for p in self.projects() if user_projects.filer_id(p) == fid and p["stage"] == "open"), None)


def instructions(db: sqlite3.Connection, c: dict[str, Any] | None) -> str:
    """What a task's project tells an AI working on the task: the stages its tasks move
    through, where this one is, the move to the next stage, and the user's templates word for word
    (any project can hold these: ``detail.stages``, ``detail.templates``, ``detail.settings``)."""
    seen: set[str] = set()
    p = commitments.get(db, (c or {}).get("parent_id") or "")
    while p is not None and p["id"] not in seen and not p["detail"].get("stages"):
        seen.add(p["id"])
        p = commitments.get(db, p.get("parent_id") or "")
    if c is None or p is None:
        return ""
    stages = p["detail"]["stages"]
    lines = [f"This task is in the user's project {p['title']}. Its tasks move through these stages, in order:"]
    for n, s in enumerate(stages, 1):
        nxt = f" Next: {s['next']}" + (f" (the user {s['template']} template)" if s.get("template") else "") + "." if s.get("next") else ""
        lines.append(f"{n}. {s['title']}.{nxt}")
    at = next((s for s in stages if s["id"] == c["detail"].get("stage")), None)
    if at:
        lines.append(f"This task is at: {at['title']}. Offer the move to the next stage first"
                     + (", written from the user's template." if at.get("template") else "."))
    templates = p["detail"].get("templates") or {}
    if templates:
        lines.append("The user's templates, word for word (fill in the {placeholders}; never reword them; \"invite\" means the user "
                     "email_template, or text_template for someone reached by text):\n"
                     + "\n".join(f"{name}:\n{text}" for name, text in templates.items()))
    settings = p["detail"].get("settings") or {}
    if settings:
        lines.append("The project's settings: " + "; ".join(f"{k}: {v}" for k, v in settings.items()))
    person = c["detail"].get("person") or {}
    if person:
        lines.append("The person: " + json.dumps(person, ensure_ascii=False))
    return "\n".join(lines)


def short_title(p: dict[str, Any]) -> str:
    return str(p["detail"].get("short") or p["title"])


# ---- setting up the user's projects ------------------------------------------------------------


def ensure_seeded(db: sqlite3.Connection) -> None:
    """Make sure the projects in the user's settings exist."""
    for n, seed in enumerate(seeds()):
        c = commitments.by_legacy_key(db, seed["legacy"])
        extra = {k: seed[k] for k in ("folder", "agent", "short", "about") if seed[k]}
        if c is None:
            commitments.create(
                db, "user",
                {"title": seed["title"], "direction": "self", "origin": "user_chose", "started_by": "user",
                 "is_project": 1, "position": n, "detail": dict(extra, seeded=True)},
                legacy_key=seed["legacy"], reason=SEED_REASON,
            )
        elif not c["detail"].get("seeded"):
            fields: dict[str, Any] = {"position": n, "detail": dict(c["detail"], seeded=True, **extra)}
            commitments.change(db, c["id"], "user", fields, reason=SEED_REASON)


# ---- what the user sees -----------------------------------------------------------------------


def _items() -> dict[str, dict[str, Any]]:
    return store.load()["items"]


def standing(c: dict[str, Any], item: dict[str, Any] | None) -> str:
    """Where a task stands, as the project page groups it: open, waiting, later or done.
    Until the page reads commitments, a click on Today's list changes only the item, so
    the item counts too."""
    if c["stage"] in ("done", "declined", "stale"):
        return "done"
    if item is not None:
        if item.get("status") in FINISHED:
            return "done"
        if item.get("not_now") or str(item.get("later_until") or "") > date.today().isoformat():
            return "later"
        if item.get("state") == "waiting":
            return "waiting"
    if c["stage"] == "parked":
        return "later"
    if c["direction"] == "theirs":
        return "waiting"
    return "open"


def task_link(db: sqlite3.Connection, c: dict[str, Any], items: dict[str, dict[str, Any]]) -> str:
    """The key of a task's own page: its item, or a commitment the judge found in a
    conversation (its item is made when the page opens). "" when it has no page."""
    if c["legacy_key"] and c["legacy_key"] in items:
        return str(c["legacy_key"])
    if ledger.is_commitment_key(c["id"]) and commitments.conversations_of(db, c["id"]):
        return str(c["id"])
    return ""


def project_list(db: sqlite3.Connection) -> dict[str, Any]:
    book, items = Book(db), _items()
    rows, completed = [], []
    for p in book.projects():
        tasks = [t for t in book.tasks(p["id"]) if standing(t, items.get(t["legacy_key"] or "")) == "open"]
        tasks.sort(key=_order)
        tasks = [t for t in tasks if not is_then(t)] + [t for t in tasks if is_then(t)]
        # The task shown is the one due soonest, else the first in the user's order.
        dated = sorted((t for t in tasks if _when(t)), key=lambda t: t["due"])
        shown = (dated or tasks or [None])[0]
        row = {"id": p["id"], "title": p["title"], "next": shown["title"] if shown else "",
               "next_key": task_link(db, shown, items) if shown else "", "open": len(tasks),
               "when": _when(shown) if shown else "", "due": shown["due"][:10] if dated and shown else ""}
        (rows if p["stage"] == "open" else completed).append(row)
    return {"ready": True, "projects": sorted(rows, key=_urgency), "completed": completed}


def _when(c: dict[str, Any]) -> str:
    return page.due_spot({"due": c["due"] or "", "due_set": c["detail"].get("due_set_by") == "user"})


def _urgency(row: dict[str, Any]) -> tuple[int, str]:
    """Projects by what the user has to do (the user, 2026-11-16): one with a task due comes first,
    soonest first; then those with tasks and no date, in the user's order; one with nothing to do last."""
    return (0, row["due"]) if row["due"] else (1, "") if row["open"] else (2, "")


def _order(c: dict[str, Any]) -> tuple[Any, ...]:
    # The user's order first (position), then oldest first, like every list the user works through.
    return (c["position"], str(c["detail"].get("asked_at") or c["created_at"]))


def is_then(c: dict[str, Any]) -> bool:
    """A task the user put under Then. Once any task in a project is, Next holds every other open
    task; until then the page shows the first three as Next (the user, Nov 14: Next is the big
    list of people to onboard, Then the influencer work)."""
    return bool(c["detail"].get("then"))


def split_next(
    open_tasks: list[Any], then_of: Callable[[Any], bool], sectioned: bool = False
) -> tuple[list[Any], list[Any], bool]:
    """Open tasks (in page order) as (Next, Then, sectioned). ``sectioned`` is the project's
    own flag: once the user has dragged tasks between the two, an empty Then stays empty."""
    if sectioned or any(then_of(t) for t in open_tasks):
        return [t for t in open_tasks if not then_of(t)], [t for t in open_tasks if then_of(t)], True
    return open_tasks[:3], open_tasks[3:], False


def project_page(db: sqlite3.Connection, pid: str) -> dict[str, Any] | None:
    book = Book(db)
    p = book.by_id.get(pid)
    if p is None or not is_users(p):
        return None
    items = _items()
    groups: dict[str, list[dict[str, Any]]] = {"open": [], "waiting": [], "later": [], "done": []}
    then_ids: set[str] = set()
    for t in sorted(book.tasks(pid), key=_order):
        item = items.get(t["legacy_key"] or "")
        where = standing(t, item)
        steps = book.steps(t["id"])
        row = task_row(t, item, steps, page)
        if where == "waiting":
            since = str((item or {}).get("asked_at") or t["detail"].get("asked_at") or t["created_at"])
            row["days"] = max(0, (datetime.now() - datetime.fromisoformat(since[:19])).days)
        if where == "done":
            row["closed_at"] = str((item or {}).get("finished_at") or t["updated_at"])
        if where == "open" and is_then(t):
            then_ids.add(row["cid"])
        groups[where].append(row)
    nxt, then, sectioned = split_next(groups["open"], lambda r: r["cid"] in then_ids, bool(p["detail"].get("sectioned")))
    groups["done"].sort(key=lambda r: r.get("closed_at", ""), reverse=True)
    others = [{"id": o["id"], "title": o["title"]} for o in book.projects() if o["id"] != pid and o["stage"] == "open"]
    return {
        "id": p["id"],
        "title": p["title"],
        "status": p["stage"],
        "version": p["version"],
        "words": goal_words(p),
        "notes": list(p["detail"].get("notes") or []),
        "tasks": groups["open"],
        "next": nxt,
        "then": then,
        "sectioned": sectioned,
        "waiting": groups["waiting"],
        "later": groups["later"],
        "done": groups["done"][:60],
        "others": others,
        "has_sources": bool(goal_path(p) or playbook_path(p)),
    }


def task_row(c: dict[str, Any], item: dict[str, Any] | None, steps: list[dict[str, Any]], page: Any) -> dict[str, Any]:
    """A task as Today's own item component draws it, plus what the project page adds."""
    # Steps are a checklist under the task (task_steps.py); the title stays the task's own.
    extra = {
        "cid": c["id"],
        "steps": [dict(s["detail"], cid=s["id"], title=s["title"], done=s["stage"] == "done",
                       kind=s["detail"].get("kind") or "plain") for s in steps],
        "steps_done": sum(1 for s in steps if s["stage"] == "done"),
        "step_total": len(steps),
    }
    # The task's date is the commitment's (the user sets it in the picker), not its old item's.
    dated = {"due": c["due"] or "", "due_set": c["detail"].get("due_set_by") == "user"}
    if item is not None:
        item = dict(item, **dated)
        row = page.row(item, sendable=page.can_send(item), **extra)
        row["ask"] = re.sub(r"^In the group [^:]+: ", "", page.clean_text(item.get("ask_text") or ""))
        row["title"] = c["title"] or row["title"]
        # The task's title, never a step's words, and no "then:" step line from the old plan.
        row["next_step"] = ""
        return row
    # A task with no item behind it: the user's own, shown by its title.
    return dict(
        {"key": "c:" + c["id"], "who": "", "words": "", "mine_words": True, "title": c["title"], "kind": "todo",
         "when": page.due_spot(dated), "due": dated["due"][:10], "status": c["stage"], "state": "mine", "url": "", "source": "", "open_label": "", "draft": "",
         "nudge": "", "questions": [], "emails": 0, "can_draft": False, "manual": True, "filed": "", "error": "",
         "next_step": c["next_step"], "own": True, "move": None, "goal_id": "", "notes": "", "goal": "",
         "placeholder": False, "left_off": "", "sendable": False, "ask": ""},
        **extra,
    )


def main_list_labels(db: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """Item key -> the project of the user's its commitment sits in, for the grey name on Today's
    list. Empty before the import has run."""
    book = Book(db)
    out = {}
    for c in book.all:
        p = book.project_of(c) if c["stage"] in ("open", "parked") and not c["is_project"] else None
        if p and p["stage"] == "open":
            label = {"id": p["id"], "title": short_title(p), "full": p["title"]}
            out[c["id"]] = label  # rows for commitments the judge found are keyed by id
            if c["legacy_key"]:
                out[c["legacy_key"]] = label
    return out


def step_progress(db: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """Row key -> (steps done, steps) for every open task with steps, for the progress ring."""
    book, out = Book(db), {}
    for c in book.all:
        if c["stage"] not in ("open", "parked") or is_step(c) or c["is_project"]:
            continue
        steps = book.steps(c["id"])
        if steps:
            prog = (sum(1 for s in steps if s["stage"] == "done"), len(steps))
            out[c["id"]] = prog
            if c["legacy_key"]:
                out[c["legacy_key"]] = prog
    return out


def filing_suggestions(db: sqlite3.Connection, items: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Item key -> a project of the user's it looks like part of (the reader's guess), for the
    quiet "Part of ...?" the user can accept with one tap. See ``projects.file_or_suggest``."""
    book = Book(db)
    out = {}
    for item in items:
        fid = str(item.get("project") or "")
        p = book.by_filer_id(fid) if fid else None
        if p is None:
            continue
        c = book.by_legacy.get(item["key"])
        if c is not None and (book.project_of(c) or p["id"] in (c["detail"].get("not_in") or [])):
            continue
        out[item["key"]] = {"id": p["id"], "title": short_title(p), "full": p["title"]}
    return out


def raw_sources(db: sqlite3.Connection, pid: str) -> list[dict[str, str]]:
    p = commitments.get(db, pid)
    if p is None:
        return []
    out = []
    for label, path in (("Goal.md, in your words", goal_path(p)), ("The project agent's playbook", playbook_path(p))):
        if path:
            out.append({"label": label, "path": str(path), "text": user_projects.read(path)})
    return out


# ---- writes, each undoable ---------------------------------------------------------------


class Batch:
    """Writes made together, recorded so one Undo reverses them all (as more events)."""

    def __init__(self, db: sqlite3.Connection, actor: str, label: str) -> None:
        self.db, self.actor, self.label = db, actor, label
        self.ops: list[dict[str, Any]] = []
        self.changed: list[str] = []  # item keys (or c:<id>) the page marks with a green edge
        self.counts: dict[str, int] = {}
        self.own: dict[str, int] = {}  # versions this batch itself moved a commitment to

    def _mark(self, c: dict[str, Any] | None) -> None:
        if c is None:
            return
        key = c.get("legacy_key") or "c:" + c["id"]
        if key not in self.changed:
            self.changed.append(key)

    def count(self, what: str) -> None:
        self.counts[what] = self.counts.get(what, 0) + 1

    def change(self, cid: str, fields: dict[str, Any], reason: str, *, expect: int | None = None,
               kind: str = "changed", evidence: tuple[str, ...] = (), mark: bool = True) -> dict[str, Any]:
        cur = commitments.get(self.db, cid)
        if cur is None:
            raise ProjectError("That task isn't there any more.")
        before = {k: cur[k] for k in fields}
        out = commitments.change(self.db, cid, self.actor, fields, expect_version=expect, reason=reason,
                                 kind=kind, evidence=evidence)
        self.ops.append({"t": "c", "cid": cid, "before": before})
        self.own[cid] = out["version"]
        if mark:
            self._mark(out)
        return out

    def create(self, fields: dict[str, Any], reason: str, legacy_key: str | None = None,
               links: tuple[tuple[str, str, str], ...] = ()) -> str:
        cid = commitments.create(self.db, self.actor, fields, legacy_key=legacy_key, links=links, reason=reason)
        self.ops.append({"t": "new", "cid": cid})
        if not is_step({"labels": fields.get("labels") or []}):
            self._mark(commitments.get(self.db, cid))
        return cid

    def item(self, key: str, fields: dict[str, Any]) -> None:
        """Mirror a change onto Today's item (the list still reads it until stage 4)."""
        before = mirror_item(key, fields)
        if before is not None:
            self.ops.append({"t": "json", "key": key, "before": before})

    def save(self) -> str:
        bid = "b" + datetime.now().strftime("%Y%m%d%H%M%S%f")
        self.db.execute(
            "INSERT INTO undo_batches (id, at, label, ops) VALUES (?, ?, ?, ?)",
            (bid, store.now_iso(), self.label, json.dumps(self.ops, ensure_ascii=False)),
        )
        return bid


def mirror_item(key: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Set fields on a JSON item (None removes one); returns what they were, or None when
    there is no such item."""

    def change(state: dict[str, Any]) -> dict[str, Any] | None:
        item = state["items"].get(key)
        if item is None:
            return None
        before = {k: item.get(k, ABSENT) for k in fields}
        for k, v in fields.items():
            if v is None or v == ABSENT:
                item.pop(k, None)
            else:
                item[k] = v
        return before

    if not key or key.startswith("c:"):
        return None
    return store.mutate(change)


def undo(db: sqlite3.Connection, bid: str) -> str:
    row = db.execute("SELECT * FROM undo_batches WHERE id = ?", (bid,)).fetchone()
    if row is None:
        raise ProjectError("Nothing to undo.")
    if row["undone_at"]:
        return "Already undone"
    reason = f"Undo: {row['label']}"
    for op in reversed(json.loads(row["ops"])):
        if op["t"] == "c":
            commitments.change(db, op["cid"], "user", op["before"], reason=reason, kind="undone")
        elif op["t"] == "new":
            commitments.change(db, op["cid"], "user", {"stage": "stale", "stage_reason": "undone"},
                               reason=reason, kind="undone")
        elif op["t"] == "json":
            mirror_item(op["key"], op["before"])
        elif op["t"] == "sql" and op["sql"] in UNDO_SQL:
            db.execute(op["sql"], op["args"])
        elif op["t"] == "learned":
            learned.set_removed(db, op["id"])
    db.execute("UPDATE undo_batches SET undone_at = ? WHERE id = ?", (store.now_iso(), bid))
    return "Put back"


def _project(db: sqlite3.Connection, pid: str) -> dict[str, Any]:
    p = commitments.get(db, pid)
    if p is None or not is_users(p):
        raise ProjectError("There's no such project.")
    return p


def create_project(db: sqlite3.Connection, title: str) -> dict[str, str]:
    last = db.execute("SELECT max(position) FROM commitments WHERE is_project = 1").fetchone()[0] or 0
    b = Batch(db, "user", f"new project {title}")
    pid = b.create({"title": title, "direction": "self", "origin": "user_chose", "started_by": "user",
                    "is_project": 1, "position": int(last) + 1, "detail": {}}, reason="a project the user named")
    return {"id": pid, "batch": b.save()}


def rename(db: sqlite3.Connection, pid: str, title: str) -> str:
    p = _project(db, pid)
    b = Batch(db, "user", f"renamed {p['title']}")
    b.change(pid, {"title": title}, reason="the user renamed it")
    return b.save()


def set_words(db: sqlite3.Connection, pid: str, text: str) -> str:
    p = _project(db, pid)
    b = Batch(db, "user", "the user's words on what it's for")
    b.change(pid, {"detail": dict(p["detail"], words=text)}, reason="the user's words on what the project is for")
    return b.save()


def add_note(db: sqlite3.Connection, pid: str, text: str, task: dict[str, Any] | None = None) -> None:
    """What the user typed in the box, kept verbatim with when, never removed (not even by Undo)."""
    p = _project(db, pid)
    note = {"at": store.now_iso(), "text": text}
    if task:
        note.update(task=task["id"], task_title=task["title"])
    notes = list(p["detail"].get("notes") or []) + [note]
    commitments.change(db, pid, "user", {"detail": dict(p["detail"], notes=notes)},
                       reason="what the user said in the project's box", kind="note")


def complete(db: sqlite3.Connection, pid: str) -> str:
    """The whole project done, its open tasks with it (nothing is sent or archived)."""
    p = _project(db, pid)
    book, items = Book(db), _items()
    b = Batch(db, "user", f"completed {p['title']}")
    for t in book.tasks(pid):
        if standing(t, items.get(t["legacy_key"] or "")) in ("open", "waiting", "later"):
            finish(b, t, "done", "the project is complete")
    b.change(pid, {"stage": "done", "stage_reason": "the user completed the project"}, reason="the user completed it", mark=False)
    return b.save()


def finish(b: Batch, t: dict[str, Any], stage: str, why: str, evidence: str = "") -> None:
    """Close a task (done, declined or stale) and its item, with nothing leaving Today."""
    b.change(t["id"], {"stage": stage, "stage_reason": why}, reason=evidence or why)
    now = store.now_iso()
    b.item(t["legacy_key"] or "", {
        "status": "done" if stage == "done" else "skipped", "finished_at": now, "cleared_at": now,
        "cleared_reason": f"{why}; nothing sent",
    })


def ensure_commitment(db: sqlite3.Connection, key: str) -> dict[str, Any]:
    """The commitment behind a Today item, made now (as the import would) when the item
    came after the import."""
    c = commitments.by_legacy_key(db, key)
    if c is not None:
        return c
    item = _items().get(key)
    if item is None:
        raise ProjectError("That item isn't in Today.")
    direction, origin, started_by = commitments_import.classify(item)
    conv = conversations.conversation_id(item)
    cid = commitments.create(
        db, "sync",
        {"title": item.get("title") or item.get("ask_text") or key, "direction": direction, "origin": origin,
         "started_by": started_by, "counterparty": "" if direction == "self" else str(item.get("who") or ""),
         "due": str(item.get("due") or ""),
         "detail": {k: item[k] for k in commitments_import.DETAIL_KEYS if item.get(k) not in (None, "", [], {})}},
        legacy_key=key, links=[(conv, commitments_import.origin_message(item), "origin")] if conv else [],
        reason=f"Today item {key}, tracked as a commitment when the user filed it",
    )
    out = commitments.get(db, cid)
    assert out is not None
    return out


def _end_position(db: sqlite3.Connection, pid: str) -> int:
    """A place after every task already in the project: a task the user adds or moves in goes at the
    bottom of its list, not the top (the user, Nov 15)."""
    last = db.execute("SELECT max(position) FROM commitments WHERE parent_id = ?", (pid,)).fetchone()[0]
    return int(last or 0) + 1


def move_task(b: Batch, c: dict[str, Any], to: str, why: str) -> None:
    """Into a project of the user's (``to``), or out of every project (``to`` empty)."""
    book = Book(b.db)
    was = book.project_of(c)
    if to:
        _project(b.db, to)
    fields: dict[str, Any] = {"parent_id": to or None}
    if to and (was or {}).get("id") != to:
        fields["position"] = _end_position(b.db, to)
    if was and was["id"] != to:
        fields["detail"] = dict(c["detail"], not_in=sorted({*(c["detail"].get("not_in") or []), was["id"]}))
    b.change(c["id"], fields, reason=why)


def _new_todo(title: str) -> str:
    """A to-do of the user's on Today's list (as its Add box makes one), not picked for today."""
    key = f"manual:{datetime.now().timestamp():.0f}"
    while key in _items():
        key += "x"
    now = store.now_iso()
    store.mutate(lambda s: s["items"].__setitem__(key, {
        "key": key, "source": "manual", "manual": True, "who": "", "title": title, "ask_text": title,
        "state": "mine", "status": "open", "asked_at": now, "did": "", "draft": "", "questions": [], "created": now,
    }))
    return key


def add_task(b: Batch, pid: str, title: str, why: str) -> str:
    """A new task of the user's in the project: a to-do on Today's list too, so it shows there."""
    key = _new_todo(title)
    c = commitments.by_legacy_key(b.db, key)
    assert c is not None  # the to-do is its task (cards.py)
    commitments.change(b.db, c["id"], b.actor, {"parent_id": pid, "position": _end_position(b.db, pid)}, reason=why)
    b.ops.append({"t": "new", "cid": c["id"]})  # Undo sets the new task aside
    b._mark(commitments.get(b.db, c["id"]))
    return c["id"]


def order_tasks(db: sqlite3.Connection, pid: str, next_cids: list[str], then_cids: list[str]) -> dict[str, str]:
    """The user's drag on a project page: the open tasks of Next and Then, each in the user's order. Tasks
    the user did not list keep their place after these."""
    book = Book(db)
    p = book.by_id.get(pid)
    if p is None or not is_users(p):
        raise ProjectError("There's no such project.")
    mine = {t["id"]: t for t in book.tasks(pid)}
    wanted = [c for c in [*next_cids, *then_cids] if c in mine]
    if len(set(wanted)) != len(wanted) or not wanted:
        raise ProjectError("That order doesn't match the project's tasks.")
    then = set(then_cids)
    rest = sorted((t for c, t in mine.items() if c not in set(wanted)), key=_order)
    b = Batch(db, "user", "Reordered tasks")
    for n, t in enumerate([*(mine[c] for c in wanted), *rest], start=1):
        fields: dict[str, Any] = {}
        if t["position"] != n:
            fields["position"] = n
        if t["id"] in set(wanted) and is_then(t) != (t["id"] in then):
            fields["detail"] = dict(t["detail"], then=t["id"] in then)
        if fields:
            b.change(t["id"], fields, reason="the user dragged it", mark=False)
    if not p["detail"].get("sectioned"):
        b.change(p["id"], {"detail": dict(p["detail"], sectioned=True)}, reason="the user split Next and Then", mark=False)
    return {"batch": b.save() if b.ops else "", "message": "Moved"}


def task_action(db: sqlite3.Connection, pid: str, key: str, action: str, to: str = "") -> dict[str, str]:
    """The ··· menu on a project's task: not now, move, not mine, drop (and done, add)."""
    c = ensure_commitment(db, key) if not key.startswith("c:") else commitments.get(db, key[2:])
    if c is None:
        raise ProjectError("That task isn't there any more.")
    b = Batch(db, "user", action)
    if action == "later":
        b.change(c["id"], {"stage": "parked", "stage_reason": "Not now"}, reason="the user said not now")
        b.item(c["legacy_key"] or "", {"not_now": store.now_iso()})
        msg = "Moved to Later"
    elif action == "back":
        b.change(c["id"], {"stage": "open", "stage_reason": ""}, reason="the user brought it back")
        b.item(c["legacy_key"] or "", {"not_now": None, "later_until": None})
        msg = "Back on the list"
    elif action == "move":
        move_task(b, c, to, "the user moved it")
        msg = "Moved to " + _project(db, to)["title"] if to else "Taken out of this project"
    elif action == "notmine":
        finish(b, c, "declined", "not the user's to do, the user said")
        b.item(c["legacy_key"] or "", {"filed": "not_yours"})
        msg = "Marked not yours"
    elif action == "drop":
        finish(b, c, "declined", "the user dropped it")
        msg = "Dropped"
    elif action == "done":
        finish(b, c, "done", "the user marked it done")
        msg = "Done"
    elif action == "add":
        move_task(b, c, pid, "the user put it in this project")
        msg = "Moved into this project"
    else:
        raise ProjectError("That isn't one of the choices.")
    return {"batch": b.save(), "message": msg}


def file_item(db: sqlite3.Connection, key: str, pid: str) -> str:
    """The user accepted Today's guess that an item is part of a project."""
    c = ensure_commitment(db, key)
    b = Batch(db, "user", "filed into a project")
    move_task(b, c, pid, "the user said it's part of this project")
    return b.save()


def new_task(db: sqlite3.Connection, pid: str, title: str) -> str:
    _project(db, pid)
    b = Batch(db, "user", f"added {title}")
    add_task(b, pid, title, "the user added it to the project")
    return b.save()


def _filing_target(book: Book, item: dict[str, Any] | None) -> dict[str, Any] | None:
    """The project of the user's to file an item into automatically, or None. Never moves it out of
    a project it is already in, nor back into one the user took it out of."""
    if projects.file_or_suggest() != "file" or item is None or item.get("status") in FINISHED:
        return None
    p = book.by_filer_id(str(item.get("project") or "")) if item.get("project") else None
    c = book.by_legacy.get(item["key"])
    if p is None or (c is not None and (book.project_of(c) or p["id"] in (c["detail"].get("not_in") or []))):
        return None
    return p


def file_into(db: sqlite3.Connection, cid: str, p: dict[str, Any], reason: str) -> None:
    """Today files a task into a project of the user's by itself, at the bottom, saying why."""
    commitments.change(db, cid, "sync", {"parent_id": p["id"], "position": _end_position(db, p["id"])}, kind="filed",
                       reason=reason)


def _file(db: sqlite3.Connection, key: str, p: dict[str, Any]) -> None:
    c = commitments.by_legacy_key(db, key) or ensure_commitment(db, key)
    file_into(db, c["id"], p, f"Today's reader read it as part of {p['title']}; filed there by itself (the user, 2026-11-13). "
              "Move it out from the task's menu.")


def auto_file_pending(db: sqlite3.Connection, items: list[dict[str, Any]]) -> int:
    """Catch up: file every open item whose reading names one of the user's projects."""
    if projects.file_or_suggest() != "file":
        return 0
    book, n = Book(db), 0
    for item in items:
        p = _filing_target(book, item) if item.get("project") else None
        if p is not None:
            _file(db, item["key"], p)
            n += 1
    return n
