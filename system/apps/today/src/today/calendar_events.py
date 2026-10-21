"""The user's primary Google calendar, so goals and drafts know which events are real and when.

Each sync saves the events from two days back to three weeks ahead as a raw record
(``raw/calendar.json``); prompts read that copy, never the live calendar.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from today import config, connectors, store

log = logging.getLogger(__name__)

KEY = "calendar"
EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def refresh() -> int:
    """Fetch and save the calendar; a failure keeps the last saved copy."""
    now = datetime.now(timezone.utc)
    params = {
        "timeMin": (now - timedelta(days=2)).isoformat(),
        "timeMax": (now + timedelta(days=21)).isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "250",
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
    store.save_raw(KEY, {"fetched_at": store.now_iso(), "events": events})
    return len(events)


def _when(start: dict[str, Any]) -> str:
    if "dateTime" in start:
        return datetime.fromisoformat(start["dateTime"]).strftime("%a %b %d %-I:%M%p")
    return datetime.fromisoformat(start.get("date", "1970-01-01")).strftime(
        "%a %b %d (all day)"
    )


def brief(limit: int = 120) -> str:
    """One line per event the user hasn't declined, for a prompt. Empty when never fetched."""
    record = store.load_raw(KEY)
    if not record:
        return ""
    lines = []
    for e in record.get("events", []):
        people = e.get("attendees") or []
        if any(a.get("self") and a.get("responseStatus") == "declined" for a in people):
            continue
        guests = [
            a.get("displayName") or a.get("email", "")
            for a in people
            if not a.get("self") and not a.get("resource")
        ]
        line = f"- {_when(e.get('start') or {})}: {e.get('summary', '(no title)')}"
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
        f"{config.possessive()} calendar (as of {record.get('fetched_at', '')}):\n"
        + "\n".join(lines[:limit])
        + "\n"
    )


def add(event: dict[str, Any], source: str = "") -> dict[str, Any]:
    """Create the event on the user's primary calendar. No invitations go out: it has no guests."""
    zone = config.load().timezone
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
