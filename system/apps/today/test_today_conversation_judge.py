"""The per-conversation judge (conversation_judge.py): it shows the whole conversation with
message labels and the tracked commitments, and applies the answer as judge events: new
commitments linked to their messages, changes only against the version it read, the user's own
decisions kept unless someone wrote since. The AI is stubbed."""

import sqlite3
import time
from collections.abc import Generator
from pathlib import Path

import pytest
from today import (
    ai,
    commitments,
    conversation_judge,
    conversations,
    store,
    user_rules,
)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[sqlite3.Connection, None, None]:
    monkeypatch.setattr(conversation_judge, "_sent", lambda since: [])  # never the user's real mail or Slack
    conversations.save_record(tmp_path, "text:c:1", {
        "source": "text", "chat_id": "c", "channel_label": "WhatsApp text with Theo",
        "messages": [
            {"id": "1", "ts": 1000.0, "name": "the user", "text": "Could you find us a circling time?", "is_me": True},
            {"id": "2", "ts": 2000.0, "name": "Theo", "text": "Sure, Dec 6 works", "is_me": False},
        ],
    })
    with conversations.opened(tmp_path) as conn:
        yield conn


def tracked(db: sqlite3.Connection, actor: str = "import") -> str:
    return commitments.create(
        db, actor,
        {"title": "Wait for Theo to confirm a circling time", "direction": "theirs", "origin": "user_asked",
         "started_by": "user", "counterparty": "Theo"},
        links=[("text:c", "1", "origin")],
    )


def test_context_labels_messages_and_shows_tracked(db: sqlite3.Connection) -> None:
    cid = tracked(db)
    body, labels, shown = conversation_judge.context(db, "text:c")
    assert labels == {"m1": "1", "m2": "2"}
    assert "[m2]" in body and "Theo: Sure, Dec 6 works" in body and "the user: Could you" in body
    assert [c["id"] for c in shown] == [cid] and '"from_message": "m1"' in body


