"""Projects as commitments (project_book.py, act.py, group_suggestions.py, learned.py).

Every AI call is stubbed, and every path that could send, archive or write outside Today
fails the test if it is reached (the user: nothing goes out without the user)."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from today import (
    act,
    actions,
    ai,
    assistant_actions,
    commitments,
    commitments_import,
    connectors,
    conversations,
    group_suggestions,
    learned,
    planning,
    project_book,
    projects,
    runner,
    store,
    task_steps,
    user_projects,
    work,
)
from today import page as page_mod

FUND_ITEM = "email:alex@acme.example:abc"
GOAL_ITEM = "email:alex@acme.example:def"
LOOSE = "manual:loose"


def _never(*_a: Any, **_k: Any) -> None:
    raise AssertionError("nothing may be sent, archived or written outside Today")


@pytest.fixture(autouse=True)
def _guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("slack_post_message", "gmail_send_reply", "gmail_set_inbox"):
        monkeypatch.setattr(connectors, name, _never)
    monkeypatch.setattr(group_suggestions, "run_soon_if_due", lambda: False)
    monkeypatch.setattr(ai, "complete_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unstubbed AI call")))
    folder = tmp_path / "Projects" / "Fund I"
    folder.mkdir(parents=True)
    (folder / "Goal.md").write_text("# Fund I\n\n## Purpose\n\n> FundPortal emails and transfers.\n\n> What I'm owed.\n\n## Done looks like\n- all\n")
    (tmp_path / "agents" / "fund").mkdir(parents=True)
    (tmp_path / "agents" / "fund" / "playbook.md").write_text("# Playbook\n\n- **Hugo** waits on you\n")
    monkeypatch.setattr(user_projects, "PROJECTS_DIR", tmp_path / "Projects")
    monkeypatch.setattr(user_projects, "AGENTS_DIR", tmp_path / "agents")
    # The user's three projects, set up in the settings (none ship by default).
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (store.DATA_DIR / "settings.json").write_text(json.dumps({"projects": [
        {"id": "fund", "title": "Fund I", "short": "Fund", "folder": "Fund I", "agent": "fund",
         "about": "the fund's investors, transfers and paperwork"},
        {"id": "onboarding", "title": "Customer onboarding"},
        {"id": "showcases", "title": "Customer showcases"},
    ]}))


def _item(key: str, **kw: Any) -> dict[str, Any]:
    base = {"key": key, "status": "open", "source": "email", "kind": "asked", "state": "ready", "who": "Hugo",
            "title": "Answer Hugo on Section 1", "ask_text": "Could you let us know?", "asked_at": "2026-11-12T10:00:00",
            "draft": "Yes, fine.", "account": "alex@acme.example", "ref": {"thread_id": key[-3:]}}
    base.update(kw)
    return base


def _seed(imported: bool = True) -> None:
    items = {
        FUND_ITEM: _item(FUND_ITEM, project="fund"),
        GOAL_ITEM: _item(GOAL_ITEM, who="Ray", title="Confirm the share count", goal_id="g1"),
        LOOSE: {"key": LOOSE, "status": "open", "source": "manual", "manual": True, "state": "mine",
                "title": "Get Robin the user's gift", "asked_at": "2026-11-12T10:00:00", "filed": {"why": "a note to self"}},
    }
    goals = {"g1": {"id": "g1", "title": "Fund I ready for the IPO", "status": "open", "project": "fund",
                    "chain": [{"id": "s1", "step": "Confirm", "covers": [GOAL_ITEM]}]}}
    store.mutate(lambda s: (s["items"].update(items), s.setdefault("goals", {}).update(goals)))
    if imported:
        with conversations.opened(store.DATA_DIR) as db:
            commitments_import.run(db, store.load(), {}, None, {})


def _must(c: dict[str, Any] | None) -> dict[str, Any]:
    assert c is not None
    return c


def _label(prompt: str, title: str) -> str:
    m = re.search(r'"task": "(T\d+)",\s*"title": "' + re.escape(title), prompt)
    assert m, prompt
    return m.group(1)


def _client() -> Any:
    return runner.app.test_client()


def _fund(db: Any) -> dict[str, Any]:
    c = commitments.by_legacy_key(db, "project:fund")
    assert c is not None
    return c


# ---- setup and reading -------------------------------------------------------------------


def test_user_three_projects_with_their_tasks_and_user_words() -> None:
    _seed()
    with project_book.opened() as db:
        out = project_book.project_list(db)
        assert [p["title"] for p in out["projects"]] == ["Fund I", "Customer onboarding", "Customer showcases"]
        fund = _fund(db)
        page = project_book.project_page(db, fund["id"])
    assert page is not None
    # The task in the old goal under the fund is one of the fund's tasks too.
    assert sorted(t["key"] for t in page["tasks"]) == sorted([FUND_ITEM, GOAL_ITEM])
    assert page["words"] == "FundPortal emails and transfers.\n\nWhat I'm owed."
    assert page["has_sources"]
    labels = None
    with project_book.opened() as db:
        labels = project_book.main_list_labels(db)
    assert labels[GOAL_ITEM]["title"] == "Fund" and labels[GOAL_ITEM]["id"] == fund["id"]
    assert LOOSE not in labels


def test_the_list_names_each_rows_project_and_now_is_unchanged() -> None:
    _seed()
    state = actions.view_state()
    page = state["page"]
    # With no weekly note, Now takes its three as before; project tasks are candidates like
    # any other, and keep their project name there too.
    assert sorted(r["key"] for r in page["now"]) == sorted([FUND_ITEM, GOAL_ITEM, LOOSE])
    rows = {r["key"]: r for r in page["now"]}
    assert rows[FUND_ITEM]["project"]["title"] == "Fund"
    assert rows[GOAL_ITEM]["project"]["title"] == "Fund"  # the old goal is not a project of the user's
    assert rows[LOOSE]["project"] is None
    assert page["projects_open"] == 3


def test_project_tasks_show_in_their_sections_and_never_hide_in_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    monkeypatch.setattr(page_mod, "NOW_N", 0)
    page = actions.view_state()["page"]
    sections = {r["key"]: name for name in ("replies", "answers", "todos", "waiting", "later") for r in page[name]}
    assert sections == {FUND_ITEM: "replies", GOAL_ITEM: "replies", LOOSE: "todos"}
    assert next(r for r in page["replies"] if r["key"] == FUND_ITEM)["project"]["id"]


def test_auto_filing_into_user_project_and_never_back_once_user_moves_it_out() -> None:
    assert projects.file_or_suggest() == "file"
    _seed()
    store.mutate(lambda s: s["items"].update({"manual:new": dict(_item("manual:new"), project="fund")}))
    with project_book.opened() as db:
        assert project_book.auto_file_pending(db, [store.load()["items"]["manual:new"]]) == 1
        c = commitments.by_legacy_key(db, "manual:new")
        assert c and c["parent_id"] == _fund(db)["id"]
        ev = commitments.history(db, c["id"])[-1]
        assert ev["actor"] == "sync" and ev["kind"] == "filed" and "filed there by itself" in ev["reason"]
        project_book.task_action(db, "", "manual:new", "move", "")
        assert project_book.auto_file_pending(db, [store.load()["items"]["manual:new"]]) == 0
        assert _must(commitments.by_legacy_key(db, "manual:new"))["parent_id"] is None


def test_prompt_lines_name_user_projects() -> None:
    _seed()
    with project_book.opened():
        pass  # opening Today sets up the user's three projects
    lines = user_projects.prompt_lines()
    assert '"fund": Fund I.' in lines and "Customer showcases" in lines
    assert "Customer showcases" in act.system()


# ---- writes through the routes, each with Undo -------------------------------------------------


def test_new_rename_add_and_complete_with_undo() -> None:
    _seed()
    c = _client()
    new = c.post("/api/projects/new", json={"title": "Spring Summit talk"}).get_json()
    assert new["ok"] and new["id"]
    assert c.post("/api/projects/rename", json={"id": new["id"], "title": "Spring Summit"}).get_json()["ok"]
    added = c.post("/api/projects/add", json={"id": new["id"], "title": "Write the talk"}).get_json()
    assert added["ok"]
    moved = c.post("/api/projects/add", json={"id": new["id"], "key": LOOSE}).get_json()
    assert moved["message"] == "Moved into this project"
    page = c.get(f"/api/projects/page?id={new['id']}").get_json()
    assert page["title"] == "Spring Summit" and {t["title"] for t in page["tasks"]} == {"Write the talk", "Get Robin the user's gift"}
    done = c.post("/api/projects/complete", json={"id": new["id"]}).get_json()
    assert store.load()["items"][LOOSE]["status"] == "done"
    assert new["id"] in [p["id"] for p in c.get("/api/projects").get_json()["completed"]]
    assert c.post("/api/undo", json={"batch": done["batch"]}).get_json()["message"] == "Put back"
    assert store.load()["items"][LOOSE]["status"] == "open"
    with project_book.opened() as db:
        p = commitments.get(db, new["id"])
        assert p and p["stage"] == "open"
        # Undo is more events, never a deletion.
        assert [e["kind"] for e in commitments.history(db, new["id"])][-1] == "undone"
    assert c.post("/api/undo", json={"batch": done["batch"]}).get_json()["message"] == "Already undone"


@pytest.mark.parametrize(("action", "check"), [
    ("later", lambda it: bool(it.get("not_now"))),
    ("notmine", lambda it: it["status"] == "skipped" and it["filed"] == "not_yours"),
    ("drop", lambda it: it["status"] == "skipped"),
    ("done", lambda it: it["status"] == "done"),
])
def test_task_menu_actions_and_undo(action: str, check: Any) -> None:
    _seed()
    with project_book.opened() as db:
        pid = _fund(db)["id"]
    res = _client().post("/api/projects/task", json={"id": pid, "key": FUND_ITEM, "action": action}).get_json()
    assert res["ok"] and check(store.load()["items"][FUND_ITEM])
    _client().post("/api/undo", json={"batch": res["batch"]})
    it = store.load()["items"][FUND_ITEM]
    assert it["status"] == "open" and not it.get("not_now") and not it.get("filed")


def test_move_to_another_project_and_back_from_later() -> None:
    _seed()
    with project_book.opened() as db:
        pid, other = _fund(db)["id"], _must(commitments.by_legacy_key(db, "project:showcases"))["id"]
    c = _client()
    assert c.post("/api/projects/task", json={"id": pid, "key": FUND_ITEM, "action": "move", "to": other}).get_json()["message"] == "Moved to Customer showcases"
    assert FUND_ITEM in [t["key"] for t in c.get(f"/api/projects/page?id={other}").get_json()["tasks"]]
    c.post("/api/projects/task", json={"id": other, "key": FUND_ITEM, "action": "later"})
    assert FUND_ITEM in [t["key"] for t in c.get(f"/api/projects/page?id={other}").get_json()["later"]]
    c.post("/api/projects/task", json={"id": other, "key": FUND_ITEM, "action": "back"})
    assert FUND_ITEM in [t["key"] for t in c.get(f"/api/projects/page?id={other}").get_json()["tasks"]]


def test_errors_are_plain() -> None:
    _seed()
    c = _client()
    assert c.post("/api/projects/new", json={"title": " "}).status_code == 400
    assert c.post("/api/projects/rename", json={"id": "nope", "title": "x"}).get_json()["error"] == "There's no such project."
    assert c.get("/api/projects/page?id=nope").status_code == 400
    assert c.post("/api/projects/task", json={"id": "", "key": FUND_ITEM, "action": "send"}).status_code == 400


# ---- the box -------------------------------------------------------------------------------


def test_help_gives_the_task_a_checklist_and_undo_reverses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    planning.set_step("todo:" + FUND_ITEM, "an old next step")  # from the old step list
    with project_book.opened() as db:
        pid = _fund(db)["id"]
        task = _must(commitments.by_legacy_key(db, FUND_ITEM))
    seen: dict[str, str] = {}

    def fake(prompt: str, **kw: Any) -> dict[str, Any]:
        seen["prompt"], seen["system"] = prompt, kw["system"]
        label = _label(prompt, "Answer Hugo")
        return {"operations": [
            {"op": "rewrite", "task": label, "title": "Collect my 0.5% on each LP transfer"},
            {"op": "steps", "task": label, "steps": [
                {"text": "Open approved transfers in FundPortal", "kind": "link", "url": "https://portal.fund.example",
                 "label": "Open FundPortal", "note": "Leo Hart's transfer to the DAF has no fee."},
                {"text": "Fill in each transfer's amount", "kind": "record", "fill_label": "Fill in from FundPortal emails",
                 "table": {"columns": ["Transfer", "Amount", "Your 0.5%"], "rows": [["Victor to Northwind"]],
                           "percent": {"of": 1, "rate": 0.005}}},
                {"text": "Email each seller their amount", "kind": "draft", "label": "Draft the emails"},
            ]},
            {"op": "add", "title": "Ask Anton how the fund lead is paid"},
            {"op": "send", "task": label},
        ]}

    monkeypatch.setattr(ai, "complete_json", fake)
    said = "I need to calculate each transfer from the ones I've approved in FundPortal, then follow up with each person"
    res = _client().post("/api/assistant", json={"scope": {"kind": "project", "pid": pid, "task": task["id"]}, "say": said}).get_json()
    assert res["ok"] and res["summary"].startswith("Done: 1 retitled, steps for 1 task, 1 added")
    assert FUND_ITEM in res["changed"]
    assert "The user is talking about this task" in seen["prompt"] and said in seen["prompt"]
    assert "Never send, archive or post anything" in seen["system"] and "at most about 8 words" in seen["system"]
    page = _client().get(f"/api/projects/page?id={pid}").get_json()
    row = next(t for t in page["tasks"] if t["key"] == FUND_ITEM)
    # The title stays the task; the steps are a checklist under it.
    assert row["title"] == "Collect my 0.5% on each LP transfer" and row["next_step"] == ""
    assert [(s["title"], s["kind"], s["done"]) for s in row["steps"]] == [
        ("Open approved transfers in FundPortal", "link", False),
        ("Fill in each transfer's amount", "record", False),
        ("Email each seller their amount", "draft", False),
    ]
    assert row["steps"][0]["url"] == "https://portal.fund.example" and row["steps"][0]["note"].startswith("Leo Hart")
    assert row["steps"][1]["table"]["percent"] == {"of": 1, "rate": 0.005}
    assert (row["steps_done"], row["step_total"]) == (0, 3)
    # The user's old next step was the task's first step; the new checklist replaced it (one list, no "then:" line).
    assert planning.summary()["steps"]["todo:" + FUND_ITEM]["text"] == "Open approved transfers in FundPortal"
    assert page["notes"][-1]["text"] == said and page["notes"][-1]["task"] == task["id"]
    with project_book.opened() as db:
        assert commitments.history(db, task["id"])[-1]["actor"] == "assistant"
    # Undo of the whole box batch: title back, steps gone, the added task dropped. The user's words stay.
    _client().post("/api/undo", json={"batch": res["batch"]})
    page = _client().get(f"/api/projects/page?id={pid}").get_json()
    row = next(t for t in page["tasks"] if t["key"] == FUND_ITEM)
    assert row["title"] == "Answer Hugo on Section 1" and row["step_total"] == 1  # the user's old step is back
    assert "Ask Anton how the fund lead is paid" not in [t["title"] for t in page["tasks"]]
    assert page["notes"][-1]["text"] == said
    assert planning.summary()["steps"]["todo:" + FUND_ITEM]["text"] == "an old next step"


def test_steps_from_the_ai_follow_the_rules() -> None:
    title = "Collect my 0.5% on each LP transfer"
    out = task_steps.clean_steps([
        "List the transfers you approved in FundPortal (Victor to Northwind, Neil, Peter if any)",
        {"text": "Work out 0.5% of each - leave out Leo Hart's transfer to the DAF", "kind": "record"},
        "Collect my 0.5% on each LP transfer",  # the title again
        "Go through every single one of the transfers that you approved and then work out what each owes you",
        {"text": "Open FundPortal", "kind": "link"},  # a link with nowhere to go
        {"text": "Email each seller", "kind": "draft"},
        "Log each payment", "Thank Victor", "Close the loop with FundPortal", "One more step",
    ], title)
    texts = [s["text"] for s in out]
    assert texts[0] == "List the transfers you approved in FundPortal"
    assert out[0]["note"] == "Victor to Northwind, Neil, Peter if any."
    assert texts[1] == "Work out 0.5% of each" and out[1]["note"] == "Leave out Leo Hart's transfer to the DAF." or out[1]["note"] == "leave out Leo Hart's transfer to the DAF."
    assert title not in texts
    assert all("(" not in t and len(t.split()) <= task_steps.HARD_MAX_WORDS for t in texts)
    assert out[2]["text"] == "Open FundPortal" and out[2]["kind"] == "plain"
    assert len(out) == task_steps.MAX_STEPS


def test_a_step_never_overwrites_the_title(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    with project_book.opened() as db:
        pid, task = _fund(db)["id"], _must(commitments.by_legacy_key(db, FUND_ITEM))

    def fake(prompt: str, **kw: Any) -> dict[str, Any]:
        label = _label(prompt, "Answer Hugo")
        long = "Read through every transfer you have approved in FundPortal so far and write each one down carefully"
        return {"operations": [
            {"op": "rewrite", "task": label, "title": "Open approved transfers in FundPortal"},
            {"op": "steps", "task": label, "steps": ["Open approved transfers in FundPortal", long]},
        ]}

    monkeypatch.setattr(ai, "complete_json", fake)
    assistant = act.ask({"kind": "project", "pid": pid, "task": task["id"]}, "")  # the icon with an empty box
    assert assistant["batch"]
    page = _client().get(f"/api/projects/page?id={pid}").get_json()
    row = next(t for t in page["tasks"] if t["key"] == FUND_ITEM)
    assert row["title"] == "Answer Hugo on Section 1"
    assert [s["title"] for s in row["steps"]] == ["Open approved transfers in FundPortal"]  # the long one rejected
    assert page["notes"] == []  # nothing the user said, nothing kept


def test_editing_steps_like_a_checklist_and_ticking_the_last_one() -> None:
    _seed()
    with project_book.opened() as db:
        pid, task = _fund(db)["id"], _must(commitments.by_legacy_key(db, FUND_ITEM))
    c = _client()
    one = c.post("/api/projects/step/add", json={"task": task["id"], "title": "Open FundPortal"}).get_json()["cid"]
    three = c.post("/api/projects/step/add", json={"task": task["id"], "title": "Email each seller"}).get_json()["cid"]
    two = c.post("/api/projects/step/add", json={"task": task["id"], "title": "", "after": one}).get_json()["cid"]
    c.post("/api/projects/step/edit", json={"cid": two, "title": "Fill in amounts"})

    def steps() -> list[tuple[str, bool]]:
        row = next(t for t in c.get(f"/api/projects/page?id={pid}").get_json()["tasks"] if t["key"] == FUND_ITEM)
        return [(s["title"], s["done"]) for s in row["steps"]]

    assert steps() == [("Open FundPortal", False), ("Fill in amounts", False), ("Email each seller", False)]
    gone = c.post("/api/projects/step/remove", json={"cid": three}).get_json()
    assert [t for t, _ in steps()] == ["Open FundPortal", "Fill in amounts"]
    c.post("/api/undo", json={"batch": gone["batch"]})
    assert len(steps()) == 3
    c.post("/api/projects/step/edit", json={"cid": three, "title": "  "})  # emptied: removed
    assert len(steps()) == 2
    assert c.post("/api/projects/step/tick", json={"cid": one, "done": True}).get_json()["all_done"] is False
    assert c.post("/api/projects/step/tick", json={"cid": two, "done": True}).get_json()["all_done"] is True
    assert steps() == [("Open FundPortal", True), ("Fill in amounts", True)]
    with project_book.opened() as db:
        kinds = [e["kind"] for e in commitments.history(db, two)]
        assert kinds[0] == "created" and "changed" in kinds
        assert _must(commitments.get(db, task["id"]))["next_step"] == ""
    # The Today list's row carries the progress ring's numbers.
    rows = {r["key"]: r for name in ("now", "replies", "answers", "todos") for r in actions.view_state()["page"][name]}
    assert (rows[FUND_ITEM]["steps_done"], rows[FUND_ITEM]["step_total"]) == (2, 2)
    assert c.post("/api/projects/step/tick", json={"cid": "nope"}).status_code == 400


def test_help_never_overwrites_user_change_made_while_it_was_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    with project_book.opened() as db:
        pid, task = _fund(db)["id"], _must(commitments.by_legacy_key(db, FUND_ITEM))

    def fake(prompt: str, **kw: Any) -> dict[str, Any]:
        with project_book.opened() as db:  # the user renamed it meanwhile
            commitments.change(db, task["id"], "user", {"title": "The user's own title"})
        return {"operations": [{"op": "rewrite", "task": _label(prompt, "Answer Hugo"), "title": "AI title"}]}

    monkeypatch.setattr(ai, "complete_json", fake)
    res = act.ask({"kind": "project", "pid": pid, "task": task["id"]}, "tidy this up")
    assert "1 left as it was" in res["summary"]
    with project_book.opened() as db:
        assert _must(commitments.get(db, task["id"]))["title"] == "The user's own title"


def test_help_needs_words_and_keeps_them_when_the_ai_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    with project_book.opened() as db:
        pid = _fund(db)["id"]
    assert _client().post("/api/assistant", json={"scope": {"kind": "project", "pid": pid}, "say": "  "}).status_code == 400
    monkeypatch.setattr(ai, "complete_json", lambda *a, **k: (_ for _ in ()).throw(ai.ClaudeError("down")))
    res = _client().post("/api/assistant", json={"scope": {"kind": "project", "pid": pid}, "say": "remember this"})
    assert res.status_code == 400 and "down" in res.get_json()["error"]
    assert _client().get(f"/api/projects/page?id={pid}").get_json()["notes"][-1]["text"] == "remember this"


# ---- the feedback bar and what Today has learned --------------------------------------------


def test_feedback_refiles_a_referenced_item_remembers_it_and_undo(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    prompts: list[str] = []

    def fake(prompt: str, **kw: Any) -> dict[str, Any]:
        prompts.append(prompt)
        return {"operations": [{"op": "refile", "item": "T1", "as": "not_yours"}]}

    monkeypatch.setattr(ai, "complete_json", fake)
    said = "This isn't mine, Ben handles FundPortal forms"
    res = _client().post("/api/assistant", json={"scope": {"kind": "tasks", "keys": [FUND_ITEM]}, "say": said}).get_json()
    assert res["ok"] and res["summary"] == "Done: 1 refiled · remembered for next time"
    assert "Could you let us know?" in prompts[0] and said in prompts[0]
    it = store.load()["items"][FUND_ITEM]
    assert it["status"] == "skipped" and it["filed"] == "not_yours"
    rows = _client().get("/api/learned").get_json()["learned"]
    assert rows[0]["text"] == said and rows[0]["items"][0]["now"] == "someone else's"
    # Both readers see it: the reply reader's guidance, and anything reading prompt_text(db).
    assert said in work.guidance(store.load()["items"][LOOSE])
    assert said in _client().get("/api/state").get_json()["learned"][0]["text"]
    _client().post("/api/undo", json={"batch": res["batch"]})
    assert store.load()["items"][FUND_ITEM]["status"] == "open"
    assert _client().get("/api/learned").get_json()["learned"] == []


def test_learned_corrections_can_be_removed_and_restored() -> None:
    with conversations.opened(store.DATA_DIR) as db:
        lid = learned.add(db, "Group-thread updates aren't asks", [])
    assert "Group-thread updates aren't asks" in learned.prompt_text_now()
    _client().post("/api/learned/remove", json={"id": lid})
    assert learned.prompt_text_now() == ""
    _client().post("/api/learned/remove", json={"id": lid, "removed": False})
    assert "Group-thread" in learned.prompt_text_now()
    state = _client().get("/api/state").get_json()
    assert state["learned"][0]["id"] == lid


# ---- suggested groupings --------------------------------------------------------------------


def test_groupings_file_into_user_projects_suggest_the_rest_and_respect_user_no(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    extra = {f"manual:{n}": {"key": f"manual:{n}", "status": "open", "source": "manual", "manual": True, "state": "mine",
                             "title": t, "asked_at": "2026-11-12T10:00:00"}
             for n, t in enumerate(["Sign Oscar's buyback", "Pay Oscar", "Book Spring Summit", "Write Spring Summit talk"])}
    store.mutate(lambda s: s["items"].update(extra))
    with project_book.opened() as db:
        for it in extra.values():
            project_book.ensure_commitment(db, str(it["key"]))
        cid = {k: _must(commitments.by_legacy_key(db, k))["id"] for k in [*extra, LOOSE]}
        fund = _fund(db)["id"]

    calls: list[dict[str, Any]] = []

    def fake(prompt: str, **kw: Any) -> dict[str, Any]:
        calls.append(json.loads(prompt))
        return {"groups": [
            {"title": "Oscar's buyback", "project": "", "keys": [cid["manual:0"], cid["manual:1"]]},
            {"title": "Spring Summit talk", "project": "", "keys": [cid["manual:2"], cid["manual:3"]]},
            {"title": "fund paperwork", "project": fund, "keys": [cid[LOOSE], cid["manual:3"]]},
            {"title": "One task", "project": "", "keys": [cid["manual:1"]]},
        ]}

    monkeypatch.setattr(ai, "complete_json", fake)
    out = group_suggestions.run()
    assert out == {"tasks": 5, "groups": 2, "filed": 2}
    body = calls[0]
    assert {p["title"] for p in body["projects"]} >= {"Fund I"}
    # Only tasks in no project of the user's: the fund's own tasks are not regrouped.
    assert cid[LOOSE] in {t["key"] for t in body["tasks"]} and "Answer Hugo" not in json.dumps(body["tasks"])
    with project_book.opened() as db:
        assert _must(commitments.get(db, cid[LOOSE]))["parent_id"] == fund
        sugg = group_suggestions.current(db)
        assert not group_suggestions.due(db)
    assert [s["title"] for s in sugg] == ["Oscar's buyback"]  # Spring Summit lost a task to the fund, leaving one
    c = _client()
    gone = c.post("/api/projects/suggestion", json={"id": sugg[0]["id"], "keep": False}).get_json()
    assert c.get("/api/projects").get_json()["suggestions"] == []
    group_suggestions.run()  # the same grouping again: the user said no
    assert c.get("/api/projects").get_json()["suggestions"] == []
    c.post("/api/undo", json={"batch": gone["batch"]})
    sugg = c.get("/api/projects").get_json()["suggestions"]
    assert [s["title"] for s in sugg][:1] == ["Oscar's buyback"]
    kept = c.post("/api/projects/suggestion", json={"id": sugg[0]["id"], "keep": True}).get_json()
    page = c.get(f"/api/projects/page?id={kept['id']}").get_json()
    assert page["title"] == "Oscar's buyback" and {t["title"] for t in page["tasks"]} == {"Sign Oscar's buyback", "Pay Oscar"}
    c.post("/api/undo", json={"batch": kept["batch"]})
    assert kept["id"] not in [p["id"] for p in c.get("/api/projects").get_json()["projects"]]


# ---- the raw sources ------------------------------------------------------------------------


def test_what_today_has_read_renders_goal_and_playbook() -> None:
    _seed()
    with project_book.opened() as db:
        pid = _fund(db)["id"]
    html = _client().get(f"/projects/sources?id={pid}").get_data(as_text=True)
    assert "<blockquote>FundPortal emails and transfers.</blockquote>" in html
    assert "<li><b>Hugo</b> waits on you</li>" in html
    assert runner.markdown_html("a <b>\n\n# H") == "<p>a &lt;b&gt;</p><h3>H</h3>"


def test_page_script_is_served_and_nothing_outside_assets() -> None:
    c = _client()
    assert c.get("/assets/projects.js").status_code == 200
    assert c.get("/assets/../runner.py").status_code == 404
    assert c.get("/assets/app.html").status_code == 404


# ---- one assistant everywhere (the user, 2026-11-15) -------------------------------------------


def test_the_project_box_is_the_one_assistant_with_options_and_a_way_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The project's box answers like a card: what it did, the next options, and Other options
    to take it back; it can write a reply for one of the project's tasks, which a card shows."""
    _seed()
    with project_book.opened() as db:
        pid, task = _fund(db)["id"], _must(commitments.by_legacy_key(db, FUND_ITEM))
    systems: list[str] = []

    def fake(prompt: str, **kw: Any) -> dict[str, Any]:
        systems.append(kw["system"])
        label = _label(prompt, "Answer Hugo")
        return {"did": "Wrote Hugo a reply.", "operations": [{"op": "draft", "task": label, "text": "Hi Hugo, here it is."}],
                "next": [{"label": "Add steps for the forms", "say": "add steps for the forms"}]}

    monkeypatch.setattr(ai, "complete_json", fake)
    scope = {"kind": "project", "pid": pid, "task": task["id"]}
    res = _client().post("/api/assistant", json={"scope": scope, "say": "write Hugo back"}).get_json()
    assert res["ok"] and res["turns"][-1]["did"] == "Wrote Hugo a reply."
    assert res["next"] == [{"label": "Add steps for the forms", "say": "add steps for the forms"}]
    assert store.load()["items"][FUND_ITEM]["draft"] == "Hi Hugo, here it is."
    assert '"op": "draft"' in systems[0] and '"op": "add"' in systems[0] and "find_threads" not in systems[0]
    # What the box went through is in the database, not in the user's files.
    assert _client().post("/api/assistant/view", json={"scope": scope}).get_json()["turns"][-1]["said"] == "write Hugo back"
    assert "act" not in store.load()["items"][FUND_ITEM]
    back = _client().post("/api/assistant/back", json={"scope": scope}).get_json()
    assert back["turns"] == [] and store.load()["items"][FUND_ITEM]["draft"] == "Yes, fine."  # the draft before it


