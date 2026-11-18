"""Slack and Gmail through the user's connected accounts (``latchkey curl``).

Read calls are used by the sync; the two send calls are used only when the user
presses Send in the app. Gmail threads are read from the synced mail store
(mail_store.py) when it is current.
"""

import base64
import html as htmllib
import json
import os
import re
import subprocess
import threading
import time
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Any
from urllib.parse import urlencode

import httpx

from today import config, mail_store, store


class ConnectorError(RuntimeError):
    pass


def dry_run(what: str, detail: Any) -> bool:
    """True when this instance must not touch the outside world (a preview or test copy
    started with TODAY_DRY_SEND=1): the would-be send is written to ``dry_sends.jsonl``
    in the data copy instead, so a check can see exactly what would have gone out."""
    if os.environ.get("TODAY_DRY_SEND") != "1":
        return False
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(store.DATA_DIR / "dry_sends.jsonl", "a") as f:
        f.write(
            json.dumps({"at": store.now_iso(), "what": what, "detail": detail}) + "\n"
        )
    return True


# What a dry run answers in place of the real service, shaped like each write's reply.
DRY_ANSWER = {
    "ok": True,
    "ts": "0",
    "id": "dry-run",
    "message": {"id": "dry-run", "threadId": ""},
    "htmlLink": "",
}


def _curl(
    args: list[str], account: str | None = None, timeout: int = 120, fail: bool = False
) -> dict:
    """One API call through the user's connected accounts. ``fail`` is curl's ``-f``: an HTTP error
    status raises instead of returning the error body."""
    if "-X" in args and args[args.index("-X") + 1] != "GET":
        if dry_run("http " + args[args.index("-X") + 1], {"account": account, "args": args}):
            return dict(DRY_ANSWER)
    client = _gateway_client()
    if client is not None:
        return _via_gateway(client, args, account, timeout, fail)
    prefix = (
        ["latchkey"]
        + (["--account", account] if account else [])
        + ["curl", "-s", "--retry", "4", "--retry-max-time", "90"]
        + (["-f"] if fail else [])
    )
    proc = subprocess.run(
        prefix + args, capture_output=True, text=True, timeout=timeout, check=False
    )
    if proc.returncode != 0:
        raise ConnectorError(
            f"latchkey curl exited {proc.returncode}: {proc.stdout[:200]} {proc.stderr[:200]}"
        )
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise ConnectorError(f"not JSON: {proc.stdout[:200]}") from e
    if not isinstance(data, dict):
        raise ConnectorError(f"unexpected answer: {proc.stdout[:200]}")
    return data


# Every call above used to start a `latchkey curl` (a Node program: about 0.8 s of CPU and
# 160 MB each), which only forwards the request to the local latchkey gateway. With the
# gateway's address in the environment, Today speaks its HTTP protocol directly over one
# kept-alive connection instead.
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_RETRIES = 4  # what `curl --retry 4 --retry-max-time 90` gave
_RETRY_BUDGET_SECONDS = 90.0
_client_lock = threading.Lock()
# The one shared client, remade only if the gateway settings change.
_shared: dict[str, Any] = {}
# Pause between retries without blocking anything else (an Event nobody sets).
_pause = threading.Event()


def gateway_settings_from_environment() -> tuple[str, dict[str, str]]:
    """The gateway URL and the auth headers latchkey itself sends, read from the environment the
    workspace sets (KeyError when there is no gateway). The permissions override is optional."""
    base = os.environ["LATCHKEY_GATEWAY"].rstrip("/")
    headers = {"x-latchkey-gateway-password": os.environ["LATCHKEY_GATEWAY_PASSWORD"]}
    permissions_override = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "")
    if permissions_override:
        headers["x-latchkey-gateway-permissions-override"] = permissions_override
    return base, headers


