"""What Today's one assistant can do: every action it may take, written once.

The user, 2026-11-15: one assistant everywhere (a task's card, the bottom bar, a project's box),
with the same actions and rules wherever the user types; adding an action is adding an entry
here, not another assistant.

An action is a name, the line that tells the model what it does and how to write it, where it
may be used ("task": one task's card; "tasks": the bottom bar, with several or no tasks;
"project": a project's box), and the function that applies it. Every action that is about a
task names it by label ("task": "T2"); on one task's card the label can be left out.

``register`` adds an action; ``for_scope`` gives the ones a place offers, in order. Actions
that belong to one area can live in their own module and register themselves.

Nothing here sends anything: there is no action for it. Sending is the user's tap.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from email.utils import parseaddr
from typing import Any

from today import (
    attached,
    calendar_events,
    commitments,
    config,
    connectors,
    project_book,
    store,
    task_context,
    task_steps,
    weekly,
)
from today.project_book import Batch, Book, ProjectError

log = logging.getLogger(__name__)

TASK, TASKS, PROJECT = "task", "tasks", "project"
EVERYWHERE = frozenset({TASK, TASKS, PROJECT})


class Target:
    """A task an action can name: its label, its Today key (an item key, or a commitment id for
    one with no item), its commitment when it has one, and the version the assistant read (an
    edit the user made meanwhile wins)."""

    def __init__(self, label: str, key: str, cid: str = "", version: int | None = None) -> None:
        self.label, self.key, self.cid, self.version = label, key, cid, version


class Doing:
    """One answer being applied, as one undoable batch: the batch, where the user is, what the user said,
    the tasks it may name, the user's projects, and what it made (for the card and for undo)."""

    def __init__(self, b: Batch, scope: str, said: str, targets: dict[str, Target], projects: list[tuple[str, str]],
                 pid: str = "", focus: str = "", proposed: set[str] | None = None) -> None:
        self.b, self.scope, self.said, self.targets, self.projects = b, scope, said, targets, projects
        self.pid = pid  # the project, on a project's box
        self.focus = focus  # the step the user is on, on a step's card
        self.proposed = proposed or set()  # every step's words in this answer (never a title)
        self.made: dict[str, Any] = {"events": [], "picked": [], "draft": "", "gone": False}
        self.record: list[dict[str, Any]] = []  # what changed, in the user's terms (for "learned")
        self.was: dict[str, str] = {}  # how Today had each task filed before (for "learned")
        self.aliases: dict[str, str] = {}  # the ids the model is given for the user's projects -> their own

    @property
    def why(self) -> str:
        return f'the user said: "{self.said[:300]}"'

    def target(self, op: dict[str, Any]) -> Target:
        label = str(op.get("task") or "")
        if not label and self.scope == TASK:
            label = "T1"
        t = self.targets.get(label)
        if t is None:
            raise ProjectError(f"no task {label or '(none named)'}")
        return t

    def commitment(self, t: Target) -> dict[str, Any]:
        """The task's commitment, made now when it has none yet."""
        c = commitments.get(self.b.db, t.cid) if t.cid else None
        if c is None:
            c = project_book.ensure_commitment(self.b.db, t.key)
            t.cid = c["id"]
        return c

    def fresh(self, t: Target) -> dict[str, Any]:
        """The commitment, refusing when the user changed it after the assistant read it."""
        c = self.commitment(t)
        if t.version is not None and c["version"] != t.version and c["id"] not in self.b.own:
            raise commitments.StaleWrite(c["id"])
        return c

    def item(self, t: Target) -> dict[str, Any]:
        return store.load()["items"].get(t.key) or {}

    def note(self, t: Target, now: str) -> None:
        item = self.item(t)
        self.record.append({"key": t.key, "who": item.get("who") or "", "title": item.get("title") or "",
                            "words": str(item.get("ask_text") or "")[:300], "was": self.was.get(t.key, ""), "now": now})
        if t.key not in self.b.changed:
            self.b.changed.append(t.key)


class Action:
    """One thing the assistant can do: its name, the line telling the model what it does and
    how to write it, the function that applies it, where it may be used, and whether it writes
    onto the task's Today item (a draft, an email)."""

    def __init__(self, name: str, prompt: str, apply: Callable[[Doing, dict[str, Any]], None],
                 scopes: frozenset[str] = EVERYWHERE, needs_item: bool = False) -> None:
        self.name, self.prompt, self.apply, self.scopes, self.needs_item = name, prompt, apply, scopes, needs_item


