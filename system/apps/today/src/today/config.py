"""Who Today works for, and where it reads what they owe.

Everything personal lives here, read from ``settings.json`` under DATA_DIR
(``data/.apps/today/settings.json`` by default), with an environment variable
overriding each field. The web app and the sync job both read it, so one file
configures both. Every field is optional; with nothing set, Today runs with an
empty list and a "connect a source" message.

settings.json (all keys optional)::

    {
      "user_name": "Alex Rivera",
      "user_org": "Acme",
      "user_role": "CEO",
      "timezone": "America/New_York",
      "email_accounts": ["alex@acme.example", "alex.rivera@mail.example"],
      "email_aliases": ["a.rivera@old.example"],
      "email_digests": [
        {"account": "alex@acme.example", "path": "data/.apps/email-review/data.json"}
      ],
      "slack_users": "data/.apps/slack-review/users.json",
      "voice_file": "data/.apps/today/voice.md",
      "projects": [
        {"id": "launch", "title": "Launch", "about": "Getting the spring launch out: ..."}
      ],
      "writing_dir": "data/.apps/writing/docs",
      "writing_url": "http://writing.localhost/#doc/",
      "weekly_dir": "data/Today/Weekly priorities",
      "someday_file": "data/Today/Someday.md",
      "projects_dir": "data/Today/Projects",
      "agents_dir": "data/agents"
    }

``voice_file`` is one file of the user's own messages that every draft copies the voice of:
standing rules on top, then "Part 1" (emails) and "Part 2" (chat messages). The older
``email_voice`` and ``chat_voice`` (one file each) are used when it is not set; with none,
drafts use no voice examples.
``projects`` are long-lived areas that hold their tasks and goals off the main list
(``about`` tells the model what belongs; optional ``folder`` and ``agent`` name the project's
folder under ``projects_dir`` and an agent whose playbook is read with it); there are none by
default. It may also be the
path of a JSON file holding that list. Each project becomes one of the user's projects in
Today the first time it runs; more can be made on the Projects tab.

``writing_dir`` is an optional folder of markdown drafts from a writing app, so a weekly writing
goal can point at the draft it names; ``writing_url`` is the link a draft opens at (the draft's
file name is appended). With neither, writing goals get a plain next step.

``weekly_dir`` is the folder the weekly priorities note is kept in, one dated file per week;
``someday_file`` the markdown file of Someday bullets; ``projects_dir`` the folder holding
one sub-folder per project, whose ``Goal.md`` is the user's own words on what the project
is for; ``agents_dir`` holds an optional ``<agent>/playbook.md`` per project, notes some
other agent keeps on it. All four are optional and default to the paths above.

Environment overrides: TODAY_USER_NAME, TODAY_USER_ORG, TODAY_USER_ROLE,
TODAY_TIMEZONE, TODAY_EMAIL_ACCOUNTS and TODAY_EMAIL_ALIASES (comma-separated),
TODAY_EMAIL_DIGESTS (``account=path`` pairs, comma-separated), TODAY_SLACK_USERS,
TODAY_VOICE_FILE, TODAY_EMAIL_VOICE,
TODAY_CHAT_VOICE, TODAY_PROJECTS (the path of a JSON file with the project list),
TODAY_WRITING_DIR, TODAY_WRITING_URL, TODAY_WEEKLY_DIR, TODAY_SOMEDAY_FILE, TODAY_PROJECTS_DIR and TODAY_AGENTS_DIR. Relative
paths are relative to the repo root, where the services run.

The input formats are documented in the app's README.
"""

import json
import os
from pathlib import Path
from typing import Any, NamedTuple

from today import store


class Settings(NamedTuple):
    user_name: str
    user_org: str
    user_role: str
    timezone: str
    email_accounts: tuple[str, ...]
    email_aliases: tuple[str, ...]
    email_digests: tuple[tuple[str, Path], ...]
    slack_users: Path | None
    email_voice: Path | None
    voice_file: Path | None
    chat_voice: Path | None
    projects: tuple[dict[str, str], ...]
    writing_dir: Path | None
    writing_url: str
    weekly_dir: Path
    someday_file: Path
    projects_dir: Path
    agents_dir: Path


DEFAULT_WEEKLY_DIR = "data/Today/Weekly priorities"
DEFAULT_SOMEDAY_FILE = "data/Today/Someday.md"
DEFAULT_PROJECTS_DIR = "data/Today/Projects"
DEFAULT_AGENTS_DIR = "data/agents"


def settings_path() -> Path:
    return store.DATA_DIR / "settings.json"


def _file() -> dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = value.split(",")
    return tuple(str(v).strip() for v in value or [] if str(v).strip())


def _digests(value: Any) -> tuple[tuple[str, Path], ...]:
    out = []
    if isinstance(value, str):
        pairs = [p.split("=", 1) for p in value.split(",") if "=" in p]
        out = [(a.strip(), b.strip()) for a, b in pairs]
    else:
        for entry in value or []:
            if isinstance(entry, dict) and entry.get("account") and entry.get("path"):
                out.append((str(entry["account"]).strip(), str(entry["path"]).strip()))
    return tuple((a, Path(p)) for a, p in out if a and p)


