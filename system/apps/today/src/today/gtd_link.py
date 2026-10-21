"""Today's Slack source: asks and promises captured as one markdown file each.

The captures directory is set in ``config`` (``slack_captures_dir``, or the
TODAY_GTD_ITEMS_DIR env var); whatever captures Slack into it (a to-do app, a
Slack scanner) is the adopter's own. Each file is ``gtd-<id>.md``: flat YAML
front matter (``id``, ``title``, ``status``, ``source: slack``,
``source_url`` = the Slack message permalink, ``source_kind`` = asked or
promised, ``source_person``, ``created_at``) and a body whose first paragraph
may end with the quoted message, like ``Sam asked: "Can you send the deck?"
(#general)``. When an item is finished in Today, its capture is marked done with
a dated note, so the to-do list does not pile up behind it. Only flat front
matter fields are read; no YAML library. With no directory configured (or
none on disk), there are simply no Slack items.
"""

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from today import config


def items_dir() -> Path | None:
    found = config.load().slack_captures_dir
    return found if found and found.is_dir() else None


def parse(path: Path) -> tuple[dict[str, str], str] | None:
    text = path.read_text()
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    fields: dict[str, str] = {}
    last = None
    for line in text[4:end].splitlines():
        if line.startswith("  ") and last:
            fields[last] += " " + line.strip()
            continue
        m = re.match(r"^([A-Za-z_]+):\s?(.*)$", line)
        if m:
            last = m.group(1)
            fields[last] = m.group(2)
    for k, v in fields.items():
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1].replace("''", "'")
        fields[k] = v
    return fields, text[end + 4 :].lstrip("\n")


def slack_captures() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    folder = items_dir()
    if folder is None:
        return out
    for path in sorted(folder.glob("gtd-*.md")):
        parsed = parse(path)
        if not parsed:
            continue
        fields, body = parsed
        if fields.get("source") != "slack" or not fields.get("source_url"):
            continue
        quote = ""
        m = re.search(r':\s"(.*)"\s\(([^)]*)\)\s*$', body.split("\n\n")[0], re.DOTALL)
        if m:
            quote = m.group(1)
        out.append(
            {
                "gtd_id": fields.get("id", path.stem),
                "status": fields.get("status", "inbox"),
                "title": fields.get("title", ""),
                "kind": fields.get("source_kind", "asked"),
                "person": fields.get("source_person", ""),
                "url": fields["source_url"],
                "created_at": fields.get("created_at", ""),
                "quote": quote,
                "summary": body.split("\n\n")[0].strip(),
            }
        )
    return out


def mark_done(gtd_id: str, note: str) -> bool:
    folder = items_dir()
    path = folder / f"{gtd_id}.md" if folder else None
    if path is None or not path.exists():
        return False
    text = path.read_text()
    stamp = datetime.now().isoformat(timespec="seconds")
    text = re.sub(r"(?m)^status: .*$", "status: done", text, count=1)
    text = re.sub(r"(?m)^updated_at: .*$", f"updated_at: '{stamp}'", text, count=1)
    text = (
        text.rstrip("\n") + f"\n\n## Today app {date.today().isoformat()}\n\n{note}\n"
    )
    path.write_text(text)
    return True


def status_of(gtd_id: str) -> str:
    folder = items_dir()
    path = folder / f"{gtd_id}.md" if folder else None
    parsed = parse(path) if path is not None and path.exists() else None
    return parsed[0].get("status", "") if parsed else ""


def projects() -> list[str]:
    """Titles of open captures marked ``is_project: true`` (context for grouping goals)."""
    folder = items_dir()
    out: list[str] = []
    for path in sorted(folder.glob("gtd-*.md")) if folder else []:
        text = path.read_text()
        if "is_project: true" in text and not re.search(
            r"^status: (done|trashed)", text, re.M
        ):
            m = re.search(r"^title: (.*)$", text, re.M)
            if m:
                out.append(m.group(1).strip("'\""))
    return out


def close_finished(items: dict[str, dict[str, Any]], finished: tuple[str, ...]) -> int:
    """Mark the capture done for every Today item that is finished, however it finished."""
    n = 0
    for item in items.values():
        gid = item.get("gtd_id")
        if not gid or item["status"] not in finished:
            continue
        if status_of(gid) in ("", "done", "trashed"):
            continue
        reason = item.get("cleared_reason") or {
            "sent": "Replied from Today.",
            "done": "Marked done in Today.",
        }.get(item["status"], "Settled in Today.")
        if mark_done(gid, f"Settled in Today: {reason}"):
            n += 1
    return n