ACTIONS: dict[str, Action] = {}


def register(action: Action) -> None:
    ACTIONS[action.name] = action


def for_scope(scope: str) -> list[Action]:
    """The actions a place offers the model (one with no line is Today's own, never offered)."""
    return [a for a in ACTIONS.values() if scope in a.scopes and a.prompt]


# ---- writing ---------------------------------------------------------------------------------


def _draft(d: Doing, op: dict[str, Any]) -> None:
    text = str(op.get("text") or "").strip()
    if not text:
        return
    t = d.target(op)
    item = d.item(t)
    c = commitments.get(d.b.db, t.cid) if t.cid else None
    # Waiting on them: the message is a nudge for when they stay quiet, not a reply now.
    waiting = item.get("state") == "waiting" or (c is not None and c["direction"] == "theirs")
    field_ = "nudge" if waiting else "draft"
    fields: dict[str, Any] = {field_: text}
    if field_ == "draft" and item.get("state") in ("you", "mine"):
        fields["state"] = "ready"
    if item.get("source") == "goal":
        # A weekly priority's message goes to the person it names, by Slack DM or email.
        people = [p.get("name") for p in (item.get("move") or {}).get("people") or []]
        to = str(op.get("to") or (people[0] if len(people) == 1 else "")).strip()
        if to:
            fields.update(send_to=weekly.resolve_recipient(item, to), answers=[to])
    d.b.item(t.key, fields)
    d.made["draft"] = text
    d.b.count("drafted")


def _person(written: str) -> dict[str, str]:
    """"Full Name <address>" as written, or the name with its address looked up in the user's mail
    (blank when unsure; the user fills it in on the card)."""
    name, address = parseaddr(written)
    name = name or (written if "@" not in written else "")
    if not address or "@" not in address:
        address = connectors.find_address(name or written, config.accounts()) if (name or written) else ""
    return {"name": name or address, "email": address}


def _email(d: Doing, op: dict[str, Any]) -> None:
    t = d.target(op)
    to = [_person(str(x)) for x in (op.get("to") or [])][:6]
    if not to or not str(op.get("body") or "").strip():
        return
    emails = list(d.item(t).get("emails") or [])
    emails.append({"to": to, "cc": [_person(str(x)) for x in (op.get("cc") or [])][:6],
                   "subject": str(op.get("subject") or "")[:200], "body": str(op["body"]), "sent": False})
    d.b.item(t.key, {"emails": emails})
    d.b.count("drafted")


def _calendar(d: Doing, op: dict[str, Any]) -> None:
    t = d.targets.get(str(op.get("task") or "T1"))
    item = d.item(t) if t else {}
    event = {"title": str(op.get("title") or item.get("title") or "")[:120], "start": str(op.get("start") or ""),
             "end": str(op.get("end") or ""), "all_day": False, "notes": f"From Today: {item.get('title', '')}"}
    datetime.fromisoformat(event["start"])
    datetime.fromisoformat(event["end"])
    ev = calendar_events.add(event, source=item.get("url") or "")
    d.made["events"].append({"id": ev.get("id", ""), "title": event["title"], "start": event["start"],
                             "link": ev.get("htmlLink", "")})


def _offer_event(d: Doing, op: dict[str, Any]) -> None:
    """An event offered on the card, which the user adds with a tap: what the background writer does
    with a "calendar" answer, since nothing goes on the user's calendar without the user."""
    t = d.target(op)
    item = d.item(t)
    event = {"title": str(op.get("title") or item.get("title") or "")[:120], "start": str(op.get("start") or ""),
             "end": str(op.get("end") or ""), "all_day": False, "location": "", "notes": f"From Today: {item.get('title', '')}"}
    datetime.fromisoformat(event["start"])
    datetime.fromisoformat(event["end"])
    offered = list(item.get("calendar") or [])
    if any((e.get("title"), e.get("start")) == (event["title"], event["start"]) for e in offered):
        return
    d.b.item(t.key, {"calendar": [*offered, event]})
    d.b.count("drafted")


# ---- shaping tasks ---------------------------------------------------------------------------


def _steps(d: Doing, op: dict[str, Any]) -> None:
    t = d.target(op)
    c = d.fresh(t)
    steps = task_steps.steps_from_op(op, c["title"])
    if steps:
        task_steps.set_steps(d.b, c, steps, d.why)
        d.b.count("steps")


