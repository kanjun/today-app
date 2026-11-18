"""Today's pages tell a phone to use its own width, so they are not a shrunken desktop page."""

from pathlib import Path

import pytest
from today import runner

APP = Path(__file__).parent / "src" / "today" / "assets" / "app.html"
VIEWPORT = "width=device-width,initial-scale=1"


@pytest.mark.frontend
def test_main_page_fits_a_phone() -> None:
    page = APP.read_text()
    # The zoom limit keeps a phone from zooming into a card's 14 px boxes (the user, Nov 15).
    assert f'name="viewport" content="{VIEWPORT},maximum-scale=1"' in page
    assert "@media (max-width:600px)" in page


def test_conversation_page_fits_a_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    item = {"key": "email:x:1", "source": "email"}
    raw = {"source": "email", "subject": "Hi", "messages": [], "url": ""}
    monkeypatch.setattr(runner.store, "load", lambda: {"items": {"email:x:1": item}})
    monkeypatch.setattr(runner.store, "load_raw", lambda key: raw)
    page = runner.app.test_client().get("/raw?key=email:x:1").get_data(as_text=True)
    assert f"content='{VIEWPORT}'" in page


LIST_JS = Path(__file__).parent / "src" / "today" / "assets" / "today_list.js"


@pytest.mark.frontend
def test_phone_tabs_and_week_list_use_the_width() -> None:
    """On a phone each tab is as wide as its word, and "+ next step" sits under a priority
    instead of in a right-hand column that squeezed the user's words (the user, 2026-11-16)."""
    css = LIST_JS.read_text()
    phone = css[css.index("@media (max-width:600px)"):]
    assert ".tabs .seg .tab{flex:1 1 auto;" in phone
    assert ".goals li .gcol{padding-right:0}" in phone
    assert ".goals li .gadd{position:static;display:block;opacity:1;" in phone
