"""Read the web pages behind links in the user's notes, so a redo can describe what they show.

Public pages only, read-only. X posts load through the public fxtwitter mirror, since
x.com itself serves an empty page without a login.
"""

import html as htmllib
import json
import re
import urllib.error
import urllib.request

URL = re.compile(r"https?://[^\s<>\"')\]]+")
X_POST = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/\w+/status/(\d+)")
MAX_LINKS = 4
MAX_CHARS = 6000
AGENT = "Mozilla/5.0 (compatible; TodayApp/1.0)"


def urls_in(text: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;:!?") for u in URL.findall(text)))


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
    """What a link shows, or why it could not be read."""
    try:
        post = X_POST.match(url)
        if post:
            page = _x_post(post.group(1))
        else:
            kind, body = _get(url)
            if "html" in kind:
                title, text = _page_text(body)
                page = {"title": title, "text": text}
            else:
                page = {"title": "", "text": body}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"url": url, "error": f"could not open it: {str(e)[:150]}"}
    return {"url": url, "title": page["title"], "text": page["text"][:MAX_CHARS]}


def read_all(text: str) -> list[dict[str, str]]:
    return [read(u) for u in urls_in(text)[:MAX_LINKS]]
