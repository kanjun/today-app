"""Who Today works for and what it reads come from settings; a missing source is an empty list, not a crash."""

import json
from pathlib import Path

import pytest
from today import actions, config, gtd_link, store, sync, work

CAPTURE = """---
id: gtd-1
title: Send Sam the deck
status: inbox
source: slack
source_url: https://acme.slack.com/archives/C0123/p1760000000000100
source_kind: asked
source_person: Sam
created_at: '2026-10-09T10:00:00'
---
Sam asked: "Can you send the deck?" (#general)
"""


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in (
        "TODAY_USER_NAME",
        "TODAY_USER_ORG",
        "TODAY_USER_ROLE",
        "TODAY_EMAIL_ACCOUNTS",
        "TODAY_EMAIL_ALIASES",
        "TODAY_EMAIL_DIGESTS",
        "TODAY_SLACK_USERS",
        "TODAY_GTD_ITEMS_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "today")
    return tmp_path


def _settings(data: Path, **fields: object) -> None:
    (data / "today").mkdir(parents=True, exist_ok=True)
    (data / "today" / "settings.json").write_text(json.dumps(fields))


def test_nothing_set_means_a_neutral_user_and_no_sources(data: Path) -> None:
    assert config.name() == "the user"
    assert config.possessive() == "the user's"
    assert config.accounts() == ()
    assert config.sources() == []
    assert sync.collect() == []
    assert "the user" in work.system()


def test_the_page_asks_to_connect_a_source(data: Path) -> None:
    view = actions.view_state()
    assert view["has_source"] is False
    assert view["sources"] == []
    assert view["owed"] == [] and view["goals"] == []


def test_settings_file_names_the_user_and_env_overrides_it(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(data, user_name="Alex Rivera", user_org="Acme", user_role="CEO")
    assert config.who_they_are() == "Alex Rivera, CEO at Acme"
    assert "Alex" in work.system() and "the user" not in work.system()
    monkeypatch.setenv("TODAY_USER_NAME", "Jordan Park")
    assert config.name() == "Jordan"


def test_a_configured_but_missing_digest_is_reported_and_skipped(data: Path) -> None:
    _settings(
        data,
        email_digests=[
            {"account": "alex@acme.example", "path": str(data / "none.json")}
        ],
    )
    assert sync.collect() == []
    [source] = config.sources()
    assert source["found"] is False
    assert actions.view_state()["has_source"] is False


def test_owed_threads_in_a_digest_become_items(data: Path) -> None:
    digest = data / "digest.json"
    digest.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "threadId": "t1",
                        "final_bucket": 1,
                        "from": "Sam Lee <sam@example.com>",
                        "date": "Thu, 8 Oct 2026 10:00:00 -0700",
                        "subject": "Deck",
                        "snippet": "Can you send it?",
                    },
                    {"threadId": "t2", "final_bucket": "7", "subject": "Newsletter"},
                ]
            }
        )
    )
    _settings(
        data, email_digests=[{"account": "alex@acme.example", "path": str(digest)}]
    )
    [item] = sync.collect()
    assert item["key"] == "email:alex@acme.example:t1"
    assert item["who"] == "Sam"
    assert item["url"].endswith("/u/alex@acme.example/#all/t1")
    assert config.accounts() == ("alex@acme.example",)
    assert actions.view_state()["has_source"] is True


def test_slack_captures_are_read_from_the_configured_folder(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = data / "captures"
    folder.mkdir()
    (folder / "gtd-1.md").write_text(CAPTURE)
    monkeypatch.setenv("TODAY_GTD_ITEMS_DIR", str(folder))
    [item] = sync.collect()
    assert item["key"] == "slack:C0123:1760000000.000100"
    assert item["ask_text"] == "Can you send the deck?"
    assert gtd_link.mark_done("gtd-1", "Sent it.")
    assert "status: done" in (folder / "gtd-1.md").read_text()


def test_env_digests_are_account_path_pairs(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "TODAY_EMAIL_DIGESTS", "a@x.example=one.json, b@x.example=two.json"
    )
    assert config.load().email_digests == (
        ("a@x.example", Path("one.json")),
        ("b@x.example", Path("two.json")),
    )


def test_sending_a_new_email_needs_an_account(data: Path) -> None:
    with pytest.raises(actions.SendError):
        actions._account({"source": "slack"})


def test_an_empty_list_never_calls_the_model(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_model(*args: object, **kwargs: object) -> dict:
        raise AssertionError("the model was called with nothing to do")

    monkeypatch.setattr(sync.ai, "complete_json", no_model)
    counts = sync.goals_phase(consolidate_now=True)
    assert counts["duplicates"] == 0 and counts["rewritten"] == 0
