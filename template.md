---
title: "Today"
description: "A daily list of what you owe people from Slack and email, with replies drafted for you to send"
thumbnail: "template.svg"
version: v2
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

Today turns the stream of things people want from you into one short, ordered list. Every 30 minutes it gathers what you owe people -- asks and promises from Slack, and the email threads your inbox digest marked Reply needed, Decision needed or TODO -- and throws out anything you have already handled, even when you answered somewhere else (a Slack ask answered by email counts). For each thing left, an assistant takes it as far as it can on its own: it reads the whole conversation and writes the reply you would send, writes intro emails, proposes a calendar event, or asks you one to three quick questions when it needs a fact only you have. Related asks are grouped into goals with ordered steps ("2025 taxes filed and paid": answer the questionnaire, then sign, then pay). You see a page with a Today list you pick and reorder, an "Owed by you" list of goals and single items, "Waiting on them" and "Saying no" sections, and search across everything. You review each draft, tell it what to change in plain words ("shorter, and offer Thursday instead"), and press Send; nothing ever leaves without that tap.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original agent onto a clean default-workspace-template base):

- `system/apps/today`
- `system/supervisord.conf.d/today.conf`
- `data/.state/cron.d/today-sync` (data, explicitly opted in)
- `data/.state/cron.d/today-cleanup` (data, explicitly opted in)

- `system/apps/today` -- the app itself, a Python package (`today`) with two console scripts:
  - `today` (`runner.py`): the Flask page and its JSON API, on port 8093. `actions.py` holds what every button does (send, save as a Gmail draft, mark done, say no, move between goals, add to the calendar).
  - `today-sync` (`sync.py`): the update. It reads the inputs (`gtd_link.py` for Slack captures, the email digest files), fetches each conversation through `connectors.py`, has `work.py` take each item as far as it can (drafts, intro emails, questions, calendar proposals, checking the user's sent mail and Slack for an answer given elsewhere), and has `goals.py` place items into goals and order each goal's steps. `--consolidate` runs the full regrouping pass.
  - `config.py`: every personal setting (name, organization, email accounts, timezone, input paths), read from `data/.apps/today/settings.json` with an environment-variable override per field. With nothing set, the page shows "Connect a source" and an empty list.
  - `ai.py`: the model calls (headless `claude -p`); `calendar_events.py`: the Google Calendar read and the add-event call; `store.py`: one locked JSON state file plus the raw copy of every conversation under `data/.apps/today/raw/`.
- `system/supervisord.conf.d/today.conf` -- the supervisord program that registers the app with `forward_port.py` (its manifest is `system/apps/today/app.toml`) at `http://localhost:8093` and runs `today`. The shell stops it a minute after its last window closes and starts it on the next request.
- `data/.state/cron.d/today-sync` -- the cron line (installed into `/etc/cron.d/` at boot) that runs `today-sync` every 30 minutes through the automations runner, which catches up after downtime and retries a failed run.
- `data/.state/cron.d/today-cleanup` -- the cron line that runs `today-sync --consolidate` once a day in the 5 am hour of the container clock: the only pass that merges, splits, closes and reorders goals.

At runtime the two scripts share nothing but `data/.apps/today/` (state, raw conversations, settings), guarded by a file lock; the page polls `/api/state` and the update writes to the same file. Slack, Gmail and Google Calendar are all reached with `latchkey curl`, so the app holds no credentials itself.

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

- requires_permission: slack-api / slack-read-all (reads the conversation around each ask, the user's own Slack id, permalinks, and searches the user's sent Slack messages for answers given elsewhere)
- requires_permission: slack-api / slack-chat-write (posts a reply in the ask's thread, only when the user presses Send)
- requires_permission: google-gmail-api / google-gmail-read-all (reads threads, history, drafts and sent mail for every configured inbox; grant it once per inbox account)
- requires_permission: google-gmail-api / google-gmail-send-messages (sends a reply or a new email, only when the user presses Send)
- requires_permission: google-gmail-api / google-gmail-write-drafts (Save as Gmail draft, and editing or sending the user's own unsent draft)
- requires_permission: google-calendar-api / google-calendar-read-events (reads the primary calendar from two days back to three weeks ahead so drafts and goals know what is already scheduled)
- requires_permission: google-calendar-api / google-calendar-write-events (adds a proposed event, with no guests, only when the user taps Add to my calendar)
- requires_llm: calls Claude through the keyless subscription path (`claude -p` with `--model sonnet`, in `system/apps/today/src/today/ai.py`); an adopter on the keyed litellm path (ANTHROPIC_API_KEY) must switch `ai.complete` per the use-ai-integration skill

Adaptation:

- **The inputs are not included and must be connected.** Today does not scan a whole inbox or Slack workspace; it works from two inputs another app produces, and this template ships neither producer. (1) An email digest JSON file per inbox: `{"messages": [...]}` with `threadId`, `final_bucket` (`"1"` Reply needed, `"2"` Decision needed, `"4"` TODO), `from`, `from_addr`, `date`, `subject`, `snippet`. (2) A folder of Slack captures, one `gtd-<id>.md` file per ask or promise, with flat front matter (`id`, `title`, `status`, `source: slack`, `source_url` = the message permalink, `source_kind` = asked or promised, `source_person`, `created_at`). The formats are spelled out in `system/apps/today/README.md`. Build or connect a producer for each (an inbox classifier and a Slack scanner, or an existing app the adopter has), then point `email_digests` and `slack_captures_dir` at them. Until then the page shows "Connect a source".
- **Who the user is.** Set `user_name`, `user_org`, `user_role` and `timezone` in `data/.apps/today/settings.json` (or the `TODAY_USER_*` / `TODAY_TIMEZONE` env vars). The assistant's prompts use them to write in the user's voice and to recognize the user in conversations; unset, it says "the user" and uses the container's timezone.
- **Which inboxes are the user's.** Set `email_accounts` (the inboxes Today reads and sends from; the first one sends new emails) and `email_aliases` (other addresses that are also the user). Without them, the user's own messages in a thread are recognized only by Gmail's SENT label, and new emails (introductions) cannot be sent.
- **Slack names (optional).** Point `slack_users` at a JSON map of Slack user id to display name, or messages show raw user ids.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting agent
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that agent's environment rather than frozen to
whatever this publisher happened to have.

Nothing extra -- runs on the stock workspace environment. It shells out only to `latchkey` (Slack, Gmail and Calendar calls) and `claude` (the model), which every workspace already has, and its Python dependencies (Flask, flask-sock) install with the app.

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

## Adaptation history

Each agent that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
