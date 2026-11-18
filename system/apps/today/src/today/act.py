"""Today's one assistant: a task's card (its options, a step's options, the bottom bar with one
task attached), the bottom bar with several tasks or none, and a project's box all come here.

The user, 2026-11-14: one box that can do all of it, with the task's context handed over; every
action offers what to do next; going back never regenerates. The user, 2026-11-15: one assistant
everywhere, with the same actions and the same rules wherever the user types.

What the user is looking at is the scope:
- ``{"kind": "task", "key": ..., "focus": ...}``: one task (``focus`` is a step the user is on);
- ``{"kind": "tasks", "keys": [...]}``: the bottom bar, about the tasks the user attached (or none);
- ``{"kind": "project", "pid": ..., "task": ...}``: a project's box (``task`` is one the user points at).

One call takes what the user is looking at (tasks labelled T1, T2, ...), the conversations, the user
calendar and voice, and what the user said or tapped; it answers with operations from the one list
of actions (``assistant_actions``) and the next two or three
options. Today applies the operations itself, as one undoable batch. There is no operation that
sends: Send stays the user's tap.

What each place has been through is its record:
- ``opening``: the options offered before the user did anything (a task's card only);
- ``turns``: what the user picked or said, in order, each with what was done (its operations, its
  undo batch, any calendar events) and the options it offered next;
- ``shelf``: turns the user went back from, by what was said, so picking the same option again
  re-applies what was already made instead of asking the model again.
A task's record is kept on its item under ``act`` (per focus); a project's box and the bottom
bar keep theirs in the database (``assistant_turns``), one row per card or place.
"""

import json
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from today import (
    ai,
    assistant_actions,
    attached,
    calendar_events,
    commitments,
    conversations,
    learned,
    ledger,
    link_pages,
    page,
    planning,
    project_book,
    store,
    task_context,
    task_steps,
    user_projects,
    user_rules,
    work,
)
from today.assistant_actions import PROJECT, TASK, TASKS, Doing, Target
from today.project_book import Batch, Book, ProjectError

log = logging.getLogger(__name__)

# Every action the assistant can take.
ACTIONS = assistant_actions.ACTIONS

MODEL = "sonnet"
TIMEOUT = 200
TRANSCRIPT_CHARS = 16000
CALENDAR_PAST_DAYS = 3  # the calendar shown reaches this far back (a month of it was half of what it read)
SAVED_CHARS = 12000  # what a search of the user's saved conversations brings back, at most
WORDS_CHARS = 900
LIST_CONVERSATION_CHARS = 4000  # each task attached in the bottom bar
OVERVIEW_CHARS = 1200  # each of a project's first tasks, when the user points at none
ELSEWHERE_ITEMS = 160  # other open things shown on a project's box, to bring in
LIST_ITEMS = 200  # the rest of the user's list shown in the bottom bar

SCOPE_TEXT = {
    TASK: "The user is looking at ONE task, labelled T1 (and sometimes one step of it). You get the task as it stands now "
          "(with every step, its notes, and what was said and done on the task's card and each step's card), the "
          "conversation it came from, the user's calendar, the user's voice, and what the user just said or tapped.",
    TASKS: "The user typed in the bar at the bottom of the user's list. The tasks the user attached to what the user said are labelled T1, "
           "T2, ... (there may be none), each with how Today has it filed, the person's own words and the conversation; "
           "the rest of the user's list follows, labelled L1, L2, ...: act on one of those only when the user clearly names it. "
           "What the user says is often a correction of how Today read something; Today remembers it for next time.",
    PROJECT: "The user is on one of the user's projects' pages and typed what is on the user's mind for it. You get the project, the user's own "
             "words on what it is for, the project agent's notes, its tasks (T1, T2, ..., each with its section Next or "
             "Then, its steps and the person's own words), who the user is waiting on, other open things on the user's Today list "
             "(I1, I2, ..., to bring in), and, when the user points at one task, that task's whole conversation. When the user "
             "points at a task and says nothing, give it a short title that is the outcome in the user's terms and a "
             "checklist from its messages and the project's notes. Prefer changing an existing task over adding a "
             "near-duplicate.",
}

SYSTEM = """You are the assistant in Today, the user's list of what the user owes people and what the user is doing. {ABOUT} {SCOPE}

Do what the user said, and only that, with the operations below. Then offer what the user would do next.

You have no tools. Your whole answer is exactly one JSON object and nothing else (to search or open a link, put a "search" or "open" operation in it):
{"did": "...", "operations": [...], "next": [{"label": "...", "say": "..."}, ...], "missing": ""}

Operations (an operation about a task names it by its label, like "task": "T2"{DEFAULT}):
{ACTIONS}
- {"op": "search", "queries": ["..."]}: when doing what the user said needs a fact or a link you don't have, up to two searches of a few key words (like "Nina notion"); each runs over the user's Gmail and over the words of every Slack conversation, text chat and email thread Today has saved, and you get the matches back and answer again.
- {"op": "open", "urls": ["..."]}: open up to three links (from the conversation, a search, or what the user said) to read what they show; Notion pages open through the user's Notion account, private ones too. You get the pages back and answer again.{FIND}
{NOTES}
"next": two or three things the user could do right after your operations are applied, the most likely first. Each is a button: "label" is at most 6 words, verb first, plain ("Tell Maya Thu 2pm works", "Put 30 min on my calendar"); "say" is the instruction you would be given if the user taps it, written as the user would say it. Rules for next:
- Each must move what the user is looking at forward from where it will stand after your operations, never repeat what you just did, and never be "think about it" or "review".
- Never offer reminders, follow-ups or check-ins the user did not ask for: the user wants fewer action items, not more.
- When you wrote a draft or email, sending it is the user's own Send button, so do not offer "Send"; offer an alternative approach instead, or [] if nothing fits.
- When the user is on one step, offer actions for that step.
- If what the user is looking at is finished, dropped, or moved off the user's list, "next" is [].

Rules:
- Never send, archive or post anything: there is no operation for it. Sending is the user's tap.
- Never invent facts, dates, amounts, links or people. If you would need one, ask for it: put the question in "did" and offer the likely answers as "next".
- Never guess a decision only the user can make (whether the user is going, yes or no, which option the user wants), not even from the user's notes on how to word things; the user's own answers on a task ("user_answers") are the user's decisions. When a reply depends on one, write no draft: ask in "did" and offer each choice as a "next" button whose "say" writes the reply that way (like "Say I can't make the 17th" and "Say yes, I'll be there").
- Write like the user's own messages in the samples, not from any idea of the user's style; follow the user's standing rules at the top of them.
- "did": one plain sentence to the user saying what you did, or what you need. Refer to people by name or as "they".
- When the user said nothing yet (the user just opened the card), operations is [], "did" is "", and "next" holds the best two or three first moves.
- If what the user said calls for no change, operations is [] and "did" says why.
- Buttons may offer anything that would be the right move, even something Today can't do yet. When the user asks for something that none of the operations can do (and no search or link would get it), never pretend and never say you tried: operations is [], "did" starts "I can't do that yet:" and says plainly what Today is missing (like "Today can't search your Slack DMs by date"), and "missing" names that ability in a few words. Else "missing" is "".

The user's projects:
{PROJECTS}"""