def test_the_bottom_bar_is_the_one_assistant_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """With several tasks attached (or none), the bar's answer has options and a way back; a
    task elsewhere on the user's list is named by its L label; done is offered, never guessed."""
    _seed()
    systems: list[str] = []

    def fake(prompt: str, **kw: Any) -> dict[str, Any]:
        systems.append(kw["system"])
        loose = next(line.split(":")[0] for line in prompt.splitlines() if "Get Robin the user's gift" in line)
        return {"did": "Put both in Later.", "operations": [{"op": "later", "task": "T1"}, {"op": "later", "task": loose}],
                "next": [{"label": "Bring them back Monday", "say": "bring them back Monday"}]}

    monkeypatch.setattr(ai, "complete_json", fake)
    res = _client().post("/api/assistant", json={"scope": {"kind": "tasks", "keys": [FUND_ITEM]},
                                                 "say": "not now, and Robin's gift too"}).get_json()
    assert res["ok"] and res["next"][0]["label"] == "Bring them back Monday" and res["summary"].startswith("Done: 2 to Later")
    items = store.load()["items"]
    assert items[FUND_ITEM]["not_now"] and items[LOOSE]["not_now"]
    assert "Mark it done" in systems[0] and '"op": "draft"' not in systems[0]
    _client().post("/api/assistant/back", json={"scope": {"kind": "tasks", "keys": []}})
    items = store.load()["items"]
    assert not items[FUND_ITEM].get("not_now") and not items[LOOSE].get("not_now")