def _gateway_client() -> "tuple[httpx.Client, str] | None":
    """The shared client and gateway base URL, or None when no gateway is configured (then
    `latchkey curl` is used as before)."""
    try:
        base, headers = gateway_settings_from_environment()
    except KeyError:
        return None
    settings = (base, tuple(sorted(headers.items())))
    with _client_lock:
        if _shared.get("settings") != settings:
            _shared["client"] = httpx.Client(headers=headers)
            _shared["settings"] = settings
        return _shared["client"], base


def _parse_curl_args(args: list[str]) -> tuple[str, dict[str, str], str | None, str]:
    """The method, headers, body and URL of the curl arguments Today passes (-X, -H, -d, URL)."""
    method, headers, body, url = "GET", {}, None, ""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-X":
            method = args[i + 1]
            i += 2
        elif a == "-H":
            name, _, value = args[i + 1].partition(":")
            headers[name.strip()] = value.strip()
            i += 2
        elif a == "-d":
            body = args[i + 1]
            if method == "GET":
                method = "POST"
            i += 2
        elif a.startswith("-"):
            raise ConnectorError(f"unsupported curl option for the gateway: {a}")
        else:
            url = a
            i += 1
    if not url:
        raise ConnectorError("no URL in the request")
    return method, headers, body, url


def _via_gateway(
    client_base: "tuple[httpx.Client, str]",
    args: list[str],
    account: str | None,
    timeout: int,
    fail: bool = False,
) -> dict:
    client, base = client_base
    method, headers, body, url = _parse_curl_args(args)
    if account:
        headers["x-latchkey-gateway-account"] = account
    started = time.monotonic()
    resp, problem = None, "no attempt made"
    for attempt in range(_RETRIES + 1):
        try:
            resp = client.request(
                method,
                f"{base}/gateway/{url}",
                headers=headers,
                content=body.encode() if body is not None else None,
                timeout=timeout,
            )
            retry = resp.status_code in _RETRY_STATUSES
            problem = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except httpx.TransportError as e:
            resp, retry, problem = None, True, f"gateway unreachable: {e}"
        if not retry or attempt >= _RETRIES or time.monotonic() - started > _RETRY_BUDGET_SECONDS:
            break
        _pause.wait(min(2**attempt, 10))
    if resp is None or (fail and resp.status_code >= 400):
        raise ConnectorError(problem)
    try:
        data = resp.json()
    except ValueError as e:
        raise ConnectorError(f"not JSON ({problem})") from e
    if not isinstance(data, dict):
        raise ConnectorError(f"unexpected answer: {resp.text[:200]}")
    if data.get("latchkeyError"):
        raise ConnectorError(f"latchkey: {data.get('error')}")
    return data


# ---- Notion ----------------------------------------------------------------

NOTION_MCP = "https://mcp.notion.com/mcp"