def _is_a_step(title: str, c: dict[str, Any], d: Doing, proposed: set[str]) -> bool:
    """A step's words never become the task's title."""
    norm = task_steps._norm(title)
    return norm in proposed or any(task_steps._norm(s["title"]) == norm for s in Book(d.b.db).steps(c["id"]))


def _title(d: Doing, op: dict[str, Any]) -> None:
    title = str(op.get("title") or "").strip()[:120]
    t = d.target(op)
    c = d.commitment(t)
    if not title or title == c["title"] or _is_a_step(title, c, d, d.proposed):
        return
    d.b.change(c["id"], {"title": title}, reason=d.why, expect=t.version if c["id"] not in d.b.own else None)
    d.b.item(t.key, {"title": title, "title_by_hand": True})
    d.b.count("retitled")
    d.note(t, f'titled "{title}"')


def _add(d: Doing, op: dict[str, Any]) -> None:
    title = str(op.get("title") or "").strip()[:120]
    if not title or not d.pid:
        return
    cid = project_book.add_task(d.b, d.pid, title, d.why)
    steps = task_steps.steps_from_op(op, title)
    if steps:
        c = commitments.get(d.b.db, cid)
        assert c is not None
        task_steps.set_steps(d.b, c, steps, d.why)
    d.b.count("added")


def _bring_in(d: Doing, op: dict[str, Any]) -> None:
    t = d.targets.get(str(op.get("item") or ""))
    if t is None or not d.pid:
        raise ProjectError(f"no item {op.get('item')}")
    project_book.move_task(d.b, d.commitment(t), d.pid, d.why)
    d.b.count("brought in")


def _project(d: Doing, op: dict[str, Any]) -> None:
    to = str(op.get("project") or "")
    to = d.aliases.get(to, to)  # projects are named to the model by short ids
    if to and to not in {pid for pid, _ in d.projects}:
        raise ProjectError("no such project")
    t = d.target(op)
    project_book.move_task(d.b, d.commitment(t), to, d.why)
    d.b.count("moved")
    d.note(t, "in " + next((title for pid, title in d.projects if pid == to), "") if to else "in no project")


def _hold_then(d: Doing, moving: str) -> None:
    """Before the first move into Next, the page's Then is just "after the first three";
    mark those tasks Then so they stay there while the moved one comes up."""
    book, items = Book(d.b.db), store.load()["items"]
    opened = [t for t in sorted(book.tasks(d.pid), key=project_book._order)
              if project_book.standing(t, items.get(t["legacy_key"] or "")) == "open"]
    project = book.by_id.get(d.pid) or {"detail": {}}
    _, then, sectioned = project_book.split_next(opened, project_book.is_then, bool(project["detail"].get("sectioned")))
    if sectioned:
        return
    for t in then:
        if t["id"] != moving:
            d.b.change(t["id"], {"detail": dict(t["detail"], then=True)}, reason=d.why, mark=False)


def _section(d: Doing, op: dict[str, Any]) -> None:
    to = str(op.get("to") or "")
    if to not in ("next", "then"):
        raise ProjectError(f"no section {to}")
    t = d.target(op)
    c = d.commitment(t)
    if not d.pid:
        p = Book(d.b.db).project_of(c)
        d.pid = p["id"] if p else ""
    if not d.pid:
        raise ProjectError("not in a project")
    if to == "next":
        _hold_then(d, c["id"])
        c = d.commitment(t)
    if project_book.is_then(c) != (to == "then"):
        d.b.change(c["id"], {"detail": dict(c["detail"], then=to == "then")}, reason=d.why)
        d.b.count("to Then" if to == "then" else "to Next")


# ---- sorting -------------------------------------------------------------------------------


def _finish(d: Doing, op: dict[str, Any], stage: str) -> None:
    t = d.target(op)
    if t.cid or commitments.by_legacy_key(d.b.db, t.key):
        c = d.fresh(t)
        if stage == "done":
            evidence = str(op.get("evidence") or "").strip()[:400]
            project_book.finish(d.b, c, "done", f"done: {evidence}" if evidence else "done, the user said", d.why)
        else:
            project_book.finish(d.b, c, "declined", str(op.get("why") or "the user dropped it")[:200], d.why)
    else:
        now = store.now_iso()
        d.b.item(t.key, {"status": "done" if stage == "done" else "skipped", "finished_at": now, "cleared_at": now,
                         "cleared_reason": ("done, the user said" if stage == "done" else "the user dropped it") + "; nothing sent"})
    d.b.count("marked done" if stage == "done" else "dropped")
    d.note(t, "done" if stage == "done" else "dropped")
    d.made["gone"] = True