def test_a_new_action_is_one_entry_and_every_place_offers_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user, 2026-11-15: more actions will come. One registered entry is enough: its
    line reaches the instructions of every place it is for, and Today applies it."""
    done: list[str] = []
    action = assistant_actions.Action("wave", '{"op": "wave", "who": "..."}: wave at a customer.',
                                      lambda d, op: done.append(str(op.get("who"))))
    monkeypatch.setitem(assistant_actions.ACTIONS, "wave", action)
    for kind in ("task", "tasks", "project"):
        assert "wave" in act.system(kind)
    _seed()
    monkeypatch.setattr(ai, "complete_json", lambda prompt, **kw: {"did": "Waved.", "operations": [
        {"op": "wave", "who": "Al"}], "next": []})
    act.ask({"kind": "tasks", "keys": []}, "wave at Al")
    assert done == ["Al"]


def test_a_project_named_as_the_model_knows_it_is_found_and_a_kept_answer_stays_with_its_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found on the user's real tasks (2026-11-15): the model names the user's projects as "fund", "onboarding"...
    and a move to one silently did nothing; and "Not now" in the bar, taken back and said again
    about another task, re-applied the first answer to the first task."""
    _seed()
    answers = [{"did": "Moved it.", "operations": [{"op": "project", "task": "T1", "project": "fund"}], "next": []}]
    monkeypatch.setattr(ai, "complete_json", lambda prompt, **kw: answers.pop(0))
    act.ask({"kind": "tasks", "keys": [LOOSE]}, "this is fund work")
    with project_book.opened() as db:
        assert _must(commitments.by_legacy_key(db, LOOSE))["parent_id"] == _fund(db)["id"]
    act.back(scope={"kind": "tasks", "keys": [LOOSE]})
    answers += [{"did": "Later.", "operations": [{"op": "later", "task": "T1"}], "next": []},
                {"did": "Later.", "operations": [{"op": "later", "task": "T1"}], "next": []}]
    act.ask({"kind": "tasks", "keys": [LOOSE]}, "Not now")
    act.back(scope={"kind": "tasks", "keys": [LOOSE]})
    act.ask({"kind": "tasks", "keys": [FUND_ITEM]}, "Not now")  # asks again, about this task
    items = store.load()["items"]
    assert items[FUND_ITEM].get("not_now") and not items[LOOSE].get("not_now") and answers == []


