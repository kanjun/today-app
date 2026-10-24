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
      "slack_captures_dir": "data/.apps/gtd/items",
      "email_voice": "data/.apps/today/voice/email.md",
      "chat_voice": "data/.apps/today/voice/slack.md",
      "projects": [
        {"id": "launch", "title": "Launch", "about": "Getting the spring launch out: ..."}
      ]
    }

``email_voice`` and ``chat_voice`` are files of the user's own messages that drafts
for email and for Slack copy the voice of; with neither, drafts use no voice examples.
``projects`` are long-lived areas that hold their tasks and goals off the main list
(``about`` tells the model what belongs); there are none by default. It may also be the
path of a JSON file holding that list.

Environment overrides: TODAY_USER_NAME, TODAY_USER_ORG, TODAY_USER_ROLE,
TODAY_TIMEZONE, TODAY_EMAIL_ACCOUNTS and TODAY_EMAIL_ALIASES (comma-separated),
TODAY_EMAIL_DIGESTS (``account=path`` pairs, comma-separated), TODAY_SLACK_USERS,
TODAY_GTD_ITEMS_DIR (the Slack captures directory), TODAY_EMAIL_VOICE,
TODAY_CHAT_VOICE, and TODAY_PROJECTS (the path of a JSON file with the project list). Relative paths are
relative to the repo root, where the services run.

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
    slack_captures_dir: Path | None
    email_voice: Path | None
    chat_voice: Path | None
    projects: tuple[dict[str, str], ...]


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
        slack_captures_dir=_path(pick("slack_captures_dir", "TODAY_GTD_ITEMS_DIR")),
        email_voice=_path(pick("email_voice", "TODAY_EMAIL_VOICE")),
        chat_voice=_path(pick("chat_voice", "TODAY_CHAT_VOICE")),
        projects=_projects(pick("projects", "TODAY_PROJECTS")),
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
    if s.slack_captures_dir:
        out.append(
            {
                "kind": "slack",
                "label": "Slack captures",
                "path": str(s.slack_captures_dir),
                "found": s.slack_captures_dir.is_dir(),
            }
        )
    return out
