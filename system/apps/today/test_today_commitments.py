"""Commitments (commitments.py, commitment_lists.py, commitments_import.py): every change is
an event, a stale write is refused, the judge can't reopen the user's decision unless someone
wrote since, lists hide nothing inside projects, only other people's asks go quiet, and
the import carries items, projects, priorities and steps over."""

import sqlite3
from collections.abc import Generator
from datetime import date, datetime
from pathlib import Path

import pytest
from today import commitment_lists, commitments, commitments_import, conversations


@pytest.fixture
def db(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    with conversations.opened(tmp_path) as conn:
        yield conn


def todo(db: sqlite3.Connection, title: str = "Book the rental car", **extra: object) -> str:
    fields = {"title": title, "direction": "mine", "origin": "user_promised", "started_by": "user"}
    fields.update(extra)
    return commitments.create(db, "import", fields)


def test_every_change_is_an_event(db: sqlite3.Connection) -> None:
    cid = todo(db)
    commitments.change(db, cid, "user", {"stage": "done"}, reason="booked")
    hist = commitments.history(db, cid)
    assert [(e["actor"], e["kind"]) for e in hist] == [("import", "created"), ("user", "changed")]
    assert hist[1]["change"] == {"stage": "done"} and hist[1]["reason"] == "booked"
    c = commitments.get(db, cid)
    assert c and c["version"] == 1 and c["user_decision_at"]


def test_a_stale_write_is_refused(db: sqlite3.Connection) -> None:
    cid = todo(db)
    read = commitments.get(db, cid)
    assert read
    commitments.change(db, cid, "user", {"stage": "done"})
    with pytest.raises(commitments.StaleWrite):
        commitments.change(db, cid, "judge", {"title": "x"}, expect_version=read["version"])


def test_judge_reopens_only_after_someone_writes(db: sqlite3.Connection) -> None:
    store_dir = Path(db.execute("PRAGMA database_list").fetchone()[2]).parent
    conv = conversations.save_record(store_dir, "text:c:1", {
        "source": "text", "chat_id": "c", "channel_label": "Theo",
        "messages": [{"id": "1", "ts": 100.0, "name": "Theo", "text": "circling?", "is_me": False}],
    })
    cid = todo(db)
    commitments.link(db, cid, conv, "1", "origin")
    commitments.change(db, cid, "user", {"stage": "done"})
    with pytest.raises(commitments.Refused):
        commitments.change(db, cid, "judge", {"stage": "open"})
    later = datetime.now().timestamp() + 60
    db.execute(
        "INSERT INTO messages (conversation_id, source_id, at, sender, is_user) VALUES (?, '2', ?, 'Theo', 0)",
        (conv, later),
    )
    assert commitments.change(db, cid, "judge", {"stage": "open"})["stage"] == "open"
    assert [c["id"] for c in commitments.for_conversation(db, conv)] == [cid]
    assert commitments.conversations_of(db, cid) == [conv]


def test_bad_values_are_refused(db: sqlite3.Connection) -> None:
    with pytest.raises(commitments.CommitmentError):
        todo(db, direction="sideways")
    cid = todo(db)
    with pytest.raises(commitments.CommitmentError):
        commitments.change(db, cid, "user", {"colour": "red"})


def test_lists_hide_nothing_inside_projects_and_only_dated_promises_never_slip(db: sqlite3.Connection) -> None:
    proj = commitments.create(db, "import", {"title": "Onboarding", "direction": "self", "origin": "user_chose",
                                             "started_by": "user", "is_project": 1})
    old = {"asked_at": "2026-09-01T10:00:00", "state": "you"}
    todo(db, "Send Ravi his invite", parent_id=proj, origin="they_asked", started_by="them", detail=old)
    todo(db, "Book the rental car", counterparty="Glen", due="2026-11-15", detail=old)
    todo(db, "Tell Kofi about ACME holdings", counterparty="Kofi", detail=dict(old, due_event="IPO"))
    todo(db, "Make my Anki card maker", direction="self", origin="user_chose", detail=dict(old, asked_at="2026-07-26T10:00:00"))
    todo(db, "Ask Theo for a circling time", direction="theirs", origin="user_asked", detail=old)
    big = commitments.create(db, "import", {"title": "Draft onboarding", "direction": "self", "origin": "user_chose",
                                            "started_by": "user", "week": "2026-11-09",
                                            "detail": {"week_goal": "g1"}})
    commitments.create(db, "import", {"title": "Share personas", "direction": "self", "origin": "user_chose",
                                      "started_by": "user", "labels": ["step"], "parent_id": big})
    out = commitment_lists.lists(db, date(2026, 11, 13))
    # Oldest first; the user's own to-dos and other people's asks slip, project or not.
    assert [(c["title"], c["project"]) for c in out["later"]] == [("Make my Anki card maker", ""), ("Send Ravi his invite", "Onboarding")]
    # "you" is a decision only the user has: Quick answers, with or without the old writer's questions.
    assert [c["title"] for c in out["answers"]] == ["Book the rental car", "Tell Kofi about ACME holdings"]
    assert [c["title"] for c in out["waiting"]] == ["Ask Theo for a circling time"]
    assert [(c["title"], [s["title"] for s in c["steps"]]) for c in out["week"]] == [("Draft onboarding", ["Share personas"])]
    assert [(c["title"], c["open"]) for c in out["projects"]] == [("Onboarding", 1)]


def test_import(db: sqlite3.Connection) -> None:
    state = {
        "items": {
            "email:a@x.org:t1": {"key": "email:a@x.org:t1", "status": "open", "source": "email", "kind": "asked",
                                 "state": "you", "who": "Ravi", "title": "Send Ravi his invite", "goal_id": "g1",
                                 "account": "a@x.org", "ref": {"thread_id": "t1", "message_id": "m1"}},
            "manual:x": {"key": "manual:x", "status": "open", "source": "manual", "manual": True, "state": "mine",
                         "title": "Make my Anki card maker", "not_now": "2026-11-01"},
            "slack:D1:2.0": {"key": "slack:D1:2.0", "status": "open", "source": "slack", "kind": "handed",
                             "state": "waiting", "who": "Robin", "title": "Wait for Robin", "ref": {"channel": "D1", "ts": "2.0"}},
            "done:y": {"key": "done:y", "status": "done", "title": "old"},
            "goalmove:1": {"key": "goalmove:1", "status": "open", "source": "goal", "title": "suggested"},
        },
        "projects": [{"id": "onboarding", "title": "Customer onboarding", "about": "getting customers set up"},
                     {"id": "launch", "title": "Launch", "about": "the spring launch"}],
        "goals": {"g1": {"id": "g1", "status": "open", "title": "Customers onboarded", "project": "onboarding", "chain": []},
                  "g2": {"id": "g2", "status": "dissolved", "title": "gone"}},
    }
    plan = {"steps": {"goal:w1": {"text": "Share personas", "then": ["Book Felix"], "done": [{"text": "Read Muse"}]}},
            "in_week": {"manual:x": "2026-11-12T10:00:00"}}
    note = {"week": "2026-11-09", "priority": "#1: onboarding",
            "goals": [{"id": "w1", "text": "Draft onboarding", "stretch": False}, {"id": "w2", "text": "Feedback", "stretch": True}]}
    counts = commitments_import.run(db, state, plan, note, {"w2": "2026-11-12"}, today="2026-11-13")
    assert counts == {"projects": 2, "old_goals": 1, "items": 3, "week": 2, "steps": 3, "merged": 0, "parked": 1}
    les = commitments.by_legacy_key(db, "email:a@x.org:t1")
    goal = commitments.by_legacy_key(db, "oldgoal:g1")
    onboarding = commitments.by_legacy_key(db, "project:onboarding")
    assert les and goal and onboarding
    assert (les["direction"], les["origin"], les["started_by"], les["counterparty"]) == ("mine", "they_asked", "them", "Ravi")
    assert les["parent_id"] == goal["id"] and goal["parent_id"] == onboarding["id"]
    assert commitments.conversations_of(db, les["id"]) == ["email:a@x.org:t1"]
    anki = commitments.by_legacy_key(db, "manual:x")
    assert anki and anki["stage"] == "parked" and anki["week"] == "2026-11-09" and anki["direction"] == "self"
    robin = commitments.by_legacy_key(db, "slack:D1:2.0")
    assert robin and (robin["direction"], robin["origin"]) == ("theirs", "handed")
    w1, w2 = commitments.by_legacy_key(db, "goal:w1"), commitments.by_legacy_key(db, "goal:w2")
    assert w1 and w2 and w1["next_step"] == "Share personas" and w2["stage"] == "done"
    steps = [c for c in commitments.all_commitments(db) if c["parent_id"] == w1["id"]]
    assert sorted((s["title"], s["stage"]) for s in steps) == [("Book Felix", "open"), ("Read Muse", "done"), ("Share personas", "open")]
    with pytest.raises(commitments.CommitmentError):
        commitments_import.run(db, state, plan, note, {})


def test_due_soon_rows_go_first_soonest_on_top_then_oldest() -> None:
    """Something due tomorrow tops the list even when older rows wait (the user, 2026-11-16)."""
    today = date(2026, 11, 16)
    rows = [
        ("old", "", "2026-10-01"),
        ("far", "2026-12-30", "2026-10-02"),
        ("thursday", "2026-11-19", "2026-11-16"),
        ("tomorrow", "2026-11-17", "2026-11-16T12:00"),
        ("bad date", "someday", "2026-10-03"),
    ]
    ordered = sorted(rows, key=lambda r: commitment_lists.order_key(r[1], r[2], today))
    assert [r[0] for r in ordered] == ["tomorrow", "thursday", "old", "far", "bad date"]