def test_every_assistant_reads_a_conversation_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user, 2026-11-15: one way of turning a conversation into text. The project box and the
    bottom bar read it as a card does: the user's unsent draft marked, never taken as sent."""
    _seed()
    store.save_raw(FUND_ITEM, {"source": "email", "thread_id": "abc", "subject": "Section 1", "account": "alex@acme.example", "messages": [
        {"id": "m1", "from": "Hugo", "to": "the user", "date": "Nov 12", "text": "Could you let us know?"},
        {"id": "m2", "from_me": True, "draft": True, "from": "the user", "to": "Hugo", "date": "Nov 13", "text": "Yes, fine."}]})
    prompts: list[str] = []
    monkeypatch.setattr(ai, "complete_json", lambda prompt, **kw: prompts.append(prompt) or {"did": "", "operations": [], "next": []})
    with project_book.opened() as db:
        pid, task = _fund(db)["id"], _must(commitments.by_legacy_key(db, FUND_ITEM))
    act.ask({"kind": "project", "pid": pid, "task": task["id"]}, "what's left?")
    act.ask({"kind": "tasks", "keys": [FUND_ITEM]}, "what's left?")
    assert work.transcript(store.load_raw(FUND_ITEM) or {}) in prompts[0]  # word for word, as a card reads it
    assert "UNSENT DRAFT" in prompts[1]  # the bar has it too (inside each attached task's JSON)
