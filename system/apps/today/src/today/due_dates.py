"""The words the user types for a task's date, turned into a date (the user, 2026-11-15: one picker at
the top right of a task, with Today, Tomorrow, a box to type in and a calendar; the green line
under the box shows the date it understood, and it has to be fast).

Plain words are read here at once, with no AI: "fri", "next fri", "fri after next", "dec 15",
"12/15", "in 2 weeks", "end of month", a date like 2026-12-15. Only words this can't read go to
a small, fast model. Dates are in the home zone (the timezone setting).
"""

import calendar
import logging
import re
import subprocess
from datetime import date, datetime, timedelta

from today import ai
from today.calendar_events import ZONE, ZONE_NAME

log = logging.getLogger(__name__)

MODEL = "haiku"
TIMEOUT = 25
TEXT_MAX = 80

DAYS = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = {m.lower(): n for n, m in enumerate(calendar.month_abbr) if m} | {"sept": 9}

SYSTEM = """You turn the words the user typed for a task's due date into one calendar date. Answer with exactly one JSON object and nothing else: {"date": "YYYY-MM-DD"}, or {"date": ""} when the words name no date."""


def today() -> date:
    return datetime.now(ZONE).date()


def _day(word: str) -> int | None:
    w = word.lower().rstrip(".")
    for name, n in DAYS.items():
        if w == name or (len(w) >= 3 and calendar.day_name[n].lower().startswith(w)):
            return n
    return None


def _month(word: str) -> int | None:
    w = word.lower().rstrip(".")
    if w in MONTHS:
        return MONTHS[w]
    for n in range(1, 13):
        if len(w) >= 3 and calendar.month_name[n].lower().startswith(w):
            return n
    return None


def _on_or_after(start: date, month: int, day: int, year: int | None) -> date | None:
    try:
        d = date(year or start.year, month, day)
        if year is None and d < start:
            d = date(start.year + 1, month, day)
        return d
    except ValueError:
        return None


def parse(text: str, now: date | None = None) -> date | None:
    """A date from plain words, or None when they need the model."""
    t0 = now or today()
    t = " ".join(re.sub(r"[,]", " ", (text or "").lower()).split())
    t = re.sub(r"^(?:due|by|on|for)\s+", "", t)
    if not t:
        return None
    if t in ("today", "tod", "tonight", "now"):
        return t0
    if t in ("tomorrow", "tmrw", "tmr", "tom", "tomorow"):
        return t0 + timedelta(days=1)
    if t in ("yesterday",):
        return t0 - timedelta(days=1)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?", t)
    if m:
        year = int(m[3]) + (2000 if m[3] and len(m[3]) == 2 else 0) if m[3] else None
        return _on_or_after(t0, int(m[1]), int(m[2]), year)
    m = re.fullmatch(r"(?:in\s+)?(a|an|one|\d+)\s+(day|days|week|weeks|month|months)(?:\s+from\s+(?:now|today))?", t)
    if m and (t.startswith("in ") or m[1].isdigit() or "from" in t):
        n = 1 if m[1] in ("a", "an", "one") else int(m[1])
        if m[2].startswith("day"):
            return t0 + timedelta(days=n)
        if m[2].startswith("week"):
            return t0 + timedelta(weeks=n)
        month = t0.month - 1 + n
        year, month = t0.year + month // 12, month % 12 + 1
        return date(year, month, min(t0.day, calendar.monthrange(year, month)[1]))
    if t in ("next week",):
        return t0 + timedelta(days=7 - t0.weekday())
    if t in ("end of week", "eow", "end of the week", "this week"):
        return t0 + timedelta(days=(4 - t0.weekday()) % 7)
    if t in ("end of month", "eom", "end of the month"):
        return date(t0.year, t0.month, calendar.monthrange(t0.year, t0.month)[1])
    if t in ("next month",):
        return date(t0.year + t0.month // 12, t0.month % 12 + 1, 1)
    m = re.fullmatch(r"(this\s+|next\s+)?([a-z]+)(\s+after\s+next)?", t)
    weekday = _day(m[2]) if m else None
    if m and weekday is not None:
        coming = t0 + timedelta(days=(weekday - t0.weekday()) % 7)
        if m[3]:
            return coming + timedelta(days=7)
        if m[1] and m[1].strip() == "next":
            # "next fri": the Friday of next week (Monday to Sunday), not one later this week.
            same_week = coming - timedelta(days=coming.weekday()) == t0 - timedelta(days=t0.weekday())
            return coming + timedelta(days=7) if same_week else coming
        return coming
    m = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{4}))?", t) or None
    if m and _month(m[1]):
        return _on_or_after(t0, _month(m[1]) or 0, int(m[2]), int(m[3]) if m[3] else None)
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)\.?(?:\s+(\d{4}))?", t)
    if m and _month(m[2]):
        return _on_or_after(t0, _month(m[2]) or 0, int(m[1]), int(m[3]) if m[3] else None)
    return None


def ask_model(text: str, now: date | None = None) -> date | None:
    """Words plain reading can't place ("the week before Thanksgiving"), from a small model."""
    t0 = now or today()
    prompt = f"Today is {t0.strftime('%A')}, {t0.isoformat()} ({ZONE_NAME} time).\nThe words: {text[:TEXT_MAX]}"
    try:
        answer = ai.complete_json(prompt, system=SYSTEM, model=MODEL, timeout=TIMEOUT)
    except (ai.ClaudeError, subprocess.TimeoutExpired, OSError) as e:
        log.warning("due date words %r: %s", text, e)
        return None
    try:
        return date.fromisoformat(str(answer.get("date") or ""))
    except ValueError:
        return None


def label(d: date, now: date | None = None) -> str:
    """How the picker says a date: "Fri Nov 27", with the year when it isn't this one."""
    t0 = now or today()
    return d.strftime("%a %b %-d") + ("" if d.year == t0.year else d.strftime(" %Y"))


def understand(text: str, *, use_model: bool = True) -> dict[str, str]:
    """The date the words name, as the picker shows it: {"date": "YYYY-MM-DD", "label": ...};
    both empty when it can't tell."""
    d = parse(text)
    if d is None and use_model and len((text or "").strip()) >= 3:
        d = ask_model(text)
    return {"date": d.isoformat(), "label": label(d)} if d else {"date": "", "label": ""}