def _later(d: Doing, op: dict[str, Any]) -> None:
    t = d.target(op)
    d.b.item(t.key, {"not_now": store.now_iso()})
    c = commitments.get(d.b.db, t.cid) if t.cid else commitments.by_legacy_key(d.b.db, t.key)
    if c:
        d.b.change(c["id"], {"stage": "parked", "stage_reason": "Not now"}, reason=d.why)
    d.b.count("to Later")
    d.note(t, "Later")
    d.made["gone"] = True


def _refile(d: Doing, op: dict[str, Any]) -> None:
    t = d.target(op)
    as_ = str(op.get("as") or "")
    c = commitments.get(d.b.db, t.cid) if t.cid else commitments.by_legacy_key(d.b.db, t.key)
    now = store.now_iso()
    if as_ == "pitch":
        d.b.item(t.key, {"filed": "pitch"})
        if c:
            d.b.change(c["id"], {"labels": sorted({*c["labels"], "pitch"})}, reason=d.why)
    elif as_ in ("remark", "not_yours"):
        d.b.item(t.key, {"filed": as_, "status": "skipped", "cleared_at": now,
                         "cleared_reason": ("said in passing" if as_ == "remark" else "not the user's to do") + ", the user said; nothing sent"})
        if c:
            d.b.change(c["id"], {"stage": "declined" if as_ == "not_yours" else "stale",
                                 "stage_reason": "not the user's to do" if as_ == "not_yours" else "said in passing"}, reason=d.why)
        d.made["gone"] = True
    elif as_ == "mine":
        d.b.item(t.key, {"filed": "", "filed_by_hand": True, "status": "open", "not_now": None})
        if c:
            d.b.change(c["id"], {"labels": [x for x in c["labels"] if x != "pitch"], "stage": "open"}, reason=d.why)
    else:
        return
    d.b.count("refiled")
    d.note(t, {"pitch": "a pitch", "remark": "said in passing", "not_yours": "someone else's", "mine": "mine"}[as_])


def _waiting(d: Doing, op: dict[str, Any]) -> None:
    t = d.target(op)
    c = d.commitment(t)
    who = str(op.get("who") or "").strip()[:40]
    d.b.change(c["id"], {"direction": "theirs", "counterparty": who or c["counterparty"]}, reason=d.why,
               expect=t.version if c["id"] not in d.b.own else None)
    d.b.item(t.key, {"state": "waiting"})
    d.b.count("waiting")


# ---- conversations and notes on a task -------------------------------------------------------


def _attach(d: Doing, op: dict[str, Any]) -> None:
    attached.attach(d.b, d.commitment(d.target(op)), str(op["conversation"]), str(op.get("title") or ""), d.why)


def _detach(d: Doing, op: dict[str, Any]) -> None:
    attached.detach(d.b, d.commitment(d.target(op)), str(op["conversation"]), d.why)


def _note(d: Doing, op: dict[str, Any]) -> None:
    c = d.commitment(d.target(op))
    old = str(op.get("replaces_id") or "")
    if old and task_context.hide_note(d.b.db, old, "assistant"):
        d.b.ops.append({"t": "sql", "sql": task_context.SHOW_SQL, "args": [old]})
    nid = task_context.add_note(d.b.db, c["id"], str(op.get("text") or ""), step_id=str(op.get("step_id") or ""),
                                said=d.said, focus=str(op.get("focus") or ""), replaces=old)
    if nid:
        d.b.ops.append({"t": "sql", "sql": task_context.HIDE_SQL, "args": [store.now_iso(), "undone", nid]})
        d.b.count("noted")


def _unnote(d: Doing, op: dict[str, Any]) -> None:
    if task_context.hide_note(d.b.db, str(op["note_id"]), "assistant"):
        d.b.ops.append({"t": "sql", "sql": task_context.SHOW_SQL, "args": [str(op["note_id"])]})


ONE_TASK = frozenset({TASK})
ON_TASKS = frozenset({TASK, PROJECT})

