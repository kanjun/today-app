---
title: "Today"
description: "A daily list of what you owe people from Slack and email, with replies drafted for you to send"
thumbnail: "template.svg"
version: v4
format: v2
---

# Today

This file is the manifest for the **Today** template (slug:
`today`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a workspace that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

A daily list of what you owe people from Slack and email, with replies drafted for you to send

Today turns the stream of things people want from you into one short, ordered task list. Every 30 minutes it gathers what you owe people -- it searches your Slack itself for what is new from you, to you or mentioning you, and reads the email threads your inbox digest marked Reply needed, Decision needed or TODO. Everything you owe or are waiting on is one list of tasks, each with a history of every change. A judge reads each conversation whole whenever something new is said in it and decides what is really owed and by whom, which list it belongs in (Replies ready, Quick answers, Yours to do, Waiting on), and whether it is already done -- even when you answered somewhere else, it happened on your calendar, or a signing service sent its "Completed" notice. One assistant then gets each card ready before you open it: the reply you would send, a nudge, an intro email, an event to offer, and the next few options. The page has five tabs: Inbox (quick captures that Today files for you, with the date and project your words give), Today (Now, your lists and this week's priorities), Week (your mornings and the week's plan), Projects (your projects, sorted by what is due, plus groupings Today suggests) and Someday (an outline). Each task opens as a card with the full thread, its checklist and notes, the one assistant box and a date picker; it works on a phone too, and opens on the Inbox. You read each draft, tell the assistant what to change in plain words, and press Send; every Send waits ten seconds for Undo, and nothing ever leaves without your tap.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original agent onto a clean default-workspace-template base):

- `system/apps/today`
- `system/supervisord.conf.d/today.conf`
- `data/.state/cron.d/today-sync` (data, explicitly opted in)

- `system/apps/today` -- the app itself, a Python package (`today`) with two console scripts:
  - `today` (`runner.py`): the Flask page and its JSON API, on port 8093. `actions.py` holds what every button does (send with a ten-second Undo, save as a Gmail draft, mark done, not now, add to the calendar); `card.py` builds a task's card; `planning.py` the Inbox, Week and Someday tabs; `project_book.py` the Projects tab; `weekly.py` the weekly priorities note and the next move toward each of its goals.
  - `today-sync` (`sync.py`): the update. It searches Slack (`slack_watch.py`: what is new from, to or mentioning the user) and reads the email digest files, fetches each conversation through `connectors.py` and saves it whole (`conversations.py`), gives each new ask a task (`ledger.py`, `commitments.py`), has the judge (`conversation_judge.py`, with its rules in `user_rules.py`) read every conversation with something new, and then has the one assistant (`act.py`, its actions in `assistant_actions.py`) write ahead the cards that changed.
  - `config.py`: every personal setting (name, organization, email accounts, timezone, digest paths, the voice file, projects, the weekly and Someday files), read from `data/.apps/today/settings.json` with an environment-variable override per field. With nothing set, the page shows "Connect a source" and an empty list; every prompt says "the user".
  - `ai.py`: the model calls (headless `claude -p`); `calendar_events.py`: the Google Calendar read and the add-event call; `store.py` and `conversations.py`: the task list, its history, the saved conversations and the raw copy of every conversation, and the page's own state, all in one SQLite file `data/.apps/today/today.db`, which the first start on an empty data folder creates.
- `system/supervisord.conf.d/today.conf` -- the supervisord program that registers the app with `forward_port.py` (its manifest is `system/apps/today/app.toml`) at `http://localhost:8093` and runs `today`. The shell stops it a minute after its last window closes and starts it on the next request.
- `data/.state/cron.d/today-sync` -- the cron line (installed into `/etc/cron.d/` at boot) that runs `today-sync` every 30 minutes through the automations runner, which catches up after downtime and retries a failed run.

At runtime the two scripts share nothing but `data/.apps/today/` (the database, state and settings), guarded by a file lock; the page polls `/api/state`. Slack, Gmail, Google Calendar and Notion are all reached through latchkey (its local gateway when the workspace has one, else `latchkey curl`), so the app holds no credentials itself.

## Recipe

This template is version `v2`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[`template.toml`](template.toml) -- its `[recipe]` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting agent must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable `requires_` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.


Activation:

