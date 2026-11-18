"""What you owe people from Slack and email, taken as far as it can go, plus your list for today.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/today/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/today/`` but
  honors the ``TODAY_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``TODAY_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://today.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import logging
import os
import re
import time
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, request, send_file
from werkzeug.serving import run_simple

from today import (
    act,
    actions,
    ai,
    card,
    commitments,
    config,
    connectors,
    conversations,
    due_dates,
    group_suggestions,
    learned,
    ledger,
    planning,
    project_book,
    store,
    task_steps,
    weekly,
    work,
)
from today import page as page_shape

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/today/`` but is overridable via the ``TODAY_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/today/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("TODAY_DATA_DIR", "data/.apps/today"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``TODAY_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("TODAY_PORT", "8093"))

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path(
    "system/apps/system_interface/imbue/system_interface/static/_static"
)
SHELL_STATIC_MODULE_NAMES = ("app_contract.js", "context_menu.js")

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT = """<script type="module">
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
</script>"""

app = Flask("today", static_folder=None)
_slow_log = logging.getLogger("today.slow")
SLOW_SECONDS = 1.0


# Any request slower than a second is logged with its path, so a slow page the user notices can be
# traced to what it waited on (the user, 2026-11-14: project pages felt slow).
@app.before_request
def _clock_in() -> None:
    request.environ["today.started"] = time.monotonic()
    request.environ["today.actor"] = store.actor.set("user")  # a change made from the user's page is the user's


@app.teardown_request
def _clock_off(_error: BaseException | None) -> None:
    token = request.environ.pop("today.actor", None)
    if token is not None:
        store.actor.reset(token)


@app.after_request
def _clock_out(response: Response) -> Response:
    took = time.monotonic() - request.environ.get("today.started", time.monotonic())
    if took > SLOW_SECONDS:
        _slow_log.warning("slow request: %s %s took %.1fs", request.method, request.full_path, took)
    return response


APP_PAGE = Path(__file__).parent / "assets" / "app.html"


@app.route("/")
def index() -> Response:
    page = APP_PAGE.read_text().replace("<!--SHELL-->", SHELL_PAGE_SCRIPT)
    # The user's home zone, for the date picker's "today" (the timezone setting).
    page = page.replace("<html>", f'<html data-tz="{escape(config.load().timezone)}">', 1)
    return Response(page, mimetype="text/html")


def _error(message: str) -> Response:
    response = jsonify({"ok": False, "error": message})
    response.status_code = 400
    return response


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@app.get("/api/state")
def api_state() -> Response:
    return jsonify(dict(actions.view_state(), learned=learned.listed()))


@app.get("/api/item")
def api_item() -> Response:
    detail = actions.item_detail(request.args.get("key", ""))
    if detail is None:
        abort(404)
    return jsonify(detail)


@app.post("/api/today")
def api_today() -> Response:
    body = _body()
    actions.set_today(str(body.get("key", "")), bool(body.get("on", True)))
    return jsonify({"ok": True})


@app.post("/api/plan")
def api_plan() -> Response:
    """The user's one decision on an item: today, tomorrow, next week, not mine, or drop."""
    body = _body()
    choice = str(body.get("choice", ""))
    if choice not in actions.PLAN_CHOICES:
        return _error("That isn't one of the choices.")
    message = actions.plan(str(body.get("key", "")), choice, str(body.get("until", "")))
    return jsonify({"ok": True, "message": message})


@app.post("/api/weekly")
def api_weekly() -> Response:
    """Save the user's priorities for the week, word for word; each goal's next move follows."""
    text = str(_body().get("text", ""))
    if not text.strip():
        return _error("Write your priorities first.")
    path = weekly.save(text)
    weekly.plan_soon(force=True)
    return jsonify({"ok": True, "message": "Saved for this week", "path": str(path)})


@app.post("/api/weekly/goal")
def api_weekly_goal() -> Response:
    """Tick one of the user's weekly goals off (or back on, for Undo). Nothing is sent."""
    body = _body()
    weekly.set_done(str(body.get("id", "")), bool(body.get("on", True)))
    return jsonify({"ok": True})


