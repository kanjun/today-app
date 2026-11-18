"""The user's work calendar, so goals and drafts know which events are real and when.

Each sync saves the events from a month back to three weeks ahead as a raw record
(``raw/calendar.json``); prompts read that copy, never the live calendar.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from today import config, connectors, store

log = logging.getLogger(__name__)

KEY = "calendar"
PAST_DAYS = 31
EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
# Every time an AI reads or writes is in the user's home zone (the ``timezone`` setting; the user,
# 2026-11-15): the user's calendar can be set to another zone while the user travels, and its
# times read as the home zone were hours off.
ZONE_NAME = config.load().timezone
ZONE = ZoneInfo(ZONE_NAME)


def refresh() -> int:
    """Fetch and save the calendar; a failure keeps the last saved copy."""
    now = datetime.now(timezone.utc)
    params = {
        # A month back, so a meeting that already happened (a session asked for weeks ago)
        # counts as settled when an item is judged.
        "timeMin": (now - timedelta(days=PAST_DAYS)).isoformat(),
        "timeMax": (now + timedelta(days=21)).isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "1000",
    }
    try:
        data = connectors._curl([f"{EVENTS}?{urlencode(params)}"])
    except connectors.ConnectorError as e:
        log.warning("calendar unavailable: %s", e)
        return 0
    if "error" in data:
        log.warning("calendar unavailable: %s", str(data["error"])[:200])
        return 0
    events = data.get("items") or []
    record = {"fetched_at": store.now_iso(), "events": events, "tz": time_zone()}
    store.save_raw(KEY, record)
    return len(events)


SETTINGS_TZ = "https://www.googleapis.com/calendar/v3/users/me/settings/timezone"


def time_zone() -> str:
    """The user's Google Calendar's current time zone (it follows the user when the user travels), or the
    last one saved when it can't be read."""
    try:
        zone = str(connectors._curl([SETTINGS_TZ]).get("value") or "")
    except connectors.ConnectorError:
        zone = ""
    return zone or str((store.load_raw(KEY) or {}).get("tz") or ZONE_NAME)


def local_iso(start: dict[str, Any]) -> str:
    """An event's start (or end) as an ISO time in the home zone; a whole day stays a date."""
    if start.get("dateTime"):
        return datetime.fromisoformat(start["dateTime"]).astimezone(ZONE).isoformat(timespec="minutes")
    return str(start.get("date") or "")


def _when(start: dict[str, Any]) -> str:
    if "dateTime" in start:
        return datetime.fromisoformat(start["dateTime"]).astimezone(ZONE).strftime("%a %b %d %-I:%M%p")
    return datetime.fromisoformat(start.get("date", "1970-01-01")).strftime(
        "%a %b %d (all day)"
    )


def video(e: dict[str, Any]) -> str:
    """Which call link the event carries, if any."""
    where = f"{e.get('location') or ''} {e.get('description') or ''}".lower()
    if "zoom.us" in where:
        return "Zoom"
    if e.get("hangoutLink") or e.get("conferenceData"):
        return "Google Meet"
    return ""


def events(ids: list[str]) -> list[dict[str, Any]]:
    """The saved events with these ids (an event no longer on the calendar is left out)."""
    record = store.load_raw(KEY) or {}
    by_id = {e.get("id"): e for e in record.get("events", [])}
    return [by_id[i] for i in ids if i in by_id]


def brief(limit: int = 400, ids: bool = False, past_days: int | None = None) -> str:
    """One line per event the user hasn't declined, for a prompt. Empty when never fetched.
    ``ids`` starts each line with the event's id, for a prompt that links events.
    ``past_days`` leaves out events that started more than that many days ago."""
    record = store.load_raw(KEY)
    if not record:
        return ""
    since = (datetime.now() - timedelta(days=past_days)).date().isoformat() if past_days is not None else ""
    lines = []
    for e in record.get("events", []):
        start = e.get("start") or {}
        if since and str(start.get("dateTime") or start.get("date") or "")[:10] < since:
            continue
        people = e.get("attendees") or []
        if any(a.get("self") and a.get("responseStatus") == "declined" for a in people):
            continue
        guests = [
            a.get("displayName") or a.get("email", "")
            for a in people
            if not a.get("self") and not a.get("resource")
        ]
        line = f"- {_when(e.get('start') or {})}: {e.get('summary', '(no title)')}"
        if ids:
            line = f"- [{e.get('id', '')}] " + line[2:]
        if video(e):
            line += f" (call on {video(e)})"
        if e.get("location") and not e["location"].startswith("http"):
            line += f" @ {e['location'][:60]}"
        if guests:
            line += (
                f" (with {', '.join(guests[:8])}{' +more' if len(guests) > 8 else ''})"
            )
        lines.append(line)
    if not lines:
        return ""
    return (
        f"{config.possessive()} calendar (as of {record.get('fetched_at', '')}; times are {ZONE_NAME}):\n"
        + "\n".join(lines[:limit])
        + "\n"
    )


def add(event: dict[str, Any], source: str = "") -> dict[str, Any]:
    """Create the event on the user's primary calendar. No invitations go out: it has no guests."""
    zone = event.get("zone") or ZONE_NAME  # what an AI writes is in the home zone
    if event.get("all_day"):
        start = {"date": event["start"][:10]}
        end_day = event.get("end", "")[:10] or event["start"][:10]
        # Google's all-day end date is exclusive.
        end_day = (
            (datetime.fromisoformat(end_day) + timedelta(days=1)).date().isoformat()
        )
        end = {"date": end_day}
    else:
        begin = datetime.fromisoformat(event["start"])
        finish = (
            datetime.fromisoformat(event["end"])
            if event.get("end")
            else begin + timedelta(hours=1)
        )
        start = {"dateTime": begin.isoformat(timespec="seconds"), "timeZone": zone}
        end = {"dateTime": finish.isoformat(timespec="seconds"), "timeZone": zone}
    notes = event.get("notes", "")
    if source:
        notes += f"\n\nFrom: {source}"
    body = {
        "summary": event["title"],
        "start": start,
        "end": end,
        "location": event.get("location", ""),
        "description": (notes + "\n\nAdded from Today.").strip(),
    }
    made = connectors._curl(
        [
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(body),
            f"{EVENTS}?sendUpdates=none",
        ]
    )
    if "error" in made or not made.get("id"):
        raise connectors.ConnectorError(f"calendar: {str(made.get('error'))[:200]}")
    return made
