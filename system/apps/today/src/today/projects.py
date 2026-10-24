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


def prompt_lines() -> str:
    """How the model picks an item's project (the configured list, for the system prompt)."""
    return "\n".join(f'  "{p["id"]}": {p["about"]}' for p in defined())


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