FIND_THREADS = """
- {"op": "find_threads", "queries": ["..."]}: when the user asks to attach a conversation (a WhatsApp, text or Signal chat or group, an email thread, a Slack conversation) that is not in "attached_threads", up to two searches by its name or the person's name, short words like "LIT" or "Maya"; you get the matches back, labelled X1, X2, ..., and answer again."""

NOTE_RULES = """Notes: the task's "notes" are what the user told you on this task's cards, kept short. Add one (in the same answer as everything else) whenever the user tells you something you and Today must keep knowing about this task: a fact ("Maya is leaving in March"), a decision ("not inviting Owen"), a preference about how it's done ("keep the reply short, no apology"), who is involved or where something is, or something you found for the user that the task depends on ("the contract was signed Nov 3, in the email from Dana"). Rules:
- One fact per note, at most about 20 words, in the user's own words where the user gave them, first person as the user said it ("Owen and I agreed to try Priya"); never add interpretation, advice or what you think the user should do.
- A plain request ("draft a reply", "make it shorter") is not a note; the preference behind it can be ("the user wants replies to Maya short").
- Never repeat a note already there; when something changed, "replaces" the old one instead of adding a contradicting one.
- Never write again anything in "notes_user_crossed_out".
- Notes are the user's decisions and facts: follow them, like "user_answers"."""

TOOLS = ("search", "open", "find_threads")


class ActError(ValueError):
    """Something the user asked can't be done; the message says why, in plain words."""


def system(kind: str = TASK) -> str:
    """The one set of instructions, for what the user is looking at: its actions come from the list."""
    rules = task_steps.STEP_RULES.split(" Each step names its kind")[0]
    actions = "\n".join("- " + a.prompt.replace("{STEP_RULES}", rules) for a in assistant_actions.for_scope(kind))
    return (SYSTEM.replace("{SCOPE}", SCOPE_TEXT[kind])
            .replace("{DEFAULT}", '; on this card it can be left out' if kind == TASK else "")
            .replace("{ACTIONS}", actions)
            .replace("{FIND}", FIND_THREADS if kind == TASK else "")
            .replace("{NOTES}", "\n" + NOTE_RULES + "\n" if kind == TASK else "")
            .replace("{PROJECTS}", user_projects.prompt_lines()))


# ---- what each place has been through --------------------------------------------------------

MISSING_SQL = ("CREATE TABLE IF NOT EXISTS missing_abilities (id INTEGER PRIMARY KEY, at TEXT NOT NULL, place TEXT NOT NULL, "
               "said TEXT NOT NULL, missing TEXT NOT NULL, did TEXT NOT NULL)")


def _place(scope: dict[str, Any]) -> str:
    """Where a record is kept: a task's card, a project's box, or the bottom bar."""
    kind = scope["kind"]
    if kind == TASK:
        return f"task:{scope['key']}|{scope.get('focus') or 'task'}"
    if kind == PROJECT:
        return f"project:{scope['pid']}"
    return "list"


def _empty() -> dict[str, Any]:
    return {"opening": None, "turns": [], "shelf": {}, "ahead": ""}


def _record(scope: dict[str, Any]) -> dict[str, Any]:
    with project_book.opened() as db:
        row = db.execute("SELECT record FROM assistant_turns WHERE place = ?", (_place(scope),)).fetchone()
    rec = json.loads(row[0]) if row else {}
    return {"opening": rec.get("opening"), "turns": list(rec.get("turns") or []), "shelf": dict(rec.get("shelf") or {}),
            "ahead": str(rec.get("ahead") or "")}


def _save(scope: dict[str, Any], rec: dict[str, Any]) -> None:
    with project_book.opened() as db:
        db.execute("INSERT OR REPLACE INTO assistant_turns (place, record, at) VALUES (?, ?, ?)",
                   (_place(scope), json.dumps(rec, ensure_ascii=False), store.now_iso()))


def _view(rec: dict[str, Any]) -> dict[str, Any]:
    turns = [{k: t.get(k) for k in ("label", "said", "did", "next", "draft")} for t in rec["turns"]]
    now = rec["turns"][-1]["next"] if rec["turns"] else rec["opening"]
    return {"turns": turns, "next": now, "ready": now is not None}


def view(records: dict[str, dict[str, Any]], focus: str = "") -> dict[str, Any]:
    """What a task's card shows for this focus: what the user picked so far and the options now.
    ``records``: the task's cards (``task_context.card_records``)."""
    rec = records.get(focus or "task") or {}
    return _view({"opening": rec.get("opening"), "turns": list(rec.get("turns") or []), "shelf": {}})


def place_view(scope: dict[str, Any]) -> dict[str, Any]:
    """What a project's box or the bottom bar shows: what the user said last and the options now."""
    return _view(_record(_scope(scope)))