def _projects(value: Any) -> tuple[dict[str, str], ...]:
    """The project list, given inline or as the path of a JSON file holding it."""
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(Path(value.strip()).read_text())
        except (OSError, ValueError):
            return ()
    out = []
    for entry in value if isinstance(value, list) else []:
        if isinstance(entry, dict) and str(entry.get("id") or "").strip():
            pid = str(entry["id"]).strip()
            out.append(
                {
                    "id": pid,
                    "title": str(entry.get("title") or pid).strip(),
                    "about": str(entry.get("about") or "").strip(),
                    # Optional: the project's folder under projects_dir (default: its title) and
                    # the name of an agent whose playbook is agents_dir/<agent>/playbook.md.
                    "folder": str(entry.get("folder") or "").strip(),
                    # Optional: a short name for the project on list rows.
                    "short": str(entry.get("short") or "").strip(),
                    "agent": str(entry.get("agent") or "").strip(),
                }
            )
    return tuple(out)


def _path(value: Any) -> Path | None:
    return Path(str(value)) if value else None


def _local_zone() -> str:
    try:
        return Path("/etc/timezone").read_text().strip() or "UTC"
    except OSError:
        return "UTC"


def load() -> Settings:
    f = _file()

    def pick(key: str, env: str) -> Any:
        value = os.environ.get(env)
        return value if value is not None and value.strip() else f.get(key)

    return Settings(
        user_name=str(pick("user_name", "TODAY_USER_NAME") or "").strip(),
        user_org=str(pick("user_org", "TODAY_USER_ORG") or "").strip(),
        user_role=str(pick("user_role", "TODAY_USER_ROLE") or "").strip(),
        timezone=str(pick("timezone", "TODAY_TIMEZONE") or "").strip() or _local_zone(),
        email_accounts=_list(pick("email_accounts", "TODAY_EMAIL_ACCOUNTS")),
        email_aliases=_list(pick("email_aliases", "TODAY_EMAIL_ALIASES")),
        email_digests=_digests(pick("email_digests", "TODAY_EMAIL_DIGESTS")),
        slack_users=_path(pick("slack_users", "TODAY_SLACK_USERS")),
        email_voice=_path(pick("email_voice", "TODAY_EMAIL_VOICE")),
        voice_file=_path(pick("voice_file", "TODAY_VOICE_FILE")),
        chat_voice=_path(pick("chat_voice", "TODAY_CHAT_VOICE")),
        projects=_projects(pick("projects", "TODAY_PROJECTS")),
        writing_dir=_path(pick("writing_dir", "TODAY_WRITING_DIR")),
        writing_url=str(pick("writing_url", "TODAY_WRITING_URL") or "").strip(),
        weekly_dir=Path(str(pick("weekly_dir", "TODAY_WEEKLY_DIR") or DEFAULT_WEEKLY_DIR)),
        someday_file=Path(str(pick("someday_file", "TODAY_SOMEDAY_FILE") or DEFAULT_SOMEDAY_FILE)),
        projects_dir=Path(str(pick("projects_dir", "TODAY_PROJECTS_DIR") or DEFAULT_PROJECTS_DIR)),
        agents_dir=Path(str(pick("agents_dir", "TODAY_AGENTS_DIR") or DEFAULT_AGENTS_DIR)),
    )


def name() -> str:
    """How prompts refer to the user: their first name, or "the user"."""
    full = load().user_name
    return full.split()[0] if full else "the user"


def possessive() -> str:
    """ "Alex's" or "the user's"."""
    n = name()
    return f"{n}'" if n.endswith("s") else f"{n}'s"


def who_they_are() -> str:
    """One phrase for the system prompts, like "Alex Rivera, CEO of Acme"."""
    s = load()
    person = s.user_name or "the user"
    if s.user_role and s.user_org:
        return f"{person}, {s.user_role} at {s.user_org}"
    if s.user_org:
        return f"{person}, who works at {s.user_org}"
    return person


def about() -> str:
    """A sentence on who the user is, for the system prompts ("" when nothing is set).
    ``ai.complete`` puts it where a prompt says ``{ABOUT}``."""
    s = load()
    if not (s.user_name or s.user_org or s.user_role):
        return ""
    return f"The user is {who_they_are()}." + (f" Call the user {name()}." if s.user_name else "")


def my_emails() -> set[str]:
    s = load()
    return {a.lower() for a in s.email_accounts + s.email_aliases}


def accounts() -> tuple[str, ...]:
    """The inboxes Today reads and sends from: the configured accounts, plus any
    inbox a digest names."""
    s = load()
    return tuple(dict.fromkeys(s.email_accounts + tuple(a for a, _ in s.email_digests)))


def default_account() -> str:
    """Where a new email goes out from: the first configured account ("" if none)."""
    found = accounts()
    return found[0] if found else ""


def sources() -> list[dict[str, Any]]:
    """Each configured input and whether its file is there, for the page's setup note."""
    s = load()
    out = [
        {
            "kind": "email",
            "label": f"Email digest for {account}",
            "path": str(path),
            "found": path.is_file(),
        }
        for account, path in s.email_digests
    ]
    # Slack needs no file: Today reads it through the connected Slack account (slack_watch.py),
    # and it counts as there once a refresh has read it.
    out.append(
        {
            "kind": "slack",
            "label": "Slack, read through your connected Slack account",
            "path": "",
            "found": bool(store.load().get("slack_checked")),
        }
    )
    return out
