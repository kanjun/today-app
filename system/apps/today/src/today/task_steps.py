"""A task's steps: a short checklist under the task, Things-style (the user, 2026-11-14).

The task's title stays the task (the outcome, in the user's terms); steps never replace it. Every
step is a commitment under its task (label ``step``), in order (``position``). The first
step not done is the current one: it alone carries a short note and one way to do it,
matched to its kind:

- ``link``: open the place the step happens (``url``);
- ``draft``: write the message(s) for the user to read (nothing sends from here);
- ``record``: a small table (or notes) the user fills in, saved as the user types; a table may
  compute a percentage column, and may be filled in from the task's own messages;
- ``plain``: nothing but the checkbox.

Ticking the box is how the user says a step is done. Every change is an event on the step.

Steps the AI writes follow ``clean_steps``: at most six, each a short physical action
(about eight words, verb first), details in the note, never the task's own title.
"""

import json
import re
import sqlite3
from typing import Any

from today import commitments, links
from today.project_book import Batch, Book, ProjectError, is_step

KINDS = ("link", "draft", "record", "plain")
MAX_STEPS = 6
HARD_MAX_WORDS = 12  # longer than this even after moving details to the note: not a step
MODEL = "sonnet"

STEP_RULES = (
    "Steps: at most 6, in order. Each step is ONE physical action, verb first, at most about 8 words, "
    "with no parentheses or explanations in it; a detail the user needs goes in the step's short \"note\". "
    "Never repeat the task's title as a step. A step done at a web page names its kind \"link\" and gives the "
    "\"url\"; every other step is \"plain\". (The card offers ways to do each step itself.)"
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def clean_step(raw: Any) -> dict[str, Any] | None:
    """One step from the AI, held to the rules; None when it can't be made one."""
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        return None
    text = re.sub(r"\s+", " ", str(raw.get("text") or raw.get("title") or "")).strip()
    note = str(raw.get("note") or "").strip()
    if not (raw.get("url") or raw.get("link") or raw.get("href")):
        # A link written into the step's words or note moves to the step's link.
        found = links.find(f"{text} {note}")
        text, note = (links.readable(x, urls=False) for x in (text, note))
        if found:
            raw = dict(raw, url=found[0])
            text, note = links.URL.sub("", text).strip(), links.URL.sub("", note).strip()
    # Details move out of the step into its note: parentheses, then anything after a dash,
    # colon or semicolon.
    extra = re.findall(r"\(([^)]*)\)", text)
    text = re.sub(r"\s*\([^)]*\)", "", text).strip()
    parts = re.split(r"\s+[-–—]\s+|:\s+|;\s+", text, maxsplit=1)
    if len(parts) > 1 and len(parts[0].split()) >= 2:
        text, rest = parts[0].strip(), parts[1].strip()
        extra.append(rest)
    text = text.rstrip(".,;:")
    if extra:
        note = " ".join(x for x in [note, *[e.strip().rstrip(".") + "." for e in extra if e.strip()]] if x)
    words = text.split()
    if not words or len(words) > HARD_MAX_WORDS:
        return None
    kind = str(raw.get("kind") or "plain")
    kind = kind if kind in KINDS else "plain"
    out: dict[str, Any] = {"text": text[0].upper() + text[1:], "kind": kind, "note": note[:240]}
    url = links.clean(raw.get("url") or raw.get("link") or raw.get("href") or "")
    if url:
        out["kind"], out["url"] = "link", url  # a step with a link is a link step, whatever it was called
    elif kind == "link":
        out["kind"] = "plain"  # a link with nowhere to go is a plain step
    for k in ("label", "fill_label"):
        if raw.get(k):
            out[k] = str(raw[k])[:40]
    table = raw.get("table")
    if kind == "record" and isinstance(table, dict) and isinstance(table.get("columns"), list):
        cols = [str(c)[:30] for c in table["columns"]][:5]
        rows = [[str(x)[:60] for x in (r if isinstance(r, list) else [r])][: len(cols)] for r in table.get("rows") or []][:20]
        pct = table.get("percent") if isinstance(table.get("percent"), dict) else None
        out["table"] = {"columns": cols, "rows": rows}
        tbl: dict[str, Any] = out["table"]
        if pct and isinstance(pct.get("of"), int) and isinstance(pct.get("rate"), (int, float)):
            tbl["percent"] = {"of": pct["of"], "rate": float(pct["rate"])}
    return out


def steps_from_op(op: dict[str, Any], title: str) -> list[dict[str, Any]]:
    """The steps of a "steps" operation, with every link it carries kept: links given per step
    by number in a separate list ("details") are put on their step, and any link left over goes
    on the first step without one, so a link the assistant found is never dropped."""
    given, details = op.get("steps"), op.get("details")
    raws = [r if isinstance(r, dict) else {"text": r} for r in (given if isinstance(given, list) else [])]
    for d in details if isinstance(details, list) else []:
        if isinstance(d, dict) and isinstance(d.get("step"), int) and 1 <= d["step"] <= len(raws):
            n = d["step"] - 1
            raws[n] = {**{k: v for k, v in d.items() if k != "step" and v}, **{k: v for k, v in raws[n].items() if v}}
    steps = clean_steps(raws, title)
    placed = {s.get("url") for s in steps}
    for url in links.find(json.dumps(op, ensure_ascii=False)):
        spare = next((s for s in steps if not s.get("url")), None)
        if url and url not in placed and spare is not None:
            spare.update(kind="link", url=url)
            placed.add(url)
    return steps


def clean_steps(raws: Any, title: str) -> list[dict[str, Any]]:
    """The AI's steps for a task, held to the rules: never the title, no repeats, at most six."""
    seen = {_norm(title)}
    out = []
    for raw in raws if isinstance(raws, list) else []:
        s = clean_step(raw)
        if s is None or _norm(s["text"]) in seen:
            continue
        seen.add(_norm(s["text"]))
        out.append(s)
    return out[:MAX_STEPS]


def step_detail(s: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in s.items() if k != "text"}


# ---- writes --------------------------------------------------------------------------------


def _step(db: sqlite3.Connection, cid: str) -> dict[str, Any]:
    s = commitments.get(db, cid)
    if s is None or not is_step(s):
        raise ProjectError("That step isn't there any more.")
    return s


def _task(db: sqlite3.Connection, cid: str) -> dict[str, Any]:
    t = commitments.get(db, cid)
    if t is None or is_step(t):
        raise ProjectError("That task isn't there any more.")
    return t


def _renumber(b: Batch, task_id: str, order: list[str]) -> None:
    for n, sid in enumerate(order):
        cur = commitments.get(b.db, sid)
        if cur is not None and cur["position"] != n:
            b.change(sid, {"position": n}, reason="the step's place in the list", mark=False)


def _sync_next(b: Batch, task_id: str) -> None:
    """The task's next step is its first step not done (or none)."""
    steps = [s for s in Book(b.db).steps(task_id) if s["stage"] == "open"]
    nxt = steps[0]["title"] if steps else ""
    task = commitments.get(b.db, task_id)
    if task is not None and task["next_step"] != nxt:
        b.change(task_id, {"next_step": nxt}, reason="its first step not done", mark=False)


def sync_next_step(db: sqlite3.Connection, task_id: str, actor: str = "import") -> None:
    """The task's next step is its first step not done, outside a batch (an import)."""
    steps = [s for s in Book(db).steps(task_id) if s["stage"] == "open"]
    nxt = steps[0]["title"] if steps else ""
    task = commitments.get(db, task_id)
    if task is not None and task["next_step"] != nxt:
        commitments.change(db, task_id, actor, {"next_step": nxt}, reason="its first step not done")


def set_steps(b: Batch, task: dict[str, Any], steps: list[dict[str, Any]], why: str) -> None:
    """Replace a task's open steps with these (already cleaned); done ones stay done."""
    book = Book(b.db)
    for s in book.steps(task["id"]):
        if s["stage"] == "open":
            b.change(s["id"], {"stage": "stale", "stage_reason": "replaced by new steps"}, reason=why, mark=False)
    start = len([s for s in book.steps(task["id"]) if s["stage"] == "done"])
    for n, s in enumerate(steps):
        b.create({"title": s["text"], "direction": task["direction"], "origin": "user_chose", "started_by": "user",
                  "parent_id": task["id"], "labels": ["step"], "position": start + n, "detail": step_detail(s)},
                 reason=why)
    _sync_next(b, task["id"])


def tick(db: sqlite3.Connection, cid: str, done: bool) -> dict[str, Any]:
    """The user ticked a step (or unticked it). ``all_done`` tells the page to finish the task."""
    s = _step(db, cid)
    b = Batch(db, "user", ("did " if done else "not done: ") + s["title"])
    b.change(cid, {"stage": "done" if done else "open", "stage_reason": "the user ticked it" if done else ""},
             reason="the user ticked the step" if done else "the user unticked the step", mark=False)
    _sync_next(b, s["parent_id"])
    left = [x for x in Book(db).steps(s["parent_id"]) if x["stage"] == "open"]
    return {"batch": b.save(), "all_done": not left}


def edit(db: sqlite3.Connection, cid: str, title: str) -> dict[str, Any]:
    s = _step(db, cid)
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return remove(db, cid)
    b = Batch(db, "user", f"edited step {s['title']}")
    if title != s["title"]:
        b.change(cid, {"title": title}, reason="the user rewrote the step", mark=False)
        _sync_next(b, s["parent_id"])
    return {"batch": b.save()}


def add(db: sqlite3.Connection, task_cid: str, title: str, after: str = "") -> dict[str, Any]:
    """A step of the user's, after ``after`` (or at the end)."""
    task = _task(db, task_cid)
    title = re.sub(r"\s+", " ", title).strip()
    order = [s["id"] for s in Book(db).steps(task_cid)]
    b = Batch(db, "user", f"added step {title or '(new)'}")
    sid = b.create({"title": title or " ", "direction": task["direction"], "origin": "user_chose", "started_by": "user",
                    "parent_id": task_cid, "labels": ["step"], "position": len(order), "detail": {"kind": "plain"}},
                   reason="the user added a step")
    at = order.index(after) + 1 if after in order else len(order)
    _renumber(b, task_cid, order[:at] + [sid] + order[at:])
    _sync_next(b, task_cid)
    return {"batch": b.save(), "cid": sid}


def remove(db: sqlite3.Connection, cid: str) -> dict[str, Any]:
    s = _step(db, cid)
    b = Batch(db, "user", f"removed step {s['title']}")
    b.change(cid, {"stage": "stale", "stage_reason": "the user removed it"}, reason="the user removed the step", mark=False)
    _sync_next(b, s["parent_id"])
    return {"batch": b.save()}
