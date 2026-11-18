"""A redo opens the links in the user's notes, so it can describe what they show."""

import json
from pathlib import Path
from typing import Any

import pytest
from today import act, connectors, link_pages, links, work

POST = "https://x.com/patnolan/status/2113451268316205515?s=20"


def test_finds_links_and_drops_trailing_punctuation() -> None:
    text = f"Also add this launch: {POST}. And https://tako.com, please"
    assert links.find(text) == [POST, "https://tako.com"]


def test_x_post_is_read_with_the_post_it_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror = {
        "tweet": {
            "author": {"name": "Pat Nolan", "screen_name": "patnolan"},
            "text": "Coolest Generative UI example.",
            "quote": {
                "author": {"name": "Vercel Developers", "screen_name": "vercel_dev"},
                "text": "@TakoViz, now on AI Gateway",
                "url": "https://x.com/vercel_dev/status/1",
            },
        }
    }
    seen: list[str] = []

    def get(url: str) -> tuple[str, str]:
        seen.append(url)
        return "application/json", json.dumps(mirror)

    monkeypatch.setattr(link_pages, "_get", get)
    page = link_pages.read(POST)
    assert seen == ["https://api.fxtwitter.com/status/2113451268316205515"]
    assert "Pat Nolan (@patnolan): Coolest" in page["text"]
    assert "Quoting Vercel Developers (@vercel_dev): @TakoViz" in page["text"]


def test_the_cards_assistant_reads_the_links_user_gives_it(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []
    monkeypatch.setattr(link_pages, "read_all", lambda text: [{"url": POST, "title": "Post on X", "text": "TAKO LAUNCH TEXT"}])
    monkeypatch.setattr(act, "_state", lambda key, item, focus: {"title": "x"})
    monkeypatch.setattr(act.calendar_events, "brief", lambda **_: "")

    def complete_json(prompt: str, **_k: Any) -> dict[str, Any]:
        prompts.append(prompt)
        return {"did": "", "operations": [], "next": []}

    monkeypatch.setattr(act.ai, "complete_json", complete_json)
    act._ask(act._task_prompt("k", {"key": "k", "source": "slack"}, "", f"Add this launch: {POST}")[0])
    assert "TAKO LAUNCH TEXT" in prompts[0] and "put a link the user asks to share in exactly" in prompts[0]


def test_slack_threads_are_kept_apart() -> None:
    """Replies from two threads in one channel are never interleaved as one exchange."""
    raw = {
        "source": "slack",
        "channel_label": "channel",
        "messages": [
            {
                "ts": "1",
                "name": "Sam",
                "text": "export idea",
                "thread_ts": "1",
            },
            {"ts": "2", "name": "Priya", "text": "uri scheme", "thread_ts": "2"},
            {"ts": "3", "name": "Sam", "text": "re uri", "thread_ts": "2"},
            {"ts": "4", "name": "Jordan", "text": "re export", "thread_ts": "1"},
            {"ts": "5", "name": "Priya", "text": "ci is faster", "thread_ts": ""},
        ],
    }
    groups = work.threads(raw, "4")
    assert [g["label"].split(",")[0] for g in groups] == [
        "The thread the ask is in",
        "A separate thread",
        "Elsewhere in the channel",
    ]
    assert [m["text"] for m in groups[0]["messages"]] == ["export idea", "re export"]
    text = work.transcript(raw, "4")
    assert text.index("re export") < text.index("--- A separate thread") and "] Jordan: re export" in text
    assert text.count(">> ") == 1 and ">> [" in text.split("Jordan")[0].rsplit("\n", 1)[-1]
    # A conversation saved before threads were recorded reads as it was saved.
    old = {"source": "slack", "messages": [{"ts": "1", "name": "Sam", "text": "hi"}]}
    assert work.threads(old) == [{"label": "", "messages": old["messages"]}]


def test_a_notion_link_opens_through_user_notion_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connectors, "notion_fetch", lambda url: f"page at {url}")
    page = link_pages.read("https://app.notion.com/p/acme/x-1?a=1&amp;b=2|Here")
    assert page["text"] == "page at https://app.notion.com/p/acme/x-1?a=1&b=2"

    def down(url: str) -> str:
        raise connectors.ConnectorError("Notion answered 401")

    monkeypatch.setattr(connectors, "notion_fetch", down)
    assert "could not open it" in link_pages.read("https://www.notion.so/abc")["error"]


def test_one_place_knows_what_a_link_looks_like() -> None:
    """The user, 2026-11-15: one shared copy of the link code. Finding, tidying and writing out
    links is links.py; every other part uses it."""
    slack = "Here's the doc: <https://app.notion.com/p/x?a=1&amp;b=2|Here>, and https://tako.com."
    assert links.find(slack) == ["https://app.notion.com/p/x?a=1&b=2", "https://tako.com"]
    assert links.readable(slack) == "Here's the doc: Here (https://app.notion.com/p/x?a=1&amp;b=2), and https://tako.com."
    assert links.readable(slack, urls=False).startswith("Here's the doc: Here, and")
    assert links.clean("<https://x.com/a|label>") == "https://x.com/a" and links.clean("no link") == ""
    assert links.is_link(" https://a.b ") and not links.is_link("see https://a.b")
    # The AI reads a Slack link as its word and address, wherever it reads a conversation.
    raw = {"source": "slack", "channel_label": "#trip", "messages": [
        {"ts": "1", "name": "Nina", "text": "<https://app.notion.com/p/nz|Here> is the doc"}]}
    assert "Here (https://app.notion.com/p/nz) is the doc" in work.transcript(raw)
    src = Path(__file__).parent / "src" / "today"
    copies = [p.name for p in src.glob("*.py") if p.name != "links.py" and "re.compile(r\"<(https?" in p.read_text()]
    assert copies == []