- requires_permission: slack-api / slack-read-all (reads the conversations the user is in, the threads around each message, the user's own Slack id, people's names and permalinks)
- requires_permission: slack-api / slack-search (searches Slack every 30 minutes for messages from the user, direct messages to the user and mentions of the user; covered by slack-read-all, listed so a narrower grant is possible)
- requires_permission: slack-api / slack-chat-write (posts a reply in the ask's thread, only when the user presses Send)
- requires_permission: slack-api / slack-conversations-write (opens a direct message to someone, only when the user sends the message a weekly goal's next move suggests)
- requires_permission: google-gmail-api / google-gmail-read-all (reads threads, history, drafts and sent mail for every configured inbox; grant it once per inbox account)
- requires_permission: google-gmail-api / google-gmail-send-messages (sends a reply or a new email, only when the user presses Send)
- requires_permission: google-gmail-api / google-gmail-write-drafts (Save as Gmail draft, and editing or sending the user's own unsent draft)
- requires_permission: google-gmail-api / google-gmail-write-threads (archives an email thread when the user finishes or drops its task, and puts it back on Undo)
- requires_permission: google-calendar-api / google-calendar-read-events (reads the primary calendar from a month back to three weeks ahead so the judge, drafts and Week know what already happened and what is scheduled)
- requires_permission: google-calendar-api / google-calendar-write-events (adds a proposed event or a planned morning block, with no guests, only when the user taps it; moving or removing the block updates or deletes it)
- requires_permission: google-calendar-api / google-calendar-read-settings (reads the calendar's own time zone, which follows the user when they travel)
- requires_permission: notion-mcp-api / notion-mcp-read-all (opens a Notion link from a conversation or a search so the assistant can read what it shows; optional, links to other sites open without it)
- requires_llm: calls Claude through the keyless subscription path (`claude -p` with `--model sonnet`, in `system/apps/today/src/today/ai.py`); an adopter on the keyed litellm path (ANTHROPIC_API_KEY) must switch `ai.complete` per the use-ai-integration skill

Adaptation:

- **The email input is not included and must be connected.** Slack needs nothing beyond the permissions above: Today searches it itself. Email works from an input another app produces, and this template ships no producer: an email digest JSON file per inbox, `{"messages": [...]}` with `threadId`, `final_bucket` (`"1"` Reply needed, `"2"` Decision needed, `"4"` TODO), `from`, `from_addr`, `date`, `subject`, `snippet` (spelled out in `system/apps/today/README.md`). Build or connect a producer (an inbox classifier, or an existing app the adopter has), then point `email_digests` at it. Until Slack has been read once or a digest is there, the page shows "Connect a source".
- **Who the user is.** Set `user_name`, `user_org`, `user_role` and `timezone` in `data/.apps/today/settings.json` (or the `TODAY_USER_*` / `TODAY_TIMEZONE` env vars). The assistant's prompts use them to write in the user's voice and to recognize the user in conversations; unset, it says "the user" and uses the container's timezone.
- **Which inboxes are the user's.** Set `email_accounts` (the inboxes Today reads and sends from; the first one sends new emails) and `email_aliases` (other addresses that are also the user). Without them, the user's own messages in a thread are recognized only by Gmail's SENT label, and new emails (introductions) cannot be sent.
- **Slack names (optional).** Point `slack_users` at a JSON map of Slack user id to display name; without it, Today looks names up in Slack and keeps them.
- **The user's voice (optional).** Point `voice_file` at one file of the user's own messages (standing rules on top, "Part 1" emails, "Part 2" chat messages) so drafts sound like them; without it, drafts use no voice examples.
- **Projects (optional).** None ship. Projects can be made on the Projects tab, or listed in the `projects` setting (`id`, `title`, `about`, and optionally `short`, `folder`, `agent`) so they exist from the first start and the reader knows what belongs in each. A project's `folder` under `projects_dir` may hold a `Goal.md` in the user's words.
- **Where the user's planning files live.** The weekly priorities notes (`weekly_dir`), the Someday outline (`someday_file`) and the project folders (`projects_dir`) default to folders under `data/Today/`; point them elsewhere if the user already keeps these files. A writing app's drafts folder (`writing_dir`, `writing_url`) is optional.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting agent
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that agent's environment rather than frozen to
whatever this publisher happened to have.

Nothing extra -- runs on the stock workspace environment. It shells out only to `latchkey` (Slack, Gmail, Calendar and Notion calls) and `claude` (the model), which every workspace already has, and its Python dependencies (Flask, flask-sock, httpx) install with the app.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new agent. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `latchkey` skill; the request
   opens the approval/login flow in the Imbue Studio app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-10-20) -- first version: the Today app alone, with its 30-minute update and morning cleanup, identity and inputs as settings

### v2 (2026-10-23) -- finishing or dropping a task archives its email with Undo; due dates; a check-off circle; projects you define; goals that say what you're trying to do and re-read their conversations and meetings; redo reads linked pages; sharper reading; optional synced mail store; a re-check of corrected mistakes

### v3 (2026-10-29) -- Later and Ignore in one menu per row; Slack shown thread by thread; drag to reorder a goal's steps; task and goal titles are edited in place and kept

### v4 (2026-11-17) -- one task list with history and a judge that reads whole conversations; Inbox, Today, Week, Someday and Projects tabs; task cards with the full thread, one assistant and one Send with undo; a date picker; projects sorted by what is due; phone layout; opens on the Inbox

## Adaptation history

Each agent that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
