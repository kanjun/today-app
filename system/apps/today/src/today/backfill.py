"""One pass over the open items: which project each is in, and when it is due.

New items get both when they are read; this catches up the ones read before projects
and due dates existed. Run: uv run python -m today.backfill
"""

import json
import logging
import sys
from datetime import datetime
from typing import Any

from today import ai, projects, store, work

log = logging.getLogger(__name__)
BATCH = 15


def system() -> str:
    return work._fill(
        "You sort <WHO>'s open tasks. For each task you get its key, who it involves, "
        "what was asked, and the end of the conversation. Answer with one JSON object: "
        '{"<key>": {"project": "<project id or empty>", "due": "YYYY-MM-DD or empty"}, ...}.\n'
        "Projects (use one only when the task is really about it, not when it merely mentions it; "
        "a lunch, catch-up or unrelated question is empty):\n"
        + projects.prompt_lines()
        + '\n"due": the date by which <POSS> part must happen when the conversation sets one '
        "(a deadline, 'by Friday', an event <NAME> must answer or prepare before). Work relative "
        "dates out from the date the message was sent. Empty when no date is given; never guess."
    )


def _excerpt(key: str) -> str:
    raw = store.load_raw(key) or {}
    msgs = raw.get("messages") or []
    tail = [
        f"[{m.get('date') or m.get('ts') or ''}] {m.get('from') or m.get('user') or ''}: "
        f"{(m.get('text') or '')[:500]}"
        for m in msgs[-2:]
    ]
    return "\n".join(tail)


def _ask(batch: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = [
        {
            "key": i["key"],
            "title": i.get("title", ""),
            "who": i.get("who", ""),
            "asked": (i.get("ask_text") or "")[:600],
            "asked_at": i.get("asked_at", ""),
            "conversation_end": _excerpt(i["key"]),
        }
        for i in batch
    ]
    prompt = f"Today is {datetime.now().strftime('%A, %B %d, %Y')}.\n" + json.dumps(
        tasks, ensure_ascii=False
    )
    return ai.complete_json(prompt, system=system())


def run() -> int:
    state = store.load()
    valid = projects.ids(state)
    todo = [
        i
        for i in state["items"].values()
        if i["status"] in ("open", "working")
        and i.get("kind") != "onboard"
        and "project" not in i
    ]
    changed = 0
    for n in range(0, len(todo), BATCH):
        batch = todo[n : n + BATCH]
        try:
            answer = _ask(batch)
        except ai.ClaudeError as e:
            log.warning("backfill batch failed: %s", e)
            continue
        updates = {}
        for i in batch:
            got = answer.get(i["key"]) or {}
            pid = str(got.get("project") or "")
            updates[i["key"]] = {
                "project": pid if pid in valid else "",
                "due": i.get("due") or work._clean_due(got.get("due")),
            }

        def change(s: dict[str, Any], updates: dict[str, Any] = updates) -> None:
            for k, u in updates.items():
                if k in s["items"] and "project" not in s["items"][k]:
                    s["items"][k].update(u)

        store.mutate(change)
        changed += len(updates)
    return changed


DUE_SYSTEM = (
    "You find deadlines in <WHO>'s open tasks. For each task you get its key, what was "
    "asked or promised, and the conversation with timestamps. Answer with one JSON object: "
    '{"<key>": {"due": "YYYY-MM-DD or empty", "why": "a few words"}, ...}.\n'
    "The due date is the earliest of: (1) when <NAME> said they would do it or get back to them "
    '("I\'ll take a look in the morning", "tomorrow", "tonight", "by Friday"; "this week" '
    'means that Friday; "after the weekend" means that Monday), counted from when it was said; '
    "(2) a deadline someone else set; (3) the date of a talk, event, call or trip the task prepares "
    "for; something <NAME> will do after an event (\"a voice note after Monday's dinner\") is due on the event's date, never before. It may already be past. Empty when the conversation gives no date; never guess."
)


def run_due() -> int:
    """Read each open, undated task's whole conversation for promised dates and deadlines."""
    state = store.load()
    todo = [
        i
        for i in state["items"].values()
        if i["status"] in ("open", "working") and not i.get("due")
    ]
    found = 0
    for n in range(0, len(todo), 6):
        batch = todo[n : n + 6]
        tasks = [
            {
                "key": i["key"],
                "task": i.get("title", ""),
                "asked_or_promised": (i.get("ask_text") or "")[:400],
                "conversation": work.transcript(store.load_raw(i["key"]) or {})[-6000:],
            }
            for i in batch
        ]
        prompt = f"Today is {datetime.now().strftime('%A, %B %d, %Y')}.\n" + json.dumps(
            tasks, ensure_ascii=False
        )
        try:
            answer = ai.complete_json(prompt, system=work._fill(DUE_SYSTEM))
        except ai.ClaudeError as e:
            log.warning("due batch failed: %s", e)
            continue
        dates = {
            i["key"]: work._clean_due((answer.get(i["key"]) or {}).get("due"))
            for i in batch
        }
        dates = {k: d for k, d in dates.items() if d}

        def change(s: dict[str, Any], dates: dict[str, str] = dates) -> None:
            for k, d in dates.items():
                if k in s["items"] and not s["items"][k].get("due"):
                    s["items"][k]["due"] = d

        store.mutate(change)
        found += len(dates)
    return found


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if "--due" in sys.argv:
        log.info("found dates for %d open items", run_due())
    else:
        log.info("sorted %d open items", run())