@app.post("/api/draft/edit")
def api_draft_edit() -> Response:
    """Keep the user's edits to a drafted message as the user types (the reply, a nudge, or an email)."""
    body = _body()
    index = body.get("index")
    try:
        actions.save_draft_edit(
            str(body.get("key", "")), str(body.get("text", "")),
            None if index is None else int(index),
            None if body.get("to") is None else str(body.get("to")),
            None if body.get("subject") is None else str(body.get("subject")),
        )
    except actions.SendError as e:
        return _error(str(e))
    return jsonify({"ok": True})


@app.post("/api/move/notes")
def api_move_notes() -> Response:
    """Keep the user's notes on a goal's move as the user types them."""
    body = _body()
    path = actions.save_move_notes(str(body.get("key", "")), str(body.get("text", "")))
    return jsonify({"ok": True, "path": path})


@app.post("/api/not-now")
def api_not_now() -> Response:
    """Move it to Later (nothing is sent); on false puts it back (Undo)."""
    body = _body()
    if body.get("from_now"):  # Not now on a row in Now: back to its own list
        return jsonify({"ok": True, "message": actions.out_of_now(str(body.get("key", "")), bool(body.get("on", True)))})
    message = actions.not_now(str(body.get("key", "")), bool(body.get("on", True)))
    return jsonify({"ok": True, "message": message})


@app.post("/api/pull-back")
def api_pull_back() -> Response:
    """Take something out of Filed away: it is the user's to reply to or do."""
    return jsonify({"ok": True, "message": actions.pull_back(str(_body().get("key", "")))})


@app.post("/api/send-soon")
def api_send_soon() -> Response:
    """Every Send button: send this exact text in a few seconds unless Undo is pressed first.
    ``what`` says what is being sent (the reply, an email, or the user's own Gmail draft) and ``fields`` its addressing; each goes on the thread with the person."""
    body = _body()
    try:
        fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
        token = actions.send_soon(str(body.get("key", "")), str(body.get("text", "")),
                                  what=str(body.get("what") or "reply"), fields=fields)
    except (actions.SendError, KeyError) as e:
        return _error(f"Not sent: {e}")
    return jsonify({"ok": True, "token": token, "seconds": actions.SEND_DELAY_SECONDS})


@app.post("/api/send-cancel")
def api_send_cancel() -> Response:
    """Undo a send that has not gone out yet."""
    stopped = actions.cancel_send(str(_body().get("token", "")))
    return jsonify({"ok": stopped, "message": "Not sent" if stopped else "Already sent"})


@app.get("/api/send-status")
def api_send_status() -> Response:
    """Whether a delayed send went out, failed, or was undone."""
    return jsonify(actions.send_status(request.args.get("token", "")))


@app.post("/api/order")
def api_order() -> Response:
    actions.set_order([str(k) for k in _body().get("order", [])])
    return jsonify({"ok": True})


@app.post("/api/add")
def api_add() -> Response:
    body = _body()
    text = str(body.get("text", "")).strip()
    if not text:
        return _error("Type something first.")
    origin = body.get("origin")
    origin = {str(k): str(v) for k, v in origin.items()} if isinstance(origin, dict) else None
    key = actions.add_manual(text, pick=bool(body.get("pick", True)), origin=origin)
    return jsonify({"ok": True, "key": key})


@app.post("/api/done")
def api_done() -> Response:
    return jsonify(
        {"ok": True, "message": actions.mark_done(str(_body().get("key", "")))}
    )


@app.post("/api/bring-back")
def api_bring_back() -> Response:
    actions.bring_back(str(_body().get("key", "")))
    return jsonify({"ok": True})


@app.post("/api/calendar/add")
def api_calendar_add() -> Response:
    body = _body()
    try:
        link = actions.add_to_calendar(
            str(body.get("key", "")), int(body.get("index", 0))
        )
    except (connectors.ConnectorError, IndexError, KeyError, ValueError) as e:
        return _error(f"Couldn't add it: {str(e)[:200]}")
    return jsonify({"ok": True, "link": link})


