"""The user's weekly priorities: the note the user writes every Monday, kept word for word, and the
next move toward each of its goals that leads the top of the page.

The note is stored verbatim, one dated file per week, in the weekly priorities folder
(the ``weekly_dir`` setting: ``<weekly_dir>/<Monday>.md``), so past weeks stay findable. The newest note carries over until the user writes a new one; nothing ever asks
the user for one. Notes the user types into a "go do" move are kept beside it, under ``Notes/``.

Parsing is forgiving: the "#1 priority" line is found by its wording, and the goals are the
lines after a "Goals:" heading (or, with no heading, every other line), bullets or not.
The user's own asides inside a goal: a stretch goal is treated like any other (the user orders them themselves),
and travel days ("traveling to a wedding Thu/Fri") keep it out of Now on those days.

Each open goal gets one concrete next move from the reader (the same headless model step
the rest of Today uses), grounded in the user's goal text and, when the #1 priority matches one of
the user's project folders, that project's Goal.md, its agent's playbook and notes. A move is an
item of its own (source "goal"): a message to start something (who to ask as answer
buttons that write it), a "go do" with links and a notes box, or a draft to keep writing (in
the writing app the ``writing_dir`` setting points at, when there is one).
As everywhere in Today, a message is only sent after the user reads it and presses Send.
"""

import hashlib
import http.client
import json
import logging
import re
import sqlite3
import threading
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from today import ai, commitments, config, connectors, conversations, links, store

log = logging.getLogger(__name__)

_SETTINGS = config.load()
WEEKLY_DIR = _SETTINGS.weekly_dir
PROJECTS_DIR = _SETTINGS.projects_dir
AGENTS_DIR = _SETTINGS.agents_dir
# The writing app's drafts (optional): a folder of markdown files, and the link a draft opens at.
WRITING_DIR = _SETTINGS.writing_dir
WRITING_LINK = _SETTINGS.writing_url
WRITING_START = _SETTINGS.writing_url.split("#")[0] if _SETTINGS.writing_url else ""
SLACK_USERS = _SETTINGS.slack_users
# The user's project folders and the agent (if any) that keeps each one's playbook, from the
# ``projects`` setting.
PROJECT_AGENTS = {p.get("folder") or p["title"]: p.get("agent") or "" for p in _SETTINGS.projects}
DAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
AWAY_WORDS = re.compile(
    r"\b(travel\w*|away|out of office|ooo|offline|flying|wedding|vacation|trip|off)\b", re.I
)


# ---- the note: stored verbatim, one dated file per week -----------------------------


def week_start(day: date | None = None) -> date:
    day = day or datetime.now().date()
    return day - timedelta(days=day.weekday())


def save(text: str) -> Path:
    """Keep the user's note word for word as this week's file (an earlier save this week is kept
    under ``.versions/`` before it is replaced)."""
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    path = WEEKLY_DIR / f"{week_start().isoformat()}.md"
    if path.exists() and path.read_text() != text:
        versions = WEEKLY_DIR / ".versions"
        versions.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        (versions / f"{path.stem} {stamp}.md").write_text(path.read_text())
    path.write_text(text)
    return path


