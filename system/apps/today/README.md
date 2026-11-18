# Today

What you owe people from Slack and email, taken as far as it can go, plus your list for today.

Every 30 minutes a sync (`today-sync`) gathers the things people have asked of you
(and the things you promised): it searches your Slack itself (`slack_watch.py`) for what is
new from you, to you or mentioning you, and reads the email threads your inbox digests list. Everything you owe or wait on is one task list with a
history of every change. A judge (`conversation_judge.py`) reads each conversation whole
whenever something new is said in it and decides what is owed, by whom, which list it
belongs in (Replies ready, Quick answers, Yours to do, Waiting on) and whether it is
already done (here, in another conversation, on your calendar, or by a signing service's
"Completed" notice). The one assistant (`act.py`) then gets each card ready ahead: a
drafted reply or nudge, an intro email, an event to offer, and the next few options.
Nothing is ever sent until you press Send, and every Send waits ten seconds for Undo.

The page has five tabs: Inbox (quick captures, filed for you), Today (Now, your lists, this
week's priorities), Week (your mornings and what you planned for the week), Projects (your
projects, sorted by what is due, plus groupings Today suggests) and Someday (an outline).
Each task opens as a card with its whole conversation, its checklist and notes, the one
assistant box, and a date picker. It works on a phone too, and opens on the Inbox.

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
| `slack_users` | `TODAY_SLACK_USERS` | Optional JSON file mapping Slack user ids to names (without it, names are looked up in Slack and kept) |
| `voice_file` | `TODAY_VOICE_FILE` | One file of your own messages that every draft copies the voice of: standing rules on top, then "Part 1" (emails) and "Part 2" (chat messages); none: no voice examples |
| `email_voice` | `TODAY_EMAIL_VOICE` | Used only without `voice_file`: a file of your own emails for email drafts |
| `chat_voice` | `TODAY_CHAT_VOICE` | Used only without `voice_file`: the same for Slack drafts |
| `projects` | `TODAY_PROJECTS` | Your projects, `[{"id", "title", "about"}]` (optional `short`, `folder`, `agent`; or the path of a JSON file holding the list); none by default. Each becomes a project on the Projects tab; you can also make them there |
| `weekly_dir` | `TODAY_WEEKLY_DIR` | Where your weekly priorities notes are kept, one file per week (default `data/Today/Weekly priorities`) |
| `someday_file` | `TODAY_SOMEDAY_FILE` | The Someday outline, a markdown file (default `data/Today/Someday.md`) |
| `projects_dir` | `TODAY_PROJECTS_DIR` | One folder per project; its `Goal.md` is your words on what the project is for (default `data/Today/Projects`) |
| `agents_dir` | `TODAY_AGENTS_DIR` | Optional notes another agent keeps per project, `<agent>/playbook.md` (default `data/agents`) |
| `writing_dir` | `TODAY_WRITING_DIR` | Optional folder of markdown drafts, so a weekly writing goal can open the draft it names |
| `writing_url` | `TODAY_WRITING_URL` | The link a draft opens at (its file name is appended) |

```json
{
  "user_name": "Alex Rivera",
  "user_org": "Acme",
  "user_role": "Head of Product",
  "timezone": "America/New_York",
  "email_accounts": ["alex@acme.example"],
  "email_digests": [{"account": "alex@acme.example", "path": "data/.apps/email-digest/data.json"}],
  "voice_file": "data/.apps/today/voice.md"
}
```

Relative paths are relative to the repo root. Until a digest file is there or Slack has
been read once, the page says so and shows an empty list.

## Input formats

Slack needs no input file: Today reads it through your connected Slack account. Email
works from a digest that something else produces:

**Email digest** (one JSON file per inbox): `{"messages": [...]}`, one entry per
message, each with `threadId` (Gmail thread id), `final_bucket` (`"1"` Reply needed,
`"2"` Decision needed, `"4"` TODO; anything else is ignored), `from` (the From
header), `from_addr`, `date` (RFC 2822), `subject` and `snippet`. Every thread with a
message in an owed bucket becomes an item; Today then reads the whole thread from
Gmail itself.

**Slack users** (optional): `{"U012ABC": "Sam Lee", ...}`, so messages show names
instead of ids.

## Data

The tasks, their history, the saved conversations and the raw records each item came
from, and the page's own state (table `today_state`), all live in one SQLite file,
`data/.apps/today/today.db`, which the app creates the first time it starts. The whole
conversation is one click away on every task ("See the whole conversation"). Data from a
version before the task list can be brought over once with `python -m today.commitments_import`.

## Changing how Today reads conversations

Whether something is owed, done, or in which list is decided by the judge
(`conversation_judge.py`, with the rules in `user_rules.py`, which every AI that decides
about a commitment reads word for word); replies, nudges, intro emails and the card's
options are written by the one assistant (`act.py`, its actions in `assistant_actions.py`),
ahead of time on each refresh (`act.write_ahead_due`) and when you ask. Who you are
(`user_name`, `user_org`, `user_role`) goes into every prompt where it says `{ABOUT}`.