# ---- planning: the user's next steps, Week, Inbox, Someday (planning.py) --------------------


def _plan_call(fn: Any, *args: Any) -> Response:
    try:
        return jsonify({"ok": True, "result": fn(*args)})
    except planning.PlanningError as e:
        return _error(str(e))
    except connectors.ConnectorError as e:
        return _error(f"Couldn't change your calendar: {str(e)[:200]}")


@app.post("/api/step")
def api_plan_step() -> Response:
    body = _body()
    return _plan_call(planning.set_step, str(body.get("ref", "")), str(body.get("text", "")))


@app.post("/api/step/then")
def api_step_then() -> Response:
    body = _body()
    text = str(body.get("text", "")).strip()
    if not text:
        return _error("Type the step first.")
    return _plan_call(planning.add_then, str(body.get("ref", "")), text)


@app.post("/api/step/done")
def api_plan_step_done() -> Response:
    return _plan_call(planning.step_done, str(_body().get("ref", "")))


@app.post("/api/in-week")
def api_in_week() -> Response:
    body = _body()
    return _plan_call(planning.set_in_week, str(body.get("key", "")), bool(body.get("on", True)))


@app.post("/api/someday/week")
def api_someday_week() -> Response:
    body = _body()
    return _plan_call(planning.set_someday_week, str(body.get("text", "")), bool(body.get("on", True)))


@app.get("/api/week")
def api_week() -> Response:
    start = request.args.get("start") or datetime.now().date().isoformat()
    try:
        datetime.fromisoformat(start)
    except ValueError:
        return _error("Not a date")
    state = store.load()
    return jsonify(planning.week_view(start, weekly.view(state), state["items"]))


@app.post("/api/week/place")
def api_week_place() -> Response:
    body = _body()
    return _plan_call(
        planning.place,
        str(body.get("ref", "")),
        str(body.get("title", "")),
        str(body.get("date", "")),
        str(body.get("start", "")),
        str(body.get("end", "")),
    )


@app.post("/api/week/time")
def api_week_time() -> Response:
    body = _body()
    return _plan_call(
        planning.retime, str(body.get("date", "")), str(body.get("start", "")), str(body.get("end", "")), str(body.get("ref", ""))
    )


@app.post("/api/week/unplace")
def api_week_unplace() -> Response:
    body = _body()
    return _plan_call(planning.unplace, str(body.get("date", "")), str(body.get("ref", "")))


@app.get("/api/inbox")
def api_inbox() -> Response:
    return jsonify({"items": planning.inbox()})


@app.post("/api/inbox")
def api_inbox_capture() -> Response:
    body = _body()
    text = str(body.get("text", "")).strip()
    if not text:
        return _error("Type something first.")
    return _plan_call(planning.capture, text, str(body.get("dest", "")))


@app.post("/api/inbox/move")
def api_inbox_move() -> Response:
    body = _body()
    return _plan_call(planning.move_capture, str(body.get("id", "")), str(body.get("dest", "")))


@app.post("/api/inbox/delete")
def api_inbox_delete() -> Response:
    return _plan_call(planning.delete_capture, str(_body().get("id", "")))


@app.get("/api/someday")
def api_someday() -> Response:
    text = planning.someday_text()
    return jsonify({"text": text, "mtime": planning.someday_mtime(), "week": planning.summary()["someday_week"]})


@app.post("/api/someday")
def api_someday_save() -> Response:
    text = _body().get("text")
    if not isinstance(text, str):
        return _error("Nothing to save.")
    return jsonify({"ok": True, "mtime": planning.save_someday(text)})


@app.get("/api/search")
def api_search() -> Response:
    return jsonify(actions.search(request.args.get("q", "")))




@app.post("/api/ignore")
def api_ignore() -> Response:
    return jsonify({"ok": True, "message": actions.ignore(str(_body().get("key", "")))})


@app.post("/api/undo-finish")
def api_undo_finish() -> Response:
    try:
        message = actions.undo_finish(str(_body().get("key", "")))
    except connectors.ConnectorError as e:
        return _error(f"Couldn't put it back in your inbox: {e}")
    return jsonify({"ok": True, "message": message})