def current() -> dict[str, Any] | None:
    """The newest note up to this week: it carries over until the user writes a new one."""
    if not WEEKLY_DIR.is_dir():
        return None
    today = week_start().isoformat()
    files = sorted(
        p for p in WEEKLY_DIR.glob("*.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem) and p.stem <= today
    )
    if not files:
        return None
    path = files[-1]
    text = path.read_text()
    return dict(parse(text), week=path.stem, text=text, path=str(path))


# ---- parsing the user's wording -----------------------------------------------------------


PRIORITY = re.compile(r"#\s*1\b|number one|\bno\.?\s*1\b|top priority|main priority|\bpriority\b", re.I)
HEADING = re.compile(r"^\s*(goals?|this week'?s goals?|weekly goals?|plan|focus)\s*:?\s*$", re.I)
BULLET = re.compile(r"^\s*(?:[-*•◦]|\d+[.)]|\[[ xX]?\])\s*")


def goal_id(text: str) -> str:
    """Stable while the user's words for the goal stay the same, so a tick survives a re-save."""
    norm = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()[:80]
    return hashlib.sha1(norm.encode()).hexdigest()[:10]


def parse(text: str) -> dict[str, Any]:
    lines = [ln.rstrip() for ln in text.splitlines()]
    priority, start = "", 0
    for n, ln in enumerate(lines):
        if ln.strip() and PRIORITY.search(ln) and not HEADING.match(ln):
            priority, start = ln.strip(), n + 1
            break
    heading = next((n for n, ln in enumerate(lines) if HEADING.match(ln)), None)
    body = lines[heading + 1 :] if heading is not None else lines[start:]
    goals = []
    for ln in body:
        words = BULLET.sub("", ln).strip()
        if not words or HEADING.match(words) or words == priority:
            continue
        goals.append(goal_meta(words))
    return {"priority": priority, "focus": focus_of(priority), "goals": goals}


def focus_of(priority: str) -> str:
    """The words naming the priority itself ("improve customer onboarding"), to set in bold."""
    m = re.search(r"(?:priority|#\s*1)[^:]*:\s*(.+)", priority, re.I)
    rest = m.group(1) if m else ""
    return re.split(r",|;| - | — |\. ", rest, maxsplit=1)[0].strip().rstrip(".")


def goal_meta(words: str) -> dict[str, Any]:
    asides = " ".join(re.findall(r"\(([^)]*)\)", words))
    stretch = bool(re.search(r"\bstretch\b", asides or words, re.I))
    away: list[int] = []
    if AWAY_WORDS.search(asides):
        away = away_days(asides)
    return {"id": goal_id(words), "text": words, "stretch": stretch, "away_days": away}


def away_days(text: str) -> list[int]:
    """Weekdays named in the user's aside ("Thu/Fri", "Thursday and Friday", "Wed-Fri")."""
    tokens = re.findall(r"[A-Za-z]+|[-–]", text)
    out: list[int] = []
    prev: int | None = None
    ranging = False
    for t in tokens:
        if t in ("-", "–"):
            ranging = prev is not None
            continue
        day = DAYS.get(t.lower())
        if day is None:
            ranging = False
            continue
        if ranging and prev is not None and day > prev:
            out.extend(range(prev + 1, day + 1))
        elif day not in out:
            out.append(day)
        prev, ranging = day, False
    return sorted(set(out))


def away_today(goal: dict[str, Any], today: date | None = None) -> bool:
    return (today or datetime.now().date()).weekday() in goal.get("away_days", [])


# ---- the week's state in Today's store ---------------------------------------------


def week_state(state: dict[str, Any], week: str) -> dict[str, Any]:
    """Ticks and moves for this note's week (a new week starts clean)."""
    w = state.get("weekly") or {}
    if w.get("week") != week:
        w = {"week": week, "moves": {}, "related": {}, "serves": {}, "steps_done": {}, "pending": {}}
        state["weekly"] = w
    for k in ("moves", "related", "serves", "steps_done", "pending"):
        w.setdefault(k, {})
    w.pop("done", None)  # ticks live on the week's tasks (done_goals), not here
    return w


def priorities(db: sqlite3.Connection, week: str) -> dict[str, dict[str, Any]]:
    """That week's priorities as tasks, by goal id: one task of the user's per goal in the user's note,
    with its week set (a commitment like any other; ``detail.week_goal`` names the goal)."""
    rows = db.execute(
        "SELECT id FROM commitments WHERE week = ? AND json_extract(detail, '$.week_goal') IS NOT NULL", (week,)
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        c = commitments.get(db, r[0])
        if c is not None and c["stage"] != "stale":
            out[str(c["detail"]["week_goal"])] = c
    return out


def sync_priorities(db: sqlite3.Connection, note: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """The user's note's goals as tasks for its week: a new goal becomes a task, the user's order is kept,
    and a goal the user took out of the note (or rewrote) goes quiet. The user's words stay the note's."""
    note = note if note is not None else current()
    if not note:
        return {}
    have = priorities(db, note["week"])
    for n, g in enumerate(note["goals"]):
        detail = {"week_goal": g["id"], "stretch": bool(g.get("stretch")), "away_days": g.get("away_days") or [],
                  "priority_line": note.get("priority", "")}
        c = have.get(g["id"])
        if c is None:
            cid = commitments.create(
                db, "sync",
                {"title": g["text"], "direction": "self", "origin": "user_chose", "started_by": "user",
                 "week": note["week"], "position": n, "detail": detail},
                cid=commitments.new_id(f"week:{note['week']}:{g['id']}"), reason=f"weekly priority, week of {note['week']}",
            )
            have[g["id"]] = commitments.get(db, cid) or {}
        elif c["position"] != n or c["title"] != g["text"]:
            have[g["id"]] = commitments.change(db, c["id"], "sync", {"position": n, "title": g["text"]},
                                               reason="the user's order in this week's note")
    gone = {gid for gid in have if gid not in {g["id"] for g in note["goals"]}}
    for gid in gone:
        c = have.pop(gid)
        if c["stage"] == "open":
            commitments.change(db, c["id"], "sync", {"stage": "stale", "stage_reason": "no longer in your weekly note"},
                               reason="the user took it out of the user's note")
    return have


def done_goals(week: str) -> dict[str, str]:
    """Goals the user crossed out that week (goal id -> when)."""
    with conversations.opened(store.DATA_DIR) as db:
        return {gid: c["updated_at"] for gid, c in priorities(db, week).items() if c["stage"] == "done"}


def set_done(gid: str, on: bool) -> None:
    """Cross a goal out for the week (it stays on the list), or undo that."""
    note = current()
    if not note:
        return
    with conversations.opened(store.DATA_DIR) as db:
        c = sync_priorities(db, note).get(gid)
        if c is None:
            return
        commitments.change(db, c["id"], "user", {"stage": "done" if on else "open",
                                                    "stage_reason": "the user crossed it out" if on else ""},
                           reason="the user crossed it out" if on else "the user un-crossed it")


# ---- context the reader is grounded in ----------------------------------------------


def matched_project(priority: str) -> str:
    """The project folder the user's #1 priority is about ("improve customer onboarding" ->
    "Customer onboarding"), or "" when none clearly is."""
    words = {w[:6] for w in re.findall(r"[a-z]{4,}", priority.lower())}
    best, score = "", 0
    for name in PROJECT_AGENTS:
        if not (PROJECTS_DIR / name).is_dir():
            continue
        own = {w[:6] for w in re.findall(r"[a-z]{4,}", name.lower())}
        hit = len(own & words)
        if hit == len(own) and hit > score:
            best, score = name, hit
    return best


def _read(path: Path, limit: int = 8000) -> str:
    try:
        return path.read_text()[:limit]
    except OSError:
        return ""


def project_context(name: str) -> str:
    if not name:
        return ""
    agent = PROJECT_AGENTS.get(name, "")
    parts: list[str] = [f"The user's project folder for this priority: {name}.", "Goal.md (the user's own words):", _read(PROJECTS_DIR / name / "Goal.md")]
    for label, file in (
        ("The project agent's playbook", "playbook.md"),
        ("The project agent's inbox of notes and links", "inbox.md"),
        ("Open proposals", "proposals.md"),
    ):
        text = _read(AGENTS_DIR / agent / file) if agent else ""
        if text:
            parts += [f"{label}:", text]
    return "\n".join(p for p in parts if p)


def writing_docs() -> list[dict[str, str]]:
    """The user's drafts in the writing app: id (file name) and title (first heading or line)."""
    out: list[dict[str, str]] = []
    if WRITING_DIR is None or not WRITING_DIR.is_dir():
        return out
    for p in sorted(WRITING_DIR.glob("*.md")):
        head = _read(p, 3000)
        m = re.search(r'(?m)^title:\s*"?(.*?)"?\s*$', head) if head.startswith("---") else None
        first = next((ln for ln in _body(head).splitlines() if ln.strip()), p.stem)
        out.append({"id": p.stem, "title": (m.group(1) if m else first.lstrip("# ")).strip()})
    return out


def _body(text: str) -> str:
    """A draft without its front matter."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            return text[end + 4 :]
    return text


def doc_for(goal_text: str, docs: list[dict[str, str]]) -> dict[str, str] | None:
    """A draft the user's goal names (by its quoted title or most of its words)."""
    quoted = [q.lower() for q in re.findall(r"[\"“]([^\"”]+)[\"”]", goal_text)]
    for d in docs:
        title = d["title"].lower().strip(" ?")
        if any(title and (q.strip(" ?") in title or title in q) for q in quoted):
            return d
    return None


def slack_people() -> dict[str, str]:
    try:
        names = json.loads(SLACK_USERS.read_text()) if SLACK_USERS else {}
    except (OSError, ValueError):
        return {}
    return names if isinstance(names, dict) else {}


def person(name: str, users: dict[str, str]) -> dict[str, str]:
    """Where a message to this person would go: their Slack DM when they are on Slack,
    else email when the user's mail knows their address (blank when neither)."""
    first = name.split()[0].lower() if name.split() else ""
    hits = [uid for uid, full in users.items() if full.lower() == name.lower()]
    hits = hits or [uid for uid, full in users.items() if full.split() and full.split()[0].lower() == first]
    if len(hits) == 1:
        return {"name": name, "slack_id": hits[0], "full": users[hits[0]], "email": ""}
    return {"name": name, "slack_id": "", "full": name, "email": ""}


def _reachable(url: str) -> bool:
    """A link the reader proposed is shown only if it actually opens (no made-up links)."""
    if not links.is_link(url):
        return False
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status < 400
    except (OSError, ValueError, http.client.HTTPException):  # URLError is an OSError
        return False


# ---- the reader: one next move per goal --------------------------------------------


SYSTEM = """You help {WHO} move the user's weekly goals forward. The user wrote the user's priorities for the week in the user's own words. For each open goal, propose the ONE next move the user could make today: concrete, small enough to do in one sitting, and something that starts or advances the work (often work nobody asked the user for).

Each move is one of:
- "message": a message the user sends to start something (ask a designer to work with the user, ask users for feedback, set up a session). Give "people": the one to three people the user would send it to, by the names the user's goal or project notes use (when the goal says "Sam or Priya", both are choices). Do not write the message yet.
- "go_do": something the user does themselves (try three products, read the onboarding call notes). Give "links": up to four real links that help, ONLY ones you are sure exist (a product's home page, a link from the user's project notes); never make one up.
- "write": a piece of writing (every writing goal gets this kind). When it is one of the user's drafts (listed below), give its id in "doc"; the title is then "Keep writing <its title>"; with no document yet, "Start writing <what>".

"title" is the move as a short imperative line in plain words (under 14 words), like "Ask Sam or Priya to sketch a simpler onboarding with you". Do not repeat a move the user already did (listed per goal).
"serves_priority": true when the goal serves the user's #1 priority.
"related": the keys of open items in the user's list (given below) that are part of this goal (for example replies to customers about why they stopped using the product); [] when none.

Answer with one JSON object: {"goals": [{"id": "...", "serves_priority": true, "move": {"kind": "message|go_do|write", "title": "...", "people": [], "links": [{"label": "...", "url": "..."}], "doc": ""}, "related": []}]}"""


def _prompt(note: dict[str, Any], w: dict[str, Any], state: dict[str, Any], goals: list[dict[str, Any]]) -> str:
    items = [
        {"key": k, "who": i.get("who", ""), "title": i.get("title", "")}
        for k, i in state["items"].items()
        if i["status"] in ("open", "working") and i.get("source") != "goal"
    ][:200]
    users = sorted(set(slack_people().values()))
    lines = [
        f"Today is {datetime.now().strftime('%A, %B %d, %Y')}.",
        "The user's note for this week, word for word:",
        note["text"],
        "",
        "The open goals to plan (id: the user's words):",
        *[f"- {g['id']}: {g['text']}" for g in goals],
    ]
    for g in goals:
        done = w["steps_done"].get(g["id"]) or []
        if done:
            lines.append(f"Moves the user already did for {g['id']}: " + "; ".join(done))
    lines += ["", project_context(matched_project(note["priority"]))]
    lines += ["", "The user's teammates on Slack: " + ", ".join(users[:200])]
    lines += ["", "The user's drafts (id: title):", *[f"- {d['id']}: {d['title']}" for d in writing_docs()]]
    lines += ["", "Open items in the user's list:", json.dumps(items, ensure_ascii=False)]
    return "\n".join(lines)


_planning = threading.Lock()


def plan(force: bool = False) -> int:
    """Give every open goal without a live move its next move. Returns how many were made."""
    note = current()
    # One planning at a time; a later request waits its turn (its goal shows a placeholder).
    if not note or not _planning.acquire(timeout=PENDING_SECONDS * 5):
        return 0
    try:
        state = store.load()
        w = week_state(dict(state), note["week"])
        live = {
            gid
            for gid, key in w["moves"].items()
            if state["items"].get(key, {}).get("status") in ("open", "working")
        }
        todo = [
            g
            for g in note["goals"]
            if g["id"] not in done_goals(note["week"]) and (force or g["id"] not in live)
        ]
        if not todo:
            return 0
        mark_pending([g["id"] for g in todo])
        answer = ai.complete_json(_prompt(note, w, state, todo), system=SYSTEM.replace("{WHO}", config.who_they_are()), timeout=300)
        made = apply_plan(note, todo, answer)
        return made
    except ai.ClaudeError as e:
        log.warning("weekly plan failed: %s", e)
        why = str(e)[:300]

        def failed(s: dict[str, Any]) -> None:
            ws = week_state(s, note["week"])
            ws.update(error=why)
            ws["pending"] = {}

        store.mutate(failed)
        return 0
    finally:
        _planning.release()


WRITING = re.compile(
    r"^\s*(write|draft|finish writing|keep writing)\b|\b(publishable|essay|blog post|memo)\b", re.I
)


def is_writing(goal_text: str) -> bool:
    return bool(WRITING.search(goal_text))


def last_lines(doc_id: str, n: int = 4) -> str:
    """Where the user left off: the last few lines of the user's draft, word for word."""
    text = _body(_read(WRITING_DIR / f"{doc_id}.md", 400000)) if WRITING_DIR else ""
    # Nor are closing sections like the acknowledgements or references.
    text = re.split(
        r"(?im)^(?:---\s*\n\s*)?#+\s*(acknowledg\w*|references|sources|notes|footnotes|endnotes)\b.*$", text
    )[0]
    text = re.sub(r"\n---\s*$", "", text.rstrip())
    # Footnote definitions sit at the end of the file but are not where the user stopped writing.
    lines = [
        ln.rstrip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("[^")
    ]
    return "\n".join(lines[-n:])


def apply_plan(note: dict[str, Any], goals: list[dict[str, Any]], answer: dict[str, Any]) -> int:
    by_id = {g["id"]: g for g in goals}
    users = slack_people()
    docs = {d["id"]: d for d in writing_docs()}
    planned = []
    entries = {str(e.get("id") or ""): e for e in answer.get("goals") or [] if isinstance(e, dict)}
    for g in goals:
        # Every open goal gets a move: the user's own words stand in when the reader gave none.
        entries.setdefault(g["id"], {"id": g["id"], "move": {"kind": "go_do", "title": g["text"]}})
    for entry in entries.values():
        g = by_id.get(str(entry.get("id") or ""))
        move = entry.get("move") or {}
        if not g or not str(move.get("title") or "").strip():
            continue
        kind = move.get("kind") if move.get("kind") in ("message", "go_do", "write") else "go_do"
        doc = docs.get(str(move.get("doc") or "")) or doc_for(g["text"], list(docs.values()))
        if is_writing(g["text"]) and kind != "message":
            # A writing goal's move is always to keep writing, where the user left off.
            kind = "write"
            move = dict(move, title=f"Keep writing {doc['title']}" if doc else move["title"])
        links = [
            {"label": str(x.get("label") or x.get("url"))[:40], "url": str(x.get("url"))}
            for x in (move.get("links") or [])[:4]
            if isinstance(x, dict) and x.get("url") and _reachable(str(x["url"]))
        ]
        people = [person(str(n), users) for n in (move.get("people") or [])[:3] if str(n).strip()]
        if kind == "message" and not people:
            kind = "go_do"
        planned.append((g, kind, str(move["title"]).strip()[:120], links, people, doc, entry))

    def change(state: dict[str, Any]) -> int:
        w = week_state(state, note["week"])
        for g, kind, title, links, people, doc, entry in planned:
            n = len(w["steps_done"].get(g["id"]) or []) + 1
            key = f"goalmove:{note['week']}:{g['id']}:{n}"
            old = state["items"].get(w["moves"].get(g["id"], ""))
            if old and old["status"] in ("open", "working") and old["key"] != key:
                old.update(status="skipped", cleared_reason="replaced by a new move", cleared_at=store.now_iso())
            item = {
                "key": key,
                "source": "goal",
                "kind": "goal",
                "who": "",
                "title": title,
                "ask_text": title,
                "status": "open",
                "asked_at": store.now_iso(),
                "created": store.now_iso(),
                "weekly_goal": g["id"],
                "goal_text": g["text"],
                "move": {
                    "kind": kind,
                    "links": links,
                    "people": people,
                    "doc": ({"id": doc["id"], "title": doc["title"], "url": WRITING_LINK + doc["id"] if WRITING_LINK else ""}
                            if doc else None),
                    "start_url": WRITING_START if kind == "write" and not doc else "",
                },
                "draft": "",
                "did": "",
                "questions": [],
                "state": "mine",
            }
            if kind == "message":
                names = [p["name"] for p in people]
                item.update(state="you", questions=[{"q": "Who to ask", "kind": "choice", "options": names}])
            prev = state["items"].get(key)
            if prev and prev.get("notes"):
                item["notes"] = prev["notes"]
            state["items"][key] = item
            w["moves"][g["id"]] = key
            w["serves"][g["id"]] = bool(entry.get("serves_priority"))
            w["related"][g["id"]] = [
                k for k in (entry.get("related") or []) if k in state["items"] and k != key
            ][:20]
        w.pop("error", None)
        for g in goals:
            w["pending"].pop(g["id"], None)  # its move arrived (or none came): no placeholder
        w["planned_at"] = store.now_iso()
        return len(planned)

    return int(store.mutate(change))


def plan_soon(force: bool = False) -> None:
    threading.Thread(target=plan, kwargs={"force": force}, daemon=True).start()


def move_done(key: str) -> None:
    """The user did this move: it goes on the goal's record and the next move is worked out."""
    item = store.load()["items"].get(key) or {}
    gid = item.get("weekly_goal")
    note = current()
    if not gid or not note:
        return

    def change(state: dict[str, Any]) -> None:
        w = week_state(state, note["week"])
        w["steps_done"].setdefault(gid, []).append(item.get("title", ""))
        w["pending"][gid] = store.now_iso()

    store.mutate(change)
    plan_soon()


# A goal's slot in Now waits this long for its next move, showing a placeholder; after
# that the slot goes to the next thing by priority until the move arrives.
PENDING_SECONDS = 60


def mark_pending(gids: list[str]) -> None:
    note = current()
    if not note:
        return

    def change(state: dict[str, Any]) -> None:
        w = week_state(state, note["week"])
        for gid in gids:
            w["pending"][gid] = store.now_iso()

    store.mutate(change)


def is_pending(w: dict[str, Any], gid: str, now: datetime | None = None) -> bool:
    at = (w.get("pending") or {}).get(gid)
    if not at:
        return False
    try:
        age = ((now or datetime.now()) - datetime.fromisoformat(str(at)[:19])).total_seconds()
    except ValueError:
        return False
    return age < PENDING_SECONDS


def placeholder(goal: dict[str, Any]) -> dict[str, Any]:
    """The quiet row holding a goal's place in Now while its next move is worked out."""
    return {
        "key": f"pending:{goal['id']}",
        "source": "goal",
        "placeholder": True,
        "status": "working",
        "state": "mine",
        "who": "",
        "weekly_goal": goal["id"],
        "goal_text": goal["text"],
        "title": f"Working out the next step for {goal['text']}...",
        "asked_at": store.now_iso(),
    }


# ---- sending a goal's message ----------------------------------------------------


def target(item: dict[str, Any]) -> dict[str, Any]:
    """Where Send would put a goal's message: the chosen person's Slack DM, else an email."""
    to = item.get("send_to") or {}
    if to.get("slack_id"):
        return {
            "can_send": True,
            "label": f"Send to {to['name']}",
            "detail": f"Sends as you in your Slack DM with {to.get('full') or to['name']}.",
            "slack_id": to["slack_id"],
        }
    if to.get("email"):
        return {
            "can_send": True,
            "label": f"Send to {to['name']}",
            "detail": f"Sends as you by email to {to['email']}, from {config.default_account()}.",
            "email": to["email"],
        }
    return {"can_send": False}


def send(item: dict[str, Any], text: str) -> str:
    t = target(item)
    if t.get("slack_id"):
        opened = connectors.slack_open_dm(t["slack_id"])
        connectors.slack_post_message(opened, text, None)
    else:
        subject = re.sub(r"\s+", " ", item.get("title", ""))[:80]
        connectors.gmail_send_new(config.default_account(), t["email"], "", subject, text)
    return t["label"].replace("Send", "Sent")


def resolve_recipient(item: dict[str, Any], name: str) -> dict[str, str]:
    """The chosen person, with an email from the user's mail when they are not on Slack."""
    people = (item.get("move") or {}).get("people") or []
    chosen = dict(next((p for p in people if p["name"] == name), None) or person(name, slack_people()))
    if not chosen.get("slack_id") and not chosen.get("email"):
        chosen["email"] = connectors.find_address(chosen.get("full") or name, config.accounts())
    return chosen


# ---- notes typed into a "go do" move -------------------------------------------------


def notes_path(item: dict[str, Any]) -> Path:
    words = re.sub(r"[^A-Za-z0-9 ]+", "", item.get("goal_text", "notes"))
    short = " ".join(words.split()[:6]) or "notes"
    week = item["key"].split(":")[1] if item["key"].count(":") >= 2 else week_start().isoformat()
    return WEEKLY_DIR / "Notes" / f"{week} {short}.md"


def save_notes(key: str, text: str) -> str:
    item = store.load()["items"][key]
    path = notes_path(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {item.get('goal_text', '')}\n\n{item.get('title', '')}\n\n{text}\n")
    store.mutate(lambda s: s["items"][key].update(notes=text))
    return str(path)


# ---- what the page shows ----------------------------------------------------------


def status_line(goal: dict[str, Any], w: dict[str, Any], items: dict[str, dict[str, Any]], docs: list[dict[str, str]]) -> str:
    """A short status at the right of a goal, only when something real stands behind it."""
    ready = [
        k for k in w["related"].get(goal["id"], [])
        if items.get(k, {}).get("status") in ("open", "working") and items[k].get("state") == "ready"
    ]
    if ready:
        return f"{len(ready)} {'message' if len(ready) == 1 else 'messages'} ready"
    if doc_for(goal["text"], docs):
        return "draft in progress"
    return ""


def now_goals(note: dict[str, Any], w: dict[str, Any]) -> list[dict[str, Any]]:
    """The goals whose moves fill Now, in the user's own order: not done, and not on one of the
    user's travel days. A stretch goal is treated like any other (the user, 2026-11-13: the user puts
    stretch goals at the bottom of the user's note themselves)."""
    return [
        g
        for g in note["goals"]
        if g["id"] not in w["done"] and not away_today(g)
    ]


def view(state: dict[str, Any]) -> dict[str, Any] | None:
    """The This-week box, and the moves Now can take, in order."""
    note = current()
    if not note:
        return None
    w = (state.get("weekly") or {}) if (state.get("weekly") or {}).get("week") == note["week"] else {}
    with conversations.opened(store.DATA_DIR) as db:
        tasks = sync_priorities(db, note)  # a new note's goals become this week's tasks
    done = {gid: c["updated_at"] for gid, c in tasks.items() if c["stage"] == "done"}
    w = {"done": done, "pending": w.get("pending", {}), "moves": w.get("moves", {}), "related": w.get("related", {}), "serves": w.get("serves", {}), "steps_done": w.get("steps_done", {}), "error": w.get("error", "")}
    items = state["items"]
    docs = writing_docs()
    def live(g: dict[str, Any]) -> dict[str, Any] | None:
        item = items.get(w["moves"].get(g["id"]) or "")
        return item if item and item["status"] in ("open", "working") else None

    moves = []
    for g in now_goals(note, w):
        m = live(g)
        if m and not m.get("not_now"):
            moves.append(m)
        elif not m and is_pending(w, g["id"]):
            moves.append(placeholder(g))
    goal_of = {k: g for g in note["goals"] for k in w["related"].get(g["id"], [])}
    return {
        "week": note["week"],
        "text": note["text"],
        "path": note["path"],
        "priority": note["priority"],
        "focus": note["focus"],
        "goals": [
            dict(
                g,
                done=g["id"] in w["done"],
                status=status_line(g, w, items, docs),
                away=away_today(g),
                move=live(g),
            )
            for g in note["goals"]
        ],
        "moves": moves,
        # A goal that should be in Now has no move yet and none is being worked out.
        "needs_plan": any(not live(g) and not is_pending(w, g["id"]) for g in now_goals(note, w)),
        "related": {k: g["text"] for k, g in goal_of.items()},
        "error": w["error"],
    }
