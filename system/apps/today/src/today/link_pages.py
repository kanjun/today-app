"""Open a link and read what it shows (links.py finds and tidies links; this opens them).

Read-only. Public pages, and Notion pages through the user's connected Notion account. X posts load
through the public fxtwitter mirror, since x.com itself serves an empty page without a login.
"""

import html as htmllib
import json
import re
import urllib.error
import urllib.request

from today import connectors, links

MAX_LINKS = 4
MAX_CHARS = 6000
NOTION_CHARS = 20000  # a Notion doc the user is filling in is read whole, up to this
AGENT = "Mozilla/5.0 (compatible; TodayApp/1.0)"


def _get(url: str) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        kind = response.headers.get("Content-Type", "")
        return kind, response.read(400_000).decode("utf-8", "replace")


def _page_text(html: str) -> tuple[str, str]:
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    meta = re.search(
        r'(?is)<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']*)',
        html,
    )
    body = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", html)
    body = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", body)
    body = htmllib.unescape(re.sub(r"<[^>]+>", " ", body))
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n\n", body).strip()
    lead = htmllib.unescape(meta.group(1)) + "\n\n" if meta else ""
    return htmllib.unescape(title.group(1).strip()) if title else "", lead + body


def _x_post(post_id: str) -> dict[str, str]:
    _, text = _get(f"https://api.fxtwitter.com/status/{post_id}")
    tweet = json.loads(text).get("tweet") or {}

    def one(t: dict) -> str:
        who = (t.get("author") or {}).get("name", "")
        handle = (t.get("author") or {}).get("screen_name", "")
        return f"{who} (@{handle}): {t.get('text', '')}"

    out = one(tweet)
    if tweet.get("quote"):
        out += (
            "\n\nQuoting "
            + one(tweet["quote"])
            + f"\n({tweet['quote'].get('url', '')})"
        )
    return {"title": "Post on X", "text": out}


def read(url: str) -> dict[str, str]:
    """What a link shows, or why it could not be read. A Notion page is read through the user
    connected Notion account, so the user's private pages open too."""
    url = links.clean(url) or url
    try:
        post = links.X_POST.match(url)
        if links.NOTION.match(url):
            page = {"title": "", "text": connectors.notion_fetch(url)}
        elif post:
            page = _x_post(post.group(1))
        else:
            kind, body = _get(url)
            if "html" in kind:
                title, text = _page_text(body)
                page = {"title": title, "text": text}
            else:
                page = {"title": "", "text": body}
    except (urllib.error.URLError, OSError, ValueError, connectors.ConnectorError) as e:
        return {"url": url, "error": f"could not open it: {str(e)[:150]}"}
    cap = NOTION_CHARS if links.NOTION.match(url) else MAX_CHARS
    return {"url": url, "title": page["title"], "text": page["text"][:cap]}


def read_all(text: str) -> list[dict[str, str]]:
    return [read(u) for u in links.find(text)[:MAX_LINKS]]
