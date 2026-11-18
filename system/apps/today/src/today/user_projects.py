"""What the reader needs to know about the user's projects: which commitments are the user's
projects, the user's words on each (its Goal.md), and the lines that tell the reader what
belongs in each.

Kept apart from project_book.py (the pages and their writes) so the reader (work.py) can
read it without importing the whole project machinery.
"""

import re
import sqlite3
from pathlib import Path
from typing import Any

from today import commitments, config, conversations, projects, store

# Where each project's folder (with its Goal.md) and each project agent's playbook live:
# the ``projects_dir`` and ``agents_dir`` settings (config.py).
PROJECTS_DIR = config.load().projects_dir
AGENTS_DIR = config.load().agents_dir


def is_old_goal(c: dict[str, Any]) -> bool:
    """One of Today's old goals, brought over by the import: not one of the user's projects
    unless the user accepted it."""
    return bool(c["is_project"]) and bool(c["detail"].get("old_goal")) and not c["detail"].get("mine")


def is_users(c: dict[str, Any]) -> bool:
    return bool(c["is_project"]) and not is_old_goal(c)


def filer_id(p: dict[str, Any]) -> str:
    """The id the reader names a project by: the configured id for a project from the settings."""
    key = p.get("legacy_key") or ""
    return key[len("project:"):] if key.startswith("project:") else p["id"]


def open_projects(db: sqlite3.Connection) -> list[dict[str, Any]]:
    mine = [c for c in commitments.all_commitments(db) if is_users(c) and c["stage"] == "open"]
    return sorted(mine, key=lambda c: (c["position"], c["created_at"]))


def goal_path(p: dict[str, Any]) -> Path | None:
    folder = p["detail"].get("folder") or p["title"]
    path = PROJECTS_DIR / str(folder) / "Goal.md"
    return path if path.is_file() else None


def playbook_path(p: dict[str, Any]) -> Path | None:
    agent = p["detail"].get("agent")
    path = AGENTS_DIR / str(agent) / "playbook.md" if agent else None
    return path if path and path.is_file() else None


def read(path: Path | None) -> str:
    try:
        return path.read_text() if path else ""
    except OSError:
        return ""


def goal_words(p: dict[str, Any]) -> str:
    """The user's words on what the project is for: what they wrote here, else the Purpose in
    its Goal.md, else its first lines."""
    if p["detail"].get("words"):
        return str(p["detail"]["words"])
    text = read(goal_path(p))
    if not text:
        return ""
    lines = text.splitlines()
    start = next((n for n, line in enumerate(lines) if line.strip().lower() == "## purpose"), None)
    body = lines[start + 1:] if start is not None else [x for x in lines if not x.startswith("# ")]
    keep: list[str] = []
    for line in body:
        if line.startswith("## ") or line.strip() == "---":
            break
        keep.append(re.sub(r"^>\s?", "", line))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()


def filer_lines(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """(id, what belongs in it) for each of the user's open projects, for the reader's prompt."""
    default = {p["id"]: p["about"] for p in projects.defined()}
    out = []
    for p in open_projects(db):
        fid = filer_id(p)
        about = p["detail"].get("filing") or default.get(fid) or p["detail"].get("about") or goal_words(p)
        out.append((fid, f'{p["title"]}. {about}' if about else str(p["title"])))
    return out


def _lines() -> list[tuple[str, str]]:
    """The user's open projects for the reader; the configured list before the import into
    commitments has run (or when the database can't be read)."""
    try:
        with conversations.opened(store.DATA_DIR) as db:
            lines = filer_lines(db)
    except (sqlite3.Error, OSError):
        lines = []
    return lines or [(p["id"], p["about"]) for p in projects.defined()]


def prompt_lines() -> str:
    """How the model picks an item's project: the user's own projects, for the system prompt."""
    return "\n".join(f'  "{pid}": {about}' for pid, about in _lines())


