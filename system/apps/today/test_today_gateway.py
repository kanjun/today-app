"""Today talks to the latchkey gateway directly (one kept-alive connection) instead of starting
a `latchkey curl` per call; a dry run still sends nothing, and without a gateway it falls
back to `latchkey curl`."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from today import connectors, store, sync


class Seen(list):  # the requests the gateway got, plus the answers it will give next
    replies: list[httpx.Response]


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Seen:
    seen = Seen()
    replies: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0) if replies else httpx.Response(200, json={"ok": True, "user_id": "U1"})

    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://gw:1989/")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "pw")
    monkeypatch.delenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", raising=False)
    monkeypatch.delenv("TODAY_DRY_SEND", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"x-latchkey-gateway-password": "pw"})
    monkeypatch.setattr(connectors, "_gateway_client", lambda: (client, "http://gw:1989"))
    monkeypatch.setattr(connectors._pause, "wait", lambda s: None)

    def no_subprocess(*a: Any, **k: Any) -> None:
        raise AssertionError("no latchkey process when the gateway is configured")

    monkeypatch.setattr(connectors.subprocess, "run", no_subprocess)
    seen.replies = replies
    return seen


def test_a_read_goes_straight_to_the_gateway(gateway: Seen) -> None:
    assert connectors.slack_get("auth.test", {})["user_id"] == "U1"
    (req,) = gateway
    assert req.method == "GET"
    assert str(req.url) == "http://gw:1989/gateway/https://slack.com/api/auth.test?"
    assert req.headers["x-latchkey-gateway-password"] == "pw"


def test_a_write_keeps_its_method_headers_body_and_account(gateway: Seen) -> None:
    connectors._curl(
        ["-X", "POST", "-H", "Content-Type: application/json", "-d", '{"a": 1}', "https://x.test/api"],
        account="alex@acme.example",
    )
    (req,) = gateway
    assert req.method == "POST"
    assert req.headers["content-type"] == "application/json"
    assert req.headers["x-latchkey-gateway-account"] == "alex@acme.example"
    assert json.loads(req.content) == {"a": 1}


def test_a_busy_answer_is_retried_then_succeeds(gateway: Seen) -> None:
    gateway.replies += [httpx.Response(503, text="busy"), httpx.Response(429, text="slow down")]
    assert connectors.slack_get("auth.test", {})["ok"]
    assert len(gateway) == 3


def test_an_api_error_still_reads_as_an_error(gateway: Seen) -> None:
    gateway.replies.append(httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))
    with pytest.raises(connectors.ConnectorError, match="channel_not_found"):
        connectors.slack_get("conversations.history", {"channel": "C0"})


def test_a_latchkey_failure_is_an_error(gateway: Seen) -> None:
    gateway.replies.append(httpx.Response(401, json={"latchkeyError": True, "error": "credentials expired"}))
    with pytest.raises(connectors.ConnectorError, match="credentials expired"):
        connectors._curl(["https://gmail.googleapis.com/x"], account="alex@acme.example")


def test_a_dry_run_sends_nothing_even_with_a_gateway(
    gateway: Seen, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TODAY_DRY_SEND", "1")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    assert connectors.slack_post_message("C1", "hi", None)["ok"]
    assert gateway == []
    assert "http POST" in (tmp_path / "dry_sends.jsonl").read_text()


def test_without_a_gateway_it_falls_back_to_latchkey_curl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)
    monkeypatch.delenv("TODAY_DRY_SEND", raising=False)
    ran: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = '{"ok": true}'
        stderr = ""

    def fake_run(cmd: list[str], **k: Any) -> Done:
        ran.append(cmd)
        return Done()

    monkeypatch.setattr(connectors.subprocess, "run", fake_run)
    assert connectors.slack_get("auth.test", {})["ok"]
    assert ran and ran[0][0] == "latchkey"


def test_a_slack_item_without_a_link_gets_slacks_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some promises were saved with no link; reading their conversation used to fail on
    every refresh ('url'). They now take Slack's permalink to the ask."""
    monkeypatch.setattr(
        connectors,
        "slack_get",
        lambda method, params: {"messages": [{"type": "message", "ts": "1.5", "user": "U2", "text": "hi"}]},
    )
    monkeypatch.setattr(connectors, "slack_permalink", lambda ch, ts: f"https://slack.test/{ch}/{ts}")
    item = {"ref": {"channel": "D1", "ts": "1.5"}}
    monkeypatch.setattr(sync.work, "_slack_me", lambda: "U1")
    raw = sync.slack_raw(item, {})
    assert raw["url"] == "https://slack.test/D1/1.5"
    # Which message a task came from is saved on the task, never marked in the conversation.
    assert "is_ask" not in raw["messages"][0]