# ---- what the user is looking at, as the model sees it ---------------------------------------------


def _commitment(db: Any, key: str) -> dict[str, Any] | None:
    return commitments.by_legacy_key(db, key)


def _words(item: dict[str, Any] | None) -> str:
    """The person's own words: what they asked."""
    if not item:
        return ""
    try:
        w = page.their_words(item)
    except (KeyError, OSError, ValueError):
        return ""
    return str(w["words"] or "")[:WORDS_CHARS]


def _conversation(key: str, chars: int) -> str:
    """A task's conversation as every assistant reads it (the same text a card's assistant and
    the reply writer get: threads apart, the ask marked, the user's unsent drafts marked), its newest
    ``chars`` characters."""
    raw = store.task_raw(key)
    if not raw.get("messages"):
        return ""
    text = work.transcript(raw, store.source_message(key))
    return text if len(text) <= chars else "[... earlier messages not shown]\n" + text[-chars:]


def _filing(item: dict[str, Any]) -> str:
    """How Today has an item filed, in plain words."""
    # "filed" is how the reader set it aside (a word), or, on the user's own notes, where a note
    # was filed ({"why": ...}), which is not a filing away.
    filed = item.get("filed") if isinstance(item.get("filed"), str) else ""
    if filed:
        return {"pitch": "a pitch", "remark": "said in passing", "not_yours": "someone else's"}.get(filed, filed)
    if item.get("not_now"):
        return "Later"
    return {"answer": "a quick answer", "reply": "a reply", "todo": "the user's to-do", "waiting": "waiting on them"}.get(
        page.kind_of(item), "on the user's list"
    )


def _state(key: str, item: dict[str, Any], focus: str) -> dict[str, Any]:
    out: dict[str, Any] = {"task": "T1", "title": item.get("title", ""), "list": item.get("state", ""), "who": item.get("who", "")}
    if item.get("source") not in ("slack", "email", "text"):
        out["no_conversation_to_reply_in"] = True
    if item.get("draft"):
        out["reply_drafted"] = item["draft"]
    if item.get("nudge"):
        out["nudge_drafted"] = item["nudge"]
    if item.get("emails"):
        out["emails_drafted"] = [{k: e.get(k) for k in ("to", "subject", "body", "sent")} for e in item["emails"]]
    if item.get("source") == "goal":
        out["people_to_ask"] = [p.get("name") for p in (item.get("move") or {}).get("people") or [] if p.get("name")]
    if item.get("answers") and item.get("source") != "goal":
        # What the user already answered on this task: the user's decisions, never to be asked again.
        out["user_answers"] = item["answers"]
    if item.get("questions") and not item.get("draft"):
        # What the reader still needs from the user before it can write; offer the answers as options.
        out["questions_for_user"] = [{"q": q.get("q"), "options": q.get("options") or []} for q in item["questions"]]
    with project_book.opened() as db:
        c = _commitment(db, key)
        if c is not None:
            p = Book(db).project_of(c)
            if p is not None:
                out["project"] = p["title"]
                out["section"] = "Then" if project_book.is_then(c) else "Next"
            # Every step, the notes, and what was done on each of the task's cards (the user, 2026-11-15).
            out.update(task_context.picture(db, c, item, focus=focus))
    if c is None:
        records = task_context.card_records_now(key)
        if records:
            out["card_history"] = task_context.history(records, {}, keep_focus=focus)
    return out


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _ending(said: str, history: list[dict[str, Any]], nothing: str) -> list[str]:
    parts = []
    if history:
        parts.append("What the user has done here so far, oldest first:\n" + "\n".join(
            f"- {t.get('label') or t.get('said')}: {t.get('did') or ''}" for t in history[-8:]))
    pages = link_pages.read_all(said) if said else []
    if pages:
        parts.append("The pages behind the links in what the user said, opened just now (put a link the user asks to share in "
                     "exactly; describe a page only from what it shows):\n" + json.dumps(pages, ensure_ascii=False)[:12000])
    parts.append(f"The user said: {said}" if said else nothing)
    parts.append("Answer with the JSON object.")
    return parts


