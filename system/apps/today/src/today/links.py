"""Links in what Today reads and writes: the one place that knows what a link looks like.

Every part of Today that finds, tidies or checks a link uses these, so a link reads the same
everywhere (the user, 2026-11-15: links must always come through):
- ``find``: every link in a piece of text, tidied, in order, once each;
- ``clean``: one link made clickable (a Slack label, ``&amp;`` and trailing punctuation dropped),
  or "" when it is not a link;
- ``readable``: text with Slack's ``<url|label>`` markup written out, as "label (url)" for the
  AI (so a link behind a word like "Here" is never missed) or as just the label for the user's screen;
- ``is_link``: whether a value is a web link at all.

Opening a link to read what it shows is ``link_pages`` (it reaches the network and the user
connected accounts; this module only reads text).
"""

import html as htmllib
import re

URL = re.compile(r"https?://[^\s<>\"')\]|]+")
SLACK_LINK = re.compile(r"<(https?://[^|>\s]+)(?:\|([^>]*))?>")  # Slack writes links as <url|label>
NOTION = re.compile(r"https?://([\w-]+\.)?notion\.(so|com|site)/", re.I)
X_POST = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/\w+/status/(\d+)")
TRAILING = ".,;:!?"


def clean(value: object) -> str:
    """The first link in ``value`` made clickable, or "" when there is none."""
    found = URL.search(htmllib.unescape(str(value or "")))
    return found.group(0).rstrip(TRAILING) if found else ""


def find(text: str) -> list[str]:
    """Every link in ``text`` (plain or Slack-style), tidied, in order, once each."""
    return list(dict.fromkeys(clean(u) for u in URL.findall(htmllib.unescape(str(text or "")))))


def readable(text: str, *, urls: bool = True) -> str:
    """Slack's link markup written out: "label (url)" (``urls``) or just the label, and a bare
    ``<url>`` as the url. Nothing else in the text changes."""
    def one(m: re.Match[str]) -> str:
        url, label = m.group(1), m.group(2) or ""
        if not label.strip() or label == url:
            return url
        return f"{label} ({url})" if urls else label

    return SLACK_LINK.sub(one, str(text or ""))


def is_link(value: object) -> bool:
    return bool(re.match(r"^https?://", str(value or "").strip()))