@app.post("/api/item/title")
def api_item_title() -> Response:
    body = _body()
    title = str(body.get("title", "")).strip()
    if not title:
        return _error("Type a title first.")
    actions.rename_item(str(body.get("key", "")), title)
    return jsonify({"ok": True})


@app.post("/api/gmail-draft")
def api_gmail_draft() -> Response:
    """Save to the user's Gmail drafts; never sends."""
    body = _body()
    index = body.get("index")
    try:
        result = actions.save_gmail_draft(
            str(body.get("key", "")),
            None if index is None else int(index),
            str(body.get("to", "")),
            str(body.get("cc", "")),
            str(body.get("subject", "")),
            str(body.get("text", "")),
        )
    except (actions.SendError, connectors.ConnectorError) as e:
        return _error(f"Not saved: {e}")
    return jsonify({"ok": True, **result})


@app.post("/api/user-draft")
def api_user_draft() -> Response:
    """Save edits into the user's own unsent Gmail draft (sending it goes through /api/send-soon)."""
    body = _body()
    try:
        key = str(body.get("key", ""))
        message = actions.save_user_draft(key, str(body.get("message_id", "")), str(body.get("text", "")))
    except (actions.SendError, connectors.ConnectorError) as e:
        return _error(f"Not saved: {e}")
    return jsonify({"ok": True, "message": message, "message_id": actions.user_draft_now(key)})




# ---- Projects: the user's projects, kept as commitments (project_book.py) ----


def _book_call(fn: Any, *args: Any) -> Response:
    """Run a project write; answer with what it returns (a batch id for Undo, and more)."""
    try:
        with project_book.opened() as db:
            out = fn(db, *args)
    except (project_book.ProjectError, commitments.CommitmentError) as e:
        return _error(str(e))
    if isinstance(out, dict):
        return jsonify(dict(out, ok=True))
    return jsonify({"ok": True, "batch": out})


@app.get("/api/projects")
def api_projects() -> Response:
    """The user's projects, Today's suggested groupings, and the user's completed projects. Opening the tab
    starts a fresh grouping pass in the background when the last one is a day old."""
    group_suggestions.run_soon_if_due()
    with project_book.opened() as db:
        out = project_book.project_list(db)
        if out["ready"]:
            out.update(suggestions=group_suggestions.current(db), suggesting=group_suggestions.running())
        else:
            out["suggestions"] = []
        return jsonify(out)


@app.get("/api/projects/page")
def api_projects_page() -> Response:
    """One project: the user's words, its tasks (the same rows as the Today list), waiting, done."""
    with project_book.opened() as db:
        out = project_book.project_page(db, request.args.get("id", ""))
    if out is None:
        return _error("No such project.")
    return jsonify(out)


@app.post("/api/projects/new")
def api_projects_new() -> Response:
    """A project the user names."""
    title = str(_body().get("title", "")).strip()
    if not title:
        return _error("Name it first.")
    return _book_call(project_book.create_project, title)


@app.post("/api/projects/rename")
def api_projects_rename() -> Response:
    body = _body()
    title = str(body.get("title", "")).strip()
    if not title:
        return _error("Type a name first.")
    return _book_call(project_book.rename, str(body.get("id", "")), title)


@app.post("/api/projects/words")
def api_projects_words() -> Response:
    """The user's own words on what the project is for, kept verbatim."""
    body = _body()
    return _book_call(project_book.set_words, str(body.get("id", "")), str(body.get("text", "")).strip())


@app.post("/api/projects/complete")
def api_projects_complete() -> Response:
    """The whole project done, its open tasks with it. Nothing is sent or archived."""
    return _book_call(project_book.complete, str(_body().get("id", "")))


@app.post("/api/projects/suggestion")
def api_projects_suggestion() -> Response:
    """Make one of Today's suggested groupings a project (keep), or wave it off for good."""
    body = _body()
    fn = group_suggestions.keep if body.get("keep") else group_suggestions.dismiss
    return _book_call(fn, int(body.get("id", 0)))