for _a in (
    Action("draft", '{"op": "draft", "task": "T1", "text": "...", "to": ""}: write or rewrite the reply in the task\'s own conversation, in the user\'s voice. On a move toward one of the user\'s weekly priorities that asks someone something, "to" is the first name of the person it goes to (one of "people_to_ask"). When the user asks for a change to an existing draft, change only what the user asked about and keep the rest word for word.', _draft, ON_TASKS, needs_item=True),
    Action("email", '{"op": "email", "task": "T1", "to": ["Full Name <address if known>"], "subject": "...", "body": "..."}: a new email outside the task\'s conversation, in the user\'s voice.', _email, ON_TASKS, needs_item=True),
    Action("calendar", '{"op": "calendar", "task": "T1", "title": "...", "start": "YYYY-MM-DDTHH:MM", "end": "YYYY-MM-DDTHH:MM"}: put a block on the user\'s own calendar (home time zone, no guests). Only at a time that is free on the calendar given, and only when the user asked for it or tapped an option that says so.', _calendar, ON_TASKS),
    # Today's own (no line, so never offered to the model): the background writer's events.
    Action("offer_event", "", _offer_event, frozenset({TASK}), needs_item=True),
    Action("steps", '{"op": "steps", "task": "T1", "steps": [{"text": "...", "kind": "plain" | "link", "url": "", "note": ""}, ...]}: replace the task\'s open steps with this checklist. {STEP_RULES} A link the user will need for a step (a doc, a form, a booking page) goes in that step\'s "url", exactly as found.', _steps, ON_TASKS),
    Action("title", '{"op": "title", "task": "T1", "title": "..."}: a better title for the task: short, the outcome or action in the user\'s terms, verb first, naming the person, at most 60 characters. Never a step\'s words.', _title),
    Action("add", '{"op": "add", "title": "...", "steps": [...]}: a new task of the user\'s in this project (steps optional, same shape as "steps").', _add, frozenset({PROJECT})),
    Action("bring_in", '{"op": "bring_in", "item": "I7"}: an open thing on the user\'s Today list belongs in this project.', _bring_in, frozenset({PROJECT})),
    Action("project", '{"op": "project", "task": "T1", "project": "<project id or empty>"}: move the task into one of the user\'s projects (ids below), or out of every project.', _project),
    Action("section", '{"op": "section", "task": "T1", "to": "next" | "then"}: move the task between Next (what the user does now) and Then (after that) in its project. When the user says which kinds of tasks belong in Next or Then, give one of these for every task whose section should change.', _section, ON_TASKS),
    Action("done", '{"op": "done", "task": "T1", "evidence": "..."}: mark the task done; quote or name what shows it. Only when the user says it is done or taps "Mark it done". When you see it is already done but the user did not say so, do not use this: say what shows it in "did" (who, what, when) and make the first "next" {"label": "Mark it done", "say": "Mark it done"}.', lambda d, op: _finish(d, op, "done")),
    Action("drop", '{"op": "drop", "task": "T1", "why": "..."}: the user no longer wants to do it. Nothing is sent.', lambda d, op: _finish(d, op, "declined")),
    Action("later", '{"op": "later", "task": "T1"}: not now; it sinks into Later.', _later),
    Action("refile", '{"op": "refile", "task": "T1", "as": "pitch" | "remark" | "not_yours" | "mine"}: how Today misread it. "pitch": a cold sales or promo ask the user will say no to; "remark": something said in passing that nobody is waiting on; "not_yours": someone else is handling it and the user did not start it, so it comes off the user\'s list; "mine": it really is the user\'s to answer or do.', _refile),
    Action("waiting", '{"op": "waiting", "task": "T1", "who": "first name"}: the user started this and the next step is someone else\'s; it goes in Waiting on and the user checks on them until it is complete.', _waiting, ON_TASKS),
    Action("attach", '{"op": "attach", "thread": "X1"}: attach that conversation to this task, so Today reads it with the task from now on. Attach only what the user named or clearly described (the user may ask for several). When more than one match fits and the user didn\'t say which, attach none: ask in "did" and offer each as a "next" button.', _attach, ONE_TASK),
    Action("detach", '{"op": "detach", "thread": "A1"}: take off a conversation attached before (one of "attached_threads").', _detach, ONE_TASK),
    Action("note", '{"op": "note", "text": "...", "step": "", "replaces": ""}: keep a note on this task, so you and every other part of Today know it from now on (see Notes below). "step" is the step it is about ("S2"), or "" for the task as a whole. "replaces" is a note it updates ("N1"), or "".', _note, ONE_TASK),
    Action("unnote", '{"op": "unnote", "note": "N1"}: take off a note that is no longer true (what the user said shows it changed).', _unnote, ONE_TASK),
):
    register(_a)
