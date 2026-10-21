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

import os
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, request, send_file
from werkzeug.serving import run_simple

from today import actions, connectors, store

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


APP_PAGE = Path(__file__).parent / "assets" / "app.html"


@app.route("/")
def index() -> Response:
    page = APP_PAGE.read_text().replace("<!--SHELL-->", SHELL_PAGE_SCRIPT)
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
    return jsonify(actions.view_state())


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


@app.post("/api/order")
def api_order() -> Response:
    actions.set_order([str(k) for k in _body().get("order", [])])
    return jsonify({"ok": True})


@app.post("/api/add")
def api_add() -> Response:
    text = str(_body().get("text", "")).strip()
    if not text:
        return _error("Type something first.")
    return jsonify({"ok": True, "key": actions.add_manual(text)})


@app.post("/api/done")
def api_done() -> Response:
    actions.mark_done(str(_body().get("key", "")))
    return jsonify({"ok": True})


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


@app.get("/api/search")
def api_search() -> Response:
    return jsonify(actions.search(request.args.get("q", "")))


@app.post("/api/no")
def api_no() -> Response:
    actions.say_no(str(_body().get("key", "")))
    return jsonify({"ok": True})


@app.post("/api/let-go")
def api_let_go() -> Response:
    actions.let_go(str(_body().get("key", "")))
    return jsonify({"ok": True})


@app.post("/api/send")
def api_send() -> Response:
    body = _body()
    try:
        label = actions.send(str(body.get("key", "")), str(body.get("text", "")))
    except (actions.SendError, connectors.ConnectorError) as e:
        return _error(f"Not sent: {e}")
    return jsonify({"ok": True, "message": label})


@app.post("/api/send-many")
def api_send_many() -> Response:
    """Send several approved drafts, one after another; report each one."""
    results = []
    for entry in _body().get("items", []):
        key = str(entry.get("key", ""))
        try:
            results.append(
                {
                    "key": key,
                    "ok": True,
                    "message": actions.send(key, str(entry.get("text", ""))),
                }
            )
        except (actions.SendError, connectors.ConnectorError, KeyError) as e:
            results.append({"key": key, "ok": False, "error": str(e)[:200]})
    return jsonify({"ok": True, "results": results})


@app.get("/api/goal")
def api_goal() -> Response:
    detail = actions.goal_detail(request.args.get("id", ""))
    if detail is None:
        abort(404)
    return jsonify(detail)


@app.post("/api/step-done")
def api_step_done() -> Response:
    body = _body()
    actions.mark_step_done(str(body.get("goal", "")), str(body.get("step", "")))
    return jsonify({"ok": True})


@app.post("/api/goal/rename")
def api_goal_rename() -> Response:
    body = _body()
    title = str(body.get("title", "")).strip()
    if not title:
        return _error("Type a name first.")
    actions.rename_goal(str(body.get("goal", "")), title)
    return jsonify({"ok": True})


@app.post("/api/goal/remove-item")
def api_goal_remove_item() -> Response:
    actions.remove_from_goal(str(_body().get("key", "")))
    return jsonify({"ok": True})


@app.post("/api/goal/move-item")
def api_goal_move_item() -> Response:
    body = _body()
    actions.move_to_goal(str(body.get("key", "")), str(body.get("goal", "")))
    return jsonify({"ok": True})


@app.post("/api/send-email")
def api_send_email() -> Response:
    body = _body()
    try:
        message = actions.send_email(
            str(body.get("key", "")),
            int(body.get("index", 0)),
            str(body.get("to", "")),
            str(body.get("cc", "")),
            str(body.get("subject", "")),
            str(body.get("text", "")),
        )
    except (actions.SendError, connectors.ConnectorError) as e:
        return _error(f"Not sent: {e}")
    return jsonify({"ok": True, "message": message})


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
    """The user's own unsent Gmail draft: save edits to it, or send it (on their tap)."""
    body = _body()
    args = (
        str(body.get("key", "")),
        str(body.get("message_id", "")),
        str(body.get("text", "")),
    )
    sending = bool(body.get("send"))
    try:
        message = (
            actions.send_user_draft(*args)
            if sending
            else actions.save_user_draft(*args)
        )
    except (actions.SendError, connectors.ConnectorError) as e:
        return _error(f"{'Not sent' if sending else 'Not saved'}: {e}")
    return jsonify({"ok": True, "message": message})


@app.post("/api/answer")
def api_answer() -> Response:
    body = _body()
    actions.answer(str(body.get("key", "")), [str(a) for a in body.get("answers", [])])
    return jsonify({"ok": True})


@app.post("/api/revise")
def api_revise() -> Response:
    body = _body()
    note = str(body.get("note", "")).strip()
    if not note:
        return _error("Say what to change first.")
    actions.revise(str(body.get("key", "")), note, bool(body.get("remember")))
    return jsonify({"ok": True})


@app.post("/api/rule/remove")
def api_rule_remove() -> Response:
    actions.remove_rule(int(_body().get("index", -1)))
    return jsonify({"ok": True})


@app.get("/raw")
def raw_view() -> Response:
    key = request.args.get("key", "")
    item = store.load()["items"].get(key)
    raw = store.load_raw(key)
    if item is None or raw is None:
        abort(404)
    rows = []
    if raw.get("source") == "slack":
        for m in raw.get("messages", []):
            when = datetime.fromtimestamp(float(m["ts"])).strftime(
                "%a %b %-d, %-I:%M %p"
            )
            cls = "msg ask" if m.get("is_ask") else "msg"
            rows.append(
                f'<div class="{cls}"><div class="meta">{escape(m.get("name", ""))} &middot; {when}</div>'
                f'<div class="text">{escape(m.get("text", ""))}</div></div>'
            )
    else:
        for m in raw.get("messages", []):
            draft = (
                '<span class="draft">Draft, not sent</span> ' if m.get("draft") else ""
            )
            rows.append(
                f'<div class="msg{" unsent" if draft else ""}"><div class="meta">{draft}{escape(m.get("from", ""))} &middot; {escape(m.get("date", ""))}'
                f"<br>To: {escape(m.get('to', ''))}{' &middot; Cc: ' + escape(m['cc']) if m.get('cc') else ''}</div>"
                f'<div class="text">{escape(m.get("text", ""))}</div></div>'
            )
    source = {"slack": "Slack", "email": "Gmail"}.get(str(raw.get("source")), "")
    page = (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Original</title><style>"
        "body{font:15px/1.5 ui-sans-serif,system-ui,sans-serif;background:#f6f4ef;color:#1c1b19;padding:28px}"
        ".wrap{max-width:680px;margin:0 auto}.msg{background:#fff;border:1px solid #e7e3da;border-radius:10px;padding:12px 16px;margin-bottom:8px}"
        ".ask{border:1.5px solid #1c1b19}.unsent{border-style:dashed;background:#fbfaf6}.draft{background:#f3e3c4;color:#7a5a1c;border-radius:4px;padding:1px 6px;font-weight:600}.meta{font-size:12px;color:#8a857c;margin-bottom:4px}.text{white-space:pre-wrap;overflow-wrap:anywhere}.meta{overflow-wrap:anywhere}"
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
    return Response(page, mimetype="text/html")


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
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