def _task_prompt(key: str, item: dict[str, Any], focus: str, said: str,
                 nothing: str = "The user just opened the card and said nothing yet.") -> tuple[str, dict[str, Target]]:
    raw = store.task_raw(key)
    parts = [
        f"Today is {datetime.now().strftime('%A, %B %d, %Y, %-I:%M %p')}.",
        "The task as it stands:\n" + json.dumps(_state(key, item, focus), ensure_ascii=False, indent=1),
        f"What it came from: {item.get('ask_text', '')}\n(Who: {item.get('who', '')}. When: {item.get('asked_at', '')}.)",
        f"The conversation:\n{work.transcript(raw, store.source_message(key))[:TRANSCRIPT_CHARS]}" if raw.get("messages") else "",
    ]
    with project_book.opened() as db:
        c = _commitment(db, key)
        parts.append(project_book.instructions(db, c))  # its project's stages and the user's templates
    for n, a in enumerate(attached.of(c) if c else [], 1):
        lines = attached.recent_lines(a["conversation"])
        if lines:
            parts.append(f"Attached conversation A{n}, {a.get('title', '')}, newest last:\n" + "\n".join(lines)[:TRANSCRIPT_CHARS // 2])
    parts.append(f"{calendar_events.brief(past_days=CALENDAR_PAST_DAYS)}{work.voice(item)}{work.guidance(item)}")
    # A task's card shows its own history in the task (card_history and notes), not here.
    parts += _ending(said, [], nothing)
    return "\n\n".join(p for p in parts if p), {"T1": Target("T1", key, c["id"] if c else "")}


def _list_prompt(keys: list[str], said: str, history: list[dict[str, Any]]) -> tuple[str, dict[str, Target]]:
    items = store.load()["items"]
    keys = [k for k in keys if k in items]
    targets: dict[str, Target] = {}
    pointed = []
    for k in keys:
        i = items[k]
        label = f"T{len(targets) + 1}"
        targets[label] = Target(label, k)
        pointed.append({"task": label, "who": i.get("who") or "", "title": i.get("title") or "", "filed_as": _filing(i),
                        "their_words": _words(i), "conversation": _conversation(k, LIST_CONVERSATION_CHARS)})
    others = [i for k, i in items.items() if k not in keys and i.get("status") in ("open", "working") and i.get("state")]
    others.sort(key=lambda i: str(i.get("asked_at") or ""), reverse=True)
    rows = []
    for n, i in enumerate(others[:LIST_ITEMS], 1):
        targets[f"L{n}"] = Target(f"L{n}", i["key"])
        rows.append(f"L{n}: {i.get('who') or 'the user'} | {i.get('title') or ''} | {_filing(i)}")
    parts = [
        f"Today is {datetime.now().strftime('%A, %B %d, %Y, %-I:%M %p')}.",
        "The tasks the user attached:\n" + json.dumps(pointed, ensure_ascii=False, indent=1) if pointed else "The user attached no task.",
        "The rest of the user's list:\n" + "\n".join(rows),
    ]
    parts += _ending(said, history, "The user said nothing.")
    return "\n\n".join(p for p in parts if p), targets


def _project_prompt(pid: str, task_cid: str, said: str, history: list[dict[str, Any]]) -> tuple[str, dict[str, Target]]:
    items = store.load()["items"]
    targets: dict[str, Target] = {}
    with project_book.opened() as db:
        book = Book(db)
        p = book.by_id.get(pid)
        if p is None or not project_book.is_users(p):
            raise ActError("There's no such project.")
        lines = [f"Today is {datetime.now().strftime('%A, %B %d, %Y, %-I:%M %p')}.", f"Project: {p['title']}"]
        words = project_book.goal_words(p)
        if words:
            lines.append(f"The user's words on what it is for:\n{words}")
        playbook = user_projects.read(project_book.playbook_path(p))
        if playbook:
            lines.append(f"The project agent's notes (its own words, not the user's):\n{playbook[:6000]}")
        notes = (p["detail"].get("notes") or [])[-8:-1]
        if notes:
            lines.append("What the user said here before:\n" + "\n".join(f"- {n['at'][:10]}: {n['text']}" for n in notes))
        tasks = sorted(book.tasks(pid), key=project_book._order)
        opened = [t for t in tasks if project_book.standing(t, items.get(t["legacy_key"] or "")) == "open"]
        nxt, _, _ = project_book.split_next(opened, project_book.is_then, bool(p["detail"].get("sectioned")))
        in_next = {t["id"] for t in nxt}
        open_rows, waiting = [], []
        subject = None
        for t in tasks:
            item = items.get(t["legacy_key"] or "")
            where = project_book.standing(t, item)
            if where not in ("open", "waiting", "later"):
                continue
            label = f"T{len(targets) + 1}"
            targets[label] = Target(label, t["legacy_key"] or t["id"], t["id"], t["version"])
            if t["id"] == task_cid:
                subject = (label, t)
            steps = [s["title"] for s in book.steps(t["id"]) if s["stage"] == "open"]
            row: dict[str, Any] = {"task": label, "title": t["title"], "who": t["counterparty"] or (item or {}).get("who") or ""}
            if where == "open":
                row["section"] = "Next" if t["id"] in in_next else "Then"
            if where == "later":
                row["list"] = "Later"
            said_by_them = _words(item)
            if said_by_them and not (item or {}).get("manual"):
                row["their_words"] = said_by_them
            if steps:
                row["next_steps"] = steps
            if (item or {}).get("draft"):
                row["reply_drafted"] = item["draft"]
            (waiting if where == "waiting" else open_rows).append(row)
        inside = {t["legacy_key"] for t in tasks if t["legacy_key"]}
    lines.append("Its tasks:\n" + json.dumps(open_rows, ensure_ascii=False, indent=1))
    if waiting:
        lines.append("The user is waiting on:\n" + json.dumps(waiting, ensure_ascii=False, indent=1))
    elsewhere = [i for k, i in items.items()
                 if k not in inside and i.get("status") in ("open", "working") and i.get("state") and i.get("source") != "goal"]
    elsewhere.sort(key=lambda i: str(i.get("asked_at") or ""), reverse=True)
    rows = []
    for n, i in enumerate(elsewhere[:ELSEWHERE_ITEMS], 1):
        targets[f"I{n}"] = Target(f"I{n}", i["key"])
        rows.append(f"I{n}: {i.get('who') or 'the user'} | {i.get('title') or ''}")
    if rows:
        lines.append("Other open things on the user's Today list:\n" + "\n".join(rows))
    voice_of: dict[str, Any] = {"source": "slack"}
    if subject is not None:
        label, t = subject
        lines.append(f"The user is talking about this task: {label} ({t['title']}).")
        recent = _conversation(t["legacy_key"] or "", TRANSCRIPT_CHARS) if t.get("legacy_key") else ""
        if recent:
            lines.append("Its conversation:\n" + recent)
        voice_of = items.get(t["legacy_key"] or "") or voice_of
    else:
        for t in tasks[:6]:
            recent = _conversation(t["legacy_key"] or "", OVERVIEW_CHARS) if t.get("legacy_key") else ""
            if recent:
                lines.append(f"Recent messages for \"{t['title']}\":\n" + recent)
    lines.append(f"{calendar_events.brief(past_days=CALENDAR_PAST_DAYS)}{work.voice(voice_of)}")
    lines += _ending(said, history, "The user pointed at the task and said nothing more.")
    return "\n\n".join(x for x in lines if x), targets


def _queries(answer: dict[str, Any], kind: str) -> list[str]:
    return [str(q) for op in answer.get("operations") or [] if isinstance(op, dict) and op.get("op") == kind
            for q in (op.get("queries") or [])][:2]


ROUNDS = 3  # searches, then opening what they found, then the answer


def _ask(prompt: str, kind: str = TASK) -> dict[str, Any]:
    """One answer; when it asks for searches (the user's email and saved conversations, or
    conversations to attach) or links to open, they are run and it answers again, up to
    ROUNDS times. Conversations found are kept on the answer as ``threads``."""
    instructions = system(kind)
    answer = ai.complete_json(prompt, system=instructions, model=MODEL, timeout=TIMEOUT)
    threads: dict[str, dict[str, Any]] = {}
    for n in range(1, ROUNDS):
        mail, convs = _queries(answer, "search"), _queries(answer, "find_threads")
        urls = [str(u) for op in answer.get("operations") or [] if isinstance(op, dict) and op.get("op") == "open"
                for u in (op.get("urls") or [])][:3]
        if not (mail or convs or urls):
            break
        if mail:
            found = [{"query": q, "results": work._read_mail(q)} for q in mail]
            with conversations.opened(store.DATA_DIR) as db:
                saved = [{"query": q, "matches": conversations.search(db, q)} for q in mail]
            prompt += ("\n\nYour searches of the user's saved Slack, texts and email found:\n"
                       + json.dumps(saved, ensure_ascii=False)[:SAVED_CHARS]
                       + "\n\nYour searches of the user's Gmail found:\n" + work.mail_block(found)
                       + "\nNever claim to have found something these results do not show. Where a message says "
                       "characters are not shown, the rest of it is missing: never treat a list, schedule or "
                       "table there as complete; say what is missing.")
        if convs:
            seen: dict[str, dict[str, Any]] = {}
            for q in convs:
                for f in attached.find(q):
                    seen.setdefault(f["conversation"], f)
            threads = {f"X{k}": f for k, f in enumerate(seen.values(), 1)}
            shown = [{"thread": label, **{k: f[k] for k in ("title", "when", "latest")}} for label, f in threads.items()]
            prompt += ("\n\nConversations matching " + ", ".join(f'"{q}"' for q in convs) + ":\n"
                       + (json.dumps(shown, ensure_ascii=False, indent=1)[:12000] if shown else "(none found)"))
        if urls:
            prompt += ("\n\nThe links you opened (describe a page only from what it shows):\n"
                       + json.dumps([link_pages.read(u) for u in urls], ensure_ascii=False)[:30000])
        last = n == ROUNDS - 1
        prompt += ("\nNow answer without searching or opening anything." if last
                   else "\nNow answer; you may open links these results show, or search once more, if you still need to.")
        answer = ai.complete_json(prompt, system=instructions, model=MODEL, timeout=TIMEOUT)
    answer["threads"] = threads
    return answer


def _resolve_threads(key: str, ops: list[dict[str, Any]], threads: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach and detach name conversations by label (X1 found now, A1 attached before): put the
    conversation itself on the operation, so re-applying it later needs no labels."""
    with project_book.opened() as db:
        c = _commitment(db, key)
    mine = {f"A{n}": a for n, a in enumerate(attached.of(c) if c else [], 1)}
    out = []
    for op in ops:
        kind, label = op.get("op"), str(op.get("thread") or "")
        if kind == "attach" and label in threads:
            op = dict(op, conversation=threads[label]["conversation"], title=threads[label]["title"])
        elif kind == "detach" and label in mine:
            op = dict(op, conversation=mine[label]["conversation"], title=mine[label].get("title", ""))
        elif kind in ("attach", "detach"):
            continue
        out.append(op)
    return out


def _resolve_notes(key: str, ops: list[dict[str, Any]], focus: str) -> list[dict[str, Any]]:
    """Notes name steps (S2) and notes (N1) by label: put the ids on the operation, so
    re-applying it later needs no labels. A note made on a step's card is about that step
    unless it says otherwise."""
    with project_book.opened() as db:
        c = _commitment(db, key)
        steps = {v: k for k, v in task_context.step_labels(db, c["id"]).items()} if c else {}
        kept = task_context.note_labels(db, c["id"]) if c else {}
    out = []
    for op in ops:
        kind = op.get("op")
        if kind == "note":
            label = str(op.get("step") or "")
            step_id = steps.get(label, "") if label else (focus if focus in steps.values() else "")
            op = dict(op, step_id=step_id, replaces_id=kept.get(str(op.get("replaces") or ""), ""), focus=focus)
        elif kind == "unnote":
            if str(op.get("note") or "") not in kept:
                continue
            op = dict(op, note_id=kept[str(op["note"])])
        out.append(op)
    return out


RENAMED = {"rewrite": "title"}  # what the project box called a title change


def _normal(ops: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """Operations in the one list's words: the project box's "rewrite" is "title", and in the
    bottom bar a task named as an "item" (as it used to be) is its "task"."""
    out = []
    for op in ops:
        op = dict(op, op=RENAMED.get(str(op.get("op")), op.get("op")))
        if kind == TASKS and op.get("item") and not op.get("task"):
            op["task"] = op.pop("item")
        out.append(op)
    return out


def _resolve_targets(ops: list[dict[str, Any]], targets: dict[str, Target]) -> list[dict[str, Any]]:
    """Put each task's key and commitment on its operation, so re-applying it later (picking an
    option the user went back from) needs no labels."""
    out = []
    for op in ops:
        label = str(op.get("task") or op.get("item") or "")
        t = targets.get(label)
        if t is not None:
            op = dict(op, target={"key": t.key, "cid": t.cid})
        out.append(op)
    return out


def _clean_next(raw: Any) -> list[dict[str, str]]:
    out = []
    for n in raw if isinstance(raw, list) else []:
        if isinstance(n, dict) and str(n.get("label") or "").strip():
            label = str(n["label"]).strip()[:60]
            out.append({"label": label, "say": str(n.get("say") or label).strip()[:400]})
    return out[:3]


# ---- doing it ----------------------------------------------------------------------------


def _targets_of(ops: list[dict[str, Any]], scope: dict[str, Any]) -> dict[str, Target]:
    """The tasks the operations name, from what was put on them when they were answered."""
    out: dict[str, Target] = {}
    if scope["kind"] == TASK:
        out["T1"] = Target("T1", scope["key"])
    for op in ops:
        t = op.get("target")
        label = str(op.get("task") or op.get("item") or "")
        if isinstance(t, dict) and label:
            out[label] = Target(label, str(t.get("key") or ""), str(t.get("cid") or ""), None)
    return out


def _apply(scope: dict[str, Any], ops: list[dict[str, Any]], said: str,
           targets: dict[str, Target] | None = None) -> dict[str, Any]:
    """Apply the operations as one undoable batch. Returns the batch id, any calendar events
    made, the draft written (for the card), what changed
    (for the page's green edges) and a summary (for the toast)."""
    kind = scope["kind"]
    targets = targets if targets is not None else _targets_of(ops, scope)
    for op in ops:
        # A task the judge found has no item yet; writing a draft onto it makes one first.
        action = assistant_actions.ACTIONS.get(str(op.get("op") or ""))
        label = str(op.get("task") or ("T1" if kind == TASK else ""))
        if action and action.needs_item and label in targets and ledger.is_commitment_key(targets[label].key):
            ledger.ensure_item(targets[label].key)
    proposed = {task_steps._norm(str(st.get("text") if isinstance(st, dict) else st))
                for op in ops if op.get("op") in ("steps", "add") for st in (op.get("steps") or [])}
    with project_book.opened() as db:
        b = Batch(db, "assistant", f"{'On the card' if kind == TASK else 'Told Today'}: {said[:80]}")
        mine = [(p["id"], p["title"]) for p in Book(db).projects() if p["stage"] == "open"]
        d = Doing(b, kind, said, targets, mine, pid=str(scope.get("pid") or ""), focus=str(scope.get("focus") or ""),
                  proposed=proposed)
        items = store.load()["items"]
        d.was = {t.key: _filing(items[t.key]) for t in targets.values() if t.key in items}
        d.aliases = {user_projects.filer_id(p): p["id"] for p in user_projects.open_projects(db)}
        skipped = 0
        for op in ops:
            action = assistant_actions.ACTIONS.get(str(op.get("op") or ""))
            if action is None or kind not in action.scopes:
                continue
            try:
                action.apply(d, op)
            except commitments.StaleWrite:
                skipped += 1  # the user changed it while Today was thinking: the user's wins
            except (ProjectError, commitments.CommitmentError, KeyError, TypeError, ValueError,
                    OSError) as e:
                log.warning("assistant op %s skipped: %s", op, e)
                skipped += 1
        if kind == TASKS and (d.record or scope.get("keys")):
            # The user's words in the bottom bar are remembered for the readers whenever they changed
            # something, or when the user pointed at tasks (it is about them even if nothing moved).
            lid = learned.add(db, said, d.record or [
                {"key": k, "who": items[k].get("who") or "", "title": items[k].get("title") or "",
                 "words": str(items[k].get("ask_text") or "")[:300], "was": d.was.get(k, ""), "now": "as the user said"}
                for k in scope.get("keys") or [] if k in items])
            b.ops.append({"t": "learned", "id": lid})
            d.made["learned"] = lid
        bid = b.save() if b.ops else ""
    if d.made["events"]:
        calendar_events.refresh()
    summary = _summary(b.counts, skipped) + (" · remembered for next time" if d.made.get("learned") else "")
    return {**d.made, "batch": bid, "changed": list(b.changed), "summary": summary, "skipped": skipped}


SUMMARY_ORDER = ("to Next", "to Then", "retitled", "steps", "added", "brought in", "drafted", "finished", "waiting",
                 "dropped", "refiled", "to Later", "moved", "marked done", "noted")


def _summary(counts: dict[str, int], skipped: int) -> str:
    parts = []
    for k in SUMMARY_ORDER:
        n = counts.get(k, 0)
        if not n:
            continue
        if k == "steps":
            parts.append(f"steps for {n} {'task' if n == 1 else 'tasks'}")
        elif k == "waiting":
            parts.append(f"{n} now waiting on others")
        else:
            parts.append(f"{n} {k}")
    if skipped:
        parts.append(f"{skipped} left as {'it was' if skipped == 1 else 'they were'}")
    return "Done: " + ", ".join(parts) if parts else "Nothing to change"


def _undo(turn: dict[str, Any]) -> None:
    """Take back what a turn did: its batch and its calendar blocks."""
    if turn.get("batch"):
        with project_book.opened() as db:
            project_book.undo(db, turn["batch"])
    for ev in turn.get("events") or []:
        if ev.get("id"):
            planning._cal_delete(ev["id"])
    if turn.get("events"):
        calendar_events.refresh()


def _item(key: str) -> dict[str, Any]:
    if ledger.is_commitment_key(key):
        ledger.ensure_item(key)
    item = store.load()["items"].get(key)
    if item is None:
        raise ActError("That task isn't there any more.")
    return item


def _scope(scope: dict[str, Any]) -> dict[str, Any]:
    kind = scope.get("kind")
    if kind == TASK:
        return {"kind": TASK, "key": str(scope.get("key") or ""), "focus": str(scope.get("focus") or "")}
    if kind == PROJECT:
        return {"kind": PROJECT, "pid": str(scope.get("pid") or ""), "task": str(scope.get("task") or "")}
    if kind == TASKS:
        return {"kind": TASKS, "keys": [str(k) for k in scope.get("keys") or []]}
    raise ActError("Say where you are first.")


def _shelf_key(scope: dict[str, Any], said: str) -> str:
    """What a taken-back answer is kept under: the user's words, and, in the bottom bar or a project's
    box, the tasks it was about (the same words about other tasks are a new question)."""
    if scope["kind"] == TASKS:
        return said + "|" + ",".join(scope["keys"])
    if scope["kind"] == PROJECT:
        return said + "|" + scope["task"]
    return said


def _note_missing(place: str, said: str, missing: str, did: str) -> None:
    """Something the user asked that Today can't do yet, kept so the gaps can be found and built
    (the user, 2026-11-15: offer it anyway, then say plainly what is missing)."""
    with project_book.opened() as db:
        db.execute(MISSING_SQL)
        db.execute("INSERT INTO missing_abilities (at, place, said, missing, did) VALUES (?, ?, ?, ?, ?)",
                   (store.now_iso(), place, said[:400], missing[:200], did[:400]))


def missing_list() -> list[dict[str, Any]]:
    with project_book.opened() as db:
        db.execute(MISSING_SQL)
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM missing_abilities ORDER BY id DESC")]


def opening(key: str, focus: str = "") -> dict[str, Any]:
    """The first options for a card (or one of its steps), worked out once and kept."""
    item = _item(key)
    scope = {"kind": TASK, "key": key, "focus": focus}
    rec = _record(scope)
    if rec["opening"] is None:
        prompt, _ = _task_prompt(key, item, focus, "")
        rec["opening"] = _clean_next(_ask(prompt, TASK).get("next"))
        _save(scope, rec)
    return _view(rec)


# ---- getting a card ready before the user opens it ------------------------------------------------

AHEAD = """The user has not opened this card yet. Today is getting it ready for the user, as it does whenever a new message comes in. Do now only what the user would want already done when the user looks, and nothing the user has to decide:
- A reply in this conversation that would finish it or move it on: write it ("draft"). A task with "no_conversation_to_reply_in" gets an email to the person instead ("email"), never a draft. For a task the user is waiting on someone for (list "waiting"), write only a short, friendly nudge asking again for the thing the user asked for, for the user to send if they stay quiet ("draft"; it is kept as the nudge): no thank-you, no extra note or suggestion, and nothing at all when they already gave it. For a pitch (a cold sales, vendor or "let's grab coffee" ask from someone with no real relationship with the user), write a short, kind, clear no.
- An introduction or a note to someone outside this conversation that was asked for or agreed to: write the email(s) ("email"), one per introduction, addressed to both people.
- A meeting, call, dinner or party the conversation settles that the user's calendar doesn't have yet: give it as "calendar". It is only offered on the card; the user adds it with a tap.
- A decision or fact only the user has is missing (whether the user is going, which option): write nothing, and offer the choices as the options.
- A task in no project that is really part of one of the user's projects (about it, not merely mentioning it; a lunch or a catch-up is not): file it there ("project").
- A fact or link the message needs (an invite link, an address, a doc) that you don't have: search for it first ("search"); never leave a placeholder like [day/time] or [link]. If it can't be found, write nothing and say so.
- A draft already written that still fits: leave it. Rewrite it only when the conversation has moved on or it is wrong.
Never mark it done, drop it, put it in Later, refile it, retitle it, move it out of a project, or change its steps or notes: those are the user's to do, or the judge's. "did" is never empty: one sentence on what you wrote, or why you wrote nothing."""
AHEAD_LABEL = "Written ahead"
AHEAD_OPS = {"draft": "draft", "email": "email", "calendar": "offer_event", "project": "project"}  # what it may do, as which action


def write_ahead(key: str, signature: str) -> dict[str, Any] | None:
    """Get a task's card ready before the user opens it (the user, 2026-11-15: the one assistant writes
    ahead, in place of the old reply writer): the reply, a nudge, intro emails and events to
    offer, and the first options. Runs once per ``signature`` (what its conversation says now);
    None when it already ran for this one. What it writes shows on the card as its first answer,
    "Written ahead", and Other options takes it back like any other."""
    scope = {"kind": TASK, "key": key, "focus": ""}
    item = _item(key)
    rec = _record(scope)
    if rec["ahead"] == signature:
        return None
    prompt, targets = _task_prompt(key, item, "", "", nothing=user_rules.RULES + "\n" + AHEAD)
    answer = _ask(prompt, TASK)
    ops = [dict(o, op=AHEAD_OPS[o["op"]]) for o in _normal(
        [o for o in answer.get("operations") or [] if isinstance(o, dict)], TASK) if o.get("op") in AHEAD_OPS]
    with project_book.opened() as db:
        c = _commitment(db, key)
        filed = c is not None and Book(db).project_of(c) is not None
    if filed:
        ops = [o for o in ops if o["op"] != "project"]  # filing is only into a project, never out or across
    ops = [o for o in ops if o["op"] != "project" or o.get("project")]
    ops = _resolve_targets(ops, targets)
    did, nxt = str(answer.get("did") or "").strip(), _clean_next(answer.get("next"))
    rec = _record(scope)  # the user may have used the card meanwhile
    if ops:
        made = _apply(scope, ops, AHEAD_LABEL, targets)
        rec["turns"].append({"label": AHEAD_LABEL, "said": "", "shelf_key": "", "did": did, "ops": ops, "next": nxt,
                             "at": store.now_iso(), "ahead": True,
                             **{k: made[k] for k in ("batch", "events", "picked", "draft", "gone")}})
    elif not rec["turns"]:
        rec["opening"] = nxt
    # Nothing written on a card the user already worked on: it stays as the user left it.
    rec["ahead"] = signature
    _save(scope, rec)
    return _view(rec)


AHEAD_PER_REFRESH = 30  # cards written ahead per refresh at most; the rest wait for the next one
AHEAD_WORKERS = 4


def ahead_due() -> list[tuple[str, str]]:
    """The tasks whose cards need writing ahead, with what their conversations say now: every
    open task of the user's or that the user waits on, tied to a conversation, whose conversation changed
    since its card was last written ahead (oldest change first). A card the old reply writer
    already wrote is taken as written for what its conversation said then (once, at the switch)."""
    items = store.load()["items"]
    due: list[tuple[float, str, str]] = []
    with project_book.opened() as db:
        last = {r[0]: (r[1] or 0.0, r[2]) for r in db.execute(
            "SELECT l.commitment_id, max(c.last_at), group_concat(c.id || '@' || c.last_at) FROM commitment_links l "
            "JOIN conversations c ON c.id = l.conversation_id GROUP BY l.commitment_id")}
        for c in commitments.all_commitments(db):
            if (c["stage"] != "open" or c["direction"] not in ("mine", "theirs") or c["is_project"]
                    or "step" in c["labels"] or c["id"] not in last):
                continue
            key = c["legacy_key"] or c["id"]
            at, sig = last[c["id"]]
            rec = task_context.card_records(db, key).get("task") or {}
            if rec.get("ahead") == sig:
                continue
            if not rec.get("ahead") and (items.get(key) or {}).get("worked_at"):
                task_context.save_card_record(db, key, "task", dict(_empty(), **rec, ahead=sig))
                continue
            due.append((at, key, sig))
    due.sort()
    return [(key, sig) for _, key, sig in due]


def write_ahead_due(limit: int = AHEAD_PER_REFRESH) -> dict[str, int]:
    """After a refresh: write ahead every card that needs it (``ahead_due``), a few at a time."""
    todo = ahead_due()
    counts = {"due": len(todo), "written": 0, "errors": 0}

    def one(job: tuple[str, str]) -> None:
        try:
            if write_ahead(*job) is not None:
                counts["written"] += 1
        except (ai.ClaudeError, ActError, ProjectError, commitments.CommitmentError, sqlite3.Error, OSError, ValueError,
                KeyError) as e:
            log.warning("writing ahead %s failed: %s", job[0], e)
            counts["errors"] += 1

    with ThreadPoolExecutor(AHEAD_WORKERS) as pool:
        list(pool.map(one, todo[:limit]))
    return counts


def ask(scope: dict[str, Any], said: str, label: str = "") -> dict[str, Any]:
    """Do what the user said or tapped, wherever the user is, and offer what comes next. Picking an
    option the user went back from re-applies what it made, without asking again."""
    scope = _scope(scope)
    kind = scope["kind"]
    said = said.strip()
    if not said and not (kind == PROJECT and scope["task"]):
        raise ActError("Say what to do first.")
    if kind == TASK:
        _item(scope["key"])
    rec = _record(scope)
    shelf_key = _shelf_key(scope, said)
    kept = rec["shelf"].pop(shelf_key, None) if said else None
    targets = None
    if kept is not None:
        ops, did, nxt = kept["ops"], kept["did"], kept["next"]
    else:
        if kind == TASK:
            item = store.load()["items"][scope["key"]]
            prompt, targets = _task_prompt(scope["key"], item, scope["focus"], said)
        elif kind == PROJECT:
            if said:
                with project_book.opened() as db:
                    p = Book(db).by_id.get(scope["pid"])
                    subject = Book(db).by_id.get(scope["task"]) if scope["task"] else None
                    if p is not None:
                        project_book.add_note(db, scope["pid"], said, subject)
            prompt, targets = _project_prompt(scope["pid"], scope["task"], said, rec["turns"])
        else:
            prompt, targets = _list_prompt(scope["keys"], said, rec["turns"])
        answer = _ask(prompt, kind)
        ops = _normal([o for o in answer.get("operations") or [] if isinstance(o, dict) and o.get("op") not in TOOLS], kind)
        if kind == TASK:
            ops = _resolve_notes(scope["key"], _resolve_threads(scope["key"], ops, answer.get("threads") or {}), scope["focus"])
        ops = _resolve_targets(ops, targets)
        did, nxt = str(answer.get("did") or "").strip(), _clean_next(answer.get("next"))
        if str(answer.get("missing") or "").strip():
            _note_missing(_place(scope), said, str(answer["missing"]).strip(), did)
    # A conversation being attached is fetched first, on its own connection, before the batch opens.
    for op in ops:
        if op.get("op") == "attach":
            attached.fetch(str(op["conversation"]))
    made = _apply(scope, ops, said, targets)
    turn = {"label": label or said or "Sort this task out", "said": said, "shelf_key": shelf_key, "did": did, "ops": ops,
            "next": [] if made["gone"] and kind == TASK else nxt, "at": store.now_iso(),
            **{k: made[k] for k in ("batch", "events", "picked", "draft", "gone")}}
    if kind == TASK and scope["focus"] and any(o.get("op") == "steps" for o in ops):
        # New steps replace the one the user was on, so what was done is kept with the task as a
        # whole, where the card shows it (with its way back) above the new steps.
        _save(scope, rec)
        scope = dict(scope, focus="")
        rec = _record(scope)
    rec["turns"].append(turn)
    _save(scope, rec)
    out = _view(rec)
    out.update(gone=made["gone"], batch=made["batch"], events=made["events"], changed=made["changed"],
               summary=made["summary"])
    return out


def back(key: str = "", focus: str = "", scope: dict[str, Any] | None = None) -> dict[str, Any]:
    """Other options: take back the last thing done here and show the options before it."""
    scope = _scope(scope or {"kind": TASK, "key": key, "focus": focus})
    if scope["kind"] == TASK:
        _item(scope["key"])
    rec = _record(scope)
    if rec["turns"]:
        turn = rec["turns"].pop()
        _undo(turn)
        if turn.get("said"):
            rec["shelf"][turn.get("shelf_key") or turn["said"]] = {k: turn[k] for k in ("ops", "did", "next")}
        _save(scope, rec)
    return _view(rec)


# ---- notes from what the user said before there were notes ----------------------------------------

HISTORY_SYSTEM = """You keep short notes on one task in Today, the user's list of what the user owes people and what the user is doing. {ABOUT} You get the task as it stands (its steps, any notes already kept) and everything the user said on its cards, with what the assistant answered, plus any notes the user gave on earlier drafts and the user's answers to its questions. Pick out what Today must keep knowing about this task.

Answer with exactly one JSON object and nothing else: {"notes": [{"text": "...", "step": "", "from": "..."}]}. "step" is the step a note is about ("S2") or "". "from" is the user's exact words the note comes from, copied, or the assistant's words for something it found. No notes is fine: {"notes": []}.

""" + NOTE_RULES.replace("(in the same answer as everything else) ", "")


