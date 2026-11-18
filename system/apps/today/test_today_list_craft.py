"""The Today list's craft from the approved mock (the user, 2026-11-13/14): one rounded list per
section in one aligned grid, a fixed icon column, quiet colours, segmented tabs, a search icon,
the Now-is-clear celebration, keyboard keys, and no microphone anywhere."""

from pathlib import Path

import pytest

ASSETS = Path(__file__).parent / "src" / "today" / "assets"


def _read(name: str) -> str:
    return (ASSETS / name).read_text()


@pytest.mark.frontend
def test_sections_are_rounded_lists_in_one_grid_with_a_fixed_icon_column() -> None:
    page, craft = _read("app.html"), _read("today_list.js")
    assert '<script src="/assets/today_list.js"></script>' in page
    assert "function listHtml(" in page and 'h+=section("Quick answers", P.answers, "answers");' in page
    # Every row has the same slots, so the icons sit in one rail whether or not a row has a date;
    # the person's name goes before the title, in grey, not in a column of its own (the user, 2026-11-16).
    assert "grid-template-columns:20px minmax(0,1fr) auto var(--rail) var(--rail)" in craft
    assert '<span class="whobefore">' in page and "who2" not in page and "fitNames" not in page
    assert '<span class="st">' in page and '<span class="dueslot">' in page and "${rowAsk(r, viewName)}" in page
    # Everything on the first line is one title line tall, so it shares the title's first-line centre.
    assert "--tlh:calc(var(--tfs) * 1.55)" in craft and "height:var(--tlh)" in craft
    # The open card starts on the title's left edge (16 + 20 + 12).
    assert "--edge:48px" in craft and "padding:0 14px 16px var(--edge)" in craft


@pytest.mark.frontend
def test_icons_show_when_useful() -> None:
    """The user, 2026-11-16: on a computer the icons show on the row under the pointer or an open card;
    on a phone the chat icon always and the calendar once the card is open."""
    craft = _read("today_list.js")
    assert "@media (hover:hover){" in craft and ".list .item.open .head .dueicon{opacity:1}" not in craft
    assert ".list .item .head:hover .askicon,.list .item .head:hover .dueicon,.list .item.open .head .askicon" in craft
    assert ".list .item:not(.open) .head .dueslot .dueicon{display:none}" in craft


@pytest.mark.frontend
def test_a_date_sits_on_the_date_icon_and_the_icons_bottoms_line_up() -> None:
    """The user, 2026-11-15: a row's date is centred where the date icon sits (a phone keeps it under
    the title); the chat bubble's bottom edge is level with the calendar's."""
    page, craft = _read("app.html"), _read("today_list.js")
    assert page.count('<span class="dueslot">${viewName!=="filed"?dueHtml(r, viewName):""}</span>') == 1
    assert page.count('<span class="dueslot">${dueHtml(r, viewName)}</span>') == 1
    assert page.count('<span class="stdue">') == 2 and 'el.querySelectorAll(".head [data-due]")' in page
    assert ".list .item .head .dueslot:has(.when){display:flex;justify-content:center" in craft
    assert ".list .item .head .stdue{display:none}" in craft
    assert "  .list .item .head .stdue{display:inline}\n  .list .item .head .dueslot .when{display:none}" in craft
    assert ".list .item .head .askicon svg{transform:translateY(1px)}" in craft
    assert ".list .item .head .dueicon svg{transform:translateY(-1px)}" in craft


@pytest.mark.frontend
def test_quiet_colours_segmented_tabs_and_a_search_icon() -> None:
    page, craft = _read("app.html"), _read("today_list.js")
    assert 'function whenHtml(w){ return `<span class="when${w==="today"?" hot":""}">' in page
    assert ".item .when{color:#6b665d}" in craft and ".item .when.hot{color:#b4541f}" in craft
    assert '<span class="seg">' in page and ".tabs .tab.on{background:#fff" in craft
    assert 'id="searchbtn" title="Search (/)"' in page and 'placeholder="Search everything"' in page


@pytest.mark.frontend
def test_now_clear_celebration_keyboard_and_no_microphone() -> None:
    page, craft, proj = _read("app.html"), _read("today_list.js"), _read("projects.js")
    assert "Now is clear" in craft and "confettiFrom(" in craft and "prefers-reduced-motion" in craft
    assert "nowCheck(S, fresh);" in page and "nowAfterRender();" in page
    for key in ('e.key === "j"', 'e.key === "x"', 'e.key === "Enter"'):
        assert key in craft
    assert "input,textarea,select,[contenteditable]" in craft  # never while typing
    for text in (page, craft, proj):
        assert "TodayVoice" not in text and "Dictate" not in text and "Type or talk" not in text
    # The finish fades first, then folds, with no pause (as the mock).
    # Press, spring and tick, a beat to see it done, then fade and fold (the user, 2026-11-14).
    assert "const fade=220, fold=big?460:380;" in page and "big?600:550" in page
    assert "@keyframes tickspring" in craft and ".check2:active{transform:scale(.86)}" in craft


@pytest.mark.frontend
def test_titles_wrap_and_open_ones_show_in_full() -> None:
    craft = _read("today_list.js")
    assert "-webkit-line-clamp:3" in craft
    assert ".list .item.open .words{font-weight:600;-webkit-line-clamp:unset;display:block;overflow:visible}" in craft
    assert "-webkit-line-clamp:1" not in craft


@pytest.mark.frontend
def test_finishing_fills_the_check_and_the_redraw_waits_for_the_fold() -> None:
    page, craft = _read("app.html"), _read("today_list.js")
    assert "check2.ticked{background:#2f6b35;border-color:#2f6b35" in craft
    load = page[page.index("async function loadList(") : page.index("let askedTimer")]
    assert load.index("await whenFinished();") < load.index("renderList()")
    # A goal's step finishes with the same motion, and a redraw is never held for long.
    assert 'if(row) finishRow(row);' in page and "setTimeout(done, 2400)" in page


@pytest.mark.frontend
def test_an_emails_hard_line_breaks_join_and_quoted_history_stays_in_the_thread() -> None:
    page, proj = _read("app.html"), _read("projects.js")
    assert "function tidyMsg(" in proj and "w = tidyMsg(words)" in proj
    assert "wrote:" in proj
    assert "function msgHtml(" in page and "Show all" in page


@pytest.mark.frontend
def test_rows_show_the_task_title_projects_once_and_the_page_stays_still() -> None:
    page, craft = _read("app.html"), _read("today_list.js")
    assert '(LIST_VIEWS.concat(["filed"]).includes(viewName)||isProjView(viewName))&&r.title?r.title:words' in page
    # Every task in a project shows its project, on every row (the user, 2026-11-14).
    assert "ditto" not in page and "ditto" not in craft
    assert "function keepPlace(" in page and "function renderList(){ keepPlace(drawList); }" in page
    # "+ next step" on the priorities card fades in at a fixed right margin; nothing moves.
    assert ".goals li:hover .gadd,.gadd:focus{opacity:1}" in page and "display:none;white-space:nowrap}" not in page
