"""Opening Today after a while starts on the Inbox (the user, 2026-11-16); coming back within half
an hour still goes back to where the user was (the user, 2026-11-15)."""

from pathlib import Path

import pytest

APP = Path(__file__).parent / "src" / "today" / "assets" / "app.html"


@pytest.mark.frontend
def test_a_fresh_open_lands_on_the_inbox_and_a_quick_return_keeps_user_place() -> None:
    page = APP.read_text()
    assert "const PLACE_KEPT_MS = 30*60*1000;" in page
    assert "Date.now() - (lastPlace.at||0) < PLACE_KEPT_MS" in page
    assert 'if(freshOpen){\n  TAB = "inbox";' in page


@pytest.mark.frontend
def test_user_last_place_wins_over_a_tab_left_in_the_address_and_every_move_is_kept() -> None:
    """The user, 2026-11-16: the user left on the Today tab and it came back on Projects, the tab in the
    address the window had first opened."""
    page = APP.read_text()
    assert 'here.get("tab")===was.get("tab")' not in page
    assert "u.pathname+u.search+u.hash); savePlace();" in page
    assert "route(); savePlace(); }" in page
