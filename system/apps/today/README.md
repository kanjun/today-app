# Today

What you owe people from Slack and email, taken as far as it can go, plus your list for today.

Every 30 minutes a sync (`today-sync`) gathers the things people have asked of you
(and the things you promised), clears the ones you have already handled anywhere,
and has the assistant take each remaining one as far as it can: a drafted reply, an
intro email, an event ready to add to your calendar, or one to three questions only
you can answer. Related items are grouped into goals with ordered steps; a morning
cleanup (`today-sync --consolidate`) regroups them once a day. Nothing is ever sent
until you press Send.

Finishing or dropping an item archives its email (with Undo); items carry due dates
(due soon rises to the top, far-off sinks with a quiet date); tasks can be grouped
into projects you define; and each goal says what you're trying to do and where it
stands, re-read whenever a linked conversation, meeting or your note on it changes.

## Settings

Everything personal is configured in `data/.apps/today/settings.json` (every key
optional), and each key can be overridden by an environment variable:

| Key | Env var | What it is |
| --- | --- | --- |
| `user_name` | `TODAY_USER_NAME` | Your name, as the assistant should call you (default "the user") |
| `user_org` | `TODAY_USER_ORG` | Where you work, for the assistant's context |
| `user_role` | `TODAY_USER_ROLE` | Your role there |
| `timezone` | `TODAY_TIMEZONE` | IANA zone for calendar events (default: the container's) |
| `email_accounts` | `TODAY_EMAIL_ACCOUNTS` | Inboxes Today reads and sends from; the first sends new emails |
| `email_aliases` | `TODAY_EMAIL_ALIASES` | Other addresses that are also you |
| `email_digests` | `TODAY_EMAIL_DIGESTS` | One digest file per inbox (`[{"account", "path"}]`, or `account=path,...`) |
| `slack_users` | `TODAY_SLACK_USERS` | A JSON file mapping Slack user ids to names |
| `slack_captures_dir` | `TODAY_GTD_ITEMS_DIR` | The folder of Slack captures |
| `email_voice` | `TODAY_EMAIL_VOICE` | A file of your own emails that email drafts copy the voice of (none: no voice examples) |
| `chat_voice` | `TODAY_CHAT_VOICE` | The same for Slack drafts |
| `projects` | `TODAY_PROJECTS` | Your projects, `[{"id", "title", "about"}]` (or the path of a JSON file holding it); none by default |

```json
{
  "user_name": "Alex Rivera",
  "user_org": "Acme",
  "user_role": "Head of Product",
  "timezone": "America/New_York",
  "email_accounts": ["alex@acme.example"],
  "email_digests": [{"account": "alex@acme.example", "path": "data/.apps/email-digest/data.json"}],
  "slack_users": "data/.apps/slack-scan/users.json",
  "slack_captures_dir": "data/.apps/slack-scan/items"
}
```

Relative paths are relative to the repo root. With no sources set up, or none of
their files on disk, the page says so and shows an empty list.

## Input formats

Today does not read your inbox or Slack history wholesale. It works from two kinds
of input that something else produces:

**Email digest** (one JSON file per inbox): `{"messages": [...]}`, one entry per
message, each with `threadId` (Gmail thread id), `final_bucket` (`"1"` Reply needed,
`"2"` Decision needed, `"4"` TODO; anything else is ignored), `from` (the From
header), `from_addr`, `date` (RFC 2822), `subject` and `snippet`. Every thread with a
message in an owed bucket becomes an item; Today then reads the whole thread from
Gmail itself.

**Slack captures** (one markdown file per ask, named `gtd-<id>.md`): flat YAML front
matter with `id`, `title`, `status` (`inbox`, `done`, `trashed`, ...),
`source: slack`, `source_url` (the message permalink), `source_kind` (`asked` or
`promised`), `source_person` and `created_at`. The body's first paragraph may end
with the quoted message, like `Sam asked: "Can you send the deck?" (#general)`.
When an item is finished in Today, its capture's `status` is set to `done` with a
dated note. A capture with `is_project: true` is offered to the goal grouping as an
existing project.

**Slack users** (optional): `{"U012ABC": "Sam Lee", ...}`, so messages show names
instead of ids.

## Data

State lives in `data/.apps/today/state.json`; the raw conversation each item came
from is kept under `data/.apps/today/raw/` and is one click away on every item
("See the conversation").

## Changing how Today reads conversations

Mistakes you correct can be kept as cases in `data/.apps/today/cases/` (each a snapshot
of the item and its conversation, plus the right reading). After any change to the
reader's prompts or rules, re-check them with `uv run python -m today.casebook`; add one
with `uv run python -m today.casebook add NAME KEY --why ... --expect ...`.