def _mcp_post(payload: dict, session: str, timeout: int = 60) -> tuple[str, dict]:
    """One JSON-RPC message to Notion's own MCP server through the user's connected account.
    Returns the session id it answers with and the message's result (empty for a notice)."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    body = json.dumps(payload)
    gw = _gateway_client()
    if gw is not None:
        client, base = gw
        try:
            resp = client.post(f"{base}/gateway/{NOTION_MCP}", headers=headers, content=body.encode(), timeout=timeout)
        except httpx.TransportError as e:
            raise ConnectorError(f"gateway unreachable: {e}") from e
        status, sid, text = resp.status_code, resp.headers.get("mcp-session-id", ""), resp.text
    else:
        args = ["latchkey", "curl", "-s", "-i", "-X", "POST", NOTION_MCP, "-d", body]
        for k, v in headers.items():
            args += ["-H", f"{k}: {v}"]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        head, _, text = proc.stdout.replace("\r\n", "\n").partition("\n\n")
        status = int((head.split() or ["0", "0"])[1]) if head.startswith("HTTP") else 0
        sid = next((ln.split(":", 1)[1].strip() for ln in head.splitlines() if ln.lower().startswith("mcp-session-id:")), "")
    if status >= 400 or status == 0:
        raise ConnectorError(f"Notion answered {status}: {text[:200]}")
    data = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")] or [text.strip()]
    if "id" not in payload or not data[-1]:
        return sid or session, {}
    try:
        msg = json.loads(data[-1])
    except ValueError as e:
        raise ConnectorError(f"Notion's answer was not JSON: {text[:200]}") from e
    if msg.get("error"):
        raise ConnectorError(f"Notion: {msg['error'].get('message', msg['error'])}")
    return sid or session, msg.get("result") or {}


def notion_fetch(url: str) -> str:
    """A Notion page's contents (as Notion's own server writes it out), read through the user
    connected account, so private pages the user can open can be read too."""
    sid, _ = _mcp_post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "today", "version": "1"}}}, "")
    _mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    _, result = _mcp_post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "notion-fetch", "arguments": {"id": url}}}, sid, timeout=90)
    text = "\n".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
    if result.get("isError"):
        raise ConnectorError(f"Notion: {text[:200]}")
    return text


# ---- Slack -----------------------------------------------------------------


def slack_get(method: str, params: dict[str, str]) -> dict:
    data = _curl([f"https://slack.com/api/{method}?{urlencode(params)}"])
    if not data.get("ok"):
        raise ConnectorError(f"slack {method}: {data.get('error')}")
    return data


def slack_post_message(channel: str, text: str, thread_ts: str | None) -> dict:
    body: dict[str, str] = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    data = _curl(
        [
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json; charset=utf-8",
            "-d",
            json.dumps(body),
            "https://slack.com/api/chat.postMessage",
        ]
    )
    if not data.get("ok"):
        raise ConnectorError(f"slack chat.postMessage: {data.get('error')}")
    return data


def slack_open_dm(user_id: str) -> str:
    """The id of the user's direct-message channel with this person (opened if new)."""
    data = _curl(
        [
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json; charset=utf-8",
            "-d",
            json.dumps({"users": user_id}),
            "https://slack.com/api/conversations.open",
        ]
    )
    if not data.get("ok"):
        raise ConnectorError(f"slack conversations.open: {data.get('error')}")
    return str((data.get("channel") or {}).get("id") or "dry-run")


def slack_permalink(channel: str, ts: str) -> str:
    try:
        return str(
            slack_get("chat.getPermalink", {"channel": channel, "message_ts": ts}).get(
                "permalink"
            )
            or ""
        )
    except ConnectorError:
        return ""


# ---- Gmail -----------------------------------------------------------------

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"


def gmail_get(path: str, account: str, params: dict[str, str] | None = None) -> dict:
    url = f"{GMAIL}/{path}" + (f"?{urlencode(params)}" if params else "")
    data = _curl([url], account=account)
    if "error" in data:
        raise ConnectorError(f"gmail {path}: {str(data['error'])[:200]}")
    return data


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
        "utf-8", "replace"
    )


def _plain_text(payload: dict) -> str:
    """The message's text/plain body (falling back to stripped HTML)."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    if mime == "text/plain" and body.get("data"):
        return _b64(body["data"])
    parts = payload.get("parts") or []
    for part in parts:
        text = _plain_text(part)
        if text:
            return text
    if mime == "text/html" and body.get("data"):
        html = _b64(body["data"])
        html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
        return htmllib.unescape(re.sub(r"[ \t]+", " ", re.sub(r"<[^>]+>", " ", html)))
    return ""


def strip_quoted(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(">"):
            continue
        if (s.startswith("On ") and s.endswith("wrote:")) or s.startswith(
            "-----Original Message-----"
        ):
            break
        out.append(line)
    return "\n".join(out).strip()


def header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []) or []:
        if h.get("name", "").lower() == name.lower():
            return str(h.get("value", ""))
    return ""


def gmail_thread(thread_id: str, account: str) -> dict:
    """The whole thread (``format=full``): from the synced mail store when it is current and
    holds the thread, else from Gmail."""
    local = mail_store.thread(thread_id, account)
    if local is not None:
        return local
    return gmail_get(f"threads/{thread_id}", account, {"format": "full"})


def clip(text: str, limit: int) -> str:
    """Text cut to ``limit`` characters, saying so when it was: a reader told only part of an
    email knows there is more instead of taking the part for the whole (the user, Nov 15: the
    unlock schedule was cut mid-table and the assistant drafted from half of it)."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more characters of this message not shown]"


