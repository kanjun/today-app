"""Planning: the user's own next steps, Week's list and placed mornings, Inbox captures, Someday.

The user's rules (2026-11-12/13), which this module encodes:

- Next steps are the user's. A step is stored only when the user writes it (or takes a suggestion with
  "Use it"); the AI's move stays a labelled suggestion. Steps added after the current one
  queue up ("then: ...") and the first becomes the step when the user checks the current one.
- Nothing is sized. Week's list is the user's weekly priorities plus only what the user chose with
  "Plan in Week" (a to-do, an Inbox capture, a Someday bullet).
- One big thing per morning. A placed morning is a Google Calendar event on the user's primary
  calendar (no guests); moving or retiming updates it, taking it off deletes it.
- Mornings run 8:00 to 13:00. The slot is the longest free stretch, read from the saved
  calendar copy (``raw/calendar.json``). The user's WRITE / DNS / DO NOT SCHEDULE holds count as
  open time. Times follow the user's Google Calendar's zone, switching after a "Flight to <city>".
- Inbox holds only the user's own captures. Today files each one (a to-do, or a Someday bullet);
  never into Week on its own.
- Someday is one markdown file of nested bullets (the ``someday_file`` setting).

Everything the user writes is stored verbatim.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
from collections.abc import Generator
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from today import (
    actions,
    ai,
    calendar_events,
    commitments,
    config,
    connectors,
    due_dates,
    project_book,
    projects,
    store,
    task_steps,
    user_projects,
    weekly,
)

log = logging.getLogger(__name__)

SOMEDAY_FILE = config.load().someday_file  # the someday_file setting

AM_START = "08:00"
AM_END = "13:00"
# A morning with less free time than this is shown as tight (the user can still place one).
MIN_MINUTES = 60
DEFAULT_ZONE = calendar_events.ZONE_NAME  # the timezone setting, until the calendar says its own
CITY_ZONE = [
    (re.compile(r"houston|austin|dallas|\biah\b|\bhou\b", re.I), "America/Chicago"),
    (re.compile(r"\b(sfo|san francisco|oakland|oak|sjc|san jose|los angeles|lax|seattle|sea)\b", re.I), "America/Los_Angeles"),
    (re.compile(r"tampa|clearwater|\btpa\b|miami|boston|\bbos\b", re.I), "America/New_York"),
    (re.compile(r"\b(jfk|lga|ewr|new york|nyc)\b", re.I), "America/New_York"),
]
ZONE_NAME = {
    "America/Chicago": "Central",
    "America/Los_Angeles": "Pacific",
    "America/New_York": "Eastern",
    "America/Denver": "Mountain",
}
HOLD = re.compile(r"^(WRITE|DNS)\b|do not schedule", re.I)
NOT_BLOCKING = re.compile(r"^(canceled|cancelled)\b|^office closed", re.I)


class PlanningError(Exception):
    """Something the user asked for can't be done as asked; the message says why, in plain words."""


# Everything here is a task record (a commitment): a weekly priority is a task of the user's with
# its week set, "Plan in Week" sets a task's week, the user's steps are the task's checklist (the
# same steps a project page shows), a placed morning is a block on the task, and an Inbox
# capture is a task that remembers it was captured.
#
# A ref names what the page means: "goal:<weekly goal id>" (this week's note), "todo:<item
# key>" (the task's id for a task with no item; "c:<id>" as project pages write it) or
# "someday:<bullet id>".


def _opened() -> Any:
    return project_book.opened()  # its undo table too: a step change is undoable like any other


def ref_of(c: dict[str, Any]) -> str:
    d = c["detail"]
    if d.get("week_goal"):
        return f"goal:{d['week_goal']}"
    if d.get("someday") and not c["legacy_key"]:
        return f"someday:{d['someday']}"
    return "todo:" + (c["legacy_key"] or c["id"])  # the key Today's lists give the task


def _someday_task(db: sqlite3.Connection, sid: str) -> dict[str, Any] | None:
    r = db.execute(
        "SELECT id FROM commitments WHERE json_extract(detail, '$.someday') = ? AND stage IN ('open', 'parked') "
        "ORDER BY created_at DESC LIMIT 1", (sid,),
    ).fetchone()
    return commitments.get(db, r[0]) if r else None


def task_of(db: sqlite3.Connection, ref: str, make: bool = True) -> dict[str, Any] | None:
    """The task a ref names; a to-do that has no task record yet gets one (``make``)."""
    kind, _, rid = ref.partition(":")
    if kind == "goal":
        note = weekly.current()
        return weekly.sync_priorities(db, note).get(rid) if note else None
    if kind == "todo":
        if rid.startswith("c:"):
            return commitments.get(db, rid[2:])
        c = commitments.by_legacy_key(db, rid) or commitments.get(db, rid)
        if c is None and make:
            try:
                c = project_book.ensure_commitment(db, rid)
            except project_book.ProjectError:
                return None
        return c
    if kind == "someday":
        return _someday_task(db, rid)
    return None


def _need(db: sqlite3.Connection, ref: str) -> dict[str, Any]:
    c = task_of(db, ref)
    if c is None:
        raise PlanningError("That isn't on your lists any more")
    return c