@app.post("/api/projects/add")
def api_projects_add() -> Response:
    """A new task in the project (``title``), or one from the user's list moved in (``key``)."""
    body = _body()
    pid, key, title = str(body.get("id", "")), str(body.get("key", "")), str(body.get("title", "")).strip()
    if key:
        return _book_call(project_book.task_action, pid, key, "add")
    if not title:
        return _error("Type the task first.")
    return _book_call(project_book.new_task, pid, title)


@app.post("/api/projects/task")
def api_projects_task() -> Response:
    """The task menu: later, move (``to``), notmine, drop, done."""
    body = _body()
    return _book_call(
        project_book.task_action, str(body.get("id", "")), str(body.get("key", "")),
        str(body.get("action", "")), str(body.get("to", "")),
    )


@app.post("/api/projects/order")
def api_projects_order() -> Response:
    """The user dragged a task on a project page: the new order of Next and Then (task ids)."""
    body = _body()
    return _book_call(
        project_book.order_tasks, str(body.get("id", "")),
        [str(c) for c in body.get("next") or []], [str(c) for c in body.get("then") or []],
    )


@app.post("/api/projects/file")
def api_projects_file() -> Response:
    """The user said an item is part of one of the user's projects."""
    body = _body()
    return _book_call(project_book.file_item, str(body.get("key", "")), str(body.get("id", "")))


@app.post("/api/projects/step/tick")
def api_step_tick() -> Response:
    """The user ticked a step (or unticked it); ``all_done`` when it was the last one."""
    body = _body()
    return _book_call(task_steps.tick, str(body.get("cid", "")), bool(body.get("done", True)))


@app.post("/api/projects/step/edit")
def api_step_edit() -> Response:
    """The user's words for a step; empty removes it."""
    body = _body()
    return _book_call(task_steps.edit, str(body.get("cid", "")), str(body.get("title", "")))


@app.post("/api/projects/step/add")
def api_step_add() -> Response:
    """A step of the user's on a task, after another step or at the end."""
    body = _body()
    task, key = str(body.get("task", "")), str(body.get("key", ""))
    if not task and key:  # a card's first step: the task gets its commitment now
        with project_book.opened() as db:
            task = project_book.ensure_commitment(db, key)["id"]
    return _book_call(task_steps.add, task, str(body.get("title", "")), str(body.get("after", "")))


@app.post("/api/projects/step/remove")
def api_step_remove() -> Response:
    return _book_call(task_steps.remove, str(_body().get("cid", "")))


@app.post("/api/undo")
def api_undo() -> Response:
    """Undo a whole batch (recorded as more events; nothing is deleted)."""
    try:
        with project_book.opened() as db:
            message = project_book.undo(db, str(_body().get("batch", "")))
    except (project_book.ProjectError, commitments.CommitmentError) as e:
        return _error(str(e))
    return jsonify({"ok": True, "message": message})


# ---- the one assistant: every card, the bottom bar and every project's box (act.py) ---------


def _act_call(fn: Any, *args: Any) -> Response:
    try:
        return jsonify({"ok": True, **fn(*args)})
    except act.ActError as e:
        return _error(str(e))
    except ai.ClaudeError as e:
        return _error(f"Today couldn't work that out: {str(e)[:200]}")


@app.get("/api/card")
def api_card() -> Response:
    """Everything one open card shows: the conversation, the checklist, the reply, the options."""
    out = card.card(request.args.get("key", ""))
    if out is None:
        abort(404)
    return jsonify(out)


@app.post("/api/card/detach")
def api_card_detach() -> Response:
    """The user took a conversation off a task (undoable); Today won't attach it again."""
    body = _body()
    return _book_call(card.detach_thread, str(body.get("key", "")), str(body.get("conversation", "")))


@app.post("/api/card/note/remove")
def api_card_note_remove() -> Response:
    """The user crossed out one of a task's notes (undoable)."""
    return _book_call(card.remove_note, str(_body().get("id", "")))


