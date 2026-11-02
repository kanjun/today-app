"""A redo opens the links in the user's notes, so it can describe what they show."""

import json
from typing import Any

import pytest
from today import links, work

POST = "https://x.com/rauchg/status/2113451268316205515?s=20"


def test_finds_links_and_drops_trailing_punctuation() -> None:
    text = f"Also add this launch: {POST}. And https://tako.com, please"
    assert links.urls_in(text) == [POST, "https://tako.com"]


def test_x_post_is_read_with_the_post_it_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror = {
        "tweet": {
            "author": {"name": "Guillermo Rauch", "screen_name": "rauchg"},
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

    monkeypatch.setattr(links, "_get", get)
    page = links.read(POST)
    assert seen == ["https://api.fxtwitter.com/status/2113451268316205515"]
    assert "Guillermo Rauch (@rauchg): Coolest" in page["text"]
    assert "Quoting Vercel Developers (@vercel_dev): @TakoViz" in page["text"]


def test_redo_prompt_carries_the_linked_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []
    monkeypatch.setattr(
        links,
        "read_all",
        lambda text: [{"url": POST, "title": "Post on X", "text": "TAKO LAUNCH TEXT"}],
    )
    monkeypatch.setattr(work, "_prompt", lambda item, raw, extra: extra)
    monkeypatch.setattr(work, "_clean", lambda a: a)

    def complete_json(prompt: str, system: str) -> dict[str, Any]:
        prompts.append(prompt)
        return {"search": [], "emails": [], "state": "ready", "draft": "x"}

    monkeypatch.setattr(work.ai, "complete_json", complete_json)
    work.revise_draft({"feedback": []}, {}, f"Add this launch: {POST}")
    assert "TAKO LAUNCH TEXT" in prompts[0]
    assert "put that exact link in the message" in prompts[0]


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
                "is_ask": True,
            },
            {"ts": "2", "name": "Priya", "text": "uri scheme", "thread_ts": "2"},
            {"ts": "3", "name": "Sam", "text": "re uri", "thread_ts": "2"},
            {"ts": "4", "name": "Jordan", "text": "re export", "thread_ts": "1"},
            {"ts": "5", "name": "Priya", "text": "ci is faster", "thread_ts": ""},
        ],
    }
    groups = work.threads(raw)
    assert [g["label"].split(",")[0] for g in groups] == [
        "The thread the ask is in",
        "A separate thread",
        "Elsewhere in the channel",
    ]
    assert [m["text"] for m in groups[0]["messages"]] == ["export idea", "re export"]
    text = work.transcript(raw)
    assert text.index("re export") < text.index("--- A separate thread")
    # A conversation saved before threads were recorded reads as it was saved.
    old = {"source": "slack", "messages": [{"ts": "1", "name": "Sam", "text": "hi"}]}
    assert work.threads(old) == [{"label": "", "messages": old["messages"]}]