def thread_messages(thread: dict, limit: int = 6000, mark_cut: bool = False) -> list[dict]:
    """Simplified messages of a Gmail thread, oldest first."""
    out = []
    mine = config.my_emails()
    for m in thread.get("messages", []) or []:
        from_value = header(m, "From")
        addrs = [a.lower() for _, a in getaddresses([from_value])]
        labels = m.get("labelIds") or []
        text = strip_quoted(_plain_text(m.get("payload", {})))
        out.append(
            {
                "id": m.get("id"),
                "from": from_value,
                "from_me": any(a in mine for a in addrs) or "SENT" in labels,
                # An unsent draft of the user's sits in the thread but was never sent.
                "draft": "DRAFT" in labels,
                "to": header(m, "To"),
                "cc": header(m, "Cc"),
                "subject": header(m, "Subject"),
                "date": header(m, "Date"),
                "internal_ms": int(m.get("internalDate") or 0),
                "message_id": header(m, "Message-ID") or header(m, "Message-Id"),
                "references": header(m, "References"),
                "text": clip(text, limit) if mark_cut else text[:limit],
            }
        )
    return out


def gmail_search(query: str, account: str, limit: int = 5) -> list[dict]:
    listed = gmail_get("messages", account, {"q": query, "maxResults": str(limit)})
    out = []
    for ref in listed.get("messages", []) or []:
        m = gmail_get(f"messages/{ref['id']}", account, {"format": "metadata"})
        out.append(
            {
                "from": header(m, "From"),
                "to": header(m, "To"),
                "subject": header(m, "Subject"),
                "date": header(m, "Date"),
                "snippet": m.get("snippet", ""),
                "thread_id": m.get("threadId", ""),
            }
        )
    return out


def find_address(name: str, accounts: tuple[str, ...]) -> str:
    """The email address the user's mail most often pairs with this person's name ("" if unsure)."""
    tokens = [t.lower() for t in re.findall(r"[A-Za-z]+", name)]
    if not tokens:
        return ""
    counts: dict[str, int] = {}
    mine = config.my_emails()
    for account in accounts:
        try:
            listed = gmail_get(
                "messages",
                account,
                {"q": f'"{name}" -category:promotions', "maxResults": "8"},
            )
            for ref in listed.get("messages", []) or []:
                m = gmail_get(f"messages/{ref['id']}", account, {"format": "metadata"})
                for hname in ("From", "To", "Cc"):
                    for display, address in getaddresses([header(m, hname)]):
                        d = display.lower()
                        if (
                            address
                            and address.lower() not in mine
                            and all(t in d for t in tokens)
                        ):
                            counts[address.lower()] = counts.get(address.lower(), 0) + 1
        except ConnectorError:
            continue
    if not counts:
        return ""
    best = max(counts, key=lambda a: counts[a])
    # Only a single-word name that matched several people is too ambiguous to guess.
    return "" if len(tokens) == 1 and len(counts) > 1 else best


def html_body(text: str) -> str:
    """The plain text as simple HTML, every line break kept: some mail apps join single line
    breaks in plain text (the user, 2026-11-13: the user's sign-off ran onto the line above)."""
    paras = re.split(r"\n{2,}", text.strip("\n"))
    return "".join(
        f'<div>{"<br>".join(htmllib.escape(line) for line in p.split(chr(10)))}</div><div><br></div>' for p in paras
    ).removesuffix("<div><br></div>")


def set_body(msg: EmailMessage, text: str) -> None:
    """Plain text with an HTML copy beside it, as Gmail itself sends."""
    msg.set_content(text)
    msg.add_alternative(html_body(text), subtype="html")