@app.post("/api/card/due")
def api_card_due() -> Response:
    """The user set or cleared a task's date in the picker (undoable)."""
    b = _body()
    return _book_call(card.set_due, str(b.get("key", "")), str(b.get("due", "")))


@app.post("/api/due/parse")
def api_due_parse() -> Response:
    """The date the words the user is typing name, for the picker's green line: plain words at once,
    anything else from a small model (``model`` false asks for the quick reading only)."""
    b = _body()
    return jsonify(due_dates.understand(str(b.get("text", ""))[:due_dates.TEXT_MAX], use_model=bool(b.get("model", True))))


@app.post("/api/assistant")
def api_assistant() -> Response:
    """The one assistant, wherever the user typed or tapped: a task's card (``kind`` "task", with
    ``key`` and ``focus``), the bottom bar ("tasks", with the ``keys`` attached) or a project's
    box ("project", with ``pid`` and the ``task`` the user points at)."""
    body = _body()
    scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
    try:
        return _act_call(act.ask, scope, str(body.get("say", "")), str(body.get("label", "")))
    except (project_book.ProjectError, commitments.CommitmentError, OSError) as e:
        return _error(str(e)[:200])


@app.post("/api/assistant/open")
def api_assistant_open() -> Response:
    """The first options for a task's card or one of its steps, worked out once and kept."""
    body = _body()
    return _act_call(act.opening, str(body.get("key", "")), str(body.get("focus", "")))


@app.post("/api/assistant/view")
def api_assistant_view() -> Response:
    """What a project's box or the bottom bar shows now: what the user said last and the options."""
    body = _body()
    return _act_call(act.place_view, body.get("scope") if isinstance(body.get("scope"), dict) else {})


@app.post("/api/assistant/back")
def api_assistant_back() -> Response:
    """Other options, wherever the user is: take back the last thing done there."""
    body = _body()
    scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
    return _act_call(act.back, "", "", scope)


@app.get("/api/learned")
def api_learned() -> Response:
    """What Today has learned from the user's corrections."""
    return jsonify({"learned": learned.listed()})


@app.post("/api/learned/remove")
def api_learned_remove() -> Response:
    body = _body()
    learned.removed(int(body.get("id", 0)), bool(body.get("removed", True)))
    return jsonify({"ok": True})


@app.get("/projects/sources")
def project_sources() -> Response:
    """What Today has read for a project: the user's Goal.md and the project agent's playbook, rendered."""
    with project_book.opened() as db:
        p = commitments.get(db, request.args.get("id", ""))
        sources = project_book.raw_sources(db, request.args.get("id", ""))
    if p is None:
        abort(404)
    body = "".join(
        f"<h2>{escape(s['label'])}</h2><div class='path'>{escape(s['path'])}</div><div class='md'>{markdown_html(s['text'])}</div>"
        for s in sources
    ) or "<p>Nothing saved for this project yet.</p>"
    return Response(
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(p['title'])}</title><style>"
        "body{font:15px/1.55 ui-sans-serif,system-ui,sans-serif;background:#f6f4ef;color:#1c1b19;padding:28px}"
        ".wrap{max-width:680px;margin:0 auto}h1,h2,h3{font-size:15px;font-weight:600}h2{margin:26px 0 2px}.path{color:#8a857c}"
        ".md{background:#fff;border:1px solid #e7e3da;border-radius:12px;padding:14px 18px;margin-top:8px}.md h3{margin:12px 0 4px}"
        ".md blockquote{border-left:3px solid #e7e3da;padding-left:12px;color:#55514a;margin:6px 0}.md ul{margin:4px 0 8px 20px}.md p{margin:6px 0}"
        "a{color:#8a857c}.back{text-decoration:none}</style></head><body><div class='wrap'>"
        "<a class='back' href='/?tab=projects' onclick='if(document.referrer.startsWith(location.origin)){history.back();return false}'>&larr; Back</a>"
        f"<h1>What Today has read for {escape(p['title'])}</h1>{body}</div>{SHELL_PAGE_SCRIPT}</body></html>",
        mimetype="text/html",
    )


