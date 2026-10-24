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
