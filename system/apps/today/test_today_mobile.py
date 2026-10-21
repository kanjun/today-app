"""Today's pages tell a phone to use its own width, so they are not a shrunken desktop page."""

from pathlib import Path

import pytest
from today import runner

APP = Path(__file__).parent / "src" / "today" / "assets" / "app.html"
VIEWPORT = "width=device-width,initial-scale=1"


@pytest.mark.frontend
def test_main_page_fits_a_phone() -> None:
    page = APP.read_text()
    assert f'name="viewport" content="{VIEWPORT}"' in page
    assert "@media (max-width:600px)" in page


def test_conversation_page_fits_a_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    item = {"key": "email:x:1", "source": "email"}
    raw = {"source": "email", "subject": "Hi", "messages": [], "url": ""}
    monkeypatch.setattr(runner.store, "load", lambda: {"items": {"email:x:1": item}})
    monkeypatch.setattr(runner.store, "load_raw", lambda key: raw)
    page = runner.app.test_client().get("/raw?key=email:x:1").get_data(as_text=True)
    assert f"content='{VIEWPORT}'" in page