def markdown_html(text: str) -> str:
    """The user's markdown as the user would read it: headings, quotes, lists and paragraphs."""
    out, para, in_list = [], [], False

    def inline(s: str) -> str:
        s = escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\*)\*(?!\s)(.+?)\*", r"<i>\1</i>", s)
        return re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<a href='\2' target='_blank'>\1</a>", s)

    def flush() -> None:
        nonlocal para
        if para:
            out.append("<p>" + "<br>".join(inline(x) for x in para) + "</p>")
            para = []

    for line in text.splitlines():
        stripped = line.strip()
        is_item = bool(re.match(r"^[-*] ", stripped))
        if in_list and not is_item:
            out.append("</ul>")
            in_list = False
        if not stripped or stripped == "---":
            flush()
        elif stripped.startswith("#"):
            flush()
            out.append(f"<h3>{inline(stripped.lstrip('#').strip())}</h3>")
        elif stripped.startswith(">"):
            flush()
            out.append(f"<blockquote>{inline(stripped.lstrip('>').strip())}</blockquote>")
        elif is_item:
            flush()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(stripped[2:])}</li>")
        else:
            para.append(stripped)
    flush()
    if in_list:
        out.append("</ul>")
    return "".join(out)


@app.get("/assets/<path:name>")
def app_asset(name: str) -> Response:
    """The page's own scripts (the Projects tab and the feedback bar) and the dictation bundle."""
    root = (Path(__file__).parent / "assets").resolve()
    path = (root / name).resolve()
    if root not in path.parents or not path.is_file() or path.suffix not in (".js", ".css", ".wasm", ".mjs", ".onnx", ".json"):
        abort(404)
    return send_file(path)


@app.get("/raw")
def raw_view() -> Response:
    key = request.args.get("key", "")
    item = store.load()["items"].get(key)
    raw = store.load_raw(key)
    if item is None and ledger.is_commitment_key(key):
        # A commitment the judge found has no item: show its whole conversation.
        found = ledger.conversation_html_rows(key) or []
        return Response(
            "".join(
                f'<div class="msg"><div class="meta">{escape(who)} &middot; {escape(when)}</div>'
                f'<div class="text">{escape(page_shape.clean_text(text))}</div></div>'
                for who, when, text in found
            ),
            mimetype="text/html",
        )
    if item is None or raw is None:
        abort(404)
    rows = []
    if raw.get("source") in ("slack", "text"):
        source = store.source_message(key)
        for group in work.threads(raw, source):
            if group["label"]:
                rows.append(f'<div class="thread">{escape(group["label"])}</div>')
            for m in group["messages"]:
                when = datetime.fromtimestamp(float(m["ts"])).strftime(
                    "%a %b %-d, %-I:%M %p"
                )
                cls = "msg ask" if store.is_source(m, source) else "msg"
                rows.append(
                    f'<div class="{cls}"><div class="meta">{escape(m.get("name", ""))} &middot; {when}</div>'
                    f'<div class="text">{escape(page_shape.clean_text(m.get("text", "")))}</div></div>'
                )
    else:
        for m in raw.get("messages", []):
            draft = (
                '<span class="draft">Draft, not sent</span> ' if m.get("draft") else ""
            )
            rows.append(
                f'<div class="msg{" unsent" if draft else ""}"><div class="meta">{draft}{escape(m.get("from", ""))} &middot; {escape(m.get("date", ""))}'
                f"<br>To: {escape(m.get('to', ''))}{' &middot; Cc: ' + escape(m['cc']) if m.get('cc') else ''}</div>"
                f'<div class="text">{escape(page_shape.clean_text(m.get("text", "")))}</div></div>'
            )
    if request.args.get("part"):
        # Just the messages, for the whole thread shown inline under an item.
        return Response("".join(rows), mimetype="text/html")
    source = {"slack": "Slack", "email": "Gmail"}.get(str(raw.get("source")), "")
    html_page = (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Original</title><style>"
        "body{font:15px/1.5 ui-sans-serif,system-ui,sans-serif;background:#f6f4ef;color:#1c1b19;padding:28px}"
        ".wrap{max-width:680px;margin:0 auto}.msg{background:#fff;border:1px solid #e7e3da;border-radius:10px;padding:12px 16px;margin-bottom:8px}"
        ".thread{font-size:12px;color:#8a857c;text-transform:uppercase;letter-spacing:.04em;margin:18px 0 6px}.ask{border:1.5px solid #1c1b19}.unsent{border-style:dashed;background:#fbfaf6}.draft{background:#f3e3c4;color:#7a5a1c;border-radius:4px;padding:1px 6px;font-weight:600}.meta{font-size:12px;color:#8a857c;margin-bottom:4px}.text{white-space:pre-wrap;overflow-wrap:anywhere}.meta{overflow-wrap:anywhere}"
        "@media (max-width:600px){body{padding:16px 12px}.msg{padding:10px 12px}}"
        "a{color:#8a857c;font-size:13px}.back{display:inline-block;margin-bottom:16px;text-decoration:none}h1{font-size:18px;margin-bottom:6px}</style></head><body><div class='wrap'>"
        f"<a class='back' href='/#{quote(key, safe='')}' onclick='if(document.referrer.startsWith(location.origin)){{history.back();return false}}'>&larr; Back</a>"
        f"<h1>{escape(raw.get('subject') or raw.get('channel_label') or 'Original conversation')}</h1>"
        + (
            f"<p style='margin-bottom:16px'><a href='{escape(raw.get('url', ''))}'>Open in {source}</a></p>"
            if raw.get("url")
            else ""
        )
        + "".join(rows)
        + "</div>"
        + SHELL_PAGE_SCRIPT
        + "</body></html>"
    )
    return Response(html_page, mimetype="text/html")