def step_view(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """A task's checklist as the page's next step: the first step not done, the ones after
    it, and the ones the user checked off."""
    open_ = [x for x in steps if x["stage"] == "open"]
    done = [{"text": x["title"], "at": x["updated_at"]} for x in steps if x["stage"] == "done"]
    at = max([x["updated_at"] for x in steps], default="")
    return {"text": open_[0]["title"] if open_ else "", "then": [x["title"] for x in open_[1:]], "done": done, "at": at,
            "ids": [x["id"] for x in open_]}


def _steps_of(db: sqlite3.Connection, cid: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT id FROM commitments WHERE parent_id = ? AND labels LIKE '%\"step\"%' AND stage IN ('open', 'done') "
        "ORDER BY position, created_at", (cid,),
    ).fetchall()
    return [c for r in rows if (c := commitments.get(db, r[0])) is not None]


def summary() -> dict[str, Any]:
    """What the main page needs: the user's steps by ref, which to-dos are on Week's list, and which
    Someday bullets the user planned in Week."""
    with _opened() as db:
        every = commitments.all_commitments(db)
        note = weekly.current()
        if note:
            weekly.sync_priorities(db, note)
            every = commitments.all_commitments(db)
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for c in every:
        if project_book.is_step(c) and c["stage"] in ("open", "done") and c["parent_id"]:
            by_parent.setdefault(c["parent_id"], []).append(c)
    week = note["week"] if note else ""
    steps, in_week, someday_week = {}, {}, {}
    for c in every:
        if project_book.is_step(c) or c["stage"] not in ("open", "parked", "done"):
            continue
        if c["detail"].get("week_goal") and c["week"] != week:
            continue  # an earlier week's priority
        mine = sorted(by_parent.get(c["id"], []), key=lambda x: (x["position"], x["created_at"]))
        ref = ref_of(c)
        if mine:
            steps[ref] = {k: v for k, v in step_view(mine).items() if k != "ids"}
        if c["week"] and not c["detail"].get("week_goal") and c["stage"] == "open":
            if ref.startswith("someday:"):
                someday_week[ref[8:]] = {"text": c["title"], "at": c["updated_at"]}
            else:
                in_week[ref[5:]] = c["detail"].get("in_week_at") or c["updated_at"]
    return {"steps": steps, "in_week": in_week, "someday_week": someday_week}


def _ref_steps(db: sqlite3.Connection, ref: str) -> dict[str, Any]:
    c = task_of(db, ref, make=False)
    return {k: v for k, v in step_view(_steps_of(db, c["id"]) if c else []).items() if k != "ids"}


# ---- the user's next steps: the task's checklist ----------------------------------------------


def set_step(ref: str, text: str) -> dict[str, Any]:
    """The user's next step, as the user wrote it: the task's first open step is rewritten (or made);
    clearing it takes that step off."""
    text = text.strip()
    with _opened() as db:
        c = _need(db, ref)
        current = step_view(_steps_of(db, c["id"]))["ids"]
        if current:
            task_steps.edit(db, current[0], text)  # an empty one removes the step
        elif text:
            task_steps.add(db, c["id"], text)  # none open: it goes after the done ones, as the step
        return _ref_steps(db, ref)


def add_then(ref: str, text: str) -> dict[str, Any]:
    """A step after the ones the user has. With none open it becomes the step."""
    text = text.strip()
    with _opened() as db:
        c = _need(db, ref)
        if text:
            task_steps.add(db, c["id"], text)
        return _ref_steps(db, ref)


def step_done(ref: str) -> dict[str, Any]:
    """The user checked the current step: it is done and the next one becomes the step."""
    with _opened() as db:
        c = _need(db, ref)
        current = step_view(_steps_of(db, c["id"]))["ids"]
        if current:
            task_steps.tick(db, current[0], True)
        return _ref_steps(db, ref)


# ---- Week's list ---------------------------------------------------------------------


def _monday(day: date | None = None) -> str:
    day = day or date.today()
    return (day - timedelta(days=day.weekday())).isoformat()


def set_in_week(key: str, on: bool) -> None:
    with _opened() as db:
        c = _need(db, "todo:" + key)
        if on:
            commitments.change(db, c["id"], "user", {"week": _monday(), "detail": dict(c["detail"], in_week_at=store.now_iso())},
                               reason="the user planned it in Week")
        elif c["week"]:
            detail = {k: v for k, v in c["detail"].items() if k != "in_week_at"}
            commitments.change(db, c["id"], "user", {"week": "", "detail": detail}, reason="the user took it off Week")


def someday_id(text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha1(norm.encode()).hexdigest()[:10]


def set_someday_week(text: str, on: bool) -> str:
    """A Someday bullet planned in Week is a task of the user's for the week; taken off, it goes
    back to Someday (the bullet itself stays in the user's outline either way)."""
    sid = someday_id(text)
    with _opened() as db:
        c = _someday_task(db, sid)
        if on:
            if c is None:
                commitments.create(
                    db, "user",
                    {"title": text.strip(), "direction": "self", "origin": "user_chose", "started_by": "user",
                     "week": _monday(), "detail": {"someday": sid, "manual": True, "asked_at": store.now_iso()}},
                    reason="the user planned a Someday bullet in Week",
                )
            else:
                commitments.change(db, c["id"], "user", {"week": _monday(), "stage": "open", "stage_reason": ""},
                                   reason="the user planned it in Week")
        elif c is not None:
            commitments.change(db, c["id"], "user", {"week": "", "stage": "parked", "stage_reason": "Someday"},
                               reason="the user took it off Week")
    return sid


# ---- the calendar: events in the user's zone, the free morning ------------------------------


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def calendar_record() -> dict[str, Any]:
    return store.load_raw(calendar_events.KEY) or {}


def base_zone(record: dict[str, Any] | None = None) -> str:
    record = calendar_record() if record is None else record
    zone = str(record.get("tz") or "")
    try:
        ZoneInfo(zone)
        return zone
    except (ValueError, KeyError):
        return DEFAULT_ZONE


def flights(record: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """Flights still ahead (landing after now), with the zone of the city they land in."""
    now = now or datetime.now(timezone.utc)
    out = []
    for e in record.get("events") or []:
        title = str(e.get("summary") or "")
        if not re.match(r"\s*flight to\b", title, re.I):
            continue
        dest = re.sub(r"^\s*flight to\s*", "", title, flags=re.I)
        zone = next((z for rx, z in CITY_ZONE if rx.search(dest)), None)
        end = _parse(((e.get("end") or {}).get("dateTime")) or "")
        if zone and end and end > now:
            out.append({"at": end, "zone": zone, "title": title, "day": end.date().isoformat()})
    return sorted(out, key=lambda f: f["at"])


def zone_on(day: str, record: dict[str, Any], now: datetime | None = None) -> str:
    """The zone the user is in on that day: the user's calendar's current zone, or where a flight before
    or on that day lands."""
    zone = base_zone(record)
    for f in flights(record, now):
        if f["day"] <= day:
            zone = f["zone"]
    return zone


def _response(e: dict[str, Any]) -> str:
    me = next((a for a in e.get("attendees") or [] if a.get("self")), None)
    return str((me or {}).get("responseStatus") or "")


def events_on(day: str, record: dict[str, Any], skip_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """That day's events with local times (in the zone the user is in that day)."""
    zone = ZoneInfo(zone_on(day, record))
    out = []
    for e in record.get("events") or []:
        if e.get("status") == "cancelled" or e.get("id") in (skip_ids or set()):
            continue
        start, end = e.get("start") or {}, e.get("end") or {}
        title = str(e.get("summary") or "(no title)")
        base = {
            "id": e.get("id", ""),
            "title": title,
            "resp": _response(e),
            "free": e.get("transparency") == "transparent",
            "url": e.get("htmlLink", ""),
        }
        if "date" in start:
            first = str(start["date"])
            last = str(end.get("date") or first)  # exclusive
            if first <= day < last or first == day:
                out.append(dict(base, all_day=True, start="", end=""))
            continue
        s, f = _parse(str(start.get("dateTime") or "")), _parse(str(end.get("dateTime") or ""))
        if not s or not f:
            continue
        s, f = s.astimezone(zone), f.astimezone(zone)
        if (f - s) >= timedelta(hours=23):
            # A many-day event ending in the small hours doesn't cover that last morning.
            last = (f - timedelta(hours=6)).date().isoformat()
            if s.date().isoformat() <= day <= last:
                out.append(dict(base, all_day=True, start="", end=""))
            continue
        if s.date().isoformat() != day:
            continue
        out.append(dict(base, all_day=False, start=s.strftime("%H:%M"), end=f.strftime("%H:%M") if f.date() == s.date() else "23:59"))
    return sorted(out, key=lambda e: (not e["all_day"], e["start"]))


def _mins(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _hhmm(n: int) -> str:
    return f"{n // 60:02d}:{n % 60:02d}"


def blocking(e: dict[str, Any]) -> bool:
    return (
        not e["all_day"]
        and not e["free"]
        and e["resp"] not in ("declined", "needsAction")
        and not NOT_BLOCKING.search(e["title"])
        and not HOLD.search(e["title"])
    )


def away_reason(evs: list[dict[str, Any]]) -> str:
    trip = next((e for e in evs if re.search(r"\bflight\b", e["title"], re.I)), None) or next(
        (e for e in evs if e["all_day"] and re.search(r"stay at|hotel|wedding", e["title"], re.I)), None
    )
    if trip:
        return "Away: " + re.split(r"\s*(?:conf#|\(|\[)", trip["title"], maxsplit=1, flags=re.I)[0]
    conf = next((e for e in evs if e["all_day"] and re.search(r"\bconf\b|conference", e["title"], re.I)), None)
    if conf:
        return "Conference: " + re.split(r"\s*[\[\]]", conf["title"], maxsplit=1)[0]
    return ""


def morning(evs: list[dict[str, Any]]) -> dict[str, Any]:
    """The longest free stretch between 8:00 and 13:00, what ends it, and notes."""
    lo, hi = _mins(AM_START), _mins(AM_END)
    busy = sorted(
        (max(_mins(e["start"]), lo), min(_mins(e["end"]), hi), e["title"])
        for e in evs
        if blocking(e)
    )
    busy = [b for b in busy if b[0] < b[1]]
    cur, best = lo, None
    for s, f, title in busy:
        if s - cur > 0 and (best is None or s - cur > best[1] - best[0]):
            best = (cur, s, title)
        cur = max(cur, f)
    if hi - cur > 0 and (best is None or hi - cur > best[1] - best[0]):
        best = (cur, hi, "")
    timed = [e for e in evs if not e["all_day"]]
    unanswered = [e for e in timed if e["resp"] == "needsAction" and e["start"] < AM_END and e["end"] > AM_START]
    hold = next((e for e in timed if HOLD.search(e["title"]) and e["start"] < AM_END), None)
    length = best[1] - best[0] if best else 0
    return {
        "start": _hhmm(best[0]) if best else None,
        "end": _hhmm(best[1]) if best else None,
        "len": length,
        "until": best[2] if best else "",
        "unanswered": [{"title": e["title"], "start": e["start"]} for e in unanswered],
        "hold": {"title": re.split(r"\s*\(", hold["title"])[0], "start": hold["start"], "end": hold["end"]} if hold else None,
        "away": away_reason(evs),
        "too_short": length < MIN_MINUTES,
    }


def zone_line(days: list[str], record: dict[str, Any]) -> str:
    zones = [zone_on(d, record) for d in days]
    base = base_zone(record)
    name = lambda z: ZONE_NAME.get(z, z)  # noqa: E731
    fl = flights(record)
    if len(set(zones)) == 1:
        z = zones[0]
        if z == base:
            return f"Times in {name(z)} time, your Google Calendar's time zone"
        f = next((f for f in reversed(fl) if f["zone"] == z and f["day"] <= days[0]), None)
        where = f" after {re.split(r'\s*\(', f['title'])[0]} on {_nice(f['day'])}" if f else ""
        return f"Times in {name(z)} time, where you'll be{where}"
    parts, cur = [], None
    for d, z in zip(days, zones):
        if z != cur:
            parts.append(f"{name(z)} from {_weekday(d)}")
            cur = z
    f = next((f for f in fl if f["zone"] == zones[-1] and f["zone"] != zones[0]), None)
    return "Times in " + ", then ".join(parts) + (f" (after {re.split(r'\s*\(', f['title'])[0]})" if f else "")


def _weekday(day: str) -> str:
    return date.fromisoformat(day).strftime("%a")


def _nice(day: str) -> str:
    return date.fromisoformat(day).strftime("%a, %b %-d")


# ---- placing a big thing on a morning ------------------------------------------------


def _event_body(title: str, day: str, start: str, end: str, zone: str) -> dict[str, Any]:
    return {
        "summary": title,
        "start": {"dateTime": f"{day}T{start}:00", "timeZone": zone},
        "end": {"dateTime": f"{day}T{end}:00", "timeZone": zone},
        "description": "Your one big thing for this morning, planned in Today's Week.\n\nAdded from Today.",
    }


def _cal_create(body: dict[str, Any]) -> str:
    made = connectors._curl(
        ["-X", "POST", "-H", "Content-Type: application/json", "-d", json.dumps(body), f"{calendar_events.EVENTS}?sendUpdates=none"]
    )
    if "error" in made or not made.get("id"):
        raise connectors.ConnectorError(f"calendar: {str(made.get('error'))[:200]}")
    return str(made["id"])


def _cal_update(event_id: str, body: dict[str, Any]) -> None:
    made = connectors._curl(
        ["-X", "PATCH", "-H", "Content-Type: application/json", "-d", json.dumps(body), f"{calendar_events.EVENTS}/{event_id}?sendUpdates=none"]
    )
    if "error" in made:
        raise connectors.ConnectorError(f"calendar: {str(made.get('error'))[:200]}")


def _cal_delete(event_id: str) -> None:
    try:
        made = connectors._curl(["-X", "DELETE", f"{calendar_events.EVENTS}/{event_id}?sendUpdates=none"])
    except connectors.ConnectorError as e:
        if str(e).strip() == "not JSON:":  # Google answers a delete with an empty 204
            return
        raise
    err = made.get("error")
    if err and (err.get("code") if isinstance(err, dict) else 0) not in (404, 410):
        raise connectors.ConnectorError(f"calendar: {str(err)[:200]}")


def default_times(day: str, record: dict[str, Any] | None = None, skip: set[str] | None = None) -> tuple[str, str]:
    record = calendar_record() if record is None else record
    m = morning(events_on(day, record, placed_event_ids() if skip is None else skip))
    if m["start"] and m["len"] >= 30:
        return m["start"], m["end"]
    return "08:00", "10:00"


# A placed morning is a block on its task: ``detail.blocks``, each {day, title, start, end,
# zone, event_id, at}. A morning's blocks run in time order: the first is its big thing, any
# after it were stacked under a finished one.


def _blocks(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute("SELECT id FROM commitments WHERE json_extract(detail, '$.blocks[0]') IS NOT NULL").fetchall()
    out = []
    for r in rows:
        c = commitments.get(db, r[0])
        if c is None:
            continue
        for b in c["detail"].get("blocks") or []:
            out.append(dict(b, ref=ref_of(c), cid=c["id"]))
    return sorted(out, key=lambda b: (b["day"], b["start"]))


def all_placed(blocks: list[dict[str, Any]] | None = None) -> Generator[tuple[str, dict[str, Any]], None, None]:
    """Every block, as (day, block), each morning's in time order."""
    if blocks is None:
        with _opened() as db:
            blocks = _blocks(db)
    for b in blocks:
        yield b["day"], b


def placed_event_ids() -> set[str]:
    return {str(p.get("event_id")) for _, p in all_placed() if p.get("event_id")}


def finished(ref: str, db: sqlite3.Connection | None = None) -> bool:
    """Is the big thing itself done (the user's weekly goal ticked, the to-do finished)?"""
    if db is None:
        with _opened() as own:
            return finished(ref, own)
    if ref.startswith("todo:") and store.load()["items"].get(ref[5:], {}).get("status") in FINISHED:
        return True
    c = task_of(db, ref, make=False)
    return bool(c) and c["stage"] in ("done", "declined", "stale")  # type: ignore[index]


def _stack_times(day: str, under: list[dict[str, Any]], record: dict[str, Any], skip: set[str]) -> tuple[str, str]:
    """A block stacked under finished ones runs from the end of the last of them (or now, on
    today) to the end of the free morning, and at least an hour and a half."""
    start = max(p["end"] for p in under)
    now = datetime.now(ZoneInfo(zone_on(day, record)))  # the clock where that morning is
    if day == now.date().isoformat():
        start = max(start, _hhmm((now.hour * 60 + now.minute + 4) // 5 * 5))
    _, end = default_times(day, record, skip)
    if _mins(end) < _mins(start) + 30:
        end = _hhmm(min(_mins(start) + 90, 23 * 60 + 55))
    return start, end


def _set_blocks(db: sqlite3.Connection, cid: str, change: Any, reason: str) -> None:
    c = commitments.get(db, cid)
    if c is None:
        return
    blocks = change(list(c["detail"].get("blocks") or []))
    commitments.change(db, cid, "user", {"detail": dict(c["detail"], blocks=blocks)}, reason=reason)


def _entry(b: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in b.items() if k not in ("ref", "cid", "day")}


def place(ref: str, title: str, day: str, start: str = "", end: str = "") -> dict[str, Any]:
    """Put ``ref`` on ``day``'s morning as a calendar event. One big thing a morning: an
    unfinished one there comes off. A finished one is never replaced; the new one stacks
    under it (the user, 2026-11-13). Already placed elsewhere this week: it moves, and its
    event is updated."""
    record = calendar_record()
    monday = (date.fromisoformat(day) - timedelta(days=date.fromisoformat(day).weekday())).isoformat()
    week_days = {(date.fromisoformat(monday) + timedelta(days=n)).isoformat() for n in range(7)}
    with _opened() as db:
        c = _need(db, ref)
        blocks = _blocks(db)
        skip = {str(b.get("event_id")) for b in blocks if b.get("event_id")}
        old = next((b for b in blocks if b["cid"] == c["id"] and b["day"] in week_days and b["day"] != day), None)
        event_id = old.get("event_id", "") if old else ""
        that_day = [b for b in blocks if b["day"] == day]
        first, stacked = (that_day[0], that_day[1:]) if that_day else (None, [])
        same = next((b for b in that_day if b["cid"] == c["id"]), None)
        stack = bool(first) and not same and finished(first["ref"], db)  # type: ignore[index]
        if stack:
            done_ones = [first, *[b for b in stacked if finished(b["ref"], db)]]
            bumped = next((b for b in stacked if not finished(b["ref"], db)), None)
            if not start or not end:
                start, end = _stack_times(day, done_ones, record, skip)  # type: ignore[arg-type]
        else:
            bumped = first if first and not same else None
            if not start or not end:
                start, end = default_times(day, record, skip)
        if _mins(end) <= _mins(start):
            raise PlanningError("The block has to end after it starts")
        zone = zone_on(day, record)
        if bumped and bumped.get("event_id"):
            _cal_delete(bumped["event_id"])
        if same:
            event_id = same.get("event_id", "")
        body = _event_body(title, day, start, end, zone)
        if event_id:
            _cal_update(event_id, body)
        else:
            event_id = _cal_create(body)
        entry = {"day": day, "title": title, "start": start, "end": end, "zone": zone, "event_id": event_id, "at": store.now_iso()}
        if bumped:
            _set_blocks(db, bumped["cid"], lambda bs: [b for b in bs if b["day"] != day], "another big thing took the morning")
        _set_blocks(db, c["id"], lambda bs: [b for b in bs if b["day"] != day and (not old or b["day"] != old["day"])] + [entry],
                    f"the user put it on the morning of {day}")
    return dict(_entry(entry), ref=ref, stacked=stack)


def _find(db: sqlite3.Connection, day: str, ref: str = "") -> dict[str, Any] | None:
    return next((b for b in _blocks(db) if b["day"] == day and (not ref or b["ref"] == ref)), None)


def retime(day: str, start: str, end: str, ref: str = "") -> dict[str, Any]:
    if _mins(end) <= _mins(start):
        raise PlanningError("The block has to end after it starts")
    with _opened() as db:
        p = _find(db, day, ref)
        if not p:
            raise PlanningError("Nothing is planned that morning")
        zone = zone_on(day, calendar_record())
        if p.get("event_id"):
            _cal_update(p["event_id"], {k: v for k, v in _event_body(p["title"], day, start, end, zone).items() if k in ("start", "end")})
        _set_blocks(db, p["cid"], lambda bs: [dict(b, start=start, end=end, zone=zone) if b["day"] == day else b for b in bs],
                    "the user moved the block's time")
    return dict(_entry(p), ref=p["ref"], start=start, end=end, zone=zone)


def unplace(day: str, ref: str = "") -> dict[str, Any] | None:
    with _opened() as db:
        p = _find(db, day, ref)
        if not p:
            return None
        if p.get("event_id"):
            _cal_delete(p["event_id"])
        _set_blocks(db, p["cid"], lambda bs: [b for b in bs if b["day"] != day], "the user took it off the morning")
    return dict(_entry(p), ref=p["ref"])


# ---- the Week view -------------------------------------------------------------------


def _goal_items(week: dict[str, Any] | None, steps: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for g in (week or {}).get("goals") or []:
        if g.get("done"):
            continue
        st = steps.get(f"goal:{g['id']}") or {}
        out.append(
            {
                "ref": f"goal:{g['id']}",
                "kind": "goal",
                "text": g["text"],
                "step": st.get("text", ""),
                "then": st.get("then", []),
                "suggested": ((g.get("move") or {}).get("title") or "") if not st.get("text") else "",
                "stretch": bool(g.get("stretch")),
            }
        )
    return out


def week_list(week: dict[str, Any] | None, items: dict[str, dict[str, Any]], plan: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    plan = plan or summary()
    steps = plan["steps"]
    also = []
    with _opened() as db:
        for key in plan["in_week"]:
            c = task_of(db, "todo:" + key, make=False)
            it = items.get(key)
            if c is None or c["stage"] != "open" or (it and it.get("status") not in ("open", "working", "new")):
                continue
            st = steps.get(f"todo:{key}") or {}
            title = c["title"]
            also.append(
                {
                    "ref": f"todo:{key}",
                    "kind": "todo",
                    "text": st.get("text") or title,
                    "then_todo": title if st.get("text") else "",
                    "step": st.get("text", ""),
                    "then": st.get("then", []),
                    "key": key,
                }
            )
    for sid, s in plan["someday_week"].items():
        st = steps.get(f"someday:{sid}") or {}
        also.append({"ref": f"someday:{sid}", "kind": "someday", "text": s["text"], "step": st.get("text", ""), "then": st.get("then", [])})
    return {"goals": _goal_items(week, steps), "also": also}


FINISHED = ("done", "sent", "cleared", "skipped")


def resolve(
    ref: str,
    lists: dict[str, list[dict[str, Any]]],
    placed: dict[str, Any],
    week: dict[str, Any] | None = None,
    items: dict[str, dict[str, Any]] | None = None,
    steps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What a placed block is, from the current lists; a goal the user has since ticked or
    rewritten still shows by its saved title. ``done`` is true once the big thing itself is
    finished (the goal ticked, the to-do done), and ``done_steps`` lists the steps the user checked
    off that day, so the morning block can celebrate it."""
    found = next((it for it in lists["goals"] + lists["also"] if it["ref"] == ref), None)
    kind, _, rid = ref.partition(":")
    out = dict(found) if found else {"ref": ref, "kind": kind, "text": placed.get("title", ""), "step": "", "then": []}
    if kind == "goal":
        goal = next((g for g in (week or {}).get("goals") or [] if g["id"] == rid), None)
        out["done"] = bool(goal and goal.get("done"))
        if goal and not found:
            out["text"] = goal["text"]
    elif kind == "todo":
        out["done"] = (items or {}).get(rid, {}).get("status") in FINISHED or finished(ref)
    else:
        out["done"] = False
    day = str(placed.get("day") or "")
    out["done_steps"] = [d["text"] for d in ((steps or {}).get(ref) or {}).get("done") or [] if not day or str(d.get("at", "")).startswith(day)]
    return out


def week_view(start: str, week: dict[str, Any] | None, items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    monday = date.fromisoformat(start)
    monday -= timedelta(days=monday.weekday())
    days = [(monday + timedelta(days=n)).isoformat() for n in range(7)]
    record = calendar_record()
    plan = summary()
    lists = week_list(week, items, plan)
    with _opened() as db:
        blocks = _blocks(db)
    skip = {str(b.get("event_id")) for b in blocks if b.get("event_id")}
    out_days = []
    for d in days:
        evs = events_on(d, record, skip)
        mine = [dict(_entry(b), ref=b["ref"]) for b in blocks if b["day"] == d]
        out_days.append(
            {
                "date": d,
                "zone": zone_on(d, record),
                "morning": morning(evs),
                "events": [e for e in evs if not e["all_day"] and e["resp"] != "declined" and not e["free"]],
                "placed": dict(mine[0], item=resolve(mine[0]["ref"], lists, dict(mine[0], day=d), week, items, plan["steps"])) if mine else None,
                "stacked": [dict(q, item=resolve(q["ref"], lists, dict(q, day=d), week, items, plan["steps"])) for q in mine[1:]],
            }
        )
    placed_refs = {b["ref"]: b["day"] for b in blocks if b["day"] in days}
    return {
        "start": days[0],
        "today": datetime.now().date().isoformat(),
        "days": out_days,
        "zone_line": zone_line(days, record),
        "calendar_at": record.get("fetched_at", ""),
        "list": lists,
        "placed_refs": placed_refs,
    }


# ---- Inbox ---------------------------------------------------------------------------

GUESS_SYSTEM = """The user jots things on the user's phone. Read each capture and answer with one JSON object:
{"dest": "today" or "someday", "due": "YYYY-MM-DD" or "", "project": "<id>" or ""}.
- "dest": "today" for something the user has to do (a task, an errand, someone to reply to or follow up with), even if not today; "someday" for an idea, a wish, something that would be nice to do or explore later, with no commitment.
- "due": the last day doing it is still useful, read only from the user's words. "by Friday" is that Friday; something to set up for an event ("ping Peter about running church this weekend") is due before the event, so the day before it starts; "tomorrow", "next week" (its Monday), "end of month" as said. "" when the user's words name no time; never invent one. Today is {TODAY}.
- "project": the id of one of the user's projects below only when the capture is clearly part of it, else "".
{PROJECTS}"""

SOMEDAY_WORDS = re.compile(r"\b(idea|would be (cool|awesome|great|nice|fun)|someday|essay|podcast|maybe|explore|learn about|what if)\b", re.I)


def read_capture(text: str) -> dict[str, str]:
    """Where a capture goes, the date the user's words give it and the project of the user's it is part of
    (the user, 2026-11-17: "Ping Peter about running church again this weekend" came in with no
    date). One small model call; when it fails, the words rule still files it, undated."""
    today = due_dates.today()
    system = (GUESS_SYSTEM.replace("{TODAY}", f"{today:%A} {today.isoformat()}")
              .replace("{PROJECTS}", user_projects.prompt_lines()))
    out = {"dest": "", "due": "", "project": ""}
    try:
        answer = ai.complete_json(f"Capture: {text}", system=system, model="haiku", timeout=40) or {}
        out = {k: str(answer.get(k) or "") for k in out}
    except (ai.ClaudeError, subprocess.TimeoutExpired) as e:  # the words rule below still files it
        log.warning("inbox guess failed: %s", e)
    if out["dest"] not in ("today", "someday"):
        out["dest"] = "someday" if SOMEDAY_WORDS.search(text) else "today"
    try:
        due = date.fromisoformat(out["due"][:10]) if out["due"] else None
    except ValueError:
        due = None
    out["due"] = due.isoformat() if due is not None and due >= today else ""
    return out


def guess(text: str) -> str:
    return read_capture(text)["dest"]


def _place(db: sqlite3.Connection, c: dict[str, Any], cap: dict[str, Any]) -> None:
    """A to-do capture's date and project, as read from the user's words (``read_capture``). The date
    counts as the user's (the refresh doesn't move it; the user changes it in the picker); the project is
    filed like the reader's filing, and moved out from the task's menu."""
    if cap.get("due"):
        commitments.change(db, c["id"], "user", {"due": cap["due"], "detail": dict(c["detail"], due_set_by="user")},
                           reason=f"the date in the user's words: {cap['due']}")
    p = project_book.Book(db).by_filer_id(cap["project"]) if cap.get("project") else None
    if p is not None and projects.file_or_suggest() == "file":
        project_book.file_into(db, c["id"], p, f"Read from the user's words as part of {p['title']}; filed there by itself. "
                               "Move it out from the task's menu.")


# A capture is the task it became, remembering it was captured (``detail.capture``: its id,
# the user's words, where it went and when). A to-do goes on Today's lists like one from the Add
# box; a Someday capture is a bullet in the user's outline and a task set aside as "Someday".


def _file(db: sqlite3.Connection, cap: dict[str, Any]) -> None:
    """Put a capture where it goes."""
    text, dest = cap["text"], cap["dest"]
    if dest in ("today", "week"):
        key = actions.add_manual(text, pick=False)
        c = project_book.ensure_commitment(db, key)
        detail: dict[str, Any] = dict(c["detail"], capture=dict(cap, ref=key))
        fields: dict[str, Any] = {"detail": detail}
        if dest == "week":
            fields["week"] = _monday()
            detail["in_week_at"] = store.now_iso()
        commitments.change(db, c["id"], "user", fields, reason=f"captured in Inbox, filed to {dest}")
        if not cap.get("moved_at"):
            _place(db, commitments.get(db, c["id"]) or c, cap)
        return
    someday_add_top(text)
    sid = someday_id(text)
    commitments.create(
        db, "user",
        {"title": text, "direction": "self", "origin": "user_chose", "started_by": "user", "stage": "parked",
         "stage_reason": "Someday", "detail": {"someday": sid, "manual": True, "asked_at": store.now_iso(),
                                               "capture": dict(cap, ref="someday:" + sid)}},
        reason="captured in Inbox, filed to Someday",
    )


def _unfile(db: sqlite3.Connection, c: dict[str, Any]) -> dict[str, Any]:
    """Take a capture back out of where it went; returns the capture."""
    cap = dict(c["detail"]["capture"])
    detail = {k: v for k, v in c["detail"].items() if k not in ("capture", "in_week_at")}
    if cap["dest"] == "someday":
        someday_remove(cap["text"])
    elif c["legacy_key"]:
        key = c["legacy_key"]

        def change(state: dict[str, Any]) -> None:
            it = state["items"].get(key)
            if it and it.get("manual") and it.get("status") == "open":
                it["status"] = "cleared"
                it["cleared_at"] = store.now_iso()
                it["cleared_by"] = "inbox"
            if key in state["today"]["order"]:
                state["today"]["order"].remove(key)

        store.mutate(change)
    commitments.change(db, c["id"], "user", {"stage": "stale", "stage_reason": "moved out of Inbox", "week": "", "detail": detail},
                       reason="the user moved or deleted the capture")
    return cap


def _captured(db: sqlite3.Connection, cid: str = "") -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT id FROM commitments WHERE json_extract(detail, '$.capture.id') IS NOT NULL"
        + (" AND json_extract(detail, '$.capture.id') = ?" if cid else "")
        + " ORDER BY json_extract(detail, '$.capture.at') DESC, rowid DESC LIMIT 40",
        (cid,) if cid else (),
    ).fetchall()
    return [c for r in rows if (c := commitments.get(db, r[0])) is not None]


def capture(text: str, dest: str = "") -> dict[str, Any]:
    text = text.strip()
    read = read_capture(text) if not dest or dest in ("today", "week") else {"due": "", "project": ""}
    dest = dest or read["dest"]
    cap = {"id": hashlib.sha1(f"{text}{store.now_iso()}".encode()).hexdigest()[:10], "text": text, "dest": dest, "at": store.now_iso(),
           **{k: read[k] for k in ("due", "project") if read.get(k)}}
    with _opened() as db:
        _file(db, cap)
        return dict(_captured(db, cap["id"])[0]["detail"]["capture"])


def move_capture(cid: str, dest: str) -> dict[str, Any]:
    with _opened() as db:
        found = _captured(db, cid)
        if not found:
            raise PlanningError("That capture is gone")
        c = found[0]
        cap = c["detail"]["capture"]
        if dest == "week" and cap["dest"] in ("today", "someday"):
            detail: dict[str, Any] = dict(c["detail"], in_week_at=store.now_iso())
            if cap["dest"] == "today":
                detail["capture"] = dict(cap, dest="week", moved_at=store.now_iso())
            else:
                detail["capture"] = dict(cap, moved_at=store.now_iso())  # still a Someday bullet, planned in Week
            fields: dict[str, Any] = {"week": _monday(), "stage": "open", "stage_reason": "", "detail": detail}
            commitments.change(db, c["id"], "user", fields, reason="the user planned the capture in Week")
        else:
            cap = _unfile(db, c)
            _file(db, dict({k: v for k, v in cap.items() if k != "ref"}, dest=dest, moved_at=store.now_iso()))
        return dict(_captured(db, cid)[0]["detail"]["capture"])


def delete_capture(cid: str) -> None:
    with _opened() as db:
        for c in _captured(db, cid):
            _unfile(db, c)


def inbox() -> list[dict[str, Any]]:
    with _opened() as db:
        return [dict(c["detail"]["capture"]) for c in _captured(db)]


# ---- Someday: one markdown file of nested bullets ------------------------------------

_someday_lock = threading.Lock()
BULLET_LINE = re.compile(r"^(\s*)[-*+]\s?(.*)$")


def someday_text() -> str:
    try:
        return SOMEDAY_FILE.read_text()
    except OSError:
        return ""


def someday_mtime() -> float:
    try:
        return SOMEDAY_FILE.stat().st_mtime
    except OSError:
        return 0.0


def save_someday(text: str) -> float:
    """The user's outline, word for word. The copy it replaces is kept under ``.versions/`` (one a
    day), so nothing the user wrote is lost."""
    with _someday_lock:
        SOMEDAY_FILE.parent.mkdir(parents=True, exist_ok=True)
        if SOMEDAY_FILE.exists():
            old = SOMEDAY_FILE.read_text()
            if old != text:
                versions = SOMEDAY_FILE.parent / ".versions"
                versions.mkdir(exist_ok=True)
                keep = versions / f"Someday {datetime.now():%Y-%m-%d}.md"
                if not keep.exists():
                    keep.write_text(old)
        tmp = SOMEDAY_FILE.with_suffix(".tmp")
        tmp.write_text(text if text.endswith("\n") or not text else text + "\n")
        os.replace(tmp, SOMEDAY_FILE)
    return someday_mtime()


def someday_add_top(text: str) -> None:
    current = someday_text()
    save_someday(f"- {text.strip()}\n" + current)


def someday_remove(text: str) -> None:
    """Take out a top-level bullet with exactly these words (and nothing under it)."""
    lines = someday_text().splitlines()
    for n, ln in enumerate(lines):
        m = BULLET_LINE.match(ln)
        if m and not m.group(1) and m.group(2).strip() == text.strip():
            nxt = lines[n + 1] if n + 1 < len(lines) else ""
            if nxt.startswith((" ", "\t")):
                return
            del lines[n]
            save_someday("\n".join(lines) + ("\n" if lines else ""))
            return