def test_apply_changes_and_creates_with_evidence(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = tracked(db)
    answer = {"changes": [
        {"commitment": cid, "stage": "done", "reason": "Theo picked Dec 6", "evidence": ["m2"]},
        {"commitment": "new", "title": "Put circling on Dec 6 in your calendar", "direction": "self",
         "origin": "user_chose", "stage": "open", "due": "2026-12-06", "due_kind": "stated",
         "due_quote": "Dec 6 works", "reason": "", "evidence": ["m2"]},
        {"commitment": "cdoesnotexist", "stage": "done", "evidence": ["m2"]},
    ]}
    monkeypatch.setattr(ai, "complete_json", lambda prompt, system, model: answer)
    out = conversation_judge.apply(db, "text:c", conversation_judge.judge(db, "text:c"))
    assert [o["outcome"].split(":")[0] for o in out] == ["changed", "created", "skipped"]
    c = commitments.get(db, cid)
    assert c and c["stage"] == "done" and c["stage_reason"] == "Theo picked Dec 6"
    last = commitments.history(db, cid)[-1]
    assert last["actor"] == "judge" and last["evidence"] == ["text:c#2"]
    new = commitments.get(db, out[1]["id"])
    assert new and new["due"] == "2026-12-06" and new["detail"]["due_quote"] == "Dec 6 works"
    assert commitments.conversations_of(db, new["id"]) == ["text:c"]


def test_a_click_meanwhile_wins(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = tracked(db)
    monkeypatch.setattr(ai, "complete_json", lambda prompt, system, model: {"changes": [
        {"commitment": cid, "stage": "stale", "reason": "x", "evidence": ["m2"]}]})
    result = conversation_judge.judge(db, "text:c")
    commitments.change(db, cid, "user", {"stage": "done"})  # the user checks it off while the judge thinks
    out = conversation_judge.apply(db, "text:c", result)
    assert out[0]["outcome"].startswith("stale")
    c = commitments.get(db, cid)
    assert c and c["stage"] == "done"


def test_user_decision_is_not_reopened_without_a_new_message(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = tracked(db)
    commitments.change(db, cid, "user", {"stage": "done"})
    monkeypatch.setattr(ai, "complete_json", lambda prompt, system, model: {"changes": [
        {"commitment": cid, "stage": "open", "reason": "x", "evidence": ["m2"]}]})
    out = conversation_judge.apply(db, "text:c", conversation_judge.judge(db, "text:c"))
    assert out[0]["outcome"].startswith("refused")


def test_a_promise_kept_in_another_conversation_is_shown_and_can_be_cited(db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The user, 2026-11-14: "I'll tell Robin" in one DM, then a message to Robin in the user's:
    the judge sees what the user wrote there and can close the promise citing it."""
    now = time.time()
    conversations.save_record(tmp_path, "slack:D2:1", {
        "source": "slack", "channel": "D2", "channel_label": "direct message",
        "messages": [
            {"ts": f"{now - 120:.6f}", "user": "UB", "name": "Robin", "text": "hi"},
            {"ts": f"{now - 60:.6f}", "user": conversations.SLACK_ME, "name": "the user", "text": "Theo would love a circling time next week"},
        ],
    })
    cid = commitments.create(db, "judge", {"title": "Tell Robin to book circling with Theo", "direction": "mine",
                                           "origin": "user_promised", "started_by": "user", "counterparty": "Theo"},
                             links=[("text:c", "2", "origin")])
    body, labels, _ = conversation_judge.context(db, "text:c")
    assert labels["e1"] == f"slack:D2#{now - 60:.6f}" and "[e1]" in body and "with Robin" in body
    monkeypatch.setattr(ai, "complete_json", lambda prompt, system, model: {"changes": [
        {"commitment": cid, "stage": "done", "reason": "you told Robin", "evidence": ["e1"]}]})
    out = conversation_judge.apply(db, "text:c", conversation_judge.judge(db, "text:c"))
    assert out[0]["outcome"] == "changed"
    assert commitments.history(db, cid)[-1]["evidence"] == [f"slack:D2#{now - 60:.6f}"]


def promise(db: sqlite3.Connection, title: str, who: str) -> str:
    return commitments.create(db, "judge", {"title": title, "direction": "mine", "origin": "user_promised",
                                            "started_by": "user", "counterparty": who}, links=[("text:c", "2", "origin")])


def test_a_promise_to_schedule_is_checked_against_user_calendar(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """The user, 2026-11-14: a promise to schedule is checked where it would be kept, the user
    calendar. Old events never push the upcoming ones out, and each says when it was added."""
    cid = promise(db, "Schedule circling with Theo", "Theo")
    now = time.time()
    day = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))  # noqa: E731
    old = [{"id": f"o{n}", "summary": "Standup", "start": {"dateTime": day(now - 6 * 86400 + n)}} for n in range(300)]
    store.save_raw("calendar", {"events": old + [
        {"id": "ev1", "summary": "Circling", "start": {"dateTime": day(now + 9 * 86400)},
         "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
         "attendees": [{"self": True}, {"displayName": "Theo Lund", "email": "theo@x.org"}]}]})
    body, labels, _ = conversation_judge.context(db, "text:c")
    label = next(k for k, v in labels.items() if v == "calendar#ev1")
    assert f"[{label}]" in body and "Circling (with Theo Lund) [added" in body
    monkeypatch.setattr(ai, "complete_json", lambda prompt, system, model: {"changes": [
        {"commitment": cid, "stage": "done", "reason": "circling is on your calendar", "evidence": [label]}]})
    conversation_judge.apply(db, "text:c", conversation_judge.judge(db, "text:c"))
    after = commitments.get(db, cid)
    assert after is not None and after["stage"] == "done"
    assert commitments.history(db, cid)[-1]["evidence"] == ["calendar#ev1"]


def test_a_promise_to_email_is_checked_against_user_sent_mail(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """An email the user sent in a thread Today never saved still shows, so "I'll send Nora the
    deck" closes when the email to Nora went out; mail to someone else stays out."""
    promise(db, "Send Nora the deck", "Nora")
    now = time.time()
    sent = [
        {"where": "email sent from alex@acme.example", "subject": "Deck", "thread_id": "t9", "ts": now - 30,
         "blob": "the user <alex@acme.example> Nora Quinn <l@x.org>  Deck  Here's the deck!"},
        {"where": "email sent from alex@acme.example", "subject": "Lunch", "thread_id": "t8", "ts": now - 20,
         "blob": "the user <alex@acme.example> Sam <s@x.org>  Lunch  Tuesday?"},
    ]
    monkeypatch.setattr(conversation_judge, "_sent", lambda since: sent)
    body, labels, _ = conversation_judge.context(db, "text:c")
    assert labels["e1"] == f"email:alex@acme.example:t9#{now - 30}" and 'in email "Deck"' in body
    assert "e2" not in labels and "Tuesday?" not in body


def test_the_judge_sorts_user_tasks_into_lists(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """The judge sees which list each task of the user's is in and moves it when that's wrong
    (the user, 2026-11-14: the judge sorts, as it reads each commitment)."""
    cid = commitments.create(
        db, "import",
        {"title": "Tell Theo whether Dec 6 works", "direction": "mine", "origin": "they_asked", "started_by": "them",
         "counterparty": "Theo", "detail": {"state": "ready"}},
        links=[("text:c", "2", "origin")],
    )
    body, _, _ = conversation_judge.context(db, "text:c")
    assert '"state": "ready"' in body and "Quick answers" in conversation_judge.SYSTEM
    answer = {"changes": [{"commitment": cid, "state": "you", "reason": "the user has to say if the user can go", "evidence": ["m2"]}]}
    monkeypatch.setattr(ai, "complete_json", lambda prompt, system, model: answer)
    out = conversation_judge.apply(db, "text:c", conversation_judge.judge(db, "text:c"))
    assert out[0]["outcome"] == "changed"
    c = commitments.get(db, cid)
    assert c and c["stage"] == "open" and c["detail"]["state"] == "you"


def test_user_rules_live_in_one_place_read_by_the_judge() -> None:
    """The user, 2026-11-15: the user's rules have their own place; the judge reads them from there (and
    the one assistant, when it writes a card ahead: test_today_act)."""
    assert user_rules.RULES.startswith("What the user wants from this")
    assert user_rules.RULES in conversation_judge.SYSTEM


def test_a_task_made_from_a_new_message_is_the_judges_to_decide(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """The old reply writer is gone (the user, 2026-11-15): a task made from a new message is
    marked unread, and the judge says whether it is real, what it is and whether it is a pitch."""
    real = commitments.create(db, "sync", {"title": "Sure, Dec 6 works", "direction": "mine", "origin": "they_asked",
                                           "started_by": "them", "counterparty": "", "detail": {"unread": True}},
                              links=[("text:c", "2", "origin")])
    noise = commitments.create(db, "sync", {"title": "Could you find us a circling time?", "direction": "mine",
                                            "origin": "they_asked", "started_by": "them", "detail": {"unread": True}},
                               links=[("text:c", "1", "origin")])
    commitments.create(db, "user", {"title": "Speak at the Acme summit", "direction": "mine", "origin": "they_asked",
                                      "started_by": "them", "counterparty": "Bea", "stage": "declined"})
    body, _, _ = conversation_judge.context(db, "text:c")
    assert body.count('"unread": true') == 2 and "The user already said no to:\n- Speak at the Acme summit (with Bea)" in body
    assert '"unread"' in conversation_judge.SYSTEM and '"pitch"' in conversation_judge.SYSTEM
    answer = {"changes": [
        {"commitment": real, "title": "Book circling with Theo on Dec 6", "counterparty": "Theo", "state": "mine",
         "pitch": False, "evidence": ["m2"]},
        {"commitment": noise, "stage": "stale", "reason": "the user's own ask, Theo answered it", "evidence": ["m2"]},
    ]}
    monkeypatch.setattr(ai, "complete_json", lambda prompt, system, model: answer)
    conversation_judge.apply(db, "text:c", conversation_judge.judge(db, "text:c"))
    r, n = commitments.get(db, real), commitments.get(db, noise)
    assert r and r["title"] == "Book circling with Theo on Dec 6" and r["detail"]["state"] == "mine"
    assert "unread" not in r["detail"] and "pitch" not in r["labels"]
    assert n and n["stage"] == "stale" and "unread" not in n["detail"]


def test_the_judge_marks_a_pitch(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = commitments.create(db, "sync", {"title": "Reply to Theo", "direction": "mine", "origin": "they_asked",
                                          "started_by": "them", "detail": {"unread": True}}, links=[("text:c", "2", "origin")])
    monkeypatch.setattr(ai, "complete_json", lambda prompt, system, model: {"changes": [
        {"commitment": cid, "pitch": True, "evidence": ["m2"]}]})
    conversation_judge.apply(db, "text:c", conversation_judge.judge(db, "text:c"))
    c = commitments.get(db, cid)
    assert c and c["labels"] == ["pitch"] and "unread" not in c["detail"]
    # Left as it was, an unread one is read all the same.
    other = commitments.create(db, "sync", {"title": "Tell Theo yes", "direction": "mine", "origin": "they_asked",
                                            "started_by": "them", "detail": {"unread": True}}, links=[("text:c", "2", "origin")])
    monkeypatch.setattr(ai, "complete_json", lambda prompt, system, model: {"changes": []})
    conversation_judge.apply(db, "text:c", conversation_judge.judge(db, "text:c"))
    o = commitments.get(db, other)
    assert o and "unread" not in o["detail"] and o["stage"] == "open"


def test_a_signing_notice_in_either_inbox_closes_a_promise_to_sign(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """The user, 2026-11-16: the Fund I side letter's DocuSign "Completed" notice sat in the user
    personal Gmail while the judge, reading only the email thread, kept "Sign the side letter" open."""
    asked: list[float] = []
    notice = {"where": "email:alex.rivera@mail.example:t1", "date": "Tue, 10 Nov 2026", "from": "Greta via Docusign",
              "subject": "Completed: Northwind - Side Letter (the user - Fund I)", "text": "All parties have completed"}
    monkeypatch.setattr(conversation_judge, "_notices", lambda since: asked.append(since) or [notice])
    body, labels, _ = conversation_judge.context(db, "text:c")
    assert asked == [] and "[s1]" not in body  # nothing here is about signing: no search
    cid = promise(db, "Sign the side letter DocuSign", "Hugo")
    body, labels, _ = conversation_judge.context(db, "text:c")
    assert asked == [1000.0] and labels["s1"] == "email:alex.rivera@mail.example:t1"
    assert "[s1] Tue, 10 Nov 2026 from Greta via Docusign: Completed: Northwind - Side Letter" in body
    assert '"Signing notices"' in conversation_judge.SYSTEM
    monkeypatch.setattr(ai, "complete_json", lambda prompt, system, model: {"changes": [
        {"commitment": cid, "stage": "done", "reason": "the side letter was completed", "evidence": ["s1"]}]})
    conversation_judge.apply(db, "text:c", conversation_judge.judge(db, "text:c"))
    after = commitments.get(db, cid)
    assert after and after["stage"] == "done"
    assert commitments.history(db, cid)[-1]["evidence"] == ["email:alex.rivera@mail.example:t1"]


def test_a_reply_in_a_slack_thread_says_which_thread_it_answers(tmp_path: Path) -> None:
    """The user, 2026-11-17: the user "yes thank you" on Robin's 409a thread sat between replies in
    another thread and was read as part of it; the ask stayed open."""
    conversations.save_record(tmp_path, "slack:D1:1.0", {"source": "slack", "channel": "D1", "messages": [
        {"ts": "1.0", "thread_ts": "1.0", "name": "Robin", "text": "Ok to docusign this?"},
        {"ts": "2.0", "thread_ts": "2.0", "name": "Robin", "text": "#1 Do you know this person?"},
        {"ts": "3.0", "thread_ts": "1.0", "name": "the user", "is_me": True, "text": "yes thank you"},
        {"ts": "4.0", "thread_ts": "2.0", "name": "the user", "is_me": True, "text": "1. yes"},
        {"ts": "5.0", "thread_ts": "0.5", "name": "Robin", "text": "a reply to a thread not kept"},
    ]})
    with conversations.opened(tmp_path) as db:
        conv = db.execute("SELECT conversation_id FROM messages LIMIT 1").fetchone()[0]
        body, _, _ = conversation_judge.context(db, conv)
    assert "[m3] " in body and "the user (replying in the thread of [m1]): yes thank you" in body
    assert "the user (replying in the thread of [m2]): 1. yes" in body
    assert "Robin (in a thread started " in body
    assert "Robin: Ok to docusign this?" in body  # a thread's first message is not a reply
