"""Projects: long-lived areas the user names that hold their tasks and goals.

The list comes from the ``projects`` setting (see config.py); there are none by default,
and then nothing is ever put in a project. A project shows on the main list only while it
has something open in it.
"""

from collections import Counter
from typing import Any

from today import config


def defined() -> list[dict[str, str]]:
    """The projects the user set up (id, title, and what belongs in it)."""
    return list(config.load().projects)


def all_projects(state: dict[str, Any]) -> list[dict[str, str]]:
    return state.get("projects") or defined()


def ids(state: dict[str, Any]) -> set[str]:
    return {p["id"] for p in all_projects(state)}


# ---- Filing into the user's projects: THE one place this is decided ---------------------
# The reader guesses a project for each item it reads (``item["project"]``, only when the item
# is really about it). The user, 2026-11-13: when an item clearly belongs to one of the user's
# projects (one they made or accepted), Today files it there itself, as a ``sync`` event saying
# why, so it can be traced and moved out with one action (project_book.auto_file). Today's own
# groupings (group_suggestions.py) stay suggestions until the user accepts one. With False, the
# guess is only offered as a quiet "Part of <project>?" the user accepts with one tap or ignores.
AUTO_FILE_INTO_USER_PROJECTS = True


def file_or_suggest() -> str:
    """"file" when the reader's guess goes straight into the user's project, else "suggest"."""
    return "file" if AUTO_FILE_INTO_USER_PROJECTS else "suggest"


def of_item(item: dict[str, Any]) -> str:
    """The item's project: whatever was set on it, else none."""
    return item.get("project") or ""


def of_goal(goal: dict[str, Any], items: dict[str, Any]) -> str:
    """A goal sits in the project most of its items are in."""
    if goal.get("project") is not None:
        return goal["project"]
    keys = {k for step in goal.get("chain", []) for k in step.get("covers", [])}
    votes = Counter(of_item(items[k]) for k in keys if k in items)
    if not votes:
        return ""
    best, n = votes.most_common(1)[0]
    return best if best and n * 2 > sum(votes.values()) else ""