def gmail_send_new(account: str, to: str, cc: str, subject: str, text: str) -> dict:
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    set_body(msg, text)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    data = _curl(
        [
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"raw": raw}),
            f"{GMAIL}/messages/send",
        ],
        account=account,
    )
    if "error" in data:
        raise ConnectorError(f"gmail send: {str(data['error'])[:300]}")
    return data


def gmail_send_reply(
    account: str,
    thread_id: str,
    to: str,
    cc: str,
    subject: str,
    in_reply_to: str,
    references: str,
    text: str,
) -> dict:
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip()
    set_body(msg, text)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    data = _curl(
        [
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(
                {"raw": raw, "threadId": thread_id} if thread_id else {"raw": raw}
            ),
            f"{GMAIL}/messages/send",
        ],
        account=account,
    )
    if "error" in data:
        raise ConnectorError(f"gmail send: {str(data['error'])[:300]}")
    return data


def gmail_save_draft(
    account: str,
    thread_id: str,
    to: str,
    cc: str,
    subject: str,
    in_reply_to: str,
    references: str,
    text: str,
    draft_id: str = "",
) -> dict:
    """Put a message in the user's Gmail drafts (in its thread when it is a reply); nothing is
    sent. With ``draft_id``, that draft is updated in place; if it is gone (the user sent or
    deleted it), a new one is made."""
    msg = EmailMessage()
    # A draft needs no address yet (Gmail keeps drafts with an empty To).
    if to:
        msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (
            references
            if in_reply_to in references
            else (references + " " + in_reply_to).strip()
        )
    set_body(msg, text)
    message: dict[str, str] = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        message["threadId"] = thread_id
    if draft_id:
        data = _curl(
            [
                "-X",
                "PUT",
                "-H",
                "Content-Type: application/json",
                "-d",
                json.dumps({"id": draft_id, "message": message}),
                f"{GMAIL}/drafts/{draft_id}",
            ],
            account=account,
        )
        if "error" not in data:
            return data
    data = _curl(
        [
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"message": message}),
            f"{GMAIL}/drafts",
        ],
        account=account,
    )
    if "error" in data:
        raise ConnectorError(f"gmail draft: {str(data['error'])[:300]}")
    return data


def gmail_find_draft(account: str, message_id: str) -> str:
    """The draft id holding this message (Gmail addresses drafts by their own ids)."""
    token = ""
    for _ in range(10):
        params = {"maxResults": "500"} | ({"pageToken": token} if token else {})
        listed = gmail_get("drafts", account, params)
        for d in listed.get("drafts", []) or []:
            if (d.get("message") or {}).get("id") == message_id:
                return str(d["id"])
        token = listed.get("nextPageToken", "")
        if not token:
            break
    raise ConnectorError("that draft is no longer in your Gmail drafts")


def gmail_rewrite_draft(account: str, draft_id: str, text: str) -> dict:
    """Replace a draft's text, keeping who it goes to, its subject and its thread."""
    d = gmail_get(f"drafts/{draft_id}", account, {"format": "full"})
    m = d.get("message") or {}
    return gmail_save_draft(
        account,
        m.get("threadId", ""),
        header(m, "To"),
        header(m, "Cc"),
        header(m, "Subject"),
        header(m, "In-Reply-To"),
        header(m, "References"),
        text,
        draft_id=draft_id,
    )


def gmail_set_inbox(account: str, thread_id: str, inbox: bool) -> None:
    """Archive a thread (take it out of the inbox), or put it back. Nothing is deleted."""
    change = {"addLabelIds" if inbox else "removeLabelIds": ["INBOX"]}
    data = _curl(
        [
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(change),
            f"{GMAIL}/threads/{thread_id}/modify",
        ],
        account=account,
    )
    if "error" in data:
        raise ConnectorError(f"gmail archive: {str(data['error'])[:300]}")


def gmail_send_draft(account: str, draft_id: str) -> dict:
    data = _curl(
        [
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"id": draft_id}),
            f"{GMAIL}/drafts/send",
        ],
        account=account,
    )
    if "error" in data:
        raise ConnectorError(f"gmail send: {str(data['error'])[:300]}")
    return data
