"""Slack and Gmail through the user's connected accounts (``latchkey curl``).

Read calls are used by the sync; the two send calls are used only when the user
presses Send in the app. Gmail threads are read from the synced mail store
(mail_store.py) when it is current.
"""

import base64
import html as htmllib
import json
import re
import subprocess
from email.message import EmailMessage
from email.utils import getaddresses
from urllib.parse import urlencode

from today import config, mail_store


class ConnectorError(RuntimeError):
    pass


def _curl(args: list[str], account: str | None = None, timeout: int = 120) -> dict:
    prefix = (
        ["latchkey"]
        + (["--account", account] if account else [])
        + ["curl", "-s", "--retry", "4", "--retry-max-time", "90"]
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


def thread_messages(thread: dict) -> list[dict]:
    """Simplified messages of a Gmail thread, oldest first."""
    out = []
    mine = config.my_emails()
    for m in thread.get("messages", []) or []:
        from_value = header(m, "From")
        addrs = [a.lower() for _, a in getaddresses([from_value])]
        labels = m.get("labelIds") or []
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
                "text": strip_quoted(_plain_text(m.get("payload", {})))[:6000],
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


def gmail_send_new(account: str, to: str, cc: str, subject: str, text: str) -> dict:
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.set_content(text)
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
    msg.set_content(text)
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
    """Put a message in the user's Gmail drafts (in its thread when it is a reply);
    nothing is sent. With ``draft_id``, that draft is updated in place; if it is gone
    (sent or deleted in Gmail), a new one is made."""
    msg = EmailMessage()
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
    msg.set_content(text)
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