@app.get("/thread")
def thread_view() -> Response:
    """A whole saved conversation, by its id: what a task's attached thread links to."""
    conv = request.args.get("c", "")
    with conversations.opened(store.DATA_DIR) as db:
        head = db.execute("SELECT title, url FROM conversations WHERE id = ?", (conv,)).fetchone()
    if head is None:
        abort(404)
    rows = "".join(
        f'<div class="msg"><div class="meta">{escape(config.name() if m["is_user"] else m["sender"])} &middot; '
        f'{datetime.fromtimestamp(m["at"]).strftime("%a %b %-d, %-I:%M %p")}</div>'
        f'<div class="text">{escape(page_shape.clean_text(m["text"] or ""))}{(" [" + escape(m["note"]) + "]") if m["note"] else ""}</div></div>'
        for m in conversations.messages(store.DATA_DIR, conv)
    )
    url = head["url"] or (f"https://mail.google.com/mail/u/{conv.split(':')[1]}/#all/{conv.split(':')[2]}" if conv.startswith("email:") else "")
    return Response(
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(head['title'])}</title><style>"
        "body{font:15px/1.5 ui-sans-serif,system-ui,sans-serif;background:#f6f4ef;color:#1c1b19;padding:28px}"
        ".wrap{max-width:680px;margin:0 auto}.msg{background:#fff;border:1px solid #e7e3da;border-radius:10px;padding:12px 16px;margin-bottom:8px}"
        ".meta{font-size:12px;color:#8a857c;margin-bottom:4px;overflow-wrap:anywhere}.text{white-space:pre-wrap;overflow-wrap:anywhere}"
        "a{color:#8a857c;font-size:13px}.back{display:inline-block;margin-bottom:16px;text-decoration:none}h1{font-size:18px;margin-bottom:6px}</style></head><body><div class='wrap'>"
        "<a class='back' href='/' onclick='history.back();return false'>&larr; Back</a>"
        f"<h1>{escape(head['title'])}</h1>"
        + (f"<p style='margin-bottom:16px'><a href='{escape(url)}'>Open the original</a></p>" if url else "")
        + (rows or "<p>No messages saved yet.</p>")
        + "</div>" + SHELL_PAGE_SCRIPT + "</body></html>",
        mimetype="text/html",
    )


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    actions.resume_sends()  # a Send still waiting out Undo when Today stopped still goes
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
